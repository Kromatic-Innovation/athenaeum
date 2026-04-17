#!/usr/bin/env bash
# UserPromptSubmit hook: search wiki FTS5 index for context relevant to
# the user's message.
#
# Queries a precomputed SQLite FTS5 index (built by session-start-recall.sh
# at SessionStart). No Python needed at query time — uses sqlite3 + jq.
#
# Tracks which pages were already surfaced this session to avoid repeating.
# Typical runtime: <50ms.
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
# Requires: sqlite3, jq (both ship with macOS)

set -euo pipefail

DB_FILE="${HOME}/.cache/athenaeum/wiki-index.db"

if [ ! -f "$DB_FILE" ]; then
  exit 0
fi

# ── Parse stdin JSON with jq (no Python cold start) ────────────────────
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null)

if [ -z "$PROMPT" ] || [ ${#PROMPT} -lt 8 ]; then
  exit 0
fi

# ── Extract search terms ────────────────────────────────────────────────
STOPWORDS="the and for are but not you all can had her was one our out has his how its let may new now old see way who did get got him she too use with from have this that they will been call come each find give help here just know like long look make many more most much must next only over said same some such take tell than them then very want well went were what when which while work also back been being both came does done down even goes going good keep last left life line made need never part place point right show small still think those turn used using where would about after again could every great might often other shall should since start state still there these thing think three through under until which while world would years your into just like made over said some than them then time very want what when will with year does really right going being looking trying running check please sure okay yeah thanks"

TERMS=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '\n' | grep -vE "^(${STOPWORDS})$" | grep -E '.{3,}' | sort -u | head -8)

if [ -z "$TERMS" ]; then
  exit 0
fi

FTS_QUERY=$(echo "$TERMS" | sed 's/.*/"&"/' | tr '\n' ' ' | sed 's/ *$//' | sed 's/" "/\" OR \"/g')

# ── Session dedup ───────────────────────────────────────────────────────
SEEN_FILE="/tmp/knowledge-seen-${SESSION_ID}"
touch "$SEEN_FILE"

EXCLUDE=""
if [ -s "$SEEN_FILE" ]; then
  EXCLUDE=$(while read -r fn; do printf "AND filename != '%s' " "$fn"; done < "$SEEN_FILE")
fi

# ── Query FTS5 index ────────────────────────────────────────────────────
RESULTS=$(sqlite3 -separator $'\t' "$DB_FILE" "
  SELECT filename, name, rank
  FROM wiki
  WHERE wiki MATCH '${FTS_QUERY}'
  ${EXCLUDE}
  ORDER BY rank
  LIMIT 3;
" 2>/dev/null || echo "")

if [ -z "$RESULTS" ]; then
  exit 0
fi

# ── Format output ───────────────────────────────────────────────────────
MATCHES=""
while IFS=$'\t' read -r fname name score; do
  MATCHES="${MATCHES}  - ${name}\n"
  echo "$fname" >> "$SEEN_FILE"
done <<< "$RESULTS"

printf '{"additionalContext":"[Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool for full details):\\n%s"}' "$MATCHES"
