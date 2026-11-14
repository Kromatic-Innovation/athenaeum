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
```

`ingest --incremental` tracks a content-hash stamp
(`~/.cache/athenaeum/ingest-manifest.json`), so it's a fast no-op when
nothing has changed. Pre-structured intake compiles with no LLM cost.
Both also take `--full` (recompile/rebuild from
scratch) and `--session <id>` (scope to one session) — see the
[CLI reference](../reference/cli.md#athenaeum-ingest) for every flag.

## I want cross-agent recall to close automatically at session end

`athenaeum session-end` composes the two on-demand steps above into one
change-gated command, meant to be invoked by a SessionEnd hook (or the
nightly-after-librarian path):

```bash
athenaeum session-end                     # incremental ingest + reindex (DEFAULT)
athenaeum session-end --dry-run           # cheap manifest-diff preview — no compile, no reindex, no model load
```

`--session <id>` and `--full` are also available — see the
[CLI reference](../reference/cli.md#athenaeum-session-end) for every flag.

Both steps are change-gated so an idle SessionEnd is cheap:

1. **Incremental `ingest`** of the session's new/changed raw intake — a
   fast no-op (zero LLM) when nothing is new; structured entries compile
   with no model cost.
2. **Then `reindex`, when the compile ran — or when the index is stale.**
   A compile that ran always reindexes. A no-op compile consults the index
   manifest (a stat-prefiltered corpus walk; no chromadb, no embedding
   model) and reindexes only if pages are actually pending, so a page that
   missed its indexing window is picked up by a later tick instead of
   drifting out of the index forever. An idle SessionEnd against an
   already-current index does no LLM work and no reindex; a failed compile
   never indexes a half-built wiki; `--dry-run` touches nothing.

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
```

Default cosine cutoff is `0.85`, overridable with `--threshold`. This command
never mutates `wiki/` — it only reports. With no embedding backend available
it degrades to an empty report rather than failing. See the
[CLI reference](../reference/cli.md#athenaeum-claims) for every flag.

## I want to find and merge duplicate wiki pages

`dedupe wiki-pages` clusters already-compiled concept/reference/principle
pages by topic/embedding similarity — complementing the raw-intake
clustering that runs during `athenaeum run`:

```bash
athenaeum dedupe wiki-pages
```

True duplicates are routed through the same `wiki/_pending_merges.md` /
`resolve_merge` approval flow used elsewhere — nothing is ever
auto-applied. Writing a proposal is idempotent: rerunning for a source set
already proposed is a no-op. `--threshold` overrides
`librarian.cluster_threshold` (default `0.55`). `athenaeum run` also runs
this pass automatically whenever `wiki/` exists; failures are logged and
non-fatal to the run. See the
[CLI reference](../reference/cli.md#athenaeum-dedupe-wiki-pages) for every flag.

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

## I want to be sure a quiesce hold is actually mine before I apply

`athenaeum quiesce` pauses the scheduled `ingest --if-triggered` path so a
long-running write lane isn't contended against. Before relying on that
pause — right before an apply, not just when you first set it — confirm the
hold is still yours:

```bash
athenaeum quiesce --status
```

Read the `holder` field back and compare it against the identity you
expect: the auto-derived `<user>@<hostname>`, or whatever you passed
explicitly to `--holder`. A hold set by someone (or something) else, or one
that has silently expired and been re-taken, is not a barrier you can trust
— `--status` is cheap and read-only, so there's no reason to skip it.

If you took the quiesce with an explicit `--holder` (for example, a lane
identity rather than the default OS user), release it with the **same**
`--holder` value:

```bash
athenaeum quiesce --release --holder my-lane-identity
```

A release whose `--holder` doesn't match a still-active sentinel's
on-disk holder now refuses rather than silently dropping someone else's
hold — the JSON response carries a `refused` marker and the sentinel stays
on disk. `--force` overrides the check when you deliberately need to clear
a hold that belongs to another party.

## The nightly run keeps asking me to recompile a page

That is the page-recompile pass, and it is **on by default**. Every night,
after the entity and auto-memory phases and before the run finalizes, the
librarian looks for pages the page-size invariant says are too big, drafts a
plan for cutting the biggest few into a parent page plus linked sub-pages,
and raises each plan as a `page-recompile` item in `athenaeum decisions`.
Nothing is rewritten until you answer one.

**Which pages get picked up** is decided by one existing setting:
`librarian.page_size_threshold_chars`. A page whose body exceeds it — or one
already flagged `needs_split: true` — is a candidate. Pages above
`librarian.oversize_hard_ceiling_chars` go first, then the most recently
updated, then the largest.

**What it costs.** The pass makes real model calls every night: a cheap
labelling pass over the page's sections, one planning call, and one call that
checks each sentence of the proposed parent summary against the sections it
cites. Five knobs bound it, all under `librarian.page_recompile`:

| Key | Default | What it bounds |
|---|---|---|
| `max_pages_per_run` | 3 | Pages planned per run, once no candidate is over the hard ceiling |
| `opening_max_pages_per_run` | 10 | Pages planned per run while **any** candidate is still over the hard ceiling |
| `max_input_chars_per_run` | 200000 | Total page-body characters fed to a model per run |
| `max_api_calls_per_run` | 40 | The pass's own model calls per run, counted against the run's `librarian.max_api_calls` |
| `max_open_items` | 5 | Unanswered `page-recompile` items before the pass stops raising new ones — and stops spending |

The larger opening budget is not a second knob you have to remember to change:
it applies automatically while the corpus still holds an over-ceiling page and
drops to the steady budget on the first run where none does.

A page that already has an unanswered item pinned to its current content is
skipped before any model call, so an item you have not got to yet costs
nothing on later nights and does not consume a slot the rest of the backlog
needs. Edit that page and it is re-planned: the item was pinned to bytes the
page no longer holds, so no answer to it could have been applied anyway.

The pass takes no reserved share of the run's wall-clock budget. It runs last,
so it simply uses what the earlier phases left, is skipped entirely when the
run deadline has already tripped, and re-checks the deadline between pages.

**Three answers**, on each item:

- **approve** — carry out the plan. The parent keeps its identity and a cited
  summary, the sub-pages are created and linked both ways, and it all lands
  under the run lock. Reversible with `athenaeum recompile undo`.
- **approve-structure-only** — authorise only the in-page restructure, and
  nothing that would touch another page.
- **reject** — leave the page alone. The pass will not ask again until the
  page's content actually changes, so a rejection is not a decision you have
  to keep repeating.

If you answer an item after the page has been edited, nothing is applied: the
plan is pinned to the content it was drafted over, the stale item drops out of
the queue, and the pass drafts a fresh plan for the new text on a later run.

**Modes**, via `librarian.page_recompile.mode`:

```yaml
librarian:
  page_recompile:
    mode: queue     # off | shadow | queue (default) | auto
```

- `off` — **the off switch.** The phase returns before it even builds a model
  client, so it costs nothing at all.
- `shadow` — plans are drafted and recorded, but no page is written and no
  item is raised. The cheapest way to see what the pass would propose.
- `queue` — the default, described above.
- `auto` — accepted, and currently behaves exactly like `queue`. The
  unattended applier it is reserved for has not shipped;
  `librarian.page_recompile.auto_apply_class` (`none` or `structure-only`) is
  likewise read and reported today but acted on by that later work, not now.

Where the pass stands is in `athenaeum status`, under *Page recompile
backlog*: how many candidates there are, how many plans are awaiting your
approval, and how many nights it has been since the pass last made progress.

## I want to undo a recompile I just applied

`athenaeum recompile apply` rewrites one oversized page into a parent plus
sub-pages, and prints the plan id it applied. `athenaeum recompile undo`
takes that plan id back out again. Check what it would do first — the dry
run writes nothing at all:

```bash
athenaeum recompile undo --dry-run <plan-id>
athenaeum recompile undo <plan-id>
```

Both forms take the run lock, so an undo never races the nightly compile;
they exit 75 while another process holds it.

There are exactly three outcomes, and the dry run names the one you would
get plus the reason for it:

- **direct** — every page the apply touched still holds exactly what the
  apply wrote. Each one is restored byte for byte from the pre-image the
  apply recorded, the sub-pages it created are deleted, and the whole undo
  lands as one commit in the knowledge store. A merged section's originals
  come back verbatim.
- **queued** — a page has been edited since the apply (or its pre-image is
  no longer in the store's git objects, which happens when the page was
  never committed before the apply ran). Nothing is written. One item goes
  into the pending-decisions queue carrying a diff of the pre-image against
  what is on disk now, so you can see exactly what a restore would
  discard. Answer it with `athenaeum decisions`.
- **refused** — another page already links to one of the created
  sub-pages, so deleting it would leave a dangling link. The command
  reports how many such links there are, and where each one is, and
  changes nothing. Re-point those links first, then undo. All four ways a
  page can reference another count: a `related` row, a `parent:` key, a
  `[[uid|Label]]` wikilink, and an ordinary markdown inline link whose
  target is the sub-page's own `uid-slug.md` filename.

A page restored by an undo is not re-planned while it still holds the
restored text, so an unattended pass cannot immediately re-apply what you
just undid. Editing the page lifts that on its own — the skip is a content
comparison, not a timer.

**The coarse fallback.** When undo reports queued or refused and you want
the apply gone regardless, revert the apply's own snapshot commit in the
knowledge store. Each apply makes exactly one commit, subject
`page-recompile apply <uid> (<plan-id>)`:

```bash
git -C <knowledge_root> log --oneline --grep="page-recompile apply"
git -C <knowledge_root> revert <commit-sha>
```

That reverses the whole apply — including any merged section's originals,
which is why the originals stay reachable through that commit — but it
reverts the page wholesale, so any later edit to the page is reverted with
it. That is the trade the finer-grained command exists to avoid.

## See also

- Guides — [Answering pending decisions](decisions.md) · [Upgrading](upgrading.md) · [Troubleshooting](troubleshooting.md)
- Modules — [librarian](../modules/librarian.md) · [intake](../modules/intake.md) · [retention](../modules/retention.md)
- Design — [provenance shape](../design/provenance-shape.md)
- Reference — [configuration](../reference/configuration.md) · [CLI](../reference/cli.md) · [exit codes](../reference/exit-codes.md)
