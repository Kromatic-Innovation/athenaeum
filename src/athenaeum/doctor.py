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
hook presence, LLM key mode, search index, last compile time), plus
:func:`check_page_relations` (issue athenaeum#2535 AC7: the count of
one-sided parent/child page relation pairs), each in one of three states:

- ``"ok"`` -- healthy, or (package version only) nothing to report.
- ``"fail"`` -- broken; ``detail`` says what is wrong and ``fix`` names the
  command or doc that fixes it. At least one ``"fail"`` makes
  :func:`exit_code` return non-zero (issue athenaeum#2298 AC2).
- ``"warn"`` -- first-run setup not done yet ("setup pending": MCP not
  registered, hooks not wired, no API key, no search index built) or
  another non-breaking note; ``fix`` names the command that finishes setup.
  Never flips the exit code (issue athenaeum#2906). Present-but-broken state
  (unparseable config, a hook whose target is missing, a corrupt index,
  a bad provider) is still ``"fail"``.
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
:mod:`athenaeum.provider` (L3), :mod:`athenaeum.search` (L3),
:mod:`athenaeum.run_summary_log` (L2), and (issue athenaeum#2535)
:mod:`athenaeum.page_relations` (L3) -- all function-local/deferred inside
each check (matching every other best-effort/advisory reader's own
discipline, e.g. :mod:`athenaeum.status`) -- plus :mod:`athenaeum`'s own
``__version__`` (a PEP 562 attribute access, not a submodule import, so it
creates no graph edge at all; see ``tests/test_import_graph_acyclic.py``'s
``_athenaeum_targets``). Nothing above L4 imports this module.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

#: One check's outcome state (issue athenaeum#2298 AC2/AC5).
Status = Literal["ok", "warn", "fail", "skip"]

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


def _mcp_candidate_paths(claude_config_dir: Path, home: Path | None) -> list[Path]:
    """Ordered, de-duplicated candidate ``.claude.json`` locations (issue
    athenaeum#2594).

    Real Claude Code installs write the user-scope MCP registration to
    EITHER of two places depending on version: inside the (possibly
    ``CLAUDE_CONFIG_DIR``-relocated) config directory this module already
    checked (``<claude_config_dir>/.claude.json``), or -- measured on
    Claude Code 2.1.325 with no ``CLAUDE_CONFIG_DIR`` set -- at the plain
    home-level path (``<home>/.claude.json``, a sibling of ``.claude/``,
    not a child of it). Checking only the first location means a correct
    ``claude mcp add --scope user`` run on the second kind of install
    reports FAIL with a fix that is the exact command the user just ran.
    The first candidate that exists wins; neither existing is the genuine
    "not registered" case (AC2).
    """
    resolved_home = (home if home is not None else Path.home()).expanduser()
    candidates = [claude_config_dir / ".claude.json", resolved_home / ".claude.json"]
    deduped: list[Path] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def check_mcp_registered(claude_config_dir: Path, *, home: Path | None = None) -> CheckResult:
    """Whether ``athenaeum`` is registered under ``.claude.json``'s
    ``mcpServers`` (issue athenaeum#2298 AC1, extended by athenaeum#2594 to
    search both locations real Claude Code versions write it to), per the
    registration the README's Quick start and ``examples/claude-code/README.md``
    both document. ``home`` mirrors :func:`check_schedule_state`'s own
    injection -- the same real-home concept, defaulting to the real home
    directory when not given.
    """
    candidates = _mcp_candidate_paths(claude_config_dir, home)
    claude_json_path = next((p for p in candidates if p.is_file()), None)
    if claude_json_path is None:
        locations = " or ".join(str(p) for p in candidates)
        return CheckResult(
            name="mcp",
            status="warn",
            detail=f"setup pending: no {locations} found",
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
            status="warn",
            detail=(
                "setup pending: no 'athenaeum' entry under mcpServers in "
                f"{claude_json_path}"
            ),
            fix=_MCP_FIX,
        )
    return CheckResult(
        name="mcp",
        status="ok",
        detail=f"athenaeum MCP server registered in {claude_json_path}",
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


#: Interpreters whose first non-flag argument is the script they run, so a
#: ``bash /path/to/hook.sh`` command resolves to the script rather than to
#: ``bash`` (which always exists, and would hide a missing script).
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_INTERPRETERS = _SHELL_INTERPRETERS | {"python", "python3", "node"}

#: Interpreter options that consume the next word as their value, so that
#: value is never mistaken for the script (``bash -o pipefail <script>``).
_OPTIONS_WITH_VALUE = frozenset(
    {"-o", "+o", "-O", "+O", "--rcfile", "--init-file", "-W", "-X", "-r", "--require"}
)

#: Non-shell interpreter options whose value is inline code, not a file:
#: ``python3 -c '...'``, ``node -e '...'``. The interpreter is the only
#: on-disk target such a command has.
_INLINE_CODE_OPTIONS = frozenset({"-c", "-m", "-e", "--eval", "-p", "--print"})

#: Shell control operators that separate one simple command from the next.
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";;", "(", ")"})

#: Stands in for a ``$(...)`` / backtick command substitution, so the words
#: inside it (``jq -r ...``) are never read as the hook command's own words.
_CMDSUB_PLACEHOLDER = "__athenaeum_cmdsub__"

#: Leading words that wrap the real command rather than being it.
_COMMAND_PREFIXES = frozenset({"nohup", "env", "command", "time", "exec"})


def _strip_command_substitutions(command: str) -> str:
    """Replace every ``$(...)`` and backtick substitution with a placeholder
    word, respecting quotes and nested parentheses.

    ``shlex`` knows nothing about command substitution: it splits
    ``SID=$(jq -r '.x')`` into ``SID=$(jq``, ``-r`` and so on, which is how
    the flag ``-r`` used to be reported as the hook's script path
    (athenaeum#3021). A single-quoted ``$(`` is literal text and is kept.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch == "$" and command.startswith("$(", i):
            depth = 0
            j = i + 1
            inner_quote: str | None = None
            while j < n:
                c = command[j]
                if inner_quote:
                    if c == inner_quote:
                        inner_quote = None
                elif c in "'\"":
                    inner_quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(_CMDSUB_PLACEHOLDER)
            i = j + 1
            continue
        if ch == "`":
            end = command.find("`", i + 1)
            out.append(_CMDSUB_PLACEHOLDER)
            i = n if end == -1 else end + 1
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "'" and quote is None:
            quote = "'"
        out.append(ch)
        i += 1
    return "".join(out)


def _split_simple_commands(command: str) -> list[list[str]]:
    """Split a hook command into its simple commands (word lists), on the
    shell control operators ``;``, ``&&``, ``||``, ``|``, ``&`` and
    parentheses. Newlines separate commands too."""
    text = _strip_command_substitutions(command).replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # ``shlex.split`` (what this check used before) disables comments; keep
    # that, so a ``#`` inside a word (``--tag=#x``) never truncates the rest.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                commands.append(current)
            current = []
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands


def _simple_command_target(words: list[str]) -> str | None:
    """The script/binary one simple command invokes, or ``None`` when it
    cannot be read with confidence.

    Skips leading wrappers (``nohup``, ``env``) and ``KEY=VALUE``
    assignments; looks through an interpreter (``bash script.sh``) to its
    script; stops at a redirection. Never returns a word that starts with
    ``-`` -- a flag is never a hook target.
    """
    i = 0
    while i < len(words):
        word = words[i]
        if word in _COMMAND_PREFIXES or _ENV_ASSIGN_RE.match(word):
            i += 1
            continue
        if word.startswith("-") and i > 0 and words[i - 1] == "env":
            i += 1  # ``env -i FOO=bar cmd``
            continue
        break
    if i >= len(words):
        return None
    head = words[i]
    if head.startswith("-") or head == _CMDSUB_PLACEHOLDER or head[:1] in "<>":
        return None
    if Path(head).name not in _INTERPRETERS:
        return head
    rest = words[i + 1 :]
    is_shell = Path(head).name in _SHELL_INTERPRETERS
    command_string_next = False
    j = 0
    while j < len(rest):
        word = rest[j]
        if word in _OPTIONS_WITH_VALUE:
            j += 2  # the option and its value
            continue
        if is_shell and word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            # ``bash -c '<cmd>'`` / ``bash -lc '<cmd>'``: the first operand
            # is a command string, resolved as a hook command of its own.
            command_string_next = True
            j += 1
            continue
        if not is_shell and word in _INLINE_CODE_OPTIONS:
            return head
        if word.startswith("-") or word.startswith("+"):
            j += 1
            continue
        if word == _CMDSUB_PLACEHOLDER or word[:1] in "<>":
            return None
        if command_string_next:
            return _resolve_hook_target(word)
        return word
    # A bare ``bash`` with no script argument reads its script from stdin.
    return head


def _references_athenaeum(target: str) -> bool:
    return "athenaeum" in target.lower() or Path(target).name in _KNOWN_HOOK_BASENAMES


def _resolve_hook_target(command: str) -> str | None:
    """The script/binary a hook ``command`` string invokes.

    A plain command resolves to its first real word, skipping a leading
    ``nohup`` and any ``KEY=VALUE`` env assignment, and looking through an
    interpreter (``bash script.sh``) to the script it runs.

    A compound command (``;``, ``&&``, ``||``, ``|``, ``&``, ``$(...)``) is
    split into simple commands and each is resolved separately
    (athenaeum#3021). The target is the one that references an athenaeum
    hook script or console entry point; failing that, the single simple
    command whose target is a path. Returns ``None`` when the target cannot
    be determined with confidence -- including an empty command. A word
    starting with ``-`` is never returned.
    """
    simple = _split_simple_commands(command)
    targets = [t for t in (_simple_command_target(w) for w in simple) if t is not None]
    if not targets:
        return None
    if len(simple) == 1:
        return targets[0]
    athenaeum_targets = [t for t in targets if _references_athenaeum(t)]
    if athenaeum_targets:
        return athenaeum_targets[0]
    path_targets = [t for t in targets if "/" in t]
    if len(path_targets) == 1:
        return path_targets[0]
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
            status="warn",
            detail=f"setup pending: no Claude Code settings file found at {settings_path}",
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
            status="warn",
            detail=f"setup pending: no athenaeum hook commands registered in {settings_path}",
            fix=fix,
        )

    missing: list[str] = []
    undetermined: list[str] = []
    for event, command in commands:
        target = _resolve_hook_target(command)
        if target is None:
            # Not knowing the target is not evidence it is missing
            # (athenaeum#3021): a false FAIL sends a new user to re-copy
            # scripts that are already in place.
            undetermined.append(f"{event} -> {command}")
        elif not _hook_target_exists(target):
            missing.append(f"{event} -> {target}")
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
    if undetermined:
        return CheckResult(
            name="hooks",
            status="warn",
            detail="could not determine hook target from a compound shell "
            "command: " + "; ".join(undetermined),
            fix="check that the script each command runs exists, or register "
            "the script directly as the hook command — see docs/guides/sidecar.md",
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


_LOGIN_FIX = "run `claude` and `/login`, or set ANTHROPIC_API_KEY"

#: Seconds allowed for ``claude auth status`` (a local read, no model call).
_LOGIN_PROBE_TIMEOUT = 20


def _claude_login_missing() -> bool:
    """True only when ``claude auth status`` positively reports no login.

    The same free probe ``import-memory`` uses (a local read; it never makes a
    billed model call). A probe that cannot answer (timeout, spawn error,
    unparseable output) is NOT treated as a missing login: a flaky probe must
    not turn a working login into a FAIL.
    """
    binary = os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude"
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_LOGIN_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return True
    try:
        status = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return False
    return isinstance(status, dict) and status.get("loggedIn") is False


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

    **A missing credential is a WARN ("setup pending"), never "ok"
    (issue athenaeum#2906, superseding the athenaeum#2298 ruling that made
    it a FAIL).** A fresh install has not had a key supplied yet; that is
    first-run setup, not breakage, so it must not exit non-zero. It still
    surfaces loudly (the regex fallback is named, with the fix). The same
    holds for the shipped ``llm.provider: claude-cli`` default when the
    ``claude`` binary or login is not there yet. An unrecognised
    ``llm.provider`` value stays a FAIL.
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
        if error is None and _claude_login_missing():
            return CheckResult(
                name="key_mode",
                status="warn",
                detail=(
                    "setup pending: llm.provider=claude-cli but Claude Code is not logged in "
                    "(`claude auth status` reports no login); compiles cannot run"
                ),
                fix=_LOGIN_FIX,
            )
        if error is None:
            return CheckResult(
                name="key_mode",
                status="ok",
                detail="llm.provider=claude-cli; Claude Code login confirmed",
            )
        return CheckResult(
            name="key_mode",
            status="warn",
            detail=(
                "setup pending: llm.provider=claude-cli but the `claude` binary is not on "
                "PATH; compiles cannot run"
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
    if shutil.which(os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude") is None:
        return CheckResult(
            name="key_mode",
            status="warn",
            detail=(
                "setup pending: no ANTHROPIC_API_KEY (process env or cached "
                f"{cache_dir / 'config.env'}) and no `claude` binary on PATH; "
                "compiles cannot run"
            ),
            fix=(
                "install Claude Code (https://claude.com/claude-code), then "
                f"{_LOGIN_FIX} — see "
                "examples/claude-code/README.md#supplying-an-api-key"
            ),
        )
    return CheckResult(
        name="key_mode",
        status="warn",
        detail=(
            "setup pending: no ANTHROPIC_API_KEY (process env or cached "
            f"{cache_dir / 'config.env'}) — topic extraction falls back to a "
            "regex extractor until a key or Claude Code login is configured"
        ),
        fix=_API_KEY_FIX,
    )


# ---------------------------------------------------------------------------
# Check 6: search index built.
# ---------------------------------------------------------------------------


def _fts5_unreadable_reason(cache_dir: Path) -> str | None:
    """``None`` when the fts5 DB opens read-only and answers a trivial query,
    else the reason it does not (a present-but-corrupt index is breakage, not
    pending setup -- issue athenaeum#2906). Read-only: ``mode=ro`` URI."""
    import sqlite3

    from athenaeum.search import _DB_NAME

    try:
        conn = sqlite3.connect(f"file:{cache_dir / _DB_NAME}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return str(exc)
    return None


def check_search_index(
    cache_dir: Path, config: dict[str, object] | None
) -> CheckResult:
    """Whether the configured search backend's index has actually been
    built (issue athenaeum#2298 AC1), reading the SAME ``search_backend``
    config key :func:`athenaeum._cmd_query.cmd_recall` resolves through.
    """
    from athenaeum.config import resolve_search_backend

    # Issue athenaeum#2458: one resolution rule, `resolve_search_backend`.
    backend = resolve_search_backend(config if isinstance(config, dict) else None)
    fix = (
        "run `athenaeum reindex --path <knowledge-root>` to build it, or "
        "start a Claude Code session (the SessionStart hook builds it "
        "automatically) — see docs/guides/sidecar.md"
    )
    if backend == "fts5":
        from athenaeum.search import fts5_index_available

        if fts5_index_available(cache_dir):
            problem = _fts5_unreadable_reason(cache_dir)
            if problem is not None:
                return CheckResult(
                    name="search_index",
                    status="fail",
                    detail=f"fts5 index at {cache_dir} is unreadable: {problem}",
                    fix=(
                        "delete the index file and rebuild it with "
                        "`athenaeum reindex --path <knowledge-root>`"
                    ),
                )
            return CheckResult(
                name="search_index", status="ok", detail=f"fts5 index present at {cache_dir}"
            )
        return CheckResult(
            name="search_index",
            status="warn",
            detail=f"setup pending: no fts5 index found under {cache_dir}",
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
        # `[mcp]`-only install: `session-end` / `recall` fall back to fts5 when
        # chromadb is absent (same `find_spec` probe the librarian uses), so an
        # fts5 index is the index this install actually serves from.
        from athenaeum.search import fts5_index_available

        if importlib.util.find_spec("chromadb") is None and fts5_index_available(cache_dir):
            return CheckResult(
                name="search_index",
                status="ok",
                detail=(
                    f"search_backend=vector but chromadb is not installed; using the "
                    f"fts5 fallback index at {cache_dir} (install the [vector] extra "
                    "for semantic search)"
                ),
            )
        return CheckResult(
            name="search_index",
            status="warn",
            detail=f"setup pending: no vector index found under {vector_dir}",
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


def check_page_relations(wiki_root: Path) -> CheckResult:
    """Count of one-sided parent/child page relation pairs (issue
    athenaeum#2535 AC7) -- a ``parent:`` key with no matching ``related``
    row on the named parent, or the reverse. Counts only, no further
    diagnosis. Read-only (AC5): :func:`athenaeum.page_relations.count_one_sided_relations`
    only ever parses existing frontmatter.

    A missing ``wiki/`` directory reports zero -- there is nothing to
    check, not a broken install (that is :func:`check_last_compile`'s and
    friends' job, not this one's).
    """
    from athenaeum.page_relations import count_one_sided_relations

    if not wiki_root.is_dir():
        return CheckResult(name="page_relations", status="ok", detail="0 one-sided pair(s)")
    count = count_one_sided_relations(wiki_root)
    if count == 0:
        return CheckResult(name="page_relations", status="ok", detail="0 one-sided pair(s)")
    return CheckResult(
        name="page_relations",
        status="fail",
        detail=f"{count} one-sided parent/child pair(s)",
        fix="athenaeum relate --child <uid> --parent <uid> (or --detach) to repair each pair",
    )


def check_merge_rejected_with_targets(wiki_root: Path) -> CheckResult:
    """A ``merge_rejected_with:`` entry naming no live page (issue
    athenaeum#2546 AC4) -- e.g. the page it named was removed by hand
    outside the fold/rename primitives (both of which keep an old identity
    resolvable via ``aliases:``, so a routine rename or fold never trips
    this). Read-only: parses existing frontmatter only, same as
    :func:`check_page_relations`.

    Resolution is identity-extended exactly like
    :func:`athenaeum.declared_relationships.declared_relationship` -- an
    entry matching ANY live page's ``name:`` OR one of its ``aliases:``
    counts as resolved -- via
    :func:`athenaeum.declared_relationships.unresolved_merge_rejected_with`,
    so this reuses the one primitive rather than re-deriving the match.
    """
    from athenaeum.declared_relationships import (
        DeclaredRelationshipFacts,
        unresolved_merge_rejected_with,
    )
    from athenaeum.models import parse_frontmatter

    if not wiki_root.is_dir():
        return CheckResult(
            name="merge_rejected_with_targets", status="ok", detail="0 dangling link(s)"
        )

    facts: list[DeclaredRelationshipFacts] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        name = meta.get("name")
        if not isinstance(name, str):
            name = ""
        rejected_raw = meta.get("merge_rejected_with")
        rejected = [str(r) for r in rejected_raw] if isinstance(rejected_raw, list) else []
        aliases_raw = meta.get("aliases")
        aliases = (
            [str(a) for a in aliases_raw if isinstance(a, str)]
            if isinstance(aliases_raw, list)
            else []
        )
        if name or rejected:
            facts.append(
                DeclaredRelationshipFacts(
                    name=name, merge_rejected_with=rejected, aliases=aliases
                )
            )

    dangling = unresolved_merge_rejected_with(facts)
    if not dangling:
        return CheckResult(
            name="merge_rejected_with_targets", status="ok", detail="0 dangling link(s)"
        )
    shown = ", ".join(f"{page!r} -> {entry!r}" for page, entry in dangling[:5])
    if len(dangling) > 5:
        shown += f", ... ({len(dangling) - 5} more)"
    return CheckResult(
        name="merge_rejected_with_targets",
        status="fail",
        detail=f"{len(dangling)} dangling merge_rejected_with link(s): {shown}",
        fix=(
            "the named page no longer exists under wiki/ with that name or "
            "alias -- fix the frontmatter by hand, or revoke the stale link "
            "with `athenaeum decisions revoke-merge-rejection --apply`"
        ),
    )


# ---------------------------------------------------------------------------
# Orchestration + rendering.
# ---------------------------------------------------------------------------


def check_removed_config_keys(config: dict) -> CheckResult:
    """WARN when ``athenaeum.yaml`` still sets a key a release removed.

    Never "fail": the key is ignored, so nothing is broken, but the operator
    may believe a switch still does something (issue athenaeum#2686). Same
    source of truth as the ``load_config`` log warning and the ``athenaeum
    run`` stderr notice: :func:`athenaeum.config.find_removed_config_keys`.
    """
    from athenaeum.config import find_removed_config_keys, format_removed_key_notice

    found = find_removed_config_keys(config)
    if not found:
        return CheckResult("removed_config_keys", "ok", "no removed config keys set")
    detail = " | ".join(format_removed_key_notice(k, r) for k, r in found)
    return CheckResult(
        "removed_config_keys",
        "warn",
        detail,
        fix="delete the listed key(s) from athenaeum.yaml",
    )


def check_unmarked_vault_locations(config: dict) -> CheckResult:
    """WARN for each outside-the-corpus location that holds routed fields but is unmarked.

    A custom location that ``storage.mapping`` routes a page type to, that has
    no corpus capability, and that does not carry ``sensitive: true`` keeps
    today's read behaviour (its values are still withheld unless the read flag
    is set), but nothing a report or an access check can read says it is a
    vault (athenaeum#3161). Never "fail": nothing is broken.
    """
    from athenaeum.config import resolve_storage_mapping
    from athenaeum.storage import StorageConfigError, available_adapters

    try:
        adapters = available_adapters(config)
        mapping = resolve_storage_mapping(config)
    except StorageConfigError as exc:
        return CheckResult("unmarked_vault_locations", "skip", f"storage config invalid: {exc}")
    unmarked = sorted(
        {
            name
            for name in mapping.values()
            if name in adapters
            and not adapters[name].corpus_policy.in_corpus
            and not adapters[name].sensitive
        }
    )
    if not unmarked:
        return CheckResult(
            "unmarked_vault_locations", "ok", "no unmarked off-corpus location holds routed fields"
        )
    return CheckResult(
        "unmarked_vault_locations",
        "warn",
        "off-corpus location(s) holding routed fields are not marked sensitive: "
        + ", ".join(unmarked),
        fix="set sensitive: true on each listed storage.adapters entry in athenaeum.yaml",
    )


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
    real home directory; the MCP check (issue athenaeum#2594) reuses it to
    also search the home-level ``.claude.json`` real Claude Code versions
    write the user-scope registration to.
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
        check_mcp_registered(resolved_claude_dir, home=home),
        check_hooks_present(resolved_claude_dir),
        check_key_mode(config, resolved_cache_dir),
        check_search_index(resolved_cache_dir, config),
        check_last_compile(resolved_cache_dir),
        check_schedule_state(home=home),
        check_page_relations(knowledge_root / "wiki"),
        check_merge_rejected_with_targets(knowledge_root / "wiki"),
        check_removed_config_keys(config),
        check_unmarked_vault_locations(config),
    ]


_STATUS_LABEL: dict[Status, str] = {
    "ok": "OK",
    "warn": "WARN",
    "fail": "FAIL",
    "skip": "SKIP",
}


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
        if result.status in ("fail", "warn") and result.fix:
            lines.append(f"    fix: {result.fix}")
    failed = [r for r in results if r.status == "fail"]
    lines.append("")
    warned = [r for r in results if r.status == "warn"]
    if failed:
        lines.append(f"{len(failed)} check(s) failed.")
    elif warned:
        lines.append(f"All checks passed, with {len(warned)} warning(s).")
    else:
        lines.append("All checks passed.")
    if failed:
        lines.append(_FRESH_INSTALL_NOTE)
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    """Non-zero iff at least one check failed (issue athenaeum#2298 AC2) --
    a ``"skip"`` never flips this (AC5)."""
    return 1 if any(r.status == "fail" for r in results) else 0
