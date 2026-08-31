#!/usr/bin/env bash
# scripts/install-git-hooks.sh — wire this repo's tracked hooks directory
# in for the current clone (athenaeum#1104).
#
# Cloning a repo never installs hooks by itself; this is the one-time
# per-clone step that makes `.githooks/pre-push` (the public-safe-lint
# push-boundary gate) actually run. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

git -C "$REPO_ROOT" config core.hooksPath .githooks
chmod +x "$REPO_ROOT"/.githooks/* 2>/dev/null || true

echo "Installed: core.hooksPath -> .githooks"
echo "public-safe-lint now runs at the push boundary; see .githooks/pre-push for the bypass."

# Some containerized dev environments force core.hooksPath via
# GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n environment
# variables, which outrank the repo-local setting just written above --
# silently defeating it. Detect that here rather than let it fail silently
# on the next push.
EFFECTIVE_HOOKS_PATH="$(git -C "$REPO_ROOT" config --get core.hooksPath || true)"
if [ "$EFFECTIVE_HOOKS_PATH" != ".githooks" ]; then
  echo
  echo "WARNING: an ambient environment is overriding core.hooksPath to"
  echo "  '$EFFECTIVE_HOOKS_PATH' (env-based git config outranks this repo's own"
  echo "  setting). Plain 'git push' will NOT run the public-safe-lint gate here."
  echo "  Use scripts/git-push-safe.sh instead of 'git push' -- it forces the"
  echo "  correct hooksPath via 'git -c', which outranks even that override."
fi
