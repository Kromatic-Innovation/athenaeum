# SPDX-License-Identifier: Apache-2.0
"""CI gate + unit tests for the declared-imports check (issue athenaeum#2136).

`test_gate_passes_on_the_current_tree` is the enforcement gate: it fails the
suite if `src/` or `tests/` imports a third-party package `pyproject.toml`
declares nowhere. The rest prove the check itself works -- that it catches the
exact `httpx` shape that motivated it, that it does NOT flag a guarded optional
import or a sibling test module, and that it refuses to report success on an
empty scan -- so a green gate is trustworthy rather than vacuous.

Mirrors `tests/test_env_docs.py`'s structure (gate + injected-gap proofs +
empty-scan refusal), deliberately: the two checks answer the same KIND of
question about the repo's own declarations.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_declared_imports.py"

_spec = importlib.util.spec_from_file_location("check_declared_imports", _SCRIPT)
assert _spec and _spec.loader
check_declared_imports = importlib.util.module_from_spec(_spec)
# Register before exec: the script defines `@dataclass`es, and dataclasses
# resolves a class's own module out of `sys.modules` while processing it.
sys.modules[_spec.name] = check_declared_imports
_spec.loader.exec_module(check_declared_imports)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_gate_passes_on_the_current_tree() -> None:
    # The gate. A new undeclared third-party import must make this red.
    assert check_declared_imports.main(["--root", str(REPO_ROOT)]) == 0


def test_gate_reports_a_denominator_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    # Evidence a sweep actually ran, rather than a silent zero-work pass.
    check_declared_imports.main(["--root", str(REPO_ROOT)])
    out = capsys.readouterr().out
    assert "OK" in out
    assert "distinct top-level imports" in out


def test_httpx_shape_is_a_violation() -> None:
    """The motivating case: a module-level import of an undeclared package."""
    sites = check_declared_imports.scan_source("import httpx\n", "tests/test_retry.py")
    found = check_declared_imports.violations(
        sites,
        declared={"anthropic"},
        providers={"httpx": ["httpx"]},
        first_party=set(),
        stdlib=set(),
    )
    assert [v.name for v in found] == ["httpx"]
    assert "pyproject.toml does not declare" in found[0].describe()
    assert "tests/test_retry.py:1" in found[0].describe()


def test_an_import_present_in_no_distribution_is_still_named() -> None:
    """The clean-`[dev]` case: the package is not installed AT ALL.

    This is what a contributor actually hits -- the collection ERROR -- and the
    check must name the import rather than depend on it being importable.
    """
    sites = check_declared_imports.scan_source("import httpx\n", "tests/test_retry.py")
    found = check_declared_imports.violations(
        sites, declared={"anthropic"}, providers={}, first_party=set(), stdlib=set()
    )
    assert [v.name for v in found] == ["httpx"]
    assert "not provided by any installed distribution" in found[0].describe()


def test_declared_anywhere_in_pyproject_is_not_a_violation() -> None:
    """A package declared in an EXTRA is declared -- `vector`'s chromadb, say."""
    declared = check_declared_imports.declared_names(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert {"chromadb", "numpy", "httpx", "packaging", "pyyaml"} <= declared
    sites = check_declared_imports.scan_source(
        "import chromadb\n", "src/athenaeum/search.py"
    )
    assert (
        check_declared_imports.violations(
            sites,
            declared=declared,
            providers={"chromadb": ["chromadb"]},
            first_party=set(),
            stdlib=set(),
        )
        == []
    )


def test_a_declared_name_satisfied_by_a_differently_named_distribution() -> None:
    """`fastmcp` is declared; the installed distribution is `fastmcp-slim`."""
    sites = check_declared_imports.scan_source(
        "import fastmcp\n", "src/athenaeum/mcp_server.py"
    )
    assert (
        check_declared_imports.violations(
            sites,
            declared={"fastmcp"},
            providers={"fastmcp": ["fastmcp-slim"]},
            first_party=set(),
            stdlib=set(),
        )
        == []
    )


def test_a_try_except_importerror_guard_is_not_a_violation() -> None:
    """A guarded optional import degrades instead of crashing -- `onnxruntime`."""
    source = "try:\n    import onnxruntime\nexcept ImportError:\n    onnxruntime = None\n"
    sites = check_declared_imports.scan_source(source, "src/athenaeum/search.py")
    assert [(s.name, s.guarded) for s in sites if s.name == "onnxruntime"] == [
        ("onnxruntime", True)
    ]
    assert (
        check_declared_imports.violations(
            sites, declared=set(), providers={}, first_party=set(), stdlib=set()
        )
        == []
    )


def test_an_except_star_guard_is_recognised() -> None:
    """`ast.TryStar` is a distinct node type, not an `ast.Try` subclass."""
    source = "try:\n    import httpx\nexcept* ImportError:\n    pass\n"
    sites = check_declared_imports.scan_source(source, "tests/test_x.py")
    assert [(s.name, s.guarded) for s in sites] == [("httpx", True)]


def test_a_guard_that_does_not_absorb_importerror_is_still_a_violation() -> None:
    source = "try:\n    import httpx\nexcept ValueError:\n    pass\n"
    sites = check_declared_imports.scan_source(source, "tests/test_x.py")
    found = check_declared_imports.violations(
        sites, declared=set(), providers={}, first_party=set(), stdlib=set()
    )
    assert [v.name for v in found] == ["httpx"]


def test_a_lazy_function_level_import_is_not_exempt() -> None:
    """athenaeum#542's ruling: deferring the crash does not declare the dep."""
    source = "def f():\n    import numpy as np\n    return np\n"
    sites = check_declared_imports.scan_source(source, "src/athenaeum/recurring_claims.py")
    found = check_declared_imports.violations(
        sites, declared=set(), providers={"numpy": ["numpy"]}, first_party=set(), stdlib=set()
    )
    assert [v.name for v in found] == ["numpy"]


def test_relative_imports_are_skipped() -> None:
    sites = check_declared_imports.scan_source("from . import sibling\n", "src/athenaeum/a.py")
    assert sites == []


def test_sibling_test_modules_are_first_party() -> None:
    """`tests/conftest.py` does `from offline_embeddings import ...`."""
    first_party = check_declared_imports.first_party_names(REPO_ROOT)
    assert {"athenaeum", "tests", "offline_embeddings", "conftest"} <= first_party


def test_scan_tree_finds_the_known_httpx_sites() -> None:
    sites = check_declared_imports.scan_tree(REPO_ROOT)
    httpx_paths = {s.path for s in sites if s.name == "httpx"}
    assert "tests/test_retry.py" in httpx_paths
    assert "tests/test_batch_mode.py" in httpx_paths


def test_empty_scan_refuses_to_report_success(tmp_path: Path) -> None:
    # A scan that found nothing is a broken scan, never a clean one.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert check_declared_imports.main(["--root", str(tmp_path)]) == 2
