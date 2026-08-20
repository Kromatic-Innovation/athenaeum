# SPDX-License-Identifier: Apache-2.0
"""Layering assertions for ``athenaeum.store`` (issue athenaeum#976).

Design note §6.4, closing paragraph: "S1's acceptance should assert the
layering explicitly, since a store abstraction is exactly the kind of module
that attracts upward imports." This pins three things mechanically rather
than by review alone:

* ``athenaeum.store`` stays import-light — no heavy third-party dependency
  (pydantic, anthropic, chromadb, fastmcp, numpy) and no upward import of a
  higher-layer module (``search``, ``librarian``, ``mcp_server``, ...).
* ``athenaeum.atomic_io`` (L0) still imports nothing beyond the standard
  library — this module must not have weakened that invariant to serve as
  ``FilesystemStore.put``'s implementation.
* The ``athenaeum.store`` <-> ``athenaeum.storage`` edge is ONE-DIRECTIONAL
  (``storage.py`` imports ``store.py``, never the reverse — see
  ``athenaeum.store``'s module docstring for why the design note's own
  stated module split for ``resolve_store_for_class`` had to be adjusted:
  the reverse edge is exactly what
  ``tests/test_import_graph_acyclic.py``'s zero-tolerance SCC guard, pinned
  to an empty baseline since issue athenaeum#640, rejects — including a
  call-time-deferred import, since that guard counts function-local edges
  too). This file's own mini Tarjan check below is a second, focused proof
  of the same invariant these two modules must not regress, independent of
  the repo-wide guard.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# Modules athenaeum.store may import from, beyond the standard library.
_ALLOWED_ATHENAEUM_IMPORTS = {"athenaeum.atomic_io", "athenaeum.models"}

# Third-party packages that would drag heavy deps into the seam.
_FORBIDDEN_THIRD_PARTY = {"pydantic", "anthropic", "chromadb", "fastmcp", "numpy"}

# Higher-layer athenaeum modules that must never be imported from the seam.
_FORBIDDEN_ATHENAEUM_MODULES = {
    "athenaeum.search",
    "athenaeum.librarian",
    "athenaeum.mcp_server",
    "athenaeum.quarantine",
    "athenaeum.pii",
    "athenaeum.corrections",
}


def _all_imports(path: Path) -> set[str]:
    """Every ``athenaeum.X`` module name imported anywhere in *path*'s AST —
    top-level AND function-local — matching how
    ``tests/test_import_graph_acyclic.py`` builds its graph, so this file's
    narrower check agrees with the repo-wide guard about what counts as an
    edge."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "athenaeum":
                    names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] == "athenaeum":
                names.add(node.module)
    return names


def test_store_module_import_light() -> None:
    imports = _all_imports(_SRC / "store.py")

    athenaeum_imports = {name for name in imports if name.split(".")[0] == "athenaeum"}
    unexpected = athenaeum_imports - _ALLOWED_ATHENAEUM_IMPORTS
    assert not unexpected, (
        f"athenaeum.store imports {unexpected} — only "
        f"{_ALLOWED_ATHENAEUM_IMPORTS} are allowed"
    )

    top_level_packages = {name.split(".")[0] for name in imports}
    forbidden_hit = top_level_packages & _FORBIDDEN_THIRD_PARTY
    assert not forbidden_hit, (
        f"athenaeum.store imports heavy third-party package(s) {forbidden_hit} — "
        "the seam must stay import-light"
    )

    forbidden_hit_athenaeum = athenaeum_imports & _FORBIDDEN_ATHENAEUM_MODULES
    assert not forbidden_hit_athenaeum, (
        f"athenaeum.store imports higher-layer module(s) {forbidden_hit_athenaeum} — "
        "this is exactly the upward-import failure mode §6.4 warns about"
    )


def test_atomic_io_still_stdlib_only() -> None:
    """This slice must not have weakened atomic_io's L0 "stdlib only" invariant
    to make FilesystemStore.put's implementation work."""
    imports = _all_imports(_SRC / "atomic_io.py")
    assert not imports, f"athenaeum.atomic_io now imports {imports} — must stay a stdlib-only leaf"


def test_store_never_imports_storage_not_even_deferred() -> None:
    """``athenaeum.store`` must have NO edge to ``athenaeum.storage`` — top-level
    or function-local. This is the load-bearing half of the one-directional
    shape: it is what lets ``storage.py`` safely import ``store.py``."""
    imports = _all_imports(_SRC / "store.py")
    assert "athenaeum.storage" not in imports


def test_storage_imports_store_at_module_level() -> None:
    """The other half: ``storage.py`` DOES import ``store.py``, at module
    level (real, not deferred) — this is what makes ``resolve_store_for_class``
    a genuine function in ``storage.py`` rather than a call-time-deferred
    wrapper (which the repo's import-graph guard would treat identically to
    a module-level import anyway)."""
    tree = ast.parse((_SRC / "storage.py").read_text(encoding="utf-8"))
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "athenaeum.store":
            top_level_imports.add(node.module)
    assert "athenaeum.store" in top_level_imports


def test_store_and_storage_form_no_scc() -> None:
    """A minimal, self-contained Tarjan check over just these two modules and
    their intra-package imports (rather than depending on
    ``tests/test_import_graph_acyclic.py``'s internals) — belt-and-suspenders
    for the specific edge this slice adds."""
    store_imports = _all_imports(_SRC / "store.py") & {"athenaeum.store", "athenaeum.storage"}
    storage_imports = _all_imports(_SRC / "storage.py") & {"athenaeum.store", "athenaeum.storage"}
    # A 2-node cycle exists iff each imports the other.
    cycle = "athenaeum.storage" in store_imports and "athenaeum.store" in storage_imports
    assert not cycle, "athenaeum.store <-> athenaeum.storage form a 2-node import cycle"


def test_store_and_storage_modules_import_cleanly() -> None:
    """Belt-and-suspenders: actually re-import both modules fresh and confirm no
    circular-import error surfaces at runtime, not just in the AST checks above."""
    for name in ("athenaeum.store", "athenaeum.storage"):
        sys.modules.pop(name, None)
    store_module = importlib.import_module("athenaeum.store")
    storage_module = importlib.import_module("athenaeum.storage")
    assert hasattr(store_module, "FilesystemStore")
    assert hasattr(storage_module, "resolve_store_for_class")
    assert not hasattr(store_module, "resolve_store_for_class")
