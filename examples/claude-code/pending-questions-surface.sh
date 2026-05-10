#!/usr/bin/env bash
# SessionStart hook: surface unresolved pending memory questions.
#
# When the librarian flags a contradiction or ambiguity, it lands in
# `~/knowledge/wiki/_pending_questions.md` as a checkbox block. Until the
# user resolves it (in the file, via the MCP `resolve_question` tool, or
# the `resolve-questions` skill), it just sits there and the user forgets
# it exists. This hook:
#
#   1. Resolves the athenaeum CLI binary (env: ATHENAEUM_CLI / ATHENAEUM_PYTHON
#      + ATHENAEUM_SRC; falls back to `athenaeum` on PATH).
#   2. Honors a snooze cache at `~/.cache/athenaeum/pending-questions-snoozed-until`.
#      If the file exists with an ISO-8601 datetime in the future, the hook
#      exits 0 silently. The skill (or the user) writes that file when they
#      defer; this hook only reads it.
#   3. Calls `athenaeum questions count --json` to get count + oldest date.
#   4. If count > 0, prints a one-block prompt to stdout (Claude Code
#      injects stdout as additional system context).
#
# Fail-silent guarantee: every external call has `|| true`. A malformed
# pending-questions file or a missing CLI must NEVER block session startup.
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/pending-questions-surface.sh 2>/dev/null || true",
#         "timeout": 5
#       }]
#     }]
#   }
#
# Environment variables:
#   KNOWLEDGE_ROOT             Default: ~/knowledge
#   ATHENAEUM_CLI              Default: athenaeum (override for editable installs)
#   ATHENAEUM_PYTHON           Default: python3
#   ATHENAEUM_SRC              Optional source checkout — if set, runs
#                              `python3 -m athenaeum.cli` from there.
#   ATHENAEUM_PQ_SNOOZE_HOURS  Documented for the skill: 24 (default). The
#                              hook itself only reads the snooze file; it
#                              doesn't consult the env var.
#   ATHENAEUM_PQ_HOOK_DEBUG    Set to 1 to surface CLI errors on stderr.

set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
CACHE_DIR="${HOME}/.cache/athenaeum"
SNOOZE_FILE="${CACHE_DIR}/pending-questions-snoozed-until"

# Wiki must exist; otherwise nothing to do.
[ -d "${KNOWLEDGE_ROOT}/wiki" ] || exit 0

_debug() {
  if [ "${ATHENAEUM_PQ_HOOK_DEBUG:-0}" = "1" ]; then
    printf '[pending-questions] %s\n' "$*" >&2
  fi
}

# ── Snooze check ──────────────────────────────────────────────────────────
# The snooze file holds an ISO-8601 instant (`date -u +%FT%TZ`). If the
# stored value is in the future, exit silently. Anything malformed is
# ignored — fail-open is correct here so a corrupt cache doesn't lock
# the user out of their pending-question stream forever.
if [ -f "${SNOOZE_FILE}" ]; then
  _snoozed_until="$(cat "${SNOOZE_FILE}" 2>/dev/null || true)"
  _now="$(date -u +%FT%TZ 2>/dev/null || true)"
  if [ -n "${_snoozed_until}" ] && [ -n "${_now}" ]; then
    # Lexicographic compare works on `YYYY-MM-DDTHH:MM:SSZ` (UTC fixed zone).
    if [ "${_now}" \< "${_snoozed_until}" ]; then
      _debug "snoozed until ${_snoozed_until}"
      exit 0
    fi
  fi
fi

# ── Resolve CLI invocation ────────────────────────────────────────────────
_cli_argv=()

if [ -n "${ATHENAEUM_SRC:-}" ] && [ -d "${ATHENAEUM_SRC}/src/athenaeum" ]; then
  _python="${ATHENAEUM_PYTHON:-python3}"
  _cli_argv=(
    "${_python}"
    "-c"
    "import sys; sys.path.insert(0, '${ATHENAEUM_SRC}/src'); from athenaeum.cli import main; sys.exit(main(sys.argv[1:]))"
  )
elif [ -n "${ATHENAEUM_CLI:-}" ] && command -v "${ATHENAEUM_CLI}" >/dev/null 2>&1; then
  _cli_argv=("${ATHENAEUM_CLI}")
elif command -v athenaeum >/dev/null 2>&1; then
  _cli_argv=(athenaeum)
else
  _debug "no athenaeum CLI on PATH; exiting silent"
  exit 0
fi

# ── Query pending count ───────────────────────────────────────────────────
_json="$(
  "${_cli_argv[@]}" questions count --path "${KNOWLEDGE_ROOT}" --json 2>/dev/null \
    || true
)"

if [ -z "${_json}" ]; then
  _debug "questions count returned empty"
  exit 0
fi

# Parse {"count": N, "oldest": "..."} without depending on jq. Pure-bash
# extraction that fails open: any parse miss → exit 0 silently.
_count="$(printf '%s' "${_json}" | sed -n 's/.*"count"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n1)"
_oldest="$(printf '%s' "${_json}" | sed -n 's/.*"oldest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"

if [ -z "${_count}" ] || [ "${_count}" = "0" ]; then
  _debug "count=${_count:-empty}; nothing to surface"
  exit 0
fi

# ── Surface to Claude ─────────────────────────────────────────────────────
# Plain text on stdout — Claude Code injects this as additional system
# context for SessionStart hooks. Keep concise; the user reads it once.
{
  echo "[Pending memory questions] ${_count} unresolved (oldest: ${_oldest:-unknown})."
  echo "Resolve interactively now, defer to tomorrow, or skip?"
  echo "Available via: \`athenaeum questions next --with-proposal\` or the resolve-questions skill."
} || true

exit 0
