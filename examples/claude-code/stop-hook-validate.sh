#!/usr/bin/env bash
# stop-hook-validate.sh — Claude Code Stop-hook validator.
# Walks ~/.claude/projects/<scope>/memory/*.md and warns on auto-memory
# files that lack the citation fields (originSessionId, originTurn).
# Non-blocking by default; set VALIDATE_MODE=block to fail the hook and
# signal Claude Code that the session closed with policy violations.
#
# See docs/integrations/claude-code.md §3 "Citation policy" and §4
# "Stop-hook validator" for the surrounding policy.
set -euo pipefail

CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
MODE="${VALIDATE_MODE:-warn}"   # warn | block

if [ ! -d "$CLAUDE_PROJECTS" ]; then
  exit 0
fi

bad=0
checked=0
while IFS= read -r -d '' file; do
  checked=$((checked + 1))
  # Extract the first frontmatter block (between the first pair of --- lines).
  fm="$(awk 'BEGIN{c=0} /^---$/{c++; next} c==1{print} c>1{exit}' "$file" 2>/dev/null || true)"
  for field in originSessionId originTurn; do
    if ! grep -qE "^${field}:" <<<"$fm"; then
      echo "WARN: $file missing $field" >&2
      bad=$((bad + 1))
    fi
  done
done < <(find "$CLAUDE_PROJECTS" -path '*/memory/*.md' -type f -print0 2>/dev/null)

if [ "$bad" -eq 0 ]; then
  exit 0
fi

echo "stop-hook-validate: $bad citation issue(s) across $checked file(s)." >&2
if [ "$MODE" = "block" ]; then
  echo "VALIDATE_MODE=block — failing stop hook. Set VALIDATE_MODE=warn to downgrade." >&2
  exit 2
fi
exit 0
