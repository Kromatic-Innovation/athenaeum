# SPDX-License-Identifier: Apache-2.0
"""Tests for the unapplied-answer visibility scan (issue athenaeum#2544 AC3/AC4).

``athenaeum decisions answer`` used to tell the caller nothing about
answers already waiting on disk for the same item. These tests exercise
the library functions directly:

- :func:`athenaeum.decision_answers.scan_unapplied_answers` — AC4's
  read-only lookup by decision id or page id.
- :func:`athenaeum.decision_answers.scan_overlapping_merge_proposals` /
  :func:`athenaeum.decision_answers.check_pending_conflicts` — AC3's
  write-path visibility, including the merge-specific "another proposal
  whose sources overlap" case.

CLI-level warn-vs-refuse coverage for the simple same-id case lives in
``tests/test_decisions_answer_coordinate_cli.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.decision_answers import (
    _decision_is_open,
    check_pending_conflicts,
    render_decision_answer,
    scan_overlapping_merge_proposals,
    scan_unapplied_answers,
    write_decision_answer,
)


def _write_answer_file(raw_root: Path, *, name: str, **kwargs: object) -> Path:
    answers_dir = raw_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)
    text = render_decision_answer(resolved_at="2026-11-01T00:00:00+00:00", **kwargs)  # type: ignore[arg-type]
    path = answers_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _seed_coordinate_item(wiki_root: Path) -> str:
    """Queue one real `coordinate` item; returns its decision id."""
    from athenaeum.comparator import CompareOutcome, page_from_text
    from athenaeum.verdict_effects import build_coordinate_request, queue_coordinate_batch

    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / "page-a.md").write_text(
        "---\nname: Page A\n---\nbody\n", encoding="utf-8"
    )
    (wiki_root / "page-b.md").write_text(
        "---\nname: Page B\n---\nbody\n", encoding="utf-8"
    )
    page_a = page_from_text("page-a", "---\nname: Page A\n---\nbody\n")
    page_b = page_from_text("page-b", "---\nname: Page B\n---\nbody\n")
    outcome = CompareOutcome(verdict="underdetermined", missing=["subject"])
    member = {
        "pair_key": "page-a+page-b",
        "request": build_coordinate_request(page_a, page_b, outcome),
        "conflict_type": "ambiguous",
    }
    queue_coordinate_batch([member], wiki_root=wiki_root, config=None)

    from athenaeum.decisions import list_pending_decisions

    return list_pending_decisions(wiki_root)[0]["id"]


def _write_merge_block(
    merges_path: Path, *, target: str, sources: list[Path], resolved: bool = False
) -> None:
    header = "# Pending Merges\n\n" if not merges_path.exists() else ""
    existing = merges_path.read_text(encoding="utf-8") if merges_path.exists() else ""
    checkbox = "[x]" if resolved else "[ ]"
    sources_line = ", ".join(s.name for s in sources)
    sources_block = "\n".join(f"- {s}" for s in sources)
    block = (
        f'## [2026-06-20] Merge: "{target}"\n'
        f"- {checkbox} Approve this merge? Sources: {sources_line}\n"
        "**Rationale**: overlap\n"
        "**Sources**:\n"
        f"{sources_block}\n"
        "**Confidence**: 0.84\n"
        "**Draft**:\n```markdown\nx\n```\n"
    )
    if resolved:
        block += "**Decision**: approve\n"
    sep = "\n---\n\n" if existing.strip() else ""
    merges_path.write_text(header + existing + sep + block, encoding="utf-8")


class TestScanUnappliedAnswersByDecisionId:
    def test_finds_an_unapplied_answer_for_the_same_id(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = _seed_coordinate_item(wiki_root)

        verdict = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )
        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=decision_id,
            decision_type="coordinate",
            verdict=verdict,
        )

        found = scan_unapplied_answers(raw_root, wiki_root, decision_id=decision_id)
        assert len(found) == 1
        assert found[0].decision_id == decision_id
        assert found[0].decision_type == "coordinate"
        assert found[0].verdict == verdict
        assert found[0].resolved_at

    def test_two_answer_files_for_one_id_both_surface(self, tmp_path: Path) -> None:
        """Issue athenaeum#2544 AC3's own "Test with two answer files for
        one id" — this is the plain file-level proof that both are
        visible; the CLI warn/refuse behavior is covered separately."""
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = _seed_coordinate_item(wiki_root)
        verdict = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        _write_answer_file(
            raw_root,
            name="20261101T000000Z-coordinate-1.md",
            decision_id=decision_id,
            decision_type="coordinate",
            verdict=verdict,
        )
        _write_answer_file(
            raw_root,
            name="20261101T000100Z-coordinate-2.md",
            decision_id=decision_id,
            decision_type="coordinate",
            verdict=verdict,
        )

        found = scan_unapplied_answers(raw_root, wiki_root, decision_id=decision_id)
        assert len(found) == 2

    def test_applied_decision_no_longer_shows(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = _seed_coordinate_item(wiki_root)
        verdict = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )
        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=decision_id,
            decision_type="coordinate",
            verdict=verdict,
        )

        from athenaeum.decision_answers import apply_decision_answers
        from athenaeum.runlock import RunLock

        with RunLock(wiki_root) as lock:
            report = apply_decision_answers(wiki_root, raw_root, lock=lock)
        assert report.applied == 1

        # The file is still on disk (never deleted) but is now APPLIED, so
        # it must not be reported as unapplied any more.
        found = scan_unapplied_answers(raw_root, wiki_root, decision_id=decision_id)
        assert found == []

    def test_no_matching_id_returns_empty(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        wiki_root.mkdir(parents=True, exist_ok=True)
        found = scan_unapplied_answers(raw_root, wiki_root, decision_id="no-such-id")
        assert found == []


class TestScanUnappliedAnswersByPage:
    def test_coordinate_answer_matches_either_page_in_its_pair(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = _seed_coordinate_item(wiki_root)
        verdict = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )
        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=decision_id,
            decision_type="coordinate",
            verdict=verdict,
        )

        for page_id in ("page-a", "page-b"):
            found = scan_unapplied_answers(raw_root, wiki_root, page_id=page_id)
            assert len(found) == 1
            assert found[0].decision_id == decision_id

        assert scan_unapplied_answers(raw_root, wiki_root, page_id="page-c") == []

    def test_requires_exactly_one_of_decision_id_or_page_id(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(ValueError):
            scan_unapplied_answers(tmp_path / "raw", tmp_path / "wiki")
        with pytest.raises(ValueError):
            scan_unapplied_answers(
                tmp_path / "raw", tmp_path / "wiki", decision_id="a", page_id="b"
            )


class TestMergeSourceOverlap:
    def test_overlapping_merge_proposal_is_reported(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        wiki_root.mkdir(parents=True, exist_ok=True)
        shared = wiki_root / "shared-page.md"
        shared.write_text("---\nname: Shared\n---\nbody\n", encoding="utf-8")
        other = wiki_root / "other-page.md"
        other.write_text("---\nname: Other\n---\nbody\n", encoding="utf-8")

        merges_path = wiki_root / "_pending_merges.md"
        _write_merge_block(merges_path, target="Target A", sources=[shared, other])
        _write_merge_block(merges_path, target="Target B", sources=[shared])

        from athenaeum.pending_merges import parse_pending_merges

        pms = parse_pending_merges(merges_path)
        assert len(pms) == 2
        pm_a, pm_b = pms

        # Write an answer file for proposal A.
        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=pm_a.id,
            decision_type="merge",
            verdict="approve",
        )

        # Proposal B's own sources overlap A's (the shared page) — must
        # be reported when someone is about to answer B.
        overlapping = scan_overlapping_merge_proposals(
            raw_root, wiki_root, decision_id=pm_b.id, sources=pm_b.sources
        )
        assert len(overlapping) == 1
        assert overlapping[0].decision_id == pm_a.id

        # check_pending_conflicts (the write path's own call) surfaces the
        # same thing when answering B.
        conflicts = check_pending_conflicts(
            raw_root, wiki_root, decision_id=pm_b.id, decision_type="merge"
        )
        assert any(c.decision_id == pm_a.id for c in conflicts)

    def test_non_overlapping_merge_proposal_is_not_reported(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        wiki_root.mkdir(parents=True, exist_ok=True)
        page_1 = wiki_root / "page-1.md"
        page_1.write_text("---\nname: One\n---\nbody\n", encoding="utf-8")
        page_2 = wiki_root / "page-2.md"
        page_2.write_text("---\nname: Two\n---\nbody\n", encoding="utf-8")

        merges_path = wiki_root / "_pending_merges.md"
        _write_merge_block(merges_path, target="Target A", sources=[page_1])
        _write_merge_block(merges_path, target="Target B", sources=[page_2])

        from athenaeum.pending_merges import parse_pending_merges

        pm_a, pm_b = parse_pending_merges(merges_path)

        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=pm_a.id,
            decision_type="merge",
            verdict="approve",
        )

        overlapping = scan_overlapping_merge_proposals(
            raw_root, wiki_root, decision_id=pm_b.id, sources=pm_b.sources
        )
        assert overlapping == []


class TestDecisionIsOpenArchivalPolarity:
    """`_decision_is_open`'s `question`/`coordinate` branch used to treat
    an `id_not_found` lookup against `_pending_questions.md` as OPEN —
    but `id_not_found` is also exactly what a FULLY ARCHIVED decision
    looks like once `ingest_answers` moves its block out to
    `_pending_questions_archive.md` (confirmed empirically: `resolve_by_id`
    only flips the checkbox in place; the archive move is a separate step
    in the same `ingest-answers` tick). Getting this polarity wrong means
    a long-settled decision's stale answer file would warn/refuse
    forever."""

    @staticmethod
    def _seed_and_fully_archive_question(wiki_root: Path, raw_root: Path) -> str:
        """A real `question` decision, answered and taken all the way
        through archival (apply_decision_answers + ingest_answers), so its
        id is no longer in `_pending_questions.md` at all. Returns the id."""
        from athenaeum.answers import ingest_answers, parse_pending_questions
        from athenaeum.decision_answers import apply_decision_answers
        from athenaeum.runlock import RunLock

        wiki_root.mkdir(parents=True, exist_ok=True)
        raw_root.mkdir(parents=True, exist_ok=True)
        (raw_root / "sessions").mkdir(parents=True, exist_ok=True)
        (raw_root / "sessions" / "x.md").write_text(
            "---\nname: Session X\n---\nAcme Corp is Series A.\n", encoding="utf-8"
        )
        pending_path = wiki_root / "_pending_questions.md"
        block = (
            '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)\n'
            "- [ ] Is Acme still Series A?\n"
            "**Conflict type**: principled\n"
            "**Description**: two conflicting statements.\n"
        )
        pending_path.write_text("# Pending Questions\n\n" + block, encoding="utf-8")
        decision_id = parse_pending_questions(pending_path)[0].id

        write_decision_answer(
            raw_root,
            wiki_root=wiki_root,
            decision_id=decision_id,
            decision_type="question",
            verdict="Resolved: Acme is Series A.",
        )
        with RunLock(wiki_root) as lock:
            report = apply_decision_answers(wiki_root, raw_root, lock=lock)
        assert report.applied == 1

        count = ingest_answers(pending_path, raw_root, client=None, config=None)
        assert count == 1

        # Sanity: really gone from the live queue, really present in the
        # archive -- otherwise this fixture isn't testing what it claims.
        assert decision_id not in {
            pq.id for pq in parse_pending_questions(pending_path)
        }
        archive_path = wiki_root / "_pending_questions_archive.md"
        assert archive_path.exists()
        assert decision_id in {pq.id for pq in parse_pending_questions(archive_path)}

        return decision_id

    def test_archived_question_is_closed_not_open(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = self._seed_and_fully_archive_question(wiki_root, raw_root)

        assert _decision_is_open(wiki_root, "question", decision_id) is False

    def test_archived_question_answer_file_does_not_surface_as_pending_conflict(
        self, tmp_path: Path
    ) -> None:
        """The integration version of the same bug: the answer file
        `apply_decision_answers` already wrote for the now-archived
        decision (never deleted -- see module docstring) must not be
        reported as an unapplied/pending conflict any more."""
        wiki_root = tmp_path / "wiki"
        raw_root = tmp_path / "raw"
        decision_id = self._seed_and_fully_archive_question(wiki_root, raw_root)

        found = scan_unapplied_answers(raw_root, wiki_root, decision_id=decision_id)
        assert found == []

        conflicts = check_pending_conflicts(
            raw_root, wiki_root, decision_id=decision_id, decision_type="question"
        )
        assert conflicts == []

    def test_open_question_is_still_open(self, tmp_path: Path) -> None:
        """The counterpart: a question that has NOT been answered/archived
        must still be reported open (this is the behavior the fix must
        not regress)."""
        from athenaeum.answers import parse_pending_questions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True, exist_ok=True)
        pending_path = wiki_root / "_pending_questions.md"
        block = (
            '## [2026-05-01] Entity: "Other Co" (from sessions/y.md)\n'
            "- [ ] Is Other Co still private?\n"
            "**Conflict type**: principled\n"
            "**Description**: unresolved.\n"
        )
        pending_path.write_text("# Pending Questions\n\n" + block, encoding="utf-8")
        decision_id = parse_pending_questions(pending_path)[0].id

        assert _decision_is_open(wiki_root, "question", decision_id) is True

    def test_never_seen_anywhere_stays_on_the_safe_side(self, tmp_path: Path) -> None:
        """Genuinely unknown (no live entry, no archive entry, not even a
        `_pending_questions.md` file) is ambiguous, not proven closed --
        must stay reported OPEN per this function's own safe-side rule."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True, exist_ok=True)
        assert _decision_is_open(wiki_root, "question", "totally-unknown-id") is True
