#!/usr/bin/env bash
# public-safe-lint-gate.sh — push-boundary wrapper around public-safe-lint.sh
# (athenaeum#1104).
#
# Runs the org-agnostic scanner (public-safe-lint.sh) against a given path
# and additionally FAILS when the set of rules carrying active suppressions
# is not a subset of the committed
# `<scan-root>/.public-safe-lint-suppression-allowlist` -- closing the
# "green but 3/7 rules partially suppressed" gap the scanner's own verdict
# line admits to (its `SUPPRESSED [...]` lines and coverage summary). A
# genuine hit (public-safe-lint.sh exit != 0 -- a real FAIL or a canary
# failure) still fails outright, unchanged.
#
# No allowlist file at all means the asserted suppressed-rule count is 0:
# any active suppression fails the gate until a rule name is explicitly
# added to that file.
#
# Usage: public-safe-lint-gate.sh <path-to-scan> [<path-to-public-safe-lint.sh>]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:?usage: $(basename "$0") <path-to-scan> [<path-to-public-safe-lint.sh>]}"
LINTER="${2:-$SCRIPT_DIR/../public-safe-lint.sh}"

if [ ! -f "$LINTER" ]; then
  echo "GATE FAIL: linter script not found at $LINTER" >&2
  exit 1
fi

LINT_OUTPUT="$(mktemp)"
trap 'rm -f "$LINT_OUTPUT"' EXIT

bash "$LINTER" "$ROOT" >"$LINT_OUTPUT" 2>&1
LINT_EXIT=$?

cat "$LINT_OUTPUT"

if [ "$LINT_EXIT" -ne 0 ]; then
  echo "GATE FAIL: public-safe-lint.sh reported a hit (or canary failure) -- see output above." >&2
  exit 1
fi

# Rule names with active suppressions this run, parsed from the linter's own
# "SUPPRESSED [rule-name]: file:line ..." lines -- same file+rule-name-only
# discipline as the linter itself, never the matched content.
mapfile -t SUPPRESSED_NOW < <(grep '^SUPPRESSED \[' "$LINT_OUTPUT" \
    | sed -E 's/^SUPPRESSED \[([a-zA-Z-]+)\].*/\1/' | sort -u)

ALLOWLIST_FILE="$ROOT/.public-safe-lint-suppression-allowlist"
mapfile -t ALLOWED < <(grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST_FILE" 2>/dev/null \
    | sed -E 's/[[:space:]]+$//')

UNAPPROVED=()
for rule in "${SUPPRESSED_NOW[@]:-}"; do
  [ -z "$rule" ] && continue
  found=0
  for allowed in "${ALLOWED[@]:-}"; do
    [ "$rule" = "$allowed" ] && { found=1; break; }
  done
  [ "$found" -eq 0 ] && UNAPPROVED+=("$rule")
done

if [ "${#UNAPPROVED[@]}" -gt 0 ]; then
  {
    echo "GATE FAIL: rule(s) [${UNAPPROVED[*]}] have active suppressions not present in"
    echo "  $ALLOWLIST_FILE"
    echo "  A newly-suppressed rule must be reviewed and explicitly added to that file"
    echo "  (or the suppression removed from .public-safe-lintignore) before this passes."
  } >&2
  exit 1
fi

echo "GATE OK: public-safe-lint clean; suppressed rule(s) (${SUPPRESSED_NOW[*]:-none}) all present in allowlist."
exit 0
