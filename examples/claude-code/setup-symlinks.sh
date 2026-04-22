#!/usr/bin/env bash
# setup-symlinks.sh — bridge ~/.claude/projects/<scope>/memory into
# $KNOWLEDGE_ROOT/raw/auto-memory/<scope>/ so Athenaeum's librarian can
# ingest Claude Code's auto-memory files alongside other raw intake.
#
# Idempotent: re-running is a no-op for already-linked scopes.
# See docs/integrations/claude-code.md for the full integration guide.
set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--dry-run]

Environment:
  KNOWLEDGE_ROOT   Athenaeum knowledge base root (default: \$HOME/knowledge)
  CLAUDE_PROJECTS  Claude Code projects directory (default: \$HOME/.claude/projects)

The script walks every \$CLAUDE_PROJECTS/<scope>/memory/ directory and
symlinks it under \$KNOWLEDGE_ROOT/raw/auto-memory/<scope>/. Existing
valid symlinks are left alone; anything else at the destination is
skipped with a warning.
EOF
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg (use --help)" >&2
      exit 64
      ;;
  esac
done

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY: $*"
  else
    "$@"
  fi
}

AUTO_ROOT="$KNOWLEDGE_ROOT/raw/auto-memory"
run mkdir -p "$AUTO_ROOT"

if [ ! -d "$CLAUDE_PROJECTS" ]; then
  echo "No $CLAUDE_PROJECTS directory — nothing to link." >&2
  exit 0
fi

linked=0
skipped=0
for project_dir in "$CLAUDE_PROJECTS"/*/; do
  [ -d "$project_dir" ] || continue
  scope="$(basename "$project_dir")"
  src="${project_dir%/}/memory"
  dest="$AUTO_ROOT/$scope"

  if [ ! -d "$src" ]; then
    continue
  fi

  if [ -L "$dest" ]; then
    current="$(readlink "$dest")"
    if [ "$current" = "$src" ]; then
      skipped=$((skipped + 1))
      continue
    fi
    echo "WARN: $dest points at $current, expected $src — skipping" >&2
    continue
  fi

  if [ -e "$dest" ]; then
    echo "WARN: $dest exists and is not a symlink — skipping" >&2
    continue
  fi

  run ln -s "$src" "$dest"
  echo "linked $scope"
  linked=$((linked + 1))
done

echo "Done. linked=$linked already-linked=$skipped"
