#!/usr/bin/env bash
# SessionStart hook: inject wiki context based on the current working directory.
#
# Cheap, dependency-free heuristic. Derives keywords from the cwd path and
# greps wiki/ for matching pages. Surfaces page name + first paragraph as a
# single-block startup hint, so the model knows which entities are relevant
# to the project being opened — *before* any prompt is submitted.
#
# Pairs naturally with `session-start-recall.sh` (the main FTS5/vector index
# builder). This script costs nothing if the wiki is missing and is silent
# when no keywords or no matches are found, so it's safe to wire alongside.
#
# Configure in ~/.claude/settings.json (alongside session-start-recall.sh):
#   "hooks": {
#     "SessionStart": [{
#       "hooks": [
#         { "type": "command", "command": "/path/to/session-start-recall.sh" },
#         { "type": "command", "command": "/path/to/wiki-context-inject.sh" }
#       ]
#     }]
#   }
#
# Environment variables:
#   KNOWLEDGE_ROOT       Knowledge base root (default: ~/knowledge)
#   KNOWLEDGE_WIKI_PATH  Wiki directory (default: $KNOWLEDGE_ROOT/wiki)
#   ATHENAEUM_INJECT_SKIP_WORDS  Pipe-separated cwd path segments to ignore
#                                (default: Code|Users|home|workspace|src|lib|app|var|tmp|usr)
#   ATHENAEUM_INJECT_MAX_RESULTS Max wiki pages to surface (default: 3)
#
# Fail-silent contract: every external call is `||true`-guarded; the hook
# must never block session startup.

set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$KNOWLEDGE_ROOT/wiki}"
SKIP_WORDS="${ATHENAEUM_INJECT_SKIP_WORDS:-Code|Users|home|workspace|src|lib|app|var|tmp|usr}"
MAX_RESULTS="${ATHENAEUM_INJECT_MAX_RESULTS:-3}"

[ -d "$WIKI_ROOT" ] || exit 0

# Derive keywords from cwd path segments. Drop generic words and tokens
# shorter than 3 characters (too noisy). The tail -5 keeps the most-specific
# (deepest) segments, which usually carry the project identity.
CWD="$(pwd)"
PROJECT_NAME="$(basename "$CWD")"
KEYWORDS=$(echo "$CWD" | tr '/-' '\n' \
  | grep -vE "^(${SKIP_WORDS})?$" \
  | grep -E '.{3,}' \
  | tail -5 \
  | tr '\n' '|' \
  | sed 's/|$//')

[ -n "$KEYWORDS" ] || exit 0

# Iterate wiki pages, match against frontmatter + opening body. Files
# starting with `_` are index/system pages (e.g. `_pending_questions.md`)
# and skipped to avoid surfacing meta-pages as project context.
MATCHES=""
MATCH_COUNT=0

for md_file in "$WIKI_ROOT"/*.md; do
  [ -f "$md_file" ] || continue
  case "$(basename "$md_file")" in _*) continue ;; esac

  if head -30 "$md_file" 2>/dev/null | grep -qiE "$KEYWORDS"; then
    PAGE_NAME=$(grep -m1 '^name:' "$md_file" 2>/dev/null \
      | sed 's/^name:[[:space:]]*//' | tr -d '"'"'" \
      || basename "$md_file" .md)
    # First non-frontmatter, non-heading paragraph as a one-line summary.
    SUMMARY=$(awk '
      /^---$/ { if (++c == 2) next; if (c > 1) p = 1; next }
      p && /^[^#]/ && NF { print; exit }
    ' "$md_file" 2>/dev/null || echo "")

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
