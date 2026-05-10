#!/usr/bin/env bash
# Out-of-band knowledge index rebuild — for SessionEnd or on-demand use.
#
# `session-start-recall.sh` builds the index synchronously at session start.
# That works for small wikis but becomes painful (~45s+) once the wiki
# has thousands of pages. This script is the deferred-rebuild alternative:
# wire it into a SessionEnd hook (detached via `nohup ... &`) so the
# rebuild happens between sessions instead of on the critical path.
#
# Uses an atomic directory lock at $CACHE_DIR/rebuild.lock to prevent
# concurrent rebuilds when multiple sessions exit at once. Stale locks
# (>1h old) are reclaimed automatically.
#
# Reads backend selection from $CACHE_DIR/config.env (written by
# session-start-recall.sh on a prior run). Defaults to fts5.
#
# Configure as a SessionEnd hook in ~/.claude/settings.json:
#   "SessionEnd": [{
#     "hooks": [{
#       "type": "command",
#       "command": "nohup /path/to/rebuild-index.sh >/dev/null 2>&1 &"
#     }]
#   }]
#
# Environment variables:
#   KNOWLEDGE_ROOT       Knowledge base root (default: ~/knowledge)
#   KNOWLEDGE_WIKI_PATH  Wiki directory (default: $KNOWLEDGE_ROOT/wiki)
#   ATHENAEUM_PYTHON     Python interpreter with athenaeum deps
#   ATHENAEUM_SRC        Path to athenaeum source checkout (optional)

set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$KNOWLEDGE_ROOT/wiki}"
CACHE_DIR="${HOME}/.cache/athenaeum"
CONFIG_ENV="${CACHE_DIR}/config.env"
LOCK_DIR="${CACHE_DIR}/rebuild.lock"
LOG_FILE="${CACHE_DIR}/rebuild.log"
PYTHON="${ATHENAEUM_PYTHON:-python3}"

[ -d "$WIKI_ROOT" ] || exit 0

mkdir -p "$CACHE_DIR"

# ── Atomic lock acquisition ──────────────────────────────────────────────
# `mkdir` is atomic on POSIX. Either we own the lock or someone else does
# and we exit cheaply. Stale locks (>1h) are reclaimed — covers the case
# where a prior rebuild crashed without releasing.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_age=0
  if ctime_mac="$(stat -f %c "$LOCK_DIR" 2>/dev/null)"; then
    lock_age=$(( $(date +%s) - ctime_mac ))
  elif ctime_linux="$(stat -c %Y "$LOCK_DIR" 2>/dev/null)"; then
    lock_age=$(( $(date +%s) - ctime_linux ))
  fi
  if [ "$lock_age" -gt 3600 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] rebuild: stale lock (${lock_age}s old), reclaiming" >> "$LOG_FILE"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] rebuild: another rebuild in progress (lock ${lock_age}s old), skipping" >> "$LOG_FILE"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

# ── Read backend ─────────────────────────────────────────────────────────
if [ -f "$CONFIG_ENV" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_ENV"
fi
SEARCH_BACKEND="${SEARCH_BACKEND:-fts5}"

# ── Delegate to athenaeum.search ─────────────────────────────────────────
_SEARCH_MOD="${ATHENAEUM_SRC:-}/src/athenaeum/search.py"
start_ts="$(date +%s)"

_run_python_build() {
  # $1 = build function name, label = $2
  local fn="$1" label="$2"
  "$PYTHON" -c "
import sys, os, importlib.util
src = os.environ.get('ATHENAEUM_SRC', '')
path = os.path.join(src, 'src/athenaeum/search.py') if src else ''
if path and os.path.isfile(path):
    spec = importlib.util.spec_from_file_location('athenaeum.search', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, '${fn}')
else:
    from athenaeum.search import ${fn} as fn
try:
    count = fn(sys.argv[1], sys.argv[2])
    print(f'${label} index: {count} wiki pages')
except ImportError as e:
    print(f'${label} backend unavailable: {e}', file=sys.stderr)
    sys.exit(2)
" "$WIKI_ROOT" "$CACHE_DIR" 2>&1
}

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] rebuild: start backend=${SEARCH_BACKEND}"

  if [ "$SEARCH_BACKEND" = "vector" ]; then
    _run_python_build build_vector_index "vector"
    # FTS5 as secondary: hybrid recall in user-prompt-recall.sh wants
    # both, and FTS5 is cheap (~1s for 3k pages) next to vector's ~45s.
    _run_python_build build_fts5_index "fts5 (secondary)"
  elif [ "$SEARCH_BACKEND" = "fts5" ]; then
    _run_python_build build_fts5_index "fts5"
  else
    echo "unknown search backend: $SEARCH_BACKEND" >&2
    exit 1
  fi

  duration=$(( $(date +%s) - start_ts ))
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] rebuild: done duration=${duration}s"
} >> "$LOG_FILE" 2>&1
