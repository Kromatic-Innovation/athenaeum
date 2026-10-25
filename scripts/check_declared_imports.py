#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail the build when `src/` or `tests/` imports a third-party package that
`pyproject.toml` does not declare (issue athenaeum#2136).

WHY THIS EXISTS. `tests/test_batch_mode.py`, `tests/test_retry.py`,
`tests/test_retry_registry.py` and `tests/test_librarian_zero_yield.py` all
`import httpx` at module level, and `httpx` was declared nowhere in
`pyproject.toml`. It arrived only as a transitive of `anthropic`, so the suite
worked for as long as the resolver happened to pick an `anthropic` that still
depended on it. `anthropic` 1.17.0 switched to `httpx2`; a contributor following
CONTRIBUTING.md (`pip install -e ".[dev]"`, no `requirements-ci.lock`) then got
three collection ERRORs and zero tests run. CI never saw it, because CI installs
with the lock, which pins the older `anthropic` that still carries `httpx`.

So the defect class is "an import whose availability is an accident of
resolution", and the cheapest honest check is a direct one: every third-party
top-level import under `src/` and `tests/` must be declared in
`[project.dependencies]` or in one of the `[project.optional-dependencies]`
extras. A second instance of the same class was found by this check on its first
run (`packaging`, imported at module level by `tests/test_ci_lock_parity.py` and
`tests/test_cli_tool_bridge_mcp_api_guard.py`, arriving transitively via pip's
own build requirements).

WHAT COUNTS AS A VIOLATION. An import is a violation when all three hold:

  1. its top-level name is neither stdlib nor first-party (resolvable inside the
     scanned trees, which is also how pytest resolves `from offline_embeddings
     import ...` between sibling test modules), AND
  2. no requirement in `pyproject.toml` declares it -- matched by the
     distribution(s) that provide the import name in THIS environment, and also
     by the import name itself (a declared `fastmcp` is satisfied by the
     `fastmcp-slim` distribution, which is what provides the `fastmcp` import
     package), AND
  3. it is not guarded by `try: ... except ImportError` -- a guarded import is a
     deliberate optional-feature probe that degrades instead of crashing
     (`onnxruntime` in `search.py`, `chromadb` in `tests/conftest.py`).

A LAZY import (inside a function body) is NOT exempt: deferring the failure from
import time to call time does not make an undeclared dependency declared. The
repo already decided this once -- `numpy` was a lazy, undeclared,
chromadb-transitive import in `recurring_claims.py` until athenaeum#542 declared
it explicitly, for exactly this reason.

An import DECLARED in pyproject but absent from the current environment is NOT a
violation: that is an optional extra this interpreter simply did not install
(`chromadb`/`numpy` under a `[dev]`-only install). The collect-only step in the
`clean-venv` CI job is what catches a hard import of something absent.

Usage:
    python scripts/check_declared_imports.py [--root <repo root>]
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # `packaging` is declared in the `dev` extra; this script runs under it.
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError:  # pragma: no cover - defensive, see docstring
    print(
        "check_declared_imports: `packaging` is not installed; "
        'install the dev extra (`pip install -e ".[dev]"`).',
        file=sys.stderr,
    )
    raise

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent

# Scanned trees. `scripts/` is deliberately NOT scanned: those are operator
# tools run ad hoc, not part of the installed package or the test suite, and
# several legitimately probe for host tooling that no install declares.
SCAN_DIRS = ("src", "tests")


@dataclass(frozen=True)
class ImportSite:
    """One top-level import name, and where/how it was imported."""

    name: str
    path: str
    line: int
    guarded: bool


@dataclass(frozen=True)
class Violation:
    name: str
    sites: tuple[ImportSite, ...]
    providers: tuple[str, ...]

    def describe(self) -> str:
        where = ", ".join(f"{s.path}:{s.line}" for s in self.sites[:4])
        if len(self.sites) > 4:
            where += f", +{len(self.sites) - 4} more"
        if self.providers:
            via = (
                f"provided by the installed distribution(s) {sorted(self.providers)}, "
                "which pyproject.toml does not declare"
            )
        else:
            via = (
                "not provided by any installed distribution -- it is reaching the "
                "suite only by accident of resolution, or not at all"
            )
        return f"{self.name}: {via}. Imported at {where}"


def _guarding_handlers_catch_importerror(handlers: Sequence[ast.ExceptHandler]) -> bool:
    """True when a `try` block's handlers absorb a missing module."""
    absorbing = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    for handler in handlers:
        if handler.type is None:  # bare `except:`
            return True
        candidates = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        for node in candidates:
            if isinstance(node, ast.Name) and node.id in absorbing:
                return True
            if isinstance(node, ast.Attribute) and node.attr in absorbing:
                return True
    return False


def scan_source(text: str, path: str) -> list[ImportSite]:
    """Every absolute top-level import name in one module, with guarded-ness.

    Relative imports (`from . import x`) are skipped: they are first-party by
    construction and carry no top-level name.
    """
    tree = ast.parse(text)
    sites: list[ImportSite] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try):
            inner_guarded = guarded or _guarding_handlers_catch_importerror(node.handlers)
            for child in node.body:
                walk(child, inner_guarded)
            # `except`/`else`/`finally` bodies are not protected by this try.
            for child in list(node.handlers) + list(node.orelse) + list(node.finalbody):
                walk(child, guarded)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                sites.append(
                    ImportSite(alias.name.split(".")[0], path, node.lineno, guarded)
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                sites.append(
                    ImportSite(node.module.split(".")[0], path, node.lineno, guarded)
                )
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    for child in ast.iter_child_nodes(tree):
        walk(child, False)
    return sites


def scan_tree(root: Path, scan_dirs: Iterable[str] = SCAN_DIRS) -> list[ImportSite]:
    """Scan every `.py` file under `root/<dir>` for `dir` in `scan_dirs`."""
    sites: list[ImportSite] = []
    for directory in scan_dirs:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
                continue
            try:
                sites.extend(scan_source(text, str(path.relative_to(root))))
            except SyntaxError:  # pragma: no cover - a deliberately broken fixture
                continue
    return sites


def declared_names(pyproject_text: str) -> set[str]:
    """Canonical names of every requirement declared anywhere in pyproject.

    `[project.dependencies]` plus EVERY extra, not just `dev`: a package
    declared in `vector` or `maya-eval` is declared, not undeclared -- the
    question this check asks is "does pyproject know about it at all".
    """
    data = tomllib.loads(pyproject_text)
    project = data.get("project", {})
    names: set[str] = set()
    for spec in project.get("dependencies", []):
        names.add(canonicalize_name(Requirement(spec).name))
    for specs in project.get("optional-dependencies", {}).values():
        for spec in specs:
            names.add(canonicalize_name(Requirement(spec).name))
    return names


def first_party_names(root: Path, scan_dirs: Iterable[str] = SCAN_DIRS) -> set[str]:
    """Top-level names that resolve INSIDE the scanned trees.

    pytest puts a test file's own directory on `sys.path` when the directory has
    no `__init__.py`, which is how `tests/conftest.py` can do `from
    offline_embeddings import ...`. Mirroring that here is what keeps sibling
    test-module imports from reading as undeclared third-party packages. Every
    directory that contributes a scanned file contributes its own module and
    package names.
    """
    names: set[str] = {"athenaeum"}
    for directory in scan_dirs:
        base = root / directory
        if not base.is_dir():
            continue
        names.add(base.name)
        for path in base.rglob("*.py"):
            names.add(path.stem)
            parent = path.parent
            for entry in parent.iterdir():
                if entry.is_dir():
                    names.add(entry.name)
    return names


def violations(
    sites: Sequence[ImportSite],
    declared: set[str],
    providers: Mapping[str, Sequence[str]],
    first_party: set[str],
    stdlib: set[str] | None = None,
) -> list[Violation]:
    """The undeclared, unguarded, third-party imports among `sites`."""
    stdlib = sys.stdlib_module_names if stdlib is None else stdlib  # type: ignore[assignment]
    by_name: dict[str, list[ImportSite]] = {}
    for site in sites:
        name = site.name
        if name in stdlib or name in first_party or name.startswith("_"):
            continue
        if canonicalize_name(name) in declared:
            continue
        provided_by = tuple(providers.get(name, ()))
        if provided_by and any(canonicalize_name(d) in declared for d in provided_by):
            continue
        # Reaching here with no provider at all means pyproject does not declare
        # the name under any spelling either (declared-but-not-installed was
        # already accepted by the `canonicalize_name` check above).
        by_name.setdefault(name, []).append(site)

    found: list[Violation] = []
    for name, name_sites in sorted(by_name.items()):
        unguarded = tuple(s for s in name_sites if not s.guarded)
        if not unguarded:
            continue
        found.append(
            Violation(name, unguarded, tuple(sorted(set(providers.get(name, ())))))
        )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    sites = scan_tree(root)
    if not sites:
        print(
            f"check_declared_imports: scanned {root} and found NO imports at all -- "
            "refusing to report success on an empty scan.",
            file=sys.stderr,
        )
        return 2

    declared = declared_names((root / "pyproject.toml").read_text(encoding="utf-8"))
    found = violations(
        sites, declared, packages_distributions(), first_party_names(root)
    )
    distinct = {s.name for s in sites}

    if found:
        print("check_declared_imports: UNDECLARED third-party imports\n", file=sys.stderr)
        for violation in found:
            print(f"  FAIL {violation.describe()}", file=sys.stderr)
        print(
            "\nDeclare each in pyproject.toml with a bounded range (see the"
            " top-of-file comment on `[project].dependencies`), or guard the"
            " import with `try: ... except ImportError` if the feature is"
            " genuinely optional.",
            file=sys.stderr,
        )
        return 1

    print(
        f"check_declared_imports: OK -- {len(distinct)} distinct top-level imports "
        f"across {len(sites)} import sites in {'/, '.join(SCAN_DIRS)}/; "
        f"{len(declared)} requirements declared in pyproject.toml."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
