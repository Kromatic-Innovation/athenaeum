# `athenaeum schedule install` / `remove` with systemd, 2026-11-16

Verification for athenaeum#2597. This is the systemd reading that
athenaeum#2366 left open. The earlier report,
[schedule-host-verification-2026-11-14.md](schedule-host-verification-2026-11-14.md)
(PR athenaeum#2586), covered launchd and Linux cron. Its container had no
init system, so the systemd user timer that athenaeum#2304 generates never
ran. This report runs it in a container where systemd is PID 1, with a real
`systemd --user` manager, and lets the timer fire a real tick.

**Two runs.** The first run, on athenaeum 0.24.0, found that `remove` left
systemd's persistent-timer stamp behind. That was filed and fixed as
athenaeum#2707 (PR athenaeum#2716). The whole procedure was then repeated from
scratch on athenaeum 0.25.0, which includes the fix. The results below are
from that **re-run**. The first run's findings are kept under
[First run (0.24.0)](#first-run-0240).

## Summary

| # | Acceptance criterion | Result |
|---|---|---|
| 1 | On a systemd host, `install` loads the systemd unit or timer, and a scheduled tick compiles a pending raw file with no login environment | **Pass.** The timer fired at its calendar minute. The service exited 0 and both raw files compiled into `wiki/`. The tick's environment was the user manager's defaults plus the unit's `Environment=` lines. |
| 2 | `remove` leaves no unit, timer or generated file behind, checked by listing the systemd user unit locations before and after | **Pass.** The service, the timer, the `timers.target.wants` symlink, the run log, the run-log directory and systemd's persistent-timer stamp are all gone. No file remains. Empty directories remain (see AC2). On 0.24.0 the stamp was left behind; that was fixed by athenaeum#2707. |
| 3 | `doctor` reports scheduled after install and unscheduled after remove | **Pass** |
| 4 | Dated report with host type, versions and repeatable steps, no host-specific names or paths | This document |

The LLM was a **local stub**: a script standing in for the `claude` binary
that returns canned JSON (see [The LLM stub](#the-llm-stub)). No credential,
API key or login token was used, and nothing left the container. So this run
proves the scheduling and environment path. It says nothing about compile
quality. A tick against a real model was not run on systemd. The 2026-11-14
report covers real-model ticks on launchd and cron.

The same run also re-checked the three defects that blocked this issue. All
three are fixed on the systemd path (see [Blocker re-checks](#blocker-re-checks)).

## Host and version

| | |
|---|---|
| Host type | **A Docker container, not a VM or bare metal**, booted with `/sbin/init` (systemd) as PID 1, on Docker 29.7.2's Linux engine on an arm64 Mac |
| OS | Debian GNU/Linux 12 (bookworm), `debian_version` 12.15, from image `python:3.13-slim-bookworm`; kernel 7.0.12-linuxkit; cgroup v2 |
| systemd | 252 (252.39-1~deb12u2). `systemctl is-system-running` reported `running` |
| Python | 3.13.18 |
| Scheduler | `systemd --user` manager (`user@1000.service`), started by `loginctl enable-linger` |
| User | An unprivileged user created in the image. `athenaeum` and `claude` were both absent from its `PATH` |
| Clock | Container time is UTC. Every time below is UTC |

- Re-run: athenaeum 0.25.0, built from `origin/develop` at commit
  `c53e19fa27d7914958892b9c693797c8be2f4936`, which contains the
  athenaeum#2707 fix (`e243f11f`). First run: athenaeum 0.24.0 at
  `0b6cffd85a43f1a0c95fdeb25f8d0f313eac32b1`. Each build was exported with
  `git archive`, copied into the container and installed with `pip install`
  into a venv at `~/venv`.
- `detect_platform()` found `systemctl` on `PATH` and chose `systemd` by
  itself. No `--backend` flag was passed.

## The LLM stub

`schedule install` refuses to install without a key source (athenaeum#2304
AC3). With `llm.provider: claude-cli`, the key source is the `claude` binary.
Its resolved absolute path is embedded in the unit as
`ATHENAEUM_CLAUDE_CLI_BIN`. Here that binary is a stub at
`/opt/fake-claude/claude`. That directory is not on the user manager's `PATH`,
so the stub can only run at the tick if the embedded variable is honoured. The
stub running at all therefore shows that the key source resolved without a
login shell.

The stub reads the prompt from stdin, the same way `ClaudeCliClient` sends it.
It logs a JSON line per call to `~/fake-claude.log` with the timestamp, the
sorted environment-variable **names** (never the values) and `PATH`. It then
prints a `--output-format json` success envelope. The answer text comes from a
keyword table, so the two classifiers a compile of an auto-memory file calls
(`claim_kind` and `decay_bucket`) get schema-valid answers.

`/opt/fake-claude/claude` (mode 0755):

```python
#!/usr/local/bin/python3
import json, os, sys, time

argv = sys.argv[1:]
system = argv[argv.index("--system-prompt") + 1] if "--system-prompt" in argv else ""
user = sys.stdin.read()

answer = "{}"
try:
    table = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "fake-claude-responses.json")))
    for keyword, text in table:
        if keyword in system or keyword in user:
            answer = text
            break
except OSError:
    pass

with open(os.path.join(os.environ.get("HOME", "/tmp"), "fake-claude.log"), "a") as fh:
    fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "env_names": sorted(os.environ),
                         "env_PATH": os.environ.get("PATH"),
                         "answer_head": answer[:120]}) + "\n")

print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": answer, "stop_reason": "end_turn",
                  "usage": {"input_tokens": 10, "output_tokens": 10,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}}))
```

`/opt/fake-claude/fake-claude-responses.json`:

```json
[
  ["by its EPISTEMIC KIND", "{\"claim_kind\": \"fact\"}"],
  ["by HOW IT DECAYS", "{\"bucket\": \"durable\"}"]
]
```

## Steps (repeatable)

Use a throwaway knowledge root and a throwaway cache directory.

### Boot a systemd container

1. Build the image from this `Dockerfile`:

   ```dockerfile
   FROM python:3.13-slim-bookworm
   RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd systemd-sysv dbus dbus-user-session libpam-systemd procps git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash verifier
   STOPSIGNAL SIGRTMIN+3
   CMD ["/sbin/init"]
   ```

   ```sh
   docker build -t athenaeum-systemd-verify .
   docker run -d --name athenaeum-sd-verify --privileged --cgroupns=host \
     -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
     athenaeum-systemd-verify
   docker exec athenaeum-sd-verify systemctl is-system-running --wait   # running
   docker exec athenaeum-sd-verify loginctl enable-linger verifier
   ```

2. `docker exec` gives no session bus. Every later command that runs
   `athenaeum schedule`, `athenaeum doctor` or `systemctl --user` runs as the
   user with the bus set by hand:

   ```sh
   UH=$(docker exec athenaeum-sd-verify getent passwd verifier | cut -d: -f6)
   docker exec -u verifier -w "$UH" -e HOME="$UH" \
     -e XDG_RUNTIME_DIR=/run/user/1000 \
     -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
     athenaeum-sd-verify <command>
   ```

   This environment belongs to the *installing* shell only. The user manager
   that runs the tick has its own environment (step 5), and
   `systemctl --user import-environment` was **not** run.

### Setup

3. Copy a `git archive` of the commit into the container. Then
   `python3 -m venv ~/venv && ~/venv/bin/pip install <src>` and check that
   `~/venv/bin/athenaeum --version` prints the expected version (`athenaeum 0.25.0` on the re-run). Install the
   stub and its table under `/opt/fake-claude/`.
4. Run `~/venv/bin/athenaeum init --path ~/verify-kb`, then append
   `llm:` / `  provider: claude-cli` to `~/verify-kb/athenaeum.yaml`. The unit
   does not embed the provider, so the provider has to come from the yaml.
   Create an empty `~/verify-cache`. Write two invented raw files,
   `bakery.md` and `lighthouse.md`, to
   `~/verify-kb/raw/auto-memory/demo-scope/`. Each has frontmatter `name`,
   `description` and `metadata.type: project`, plus one sentence about
   invented people and places.
5. Record the baseline:
   - each existing directory from `systemd-analyze --user unit-paths`, listed
     recursively
   - `systemctl --user list-unit-files 'athenaeum*'`
   - `systemctl --user list-timers --all`
   - `find ~/.config ~/.local`
   - `systemctl --user show-environment`

### Counter-example and `doctor` before install

6. `~/venv/bin/athenaeum doctor --path ~/verify-kb --cache-dir ~/verify-cache`.
7. `~/venv/bin/athenaeum schedule install --path ~/verify-kb --cache-dir ~/verify-cache`,
   with `ATHENAEUM_CLAUDE_CLI_BIN` unset and no `claude` on `PATH`. Expect
   exit 1 and no `~/.config`.

### Install and a real tick

8. Pick a minute two to three minutes ahead (`date -u`). Then run:

   ```sh
   ATHENAEUM_CLAUDE_CLI_BIN=/opt/fake-claude/claude \
     ~/venv/bin/athenaeum schedule install --path ~/verify-kb \
     --cache-dir ~/verify-cache --hour H --minute M
   ```

   Run it by absolute path with the venv **not** on `PATH`.
9. Inspect:
   - the two unit files and `stat -c %a` on each
   - `systemctl --user is-enabled` / `is-active athenaeum-run.timer`
   - `list-timers` (NEXT should be the chosen minute)
   - `systemctl --user show athenaeum-run.service -p Environment -p ExecStart`
   - `doctor`
   - the step 5 listing again
10. **Do not start the service by hand.** Wait until
    `systemctl --user show athenaeum-run.service -p ExecMainExitTimestampMonotonic`
    is non-zero. Then record:
    - the timer's `LastTriggerUSec`
    - the service's `Result`, `ExecMainStatus` and start and exit timestamps
    - `journalctl --user -u athenaeum-run.service`
    - `~/.local/state/athenaeum/schedule-run.log`
    - `~/fake-claude.log`
    - the contents of `raw/` and `wiki/`
    - `git log` of the knowledge root

### Remove

11. Run `~/venv/bin/athenaeum schedule remove`, then `doctor`, then the step 5
    listing. Diff the listing against the baseline.
12. Run `remove` a second time. Expect a no-op with exit 0.

## Results

### Counter-example and `doctor` before install

- `doctor` before install: `[OK] schedule: not scheduled (systemd): no systemd
  timer at ~/.config/systemd/user/athenaeum-run.timer`.
- `install` with no resolvable `claude`: exit 1, `llm.provider is claude-cli
  but it cannot run: claude-cli provider selected but the 'claude' binary was
  not found on PATH ...`. Nothing was written (`~/.config` still absent).

### AC1: install and a scheduled tick, pass

`install` exited 0 and printed:

```
athenaeum schedule install: systemd schedule installed (~/.config/systemd/user/athenaeum-run.service, ~/.config/systemd/user/athenaeum-run.timer)
  athenaeum binary: ~/venv/bin/athenaeum
```

The generated service file. Its paths are absolute in the file and shown here
with `/home/<user>`:

```ini
[Unit]
Description=Athenaeum librarian compile (generated by `athenaeum schedule install` -- do not edit by hand)

[Service]
Type=oneshot
Environment=ATHENAEUM_CLAUDE_CLI_BIN=/opt/fake-claude/claude
Environment=ATHENAEUM_CACHE_DIR=/home/<user>/verify-cache
ExecStart=/home/<user>/venv/bin/athenaeum run --path /home/<user>/verify-kb
StandardOutput=append:/home/<user>/.local/state/athenaeum/schedule-run.log
StandardError=append:/home/<user>/.local/state/athenaeum/schedule-run.log
```

The timer had `OnCalendar=*-*-* 10:25:00`, `Persistent=true` and
`WantedBy=timers.target`.

Both unit files were mode **0600**. The 2026-11-14 report left this open.

Loader state after install:

- `is-enabled` returned `enabled`, and `is-active` returned `active`.
- `list-unit-files` listed `athenaeum-run.timer enabled` and
  `athenaeum-run.service static`.
- `list-timers` showed NEXT as `Mon 2026-11-16 10:25:00 UTC`, with LAST as `-`.
- `install` also created the symlink
  `~/.config/systemd/user/timers.target.wants/athenaeum-run.timer`. Enabling
  the timer made the user manager create the stamp
  `~/.local/share/systemd/timers/stamp-athenaeum-run.timer`; the tick then
  updated its mtime.

The tick fired from the timer, and nothing started it by hand:

- Timer: `LastTriggerUSec=Mon 2026-11-16 10:25:02 UTC`, `Result=success`.
  NEXT moved to `Tue 2026-11-17 10:25:00 UTC`.
- Service: `Result=success`, `ExecMainCode=1` (exited), `ExecMainStatus=0`.
  It started at 10:25:02 and exited at 10:25:03.
- Journal: `Starting athenaeum-run.service ...`, then `Finished
  athenaeum-run.service ...`, then `Consumed 1.203s CPU time`.
- Run log (`~/.local/state/athenaeum/schedule-run.log`, 25 lines):
  - `Discovered 2 auto-memory file(s) across 1 scope(s)`
  - `merge: wrote .../wiki/auto-bakery.md`
  - `merge: wrote .../wiki/auto-lighthouse.md`
  - `retire: moved 2 raw file(s) into 2 wiki entr(y/ies); held 0; committed`
  - `Token usage: 4 API calls`
  - No `ERROR` lines and no traceback.
- Knowledge root: `raw/auto-memory/` is gone, and `wiki/auto-bakery.md` and
  `wiki/auto-lighthouse.md` exist. Two librarian commits sit on top of the init
  commit (`auto-memory raw intake provenance snapshot`, then
  `move-then-retire (2 moved, 0 held)`).
- Stub log: 4 calls, 2 per file (`claim_kind`, then `decay_bucket`), all at
  10:25:03.

**No login environment.** The user manager's environment
(`systemctl --user show-environment`) was:

- `HOME`
- `LANG=C.UTF-8`
- `LOGNAME`
- `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
- `SHELL`
- `SYSTEMD_EXEC_PID`
- `USER`
- `XDG_RUNTIME_DIR`
- `DBUS_SESSION_BUS_ADDRESS`

At the tick, the environment-variable names the stub saw were exactly these
four groups:

- that set, plus the systemd-provided `INVOCATION_ID`, `MANAGERPID`,
  `TRIGGER_UNIT`, `TRIGGER_TIMER_REALTIME_USEC` and
  `TRIGGER_TIMER_MONOTONIC_USEC`
- the unit's two `Environment=` lines, `ATHENAEUM_CLAUDE_CLI_BIN` and
  `ATHENAEUM_CACHE_DIR`
- three variables athenaeum sets itself on the `claude` subprocess:
  `CLAUDE_CODE_SAFE_MODE`, `CLAUDE_SUPPRESS_NOTIFY` and
  `ORT_DISABLE_TELEMETRY`

Nothing came from a shell profile or a login session. Neither the stub's
directory nor the venv was on `PATH`.

### AC2: `remove`, pass

`remove` exited 0 and printed:

```
athenaeum schedule remove: systemd schedule removed (~/.config/systemd/user/athenaeum-run.service, ~/.config/systemd/user/athenaeum-run.timer)
  deleted ~/.local/state/athenaeum/schedule-run.log
  deleted ~/.local/state/athenaeum
```

Afterwards, `systemctl --user status athenaeum-run.timer` returned `Unit
athenaeum-run.timer could not be found`. `list-unit-files 'athenaeum*'` and
`list-timers --all` were back to `0 ... listed`. The second `remove` printed
`no systemd schedule was installed` and exited 0.

Every directory in `systemd-analyze --user unit-paths` was listed before
install and after remove. The only unit-path difference is that
`~/.config/systemd/user` now exists, and it is empty. The
`timers.target.wants` symlink and its directory are gone. The full diff of the
baseline against the post-remove listing:

```
> [~/.config/systemd/user]                                   (empty unit path)
> ~/.config
> ~/.config/systemd
> ~/.config/systemd/user
> ~/.local
> ~/.local/share
> ~/.local/share/systemd
> ~/.local/share/systemd/timers
> ~/.local/state
```

- **Timer stamp deleted (athenaeum#2707):** just before `remove`,
  `~/.local/share/systemd/timers/` held `stamp-athenaeum-run.timer` (mtime
  10:25, the tick). After `remove` that directory is empty. Every line in the
  diff above is a directory; no file remains.
- **Empty directories remain:** `~/.config/systemd/user` (and its parents)
  and `~/.local/share/systemd/timers`, both created by `install` and the
  user manager, and `~/.local/state`. These are empty directories, not
  files. `remove` deliberately never deletes the shared `~/.local/state`
  (athenaeum#2596), and it did delete its own `~/.local/state/athenaeum`. They
  are listed here so the issue owner can rule on them, the same way the
  2026-11-14 report listed the empty crontab. They were not part of
  athenaeum#2707.

### AC3: `doctor`, pass

- Before install:
  `[OK] schedule: not scheduled (systemd): no systemd timer at ~/.config/systemd/user/athenaeum-run.timer`.
- After install:
  `[OK] schedule: scheduled: systemd timer present at ~/.config/systemd/user/athenaeum-run.timer`.
- After remove: `[OK] schedule: not scheduled (systemd): ...`, the same as
  before install.

## Blocker re-checks

| Issue | Check on the systemd path | Result |
|---|---|---|
| athenaeum#2583 (binary resolved from `PATH`) | `install` was run as `~/venv/bin/athenaeum` with no `athenaeum` on `PATH`. `ExecStart=` names the venv binary, and `install` printed `athenaeum binary: ~/venv/bin/athenaeum` | Fixed |
| athenaeum#2584 (unit ignores `--cache-dir`) | The service carries `Environment=ATHENAEUM_CACHE_DIR=.../verify-cache`. After the tick the throwaway cache held the manifest, spend and run-summary ledgers, and the default cache `~/.cache/athenaeum` did not exist | Fixed |
| athenaeum#2596 (`remove` leaves logs) | `remove` deleted `schedule-run.log` and the now-empty `~/.local/state/athenaeum`, and left the shared `~/.local/state` | Fixed |

## Findings

Filed and fixed:

- **athenaeum#2707**: on 0.24.0, `remove` left systemd's persistent-timer
  stamp `~/.local/share/systemd/timers/stamp-athenaeum-run.timer`. It was
  fixed in PR athenaeum#2716 and verified by the re-run (AC2).

Not filed (judgment calls, for the issue owner):

- On systemd, `doctor` reports `scheduled` when the timer **file** exists. It
  does not ask the user manager whether the timer is loaded or enabled. A unit
  file left behind by a failed `enable` would still read as scheduled.
- `install` and `remove` need a reachable user bus. From a bare `docker exec`
  (and likely `sudo -u` or `su` without a login session), `systemctl --user`
  cannot connect unless `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are
  set. That failure was not exercised here. On a normal desktop or SSH login
  the bus is already set.
- Without linger, a user manager stops at logout and the timer only fires
  while the user is logged in. This run used `loginctl enable-linger`. A
  headless host needs the same, and the docs do not say so.
- The run log still does not name the key source it used. Here the stub
  running at all is the evidence (see AC1).

## First run (0.24.0)

The first run used the same procedure on athenaeum 0.24.0 (`0b6cffd8`). AC1
and AC3 passed with the same evidence: the tick fired at 01:28:02, exited 0,
moved both raw files and saw the same environment-variable names. AC2 was not
met. After `remove`, `~/.local/share/systemd/timers/stamp-athenaeum-run.timer`
remained, because nothing deleted it. Running
`systemctl --user stop athenaeum-run.timer` and then
`systemctl --user clean --what=state athenaeum-run.timer` on a separate test
install exited 0 and deleted the stamp. That became the athenaeum#2707 fix.

## What was left on the host

- Each run's container was destroyed with `docker rm -f`, taking the
  knowledge root, the cache, the stub and the logs with it.
- The image `athenaeum-systemd-verify` was removed. The base image
  `python:3.13-slim-bookworm` remains in the local Docker cache, as it did
  after the 2026-11-14 run.
