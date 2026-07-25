#!/usr/bin/env bash
# Promote develop -> main and sync the deploy checkout, in one command.
#
# WHAT: closes the gap discovered 2026-07-25 running the athenaeum#460 fix's
# production drain — promoting via `promote-main.yml` and syncing the deploy
# checkout via `deploy-sync.sh` were two separate manual steps, and nothing
# runs the second automatically after the first (confirmed: cron-fleet's
# pre-dawn-sweep.sh only invokes the librarian wrapper, it does not sync any
# deploy checkout). Skipping the second step silently leaves the MCP server
# and nightly librarian running stale code even though `main` has moved.
#
# This script chains, in order:
#   1. Wait for CI Required to go green on develop's HEAD (avoids firing the
#      promotion gate before develop's own test matrix finishes — the exact
#      failure hit on the first promote attempt today: the gate requires a
#      COMPLETED success on the source SHA, not just a passing run so far).
#   2. Trigger promote-main.yml (workflow_dispatch) and block until it
#      completes, failing loudly (non-zero exit) if the promotion itself fails.
#   3. Run deploy-sync.sh against the deploy checkout: fetch + fast-forward +
#      reinstall (`pip install -e`) + stamp dist/.build-sha.
#   4. Print the reinstalled version + stamped SHA so the operator can see,
#      in one glance, that the deploy checkout now matches what was promoted.
#
# USAGE:
#   scripts/promote-and-deploy.sh --reason "..." \
#       [--repo OWNER/REPO] [--deploy-dir PATH] [--no-wait-for-ci] [--ci-timeout SECONDS]
#
# Requires: gh CLI authenticated with access to trigger workflow_dispatch,
# read check-runs, and watch run status on the target repo.
set -euo pipefail

REPO="Kromatic-Innovation/athenaeum"
DEPLOY_DIR="${ATHENAEUM_DEPLOY_DIR:-$HOME/local-deploys/athenaeum}"
REASON=""
WAIT_FOR_CI=1
CI_TIMEOUT="${ATHENAEUM_PROMOTE_CI_TIMEOUT:-900}"
REQUIRED_CHECK="CI Required"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) REASON="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --deploy-dir) DEPLOY_DIR="$2"; shift 2 ;;
    --no-wait-for-ci) WAIT_FOR_CI=0; shift ;;
    --ci-timeout) CI_TIMEOUT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$REASON" ]; then
  echo "usage: $0 --reason \"...\" [--repo OWNER/REPO] [--deploy-dir PATH] [--no-wait-for-ci]" >&2
  exit 2
fi

if [ "$WAIT_FOR_CI" = "1" ]; then
  develop_sha="$(gh api "repos/${REPO}/commits/develop" --jq '.sha')"
  echo "==> waiting for '${REQUIRED_CHECK}' to go green on develop (${develop_sha})"
  elapsed=0
  poll_interval=15
  while true; do
    conclusion="$(gh api "repos/${REPO}/commits/${develop_sha}/check-runs" \
      --jq ".check_runs[] | select(.name == \"${REQUIRED_CHECK}\") | .conclusion" 2>/dev/null | head -n1 || true)"
    if [ "$conclusion" = "success" ]; then
      echo "==> CI green on ${develop_sha}"
      break
    fi
    if [ "$conclusion" = "failure" ] || [ "$conclusion" = "cancelled" ]; then
      echo "error: '${REQUIRED_CHECK}' concluded '${conclusion}' on ${develop_sha} — not promoting." >&2
      exit 1
    fi
    if [ "$elapsed" -ge "$CI_TIMEOUT" ]; then
      echo "error: timed out after ${CI_TIMEOUT}s waiting for '${REQUIRED_CHECK}' on ${develop_sha} (last conclusion: '${conclusion:-<missing>}')." >&2
      exit 1
    fi
    sleep "$poll_interval"
    elapsed=$((elapsed + poll_interval))
  done
fi

# workflow_dispatch does not hand back a run id synchronously, so record
# whatever run is currently newest BEFORE triggering. Polling for "any run
# id" after triggering is not enough — promote-main.yml has run before, so
# the very first poll would immediately return that PRIOR, already-completed
# run, and `gh run watch` would return success instantly without ever
# checking the run we just fired. Instead poll until the newest run id
# actually CHANGES from the pre-trigger snapshot (run ids are monotonically
# increasing, so a changed top-of-list id is a new run).
prior_run_id="$(gh run list --repo "$REPO" --workflow promote-main.yml --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"

echo "==> triggering promote-main.yml on ${REPO}"
gh workflow run promote-main.yml --repo "$REPO" -f reason="$REASON"

echo "==> waiting for the new run to appear..."
run_id=""
for _ in $(seq 1 30); do
  candidate="$(gh run list --repo "$REPO" --workflow promote-main.yml --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ "$candidate" != "$prior_run_id" ]; then
    run_id="$candidate"
    break
  fi
  sleep 2
done
if [ -z "$run_id" ]; then
  echo "error: could not find the newly triggered promote-main.yml run (still seeing prior run ${prior_run_id:-<none>})" >&2
  exit 1
fi
echo "==> run id: ${run_id} — waiting for completion"
gh run watch "$run_id" --repo "$REPO" --exit-status

echo "==> promote succeeded, syncing deploy checkout at ${DEPLOY_DIR}"
"${DEPLOY_DIR}/scripts/deploy-sync.sh"

echo "==> verifying"
if [ -x "${DEPLOY_DIR}/.venv/bin/athenaeum" ]; then
  "${DEPLOY_DIR}/.venv/bin/athenaeum" --version
fi
if [ -f "${DEPLOY_DIR}/dist/.build-sha" ]; then
  echo "build-sha: $(cat "${DEPLOY_DIR}/dist/.build-sha")"
fi

cat <<'EOF'

==> Done. If the MCP server (Claude Code's `athenaeum` server) needs the
    update, restart the session — it re-execs the binary at
    <deploy-dir>/.venv/bin/athenaeum, which was just reinstalled above.
EOF
