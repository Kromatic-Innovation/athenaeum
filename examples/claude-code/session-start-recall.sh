#!/usr/bin/env bash
# SessionStart hook: read config, optionally bootstrap ANTHROPIC_API_KEY
# from 1Password, and build the search index.
#
# 1. Reads athenaeum.yaml for config (auto_recall, search_backend)
# 2. Writes a shell-readable cache at ~/.cache/athenaeum/config.env
# 3. If `op` (1Password CLI) is signed in and ANTHROPIC_API_KEY isn't
#    already exported, fetches it and caches it in config.env. This is
#    required for the optional LLM topic extractor — Claude Code's own
#    CLAUDE_CODE_OAUTH_TOKEN is scoped to its inference endpoint and the
#    general Messages API rejects it with "401 OAuth authentication is
#    currently not supported".
# 4. Builds the configured search index (FTS5 and/or vector).
#
# Issue athenaeum#1120 (AC2 — checked, unaffected): this hook writes
# stderr diagnostics only and emits no `hookSpecificOutput`/
# `additionalContext` at all, so it is not an unprompted-push path — it has
# no `hot`-tier gap and no push-budget gap to route. It IS the writer for
# the FTS5 index (`athenaeum.search.build_fts5_index`) and for
# `PUSH_TOKEN_BUDGET` in config.env, both of which
# `user-prompt-recall.sh` (the actual unprompted-push hook) reads.
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
#   ATHENAEUM_OP_KEY_PATH  1Password path (default: op://Agent Tools/Anthropic API Key/credential)
#   ATHENAEUM_OP_JEV_KEY_PATH  1Password path for JEV_API_KEY (default: op://Agent Tools/Jev Athenaeum Key/credential)
#   ATHENAEUM_OP_INTERACTIVE      1 = opt into 1Password's interactive desktop-app
#                                 integration: `op` may run without a service-account
#                                 token, and the non-interactive suppression flags
#                                 below are NOT set (default: unset, athenaeum#2193)
#   ATHENAEUM_OP_REFRESH          1 = re-fetch from `op` even when a cached line for
#                                 that credential already exists (default: unset,
#                                 athenaeum#2193)
#   ATHENAEUM_OP_TIMEOUT_SECONDS  Bound, in seconds, on a single `op read` (default:
#                                 10, athenaeum#2193)

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

# ── Preserve cached credentials across the rewrite below ───────────────────
# Issue athenaeum#1886: both config.env writers below (the python path and
# the bash fallback) truncate the file from scratch and neither carries a
# credential forward -- only a *fresh* `op read`, later in this script,
# re-adds it. Any run where that fetch fails or is skipped (expired `op`
# session, no `op` on PATH, a non-interactive spawn, or the variable already
# being exported so the fetch branch never runs) was silently dropping the
# key. Capture whatever is cached on disk now, before either writer runs, so
# each one can be restored unless a fresh fetch supersedes it.
#
# Issue athenaeum#2056 generalises this from a single ANTHROPIC_API_KEY
# special case to a small table of (ENV_NAME, default op path, override env)
# entries, so a credential appended to config.env by something other than
# this hook's own ANTHROPIC fetch (e.g. JEV_API_KEY, cached by athenaeum#1997
# / athenaeum#2054's Jev decision provider) survives the rewrite too.
# Parallel indexed arrays, not an associative array: stock macOS ships bash
# 3.2.57, which has no `declare -A`.
_CRED_ENV_NAMES=(ANTHROPIC_API_KEY JEV_API_KEY)
_CRED_DEFAULT_PATHS=("op://Agent Tools/Anthropic API Key/credential" "op://Agent Tools/Jev Athenaeum Key/credential")
_CRED_OVERRIDE_VARS=(ATHENAEUM_OP_KEY_PATH ATHENAEUM_OP_JEV_KEY_PATH)
# No warn-on-missing entry here: the no-key warning at the end of this
# script stays hardcoded to ANTHROPIC_API_KEY only (Jev is optional,
# athenaeum#2056 AC) -- see the comment on that block below.

# ── Non-interactive, bounded `op read` (athenaeum#2193) ─────────────────────
# `op` probes the 1Password desktop app's group container on every call
# unless OP_LOAD_DESKTOP_APP_SETTINGS=false; on macOS that is the App Data
# TCC service and the probe can block indefinitely, turning a background
# SessionStart hook into a stuck OS permission prompt.
# OP_BIOMETRIC_UNLOCK_ENABLED=false suppresses the matching biometric-unlock
# prompt. Both flags are scoped to the single `op` subprocess via `env`,
# NEVER `export`ed process-wide -- everything else this hook spawns (the
# Python index build, etc.) must be unaffected. Skipped entirely when
# ATHENAEUM_OP_INTERACTIVE=1: that is an explicit opt-in to 1Password's
# interactive desktop-app integration, where these flags would be
# counterproductive.
#
# Resolve a timeout runner once (not once per credential): prefer GNU/BSD
# `timeout`, then macOS's `gtimeout` (coreutils via Homebrew), else a
# portable background+poll+kill fallback that works on stock macOS bash
# 3.2.57 (no `declare -A`, no `wait -n`).
_athenaeum_op_timeout_bin=""
if command -v timeout >/dev/null 2>&1; then
  _athenaeum_op_timeout_bin="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  _athenaeum_op_timeout_bin="gtimeout"
fi

# _athenaeum_op_read <op-path>
# Writes the fetched value to stdout, nothing else; returns non-zero on any
# failure or timeout. No stderr output of its own -- the caller's existing
# `if _fetched_key="$(...)" && [ -n "$_fetched_key" ]` guard already treats
# a non-zero return (or empty stdout) as "leave the cached line alone", and
# that silent-failure shape is itself part of the athenaeum#2056 contract
# ("missing Jev item produces no line and no warning").
_athenaeum_op_read() {
  local _op_path="$1"
  local _op_budget="${ATHENAEUM_OP_TIMEOUT_SECONDS:-10}"
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

_cached_cred_lines=()
_had_live_cred=()
_live_cred_values=()
_cred_count=${#_CRED_ENV_NAMES[@]}
_i=0
while [ "$_i" -lt "$_cred_count" ]; do
  _name="${_CRED_ENV_NAMES[$_i]}"

  _line=""
  if [ -f "$CONFIG_ENV" ]; then
    _line="$(grep "^${_name}=" "$CONFIG_ENV" 2>/dev/null | tail -n 1 || true)"
  fi
  _cached_cred_lines[_i]="$_line"

  # Same issue, plan step 2: if the caller's own process already exported
  # this credential, that value is scoped to this process, not to the
  # cache -- remember it so it can be restored after `source "$CONFIG_ENV"`
  # below, which would otherwise silently overwrite it with whatever the
  # cached line above says (stale, or empty).
  _live_val="${!_name:-}"
  if [ -n "$_live_val" ]; then
    _had_live_cred[_i]=true
  else
    _had_live_cred[_i]=false
  fi
  _live_cred_values[_i]="$_live_val"

  _i=$((_i + 1))
done

# ── Read config ────────────────────────────────────────────────────────────
_read_config_ok=false

if "$PYTHON" -c "
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
# fresh `op read` below supersedes a given line by rewriting it; if that
# fetch fails or is skipped, this cached line is what stops the hook from
# silently dropping the credential (athenaeum#1886, generalised by
# athenaeum#2056 to every entry in the table above, not just
# ANTHROPIC_API_KEY).
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

# ── Optional: bootstrap each credential from 1Password ─────────────────────
# Claude Code authenticates with CLAUDE_CODE_OAUTH_TOKEN, which the
# general Messages API rejects (401). The LLM topic extractor in
# user-prompt-recall.sh needs a real console key, and the Jev decision
# provider (athenaeum#1997, athenaeum#2054) needs its own. For each
# credential in the table above: when `op` is signed in and the variable
# isn't already exported, fetch + cache it. Override path via the entry's
# own override env (ATHENAEUM_OP_KEY_PATH / ATHENAEUM_OP_JEV_KEY_PATH).
# Silent on any failure -- when the variable is already exported (fetch
# branch skipped, athenaeum#1886 plan step 2) or `op` is missing/fails or
# the 1Password item does not exist, the cached line restored above is left
# as-is; that cached value belongs to the file, not to this process's
# already-exported one. A missing Jev item in particular must produce no
# line and no warning (athenaeum#2056 AC) -- this silent-failure shape
# already delivers that for free, since the warning block below only ever
# checks ANTHROPIC_API_KEY.
#
# Gates on `_had_live_cred[$_i]` (captured before config.env was even read),
# NOT on the live variable here -- the cache-restore above just sourced a
# cached/preserved line into that same variable, so testing it directly
# would make a run that ever cached a key skip every future fetch forever,
# permanently defeating "a fresh `op read` still replaces the cached value"
# (athenaeum#1886 AC2).
#
# athenaeum#2193 deliberately NARROWS that athenaeum#1886 AC2 guarantee:
# instead of an unconditional per-session re-read, a fresh fetch now only
# happens when nothing is cached yet (`_cached_cred_lines[$_i]` -- captured
# BEFORE either writer above truncated config.env, not a re-read of the
# file and not the live variable, same reasoning as the paragraph above) OR
# the operator explicitly asks for one via ATHENAEUM_OP_REFRESH=1. An
# unconditional per-session `op read` is exactly what turns a one-time OS
# permission grant into a recurring prompt -- so the "always re-read"
# behaviour AC2 established is now opt-in, not the default. Likewise, the
# fetch as a whole is only attempted when a non-interactive credential is
# available (OP_SERVICE_ACCOUNT_TOKEN) or the operator opted into
# interactive 1Password via ATHENAEUM_OP_INTERACTIVE=1 -- a whole-loop
# invariant (same for every credential), so hoisted out of the loop below
# as `_op_allowed` rather than recomputed per entry.
if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || [ "${ATHENAEUM_OP_INTERACTIVE:-}" = "1" ]; then
  _op_allowed=true
else
  _op_allowed=false
fi

_i=0
while [ "$_i" -lt "$_cred_count" ]; do
  _name="${_CRED_ENV_NAMES[$_i]}"
  _default_path="${_CRED_DEFAULT_PATHS[$_i]}"
  _override_var="${_CRED_OVERRIDE_VARS[$_i]}"
  _override_val="${!_override_var:-}"
  if [ -n "$_override_val" ]; then
    _key_path="$_override_val"
  else
    _key_path="$_default_path"
  fi

  if [ "${_had_live_cred[$_i]}" = false ] && command -v op >/dev/null 2>&1 \
    && [ "$_op_allowed" = true ] \
    && { [ -z "${_cached_cred_lines[$_i]}" ] || [ "${ATHENAEUM_OP_REFRESH:-}" = "1" ]; }; then
    if _fetched_key="$(_athenaeum_op_read "$_key_path" 2>/dev/null)" && [ -n "$_fetched_key" ]; then
      # mktemp inside the (already-restricted) cache dir — gives us an
      # unpredictable path mode 600 atomically, closing the window where
      # `${CONFIG_ENV}.tmp` could have been pre-created as a symlink or
      # read in the gap between open() and chmod().
      tmp_env=$(mktemp "${CACHE_DIR}/config.env.XXXXXX")
      # Belt-and-suspenders: mktemp's mkstemp() already creates the file at
      # 0600, but assert it explicitly rather than relying on that being
      # true on every mktemp implementation this hook might run under.
      chmod 600 "$tmp_env"
      grep -v "^${_name}=" "$CONFIG_ENV" > "$tmp_env" 2>/dev/null || true
      printf '%s=%s\n' "$_name" "$_fetched_key" >> "$tmp_env"
      mv "$tmp_env" "$CONFIG_ENV"
    fi
  fi

  _i=$((_i + 1))
done

# Issue athenaeum#1886 (plan step 3): make the degrade visible. Every
# recall hook that wants the LLM topic extractor reads this cache, not the
# live fetch, so a run that ends with no key here is a silent quality drop
# everywhere else until someone notices "generic-looking recall names".
# Exactly one line, and only in the no-key case -- a run that ends with a
# key (cached, restored, or freshly fetched) prints nothing new.
_final_api_key_line="$(grep '^ANTHROPIC_API_KEY=' "$CONFIG_ENV" 2>/dev/null | tail -n 1 || true)"
case "$_final_api_key_line" in
  ANTHROPIC_API_KEY=?*) ;;
  *)
    echo "[Knowledge] No ANTHROPIC_API_KEY cached — LLM topic extraction will fall back to regex-only extraction until a fetch succeeds." >&2
    ;;
esac

# ── Build search index ─────────────────────────────────────────────────────
# Always build FTS5 — it's cheap (~1s for 3k pages) and rescues short-query
# recall even when the vector backend is the primary. See docs/design/recall-architecture.md.
"$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from athenaeum.search import build_fts5_index
count = build_fts5_index(sys.argv[1], sys.argv[2])
print(f'[Knowledge] FTS5 index: {count} wiki pages', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR"

# Cache the canonical stopword list once per session. The per-turn
# recall hook reads this file instead of hard-coding its own copy,
# which keeps it in sync with the Python FTS5 filter (issue athenaeum#46).
# mktemp+mv keeps the write atomic so a concurrent read never sees
# a partial file.
_stopwords_tmp=$(mktemp "${CACHE_DIR}/stopwords.txt.XXXXXX")
if "$PYTHON" -c "
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

# Cache the overflow-breadcrumb template once per session (issue athenaeum#1783),
# same "cache once here, read the cached copy per turn" shape the stopword
# list above established -- the per-turn hook reads this cached file
# directly rather than paying an `importlib.resources` lookup on the hot
# per-turn path. Prompt text is content, not code
# (`policies/prompt-text-is-content.md`): the wording lives in
# `src/athenaeum/prompts/recall_overflow_breadcrumb.md`, never inlined into
# this script or into the hook's awk. AC: "If the cache copy is missing,
# the hook emits no overflow line and still succeeds" -- so a failure here
# is silent, exactly like the stopwords copy above.
_overflow_tmp=$(mktemp "${CACHE_DIR}/recall_overflow_breadcrumb.md.XXXXXX")
if "$PYTHON" -c "
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

if [ "${SEARCH_BACKEND:-vector}" = "vector" ]; then
  # Vector rebuild is expensive (~45s on a ~3k-page wiki) so skip when the
  # existing index is newer than the newest wiki page. FTS5 above is cheap
  # enough to always rebuild. Override with ATHENAEUM_FORCE_REBUILD=1.
  VECTOR_DIR="${CACHE_DIR}/wiki-vectors"
  _vector_fresh=false
  if [ -d "$VECTOR_DIR" ] && [ "${ATHENAEUM_FORCE_REBUILD:-0}" != "1" ]; then
    _idx_mtime=$(find "$VECTOR_DIR" -type f -print0 2>/dev/null \
      | xargs -0 stat -f %m 2>/dev/null \
      | sort -n | tail -1 || echo 0)
    _wiki_mtime=$(find "$WIKI_ROOT" -type f -name '*.md' -print0 2>/dev/null \
      | xargs -0 stat -f %m 2>/dev/null \
      | sort -n | tail -1 || echo 0)
    _idx_mtime="${_idx_mtime:-0}"
    _wiki_mtime="${_wiki_mtime:-0}"
    if [ "$_idx_mtime" -gt "$_wiki_mtime" ] && [ "$_idx_mtime" -gt 0 ]; then
      _vector_fresh=true
      echo "[Knowledge] Vector index fresh — skipping rebuild." >&2
    fi
  fi

  if [ "$_vector_fresh" = false ]; then
    "$PYTHON" -c "
import sys, os
src = os.environ.get('ATHENAEUM_SRC', '')
if src:
    sys.path.insert(0, os.path.join(src, 'src'))
from athenaeum.search import build_vector_index
try:
    count = build_vector_index(sys.argv[1], sys.argv[2])
    print(f'[Knowledge] Vector index: {count} wiki pages', file=sys.stderr)
except ImportError as e:
    print(f'[Knowledge] Vector backend unavailable: {e}', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR" || true
  fi
fi
