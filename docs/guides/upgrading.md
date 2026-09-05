**Reference:** [retention](../modules/retention.md) · [librarian](../modules/librarian.md)

# Upgrading and data lifecycle

`athenaeum run` doesn't just add to your knowledge base — once a
non-contradictory cluster has been compiled into its canonical
`wiki/auto-<topic>.md` entry, the move-then-retire pass **moves** each
underlying raw fact into the wiki entry (as an origin-traced footnote) and
**`git rm`s the raw file** so it doesn't re-enter the nightly loop. This is
on by default. If you upgrade an existing install and run without reading
this section, raw auto-memory files will start disappearing from the
working tree — recoverable, but only from git history.

## I want to know what gets moved vs. what's held back

Only non-contradictory clusters are retired. A cluster is **held** in the
intake queue (never deleted) when:

- the detector flags a contradiction,
- detection degraded (offline / API error / unparseable response), or
- a member is referenced by an open entry in `_pending_questions.md` or
  `_pending_merges.md`.

When in doubt, the pass keeps the raw file.

## I want to recover a retired raw file

Recovery is git-only. The pass refuses to run when `knowledge_root` isn't
a git repo, and it never hard-`unlink`s. Each retirement lands as two
commits in your knowledge repo:

- **Commit A — provenance snapshot.** The raw intake about to be retired
  is committed first (a scoped `git add` of exactly those files), so every
  file that's about to be deleted is recoverable from history.
- **Commit B — move + delete together.** The wiki updates (new footnotes,
  a `retired: true` marker) and the raw `git rm`s land in a single commit,
  so the fact is never simultaneously absent from both the raw file and
  the wiki.

To recover, find commit B (or A) in your knowledge repo and
`git show`/`git checkout` the path.

**Warning:** recovery depends entirely on git history. Anything that
rewrites or discards that history can lose retired raw permanently — `git
gc` pruning unreachable objects, a squash/rebase that collapses the
snapshot commits, or simply never committing (running on a dirty repo) or
never pushing to a backup remote. If you rely on retired-raw recovery,
keep the knowledge repo's history intact and pushed.

## I want the knowledge repo pushed automatically after a run

A scheduled nightly run commits locally but doesn't push by default, so
origin silently drifts and the git-only recovery story only holds on the
machine that ran the librarian. Two ways to turn on a post-run push:

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
invokes `git push` (using your ambient git auth — credential helper or
SSH; no tokens or secrets are handled by athenaeum) after a successful run
that produced at least one commit. `--dry-run` never pushes; a run with no
new commits never pushes. A push failure is reported as a non-fatal
warning (log line `athenaeum-push-failed:`) — commits stay local and the
next run retries (`git push` is idempotent).

## I want to preview what a run would retire before it happens

```bash
athenaeum run --dry-run
```

Computes the exact same plan and logs a structured report without moving,
deleting, or committing anything.

## I want to turn move-then-retire off

```bash
athenaeum run --no-retire          # one run: skip the retire pass entirely
```

```yaml
# athenaeum.yaml — persistent opt-out
librarian:
  retire: false
```

The `--no-retire` CLI flag overrides the yaml toggle. When disabled, raw
auto-memory is neither moved into the wiki nor `git rm`'d — it stays in
the intake queue and is re-examined on every run.

## I want to know about the other destructive-by-design command

`athenaeum auto-memory prune --apply` is a second opt-in command that
`git rm`s pages (operational `wiki/auto-*.md`), with the same git-only
recovery story as move-then-retire. It's dry-run by default — see [Daily
operation](daily-operation.md).

## See also

- Guides — [Daily operation](daily-operation.md) · [Troubleshooting](troubleshooting.md)
- Modules — [retention](../modules/retention.md) · [librarian](../modules/librarian.md)
- Design — [provenance shape](../design/provenance-shape.md) (§8.8, `bucket:`/`valid_until:` contract)
- Reference — [exit codes](../reference/exit-codes.md) · [configuration](../reference/configuration.md)
