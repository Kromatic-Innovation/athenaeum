"""Tests for `athenaeum merges {list,next,count}` (issue #401).

The merges half of the unified decisions surface — previously there was NO
CLI for `wiki/_pending_merges.md` at all. Mirrors `test_questions_cli.py`:
build the sidecar by hand and exercise the three modes, plus the per-source
title + gist enrichment the issue's live-triage comment requires.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main


def _source_page(path: Path, *, name: str, description: str | None, body: str) -> None:
    fm = [f"name: {name}", "type: concept"]
    if description is not None:
        fm.append(f"description: {description}")
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + body + "\n", encoding="utf-8"
    )


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    src_a = wiki / "34f82884-mcp-public-auth.md"
    src_b = wiki / "9a01bb22-oauth-refresh.md"
    _source_page(
        src_a,
        name="MCP Public Auth Design",
        description=None,  # no description => gist falls back to first body line
        body="Design for public MCP auth using short-lived tokens.",
    )
    _source_page(
        src_b,
        name="OAuth 2.1 Refresh-Token Rotation",
        description="Refresh-token rotation under OAuth 2.1.",
        body="body ignored because description present",
    )
    (wiki / "_pending_merges.md").write_text(
        "# Pending Merges\n\n"
        '## [2026-06-20] Merge: "auth"\n'
        f"- [ ] Approve this merge? Sources: {src_a.name}, {src_b.name}\n"
        "**Rationale**: cosine 0.92 topic overlap\n"
        "**Sources**:\n"
        f"- {src_a}\n"
        f"- {src_b}\n"
        "**Confidence**: 0.92\n"
        "**Draft**:\n"
        "```markdown\n"
        "merged body\n"
        "```\n"
        "\n---\n\n"
        '## [2026-07-05] Merge: "resolved-target"\n'
        "- [x] Approve this merge? Sources: a.md\n"
        "**Rationale**: already handled\n"
        "**Sources**:\n"
        "- /k/a.md\n"
        "**Confidence**: 0.70\n"
        "**Draft**:\n"
        "```markdown\n"
        "x\n"
        "```\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_count_text(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "count", "--path", str(knowledge_root)])
    assert rc == 0
    assert "1 unresolved" in out
    assert "2026-06-20" in out  # oldest; resolved [x] block excluded


def test_count_json(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "count", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    assert json.loads(out) == {"count": 1, "oldest": "2026-06-20"}


def test_count_empty(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["merges", "count", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) == {"count": 0, "oldest": None}


def test_list_excludes_resolved_and_enriches_sources(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "list", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 1  # resolved-target [x] excluded
    merge = payload[0]
    assert merge["merge_target_name"] == "auth"
    assert merge["confidence"] == 0.92
    # Per-source human title + gist (NOT the uuid-slug) — the #401 contract.
    titles = [s["title"] for s in merge["sources"]]
    assert titles == ["MCP Public Auth Design", "OAuth 2.1 Refresh-Token Rotation"]
    gists = {s["title"]: s["gist"] for s in merge["sources"]}
    # description-less source falls back to first body line
    assert gists["MCP Public Auth Design"].startswith("Design for public MCP auth")
    # source with a description uses it
    assert gists["OAuth 2.1 Refresh-Token Rotation"] == "Refresh-token rotation under OAuth 2.1."
    # Phrased as an answerable question naming both titles.
    assert "MCP Public Auth Design" in merge["question"]
    assert "OAuth 2.1 Refresh-Token Rotation" in merge["question"]
    assert merge["question"].startswith("Merge these 2 pages")


def test_list_limit(knowledge_root: Path) -> None:
    rc, out = _run(
        ["merges", "list", "--path", str(knowledge_root), "--limit", "0", "--json"]
    )
    assert rc == 0
    assert len(json.loads(out)) == 1


def test_next_returns_oldest(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "next", "--path", str(knowledge_root), "--json"])
    assert rc == 0
    merge = json.loads(out)
    assert merge["created_at"] == "2026-06-20"
    assert merge["merge_target_name"] == "auth"


def test_next_empty_returns_null(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["merges", "next", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) is None


def test_next_text_format(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "next", "--path", str(knowledge_root)])
    assert rc == 0
    assert "Merge:" in out
    assert "MCP Public Auth Design" in out


def test_list_text_format(knowledge_root: Path) -> None:
    rc, out = _run(["merges", "list", "--path", str(knowledge_root)])
    assert rc == 0
    assert "auth" in out
    assert "resolved-target" not in out  # [x] excluded


def test_missing_source_degrades_gracefully(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_pending_merges.md").write_text(
        "# Pending Merges\n\n"
        '## [2026-06-01] Merge: "orphan"\n'
        "- [ ] Approve this merge? Sources: gone.md\n"
        "**Rationale**: r\n"
        "**Sources**:\n"
        "- /nonexistent/28e56467-some-slug.md\n"
        "**Confidence**: 0.5\n"
        "**Draft**:\n```markdown\nx\n```\n",
        encoding="utf-8",
    )
    rc, out = _run(["merges", "list", "--path", str(tmp_path), "--json"])
    assert rc == 0
    src = json.loads(out)[0]["sources"][0]
    # uuid prefix stripped for the fallback title; gist empty (file unreadable).
    assert src["title"] == "some-slug"
    assert src["gist"] == ""


def test_bad_subcommand_usage() -> None:
    rc, _ = _run(["merges"])
    assert rc == 2


def test_count_empty_text(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["merges", "count", "--path", str(tmp_path)])
    assert rc == 0
    assert out.strip() == "0 unresolved"


def test_list_empty_text(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["merges", "list", "--path", str(tmp_path)])
    assert rc == 0
    assert out.strip() == "0 unresolved"


def test_list_text_multiple_blocks_separated(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_pending_merges.md").write_text(
        "# Pending Merges\n\n"
        '## [2026-06-01] Merge: "one"\n'
        "- [ ] Approve? Sources: a.md\n**Rationale**: r1\n"
        "**Sources**:\n- /k/a.md\n**Confidence**: 0.5\n**Draft**:\n```markdown\nx\n```\n"
        "\n---\n\n"
        '## [2026-06-02] Merge: "two"\n'
        "- [ ] Approve? Sources: b.md\n**Rationale**: r2\n"
        "**Sources**:\n- /k/b.md\n**Confidence**: 0.6\n**Draft**:\n```markdown\ny\n```\n",
        encoding="utf-8",
    )
    rc, out = _run(["merges", "list", "--path", str(tmp_path)])
    assert rc == 0
    assert 'Merge: \'one\'' in out and 'Merge: \'two\'' in out
    assert "rationale: r1" in out
