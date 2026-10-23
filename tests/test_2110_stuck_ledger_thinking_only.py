"""Issue athenaeum#2110: stuck-ledger entries from the thinking-only bug retry fresh."""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.stuck_ledger import STUCK_MANIFEST_NAME, load_stuck_ledger

_DETAIL = (
    "response_text: no text block found among 1 content block(s); "
    "block types were ['thinking']"
)


def _write(root: Path, files: dict[str, dict]) -> None:
    (root / STUCK_MANIFEST_NAME).write_text(
        json.dumps({"files": files}), encoding="utf-8"
    )


def test_thinking_only_value_error_entry_is_dropped_at_load(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "s/a.md": {
                "failures": 2,
                "last_error": "ValueError",
                "last_error_detail": _DETAIL,
            },
            "s/b.md": {
                "failures": 2,
                "last_error": "ValueError",
                "last_error_detail": "some other value problem",
            },
            "s/c.md": {"failures": 1, "last_error": "ValueError"},
        },
    )
    assert set(load_stuck_ledger(tmp_path)) == {"s/b.md", "s/c.md"}
