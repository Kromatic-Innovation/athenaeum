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
        f'## [{date}] Entity: "{entity}" (from {source})\n'
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

    def test_recovers_checkbox_less_block_with_description(
        self, pending_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A stray/legacy escalation writer that omits the `- [ ]` line still
        # produces a recoverable block when it carries a `**Description**:`.
        # The parser must synthesize a checkbox from the description's first
        # line rather than silently dropping the block forever.
        content = (
            "# Pending Questions\n\n"
            '## [2026-04-20] Entity: "Acme" (from sessions/x.md)\n'
            "**Conflict type**: principled\n"
            "**Description**: Wiki says Series A; new raw implies Series B.\n"
        )
        pending_path.write_text(content)
        parsed = parse_pending_questions(pending_path)
        assert len(parsed) == 1
        pq = parsed[0]
        assert pq.entity == "Acme"
        assert pq.answered is False
        # Question synthesized from the first line of the description.
        assert pq.question == "Wiki says Series A; new raw implies Series B."
        assert pq.conflict_type == "principled"
        assert "Series B" in pq.description
        # The recovered raw_block now carries a real `- [ ]` line so the
        # repair persists across file rewrites (no relapse to skipped).
        assert "- [ ] " in pq.raw_block
        err = capsys.readouterr().err
        assert "synthesized checkbox from description" in err

    def test_recovered_block_persists_checkbox_through_resolve(
        self, pending_path: Path
    ) -> None:
        # End-to-end: a checkbox-less block should be answerable via
        # resolve_by_id, and the rewritten file must retain the checkbox so
        # a subsequent parse sees a normal block (no relapse).
        content = (
            "# Pending Questions\n\n"
            '## [2026-04-20] Entity: "Acme" (from sessions/x.md)\n'
            "**Conflict type**: principled\n"
            "**Description**: Series A vs Series B?\n"
        )
        pending_path.write_text(content)
        pq = parse_pending_questions(pending_path)[0]

        result = resolve_by_id(pending_path, pq.id, "Series B, closed March 2026.")
        assert result["ok"] is True

        # File now has a checked checkbox and the answer body.
        text = pending_path.read_text()
        assert "- [x] Series A vs Series B?" in text
        assert "Series B, closed March 2026." in text

        # Re-parse: block is well-formed and answered (no skip warning).
        reparsed = parse_pending_questions(pending_path)
        assert len(reparsed) == 1
        assert reparsed[0].answered is True

    def test_skips_block_without_checkbox_or_description(
        self, pending_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No checkbox AND no description => no recoverable question => skip.
        content = (
            "# Pending Questions\n\n"
            '## [2026-04-20] Entity: "Acme" (from sessions/x.md)\n'
            "**Conflict type**: principled\n"
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

    def test_round_trip_single_answer(self, pending_path: Path, raw_root: Path) -> None:
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

        archive_text = (
            pending_path.parent / "_pending_questions_archive.md"
        ).read_text()
        assert "Answered Co" in archive_text
        assert "Unanswered Co" not in archive_text

    def test_idempotent_rerun_is_noop(self, pending_path: Path, raw_root: Path) -> None:
        answered = _block(checkbox="[x]", answer="Confirmed.")
        pending_path.write_text("# Pending Questions\n\n" + answered)

        assert ingest_answers(pending_path, raw_root) == 1
        # Second run — nothing answered remains.
        assert ingest_answers(pending_path, raw_root) == 0
        # Still exactly one raw answer file.
        assert len(list((raw_root / "answers").glob("*.md"))) == 1

    def test_archive_newest_first(self, pending_path: Path, raw_root: Path) -> None:
        # First run: archive "First" block.
        first = _block(entity="First Co", checkbox="[x]", answer="answer one")
        pending_path.write_text("# Pending Questions\n\n" + first)
        ingest_answers(pending_path, raw_root)

        # Second run: add + archive "Second" block.
        second = _block(entity="Second Co", checkbox="[x]", answer="answer two")
        pending_path.write_text("# Pending Questions\n\n" + second)
        ingest_answers(pending_path, raw_root)

        archive_text = (
            pending_path.parent / "_pending_questions_archive.md"
        ).read_text()
        second_pos = archive_text.find("Second Co")
        first_pos = archive_text.find("First Co")
        assert second_pos != -1 and first_pos != -1
        assert second_pos < first_pos  # newest-first

    def test_malformed_block_preserved_verbatim(
        self,
        pending_path: Path,
        raw_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        answered = _block(checkbox="[x]", answer="ok")
        malformed = (
            "## Not a real header\n" "- [x] garbled\n" "**Conflict type**: ???\n"
        )
        pending_path.write_text(
            "# Pending Questions\n\n" + answered + "\n---\n\n" + malformed
        )

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        # Malformed block survived in the primary file so the human can fix it.
        primary_text = pending_path.read_text()
        assert "Not a real header" in primary_text

    def test_collision_safe_filenames(self, pending_path: Path, raw_root: Path) -> None:
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
        pending_path.write_text("# Pending Questions\n\n" + a + "\n---\n\n" + b)
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

        result = resolve_by_id(pending_path, qid, "They closed Series B in March 2026.")
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
        assert result["error_code"] == "id_not_found"
        assert "not found" in result["message"]

    def test_missing_file_returns_error(self, pending_path: Path) -> None:
        result = resolve_by_id(pending_path, "anyid", "answer")
        assert result["ok"] is False
        assert result["error_code"] == "file_missing"
        assert "not found" in result["message"]

    def test_already_answered_returns_error(self, pending_path: Path) -> None:
        pending_path.write_text("# Pending Questions\n\n" + _block())
        items = list_unanswered(pending_path)
        qid = items[0]["id"]
        assert resolve_by_id(pending_path, qid, "first")["ok"] is True
        # Second call on the same id: checkbox already [x]. Because the id
        # hash is over header + question text only (not checkbox state),
        # the id is stable across the `[ ]` -> `[x]` flip and the error
        # path we want is `already_answered` — this positively asserts the
        # id invariant documented on `_make_id`.
        result = resolve_by_id(pending_path, qid, "second")
        assert result["ok"] is False
        assert result["error_code"] == "already_answered"
        assert "already answered" in result["message"]


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


@pytest.mark.parametrize(
    ("entity_name", "raw_ref"),
    [
        ("Acme Corp", "sessions/foo.md"),  # clean baseline
        ('Acme "The Great" Corp', "sessions/foo.md"),  # embedded quotes
        ("Acme Corp", "sessions/foo (v2).md"),  # parens in ref
        ('Acme "The Great" Corp', "sessions/foo (v2).md"),  # both
        ('Université "Étoile" Inc', "sessions/straße.md"),  # unicode entity + ref
        ("Globex", "sessions/notes (rev 3) (final).md"),  # multiple parens in ref
    ],
)
def test_tier4_round_trip_hostile_inputs(
    tmp_path: Path, entity_name: str, raw_ref: str
) -> None:
    """tier4_escalate output must round-trip through parse_pending_questions
    cleanly even when entity_name contains double quotes and raw_ref contains
    parentheses. Guards the renderer/parser contract documented in #61.
    """
    from athenaeum.models import EscalationItem
    from athenaeum.tiers import tier4_escalate

    pending = tmp_path / "_pending_questions.md"
    items = [
        EscalationItem(
            raw_ref=raw_ref,
            entity_name=entity_name,
            conflict_type="principled",
            description="Conflict description; contract test.",
        ),
    ]
    tier4_escalate(items, pending)

    # Raw file frontmatter sanity: header uses the escaped form for quotes
    # but the raw ref is written unmolested.
    raw_text = pending.read_text(encoding="utf-8")
    assert raw_ref in raw_text  # paths stored verbatim

    parsed = parse_pending_questions(pending)
    assert len(parsed) == 1, f"expected 1 block, got {len(parsed)}"
    pq = parsed[0]
    assert pq.entity == entity_name  # round-trips after unescape
    assert pq.source == raw_ref  # paths preserved


# ---------------------------------------------------------------------------
# Issue #197: source write-back — ratified verdicts edit the SOURCE memory file
# ---------------------------------------------------------------------------


def _source_block(
    *,
    entity: str = "Acme Corp",
    source: str,
    answer: str,
    also_affects: str = "",
    passage: str = "Acme is Series A as of 2024.",
    checkbox: str = "[x]",
) -> str:
    description = f"Wiki says Series A; new raw implies Series B.\nPassage A: {passage}"
    block = (
        f'## [2026-04-20] Entity: "{entity}" (from {source})\n'
        f"- {checkbox} Is Acme still Series A?\n"
        f"**Conflict type**: principled\n"
        f"**Description**: {description}\n"
    )
    if also_affects:
        block += f"**Also affects**: {also_affects}\n"
    block += f"\n{answer}\n"
    return block


def _write_source(path: Path, body: str, *, name: str = "Acme Corp") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: reference\n"
        f"name: {name}\n"
        "source: user:session-2026-04-10\n"
        "---\n\n" + body,
        encoding="utf-8",
    )


def _two_member_block(
    *,
    a_rel: str,
    b_rel: str,
    answer: str,
    entity: str = "Acme Corp",
) -> str:
    """Pending block naming two source members in resolver a/b order.

    ``**Member paths**: a, b`` carries the source paths the verdict applies
    to; ``member_paths[0]`` is side a, ``member_paths[1]`` is side b — the
    same order develop's ``enact_resolution`` expects.
    """
    return (
        f'## [2026-04-20] Entity: "{entity}" (from {a_rel})\n'
        f"- [x] Which Acme series is current?\n"
        f"**Conflict type**: principled\n"
        f"**Member paths**: {a_rel}, {b_rel}\n"
        f"**Description**: Wiki says Series A; new raw implies Series B.\n"
        f"\n{answer}\n"
    )


class TestSourceWriteBack:
    def test_correct_deletes_wrong_member_file(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # develop's contract: correct_a means side a is correct → DELETE the
        # WRONG member (side b = member_paths[1]). The wiki regenerates clean
        # from the surviving member; no in-place passage rewrite.
        a_rel = "auto-memory/scope/reference_acme_a.md"
        b_rel = "auto-memory/scope/reference_acme_b.md"
        a = raw_root / a_rel
        b = raw_root / b_rel
        _write_source(a, "Acme is Series A as of 2024.\n")
        _write_source(b, "Acme is Series B.\n", name="Acme B")

        block = _two_member_block(
            a_rel=a_rel,
            b_rel=b_rel,
            answer="correct_a\nSide a is right.",
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        # Winner (a) survives; wrong member (b) deleted.
        assert a.exists()
        assert not b.exists()

        # Provenance doc STILL written.
        answer_files = list((raw_root / "answers").glob("*.md"))
        assert len(answer_files) == 1

    def test_keep_marks_loser_superseded(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # keep_a: a wins, the LOSING member (b) is marked superseded_by the
        # winner's name. Non-destructive — both files kept.
        a_rel = "auto-memory/scope/reference_acme_a.md"
        b_rel = "auto-memory/scope/reference_acme_b.md"
        a = raw_root / a_rel
        b = raw_root / b_rel
        _write_source(a, "Acme is Series A.\n", name="Acme Corp")
        _write_source(b, "Acme is Series B.\n", name="Acme B")

        block = _two_member_block(
            a_rel=a_rel, b_rel=b_rel, answer="keep_a\nKeep the A snapshot."
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        assert ingest_answers(pending_path, raw_root) == 1

        # Both files kept; loser b marked superseded_by the winner name.
        assert a.exists()
        assert b.exists()
        assert "superseded_by: Acme Corp" in b.read_text()

    def test_deprecate_both_marks_both_members(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        a_rel = "auto-memory/scope/reference_acme_a.md"
        b_rel = "auto-memory/scope/reference_acme_b.md"
        a = raw_root / a_rel
        b = raw_root / b_rel
        _write_source(a, "Acme is Series A.\n")
        _write_source(b, "Acme is Series B.\n", name="Acme B")

        block = _two_member_block(
            a_rel=a_rel, b_rel=b_rel, answer="deprecate_both\nBoth stale."
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        assert ingest_answers(pending_path, raw_root) == 1
        assert "deprecated: true" in a.read_text()
        assert "deprecated: true" in b.read_text()

    def test_archive_marks_single_source_deprecated(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # A human "archive/mark historical" verdict on a single named source
        # reuses the deprecated:true frontmatter marker — no new editor.
        src_rel = "auto-memory/scope/reference_acme.md"
        src = raw_root / src_rel
        _write_source(src, "Acme is Series A as of 2024.\n")

        block = _source_block(source=src_rel, answer="archive\nNo longer relevant.")
        pending_path.write_text("# Pending Questions\n\n" + block)

        assert ingest_answers(pending_path, raw_root) == 1
        src_text = src.read_text()
        assert "deprecated: true" in src_text
        # Body content preserved (non-destructive whole-file marker).
        assert "Acme is Series A as of 2024." in src_text

    def test_retain_both_annotates_source_non_destructively(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        src_rel = "auto-memory/scope/reference_acme.md"
        src = raw_root / src_rel
        _write_source(src, "Acme is Series A as of 2024.\n")

        block = _source_block(
            source=src_rel,
            answer=(
                "retain_both_with_context\n"
                "Series A is the 2024 snapshot; Series B is 2026. Both valid."
            ),
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        src_text = src.read_text()
        # Original passage NOT deleted.
        assert "Acme is Series A as of 2024." in src_text
        # Annotation recorded.
        assert "2024 snapshot" in src_text

        # Provenance still emitted.
        assert len(list((raw_root / "answers").glob("*.md"))) == 1

    def test_free_text_answer_without_token_annotated_not_dropped(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        src_rel = "auto-memory/scope/reference_acme.md"
        src = raw_root / src_rel
        _write_source(src, "Acme is Series A as of 2024.\n")

        block = _source_block(
            source=src_rel,
            answer="Honestly I'm not sure but lean towards keeping it for now.",
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        count = ingest_answers(pending_path, raw_root)
        assert count == 1

        src_text = src.read_text()
        # Free-text recorded as an authoritative annotation — never dropped.
        assert "lean towards keeping it" in src_text
        # Non-destructive: original passage preserved.
        assert "Acme is Series A as of 2024." in src_text

    def test_provenance_still_written_when_source_missing(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        # Source file does not exist on disk — write-back is a no-op but the
        # audit trail must still be emitted.
        block = _source_block(
            source="auto-memory/scope/gone.md",
            answer="correct_a\nNew value.",
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        count = ingest_answers(pending_path, raw_root)
        assert count == 1
        assert len(list((raw_root / "answers").glob("*.md"))) == 1


def _freetext_proposer_client(
    new_body: str,
    *,
    path_str: str,
    input_tokens: int = 120,
    output_tokens: int = 40,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
):
    """Fake Anthropic client for the free-text source-edit proposer.

    Returns a JSON ``edits`` payload naming ``path_str`` with the supplied
    ``new_body``, and a ``.usage`` carrying real ints so
    :func:`athenaeum.models.cache_usage_counts` accumulates non-zero counts
    (a bare ``MagicMock`` would coerce to 0 via the int-guard).
    """
    import json
    from unittest.mock import MagicMock

    client = MagicMock()
    response = MagicMock()
    payload = json.dumps(
        {"edits": [{"path": path_str, "changed": True, "new_body": new_body}]}
    )
    response.content = [MagicMock(text=payload)]
    response.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    client.messages.create.return_value = response
    return client


class TestIngestAnswersUsageAccounting:
    """Issue #248: the ingest-answers free-text path is metered.

    The free-text proposer (the only LLM call on this path) accumulates its
    response's token + cache counts into a run-level ``TokenUsage`` and the
    call site counts one ``api_calls`` attempt. A one-line cost summary is
    emitted when >= 1 API call was made, and absent otherwise.
    """

    def test_freetext_proposer_accumulates_tokens_and_cache(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        """RED on current code: tokens/cache flow into the threaded usage."""
        from athenaeum.models import TokenUsage
        from athenaeum.resolutions import propose_freetext_source_edits

        src = raw_root / "auto-memory" / "scope" / "reference_acme.md"
        _write_source(src, "Acme is Series A as of 2024.\n")
        _, body = (
            src.read_text(encoding="utf-8").split("---\n\n", 1)
            if "---\n\n" in src.read_text(encoding="utf-8")
            else ("", "Acme is Series A as of 2024.\n")
        )

        client = _freetext_proposer_client(
            "Acme is Series B as of 2026.\n",
            path_str=str(src),
            input_tokens=120,
            output_tokens=40,
            cache_creation_input_tokens=15,
            cache_read_input_tokens=7,
        )
        usage = TokenUsage()
        proposed = propose_freetext_source_edits(
            "Use the 2026 figure.",
            [(src, body)],
            ["Acme is Series A as of 2024."],
            client,
            None,
            usage=usage,
        )

        assert proposed  # the proposer returned an edit
        assert usage.input_tokens == 120
        assert usage.output_tokens == 40
        assert usage.cache_creation_input_tokens == 15
        assert usage.cache_read_input_tokens == 7
        # #239 convention: the callee never bumps api_calls (caller counts).
        assert usage.api_calls == 0

    def test_callee_does_not_bump_api_calls(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        """Mirror the #239 convention test: the proposer counts no attempt."""
        from athenaeum.models import TokenUsage
        from athenaeum.resolutions import propose_freetext_source_edits

        src = raw_root / "auto-memory" / "scope" / "reference_acme.md"
        _write_source(src, "Acme is Series A.\n")
        client = _freetext_proposer_client("Acme is Series B.\n", path_str=str(src))
        usage = TokenUsage()
        propose_freetext_source_edits(
            "Use the newer value.",
            [(src, "Acme is Series A.\n")],
            [],
            client,
            None,
            usage=usage,
        )
        assert usage.api_calls == 0

    def test_call_without_usage_keeps_current_behavior(
        self, pending_path: Path, raw_root: Path
    ) -> None:
        """External callers omitting ``usage`` are unaffected (keyword-default)."""
        from athenaeum.resolutions import propose_freetext_source_edits

        src = raw_root / "auto-memory" / "scope" / "reference_acme.md"
        _write_source(src, "Acme is Series A.\n")
        client = _freetext_proposer_client("Acme is Series B.\n", path_str=str(src))
        # No usage kwarg — must not raise and must still return the edit.
        proposed = propose_freetext_source_edits(
            "Use the newer value.",
            [(src, "Acme is Series A.\n")],
            [],
            client,
            None,
        )
        assert proposed[src] == "Acme is Series B.\n"

    def test_ingest_run_accumulates_and_logs_summary(
        self,
        pending_path: Path,
        raw_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """End-to-end: a free-text ingest run meters spend and logs a summary."""
        import logging

        src_rel = "auto-memory/scope/reference_acme.md"
        src = raw_root / src_rel
        _write_source(src, "Acme is Series A as of 2024.\n")

        block = _source_block(
            source=src_rel,
            answer="Use the 2026 figure going forward.",
        )
        pending_path.write_text("# Pending Questions\n\n" + block)

        client = _freetext_proposer_client(
            "Acme is Series B as of 2026.\n",
            path_str=str(src),
            input_tokens=200,
            output_tokens=60,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=5,
        )
        caplog.set_level(logging.INFO, logger="athenaeum.answers")

        count = ingest_answers(pending_path, raw_root, client=client)
        assert count == 1

        messages = [r.getMessage() for r in caplog.records]
        summary = [m for m in messages if m.startswith("Token usage:")]
        assert summary, messages
        line = summary[0]
        assert "1 API calls" in line
        assert "200 input + 60 output = 260 total" in line
        assert "(cache: 10 written, 5 read)" in line
        assert "estimated)" in line

    def test_no_summary_when_no_api_call(
        self,
        pending_path: Path,
        raw_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A run that makes no API call emits no summary line."""
        import logging

        src_rel = "auto-memory/scope/reference_acme.md"
        src = raw_root / src_rel
        _write_source(src, "Acme is Series A as of 2024.\n")

        # An enacting verdict (archive) never calls the proposer; client=None
        # also guarantees no API call.
        block = _source_block(source=src_rel, answer="archive\nNo longer relevant.")
        pending_path.write_text("# Pending Questions\n\n" + block)
        caplog.set_level(logging.INFO, logger="athenaeum.answers")

        count = ingest_answers(pending_path, raw_root, client=None)
        assert count == 1
        messages = [r.getMessage() for r in caplog.records]
        assert not any(m.startswith("Token usage:") for m in messages), messages


def test_corrected_source_no_longer_regenerates_conflict(
    pending_path: Path, raw_root: Path
) -> None:
    """Regression / real acceptance (issue #197).

    Proves the fix changes the detector's verdict, not merely the cluster
    structure. A genuinely-conflicting 2-member cluster (a vs b) WOULD flag
    pre-ingest (proven with a stubbed contradiction verdict). After ingest
    enacts ``correct_a`` and write-back DELETES the wrong member (b), the
    surviving member SET — rebuilt from disk — no longer contains the
    conflicting partner, so the same stubbed detector cannot re-flag it.

    Fix-dependent: if write-back is disabled, b survives on disk, the
    rebuilt set is still ``[a, b]``, and the stub re-flags ``detected=True``
    — so this test goes red. The post-ingest assertion's outcome is driven
    by whether the wrong member was actually removed, not by a structural
    singleton/None-client short-circuit.
    """
    from unittest.mock import MagicMock

    from athenaeum.contradictions import detect_contradictions
    from athenaeum.models import AutoMemoryFile

    a_rel = "auto-memory/scope/reference_acme_a.md"
    b_rel = "auto-memory/scope/reference_acme_b.md"
    a = raw_root / a_rel
    b = raw_root / b_rel
    _write_source(a, "Acme is Series A as of 2024.\n")
    _write_source(b, "Acme is Series B as of 2026.\n", name="Acme B")

    # Stub the Anthropic client to return a contradiction verdict for the
    # a/b claim-pair. Mirrors tests/test_contradictions.py::_fake_client —
    # no network call is made.
    contradiction_payload = (
        '{"detected": true, "conflict_type": "factual", '
        '"members_involved": ["scope/reference_acme_a.md", '
        '"scope/reference_acme_b.md"], '
        '"conflicting_passages": ["Acme is Series A as of 2024.", '
        '"Acme is Series B as of 2026."], '
        '"rationale": "Series A vs Series B for the same company."}'
    )
    stub_client = MagicMock()
    stub_response = MagicMock()
    stub_response.content = [MagicMock(text=contradiction_payload)]
    stub_client.messages.create.return_value = stub_response

    def _members_on_disk() -> list[AutoMemoryFile]:
        """Rebuild the member cluster from whatever survives on disk."""
        return [
            AutoMemoryFile(path=p, origin_scope="scope", memory_type="reference")
            for p in sorted((raw_root / "auto-memory/scope").glob("*.md"))
        ]

    # PRE-ingest: the genuine 2-member cluster WOULD flag. This proves the
    # pair really conflicts and the stub really detects it — independent of
    # any write-back behavior.
    pre = detect_contradictions(_members_on_disk(), client=stub_client)
    assert pre.detected is True

    block = _two_member_block(
        a_rel=a_rel, b_rel=b_rel, answer="correct_a\nSide a is right."
    )
    pending_path.write_text("# Pending Questions\n\n" + block)

    assert ingest_answers(pending_path, raw_root) == 1

    # Write-back enacted ``correct_a``: wrong member (b) deleted, a kept.
    assert a.exists()
    assert not b.exists()

    # POST-ingest: re-run the SAME stub detector over the surviving member
    # set. The conflicting partner is gone, so the cluster can no longer be
    # flagged. This is fix-dependent — with write-back disabled, b would
    # still be on disk, the set would still be [a, b], and the stub would
    # re-flag detected=True (test would fail).
    post = detect_contradictions(_members_on_disk(), client=stub_client)
    assert post.detected is False


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


# ---------------------------------------------------------------------------
# Q4: multi-line description capture
# ---------------------------------------------------------------------------


def test_description_captures_multiple_lines(pending_path: Path) -> None:
    """A **Description**: that spans 3+ lines should survive the parse.

    Prior implementation captured only the first line, silently dropping
    continuation text. Guards Quine Q4.
    """
    content = (
        "# Pending Questions\n\n"
        '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)\n'
        "- [ ] Is Acme still Series A?\n"
        "**Conflict type**: principled\n"
        "**Description**: Line one of description.\n"
        "Line two continues the description.\n"
        "Line three wraps it up.\n"
    )
    pending_path.write_text(content)
    parsed = parse_pending_questions(pending_path)
    assert len(parsed) == 1
    desc = parsed[0].description
    assert "Line one of description." in desc
    assert "Line two continues the description." in desc
    assert "Line three wraps it up." in desc


# ---------------------------------------------------------------------------
# Q5: archive dedup guard
# ---------------------------------------------------------------------------


def test_archive_skips_duplicate_entry(
    pending_path: Path,
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the exact raw_block of an answered item is already in the archive,
    a second ingest must not duplicate it. Guards Quine Q5.
    """
    answered = _block(checkbox="[x]", answer="Confirmed.")
    pending_path.write_text("# Pending Questions\n\n" + answered)

    first = ingest_answers(pending_path, raw_root)
    assert first == 1

    archive_path = pending_path.parent / "_pending_questions_archive.md"
    after_first = archive_path.read_text(encoding="utf-8")
    first_count = after_first.count("Acme Corp")
    assert first_count == 1

    # Simulate the user re-pasting the already-answered block into the
    # primary file (for example after restoring from a backup).
    pending_path.write_text("# Pending Questions\n\n" + answered)
    second = ingest_answers(pending_path, raw_root)
    # The block is still "answered" so it is ingested once for the raw
    # intake step, but the archive dedup guard should catch the duplicate.
    assert second == 1

    after_second = archive_path.read_text(encoding="utf-8")
    second_count = after_second.count("Acme Corp")
    assert (
        second_count == 1
    ), f"archive must not duplicate: before={first_count}, after={second_count}"
    err = capsys.readouterr().err
    assert "skipping duplicate archive entry" in err


# ---------------------------------------------------------------------------
# Q7: id stability invariant (locked on both sides)
# ---------------------------------------------------------------------------


def test_id_stable_across_checkbox_flip(pending_path: Path) -> None:
    """Flipping ``- [ ]`` to ``- [x]`` must not change the question id.

    This is the contract that lets an agent cache the id from
    list_pending_questions and still call resolve_question against it —
    without this invariant, resolve_question would be a race with id churn.
    """
    pending_path.write_text("# Pending Questions\n\n" + _block(checkbox="[ ]"))
    id_before = parse_pending_questions(pending_path)[0].id

    pending_path.write_text("# Pending Questions\n\n" + _block(checkbox="[x]"))
    id_after = parse_pending_questions(pending_path)[0].id

    assert id_before == id_after


def test_id_changes_when_question_edited(pending_path: Path) -> None:
    """Editing the question text itself MUST produce a new id.

    The inverse of test_id_stable_across_checkbox_flip — locks the id
    invariant on both sides so a future refactor can't silently break it.
    """
    pending_path.write_text(
        "# Pending Questions\n\n" + _block(question="Original question?")
    )
    id_before = parse_pending_questions(pending_path)[0].id

    pending_path.write_text(
        "# Pending Questions\n\n" + _block(question="Rephrased question?")
    )
    id_after = parse_pending_questions(pending_path)[0].id

    assert id_before != id_after


# ---------------------------------------------------------------------------
# Q6: structured error codes (happy path)
# ---------------------------------------------------------------------------


def test_resolve_by_id_success_returns_structured_fields(pending_path: Path) -> None:
    pending_path.write_text("# Pending Questions\n\n" + _block())
    items = list_unanswered(pending_path)
    qid = items[0]["id"]
    result = resolve_by_id(pending_path, qid, "Confirmed.")
    assert result["ok"] is True
    assert result["error_code"] is None
    assert result["message"] == "ok"
    assert result["resolved_block"] is not None
    assert "[x]" in result["resolved_block"]
    # Legacy aliases retained for backward compat.
    assert result["block"] == result["resolved_block"]
