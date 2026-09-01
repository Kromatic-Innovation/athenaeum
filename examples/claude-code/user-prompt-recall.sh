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
  case "${ATHENAEUM_DISABLED:-}" in
    1 | true | yes | on | all) return 0 ;;
    compile) return 1 ;;
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

FTS_RESULTS=""
if [ -f "$DB_FILE" ]; then
  if [ "$HAS_TIER_COLUMN" = true ]; then
    FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename, name, rank
      FROM wiki
      WHERE wiki MATCH '${FTS_QUERY}'
      AND memory_tier = 'hot'
      ${EXCLUDE}
      ORDER BY rank
      LIMIT 3;
    " 2>/dev/null || echo "")
  else
    FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename, name, rank
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
if [ "$HAS_TIER_COLUMN" = true ] && [ -n "$VECTOR_RESULTS" ]; then
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

  _hot_vector_filenames=""
  if [ -n "$_vector_in_list" ]; then
    _hot_vector_filenames=$(sqlite3 -separator $'\t' "$DB_FILE" "
      SELECT filename FROM wiki
      WHERE filename IN (${_vector_in_list})
      AND memory_tier = 'hot';
    " 2>/dev/null || echo "")
  fi

  VECTOR_RESULTS=$(printf '%s\n' "$VECTOR_RESULTS" | awk -F'\t' -v hot="$_hot_vector_filenames" '
    BEGIN {
      n = split(hot, arr, "\n")
      for (i = 1; i <= n; i++) if (arr[i] != "") keep[arr[i]] = 1
    }
    NF >= 1 && ($1 in keep)
  ')
fi

# Merge: FTS5 first (lexical precision), then vector, dedupe, cap 3.
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
# candidate's own cost is its "  - ${name}\n" bullet line — the exact
# text built into MATCHES and the final payload below — sized with
# athenaeum.push_metrics.estimate_tokens's formula (`max(0, len(text) //
# 4)`), expressed here as `int(length(block) / 4)`. The wrapper preamble
# ("[Knowledge context] ... :\n") is charged ONCE up front rather than
# divided across candidates: it is emitted exactly once in the final
# payload regardless of how many bullets follow it, so a per-entry share
# would both double-count it in aggregate and require knowing the final
# candidate count before the greedy pass that determines it.
PREAMBLE=$(printf '[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\n')
RESULTS=$(printf '%s' "$RESULTS" | awk -F'\t' -v preamble="$PREAMBLE" -v budget="$BUDGET" '
  BEGIN { total = int(length(preamble) / 4) }
  {
    block = "  - " $2 "\n"
    cost = int(length(block) / 4)
    if (total + cost > budget) next
    total += cost
    print
  }
')

[ -n "$RESULTS" ] || exit 0

# ── Format output ───────────────────────────────────────────────────────
# Must be wrapped in hookSpecificOutput.hookEventName — Claude Code
# silently ignores a flat {"additionalContext": ...} payload.
MATCHES=""
while IFS=$'\t' read -r fname name score; do
  MATCHES="${MATCHES}  - ${name}\n"
  echo "$fname" >> "$SEEN_FILE"
done <<< "$RESULTS"

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\\n%s"}}' "$MATCHES"
