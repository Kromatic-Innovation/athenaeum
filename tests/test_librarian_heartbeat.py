# SPDX-License-Identifier: Apache-2.0
"""Integration tests: dark-zone phases emit ``librarian-heartbeat`` lines (#398).

The T3 entity-merge pass (merge.py) and the post-compile phases (the #290
wiki-dedup pass and the #188 re-resolve pass) previously produced NO per-unit
progress logging, so a stall in any of them was invisible in the log. These
tests drive each phase directly (reusing the fixture/stub conventions from
``tests/test_librarian_merge.py`` and ``tests/test_wiki_dedupe.py``) and
assert the ``librarian-heartbeat`` start/done lines appear via ``caplog``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.merge import merge_clusters_to_wiki
from athenaeum.models import EscalationItem
from athenaeum.tiers import reresolve_open_questions, tier4_escalate
from athenaeum.wiki_dedupe import propose_wiki_page_merges


def _heartbeat_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [rec.message for rec in caplog.records if "librarian-heartbeat" in rec.message]


def _write_am_file(
    scope_dir: Path,
    filename: str,
    *,
    frontmatter_name: str,
    description: str,
    origin_session_id: str,
    origin_turn: int,
    sources: list[dict[str, object]],
    body: str,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {frontmatter_name}",
        "type: auto-memory",
        f"description: {description}",
        f"origin_session_id: {origin_session_id}",
        f"origin_turn: {origin_turn}",
        "sources:",
    ]
    for src in sources:
        lines.append(f"  - session: {src['session']}")
        lines.append(f"    turn: {src['turn']}")
        lines.append(f"    date: {src['date']}")
        lines.append(f"    excerpt: {src['excerpt']}")
    lines.append("---")
    lines.append(body)
    path = scope_dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_cluster_jsonl(knowledge_root: Path, rows: list[dict[str, object]]) -> Path:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return out


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n"
        "  extra_intake_roots:\n"
        "    - raw/auto-memory\n"
        "librarian:\n"
        "  heartbeat_interval: 0\n",
        encoding="utf-8",
    )


@pytest.fixture
def merge_root_two_clusters(tmp_path: Path) -> Path:
    """2 single-member clusters — enough for a real (non-empty) merge run."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristan-Code-proj"

    specs = [
        ("note_one.md", "s-aaa", 1, "First standalone note."),
        ("note_two.md", "s-bbb", 1, "Second standalone note."),
    ]
    for filename, session, turn, body in specs:
        _write_am_file(
            scope,
            filename,
            frontmatter_name=filename.replace("_", " ").replace(".md", ""),
            description="standalone note",
            origin_session_id=session,
            origin_turn=turn,
            sources=[
                {
                    "session": session,
                    "turn": turn,
                    "date": "2026-07-01",
                    "excerpt": body,
                }
            ],
            body=body,
        )

    rows = [
        {
            "cluster_id": f"proj-000{i + 1}",
            "member_paths": [f"-Users-tristan-Code-proj/{filename}"],
            "centroid_score": 1.0,
            "rationale": "singleton",
        }
        for i, (filename, _, _, _) in enumerate(specs)
    ]
    _write_cluster_jsonl(knowledge_root, rows)
    _write_config(knowledge_root)
    return knowledge_root


class TestMergeHeartbeats:
    def test_merge_write_and_merge_detect_emit_start_and_done(
        self, merge_root_two_clusters: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real (non-dry-run, client=None deterministic) merge run emits
        ``merge-detect`` and ``merge-write`` heartbeat start/done lines."""
        caplog.set_level(logging.INFO, logger="athenaeum")
        entries = merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=False,
            client=None,
        )
        assert len(entries) == 2

        lines = _heartbeat_lines(caplog)
        detect_lines = [line for line in lines if "phase=merge-detect" in line]
        write_lines = [line for line in lines if "phase=merge-write" in line]

        assert any("status=start" in line for line in detect_lines)
        assert any("status=done" in line for line in detect_lines)
        assert any("status=start" in line for line in write_lines)
        assert any("status=done" in line for line in write_lines)
        # 2 clusters -> 2 write ticks with interval_s=0 (always emit).
        assert sum("status=tick" in line for line in write_lines) == 2

        done_line = next(line for line in write_lines if "status=done" in line)
        assert "done=2" in done_line
        assert "compiled=2" in done_line

    def test_merge_dry_run_still_emits_detect_heartbeat(
        self, merge_root_two_clusters: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=True,
            client=None,
        )
        lines = _heartbeat_lines(caplog)
        detect_lines = [line for line in lines if "phase=merge-detect" in line]
        assert any("status=start" in line for line in detect_lines)
        assert any("status=done" in line for line in detect_lines)


# ---------------------------------------------------------------------------
# wiki-dedupe (#290)
# ---------------------------------------------------------------------------

_BODY_A = "Kromatic is Tristan's primary venture and main business focus."
_BODY_B = "Tristan's primary venture is Kromatic, his main company."
_VEC_A = [1.0, 0.0]
_VEC_B = [0.98, 0.2]
_TEXT_TO_VEC = {_BODY_A: _VEC_A, _BODY_B: _VEC_B}


def _fake_embed(texts: list[str]) -> list[list[float]] | None:
    return [_TEXT_TO_VEC.get(t.strip(), [0.0, 0.0]) for t in texts]


def _write_wiki_page(wiki_root: Path, filename: str, body: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {filename[:-3]}\ntype: concept\n---\n{body}\n"
    path = wiki_root / filename
    path.write_text(text, encoding="utf-8")
    return path


class TestWikiDedupeHeartbeat:
    def test_propose_wiki_page_merges_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        wiki_root = tmp_path / "wiki"
        _write_wiki_page(wiki_root, "venture-a.md", _BODY_A)
        _write_wiki_page(wiki_root, "venture-b.md", _BODY_B)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={"librarian": {"heartbeat_interval": 0}},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert len(proposals) == 1

        lines = _heartbeat_lines(caplog)
        dedupe_lines = [line for line in lines if "phase=wiki-dedupe" in line]
        assert any("status=start" in line for line in dedupe_lines)
        assert any("status=done" in line for line in dedupe_lines)

    def test_no_candidates_still_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={"librarian": {"heartbeat_interval": 0}},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert proposals == []

        lines = _heartbeat_lines(caplog)
        dedupe_lines = [line for line in lines if "phase=wiki-dedupe" in line]
        assert any("status=start" in line for line in dedupe_lines)
        assert any("status=done" in line for line in dedupe_lines)
        done_line = next(line for line in dedupe_lines if "status=done" in line)
        assert "done=0" in done_line
        assert "total=0" in done_line


# ---------------------------------------------------------------------------
# reresolve (#188)
# ---------------------------------------------------------------------------


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _write_reresolve_member(
    knowledge_root: Path, scope: str, filename: str, body: str
) -> str:
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: " + filename[:-3] + "\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return f"{scope}/{filename}"


def _escalate_proposalless(knowledge_root: Path) -> Path:
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    ref_a = _write_reresolve_member(
        knowledge_root, "scope-x", "feedback_a.md", "Tristan is German."
    )
    ref_b = _write_reresolve_member(
        knowledge_root, "scope-x", "feedback_b.md", "Tristan is NOT German."
    )
    description = (
        "Detector says these conflict.\n"
        "Passage 1: Tristan is German.\n"
        "Passage 2: Tristan is NOT German.\n"
        f"Members involved: {ref_a}, {ref_b}"
    )
    pending = wiki / "_pending_questions.md"
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-tristan.md",
                entity_name="Tristan",
                conflict_type="factual",
                description=description,
            )
        ],
        pending,
    )
    return pending


def _payload(action: str, *, winner: str = "a", confidence: float = 0.5) -> str:
    return (
        f'{{"recommended_winner": "{winner}", "action": "{action}", '
        f'"confidence": {confidence}, '
        '"rationale": "test verdict rationale.", '
        '"source_precedence_used": ["a:user > b:unsourced"]}'
    )


class TestReresolveHeartbeat:
    def test_reresolve_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        pending = _escalate_proposalless(tmp_path)

        client = _fake_client(_payload("keep_a", confidence=0.5))
        count = reresolve_open_questions(
            pending, client=client, config={"librarian": {"heartbeat_interval": 0}}
        )
        assert count == 1

        lines = _heartbeat_lines(caplog)
        reresolve_lines = [line for line in lines if "phase=reresolve" in line]
        assert any("status=start" in line for line in reresolve_lines)
        assert any("status=done" in line for line in reresolve_lines)
        assert any("status=tick" in line for line in reresolve_lines)
        tick_line = next(line for line in reresolve_lines if "status=tick" in line)
        assert "unit=Tristan" in tick_line

    def test_no_open_questions_still_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text("# Pending Questions\n", encoding="utf-8")

        client = _fake_client(_payload("keep_a"))
        count = reresolve_open_questions(pending, client=client, config={})
        assert count == 0

        lines = _heartbeat_lines(caplog)
        reresolve_lines = [line for line in lines if "phase=reresolve" in line]
        assert any("status=start" in line for line in reresolve_lines)
        assert any("status=done" in line for line in reresolve_lines)
