#!/usr/bin/env bash
# UserPromptSubmit hook: surface wiki pages relevant to the user's message.
#
# Runs a hybrid FTS5 + (optional) vector search against the athenaeum index
# built by session-start-recall.sh. Typical runtime: <50ms (FTS5 only),
# ~400ms (vector), ~1.5s when the LLM topic extractor is enabled. The
# <50ms FTS5-only contract still holds under issue athenaeum#1120's
# hot-tier filter and push-token budget: tier filtering costs one
# UNINDEXED column read inside the SQL already being run (no new query,
# no new process), and budget enforcement costs one extra `awk` pass over
# an at-most-3-row stream already in memory — no Python startup, which
# measured 360-450ms warm/~1090ms cold on this box (see athenaeum#1120's
# seam-decision comment below) and is exactly what this shell-native
# design avoids paying on every turn. The hot-tier filter applies to BOTH
# backends, not just FTS5: the vector branch already pays the ~400ms
# Python interpreter start noted above, so the one bounded (<=3-row)
# sqlite3 lookup its own tier post-filter adds (~1-3ms, see that section)
# is immaterial against a cost already being paid on that path.
#
# Why hybrid. FTS5 phrase match rescues short proper-noun queries that
# collide in vector space ("Return Path" embeds closer to any page
# containing "path" than to a sparse entity page). Vector search
# discovers semantic neighbours with no lexical overlap ("iterative
# feedback loops" -> "Innovation Accounting"). Each backend rescues a
# class of queries the other handles poorly — the merge is load-bearing.
#
# Rendered summary + relevance-only ranking (issue athenaeum#1344). Each
# pushed bullet used to be a bare page name — "  - book-real-startup" —
# which gives the reading session nothing to decide whether an explicit
# `recall` is worth issuing. The `description` FTS5 column (populated on
# ~86% of the corpus, ~113 chars average) is now rendered alongside the
# name as "  - ${name} — ${description}", clamped to 200 characters on a
# UTF-8 character boundary and tab/newline-sanitised entirely in SQL (see
# `PM_DESC_EXPR` below) before it ever reaches bash. Selection and
# ordering are UNCHANGED — still `ORDER BY rank` (BM25) alone, still
# `LIMIT 3`, still the same hot-tier gate — this issue only widens what
# each already-selected row renders, not which rows are selected. The
# awk budget pass is now the SINGLE place the bullet is built (priced and
# emitted from the same field, not two independently-maintained copies —
# see that section for why that matters), and every bullet is JSON-escaped
# immediately before being folded into `$MATCHES`, since free-form prose
# makes the embedded-quote/backslash hazard in the final raw-into-JSON
# `printf` common rather than rare.
#
# Push telemetry (issue athenaeum#1343). This hook used to record NOTHING
# about what it pushed — the exact reason issue athenaeum#1120's
# `AND memory_tier = 'hot'` gate (below) was able to silently over-exclude
# 96.56% of the corpus for weeks with nothing watching. Every turn that
# renders at least one candidate now appends one JSONL row to the SAME
# durable ledger `athenaeum.push_metrics.record_push` writes for the
# explicit `recall` MCP path, tagged `"source":"sidecar"` so the two
# writers stay separable. Still shell/awk-only: the append costs pure
# bash string building plus at most one `shasum`/`sha256sum` subprocess —
# no Python interpreter start added to this path (see the query_hash
# section near the bottom of this file for the one subprocess it does
# spend, and the <50ms contract this is measured against above).
#
# Hot-tier filter + push-token budget (issue athenaeum#1120). Unprompted
# recall (this hook) previously queried FTS5 directly and never saw the
# `hot`-tier filter or `push_budget.tokens_per_turn` budget that issue
# athenaeum#718 / PR athenaeum#1117 built for the *prompted* (`recall` MCP tool)
# path. The tier model itself is NOT reimplemented here in shell:
# `athenaeum.memory_tiers.resolve_tier` runs once, at index-build time
# (`athenaeum.search.FTS5Backend._row_for`, schema v4), and stores its
# verdict in the `memory_tier` FTS5 column — this hook just reads that
# column, the same established pattern `audience` (athenaeum#312) and
# `type` (athenaeum#964) already use so shell/SQL can filter without
# Python. The ONLY duplicated surface is the greedy budget-accumulation
# loop and the token estimator (`athenaeum.push_metrics.estimate_tokens`
# = `max(0, len(text) // 4)`, a single arithmetic expression, faithfully
# expressed in awk as `int(length(s)/4)`). Coordinate fit is a no-op for
# this surface: this hook has a `session_id`, not a scope coordinate, so
# `scope_relation` is `None` for every candidate — neutral weight for
# all — which means `push_score` ranking degenerates exactly to
# relevance order, i.e. the FTS5 `rank` ordering this hook already uses.
# That is why this hook needs NO `push_score` reimplementation, and it is
# the load-bearing reason the shell-native seam is safe rather than a
# silent behavioural drift from the Python path.
#
# Optional LLM query-rewriting. If `athenaeum query-topics` is available,
# the raw prompt is first run through the configured LLM provider (Haiku
# via the Messages API, or Claude Code's own CLI under `llm.provider:
# claude-cli` — no ANTHROPIC_API_KEY needed either way) to extract
# substantive topics while ignoring meta-instructions ("quote verbatim",
# "don't call tools"). Falls back silently to a regex+stopword extractor
# when unavailable.
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "UserPromptSubmit": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/user-prompt-recall.sh",
#         "timeout": 5
#       }]
#     }]
#   }
#
# Requires: sqlite3, jq (ship with macOS). Python only when vector is on.

set -euo pipefail

# ── Kill switch (issue athenaeum#379) ───────────────────────────────────────────────
# Honour ~/.cache/athenaeum/disabled (+ ATHENAEUM_DISABLED). Mirrors
# athenaeum.killswitch.is_disabled("recall"): the "all" scope no-ops every
# hook; the "compile" scope leaves recall on. Costs no Python startup.
__athenaeum_recall_disabled() {
  # Normalize like killswitch._env_scope()'s `raw.strip().lower()`. `read`
  # with default IFS strips leading/trailing whitespace while preserving
  # internal (so `tr ue` stays unrecognised, matching Python). The case
  # patterns fold case explicitly: `${_val,,}` is bash 4.0+ and stock macOS
  # ships bash 3.2.57 -- see the athenaeum#1104 / athenaeum#1343 precedents
  # in user-prompt-recall.sh. Both constructs are fork-free.
  local _val=""
  read -r _val <<< "${ATHENAEUM_DISABLED:-}" || true
  case "$_val" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn] | [Aa][Ll][Ll]) return 0 ;;
    [Cc][Oo][Mm][Pp][Ii][Ll][Ee]) return 1 ;;
  esac
  local f="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}/disabled"
  [ -f "$f" ] || return 1
  grep -Eq '"scope"[[:space:]]*:[[:space:]]*"compile"|^[[:space:]]*compile[[:space:]]*$' "$f" 2>/dev/null && return 1
  return 0
}
__athenaeum_recall_disabled && exit 0

CACHE_DIR="${HOME}/.cache/athenaeum"
CONFIG_ENV="${CACHE_DIR}/config.env"
DB_FILE="${CACHE_DIR}/wiki-index.db"
VECTOR_DIR="${CACHE_DIR}/wiki-vectors"
ATHENAEUM_CLI="${ATHENAEUM_CLI:-athenaeum}"
PYTHON="${ATHENAEUM_PYTHON:-python3}"

# ── Source config ──────────────────────────────────────────────────────
# `set -a` auto-exports sourced variables so child processes (notably
# `athenaeum query-topics`) inherit them — including ANTHROPIC_API_KEY,
# for providers that need one. Without it, `source` sets vars only in
# this shell and the child would silently run without them. Under
# `llm.provider: claude-cli` no key is needed at all.
if [ -f "$CONFIG_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_ENV"
  set +a
fi
AUTO_RECALL="${AUTO_RECALL:-true}"
SEARCH_BACKEND="${SEARCH_BACKEND:-fts5}"
# Issue athenaeum#1120: env override first (mirrors
# athenaeum.config.resolve_push_token_budget's own precedence), then the
# config.env value session-start-recall.sh cached from
# `push_budget.tokens_per_turn`, then the same 1200 default the library
# falls through to. Guard against a non-numeric/<=0 value the same way
# the library does.
BUDGET="${ATHENAEUM_PUSH_TOKEN_BUDGET:-${PUSH_TOKEN_BUDGET:-1200}}"
case "$BUDGET" in
  ''|*[!0-9]*) BUDGET=1200 ;;
  0) BUDGET=1200 ;;
esac

# ── Sidecar push telemetry setup (issue athenaeum#1343) ─────────────────
# Every unprompted push this hook renders gets one JSONL row appended to
# the SAME durable ledger `athenaeum.push_metrics.record_push` writes for
# the explicit `recall` MCP path — previously this hook wrote nothing at
# all (see the issue's motivation: the `AND memory_tier = 'hot'` gate
# above shipped with no telemetry, so its 96.56% over-exclusion on the
# real corpus went undetected for weeks). Setup only; the actual append
# happens after the budget pass below, which is the only place that
# knows the *rendered* set.

# Enablement (D10): mirrors `athenaeum.config.resolve_push_metrics_enabled`'s
# precedence exactly — `ATHENAEUM_PUSH_METRICS_ENABLED` env >
# `PUSH_METRICS_ENABLED` (cached from `push_metrics.enabled` yaml by
# session-start-recall.sh, same shape as `PUSH_TOKEN_BUDGET`) > default
# on. The env layer has an asymmetry that must be reproduced exactly: an
# env var that is SET but EMPTY is FALSEY (off), while an UNSET env var
# falls through to the yaml/default layer — `${VAR:-x}` conflates those
# two cases in shell, so the "is it set at all" test below uses
# `${VAR+x}`, not `${VAR:-x}`.
PM_ENABLED=true
if [ -n "${ATHENAEUM_PUSH_METRICS_ENABLED+x}" ]; then
  case "$(printf '%s' "$ATHENAEUM_PUSH_METRICS_ENABLED" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    0 | false | no | off | "") PM_ENABLED=false ;;
    *) PM_ENABLED=true ;;
  esac
elif [ "${PUSH_METRICS_ENABLED:-true}" = "false" ]; then
  PM_ENABLED=false
fi

# Ledger path (D3): reproduces `push_metrics.durable_push_records_path`'s
# two-branch rule exactly — new (`<wiki_root>/_push_records.jsonl`) when
# it exists or the legacy cache-dir file does not, else legacy
# (`<cache_dir>/_push_records.jsonl`). Deliberately a SEPARATE resolution
# from this hook's own `$CACHE_DIR` above (which is pinned to `$HOME` and
# does not honour `ATHENAEUM_CACHE_DIR`) — the ledger must resolve to
# exactly where `push_metrics.push_records_path`/`durable_push_records_path`
# would, or a hook-written row and a Python-written row could split
# across two different files. `wiki_root` mirrors
# `session-start-recall.sh:59-60`'s identical expression
# (`mcp_server.py:423` confirms `wiki_root = knowledge_root / "wiki"`).
PM_KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
PM_WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-${PM_KNOWLEDGE_ROOT}/wiki}"
PM_CACHE_DIR="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}"
PM_LEDGER_NEW="${PM_WIKI_ROOT}/_push_records.jsonl"
PM_LEDGER_LEGACY="${PM_CACHE_DIR}/_push_records.jsonl"
if [ -f "$PM_LEDGER_NEW" ] || [ ! -f "$PM_LEDGER_LEGACY" ]; then
  PM_LEDGER_PATH="$PM_LEDGER_NEW"
else
  PM_LEDGER_PATH="$PM_LEDGER_LEGACY"
fi

# ── Push telemetry helpers (pure bash — no subprocess on the hot path) ──
#
# CONVENTION: these helpers return their result in the global `_PM_RET`
# rather than printing it, and callers read `_PM_RET` immediately. That
# is deliberate and load-bearing, not a style choice: `x=$(helper ...)`
# FORKS a subshell even when `helper` is a shell function, and this path
# calls six of them PER PUSHED ITEM (~18 forks per turn on a 3-item
# push). Measured on the fixture index, the command-substitution form
# cost ~11-29ms per turn against the <50ms contract stated in this
# file's header — the same order as the Python interpreter start this
# whole shell-native design exists to avoid. Initialized here so `set -u`
# can never see it unset.
_PM_RET=""


# `id` (AC "id is never a name-derived slug"): the FTS5 `wiki` table has
# no `uid` column, so this shell fallback derives an id from the filename
# alone, mirroring `push_metrics.opaque_push_id`'s non-uid branch. A
# compiled entity's filename is `<8-hex-uid-prefix>-<slugified-name>.md`
# (`athenaeum.models.WikiEntity.filename`) — recording only the 8-hex
# prefix keeps the name-derived slug out of the ledger entirely. A
# raw-intake filename (`<timestamp>Z-<hash>.md`) never matches that shape
# and is recorded whole (it carries no name to leak).
_pm_opaque_push_id() {
  case "$1" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-*)
      _PM_RET="${1:0:8}"
      ;;
    *)
      _PM_RET="$1"
      ;;
  esac
}

# `scope` (D5): derived from the index's `audience` column, the only
# audience representation this shell hook can see. Mirrors
# `build_push_record`'s intent through `models.audience_index_string`'s
# delimiter-anchored shape (`"|"` empty sentinel, `"|__access_open__|"`
# public marker, `"|role|role|"` roles, any combination): audience-empty
# -> "owner"; public marker alone -> "open"; roles present (with or
# without the public marker) -> sorted, comma-joined roles.
#
# NO bash arrays here — deliberately (issue athenaeum#1343 review finding).
# `#!/usr/bin/env bash` on stock macOS resolves to `/bin/bash`, GNU bash
# 3.2.57 (Apple stopped shipping newer bash over the GPLv3 relicense).
# Under bash 3.2 with `set -u`, referencing an empty array (`${#a[@]}`,
# `${a[0]}`) can raise "unbound variable" — exactly the class of bug
# athenaeum#1104 already found and fixed by removing a bash-4-only
# `mapfile` call from `scripts/public-safe-lint-gate.sh` for this same
# stock-macOS-bash reason (see CHANGELOG.md ~line 2190). The public-marker-
# only case (`|__access_open__|`, a normal public page) hits exactly that
# empty-array state here, so this is rewritten as a plain string pipeline
# through `tr`/`grep`/`sort` instead — slower by a negligible amount for
# an at-most-3-item, at-most-a-handful-of-roles input, but correct on
# every bash this hook ships to.
_pm_scope_from_audience() {
  local aud="${1:-|}"
  local trimmed="${aud#|}"
  trimmed="${trimmed%|}"
  local had_public=false joined="" part rest="$trimmed"
  # Pure parameter expansion: no arrays (bash 3.2, see above) and NO
  # subprocess at all. Issue athenaeum#1343's "shell/awk plus at most one
  # shasum/sha256sum subprocess" contract is a PER-TURN budget, and this
  # helper runs once PER PUSHED ITEM — a `tr | grep | sort` pipeline here
  # would fork ~7 processes per item (~20 per turn) against a <50ms
  # contract, which is exactly the cost this shell-native design exists
  # to avoid.
  #
  # No sort is needed: `models.delimited_index_string` already emits its
  # tokens `sorted({v for v in values if v})` (models.py:1158), so the
  # tokens arrive sorted, deduped and empty-free — the same ordering
  # `build_push_record`'s own `",".join(sorted(roles))` produces. Walking
  # them in index order therefore reproduces that join exactly.
  while [ -n "$rest" ]; do
    part="${rest%%|*}"
    if [ "$part" = "$rest" ]; then
      rest=""
    else
      rest="${rest#*|}"
    fi
    if [ "$part" = "__access_open__" ]; then
      had_public=true
    elif [ -n "$part" ]; then
      if [ -n "$joined" ]; then
        joined="${joined},${part}"
      else
        joined="$part"
      fi
    fi
  done
  if [ -n "$joined" ]; then
    _PM_RET="$joined"
  elif [ "$had_public" = true ]; then
    _PM_RET=open
  else
    _PM_RET=owner
  fi
}

# Numeric guard (issue athenaeum#1343 review findings, defects 1 & 3).
# Used before ANY value derived from a parsed `$RESULTS` row is either
# used in bash arithmetic or interpolated unquoted into JSON. Two
# distinct hazards this closes:
#   (1) `read -r fname name rank audience mtier backend cost` shifts
#       fields if an indexed column (e.g. `name`) ever contains a literal
#       tab -- `read` dumps all overflow into the LAST variable, so
#       `cost` can become a compound non-numeric string. Arithmetic on
#       that (`$(( total + cost ))`) makes bash's arithmetic evaluator
#       treat a leading identifier-shaped token as a VARIABLE NAME, and
#       under `set -u` an unbound one aborts the whole script — this is
#       NOT suppressed by wrapping the caller in `|| true` (verified:
#       `set -u`'s unbound-variable abort fires even when the failing
#       command sits inside a function invoked as `f || true`; only
#       ordinary non-zero exit statuses are suppressed that way).
#   (2) `relevance` must never be interpolated as a bare, unquoted,
#       possibly-empty/non-numeric token — `"relevance":,` is malformed
#       JSON `read_push_records` cannot parse.
# `[[ =~ ]]` extended-regex matching is available since bash 3.0, so this
# is bash-3.2-safe too.
_pm_is_number() {
  [[ "$1" =~ ^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$ ]]
}

# Shift one TAB-delimited field off `$_PM_ROW_REST` into `$_PM_RET`.
#
# Why this exists rather than `IFS=$'\t' read -r a b c ...`: bash treats
# TAB as IFS *whitespace* regardless of what IFS is set to, so a run of
# delimiters collapses and an EMPTY field is silently dropped, shifting
# every later field left. Verified directly:
#
#   IFS=$'\t' read -r a b c <<< $'a\t\tc'   -> a=a  b=c  c=      (WRONG)
#   awk -F'\t' on the same line               -> NF=3 $2="" $3=c  (right)
#
# That is not hypothetical here: `description` is absent on ~14% of the
# corpus and `memory_tier` is the empty literal on a legacy DB, so the
# naive form recorded `token_cost: 0` for every description-less page —
# the ledger's own cost accounting reading as zero, which is precisely
# the "reads as zero forever" hazard issue athenaeum#1343 exists to
# close. Parameter expansion has no such special-casing, costs no fork,
# and works on bash 3.2 (see the athenaeum#1104 precedent above).
_pm_shift_field() {
  case "$_PM_ROW_REST" in
    *"$_PM_TAB"*)
      _PM_RET="${_PM_ROW_REST%%"$_PM_TAB"*}"
      _PM_ROW_REST="${_PM_ROW_REST#*"$_PM_TAB"}"
      ;;
    *)
      _PM_RET="$_PM_ROW_REST"
      _PM_ROW_REST=""
      ;;
  esac
}
_PM_TAB=$'\t'
_PM_ROW_REST=""

# Minimal RFC 8259 string escaper (D11 — no jq/python on this path).
# Escapes backslash, double-quote, and C0 control characters as
# `\uXXXX`. The values passed through this are index-derived (filenames,
# audience tokens) rather than arbitrary user text, but escaping
# unconditionally is cheap (pure bash, no subprocess) and removes the
# question entirely.
_pm_json_escape() {
  local s="$1" out="" c i len ord hex
  # Fast path (issue athenaeum#1344): the loop below walks the string one
  # character at a time in pure bash, which is fine for a page `name` but
  # is now also asked to walk a description clamped at 200 chars, three
  # times per turn. The overwhelming majority of those strings contain
  # nothing that needs escaping at all, and a single glob test settles
  # that in one operation instead of 200. Anything that DOES need work
  # still falls through to the exact same loop, so this is a short
  # circuit, not a second implementation.
  case "$s" in
    *[\\\"]* | *[[:cntrl:]]*) : ;;
    *) _PM_RET="$s"; return ;;
  esac
  len=${#s}
  for (( i = 0; i < len; i++ )); do
    c="${s:i:1}"
    case "$c" in
      '\') out+='\\' ;;
      '"') out+='\"' ;;
      *)
        printf -v ord '%d' "'$c"
        if [ "$ord" -lt 32 ]; then
          printf -v hex '%04x' "$ord"
          out+="\\u${hex}"
        else
          out+="$c"
        fi
        ;;
    esac
  done
  _PM_RET="$out"
}

# Builds and appends the ONE telemetry row for this turn (issue
# athenaeum#1343 review finding, defect 1). Deliberately a SEPARATE pass
# over `$RESULTS` from the render loop below, invoked exactly once as
# `_pm_record_push || true` — never inlined into the render loop.
#
# Why this matters under `set -euo pipefail`: `f || true` DOES suppress
# an ordinary non-zero exit from anything inside `f` (verified: a `false`
# inside a function called as `f || true` does not abort the script).
# But it does NOT suppress a `set -u` unbound-variable abort, which fires
# immediately regardless of how the failing command's exit status would
# otherwise be tested (verified separately). A tab embedded in an
# indexed `name` column shifts the `read -r fname name rank audience
# mtier backend description bullet cost` fields — `read` dumps all
# overflow into the LAST variable, so `cost` can become a compound
# non-numeric string, and bash arithmetic on it (`$(( total + cost ))`)
# tries to resolve a leading-identifier-shaped token as a variable name,
# which is exactly the unbound-variable abort `|| true` cannot catch. So
# the render loop below is kept to ONLY what it did before this issue
# (build MATCHES, write SEEN_FILE) — it can never be broken by this
# function — and this function additionally guards every value it puts
# in arithmetic or unquoted JSON with `_pm_is_number` first, so even a
# shifted/garbled row degrades to a safe default (cost 0, relevance null)
# instead of crashing. `description`/`bullet` (issue athenaeum#1344, fields
# 7-8) are read into named locals purely to keep `cost` (field 9) in the
# LAST position this function's arithmetic guard expects — this function
# never uses either value itself, since `tier`/`scope`/`relevance` etc.
# don't derive from the rendered bullet text.
_pm_record_push() {
  [ "$PM_ENABLED" = true ] || return 0

  local fname name rank audience mtier backend description bullet cost
  local _pm_id _pm_scope _pm_id_esc _pm_scope_esc _pm_mtier_esc _pm_backend_esc
  local _pm_cost _pm_relevance _pm_item
  local pm_items_json="" pm_total_cost=0 pm_item_count=0

  while IFS= read -r _PM_ROW_REST; do
    [ -n "$_PM_ROW_REST" ] || continue
    # Nine TAB-delimited fields, split WITHOUT `read`'s IFS-whitespace
    # field-squashing — see `_pm_shift_field` above for why that matters
    # and for the verified counter-example.
    _pm_shift_field; fname="$_PM_RET"
    _pm_shift_field; name="$_PM_RET"
    _pm_shift_field; rank="$_PM_RET"
    _pm_shift_field; audience="$_PM_RET"
    _pm_shift_field; mtier="$_PM_RET"
    _pm_shift_field; backend="$_PM_RET"
    _pm_shift_field; description="$_PM_RET"
    _pm_shift_field; bullet="$_PM_RET"
    _pm_shift_field; cost="$_PM_RET"
    [ -n "$fname" ] || continue

    if _pm_is_number "$cost"; then
      _pm_cost="$cost"
    else
      _pm_cost=0
    fi

    if [ "$backend" = "vector" ]; then
      _pm_relevance="null"
    elif _pm_is_number "$rank"; then
      _pm_relevance="$rank"
    else
      _pm_relevance="null"
    fi

    _pm_opaque_push_id "$fname"; _pm_id="$_PM_RET"
    _pm_scope_from_audience "$audience"; _pm_scope="$_PM_RET"
    _pm_json_escape "$_pm_id"; _pm_id_esc="$_PM_RET"
    _pm_json_escape "$_pm_scope"; _pm_scope_esc="$_PM_RET"
    _pm_json_escape "$mtier"; _pm_mtier_esc="$_PM_RET"
    # `backend` is escaped too (not just interpolated raw): under normal
    # operation it is always the literal "fts5"/"vector" this script
    # itself wrote, but a shifted/garbled row (the tab-in-`name` case
    # above) could otherwise carry a stray quote/backslash into it.
    _pm_json_escape "$backend"; _pm_backend_esc="$_PM_RET"

    _pm_item="{\"id\":\"${_pm_id_esc}\",\"tier\":\"internal\",\"scope\":\"${_pm_scope_esc}\",\"token_cost\":${_pm_cost},\"relevance\":${_pm_relevance},\"backend\":\"${_pm_backend_esc}\",\"memory_tier\":\"${_pm_mtier_esc}\"}"
    if [ -n "$pm_items_json" ]; then
      pm_items_json="${pm_items_json},${_pm_item}"
    else
      pm_items_json="$_pm_item"
    fi
    pm_total_cost=$(( pm_total_cost + _pm_cost ))
    pm_item_count=$(( pm_item_count + 1 ))
  done <<< "$RESULTS"

  # A turn that pushes nothing never reaches here in practice (`[ -n
  # "$RESULTS" ] || exit 0` runs before this function is called), but the
  # guard is kept so this function is safe to call unconditionally.
  [ "$pm_item_count" -gt 0 ] || return 0

  # query_hash (D2): sha256 of the RAW PROMPT text, truncated to 16 hex
  # chars — the SAME digest `push_metrics._query_hash` computes. The
  # prompt text itself is NEVER written. This is the ONE shasum/sha256sum
  # subprocess this path spends (the issue's "shell/awk plus at most one
  # shasum/sha256sum subprocess" contract) — everything else above is
  # pure bash/awk or a bounded sqlite3 lookup already paid for by the
  # recall query itself.
  local pm_query_hash pm_ts pm_session_id_esc pm_record
  if command -v sha256sum >/dev/null 2>&1; then
    pm_query_hash=$(printf '%s' "$PROMPT" | sha256sum); pm_query_hash="${pm_query_hash:0:16}"
  elif command -v shasum >/dev/null 2>&1; then
    pm_query_hash=$(printf '%s' "$PROMPT" | shasum -a 256); pm_query_hash="${pm_query_hash:0:16}"
  else
    pm_query_hash=""
  fi

  # ts (D9): second-resolution, Z-suffixed — `_parse_ts`'s
  # `datetime.fromisoformat(raw.replace("Z", "+00:00"))` accepts this
  # exactly. BSD/macOS `date` has no `%N`, and this hook is macOS-first,
  # so this deliberately does not attempt microsecond resolution the way
  # `push_metrics._now_iso()` does.
  # `printf '%(fmt)T'` is a bash 4.2+ BUILTIN — no fork at all. Stock
  # macOS ships bash 3.2.57 (see the athenaeum#1104 precedent noted
  # above), which lacks it, so fall back to `date` there. On bash 4.2+
  # this path therefore spends exactly ONE subprocess in total (the
  # sha256 above), which is the issue athenaeum#1343 contract; on bash
  # 3.2 it spends two, because bash 3.2 has no way to read the wall
  # clock without one. TZ=UTC makes the builtin's output UTC, matching
  # `date -u` and `push_metrics._now_iso()`'s timezone-aware stamp.
  if ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 2))); then
    local _pm_oldtz="${TZ-__unset__}"
    TZ=UTC printf -v pm_ts '%(%Y-%m-%dT%H:%M:%SZ)T' -1
    if [ "$_pm_oldtz" = "__unset__" ]; then unset TZ; else TZ="$_pm_oldtz"; fi
  else
    pm_ts=$(TZ=UTC date -u +%Y-%m-%dT%H:%M:%SZ)
  fi
  _pm_json_escape "$SESSION_ID"; pm_session_id_esc="$_PM_RET"
  pm_record="{\"v\":1,\"session_id\":\"${pm_session_id_esc}\",\"ts\":\"${pm_ts}\",\"query_hash\":\"${pm_query_hash}\",\"backend\":\"${SEARCH_BACKEND}\",\"items\":[${pm_items_json}],\"pushed_count\":${pm_item_count},\"token_cost\":${pm_total_cost},\"token_cost_estimated\":true,\"source\":\"sidecar\"}"

  # Best-effort, single O_APPEND write of one complete line — never
  # breaks or delays the push. `>>` opens with O_APPEND and `printf`
  # issues one write(2) for a line this short (well under PIPE_BUF), so
  # two concurrent hook runs can never interleave a partial line,
  # matching `store.append_line_durable`'s atomicity guarantee (this
  # path skips its `fsync`: a per-turn fsync would add a syscall this hot
  # path cannot afford, and a torn TRAILING line on a crash is already
  # the tolerated failure mode every ledger reader in this codebase
  # accepts). Each command below has its OWN `|| true` — belt-and-braces
  # alongside the caller's `_pm_record_push || true`, since an unbound-
  # variable abort (unlike an ordinary failure) is not caught by the
  # caller's guard, and every value reaching this point has already been
  # through the numeric guards above.
  if [ "$PM_LEDGER_PATH" = "$PM_LEDGER_NEW" ]; then
    mkdir -p "$PM_WIKI_ROOT" 2>/dev/null || true
  else
    mkdir -p "$PM_CACHE_DIR" 2>/dev/null || true
  fi
  printf '%s\n' "$pm_record" >> "$PM_LEDGER_PATH" 2>/dev/null || true
  return 0
}

[ "$AUTO_RECALL" = "true" ] || exit 0

# Bail only when BOTH backends are unavailable. Hybrid merge tolerates
# one being absent.
if [ ! -f "$DB_FILE" ] && [ ! -d "$VECTOR_DIR" ]; then
  exit 0
fi

# ── Parse stdin ─────────────────────────────────────────────────────────
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null)

if [ -z "$PROMPT" ] || [ ${#PROMPT} -lt 8 ]; then
  exit 0
fi

# ── Extract search terms ────────────────────────────────────────────────
TERMS=""
# Do NOT gate this on ANTHROPIC_API_KEY (athenaeum#792). `query-topics`
# routes through build_llm_client, which honors `llm.provider` — under
# `claude-cli` it authenticates via the ambient Claude Code login and
# needs no API key at all. Any provider/config combination that can't
# build a client already returns empty here, which falls through to the
# regex fallback below; a shell-side key check adds nothing that failure
# path doesn't already do, and it silently disabled the extractor for
# every claude-cli user.
if command -v "$ATHENAEUM_CLI" >/dev/null 2>&1; then
  TERMS=$("$ATHENAEUM_CLI" query-topics "$PROMPT" --timeout 3 2>/dev/null || echo "")
fi

# Sanitize to alphanum tokens before query-building. Anything that flows
# into FTS_QUERY below ends up inside a single-quoted SQL literal passed
# to `sqlite3 ... "WHERE wiki MATCH '${FTS_QUERY}'"`, so a stray ' in an
# LLM-returned topic (e.g. "Tristan's project") would break out of the
# literal and inject SQL. Alphanum-only matches the fallback extractor's
# surface and keeps FTS5 happy.
if [ -n "$TERMS" ]; then
  TERMS=$(echo "$TERMS" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '\n' | grep -E '.{3,}' | sort -u | head -8)
fi

if [ -z "$TERMS" ]; then
  # Read the canonical stopword list cached at SessionStart. Single
  # source of truth with athenaeum.search.STOPWORDS (issue athenaeum#46); the
  # file is rewritten on every session start so list updates pick up
  # automatically. If the cache is missing (e.g. SessionStart hook
  # didn't run), fall back to a minimal baked-in list so the hook
  # still works degradedly rather than returning zero terms.
  if [ -s "${CACHE_DIR}/stopwords.txt" ]; then
    STOPWORDS=$(tr '\n' '|' < "${CACHE_DIR}/stopwords.txt" | sed 's/|$//')
  else
    STOPWORDS="the|and|for|are|but|not|you|all|can|had|was|one|our|out|has|from|with|this|that|they|will|have|been|what|when|which|while|the"
  fi
  TERMS=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '\n' | grep -vE "^(${STOPWORDS})$" | grep -E '.{3,}' | sort -u | head -8)
fi

[ -n "$TERMS" ] || exit 0

# FTS5 query: "term1" OR "term2" OR ... (lowercased, quoted for phrases).
FTS_QUERY=$(echo "$TERMS" | tr '[:upper:]' '[:lower:]' | sed 's/.*/"&"/' | tr '\n' ' ' | sed 's/ *$//' | sed 's/" "/\" OR \"/g')
# Vector query: topics concatenated (no meta-drift from full prompt).
VECTOR_QUERY=$(echo "$TERMS" | tr '\n' ' ' | sed 's/ *$//')
[ -n "$VECTOR_QUERY" ] || VECTOR_QUERY="$PROMPT"

# ── Session dedup ───────────────────────────────────────────────────────
SEEN_FILE="/tmp/knowledge-seen-${SESSION_ID}"
touch "$SEEN_FILE"
EXCLUDE=""
if [ -s "$SEEN_FILE" ]; then
  EXCLUDE=$(while read -r fn; do printf "AND filename != '%s' " "$fn"; done < "$SEEN_FILE")
fi

# ── Query backends ──────────────────────────────────────────────────────
# Issue athenaeum#1120 — legacy-DB safety, probed ONCE and shared by BOTH
# the FTS5 query below and the vector-hit tier post-filter further down
# (a warm/cold page reached via the vector backend must be held to the
# SAME hot-tier bar as an FTS5 hit — see that section for why). A DB
# built by an older athenaeum predates the `memory_tier` column (schema
# v4, see athenaeum.search.FTS5Backend._SCHEMA_VERSION's comment).
# Selecting a column that doesn't exist raises `sqlite3.OperationalError`,
# which this hook's own `2>/dev/null || echo ""` swallow would otherwise
# turn into a ZERO recall for every turn until the index happens to be
# rebuilt — exactly the failure class that _SCHEMA_VERSION comment warns
# about. Probe for the column first and fall back to the
# pre-athenaeum#1120 unfiltered query when it's absent, so an un-rebuilt
# index degrades BOTH branches consistently to today's (unfiltered)
# behaviour instead of one branch filtering and the other not.
HAS_TIER_COLUMN=false
if [ -f "$DB_FILE" ] && sqlite3 "$DB_FILE" "PRAGMA table_info(wiki);" 2>/dev/null | grep -q '|memory_tier|'; then
  HAS_TIER_COLUMN=true
fi

# Issue athenaeum#1344 — same legacy-DB hazard as HAS_TIER_COLUMN above,
# same probe shape: a DB built before `description` existed would raise
# `sqlite3.OperationalError` on a SELECT that names it, which this hook's
# own `2>/dev/null || echo ""` would otherwise swallow into a silent ZERO
# recall for the whole turn. Probed once and shared by both the FTS5
# query below and the vector-hit metadata lookup further down, exactly
# like HAS_TIER_COLUMN.
#
# Unlike HAS_TIER_COLUMN, this does NOT need a second whole-query branch:
# tier changes the WHERE clause (a column that doesn't exist can't be
# filtered on), but description only changes one SELECT-list expression,
# so gating just that expression through `DESC_COL` below degrades both
# the tier and no-tier branches to the SAME name-only render together,
# rather than duplicating all four combinations of (tier x description)
# column presence into four near-identical queries.
HAS_DESCRIPTION_COLUMN=false
if [ -f "$DB_FILE" ] && sqlite3 "$DB_FILE" "PRAGMA table_info(wiki);" 2>/dev/null | grep -q '|description|'; then
  HAS_DESCRIPTION_COLUMN=true
fi

# Issue athenaeum#1344 — the ONE SQL expression that renders `description`
# for the bullet, reused verbatim everywhere a row is read (both FTS5
# branches below, and both vector-metadata branches further down) so the
# render can never disagree with itself between backends or between the
# tier/no-tier branches (AC "the vector branch renders identically").
# Two things happen here, deliberately in SQL rather than in awk/bash:
#   1. `replace(...)` collapses any embedded tab/newline/CR in the
#      description to a single space BEFORE the value ever reaches the
#      tab-separated pipeline below — protects the `awk -F'\t'`/`read
#      -r ... IFS=$'\t'` field positions downstream (AC "does not shift
#      awk field positions"), the same hazard `name` already has (see
#      the tab-in-name regression test), now closed for `description` at
#      the source instead of merely tolerated.
#   2. `substr(..., 1, 200)` clamps to the 200-char authoring-convention
#      bound the issue recommends. Done in SQL, not bash, because
#      SQLite's `substr`/`length` are UTF-8-CHARACTER-aware (counts
#      codepoints, not bytes) for TEXT values — verified directly against
#      this box's sqlite3 CLI with a 250-character accented string:
#      `substr` returns exactly 200 characters, never a byte-split
#      trailing multi-byte sequence. This closes the "clamp before
#      sanitise" hazard the issue flags (the corpus contains accented
#      names): the clamp must happen on RAW text, character-safe, before
#      `_pm_json_escape` ever runs on it below — escaping first and then
#      byte-slicing at 200 could otherwise cut a `\uXXXX` escape or a
#      multi-byte UTF-8 sequence in half.
# When the column doesn't exist (`HAS_DESCRIPTION_COLUMN=false`), this is
# just the SQL literal `''` — no column reference at all, so the query is
# valid against a pre-athenaeum#1344 index and every row's 7th field is
# simply empty, which the budget pass below already renders as a
# name-only bullet (AC "empty/absent description renders exactly as
# today").
PM_DESC_EXPR="substr(replace(replace(replace(description, char(9), ' '), char(10), ' '), char(13), ' '), 1, 200)"
DESC_COL="''"
if [ "$HAS_DESCRIPTION_COLUMN" = true ]; then
  DESC_COL="$PM_DESC_EXPR"
fi

FTS_RESULTS=""
if [ -f "$DB_FILE" ]; then
  if [ "$HAS_TIER_COLUMN" = true ]; then
    # Issue athenaeum#1343 (Plan step 3): `audience` and `memory_tier` added
    # to the SELECT list purely to feed the telemetry row below — a wider
    # row from the SAME query, no new query. The trailing literal
    # `'fts5'` tags each row with the backend it came from, so the merge
    # step downstream never needs to guess. Issue athenaeum#1344 widens
    # this SAME query once more with `${DESC_COL}` (see above) — still one
    # query, no second lookup, no new process. Ordering stays `ORDER BY
    # rank` alone: no tier or description term participates in selection
    # or ordering (AC "ordering and selection are by relevance alone").
    FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename, name, rank, audience, memory_tier, 'fts5', ${DESC_COL}
      FROM wiki
      WHERE wiki MATCH '${FTS_QUERY}'
      AND memory_tier = 'hot'
      ${EXCLUDE}
      ORDER BY rank
      LIMIT 3;
    " 2>/dev/null || echo "")
  else
    # Legacy DB (no memory_tier column): `audience` predates memory_tier
    # (issue athenaeum#312 vs. schema v4) and is safe to select
    # unconditionally; memory_tier is recorded as the literal empty
    # string per item (D8 — the column doesn't exist on this DB).
    FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename, name, rank, audience, '', 'fts5', ${DESC_COL}
      FROM wiki
      WHERE wiki MATCH '${FTS_QUERY}'
      ${EXCLUDE}
      ORDER BY rank
      LIMIT 3;
    " 2>/dev/null || echo "")
  fi
fi

VECTOR_RESULTS=""
VECTOR_ERR=""
if [ "$SEARCH_BACKEND" = "vector" ] && [ -d "$VECTOR_DIR" ]; then
  # Failures here are non-fatal — the hook still surfaces FTS5 results —
  # but we capture stderr to $VECTOR_ERR so ATHENAEUM_HOOK_DEBUG=1 can
  # surface the reason. Most common cause: chromadb import missing in
  # the python3 on PATH (see `pip install athenaeum[vector]`).
  _vector_tmp=$(mktemp -t athenaeum-vec-XXXXXX)
  VECTOR_RESULTS=$("$PYTHON" -c "
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    spec = importlib.util.spec_from_file_location('athenaeum.search', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    query_vector_index = mod.query_vector_index
else:
    from athenaeum.search import query_vector_index
seen = set()
seen_file = sys.argv[2]
if os.path.isfile(seen_file):
    with open(seen_file) as f:
        seen = set(l.strip() for l in f)
for fname, name, score in query_vector_index(sys.argv[1], os.path.expanduser('~/.cache/athenaeum'), n=3, exclude=seen):
    print(f'{fname}\t{name}\t{score}')
" "$VECTOR_QUERY" "$SEEN_FILE" 2>"$_vector_tmp" || true)
  VECTOR_ERR=$(cat "$_vector_tmp" 2>/dev/null || echo "")
  rm -f "$_vector_tmp"
  if [ -n "$VECTOR_ERR" ] && [ "${ATHENAEUM_HOOK_DEBUG:-0}" = "1" ]; then
    echo "athenaeum recall: vector backend failed: ${VECTOR_ERR}" >&2
  fi
fi

# ── Post-filter vector hits to the SAME hot-tier verdict (issue athenaeum#1120) ──
# The FTS5 branch above enforces `memory_tier = 'hot'` INSIDE its own SQL
# WHERE clause; without an equivalent check here, a warm/cold page
# surfaced by the vector backend would bypass the filter entirely under
# `SEARCH_BACKEND=vector` — a dial that looks enforced but silently isn't
# on that deployed path. No second tier model is introduced: this is a
# second, bounded lookup into the SAME index-carried verdict the FTS5
# query already reads, restricted to the (at most 3) filenames the vector
# backend actually returned — never an unbounded `WHERE memory_tier =
# 'hot'` scan, which against the real ~23k-page corpus would pull the
# whole hot set on every turn for no reason. Skipped when the DB predates
# the `memory_tier` column (`HAS_TIER_COLUMN=false`, legacy-DB safety
# above), so both branches degrade to the SAME pre-athenaeum#1120
# unfiltered behaviour together rather than one filtering and the other
# not. Cost: the vector branch already pays a Python interpreter start
# (~400ms, see the header latency note); one more bounded (<=3-row)
# sqlite3 lookup (~1-3ms) does not touch that contract.
#
# Issue athenaeum#1343: this same bounded lookup is widened (not a new
# query) to also carry `audience` and `memory_tier` through for each
# surviving vector hit into `VECTOR_META` (a `filename\taudience\tmemory_tier`
# map), so the telemetry row built below can record `scope` (D5) and
# `memory_tier` (D8) for a vector-sourced item exactly as it does for an
# FTS5-sourced one.
#
# Issue athenaeum#1344: widened once more (still the SAME bounded lookup,
# still no new query) to also carry `${DESC_COL}` — the identical
# clamped/sanitised SQL expression the FTS5 branch above uses — so a
# vector-sourced hit's description comes from the SAME index row an
# FTS5-sourced hit's would, and the two backends can never render
# differently for the same page (AC "the vector branch renders
# identically").
VECTOR_META=""
if [ -f "$DB_FILE" ] && [ -n "$VECTOR_RESULTS" ]; then
  _vector_filenames=$(printf '%s\n' "$VECTOR_RESULTS" | awk -F'\t' 'NF >= 1 && $1 != "" { print $1 }')
  _vector_in_list=""
  if [ -n "$_vector_filenames" ]; then
    # Filenames come from the index, not user input, but are interpolated
    # into SQL exactly like FTS_QUERY is above — so they get the same
    # escaping discipline: double any embedded single quote (SQL's own
    # literal-escape convention), matching the sanitizer comment on
    # FTS_QUERY's construction.
    while IFS= read -r _fn; do
      [ -n "$_fn" ] || continue
      _fn_escaped=$(printf '%s' "$_fn" | sed "s/'/''/g")
      if [ -n "$_vector_in_list" ]; then
        _vector_in_list="${_vector_in_list},'${_fn_escaped}'"
      else
        _vector_in_list="'${_fn_escaped}'"
      fi
    done <<< "$_vector_filenames"
  fi

  if [ -n "$_vector_in_list" ]; then
    if [ "$HAS_TIER_COLUMN" = true ]; then
      # `VECTOR_META` is only populated for filenames that ARE
      # `memory_tier = 'hot'` — it is therefore simultaneously the
      # audience/tier lookup AND the authoritative "kept" set for the
      # hot-tier post-filter below.
      VECTOR_META=$(sqlite3 -separator $'\t' "$DB_FILE" "
        SELECT filename, audience, memory_tier, ${DESC_COL} FROM wiki
        WHERE filename IN (${_vector_in_list})
        AND memory_tier = 'hot';
      " 2>/dev/null || echo "")
      _hot_vector_filenames=$(printf '%s\n' "$VECTOR_META" | awk -F'\t' 'NF >= 1 && $1 != "" { print $1 }')
      VECTOR_RESULTS=$(printf '%s\n' "$VECTOR_RESULTS" | awk -F'\t' -v hot="$_hot_vector_filenames" '
        BEGIN {
          n = split(hot, arr, "\n")
          for (i = 1; i <= n; i++) if (arr[i] != "") keep[arr[i]] = 1
        }
        NF >= 1 && ($1 in keep)
      ')
    else
      # Legacy DB (no memory_tier column): the hot-tier gate is skipped
      # on BOTH branches (see the FTS5 probe above), so no filtering
      # happens here either. `audience` predates memory_tier (issue
      # athenaeum#312 vs. schema v4) and is safe to select
      # unconditionally; memory_tier per item is recorded as "" (D8 —
      # the column doesn't exist on this DB, nothing truthful to carry).
      VECTOR_META=$(sqlite3 -separator $'\t' "$DB_FILE" "
        SELECT filename, audience, '', ${DESC_COL} FROM wiki
        WHERE filename IN (${_vector_in_list});
      " 2>/dev/null || echo "")
    fi
  fi
fi

# Normalize VECTOR_RESULTS (filename, name, score) to the SAME 7-field
# shape the FTS5 branch's widened SELECT already produces (filename,
# name, rank-or-empty, audience, memory_tier, backend, description),
# joining in `VECTOR_META` by filename. `score` is a vector-similarity
# score, NOT a BM25 rank — recording it as `relevance` would silently mix
# two incomparable scales, so the rank/relevance field is left EMPTY
# here; the ledger writer below maps `backend == "vector"` to a JSON
# `null` relevance instead (D7). Normalizing here means the merge step
# downstream never needs to know which backend a row came from. Issue
# athenaeum#1344 adds `description`, resolved from the SAME `VECTOR_META`
# lookup above (already clamped/sanitised in SQL) — a filename with no
# `VECTOR_META` row (shouldn't happen: every surviving vector hit was
# looked up above) degrades to an empty description, i.e. a name-only
# bullet, same as the FTS5 branch's own degrade path.
if [ -n "$VECTOR_RESULTS" ]; then
  VECTOR_RESULTS=$(printf '%s\n' "$VECTOR_RESULTS" | awk -F'\t' -v meta="$VECTOR_META" '
    BEGIN {
      m = split(meta, marr, "\n")
      for (i = 1; i <= m; i++) {
        if (marr[i] == "") continue
        split(marr[i], f, "\t")
        aud[f[1]] = f[2]
        tier[f[1]] = f[3]
        desc[f[1]] = f[4]
      }
    }
    NF >= 2 && $1 != "" {
      a = ($1 in aud) ? aud[$1] : "|"
      t = ($1 in tier) ? tier[$1] : ""
      d = ($1 in desc) ? desc[$1] : ""
      printf "%s\t%s\t\t%s\t%s\tvector\t%s\n", $1, $2, a, t, d
    }
  ')
fi

# Merge: FTS5 first (lexical precision), then vector, dedupe, cap 3. Rows
# are 7 fields wide now (issue athenaeum#1344 added `description` as the
# 7th — see the SELECTs above); `NF >= 2` only ever checked that a row
# has at least a filename and a name, so it needed no change.
RESULTS=$(printf '%s\n%s\n' "$FTS_RESULTS" "$VECTOR_RESULTS" \
  | awk -F'\t' 'NF >= 2 && $1 != "" && !seen[$1]++' \
  | head -3)

# ── Enforce the push-token budget (issue athenaeum#1120) ────────────────
# Mirrors athenaeum.memory_tiers.select_for_push's greedy-pack behaviour
# over the merged, deduped, rank-ordered candidates above: a candidate is
# included and its token cost added to the running total ONLY if doing so
# keeps the total <= budget. A candidate that would exceed the budget is
# SKIPPED (never truncated) — later, smaller candidates are still
# considered, so the budget is packed rather than cut off at the first
# miss (see select_for_push's docstring for the reference behaviour this
# loop reproduces).
#
# What is metered: the literal text this hook actually emits. Each
# candidate's own cost is its "  - ${bullet}\n" line — the exact text
# built into MATCHES and the final payload below — sized with
# athenaeum.push_metrics.estimate_tokens's formula (`max(0, len(text) //
# 4)`), expressed here as `int(length(block) / 4)`. The wrapper preamble
# ("[Knowledge context] ... :\n") is charged ONCE up front rather than
# divided across candidates: it is emitted exactly once in the final
# payload regardless of how many bullets follow it, so a per-entry share
# would both double-count it in aggregate and require knowing the final
# candidate count before the greedy pass that determines it.
#
# Issue athenaeum#1344 (review findings 1 and 3 in the brief this issue was
# built from — "the bullet is built in two places" and "clamp before
# sanitise"): this is now the SINGLE place the rendered bullet is built,
# not just priced. `desc` (field 7) already arrived clamped to 200 chars
# on a character boundary and tab/newline-sanitised, straight from the
# `${DESC_COL}` SQL expression above — nothing left to do here but decide
# whether to append it. An empty description falls back to the bare name
# (no dangling " — " separator, AC counter-example). The bullet is
# appended as a NEW trailing field, UNESCAPED — JSON-escaping happens
# once, in the output loop below, immediately before each bullet is
# concatenated into MATCHES; escaping here (before every candidate's cost
# is known) would risk pricing and emitting two different strings if a
# future edit touched one path and not the other, exactly the drift this
# refactor exists to make structurally impossible. The two remaining
# consumers of this stream (`_pm_record_push` and the output loop) both
# read through to this same field by position, so the priced text and the
# emitted text are identical by construction.
PREAMBLE=$(printf '[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\n')
RESULTS=$(printf '%s' "$RESULTS" | awk -F'\t' -v preamble="$PREAMBLE" -v budget="$BUDGET" '
  BEGIN { total = int(length(preamble) / 4) }
  {
    name = $2
    desc = $7
    bullet = (desc != "") ? name " — " desc : name
    block = "  - " bullet "\n"
    cost = int(length(block) / 4)
    if (total + cost > budget) next
    total += cost
    # Issue athenaeum#1343/#1344: append the rendered `bullet` (8th field)
    # and this candidate'"'"'s own token cost (9th field) — the telemetry
    # row built below REUSES `cost` verbatim (per-item and, summed, in
    # aggregate) rather than recomputing the estimate a second way, and
    # the output loop below REUSES `bullet` verbatim rather than
    # re-deriving it from `name` alone.
    print $0 "\t" bullet "\t" cost
  }
')

[ -n "$RESULTS" ] || exit 0

# Issue athenaeum#1344 — narrow `$RESULTS` down to just `filename\tbullet`
# pairs BEFORE the render loop touches it, via awk (not bash `read`). This
# is not a style choice: bash's `read` treats TAB as "IFS whitespace"
# no matter what IFS is set to (`IFS=$'\t' read -r a b c <<< $'a\t\tc'`
# silently SQUASHES the empty middle field and shifts `c` into `$b` —
# verified directly against this box's bash 5.2; non-whitespace IFS
# characters like `,` do not do this, but tab is special-cased regardless
# of the IFS value). `audience` and `memory_tier` (fields 4-5 of the
# 9-field row) are BOTH genuinely empty for a legacy pre-athenaeum#1120 DB
# (see that branch's SQL above) — exactly the shape the legacy-DB test
# below feeds through this hook — and reading a `read -r` variable list
# deep enough to reach `bullet` (field 8) over that row would silently
# swallow it into an earlier field, corrupting the very text this loop
# exists to render (the render loop has none of `_pm_record_push`'s
# numeric guards to fail safe with — a shifted field here is just WRONG
# output, not a caught default). awk's own field splitting has no such
# whitespace special-casing (verified above, and already relied on by
# every OTHER awk pass in this file) — extracting just the two fields the
# render loop needs, in awk, sidesteps the hazard entirely rather than
# working around it.
MATCH_LINES=$(printf '%s\n' "$RESULTS" | awk -F'\t' '{ print $1 "\t" $8 }')

# ── Format output ───────────────────────────────────────────────────────
# Must be wrapped in hookSpecificOutput.hookEventName — Claude Code
# silently ignores a flat {"additionalContext": ...} payload.
#
# Issue athenaeum#1343 review finding (defect 1): this loop does ONLY what
# it did before that issue — build MATCHES, write SEEN_FILE — plus, as of
# issue athenaeum#1344, one JSON-escape call per bullet (see below). It
# still does no arithmetic and touches no numeric field, so it remains
# immune to the tab-shifted-field hazard `_pm_record_push`'s header
# comment describes: even a garbled row just produces a garbled (but
# non-crashing) bullet here, same tolerance the pre-athenaeum#1344 code had
# for a tab embedded in `name`. Telemetry is built and appended entirely
# separately, above/below this loop, not here.
#
# Issue athenaeum#1344: consumes the `bullet` field the budget pass above
# already rendered — name-only or "name — description", already clamped
# and tab/newline-sanitised — rather than re-deriving it from `name`
# alone (requirement: the priced text and the emitted text must be the
# SAME string, not two independently-maintained ones that can drift). It
# reads from `$MATCH_LINES` (see above), a narrowed 2-field stream, not
# `$RESULTS` directly.
#
# The raw-into-JSON hazard this closes: `$MATCHES` is interpolated RAW
# into a JSON string literal by the final `printf` below. Before this
# issue the only thing in a bullet was a page `name`, so a stray `"` or
# `\` was rare; `description` is free-form prose (quotes and backslashes
# are common), so every bullet is now run through `_pm_json_escape`
# (reused from issue athenaeum#1343, not a second escaper) before being
# concatenated. Escaping happens PER BULLET, before the literal `\n`
# separator is appended — critical, because escaping the ALREADY-JOINED
# `$MATCHES` string afterwards would double-escape that intentional
# literal `\n` (turning it into a literal backslash-n visible in the
# output instead of a real line break) as well as every earlier bullet's
# already-escaped characters.
MATCHES=""
while IFS=$'\t' read -r fname bullet; do
  _pm_json_escape "$bullet"
  MATCHES="${MATCHES}  - ${_PM_RET}\n"
  echo "$fname" >> "$SEEN_FILE"
done <<< "$MATCH_LINES"

# Sidecar push telemetry (issue athenaeum#1343): exactly one call, and the
# ONLY thing standing between a failure inside `_pm_record_push` and this
# script's `set -e` is this `|| true` — see that function's header
# comment for the `set -u` caveat it does NOT rely on `|| true` to cover.
_pm_record_push || true

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\\n%s"}}' "$MATCHES"
