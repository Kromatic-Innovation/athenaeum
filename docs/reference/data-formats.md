# Data formats

**Reference page.** The on-disk shapes: what raw intake looks like, what a compiled wiki
page looks like, and what a run leaves behind.


**Raw intake** lives in `raw/{source}/*.md` with the naming convention
`{timestamp}-{uuid8}.md` (e.g., `20240406T120000Z-aabb0011.md`). Each file is
a plain markdown document containing observations, notes, or session
transcripts. The `{source}` directory identifies the origin (e.g.,
`sessions`, `imports`).

**Wiki entity pages** live in `wiki/` with YAML frontmatter:

```yaml
---
uid: a1b2c3d4
type: person
name: Alice Zhang
aliases: [Alice]
access: internal
tags: [active]
created: '2024-04-06'
updated: '2024-04-06'
---
```

Entities are indexed in `wiki/_index.md` grouped by type. Conflicts requiring
human review are appended to `wiki/_pending_questions.md`. Each run logs
token usage and estimated costs at the end.

**Degraded runs.** When a run exhausts its API call budget (see
`ATHENAEUM_MAX_API_CALLS` above), it writes a `wiki/_deferred_work.md`
manifest itemizing the raw files it did not process and ends with a
warning-level `Done (DEGRADED — budget exhausted)` summary line — the
machine-greppable signal that intake was deferred rather than completed.
The deferred files stay on disk and are picked up automatically by the
next run. The manifest is overwritten on every budget-tripped run and
cleared by the next clean run (full, merge-only, or cluster-only).
The cap is enforced at the entity-tier loop; merge-phase and re-resolve
calls count toward the budget but do not themselves stop the run, so a
merge-heavy run can overshoot the cap before enforcement kicks in.
A degraded run still exits `0` by default; pass `athenaeum run
--strict-budget` to make a budget-tripped run exit nonzero instead —
opt-in, for exit-code-based alerting.

## Data lifecycle and upgrade impact

> **Upgrade impact (0.10.0) — `athenaeum run` now MOVES and DELETES raw
> auto-memory by default.** `raw/auto-memory/` is an *expiring intake queue*,
> not a permanent store. As of 0.10.0, once the librarian has compiled a
> cluster into its canonical `wiki/auto-<topic>.md` entry and the
> contradiction detector has run clean, the move-then-retire pass (issue )
> **moves** each non-contradictory raw fact into the wiki entry (as an
> origin-traced footnote) and **`git rm`s the raw file** so it no longer
> re-enters the nightly loop. This is on by default. If you upgrade and run
> without reading this, your raw auto-memory files will start disappearing
> from the working tree — recoverable, but only from git history.

**What is moved vs. held.** Only non-contradictory clusters are retired.
A cluster is **held** in the queue (never deleted) when the detector flags a
contradiction, when the detection degraded (offline / API error / unparseable
response), or when a member is referenced by an open entry in
`_pending_questions.md` / `_pending_merges.md`. When in doubt, the pass keeps
the raw.

**Recovery is git-only.** The pass refuses to run when `knowledge_root` is not
a git repo, and it never hard-`unlink`s. Each retirement lands as two commits
in your knowledge repo:

- **Commit A — provenance snapshot.** The raw intake about to be retired is
  committed first (scoped `git add` of exactly those files) so every deleted
  file is recoverable from history.
- **Commit B — move + delete together.** The wiki updates (new footnotes,
  `retired: true` marker) and the raw `git rm`s land in a single commit, so the
  fact is never simultaneously absent from both the raw file and the wiki.

To recover a retired raw file, find commit B (or A) in your knowledge repo and
`git show`/`git checkout` the path. **Warning:** because recovery depends
entirely on git history, anything that rewrites or discards that history can
lose retired raw permanently — `git gc` pruning unreachable objects, a
squash/rebase that collapses the snapshot commits, or simply never committing
(running on a dirty repo) / never pushing to a backup remote. If you rely on
retired-raw recovery, keep the knowledge repo's history intact and pushed.

**Pushing after every run (opt-in, issue ).** A scheduled nightly run
commits locally, but does not push by default — so origin silently drifts and
the git-only recovery story only holds on the machine that ran the librarian.
Two ways to turn on a post-run push so origin stays current:

```bash
athenaeum run --push               # one run: push after this run
```

```yaml
# athenaeum.yaml — persistent opt-in
librarian:
  push_after_run: true
  # Optional; defaults are origin + the current branch's upstream.
  # push_remote: origin
  # push_branch: develop
```

The `--push` CLI flag overrides the yaml toggle. When enabled, athenaeum
invokes `git push` (using the operator's ambient git auth — credential helper
/ SSH; no tokens or secrets are handled by athenaeum) after a successful run
that produced at least one commit. `--dry-run` never pushes; a run with no
new commits never pushes. A push failure is reported as a non-fatal warning
(distinct log line `athenaeum-push-failed:`) — commits remain local and the
next run retries (`git push` is idempotent).

**`--dry-run`** computes the exact same plan and logs a structured report
without moving, deleting, or committing anything — use it to preview what a
run would retire.

**Disabling it.** Move-then-retire stays on by default, but you can turn it
off two ways:

```bash
athenaeum run --no-retire          # one run: skip the retire pass entirely
```

```yaml
# athenaeum.yaml — persistent opt-out
librarian:
  retire: false
```

The `--no-retire` CLI flag overrides the yaml toggle. When disabled, raw
auto-memory is neither moved into the wiki nor `git rm`'d; it stays in the
intake queue and is re-examined on every run.

> **Related destructive operation.** `athenaeum auto-memory prune --apply` is a
> second opt-in command that `git rm`s pages (operational `wiki/auto-*.md`),
> with the same git-only recovery story as move-then-retire. It is dry-run by
> default — see [Daily operation](../guides/daily-operation.md).


## See also

- [Intake](../modules/intake.md) — what may be written to `raw/`, and what is refused.
- [Librarian](../modules/librarian.md) — how raw becomes a wiki page.
- [Shape](../modules/shape.md) — the rules a claim must satisfy.
- [Upgrading](../guides/upgrading.md) — what a version bump does to an existing wiki.
