"""Issue athenaeum#2705: an answered ``classification_failed`` question applies
after the raw note it was raised from has been consumed.

The athenaeum#1126 address gate raises the question against a raw note the
librarian removes on its normal schedule. Before this fix every answer sat in
``held — no source ref resolved`` forever. Now the block's retained
statement copy plus the answer are re-submitted as raw intake the next
compile reads, a dismissal writes nothing, and an answer with nothing left to
apply is reported as unrecoverable instead of held.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.answers import (
    IngestAnswersReport,
    ingest_answers,
    parse_pending_questions,
    resolve_by_id,
)
from athenaeum.intake import discover_raw_files
from athenaeum.models import EscalationItem, parse_frontmatter
from athenaeum.tiers import tier4_escalate

STATEMENT = (
    "Dana Example wrote from dana@example.com about the Q3 pilot.\n"
    "She is the program lead at Example Labs."
)
RAW_REF = "voltaire/20260707T000000Z-b071d7ff.md"


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    raw = tmp_path / "raw"
    raw.mkdir()
    return wiki / "_pending_questions.md", raw


def _raise_question(pending: Path, raw: Path) -> str:
    """Raise a classification_failed question exactly as the librarian does."""
    note = raw / RAW_REF
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(STATEMENT + "\n", encoding="utf-8")
    item = EscalationItem(
        raw_ref=RAW_REF,
        entity_name="dana@example.com",
        conflict_type="classification_failed",
        description=(
            "This statement's subject ('dana@example.com') is an email address "
            "that resolves to no known entity (reason: no-match); no "
            "address-named page was created (athenaeum#1126). The statement "
            f"text follows so the fact is not lost:\n\n{note.read_text()[:2000]}"
        ),
    )
    tier4_escalate([item], pending)
    (pq,) = parse_pending_questions(pending)
    assert pq.conflict_type == "classification_failed"
    # AC1: the content needed to apply an answer is retained in the record.
    assert "program lead at Example Labs" in pq.observation
    return pq.id


def _reingest_notes(raw: Path) -> list[Path]:
    d = raw / "question-answers"
    return sorted(d.glob("*.md")) if d.is_dir() else []


class TestAnswerAppliesAfterRawConsumed:
    def test_raise_consume_answer_reingests(self, roots: tuple[Path, Path]) -> None:
        pending, raw = roots
        qid = _raise_question(pending, raw)
        # The librarian consumes the raw note on its normal schedule.
        (raw / RAW_REF).unlink()

        answer = "This address belongs to Dana Example; create a person page for her."
        assert resolve_by_id(pending, qid, answer)["ok"]
        report = IngestAnswersReport()
        assert ingest_answers(pending, raw, report=report) == 1

        # AC2: no longer held.
        assert report.held == 0
        assert report.reingested == 1
        assert "no source ref resolved" not in pending.read_text()
        assert parse_pending_questions(pending) == []
        archive = (pending.parent / "_pending_questions_archive.md").read_text()
        assert "**Write-back**: reingested — retained copy + answer" in archive

        # The re-ingest note carries the statement AND the ruling, and the
        # librarian's own discovery picks it up as intake (raw/answers/ is
        # skipped by design, so it must not land there).
        (note,) = _reingest_notes(raw)
        meta, body = parse_frontmatter(note.read_text())
        assert meta["source"] == f"pending-question:{qid}"
        assert meta["original_source"] == f"raw/{RAW_REF}"
        assert "program lead at Example Labs" in body
        assert answer in body
        discovered = {f.path for f in discover_raw_files(raw)}
        assert note in discovered
        (rf,) = [f for f in discover_raw_files(raw) if f.path == note]
        assert rf.uuid8 == qid[:8]  # canonical `<ts>-<8hex>.md` name

    def test_rerun_is_idempotent(self, roots: tuple[Path, Path]) -> None:
        pending, raw = roots
        qid = _raise_question(pending, raw)
        (raw / RAW_REF).unlink()
        resolve_by_id(pending, qid, "Belongs to Dana Example.")
        ingest_answers(pending, raw)
        assert ingest_answers(pending, raw) == 0
        assert len(_reingest_notes(raw)) == 1

    def test_crash_before_pending_rewrite_does_not_duplicate(
        self, roots: tuple[Path, Path]
    ) -> None:
        pending, raw = roots
        qid = _raise_question(pending, raw)
        (raw / RAW_REF).unlink()
        resolve_by_id(pending, qid, "Belongs to Dana Example.")
        before = pending.read_text()
        ingest_answers(pending, raw)
        # Simulate a run that wrote the note but died before the pending
        # file was rewritten: restore the answered block.
        pending.write_text(before)
        (pending.parent / "_pending_questions_archive.md").unlink()
        assert ingest_answers(pending, raw) == 1
        assert len(_reingest_notes(raw)) == 1

    @pytest.mark.parametrize(
        "answer",
        [
            "dismiss: not a person - do not create a person record",
            "not_a_conflict",
            "Skip.",
        ],
    )
    def test_dismissal_writes_nothing(
        self, roots: tuple[Path, Path], answer: str
    ) -> None:
        pending, raw = roots
        qid = _raise_question(pending, raw)
        (raw / RAW_REF).unlink()
        resolve_by_id(pending, qid, answer)
        report = IngestAnswersReport()
        assert ingest_answers(pending, raw, report=report) == 1
        assert report.reingest_dismissed == 1
        assert report.held == 0
        assert _reingest_notes(raw) == []
        archive = (pending.parent / "_pending_questions_archive.md").read_text()
        assert "**Write-back**: dismissed" in archive
        # The statement is kept in the archive, so it can still be re-submitted.
        assert "program lead at Example Labs" in archive

    def test_raw_note_still_present_also_reingests(
        self, roots: tuple[Path, Path]
    ) -> None:
        pending, raw = roots
        qid = _raise_question(pending, raw)
        resolve_by_id(pending, qid, "Belongs to Dana Example.")
        ingest_answers(pending, raw)
        assert len(_reingest_notes(raw)) == 1
        # The source raw note is never annotated by this class.
        assert (raw / RAW_REF).read_text() == STATEMENT + "\n"


def _held_block(*, observation: bool, answer: str = "Belongs to Dana Example.") -> str:
    """An answered block an older run already stamped ``held``."""
    obs = (
        "**Observation**:\n```text\n" + STATEMENT + "\n```\n" if observation else ""
    )
    return (
        f'## [2026-09-03] Entity: "dana@example.com" (from {RAW_REF})\n'
        "- [x] This statement's subject ('dana@example.com') is an email address "
        "that resolves to no known entity (reason: no-match); no address-named "
        "page was created.\n\n"
        "**Conflict type**: classification_failed\n"
        "**Description**: This statement's subject ('dana@example.com') is an "
        "email address that resolves to no known entity (reason: no-match).\n"
        f"{obs}"
        f"\n{answer}\n\n"
        f"**Write-back**: held — no source ref resolved ({RAW_REF}) as of "
        '2026-10-15T08:15:16Z; restore the source or change "held" to '
        '"waived" to archive without a write\n'
    )


class TestHeldAnswersReapply:
    def test_held_with_retained_copy_is_reapplied(
        self, roots: tuple[Path, Path]
    ) -> None:
        pending, raw = roots
        pending.write_text("# Pending Questions\n\n" + _held_block(observation=True))
        report = IngestAnswersReport()
        assert ingest_answers(pending, raw, report=report) == 1
        assert report.reingested == 1
        assert report.held == 0
        (note,) = _reingest_notes(raw)
        body = note.read_text()
        assert "program lead at Example Labs" in body
        assert "Belongs to Dana Example." in body
        assert parse_pending_questions(pending) == []
        # The provenance file was written by the earlier run; not rewritten.
        assert not (raw / "answers").exists()

    def test_held_without_retained_copy_is_reported_unrecoverable(
        self, roots: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        pending, raw = roots
        pending.write_text("# Pending Questions\n\n" + _held_block(observation=False))
        report = IngestAnswersReport()
        with caplog.at_level("WARNING", logger="athenaeum.answers"):
            assert ingest_answers(pending, raw, report=report) == 1
        assert report.unrecoverable == 1
        assert report.held == 0
        assert _reingest_notes(raw) == []
        # Never silently held: archived with an explicit stamp and a warning.
        assert parse_pending_questions(pending) == []
        archive = (pending.parent / "_pending_questions_archive.md").read_text()
        assert "**Write-back**: unrecoverable — no retained copy" in archive
        assert any("unrecoverable" in r.getMessage() for r in caplog.records)

    def test_held_without_copy_but_source_restored_is_reapplied(
        self, roots: tuple[Path, Path]
    ) -> None:
        pending, raw = roots
        note = raw / RAW_REF
        note.parent.mkdir(parents=True)
        note.write_text("---\nsource: api:mail\n---\n" + STATEMENT + "\n")
        pending.write_text("# Pending Questions\n\n" + _held_block(observation=False))
        report = IngestAnswersReport()
        ingest_answers(pending, raw, report=report)
        assert report.reingested == 1
        (reingest,) = _reingest_notes(raw)
        text = reingest.read_text()
        assert "program lead at Example Labs" in text
        assert "source: api:mail" not in text  # original frontmatter dropped

    def test_waived_held_block_archives_without_reingest(
        self, roots: tuple[Path, Path]
    ) -> None:
        pending, raw = roots
        block = _held_block(observation=True).replace(
            "**Write-back**: held", "**Write-back**: waived"
        )
        pending.write_text("# Pending Questions\n\n" + block)
        report = IngestAnswersReport()
        assert ingest_answers(pending, raw, report=report) == 1
        assert report.waived == 1
        assert _reingest_notes(raw) == []


def test_cli_summary_names_reingest_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from athenaeum.cli import main

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (tmp_path / "raw").mkdir()
    (wiki / "_pending_questions.md").write_text(
        "# Pending Questions\n\n"
        + _held_block(observation=True)
        + "\n---\n\n"
        + _held_block(observation=False).replace("2026-09-03", "2026-09-04")
    )
    assert main(["ingest-answers", "--path", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "0 held" in out
    assert "1 re-ingested, 0 dismissed, 1 unrecoverable." in out
