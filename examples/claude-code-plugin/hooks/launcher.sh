#!/usr/bin/env bash
# launcher.sh -- runs the ONE pinned athenaeum release through `uv`, with no
# pip fallback, for the Claude Code plugin tree staged in this directory
# (issue athenaeum#2368). Two callers use this script:
#
#   1. ../.mcp.json's "athenaeum" server: `launcher.sh serve` -- passthrough
#      mode. Blocks (as `uv run` naturally does) if the pinned release has
#      never been materialized yet; that is a one-time cost the MCP
#      connection is allowed to wait on.
#   2. hooks.json's SessionStart entry: `launcher.sh --ensure-ready` --
#      never blocks. It reports which of three states hold (ready / already
#      being prepared by a prior invocation / just kicked off a background
#      prepare) and returns immediately either way, so a slow first-time
#      dependency resolution can never blow a hook's timeout budget.
#
# Deliberately POSIX-ish bash with (almost) no external-command dependency
# on its two tested paths (see tests/test_claude_code_plugin.py):
#   - the "uv missing" branch (AC3) returns before touching the filesystem
#     at all.
#   - the "someone else is already preparing" branch of --ensure-ready
#     (AC4) only tests `[ -d ... ]` and echoes -- both bash builtins.
# `mkdir`/`nohup` are only reached once uv is confirmed present AND this
# invocation is the one that wins the race to prepare, which keeps both
# acceptance-criteria tests free of any real `uv`/network dependency.
#
# No new ATHENAEUM_* environment variable is introduced here -- only
# ATHENAEUM_CACHE_DIR, already documented in docs/reference/configuration.md
# and read the same way examples/claude-code/session-start-recall.sh reads
# it, so scripts/check_env_docs.py's src/+examples/claude-code/ scan surface
# does not need to grow to cover this new script (it lives under
# examples/claude-code-plugin/, outside that scan).
set -u

# ---------------------------------------------------------------------------
# Resolve paths without `dirname`/`realpath` (keeps the uv-missing path free
# of any external command -- see header).
# ---------------------------------------------------------------------------
_script_dir="${0%/*}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${_script_dir}/..}"
MANIFEST="${PLUGIN_ROOT}/.claude-plugin/plugin.json"

# ---------------------------------------------------------------------------
# AC3: uv is a hard prerequisite, no pip fallback. Exit 0 either way -- a
# missing optional tool must not kill the session -- but ALWAYS name `uv`
# and what to do about it, never a silent no-op.
#
# docs/guides/install.md's own "What happens when a prerequisite is missing"
# section is the closest existing athenaeum prose on this exact failure
# ("If `uv` isn't on your PATH, the shell says so immediately (`command not
# found`) -- nothing subtle"); `athenaeum doctor` (src/athenaeum/doctor.py)
# has no uv-specific check to quote -- it checks the INSTALLED package,
# interpreter, MCP registration, hooks, key mode, index and schedule, never
# the presence of uv itself -- so this message is new wording consistent
# with that install guide's remediation (`uv tool install`), not a quote
# lifted from doctor.py. See README.md's "What we could not verify" note.
# ---------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "[athenaeum plugin] 'uv' was not found on PATH. This plugin requires uv to run the pinned athenaeum release -- there is no pip fallback here. Install it (see https://docs.astral.sh/uv/getting-started/installation/ or run: curl -LsSf https://astral.sh/uv/install.sh | sh), then restart this session." >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# Pin: read the SINGLE "version" key out of plugin.json with no jq/grep/sed
# dependency -- a plain bash parameter-expansion scan, matched against the
# FIRST line containing the literal substring "version" (plugin.json's own
# top-level "version" field is the first such line; see
# tests/test_claude_code_plugin.py::test_manifest_version_pin_matches_pyproject
# for the authoritative, real-JSON-parsed comparison against pyproject.toml).
# ---------------------------------------------------------------------------
PINNED_VERSION=""
if [ -f "$MANIFEST" ]; then
  while IFS= read -r _line; do
    case "$_line" in
      *'"version"'*)
        _rest="${_line#*\"version\"}"
        _rest="${_rest#*:}"
        _rest="${_rest#*\"}"
        PINNED_VERSION="${_rest%%\"*}"
        break
        ;;
    esac
  done < "$MANIFEST"
fi

CACHE_DIR="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}"
PLUGIN_CACHE_DIR="${CACHE_DIR}/claude-code-plugin"
READY_MARKER="${PLUGIN_CACHE_DIR}/ready-${PINNED_VERSION}"
LOCK_DIR="${PLUGIN_CACHE_DIR}/prepare.lock"
PREPARE_LOG="${PLUGIN_CACHE_DIR}/prepare.log"

_run_pinned() {
  exec uv run --python 3.13 --with "athenaeum==${PINNED_VERSION}" athenaeum "$@"
}

# ---------------------------------------------------------------------------
# Background worker -- invoked as `"$0" --prepare`, only by this script's
# own --ensure-ready branch below (never directly by a hook or .mcp.json).
# Owns the lock end-to-end: creates the ready marker on success, always
# clears the lock whether it succeeded or not (AC4's "first run while
# pending" state is exactly the window between lock-creation here and
# marker-creation below).
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--prepare" ]; then
  mkdir -p "$PLUGIN_CACHE_DIR" 2>/dev/null || true
  {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] prepare: start pin=${PINNED_VERSION}"
    if uv run --python 3.13 --with "athenaeum==${PINNED_VERSION}" athenaeum --version; then
      : > "$READY_MARKER"
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] prepare: ready"
    else
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] prepare: failed, will retry on next session"
    fi
  } >>"$PREPARE_LOG" 2>&1
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  exit 0
fi

# ---------------------------------------------------------------------------
# AC4: never-blocking hook mode.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--ensure-ready" ]; then
  if [ -f "$READY_MARKER" ]; then
    echo "[athenaeum plugin] athenaeum ${PINNED_VERSION} is ready."
    exit 0
  fi
  if [ -d "$LOCK_DIR" ]; then
    echo "[athenaeum plugin] still preparing athenaeum ${PINNED_VERSION} via uv (first run) -- recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
    exit 0
  fi
  mkdir -p "$PLUGIN_CACHE_DIR" 2>/dev/null || true
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    nohup "$0" --prepare >>"$PREPARE_LOG" 2>&1 &
    disown 2>/dev/null || true
    echo "[athenaeum plugin] still preparing athenaeum ${PINNED_VERSION} via uv (first run, just started) -- recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
  else
    # Lost the race to another concurrent --ensure-ready; same message.
    echo "[athenaeum plugin] still preparing athenaeum ${PINNED_VERSION} via uv (first run) -- recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Passthrough mode (.mcp.json's command): run the pinned release for real.
# `uv run` materializes the environment itself on a cold cache -- this is
# allowed to block, unlike --ensure-ready above.
# ---------------------------------------------------------------------------
_run_pinned "$@"
