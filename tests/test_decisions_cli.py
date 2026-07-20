"""Tests for `athenaeum decisions {list,next,count}` (issue #401).

The unified "human decisions needed" list: pending questions AND merges in
one queue, each tagged by type, oldest first.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main


def _write_questions(wiki: Path) -> None:
    (wiki / "_pending_questions.md").write_text(
        "# Pending Questions\n\n"
        '## [2026-07-01] Entity: "Acme Corp" (from sessions/x.md)\n'
        "- [ ] Is Acme still Series A?\n"
        "**Conflict type**: principled\n"
        "**Description**: two conflicting statements\n"
        "**Proposed resolution**: keep_a\n"
        "**Confidence**: 0.90\n"
        "\n---\n\n"
        '## [2026-07-10] Entity: "Done Co" (from sessions/y.md)\n'
        "- [x] Already answered?\n"
        "**Conflict type**: principled\n"
        "**Description**: resolved\n",
        encoding="utf-8",
    )


def _write_merges(wiki: Path) -> None:
    src = wiki / "aa11bb22-lean-startup.md"
    src.write_text(
        "---\nname: Lean Startup\ntype: concept\n---\n"
        "Build-measure-learn loop.\n",
        encoding="utf-8",
    )
    (wiki / "_pending_merges.md").write_text(
        "# Pending Merges\n\n"
        '## [2026-06-20] Merge: "startup"\n'
        f"- [ ] Approve this merge? Sources: {src.name}\n"
        "**Rationale**: overlap\n"
        "**Sources**:\n"
        f"- {src}\n"
        "**Confidence**: 0.84\n"
        "**Draft**:\n```markdown\nx\n```\n",
        encoding="utf-8",
    )


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_questions(wiki)
    _write_merges(wiki)
    return tmp_path


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_count_text(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "count", "--path", str(knowledge_root)])
    assert rc == 0
    # 1 unanswered question + 1 unresolved merge (resolved [x] excluded).
    assert "2 decisions pending (1 questions, 1 merges" in out
    assert "oldest" in out and "d)" in out


def test_count_json(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "count", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 2
    assert payload["questions"] == 1
    assert payload["merges"] == 1
    assert payload["oldest"] == "2026-06-20"  # the merge is older
    assert isinstance(payload["oldest_age_days"], int)


def test_count_empty(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["decisions", "count", "--path", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload == {
        "count": 0,
        "questions": 0,
        "merges": 0,
        "oldest": None,
        "oldest_age_days": None,
    }


def test_count_empty_text(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["decisions", "count", "--path", str(tmp_path)])
    assert rc == 0
    assert out.strip() == "0 decisions pending"


def test_list_unified_oldest_first(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "list", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert [d["type"] for d in payload] == ["merge", "question"]  # 06-20 before 07-01
    assert payload[0]["confidence"] == 0.84
    assert payload[1]["confidence"] is None
    # Every item has the common fields.
    for d in payload:
        assert set(d) >= {"type", "id", "created_at", "summary", "confidence", "payload"}


def test_list_excludes_resolved(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "list", "--path", str(knowledge_root), "--json"])
    payload = json.loads(out)
    summaries = " ".join(d["summary"] for d in payload)
    assert "Already answered?" not in summaries


def test_list_with_proposal(knowledge_root: Path) -> None:
    rc, out = _run(
        ["decisions", "list", "--path", str(knowledge_root), "--with-proposal", "--json"]
    )
    payload = json.loads(out)
    question = next(d for d in payload if d["type"] == "question")
    assert "**Proposed resolution**: keep_a" in question["payload"]["proposal"]


def test_list_limit(knowledge_root: Path) -> None:
    rc, out = _run(
        ["decisions", "list", "--path", str(knowledge_root), "--limit", "1", "--json"]
    )
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["type"] == "merge"  # oldest


def test_next_returns_oldest(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "next", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    decision = json.loads(out)
    assert decision["type"] == "merge"
    assert decision["created_at"] == "2026-06-20"


def test_next_empty_null(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["decisions", "next", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) is None


def test_next_text(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "next", "--path", str(knowledge_root)])
    assert rc == 0
    assert "merge" in out
    assert "Lean Startup" in out


def test_list_text(knowledge_root: Path) -> None:
    rc, out = _run(["decisions", "list", "--path", str(knowledge_root)])
    assert rc == 0
    assert "Lean Startup" in out
    assert "Is Acme still Series A?" in out


def test_bad_subcommand_usage() -> None:
    rc, _ = _run(["decisions"])
    assert rc == 2


def test_list_empty_text(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["decisions", "list", "--path", str(tmp_path)])
    assert rc == 0
    assert out.strip() == "0 decisions pending"


def test_list_text_with_proposal_renders_proposal(knowledge_root: Path) -> None:
    rc, out = _run(
        ["decisions", "list", "--path", str(knowledge_root), "--with-proposal"]
    )
    assert rc == 0
    assert "proposal:" in out
    assert "**Proposed resolution**: keep_a" in out


def test_questions_only(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_questions(wiki)
    rc, out = _run(["decisions", "count", "--path", str(tmp_path), "--json"])
    payload = json.loads(out)
    assert payload["questions"] == 1
    assert payload["merges"] == 0
