# Claude Code auto-memory integration

Claude Code ships a first-party "auto-memory" feature: the agent can write
durable notes to `~/.claude/projects/<scope>/memory/` and load them back on
future sessions. This works well inside a single agent but has two gaps when
you run a team or a mix of agents:

1. The notes are scoped to Claude Code — other tools (search, MCP clients, a
   second agent runtime) can't read them.
2. There is no compilation step. Notes accumulate as a flat pile of markdown
   with no entity consolidation, no deduplication, and no conflict surfacing.

Athenaeum fills both gaps. Wire Claude Code's auto-memory directory into
Athenaeum's `raw/` intake tree via symlink, and the existing librarian
pipeline picks the files up, clusters near-duplicates, merges them into
entity wiki pages, and flags contradictions for review. Claude Code keeps
writing the same files it always did; Athenaeum reads them as another intake
source.

This guide is the generic, adopter-facing setup. If you also want a per-turn
shell-hook sidecar (auto-recall, pre-compact save), see
[`examples/claude-code/README.md`](../../examples/claude-code/README.md).
The two are complementary and can be run together.

## 1. Directory layout

Two trees, one bridged by a symlink per scope:

```
~/.claude/projects/<scope>/memory/       (Claude Code writes here)
    my_first_note.md
    another_note.md
    ...

                 │
                 │  symlink per scope
                 ▼

~/knowledge/raw/auto-memory/<scope>/     (Athenaeum reads here)
    my_first_note.md        -> ../../../.claude/projects/<scope>/memory/my_first_note.md
    another_note.md         -> ...
```

`<scope>` is whatever Claude Code calls the project — typically the
flattened working-directory path (e.g. `-Users-alice-Code` for work started
in `/Users/alice/Code`). You can have many scopes; symlink each
independently.

Athenaeum discovers auto-memory files under any `raw/auto-memory/<scope>/`
directory (see `athenaeum.librarian.discover_auto_memory_files`). The scope
name is propagated through the pipeline as `origin_scope` on each
source entry so you can trace a consolidated wiki claim back to the
project it came from.

## 2. Symlink setup

Copy [`examples/claude-code/setup-symlinks.sh`](../../examples/claude-code/setup-symlinks.sh)
or paste the inline version below. It is idempotent and supports a dry run.

```bash
#!/usr/bin/env bash
# setup-symlinks.sh — bridge ~/.claude/projects/*/memory into Athenaeum raw/
set -euo pipefail

KNOWLEDGE_ROOT="${KNOWLEDGE_ROOT:-$HOME/knowledge}"
CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run]"
      echo "  KNOWLEDGE_ROOT  (default: \$HOME/knowledge)"
      echo "  CLAUDE_PROJECTS (default: \$HOME/.claude/projects)"
      exit 0
      ;;
  esac
done

run() { [ "$DRY_RUN" -eq 1 ] && echo "DRY: $*" || "$@"; }

AUTO_ROOT="$KNOWLEDGE_ROOT/raw/auto-memory"
run mkdir -p "$AUTO_ROOT"

[ -d "$CLAUDE_PROJECTS" ] || { echo "No $CLAUDE_PROJECTS — skipping."; exit 0; }

for project_dir in "$CLAUDE_PROJECTS"/*/; do
  scope="$(basename "$project_dir")"
  src="$project_dir/memory"
  dest="$AUTO_ROOT/$scope"
  [ -d "$src" ] || continue
  if [ -L "$dest" ]; then
    current="$(readlink "$dest")"
    [ "$current" = "$src" ] && continue
    echo "WARN: $dest points at $current, expected $src — skipping"
    continue
  fi
  if [ -e "$dest" ]; then
    echo "WARN: $dest exists and is not a symlink — skipping"
    continue
  fi
  run ln -s "$src" "$dest"
  echo "linked $scope"
done
```

Run it:

```bash
bash examples/claude-code/setup-symlinks.sh --dry-run   # preview
bash examples/claude-code/setup-symlinks.sh             # apply
```

Safe to re-run after new Claude Code projects appear; existing valid symlinks
are left alone.

## 3. Citation policy (optional but recommended)

Athenaeum can ingest auto-memory files with any content. But if you want the
consolidated wiki to cite back to the original Claude Code turn that
produced each fact, add YAML frontmatter with `sources[]` entries to your
auto-memory files. The librarian's merge pass propagates these citations
verbatim into the consolidated wiki entry.

Required/recommended fields:

| Field             | Required | Description                                                                 |
|-------------------|----------|-----------------------------------------------------------------------------|
| `name`            | yes      | Short memory slug, e.g. `project_acme_corp`                                 |
| `description`     | yes      | One-line summary                                                            |
| `type`            | no       | One of `project`, `reference`, `feedback`, `user` (matches file prefix)     |
| `originSessionId` | yes (strict) | Claude Code session UUID that produced this memory                      |
| `originTurn`      | yes (strict) | Turn index within that session                                          |
| `sources[]`       | append-only | List of source maps with `session`, `turn`, optional `excerpt`          |

Example file (see also
[`examples/claude-code/auto-memory-frontmatter.example.md`](../../examples/claude-code/auto-memory-frontmatter.example.md)):

```markdown
---
name: project_acme_corp
description: Acme Corp is a Series B logistics platform led by Priya Shah.
type: project
originSessionId: 01JZ8X6P4Q2K7N1F8V4S9W3R0T
originTurn: 12
sources:
  - session: 01JZ8X6P4Q2K7N1F8V4S9W3R0T
    turn: 12
    excerpt: "Priya confirmed the Series B closed 2026-03-12."
---

Acme Corp is a Series B logistics platform. Priya Shah is the CEO;
she confirmed the Series B close date in session turn 12.
```

**Citation-strict vs. permissive.** Athenaeum ingests uncited files fine —
they just land in the consolidated wiki without provenance. If you want to
enforce "every fact carries a source", run the stop-hook validator (Section
4) to warn when auto-memory files lack `originSessionId`/`originTurn`. The
validator can be made blocking or non-blocking; start non-blocking while you
bootstrap the habit.

**Append-only `sources[]`.** When Claude Code later adds a corroborating
turn to an existing memory, append a new entry to `sources[]` rather than
rewriting the list. The merge pass dedupes by `(session, turn)` so duplicate
appends are harmless, but rewrites destroy provenance.

## 4. Stop-hook validator template

A Claude Code `Stop` hook can check auto-memory frontmatter at session end
and warn (or fail) on missing citation fields. Generic template in
[`examples/claude-code/stop-hook-validate.sh`](../../examples/claude-code/stop-hook-validate.sh):

```bash
#!/usr/bin/env bash
# stop-hook-validate.sh — warn when auto-memory files lack citation fields
set -euo pipefail

CLAUDE_PROJECTS="${CLAUDE_PROJECTS:-$HOME/.claude/projects}"
MODE="${VALIDATE_MODE:-warn}"   # warn | block

bad=0
while IFS= read -r -d '' file; do
  fm="$(awk '/^---$/{c++; next} c==1' "$file" 2>/dev/null || true)"
  for field in originSessionId originTurn; do
    if ! grep -qE "^${field}:" <<<"$fm"; then
      echo "WARN: $file missing $field" >&2
      bad=$((bad + 1))
    fi
  done
done < <(find "$CLAUDE_PROJECTS" -path '*/memory/*.md' -print0 2>/dev/null)

if [ "$bad" -gt 0 ] && [ "$MODE" = "block" ]; then
  echo "Citation policy: $bad violation(s); set VALIDATE_MODE=warn to downgrade." >&2
  exit 2
fi
exit 0
```

Wire it into `~/.claude/settings.json` as a `Stop` hook. Start with
`VALIDATE_MODE=warn`; promote to `block` once your memories are citing
consistently.

## 5. How the librarian ingests and consolidates

Once the symlinks are in place, the normal `athenaeum run` pipeline handles
the rest. Five steps, all existing code:

1. **Discover.** `athenaeum.librarian.discover_auto_memory_files` walks
   `raw/auto-memory/<scope>/` and returns parsed `AutoMemoryFile` records
   (frontmatter + body + computed slug).
2. **Cluster.** `athenaeum.clusters.cluster_auto_memory_files` embeds each
   record with the vector backend and groups near-duplicates using a
   tunable cosine threshold. Output: a cluster JSONL you can inspect with
   `athenaeum run --cluster-only`.
3. **Merge.** `athenaeum.merge.merge_clusters_to_wiki` writes one
   consolidated wiki entry per cluster at `wiki/auto-<topic-slug>.md`,
   propagating `origin_scope` per source and union-ing `sources[]` with
   `(session, turn)` dedupe. Size-1 clusters still produce an entry.
   Source frontmatter stays untouched.
4. **Detect contradictions.** `athenaeum.contradictions.detect_contradictions`
   (C4) flags clusters where the consolidated wiki makes claims that
   disagree with one or more source files. Flagged clusters surface in
   `wiki/_pending_questions.md` and carry a `contradictions_detected: true`
   marker on the wiki entry.
5. **Answer escalations.** Unresolved contradictions and ambiguities wait
   in `wiki/_pending_questions.md`. You resolve them by editing the file or
   via the `resolve_question` MCP tool; `athenaeum ingest-answers` then
   folds the resolution back into the wiki.

All five steps run from a single `athenaeum run`. You can inspect any stage
in isolation with `--cluster-only` or `--merge-only`.

## 6. Quick start

```bash
# 1. Install Athenaeum and initialise a knowledge base
pip install athenaeum
athenaeum init --path ~/knowledge

# 2. Bridge Claude Code auto-memory into raw/
bash examples/claude-code/setup-symlinks.sh --dry-run   # preview
bash examples/claude-code/setup-symlinks.sh             # apply

# 3. (Optional) Add the citation validator as a Claude Code Stop hook
#    Edit ~/.claude/settings.json — add a hook entry pointing at
#    examples/claude-code/stop-hook-validate.sh

# 4. Use Claude Code normally. Each memory write lands in
#    ~/.claude/projects/<scope>/memory/ and is immediately visible under
#    ~/knowledge/raw/auto-memory/<scope>/ via the symlink.

# 5. Compile raw → wiki
athenaeum run --path ~/knowledge

# 6. Inspect the consolidated entities
ls ~/knowledge/wiki/auto-*.md
```

Run step 5 on whatever cadence suits you: manually between sessions, via
cron/launchd, or as a post-session hook. The pipeline is idempotent — files
already consolidated are skipped on re-runs.

## See also

- [`examples/claude-code/README.md`](../../examples/claude-code/README.md) — per-turn recall hooks (complementary to this guide)
- [`docs/recall-architecture.md`](../recall-architecture.md) — hybrid FTS5 + vector recall details
- [`docs/why-athenaeum.md`](../why-athenaeum.md) — design rationale for the intake/compile split
