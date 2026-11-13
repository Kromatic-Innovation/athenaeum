# SPDX-License-Identifier: Apache-2.0
"""``athenaeum relate`` (issue athenaeum#2535 AC6).

Driven through ``cli.main`` so the argparse wiring and exit codes are
exercised on the real path an operator takes. Domain logic itself is
covered exhaustively in ``tests/test_page_relations.py``; this module only
covers the CLI seam: dispatch, the run lock, and exit codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import parse_frontmatter, render_frontmatter


def _write_page(wiki_root: Path, *, uid: str, name: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(
        render_frontmatter({"uid": uid, "type": "reference", "name": name}) + "\nBody.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    _write_page(wiki, uid="parent01", name="Acme Corp")
    _write_page(wiki, uid="child001", name="Client Counts")
    return root


class TestAttachAndDetach:
    def test_attach_sets_relation_with_via_manual(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "relate",
                "--child",
                "child001",
                "--parent",
                "parent01",
                "--path",
                str(knowledge),
            ]
        )
        assert rc == 0
        parent_meta, _ = parse_frontmatter(
            (knowledge / "wiki" / "parent01-acme-corp.md").read_text(encoding="utf-8")
        )
        assert parent_meta["related"] == [{"uid": "child001", "role": "child", "via": "manual"}]
        assert "now a child of" in capsys.readouterr().out

    def test_detach_removes_relation(self, knowledge: Path) -> None:
        rc = main(
            ["relate", "--child", "child001", "--parent", "parent01", "--path", str(knowledge)]
        )
        assert rc == 0

        rc = main(["relate", "--child", "child001", "--detach", "--path", str(knowledge)])
        assert rc == 0

        child_meta, _ = parse_frontmatter(
            (knowledge / "wiki" / "child001-client-counts.md").read_text(encoding="utf-8")
        )
        assert "parent" not in child_meta

    def test_refusal_exits_1_and_writes_nothing(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = (knowledge / "wiki" / "child001-client-counts.md").read_text(encoding="utf-8")

        rc = main(
            ["relate", "--child", "child001", "--parent", "no-such-uid", "--path", str(knowledge)]
        )

        assert rc == 1
        assert "error:" in capsys.readouterr().err
        assert (knowledge / "wiki" / "child001-client-counts.md").read_text(
            encoding="utf-8"
        ) == before

    def test_missing_wiki_root_exits_1(self, tmp_path: Path) -> None:
        empty_root = tmp_path / "nowhere"
        rc = main(
            ["relate", "--child", "x", "--parent", "y", "--path", str(empty_root)]
        )
        assert rc == 1


class TestRunLock:
    def test_exits_75_when_lock_is_held(self, knowledge: Path) -> None:
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = main(
                [
                    "relate",
                    "--child",
                    "child001",
                    "--parent",
                    "parent01",
                    "--path",
                    str(knowledge),
                ]
            )
        finally:
            lock.release()

        assert rc == EXIT_LOCK_HELD

    def test_lock_is_released_after_success(self, knowledge: Path) -> None:
        from athenaeum.runlock import RunLock

        rc = main(
            ["relate", "--child", "child001", "--parent", "parent01", "--path", str(knowledge)]
        )
        assert rc == 0

        # The lock must be free again -- a fresh non-blocking acquire succeeds.
        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        lock.release()
