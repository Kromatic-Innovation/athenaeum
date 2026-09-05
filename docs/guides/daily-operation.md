**Reference:** [librarian](../modules/librarian.md) · [intake](../modules/intake.md)

# Day-to-day operation

The nightly `athenaeum run` is the batch path: it reads pending `raw/`
files, runs them through the tiered librarian, and writes compiled entity
pages to `wiki/`. Everything below is what to reach for between nightly
runs, or once a wiki has been running for a while and needs upkeep.

## I want a just-remembered fact to be recallable right now

`remember` only writes to `raw/`; `recall` only reads the compiled `wiki/`
index; the librarian that connects them normally runs nightly. Without
intervention, a memory written by one agent is invisible to every other
agent — and to you — until the next nightly run. Two on-demand commands
close that gap without waiting for the batch job. Both are single-flight
(they share the same run lock as `run`) and print a one-line JSON summary
with counts and duration; both exit non-zero on failure.

```bash
# Compile only raw intake that's new/changed since the last ingest, then
# refresh the search index — the round-trip that makes a memory recallable.
athenaeum ingest              # --incremental is the DEFAULT (fast no-op if nothing changed)
athenaeum reindex             # --incremental hash-diff delta

athenaeum ingest --full       # recompile all pending raw intake
athenaeum reindex --full      # rebuild the index from scratch
athenaeum ingest --session <id>   # scope new/changed detection to one session
```

`ingest --incremental` tracks a content-hash stamp
(`~/.cache/athenaeum/ingest-manifest.json`), so it's a fast no-op when
nothing has changed. Pre-structured intake compiles with no LLM cost.
`reindex` is the canonical name; `rebuild-index` is a back-compat alias for
the exact same command.

## I want cross-agent recall to close automatically at session end

`athenaeum session-end` composes the two on-demand steps above into one
change-gated command, meant to be invoked by a SessionEnd hook (or the
nightly-after-librarian path):

```bash
athenaeum session-end                     # incremental ingest + reindex (DEFAULT)
athenaeum session-end --session <id>      # scope new/changed detection to one session
athenaeum session-end --full              # force a full recompile + full index rebuild
athenaeum session-end --dry-run           # cheap manifest-diff preview — no compile, no reindex, no model load
```

Both steps are change-gated so an idle SessionEnd is cheap:

1. **Incremental `ingest`** of the session's new/changed raw intake — a
   fast no-op (zero LLM) when nothing is new; structured entries compile
   with no model cost.
2. **Then `reindex`, but only when the compile actually ran and
   succeeded.** An idle SessionEnd does no LLM work and no reindex; a
   failed compile never indexes a half-built wiki; `--dry-run` touches
   nothing.

The result: a memory `remember`ed in one session becomes recallable as a
fully-resolved wiki entry the moment that session ends, with no waiting on
the nightly librarian. The hook that fires this at session end lives in
your Claude Code workspace config, not in this repo — this repo ships the
command it calls. See [Passive recall via hooks](sidecar.md).

## I want to find claims restated across different wiki entities

Read-only. Scans the wiki, embeds each claim, and prints a YAML report
grouping claims that recur across two or more distinct entities:

```bash
athenaeum claims --find
athenaeum claims --find --threshold 0.9 --path ~/knowledge
```

Default cosine cutoff is `0.85`. This command never mutates `wiki/` — it
only reports. With no embedding backend available it degrades to an empty
report rather than failing.

## I want to find and merge duplicate wiki pages

`dedupe wiki-pages` clusters already-compiled concept/reference/principle
pages by topic/embedding similarity — complementing the raw-intake
clustering that runs during `athenaeum run`:

```bash
athenaeum dedupe wiki-pages
athenaeum dedupe wiki-pages --dry-run --threshold 0.6
```

True duplicates are routed through the same `wiki/_pending_merges.md` /
`resolve_merge` approval flow used elsewhere — nothing is ever
auto-applied. Writing a proposal is idempotent: rerunning for a source set
already proposed is a no-op. `--threshold` overrides
`librarian.cluster_threshold` (default `0.55`). `athenaeum run` also runs
this pass automatically whenever `wiki/` exists; failures are logged and
non-fatal to the run.

## I want the resolved-decisions archive to stop growing unbounded

`resolve_merge` and `resolve_question` only flip the checkbox in place —
neither archives on its own. Two commands move resolved blocks out of the
live sidecar files:

```bash
athenaeum ingest-merges --path ~/knowledge
athenaeum ingest-answers --path ~/knowledge
```

`ingest-merges` moves every resolved block out of
`wiki/_pending_merges.md` into `wiki/_pending_merges_archive.md`
(newest-first, append-only). `ingest-answers` rewrites each answered
question as a raw intake file under `raw/answers/`, then moves it into
`wiki/_pending_questions_archive.md`; the next `athenaeum run` folds the
answer into the wiki entity. See [Answering pending questions](decisions.md)
for the full loop. Both are idempotent and should be scheduled
periodically — this is exactly the archival step that, left unrun, let a
live sidecar file grow into the millions of lines in production before
these commands existed.

## I want to retire operational scratch pages

`auto-memory prune` retires operational/ephemeral `wiki/auto-*.md` pages
(throwaway scratch scopes, install-token boilerplate, anything flagged
`ephemeral: true`), using the same classifier the intake gate applies:

```bash
athenaeum auto-memory prune              # dry-run: prints kill-list + retained-list
athenaeum auto-memory prune --apply      # git rm the kill-list, rebuild the recall index
```

Dry-run is the default and exits `2` when candidates exist (a usable CI /
sign-off signal), `0` when there's nothing to prune. `--apply` scopes its
commit to exactly the kill-list, so unrelated staged work is never swept
in. Recovery is git-only — the command refuses to run outside a git repo
and never hard-deletes.

## I want to archive expired daily-bucket pages

`decay-sweep` archives EXPIRED `bucket: daily` wiki pages — `weekly` /
`durable` / unbucketed pages are never candidates:

```bash
athenaeum decay-sweep              # dry-run: prints kill-list + retained-list
athenaeum decay-sweep --apply      # git-archive the kill-list, rebuild the recall index
```

Makes zero LLM calls — it's deterministic frontmatter/date comparison
only. `--apply` writes a provenance-snapshot commit, then a `git rm`
commit, mirroring the two-commit discipline elsewhere in this repo. See
the `bucket:` / `valid_until:` frontmatter contract in
[provenance shape](../design/provenance-shape.md) §8.8.

## See also

- Guides — [Answering pending decisions](decisions.md) · [Upgrading](upgrading.md) · [Troubleshooting](troubleshooting.md)
- Modules — [librarian](../modules/librarian.md) · [intake](../modules/intake.md) · [retention](../modules/retention.md)
- Design — [provenance shape](../design/provenance-shape.md)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
