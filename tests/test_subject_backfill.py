# SPDX-License-Identifier: Apache-2.0
"""``subject := uid`` derivation removal regression (athenaeum#1656).

The operator rejected stamping a page's own ``uid`` into its ``subject:``
field (parent issue's AC3 re-scope): two duplicate pages would each get a
different ``subject``, defeating the field's purpose as the shared
real-world thing multiple pages are about. `athenaeum.subject_backfill` and
`athenaeum._cmd_subject` — the derivation and its CLI presentation — are
deleted outright, so `athenaeum subject backfill` is no longer a command at
all. This module pins that: the command is gone, and even an `--apply`
invocation cannot write anything, because argparse rejects the unknown
`subject` token before any handler runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from athenaeum.cli import build_parser, main


def _page(root: Path, name: str, frontmatter: str, body: str = "Body text.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


class TestSubjectBackfillCommandRemoved:
    def test_apply_exits_nonzero_and_leaves_pages_byte_identical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        wiki.mkdir(parents=True)
        page = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        before = page.read_bytes()

        with pytest.raises(SystemExit) as excinfo:
            main(["subject", "backfill", "--path", str(wiki.parent), "--apply"])

        assert excinfo.value.code != 0
        assert page.read_bytes() == before

    def test_subject_is_not_a_known_top_level_command(self) -> None:
        parser = build_parser()
        top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        assert "subject" not in top.choices
