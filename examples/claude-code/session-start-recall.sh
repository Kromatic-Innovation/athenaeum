#!/usr/bin/env bash
# SessionStart hook: read config, optionally bootstrap ANTHROPIC_API_KEY
# from 1Password, and build the search index.
#
# 1. Reads athenaeum.yaml for config (auto_recall, search_backend)
# 2. Writes a shell-readable cache at ~/.cache/athenaeum/config.env
# 3. Builds the FTS5 index synchronously (cheap, ~1s/3k pages).
# 4. Decides whether the EXPENSIVE work -- a vector index (re)build and/or
#    an `op` credential fetch -- is needed, and if so forks it as a
#    DETACHED background job behind a lock (issue athenaeum#2204 AC3). This
#    hook's own foreground path never runs either of them and never waits
#    on the background job (AC4): the next turn, or the next session, uses
#    whatever index is already on disk.
#
# Issue athenaeum#2204: every foreground step below is bound by WHATEVER'S
# LEFT of `ATHENAEUM_HOOK_BUDGET_SECONDS` (AC1/AC2) rather than an
# independent fixed timeout -- see the "Foreground budget" section. That is
# also why the credential fetch and the vector rebuild moved out of the
# foreground entirely rather than merely being clamped: two `op read` calls
# at up to `ATHENAEUM_OP_TIMEOUT_SECONDS` each, plus a ~45s vector rebuild,
# is precisely the arithmetic that used to exceed a 15s host timeout
# (`settings-snippet.json` shipped `"timeout": 15` while this header
# recommended 60 -- AC5 fixed that disagreement by raising the snippet to
# 60, matching the recommendation below). Removing those two from the timed
# path is a strict superset of clamping them: nothing that remains in the
# foreground section can blow the budget, by construction, not merely by a
# tighter bound.
#
# Issue athenaeum#1120 (AC2 — checked, unaffected): this hook writes
# stderr diagnostics only and emits no `hookSpecificOutput`/
# `additionalContext` at all, so it is not an unprompted-push path — it has
# no `hot`-tier gap and no push-budget gap to route. It IS the writer for
# the FTS5 index (`athenaeum.search.build_fts5_index`) and for
# `PUSH_TOKEN_BUDGET` in config.env, both of which
# `user-prompt-recall.sh` (the actual unprompted-push hook) reads. The
# AC9 once-per-session degradation notice (issue athenaeum#2204) is the
# one exception to "no hookSpecificOutput": that notice is surfaced by
# whichever hook calls `athenaeum.hook_status.maybe_session_notice` FIRST
# for a session, and in practice that is almost always the packaged
# `athenaeum-claude-hook` adapter (`user-prompt-recall.sh`'s target), which
# fires on the very first user turn -- this script never reads stdin and
# so never attempts the notice itself, by design (see that module's
# docstring for why a single shared gate across hooks still satisfies
# "at most once per session" either way).
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/session-start-recall.sh",
#         "timeout": 60
#       }]
#     }]
#   }
#
# Environment variables:
#   KNOWLEDGE_ROOT       Path to knowledge directory (default: ~/knowledge)
#   KNOWLEDGE_WIKI_PATH  Path to wiki directory (default: $KNOWLEDGE_ROOT/wiki)
#   ATHENAEUM_PYTHON     Python interpreter with athenaeum deps
#   ATHENAEUM_SRC        Path to athenaeum source checkout (optional)
#   ATHENAEUM_HOOK_BUDGET_SECONDS  Whole-second foreground budget shared by
#                                 every step below (default: 10, issue
#                                 athenaeum#2204). Each step is bound by
#                                 whatever's left of this budget, not by its
#                                 own fixed timeout; a step that would start
#                                 after the budget is already spent is
#                                 skipped and recorded as a `step_timeout`
#                                 degradation (see "athenaeum status").
#   ATHENAEUM_OP_KEY_PATH  1Password path (default: op://Agent Tools/Anthropic API Key/credential)
#   ATHENAEUM_OP_JEV_KEY_PATH  1Password path for JEV_API_KEY (default: op://Agent Tools/Jev Athenaeum Key/credential)
#   ATHENAEUM_OP_INTERACTIVE      1 = opt into 1Password's interactive desktop-app
#                                 integration: `op` may run without a service-account
#                                 token, and the non-interactive suppression flags
#                                 below are NOT set (default: unset, athenaeum#2193)
#   ATHENAEUM_OP_REFRESH          1 = re-fetch from `op` even when a cached line for
#                                 that credential already exists (default: unset,
#                                 athenaeum#2193)
#   ATHENAEUM_OP_TIMEOUT_SECONDS  Bound, in WHOLE seconds, on a single `op read`
#                                 (default: 10, athenaeum#2193). Must be a
#                                 positive integer -- a fractional, non-numeric,
#                                 empty, or zero value silently falls back to
#                                 the default rather than failing the fetch.
#                                 Applies inside the DETACHED background job
#                                 (issue athenaeum#2204) -- it no longer
#                                 bounds anything in this script's own
#                                 foreground path.
#   ATHENAEUM_FORCE_REBUILD       1 = force a vector-index rebuild even if the
#                                 existing one is fresher than the wiki.

set -euo pipefail

# ── Kill switch (issue athenaeum#379) ───────────────────────────────────────────────
# Honour ~/.cache/athenaeum/disabled (+ ATHENAEUM_DISABLED). Mirrors
# athenaeum.killswitch.is_disabled("recall"): the "all" scope no-ops every
# hook; the "compile" scope leaves recall on. Costs no Python startup.
__athenaeum_recall_disabled() {
  # Normalize like killswitch._env_scope()'s `raw.strip().lower()`. `read`
  # with default IFS strips leading/trailing whitespace while preserving
  # internal (so `tr ue` stays unrecognised, matching Python). The case
  # patterns fold case explicitly: `${_val,,}` is bash 4.0+ and stock macOS
  # ships bash 3.2.57 -- see the athenaeum#1104 / athenaeum#1343 precedents
  # in user-prompt-recall.sh. Both constructs are fork-free.
  local _val=""
  read -r _val <<< "${ATHENAEUM_DISABLED:-}" || true
  case "$_val" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn] | [Aa][Ll][Ll]) return 0 ;;
    [Cc][Oo][Mm][Pp][Ii][Ll][Ee]) return 1 ;;
  esac
  local f="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}/disabled"
  [ -f "$f" ] || return 1
  grep -Eq '"scope"[[:space:]]*:[[:space:]]*"compile"|^[[:space:]]*compile[[:space:]]*$' "$f" 2>/dev/null && return 1
  return 0
}
__athenaeum_recall_disabled && exit 0

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$KNOWLEDGE_ROOT/wiki}"
CACHE_DIR="${HOME}/.cache/athenaeum"
CONFIG_ENV="${CACHE_DIR}/config.env"
PYTHON="${ATHENAEUM_PYTHON:-python3}"

[ -d "$WIKI_ROOT" ] || exit 0

# Cache dir holds ANTHROPIC_API_KEY in config.env. Restrict to owner-only
# before writing anything, and set umask so new files inherit mode 600.
# Prevents a brief window where a freshly-written key is world-readable.
mkdir -p "$CACHE_DIR"
chmod 700 "$CACHE_DIR"
umask 077

# ── Background-build lock + log (issue athenaeum#2204 AC3) ─────────────────
# The SAME lockfile `rebuild-index.sh` already uses: a session-start
# background build and a SessionEnd `rebuild-index.sh` run mutually exclude
# each other too, not just two session-start invocations racing each other.
LOCK_DIR="${CACHE_DIR}/rebuild.lock"
REBUILD_LOG="${CACHE_DIR}/rebuild.log"

# ── Foreground budget (issue athenaeum#2204 AC1/AC2) ────────────────────────
# ONE shared budget for every step this script still runs synchronously.
# Mirrors `athenaeum.config.resolve_hook_budget_seconds`'s exact precedence
# and fallback literal (env > default 10) -- kept as a bash-native parse
# rather than a python call so this script never pays a python startup just
# to learn its own budget; `tests/test_hook_budget_2204.py` pins the two
# literals equal.
HOOK_BUDGET_SECONDS="${ATHENAEUM_HOOK_BUDGET_SECONDS:-10}"
case "$HOOK_BUDGET_SECONDS" in
  ''|*[!0-9]*|0) HOOK_BUDGET_SECONDS=10 ;;
esac
_athenaeum_deadline_epoch=$(( $(date +%s) + HOOK_BUDGET_SECONDS ))
# Test-only seam (issue athenaeum#2204): deterministically exhaust the
# foreground budget without a real multi-second wait, by backdating the
# deadline into the past -- `0` is not usable for this (it means "use the
# default" per the normalize guard just above, matching the python
# resolver's own `> 0` guard). Never documented as a public knob; read
# nowhere else in this script.
if [ -n "${ATHENAEUM_TEST_BUDGET_ALREADY_SPENT:-}" ]; then
  _athenaeum_deadline_epoch=0
fi

# _athenaeum_remaining_seconds: whole seconds left before the foreground
# budget expires, floored at 0 -- never negative, since a `timeout 0`
# argument means "no timeout at all" to GNU coreutils, the opposite of what
# is wanted once the budget is spent.
_athenaeum_remaining_seconds() {
  local _now _remaining
  _now=$(date +%s)
  _remaining=$(( _athenaeum_deadline_epoch - _now ))
  if [ "$_remaining" -lt 1 ]; then
    echo 0
  else
    echo "$_remaining"
  fi
}

# ── Resolve a timeout runner once (not once per step): prefer GNU/BSD
# `timeout`, then macOS's `gtimeout` (coreutils via Homebrew). When neither
# is on PATH, `_athenaeum_run_budgeted` below falls back to running the step
# unbounded (best effort, same posture `_athenaeum_op_read`'s own portable
# fallback documents for the one case it still needs to handle itself).
_athenaeum_op_timeout_bin=""
if command -v timeout >/dev/null 2>&1; then
  _athenaeum_op_timeout_bin="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  _athenaeum_op_timeout_bin="gtimeout"
fi

# _athenaeum_record_degradation <event> <detail>
# Shells out to `athenaeum.hook_status.record_degradation` -- this script's
# only way to reach that module. Best-effort: a status-file write must
# never fail this hook, matching every other fail-open contract here.
_athenaeum_record_degradation() {
  local _event="$1" _detail="$2"
  "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from pathlib import Path
from athenaeum.hook_status import record_degradation
record_degradation(Path(sys.argv[1]), 'session-start', sys.argv[2], sys.argv[3])
" "$CACHE_DIR" "$_event" "$_detail" 2>/dev/null || true
}

# _athenaeum_run_budgeted <step-name> <cmd...>
# Runs <cmd...> bound by WHATEVER'S LEFT of the foreground budget (issue
# athenaeum#2204 AC2) -- never an independent fixed timeout for this one
# step, which is exactly the shape that let two separate `ATHENAEUM_OP_
# TIMEOUT_SECONDS`-bounded `op read` calls sum past a 15s host timeout
# before this issue. Records a `step_timeout` degradation and returns the
# sentinel 75 (EX_TEMPFAIL -- deliberately NOT 1, so a caller can tell "the
# budget was already spent, nothing ran" apart from <cmd...>'s own normal
# exit code 1) without running anything once the budget is already
# exhausted. If <cmd...> itself overruns the remaining time, this also
# records a `step_timeout` degradation and returns the timeout runner's own
# 124 -- both sentinels (75, 124) are the "tolerate and continue" codes the
# FTS5-build call site below checks for; any OTHER non-zero is <cmd...>'s
# genuine own failure and is returned completely unchanged (issue
# athenaeum#1826 AC1: a genuine build failure must still be loud).
_athenaeum_run_budgeted() {
  local _step="$1"; shift
  local _remaining
  _remaining=$(_athenaeum_remaining_seconds)
  if [ "$_remaining" -eq 0 ]; then
    _athenaeum_record_degradation step_timeout "${_step}: skipped, foreground budget exhausted"
    return 75
  fi
  local _status
  if [ -n "$_athenaeum_op_timeout_bin" ]; then
    "$_athenaeum_op_timeout_bin" "$_remaining" "$@"
    _status=$?
  else
    "$@"
    _status=$?
  fi
  if [ "$_status" -eq 124 ]; then
    _athenaeum_record_degradation step_timeout "${_step}: exceeded the remaining foreground budget (${_remaining}s)"
  fi
  return "$_status"
}

# ── Non-interactive, bounded `op read` (athenaeum#2193) ─────────────────────
# `op` probes the 1Password desktop app's group container on every call
# unless OP_LOAD_DESKTOP_APP_SETTINGS=false; on macOS that is the App Data
# TCC service and the probe can block indefinitely. Issue athenaeum#2204
# moved every caller of this function into the DETACHED background job
# (see `_athenaeum_run_background_build` below) -- a hung `op` can no
# longer turn an interactive SessionStart hook into a stuck OS permission
# prompt, because nothing in the host-timed foreground path calls it
# anymore. `ATHENAEUM_OP_TIMEOUT_SECONDS` still bounds a single `op read`
# exactly as before; it is simply no longer part of this script's own
# budget arithmetic.
# OP_BIOMETRIC_UNLOCK_ENABLED=false suppresses the matching biometric-unlock
# prompt. Both flags are scoped to the single `op` subprocess via `env`,
# NEVER `export`ed process-wide -- everything else this hook spawns (the
# Python index build, etc.) must be unaffected. Skipped entirely when
# ATHENAEUM_OP_INTERACTIVE=1: that is an explicit opt-in to 1Password's
# interactive desktop-app integration, where these flags would be
# counterproductive.
#
# _athenaeum_op_read <op-path>
# Writes the fetched value to stdout, nothing else; returns non-zero on any
# failure or timeout. No stderr output of its own -- the caller's existing
# `if _fetched_key="$(...)" && [ -n "$_fetched_key" ]` guard already treats
# a non-zero return (or empty stdout) as "leave the cached line alone", and
# that silent-failure shape is itself part of the athenaeum#2056 contract
# ("missing Jev item produces no line and no warning").
_athenaeum_op_read() {
  local _op_path="$1"
  # Normalise the budget BEFORE either branch below spawns `op` (Seer
  # finding on athenaeum#2193): a non-integer value (fractional,
  # non-numeric, or empty-but-set) used to survive past this point because
  # GNU `timeout` accepts a fractional argument on the branch below, then
  # break the portable fallback's pure-integer arithmetic AFTER `op` was
  # already running -- either the subshell aborts and orphans `op` (leaking
  # its mode-600 temp file, since the `rm -f` never runs) or
  # `_op_budget_ds` ends up empty and the bounded-wait loop's `-ge` test
  # errors every iteration, is read as false, and never times out -- the
  # exact unbounded hang this issue exists to prevent. A malformed knob
  # must not be able to hang the hook or skip the credential, so anything
  # that isn't a positive integer silently falls back to the documented
  # default of 10 -- no stderr here, this hook's contract in this area is
  # silence (athenaeum#2056).
  local _op_budget="${ATHENAEUM_OP_TIMEOUT_SECONDS:-10}"
  case "$_op_budget" in
    ''|*[!0-9]*|0) _op_budget=10 ;;
  esac
  local _op_cmd

  if [ "${ATHENAEUM_OP_INTERACTIVE:-}" = "1" ]; then
    _op_cmd=(op read "$_op_path")
  else
    _op_cmd=(env OP_LOAD_DESKTOP_APP_SETTINGS=false OP_BIOMETRIC_UNLOCK_ENABLED=false op read "$_op_path")
  fi

  if [ -n "$_athenaeum_op_timeout_bin" ]; then
    "$_athenaeum_op_timeout_bin" "$_op_budget" "${_op_cmd[@]}" 2>/dev/null
    return $?
  fi

  # Portable fallback (no `timeout`/`gtimeout` on PATH): background + poll
  # + kill. The fetched value is a credential, so its temp file lives in
  # $CACHE_DIR (already restricted to 700) at mode 600, mktemp'd like the
  # config.env tmp handling above -- never a world-readable /tmp path. Poll
  # in deciseconds: bash has no floating point, and 1-second polling would
  # make a short-lived fetch wait up to a full second longer than needed.
  local _op_tmp
  _op_tmp=$(mktemp "${CACHE_DIR}/op-read.XXXXXX")
  chmod 600 "$_op_tmp"
  "${_op_cmd[@]}" > "$_op_tmp" 2>/dev/null &
  local _op_pid=$!
  local _op_budget_ds=$((_op_budget * 10))
  local _op_waited_ds=0
  while kill -0 "$_op_pid" 2>/dev/null; do
    if [ "$_op_waited_ds" -ge "$_op_budget_ds" ]; then
      kill "$_op_pid" 2>/dev/null || true
      sleep 0.2
      kill -9 "$_op_pid" 2>/dev/null || true
      wait "$_op_pid" 2>/dev/null || true
      rm -f "$_op_tmp"
      return 1
    fi
    sleep 0.1
    _op_waited_ds=$((_op_waited_ds + 1))
  done

  local _op_status=0
  wait "$_op_pid" 2>/dev/null || _op_status=$?
  if [ "$_op_status" -eq 0 ]; then
    cat "$_op_tmp"
    rm -f "$_op_tmp"
    return 0
  fi
  rm -f "$_op_tmp"
  return 1
}

# _athenaeum_vector_stale: exit 0 (true/stale) if the vector index is
# MISSING, older than the newest wiki page, or ATHENAEUM_FORCE_REBUILD=1;
# exit 1 (false/fresh) otherwise. Shared by the foreground "is a background
# build worth starting at all" decision and the background job's own
# "actually spend the ~45s" gate, so the two can never disagree.
_athenaeum_vector_stale() {
  local _vector_dir="${CACHE_DIR}/wiki-vectors"
  if [ "${ATHENAEUM_FORCE_REBUILD:-0}" = "1" ]; then
    return 0
  fi
  if [ ! -d "$_vector_dir" ]; then
    return 0
  fi
  # Portable mtime: GNU coreutils takes `stat -c %Y`, BSD/macOS takes
  # `stat -f %m`. GNU reads `-f` as --file-system, so the BSD form alone
  # never yields a number on Linux and the index always looks stale.
  local _stat_mtime
  if stat -c %Y / >/dev/null 2>&1; then
    _stat_mtime=(stat -c %Y)
  else
    _stat_mtime=(stat -f %m)
  fi
  local _idx_mtime _wiki_mtime
  _idx_mtime=$(find "$_vector_dir" -type f -print0 2>/dev/null \
    | xargs -0 "${_stat_mtime[@]}" 2>/dev/null \
    | sort -n | tail -1 || echo 0)
  _wiki_mtime=$(find "$WIKI_ROOT" -type f -name '*.md' -print0 2>/dev/null \
    | xargs -0 "${_stat_mtime[@]}" 2>/dev/null \
    | sort -n | tail -1 || echo 0)
  _idx_mtime="${_idx_mtime:-0}"
  _wiki_mtime="${_wiki_mtime:-0}"
  if [ "$_idx_mtime" -gt "$_wiki_mtime" ] && [ "$_idx_mtime" -gt 0 ]; then
    return 1
  fi
  return 0
}

# _athenaeum_run_background_build: the DETACHED worker (issue athenaeum#2204
# AC3/AC4), invoked only via `"$0" --run-background-build` (see the dispatch
# check below) and only ever forked, never awaited, by the foreground
# section further down. This function -- not its caller -- owns the lock:
# the foreground section just forks it and moves on, so a session/turn that
# starts while a build is in flight never blocks on anything here (AC4). Two
# concurrent sessions can each fork this function; only one of those two
# invocations wins the `mkdir` below, and the loser returns immediately
# having done nothing (AC3) -- exactly `rebuild-index.sh`'s existing,
# already-tested lock contract, reused rather than reinvented, against the
# SAME lockfile so the two entry points exclude each other too.
_athenaeum_run_background_build() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    local _lock_age=0
    local _ctime
    if _ctime="$(stat -f %c "$LOCK_DIR" 2>/dev/null)"; then
      _lock_age=$(( $(date +%s) - _ctime ))
    elif _ctime="$(stat -c %Y "$LOCK_DIR" 2>/dev/null)"; then
      _lock_age=$(( $(date +%s) - _ctime ))
    fi
    if [ "$_lock_age" -gt 3600 ]; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: stale lock (${_lock_age}s old), reclaiming" >> "$REBUILD_LOG"
      rm -rf "$LOCK_DIR"
      if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: lost the race reclaiming a stale lock, skipping" >> "$REBUILD_LOG"
        return 0
      fi
    else
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: another build in progress (lock ${_lock_age}s old), skipping" >> "$REBUILD_LOG"
      return 0
    fi
  fi
  trap 'rm -rf "$LOCK_DIR"' EXIT

  # Re-derive what the foreground invocation already resolved and cached to
  # config.env -- this is a fresh process (`"$0" --run-background-build`),
  # so nothing survives except the real environment.
  if [ -f "$CONFIG_ENV" ]; then
    # shellcheck disable=SC1090
    source "$CONFIG_ENV"
  fi

  {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: start backend=${SEARCH_BACKEND:-vector}"

    # -- credential refresh (issue athenaeum#2056's table, moved here by
    # issue athenaeum#2204) ------------------------------------------------
    if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || [ "${ATHENAEUM_OP_INTERACTIVE:-}" = "1" ]; then
      if command -v op >/dev/null 2>&1; then
        local _i=0
        while [ "$_i" -lt "$_cred_count" ]; do
          local _name="${_CRED_ENV_NAMES[$_i]}"
          local _default_path="${_CRED_DEFAULT_PATHS[$_i]}"
          local _override_var="${_CRED_OVERRIDE_VARS[$_i]}"
          local _override_val="${!_override_var:-}"
          local _key_path="${_override_val:-$_default_path}"
          local _existing_line=""
          if [ -f "$CONFIG_ENV" ]; then
            _existing_line="$(grep "^${_name}=" "$CONFIG_ENV" 2>/dev/null | tail -n 1 || true)"
          fi
          local _needs_fetch=false
          case "$_existing_line" in
            "${_name}="?*) [ "${ATHENAEUM_OP_REFRESH:-}" = "1" ] && _needs_fetch=true ;;
            *) _needs_fetch=true ;;
          esac
          if [ "$_needs_fetch" = true ]; then
            local _fetched_key
            if _fetched_key="$(_athenaeum_op_read "$_key_path" 2>/dev/null)" && [ -n "$_fetched_key" ]; then
              local _tmp_env
              _tmp_env=$(mktemp "${CACHE_DIR}/config.env.XXXXXX")
              chmod 600 "$_tmp_env"
              grep -v "^${_name}=" "$CONFIG_ENV" > "$_tmp_env" 2>/dev/null || true
              printf '%s=%s\n' "$_name" "$_fetched_key" >> "$_tmp_env"
              mv "$_tmp_env" "$CONFIG_ENV"
              echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: refreshed ${_name}"
            else
              echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: ${_name} fetch failed or empty"
            fi
          fi
          _i=$((_i + 1))
        done
      fi
    fi

    # -- vector index build ------------------------------------------------
    if [ "${SEARCH_BACKEND:-vector}" = "vector" ] && _athenaeum_vector_stale; then
      # Test-only seam (issue athenaeum#2204): lets a test hold this lock
      # for a short, controlled duration to exercise the AC3/AC4
      # concurrency properties without a real ~45s vector build. Never
      # documented as a public knob; read nowhere else in this script.
      if [ -n "${ATHENAEUM_TEST_SLOW_BUILD_SECONDS:-}" ]; then
        sleep "$ATHENAEUM_TEST_SLOW_BUILD_SECONDS"
      fi
      "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from athenaeum.search import build_vector_index
try:
    count = build_vector_index(sys.argv[1], sys.argv[2])
    print(f'vector index: {count} wiki pages')
except ImportError as e:
    print(f'vector backend unavailable: {e}', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR" || true
    fi

    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] background-build: done"
  } >> "$REBUILD_LOG" 2>&1
}

# ── Credential table (issue athenaeum#2056) ─────────────────────────────────
# (ENV_NAME, default op path, override env) entries. Used both by the
# foreground preserve/restore logic below and by the background fetch loop
# above. Parallel indexed arrays, not an associative array: stock macOS
# ships bash 3.2.57, which has no `declare -A`.
_CRED_ENV_NAMES=(ANTHROPIC_API_KEY JEV_API_KEY)
_CRED_DEFAULT_PATHS=("op://Agent Tools/Anthropic API Key/credential" "op://Agent Tools/Jev Athenaeum Key/credential")
_CRED_OVERRIDE_VARS=(ATHENAEUM_OP_KEY_PATH ATHENAEUM_OP_JEV_KEY_PATH)
_cred_count=${#_CRED_ENV_NAMES[@]}
# No warn-on-missing entry here: the no-key warning further down stays
# hardcoded to ANTHROPIC_API_KEY only (Jev is optional, athenaeum#2056 AC).

# ── Background-worker dispatch ──────────────────────────────────────────────
# Must come after every function/data definition above (this is the SAME
# script, re-invoked with one extra argument) and before anything in the
# normal foreground flow below, which this invocation must NOT also run.
if [ "${1:-}" = "--run-background-build" ]; then
  _athenaeum_run_background_build
  exit $?
fi

# ══════════════════════════════════════════════════════════════════════════
# Normal foreground flow (host-timed; everything below is bound by
# $HOOK_BUDGET_SECONDS, directly or by virtue of doing no slow work at all)
# ══════════════════════════════════════════════════════════════════════════

# ── Preserve cached credentials across the rewrite below ───────────────────
# Issue athenaeum#1886: both config.env writers below (the python path and
# the bash fallback) truncate the file from scratch and neither carries a
# credential forward -- only a *fresh* `op read` (now background-only, issue
# athenaeum#2204) re-adds it. Capture whatever is cached on disk now, before
# either writer runs, so each one can be restored unless a fresh fetch
# supersedes it.
#
# Issue athenaeum#2056 generalises this from a single ANTHROPIC_API_KEY
# special case to the credential table above, so a credential appended to
# config.env by something other than this hook's own fetch (e.g. JEV_API_KEY)
# survives the rewrite too.
_cached_cred_lines=()
_had_live_cred=()
_live_cred_values=()
_i=0
while [ "$_i" -lt "$_cred_count" ]; do
  _name="${_CRED_ENV_NAMES[$_i]}"

  _line=""
  if [ -f "$CONFIG_ENV" ]; then
    _line="$(grep "^${_name}=" "$CONFIG_ENV" 2>/dev/null | tail -n 1 || true)"
  fi
  # Seer finding on athenaeum#2193: discard a matched line whose VALUE is
  # empty (e.g. a hand-edited `ANTHROPIC_API_KEY=` -- README option 2
  # explicitly invites editing this file by hand).
  case "$_line" in
    "${_name}="?*) _cached_cred_lines[_i]="$_line" ;;
    *) _cached_cred_lines[_i]="" ;;
  esac

  # Same issue, plan step 2: if the caller's own process already exported
  # this credential, that value is scoped to this process, not to the
  # cache -- remember it so it can be restored after `source "$CONFIG_ENV"`
  # below.
  _live_val="${!_name:-}"
  if [ -n "$_live_val" ]; then
    _had_live_cred[_i]=true
  else
    _had_live_cred[_i]=false
  fi
  _live_cred_values[_i]="$_live_val"

  _i=$((_i + 1))
done

# ── Read config (budget-wrapped, issue athenaeum#2204 AC2) ─────────────────
_read_config_ok=false

if _athenaeum_run_budgeted "config-read" "$PYTHON" -c "
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
cfg_path = os.path.join(src, 'src/athenaeum/config.py') if src else ''
if cfg_path and os.path.isfile(cfg_path):
    spec = importlib.util.spec_from_file_location('athenaeum_config_only', cfg_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    load_config = mod.load_config
    cap_ceiling_default = mod.RECALL_CAP_CEILING_DEFAULT
else:
    from athenaeum.config import RECALL_CAP_CEILING_DEFAULT as cap_ceiling_default
    from athenaeum.config import load_config
cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
env_path = sys.argv[2]
with open(env_path, 'w') as f:
    f.write(f'AUTO_RECALL={str(cfg.get(\"auto_recall\", True)).lower()}\n')
    f.write(f'SEARCH_BACKEND={cfg.get(\"search_backend\", \"vector\")}\n')
    provider = 'chromadb'
    if isinstance(cfg.get('vector'), dict):
        provider = cfg['vector'].get('provider', 'chromadb')
    f.write(f'VECTOR_PROVIDER={provider}\n')
    # Issue athenaeum#1120: cache the yaml-configured push-per-turn budget so
    # the per-turn hook can enforce it without a Python import. Deliberately
    # the hook-local name PUSH_TOKEN_BUDGET, NOT ATHENAEUM_PUSH_TOKEN_BUDGET
    # — this file is sourced under \`set -a\` (auto-export) by both hooks, so
    # writing the library's own env-var name here would inject a resolved
    # value into every child process and silently shadow
    # athenaeum.config.resolve_push_token_budget's own env>yaml precedence
    # for anything downstream (e.g. the MCP server's unprompted-recall path).
    # Only the yaml value is resolved here — env override is applied at
    # per-turn-hook runtime, not baked in at session start.
    tokens_per_turn = 1200
    push_budget_cfg = cfg.get('push_budget')
    if isinstance(push_budget_cfg, dict):
        raw = push_budget_cfg.get('tokens_per_turn')
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            tokens_per_turn = raw
    f.write(f'PUSH_TOKEN_BUDGET={tokens_per_turn}\n')
    # Issue athenaeum#1783: cache the yaml-configured push-path relevance-
    # bounded cap ceiling so the per-turn hook can read it without a Python
    # import -- same hook-local-name reasoning as PUSH_TOKEN_BUDGET above
    # (RECALL_CAP_CEILING, not ATHENAEUM_RECALL_CAP_CEILING, so this cached
    # value can never shadow athenaeum.config.resolve_recall_cap_ceiling's
    # own env>yaml precedence for a downstream Python caller). Only the
    # yaml value is resolved here; the env override is applied at per-turn-
    # hook runtime, not baked in at session start. cap_ceiling_default
    # (bound above, from the SAME athenaeum.config module load_config came
    # from) is the single source of truth for the fallback -- never a
    # second hardcoded literal here.
    cap_ceiling = cap_ceiling_default
    recall_cfg = cfg.get('recall')
    if isinstance(recall_cfg, dict):
        cap_cfg = recall_cfg.get('cap')
        if isinstance(cap_cfg, dict):
            raw = cap_cfg.get('ceiling')
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                cap_ceiling = raw
    f.write(f'RECALL_CAP_CEILING={cap_ceiling}\n')
    # Issue athenaeum#1343 (D10): cache the yaml-configured push-metrics
    # enable flag so the per-turn hook's telemetry append can honour
    # \`push_metrics.enabled\` without a Python import — SAME shape as
    # PUSH_TOKEN_BUDGET immediately above (hook-local name, not the
    # library's own \`ATHENAEUM_PUSH_METRICS_ENABLED\`, for the identical
    # auto-export-shadowing reason given in that comment). Only the yaml
    # value is resolved here; the env override + the falsey-token set +
    # the set-but-empty-is-off asymmetry are all applied at per-turn-hook
    # runtime, not baked in at session start.
    push_metrics_enabled = True
    push_metrics_cfg = cfg.get('push_metrics')
    if isinstance(push_metrics_cfg, dict):
        raw = push_metrics_cfg.get('enabled')
        if isinstance(raw, bool):
            push_metrics_enabled = raw
    f.write(f'PUSH_METRICS_ENABLED={str(push_metrics_enabled).lower()}\n')
" "$KNOWLEDGE_ROOT" "$CONFIG_ENV" 2>/dev/null; then
  _read_config_ok=true
fi

if [ "$_read_config_ok" = false ]; then
  CONFIG_YAML="${KNOWLEDGE_ROOT}/athenaeum.yaml"
  _auto_recall="true"
  # Issue athenaeum#1825: re-pinned default, matching
  # athenaeum.config._DEFAULTS["search_backend"].
  _search_backend="vector"
  _vector_provider="chromadb"
  # Issue athenaeum#1120: same yaml-only resolution as the python path above
  # — env override happens at per-turn-hook runtime, not here.
  _push_budget="1200"
  # Issue athenaeum#1343 (D10): same yaml-only resolution, same
  # hook-local-name reasoning, as PUSH_TOKEN_BUDGET above.
  _push_metrics_enabled="true"
  # Issue athenaeum#1783: same yaml-only resolution, same hook-local-name
  # reasoning -- but `recall.cap.ceiling` nests one level deeper than
  # `push_budget.tokens_per_turn`, so this needs its own `_in_recall` /
  # `_in_recall_cap` pair rather than reusing the single-level pattern the
  # other sections above use. Last-resort literal (no `athenaeum.config`
  # import is reachable in this branch at all): matches
  # `athenaeum.config.RECALL_CAP_CEILING_DEFAULT` today (see that
  # constant's own comment for the derivation test that pins it).
  _recall_cap_ceiling="7"
  if [ -f "$CONFIG_YAML" ]; then
    _in_vector=false
    _in_push_budget=false
    _in_push_metrics=false
    _in_recall=false
    _in_recall_cap=false
    while IFS= read -r line; do
      line="${line%%#*}"
      case "$line" in
        auto_recall:*)    _auto_recall="$(echo "${line#auto_recall:}" | tr -d ' ')"; _in_vector=false; _in_push_budget=false; _in_push_metrics=false; _in_recall=false; _in_recall_cap=false ;;
        search_backend:*) _search_backend="$(echo "${line#search_backend:}" | tr -d ' ')"; _in_vector=false; _in_push_budget=false; _in_push_metrics=false; _in_recall=false; _in_recall_cap=false ;;
        vector:*)         _in_vector=true; _in_push_budget=false; _in_push_metrics=false; _in_recall=false; _in_recall_cap=false ;;
        push_budget:*)    _in_push_budget=true; _in_vector=false; _in_push_metrics=false; _in_recall=false; _in_recall_cap=false ;;
        push_metrics:*)   _in_push_metrics=true; _in_vector=false; _in_push_budget=false; _in_recall=false; _in_recall_cap=false ;;
        recall:*)         _in_recall=true; _in_recall_cap=false; _in_vector=false; _in_push_budget=false; _in_push_metrics=false ;;
        "  provider:"*|"    provider:"*)
          [ "$_in_vector" = true ] && _vector_provider="$(echo "${line#*provider:}" | tr -d ' ')" ;;
        "  tokens_per_turn:"*|"    tokens_per_turn:"*)
          [ "$_in_push_budget" = true ] && _push_budget="$(echo "${line#*tokens_per_turn:}" | tr -d ' ')" ;;
        "  enabled:"*|"    enabled:"*)
          [ "$_in_push_metrics" = true ] && _push_metrics_enabled="$(echo "${line#*enabled:}" | tr -d ' ')" ;;
        "  cap:"*|"    cap:"*)
          [ "$_in_recall" = true ] && _in_recall_cap=true ;;
        "    ceiling:"*|"      ceiling:"*)
          [ "$_in_recall_cap" = true ] && _recall_cap_ceiling="$(echo "${line#*ceiling:}" | tr -d ' ')" ;;
        *) case "$line" in "  "*|"	"*) ;; ?*) _in_vector=false; _in_push_budget=false; _in_push_metrics=false; _in_recall=false; _in_recall_cap=false ;; esac ;;
      esac
    done < "$CONFIG_YAML"
  fi
  # Guard against a non-numeric/<=0 yaml value, same fallthrough
  # athenaeum.config.resolve_push_token_budget applies.
  case "$_push_budget" in
    ''|*[!0-9]*) _push_budget="1200" ;;
    0) _push_budget="1200" ;;
  esac
  # Same fallthrough guard, mirroring athenaeum.config.resolve_recall_cap_ceiling.
  case "$_recall_cap_ceiling" in
    ''|*[!0-9]*) _recall_cap_ceiling="7" ;;
    0) _recall_cap_ceiling="7" ;;
  esac
  # Normalize to the same lowercase true/false shape the python branch's
  # str(bool).lower() writes, same falsey-token set the per-turn hook
  # itself understands — a non-boolean-looking yaml value falls through
  # to the default (on), same as resolve_push_metrics_enabled's own
  # "non-bool yaml value falls through to the default" rule.
  case "$(echo "$_push_metrics_enabled" | tr '[:upper:]' '[:lower:]')" in
    false | no | off | 0) _push_metrics_enabled="false" ;;
    *) _push_metrics_enabled="true" ;;
  esac
  {
    echo "AUTO_RECALL=${_auto_recall}"
    echo "SEARCH_BACKEND=${_search_backend}"
    echo "VECTOR_PROVIDER=${_vector_provider}"
    echo "PUSH_TOKEN_BUDGET=${_push_budget}"
    echo "PUSH_METRICS_ENABLED=${_push_metrics_enabled}"
    echo "RECALL_CAP_CEILING=${_recall_cap_ceiling}"
  } > "$CONFIG_ENV"
fi

# Restore each cached credential both writers above just truncated away. A
# fresh `op read` in the BACKGROUND job (issue athenaeum#2204) supersedes a
# given line by rewriting it; until that completes, this cached line is
# what stops the hook from silently dropping the credential (athenaeum#1886,
# generalised by athenaeum#2056 to every entry in the table above).
_i=0
while [ "$_i" -lt "$_cred_count" ]; do
  _line="${_cached_cred_lines[$_i]}"
  if [ -n "$_line" ]; then
    printf '%s\n' "$_line" >> "$CONFIG_ENV"
  fi
  _i=$((_i + 1))
done

# Re-assert the file mode explicitly (athenaeum#1179): `umask 077` above only
# governs permissions at file *creation*. Both writers above use `open(...,
# 'w')` / shell `>` redirection, which TRUNCATE rather than recreate an
# existing file — so if config.env already exists with a looser mode (a
# stale copy from before this hardening, a manual `touch`, or a platform
# where umask doesn't apply as expected), the umask never fixes it. This
# `chmod` makes the 0600 guarantee unconditional on every run, independent
# of the file's prior state.
chmod 600 "$CONFIG_ENV"

# shellcheck disable=SC1090
source "$CONFIG_ENV"

# Restore the caller's own exported value for each credential that had one:
# `source` above just assigned it from config.env's cached/restored line (or
# unset it, if there was none), which must not clobber a value that belongs
# to this process rather than the cache (athenaeum#1886 plan step 2,
# generalised by athenaeum#2056 to the whole table).
_i=0
while [ "$_i" -lt "$_cred_count" ]; do
  if [ "${_had_live_cred[$_i]}" = true ]; then
    printf -v "${_CRED_ENV_NAMES[$_i]}" '%s' "${_live_cred_values[$_i]}"
  fi
  _i=$((_i + 1))
done

# ── Build FTS5 index (budget-wrapped, issue athenaeum#2204 AC2) ────────────
# Always build FTS5 — it's cheap (~1s for 3k pages) and rescues short-query
# recall even when the vector backend is the primary. See
# docs/design/recall-architecture.md. A budget-exhaustion skip (sentinel 75)
# or the timeout runner's own 124 is tolerated (recorded as a degradation,
# already done inside `_athenaeum_run_budgeted`); any OTHER non-zero is a
# GENUINE build failure and must still exit this hook non-zero with the
# traceback on stderr, exactly as before this issue (athenaeum#1826 AC1) --
# `set +e`/`set -e` bracket the call so `$?` can be inspected instead of
# `set -e` aborting on the spot, before this script gets to decide which of
# those two cases it is.
set +e
_athenaeum_run_budgeted "fts5-build" "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from athenaeum.search import build_fts5_index
count = build_fts5_index(sys.argv[1], sys.argv[2])
print(f'[Knowledge] FTS5 index: {count} wiki pages', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR"
_athenaeum_fts5_status=$?
set -e
if [ "$_athenaeum_fts5_status" -ne 0 ] \
  && [ "$_athenaeum_fts5_status" -ne 75 ] \
  && [ "$_athenaeum_fts5_status" -ne 124 ]; then
  exit "$_athenaeum_fts5_status"
fi

# Cache the canonical stopword list once per session (budget-wrapped). The
# per-turn recall hook reads this file instead of hard-coding its own copy,
# which keeps it in sync with the Python FTS5 filter (issue athenaeum#46).
# mktemp+mv keeps the write atomic so a concurrent read never sees
# a partial file.
_stopwords_tmp=$(mktemp "${CACHE_DIR}/stopwords.txt.XXXXXX")
if _athenaeum_run_budgeted "stopwords-cache" "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from athenaeum.search import STOPWORDS
print('\n'.join(STOPWORDS))
" > "$_stopwords_tmp" 2>/dev/null && [ -s "$_stopwords_tmp" ]; then
  mv "$_stopwords_tmp" "${CACHE_DIR}/stopwords.txt"
else
  rm -f "$_stopwords_tmp"
fi

# Cache the overflow-breadcrumb template once per session (issue athenaeum#1783,
# budget-wrapped), same "cache once here, read the cached copy per turn"
# shape the stopword list above established -- the per-turn hook reads this
# cached file directly rather than paying an `importlib.resources` lookup on
# the hot per-turn path. Prompt text is content, not code
# (`policies/prompt-text-is-content.md`): the wording lives in
# `src/athenaeum/prompts/recall_overflow_breadcrumb.md`, never inlined into
# this script or into the hook's awk. AC: "If the cache copy is missing,
# the hook emits no overflow line and still succeeds" -- so a failure here
# is silent, exactly like the stopwords copy above.
_overflow_tmp=$(mktemp "${CACHE_DIR}/recall_overflow_breadcrumb.md.XXXXXX")
if _athenaeum_run_budgeted "overflow-template-cache" "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
import importlib.resources
resource = importlib.resources.files('athenaeum.prompts').joinpath('recall_overflow_breadcrumb.md')
sys.stdout.write(resource.read_text(encoding='utf-8'))
" > "$_overflow_tmp" 2>/dev/null && [ -s "$_overflow_tmp" ]; then
  mv "$_overflow_tmp" "${CACHE_DIR}/recall_overflow_breadcrumb.md"
else
  rm -f "$_overflow_tmp"
fi

# ── Decide whether background work is needed, and kick it off ─────────────
# Issue athenaeum#2204 AC3/AC4: credential refresh and the (expensive, ~45s)
# vector index rebuild are the two foreground costs that used to be able to
# exceed the host's SessionStart timeout. Neither runs here any more --
# this section only DECIDES whether either is worth doing and, if so, forks
# `_athenaeum_run_background_build` into a detached process. This script
# never waits on it and never touches the lock itself (see that function's
# own docstring for why): a turn or session that starts while a build is in
# flight always proceeds immediately on whatever index is currently on
# disk, never blocking on this fork.
_athenaeum_need_background=false

if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || [ "${ATHENAEUM_OP_INTERACTIVE:-}" = "1" ]; then
  if command -v op >/dev/null 2>&1; then
    _i=0
    while [ "$_i" -lt "$_cred_count" ]; do
      if [ "${_had_live_cred[$_i]}" = false ] \
        && { [ -z "${_cached_cred_lines[$_i]}" ] || [ "${ATHENAEUM_OP_REFRESH:-}" = "1" ]; }; then
        _athenaeum_need_background=true
      fi
      _i=$((_i + 1))
    done
  fi
fi

_athenaeum_vector_ever_built=true
if [ "${SEARCH_BACKEND:-vector}" = "vector" ]; then
  if [ ! -d "${CACHE_DIR}/wiki-vectors" ]; then
    _athenaeum_vector_ever_built=false
  fi
  if _athenaeum_vector_stale; then
    _athenaeum_need_background=true
  else
    echo "[Knowledge] Vector index fresh — skipping rebuild." >&2
  fi
fi

# ATHENAEUM_TEST_NO_AUTOFORK is a test-only seam (issue athenaeum#2204):
# lets a test drive `--run-background-build` itself, synchronously and
# deterministically, instead of racing this script's own real detached
# fork. Never documented as a public knob; read nowhere else in this
# script. Production installs never set it, so the fork below always runs.
if [ "$_athenaeum_need_background" = true ] && [ "${ATHENAEUM_TEST_NO_AUTOFORK:-}" != "1" ]; then
  nohup "$0" --run-background-build >>"$REBUILD_LOG" 2>&1 &
  disown 2>/dev/null || true
fi

if [ "$_athenaeum_vector_ever_built" = false ]; then
  _athenaeum_record_degradation index_not_ready "no vector index has been built yet for ${WIKI_ROOT} -- recall this session is served from FTS5 only until the background build (or a later session) finishes"
fi

# Issue athenaeum#1886 (plan step 3): make the degrade visible. Every
# recall hook that wants the LLM topic extractor reads this cache, not the
# live fetch, so a run that ends with no key here is a silent quality drop
# everywhere else until someone notices "generic-looking recall names".
# Exactly one line, and only in the no-key case -- a run that ends with a
# key (cached, restored, or freshly fetched by a PRIOR session's background
# job) prints nothing new. Issue athenaeum#2204 adds the durable
# `record_degradation` call alongside the pre-existing stderr line: stderr
# is exactly the channel the shipped `settings-snippet.json` discards
# (`2>/dev/null || true`), so the status-file write is now the signal that
# actually survives a default install; the stderr line stays for the
# "run the hook by hand" debugging flow the README documents.
#
# athenaeum#2193 AC6: a caller that supplies ANTHROPIC_API_KEY LIVE (so
# `_had_live_cred[0]` was true) never gets a line written into config.env
# -- checking the file alone would warn even though a perfectly usable key
# is sitting in this process's own `$ANTHROPIC_API_KEY` right now (restored
# earlier in this script). Check that live variable too.
_final_api_key_line="$(grep '^ANTHROPIC_API_KEY=' "$CONFIG_ENV" 2>/dev/null | tail -n 1 || true)"
case "$_final_api_key_line" in
  ANTHROPIC_API_KEY=?*) ;;
  *)
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "[Knowledge] No ANTHROPIC_API_KEY cached — LLM topic extraction will fall back to regex-only extraction until a fetch succeeds." >&2
      _athenaeum_record_degradation no_api_key "no ANTHROPIC_API_KEY cached or exported; LLM topic extraction falls back to regex-only until a fetch succeeds"
    fi
    ;;
esac
