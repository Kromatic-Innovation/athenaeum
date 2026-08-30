#!/usr/bin/env bash
# ci-attestation-publish.sh — publish the green-CI attestation for a tree.
#
# Ported from hestia's scripts/ci-attestation-publish.sh (hestia#1940) for
# athenaeum#1160. Called at the end of a run in which every quality job
# genuinely succeeded (not skipped), creating `refs/ci-green/v1/<hex>`
# pointing at the commit that was tested. `scripts/ci-attestation-gate.sh`
# later finds that ref and — never trusting it — verifies it against GitHub's
# own check-run record and by recomputing the key.
#
# `:` is illegal in a git ref name, so the key `v1:<hex>` becomes the ref
# `refs/ci-green/v1/<hex>`.
#
# ── WHICH COMMIT THE REF POINTS AT, AND WHY IT IS NOT ALWAYS $GITHUB_SHA ────
#
# On a `pull_request` event `$GITHUB_SHA` is the MERGE PREVIEW
# (`refs/pull/N/merge`), which is the tree Actions checks out and therefore the
# tree that gets tested — but GitHub attributes the run's check-runs to the PR
# HEAD sha, not to the merge preview. So a ref pointing at the merge preview
# would fail the gate's first verification forever — there are no check-runs
# there to find — and the attestation would never once be reused.
#
# The ref therefore points at the commit GitHub actually stamped: the PR head
# on a `pull_request` event, `$GITHUB_SHA` on a push.
#
# That is only SOUND while the head's tree and the tested tree are the same
# tree, which is exactly the condition under which the key recomputed on the
# proving commit equals the key that was tested. When `develop` has moved under
# an out-of-date PR the two diverge — the merge preview was tested, the head
# was not — and publishing would attest a tree nobody built. So this script
# recomputes the key on the proving commit and REFUSES to publish on a
# mismatch. That is the gate's verify-2, run at publish time: the publisher
# never creates a ref it already knows the gate would reject.
#
# REPLACING AN EXISTING REF. If the ref already exists pointing at a different
# commit, it is deleted and recreated rather than force-pushed. That is not
# cosmetic: this path is reached only after a full, green run, so the outcome
# is a stale or unverifiable attestation being replaced by a fresh one that was
# just earned, and delete-then-create says exactly that in the reflog while a
# force update would not.
#
# PUBLISHING IS AN OPTIMISATION, NEVER CORRECTNESS. A failed push means the next
# build of this tree runs CI in full, which is the same thing that happens
# today. So a failure is a loud `::warning::` and exit 0, not a red job: failing
# here would turn a missed saving into a blocked merge.
#
# Usage:
#   ci-attestation-publish.sh --key <v1:hex> --sha <proving commit>
#                             [--remote origin]
#
# Env knobs (tests use these; CI uses the defaults):
#   CI_ATTEST_DRY_RUN=1     print the git commands instead of running them
#   CI_KEY_SCRIPT           path to ci-key.sh (default: alongside this script)

set -uo pipefail

KEY=""
SHA=""
REMOTE="origin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_KEY_SCRIPT="${CI_KEY_SCRIPT:-$SCRIPT_DIR/ci-key.sh}"

die() { printf '%s\n' "ci-attestation-publish.sh: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --key)    KEY="${2:-}"; shift 2 ;;
    --sha)    SHA="${2:-}"; shift 2 ;;
    --remote) REMOTE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *)        die "unknown argument: $1" ;;
  esac
done

[ -n "$KEY" ] || die "--key is required"
[ -n "$SHA" ] || die "--sha is required"

case "$KEY" in
  v1:*) ;;
  *) die "unrecognised key format: '$KEY' (expected v1:<hex>)" ;;
esac

REF="refs/ci-green/${KEY/://}"

# Verify-2, at publish time. A mismatch means the proving commit does not carry
# the tree that was tested — an out-of-date PR whose base moved — so there is
# nothing here worth attesting and publishing would be a lie the gate would
# later catch. Not an error: the push run that follows the merge will attest
# the tree it actually builds.
if ! git cat-file -e "${SHA}^{commit}" 2>/dev/null; then
  git fetch --no-tags --quiet "$REMOTE" "$SHA" 2>/dev/null \
    || git fetch --no-tags --depth 1 --quiet "$REMOTE" "$SHA" 2>/dev/null \
    || true
fi

if ! git cat-file -e "${SHA}^{commit}" 2>/dev/null; then
  echo "::warning title=CI attestation not published::the proving commit $SHA is not reachable, so its key cannot be checked. Nothing published; this tree will run CI again next time."
  exit 0
fi

PROVING_KEY=$(bash "$CI_KEY_SCRIPT" "$SHA" 2>/dev/null) || PROVING_KEY=""

if [ "$PROVING_KEY" != "$KEY" ]; then
  echo "Not publishing: the tested tree keys $KEY but the proving commit $SHA keys ${PROVING_KEY:-<unreadable>}."
  echo "That is the out-of-date-PR case — the merge preview was tested and the branch head was not — and an attestation here would describe a tree nobody built. The push run after the merge will attest the tree it actually builds."
  exit 0
fi

run_git() {
  if [ -n "${CI_ATTEST_DRY_RUN:-}" ]; then
    printf 'DRY-RUN git %s\n' "$*"
    return 0
  fi
  git "$@"
}

EXISTING=$(git ls-remote "$REMOTE" "$REF" 2>/dev/null | cut -f1 | head -1)

if [ "$EXISTING" = "$SHA" ]; then
  echo "Attestation $REF already points at $SHA — nothing to publish."
  exit 0
fi

if [ -n "$EXISTING" ]; then
  echo "Replacing stale attestation $REF ($EXISTING -> $SHA)."
  run_git push "$REMOTE" ":$REF" 2>&1 \
    || echo "::warning title=CI attestation::could not delete the existing $REF; the create below will be refused and this tree will simply run CI again next time."
fi

if run_git push "$REMOTE" "$SHA:$REF" 2>&1; then
  echo "Published attestation $REF -> $SHA."
else
  echo "::warning title=CI attestation not published::pushing $SHA to $REF was refused. Nothing is broken — the next build of this tree runs CI in full, exactly as it does without this mechanism — but the saving is lost until this is fixed. Check that the publishing job has 'contents: write'."
fi

exit 0
