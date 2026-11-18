# SPDX-License-Identifier: Apache-2.0
"""ruff's ``target-version`` matches the ``requires-python`` floor (athenaeum#2728).

A lint target older than the supported Python floor lets syntax and idioms
through that the floor makes unnecessary; a newer one would flag code the
floor still has to run.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_FLOOR_RE = re.compile(r">=\s*(\d+)\.(\d+)")
_TARGET_RE = re.compile(r"py(\d)(\d+)")


def target_mismatch(pyproject: dict) -> str | None:
    """A description of the mismatch, or ``None`` when the two agree."""
    requires = pyproject["project"]["requires-python"]
    target = pyproject["tool"]["ruff"]["target-version"]
    floor = _FLOOR_RE.search(requires)
    parsed = _TARGET_RE.fullmatch(target)
    assert floor, f"requires-python {requires!r} has no >=X.Y floor"
    assert parsed, f"ruff target-version {target!r} is not of the form pyXY"
    if floor.groups() != parsed.groups():
        return f"ruff target-version {target!r} differs from requires-python {requires!r}"
    return None


def test_ruff_target_matches_requires_python_floor() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert target_mismatch(pyproject) is None


def test_fails_on_a_fixture_where_they_differ() -> None:
    fixture = {
        "project": {"requires-python": ">=3.13"},
        "tool": {"ruff": {"target-version": "py311"}},
    }
    assert target_mismatch(fixture) is not None


def test_passes_on_a_fixture_where_they_agree() -> None:
    fixture = {
        "project": {"requires-python": ">=3.13"},
        "tool": {"ruff": {"target-version": "py313"}},
    }
    assert target_mismatch(fixture) is None
