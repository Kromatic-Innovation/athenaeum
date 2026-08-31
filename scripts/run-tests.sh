#!/usr/bin/env bash
# scripts/run-tests.sh — the checked-in "did the tests pass" entrypoint
# (athenaeum#1105).
#
# Two independent failure modes this closes:
#
#   1. `pytest ... | tee out.log; echo $?` reports tee's exit status, not
#      pytest's. This script never pipes pytest into anything internally:
#      it redirects pytest's combined stdout+stderr straight to a scratch
#      file with `>`, captures pytest's own $? from that direct invocation,
#      and only afterwards `cat`s the file for display. If YOU pipe this
#      script's own output further (`scripts/run-tests.sh | tee -a run.log`),
#      plain bash pipeline semantics mean a bare `$?` after THAT outer pipe
#      still reports `tee`'s status, not this script's -- that is an
#      external shell mechanic no invoked program can override from the
#      inside. Read `${PIPESTATUS[0]}` (bash) for this script's real exit
#      code in that case, or avoid the outer pipe entirely:
#      `scripts/run-tests.sh > run.log 2>&1`.
#
#   2. "The suite passed" being unfalsifiable, because the failing set is
#      environment-sensitive (0 on CI, a handful on one developer host, 40
#      on another) and nothing committed to diff against.
#
# Baseline shape (decision recorded in athenaeum#1105's PR body): the
# committed baseline (tests/known-ci-failures.txt) is the CI-ENVIRONMENT
# baseline ONLY -- CI is green on develop, so that file is empty besides
# its own header comment. A failing nodeid NOT in it is reported under a
# separate "UNRECOGNIZED" heading, never silently folded into "known", and
# ALWAYS makes this script exit non-zero -- including on a host whose
# environment produces failures CI doesn't see. That is deliberate: this
# script cannot know, from a single run, whether an unrecognized failure is
# a regression from your change or a pre-existing host quirk (answering
# that needs a same-host comparison against the base branch -- see
# `scripts/test-baseline.sh`-style tooling in the wider workspace for that
# question). It surfaces the failure either way rather than waving it
# through, which is the whole point.
#
# Usage:
#   scripts/run-tests.sh [pytest args...]
#   scripts/run-tests.sh --update-baseline [pytest args...]
#   scripts/run-tests.sh --baseline <path> [pytest args...]   # mainly for tests
#
# Bypass (verbatim, athenaeum#1105's own issue text): "a caller may invoke
# pytest directly instead of the wrapper when debugging a single test" --
# nothing here prevents `pytest tests/test_x.py::test_y -v` directly; this
# script is an additional, optional layer for the "is the WHOLE suite
# checkably green" question, not a replacement for ad hoc pytest use.
# "the known-fail baseline accepts an explicit --update-baseline with the
# new nodeids recorded in the diff" -- see below; it rewrites the baseline
# to exactly the current failing set and reports what changed.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ATHENAEUM_PYTHON:-python3}"
BASELINE_FILE="$REPO_ROOT/tests/known-ci-failures.txt"
UPDATE_BASELINE=0
ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --update-baseline)
      UPDATE_BASELINE=1
      shift
      ;;
    --baseline)
      BASELINE_FILE="$2"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

if [ "${#ARGS[@]}" -eq 0 ]; then
  ARGS=(tests/)
fi

OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT

# Direct redirection -- never a pipe -- so pytest's own exit code is
# captured exactly, with nothing downstream able to launder it. Also
# catches the "pytest isn't even installed" shape directly: that produces
# a non-zero PYTEST_EXIT with no `FAILED` lines at all, handled below.
#
# Deliberately run from the CALLER's cwd, not $REPO_ROOT -- so a nodeid in
# both the pytest output and the committed baseline is the same relative
# path regardless of which repo subdirectory you invoke this from,
# matching plain `pytest` behavior and letting tests point this script at
# an unrelated scratch project.
"$PYTHON_BIN" -m pytest "${ARGS[@]}" >"$OUT_FILE" 2>&1
PYTEST_EXIT=$?

cat "$OUT_FILE"

mapfile -t CURRENT_FAILURES < <(grep '^FAILED ' "$OUT_FILE" | sed -E 's/^FAILED //; s/ - .*$//' | sort -u)
mapfile -t BASELINE < <(grep -vE '^[[:space:]]*(#|$)' "$BASELINE_FILE" 2>/dev/null | sed -E 's/[[:space:]]+$//' | sort -u)

if [ "$UPDATE_BASELINE" -eq 1 ]; then
  ADDED=()
  REMOVED=()
  for nodeid in "${CURRENT_FAILURES[@]:-}"; do
    [ -z "$nodeid" ] && continue
    already=0
    for known in "${BASELINE[@]:-}"; do
      [ "$nodeid" = "$known" ] && { already=1; break; }
    done
    [ "$already" -eq 0 ] && ADDED+=("$nodeid")
  done
  for known in "${BASELINE[@]:-}"; do
    [ -z "$known" ] && continue
    still=0
    for nodeid in "${CURRENT_FAILURES[@]:-}"; do
      [ "$nodeid" = "$known" ] && { still=1; break; }
    done
    [ "$still" -eq 0 ] && REMOVED+=("$known")
  done

  {
    echo "# Committed CI-environment known-fail baseline (athenaeum#1105)."
    echo "# Regenerated by: scripts/run-tests.sh --update-baseline"
    echo "# One pytest nodeid per line. Empty (besides this header) means CI is"
    echo "# expected fully green -- true as of the last regeneration below."
    printf '%s\n' "${CURRENT_FAILURES[@]:-}"
  } | grep -v '^$' >"$BASELINE_FILE" || true
  # grep -v can legitimately leave the file with only the header when there
  # are zero current failures; make sure the header itself always lands.
  if [ ! -s "$BASELINE_FILE" ]; then
    {
      echo "# Committed CI-environment known-fail baseline (athenaeum#1105)."
      echo "# Regenerated by: scripts/run-tests.sh --update-baseline"
      echo "# One pytest nodeid per line. Empty (besides this header) means CI is"
      echo "# expected fully green -- true as of the last regeneration below."
    } >"$BASELINE_FILE"
  fi

  echo
  echo "== baseline updated: $BASELINE_FILE =="
  echo "now tracking ${#CURRENT_FAILURES[@]} nodeid(s)."
  if [ "${#ADDED[@]}" -gt 0 ]; then
    echo "added:"
    printf '  + %s\n' "${ADDED[@]}"
  fi
  if [ "${#REMOVED[@]}" -gt 0 ]; then
    echo "removed (no longer failing):"
    printf '  - %s\n' "${REMOVED[@]}"
  fi
  exit "$PYTEST_EXIT"
fi

UNRECOGNIZED=()
KNOWN_STILL_FAILING=()
for nodeid in "${CURRENT_FAILURES[@]:-}"; do
  [ -z "$nodeid" ] && continue
  found=0
  for known in "${BASELINE[@]:-}"; do
    [ "$nodeid" = "$known" ] && { found=1; break; }
  done
  if [ "$found" -eq 1 ]; then
    KNOWN_STILL_FAILING+=("$nodeid")
  else
    UNRECOGNIZED+=("$nodeid")
  fi
done

echo
echo "== run-tests.sh baseline diff (against $BASELINE_FILE) =="
echo "pytest exit code: $PYTEST_EXIT"
echo "failing now:      ${#CURRENT_FAILURES[@]}"
echo "known (baseline): ${#KNOWN_STILL_FAILING[@]}"
echo "unrecognized:     ${#UNRECOGNIZED[@]}"

if [ "${#KNOWN_STILL_FAILING[@]}" -gt 0 ]; then
  echo "-- KNOWN (in committed baseline -- not a new problem) --"
  printf '  %s\n' "${KNOWN_STILL_FAILING[@]}"
fi

if [ "${#UNRECOGNIZED[@]}" -gt 0 ]; then
  echo "-- UNRECOGNIZED (not in committed baseline) --"
  printf '  %s\n' "${UNRECOGNIZED[@]}"
  echo
  echo "These are not in $BASELINE_FILE, which reflects CI (green on develop)."
  echo "This may be a regression from your change, OR a failure specific to"
  echo "this host/environment that CI does not see (see this script's header)."
  echo "Either way it is not silently passed. If you've confirmed it's"
  echo "pre-existing AND host-specific, do NOT fold it into the baseline via"
  echo "--update-baseline -- that file must keep reflecting CI, not this host."
  exit 1
fi

if [ "$PYTEST_EXIT" -ne 0 ] && [ "${#CURRENT_FAILURES[@]}" -eq 0 ]; then
  # pytest failed for a reason that produced no `FAILED` nodeids at all --
  # e.g. a collection error, or pytest itself missing (Instance 1 in the
  # issue). Never treat that as a clean, baseline-only run.
  echo "pytest exited $PYTEST_EXIT with no FAILED nodeids parsed -- treating as a failure (collection error / crash), not a clean baseline-only run." >&2
  exit "$PYTEST_EXIT"
fi

exit 0
