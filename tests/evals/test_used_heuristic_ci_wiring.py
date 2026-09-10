# SPDX-License-Identifier: Apache-2.0
"""Offline proof that issue athenaeum#1575's driver is never run by CI.

Mirrors ``tests/evals/test_containment_ci_wiring.py``: the measurement CLI is
a manual/local tool. It must not appear in ``ci.yml`` (nor become a required
check by appearing there at all) and must not be selected by ``evals.yml``'s
metered eval run.

UNMARKED — no network, no credential; reads the committed workflow files.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

DRIVER = "used_heuristic_cli"


def _workflow_text(name: str) -> str:
    path = WORKFLOWS / name
    assert path.is_file(), f"expected workflow file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_driver_is_not_invoked_by_ci_yml() -> None:
    assert DRIVER not in _workflow_text("ci.yml")


def test_driver_is_not_selected_by_evals_yml() -> None:
    assert DRIVER not in _workflow_text("evals.yml")
