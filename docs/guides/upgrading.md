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

## I want to know about the removed `rebuild-index` alias

`athenaeum rebuild-index` is removed. It was an undocumented back-compat
alias for `athenaeum reindex` — same command, same flags, same output —
and never printed a deprecation warning, so there is no warning to watch
for. If a cron job, wrapper script, or hook calls `athenaeum
rebuild-index`, change it to call `athenaeum reindex` instead.

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

## I want to know what's gone: Tier-4 dedup and five default-on librarian switches

Tier-4 pending-question dedup, the compile-time relatedness writer, delta
compile, the delta live client, and the nightly name-collision and
qualified-name scans were each on by default with an off switch. All six
switches are removed; the on behaviour now runs unconditionally:

| Removed switch | What it gated |
|---|---|
| `ATHENAEUM_TIER4_DEDUP` | Collapsing duplicate pending-question escalations by source-memory pair |
| `librarian.relatedness_writer` / `ATHENAEUM_RELATEDNESS_WRITER` | The compile-time `related:` edge writer |
| `librarian.delta.enabled` | Delta-scoped (vs. whole-corpus) compile on the deterministic path |
| `librarian.delta.live_client` | Delta-scoped compile on the nightly LLM run |
| `librarian.name_collision_scan` | The nightly exact-name-collision scan |
| `librarian.qualified_name_scan` | The nightly `name (qualifier)` entity-split scan |

A stale `athenaeum.yaml` still setting one of the four removed yaml keys
above loads fine — the key is ignored and logged with the same warning
every other removed key gets. Setting either removed environment variable
now has no effect.

If you previously set `librarian.delta.enabled: false` (or
`librarian.delta.live_client: false`) to force a whole-corpus compile
instead of a delta-scoped one, run `athenaeum run --full-compile` to get
the same whole-corpus compile for that run.

## I want to know about seven one-shot repair commands that are gone in 0.24.0

Each of these commands existed to repair data left behind by one specific,
now-fixed bug. Once the underlying defect was fixed, the command had nothing
left to do for anyone running a current install — so it is removed outright
in 0.24.0, with no deprecation period and no replacement:

| Removed command |
|---|
| `athenaeum auto-memory prune` |
| `athenaeum auto-memory prune-index` |
| `athenaeum auto-memory prune-code-entities` |
| `athenaeum dedup-oversize-escalations` |
| `athenaeum storage prune-dispositions` |
| `athenaeum storage audit-h1-redaction` |
| `athenaeum paste-cleanup` |

**If you are upgrading from an older release and still have the pre-fix data
on disk** (for example, you skipped several releases, or your knowledge base
predates the engine fix each command was written around), run the command you
need on your *current, pre-upgrade* release before upgrading — each one is
dry-run by default, so you can check first whether it finds anything to do.
Once you upgrade, the command itself is gone; its one-time job cannot be run
again from a newer release.

## I want to know what changed in the MCP decision-queue tool responses

The `resolve_question`, `resolve_merge`, `review_audit_item`, and
`raise_decision` MCP tool responses (and the matching `athenaeum` Python
helpers, `answers.resolve_by_id` and `answers.raise_pending_question`) no
longer carry a handful of legacy keys that duplicated other fields:

- **`error`** is gone everywhere it appeared. Read **`message`** instead —
  `error` was always an exact copy of it (or `None` on success).
- **`block`** is gone from `resolve_question` and `resolve_merge`. Read
  **`resolved_block`** instead — on these two tools it was, and still is,
  always `None`: both defer the actual question/merge rewrite to the next
  `ingest-answers` tick, so neither ever rewrites anything synchronously.
- **`block`** is gone from `raise_decision` (and the underlying
  `answers.raise_pending_question`). Read **`raw_block`** instead — `block`
  was always an exact copy of it (the newly rendered queue block on
  success, `None` on failure).

Nothing else changed: `ok`, `error_code`, `message`, `deferred`,
`answer_file`, `decision_id`, `raw_block`, and `resolved_block` (where it
already carried real data, e.g. `answers.resolve_by_id`) are all unchanged.

## See also

- Guides — [Daily operation](daily-operation.md) · [Troubleshooting](troubleshooting.md)
- Modules — [retention](../modules/retention.md) · [librarian](../modules/librarian.md)
- Design — [provenance shape](../design/provenance-shape.md) (§8.8, `bucket:`/`valid_until:` contract)
- Reference — [exit codes](../reference/exit-codes.md) · [configuration](../reference/configuration.md)
