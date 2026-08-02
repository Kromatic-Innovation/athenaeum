#!/usr/bin/env bash
# Deploy-sync + SHA stamp for athenaeum (issue athenaeum#413).
#
# WHAT: since cwc#1529 (2026-07-25), athenaeum DOES keep a separate
# main-pinned deploy worktree at ~/local-deploys/athenaeum (like hestia/
# voltaire's `<repo>-deploy` checkouts, guarded there by deploy-guard.sh; see
# the hestia#691 audit) — the dev tree at ~/Code/athenaeum is no longer what
# the MCP server or the nightly librarian execute. This script is the
# lighter-weight equivalent of deploy-guard.sh for that shape: fast-forward
# the deploy checkout to its deploy ref, reinstall the editable package (so
# new/changed dependencies and entry points actually take effect — a gap
# discovered 2026-07-25 when two promotions in a row required a manual
# `pip install -e` after this script because it only synced files), then
# stamp the running commit into `dist/.build-sha` via
# scripts/write_build_sha.py. The stamp lets the cross-repo deploy-lag
# aggregator (code-workspace-config#1428) answer "what commit is athenaeum
# actually running" by reading that one file.
#
# `dist/` is gitignored — the stamp is a local build artifact, never committed.
#
# USAGE:
#   scripts/deploy-sync.sh          fast-forward, reinstall, rewrite the stamp
#   scripts/deploy-sync.sh --check  print a decision (in-sync|drift|error), mutate nothing
#
# NOTE: this script only syncs the LOCAL deploy checkout. It does not
# promote develop -> main on GitHub — run `scripts/promote-and-deploy.sh`
# for the single command that does both, or trigger `promote-main.yml`
# first (GitHub Actions UI or `gh workflow run promote-main.yml -f reason=...`).
#
# TEST/CI HOOKS (offline determinism — never set in production):
#   ATHENAEUM_DEPLOY_DIR    repo root to sync/stamp (default: this script's `..`)
#   ATHENAEUM_DEPLOY_REF    deploy ref to track      (default: main)
#   ATHENAEUM_SYNC_FETCH=0  skip `git fetch` + fast-forward; stamp the checkout as-is
#   ATHENAEUM_SYNC_FF_CMD   fast-forward command (default: `git merge --ff-only origin/<ref>`)
#   ATHENAEUM_SYNC_REINSTALL=0  skip the `pip install -e` reinstall step
#   ATHENAEUM_DEPLOY_EXTRAS pip extras to install (default: mcp,vector — what
#                           the MCP server + librarian's vector search need)
#   ATHENAEUM_PYTHON        python interpreter for the stamp script (default: python3)
set -euo pipefail

_ds_script_dir() { ( cd "$(dirname "${BASH_SOURCE[0]}")" && pwd ); }
_ds_dir() {
  if [ -n "${ATHENAEUM_DEPLOY_DIR:-}" ]; then
    printf '%s' "$ATHENAEUM_DEPLOY_DIR"
  else
    ( cd "$(_ds_script_dir)/.." && pwd )
  fi
}
_ds_ref() { printf '%s' "${ATHENAEUM_DEPLOY_REF:-main}"; }
_ds_python() { printf '%s' "${ATHENAEUM_PYTHON:-python3}"; }

dir="$(_ds_dir)"
ref="$(_ds_ref)"

# --check: report drift without mutating anything.
if [ "${1:-}" = "--check" ]; then
  if [ ! -e "$dir/.git" ]; then
    echo "error: $dir is not a git checkout"
    exit 20
  fi
  head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
  stamped=""
  [ -f "$dir/dist/.build-sha" ] && stamped="$(tr -d '[:space:]' < "$dir/dist/.build-sha")"
  if [ -n "$stamped" ] && [ "$stamped" = "$head" ]; then
    echo "in-sync $head"
    exit 0
  fi
  echo "drift stamp=${stamped:-<none>} head=${head:-<unknown>}"
  exit 10
fi

if [ ! -e "$dir/.git" ]; then
  echo "athenaeum deploy-sync: $dir is not a git checkout" >&2
  exit 1
fi

if [ "${ATHENAEUM_SYNC_FETCH:-1}" != "0" ]; then
  git -C "$dir" fetch --quiet --no-tags origin "$ref"
  ff_cmd="${ATHENAEUM_SYNC_FF_CMD:-git merge --ff-only "origin/$ref"}"
  ( cd "$dir" && eval "$ff_cmd" )
fi

# Reinstall the editable package so new/changed dependencies and entry points
# actually take effect — a `git merge --ff-only` above only updates files on
# disk, it does not touch whatever the venv's site-packages already resolved
# at the last install. Skippable (ATHENAEUM_SYNC_REINSTALL=0) for callers that
# just want the fetch+stamp (e.g. --check-adjacent tooling, tests).
if [ "${ATHENAEUM_SYNC_REINSTALL:-1}" != "0" ]; then
  venv_pip="$dir/.venv/bin/pip"
  if [ -x "$venv_pip" ]; then
    extras="${ATHENAEUM_DEPLOY_EXTRAS:-mcp,vector}"
    ( cd "$dir" && "$venv_pip" install -q -e ".[${extras}]" )
  else
    echo "deploy-sync: no venv pip at $venv_pip — skipping reinstall" >&2
  fi
fi

# Stamp the deploy checkout ($dir) using the stamp script shipped alongside
# this one — in production they live in the same scripts/ dir; keeping them
# decoupled lets the sync stamp a checkout other than the one it ships from.
ATHENAEUM_BUILD_SHA_ROOT="$dir" "$(_ds_python)" "$(_ds_script_dir)/write_build_sha.py"
