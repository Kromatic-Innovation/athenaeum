# SPDX-License-Identifier: Apache-2.0
"""Snapshot pin on the ``_RESOLVE_SYSTEM`` prompt string (issue #169, Lane 3).

The resolver prompt is load-bearing — the action taxonomy, source-precedence
rules, and JSON output contract all live in one string. A casual edit can
silently change classification behavior on every downstream caller.

This test pins the exact prompt text to a canonical snapshot stored under
``tests/data/resolve_system.txt``. Any intentional prompt edit MUST update
the snapshot file in the same commit; the assertion below will fail on
drift, forcing the change into review.

The snapshot lives in a sibling fixture file (not inlined) only because
the prompt is ~6 KB. This is a plain ``Path.read_text()`` — no snapshot-
testing library is used.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.resolutions import _RESOLVE_SYSTEM

_SNAPSHOT_PATH = Path(__file__).parent / "data" / "resolve_system.txt"


def test_resolve_system_matches_snapshot() -> None:
    expected = _SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert _RESOLVE_SYSTEM == expected, (
        "The _RESOLVE_SYSTEM prompt drifted from the pinned snapshot at "
        f"{_SNAPSHOT_PATH}. If the change is intentional, update the "
        "snapshot file in the same commit so reviewers see the diff."
    )
