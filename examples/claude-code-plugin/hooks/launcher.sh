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
#      never blocks. It reports which state holds (ready / a LIVE prior
#      prepare still running / a STALE prior prepare that died and is being
#      restarted / one just kicked off) and returns immediately either way,
#      so a slow first-time dependency resolution can never blow a hook's
#      timeout budget. "Stale" (PR athenaeum#2575 review) means the lock
#      directory survived a worker that never reached its own cleanup --
#      SIGKILL, the session closing, a reboot mid-build -- which otherwise
#      wedges every future session in "still preparing" forever with no
#      self-recovery. See `_lock_is_live` below.
#
# Deliberately POSIX-ish bash with (almost) no external-command dependency
# on its two tested paths (see tests/test_claude_code_plugin.py):
#   - the "uv missing" branch (AC3) returns before touching the filesystem
#     at all.
#   - the "someone else is already preparing" branch of --ensure-ready
#     (AC4) only tests `[ -d ... ]`, `date +%s`, `kill -0` and echoes --
#     no real `uv` invocation.
# `mkdir`/`nohup` are only reached once uv is confirmed present AND this
# invocation is the one that wins the race to prepare, which keeps the
# uv-absent acceptance-criteria test free of any real `uv`/network
# dependency.
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
  _uv_msg="[athenaeum plugin] 'uv' was not found on PATH. This plugin requires uv to run the pinned athenaeum release -- there is no pip fallback here. Install it (see https://docs.astral.sh/uv/getting-started/installation/ or run: curl -LsSf https://astral.sh/uv/install.sh | sh), then restart this session."
  if [ "${1:-}" = "--ensure-ready" ]; then
    # SessionStart hook: stdout is the ONLY channel the user and the model
    # both see (stderr only reaches a debug log -- athenaeum#3046). Emit hook
    # JSON: `systemMessage` shows to the user, `additionalContext` goes to the
    # model. The message has no double quotes or backslashes, so it is
    # JSON-safe as written.
    echo "{\"systemMessage\": \"${_uv_msg}\", \"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\", \"additionalContext\": \"${_uv_msg}\"}}"
  else
    # MCP passthrough: stdout is the protocol channel, so use stderr.
    echo "$_uv_msg" >&2
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Pin: read the SINGLE "version" key out of plugin.json with no jq/grep/sed
# dependency -- a plain bash parameter-expansion scan, matched against the
# FIRST line containing the literal substring "version" (plugin.json's own
# top-level "version" field is the first such line; see
# tests/test_claude_code_plugin.py::test_manifest_version_pin_matches_latest_release
# for the authoritative, real-JSON-parsed comparison against the latest
# released version in CHANGELOG.md).
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
READY_MARKER="${PLUGIN_CACHE_DIR}/ready-mcp-${PINNED_VERSION}"
LOCK_DIR="${PLUGIN_CACHE_DIR}/prepare.lock"
PREPARE_LOG="${PLUGIN_CACHE_DIR}/prepare.log"

#: Generous ceiling on a cold `uv` dependency build -- a first-time pull on
#: a slow link is minutes, not seconds, so this is picked with headroom
#: rather than tight (see README.md, "Recovering from an interrupted first
#: run"). Past this age a lock is reclaimed even if its recorded PID still
#: happens to resolve to SOME running process (PIDs get reused).
STALE_LOCK_TIMEOUT_SECONDS=1800

#: The pinned requirement. The `[mcp]` extra is what puts `fastmcp` in the
#: environment; without it `athenaeum serve` dies on import (athenaeum#3046).
PINNED_SPEC="athenaeum[mcp]==${PINNED_VERSION}"

#: Default matches `athenaeum serve --path` / `athenaeum init --path`.
DEFAULT_KNOWLEDGE_PATH="~/knowledge"

_run_pinned() {
  exec uv run --python 3.13 --with "$PINNED_SPEC" athenaeum "$@"
}

# ---------------------------------------------------------------------------
# MCP passthrough only: make sure the knowledge directory exists before
# `serve`, by calling `athenaeum init` (no layout re-implemented in shell;
# init is non-interactive). Runs synchronously inside the MCP start so a
# first session never depends on the SessionStart warm-up having finished --
# Claude Code caches a failed first MCP connect for ~15 minutes, so a
# start-up race must not be able to fail it (athenaeum#3046). `init` output
# goes to stderr: stdout is the MCP protocol channel.
# ---------------------------------------------------------------------------
_ensure_knowledge_dir() {
  _ek_path="$DEFAULT_KNOWLEDGE_PATH"
  _ek_prev=""
  for _ek_arg in "$@"; do
    case "$_ek_prev" in
      --path) _ek_path="$_ek_arg" ;;
    esac
    case "$_ek_arg" in
      --path=*) _ek_path="${_ek_arg#--path=}" ;;
    esac
    _ek_prev="$_ek_arg"
  done
  # An empty value or an unsubstituted `${user_config...}` token means the
  # option was not supplied: fall back to the default.
  case "$_ek_path" in
    '' | *'${'*) _ek_path="$DEFAULT_KNOWLEDGE_PATH" ;;
  esac
  case "$_ek_path" in
    '~') _ek_dir="$HOME" ;;
    '~/'*) _ek_dir="${HOME}/${_ek_path#\~/}" ;;
    *) _ek_dir="$_ek_path" ;;
  esac
  if [ ! -d "$_ek_dir" ]; then
    echo "[athenaeum plugin] knowledge directory ${_ek_dir} is missing -- running athenaeum init." >&2
    uv run --python 3.13 --with "$PINNED_SPEC" athenaeum init --path "$_ek_dir" >&2 || true
  fi
}

# ---------------------------------------------------------------------------
# Liveness check for an existing $LOCK_DIR (issue athenaeum#2368 follow-up,
# PR athenaeum#2575 review). A worker that dies before reaching its own
# `rm -rf "$LOCK_DIR"` -- SIGKILL, the session closing, a reboot mid-build
# -- must not wedge this lock forever; every future --ensure-ready would
# otherwise read the bare `[ -d "$LOCK_DIR" ]` as "still preparing" with no
# self-recovery. "Live" requires BOTH a readable, currently-running PID AND
# a recorded start time inside the timeout above -- any missing/unreadable
# liveness datum (the worker died before writing it, or a reboot wiped
# /proc but left the directory on a persistent cache) reads as STALE, never
# as live by default. `kill -0`/`date +%s` only -- no GNU-only `date`
# flags -- this runs on an operator's Mac, not in CI.
# ---------------------------------------------------------------------------
_lock_is_live() {
  _ll_dir="$1"
  _ll_now=$(date +%s 2>/dev/null) || return 1

  _ll_started=""
  [ -f "${_ll_dir}/started-at" ] && _ll_started=$(cat "${_ll_dir}/started-at" 2>/dev/null)
  case "$_ll_started" in
    '' | *[!0-9]*) return 1 ;; # missing/unreadable/non-numeric -- stale, not live
  esac
  if [ "$(( _ll_now - _ll_started ))" -gt "$STALE_LOCK_TIMEOUT_SECONDS" ]; then
    return 1 # too old, regardless of what the pid file says
  fi

  _ll_pid=""
  [ -f "${_ll_dir}/pid" ] && _ll_pid=$(cat "${_ll_dir}/pid" 2>/dev/null)
  case "$_ll_pid" in
    '' | *[!0-9]*) return 1 ;; # missing/unreadable/non-numeric -- stale, not live
  esac
  kill -0 "$_ll_pid" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Background worker -- invoked as `"$0" --prepare`, only by this script's
# own --ensure-ready branch below (never directly by a hook or .mcp.json).
# Owns the lock end-to-end ON THE NORMAL PATH: creates the ready marker on
# success, always clears the lock whether it succeeded or not (AC4's "first
# run while pending" state is exactly the window between lock-creation in
# --ensure-ready and marker-creation below). If this process is killed
# before reaching its own `rm -rf "$LOCK_DIR"` below, the lock is instead
# reclaimed by a LATER --ensure-ready's `_lock_is_live` staleness check --
# this function does not need to defend against its own death, only do the
# right thing when it runs to completion.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--prepare" ]; then
  mkdir -p "$PLUGIN_CACHE_DIR" 2>/dev/null || true
  {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] prepare: start pin=${PINNED_VERSION}"
    if uv run --python 3.13 --with "$PINNED_SPEC" athenaeum --version; then
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

  _restarted_after_stale=0
  if [ -d "$LOCK_DIR" ]; then
    if _lock_is_live "$LOCK_DIR"; then
      echo "[athenaeum plugin] still preparing athenaeum ${PINNED_VERSION} via uv (first run) -- recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
      exit 0
    fi
    # Stale: the worker that held this lock is gone (dead PID, missing
    # liveness data, or past STALE_LOCK_TIMEOUT_SECONDS) without ever
    # reaching its own cleanup. Reclaim and restart rather than leaving a
    # future session reading "still preparing" forever.
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    _restarted_after_stale=1
  fi

  mkdir -p "$PLUGIN_CACHE_DIR" 2>/dev/null || true
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    date +%s > "${LOCK_DIR}/started-at" 2>/dev/null || true
    nohup "$0" --prepare >>"$PREPARE_LOG" 2>&1 &
    _worker_pid=$!
    echo "$_worker_pid" > "${LOCK_DIR}/pid" 2>/dev/null || true
    disown 2>/dev/null || true
    if [ "$_restarted_after_stale" = "1" ]; then
      echo "[athenaeum plugin] previous attempt to prepare athenaeum ${PINNED_VERSION} via uv appears to have died without finishing -- restarting it. recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
    else
      echo "[athenaeum plugin] still preparing athenaeum ${PINNED_VERSION} via uv (first run, just started) -- recall runs in reduced/FTS5-only mode until this finishes; see ${PREPARE_LOG}."
    fi
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
if [ "${1:-}" = "serve" ]; then
  _ensure_knowledge_dir "$@"
fi
_run_pinned "$@"
