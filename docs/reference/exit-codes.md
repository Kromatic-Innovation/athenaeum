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
| `75` | `EXIT_GRACEFUL_PARTIAL` **or** `EXIT_LOCK_HELD` | **Two unrelated constants both evaluate to `75` — see "`75` also collides with `EXIT_LOCK_HELD`" below (issue athenaeum#1379).** (1) `EXIT_GRACEFUL_PARTIAL` (`src/athenaeum/librarian.py`): athenaeum's own internal wall-clock deadline tripped. Partial progress is committed (`git_snapshot`), the remaining/deferred intake is left on disk (`wiki/_deferred_work.md`), and the run stopped itself *before* anything external intervened. (2) `EXIT_LOCK_HELD` (`src/athenaeum/_cli_shared.py`): any of the ten `_cmd_*` modules that call `_acquire_or_exit` — `athenaeum run` (`_cmd_run.py`), every `athenaeum curate` subcommand (`_cmd_curate.py`), `athenaeum decay-sweep` (`_cmd_decay.py`), `athenaeum drain` (`_cmd_drain.py`), `athenaeum index`'s `rebuild-index`/`ingest`/`session-end` (`_cmd_index.py`), `athenaeum pending`'s `ingest-answers`/`ingest-merges`/`reresolve-questions` (`_cmd_pending.py`), `athenaeum pii-restore` (`_cmd_pii_restore.py`), `athenaeum reconcile` (`_cmd_reconcile.py`), `athenaeum repair` and its `backfill-sources`/`bounce-fold` subcommands (`_cmd_repair.py`), or `athenaeum storage prune-dispositions` (`_cmd_storage.py`) — failed to acquire the run lock in `_acquire_or_exit` — **before any pipeline work started.** Nothing was committed and nothing was deferred to disk, so no clause of the `EXIT_GRACEFUL_PARTIAL` description above applies to it. | **Depends which producer.** `EXIT_GRACEFUL_PARTIAL`: **Yes** — nothing was killed, the next run continues from where this one stopped. `EXIT_LOCK_HELD`: **N/A** — no run happened, so there is nothing to resume; retry once the lock is free. |
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
mutating command — any of the ten `_cmd_*` modules that call it:
`athenaeum run` (`src/athenaeum/_cmd_run.py`), any `athenaeum curate`
subcommand (`src/athenaeum/_cmd_curate.py`), `athenaeum decay-sweep`
(`src/athenaeum/_cmd_decay.py`), `athenaeum drain`
(`src/athenaeum/_cmd_drain.py`), `athenaeum index`'s
`rebuild-index`/`ingest`/`session-end` (`src/athenaeum/_cmd_index.py`),
`athenaeum pending`'s `ingest-answers`/`ingest-merges`/`reresolve-questions`
(`src/athenaeum/_cmd_pending.py`), `athenaeum pii-restore`
(`src/athenaeum/_cmd_pii_restore.py`), `athenaeum reconcile`
(`src/athenaeum/_cmd_reconcile.py`), `athenaeum repair` and its
`backfill-sources`/`bounce-fold` subcommands
(`src/athenaeum/_cmd_repair.py`), or `athenaeum storage prune-dispositions`
(`src/athenaeum/_cmd_storage.py`) — cannot acquire the run lock. This happens **before any pipeline work starts**: no
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

`3` is not reused from anywhere else in this table (`0`/`1`/`75`/`124` are
all already spoken for) and is not a `sysexits.h` reservation the way `75`
is — it is simply the smallest unused small integer, matching this project's
existing convention of picking a small distinct code (`1`) for a generic
"something didn't fully succeed" condition and reserving specific codes
(`75`, `124`) only where a consumer needs to distinguish causes. `3` is
distinguishable from `75` (wall-clock deadline — already non-zero and
already resumable regardless of files committed, so it does not need this
code layered on top: the `EXIT_GRACEFUL_PARTIAL` branch in `run()`'s
finalize cascade is checked and returns FIRST, before the athenaeum#1135
check) and from `124` (nothing external intervened).

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

## Consumers

- **`athenaeum drain`** (`src/athenaeum/drain.py`) forces `max_runtime=0` and
  `install_signal_handlers=False` on every window it runs, so it cannot
  observe either `75` or `124` from those calls today — its rc check still
  names `EXIT_GRACEFUL_PARTIAL` defensively for future callers that might
  thread deadline/signal handling through.
- **The nightly cron wrapper and the Claude Code `SessionEnd` hook wrapper**
  (`scripts/hooks/knowledge-rebuild-index.sh` in
  `Kromatic-Innovation/code-workspace-config`, a different repo) wrap
  `athenaeum run` / `athenaeum session-end` in an external coreutils
  `timeout --signal=TERM`, and are exactly the callers this split is for.
  **As of this writing the wrapper has not yet been updated** to recognise
  `75` — that update is tracked separately at
  [cwc#2556](https://github.com/Kromatic-Innovation/code-workspace-config/issues/2556).
  Until it ships, a graceful `75` exit falls through the wrapper's generic
  non-zero-rc handling rather than its dedicated `TIMEOUT` path: no data is
  lost (partial progress is committed and the run is resumable exactly as
  described above), but the wrapper's logs and `INGEST_DEGRADED` signal will
  misreport a clean partial run as an unspecified failure until cwc#2556
  lands.
