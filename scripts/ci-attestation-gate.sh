#!/usr/bin/env bash
# ci-attestation-gate.sh — decide whether a green CI attestation already covers
# the tree being built, so `ci.yml` can skip the redundant run.
#
# Ported from hestia's scripts/ci-attestation-gate.sh (hestia#1940) for
# athenaeum#1160 — adopting the attestation/CI-key short-circuit hestia
# piloted (cwc#2755) so `push: [develop]` can be restored here without
# reintroducing the duplicate full-matrix run athenaeum#1031 removed.
#
# Layered IN FRONT of the merge-commit detector `scripts/ci-detect-merged-pr.sh`,
# which stays intact as the fallback for a key miss. The two coexist cleanly
# because they answer the same question from independent evidence: this
# script asks "has this exact tree been tested green", the detector asks "is
# this push the merge commit of a PR that already passed". Neither can answer
# the other's case, so the gate tries this one first and falls through on a
# miss.
#
# ── THE REF IS AN UNTRUSTED INDEX, NEVER PROOF ──────────────────────────────
#
# `refs/ci-green/*` CANNOT be protected: GitHub's rulesets API rejects custom
# namespaces at validation (`target` accepts only `branch`/`tag`/`push`), so
# anyone who can push to the repo can create one pointing anywhere. A found ref
# is therefore a HINT about which commit to go and check — never a verdict.
#
# On every hit this script verifies BOTH of the following, and reuses the
# attestation only if both hold:
#
#   1. the proving commit carries a green, completed `CI Required` check-run —
#      GitHub's own record, read from the API, not anything we wrote; and
#   2. recomputing the CI key on the proving commit yields the key claimed by
#      the ref name.
#
# Forging the ref then buys nothing: point it at any commit you like and step 2
# recomputes that commit's key and finds it is not the key you claimed.
#
# BOTH steps run on EVERY hit, not short-circuited on the first refusal. That
# costs one extra fetch on an already-doomed hit and buys two things: the log
# says exactly which leg failed, and "both verifications ran" is a property a
# test can assert rather than an ordering that a later edit could quietly
# invert.
#
# ── REF NAMING ──────────────────────────────────────────────────────────────
#
# The key is `v1:<64 hex>` but `:` is ILLEGAL in a git ref name, so the ref is
# `refs/ci-green/v1/<64 hex>` — the key with its separator mapped to `/`.
#
# ── FAILURE DIRECTION ───────────────────────────────────────────────────────
#
# Every uncertainty resolves to `skip=false` — run the full suite. A false miss
# costs one CI run; a false hit ships untested code. An API refusal is NEVER
# collapsed into an answer: HTTP status is read separately from the body, and
# 401/403 is reported as a loud misconfiguration rather than rendered as "no
# attestation".
#
# The script always exits 0. `CI Required` fails closed when `check_duplicate`
# does not succeed, so a gate that exited non-zero would wedge the very commit
# it is trying to make cheap.
#
# Usage:
#   ci-attestation-gate.sh --repo <owner/name> --key <v1:hex>
#                          [--commit-message <msg>] [--output <file>]
#                          [--remote origin] [--max-age-days 30]
#
# Env knobs (tests use these; CI uses the defaults):
#   GH_TOKEN                bearer token for the check-runs read
#   GITHUB_API_URL          API base (default https://api.github.com)
#   CI_KEY_SCRIPT           path to ci-key.sh (default: alongside this script)
#   CI_ATTEST_NOW_EPOCH     pin "now" for the expiry test

set -uo pipefail

REPO=""
KEY=""
COMMIT_MSG=""
OUTPUT="${GITHUB_OUTPUT:-}"
REMOTE="origin"
MAX_AGE_DAYS=30
API_BASE="${GITHUB_API_URL:-https://api.github.com}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_KEY_SCRIPT="${CI_KEY_SCRIPT:-$SCRIPT_DIR/ci-key.sh}"

die() { printf '%s\n' "ci-attestation-gate.sh: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)           REPO="${2:-}"; shift 2 ;;
    --key)            KEY="${2:-}"; shift 2 ;;
    --commit-message) COMMIT_MSG="${2:-}"; shift 2 ;;
    --output)         OUTPUT="${2:-}"; shift 2 ;;
    --remote)         REMOTE="${2:-}"; shift 2 ;;
    --max-age-days)   MAX_AGE_DAYS="${2:-}"; shift 2 ;;
    -h|--help)        sed -n '1,70p' "$0"; exit 0 ;;
    *)                die "unknown argument: $1" ;;
  esac
done

[ -n "$REPO" ] || die "--repo is required"
[ -n "$KEY" ] || die "--key is required"

emit() {
  if [ -n "$OUTPUT" ]; then
    printf 'skip=%s\n' "$1" >> "$OUTPUT"
  else
    printf 'skip=%s\n' "$1"
  fi
  exit 0
}

# A malformed key means the key computation itself went wrong. Note the
# asymmetry with a MISSING `--key` above, which is a `die`: a missing argument
# means the workflow is wired wrong and should fail loudly and immediately,
# whereas a malformed value is a runtime fault, and exiting non-zero here would
# fail `check_duplicate` and so wedge `CI Required`, which fails closed. Loud
# and safe beats loud and stuck.
case "$KEY" in
  v1:[0-9a-f]*)
    if [ "${#KEY}" -ne 67 ]; then
      echo "::error title=CI attestation gate got a malformed key::'${KEY}' is not v1:<64 hex>. Running CI in full."
      emit false
    fi
    ;;
  *)
    echo "::error title=CI attestation gate got a malformed key::'${KEY}' is not v1:<64 hex>. Running CI in full."
    emit false
    ;;
esac

# ── escape hatch ────────────────────────────────────────────────────────────
# For a sticky flake, or any time a human wants the suite run regardless of the
# evidence. Deleting the ref is the other hatch and needs no code here.
case "$COMMIT_MSG" in
  *"[ci force]"*)
    echo "Commit message carries [ci force] — attestation reuse is disabled for this run."
    emit false
    ;;
esac

REF="refs/ci-green/${KEY/://}"

# ── lookup: a HINT, not a verdict ───────────────────────────────────────────
PROVING=$(git ls-remote "$REMOTE" "$REF" 2>/dev/null | cut -f1 | head -1)

if [ -z "$PROVING" ]; then
  echo "No attestation at $REF — CI runs in full."
  emit false
fi

echo "Attestation hint found: $REF -> $PROVING. This is an UNTRUSTED index; verifying."

# ── verify 1 — GitHub's own record for the proving commit ───────────────────
V1_OK=""
V1_DETAIL=""
COMPLETED_AT=""

body="$(mktemp)"
http=$(curl -sS -L -o "$body" -w '%{http_code}' \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN:-}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "${API_BASE}/repos/${REPO}/commits/${PROVING}/check-runs?per_page=100" 2>/dev/null)
[ -n "$http" ] || http="000"

case "$http" in
  200)
    # The newest COMPLETED `CI Required` run wins. `status` is checked as well
    # as `conclusion`: an in-progress run has a null conclusion, and `null`
    # must never read as success. Assign, THEN read: a here-string always
    # synthesises a trailing newline, so `read ... <<<"$(jq ...)"` would
    # succeed even when the substitution captured nothing.
    parsed=$(jq -r '
      [ .check_runs[]?
        | select(.name == "CI Required")
        | select(.status == "completed")
        | select(.completed_at != null) ]
      | sort_by(.completed_at) | last
      | if . == null then "absent -" else "\(.conclusion) \(.completed_at)" end
    ' "$body" 2>/dev/null) || parsed=""
    if [ -z "$parsed" ]; then
      parsed="unparseable -"
    fi
    read -r conclusion COMPLETED_AT <<<"$parsed"
    if [ "$conclusion" = "success" ]; then
      V1_OK=1
      V1_DETAIL="CI Required concluded success at ${COMPLETED_AT}"
    else
      V1_DETAIL="CI Required on ${PROVING} is '${conclusion}', not 'success'"
    fi
    ;;
  401|403)
    V1_DETAIL="HTTP $http reading check-runs for ${PROVING} — an authorization refusal, NOT an answer"
    echo "::error title=CI attestation gate is misconfigured::${V1_DETAIL}. The check_duplicate job needs 'checks: read' to verify an attestation. Running CI in full."
    ;;
  *)
    V1_DETAIL="HTTP $http reading check-runs for ${PROVING}"
    ;;
esac
rm -f "$body"

# ── verify 1b — the attestation must not be stale ───────────────────────────
# The key covers the repo, not the runner image, the base image's package
# versions, or any pinned action's floating tag. Thirty days is the point past
# which "the tree is identical" stops implying "the run would still be green".
if [ -n "$V1_OK" ]; then
  now="${CI_ATTEST_NOW_EPOCH:-$(date -u +%s)}"
  proved_at=""
  if command -v date >/dev/null 2>&1; then
    proved_at=$(date -u -d "$COMPLETED_AT" +%s 2>/dev/null) \
      || proved_at=$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$COMPLETED_AT" +%s 2>/dev/null) \
      || proved_at=""
  fi
  if [ -z "$proved_at" ] && command -v python3 >/dev/null 2>&1; then
    proved_at=$(python3 -c '
import sys, datetime
print(int(datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=datetime.timezone.utc).timestamp()))' "$COMPLETED_AT" 2>/dev/null) || proved_at=""
  fi
  if [ -z "$proved_at" ]; then
    V1_OK=""
    V1_DETAIL="could not parse the proving run's completed_at ('${COMPLETED_AT}')"
  else
    age_days=$(( (now - proved_at) / 86400 ))
    if [ "$age_days" -gt "$MAX_AGE_DAYS" ]; then
      V1_OK=""
      V1_DETAIL="the proving run is ${age_days}d old (limit ${MAX_AGE_DAYS}d) — expired"
    else
      V1_DETAIL="${V1_DETAIL} (${age_days}d old, limit ${MAX_AGE_DAYS}d)"
    fi
  fi
fi

# ── verify 2 — recompute the key on the proving commit ──────────────────────
# Recomputed HERE, at verification time, from the proving commit's own tree.
# Never read back from the ref name, a build artifact, or any other mutable
# place: the ref name is the CLAIM being checked, so trusting it would make the
# check circular.
V2_OK=""
V2_DETAIL=""

if ! git cat-file -e "${PROVING}^{commit}" 2>/dev/null; then
  # `actions/checkout` clones at depth 1, and the proving commit is usually a
  # PR head rather than a branch tip, so ask for that one object. The plain
  # fetch is tried first because a `--depth 1` fetch into a repo that is NOT
  # shallow leaves a shallow boundary behind it.
  git fetch --no-tags --quiet "$REMOTE" "$PROVING" 2>/dev/null \
    || git fetch --no-tags --depth 1 --quiet "$REMOTE" "$PROVING" 2>/dev/null \
    || true
fi

if ! git cat-file -e "${PROVING}^{commit}" 2>/dev/null; then
  V2_DETAIL="the proving commit ${PROVING} could not be fetched from ${REMOTE}"
else
  RECOMPUTED=$(bash "$CI_KEY_SCRIPT" "$PROVING" 2>/dev/null) || RECOMPUTED=""
  if [ -z "$RECOMPUTED" ]; then
    V2_DETAIL="recomputing the key on ${PROVING} failed"
  elif [ "$RECOMPUTED" = "$KEY" ]; then
    V2_OK=1
    V2_DETAIL="recomputed key on ${PROVING} matches ${KEY}"
  else
    V2_DETAIL="recomputed key on ${PROVING} is ${RECOMPUTED}, but the ref claims ${KEY}"
    echo "::warning title=CI attestation refused::${V2_DETAIL}. The ref does not describe the commit it points at. That is either a forged/mispointed attestation or a change to the key algorithm; either way it is refused and CI runs in full."
  fi
fi

echo "verify-1 (GitHub check-run): $([ -n "$V1_OK" ] && echo PASS || echo FAIL) — ${V1_DETAIL}"
echo "verify-2 (recomputed key):   $([ -n "$V2_OK" ] && echo PASS || echo FAIL) — ${V2_DETAIL}"

if [ -n "$V1_OK" ] && [ -n "$V2_OK" ]; then
  echo "Attestation verified on both legs — reusing it and skipping the redundant CI run."
  emit true
fi

echo "Attestation not verified — CI runs in full."
emit false
