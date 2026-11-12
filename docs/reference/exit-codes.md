# Exit Codes

`athenaeum run` (and its `session-end`/`ingest` callers, which propagate the
same code through `IngestResult.exit_code` / `SessionEndResult.exit_code`)
returns one of the codes below. This is the canonical reference — other docs
([configuration.md](configuration.md)) link here instead of restating the
table. `athenaeum entity` has its own, separate exit-code contract — see
"`athenaeum entity`" further down — documented here rather than in a second
file so a caller checking one command's codes doesn't miss that another
command's `1` means something else.

| Code | Name | Meaning | Resumable? |
|---|---|---|---|
| `0` | — | Clean run — no failed files, no budget/deadline/spend-ceiling trip, no `EXIT_LIBRARIAN_REFUSAL`-eligible zero-progress trip; or `--strict-budget` is off and a non-zero-progress trip happened anyway; or `--allow-degraded` waived an otherwise-refusal-eligible zero-progress trip. | n/a |
| `1` | — | A file failed processing (retried next run), or `--strict-budget` is set and the run deferred work under any resource budget/ceiling. | Yes — deferred/failed work is picked up by the next run. |
| `3` | `EXIT_LIBRARIAN_REFUSAL` | **DEGRADED REFUSAL (issue athenaeum#1135): the run stopped early for a resource reason (`reason=budget` / `spend-ceiling` / `entity-share`) AND committed ZERO files.** Before this code existed, this case fell through to `0` — indistinguishable from a genuine success by exit code, the exact gap this issue closes (`athenaeum drain` already refused loudly on the analogous "made ZERO progress" condition; this brings `athenaeum run` up to the same standard). The `librarian-run-degraded reason=<reason> files=0 [spend=<consumed>/<cap>]` marker line (ERROR) is ALWAYS logged when the predicate holds, regardless of this code — see "Why 3 is its own code" below. Default-ON (no flag needed to opt in); `--allow-degraded` opts OUT (exits `0` instead, marker line still fires); `--strict-budget` takes precedence when both flags are set (its broader "any deferral" check runs first and returns `1`). | **Yes** — nothing was lost; the deferred intake is picked up exactly like any other budget/deadline trip. |
| `75` | `EXIT_GRACEFUL_PARTIAL` **or** `EXIT_LOCK_HELD` | **Two unrelated constants both evaluate to `75` — see "`75` also collides with `EXIT_LOCK_HELD`" below (issue athenaeum#1379).** (1) `EXIT_GRACEFUL_PARTIAL` (`src/athenaeum/librarian.py`): athenaeum's own internal wall-clock deadline tripped. Partial progress is committed (`git_snapshot`), the remaining/deferred intake is left on disk (`wiki/_deferred_work.md`), and the run stopped itself *before* anything external intervened. (2) `EXIT_LOCK_HELD` (`src/athenaeum/_cli_shared.py`): any of the `_cmd_*` modules that call `_acquire_or_exit` (a count is deliberately not given here — it drifted twice; the enumeration below is the list `tests/test_exit_codes_doc_parity.py` checks) — `athenaeum run` (`_cmd_run.py`), `athenaeum attribution place`/`athenaeum attribution ack-observe-report` (`_cmd_attribution.py`), `athenaeum audit` (`_cmd_audit.py`), `athenaeum paste-cleanup` (`_cmd_paste_cleanup.py`), `athenaeum dedupe`/`athenaeum auto-memory` (`_cmd_curate.py`; `athenaeum claims` is read-only and never takes the lock), `athenaeum decay-sweep` (`_cmd_decay.py`), `athenaeum decisions challenge-coordinate` (`_cmd_decisions.py`, issue athenaeum#1994), `athenaeum decompose-page` (`_cmd_decompose_page.py`), `athenaeum correct-notes` (`_cmd_correct_notes.py`), `athenaeum drain` (`_cmd_drain.py`), `athenaeum import-memory` (`_cmd_import_memory.py`, issue athenaeum#2316), `athenaeum rebuild-index`/`athenaeum ingest`/`athenaeum session-end` (`_cmd_index.py`), `athenaeum ingest-answers`/`athenaeum ingest-merges`/`athenaeum reresolve-questions`/`athenaeum dedup-oversize-escalations` (`_cmd_pending.py`), `athenaeum pii-restore` (`_cmd_pii_restore.py`), `athenaeum quarantine requeue --apply` (`_cmd_quarantine.py`, issue athenaeum#2075), `athenaeum receipts anchor` (`_cmd_receipts.py`, issue athenaeum#2040), `athenaeum reconcile` (`_cmd_reconcile.py`), `athenaeum recover-parked` (`_cmd_recover_parked.py`, issue athenaeum#2078), `athenaeum repair` (`_cmd_repair.py`; the lock is acquired under every mode — `--tag-indent`/`--value-quoting`/`--legacy-source-slugs`/`--backfill-sources`/`--bounce-fold`/`--contact-records`/`--all`), `athenaeum retire-pages` (`_cmd_retire.py`), `athenaeum split-oversize` (`_cmd_split_oversize.py`, issue athenaeum#2078), `athenaeum storage prune-dispositions` (`_cmd_storage.py`), or `athenaeum subject-population --from-report --apply` (`_cmd_subject_population.py`) — failed to acquire the run lock in `_acquire_or_exit` — **before any pipeline work started.** Nothing was committed and nothing was deferred to disk, so no clause of the `EXIT_GRACEFUL_PARTIAL` description above applies to it. | **Depends which producer.** `EXIT_GRACEFUL_PARTIAL`: **Yes** — nothing was killed, the next run continues from where this one stopped. `EXIT_LOCK_HELD`: **N/A** — no run happened, so there is nothing to resume; retry once the lock is free. |
| `124` | `EXIT_EXTERNAL_KILL` | **An external kill signal (SIGTERM/SIGINT) was delivered to the process** — matching coreutils `timeout`(1) semantics, which itself exits 124 when it SIGTERMs a child that overran its wall clock. athenaeum's opt-in signal handler (`install_signal_handlers=True`, the CLI default) makes a best-effort partial-progress commit before re-raising this code, but the STOP REQUEST originated outside athenaeum's own deadline logic. | Best-effort — whatever was committed before the signal is resumable, but the commit itself is not guaranteed (a SIGKILL after the `timeout` grace period gives the handler no chance to run at all). |

## Why 75 and 124 are two different codes (issue athenaeum#897)

Before athenaeum#897, `run()` returned `124` for **both** cases above: its own
internal deadline check *and* a delivered external kill signal. That
collision made a clean, resumable, partial-progress run indistinguishable
from a hard kill from the exit code (or from log text) alone — a consumer
like the SessionEnd wrapper's `rc == 124` branch logged every graceful stop
as `TIMEOUT ... aborted` and set a degraded flag, even when nothing was
actually killed and no data was lost.

`75` is [`EX_TEMPFAIL`](https://man.freebsd.org/cgi/man.cgi?query=sysexits)
in the BSD `sysexits.h` convention — "temporary failure; user is invited to
retry" — which is exactly the semantics of a graceful internal-deadline stop:
partial progress committed, remaining work resumable on the next tick. `124`
keeps meaning exactly what coreutils `timeout` means by it: the process was
killed from outside.

**The contract going forward:** `124` is returned *only* by athenaeum's
signal handler (`_commit_partial_and_exit` in `src/athenaeum/librarian.py`),
reacting to a delivered SIGTERM/SIGINT. Every internal deadline-check path —
`RunContext.stop_on_deadline`, the entity-loop `deadline_tripped` finalize
branch, and every `RunDeadlineExceeded` catch site — returns `75`
(`EXIT_GRACEFUL_PARTIAL`) instead. Both constants are defined in
`src/athenaeum/librarian.py` next to `DEFAULT_MAX_RUNTIME`.

## `75` also collides with `EXIT_LOCK_HELD` (issue athenaeum#1379)

The section above fixed one `75` collision (the internal-deadline case vs an
external kill) by giving the external-kill case its own code, `124`. `75`
itself turns out not to be unique either: a second, unrelated constant
evaluates to the same integer.

`EXIT_LOCK_HELD = 75` (`src/athenaeum/_cli_shared.py`) is returned by the
`_acquire_or_exit` helper (also in `_cli_shared.py`, issue athenaeum#309) when a
mutating command — any of the `_cmd_*` modules that call it:
`athenaeum run` (`src/athenaeum/_cmd_run.py`), `athenaeum attribution place`/`athenaeum attribution ack-observe-report`
(`src/athenaeum/_cmd_attribution.py`), `athenaeum audit`
(`src/athenaeum/_cmd_audit.py`), `athenaeum paste-cleanup`
(`src/athenaeum/_cmd_paste_cleanup.py`, issue athenaeum#1717), `athenaeum dedupe`/`athenaeum auto-memory`
(`src/athenaeum/_cmd_curate.py`; `athenaeum claims` is read-only and never takes the lock), `athenaeum decay-sweep`
(`src/athenaeum/_cmd_decay.py`), `athenaeum decisions challenge-coordinate`
(`src/athenaeum/_cmd_decisions.py`, issue athenaeum#1994), `athenaeum decompose-page`
(`src/athenaeum/_cmd_decompose_page.py`, issue athenaeum#1914), `athenaeum correct-notes`
(`src/athenaeum/_cmd_correct_notes.py`), `athenaeum drain`
(`src/athenaeum/_cmd_drain.py`), `athenaeum import-memory`
(`src/athenaeum/_cmd_import_memory.py`, issue athenaeum#2316), `athenaeum rebuild-index`/`athenaeum ingest`/`athenaeum session-end`
(`src/athenaeum/_cmd_index.py`),
`athenaeum ingest-answers`/`athenaeum ingest-merges`/`athenaeum reresolve-questions`/
`athenaeum dedup-oversize-escalations` (`src/athenaeum/_cmd_pending.py`), `athenaeum pii-restore`
(`src/athenaeum/_cmd_pii_restore.py`), `athenaeum quarantine requeue --apply`
(`src/athenaeum/_cmd_quarantine.py`, issue athenaeum#2075), `athenaeum receipts anchor`
(`src/athenaeum/_cmd_receipts.py`, issue athenaeum#2040), `athenaeum reconcile`
(`src/athenaeum/_cmd_reconcile.py`), `athenaeum recover-parked`
(`src/athenaeum/_cmd_recover_parked.py`, issue athenaeum#2078), `athenaeum repair`
(`src/athenaeum/_cmd_repair.py`; the lock is acquired under every mode — `--tag-indent`/`--value-quoting`/`--legacy-source-slugs`/`--backfill-sources`/`--bounce-fold`/`--contact-records`/`--all`), `athenaeum retire-pages`
(`src/athenaeum/_cmd_retire.py`), `athenaeum split-oversize`
(`src/athenaeum/_cmd_split_oversize.py`, issue athenaeum#2078), `athenaeum storage prune-dispositions`
(`src/athenaeum/_cmd_storage.py`), or `athenaeum subject-population --from-report --apply`
(`src/athenaeum/_cmd_subject_population.py`) — cannot acquire the run lock. This happens **before any pipeline work starts**: no
file is compiled, no `git_snapshot` commit happens, and
`wiki/_deferred_work.md` is never written. None of the "partial progress
committed, remaining/deferred intake left on disk, resumable" language that
describes `EXIT_GRACEFUL_PARTIAL` above applies to a `75` produced this way —
a run that exits `75` for lock contention did not run at all, so there is
nothing to resume.

So a caller keying `rc == 75` off this document alone cannot tell "partial
progress committed, resumable" (`EXIT_GRACEFUL_PARTIAL`) apart from "another
process holds the lock, zero work done, nothing to resume, just retry"
(`EXIT_LOCK_HELD`) — the exact shape of collision the `75`/`124` split above
was written to prevent, now reproduced within `75` itself. Today the only
way to tell them apart from outside the process is the stderr text:
`_acquire_or_exit` prints `error: <LockHeld message>` before returning,
while a deadline trip logs through the normal `librarian-run-*` marker lines
instead and never prints that string.

**Renumbering `EXIT_LOCK_HELD` or `EXIT_GRACEFUL_PARTIAL` so the two no
longer share a value is a separate, open decision.** It is a caller-visible
contract change with its own review, out of scope for the issue that added
this section. This section only makes the existing collision legible; it
does not resolve it.

## Why 3 is its own code (issue athenaeum#1135)

Before this issue, a run that stopped early for a resource reason (budget /
spend-ceiling / entity-share) and, as a result, compiled ZERO files fell all
the way through `run()`'s return-code cascade to the default `0` — the SAME
code a fully successful run returns. A monitoring session reported such a
run as healthy 190s after it had exited having done nothing: every
deterministic phase still ran, the git snapshot commit (a no-op) still
happened, the `librarian-run-summary` line still logged
`calls=0 created=0 ... files=0 reason=budget`, but nothing in the exit code
said so. `athenaeum drain` (`src/athenaeum/drain.py`) already refuses loudly
on the analogous "a window made ZERO progress" condition (`log.error(...
"stopping loudly to avoid a spin")`, exit `1`); `EXIT_LIBRARIAN_REFUSAL`
brings the plain `athenaeum run` entry path up to that same standard.

`3` is not reused by anything else in `run()`'s OWN return-code cascade
(`0`/`1`/`75`/`124` are all already spoken for there), and is not a
`sysexits.h` reservation the way `75` is — it is simply the smallest unused
small integer within that cascade, matching this project's existing
convention of picking a small distinct code (`1`) for a generic "something
didn't fully succeed" condition and reserving specific codes (`75`, `124`)
only where a consumer needs to distinguish causes.

**`3` is, however, independently reused by three other, unrelated command
contracts** — the same shape of coincidental small-integer collision as the
`75`/`EXIT_LOCK_HELD` collision documented above, not a design relationship:
`athenaeum.surface_divergence.EXIT_DIVERGED` (`athenaeum surface-divergence`,
see "`athenaeum surface-divergence`" below), `athenaeum subject-population`'s
own stopped-at-ceiling code (`src/athenaeum/_cmd_subject_population.py`, see
"`athenaeum subject-population`" below), and `VERIFY_PROBLEM_EXIT`
(`src/athenaeum/_cmd_receipts.py`, see "`athenaeum receipts`" below) all
return the literal value `3` for a meaning that has nothing to do with
`EXIT_LIBRARIAN_REFUSAL` and shares no call path with `run()`. A caller
keying off `rc == 3` alone, without also knowing which command it ran,
cannot tell any of these four apart.

Within `run()`'s own cascade, `3` is distinguishable from `75` (wall-clock
deadline — already non-zero and already resumable regardless of files
committed, so it does not need this code layered on top: the
`EXIT_GRACEFUL_PARTIAL` branch in `run()`'s finalize cascade is checked and
returns FIRST, before the athenaeum#1135 check) and from `124` (nothing
external intervened).

**The contract:** `run()` returns `EXIT_LIBRARIAN_REFUSAL` only when (a) the
entity phase's `reason` (`RunContext.entity_exit_reason`) names an early
stop — `deadline`, `entity-share`, `budget`, or `spend-ceiling` — that is
not `"completed"`, AND (b) the run committed zero files
(`RunContext.files_processed_count == 0`, the same run-level figure the
athenaeum#899 zero-yield alarm reads), AND (c) the caller did not pass
`allow_degraded=True` (the CLI `--allow-degraded` flag). The
`librarian-run-degraded reason=<reason> files=0 [spend=<consumed>/<cap>]`
marker line (logged at ERROR) fires whenever (a) and (b) hold, REGARDLESS of
`(c)` — the exit code is the opt-out; the log line never is, so a cron
wrapper that only greps logs (rather than checking `$?`) still catches a
`--allow-degraded` run. Both constants (`EXIT_LIBRARIAN_REFUSAL` and the
predicate helpers) live in `src/athenaeum/librarian.py` next to
`EXIT_GRACEFUL_PARTIAL` / `EXIT_EXTERNAL_KILL`.

**Deliberately separate from the athenaeum#899 zero-yield alarm**
(`_zero_yield_tripped`, `src/athenaeum/librarian.py`): that predicate
requires `api_calls > 0` (a run that made zero calls is idle, not
wasteful) — exactly the gap `EXIT_LIBRARIAN_REFUSAL` fills, since a
budget-already-exhausted run trips the ceiling check BEFORE spending a
single call this run (`calls=0`). Neither predicate's logic feeds the
other.

**Propagation:** `athenaeum ingest` and `athenaeum session-end` both call
`librarian.run()` directly and propagate its return value unchanged (see the
top of this doc) — so a zero-progress refusal surfaces as
`EXIT_LIBRARIAN_REFUSAL` from those callers too, exactly like every other
code in this table already does. Two observable, and in both cases
beneficial, downstream effects of this propagation: `ingest()`'s
"stamp this ingest as successful" write (guarded by `exit_code == 0`) is
skipped for a run that compiled nothing, and `session_end()`'s
change-gated reindex step (`should_reindex`, also guarded by
`exit_code == 0`) is skipped rather than reindexing a wiki that provably did
not change. Neither path narrows what it reports beyond the exit code
itself; `IngestResult`/`SessionEndResult`'s other fields (`compiled`,
`new_or_changed`, etc.) are computed exactly as before.

## Which phases the two run-level caps bind (issue athenaeum#2132)

`--max-api-calls` and `--max-runtime` are run-level ceilings, and they bind
**every** LLM-spending phase, including the wiki-page dedup pass. Before
athenaeum#2132 that pass was the one exception, in two compounding ways:

- Its comparator Gate-2 calls landed their tokens and cost into the run's
  `TokenUsage` but never incremented `api_calls` — the counter
  `--max-api-calls` is denominated in, and the one the run summary's
  `economics.calls` reports. The phase's spend was invisible to the budget it
  was spending against.
- Its candidate-pair loop had no stop condition at all, and the caller checked
  the wall-clock deadline only once the whole pass had returned.

Together those let one run make 3980 calls against a cap of 150 and spend
16,386 s against a 3600 s deadline — and because the pass runs *before* the
entity, auto-memory, retire, reresolve and audit-drain phases, that night's
raw intake never happened.

Both caps are now checked **between candidate pairs**, before anything is
spent on the next one:

- Stopping early is resumable and decides nothing by force. The pairs not
  reached are simply not examined; the next run sees them exactly as this one
  did. Whatever the partial pass *did* decide is still flushed — ledgered
  verdicts, batched `coordinate` queue items, and the per-run attribution
  snapshot.
- The phase's `librarian-run-summary` segment reports `reason=budget` or
  `reason=deadline` instead of `reason=completed`, plus `pairs_examined` and
  `pairs_remaining` so the size of the remaining backlog is on the record.
- When a wall-clock trip ends the run at or before this phase, the summary
  additionally names every phase the trip **starved** —
  `entity secs=0.000 reason=skipped-deadline` and the same for `auto-memory`,
  `retire`, `reresolve` and `audit-nightly-drain`. A starved night used to be
  visible only as the *absence* of those segments, which reads identically to
  "that phase is disabled".

A run that stops this way still exits `75` (`EXIT_GRACEFUL_PARTIAL`) with
partial progress committed, exactly as the table above describes.

## `athenaeum entity`

A separate command family from `run()`'s cascade above — its own small
integer space, not a continuation of the table at the top of this doc.

| Code | Name | Meaning |
|---|---|---|
| `0` | — | The uid resolved to a page; the record printed to stdout (redaction markers in place of withheld fields unless `--include-excluded`). |
| `1` | `EXIT_NOT_FOUND` | The uid does not resolve to any page. |
| `70` | `EXIT_INTERNAL_ERROR` | The uid DID resolve, but something else in the read/serialize path failed (e.g. an unserializable frontmatter value) before a full record could be printed. |

### Why `1` and `70` are two different codes (issue athenaeum#1270)

Before this issue, `_read_entity_to_stdout` (`src/athenaeum/_cmd_query.py`)
had no `try`/`except` around its `json.dumps(result.to_dict(), ...)` call. A
page whose frontmatter carried a raw `datetime.date`/`datetime` value (a bare
YAML date, unquoted) made that call raise `TypeError: Object of type date is
not JSON serializable`, which propagated uncaught out of `main()` and exited
`1` — the exact SAME code the "no page for this uid" branch already
returned. A caller keying off the exit status alone (`google-contact-sync`'s
`read_person()` maps every nonzero exit that isn't argparse's `invalid
choice` to "unknown uid") could not tell an existing-but-broken record from a
genuinely absent one; a person who exists but holds a `date` value read as
not existing, silently.

The `TypeError` itself is fixed independently (issue athenaeum#1110 — a
`default=` handler coercing `date`/`datetime` to ISO-8601). Fixing only that
would leave the collision itself in place: the next unserializable type, or
any other read-path failure past the not-found check, would reproduce this
exact bug. `_read_entity_to_stdout` now wraps everything past the not-found
check in `try`/`except Exception`, returning `EXIT_INTERNAL_ERROR` (`70`,
BSD `sysexits.h`'s `EX_SOFTWARE`) instead of letting it fall through to
Python's default uncaught-exception exit status. `EXIT_NOT_FOUND` (`1`) and
`EXIT_INTERNAL_ERROR` (`70`) are both defined in `src/athenaeum/_cli_shared.py`
next to `EXIT_LOCK_HELD`.

This table's `1` is unrelated to the `run()` table's `1` above (`run()`'s
`1` means "a file failed processing") — each command family owns its own
small-integer contract; nothing here is a claim that the two `1`s share
meaning across commands.

## Other command-specific exit codes

The two tables above are `run()`'s cascade and `athenaeum entity`'s small
contract — the two families big enough, and reused by enough other commands
(`ingest`, `session-end`), to warrant a table of their own. Every other
`_cmd_*.py` command also returns `0` on success, and most return a plain `1`
for a generic, command-specific operational failure (a missing root
directory, a caught exception, a validation error, an apply attempted
without a git repository, ...) that this doc does not enumerate per command
— that `1` carries no further distinguishable meaning the way `run()`'s or
`athenaeum entity`'s `1` do, and a future reader should not infer one from
its absence here. What follows is every DISTINCT, non-generic code a
`_cmd_*.py` handler returns, grouped by shared convention where one is
shared, so a wrapper branching on a non-`0`/`1` code (or on a command-
specific `1`) has somewhere to look.

### `2` as a usage error (dispatcher convention)

A command whose own CLI surface has an optional (not `required=True`)
further sub-action — `athenaeum axiom {promote,demote,list}`, for example —
prints a `usage: ...` line to stderr and returns `2` itself when that
sub-action is missing or unrecognized. This mirrors argparse's own exit
code for a usage error (the code argparse itself would return if the
sub-action were instead a required positional); it is not a distinct
athenaeum-specific contract, so it is named here once rather than per
command. The same code covers a small number of other usage/argument
validation failures that are not a missing sub-action — an invalid `--since`
value (`athenaeum spend`), a disabled feature flag a command refuses to run
without (`athenaeum dedupe wiki-pages`, `athenaeum merges recompare`), or an
unresolvable `--into`/`--merge` reference (`athenaeum merges propose-fold`)
— all reported the same way: a message to stderr, then `2`, before any work
starts.

Commands following this convention: `athenaeum authority`, `athenaeum axiom`,
`athenaeum schema`, `athenaeum description`, `athenaeum dimensions`,
`athenaeum memory-class`, `athenaeum questions`, `athenaeum triage`,
`athenaeum verdicts`, `athenaeum calibration`, `athenaeum schedule`,
`athenaeum push-metrics`, `athenaeum attribution`, `athenaeum decisions`,
`athenaeum merges`, `athenaeum dedupe`, `athenaeum claims`, `athenaeum spend`,
`athenaeum session-end` (its own `--references-only`/`--session` usage
check), and `athenaeum subject-population` (`--from-report`/`--apply`
combined incorrectly).

### `2` meaning "found something" (dry-run / lint convention)

Two overlapping groups of commands use `2` the opposite way: not as an
error, but as a **positive finding**, so a CI job or sign-off script can
distinguish "ran clean" from "found something to act on" without parsing
output.

**Pure lint reports (no apply mode; `0`/`2` only):** `athenaeum fold-lint`
(`EXIT_VIOLATIONS_FOUND`), `athenaeum outbound-lint` (`EXIT_PII_FOUND`),
`athenaeum bounce-contract` (`EXIT_NONCONFORMING`), `athenaeum storage
lint-pii` (`EXIT_PII_FOUND`), `athenaeum storage lint-mapping`
(`EXIT_MAPPING_ISSUES`), and `athenaeum storage audit-h1-redaction` (also
`EXIT_PII_FOUND` — a non-defect, marker-mid-heading finding alone does not
trip it; see `_cmd_storage.py`'s own docstring).

**Dry-run/apply commands (`0`/`1`/`2` ternary — "mirroring `repair`" is the
literal phrase several of these modules' own docstrings use):** `0` is a
clean run (nothing to do, or `--apply` succeeded with no errors); `1` is an
error (apply attempted without git, an unreadable page, a lookup failure,
...); `2` is a dry run that found something that WOULD be changed if
`--apply` were passed — this is the CI/sign-off signal. Members:
`athenaeum repair` (every mode), `athenaeum decay-sweep`, `athenaeum
retire-pages`, `athenaeum reconcile`, `athenaeum pii-restore`, `athenaeum
auto-memory prune`, `athenaeum auto-memory prune-index`, `athenaeum
auto-memory prune-code-entities`, and `athenaeum ingest --evaluate-only`
(the one member with no `--apply` of its own — `2` means "a trigger fired;
run `ingest --if-triggered` next", per `_cmd_ingest_evaluate_only`'s own
docstring, which names `decay-sweep` and `repair` as the pattern it mirrors).

### `athenaeum contact-surface-lint`

Its own small contract, deliberately inverted from the lint convention
above (and never named in the `EXIT_LOCK_HELD` list above — it is read-only
and never takes the run lock):

| Code | Meaning |
|---|---|
| `0` | Clean: nothing in any base report, and (with `--attribution`) no suspect verdict. |
| `1` | Findings: the surface, or the attribution sweep, found something to report. |
| `2` | Error: the `pii` class is not mapped to an excluded surface, the resolved contacts root IS the wiki root, or any other setup/read failure. |

### `athenaeum surface-divergence`

| Code | Name | Meaning |
|---|---|---|
| `0` | — | The field is not diverged (or reads clean). |
| `2` | `EXIT_SURFACE_UNREADABLE` | The excluded surface could not be read. |
| `3` | `EXIT_DIVERGED` | The registered field has diverged beyond its declared allowance — **reuses the literal value `3`**; see "Why 3 is its own code" above. |

(`EXIT_DIVERGED` and `EXIT_SURFACE_UNREADABLE` are defined in
`src/athenaeum/surface_divergence.py`, not `_cli_shared.py`.)

### `athenaeum subject-population`

`2` follows the usage-error convention above (an invalid `--from-report`/
`--apply` combination). Its own distinct code: stopping early because the
run's confirmer-call ceiling was hit (`report.stopped_due_to_ceiling`)
returns `3` — **also reusing the literal value `3`**; see "Why 3 is its own
code" above. The run is resumable: `athenaeum subject-population --resume
<report-path>`.

### `athenaeum import-memory`

`EXIT_INCOMPLETE = 1` (`src/athenaeum/_cmd_import_memory.py`) — a DISTINCT
meaning from a generic command failure, even though it shares the literal
value `1`: the import ran but did not finish importing everything it was
given (as opposed to failing outright). `0` is a complete import; any other
failure before that point is a generic, undifferentiated error (also `1` —
this command does not distinguish the two by exit code, only by the error
text it prints).

### `athenaeum receipts`

Its own small contract, spelled out in `_cmd_receipts.py`'s own module
docstring; `anchor` is the only one of its four verbs that takes the run
lock and can therefore return `EXIT_LOCK_HELD` (`75`) — see the
`EXIT_LOCK_HELD` producer list above.

| Code | Name | Meaning |
|---|---|---|
| `0` | — | Clean: sealed/verified everything, or no incident. |
| `2` | — | Usage: `seal`/`verify` run with no `signing` sealer configured. |
| `3` | `VERIFY_PROBLEM_EXIT` | `verify`: at least one seal failed or one range is unsealed — **reuses the literal value `3`**; see "Why 3 is its own code" above. |
| `4` | `KEY_UNAVAILABLE_EXIT` | `seal`: the configured key provider could not be reached. Not an error in the usual sense — reported distinctly from a crash. |
| `5` | `INCIDENT_EXIT` | `attest-readout`: at least one incident exists (a provenance miss, a hook-error class, or a non-zero session-close deficit). No threshold — one incident is enough. |

### `athenaeum ingest-answers` / `ingest-merges` / `reresolve-questions` / `dedup-oversize-escalations`

All four (`_cmd_pending.py`) distinguish an EXPECTED validation failure
(`1` — an unresolvable item, a malformed input file, ...) from an
UNEXPECTED exception raised while applying, after the run lock was already
acquired (`2`, always released via `finally` before returning). A caller
that only checks "zero or not" does not need the distinction; one that logs
differently for "this needs a human to fix the input" versus "this is a
bug" does.

## Consumers

- **`athenaeum drain`** (`src/athenaeum/drain.py`) forces `max_runtime=0` and
  `install_signal_handlers=False` on every window it runs, so it cannot
  observe either `75` or `124` from those calls today — its rc check still
  names `EXIT_GRACEFUL_PARTIAL` defensively for future callers that might
  thread deadline/signal handling through.
- **An external wrapper** that runs `athenaeum run` / `athenaeum session-end`
  under a coreutils `timeout --signal=TERM` is exactly the caller the
  `75`/`124` split above is for. If such a wrapper has not been updated to
  recognise `75` as the graceful-partial case, a graceful `75` exit falls
  through its generic non-zero-rc handling rather than any dedicated
  timeout path it has. No data is lost either way — partial progress is
  committed and the run is resumable exactly as described above — but an
  un-updated wrapper's own logging may misreport a clean partial run as an
  unspecified failure. This is a gap in the wrapper's own rc-handling, not
  in athenaeum; this doc does not track deployment-specific wrapper state.
