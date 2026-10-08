# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum convergence`` (issue athenaeum#2020 AC3)."""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum._cmd_convergence import cmd_convergence
from athenaeum.cli import build_parser


def _knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


class TestParserWiring:
    def test_convergence_subcommand_is_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["convergence", "--json"])
        assert args.func is cmd_convergence


class TestCommand:
    def test_json_output_has_a_reading(self, tmp_path: Path, capsys) -> None:
        knowledge_root = _knowledge_root(tmp_path)
        parser = build_parser()
        args = parser.parse_args(["convergence", "--path", str(knowledge_root), "--json"])
        rc = args.func(args)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["reading"] == "insufficient_data"

    def test_human_output_mentions_reading(self, tmp_path: Path, capsys) -> None:
        knowledge_root = _knowledge_root(tmp_path)
        parser = build_parser()
        args = parser.parse_args(["convergence", "--path", str(knowledge_root)])
        rc = args.func(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "convergence report: insufficient_data" in out
