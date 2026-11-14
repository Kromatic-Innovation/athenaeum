# SPDX-License-Identifier: Apache-2.0
"""Tests for the staged Claude Code plugin tree (issue athenaeum#2368).

One test per acceptance criterion in the issue:

- AC1: every JSON file in the plugin tree parses, and every hook command
  resolves to a script that actually exists, either inside the plugin tree
  or inside ``examples/claude-code/`` (the hooks it reuses, per the issue's
  own "no second copy that can drift" instruction).
- AC2: the manifest's pinned athenaeum version must equal
  ``pyproject.toml``'s ``[project].version`` -- a drift test that is
  SUPPOSED to go red at a release until the manifest is bumped alongside it
  (see the plugin README's "Version-bump rule").
- AC3: the launcher, run with ``uv`` genuinely absent from ``PATH`` (a
  from-scratch minimal ``PATH`` directory under ``tmp_path``, never the
  real ``PATH`` -- the real test box always has uv installed), exits 0 and
  names ``uv`` in its output.
- AC4: the launcher's ``--ensure-ready`` hook mode, invoked while a prior
  invocation's prepare lock is already held, returns fast (no real ``uv``
  network call needed -- the lock-held branch returns before touching
  ``uv`` at all) and says it is still preparing.

None of these need network access or a real login; see the module docstring
of ``hooks/launcher.sh`` for why the two bash-only tested paths (AC3, the
"someone else already preparing" half of AC4) need nothing beyond
``env``/``bash`` (and, for AC4, a inert ``uv`` stub that is never actually
executed) on the constructed ``PATH``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO / "examples" / "claude-code-plugin"
_CLAUDE_CODE_DIR = _REPO / "examples" / "claude-code"
_MANIFEST = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
_MCP_JSON = _PLUGIN_DIR / ".mcp.json"
_HOOKS_JSON = _PLUGIN_DIR / "hooks" / "hooks.json"
_LAUNCHER = _PLUGIN_DIR / "hooks" / "launcher.sh"
_PYPROJECT = _REPO / "pyproject.toml"


# ---------------------------------------------------------------------------
# AC1: the staged tree, every JSON file parses, every hook command resolves.
# ---------------------------------------------------------------------------


def test_plugin_tree_has_the_required_files() -> None:
    assert _MANIFEST.is_file(), "no .claude-plugin/plugin.json manifest"
    assert _MCP_JSON.is_file(), "no .mcp.json"
    assert _HOOKS_JSON.is_file(), "no hooks/hooks.json"
    assert _LAUNCHER.is_file(), "no hooks/launcher.sh launcher"


def _all_json_files() -> list[Path]:
    found = sorted(_PLUGIN_DIR.rglob("*.json"))
    assert found, "no *.json found under examples/claude-code-plugin/ -- scan is broken"
    return found


def test_every_json_file_in_the_plugin_tree_parses() -> None:
    for path in _all_json_files():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path.relative_to(_REPO)} is not valid JSON: {exc}")


def _iter_hook_commands(hooks_config: dict) -> list[str]:
    """Flatten every ``command`` string out of a hooks.json-shaped dict,
    tolerating any malformed/missing nesting level (mirrors
    ``athenaeum.doctor._iter_hook_commands``'s own tolerance, reimplemented
    here rather than imported since that function reads a different file
    shape -- ``settings.json``'s top-level ``hooks`` key -- not this
    plugin's own ``hooks/hooks.json``, whose ``hooks`` key sits one level up
    from what ``.claude-plugin/plugin.json`` or ``settings.json`` wrap it
    in)."""
    out: list[str] = []
    hooks = hooks_config.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                continue
            for hook in inner:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    out.append(hook["command"])
    return out


def _resolve_command_target(command: str) -> Path | None:
    """First real token of a hook command -- substituting
    ``${CLAUDE_PLUGIN_ROOT}`` with the plugin's own real directory -- same
    shape as ``athenaeum.doctor._resolve_hook_target``, reimplemented
    locally for the same reason as ``_iter_hook_commands`` above."""
    substituted = command.replace("${CLAUDE_PLUGIN_ROOT}", str(_PLUGIN_DIR))
    try:
        tokens = shlex.split(substituted)
    except ValueError:
        tokens = substituted.split()
    for token in tokens:
        if token in ("nohup",):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        if "/" in token:
            return Path(token)
        return None  # a bare command name (sh, bash, ...) -- not a script to resolve
    return None


def test_every_hook_command_points_at_a_real_script_in_scope() -> None:
    hooks_config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    commands = _iter_hook_commands(hooks_config)
    assert commands, "hooks.json has no hook commands -- scan is broken"
    checked = 0
    for command in commands:
        target = _resolve_command_target(command)
        if target is None:
            continue
        checked += 1
        resolved = target.resolve()
        plugin_root = _PLUGIN_DIR.resolve()
        claude_code_root = _CLAUDE_CODE_DIR.resolve()
        in_plugin_tree = plugin_root in resolved.parents or resolved == plugin_root
        in_claude_code = claude_code_root in resolved.parents
        assert in_plugin_tree or in_claude_code, (
            f"hook command {command!r} resolves to {resolved}, which is "
            "neither inside examples/claude-code-plugin/ nor examples/claude-code/"
        )
        assert resolved.is_file(), (
            f"hook command {command!r} points at {resolved}, which does not exist"
        )
    assert checked > 0, "every hook command token was a bare command name -- test is vacuous"


def test_hooks_json_reuses_the_existing_recall_hooks_not_a_second_copy() -> None:
    """The issue's own instruction: hooks REUSE examples/claude-code/'s
    scripts. This fails if a future edit copies one of them into the
    plugin tree instead of referencing the original."""
    hooks_config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    commands = " ".join(_iter_hook_commands(hooks_config))
    for shared_script in (
        "session-start-recall.sh",
        "user-prompt-recall.sh",
        "wiki-context-inject.sh",
        "pending-questions-surface.sh",
        "pre-compact-save.sh",
    ):
        if shared_script in commands:
            assert not (_PLUGIN_DIR / "hooks" / shared_script).exists(), (
                f"{shared_script} is referenced AND duplicated under the plugin's "
                "own hooks/ directory -- two copies that can drift"
            )


# ---------------------------------------------------------------------------
# AC2: the manifest's pinned version must track pyproject.toml.
# ---------------------------------------------------------------------------


def _pyproject_version() -> str:
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "could not find [project].version in pyproject.toml -- scan is broken"
    return match.group(1)


def test_manifest_pins_exactly_one_athenaeum_version() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(manifest.get("version"), str) and manifest["version"], (
        "plugin.json has no top-level 'version' string -- nothing is pinned"
    )


def test_manifest_version_pin_matches_pyproject() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    pinned = manifest["version"]
    released = _pyproject_version()
    assert pinned == released, (
        f"plugin.json pins athenaeum=={pinned} but pyproject.toml's [project].version "
        f"is {released} -- bump the manifest's 'version' at this release "
        "(see examples/claude-code-plugin/README.md, 'Version-bump rule')"
    )


def test_launcher_derives_the_same_pin_from_the_manifest() -> None:
    """Guards against a second, independently-drifting copy of the pin
    living inside launcher.sh itself -- the script parses it out of
    plugin.json at runtime rather than hardcoding it a second time."""
    script_text = _LAUNCHER.read_text(encoding="utf-8")
    assert '"version"' in script_text, (
        "launcher.sh no longer reads the 'version' key out of plugin.json -- "
        "check it has not grown a second hardcoded version literal instead"
    )
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] not in (
        "0.0.0",
        "",
    ), "manifest version pin looks like a placeholder"


# ---------------------------------------------------------------------------
# AC3: uv genuinely absent -> exit 0, visible message naming uv.
# ---------------------------------------------------------------------------


def _minimal_path_dir(tmp_path: Path, *, with_uv: bool) -> Path:
    """A from-scratch PATH directory containing only ``env`` and ``bash``
    (plus, optionally, an inert ``uv`` stub) -- never the real ``PATH``, so
    this is true regardless of whether the box running CI happens to have
    uv installed."""
    bin_dir = tmp_path / "minimal-bin"
    bin_dir.mkdir()
    for tool in ("env", "bash"):
        real = shutil.which(tool)
        assert real, f"{tool} is not on the real PATH -- cannot build the test fixture"
        (bin_dir / tool).symlink_to(real)
    if with_uv:
        uv_stub = bin_dir / "uv"
        uv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        uv_stub.chmod(0o755)
    return bin_dir


def _run_launcher(
    *args: str, tmp_path: Path, path_dir: Path, cache_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": str(path_dir), "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_DIR)}
    if cache_dir is not None:
        env["ATHENAEUM_CACHE_DIR"] = str(cache_dir)
    return subprocess.run(
        [str(_LAUNCHER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_launcher_with_uv_absent_exits_zero_and_names_uv(tmp_path: Path) -> None:
    bin_dir = _minimal_path_dir(tmp_path, with_uv=False)
    result = _run_launcher("--ensure-ready", tmp_path=tmp_path, path_dir=bin_dir)
    assert result.returncode == 0, (
        f"a missing uv must not kill the session: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "uv" in combined, f"message does not name uv: {combined!r}"
    assert combined.strip(), "a missing uv must not be a silently dead hook"


def test_launcher_passthrough_mode_also_handles_uv_absent(tmp_path: Path) -> None:
    """Same counter-example, but through the .mcp.json-shaped passthrough
    invocation (no --ensure-ready) -- the uv check runs before any
    mode-specific branch, so both callers get the same protection."""
    bin_dir = _minimal_path_dir(tmp_path, with_uv=False)
    result = _run_launcher("serve", tmp_path=tmp_path, path_dir=bin_dir)
    assert result.returncode == 0
    assert "uv" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# AC4: first run, dependency build pending -> hook returns fast, says so.
# ---------------------------------------------------------------------------


def test_ensure_ready_returns_fast_and_reports_preparing_while_a_build_is_pending(
    tmp_path: Path,
) -> None:
    bin_dir = _minimal_path_dir(tmp_path, with_uv=True)
    cache_dir = tmp_path / "cache"
    lock_dir = cache_dir / "claude-code-plugin" / "prepare.lock"
    lock_dir.mkdir(parents=True)

    started = time.monotonic()
    result = _run_launcher(
        "--ensure-ready", tmp_path=tmp_path, path_dir=bin_dir, cache_dir=cache_dir
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 2.0, (
        f"a pending first-run build must not block the hook: took {elapsed:.2f}s"
    )
    combined = result.stdout + result.stderr
    assert "preparing" in combined.lower(), (
        f"no 'still preparing' message while a build is pending: {combined!r}"
    )
    # The lock this test seeded must be left alone -- this invocation did
    # not win the race to prepare, so it must not touch the lock at all.
    assert lock_dir.is_dir(), "the pending-build lock was removed by a non-owning invocation"


def test_ensure_ready_reports_ready_once_the_marker_exists(tmp_path: Path) -> None:
    bin_dir = _minimal_path_dir(tmp_path, with_uv=True)
    cache_dir = tmp_path / "cache"
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    marker = cache_dir / "claude-code-plugin" / f"ready-{manifest['version']}"
    marker.parent.mkdir(parents=True)
    marker.touch()

    result = _run_launcher(
        "--ensure-ready", tmp_path=tmp_path, path_dir=bin_dir, cache_dir=cache_dir
    )
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "ready" in combined
    assert "preparing" not in combined
