#!/usr/bin/env bash
# scripts/git-push-safe.sh — `git push` wrapper that guarantees the
# public-safe-lint push-boundary gate (.githooks/pre-push, athenaeum#1104)
# actually runs, even when an ambient environment forces core.hooksPath
# elsewhere.
#
# Some containerized dev environments this repo is developed from set
# GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n environment
# variables that pin `core.hooksPath` workspace-wide, for that
# environment's own unrelated hook needs. Git's environment-based config
# overrides outrank a repo's own committed `.git/config` setting (i.e.
# `git config core.hooksPath .githooks`, as installed by
# scripts/install-git-hooks.sh) -- so in that environment, the gate can be
# silently skipped even though the repo config looks correct.
# `git -c core.hooksPath=...` (command-line config) outranks even that
# environment override, so this wrapper is the reliable path in exactly
# that situation. On a plain host with no such override it behaves
# identically to `git push`.
#
# Usage: scripts/git-push-safe.sh [git push args...]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec git -C "$REPO_ROOT" -c core.hooksPath=.githooks push "$@"
