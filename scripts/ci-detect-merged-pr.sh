#!/usr/bin/env bash
# ci-detect-merged-pr.sh — decide whether a push is the merge commit of an
# already-merged PR, so `ci.yml` can skip the redundant post-merge CI run.
#
# Ported verbatim in behaviour from code-workspace-config's
# scripts/ci-detect-merged-pr.sh (cwc#2758), which hestia also adopted
# unmodified (hestia#1938). athenaeum#1160 adopts the same pattern here as the
# short-circuit half of restoring `push: [develop]` (cwc#2755 piloted this in
# hestia). Layered as the FALLBACK behind `ci-attestation-gate.sh`
# (`scripts/ci-key.sh` + `ci-attestation-publish.sh`), which tries first
# because it also reaches squash/rebase merges this detector cannot see (no
# `Merge pull request #` subject).
#
# CAUSE the original bug guards against (measured on cwc/hestia, not assumed).
# A workflow that declares `permissions: contents: read` at file level sets
# every unnamed scope to `none`, so `GITHUB_TOKEN` carries no `pull-requests`
# scope. On a private repo, `GET /repos/{o}/{r}/pulls/{n}` then answers 403
# `Resource not accessible by integration`, and a naive
# `| jq -r '.merged // false'` renders that refusal as the string `false`,
# indistinguishable from a genuine "not merged" — so the check never once
# deduplicated, silently, for months.
#
# THE FIX has two halves and needs both:
#   1. `check_duplicate` is granted `pull-requests: read` (in ci.yml).
#   2. This script never collapses an API failure into an answer. It reads the
#      HTTP status separately from the body and reports one of four outcomes:
#        merged        - HTTP 200, `.merged` is literally true   -> skip CI
#        not-merged    - HTTP 200, `.merged` is literally false  -> run CI
#        denied        - 401/403: the token cannot read the PR   -> run CI, ::error::
#        indeterminate - transient/unparseable after N attempts  -> run CI, ::warning::
#      The last two still fall back to running CI — failing OPEN on work is the
#      safe direction — but they are LOUD.
#
# Retry policy: transient classes (404, 429, 5xx, network failure, and a 200
# whose body carries no boolean `merged`) are retried with linear backoff.
# 401/403 are NOT retried: they are deterministic authorization answers.
#
# Usage:
#   ci-detect-merged-pr.sh --event-name <name> --repo <owner/name> \
#                          --commit-message <msg> [--output <file>]
#
# Writes exactly one `skip=true` / `skip=false` line to --output (default: the
# file named by $GITHUB_OUTPUT, else stdout). Always exits 0: a detector that
# fails the job would wedge the merge commit it is trying to make cheap,
# because `CI Required` fails closed when `check_duplicate` does not succeed.
#
# Env knobs (tests use these; CI uses the defaults):
#   GH_TOKEN                  bearer token for the API read
#   GITHUB_API_URL            API base (default https://api.github.com)
#   CI_DETECT_ATTEMPTS        max attempts for a transient class (default 3)
#   CI_DETECT_BACKOFF_SEC     linear backoff unit in seconds (default 2)

set -uo pipefail

EVENT_NAME=""
REPO=""
COMMIT_MSG=""
OUTPUT="${GITHUB_OUTPUT:-}"
API_BASE="${GITHUB_API_URL:-https://api.github.com}"
ATTEMPTS="${CI_DETECT_ATTEMPTS:-3}"
BACKOFF="${CI_DETECT_BACKOFF_SEC:-2}"

die() { printf '%s\n' "ci-detect-merged-pr.sh: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --event-name)     EVENT_NAME="${2:-}"; shift 2 ;;
    --repo)           REPO="${2:-}"; shift 2 ;;
    --commit-message) COMMIT_MSG="${2:-}"; shift 2 ;;
    --output)         OUTPUT="${2:-}"; shift 2 ;;
    -h|--help)        sed -n '1,60p' "$0"; exit 0 ;;
    *)                die "unknown argument: $1" ;;
  esac
done

[ -n "$EVENT_NAME" ] || die "--event-name is required"
[ -n "$REPO" ] || die "--repo is required"

# Emit the decision and stop. `skip=false` is the safe direction: run everything.
emit() {
  if [ -n "$OUTPUT" ]; then
    printf 'skip=%s\n' "$1" >> "$OUTPUT"
  else
    printf 'skip=%s\n' "$1"
  fi
  exit 0
}

if [ "$EVENT_NAME" != "push" ]; then
  echo "Event is '$EVENT_NAME', not 'push' — CI runs in full."
  emit false
fi

# A GitHub merge commit's subject is "Merge pull request #<n> from <branch>".
# Trim with sed rather than xargs: xargs treats quote characters as special and
# aborts on any commit subject carrying an unbalanced one.
FIRST_LINE=$(printf '%s' "$COMMIT_MSG" | head -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

case "$FIRST_LINE" in
  "Merge pull request #"*) ;;
  *)
    echo "Head commit is not a PR merge commit — CI runs in full."
    emit false
    ;;
esac

echo "Merge commit detected — checking PR merge status."

# Anchored at the subject's start. `grep -oP '(?<=#)\d+'` would happily take a
# hash-prefixed number from anywhere in the line, and depends on GNU grep's
# PCRE build being present.
PR_NUM=$(printf '%s' "$FIRST_LINE" | sed -n 's/^Merge pull request #\([0-9][0-9]*\).*$/\1/p')

if [ -z "$PR_NUM" ]; then
  echo "::warning title=duplicate-CI check::Merge-commit subject carried no PR number: '${FIRST_LINE}'. Running CI in full."
  emit false
fi

RESULT=""
DETAIL=""
ATTEMPTS_MADE=0

lookup_pr() {
  local attempt=1 http body merged sleep_for
  while :; do
    ATTEMPTS_MADE="$attempt"
    body="$(mktemp)"
    http=$(curl -sS -L -o "$body" -w '%{http_code}' \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${GH_TOKEN:-}" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "${API_BASE}/repos/${REPO}/pulls/${PR_NUM}" 2>/dev/null)
    if [ -z "$http" ]; then http="000"; fi

    case "$http" in
      200)
        merged=$(jq -r 'if type == "object" and (has("merged")) and ((.merged | type) == "boolean")
                        then (.merged | tostring) else "absent" end' "$body" 2>/dev/null) || merged="absent"
        rm -f "$body"
        case "$merged" in
          true)  RESULT="merged";     DETAIL="HTTP 200, .merged=true";  return 0 ;;
          false) RESULT="not-merged"; DETAIL="HTTP 200, .merged=false"; return 0 ;;
          *)     DETAIL="HTTP 200 but the body carried no boolean .merged field" ;;
        esac
        ;;
      401|403)
        # Deterministic authorization answer — retrying only disguises it.
        rm -f "$body"
        RESULT="denied"
        DETAIL="HTTP $http from ${API_BASE}/repos/${REPO}/pulls/${PR_NUM}"
        return 0
        ;;
      000)
        rm -f "$body"
        DETAIL="the API request failed before returning a status (network error)"
        ;;
      *)
        rm -f "$body"
        DETAIL="HTTP $http"
        ;;
    esac

    if [ "$attempt" -ge "$ATTEMPTS" ]; then
      RESULT="indeterminate"
      return 0
    fi
    sleep_for=$(( BACKOFF * attempt ))
    echo "PR #${PR_NUM} read was inconclusive (${DETAIL}); retrying in ${sleep_for}s (attempt $((attempt + 1)) of ${ATTEMPTS})."
    sleep "$sleep_for"
    attempt=$((attempt + 1))
  done
}

lookup_pr

case "$RESULT" in
  merged)
    echo "PR #${PR_NUM} is merged (${DETAIL}) — skipping the redundant post-merge CI run."
    emit true
    ;;
  not-merged)
    echo "::warning title=duplicate-CI check::PR #${PR_NUM} reports ${DETAIL} on a merge-commit push. This is a definite 'not merged' answer, not a failed read. Running CI in full."
    emit false
    ;;
  denied)
    echo "::error title=duplicate-CI check is misconfigured::Could not read PR #${PR_NUM}: ${DETAIL}. This is an authorization failure, NOT a 'not merged' answer — the check_duplicate job needs 'pull-requests: read'. Running CI in full; every merge commit will pay a duplicate run until this is fixed."
    emit false
    ;;
  *)
    echo "::warning title=duplicate-CI check could not reach a verdict::Could not determine whether PR #${PR_NUM} is merged after ${ATTEMPTS_MADE} attempt(s); last outcome: ${DETAIL}. Running CI in full."
    emit false
    ;;
esac
