# SPDX-License-Identifier: Apache-2.0
"""``athenaeum audit --stale`` — the stale-page review report CLI surface
(issue athenaeum#1630).

Covers: the flag is wired into the existing ``audit`` subparser (not a
second top-level command); ``--stale`` renders a table by default and JSON
with ``--json``; ``--limit`` caps the rows; read-only (no LLM client, no
lock). All fixtures — no test reads or writes a live knowledge store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum import _cmd_audit
from athenaeum.cli import build_parser


def _audit_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return subparsers_action.choices["audit"]


def _page(root: Path, name: str, uid: str, *, extra: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\nuid: {uid}\ntype: concept\nname: {uid}\n{extra}---\nBody.\n"
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


def test_stale_flag_registered_on_audit_subparser() -> None:
    parser = _audit_parser()
    assert parser.get_default("func") is _cmd_audit.cmd_audit
    args = parser.parse_args(["--stale"])
    assert args.stale is True


def test_stale_defaults_to_false() -> None:
    parser = _audit_parser()
    args = parser.parse_args([])
    assert args.stale is False


def test_stale_table_output(knowledge_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _page(knowledge_root / "wiki", "never.md", "never1")
    parser = _audit_parser()
    args = parser.parse_args(["--stale", "--path", str(knowledge_root)])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "never1" in out
    assert "reason=never-audited" in out


def test_stale_json_output(knowledge_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _page(knowledge_root / "wiki", "never.md", "never1")
    parser = _audit_parser()
    args = parser.parse_args(["--stale", "--json", "--path", str(knowledge_root)])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["pages"][0]["uid"] == "never1"
    assert "stale_after_days" in payload


def test_stale_limit_caps_rows(knowledge_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for i in range(5):
        _page(knowledge_root / "wiki", f"p{i}.md", f"uid{i}")
    parser = _audit_parser()
    args = parser.parse_args(["--stale", "--json", "--limit", "2", "--path", str(knowledge_root)])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2


def test_stale_never_builds_llm_client(
    knowledge_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page(knowledge_root / "wiki", "never.md", "never1")

    def _boom(*args, **kwargs):
        raise AssertionError("--stale must never build an LLM client")

    monkeypatch.setattr("athenaeum.provider.build_llm_client", _boom)
    parser = _audit_parser()
    args = parser.parse_args(["--stale", "--path", str(knowledge_root)])
    rc = args.func(args)
    assert rc == 0
