"""Tests for `athenaeum questions {list,next,count}` (issue #128).

Covers the three modes against a synthetic `_pending_questions.md`:

- `count`: text + JSON
- `list`: respects `--limit`, includes proposal block when `--with-proposal`,
  excludes resolved (`[x]`) entries
- `next`: returns oldest unresolved; emits `null` JSON on empty

All tests build the file by hand (the parser fixtures live in
`test_answers.py`); we re-use only `parse_pending_questions` indirectly
through the CLI surface.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main


def _block(
    *,
    date: str,
    entity: str,
    question: str,
    checkbox: str = "[ ]",
    conflict_type: str = "principled",
    description: str = "synthetic conflict",
    proposal: str | None = None,
) -> str:
    block = (
        f'## [{date}] Entity: "{entity}" (from sessions/{date}-x.md)\n'
        f"- {checkbox} {question}\n"
        f"**Conflict type**: {conflict_type}\n"
        f"**Description**: {description}\n"
    )
    if proposal:
        block += proposal.rstrip() + "\n"
    return block


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    pending = wiki / "_pending_questions.md"
    pending.write_text(
        "# Pending Questions\n\n"
        + _block(
            date="2026-04-10",
            entity="Acme Corp",
            question="Is Acme still Series A?",
            proposal=(
                "**Proposed resolution**: keep_a\n"
                "**Confidence**: 0.92\n"
                "**Rationale**: user direct statement overrides legacy import.\n"
                "**Source precedence**: a:user > b:unsourced"
            ),
        )
        + "\n---\n\n"
        + _block(
            date="2026-04-20",
            entity="Beta Inc",
            question="Is Beta HQ in Boston?",
        )
        + "\n---\n\n"
        + _block(
            date="2026-04-25",
            entity="Resolved Co",
            question="Already answered question",
            checkbox="[x]",
        )
        + "\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


def test_count_text(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "count", "--path", str(knowledge_root)])
    assert rc == 0
    assert "2 unresolved" in out
    assert "2026-04-10" in out  # oldest


def test_count_json(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "count", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload == {"count": 2, "oldest": "2026-04-10"}


def test_count_empty_knowledge(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["questions", "count", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) == {"count": 0, "oldest": None}


# ---------------------------------------------------------------------------
# next
# ---------------------------------------------------------------------------


def test_next_returns_oldest(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "next", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["entity"] == "Acme Corp"
    assert payload["created_at"] == "2026-04-10"
    # No --with-proposal => no proposal field.
    assert "proposal" not in payload


def test_next_with_proposal(knowledge_root: Path) -> None:
    rc, out = _run(
        [
            "questions",
            "next",
            "--path",
            str(knowledge_root),
            "--with-proposal",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["entity"] == "Acme Corp"
    assert "**Proposed resolution**: keep_a" in payload["proposal"]
    assert "**Confidence**: 0.92" in payload["proposal"]


def test_next_empty_returns_null(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["questions", "next", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) is None


def test_next_text_format(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "next", "--path", str(knowledge_root)])
    assert rc == 0
    assert "Acme Corp" in out
    assert "Is Acme still Series A?" in out


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_excludes_resolved(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "list", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 2
    entities = {item["entity"] for item in payload}
    assert entities == {"Acme Corp", "Beta Inc"}
    # Resolved Co has [x] — must not appear.
    assert "Resolved Co" not in entities


def test_list_with_limit(knowledge_root: Path) -> None:
    rc, out = _run(
        [
            "questions",
            "list",
            "--path",
            str(knowledge_root),
            "--limit",
            "1",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["entity"] == "Acme Corp"  # oldest first


def test_list_with_proposal_includes_proposal_block(knowledge_root: Path) -> None:
    rc, out = _run(
        [
            "questions",
            "list",
            "--path",
            str(knowledge_root),
            "--with-proposal",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    by_entity = {item["entity"]: item for item in payload}
    assert "**Proposed resolution**: keep_a" in by_entity["Acme Corp"]["proposal"]
    # Beta has no resolver block — proposal field should be empty string.
    assert by_entity["Beta Inc"]["proposal"] == ""


def test_list_text_format_lists_both(knowledge_root: Path) -> None:
    rc, out = _run(["questions", "list", "--path", str(knowledge_root)])
    assert rc == 0
    assert "Acme Corp" in out
    assert "Beta Inc" in out
    assert "Resolved Co" not in out
