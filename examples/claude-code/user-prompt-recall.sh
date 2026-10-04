#!/usr/bin/env bash
# UserPromptSubmit hook: surface wiki pages relevant to the user's message.
#
# THIN EXAMPLE -- it contains no recall logic of its own. No SQL, no
# ranking, no slot cap, no render budget, no telemetry arithmetic. It
# resolves the packaged adapter (`athenaeum-claude-hook`, i.e.
# `athenaeum.claude_code_adapter`) and lets the hook's stdin JSON flow
# straight into it.
#
# Why thin. This file used to be a ~1650-line bash/awk reimplementation of
# per-turn recall: its own FTS5 SELECT, its own BM25 ordering, its own
# slot cap, its own budget packing, its own overflow notice. Two
# implementations of one contract drift, and this one did -- repeatedly,
# and silently, because nothing compared them. The adapter calls
# `athenaeum.context.build_context_for_turn`, the same per-turn sequence
# `athenaeum context --stdin-json` runs, so the hook's output and the
# CLI's output cannot diverge independently. Everything the old shell body
# did lives behind that one call now: the kill switch, `AUTO_RECALL`, the
# `SEARCH_BACKEND` choice, the minimum prompt length, the recall-cap
# ceiling, push telemetry, and the relevance-cap overflow line.
#
# A repo guard (`scripts/check_hook_examples.py`, exercised by
# `tests/test_hook_examples_guard.py`) fails CI if search SQL or ranking
# logic is reintroduced into any shipped hook example, or if this file
# stops delegating to the packaged adapter. Re-forking is meant to be
# mechanically impossible, not merely discouraged.
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "UserPromptSubmit": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/user-prompt-recall.sh 2>/dev/null || true",
#         "timeout": 5
#       }]
#     }]
#   }
#
# Installing the package also puts `athenaeum-claude-hook` on the PATH, so
# the hook command can name that console script directly and skip this
# launcher entirely. This file stays shipped for the copy-the-kit install
# flow (see `settings-snippet.json`) and for a venv whose bin directory is
# not on the PATH Claude Code's hook subprocess inherits.
#
# Env knobs this launcher itself reads -- every other knob belongs to the
# adapter and the core behind it (see docs/reference/environment.md):
#   ATHENAEUM_PYTHON  interpreter to use when the console script is absent
#                     (default: python3)
#   ATHENAEUM_SRC     source checkout to run from, instead of an installed
#                     package (prepends "$ATHENAEUM_SRC/src" to PYTHONPATH)
#
# Requires: an installed `athenaeum` package (or `ATHENAEUM_SRC`). No
# sqlite3, no jq.

# No `set -e`: a recall failure must never fail a user turn. The adapter is
# itself fail-safe (it exits 0 on any internal error, printing nothing), so
# the only non-zero exits reachable here are a missing or broken install.
set -uo pipefail

if command -v athenaeum-claude-hook >/dev/null 2>&1; then
  exec athenaeum-claude-hook
fi

# Fallback: no console script on the PATH. Run the adapter as a module.
if [ -n "${ATHENAEUM_SRC:-}" ]; then
  PYTHONPATH="${ATHENAEUM_SRC}/src${PYTHONPATH:+:${PYTHONPATH}}"
  export PYTHONPATH
fi

PYTHON="${ATHENAEUM_PYTHON:-python3}"
if command -v "$PYTHON" >/dev/null 2>&1; then
  exec "$PYTHON" -m athenaeum.claude_code_adapter
fi

# Nothing installed to run. Stay silent and let the turn proceed.
exit 0
