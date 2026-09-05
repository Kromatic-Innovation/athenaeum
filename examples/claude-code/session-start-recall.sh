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
# the `memory_tier`-carrying FTS5 index (`athenaeum.search.build_fts5_index`,
# schema v4) and for `PUSH_TOKEN_BUDGET` in config.env, both of which
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
else:
    from athenaeum.config import load_config
cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
env_path = sys.argv[2]
with open(env_path, 'w') as f:
    f.write(f'AUTO_RECALL={str(cfg.get(\"auto_recall\", True)).lower()}\n')
    f.write(f'SEARCH_BACKEND={cfg.get(\"search_backend\", \"fts5\")}\n')
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
  _search_backend="fts5"
  _vector_provider="chromadb"
  # Issue athenaeum#1120: same yaml-only resolution as the python path above
  # — env override happens at per-turn-hook runtime, not here.
  _push_budget="1200"
  # Issue athenaeum#1343 (D10): same yaml-only resolution, same
  # hook-local-name reasoning, as PUSH_TOKEN_BUDGET above.
  _push_metrics_enabled="true"
  if [ -f "$CONFIG_YAML" ]; then
    _in_vector=false
    _in_push_budget=false
    _in_push_metrics=false
    while IFS= read -r line; do
      line="${line%%#*}"
      case "$line" in
        auto_recall:*)    _auto_recall="$(echo "${line#auto_recall:}" | tr -d ' ')"; _in_vector=false; _in_push_budget=false; _in_push_metrics=false ;;
        search_backend:*) _search_backend="$(echo "${line#search_backend:}" | tr -d ' ')"; _in_vector=false; _in_push_budget=false; _in_push_metrics=false ;;
        vector:*)         _in_vector=true; _in_push_budget=false; _in_push_metrics=false ;;
        push_budget:*)    _in_push_budget=true; _in_vector=false; _in_push_metrics=false ;;
        push_metrics:*)   _in_push_metrics=true; _in_vector=false; _in_push_budget=false ;;
        "  provider:"*|"    provider:"*)
          [ "$_in_vector" = true ] && _vector_provider="$(echo "${line#*provider:}" | tr -d ' ')" ;;
        "  tokens_per_turn:"*|"    tokens_per_turn:"*)
          [ "$_in_push_budget" = true ] && _push_budget="$(echo "${line#*tokens_per_turn:}" | tr -d ' ')" ;;
        "  enabled:"*|"    enabled:"*)
          [ "$_in_push_metrics" = true ] && _push_metrics_enabled="$(echo "${line#*enabled:}" | tr -d ' ')" ;;
        *) case "$line" in "  "*|"	"*) ;; ?*) _in_vector=false; _in_push_budget=false; _in_push_metrics=false ;; esac ;;
      esac
    done < "$CONFIG_YAML"
  fi
  # Guard against a non-numeric/<=0 yaml value, same fallthrough
  # athenaeum.config.resolve_push_token_budget applies.
  case "$_push_budget" in
    ''|*[!0-9]*) _push_budget="1200" ;;
    0) _push_budget="1200" ;;
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
  } > "$CONFIG_ENV"
fi

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

# ── Optional: bootstrap ANTHROPIC_API_KEY from 1Password ────────────────
# Claude Code authenticates with CLAUDE_CODE_OAUTH_TOKEN, which the
# general Messages API rejects (401). The LLM topic extractor in
# user-prompt-recall.sh needs a real console key. When `op` is signed
# in and ANTHROPIC_API_KEY isn't already set, fetch + cache it.
# Override path via ATHENAEUM_OP_KEY_PATH. Silent on any failure.
_KEY_PATH="${ATHENAEUM_OP_KEY_PATH:-op://Agent Tools/Anthropic API Key/credential}"
if [ -z "${ANTHROPIC_API_KEY:-}" ] && command -v op >/dev/null 2>&1; then
  if _fetched_key="$(op read "$_KEY_PATH" 2>/dev/null)" && [ -n "$_fetched_key" ]; then
    # mktemp inside the (already-restricted) cache dir — gives us an
    # unpredictable path mode 600 atomically, closing the window where
    # `${CONFIG_ENV}.tmp` could have been pre-created as a symlink or
    # read in the gap between open() and chmod().
    tmp_env=$(mktemp "${CACHE_DIR}/config.env.XXXXXX")
    # Belt-and-suspenders: mktemp's mkstemp() already creates the file at
    # 0600, but assert it explicitly rather than relying on that being true
    # on every mktemp implementation this hook might run under.
    chmod 600 "$tmp_env"
    grep -v '^ANTHROPIC_API_KEY=' "$CONFIG_ENV" > "$tmp_env" 2>/dev/null || true
    printf 'ANTHROPIC_API_KEY=%s\n' "$_fetched_key" >> "$tmp_env"
    mv "$tmp_env" "$CONFIG_ENV"
  fi
fi

# ── Build search index ─────────────────────────────────────────────────────
# Always build FTS5 — it's cheap (~1s for 3k pages) and rescues short-query
# recall even when the vector backend is the primary. See docs/design/recall-architecture.md.
"$PYTHON" -c "
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    spec = importlib.util.spec_from_file_location('athenaeum_search_only', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    build_fts5_index = mod.build_fts5_index
else:
    from athenaeum.search import build_fts5_index
count = build_fts5_index(sys.argv[1], sys.argv[2])
print(f'[Knowledge] FTS5 index: {count} wiki pages', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR" 2>&1 || true

# Cache the canonical stopword list once per session. The per-turn
# recall hook reads this file instead of hard-coding its own copy,
# which keeps it in sync with the Python FTS5 filter (issue athenaeum#46).
# mktemp+mv keeps the write atomic so a concurrent read never sees
# a partial file.
_stopwords_tmp=$(mktemp "${CACHE_DIR}/stopwords.txt.XXXXXX")
if "$PYTHON" -c "
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    spec = importlib.util.spec_from_file_location('athenaeum_search_only', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    STOPWORDS = mod.STOPWORDS
else:
    from athenaeum.search import STOPWORDS
print('\n'.join(STOPWORDS))
" > "$_stopwords_tmp" 2>/dev/null && [ -s "$_stopwords_tmp" ]; then
  mv "$_stopwords_tmp" "${CACHE_DIR}/stopwords.txt"
else
  rm -f "$_stopwords_tmp"
fi

if [ "${SEARCH_BACKEND:-fts5}" = "vector" ]; then
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
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    spec = importlib.util.spec_from_file_location('athenaeum_search_only', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    build_vector_index = mod.build_vector_index
else:
    from athenaeum.search import build_vector_index
try:
    count = build_vector_index(sys.argv[1], sys.argv[2])
    print(f'[Knowledge] Vector index: {count} wiki pages', file=sys.stderr)
except ImportError as e:
    print(f'[Knowledge] Vector backend unavailable: {e}', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR" 2>&1 || true
  fi
fi
