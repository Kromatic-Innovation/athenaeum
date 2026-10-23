# Codex context-file adapter

`athenaeum codex-context` is the first non-Claude sidecar adapter (issue
athenaeum#1364). It is a **Tier-2 static context-file adapter**
([contract §3.1](../extending/sidecar-adapter-contract.md)): a scheduled job
renders one snapshot of recall to `~/.codex/athenaeum-context.md`. There is no
per-turn push. It is a proof against the contract, not a supported
integration.

```bash
athenaeum codex-context --seed "projects I am working on" \
  [--output PATH|-] [--interval-seconds 900] [--max-age-seconds N] \
  [--n N] [--backend fts5|vector] [--cache-dir DIR]
```

One `--seed` string, one `build_context` call per refresh, `use_llm=False`,
empty exclude set, no seen-file. The file is replaced atomically (new files
0600; an existing file keeps its mode). The seed and the raw envelope are
never written to the file. No push telemetry is recorded.

## The AGENTS.md pointer (operator step)

The adapter never edits `~/.codex/AGENTS.md`. Add this line yourself:

> At the start of every session, read `~/.codex/athenaeum-context.md` (an
> Athenaeum knowledge snapshot, refreshed periodically; its header states the
> interval) and apply the freshness rule in its header before relying on
> anything in it.

**Unverified:** whether Codex follows that pointer is unverified. The local
Codex binary is quarantined, so its behaviour cannot be observed here. The
pointer is an instruction the agent follows, not a file the host inlines, so
this is Tier 2 with an instruction-mediated read. Check it yourself the next
time Codex runs.

## Scheduling (launchd example)

`examples/codex/com.athenaeum.codex-context.plist` is an example only; the repo
installs nothing. It sets `StartInterval` 900 and `RunAtLoad`, uses the venv's
absolute `bin/athenaeum`, and passes `--cache-dir` because launchd has no login
shell environment. Keep `StartInterval` equal to `--interval-seconds`: the
adapter does not schedule itself, and the snapshot only declares the cadence.

## Staleness

Every snapshot header has `refresh_interval_seconds`, `generated_at` and
`stale_after` (default `generated_at` + twice the interval, so one missed run is
survived). The reader applies the freshness rule printed in the file.

Expired ok snapshot (the job stopped running; time is now past `stale_after`):

```text
- status: ok
- refresh_interval_seconds: 900
- generated_at: 2026-10-23T14:00:00Z
- stale_after: 2026-10-23T14:30:00Z
```

Refresh-failed tombstone (written immediately, exit code 1, no bullets):

```text
- status: refresh-failed
- generated_at: 2026-10-23T14:15:00Z
- stale_after: 2026-10-23T14:15:00Z

STALE: the last refresh failed; no snapshot is available. ...
```

Other statuses: `empty` (no matches), `disabled` (kill switch scope all; the
snapshot is cleared on the next refresh), `withheld` (see egress).

**Dead-scheduler limitation.** If the launchd unit is unloaded or the venv is
broken, nothing rewrites the file. Only the reader comparing the current time
with `stale_after` can detect that; no file-side mechanism can.

## Data egress

Whatever Codex reads from the snapshot goes to OpenAI as model input: page
names and short descriptions, which can name people. The adapter fails closed
(operator ruling 2026-10-23): if any matching page has a non-public audience,
the whole snapshot is withheld with status `withheld` and no bullets. There is no
per-page filtering. Public pages are rendered as the core ranked them.

An untagged page counts as non-public: a page without `access: open` is stored
with audience `|`, which parses as not public, so one such page among the
candidates withholds the snapshot. Most pages are untagged until tagged, so
expect `withheld` for many seeds. To get bullets, tag the pages your seed
matches with `access: open`, and only if they are safe to send to OpenAI.
