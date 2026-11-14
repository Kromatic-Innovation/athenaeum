# `athenaeum schedule install` / `remove` on real hosts, 2026-11-14

Verification for athenaeum#2366, of the command built in athenaeum#2304 (epic
athenaeum#2296). athenaeum#2304 proved generation and the install/remove logic
with unit tests and mocked loaders. This report records a real scheduled tick
on two hosts, with no login environment.

## Summary

| # | Acceptance criterion | Result |
|---|---|---|
| 1 | macOS: `install` loads a launchd unit, a scheduled tick compiles a pending raw file with no login environment, run log shows the key source resolved | **Pass** (see note on "key source resolved") |
| 2 | Linux: the same with the cron or systemd entry | **Pass for cron** in a container. **systemd: not run**; split to athenaeum#2597 by operator ruling. |
| 3 | `remove` leaves no unit, entry or generated file behind, checked by listing unit locations before and after | **Pass** by operator ruling. No unit or entry remains on either host and the platform listings return to baseline. Run logs remain and an empty crontab remains (Linux); the ruling is that leftover run logs do not fail this criterion, and log cleanup is follow-up athenaeum#2596. See AC3. |
| 4 | `doctor` reports scheduled after install and unscheduled after remove, both hosts | **Pass** |
| 5 | Counter-example: no key source, `install` refuses and says why | **Pass** on both hosts |
| 6 | Dated report with host OS versions, version, repeatable steps, results | This document |

Defects found, filed as their own issues: athenaeum#2583 (binary resolved from
`PATH`, not the running environment) and athenaeum#2584 (unit does not carry
the cache directory given to `install`).

## Hosts and version

| | macOS | Linux |
|---|---|---|
| Host type | Real machine (arm64 Mac) | **A Docker container, not a VM or bare metal**, on that same machine's Docker 29.7.2 Linux engine |
| OS | macOS 26.6.2 (build 25G83) | Debian GNU/Linux 12 (bookworm) image `python:3.13-slim-bookworm`, kernel 7.0.12-linuxkit |
| Python | 3.13.12 (venv) | 3.13.18 |
| Scheduler | launchd (user LaunchAgent) | **cron** (`cron` daemon started by hand; no init system, no `systemctl`) |
| User | the login user | an unprivileged user created in the container |

- athenaeum 0.23.5, built from `origin/develop` at commit
  `200b7c12ed3586f7ebf623749efda2ae4901c580`, installed with `pip install` from
  a worktree of that commit (macOS: into a fresh venv; Linux: copied into the
  container and installed into a venv).
- Because a container has no systemd, `detect_platform()` selected `cron` on
  Linux by itself. The systemd user-timer generator was **not exercised on a
  real host**; AC2 passes through the cron option the issue allows. A host with
  a systemd user session is still unverified; that verification is split to
  athenaeum#2597 by operator ruling.

## Key source

The mechanism athenaeum#2304 documents: with `llm.provider: api`, the key is
read from the process environment or `<cache_dir>/config.env`, and embedded
verbatim in the unit so the tick needs no login shell.

- The key was an Anthropic API key fetched from 1Password into a throwaway
  `config.env` (mode 0600) and never printed. The same file was deleted
  afterwards; on Linux the container, including its crontab, was destroyed.
- `athenaeum init` writes `llm.provider: claude-cli` when a `claude` binary is
  on `PATH` (macOS did, the container did not). The macOS config was edited to
  `provider: api` to exercise the key-file path. The `claude-cli` branch of
  `resolve_key_source` was **not** tested on a real host.
- Spend: three compiles, about USD 0.005 in total (two raw files each on the
  first macOS tick and on Linux, one on the second macOS tick).

## Steps (repeatable)

Use a throwaway knowledge root and a throwaway cache directory. Never point the
unit at a real knowledge root.

### Setup (both hosts)

1. Build `athenaeum` from the commit above into a fresh venv; confirm
   `athenaeum --version`.
2. `athenaeum init --path <throwaway-root>`; on a host where init chose
   `claude-cli`, edit `athenaeum.yaml` to `llm:` / `provider: api`.
3. Record the baseline: platform unit directory listing, the loader's list of
   athenaeum units (`launchctl list`, `crontab -l`), and the log directory
   listing. macOS: `LaunchAgents`, `launchctl list`, `~/Library/Logs`.
   Linux: `crontab -l`, `ls -a ~`, `~/.config/systemd/user`, `~/.local`.
4. Put two or three invented raw files under
   `<throwaway-root>/raw/auto-memory/<scope>/` (frontmatter `name`,
   `description`, `metadata.type`, plus a short body about invented people and
   places).

### AC5, counter-example (before any key exists)

5. With `ANTHROPIC_API_KEY` unset and an empty cache directory, run
   `athenaeum schedule install --path <root> --cache-dir <empty-cache>`.
   Expect exit 1, the `no key source available ... Refusing to install a
   schedule that would fail every night` message, and no new unit.
6. `athenaeum doctor --path <root> --cache-dir <empty-cache>`: expect
   `schedule: not scheduled`.

### AC1/AC2, install and a real tick

7. Write `ANTHROPIC_API_KEY=<key>` to `<cache>/config.env` (umask 077, from a
   secret store, not echoed).
8. Pick a minute two to three minutes ahead. Run
   `athenaeum schedule install --path <root> --cache-dir <cache> --hour H
   --minute M`, with the `athenaeum` you built first on `PATH` (see
   athenaeum#2583). Expect exit 0.
9. Check the generated unit: macOS `plutil -lint` on the plist and read it with
   the key masked; Linux `crontab -l`. Confirm absolute paths and that the only
   environment is the key.
10. Linux only: start the `cron` daemon (`/usr/sbin/cron`) as root.
11. `athenaeum doctor ...`: expect `schedule: scheduled`.
12. Wait for the minute. macOS: `launchctl print gui/<uid>/com.athenaeum.run`
    shows `runs = 1`, `last exit code = 0`, and the log is
    `~/Library/Logs/athenaeum-run.log`. Linux:
    `~/.local/state/athenaeum/schedule-run.log`.
13. Confirm the raw files moved into `wiki/` and the log shows the compile.

### AC3/AC4, remove

14. `athenaeum schedule remove ...`: expect exit 0.
15. `athenaeum doctor ...`: expect `schedule: not scheduled`.
16. Re-list everything recorded in step 3 and diff against the baseline.
17. Run `remove` a second time: expect a no-op, exit 0.

## Results

### AC5: counter-example, passes on both hosts

- macOS: exit 1 with the refusal message; the LaunchAgents listing was
  unchanged; `doctor` reported `not scheduled (launchd)`.
- Linux: exit 1 with the same message; `crontab -l` still reported no crontab;
  `doctor` reported `not scheduled (cron)`.

### AC1: macOS, passes

- `install` exited 0, wrote one plist (mode 0600, `plutil -lint` OK) and
  `launchctl list` then listed `com.athenaeum.run`.
- The plist had absolute paths and one environment variable, the key. No
  `PATH`, no `HOME` from a login shell.
- At the scheduled minute launchd started the job: `runs = 1`,
  `last exit code = 0`. The log shows two raw files discovered, four Anthropic
  API calls returning `200 OK`, two wiki pages written, "moved 2 raw file(s)
  into 2 wiki entr(y/ies)" and "~$0.0020 estimated".
- **Key source resolved:** the log has no line that says so in words. The
  evidence is that the only place the key existed for the job was the plist's
  `EnvironmentVariables`, and the calls succeeded. Criterion met on that
  reading; a line naming the key source would make it checkable at a glance.
- The tick ran twice. The **first tick ran a different
  athenaeum install than the one that ran `install`** (athenaeum#2583), so it
  was repeated with the built venv first on `PATH`; the second tick ran the
  built commit's binary (`launchctl print` `program =` the venv), compiled one
  raw file, and exited 0.

### AC2: Linux (cron, container), passes for cron

- `install` selected `cron` and wrote one tagged crontab line with an absolute
  `athenaeum` path, the key inline, output appended to
  `~/.local/state/athenaeum/schedule-run.log`.
- The cron daemon fired the line at the scheduled minute. The log shows four
  `200 OK` API calls, two wiki pages written, "moved 2 raw file(s)", about USD
  0.0020. The raw directory was emptied and `wiki/auto-bakery.md` and
  `wiki/auto-lighthouse.md` exist.
- systemd was not run (no systemd in the container).

### AC3: `remove`, pass by operator ruling

Unit and entry: gone on both hosts, and re-listing the platform's unit
locations returned to baseline.

- macOS: LaunchAgents listing and `launchctl list` equal the baseline after
  `remove`. `doctor` reports `not scheduled`.
- Linux: `crontab -l` lists no athenaeum entry; `~/.config/systemd/user` does
  not exist.

Residue the criterion's wording ("no generated file behind") would count:

- **macOS:** `~/Library/Logs/athenaeum-run.log` remains. launchd creates it
  from the plist's `StandardOutPath`; `remove` does not delete it. Not
  mentioned in `schedule.py`. Deleted by hand to return to baseline.
- **Linux:** `~/.local/state/athenaeum/schedule-run.log` and its parent
  directories remain. This is deliberate and documented in `schedule.remove`
  (an operator may want the run history).
- **Linux:** after the last entry is removed, the user's crontab is an empty
  file, not "no crontab" as at baseline (`crontab -l` prints nothing instead of
  `no crontab for <user>`).

**Operator ruling:** the leftover run logs do not fail AC3. Cleaning them up
(and, if wanted, the empty crontab) is follow-up athenaeum#2596. The first
draft of this report called the criterion partial pending that ruling.

### AC4: `doctor`, passes on both hosts

- After `install`: `schedule: scheduled: launchd plist present at ...` (macOS)
  and `schedule: scheduled: crontab entry present (tagged
  'athenaeum-schedule-managed')` (Linux).
- After `remove`: `schedule: not scheduled (launchd)` / `not scheduled (cron)`.
- Also `not scheduled` before any install, on both hosts.

## Findings

Filed:

- **athenaeum#2583**: `resolve_athenaeum_binary` uses `shutil.which`, so an
  `install` run from one environment schedules whichever `athenaeum` is first on
  `PATH`. Silent; seen on macOS.
- **athenaeum#2584**: `--cache-dir` is read for the key but not embedded in the
  unit, so the scheduled run uses the default cache directory. On the first
  macOS tick this overwrote the default cache's auto-memory manifest and
  recovery-yield state and appended rows to the spend and run-summary ledgers
  on the verification host, because the throwaway knowledge root shares the
  global cache. Reported to the host owner; the files were not repaired here. The
  second macOS tick had `ATHENAEUM_CACHE_DIR` added to the plist by hand and
  did not touch the default cache (checked by comparing the manifest before and
  after).

Not filed (judgment calls, for the issue owner):

- The empty crontab left by `remove`; run-log cleanup is athenaeum#2596.
- The key is embedded in plain text in the unit (plist mode 0600, crontab line
  readable by the owning user via `crontab -l`). Probably the documented trade
  for "no login shell", but worth a line in the docs. The systemd unit file's
  mode was not checked (not run).
- The run log does not name the key source it used (AC1 note).
- Logs from a scheduled tick report `auto-memory: recovery yield ... below
  threshold` and `librarian-zero-yield` warnings on a fresh throwaway root;
  expected for a root with no history, noted so a reader does not mistake them
  for a schedule failure.

## What was left on each host

- macOS: nothing of the schedule remains. LaunchAgents, `launchctl list`,
  crontab and the Logs directory match the baseline taken before `install`.
  The throwaway key file was deleted. The default cache directory was modified
  by the first tick as described under athenaeum#2584.
- Linux: the container was destroyed, taking the crontab, the key file and the
  logs with it. The two images pulled for it remain in the local Docker cache.
