# Claude Code auto-memory integration

**Reference:** [MCP surface](../modules/mcp.md) · [Intake](../modules/intake.md) · [Recall](../modules/recall.md)

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
flattened working-directory path (e.g. `-Users-you-Code` for work started
in `/Users/<you>/Code`). You can have many scopes; symlink each
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

> **Claude Code's native writer emits four keys by default** — `name`,
> `description`, `metadata.type`, and (2.1.214+) `modified` — and nothing in
> that set carries provenance. Left alone, a natively-written memory therefore
> reaches the librarian with neither `sources[]` nor `originSessionId`, which
> is the gap origin-session recovery exists to close.
>
> **Those four keys are a prompt convention, not a writer-enforced schema.**
> Memory files are authored with the ordinary `Write` tool rather than
> serialized through a fixed struct, so additional frontmatter keys can be
> written and they persist verbatim into `raw/auto-memory/`. A project or user
> `CLAUDE.md` can therefore instruct the writer to stamp `originSessionId`
> (and the other fields in the table below), and a natively-written memory
> *can* opt into this path. Verified end-to-end: a memory written with
> `originSessionId`, `claim_kind` and `sources[]` was claimed by discovery,
> compiled, and retired into a wiki entry carrying a populated `sources[]`
> where the native default would have left it empty.
>
> Prefer this to recovery where you can get it — `intake` gates recovery on
> `origin_session_id is None and not sources`, so a declared session id wins,
> and unlike recovery it never depends on file mtimes surviving the operator's
> sync method. But treat it as an *optimization over* recovery, never a
> replacement: compliance is best-effort per session, and whether the extra
> keys survive a Claude Code rewrite is not established — it stamps a
> `modified` timestamp (2.1.214+), which implies it does touch frontmatter on
> files that have it. Recovery remains the backstop for every file the
> convention missed.
>
> Stamp only keys this path actually reads (see the field reference below).
> Extra keys are harmless but inert — they persist in `raw/` and are discarded
> at compile.
>
> Provenance for natively-written memories comes from **origin-session
> recovery at intake** instead, which needs nothing from the memory file. Athenaeum resolves the session that wrote it from the
> scope's own transcripts — exactly, when a transcript shows a writing
> tool-use naming the file; otherwise from a unique write-time window — and
> feeds the recovered session into the same `_am_as_implicit_source` fallback
> the `sources[]` path falls back to. When neither resolves unambiguously
> (transcript rolled off, or two concurrent sessions in one project) the
> memory keeps `sources: []` rather than being attributed to a guess.
>
> Recovery supplies an **origin, not a verdict**: the claim keeps the honest
> `inferred` `source_type` until `transcript_verify.verify_user_stated`
> confirms it against the transcript itself.

Required/recommended fields:

| Field             | Required | Description                                                                 |
|-------------------|----------|-----------------------------------------------------------------------------|
| `name`            | yes      | Short memory slug, e.g. `project_acme_corp`                                 |
| `description`     | yes      | One-line summary                                                            |
| `type`            | no       | One of `project`, `reference`, `feedback`, `user` (matches file prefix)     |
| `originSessionId` | yes (strict) | Claude Code session UUID that produced this memory                      |
| `originTurn`      | yes (strict) | Turn index within that session                                          |
| `sources[]`       | append-only | List of source maps with `session`, `turn`, optional `excerpt`          |

### Full field reference

Every frontmatter key read off an auto-memory file by
`intake.discover_auto_memory_files` — i.e. the keys that reach an
`AutoMemoryFile` and survive into `merge.render_merged_entry`. All are optional
unless the table above marks them required, and (except `bucket`, noted below)
**all fail open**: an unrecognized value is dropped and the compile continues,
so a guessed value is worse than an omitted key.

> **This is the auto-memory path only.** Athenaeum has a second, separate
> intake path for entity-schema pages (`tier0_handle_upsert` → `WikiEntity`),
> which reads a different key set with different defaults — including `access`,
> `tags` and `aliases`, whose vocabularies each operator defines in their own
> `wiki/_schema/`. **None of those three is read off an auto-memory file**, so
> stamping them on a memory does nothing. Don't carry a key across from one
> table to the other, and read your own store's vocabularies via the
> `entity_schema` MCP tool rather than copying another deployment's.

The vocabularies below are core-code constants, identical in every deployment.

| Field | Vocabulary / shape | Absent or invalid |
|---|---|---|
| `metadata.type` | `feedback` \| `project` \| `reference` \| `user` \| `recall`. **Only a fallback**: the primary claim path is the filename (`<type>_<slug>.md`, `AUTO_MEMORY_FILE_RE`), and frontmatter is consulted only when the filename misses — which is the normal case for Claude Code, whose writer names files `<kebab-slug>.md`. A top-level `memory_type` is read as a further fallback, and a scalar `metadata: feedback` is tolerated. | With neither a conforming filename nor a recognized declared type, the file is **silently skipped** — not routed anywhere else. It becomes invisible to every discovery path. This is the one field whose absence loses the memory outright. |
| `originSessionId` | Claude Code session UUID — the basename of the scope's `~/.claude/projects/<scope>/<uuid>.jsonl` transcript. | Origin-session recovery runs instead, resolving from the scope's transcripts and the file's mtime. |
| `originTurn` | Integer turn index within that session. | Omitted from the synthesized source ref; the session alone still resolves. |
| `sources[]` | List of maps: `session`, optional `turn`, optional `excerpt`, plus `source_type` / `source_ref`. Deduped on `(session, turn)`. | Falls back to a synthetic source built from `originSessionId`, then to recovery. Empty at every stage ⇒ `sources: []` on the compiled page. |
| `source_type` | `user-stated` \| `agent-observed` \| `external` \| `document` \| `inferred` \| `model-prior`. `agent-observed`, `inferred` and `model-prior` are the AI-attributed channels and should carry `model:`. | Defaults to `inferred` — the honest fallback for an origin that cannot be established. Never silently promoted to `user-stated`. |
| `source_ref` | Free-form ref: a URL, an issue ref, or a `<session>#turn<N>`. Never the raw `auto-memory/...` filename — that shape is rejected. | Back-filled from session + turn. |
| `claim_kind` | `fact` \| `observation` \| `opinion` \| `decision` \| `policy` \| `definition`. Drives the resolver's stance short-circuit; `opinion` routes a conflicting pair to `attribute_both` rather than picking a winner. **An author-supplied value is never overwritten and skips the classifier's LLM call entirely** (`librarian._stamp_unclassified_claim_kinds`). | The nightly run classifies it in one cheap LLM call and stamps it once. On failure, `""` (unclassified) — the resolver's LLM path decides as before. An out-of-vocabulary value logs a debug breadcrumb and is discarded. |
| `valid_from` / `valid_until` | ISO dates bounding the claim's validity. Travel with the claim into each compiled source record, so validity is per-claim rather than per-page. | Unbounded. |
| `model` | The model that produced the claim. Expected on the AI-attributed `source_type` channels; validation is fail-open, so it is not enforced. | `""`. |
| `on_behalf_of` | Who the claim was made for. | `""`. |
| `asserter` | Structured asserter annotation. | `{}`. |
| `bucket` | `daily` \| `weekly` \| `durable` — the decay bucket, set at intake and carried onto the compiled page. | `""` at read time, but note this is the **one key that does not fail open on write**: an invalid value raises rather than being discarded. Omit it unless you mean it. |
| `supersedes` / `superseded_by` / `refines` | Ref to another memory or entity. | Unset; no supersession edge is drawn. |
| `deprecated` | Truthy marker. | Not deprecated. |

**Read on the entity path only, *not* here:** `observed_at`, `access`, `tags`,
`aliases`, `created`, `updated`. Stamping any of them on an auto-memory file is
a no-op the compile discards.

A `CLAUDE.md` convention that stamps `originSessionId` and `claim_kind`
captures most of the available value in two lines. What each one buys:

- `claim_kind` — **saves one LLM call per memory, once ever.** The nightly run
  otherwise classifies each unclassified file in a cheap Haiku call and stamps
  the result back into the file; a valid author-supplied value is skipped
  outright, and the run-loop wrapper short-circuits before even re-reading the
  frontmatter.
- `originSessionId` — saves transcript-scanning I/O, not tokens
  (`session_recovery` makes no LLM call), and beats recovery on accuracy
  wherever mtimes stop reflecting write time.

Add `originTurn` too if the writer can determine it reliably — the Section 4
validator checks for it alongside `originSessionId`, and it sharpens the
synthesized `<session>#turn<N>` ref. A *wrong* turn index is worse than an
absent one, though, so omit it rather than guess (and trim the template's check
if your writer cannot supply it).

Hand-authoring `sources[]` is **not** recommended: choosing `source_type`
correctly means checking the claim against the transcript, which is
`transcript_verify.verify_user_stated`'s job, and a confidently-wrong
`external` is worse than a recovered `inferred`.

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
they just land in the consolidated wiki, relying on origin-session recovery
(above) for provenance. If you want to enforce "every fact carries a source"
for the intake you *do* control, run the stop-hook validator (Section 4) to
warn when auto-memory files lack `originSessionId`/`originTurn`. The
validator can be made blocking or non-blocking; start non-blocking while you
bootstrap the habit. Point it at your adapter's or your own output —
running it blocking over `*/memory/*.md` fails on every natively-written
file by construction, for a field that writer cannot emit.

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
   in `wiki/_pending_questions.md`. You resolve them by editing the file
   directly, or via the `resolve_question` MCP tool (which records the
   answer as a decision-answer file under `raw/answers/`, see
   [`docs/design/contradiction-detection.md`](../design/contradiction-detection.md#decision-answer-files-unified-decision-resolution-as-intake-athenaeum908));
   `athenaeum ingest-answers` then applies it deterministically and folds
   the resolution back into the wiki.

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
- [`docs/design/recall-architecture.md`](../design/recall-architecture.md) — hybrid FTS5 + vector recall details
- [`docs/why-athenaeum.md`](../why-athenaeum.md) — design rationale for the intake/compile split
