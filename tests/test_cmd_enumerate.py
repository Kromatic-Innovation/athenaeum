# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum enumerate`` (issue athenaeum#965) — the CLI surface
over :func:`athenaeum.enumeration.enumerate_entities`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum._cmd_enumerate import _parse_where, cmd_enumerate
from athenaeum.cli import build_parser
from athenaeum.enumeration import FieldPredicate


def _write_wiki(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alice.md").write_text(
        "---\nuid: u-alice\ntype: person\nname: Alice Example\n"
        "current_company: Acme Corp\n---\n\nBody.\n"
    )
    (wiki / "bob.md").write_text(
        "---\nuid: u-bob\ntype: person\nname: Bob Example\n"
        "current_company: Other Co\n---\n\nBody.\n"
    )
    return knowledge


class TestParseWhere:
    def test_simple_eq(self) -> None:
        pred = _parse_where("current_title:eq:Manager")
        assert pred == FieldPredicate(fields=("current_title",), kind="eq", value="Manager")

    def test_fallback_fields(self) -> None:
        pred = _parse_where("a,b:substring:x")
        assert pred.fields == ("a", "b")

    def test_ne_negates_eq(self) -> None:
        pred = _parse_where("do_not_email:ne:true")
        assert pred.kind == "eq"
        assert pred.negate is True

    def test_value_may_contain_colons(self) -> None:
        pred = _parse_where("uid:regex:^u-[0-9]+:[a-z]+$")
        assert pred.value == "^u-[0-9]+:[a-z]+$"

    def test_missing_parts_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_where("current_title:eq")

    def test_bad_kind_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_where("current_title:startswith:foo")


class TestCmdEnumerate:
    def test_enumerate_prints_json_hits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_wiki(tmp_path)
        args = argparse.Namespace(
            entity_type="person",
            where=[],
            sort_key="name",
            ascending=False,
            limit=50,
            cursor=None,
            field=[],
            with_pii=False,
            audience=None,
            path=knowledge,
            cache_dir=tmp_path / "cache",
        )
        rc = cmd_enumerate(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert {h["uid"] for h in payload["hits"]} == {"u-alice", "u-bob"}
        assert payload["next_cursor"] is None
        assert payload["known_classes"] == []

    def test_enumerate_with_predicate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_wiki(tmp_path)
        args = argparse.Namespace(
            entity_type="person",
            where=[FieldPredicate(fields=("current_company",), kind="eq", value="Acme Corp")],
            sort_key="name",
            ascending=False,
            limit=50,
            cursor=None,
            field=[],
            with_pii=False,
            audience=None,
            path=knowledge,
            cache_dir=tmp_path / "cache",
        )
        rc = cmd_enumerate(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert [h["uid"] for h in payload["hits"]] == ["u-alice"]

    def test_unrecognized_type_names_known_classes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_wiki(tmp_path)
        args = argparse.Namespace(
            entity_type="not-a-class",
            where=[],
            sort_key="name",
            ascending=False,
            limit=50,
            cursor=None,
            field=[],
            with_pii=False,
            audience=None,
            path=knowledge,
            cache_dir=tmp_path / "cache",
        )
        rc = cmd_enumerate(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["hits"] == []
        assert "person" in payload["known_classes"]

    def test_pii_field_without_flag_errors_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_wiki(tmp_path)
        args = argparse.Namespace(
            entity_type="person",
            where=[],
            sort_key="name",
            ascending=False,
            limit=50,
            cursor=None,
            field=["do_not_email"],
            with_pii=False,
            audience=None,
            path=knowledge,
            cache_dir=tmp_path / "cache",
        )
        rc = cmd_enumerate(args)
        assert rc == 1
        assert "with_pii" in capsys.readouterr().err

    def test_missing_wiki_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = argparse.Namespace(
            entity_type="person",
            where=[],
            sort_key="name",
            ascending=False,
            limit=50,
            cursor=None,
            field=[],
            with_pii=False,
            audience=None,
            path=tmp_path / "no-such-knowledge",
            cache_dir=tmp_path / "cache",
        )
        rc = cmd_enumerate(args)
        assert rc == 1


class TestArgvWiring:
    def test_build_parser_registers_enumerate(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "enumerate",
                "--type",
                "person",
                "--where",
                "current_company:substring:Acme",
                "--sort",
                "warm_score",
                "--limit",
                "10",
            ]
        )
        assert args.entity_type == "person"
        assert args.where == [
            FieldPredicate(fields=("current_company",), kind="substring", value="Acme")
        ]
        assert args.sort_key == "warm_score"
        assert args.limit == 10
        assert args.func is cmd_enumerate

    def test_argv_end_to_end(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_wiki(tmp_path)
        parser = build_parser()
        args = parser.parse_args(
            [
                "enumerate",
                "--type",
                "person",
                "--path",
                str(knowledge),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        rc = args.func(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert {h["uid"] for h in payload["hits"]} == {"u-alice", "u-bob"}
