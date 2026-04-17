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
" "$KNOWLEDGE_ROOT" "$CONFIG_ENV" 2>/dev/null; then
  _read_config_ok=true
fi

if [ "$_read_config_ok" = false ]; then
  CONFIG_YAML="${KNOWLEDGE_ROOT}/athenaeum.yaml"
  _auto_recall="true"
  _search_backend="fts5"
  _vector_provider="chromadb"
  if [ -f "$CONFIG_YAML" ]; then
    _in_vector=false
    while IFS= read -r line; do
      line="${line%%#*}"
      case "$line" in
        auto_recall:*)    _auto_recall="$(echo "${line#auto_recall:}" | tr -d ' ')"; _in_vector=false ;;
        search_backend:*) _search_backend="$(echo "${line#search_backend:}" | tr -d ' ')"; _in_vector=false ;;
        vector:*)         _in_vector=true ;;
        "  provider:"*|"    provider:"*)
          [ "$_in_vector" = true ] && _vector_provider="$(echo "${line#*provider:}" | tr -d ' ')" ;;
        *) case "$line" in "  "*|"	"*) ;; ?*) _in_vector=false ;; esac ;;
      esac
    done < "$CONFIG_YAML"
  fi
  {
    echo "AUTO_RECALL=${_auto_recall}"
    echo "SEARCH_BACKEND=${_search_backend}"
    echo "VECTOR_PROVIDER=${_vector_provider}"
  } > "$CONFIG_ENV"
fi

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
    grep -v '^ANTHROPIC_API_KEY=' "$CONFIG_ENV" > "$tmp_env" 2>/dev/null || true
    printf 'ANTHROPIC_API_KEY=%s\n' "$_fetched_key" >> "$tmp_env"
    mv "$tmp_env" "$CONFIG_ENV"
  fi
fi

# ── Build search index ─────────────────────────────────────────────────────
# Always build FTS5 — it's cheap (~1s for 3k pages) and rescues short-query
# recall even when the vector backend is the primary. See docs/recall-architecture.md.
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
# which keeps it in sync with the Python FTS5 filter (issue #46).
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
