# Exit Codes

`athenaeum run` (and its `session-end`/`ingest` callers, which propagate the
same code through `IngestResult.exit_code` / `SessionEndResult.exit_code`)
returns one of the codes below. This is the canonical reference — other docs
([configuration.md](configuration.md)) link here instead of restating the
table.

| Code | Name | Meaning | Resumable? |
|---|---|---|---|
| `0` | — | Clean run. No failed files, no budget/deadline trip (or `--strict-budget` is off and one tripped anyway). | n/a |
| `1` | — | A file failed processing (retried next run), or `--strict-budget` is set and the run deferred work under the API-call budget. | Yes — deferred/failed work is picked up by the next run. |
| `75` | `EXIT_GRACEFUL_PARTIAL` | **athenaeum's own internal wall-clock deadline tripped.** Partial progress is committed (`git_snapshot`), the remaining/deferred intake is left on disk (`wiki/_deferred_work.md`), and the run stopped itself *before* anything external intervened. | **Yes.** Nothing was killed; the next run continues from where this one stopped. |
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
