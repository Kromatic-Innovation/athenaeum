# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schedule install`` / ``remove`` — a recurring `athenaeum run`
schedule (issue athenaeum#2304, split off the install epic athenaeum#2296).

**Motivation (see the issue body):** nothing schedules the nightly compile
for a Claude Code sidecar install (``docs/guides/sidecar.md``,
``docs/guides/claude-code.md``) — an operator is left to hand-write a cron
line or copy ``examples/claude-code/com.athenaeum.run.plist`` themselves, and
the single most common way to get that wrong is the exact failure named in
``docs/guides/troubleshooting.md``'s "Claude Code auth" table: the key that
satisfies the Claude Code hooks (via ``~/.cache/athenaeum/config.env``) is
NOT read by ``athenaeum run`` itself, so a hand-written schedule compiles
fine interactively and then fails every night under cron/launchd with
``ANTHROPIC_API_KEY not set``. This module exists to generate a unit that
gets the environment right the first time, and to refuse outright (AC3)
rather than install something that is guaranteed to fail every run.

**Scope split (operator ruling 2026-11-10):** this module only generates
units, writes them to disk, and invokes the platform loader — every loader
call is injectable (:data:`LoaderFn`) and every test exercises this module
against a TEMPORARY home directory with the loader mocked. Verifying a REAL
scheduled tick fires on a real macOS/Linux host, logged in or not, is
athenaeum#2366 and is explicitly out of scope here.

**Three platforms, three generators, one shared contract:**

- macOS -- a launchd ``LaunchAgent`` plist, generated from the same shape as
  the existing hand-written ``examples/claude-code/com.athenaeum.run.plist``
  (:func:`generate_launchd_plist`), loaded with ``launchctl load -w``.
- Linux with a systemd user session (``systemctl`` on ``PATH``) -- a
  ``systemd --user`` service + calendar timer pair
  (:func:`generate_systemd_service` / :func:`generate_systemd_timer`),
  enabled with ``systemctl --user enable --now``. Chosen over cron as the
  Linux default (a reversible default -- see :func:`detect_platform`)
  because a user timer survives independently of any particular shell and
  reports its own run history via ``journalctl``.
- Linux without a systemd user session -- a tagged crontab line
  (:func:`generate_cron_line`), installed via ``crontab -``.

**The key-source contract (AC2 + AC3).** :func:`resolve_key_source` resolves
the SAME global ``llm.provider`` chain :func:`athenaeum.provider.resolve_provider`
already defines (env > yaml > ``"api"`` default) -- not a second guess at
it -- and returns a small environment-variable map that the three
generators embed VERBATIM into the unit, so the schedule never depends on a
login shell to resolve it:

- ``api`` provider: ``ANTHROPIC_API_KEY``, read from the process env or the
  cached ``<cache_dir>/config.env`` (the same two seams
  :func:`athenaeum.doctor.check_key_mode` reads, intentionally duplicated
  here rather than imported -- that module's own docstring already
  documents this exact duplication convention for ``config.env`` readers,
  e.g. :func:`athenaeum.claude_code_adapter._load_config_env`).
- ``claude-cli`` provider: :func:`athenaeum.provider.preflight_provider`
  must find the ``claude`` binary; its RESOLVED ABSOLUTE PATH is embedded as
  ``ATHENAEUM_CLAUDE_CLI_BIN`` (a knob :mod:`athenaeum.provider` already
  reads) rather than relying on the scheduled job's restricted ``PATH`` to
  find a bare ``claude`` the way an interactive shell would.
- **Neither resolves (AC3 counter-example):** :func:`resolve_key_source`
  raises :class:`NoKeySourceError` and ``install`` refuses outright, naming
  the exact fix, instead of installing a schedule that fails every night.

**Single-flight protection is NOT reimplemented here (per the issue's own
Plan).** The generated unit's only job is to invoke ``athenaeum run``, which
already takes :class:`athenaeum.runlock.RunLock` itself
(:func:`athenaeum._cmd_run.cmd_run`) -- so a scheduled tick overlapping a
manual run is already serialized by the lock `run` already holds. `install`/
`remove` themselves never touch the knowledge root's git state (they only
read/write the unit file(s) under ``home`` and shell out to the platform
loader), so neither takes the run lock itself and neither appears in
``docs/reference/exit-codes.md``'s ``EXIT_LOCK_HELD`` producer list.

**Scope limitation, stated plainly:** the embedded key source resolves the
GLOBAL ``llm.provider`` only (the same default every one of ``run``'s five
LLM knobs falls back to absent a ``llm.providers.<knob>`` override). An
operator who overrides an individual knob (``llm.providers.write``, etc.) to
a DIFFERENT provider than the global default is responsible for embedding
that knob's own key some other way -- this module does not attempt to
enumerate and resolve all five independently.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.config` (L2) and
:mod:`athenaeum.provider` (L3), both function-local, matching every other
best-effort/advisory reader's own discipline (see :mod:`athenaeum.doctor`'s
own docstring for the same note). Nothing above L4 imports this module.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


#: One loader subprocess call's outcome: (returncode, stdout, stderr).
#: Tests substitute :data:`LoaderFn` so no real ``launchctl``/``systemctl``/
#: ``crontab`` process is ever spawned (mirrors
#: :data:`athenaeum.doctor.FetchFn`'s same injection shape).
@dataclass(frozen=True)
class LoaderResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


#: ``(argv, stdin_text_or_None) -> LoaderResult``. ``stdin_text`` carries the
#: new crontab body for ``crontab -``; every other call passes ``None``.
LoaderFn = Callable[[list[str], "str | None"], LoaderResult]


def _default_loader(argv: list[str], input_text: str | None = None) -> LoaderResult:
    try:
        proc = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        # The platform's own loader binary is missing (e.g. a dev container
        # with no `crontab`) -- report it as a failed call rather than
        # crashing `install`/`remove` outright.
        return LoaderResult(127, "", f"{argv[0]}: {exc}")
    return LoaderResult(proc.returncode, proc.stdout, proc.stderr)


class NoKeySourceError(RuntimeError):
    """Raised by :func:`resolve_key_source` when no key source resolves
    (issue athenaeum#2304 AC3) -- the counter-example ``install`` must refuse
    for, rather than schedule a job that fails every night."""


class UnsupportedPlatformError(RuntimeError):
    """Raised by :func:`detect_platform` for a platform with no generator
    (Windows -- out of scope here, tracked by athenaeum#2203)."""


Platform = Literal["launchd", "systemd", "cron"]

#: Default schedule time (03:00 local), matching
#: ``examples/claude-code/com.athenaeum.run.plist``'s own
#: ``StartCalendarInterval`` and this directory's README "Option B" cron line.
DEFAULT_HOUR = 3
DEFAULT_MINUTE = 0

LAUNCHD_LABEL = "com.athenaeum.run"
SYSTEMD_UNIT_NAME = "athenaeum-run"
#: Trailing marker every generated crontab line carries, so install/remove/
#: status can find (and ONLY touch) the line this module itself wrote,
#: leaving every other crontab entry byte-for-byte untouched.
CRON_TAG = "athenaeum-schedule-managed"


@dataclass(frozen=True)
class KeySource:
    """Resolved environment to embed verbatim in the generated unit."""

    kind: Literal["api_key", "claude_cli"]
    env: dict[str, str] = field(default_factory=dict)


def _read_cached_config_env(cache_dir: Path) -> dict[str, str]:
    """Read-only ``KEY=VALUE`` parse of ``<cache_dir>/config.env``.

    Deliberately a documented duplicate of
    :func:`athenaeum.doctor._read_cached_config_env` (and
    :func:`athenaeum.claude_code_adapter._load_config_env`,
    :func:`athenaeum._cmd_audit._load_cache_config_env`) -- see this
    module's own docstring for why a fourth reader is cheaper than an
    import across L4 siblings here.
    """
    path = cache_dir / "config.env"
    out: dict[str, str] = {}
    try:
        if not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def resolve_key_source(config: dict[str, Any] | None, cache_dir: Path) -> KeySource:
    """Resolve the environment a scheduled ``athenaeum run`` needs to find
    its key source WITHOUT a login shell (issue athenaeum#2304 AC2), or raise
    :class:`NoKeySourceError` naming exactly why none is available (AC3).
    """
    from athenaeum.provider import ProviderConfigError, preflight_provider, resolve_provider

    try:
        provider = resolve_provider(config)
    except ProviderConfigError as exc:
        raise NoKeySourceError(str(exc)) from exc

    if provider == "claude-cli":
        error = preflight_provider("claude-cli")
        if error is not None:
            raise NoKeySourceError(
                f"llm.provider is claude-cli but it cannot run: {error}"
            )
        bin_name = os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude"
        resolved = shutil.which(bin_name) or bin_name
        return KeySource(
            kind="claude_cli",
            env={"ATHENAEUM_CLAUDE_CLI_BIN": str(Path(resolved).resolve())},
        )

    # provider == "api"
    key = os.environ.get("ANTHROPIC_API_KEY") or _read_cached_config_env(cache_dir).get(
        "ANTHROPIC_API_KEY"
    )
    if not key:
        raise NoKeySourceError(
            "no key source available: no ANTHROPIC_API_KEY in the process "
            f"environment or cached {cache_dir / 'config.env'}, and "
            "llm.provider is not claude-cli. Refusing to install a schedule "
            "that would fail every night -- set ANTHROPIC_API_KEY, run a "
            "Claude Code session so the hooks cache one, or set "
            "llm.provider: claude-cli and log in with `claude`."
        )
    return KeySource(kind="api_key", env={"ANTHROPIC_API_KEY": key})


def detect_platform(
    *, system: str | None = None, systemctl_available: bool | None = None
) -> Platform:
    """Which generator applies here (issue athenaeum#2304 AC1).

    ``system``/``systemctl_available`` are injectable so tests exercise all
    three branches without needing the actual host they run on to match.
    **Reversible default:** Linux prefers a systemd user timer over cron
    whenever ``systemctl`` is on ``PATH`` -- a timer survives independently
    of any shell and is inspectable via ``journalctl``, whereas a crontab
    line is the right fallback for a minimal/non-systemd Linux box. Either
    is a valid reading of the issue's "a cron or systemd entry on Linux";
    ``--backend`` on the CLI overrides this detection either way.
    """
    sys_name = system if system is not None else platform.system()
    if sys_name == "Darwin":
        return "launchd"
    if sys_name == "Linux":
        available = (
            systemctl_available
            if systemctl_available is not None
            else shutil.which("systemctl") is not None
        )
        return "systemd" if available else "cron"
    raise UnsupportedPlatformError(
        f"athenaeum schedule has no generator for platform {sys_name!r} "
        "(Windows is out of scope -- see athenaeum#2203)"
    )


def resolve_athenaeum_binary() -> Path:
    """Absolute path to the ``athenaeum`` console script (AC2's "absolute
    paths"), matching the hand-written example plist's own
    ``/REPLACE_ME/path/to/venv/bin/athenaeum`` -- resolved for real instead
    of left as a placeholder.
    """
    found = shutil.which("athenaeum")
    if found:
        return Path(found).resolve()
    # Fallback: the console script sits next to the interpreter in a venv.
    return (Path(sys.executable).parent / "athenaeum").resolve()


# ---------------------------------------------------------------------------
# Per-platform paths.
# ---------------------------------------------------------------------------


def _launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "athenaeum-run.log"


def _systemd_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _systemd_service_path(home: Path) -> Path:
    return _systemd_dir(home) / f"{SYSTEMD_UNIT_NAME}.service"


def _systemd_timer_path(home: Path) -> Path:
    return _systemd_dir(home) / f"{SYSTEMD_UNIT_NAME}.timer"


def _state_log_path(home: Path) -> Path:
    """Shared log destination for the Linux generators."""
    return home / ".local" / "state" / "athenaeum" / "schedule-run.log"


# ---------------------------------------------------------------------------
# Generators -- pure string-building, no I/O (issue athenaeum#2304 AC2: every
# assertion the tests make is on this returned text).
# ---------------------------------------------------------------------------


def generate_launchd_plist(
    athenaeum_bin: Path,
    knowledge_root: Path,
    key_source: KeySource,
    *,
    hour: int,
    minute: int,
    log_path: Path,
) -> str:
    """Launchd plist, same shape as
    ``examples/claude-code/com.athenaeum.run.plist`` (this issue's own Plan
    names that file as the template)."""
    env_entries = "".join(
        f"    <key>{name}</key>\n    <string>{value}</string>\n"
        for name, value in key_source.env.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<!-- Generated by `athenaeum schedule install` -- do not edit by "
        "hand; re-run install to regenerate. -->\n"
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{LAUNCHD_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{athenaeum_bin}</string>\n"
        "    <string>run</string>\n"
        "    <string>--path</string>\n"
        f"    <string>{knowledge_root}</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_entries}"
        "  </dict>\n"
        "  <key>StartCalendarInterval</key>\n"
        "  <dict>\n"
        "    <key>Hour</key>\n"
        f"    <integer>{hour}</integer>\n"
        "    <key>Minute</key>\n"
        f"    <integer>{minute}</integer>\n"
        "  </dict>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{log_path}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{log_path}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def generate_systemd_service(
    athenaeum_bin: Path, knowledge_root: Path, key_source: KeySource, *, log_path: Path
) -> str:
    env_lines = "".join(f"Environment={name}={value}\n" for name, value in key_source.env.items())
    return (
        "[Unit]\n"
        "Description=Athenaeum librarian compile "
        "(generated by `athenaeum schedule install` -- do not edit by hand)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{env_lines}"
        f"ExecStart={athenaeum_bin} run --path {knowledge_root}\n"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
    )


def generate_systemd_timer(*, hour: int, minute: int) -> str:
    return (
        "[Unit]\n"
        f"Description=Daily schedule for {SYSTEMD_UNIT_NAME}.service "
        "(generated by `athenaeum schedule install` -- do not edit by hand)\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def generate_cron_line(
    athenaeum_bin: Path,
    knowledge_root: Path,
    key_source: KeySource,
    *,
    hour: int,
    minute: int,
    log_path: Path,
) -> str:
    env_prefix = "".join(f"{name}={value} " for name, value in key_source.env.items())
    return (
        f"{minute} {hour} * * * {env_prefix}{athenaeum_bin} run "
        f"--path {knowledge_root} >> {log_path} 2>&1 # {CRON_TAG}"
    )


def _crontab_lines(loader: LoaderFn) -> list[str]:
    """Current crontab, split into lines; a non-zero exit (typically "no
    crontab for user") reads as an empty crontab, never an error -- that is
    the normal steady state before the first ``install``."""
    result = loader(["crontab", "-l"], None)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    platform: Platform
    paths: tuple[Path, ...]
    loader_returncode: int
    loader_output: str


@dataclass(frozen=True)
class RemoveResult:
    platform: Platform
    removed_paths: tuple[Path, ...]
    loader_returncode: int
    loader_output: str
    had_schedule: bool


def install(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    home: Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    loader: LoaderFn | None = None,
    platform_override: Platform | None = None,
    athenaeum_bin: Path | None = None,
) -> InstallResult:
    """Generate the platform's unit, write it, and invoke the loader (issue
    athenaeum#2304 AC1). Raises :class:`NoKeySourceError` (AC3) before
    writing anything when no key source resolves."""
    from athenaeum.config import resolve_cache_dir

    loader_fn = loader or _default_loader
    resolved_home = (home or Path.home()).expanduser().resolve()
    resolved_cache_dir = resolve_cache_dir(cache_dir).resolve()
    key_source = resolve_key_source(config, resolved_cache_dir)  # may raise NoKeySourceError
    plat = platform_override or detect_platform()
    bin_path = athenaeum_bin or resolve_athenaeum_binary()
    resolved_knowledge_root = Path(knowledge_root).expanduser().resolve()

    if plat == "launchd":
        plist_path = _launchd_plist_path(resolved_home)
        log_path = _launchd_log_path(resolved_home)
        content = generate_launchd_plist(
            bin_path,
            resolved_knowledge_root,
            key_source,
            hour=hour,
            minute=minute,
            log_path=log_path,
        )
        _write(plist_path, content)
        result = loader_fn(["launchctl", "load", "-w", str(plist_path)], None)
        return InstallResult(plat, (plist_path,), result.returncode, result.output)

    if plat == "systemd":
        service_path = _systemd_service_path(resolved_home)
        timer_path = _systemd_timer_path(resolved_home)
        log_path = _state_log_path(resolved_home)
        _write(
            service_path,
            generate_systemd_service(
                bin_path, resolved_knowledge_root, key_source, log_path=log_path
            ),
        )
        _write(timer_path, generate_systemd_timer(hour=hour, minute=minute))
        reload_result = loader_fn(["systemctl", "--user", "daemon-reload"], None)
        enable_result = loader_fn(
            ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT_NAME}.timer"], None
        )
        rc = enable_result.returncode or reload_result.returncode
        return InstallResult(
            plat, (service_path, timer_path), rc, reload_result.output + enable_result.output
        )

    # cron
    log_path = _state_log_path(resolved_home)
    existing = [line for line in _crontab_lines(loader_fn) if CRON_TAG not in line]
    new_line = generate_cron_line(
        bin_path, resolved_knowledge_root, key_source, hour=hour, minute=minute, log_path=log_path
    )
    new_text = "\n".join([*existing, new_line]) + "\n"
    result = loader_fn(["crontab", "-"], new_text)
    return InstallResult(plat, (), result.returncode, result.output)


def remove(
    *,
    home: Path | None = None,
    loader: LoaderFn | None = None,
    platform_override: Platform | None = None,
) -> RemoveResult:
    """Unload and delete everything ``install`` wrote (issue athenaeum#2304
    AC1). Idempotent: removing an already-absent schedule is a successful
    no-op (``had_schedule=False``), not an error."""
    loader_fn = loader or _default_loader
    resolved_home = (home or Path.home()).expanduser().resolve()
    plat = platform_override or detect_platform()

    if plat == "launchd":
        plist_path = _launchd_plist_path(resolved_home)
        had = plist_path.is_file()
        if not had:
            return RemoveResult(plat, (), 0, "", False)
        result = loader_fn(["launchctl", "unload", str(plist_path)], None)
        plist_path.unlink()
        return RemoveResult(plat, (plist_path,), result.returncode, result.output, True)

    if plat == "systemd":
        service_path = _systemd_service_path(resolved_home)
        timer_path = _systemd_timer_path(resolved_home)
        had = service_path.is_file() or timer_path.is_file()
        if not had:
            return RemoveResult(plat, (), 0, "", False)
        disable_result = loader_fn(
            ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT_NAME}.timer"], None
        )
        removed = []
        for path in (service_path, timer_path):
            if path.is_file():
                path.unlink()
                removed.append(path)
        reload_result = loader_fn(["systemctl", "--user", "daemon-reload"], None)
        rc = disable_result.returncode or reload_result.returncode
        return RemoveResult(
            plat, tuple(removed), rc, disable_result.output + reload_result.output, True
        )

    # cron
    existing = _crontab_lines(loader_fn)
    had = any(CRON_TAG in line for line in existing)
    if not had:
        return RemoveResult(plat, (), 0, "", False)
    filtered = [line for line in existing if CRON_TAG not in line]
    new_text = "\n".join(filtered) + ("\n" if filtered else "")
    result = loader_fn(["crontab", "-"], new_text)
    return RemoveResult(plat, (), result.returncode, result.output, True)


def _write(path: Path, content: str) -> None:
    from athenaeum.atomic_io import atomic_write_text

    atomic_write_text(path, content)


# ---------------------------------------------------------------------------
# Read-only status, for `athenaeum doctor` (issue athenaeum#2304 AC4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleStatus:
    scheduled: bool
    platform: Platform | None
    detail: str


def status(
    *,
    home: Path | None = None,
    loader: LoaderFn | None = None,
    platform_override: Platform | None = None,
) -> ScheduleStatus:
    """Read-only: does a schedule this module would recognize exist right
    now (issue athenaeum#2304 AC4)? Never writes anything -- the cron branch
    only LISTS the crontab (``crontab -l``)."""
    resolved_home = (home or Path.home()).expanduser().resolve()
    try:
        plat = platform_override or detect_platform()
    except UnsupportedPlatformError as exc:
        return ScheduleStatus(False, None, str(exc))

    if plat == "launchd":
        plist_path = _launchd_plist_path(resolved_home)
        if plist_path.is_file():
            return ScheduleStatus(True, plat, f"launchd plist present at {plist_path}")
        return ScheduleStatus(False, plat, f"no launchd plist at {plist_path}")

    if plat == "systemd":
        timer_path = _systemd_timer_path(resolved_home)
        if timer_path.is_file():
            return ScheduleStatus(True, plat, f"systemd timer present at {timer_path}")
        return ScheduleStatus(False, plat, f"no systemd timer at {timer_path}")

    # cron
    existing = _crontab_lines(loader or _default_loader)
    if any(CRON_TAG in line for line in existing):
        return ScheduleStatus(True, plat, f"crontab entry present (tagged {CRON_TAG!r})")
    return ScheduleStatus(False, plat, "no athenaeum-managed crontab entry found")


__all__ = [
    "CRON_TAG",
    "DEFAULT_HOUR",
    "DEFAULT_MINUTE",
    "InstallResult",
    "KeySource",
    "LoaderFn",
    "LoaderResult",
    "NoKeySourceError",
    "Platform",
    "RemoveResult",
    "ScheduleStatus",
    "UnsupportedPlatformError",
    "detect_platform",
    "generate_cron_line",
    "generate_launchd_plist",
    "generate_systemd_service",
    "generate_systemd_timer",
    "install",
    "remove",
    "resolve_athenaeum_binary",
    "resolve_key_source",
    "status",
]
