"""Tests for athenaeum.answers — pending-question answer ingestion."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.answers import (
    PendingQuestion,
    ingest_answers,
    list_unanswered,
    parse_pending_questions,
    resolve_by_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _block(
    *,
    date: str = "2026-04-20",
    entity: str = "Acme Corp",
    source: str = "sessions/20240406T120000Z-aabb0011.md",
    checkbox: str = "[ ]",
    question: str = "Is Acme still Series A after the 2026 recap?",
    conflict_type: str = "principled",
    description: str = "Wiki says Series A; new raw implies Series B.",
    answer: str = "",
) -> str:
    block = (
        f"## [{date}] Entity: \"{entity}\" (from {source})\n"
        f"- {checkbox} {question}\n"
        f"**Conflict type**: {conflict_type}\n"
        f"**Description**: {description}\n"
    )
    if answer:
        block += f"\n{answer}\n"
    return block


@pytest.fixture
def pending_path(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki / "_pending_questions.md"


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw


# ---------------------------------------------------------------------------
# parse_pending_questions
# ---------------------------------------------------------------------------


class TestParsePendingQuestions:
    def test_returns_empty_when_file_missing(self, pending_path: Path) -> None:
        assert parse_pending_questions(pending_path) == []

    def test_parses_single_unanswered_block(self, pending_path: Path) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        parsed = parse_pending_questions(pending_path)
        assert len(parsed) == 1
        pq = parsed[0]
        assert pq.entity == "Acme Corp"
        assert pq.source == "sessions/20240406T120000Z-aabb0011.md"
        assert pq.question.startswith("Is Acme still")
        assert pq.conflict_type == "principled"
        assert "Series B" in pq.description
        assert pq.created_at == "2026-04-20"
        assert pq.answered is False
        assert pq.answer_lines == []
        assert len(pq.id) == 12

    def test_parses_answered_block_with_body(self, pending_path: Path) -> None:
        block = _block(
            checkbox="[x]",
            answer="They closed Series B in March 2026. Prior wiki is stale.",
        )
        pending_path.write_text("# Pending Questions\n\n" + block)
        parsed = parse_pending_questions(pending_path)
        assert len(parsed) == 1
        pq = parsed[0]
        assert pq.answered is True
        assert any("Series B in March" in line for line in pq.answer_lines)

    def test_id_stable_across_runs(self, pending_path: Path) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        first = parse_pending_questions(pending_path)[0].id
        second = parse_pending_questions(pending_path)[0].id
        assert first == second

    def test_id_differs_between_blocks(self, pending_path: Path) -> None:
        content = (
            "# Pending Questions\n\n"
            + _block(entity="Acme Corp")
            + "\n---\n\n"
            + _block(entity="Globex")
        )
        pending_path.write_text(content)
        parsed = parse_pending_questions(pending_path)
        assert len({pq.id for pq in parsed}) == 2

    def test_skips_malformed_header(
        self, pending_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        content = (
            "# Pending Questions\n\n"
            "## Not a real header\n"
            "- [ ] some question\n"
            "**Conflict type**: ambiguous\n"
            "**Description**: desc\n"
        )
        pending_path.write_text(content)
        parsed = parse_pending_questions(pending_path)
        assert parsed == []
        err = capsys.readouterr().err
        assert "malformed header" in err

    def test_skips_block_without_checkbox(
        self, pending_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        content = (
            "# Pending Questions\n\n"
            "## [2026-04-20] Entity: \"Acme\" (from sessions/x.md)\n"
            "**Conflict type**: principled\n"
            "**Description**: desc\n"
        )
        pending_path.write_text(content)
        parsed = parse_pending_questions(pending_path)
        assert parsed == []
        err = capsys.readouterr().err
        assert "without `- [ ]` line" in err


# ---------------------------------------------------------------------------
# ingest_answers — round-trip + idempotency + mixed state
# ---------------------------------------------------------------------------


class TestIngestAnswers:
    def test_noop_when_file_missing(self, pending_path: Path, raw_root: Path) -> None:
        assert ingest_answers(pending_path, raw_root) == 0

    def test_noop_when_no_answered_blocks(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        original = pending_path.read_text()
        assert ingest_answers(pending_path, raw_root) == 0
        # File untouched, no archive created.
        assert pending_path.read_text() == original
        assert not (pending_path.parent / "_pending_questions_archive.md").exists()

    def test_round_trip_single_answer(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        answered = _block(
            checkbox="[x]",
            answer="They closed Series B in March 2026.",
        )
        pending_path.write_text("# Pending Questions\n\n" + answered)

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        # Raw intake file exists with correct frontmatter.
        answer_files = list((raw_root / "answers").glob("*.md"))
        assert len(answer_files) == 1
        text = answer_files[0].read_text()
        assert "source: pending_question_answer" in text
        assert "original_source: raw/sessions/20240406T120000Z-aabb0011.md" in text
        assert "entity: Acme Corp" in text
        assert "resolved_at:" in text
        assert "Series B in March 2026" in text

        # Archive file contains the block.
        archive = pending_path.parent / "_pending_questions_archive.md"
        assert archive.exists()
        archive_text = archive.read_text()
        assert "Acme Corp" in archive_text
        assert "**Archived**:" in archive_text

        # Primary file no longer contains the answered block.
        primary_text = pending_path.read_text()
        assert "Acme Corp" not in primary_text
        assert "# Pending Questions" in primary_text

    def test_mixed_state_leaves_unanswered_blocks(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        answered = _block(
            entity="Answered Co",
            checkbox="[x]",
            answer="Confirmed Series B.",
        )
        unanswered = _block(entity="Unanswered Co")
        pending_path.write_text(
            "# Pending Questions\n\n" + answered + "\n---\n\n" + unanswered
        )

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        primary_text = pending_path.read_text()
        assert "Unanswered Co" in primary_text
        assert "Answered Co" not in primary_text
        # Checkbox for unanswered preserved.
        assert "- [ ]" in primary_text

        archive_text = (pending_path.parent / "_pending_questions_archive.md").read_text()
        assert "Answered Co" in archive_text
        assert "Unanswered Co" not in archive_text

    def test_idempotent_rerun_is_noop(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        answered = _block(checkbox="[x]", answer="Confirmed.")
        pending_path.write_text("# Pending Questions\n\n" + answered)

        assert ingest_answers(pending_path, raw_root) == 1
        # Second run — nothing answered remains.
        assert ingest_answers(pending_path, raw_root) == 0
        # Still exactly one raw answer file.
        assert len(list((raw_root / "answers").glob("*.md"))) == 1

    def test_archive_newest_first(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # First run: archive "First" block.
        first = _block(entity="First Co", checkbox="[x]", answer="answer one")
        pending_path.write_text("# Pending Questions\n\n" + first)
        ingest_answers(pending_path, raw_root)

        # Second run: add + archive "Second" block.
        second = _block(entity="Second Co", checkbox="[x]", answer="answer two")
        pending_path.write_text("# Pending Questions\n\n" + second)
        ingest_answers(pending_path, raw_root)

        archive_text = (pending_path.parent / "_pending_questions_archive.md").read_text()
        second_pos = archive_text.find("Second Co")
        first_pos = archive_text.find("First Co")
        assert second_pos != -1 and first_pos != -1
        assert second_pos < first_pos  # newest-first

    def test_malformed_block_preserved_verbatim(
        self, pending_path: Path, raw_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        answered = _block(checkbox="[x]", answer="ok")
        malformed = (
            "## Not a real header\n"
            "- [x] garbled\n"
            "**Conflict type**: ???\n"
        )
        pending_path.write_text(
            "# Pending Questions\n\n" + answered + "\n---\n\n" + malformed
        )

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        # Malformed block survived in the primary file so the human can fix it.
        primary_text = pending_path.read_text()
        assert "Not a real header" in primary_text

    def test_collision_safe_filenames(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # Two answered blocks for the same entity resolved in one run should
        # not clobber each other's raw files.
        a = _block(entity="Acme Co", checkbox="[x]", answer="first answer")
        b = _block(
            entity="Acme Co",
            source="sessions/other.md",
            checkbox="[x]",
            answer="second answer",
            question="Second distinct question?",
        )
        pending_path.write_text(
            "# Pending Questions\n\n" + a + "\n---\n\n" + b
        )
        count = ingest_answers(pending_path, raw_root)
        assert count == 2
        assert len(list((raw_root / "answers").glob("*.md"))) == 2


# ---------------------------------------------------------------------------
# MCP helper surface
# ---------------------------------------------------------------------------


class TestListUnanswered:
    def test_returns_unanswered_only(self, pending_path: Path) -> None:
        content = (
            "# Pending Questions\n\n"
            + _block(entity="Open One")
            + "\n---\n\n"
            + _block(entity="Closed One", checkbox="[x]", answer="answer")
        )
        pending_path.write_text(content)
        items = list_unanswered(pending_path)
        assert len(items) == 1
        assert items[0]["entity"] == "Open One"
        assert "id" in items[0]
        assert "question" in items[0]

    def test_empty_when_file_missing(self, pending_path: Path) -> None:
        assert list_unanswered(pending_path) == []


class TestResolveById:
    def test_happy_path_flips_checkbox_and_inserts_answer(
        self, pending_path: Path
    ) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        items = list_unanswered(pending_path)
        assert len(items) == 1
        qid = items[0]["id"]

        result = resolve_by_id(
            pending_path, qid, "They closed Series B in March 2026."
        )
        assert result["ok"] is True
        assert "[x]" in result["block"]
        assert "Series B" in result["block"]

        # File on disk now has the flipped checkbox + answer.
        text = pending_path.read_text()
        assert "- [x]" in text
        assert "Series B in March 2026" in text
        # Block is still in the primary file — archival happens on ingest.
        assert "Acme Corp" in text

    def test_not_found_returns_error(self, pending_path: Path) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        result = resolve_by_id(pending_path, "doesnotexist", "answer")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_missing_file_returns_error(self, pending_path: Path) -> None:
        result = resolve_by_id(pending_path, "anyid", "answer")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_already_answered_returns_error(
        self, pending_path: Path
    ) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        items = list_unanswered(pending_path)
        qid = items[0]["id"]
        assert resolve_by_id(pending_path, qid, "first")["ok"] is True
        # Second call on the same id: checkbox already [x].
        result = resolve_by_id(pending_path, qid, "second")
        assert result["ok"] is False
        # Either "already answered" or "not found" (id may shift when
        # checkbox line changes depending on hash inputs). We only hash
        # header + question (not checkbox state), so the id is stable and
        # the error path we want is "already answered".
        assert "already answered" in result["error"]


# ---------------------------------------------------------------------------
# Smoke: PendingQuestion dataclass round-trip
# ---------------------------------------------------------------------------


def test_tier4_render_round_trips_through_parser(tmp_path: Path) -> None:
    """End-to-end: tier4_escalate output is parseable by parse_pending_questions.

    Highest-risk regression in issue #61 — mismatched regex between the
    renderer in `tiers.py` and the parser here. This test fails loudly
    the moment either side drifts.
    """
    from athenaeum.models import EscalationItem
    from athenaeum.tiers import tier4_escalate

    pending = tmp_path / "_pending_questions.md"
    items = [
        EscalationItem(
            raw_ref="sessions/20240406T120000Z-aabb.md",
            entity_name="Acme Corp",
            conflict_type="principled",
            description="Prior wiki says Series A; new raw implies Series B.",
        ),
    ]
    tier4_escalate(items, pending)

    parsed = parse_pending_questions(pending)
    assert len(parsed) == 1
    pq = parsed[0]
    assert pq.entity == "Acme Corp"
    assert pq.source == "sessions/20240406T120000Z-aabb.md"
    assert pq.conflict_type == "principled"
    assert pq.answered is False
    assert pq.question  # non-empty


def test_pending_question_is_dataclass() -> None:
    pq = PendingQuestion(
        id="abc",
        entity="E",
        source="s",
        question="q",
        conflict_type="ambiguous",
        description="d",
        created_at="2026-04-20",
        answered=False,
        answer_lines=[],
        raw_block="",
    )
    assert pq.id == "abc"
    assert pq.answered is False
