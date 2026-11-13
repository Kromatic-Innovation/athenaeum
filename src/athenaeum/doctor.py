# SPDX-License-Identifier: Apache-2.0
"""``athenaeum doctor`` — read-only install health check (issue athenaeum#2298).

Install failures are silent today: the shipped hook commands end in
``2>/dev/null || true``, a missing credential degrades topic extraction to a
regex fallback with its only warning discarded (see
``docs/guides/troubleshooting.md``'s "Claude Code auth" section), and
``athenaeum test-mcp`` only exercises a scratch directory, never the
operator's real install (see ``README.md``'s Quick start). This module is the
one place that checks every link of a REAL install and names the fix for
anything broken, so a user does not have to grep logs or run the pipeline by
hand to find out.

**Contract:** :func:`run_checks` returns one :class:`CheckResult` per link
named in the issue (package version, interpreter version, MCP registration,
hook presence, LLM key mode, search index, last compile time), each in one
of three states:

- ``"ok"`` -- healthy, or (package version only) nothing to report.
- ``"fail"`` -- broken; ``detail`` says what is wrong and ``fix`` names the
  command or doc that fixes it. At least one ``"fail"`` makes
  :func:`exit_code` return non-zero (issue athenaeum#2298 AC2).
- ``"skip"`` -- cannot be determined right now and that is NOT a failure
  (the package-version check when offline, or on any network error/timeout
  — issue athenaeum#2298 AC5). A ``"skip"`` never flips the exit code.

**Read-only, by construction (issue athenaeum#2298 AC5).** Every check here
reads existing state -- ``athenaeum.yaml``, the cache dir, ``~/.claude``'s
``settings.json`` / ``.claude.json``, environment variables, PyPI's public
JSON API -- and never writes to the knowledge directory or to Claude Code's
own config. No check here calls ``athenaeum.init``-style scaffolding, builds
an index, or touches a lock file. ``tests/test_doctor.py`` asserts the
knowledge root's file set and mtimes are unchanged after a full
:func:`run_checks` call, which is the only honest proof of this contract.

**One small function per check** (the issue's own Plan item), each reading
the SAME config/cache-dir/Claude-config-dir seams the hooks and the MCP
server already resolve through (:mod:`athenaeum.config`,
:mod:`athenaeum.provider`, :mod:`athenaeum.search`,
:mod:`athenaeum.run_summary_log`) rather than a parallel implementation of
any of them.

**Key-mode decision (issue athenaeum#2298, orchestrator ruling):** the regex
fallback is a FAIL, not a warn-and-pass. The issue's own motivation names
this exact failure mode ("a missing key degrades topic extraction to a
regex with its only warning discarded") as the silent failure users need
made visible -- a plain "OK, regex mode" would repeat the same silence this
command exists to end.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.provider` (L3), :mod:`athenaeum.search` (L3), and
:mod:`athenaeum.run_summary_log` (L2) -- all function-local/deferred inside
each check (matching every other best-effort/advisory reader's own
discipline, e.g. :mod:`athenaeum.status`) -- plus :mod:`athenaeum`'s own
``__version__`` (a PEP 562 attribute access, not a submodule import, so it
creates no graph edge at all; see ``tests/test_import_graph_acyclic.py``'s
``_athenaeum_targets``). Nothing above L4 imports this module.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

#: One check's outcome state (issue athenaeum#2298 AC2/AC5).
Status = Literal["ok", "fail", "skip"]

#: Injectable PyPI-JSON fetch: (url, timeout_seconds) -> raw response bytes.
#: Tests substitute this so no real network call is ever made (mirrors
#: :data:`athenaeum.decision_provider.Transport`'s same injection shape).
FetchFn = Callable[[str, float], bytes]


@dataclass(frozen=True)
class CheckResult:
    """One named link's health (issue athenaeum#2298 AC1-AC2)."""

    name: str
    status: Status
    detail: str
    #: The command or doc that fixes this check. Always set when
    #: ``status == "fail"`` (AC2); unset ("") otherwise.
    fix: str = ""


# ---------------------------------------------------------------------------
# Shared resolution helpers -- read-only, no new state.
# ---------------------------------------------------------------------------


def default_claude_config_dir() -> Path:
    """Resolve the Claude Code config home, honoring ``CLAUDE_CONFIG_DIR``.

    Mirrors :func:`athenaeum.transcript_verify.default_projects_root`'s own
    resolution (``CLAUDE_CONFIG_DIR`` relocates the whole ``~/.claude`` tree)
    rather than re-deriving a third copy of the same precedence -- this
    module just does not import that one (it lives one layer up, L3-ish by
    its own docstring, and the identical two-line body is cheaper to repeat
    than to pull the module in for).
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _read_cached_config_env(cache_dir: Path) -> dict[str, str]:
    """Read-only parse of ``<cache_dir>/config.env`` into a plain dict.

    Deliberately does NOT mutate ``os.environ`` -- unlike
    :func:`athenaeum.claude_code_adapter._load_config_env` and
    :func:`athenaeum._cmd_audit._load_cache_config_env` (each already a
    documented duplicate of the other, per those modules' own docstrings),
    this third reader stays side-effect-free because a diagnostic command
    must never change the process's own environment as a side effect of
    being run. ``KEY=VALUE`` lines only; blank lines and ``#``-comments are
    skipped; a missing or unreadable file returns ``{}``.
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


# ---------------------------------------------------------------------------
# Check 1: package version against the latest PyPI release.
# ---------------------------------------------------------------------------

_PYPI_URL = "https://pypi.org/pypi/athenaeum/json"
_PYPI_TIMEOUT_DEFAULT = 3.0


def _default_fetch(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def check_package_version(
    *, fetch: FetchFn | None = None, timeout: float = _PYPI_TIMEOUT_DEFAULT
) -> CheckResult:
    """Installed version vs. the latest PyPI release (issue athenaeum#2298 AC1/AC5).

    Offline, or on ANY network/parse failure, this is ``"skip"`` -- never
    ``"fail"`` -- so :func:`run_checks` works offline (AC5) and a flaky
    network never makes an otherwise-healthy install look broken. A
    successful comparison is always ``"ok"``: being a release behind is
    reported for visibility, not treated as a broken link -- the PATCH digit
    moves on every merge to ``develop`` (see ``README.md``'s Quick start),
    so flagging drift as a failure would make every install but the one
    built seconds ago "fail" doctor for no actionable reason.
    """
    from athenaeum import __version__ as installed_version

    fetch_fn = fetch or _default_fetch
    try:
        raw = fetch_fn(_PYPI_URL, timeout)
        payload = json.loads(raw)
        latest = str(payload["info"]["version"])
    except Exception as exc:  # noqa: BLE001 — any failure here is "skip", never "fail"
        return CheckResult(
            name="package_version",
            status="skip",
            detail=(
                f"could not reach PyPI to check the latest release "
                f"({exc.__class__.__name__}); running {installed_version}"
            ),
        )
    if latest == installed_version:
        detail = f"running {installed_version} (latest)"
    else:
        detail = f"running {installed_version}; latest release is {latest}"
    return CheckResult(name="package_version", status="ok", detail=detail)


# ---------------------------------------------------------------------------
# Check 2: interpreter version.
# ---------------------------------------------------------------------------

#: README's own Quick start floor: "Requires Python 3.13+".
MIN_PYTHON = (3, 13)


def check_interpreter_version(
    version_info: tuple[int, int, int] | None = None,
) -> CheckResult:
    """Running interpreter against the documented 3.13+ floor (issue athenaeum#2298 AC1).

    *version_info* is injectable (defaults to the real ``sys.version_info``)
    so a test can exercise the below-floor branch without needing a second
    interpreter — mirrors :func:`check_package_version`'s ``fetch``
    injection for the same reason.
    """
    major, minor, micro = version_info if version_info is not None else sys.version_info[:3]
    version_str = f"{major}.{minor}.{micro}"
    if (major, minor) >= MIN_PYTHON:
        return CheckResult(
            name="interpreter", status="ok", detail=f"Python {version_str}"
        )
    return CheckResult(
        name="interpreter",
        status="fail",
        detail=(
            f"Python {version_str} is below the required "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
        ),
        fix=(
            f"reinstall under Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ — see "
            "README.md's Quick start"
        ),
    )


# ---------------------------------------------------------------------------
# Check 3: MCP server registered.
# ---------------------------------------------------------------------------

_MCP_FIX = "claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge"


def check_mcp_registered(claude_config_dir: Path) -> CheckResult:
    """Whether ``athenaeum`` is registered under ``.claude.json``'s
    ``mcpServers`` (issue athenaeum#2298 AC1), per the registration the README's
    Quick start and ``examples/claude-code/README.md`` both document.
    """
    claude_json_path = claude_config_dir / ".claude.json"
    if not claude_json_path.is_file():
        return CheckResult(
            name="mcp",
            status="fail",
            detail=f"no {claude_json_path} found",
            fix=_MCP_FIX,
        )
    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="mcp",
            status="fail",
            detail=f"{claude_json_path} could not be parsed as JSON: {exc}",
            fix=f"fix the JSON syntax in {claude_json_path}",
        )
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "athenaeum" not in servers:
        return CheckResult(
            name="mcp",
            status="fail",
            detail=f"no 'athenaeum' entry under mcpServers in {claude_json_path}",
            fix=_MCP_FIX,
        )
    return CheckResult(
        name="mcp", status="ok", detail="athenaeum MCP server registered"
    )


# ---------------------------------------------------------------------------
# Check 4: hooks present -- registered AND their target actually exists
# (issue athenaeum#2298 AC4's counter-example: presence in settings.json alone
# must not pass).
# ---------------------------------------------------------------------------

#: Basenames of the shipped example hook scripts / the packaged adapter
#: console script -- matched case-sensitively against a hook command's first
#: token's basename. Combined with the ``"athenaeum"`` substring check below
#: so a custom install directory (not the docs' suggested
#: ``~/.claude/hooks/athenaeum/``) is still recognized.
_KNOWN_HOOK_BASENAMES = frozenset(
    {
        "session-start-recall.sh",
        "wiki-context-inject.sh",
        "user-prompt-recall.sh",
        "pre-compact-save.sh",
        "pending-questions-surface.sh",
        "rebuild-index.sh",
        "stop-hook-validate.sh",
        "athenaeum-claude-hook",
    }
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_HOOKS_FIX = (
    "merge examples/claude-code/settings-snippet.json into "
    "{settings_path} (replacing /path/to/ with your actual scripts "
    "directory) — see docs/guides/sidecar.md"
)


def _iter_hook_commands(settings: dict) -> list[tuple[str, str]]:
    """``(event_name, command)`` pairs from a parsed ``settings.json``'s
    ``hooks`` block, tolerating any malformed/missing nesting level."""
    out: list[tuple[str, str]] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if isinstance(command, str) and command.strip():
                    out.append((str(event), command))
    return out


def _is_athenaeum_hook(command: str) -> bool:
    if "athenaeum" in command.lower():
        return True
    target = _resolve_hook_target(command)
    if target is None:
        return False
    return Path(target).name in _KNOWN_HOOK_BASENAMES


def _resolve_hook_target(command: str) -> str | None:
    """First real token of a hook ``command`` string -- the script/binary it
    invokes -- skipping a leading ``nohup`` and any ``KEY=VALUE`` env
    assignment. Returns ``None`` only for an empty/unparseable command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token == "nohup":
            continue
        if _ENV_ASSIGN_RE.match(token):
            continue
        return token
    return None


def _hook_target_exists(target: str) -> bool:
    expanded = Path(target).expanduser()
    if expanded.is_absolute() or "/" in target:
        return expanded.exists()
    # A bare command name (no path component) resolves via PATH, same as the
    # shell would resolve it.
    return shutil.which(target) is not None


def check_hooks_present(claude_config_dir: Path) -> CheckResult:
    """Athenaeum hooks are registered AND every registered target actually
    exists on disk (issue athenaeum#2298 AC1/AC4).

    AC4's counter-example is exactly the ``missing`` branch below: a hook
    entry fully present in ``settings.json`` whose target file does not
    exist on disk fails this check -- presence in settings alone is never
    sufficient.
    """
    settings_path = claude_config_dir / "settings.json"
    fix = _HOOKS_FIX.format(settings_path=settings_path)
    if not settings_path.is_file():
        return CheckResult(
            name="hooks",
            status="fail",
            detail=f"no Claude Code settings file found at {settings_path}",
            fix=fix,
        )
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="hooks",
            status="fail",
            detail=f"{settings_path} could not be parsed as JSON: {exc}",
            fix=f"fix the JSON syntax in {settings_path}",
        )
    if not isinstance(settings, dict):
        settings = {}

    commands = [
        (event, command)
        for event, command in _iter_hook_commands(settings)
        if _is_athenaeum_hook(command)
    ]
    if not commands:
        return CheckResult(
            name="hooks",
            status="fail",
            detail=f"no athenaeum hook commands registered in {settings_path}",
            fix=fix,
        )

    missing: list[str] = []
    for event, command in commands:
        target = _resolve_hook_target(command)
        if target is None or not _hook_target_exists(target):
            missing.append(f"{event} -> {target or command}")
    if missing:
        return CheckResult(
            name="hooks",
            status="fail",
            detail="registered hook target(s) not found on disk: "
            + "; ".join(missing),
            fix="re-copy the scripts from examples/claude-code/ to the "
            "directory settings.json references, or fix the path — see "
            "docs/guides/sidecar.md",
        )
    return CheckResult(
        name="hooks",
        status="ok",
        detail=f"{len(commands)} athenaeum hook command(s) registered, all targets present",
    )


# ---------------------------------------------------------------------------
# Check 5: LLM key mode -- API key / Claude Code login / regex fallback.
# ---------------------------------------------------------------------------

_API_KEY_FIX = (
    "supply an API key, or set llm.provider: claude-cli — see "
    "examples/claude-code/README.md#supplying-an-api-key"
)


def check_key_mode(
    config: dict[str, object] | None, cache_dir: Path
) -> CheckResult:
    """Which mode topic extraction (and every other LLM call) actually runs
    in: an Anthropic API key, the ambient Claude Code subscription login, or
    the regex fallback (issue athenaeum#2298 AC1).

    Resolved through the SAME seam :mod:`athenaeum.query_topics` calls
    (:func:`athenaeum.provider.resolve_provider` for the ``"topic"`` knob,
    the knob the recall hook actually routes through) rather than a second
    guess at which provider is active.

    **The regex fallback is reported as a FAILURE, not a warning
    (orchestrator ruling for issue athenaeum#2298).** The issue's own
    motivation names this exact degradation -- a missing key silently
    dropping topic extraction to regex-only -- as the silent failure this
    command exists to surface; reporting it as a mere "OK, degraded" note
    would repeat that same silence under a different command.
    """
    from athenaeum.provider import ProviderConfigError, preflight_provider, resolve_provider

    try:
        provider = resolve_provider(config, knob="topic")
    except ProviderConfigError as exc:
        return CheckResult(
            name="key_mode",
            status="fail",
            detail=str(exc),
            fix="fix llm.provider / llm.providers.topic in athenaeum.yaml",
        )

    if provider == "claude-cli":
        error = preflight_provider("claude-cli")
        if error is None:
            return CheckResult(
                name="key_mode",
                status="ok",
                detail="llm.provider=claude-cli; Claude Code login detected",
            )
        return CheckResult(
            name="key_mode",
            status="fail",
            detail=(
                "topic extraction (and every other LLM call) will fall back "
                f"to the regex extractor: {error}"
            ),
            fix=error,
        )

    # provider == "api"
    cached_env = _read_cached_config_env(cache_dir)
    key = os.environ.get("ANTHROPIC_API_KEY") or cached_env.get("ANTHROPIC_API_KEY")
    if key:
        return CheckResult(
            name="key_mode",
            status="ok",
            detail="ANTHROPIC_API_KEY is set; topic extraction uses the API",
        )
    return CheckResult(
        name="key_mode",
        status="fail",
        detail=(
            "no ANTHROPIC_API_KEY (process env or cached "
            f"{cache_dir / 'config.env'}) — topic extraction silently falls "
            "back to a regex extractor"
        ),
        fix=_API_KEY_FIX,
    )


# ---------------------------------------------------------------------------
# Check 6: search index built.
# ---------------------------------------------------------------------------


def check_search_index(
    cache_dir: Path, config: dict[str, object] | None
) -> CheckResult:
    """Whether the configured search backend's index has actually been
    built (issue athenaeum#2298 AC1), reading the SAME ``search_backend``
    config key :func:`athenaeum._cmd_query.cmd_recall` resolves through.
    """
    backend = (
        str(config.get("search_backend", "vector"))
        if isinstance(config, dict)
        else "vector"
    )
    fix = (
        "run `athenaeum reindex --path <knowledge-root>` to build it, or "
        "start a Claude Code session (the SessionStart hook builds it "
        "automatically) — see docs/guides/sidecar.md"
    )
    if backend == "fts5":
        from athenaeum.search import fts5_index_available

        if fts5_index_available(cache_dir):
            return CheckResult(
                name="search_index", status="ok", detail=f"fts5 index present at {cache_dir}"
            )
        return CheckResult(
            name="search_index",
            status="fail",
            detail=f"no fts5 index found under {cache_dir}",
            fix=fix,
        )
    if backend == "vector":
        # `_VECTOR_DIR` is search.py's own private constant -- imported
        # directly rather than duplicating the literal (same precedent as
        # tests/test_search.py's own direct imports of it) because search.py
        # exposes no public `vector_index_available` counterpart to
        # `fts5_index_available`.
        from athenaeum.search import _VECTOR_DIR

        vector_dir = cache_dir / _VECTOR_DIR
        if vector_dir.is_dir() and any(vector_dir.iterdir()):
            return CheckResult(
                name="search_index",
                status="ok",
                detail=f"vector index present at {vector_dir}",
            )
        return CheckResult(
            name="search_index",
            status="fail",
            detail=f"no vector index found under {vector_dir}",
            fix=fix,
        )
    # "keyword" (or any other value): scan-on-query, nothing to build.
    return CheckResult(
        name="search_index",
        status="ok",
        detail=f"search_backend={backend} scans on query; no index to build",
    )


# ---------------------------------------------------------------------------
# Check 7: last compile time -- informational only (never fails: a fresh
# install that has never run `athenaeum run` yet is not a broken install).
# ---------------------------------------------------------------------------


def check_last_compile(cache_dir: Path) -> CheckResult:
    """The most recent librarian compile run's timestamp, if any (issue
    athenaeum#2298 AC1). Always ``"ok"`` -- "never compiled yet" is a normal
    state for a fresh install, not a broken link.
    """
    from athenaeum.run_summary_log import (
        default_run_summary_ledger_path,
        read_run_summary_ledger,
    )

    ledger_path = default_run_summary_ledger_path(cache_dir)
    records = read_run_summary_ledger(ledger_path)
    if not records:
        return CheckResult(
            name="last_compile", status="ok", detail="no compile run recorded yet"
        )
    latest_ts = records[-1].get("ts")
    detail = f"last compile: {latest_ts}" if latest_ts else "last compile: (timestamp unavailable)"
    return CheckResult(name="last_compile", status="ok", detail=detail)


# ---------------------------------------------------------------------------
# Check 8: schedule state -- scheduled or not (issue athenaeum#2304 AC4).
# ---------------------------------------------------------------------------


def check_schedule_state(
    *,
    home: Path | None = None,
    platform_override: Literal["launchd", "systemd", "cron"] | None = None,
) -> CheckResult:
    """Whether a recurring ``athenaeum run`` schedule is installed, and
    which platform's generator applies (issue athenaeum#2304 AC4).

    Always ``"ok"`` -- reports BOTH the scheduled and the unscheduled state
    (an unscheduled install is a normal, common state, not a broken one;
    real-host verification that an installed schedule actually FIRES is
    athenaeum#2366, out of scope here). ``"skip"`` only for a platform with
    no generator (Windows). Delegates entirely to
    :func:`athenaeum.schedule.status`, which is itself read-only.
    ``platform_override`` mirrors that function's own injection, for tests.
    """
    from athenaeum import schedule

    result = schedule.status(home=home, platform_override=platform_override)
    if result.platform is None:
        return CheckResult(name="schedule", status="skip", detail=result.detail)
    if result.scheduled:
        return CheckResult(name="schedule", status="ok", detail=f"scheduled: {result.detail}")
    return CheckResult(
        name="schedule",
        status="ok",
        detail=f"not scheduled ({result.platform}): {result.detail} -- run "
        "`athenaeum schedule install` to add one",
    )


# ---------------------------------------------------------------------------
# Orchestration + rendering.
# ---------------------------------------------------------------------------


def run_checks(
    knowledge_root: Path,
    *,
    cache_dir: Path | None = None,
    claude_config_dir: Path | None = None,
    fetch: FetchFn | None = None,
    home: Path | None = None,
) -> list[CheckResult]:
    """Run every check and return the full list (issue athenaeum#2298 AC1-AC3).

    Strictly read-only (AC5): every argument is resolved exactly the way the
    hooks/MCP server already resolve it (:func:`athenaeum.config.load_config`,
    :func:`athenaeum.config.resolve_cache_dir`, :func:`default_claude_config_dir`),
    and no check below writes anything. ``home`` (issue athenaeum#2304) is the
    same injection :func:`athenaeum.schedule.status` takes, defaulting to the
    real home directory.
    """
    from athenaeum.config import load_config, resolve_cache_dir

    resolved_cache_dir = resolve_cache_dir(cache_dir).resolve()
    resolved_claude_dir = (
        claude_config_dir or default_claude_config_dir()
    ).expanduser().resolve()
    config = load_config(knowledge_root)

    return [
        check_package_version(fetch=fetch),
        check_interpreter_version(),
        check_mcp_registered(resolved_claude_dir),
        check_hooks_present(resolved_claude_dir),
        check_key_mode(config, resolved_cache_dir),
        check_search_index(resolved_cache_dir, config),
        check_last_compile(resolved_cache_dir),
        check_schedule_state(home=home),
    ]


_STATUS_LABEL: dict[Status, str] = {"ok": "OK", "fail": "FAIL", "skip": "SKIP"}


#: Appended (issue athenaeum#2450) only when at least one check is "fail" --
#: a fresh or not-yet-configured install reports FAILs for every setup step
#: not yet done, and nothing before this line said that was expected rather
#: than broken. Points back at the per-FAIL ``fix:`` lines rather than
#: repeating them.
_FRESH_INSTALL_NOTE = (
    "FAILs above are expected on a fresh or not-yet-configured install "
    "until setup is complete -- see the \"fix:\" line under each FAIL for "
    "what to run."
)


def format_report(results: list[CheckResult]) -> str:
    """Human-readable report (issue athenaeum#2298 AC2): every failed check
    prints what is wrong AND the fix.

    When at least one check is ``"fail"``, the report ends with a short
    note (issue athenaeum#2450) saying FAILs are expected on a fresh or
    not-yet-configured install and pointing back at the per-FAIL ``fix:``
    lines above -- never printed when every check is ``"ok"``/``"skip"``,
    and it changes no exit code (that is still :func:`exit_code`'s call).
    """
    lines = ["Athenaeum Doctor", "=" * 40]
    for result in results:
        label = _STATUS_LABEL.get(result.status, result.status.upper())
        lines.append(f"[{label}] {result.name}: {result.detail}")
        if result.status == "fail" and result.fix:
            lines.append(f"    fix: {result.fix}")
    failed = [r for r in results if r.status == "fail"]
    lines.append("")
    lines.append(
        f"{len(failed)} check(s) failed." if failed else "All checks passed."
    )
    if failed:
        lines.append(_FRESH_INSTALL_NOTE)
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    """Non-zero iff at least one check failed (issue athenaeum#2298 AC2) --
    a ``"skip"`` never flips this (AC5)."""
    return 1 if any(r.status == "fail" for r in results) else 0
