#!/usr/bin/env bash
# SessionStart hook: build a SQLite FTS5 index of wiki frontmatter.
#
# Extracts name, tags, aliases, and description from every wiki page into
# a full-text search index. The per-turn hook (user-prompt-recall.sh)
# queries this index in <50ms instead of scanning thousands of files.
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
#   KNOWLEDGE_WIKI_PATH  Path to wiki directory (default: ~/knowledge/wiki)

set -euo pipefail

WIKI_ROOT="${KNOWLEDGE_WIKI_PATH:-$HOME/knowledge/wiki}"
CACHE_DIR="${HOME}/.cache/athenaeum"
DB_FILE="${CACHE_DIR}/wiki-index.db"

if [ ! -d "$WIKI_ROOT" ]; then
  exit 0
fi

mkdir -p "$CACHE_DIR"

python3 -c "
import os, sys, sqlite3

wiki_root = sys.argv[1]
db_path = sys.argv[2]

if os.path.exists(db_path):
    os.remove(db_path)

conn = sqlite3.connect(db_path)
conn.execute('''CREATE VIRTUAL TABLE wiki USING fts5(
    filename, name, tags, aliases, description,
    tokenize=\"porter unicode61\"
)''')

rows = []
for fname in os.listdir(wiki_root):
    if not fname.endswith('.md') or fname.startswith('_'):
        continue
    path = os.path.join(wiki_root, fname)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(2000)
    except OSError:
        continue

    name = tags = aliases = description = ''
    if text.startswith('---'):
        end = text.find('---', 4)
        if end > 0:
            fm = text[4:end]
            for line in fm.splitlines():
                line = line.strip()
                if line.startswith('name:'):
                    name = line[5:].strip().strip('\"').strip(\"'\")
                elif line.startswith('tags:'):
                    tags = line[5:].strip().strip('[]')
                elif line.startswith('aliases:'):
                    aliases = line[8:].strip().strip('[]')
                elif line.startswith('description:'):
                    description = line[12:].strip().strip('\"').strip(\"'\")

    if not name:
        name = fname.replace('.md', '')

    rows.append((fname, name, tags, aliases, description))

conn.executemany('INSERT INTO wiki VALUES (?,?,?,?,?)', rows)
conn.commit()
conn.close()
print(f'[Knowledge] FTS5 index: {len(rows)} wiki pages', file=sys.stderr)
" "$WIKI_ROOT" "$DB_FILE" 2>&1 || true
