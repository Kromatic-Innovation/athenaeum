#!/usr/bin/env bash
# SessionStart hook: read config and build the search index.
#
# 1. Reads athenaeum.yaml for config (auto_recall, search_backend)
# 2. Writes a shell-readable cache at ~/.cache/athenaeum/config.env
# 3. Builds the appropriate search index (FTS5 or vector)
#
# The per-turn hook reads config.env (no Python needed at query time for FTS5).
#
# Configure in ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/path/to/session-start-recall.sh",
#         "timeout": 15
#       }]
#     }]
#   }
#
# Environment variables:
#   KNOWLEDGE_ROOT       Path to knowledge directory (default: ~/knowledge)
#   KNOWLEDGE_WIKI_PATH  Path to wiki directory (default: $KNOWLEDGE_ROOT/wiki)

set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$KNOWLEDGE_ROOT/wiki}"
CACHE_DIR="${HOME}/.cache/athenaeum"
CONFIG_ENV="${CACHE_DIR}/config.env"

if [ ! -d "$WIKI_ROOT" ]; then
  exit 0
fi

mkdir -p "$CACHE_DIR"

# ── Read config ────────────────────────────────────────────────────────────
# Try athenaeum Python module; fall back to pure-shell YAML parsing.
_read_config_ok=false

if python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ.get('ATHENAEUM_SRC', ''), 'src'))
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

source "$CONFIG_ENV"

# ── Build search index ─────────────────────────────────────────────────────
if [ "$SEARCH_BACKEND" = "fts5" ]; then
  # pip install athenaeum provides the search module
  python3 -c "
import sys, os
sys.path.insert(0, os.path.join(os.environ.get('ATHENAEUM_SRC', ''), 'src'))
from athenaeum.search import build_fts5_index
count = build_fts5_index(sys.argv[1], sys.argv[2])
print(f'[Knowledge] FTS5 index: {count} wiki pages', file=sys.stderr)
" "$WIKI_ROOT" "$CACHE_DIR" 2>&1 || true
fi
