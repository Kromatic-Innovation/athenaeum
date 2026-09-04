# SPDX-License-Identifier: Apache-2.0
"""Public-surface extraction and the semver guard over it (issue athenaeum#1335).

Why this exists: the 0.19.45 candidate removed four public entry points
(``athenaeum people``, ``bounce-divergence``, ``do-not-email-divergence``, the
``read_person`` MCP tool) while moving only the PATCH digit from the last
published release, ``v0.19.0``. The CHANGELOG documented every removal honestly;
the version number contradicted it, and the version number is what dependency
resolvers read. Two zenodotus reviewers caught it by reading; nothing in CI did.

The guard is deliberately a SET DIFFERENCE, not a count. Between v0.19.0 and the
candidate the CLI grew 39 -> 48 subcommands and ``__all__`` grew 21 -> 32 names,
so every count-based check — "did the surface shrink?" — passes while four
named entry points are gone. Only asking *which names disappeared* sees it.

Three surface dimensions, because the four removals span three of them and a
guard covering one would have caught at most two of the four:

============================  =====================  =========================
dimension                     extracted by           removals it would catch
============================  =====================  =========================
CLI top-level subcommands     runtime, from          ``athenaeum people``,
                              :func:`build_parser`   ``bounce-divergence``,
                              subparser choices      ``do-not-email-divergence``
Python API                    runtime,               (none of the four, but it
                              ``athenaeum.__all__``  is the documented import
                                                     contract)
MCP tools                     static, AST over       ``read_person``
                              ``mcp_server.py``
============================  =====================  =========================

A fourth dimension, ``dir_attrs``, was added for athenaeum#1401. PR #1373
rewrote ``src/athenaeum/__init__.py`` to lazily export via PEP 562
``__getattr__`` instead of eagerly importing ``athenaeum.librarian``, and as a
side effect stopped populating ~40 submodule names (``athenaeum.config``,
``.pii``, ``.spend``, ... — never listed in ``__all__``, only ever reachable
because the eager import happened to leave them as module attributes).
``dir(athenaeum)`` went 78 -> 50 names between v0.19.0 and 0.20.0 and none of
the other three dimensions saw it: ``__all__`` was untouched, nothing CLI- or
MCP-shaped was removed. ``dir_attrs`` is guarded by the exact same rule as the
other three — a name gone from ``dir(<module>)`` requires a minor-or-greater
version bump, same as a name gone from ``__all__`` — deliberately, not a new
mechanism: an accidental side-effect export is still something a consumer's
code may depend on (``athenaeum.config`` worked and someone may have written
``import athenaeum; athenaeum.config.something``), so it gets the same
explicit-acceptance-via-version-bump treatment as a declared one. The
alternative (hard-fail any ``dir()`` removal forever, even on a proper minor
bump) would make PEP 562 lazy-export adoption impossible in any future
package, and the existing three dimensions already establish "a minor bump IS
the explicit accept" as this guard's idiom — inventing a second, stricter
gate for the fourth dimension alone would be inconsistent for no
detection-power gain (the version-bump rule still requires the bump to
actually happen, at which point the corresponding CHANGELOG entry — see
athenaeum#1400 — is where the specific delta gets *named* for a human
reader).

**Why extracted via a fresh subprocess, unlike the other three.** PEP 562
``__getattr__`` MAY cache resolved attributes onto the module's ``__dict__``
after first access (CPython does). An earlier test in the same pytest run
that did ``athenaeum.config`` would warm the cache and make ``dir_attrs()``
over-report if it ran in-process against the already-imported module. Every
other dimension is immune to this (``__all__`` is a static list; CLI/MCP
don't touch ``__getattr__``), so ``dir_attrs`` alone always subprocesses.

**Why the CLI and Python surfaces are extracted at RUNTIME and MCP statically.**
Subcommands are registered across ~20 ``_cmd_*.py`` modules, and several of them
register nested sub-subparsers (``athenaeum query person`` was one). A static
scan for ``add_parser("...")`` cannot tell a top-level subcommand from a nested
one without reimplementing argparse, and conflating the two would make the
baseline wrong in both directions. ``build_parser()`` answers exactly and
cheaply. MCP tools are the opposite case: they are ``@mcp.tool()``-decorated
functions inside one builder function in a single module, so the AST is
unambiguous, and reading them statically avoids constructing a FastMCP server
(and depending on the ``mcp`` extra) just to enumerate names.

:func:`extract_surface` therefore takes an optional *source_root*, so the same
extractor runs against a historical checkout — which is how
``scripts/snapshot_public_surface.py`` produced the committed v0.19.0 baseline,
rather than anyone hand-listing what v0.19.0 "had".
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: The four dimensions, in the order reports render them.
SURFACE_DIMENSIONS: tuple[str, ...] = (
    "cli_subcommands",
    "python_all",
    "mcp_tools",
    "dir_attrs",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = _REPO_ROOT / "tests" / "fixtures" / "public_surface_baseline.json"


def cli_subcommands() -> list[str]:
    """Top-level ``athenaeum <cmd>`` subcommands, from the live parser."""
    from athenaeum.cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return sorted(action.choices)
    raise AssertionError("athenaeum.cli.build_parser() registered no subparsers")


def python_all() -> list[str]:
    """The package's declared import contract, ``athenaeum.__all__``."""
    import athenaeum

    return sorted(athenaeum.__all__)


def _is_mcp_tool_ref(node: ast.expr) -> ast.Call | None:
    """``mcp.tool`` / ``mcp.tool(...)`` -> the Call node (or a synthetic None).

    Returns the ``ast.Call`` when the reference was called with arguments (so
    the caller can read a ``name=`` keyword), and ``None`` for a bare
    attribute reference. Raises nothing — a non-match is signalled by the
    ``matched`` flag the caller checks first.
    """
    target = node.func if isinstance(node, ast.Call) else node
    if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
        return None
    if not (isinstance(target.value, ast.Name) and target.value.id == "mcp"):
        return None
    return node if isinstance(node, ast.Call) else None


def _explicit_tool_name(call: ast.Call | None) -> str | None:
    if call is None:
        return None
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def mcp_tool_names(source_root: Path | None = None) -> list[str]:
    """Names of the MCP tools ``mcp_server.py`` registers.

    Covers BOTH registration forms this module uses:

    * the ``@mcp.tool()`` decorator, which every tool but one uses; and
    * the explicit ``mcp.tool()(recall)`` call, which ``recall`` alone uses so
      that its dynamically-computed ``__doc__`` is attached before registration
      (see the athenaeum#964 comment at the call site).

    Missing the second form is not hypothetical — the first draft of this
    extractor did, and reported ``recall`` as a REMOVED public surface, which
    would have failed the guard on a tool that is very much still there. That
    is why :func:`mcp_tool_names` is cross-checked against the live FastMCP
    registration in ``tests/test_public_surface_guard.py``: a static extractor
    that silently under-reports turns this guard into a rubber stamp.

    An explicit ``name=`` keyword wins over the function name, matching what
    FastMCP itself does.
    """
    root = source_root or (_REPO_ROOT / "src")
    module = root / "athenaeum" / "mcp_server.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))

    names: set[str] = set()

    for node in ast.walk(tree):
        # Form 1: @mcp.tool() / @mcp.tool above a def.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
                    continue
                if not (isinstance(target.value, ast.Name) and target.value.id == "mcp"):
                    continue
                call = decorator if isinstance(decorator, ast.Call) else None
                names.add(_explicit_tool_name(call) or node.name)
            continue

        # Form 2: mcp.tool()(fn) as a bare expression statement.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Call):
            registrar = _is_mcp_tool_ref(node.func)
            inner = node.func
            target = inner.func
            if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
                continue
            if not (isinstance(target.value, ast.Name) and target.value.id == "mcp"):
                continue
            explicit = _explicit_tool_name(registrar)
            if explicit:
                names.add(explicit)
            elif node.args and isinstance(node.args[0], ast.Name):
                names.add(node.args[0].id)

    return sorted(names)


def dir_attrs(source_root: Path | None = None) -> list[str]:
    """``dir(athenaeum)`` from a FRESH interpreter (issue athenaeum#1401).

    Always subprocesses, even for the working-tree case: PEP 562
    ``__getattr__`` may cache a resolved name onto the module's ``__dict__``
    on first access, so an in-process call sharing an interpreter with earlier
    tests could see names an untouched import would not. A subprocess starts
    with a module that has never had ``__getattr__`` called on it.

    *source_root*, when given, is prepended to the subprocess's
    ``PYTHONPATH`` so a historical checkout can be measured the same way
    :func:`mcp_tool_names` reads one statically — matching how
    ``scripts/snapshot_public_surface.py`` already snapshots dimensions
    against a tree that isn't the one currently installed.
    """
    env = os.environ.copy()
    if source_root is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(source_root) + (os.pathsep + existing if existing else "")
    code = "import json, athenaeum; print(json.dumps(sorted(dir(athenaeum))))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return list(json.loads(result.stdout))


def extract_surface(source_root: Path | None = None) -> dict[str, list[str]]:
    """All four dimensions of the public surface.

    *source_root* selects the tree the STATIC dimension is read from and the
    subprocessed :func:`dir_attrs` dimension imports from; the two in-process
    runtime dimensions always reflect whatever ``athenaeum`` is importable, so
    a caller snapshotting a historical tree must put it on ``sys.path`` first
    (see ``scripts/snapshot_public_surface.py``).
    """
    return {
        "cli_subcommands": cli_subcommands(),
        "python_all": python_all(),
        "mcp_tools": mcp_tool_names(source_root),
        "dir_attrs": dir_attrs(source_root),
    }


def parse_version(version: str) -> tuple[int, int, int]:
    """``"0.19.45"`` -> ``(0, 19, 45)``, ignoring any pre-release suffix.

    Tolerates both separator styles a pre-release can use — semver's
    ``0.20.0-rc.1`` and PEP 440's suffix-without-separator ``0.20.0rc1`` —
    because the guard only ever compares major/minor and a release candidate
    for 0.20.0 must be held to 0.20.0's rules.
    """
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) < 3:
        raise ValueError(f"not a three-part version: {version!r}")
    numbers = []
    for part in parts[:3]:
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            raise ValueError(f"not a three-part version: {version!r}")
        numbers.append(int(digits))
    return (numbers[0], numbers[1], numbers[2])


def bump_is_at_least_minor(baseline: str, current: str) -> bool:
    """Whether *current* moves the minor digit or higher relative to *baseline*.

    A patch-only move (``0.19.0`` -> ``0.19.45``) is False; a minor move
    (``0.19.0`` -> ``0.20.0``) or a major one is True. Deliberately does NOT
    treat 0.x as exempt: this project's own ``CHANGELOG`` header claims
    adherence to semver, and semver's 0.x allowance is permission to break
    things in a MINOR bump, not permission to break them in a patch.
    """
    b_major, b_minor, _ = parse_version(baseline)
    c_major, c_minor, _ = parse_version(current)
    return (c_major, c_minor) > (b_major, b_minor)


def removed_names(
    baseline_surface: dict[str, list[str]], current_surface: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Per dimension, the names present in *baseline_surface* and now gone.

    Dimensions absent from either side are treated as empty rather than as an
    error, so adding a fourth dimension later does not retroactively invalidate
    a committed baseline.
    """
    removed: dict[str, list[str]] = {}
    for dimension in SURFACE_DIMENSIONS:
        before = set(baseline_surface.get(dimension, []))
        after = set(current_surface.get(dimension, []))
        gone = sorted(before - after)
        if gone:
            removed[dimension] = gone
    return removed


def check_surface_against_version(
    *,
    baseline_version: str,
    baseline_surface: dict[str, list[str]],
    current_version: str,
    current_surface: dict[str, list[str]],
) -> str | None:
    """``None`` when the version honours the surface change, else why not.

    The rule, stated once: **if any public name present in the last published
    release is gone, the version must move the minor digit or higher.** Adding
    names is always fine. Removing them on a patch bump is not.
    """
    removed = removed_names(baseline_surface, current_surface)
    if not removed:
        return None
    if bump_is_at_least_minor(baseline_version, current_version):
        return None

    lines = [
        f"version {current_version} is a PATCH-only bump from the last published "
        f"release {baseline_version}, but public surface was removed:",
        "",
    ]
    for dimension, gone in removed.items():
        lines.append(f"  {dimension}:")
        lines.extend(f"    - {name}" for name in gone)
    lines += [
        "",
        "Removing a public entry point requires a minor-or-greater bump. Either "
        f"raise the version to {parse_version(baseline_version)[0]}."
        f"{parse_version(baseline_version)[1] + 1}.0 or higher, or restore the "
        "names above.",
        "",
        "If a name was removed deliberately AND the version is right, refresh "
        "the baseline: python scripts/snapshot_public_surface.py --output "
        "tests/fixtures/public_surface_baseline.json",
    ]
    return "\n".join(lines)


def load_baseline(path: Path | None = None) -> dict[str, Any]:
    """The committed snapshot of the last published release's surface."""
    return json.loads((path or BASELINE_PATH).read_text(encoding="utf-8"))
