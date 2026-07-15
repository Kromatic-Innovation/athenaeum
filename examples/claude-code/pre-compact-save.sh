#!/usr/bin/env bash
# PreCompact hook: remind Claude to save observations before context loss.
#
# Fires before Claude Code compacts the conversation context window.
# Outputs a system message nudging Claude to save any unsaved observations
# to the knowledge base via the `remember` MCP tool.
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "PreCompact": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/pre-compact-save.sh"
#       }]
#     }]
#   }

set -euo pipefail

# ── Kill switch (issue #379) ───────────────────────────────────────────────
# Honour ~/.cache/athenaeum/disabled (+ ATHENAEUM_DISABLED). Mirrors
# athenaeum.killswitch.is_disabled("recall"): the "all" scope suppresses the
# save nudge; the "compile" scope leaves it on. Costs no Python startup.
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

cat <<'EOF'
{"systemMessage": "[Knowledge checkpoint] Context is about to be compacted. If you learned any important facts this session (decisions, people, project status, architecture choices) that haven't been saved yet, call the `remember` tool now before context is lost."}
EOF
