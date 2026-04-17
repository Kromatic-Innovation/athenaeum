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

cat <<'EOF'
{"systemMessage": "[Knowledge checkpoint] Context is about to be compacted. If you learned any important facts this session (decisions, people, project status, architecture choices) that haven't been saved yet, call the `remember` tool now before context is lost."}
EOF
