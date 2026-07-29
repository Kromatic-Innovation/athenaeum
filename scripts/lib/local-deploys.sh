#!/usr/bin/env bash
# local-deploys.sh — resolve an Internal Platform tool's live deploy-checkout
# directory (cwc#1459, split out of cwc#1422).
#
# WHY: the 6 live `*-deploy` checkouts currently sit as flat siblings under
# ~/Code (e.g. ~/Code/code-workspace-config-deploy). cwc#1422 co-locates them
# under a single $LOCAL_DEPLOYS_DIR root (default ~/local-deploys), OUTSIDE
# ~/Code, and drops the `-deploy` suffix in the new location. This helper is the
# one place that resolves "where does <repo>'s deploy checkout live", so every
# cwc script agrees and the machine migration (cwc#1422) can flip the whole
# workspace by setting a single env var.
#
# BACKWARD COMPATIBLE BY DESIGN: with $LOCAL_DEPLOYS_DIR UNSET (every machine
# that has not yet run the cwc#1422 migration) this resolves to the exact
# literal ~/Code/<repo>-deploy path used today — so this can land, auto-promote,
# and run live with ZERO behavior change before the migration happens. Once the
# migration sets $LOCAL_DEPLOYS_DIR in .zshrc and moves the directories, the
# same scripts pick up the new location automatically.
#
#   local_deploy_dir <repo-name>
#     $LOCAL_DEPLOYS_DIR set   -> $LOCAL_DEPLOYS_DIR/<repo-name>   (new home; no -deploy suffix)
#     $LOCAL_DEPLOYS_DIR unset -> $HOME/Code/<repo-name>-deploy    (today's literal path)
#
# A per-script explicit override (e.g. CWC_DEPLOY_DIR) still takes precedence and
# is the caller's responsibility to check BEFORE calling this.

# Resolve a repo's deploy-checkout directory. Echoes the path; no trailing slash.
local_deploy_dir() {
  local repo="$1"
  if [ -z "$repo" ]; then
    echo "local_deploy_dir: repo name required" >&2
    return 2
  fi
  if [ -n "${LOCAL_DEPLOYS_DIR:-}" ]; then
    # Strip any trailing slash from $LOCAL_DEPLOYS_DIR so the join is clean.
    printf '%s' "${LOCAL_DEPLOYS_DIR%/}/${repo}"
  else
    printf '%s' "$HOME/Code/${repo}-deploy"
  fi
}
