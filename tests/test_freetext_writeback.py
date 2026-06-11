# SPDX-License-Identifier: Apache-2.0
"""Tests for issue #210 — free-text answers must enact source-file edits.

Covers:
- Test 1 (career.md case): stubbed LLM removes "primary venture" framing;
  assert body rewritten, frontmatter preserved, annotation NOT appended.
- Test 2 (quantified_wins case): body with two competing numbers; ruling
  picks one; stubbed LLM removes the losing claim.
- Test 3 (fallback): client=None falls back to annotation; malformed JSON
  stub also falls back to annotation.
- Test 4: retain_both_with_context / not_a_conflict still annotate (proposer
  not invoked).
- Back-compat: existing ingest_answers callers without client/config still
  work (annotation path unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.answers import (
    PendingQuestion,
    _writeback_source,
    ingest_answers,
)  # noqa: PLC2701
from athenaeum.resolutions import propose_freetext_source_edits

# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _fake_client(payload_text: str) -> MagicMock:
    """Return a mock Anthropic client whose .messages.create yields canned text."""
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _edits_json(edits: list[dict]) -> str:
    return json.dumps({"edits": edits})


# ---------------------------------------------------------------------------
# propose_freetext_source_edits unit tests
# ---------------------------------------------------------------------------


class TestProposeFreetextSourceEdits:
    def test_client_none_returns_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "career.md"
        result = propose_freetext_source_edits(
            "Remove primary venture framing",
            [(src, "Krobar is the current primary venture.")],
            [],
            client=None,
        )
        assert result == {}

    def test_empty_sources_returns_empty(self) -> None:
        client = _fake_client(_edits_json([]))
        result = propose_freetext_source_edits("ruling", [], [], client=client)
        assert result == {}

    def test_career_case_removes_primary_venture(self, tmp_path: Path) -> None:
        src = tmp_path / "career.md"
        original_body = (
            "Tristan runs Krobar as his current primary venture.\n"
            "He also advises Kromatic.\n"
        )
        new_body = (
            "Tristan co-leads Krobar and Kromatic as co-equal ventures.\n"
            "He also advises Kromatic.\n"
        )
        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(payload)
        result = propose_freetext_source_edits(
            "Krobar and Kromatic are co-equal — stop framing Krobar as the primary venture",
            [(src, original_body)],
            ["current primary venture"],
            client=client,
        )
        assert src in result
        assert result[src] == new_body
        assert "primary venture" not in result[src]

    def test_quantified_wins_picks_one_number(self, tmp_path: Path) -> None:
        src = tmp_path / "quantified_wins.md"
        original_body = (
            "Tristan has worked with 63 multi-national companies.\n"
            "He has helped 82 corporations achieve product-market fit.\n"
        )
        new_body = "Tristan has worked with 63 multi-national companies.\n"
        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(payload)
        result = propose_freetext_source_edits(
            "Use 63 — the 82 figure was a rough estimate, remove it",
            [(src, original_body)],
            ["63 multi-national companies", "82 corporations"],
            client=client,
        )
        assert src in result
        assert "82 corporations" not in result[src]
        assert "63 multi-national" in result[src]

    def test_changed_false_excluded(self, tmp_path: Path) -> None:
        src = tmp_path / "mem.md"
        body = "Some body text.\n"
        payload = _edits_json([{"path": str(src), "changed": False, "new_body": body}])
        result = propose_freetext_source_edits(
            "ruling",
            [(src, body)],
            [],
            client=_fake_client(payload),
        )
        assert result == {}

    def test_identical_body_excluded(self, tmp_path: Path) -> None:
        src = tmp_path / "mem.md"
        body = "Unchanged body.\n"
        payload = _edits_json([{"path": str(src), "changed": True, "new_body": body}])
        result = propose_freetext_source_edits(
            "ruling",
            [(src, body)],
            [],
            client=_fake_client(payload),
        )
        assert result == {}

    def test_unknown_path_excluded(self, tmp_path: Path) -> None:
        src = tmp_path / "mem.md"
        body = "Some text.\n"
        payload = _edits_json(
            [{"path": "/nonexistent/path.md", "changed": True, "new_body": "x"}]
        )
        result = propose_freetext_source_edits(
            "ruling",
            [(src, body)],
            [],
            client=_fake_client(payload),
        )
        assert result == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "mem.md"
        client = _fake_client("NOT JSON AT ALL")
        result = propose_freetext_source_edits(
            "ruling",
            [(src, "body")],
            [],
            client=client,
        )
        assert result == {}

    def test_api_error_returns_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "mem.md"
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API down")
        result = propose_freetext_source_edits(
            "ruling",
            [(src, "body")],
            [],
            client=client,
        )
        assert result == {}

    def test_fenced_json_response_parsed(self, tmp_path: Path) -> None:
        """Issue #222: model wraps the edits object in ```json fences —
        the shared extractor must still recover the edits."""
        src = tmp_path / "mem.md"
        original_body = "Old claim.\n"
        new_body = "Corrected claim.\n"
        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(f"```json\n{payload}\n```")
        result = propose_freetext_source_edits(
            "ruling",
            [(src, original_body)],
            [],
            client=client,
        )
        assert result == {src: new_body}

    def test_prose_wrapped_json_response_parsed(self, tmp_path: Path) -> None:
        """Issue #222: leading/trailing prose around the edits object."""
        src = tmp_path / "mem.md"
        original_body = "Old claim.\n"
        new_body = "Corrected claim.\n"
        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(f"Here are the edits:\n{payload}\nHope that helps!")
        result = propose_freetext_source_edits(
            "ruling",
            [(src, original_body)],
            [],
            client=client,
        )
        assert result == {src: new_body}

    def test_fenced_response_with_trailing_example_brace(self, tmp_path: Path) -> None:
        """The #219 failure shape at this call site: fenced object plus a
        later brace span in trailing prose. The old greedy ``\\{.*\\}``
        regex swallowed both and failed to decode; the shared extractor
        prefers the fenced object."""
        src = tmp_path / "mem.md"
        original_body = "Old claim.\n"
        new_body = "Corrected claim.\n"
        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(
            f"```json\n{payload}\n```\n"
            'An unchanged file would look like {"changed": false}.'
        )
        result = propose_freetext_source_edits(
            "ruling",
            [(src, original_body)],
            [],
            client=client,
        )
        assert result == {src: new_body}


# ---------------------------------------------------------------------------
# _writeback_source integration tests
# ---------------------------------------------------------------------------


def _make_pq(
    tmp_path: Path,
    *,
    source_filename: str,
    source_body: str,
    frontmatter: str,
    answer: str,
    description: str = "Passage 1: old claim\nPassage 2: new claim",
) -> tuple[PendingQuestion, Path]:
    """Build a PendingQuestion and write the source file."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    auto_mem = raw / "auto-memory"
    auto_mem.mkdir(exist_ok=True)
    src = auto_mem / source_filename
    src.write_text(frontmatter + "\n" + source_body, encoding="utf-8")

    pq = PendingQuestion(
        id="testid001234",
        entity="Tristan",
        source=f"auto-memory/{source_filename}",
        question="Which is correct?",
        conflict_type="principled",
        description=description,
        created_at="2026-06-09",
        answered=True,
        answer_lines=[answer],
        raw_block="",
    )
    return pq, src


class TestWritebackSourceFreetext:
    def test_career_freetext_edits_source_file(self, tmp_path: Path) -> None:
        """Free-text ruling causes the source file to be rewritten (not annotated)."""
        frontmatter = "---\nname: tristan-career\nsource: user:session-1\n---"
        original_body = (
            "Tristan runs Krobar as his current primary venture.\n"
            "He also advises Kromatic.\n"
        )
        new_body = "Tristan co-leads Krobar and Kromatic as co-equal ventures.\n"
        ruling = "Krobar and Kromatic are co-equal — stop framing Krobar as the primary venture"

        payload = _edits_json(
            [
                {
                    "path": str(tmp_path / "raw" / "auto-memory" / "career.md"),
                    "changed": True,
                    "new_body": new_body,
                }
            ]
        )
        client = _fake_client(payload)

        pq, src = _make_pq(
            tmp_path,
            source_filename="career.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=ruling,
        )
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        edited = _writeback_source(pq, roots, client=client)

        assert edited == 1
        result = src.read_text(encoding="utf-8")
        # Body was rewritten
        assert "co-equal ventures" in result
        assert "primary venture" not in result
        # Frontmatter preserved
        assert "name: tristan-career" in result
        # Annotation marker NOT appended
        assert "Ratified annotation" not in result

    def test_quantified_wins_freetext_edits_source_file(self, tmp_path: Path) -> None:
        """Ruling that picks one number causes the other to be removed."""
        frontmatter = "---\nname: quantified-wins\nsource: user:session-2\n---"
        original_body = (
            "Tristan has worked with 63 multi-national companies.\n"
            "He has helped 82 corporations achieve product-market fit.\n"
        )
        new_body = "Tristan has worked with 63 multi-national companies.\n"
        ruling = "Use 63 — the 82 figure was a rough estimate, remove it"

        payload = _edits_json(
            [
                {
                    "path": str(tmp_path / "raw" / "auto-memory" / "wins.md"),
                    "changed": True,
                    "new_body": new_body,
                }
            ]
        )
        client = _fake_client(payload)

        pq, src = _make_pq(
            tmp_path,
            source_filename="wins.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=ruling,
            description="Passage 1: 63 multi-national companies\nPassage 2: 82 corporations",
        )
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        edited = _writeback_source(pq, roots, client=client)

        assert edited == 1
        result = src.read_text(encoding="utf-8")
        assert "82 corporations" not in result
        assert "63 multi-national" in result
        assert "Ratified annotation" not in result

    def test_client_none_falls_back_to_annotation(self, tmp_path: Path) -> None:
        """When client=None, free-text answer appends annotation (old behavior)."""
        frontmatter = "---\nname: tristan-career\nsource: user:session-1\n---"
        original_body = "Tristan runs Krobar as his current primary venture.\n"
        ruling = "Krobar and Kromatic are co-equal"

        pq, src = _make_pq(
            tmp_path,
            source_filename="career.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=ruling,
        )
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        edited = _writeback_source(pq, roots, client=None)

        assert edited == 1
        result = src.read_text(encoding="utf-8")
        # Annotation appended
        assert "Ratified annotation" in result
        # Original body NOT removed
        assert "primary venture" in result

    def test_malformed_json_falls_back_to_annotation(self, tmp_path: Path) -> None:
        """Malformed LLM JSON causes annotation fallback."""
        frontmatter = "---\nname: tristan-career\nsource: user:session-1\n---"
        original_body = "Tristan runs Krobar as his current primary venture.\n"
        ruling = "Krobar and Kromatic are co-equal"

        pq, src = _make_pq(
            tmp_path,
            source_filename="career.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=ruling,
        )
        client = _fake_client("THIS IS NOT JSON")
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        edited = _writeback_source(pq, roots, client=client)

        assert edited == 1
        result = src.read_text(encoding="utf-8")
        assert "Ratified annotation" in result

    def test_retain_both_verdict_still_annotates(self, tmp_path: Path) -> None:
        """retain_both_with_context verdict annotates; proposer NOT invoked."""
        frontmatter = "---\nname: tristan-career\nsource: user:session-1\n---"
        original_body = "Tristan runs Krobar as his current primary venture.\n"
        # Verdict token present → NOT a free-text answer
        answer = "retain_both_with_context both claims have merit"

        pq, src = _make_pq(
            tmp_path,
            source_filename="career.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=answer,
        )
        # If the proposer were called, it would return a rewrite. But it must NOT
        # be called for retain_both_with_context.
        client = _fake_client(
            _edits_json(
                [
                    {
                        "path": str(src),
                        "changed": True,
                        "new_body": "SHOULD NOT APPEAR\n",
                    }
                ]
            )
        )
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        _writeback_source(pq, roots, client=client)

        result = src.read_text(encoding="utf-8")
        assert "SHOULD NOT APPEAR" not in result
        assert "Ratified annotation" in result

    def test_not_a_conflict_verdict_still_annotates(self, tmp_path: Path) -> None:
        """not_a_conflict verdict annotates; proposer NOT invoked."""
        frontmatter = "---\nname: mem\nsource: user:session-1\n---"
        original_body = "Some text.\n"
        answer = "not_a_conflict these are different scenarios"

        pq, src = _make_pq(
            tmp_path,
            source_filename="mem.md",
            source_body=original_body,
            frontmatter=frontmatter,
            answer=answer,
        )
        client = _fake_client(
            _edits_json(
                [{"path": str(src), "changed": True, "new_body": "SHOULD NOT APPEAR\n"}]
            )
        )
        roots = [tmp_path / "raw", tmp_path / "wiki"]
        _writeback_source(pq, roots, client=client)

        result = src.read_text(encoding="utf-8")
        assert "SHOULD NOT APPEAR" not in result
        assert "Ratified annotation" in result


# ---------------------------------------------------------------------------
# ingest_answers back-compat tests
# ---------------------------------------------------------------------------


def _pending_block(
    *,
    entity: str = "Acme Corp",
    source: str = "auto-memory/career.md",
    answer: str,
    description: str = "Prior says X; new says Y.",
) -> str:
    return (
        f'## [2026-06-09] Entity: "{entity}" (from {source})\n'
        f"- [x] Which is correct?\n"
        f"**Conflict type**: principled\n"
        f"**Description**: {description}\n"
        f"\n{answer}\n"
    )


class TestIngestAnswersBackcompat:
    def test_no_client_arg_still_works(self, tmp_path: Path) -> None:
        """ingest_answers(pending, raw) — no client/config — is a no-op diff."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        (raw / "auto-memory").mkdir(parents=True)
        src = raw / "auto-memory" / "career.md"
        src.write_text(
            "---\nname: career\n---\n\nTristan runs Krobar as primary venture.\n",
            encoding="utf-8",
        )

        pending = wiki / "_pending_questions.md"
        pending.write_text(
            "# Pending Questions\n\n"
            + _pending_block(
                source="auto-memory/career.md",
                answer="Krobar and Kromatic are co-equal",
            )
        )
        count = ingest_answers(pending, raw)
        assert count == 1
        # Annotation path used (no client)
        result = src.read_text(encoding="utf-8")
        assert "Ratified annotation" in result

    def test_with_client_uses_proposer(self, tmp_path: Path) -> None:
        """ingest_answers with client= threads through to _writeback_source."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = tmp_path / "raw"
        (raw / "auto-memory").mkdir(parents=True)
        src = raw / "auto-memory" / "career.md"
        original_body = "Tristan runs Krobar as his current primary venture.\n"
        src.write_text(
            "---\nname: career\nsource: user:s1\n---\n\n" + original_body,
            encoding="utf-8",
        )
        new_body = "Tristan co-leads Krobar and Kromatic as co-equal ventures.\n"

        payload = _edits_json(
            [{"path": str(src), "changed": True, "new_body": new_body}]
        )
        client = _fake_client(payload)

        pending = wiki / "_pending_questions.md"
        pending.write_text(
            "# Pending Questions\n\n"
            + _pending_block(
                source="auto-memory/career.md",
                answer=(
                    "Krobar and Kromatic are co-equal — "
                    "stop framing Krobar as the primary venture"
                ),
            )
        )
        count = ingest_answers(pending, raw, client=client)
        assert count == 1
        result = src.read_text(encoding="utf-8")
        assert "co-equal ventures" in result
        assert "primary venture" not in result
        assert "Ratified annotation" not in result
