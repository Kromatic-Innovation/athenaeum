# SPDX-License-Identifier: Apache-2.0
"""CI gate + unit tests for the exact-pin parity check (issue athenaeum#2324).

`test_gate_passes_on_the_current_tree` is the enforcement gate: it fails the
suite if this repo's own `pyproject.toml` and `requirements-ci.lock` ever
duplicate an exact `==` pin again. The rest prove the checker itself works --
a deliberate-mismatch counter-example (`mismatch/`) names the right package
and both versions, and a positive control (`clean/`) proves that failure is
about the fixture's defect, not about the checker always failing.

Mirrors `tests/test_declared_imports.py`'s module-loading pattern (load the
script by file path so the real CI entry point and the test import the exact
same code, not a copy) and `tests/test_ci_lock_parity.py`'s fixture-pair
layout.

Note this is NOT where the check actually runs in CI: `scripts/
check_exact_pin_parity.py` is invoked directly by `.github/workflows/ci.yml`'s
`pin-parity` job, BEFORE any `pip install`, so it catches a mismatch before
the expensive install/test work this file's own suite needs to even collect.
This file gives the checker's logic real pytest coverage; it does not
relocate where the gate runs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_exact_pin_parity.py"

_spec = importlib.util.spec_from_file_location("check_exact_pin_parity", _SCRIPT)
assert _spec and _spec.loader
check_exact_pin_parity = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_exact_pin_parity
_spec.loader.exec_module(check_exact_pin_parity)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "exact_pin_parity"


def _read_fixture_pair(name: str) -> tuple[str, str]:
    base = FIXTURES / name
    pyproject_text = (base / "pyproject.toml").read_text()
    lock_text = (base / "requirements-ci.lock").read_text()
    return pyproject_text, lock_text


def test_gate_passes_on_the_current_tree() -> None:
    # The gate. A re-introduced duplicate exact pin must make this red.
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text()
    lock_text = (REPO_ROOT / "requirements-ci.lock").read_text()
    violations = check_exact_pin_parity.check_parity(pyproject_text, lock_text)
    assert violations == [], "\n".join(violations)


def test_fixture_duplicate_mismatch_is_reported_by_name() -> None:
    # The counter-example AC3 asks for: a deliberate pyproject.toml/lock
    # mismatch (ruff 0.17.3 vs a stale 0.16.9 duplicate), reported by name
    # with both versions quoted -- the exact athenaeum#1922/#2284 shape.
    pyproject_text, lock_text = _read_fixture_pair("mismatch")
    violations = check_exact_pin_parity.check_parity(pyproject_text, lock_text)
    assert len(violations) == 1, violations
    assert violations[0].startswith("ruff:")
    assert "ruff==0.17.3" in violations[0]
    assert "ruff==0.16.9" in violations[0]


def test_fixture_no_duplicate_passes() -> None:
    # Positive control: proves the failure above is about the deliberate
    # duplicate, not about the checker always failing. Same ruff==0.17.3
    # exact pin, but requirements-ci.lock carries no copy of it at all.
    pyproject_text, lock_text = _read_fixture_pair("clean")
    assert check_exact_pin_parity.check_parity(pyproject_text, lock_text) == []


def test_range_pins_are_not_flagged() -> None:
    # A range-pinned dependency (`pyyaml>=6.0,<7`) frozen into the lock at a
    # concrete version is the lock's whole POINT (pinning the transitive
    # closure) -- not a violation of anything. Only `==` pins in
    # pyproject.toml are in scope.
    pyproject_text = """
[project]
name = "demo"
dependencies = ["pyyaml>=6.0,<7"]
"""
    lock_text = "pyyaml==6.0.1\n"
    assert check_exact_pin_parity.check_parity(pyproject_text, lock_text) == []


def test_canonicalize_matches_pep503_normalization() -> None:
    # Name matching must be case/separator-insensitive (the lock freezes
    # `PyYAML`, pyproject.toml often writes `pyyaml`) or a real duplicate
    # could hide behind a casing difference.
    assert check_exact_pin_parity.canonicalize("PyYAML") == "pyyaml"
    assert check_exact_pin_parity.canonicalize("foo_bar.baz") == "foo-bar-baz"


def test_main_exits_nonzero_and_names_the_package_on_a_mismatch(
    monkeypatch, tmp_path, capsys
) -> None:
    # End-to-end through main(), the same entry point CI's `pin-parity` job
    # calls -- not just the pure check_parity() helper above.
    pyproject_text, lock_text = _read_fixture_pair("mismatch")
    fake_pyproject = tmp_path / "pyproject.toml"
    fake_lock = tmp_path / "requirements-ci.lock"
    fake_pyproject.write_text(pyproject_text)
    fake_lock.write_text(lock_text)
    monkeypatch.setattr(check_exact_pin_parity, "PYPROJECT", fake_pyproject)
    monkeypatch.setattr(check_exact_pin_parity, "LOCKFILE", fake_lock)

    exit_code = check_exact_pin_parity.main([])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "ruff" in out
    assert "0.17.3" in out
    assert "0.16.9" in out
