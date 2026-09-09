# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1430 AC5 — the ``dedup-oversize-escalations`` CLI subcommand.

``cmd_dedup_oversize_escalations`` is the CLI wrapper around
:func:`athenaeum.tiers.collapse_oversize_escalation_duplicates` (tested
directly, with the interesting dedup-logic cases, in
``tests/test_page_size_gate.py::TestCollapseOversizeEscalationDuplicates``).
Covered here: the CLI plumbing itself — missing knowledge dir, missing
pending file, and a real end-to-end collapse through the registered
``athenaeum`` subcommand dispatch — using ``argparse.Namespace`` calls
directly (mirrors ``tests/test_cmd_pending_provider.py``), no subprocess, no
LLM (this command never builds one).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum.cli import _SUBCOMMAND_LOADERS, build_parser


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(path=tmp_path)


class TestCliRegistration:
    def test_registered_in_subcommand_loaders(self) -> None:
        assert _SUBCOMMAND_LOADERS["dedup-oversize-escalations"] == (
            "athenaeum._cmd_pending",
            "add_pending_subparsers",
        )

    def test_parses_via_build_parser(self, tmp_path: Path) -> None:
        parser = build_parser()
        ns = parser.parse_args(["dedup-oversize-escalations", "--path", str(tmp_path)])
        assert ns.command == "dedup-oversize-escalations"
        assert ns.path == tmp_path
        assert ns.func.__name__ == "cmd_dedup_oversize_escalations"


class TestCmdDedupOversizeEscalations:
    def test_missing_knowledge_dir_returns_1(self, tmp_path: Path) -> None:
        from athenaeum._cmd_pending import cmd_dedup_oversize_escalations

        missing = tmp_path / "does-not-exist"
        rc = cmd_dedup_oversize_escalations(_args(missing))
        assert rc == 1

    def test_missing_pending_file_is_a_clean_noop(self, tmp_path: Path) -> None:
        from athenaeum._cmd_pending import cmd_dedup_oversize_escalations

        (tmp_path / "wiki").mkdir()
        rc = cmd_dedup_oversize_escalations(_args(tmp_path))
        assert rc == 0

    def test_end_to_end_collapses_duplicates_and_archives(
        self, tmp_path: Path, capsys
    ) -> None:
        from athenaeum._cmd_pending import cmd_dedup_oversize_escalations

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        # Pre-fix-shaped corpus: 3 duplicate oversize_page blocks for the
        # same entity, simulating what repeated suppressed merges wrote
        # before this issue's fix existed.
        blocks = [
            f'## [2026-09-01] Entity: "Kromatic" (from sessions/{i}.md)\n'
            f"- [ ] question {i}?\n\n"
            f"**Conflict type**: oversize_page\n"
            f"**Description**: desc {i}\n"
            for i in range(3)
        ]
        pending.write_text("# Pending Questions\n\n" + "\n\n---\n\n".join(blocks) + "\n")

        rc = cmd_dedup_oversize_escalations(_args(tmp_path))

        assert rc == 0
        out = capsys.readouterr().out
        assert "Archived 2 duplicate" in out
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 1
        archive = (wiki / "_pending_questions_archive.md").read_text()
        assert archive.count("**Conflict type**: oversize_page") == 2

    def test_is_idempotent(self, tmp_path: Path, capsys) -> None:
        from athenaeum._cmd_pending import cmd_dedup_oversize_escalations

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        blocks = [
            f'## [2026-09-01] Entity: "Kromatic" (from sessions/{i}.md)\n'
            f"- [ ] question {i}?\n\n"
            f"**Conflict type**: oversize_page\n"
            f"**Description**: desc {i}\n"
            for i in range(2)
        ]
        pending.write_text("# Pending Questions\n\n" + "\n\n---\n\n".join(blocks) + "\n")

        cmd_dedup_oversize_escalations(_args(tmp_path))
        rc = cmd_dedup_oversize_escalations(_args(tmp_path))

        assert rc == 0
        out = capsys.readouterr().out
        assert "Archived 0 duplicate" in out
