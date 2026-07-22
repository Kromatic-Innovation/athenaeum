#!/usr/bin/env bash
# Deploy-sync + SHA stamp for athenaeum (issue #413).
#
# WHAT: athenaeum's MCP server runs from a single source checkout — there is no
# separate main-pinned deploy worktree (unlike hestia/voltaire, which each keep
# a `<repo>-deploy` worktree guarded by deploy-guard.sh; see the hestia#691
# audit). This is the lighter-weight equivalent for that single-checkout shape:
# fast-forward the running checkout to its deploy ref, then stamp the running
# commit into `dist/.build-sha` via scripts/write_build_sha.py. The stamp lets
# the cross-repo deploy-lag aggregator (code-workspace-config#1428) answer
# "what commit is athenaeum actually running" by reading that one file.
#
# `dist/` is gitignored — the stamp is a local build artifact, never committed.
#
# USAGE:
#   scripts/deploy-sync.sh          fast-forward to the deploy ref, rewrite the stamp
#   scripts/deploy-sync.sh --check  print a decision (in-sync|drift|error), mutate nothing
#
# TEST/CI HOOKS (offline determinism — never set in production):
#   ATHENAEUM_DEPLOY_DIR   repo root to sync/stamp (default: this script's `..`)
#   ATHENAEUM_DEPLOY_REF   deploy ref to track      (default: main)
#   ATHENAEUM_SYNC_FETCH=0 skip `git fetch` + fast-forward; stamp the checkout as-is
#   ATHENAEUM_SYNC_FF_CMD  fast-forward command (default: `git merge --ff-only origin/<ref>`)
#   ATHENAEUM_PYTHON       python interpreter for the stamp script (default: python3)
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

# Stamp the deploy checkout ($dir) using the stamp script shipped alongside
# this one — in production they live in the same scripts/ dir; keeping them
# decoupled lets the sync stamp a checkout other than the one it ships from.
ATHENAEUM_BUILD_SHA_ROOT="$dir" "$(_ds_python)" "$(_ds_script_dir)/write_build_sha.py"
