# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum provenance adapter`` (issue athenaeum#2468).

The adapter-provenance ledger (:mod:`athenaeum.adapter_provenance`) is the
ONE ledger of the five this issue reviewed that is kept and, by this
command, exposed for the first time — the other four (compile routes, LLM
schema observations, the wiki-dedupe attribution report, never-ingest
refusals) had their writers and readers removed entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum._cmd_provenance import cmd_provenance
from athenaeum.adapter_provenance import record_adapter_provenance_for_pages
from athenaeum.cli import build_parser

_RAW_CONTENT = (
    "---\n"
    "mural_board_id: kromatic5164.1730753845696\n"
    "room: KIT Open Enrollment Workspaces\n"
    "workspace: Kromatic\n"
    "archive_path: /tmp/does-not-matter.json\n"
    "---\n\nBoard summary body.\n"
)


class TestParserWiring:
    def test_provenance_adapter_subcommand_is_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["provenance", "adapter", "--uid", "uid-x"])
        assert args.func is cmd_provenance
        assert args.provenance_target == "adapter"
        assert args.uid == "uid-x"


class TestAdapterProvenanceLookup:
    def test_page_with_provenance_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        record_adapter_provenance_for_pages(
            "mural-board-summary",
            "raw/mural-board-summary/board.md",
            _RAW_CONTENT,
            ["uid-with-provenance"],
            cache_dir=cache_dir,
        )

        parser = build_parser()
        args = parser.parse_args(
            [
                "provenance",
                "adapter",
                "--uid",
                "uid-with-provenance",
                "--cache-dir",
                str(cache_dir),
            ]
        )
        rc = args.func(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "mural-board-summary" in out
        assert "kromatic5164.1730753845696" in out
        assert "raw/mural-board-summary/board.md" in out
        assert "room: KIT Open Enrollment Workspaces" in out

    def test_page_without_provenance_says_so_and_still_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        # Seed the ledger with a DIFFERENT page's row, so the ledger file
        # exists but has nothing for the uid this call asks about.
        record_adapter_provenance_for_pages(
            "mural-board-summary",
            "raw/mural-board-summary/other.md",
            _RAW_CONTENT,
            ["uid-some-other-page"],
            cache_dir=cache_dir,
        )

        parser = build_parser()
        args = parser.parse_args(
            [
                "provenance",
                "adapter",
                "--uid",
                "uid-without-provenance",
                "--cache-dir",
                str(cache_dir),
            ]
        )
        rc = args.func(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No adapter provenance recorded" in out
        assert "uid-without-provenance" in out

    def test_json_output_round_trips_the_record(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        record_adapter_provenance_for_pages(
            "mural-board-summary",
            "raw/mural-board-summary/board.md",
            _RAW_CONTENT,
            ["uid-with-provenance"],
            cache_dir=cache_dir,
        )

        parser = build_parser()
        args = parser.parse_args(
            [
                "provenance",
                "adapter",
                "--uid",
                "uid-with-provenance",
                "--cache-dir",
                str(cache_dir),
                "--json",
            ]
        )
        rc = args.func(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["uid"] == "uid-with-provenance"
        assert len(payload["records"]) == 1
        record = payload["records"][0]
        assert record["source"] == "mural-board-summary"
        assert record["source_object_id"] == "kromatic5164.1730753845696"
        assert record["page_uid"] == "uid-with-provenance"
        assert record["fields"]["room"] == "KIT Open Enrollment Workspaces"

    def test_json_output_for_a_missing_uid_is_an_empty_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache_dir = tmp_path / "cache"
        parser = build_parser()
        args = parser.parse_args(
            [
                "provenance",
                "adapter",
                "--uid",
                "uid-never-seen",
                "--cache-dir",
                str(cache_dir),
                "--json",
            ]
        )
        rc = args.func(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"uid": "uid-never-seen", "records": []}

    def test_no_subcommand_is_a_usage_error(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["provenance"])
        rc = args.func(args)
        assert rc == 2
