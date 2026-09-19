#!/usr/bin/env bash
# UserPromptSubmit hook: surface wiki pages relevant to the user's message.
#
# Runs a hybrid FTS5 + vector search against the athenaeum index built by
# session-start-recall.sh. `SEARCH_BACKEND=vector` (hybrid vector + FTS5
# RRF fusion) is the shipped default (issue athenaeum#1825, operator ruling
# on issue athenaeum#1736) -- typical runtime ~400ms, ~1.5s when the LLM
# topic extractor is enabled. `SEARCH_BACKEND=fts5` is the opt-out fallback
# path: FTS5-only, no vector index consulted at all, typical runtime <50ms.
# That <50ms contract applies ONLY on the fts5 fallback path (no vector
# index, or an explicit `SEARCH_BACKEND=fts5`) — see athenaeum#1120's
# seam-decision comment below for why tier filtering and the push-token
# budget stayed inside that contract. It is NOT "FTS5 rows are always
# cheap to post-process": issue athenaeum#1665 corrected an earlier
# version of this file that assumed live traffic was ~100% vector (it
# is not — see that issue's relevance-floor comment below) and reasoned
# from that wrong premise that the FTS5 half never needed the same
# treatment the vector half gets. On a turn where the vector half DOES
# run, the Python interpreter start is already being paid, and the FTS5
# rows this hook already queried are passed into that SAME invocation for
# relevance-floor filtering — no second process, no new query.
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
# `AND memory_tier = 'hot'` gate (since removed — athenaeum#1345,
# athenaeum#1513) was able to silently over-exclude
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
# Push-token budget (issue athenaeum#1120). Unprompted recall (this hook)
# previously queried FTS5 directly and never saw the `hot`-tier filter or
# the `push_budget.tokens_per_turn` budget that issue athenaeum#718 / PR
# athenaeum#1117 built for the *prompted* (`recall` MCP tool) path. The
# tier half of that is now moot in both directions: issue athenaeum#1345
# removed the filter, and issue athenaeum#1514 retired the retrieval-cost
# vocabulary and its index column outright, so there is no tier model to
# reimplement in shell or to read out of a column. The ONLY duplicated
# surface is the greedy budget-accumulation loop and the token estimator
# (`athenaeum.push_metrics.estimate_tokens` = `max(0, len(text) // 4)`, a
# single arithmetic expression, faithfully expressed in awk as
# `int(length(s)/4)`). Coordinate fit and tier weighting are no longer
# part of push selection anywhere (issue athenaeum#1353 deleted the
# tier-weighted `push_score` formula and `select_for_push`, which had no
# production caller): selection is plain relevance order, budget-packed,
# everywhere it happens — i.e. the FTS5 `rank` ordering this hook already
# uses. That is why this hook needs NO push-selection-formula
# reimplementation, and it is the load-bearing reason the shell-native
# seam is safe rather than a silent behavioural drift from the Python
# path.
#
# Relevance floor (issue athenaeum#1665, gates opened by athenaeum#1492 /
# athenaeum#1571). This hook used to print every FTS5 and vector hit
# unfiltered — `recall.relevance_floor.{fts5,vector}` had no effect on it
# even when configured, because it never called `meets_relevance_floor` /
# `resolve_recall_relevance_floor` at all; a hand-rolled recall
# implementation, not a caller of the library path. A prior version of
# this comment excused leaving FTS5 unfiltered on the premise that live
# traffic was ~100% vector — WRONG: the FTS5 query runs, with an index, on
# EVERY turn regardless of `SEARCH_BACKEND`, and its rows are merged with
# the vector rows before `head -3`; a live push-ledger check found FTS5 is
# not the minority path that premise assumed (see the PR that fixed this
# for the measurement). The fix: on a turn where the vector half runs (`SEARCH_BACKEND
# =vector` and a vector index exists), the FTS5 rows this hook already
# queried via sqlite3 are passed into that SAME Python invocation and
# filtered there too, alongside the vector hits — no second process, no
# SQL-side reimplementation of the direction rule. See the FTS5 query and
# vector-invocation comments below for the mechanics. On a turn where the
# vector half does NOT run (FTS5-only deployment, or no vector index),
# the FTS5 half stays genuinely unfiltered — for THAT case, and only that
# case, adding a Python process purely to filter would break the <50ms
# FTS5-only contract this file otherwise holds to.
#
# Push-scoped vs. plain floor (issue athenaeum#1665, per
# `resolve_recall_relevance_floor`'s own `unprompted` parameter): this hook
# IS the unprompted push path (`UserPromptSubmit`), so for each backend it
# resolves `recall.relevance_floor.push.<backend>` FIRST (`unprompted=True`)
# and falls back to the plain `recall.relevance_floor.<backend>` only if
# the push-scoped key is unset — never the reverse. `resolve_recall_
# relevance_floor` itself only reads ONE of those two levels per call (its
# `unprompted` argument selects which); the fallback order across both
# calls is composed inside this hook's own Python invocation, not inside
# that library function.
#
# Failure mode: any problem resolving the floor (missing library, bad
# config, anything) degrades to `floor=None` on that backend — i.e.
# unfiltered, today's-behaviour-shaped — and prints one line to stderr
# (`athenaeum recall: relevance floor inactive: ...`), visible only under
# `ATHENAEUM_HOOK_DEBUG=1` via the same `$_vector_tmp` capture the vector
# backend's own failure diagnostic already uses. Silence-on-failure would
# make a floor regression indistinguishable from "no floor configured";
# this hook must never turn a floor problem into either a hard recall
# outage or an invisible one.
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
# Issue athenaeum#1665: same env-override/default shape
# `session-start-recall.sh` already uses for the SAME variable — an
# operator-set `KNOWLEDGE_ROOT` names where `athenaeum.yaml` (and the wiki
# it configures) actually live; falling through to `load_config()`'s own
# default (`Path.home() / "knowledge"`) instead would silently ignore that
# override wherever it differs from `$HOME/knowledge`. Used only by the
# relevance-floor config load in the vector invocation below — this
# variable is unrelated to `CACHE_DIR`/`DB_FILE`/`VECTOR_DIR` above, which
# resolve from `HOME` alone and do not vary with `KNOWLEDGE_ROOT`.
KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"

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
SEARCH_BACKEND="${SEARCH_BACKEND:-vector}"
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
# Issue athenaeum#1783: same env>yaml-cache>default precedence as BUDGET
# above, mirroring athenaeum.config.resolve_recall_cap_ceiling. The
# fallback literal (7) is pinned equal to
# athenaeum.config.RECALL_CAP_CEILING_DEFAULT by
# tests/test_recall_cap.py -- it must never be hand-typed anywhere else in
# this file, and this is the ONE place it is.
CEILING="${ATHENAEUM_RECALL_CAP_CEILING:-${RECALL_CAP_CEILING:-7}}"
case "$CEILING" in
  ''|*[!0-9]*) CEILING=7 ;;
  0) CEILING=7 ;;
esac
# The candidate-fetch window both backends widen to below -- same width as
# `athenaeum.mcp_server._HYBRID_CANDIDATE_POOL`, reused here for the same
# reason the MCP side reuses it: it needs to stay >= CEILING with room for
# the cap to have real withheld candidates to count, and matching the MCP
# side's own window keeps "how wide is a widened fetch" one answer across
# both surfaces.
WINDOW=15
if [ "$CEILING" -gt "$WINDOW" ]; then
  WINDOW="$CEILING"
fi

# ── Sidecar push telemetry setup (issue athenaeum#1343) ─────────────────
# Every unprompted push this hook renders gets one JSONL row appended to
# the SAME durable ledger `athenaeum.push_metrics.record_push` writes for
# the explicit `recall` MCP path — previously this hook wrote nothing at
# all (see the issue's motivation: the `AND memory_tier = 'hot'` gate
# shipped with no telemetry, so its 96.56% over-exclusion on the real
# corpus went undetected for weeks; the gate itself was removed by
# athenaeum#1345 / athenaeum#1513, this telemetry is what makes the mix
# shifting off it observable). Setup only; the actual append
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

# Ledger path (D3): mirrors `push_metrics.durable_push_records_path`
# exactly — ALWAYS `<cache_dir>/_push_records.jsonl`, NEVER
# `<wiki_root>/_push_records.jsonl`.
#
# Issue athenaeum#1591: this block used to reproduce that function's
# two-branch rule ("new wiki-root path when it exists or the legacy
# cache-dir file does not"). This hook is the live deployment's highest-
# frequency producer, so it is what actually drove the observed migration:
# 112 telemetry rows accrued at `~/knowledge/wiki/_push_records.jsonl` and a
# librarian run committed them into the corpus. Issue athenaeum#749's
# acceptance — push records live "outside the wiki corpus (so they never
# become claims and never enter the embedded index)" — governs, and the
# relocation athenaeum#980 AC4 applied to this one artifact is withdrawn.
#
# Still deliberately a SEPARATE resolution from this hook's own `$CACHE_DIR`
# above (which is pinned to `$HOME` and does not honour
# `ATHENAEUM_CACHE_DIR`) — the ledger must resolve to exactly where
# `push_metrics.push_records_path` would, or a hook-written row and a
# Python-written row could split across two different files.
PM_CACHE_DIR="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}"
PM_LEDGER_PATH="${PM_CACHE_DIR}/_push_records.jsonl"

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
#   (1) `read -r fname name rank audience backend cost` shifts
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
# corpus, and the vector branch emits an empty `rank` field for every
# row, so the naive form recorded `token_cost: 0` for every
# description-less page —
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

# Shared, memoized query_hash (issue athenaeum#1530). sha256 of the RAW
# PROMPT text, truncated to 16 hex chars — the SAME digest
# `push_metrics._query_hash` computes, and the ONE thing the ledger row
# below and the local topics trace further down are both keyed by. The
# prompt text itself is NEVER written anywhere. Memoized into the global
# `PM_QUERY_HASH` (idempotent — a second call is a no-op) so this spends
# at most ONE shasum/sha256sum subprocess per turn no matter how many
# callers need the value, keeping the issue athenaeum#1343 "shell/awk plus
# at most one shasum/sha256sum subprocess" contract intact even with a
# second consumer added.
PM_QUERY_HASH=""
_pm_ensure_query_hash() {
  [ -z "$PM_QUERY_HASH" ] || return 0
  if command -v sha256sum >/dev/null 2>&1; then
    PM_QUERY_HASH=$(printf '%s' "$PROMPT" | sha256sum); PM_QUERY_HASH="${PM_QUERY_HASH:0:16}"
  elif command -v shasum >/dev/null 2>&1; then
    PM_QUERY_HASH=$(printf '%s' "$PROMPT" | shasum -a 256); PM_QUERY_HASH="${PM_QUERY_HASH:0:16}"
  else
    PM_QUERY_HASH=""
  fi
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
# backend description bullet cost` fields — `read` dumps all
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
# 6-7) are read into named locals purely to keep `cost` (field 8) in the
# LAST position this function's arithmetic guard expects — this function
# never uses either value itself, since `tier`/`scope`/`relevance` etc.
# don't derive from the rendered bullet text.
_pm_record_push() {
  [ "$PM_ENABLED" = true ] || return 0

  local fname name rank audience backend description entity_type bullet cost
  local _pm_id _pm_scope _pm_id_esc _pm_scope_esc _pm_backend_esc
  local _pm_cost _pm_relevance _pm_item
  local pm_items_json="" pm_total_cost=0 pm_item_count=0

  while IFS= read -r _PM_ROW_REST; do
    [ -n "$_PM_ROW_REST" ] || continue
    # Nine TAB-delimited fields (issue athenaeum#1783 inserted `type` as a
    # new field 7, shifting `bullet`/`cost` from 7/8 to 8/9), split
    # WITHOUT `read`'s IFS-whitespace field-squashing — see
    # `_pm_shift_field` above for why that matters and for the verified
    # counter-example. `entity_type` is shifted out here to keep this
    # loop's field count in step with `$RESULTS`'s real shape; it is not
    # otherwise used by this telemetry row.
    _pm_shift_field; fname="$_PM_RET"
    _pm_shift_field; name="$_PM_RET"
    _pm_shift_field; rank="$_PM_RET"
    _pm_shift_field; audience="$_PM_RET"
    _pm_shift_field; backend="$_PM_RET"
    _pm_shift_field; description="$_PM_RET"
    _pm_shift_field; entity_type="$_PM_RET"
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
    # `backend` is escaped too (not just interpolated raw): under normal
    # operation it is always the literal "fts5"/"vector" this script
    # itself wrote, but a shifted/garbled row (the tab-in-`name` case
    # above) could otherwise carry a stray quote/backslash into it.
    _pm_json_escape "$backend"; _pm_backend_esc="$_PM_RET"

    # Issue athenaeum#1514: no `memory_tier` key. The retrieval-cost
    # vocabulary is retired, and `athenaeum.push_metrics` treats an ABSENT
    # per-item `memory_tier` as "written after the retirement" — writing
    # an empty string instead would be indistinguishable from a
    # pre-retirement row whose tier genuinely could not be resolved.
    _pm_item="{\"id\":\"${_pm_id_esc}\",\"tier\":\"internal\",\"scope\":\"${_pm_scope_esc}\",\"token_cost\":${_pm_cost},\"relevance\":${_pm_relevance},\"backend\":\"${_pm_backend_esc}\"}"
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
  # recall query itself. Computed via `_pm_ensure_query_hash` (issue
  # athenaeum#1530) rather than inline, memoized into the global
  # `PM_QUERY_HASH`, so the local topics trace below can key its own row
  # by the EXACT SAME hash this ledger row carries without spending a
  # second sha256/shasum subprocess or risking the two ever diverging.
  local pm_ts pm_session_id_esc pm_record
  _pm_ensure_query_hash
  local pm_query_hash="$PM_QUERY_HASH"

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
  # through the numeric guards above. One `mkdir` branch, not two, since
  # issue athenaeum#1591 left the ledger exactly one home.
  mkdir -p "$PM_CACHE_DIR" 2>/dev/null || true
  printf '%s\n' "$pm_record" >> "$PM_LEDGER_PATH" 2>/dev/null || true
  return 0
}

# ── Local topics trace (issue athenaeum#1530) ────────────────────────────
# athenaeum#711 decided the ledger stores a query HASH, never raw query
# text or topics — a deliberate privacy property of an artifact that may
# be aggregated or read off-machine, and this issue MUST NOT weaken it:
# `_pm_record_push` above is byte-identical in shape to before this issue
# (AC2, pinned by a shape test) — no `topics` key was added to it. Topics
# instead go to a SEPARATE, purely local, ring-buffered file
# (`_last_turn_topics.jsonl` under `$CACHE_DIR`, same directory
# `session-start-recall.sh` already writes `stopwords.txt`/`config.env`
# into), never written to the wiki, never compiled, never shipped past
# this machine — the same shape of separation athenaeum#1528 used for
# names/descriptions. `athenaeum viewer` is this file's one reader,
# joining it to a push record by the `query_hash` value both carry.
#
# Fire-and-forget, fail OPEN (AC4): a trace-write problem must degrade to
# "no topics recorded", NEVER to "no context injected" and never to a
# slower turn. Every filesystem operation below is individually `|| true`
# / `|| return 0`'d (belt-and-braces, matching `_pm_record_push`'s own
# discipline — an unbound-variable abort under `set -u` is not caught by
# the CALLER's `|| true`, only by guarding each command here), and the
# whole function is invoked BACKGROUNDED (`&`) after this hook has already
# produced its stdout, so even a slow ring-buffer trim below cannot add a
# single millisecond to the wall-clock this hook is measured against.
# `PM_CACHE_DIR` (D3's `ATHENAEUM_CACHE_DIR env > $HOME/.cache/athenaeum`
# resolution, defined near the top of this file's push-telemetry setup),
# NOT the plain `$CACHE_DIR` this hook uses for everything else. `CACHE_DIR`
# is hardcoded to `${HOME}/.cache/athenaeum` and does not honour
# `ATHENAEUM_CACHE_DIR` at all -- `_cmd_viewer.py`'s
# `_load_topics_for_query_hash` resolves this SAME file via
# `athenaeum.config.resolve_cache_dir`, whose precedence is `arg >
# ATHENAEUM_CACHE_DIR env > default`, i.e. `PM_CACHE_DIR`'s exact shape. Any
# deployment that sets `ATHENAEUM_CACHE_DIR` (this hook's own ledger write
# two lines below already accounts for that split -- see `PM_CACHE_DIR`'s
# own definition comment) would otherwise have the hook write this trace to
# one directory while the viewer reads another -- a silent
# `topics_status: "not_instrumented"` rather than an error, exactly the
# "wrong result that looks like a legitimate one" failure mode issue
# athenaeum#1530 cites athenaeum#1513 for.
PM_TOPICS_TRACE_PATH="${PM_CACHE_DIR}/_last_turn_topics.jsonl"
# Ring buffer bound (AC3): last N turns, never unbounded. Overridable for
# tests; 200 short JSON lines is a few tens of KB, trimmed every write.
PM_TOPICS_TRACE_MAX_LINES="${ATHENAEUM_TOPICS_TRACE_MAX_LINES:-200}"

# Renders `$TERMS` (newline-separated, already lowercase/alnum-sanitized —
# see the extraction block below, shared verbatim with FTS_QUERY/
# VECTOR_QUERY so the trace records exactly what the search actually ran
# on) as a JSON array of escaped strings. Reuses `_pm_json_escape` (issue
# athenaeum#1343) rather than a second escaper.
_pm_topics_json_array() {
  local line arr=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    _pm_json_escape "$line"
    if [ -n "$arr" ]; then
      arr="${arr},\"${_PM_RET}\""
    else
      arr="\"${_PM_RET}\""
    fi
  done <<< "$TERMS"
  _PM_RET="[${arr}]"
}

# Writes one topics-trace row keyed by `$PM_QUERY_HASH` (memoized by
# `_pm_ensure_query_hash`, shared with `_pm_record_push` so both rows key
# off the identical hash — AC1) and trims the file back to the ring-buffer
# bound. Every step degrades silently on failure; nothing here can raise
# under `set -e`/`set -u` in a way its own caller (`_pm_write_topics_trace
# || true`, itself backgrounded) fails to absorb.
_pm_write_topics_trace() {
  _pm_ensure_query_hash
  [ -n "$PM_QUERY_HASH" ] || return 0
  [ -n "${TERMS:-}" ] || return 0

  _pm_topics_json_array
  local topics_json="$_PM_RET"
  local ts session_id_esc record
  if ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 2))); then
    local _oldtz="${TZ-__unset__}"
    TZ=UTC printf -v ts '%(%Y-%m-%dT%H:%M:%SZ)T' -1 2>/dev/null || ts=""
    if [ "$_oldtz" = "__unset__" ]; then unset TZ; else TZ="$_oldtz"; fi
  else
    ts=$(TZ=UTC date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
  fi
  _pm_json_escape "${SESSION_ID:-unknown}"
  session_id_esc="$_PM_RET"

  record="{\"session_id\":\"${session_id_esc}\",\"ts\":\"${ts}\",\"query_hash\":\"${PM_QUERY_HASH}\",\"topics\":${topics_json}}"

  # `$PM_CACHE_DIR`, matching `$PM_TOPICS_TRACE_PATH`'s own resolution above
  # -- NOT `$CACHE_DIR`. This write is fail-open by design (AC4), which cuts
  # both ways: a missing directory would not error, it would just silently
  # produce no topics -- the same invisible-failure shape this whole review
  # finding is about, just relocated to setup instead of resolution. This
  # `mkdir -p` is a SEPARATE call from `_pm_record_push`'s own (on
  # `$PM_CACHE_DIR` for the ledger too, since athenaeum#1591) because the trace can
  # be enabled/disabled independently of the ledger and must not depend on
  # that other code path having already run.
  mkdir -p "$PM_CACHE_DIR" 2>/dev/null || return 0
  printf '%s\n' "$record" >> "$PM_TOPICS_TRACE_PATH" 2>/dev/null || return 0

  # Ring buffer (AC3): keep only the last N lines, via a temp file + atomic
  # rename so a crash mid-trim never leaves a torn/partial trace. Cheap at
  # N=200 short JSON lines; done here rather than skipped-and-let-it-grow
  # because "bounded so it cannot grow without limit" is the issue's own
  # wording for AC3, and this whole function already runs backgrounded so
  # the trim cost is never on the interactive turn's critical path.
  local tmp
  tmp=$(mktemp "${PM_TOPICS_TRACE_PATH}.XXXXXX" 2>/dev/null) || return 0
  if tail -n "$PM_TOPICS_TRACE_MAX_LINES" "$PM_TOPICS_TRACE_PATH" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$PM_TOPICS_TRACE_PATH" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
  fi
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
# Issue athenaeum#1120 added a `memory_tier` column (schema v4) and a
# `HAS_TIER_COLUMN` probe that chose between naming it and substituting a
# literal `''`. Issue athenaeum#1345 removed the tier FILTER, issue
# athenaeum#1513 shipped that removal, and issue athenaeum#1514 retired
# the tier vocabulary and dropped the column again (schema v5). The probe
# and both of its branches are gone with it: this hook now names only
# columns that exist in every schema version it can meet, so there is
# nothing left to degrade between.
#
# NOTE the legacy-DB hazard that probe existed for is still real for
# OTHER columns — see HAS_DESCRIPTION_COLUMN immediately below, which
# keeps exactly the shape this one had. Dropping a column is the safe
# direction (a SELECT that does not name it works against a v4 DB and a
# v5 DB alike); it was only ever ADDING one that needed the probe.

# Issue athenaeum#1344 — the legacy-DB hazard the note above describes,
# for the one column it still applies to: a DB built before `description`
# existed would raise `sqlite3.OperationalError` on a SELECT that names
# it, which this hook's own `2>/dev/null || echo ""` would otherwise
# swallow into a silent ZERO recall for the whole turn. Probed once and
# shared by both the FTS5 query below and the vector-hit metadata lookup
# further down, so the two degrade together.
#
# This needs no second whole-query branch: `description` only changes one
# SELECT-list expression, so gating just that expression through
# `DESC_COL` below degrades every read site to the SAME name-only render
# at once.
HAS_DESCRIPTION_COLUMN=false
if [ -f "$DB_FILE" ] && sqlite3 "$DB_FILE" "PRAGMA table_info(wiki);" 2>/dev/null | grep -q '|description|'; then
  HAS_DESCRIPTION_COLUMN=true
fi

# Issue athenaeum#1344 — the ONE SQL expression that renders `description`
# for the bullet, reused verbatim everywhere a row is read (the FTS5
# query below and the vector-metadata lookup further down) so the render
# can never disagree with itself between backends (AC "the vector branch
# renders identically").
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

# Issue athenaeum#1789 (Quine follow-up): the column list for the FTS5
# MATCH column-filter below (`{...}: (query)`) must name only columns that
# actually exist in THIS db -- FTS5 raises an error for an unknown column
# name in a filter, which the FTS5 query's `2>/dev/null || echo ""` would
# otherwise swallow into a silent EMPTY push, exactly the legacy-DB hazard
# `HAS_DESCRIPTION_COLUMN` already exists to avoid for every other read
# site in this file. Same reuse-the-probe discipline as `DESC_COL` above.
FTS_MATCH_COLS="filename name tags aliases"
if [ "$HAS_DESCRIPTION_COLUMN" = true ]; then
  FTS_MATCH_COLS="filename name tags aliases description"
fi

FTS_RESULTS=""
if [ -f "$DB_FILE" ]; then
  # Issue athenaeum#1343 (Plan step 3): `audience` was added to the SELECT
  # list purely to feed the telemetry row below — a wider row from the
  # SAME query, no new query. The trailing literal `'fts5'` tags each row
  # with the backend it came from, so the merge step downstream never
  # needs to guess. Issue athenaeum#1344 widens this SAME query once more
  # with `${DESC_COL}` (see above) — still one query, no second lookup,
  # no new process. Ordering stays `ORDER BY rank` alone: no description
  # term participates in selection or ordering (AC "ordering and
  # selection are by relevance alone").
  #
  # ENFORCEMENT SURFACE 1 of 2 (issues athenaeum#1345, athenaeum#1513).
  # This WHERE clause used to carry `AND memory_tier = 'hot'`. It is
  # gone. The gate excluded 96.5% of the real corpus (892 hot of 25,505)
  # including every one of 17,265 `person` pages, and its failure mode
  # was silent substitution rather than silence: in 10 of 12 sampled
  # queries it still returned three hits, just materially worse ones.
  # Surface 2 is the vector metadata join further down; both came out
  # together, because removing only one reintroduces branch divergence
  # with the sign flipped.
  #
  # Issue athenaeum#1514 then removed `memory_tier` from the SELECT list
  # too. It had survived the gate's removal as a telemetry-only column;
  # retiring the vocabulary retired the column (schema v5), so there is
  # no longer anything to select or to record.
  #
  # Issue athenaeum#1665: this raw query is UNFILTERED by design (ordering
  # is `ORDER BY rank` alone; the LIMIT below is the widened fetch window,
  # not a selection cut -- issue athenaeum#1783 moved the actual selection
  # cut to the merge-time ceiling+budget pass further down this file) --
  # the relevance-floor filter for these rows, when it applies at all, runs
  # LATER, inside the vector half's Python invocation below, not here. It
  # does NOT apply here unconditionally: on a turn where the vector half
  # does not run at all (no vector index, or `SEARCH_BACKEND=fts5`), these
  # rows flow straight through unfiltered, because there is no live Python
  # invocation to filter them in without spending a fresh interpreter start
  # purely for that -- which would break the `<50ms` FTS5-only latency
  # contract this file's header documents and repeatedly relies on. See
  # that header's "Relevance floor" section for the full picture and the
  # push-ledger evidence for why FTS5 needed this at all (a prior version
  # of this comment wrongly assumed FTS5 was a cold path not worth the
  # trouble).
  #
  # Issue athenaeum#1789: the ``wiki`` table gained a ``body`` column
  # (schema v6) so ``athenaeum.search.FTS5Backend`` could index page body
  # content for its own (Python) query path. This raw query never asked
  # for that -- it is not "upgraded" to search body, it is diluted: FTS5's
  # bare ``rank`` (bm25 with every INDEXED column weighted equally)
  # started spreading relevance over a column six that never existed when
  # this query's tuning was last measured, and body is far longer than
  # every other column combined, so a document's ``name``/``tags``/
  # ``aliases``/``description`` match now competes against its own body
  # noise. The ``{col1 col2}: (query)`` column-filter prefix below scopes
  # the MATCH back to exactly the five columns this query always searched
  # -- restoring this query's PRE-schema-v6 candidate set and ranking, not
  # changing it. The parentheses are load-bearing: FTS5's column filter
  # binds to only the single phrase/group immediately following the colon,
  # so `{cols}: "a" OR "b"` restricts only `"a"` and leaves `"b"` an
  # unrestricted (body-included) match -- silently defeating the whole
  # point for every term but the first (see
  # `athenaeum.search.FTS5Backend.query`'s `metadata_only` docstring,
  # where the identical Python-side query hit the identical bug). Giving
  # this query body matching WITH weighting, matching what
  # `FTS5Backend.query`'s default (non-metadata_only) path now does, is
  # issue athenaeum#1798's scope, not this fix's -- this restores prior
  # behavior, it does not add the new capability.
  # Issue athenaeum#1783: widened from `LIMIT 3` to `LIMIT $WINDOW`, and
  # `type` (already indexed UNINDEXED, `src/athenaeum/search.py`'s
  # `_CREATE_SQL`) added to the SELECT list -- one more column on this
  # SAME query, no new lookup, matching the `audience`/`${DESC_COL}`
  # precedent above. `type` feeds the withheld-by-type tally the merge
  # step below computes; it plays no part in selection or ordering
  # (`ORDER BY rank` alone, unchanged).
  FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
    SELECT filename, name, rank, audience, 'fts5', ${DESC_COL}, type
    FROM wiki
    WHERE wiki MATCH '{${FTS_MATCH_COLS}}: (${FTS_QUERY})'
    ${EXCLUDE}
    ORDER BY rank
    LIMIT ${WINDOW};
  " 2>/dev/null || echo "")
fi

VECTOR_RESULTS=""
VECTOR_ERR=""
if [ "$SEARCH_BACKEND" = "vector" ] && [ -d "$VECTOR_DIR" ]; then
  # Failures here are non-fatal — the hook still surfaces FTS5 results —
  # but we capture stderr to $VECTOR_ERR so ATHENAEUM_HOOK_DEBUG=1 can
  # surface the reason. Most common cause: chromadb import missing in
  # the python3 on PATH (see `pip install athenaeum[vector]`).
  _vector_tmp=$(mktemp -t athenaeum-vec-XXXXXX)
  # Issue athenaeum#1665: this Python invocation used to print every vector
  # hit `query_vector_index` returned, unfiltered, and never touched the
  # FTS5 rows already computed above at all. Both are now filtered here,
  # through the SAME library functions `mcp_server.py`'s own floor block
  # uses (`meets_relevance_floor` / `resolve_recall_relevance_floor`),
  # rather than reimplementing either backend's direction rule in
  # shell/SQL. See this file's header "Relevance floor" section for the
  # full design (why FTS5 needed this too, the push-scoped-first
  # resolution order, and the fail-open/stderr-diagnostic contract) — kept
  # there rather than repeated here so there is one place describing it.
  #
  # `$FTS_RESULTS` (this turn's already-queried, still-unfiltered FTS5
  # rows) and `$KNOWLEDGE_ROOT` are passed in as extra argv, not env: an
  # argv element preserves embedded newlines/tabs exactly (unlike a
  # multi-line `awk -v` value, which BWK awk rejects outright — see the
  # `VECTOR_META` join's own comment above for that hazard), and passing
  # `KNOWLEDGE_ROOT` explicitly means the config load below does not
  # depend on whether the value happened to be exported.
  #
  # The `athenaeum.config` / `meets_relevance_floor` imports are PLAIN
  # package imports, deliberately NOT routed through the `ATHENAEUM_SRC`
  # dev-path override just below. `ATHENAEUM_SRC` is not test-only — the
  # live hook sets it every turn too, pointing at the deploy checkout
  # (`session-start-recall.sh` caches it the same way), and that override
  # is what lets `query_vector_index` load from a single known file by
  # path without requiring a full package install. `athenaeum.config` has
  # a much wider transitive import surface (`athenaeum.models` and
  # others) that single-file dev-path loading does not provide for, so
  # this import goes through normal Python package resolution instead —
  # the same mechanism a real `pip install athenaeum` deployment already
  # satisfies, and the one this issue's floor mechanism is meant to run
  # through unmodified. A test that wants to exercise this path (rather
  # than the fail-open default) has to put a real `athenaeum` package
  # somewhere `sys.path` can find it — see `_vector_env`'s own comment in
  # `tests/test_shell_hooks.py` for how.
  #
  # `_vector_rc` (issue athenaeum#1665): captures the Python process's real
  # exit status via `|| _vector_rc=$?` -- NOT a bare `|| true`, which
  # discarded it entirely and left the sentinel-split logic below with no
  # way to distinguish "ran to completion" from "crashed but happened to
  # print something sentinel-shaped first". `cmd || var=$?` is the
  # standard errexit-safe idiom: under `set -e`, a command that is part of
  # an `||` list is exempt from triggering the script-wide abort, so a
  # Python failure here degrades gracefully into the three-way guard below
  # instead of killing the whole hook.
  _vector_rc=0
  VECTOR_RESULTS=$("$PYTHON" -c "
import sys, os, importlib.util
from pathlib import Path

src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    # athenaeum#1826: search.py imports `from athenaeum.authority import
    # is_pointer_stub` at module scope, so loading it as a standalone file
    # (deliberately kept -- see the block comment above -- for single-file
    # stub loading in tests, independent of the plain `athenaeum.config`
    # package import just below) still needs the real package reachable on
    # sys.path for THAT internal import to resolve. Insert it before
    # exec_module rather than switching to a plain package import here:
    # this script also puts a real athenaeum checkout's src/ on
    # PYTHONPATH (see the athenaeum.config import below), and a plain
    # `from athenaeum.search import ...` would let that real, later
    # sys.path entry's REGULAR package win over an earlier ATHENAEUM_SRC
    # namespace portion whenever it lacks __init__.py -- exactly what a
    # test's fake single-file stub is.
    real_src = os.path.join(src, 'src')
    if real_src not in sys.path:
        sys.path.insert(0, real_src)
    spec = importlib.util.spec_from_file_location('athenaeum.search', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    query_vector_index = mod.query_vector_index
else:
    from athenaeum.search import query_vector_index


def _resolve_floor(cfg, resolve_recall_relevance_floor, backend_name):
    # Issue athenaeum#1665: this hook IS the unprompted push path
    # (UserPromptSubmit) -- resolve the push-scoped knob FIRST
    # (unprompted=True) and fall back to the plain knob only if that one
    # is unset, never the reverse. resolve_recall_relevance_floor itself
    # only ever reads ONE of the two levels per call; this ordering is
    # composed here across two calls, not inside that library function.
    floor = resolve_recall_relevance_floor(cfg, backend_name, unprompted=True)
    if floor is None:
        floor = resolve_recall_relevance_floor(cfg, backend_name, unprompted=False)
    return floor


floor_vector = None
floor_fts5 = None
meets_relevance_floor = None
try:
    from athenaeum.config import load_config, resolve_recall_relevance_floor
    from athenaeum.search import meets_relevance_floor as _meets_relevance_floor
    meets_relevance_floor = _meets_relevance_floor
    knowledge_root_arg = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else ''
    cfg = load_config(Path(knowledge_root_arg) if knowledge_root_arg else None)
    floor_vector = _resolve_floor(cfg, resolve_recall_relevance_floor, 'vector')
    floor_fts5 = _resolve_floor(cfg, resolve_recall_relevance_floor, 'fts5')
except Exception as e:
    floor_vector = None
    floor_fts5 = None
    meets_relevance_floor = None
    # Issue athenaeum#1665: a silent except swallowed this entirely before
    # -- indistinguishable from 'no floor configured'. One line to stderr,
    # captured into \$_vector_tmp below exactly like the vector backend's
    # own failure diagnostic, surfaced only under ATHENAEUM_HOOK_DEBUG=1.
    print(f'athenaeum recall: relevance floor inactive: {e!r}', file=sys.stderr)

# Issue athenaeum#1665: filter the FTS5 rows this hook already queried via
# sqlite3 (sys.argv[3]), one already-tab-separated row per line -- each
# KEPT row is re-printed VERBATIM (never reconstructed), so this can never
# drift from what the SQL emitted. A row whose rank does not parse as a
# float is kept (fail open on a malformed row, never silently dropped).
fts_raw = sys.argv[3] if len(sys.argv) > 3 else ''
for line in fts_raw.split('\n'):
    if not line:
        continue
    fields = line.split('\t')
    rank_str = fields[2] if len(fields) > 2 else ''
    keep = True
    if floor_fts5 is not None and meets_relevance_floor is not None:
        try:
            rank = float(rank_str)
        except ValueError:
            rank = None
        if rank is not None and not meets_relevance_floor('fts5', rank, floor_fts5):
            keep = False
    if keep:
        print(line)

# Sentinel, not a delimiter guess: separates the (possibly floor-filtered)
# FTS5 rows above from the vector rows below in this one combined stdout
# stream, so the bash caller can split them back into \$FTS_RESULTS /
# \$VECTOR_RESULTS without a second process.
print('__ATHENAEUM_FTS5_FLOOR_END__')

seen = set()
seen_file = sys.argv[2]
if os.path.isfile(seen_file):
    with open(seen_file) as f:
        seen = set(l.strip() for l in f)
# Issue athenaeum#1783: widened from a hardcoded n=3 to argv[5] (the same
# $WINDOW the FTS5 LIMIT above widened to) -- passed as argv, not
# interpolated into this heredoc, matching this file's own precedent for
# every other per-call value threaded into this invocation.
_vector_n = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else 3
for fname, name, score in query_vector_index(sys.argv[1], os.path.expanduser('~/.cache/athenaeum'), n=_vector_n, exclude=seen):
    if floor_vector is not None and meets_relevance_floor is not None and not meets_relevance_floor('vector', score, floor_vector):
        continue
    print(f'{fname}\t{name}\t{score}')
" "$VECTOR_QUERY" "$SEEN_FILE" "$FTS_RESULTS" "$KNOWLEDGE_ROOT" "$WINDOW" 2>"$_vector_tmp") || _vector_rc=$?
  VECTOR_ERR=$(cat "$_vector_tmp" 2>/dev/null || echo "")
  rm -f "$_vector_tmp"
  if [ -n "$VECTOR_ERR" ] && [ "${ATHENAEUM_HOOK_DEBUG:-0}" = "1" ]; then
    echo "athenaeum recall: vector backend failed: ${VECTOR_ERR}" >&2
  fi

  # Issue athenaeum#1665: split the combined stdout back into the
  # (floor-filtered) FTS5 rows and the vector rows by the sentinel above --
  # a THREE-WAY guard keyed on BOTH the captured Python exit status
  # (`$_vector_rc`, set above via `|| _vector_rc=$?` instead of a bare
  # `|| true`, which discarded it entirely) and the sentinel's presence in
  # the captured text. Sentinel-presence alone is not enough: CPython
  # flushes stdout on an unhandled exception even when piped (verified
  # directly), so a crash AFTER the sentinel still leaves $VECTOR_RESULTS
  # holding a REAL but INCOMPLETE vector section -- using it as-is would
  # relabel a partial, truncated capture as "the vector rows" with nothing
  # to mark it as an unfinished list.
  #   1. sentinel present AND rc==0: the script ran to completion (nothing
  #      else exits 0) -- split normally, both halves are valid data.
  #   2. sentinel present AND rc!=0: the crash happened AFTER the
  #      filter-and-print-sentinel pass, which is unconditional and
  #      sequenced strictly before the vector loop -- the sentinel's mere
  #      presence PROVES that pass finished, so the pre-sentinel FTS5
  #      section is complete and trustworthy. Use it; force the vector
  #      section to empty instead of whatever partial rows printed before
  #      the crash.
  #   3. sentinel absent (any rc): the crash happened before or during the
  #      fts5-filter pass itself (e.g. the search.py import step, which
  #      runs first) -- there is no complete filtered section to trust, so
  #      fall back to the ORIGINAL raw/unfiltered fts5 rows the sqlite3
  #      query above computed (left untouched by this whole block) and an
  #      empty vector section. Fail open to the pre-existing behaviour,
  #      never to zero recall.
  if printf '%s\n' "$VECTOR_RESULTS" | grep -qF '__ATHENAEUM_FTS5_FLOOR_END__'; then
    _combined="$VECTOR_RESULTS"
    FTS_RESULTS=$(printf '%s\n' "$_combined" | awk '
      BEGIN { insent = 0 }
      $0 == "__ATHENAEUM_FTS5_FLOOR_END__" { insent = 1; next }
      !insent { print }
    ')
    if [ "$_vector_rc" -eq 0 ]; then
      VECTOR_RESULTS=$(printf '%s\n' "$_combined" | awk '
        BEGIN { insent = 0 }
        $0 == "__ATHENAEUM_FTS5_FLOOR_END__" { insent = 1; next }
        insent { print }
      ')
    else
      VECTOR_RESULTS=""
    fi
  else
    VECTOR_RESULTS=""
  fi
fi

# ── Metadata lookup for vector hits (issues athenaeum#1120 → athenaeum#1513) ──
# HISTORY, because this block's shape only makes sense with it: issue
# athenaeum#1120 introduced this lookup as a hot-tier POST-FILTER, so the
# vector backend would be held to the same `memory_tier = 'hot'` bar the
# FTS5 `WHERE` clause enforced. Issue athenaeum#1345 decided the gate
# comes out entirely (relevance alone decides the push); issue
# athenaeum#1513 is the regression that it had, in the interim, reached
# production on this file. The filtering is gone from BOTH surfaces.
#
# What the block does NOW is purely a metadata join: one bounded lookup
# into the SAME index rows the FTS5 query reads, restricted to the (at
# most 3) filenames the vector backend actually returned — never an
# unbounded scan — carrying `audience` and `description`
# through so a vector-sourced item renders and reports EXACTLY as an
# FTS5-sourced one. Cost: the vector branch already pays a
# Python interpreter start (~400ms, see the header latency note); one
# more bounded (<=3-row) sqlite3 lookup (~1-3ms) does not touch that
# contract.
#
# Issue athenaeum#1343: this same bounded lookup is widened (not a new
# query) to also carry `audience` through for each surviving vector hit
# into `VECTOR_META` (a `filename\taudience\tdescription` map), so the
# telemetry row built below can record `scope` (D5) for a vector-sourced
# item exactly as it does for an FTS5-sourced one. That issue also
# carried `memory_tier` (D8) through the same map; issue athenaeum#1514
# retired the tier vocabulary and dropped the column, so the map is one
# field narrower.
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
    # ENFORCEMENT SURFACE 2 of 2 (issues athenaeum#1345, athenaeum#1513).
    # This lookup used to end `AND memory_tier = 'hot'`, which made
    # `VECTOR_META` do double duty: the metadata join AND the
    # authoritative "kept" set for an awk post-filter over
    # `VECTOR_RESULTS`. Both the restriction and the derived keep-filter
    # are gone. This is the surface that mattered in practice — live
    # traffic is 100% `backend: "vector"` (32 of 32 sampled sidecar
    # pushes), so a fix that removed only the FTS5 `WHERE` above would
    # have changed nothing observable while looking green.
    #
    # What REMAINS is the lookup itself: the audience/description join
    # that feeds the render and the telemetry row, still bounded to the
    # (at most 3) filenames the vector backend actually returned — never
    # an unbounded scan. Issue athenaeum#1514 dropped `memory_tier` from
    # the projection along with the column itself (schema v5), which is
    # also what collapsed this site's two legacy/current branches into
    # the single query below.
    # Issue athenaeum#1783: `type` added to this SAME bounded lookup, same
    # reasoning as the FTS5 SELECT above -- feeds the withheld-by-type
    # tally for a vector-sourced hit exactly as it now does for an
    # FTS5-sourced one.
    VECTOR_META=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename, audience, ${DESC_COL}, type FROM wiki
      WHERE filename IN (${_vector_in_list});
    " 2>/dev/null || echo "")
  fi
fi

# Normalize VECTOR_RESULTS (filename, name, score) to the SAME 6-field
# shape the FTS5 branch's widened SELECT already produces (filename,
# name, rank-or-empty, audience, backend, description),
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
#
# `VECTOR_META` is fed to awk as the LEADING SECTION OF AWK'S OWN INPUT
# STREAM, terminated by a sentinel line — never through `-v` (issue
# athenaeum#1516). This is not a style preference. `VECTOR_META` is a
# multi-LINE blob (one row per matched filename), and a `-v` assignment
# whose value contains a newline is REJECTED OUTRIGHT by BWK awk — the
# `awk` shipped as /usr/bin/awk on macOS, i.e. the interpreter this hook
# actually runs under in deployment:
#
#   $ printf 'a\n' | awk -v m="$(printf 'x1\ty\nx2\tz\n')" '{print NR}'
#   awk: newline in string x1     y x2      z... at source line 1
#
# awk exits 2 having produced NOTHING on stdout, so the hook injected no
# context at all — a hard recall outage, not a graceful degrade (the
# hook's contract is the reverse: degrade to "no push record", never to
# "no injected context"). gawk ACCEPTS the same assignment, which is why
# this was invisible on a gnu-awk CI runner. The one-line case succeeds
# under both, which is why it was also invisible in PRODUCTION for the
# entire lifetime of the hot-tier gate: at ~3.5% hot, a vector query
# essentially never returned two or more *hot* metadata rows, so `meta`
# was empty or exactly one line. Removing that gate (athenaeum#1513) did
# not cause this bug, it merely stopped hiding it.
#
# Cost note: this stays ONE `printf | awk` pipe — `printf` is a bash
# builtin, so the newline-safe route adds no process, no temp file and
# no cleanup path, which matters on a per-turn critical path with a hard
# wall-clock budget. Field semantics are unchanged: the join is still by
# filename and the emitted row is still the same width as the FTS5
# branch's (7 fields since issue athenaeum#1783 added `type` as the new
# last one; 6 between issue athenaeum#1514 dropping `memory_tier` and
# that).
if [ -n "$VECTOR_RESULTS" ]; then
  VECTOR_RESULTS=$(printf '%s\n__ATHENAEUM_VECTOR_META_END__\n%s\n' "$VECTOR_META" "$VECTOR_RESULTS" | awk -F'\t' '
    BEGIN { inmeta = 1 }
    # The sentinel is a literal inside the program, not a `-v` value: a
    # filename column can never equal it, and hardcoding keeps this pass
    # entirely free of `-v`.
    inmeta && $0 == "__ATHENAEUM_VECTOR_META_END__" { inmeta = 0; next }
    inmeta {
      # An empty `VECTOR_META` still yields one blank leading line from
      # the printf above; skip it exactly as the old split() loop did.
      # `next` is load-bearing: a metadata row has a non-empty $1 and at
      # least 2 fields, so without it the emit rule below would fall
      # through and print metadata rows as though they were vector hits.
      if ($0 != "") { aud[$1] = $2; desc[$1] = $3; typ[$1] = $4 }
      next
    }
    NF >= 2 && $1 != "" {
      a = ($1 in aud) ? aud[$1] : "|"
      d = ($1 in desc) ? desc[$1] : ""
      ty = ($1 in typ) ? typ[$1] : ""
      printf "%s\t%s\t\t%s\tvector\t%s\t%s\n", $1, $2, a, d, ty
    }
  ')
fi

# Merge: FTS5 first (lexical precision), then vector, dedupe. Rows are 7
# fields wide (issue athenaeum#1344 added `description`, issue athenaeum#1514
# removed `memory_tier` from the middle, issue athenaeum#1783 added `type`
# as the new last field — see the SELECT above); `NF >= 2` only ever
# checked that a row has at least a filename and a name, so it has needed
# no change through any of the three. NOT cap-cut here any more (issue
# athenaeum#1783 replaced the old `| head -3` with the combined
# cap+budget+tally pass below) -- `MERGED` below carries every deduped
# candidate inside the fetch window, in relevance order.
MERGED=$(printf '%s\n%s\n' "$FTS_RESULTS" "$VECTOR_RESULTS" \
  | awk -F'\t' 'NF >= 2 && $1 != "" && !seen[$1]++')

# Issue athenaeum#1783: the overflow-breadcrumb template, cached by
# session-start-recall.sh (`session-start-recall.sh`'s own
# "recall_overflow_breadcrumb.md" copy step). Read as a plain single-line
# value: `recall_overflow_breadcrumb.md` is authored as exactly one line
# (`src/athenaeum/prompts/recall_overflow_breadcrumb.md`), but this guards
# against a multi-line value tripping BWK awk's `-v` rejection anyway
# (issue athenaeum#1516's hazard) -- fail OPEN to no overflow line, never
# to a crashed hook. AC: "If the cache copy is missing, the hook emits no
# overflow line and still succeeds."
OVERFLOW_TMPL=""
if [ -f "${CACHE_DIR}/recall_overflow_breadcrumb.md" ]; then
  OVERFLOW_TMPL=$(cat "${CACHE_DIR}/recall_overflow_breadcrumb.md" 2>/dev/null || echo "")
  case "$OVERFLOW_TMPL" in
    *$'\n'*) OVERFLOW_TMPL="" ;;
  esac
fi

# ── Enforce the ceiling, tally withheld-by-type, and enforce the push-token budget (issues athenaeum#1120, athenaeum#1783) ────────────────
# Mirrors athenaeum.context._apply_budget's greedy-pack behaviour over the
# merged, deduped, rank-ordered candidates above: a candidate is included
# and its token cost added to the running total ONLY if doing so keeps the
# total <= budget. A candidate that would exceed the budget is SKIPPED
# (never truncated) — later, smaller candidates are still considered, so
# the budget is packed rather than cut off at the first miss (see
# `_apply_budget`'s docstring for the reference behaviour this loop
# reproduces; issue athenaeum#1353 retired the older
# `athenaeum.memory_tiers.select_for_push` reference this comment used to
# cite — that function had no production caller and duplicated this same
# behaviour, and issue athenaeum#1514 deleted the module itself).
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
#
# The two `-v` assignments below are SAFE and are deliberately left as
# `-v` (audited under issue athenaeum#1516, which fixed the multi-line
# `-v` outage in the VECTOR_META join above). Neither value can ever
# contain a newline, so neither can trip BWK awk's "newline in string"
# rejection:
#   * `preamble` is a static literal defined on the very next line. It
#     carries no prompt text, no user input and no DB content, and its
#     only `\n` is trailing — which `$(...)` strips. Single-line by
#     construction.
#   * `budget` is validated near the top of this file (`case "$BUDGET"
#     in ''|*[!0-9]*) BUDGET=1200`). Any embedded newline makes the
#     value non-numeric and it is replaced by the integer default, so by
#     the time it reaches here it is a string of ASCII digits.
PREAMBLE=$(printf '[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\n')
# Issue athenaeum#1783: ONE awk pass now does what used to be two separate
# steps (`head -3` then a budget-only pack): apply the ceiling to `$MERGED`
# (every candidate past position `ceiling`, in relevance order, is
# withheld), THEN greedy-pack the survivors into the token budget exactly
# as before (a candidate that would exceed it is withheld too — "budget
# drops are counted, not silent"). Both withholding reasons tally into the
# SAME `wtype[]` map, by field 7 (`type`, defaulting to "page" — same
# convention `athenaeum.search.apply_relevance_cap` uses for the MCP
# surface), because the AC does not distinguish WHY a candidate was
# withheld, only that it was. `idx` reaching `window` at END means the
# widened fetch itself was exhausted -- there may be more withheld
# candidates this turn never even fetched, so the emitted count is then a
# LOWER BOUND ("at least N").
#
# `-v` safety (issue athenaeum#1516's hazard, audited the same way the
# VECTOR_META join above is): `preamble`/`budget`/`ceiling`/`window` are
# all single-line by construction (same reasoning as before this issue);
# `tmpl` is guarded above (falls back to "" on any embedded newline) so it
# can never trip BWK awk's "newline in string" rejection either.
RESULTS=$(printf '%s' "$MERGED" | awk -F'\t' -v preamble="$PREAMBLE" -v budget="$BUDGET" -v ceiling="$CEILING" -v window="$WINDOW" -v tmpl="$OVERFLOW_TMPL" '
  BEGIN { total = int(length(preamble) / 4); idx = 0; withheld = 0 }
  {
    idx++
    typ = ($7 != "") ? $7 : "page"
    if (idx > ceiling) {
      withheld++
      wtype[typ]++
      next
    }
    name = $2
    desc = $6
    bullet = (desc != "") ? name " — " desc : name
    block = "  - " bullet "\n"
    cost = int(length(block) / 4)
    if (total + cost > budget) {
      withheld++
      wtype[typ]++
      next
    }
    total += cost
    # Issue athenaeum#1343/#1344/#1783: append the rendered `bullet` (8th
    # field, shifted from 7th by the new `type` field) and this
    # candidate'"'"'s own token cost (9th field) — the telemetry row built
    # below REUSES `cost` verbatim (per-item and, summed, in aggregate)
    # rather than recomputing the estimate a second way, and the output
    # loop below REUSES `bullet` verbatim rather than re-deriving it from
    # `name` alone.
    print $0 "\t" bullet "\t" cost
  }
  END {
    if (withheld > 0 && tmpl != "") {
      n = 0
      for (t in wtype) { order[n] = t; n++ }
      for (i = 1; i < n; i++) {
        key = order[i]; j = i - 1
        while (j >= 0 && order[j] > key) { order[j + 1] = order[j]; j-- }
        order[j + 1] = key
      }
      types = ""
      for (i = 0; i < n; i++) {
        t = order[i]
        if (types != "") types = types ", "
        types = types wtype[t] " " t
      }
      count_str = (idx >= window) ? ("at least " withheld) : withheld
      line = tmpl
      gsub(/\{count\}/, count_str, line)
      gsub(/\{types\}/, types, line)
      print "\n__ATHENAEUM_OVERFLOW__" line
    }
  }
')

# Split the overflow sentinel (if any -- see the `withheld > 0` guard
# above) off the END of `$RESULTS` before anything downstream reads it as
# a stream of candidate rows. Always the LAST line when present (`END`
# runs after every candidate row has already been printed), so a suffix
# match is exact, never a substring collision with a real row (no indexed
# filename can equal this sentinel).
OVERFLOW_LINE=""
case "$RESULTS" in
  *$'\n'"__ATHENAEUM_OVERFLOW__"*)
    OVERFLOW_LINE="${RESULTS##*$'\n'__ATHENAEUM_OVERFLOW__}"
    RESULTS="${RESULTS%$'\n'__ATHENAEUM_OVERFLOW__*}"
    ;;
esac

if [ -z "$RESULTS" ]; then
  # Issue athenaeum#1783 AC "empty": nothing survived the cap/budget pass
  # -- the hook emits nothing at all, including no overflow line, exactly
  # as the pre-existing `exit 0` did.
  exit 0
fi

# Issue athenaeum#1344 — narrow `$RESULTS` down to just `filename\tbullet`
# pairs BEFORE the render loop touches it, via awk (not bash `read`). This
# is not a style choice: bash's `read` treats TAB as "IFS whitespace"
# no matter what IFS is set to (`IFS=$'\t' read -r a b c <<< $'a\t\tc'`
# silently SQUASHES the empty middle field and shifts `c` into `$b` —
# verified directly against this box's bash 5.2; non-whitespace IFS
# characters like `,` do not do this, but tab is special-cased regardless
# of the IFS value). `audience` (field 4 of the 9-field row) is genuinely
# empty for a page carrying no `audience:` frontmatter — and reading a
# `read -r` variable list deep enough to reach `bullet` (field 8) over
# such a row would silently
# swallow it into an earlier field, corrupting the very text this loop
# exists to render (the render loop has none of `_pm_record_push`'s
# numeric guards to fail safe with — a shifted field here is just WRONG
# output, not a caught default). awk's own field splitting has no such
# whitespace special-casing (verified above, and already relied on by
# every OTHER awk pass in this file) — extracting just the two fields the
# render loop needs, in awk, sidesteps the hazard entirely rather than
# working around it.
# Issue athenaeum#1783: field 8, not 7 -- `type` (new field 7) shifted
# `bullet` one position to the right.
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

# Issue athenaeum#1783: the overflow breadcrumb, appended AFTER every
# bullet above -- same JSON-escape call, same `\n`-joined shape, so it
# renders as one more line in the SAME `additionalContext` string. Never
# written when `$OVERFLOW_LINE` is empty (AC: no overflow line at all when
# nothing was withheld, or when the cached template was missing/unusable).
if [ -n "$OVERFLOW_LINE" ]; then
  _pm_json_escape "$OVERFLOW_LINE"
  MATCHES="${MATCHES}${_PM_RET}\n"
fi

# Sidecar push telemetry (issue athenaeum#1343): exactly one call, and the
# ONLY thing standing between a failure inside `_pm_record_push` and this
# script's `set -e` is this `|| true` — see that function's header
# comment for the `set -u` caveat it does NOT rely on `|| true` to cover.
_pm_record_push || true

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\\n%s"}}' "$MATCHES"

# Local topics trace (issue athenaeum#1530): backgrounded and fully
# `|| true`-guarded (AC4) so this can never delay the hook's stdout above
# (already produced) or fail the script — see `_pm_write_topics_trace`'s
# header comment for why every step inside it is independently guarded
# too. stdout/stderr are discarded: this is best-effort telemetry, not
# something that should ever print to a hook's transcript.
( _pm_write_topics_trace || true ) >/dev/null 2>&1 &
disown 2>/dev/null || true
