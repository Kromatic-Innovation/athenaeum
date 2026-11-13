# SPDX-License-Identifier: Apache-2.0
"""``athenaeum recompile candidates`` (issue athenaeum#2534 AC8).

Driven through ``cli.main`` so the real argparse wiring and lazy-loader
dispatch are exercised. Domain logic is covered in
``tests/test_page_recompile.py``; this module covers the CLI seam only --
in particular, the privacy-shaped constraint that stdout carries counts
and never a page name/uid/filename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import render_frontmatter


def _write(wiki: Path, uid: str, name: str, body: str, **meta_extra: object) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"uid": uid, "type": "reference", "name": name}
    meta.update(meta_extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    return path


class TestRecompileCandidatesAC8:
    def test_prints_counts_only_no_page_names(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        secret_name = "Top Secret Project Alpha"
        _write(wiki, "aaaa1111", secret_name, "x" * 50, needs_split=True)
        # A flagged page with no uid -- lands in the blocked bucket, and its
        # name must not leak either.
        (wiki / "nouidpage.md").write_text(
            "---\nneeds_split: true\nname: Hidden Blocked Page\n---\n\nShort body.\n",
            encoding="utf-8",
        )

        rc = main(["recompile", "candidates", "--path", str(knowledge)])

        out = capsys.readouterr().out
        assert rc == 0
        assert secret_name not in out
        assert "aaaa1111" not in out
        assert "Hidden Blocked Page" not in out
        assert "nouidpage" not in out
        assert "candidates:" in out
        assert "blocked:" in out
        assert "no_uid:" in out

    def test_missing_wiki_root_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["recompile", "candidates", "--path", str(tmp_path / "nope")])

        assert rc == 1
        assert "Wiki root not found" in capsys.readouterr().err

    def test_bare_recompile_prints_usage_and_exits_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["recompile"])

        assert rc == 2
        assert "recompile" in capsys.readouterr().err
