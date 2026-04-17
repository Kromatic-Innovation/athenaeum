#!/usr/bin/env bash
# UserPromptSubmit hook: surface wiki pages relevant to the user's message.
#
# Runs a hybrid FTS5 + (optional) vector search against the athenaeum index
# built by session-start-recall.sh. Typical runtime: <50ms (FTS5 only),
# ~400ms (vector), ~1.5s when the LLM topic extractor is enabled.
#
# Why hybrid. FTS5 phrase match rescues short proper-noun queries that
# collide in vector space ("Return Path" embeds closer to any page
# containing "path" than to a sparse entity page). Vector search
# discovers semantic neighbours with no lexical overlap ("iterative
# feedback loops" -> "Innovation Accounting"). Each backend rescues a
# class of queries the other handles poorly — the merge is load-bearing.
#
# Optional LLM query-rewriting. If `athenaeum query-topics` is available
# and ANTHROPIC_API_KEY is set, the raw prompt is first run through Haiku
# to extract substantive topics while ignoring meta-instructions
# ("quote verbatim", "don't call tools"). Falls back silently to a
# regex+stopword extractor when unavailable.
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

CACHE_DIR="${HOME}/.cache/athenaeum"
CONFIG_ENV="${CACHE_DIR}/config.env"
DB_FILE="${CACHE_DIR}/wiki-index.db"
VECTOR_DIR="${CACHE_DIR}/wiki-vectors"
ATHENAEUM_CLI="${ATHENAEUM_CLI:-athenaeum}"
PYTHON="${ATHENAEUM_PYTHON:-python3}"

# ── Source config ──────────────────────────────────────────────────────
# `set -a` auto-exports sourced variables so child processes (notably
# `athenaeum query-topics`, which reads ANTHROPIC_API_KEY from its own
# env) inherit them. Without it, `source` sets vars only in this shell
# and the child silently runs without the key.
if [ -f "$CONFIG_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_ENV"
  set +a
fi
AUTO_RECALL="${AUTO_RECALL:-true}"
SEARCH_BACKEND="${SEARCH_BACKEND:-fts5}"

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
if command -v "$ATHENAEUM_CLI" >/dev/null 2>&1 && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
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
  STOPWORDS="the and for are but not you all can had her was one our out has his how its let may new now old see way who did get got him she too use with from have this that they will been call come each find give help here just know like long look make many more most much must next only over said same some such take tell than them then very want well went were what when which while work also back been being both came does done down even goes going good keep last left life line made need never part place point right show small still think those turn used using where would about after again could every great might often other shall should since start state still there these thing think three through under until which while world would years your into just like made over said some than them then time very want what when will with year does really right going being looking trying running check please sure okay yeah thanks"
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
FTS_RESULTS=""
if [ -f "$DB_FILE" ]; then
  FTS_RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
    SELECT filename, name, rank
    FROM wiki
    WHERE wiki MATCH '${FTS_QUERY}'
    ${EXCLUDE}
    ORDER BY rank
    LIMIT 3;
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

# Merge: FTS5 first (lexical precision), then vector, dedupe, cap 3.
RESULTS=$(printf '%s\n%s\n' "$FTS_RESULTS" "$VECTOR_RESULTS" \
  | awk -F'\t' 'NF >= 2 && $1 != "" && !seen[$1]++' \
  | head -3)

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
