#!/usr/bin/env bash
# SessionStart hook: inject relevant wiki context based on cwd.
#
# Searches the athenaeum wiki for pages matching the current project name
# and outputs summaries so Claude "just knows" relevant context.
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/session-start-recall.sh",
#         "timeout": 15
#       }]
#     }]
#   }
#
# Environment variables:
#   KNOWLEDGE_WIKI_PATH  Path to wiki directory (default: ~/knowledge/wiki)

set -euo pipefail

WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$HOME/knowledge/wiki}"

if [ ! -d "$WIKI_ROOT" ]; then
  exit 0  # No wiki available — silently skip
fi

# Derive project keywords from cwd
CWD="$(pwd)"
PROJECT_NAME="$(basename "$CWD")"

# Build keyword list from cwd path segments (skip generic ones)
SKIP_WORDS="Code|Users|home|workspace|src|lib|app|var|tmp|usr"
KEYWORDS=$(echo "$CWD" | tr '/-' '\n' | grep -vE "^($SKIP_WORDS)?$" | grep -E '.{3,}' | tail -5 | tr '\n' '|' | sed 's/|$//')

if [ -z "$KEYWORDS" ]; then
  exit 0
fi

# Search wiki frontmatter (name, aliases, tags) and first 30 lines of body
MATCHES=""
MATCH_COUNT=0
MAX_RESULTS=3

for md_file in "$WIKI_ROOT"/*.md; do
  [ -f "$md_file" ] || continue
  # Skip index/system files
  case "$(basename "$md_file")" in _*) continue ;; esac

  if head -30 "$md_file" | grep -qiE "$KEYWORDS" 2>/dev/null; then
    # Extract name from frontmatter
    PAGE_NAME=$(grep -m1 '^name:' "$md_file" 2>/dev/null | sed 's/^name:[[:space:]]*//' | tr -d '"'"'" || basename "$md_file" .md)
    # Extract first non-frontmatter paragraph as summary
    SUMMARY=$(awk '/^---$/{if(++c==2) next; if(c>1) p=1; next} p && /^[^#]/ && NF{print; exit}' "$md_file" 2>/dev/null || echo "")

    if [ -n "$PAGE_NAME" ]; then
      MATCHES="${MATCHES}  - ${PAGE_NAME}"
      [ -n "$SUMMARY" ] && MATCHES="${MATCHES}: ${SUMMARY}"
      MATCHES="${MATCHES}
"
      MATCH_COUNT=$((MATCH_COUNT + 1))
      [ "$MATCH_COUNT" -ge "$MAX_RESULTS" ] && break
    fi
  fi
done

if [ "$MATCH_COUNT" -gt 0 ]; then
  cat <<EOF
[Knowledge context for ${PROJECT_NAME}]
Relevant wiki pages (use \`recall\` MCP tool for details):
${MATCHES}
EOF
fi
