**Reference:** [retention](../modules/retention.md) · [librarian](../modules/librarian.md) · [decisions](decisions.md)

# Upgrading and data lifecycle

## I want to know what's gone in 0.24.0

`athenaeum merges` and `athenaeum questions` are removed in 0.24.0. Every
command either surface offered has a direct `athenaeum decisions`
replacement — same flags, output and exit codes, just under the unified
queue's name:

| Removed command | Replacement |
|---|---|
| `athenaeum merges list` | `athenaeum decisions list` |
| `athenaeum merges next` | `athenaeum decisions next` |
| `athenaeum merges count` | `athenaeum decisions count` |
| `athenaeum merges revalidate` | `athenaeum decisions revalidate` |
| `athenaeum merges recompare` | `athenaeum decisions recompare` |
| `athenaeum merges scrub-pii` | `athenaeum decisions scrub-pii` |
| `athenaeum merges propose-fold` | `athenaeum decisions propose-fold` |
| `athenaeum merges provenance` | `athenaeum decisions provenance` |
| `athenaeum questions list` | `athenaeum decisions list` |
| `athenaeum questions next` | `athenaeum decisions next` |
| `athenaeum questions count` | `athenaeum decisions count` |

On 0.23.x the left column still works and prints a one-line deprecation
warning to stderr naming its own replacement from the table above; on 0.24.0
each one is an unknown command. The switch that silenced those warnings
(`librarian.deprecated_cli_surfaces_enabled` and
`ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED`) is removed with them; a stale
yaml key is ignored with a warning, like every other removed key. See
[Answering pending decisions](decisions.md) for the full
`athenaeum decisions` surface.

`athenaeum run` doesn't just add to your knowledge base — once a
non-contradictory cluster has been compiled into its canonical
`wiki/auto-<topic>.md` entry, the move-then-retire pass **moves** each
underlying raw fact into the wiki entry (as an origin-traced footnote) and
**`git rm`s the raw file** so it doesn't re-enter the nightly loop. This is
on by default. If you upgrade an existing install and run without reading
this section, raw auto-memory files will start disappearing from the
working tree — recoverable, but only from git history.

## I want to know about the removed stranded-ledger warnings

Earlier releases moved the spend ledger (`spend.jsonl`), the LLM-schema
observation ledger (`_llm_schema_observations.jsonl`), and the push-records
ledger (`_push_records.jsonl`) out of the knowledge root and into your
cache directory, and for a while athenaeum logged a one-time warning per
process if it found a leftover copy of one of these files still sitting at
its old knowledge-root location.

Those warnings are gone. If a leftover ledger from before that move is
still sitting at the old location, athenaeum now simply ignores it — it is
never read and never written. Nothing is deleted or migrated automatically;
if you want those old rows, merge the leftover file into the corresponding
file in your cache directory yourself, then remove the old copy.

## I want to know which ledgers stopped being written, and where the old files are

A cleanup audit found five internal ledgers written by live code but read
by nothing outside their own tests. One is kept and newly exposed; the
other four are no longer written at all. Nothing on disk from before this
change is touched — each leftover file is simply ignored from now on, and
you may delete it:

- **Adapter provenance — kept, now readable.** `athenaeum provenance
  adapter --uid <uid>` prints which adapter and source object produced a
  compiled page, or says none was recorded. `--json` for machine output.
  This is the one ledger of the five that is still written; nothing about
  it changed except that you can now read it from the command line.
- **Compile routes — stopped.** The per-file "which page did this raw file
  write to" record is no longer kept. The one-way routing GUARD it rode
  alongside (refusing or logging a write that lands on the wrong person's
  page) is unaffected. Old file: `_compile_route_records.jsonl` under your
  cache directory (`~/.cache/athenaeum` by default, or `ATHENAEUM_CACHE_DIR`
  if you set it).
- **LLM schema observations — stopped.** The per-parse "did the model's
  response match the expected shape" record is no longer kept; a shape
  mismatch is still logged to the process log as before, just no longer
  durably recorded. The `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED` environment
  variable that used to gate this is gone — setting it now does nothing.
  Old file: `_llm_schema_observations.jsonl` under your cache directory.
- **Wiki-dedupe attribution report — stopped.** The per-run snapshot of
  which candidate page pairs the dedupe pass examined, and why each one
  did or didn't become a merge proposal, is no longer written. The dedupe
  pass itself still proposes (or doesn't propose) merges exactly as before.
  Old files: `_wiki_dedupe_attribution.jsonl` and its timestamped rotations
  under `wiki/` in your knowledge directory.
- **Never-ingest refusals — stopped.** The append-only record of which
  files were refused for matching a configured never-ingest class is no
  longer kept. The refusal itself is unchanged: a matching file is still
  excluded from compilation this run and still left untouched on disk.
  Old file: `_never_ingest_refusals.jsonl` under your cache directory.

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
