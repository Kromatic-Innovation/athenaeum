# SPDX-License-Identifier: Apache-2.0
"""The newest answer file for a decision is the one that applies
(issue athenaeum#2514 AC5).

``apply_decision_answers`` walked ``raw/answers/*.md`` oldest-first and
dispatched EVERY conformant file, so the first one to land settled the
decision and the rest bounced off the resolver's ``already_resolved``
guard. An older answer therefore beat a newer one: on the live deployment
a 2026-10-14 reject overrode a 2026-11-12 human approval of the same merge
on every nightly tick, until the old file was moved out of the walk by
hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum.atomic_io import atomic_write_text
from athenaeum.decision_answers import (
    apply_decision_answers,
    render_decision_answer,
    write_decision_answer,
)
from athenaeum.pending_merges import parse_pending_merges, write_pending_merge


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw


def _seed_merge(wiki_root: Path) -> str:
    for name in ("alpha", "beta"):
        (wiki_root / f"feedback_{name}.md").write_text(
            f"---\nname: {name}\ntype: feedback\n---\nbody\n", encoding="utf-8"
        )
    merges_path = wiki_root / "_pending_merges.md"
    write_pending_merge(
        merges_path,
        merge_target_name="merged-topic",
        sources=[
            str(wiki_root / "feedback_alpha.md"),
            str(wiki_root / "feedback_beta.md"),
        ],
        rationale="similar topic",
        draft_merged_body="merged body\n",
        confidence=0.9,
    )
    return parse_pending_merges(merges_path)[0].id


def _answer_file(
    raw_root: Path, *, when: str, decision_id: str, verdict: str, note: str = ""
) -> Path:
    """One answer file named exactly as ``write_decision_answer`` names them,
    but at a caller-chosen instant -- the two files in the live incident
    were written four weeks apart."""
    answers = raw_root / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    path = answers / f"{when.replace('-', '').replace(':', '')}-merge-{decision_id}.md"
    atomic_write_text(
        path,
        render_decision_answer(
            decision_id=decision_id,
            decision_type="merge",
            verdict=verdict,
            note=note,
            resolved_at=when,
        ),
    )
    return path


class TestSameSecondCollision:
    """Seer review finding on PR athenaeum#2571: ``write_decision_answer``
    suffixes a same-second collision ``-1``, and in ASCII ``-`` (0x2D)
    sorts before ``.`` (0x2E) -- so a raw filename sort puts the SUFFIXED
    (newer) file first and the unsuffixed (older) one last. Under
    "last in the walk is newest" that resolves two same-second answers to
    the OLDER verdict: AC5 inverted.

    Driven through ``write_decision_answer`` itself rather than
    hand-built filenames, so a future change to the suffix scheme is
    caught here instead of silently passing.
    """

    @staticmethod
    def _freeze(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
        """Pin ``write_decision_answer``'s clock so both files land in the
        same second by construction, never by racing the test runner."""
        import athenaeum.decision_answers as module

        class _FrozenDatetime:
            @staticmethod
            def now(tz: object = None) -> datetime:
                return when

        monkeypatch.setattr(module, "datetime", _FrozenDatetime)

    def test_the_second_answer_of_the_same_second_wins(
        self, wiki_root: Path, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mid = _seed_merge(wiki_root)
        self._freeze(monkeypatch, datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc))
        first = write_decision_answer(
            raw_root,
            decision_id=mid,
            decision_type="merge",
            verdict="reject",
            note="typed the wrong verdict",
            wiki_root=wiki_root,
        )
        second = write_decision_answer(
            raw_root,
            decision_id=mid,
            decision_type="merge",
            verdict="approve",
            note="corrected, seconds later",
            wiki_root=wiki_root,
        )
        monkeypatch.undo()
        # The collision really happened, and raw ASCII sort really inverts it.
        assert second.name == f"{first.stem}-1.md"
        assert sorted([first.name, second.name]) == [second.name, first.name]

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 1
        text = (wiki_root / "_pending_merges.md").read_text(encoding="utf-8")
        assert "**Decision**: approve" in text
        assert "**Decision**: reject" not in text
        superseded = next(o for o in report.outcomes if not o.applied)
        assert superseded.path == first
        assert superseded.error_code == "superseded"
        assert superseded.superseded_by == second.name

    def test_a_third_same_second_answer_still_wins(
        self, wiki_root: Path, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-2`` must beat ``-1``, which must beat the unsuffixed file:
        the index orders NUMERICALLY, not as text."""
        mid = _seed_merge(wiki_root)
        self._freeze(monkeypatch, datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc))
        for verdict in ("reject", "reject", "approve"):
            last = write_decision_answer(
                raw_root,
                decision_id=mid,
                decision_type="merge",
                verdict=verdict,
                wiki_root=wiki_root,
            )
        monkeypatch.undo()
        assert last.name.endswith("-2.md")

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 2
        assert (wiki_root / "merged-topic.md").exists()

    def test_an_all_digit_decision_id_is_not_read_as_a_collision_index(
        self, wiki_root: Path, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A decision id is 12 hex characters and can be all digits. A
        trailing-``-digits`` regex would read the id ITSELF as the collision
        index; the ``-{type}-{id}`` infix anchor does not."""
        self._freeze(monkeypatch, datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc))
        first = write_decision_answer(
            raw_root,
            decision_id="123456789012",
            decision_type="merge",
            verdict="reject",
            wiki_root=wiki_root,
        )
        second = write_decision_answer(
            raw_root,
            decision_id="123456789012",
            decision_type="merge",
            verdict="approve",
            wiki_root=wiki_root,
        )
        monkeypatch.undo()
        assert second.name == f"{first.stem}-1.md"

        from athenaeum.decision_answers import _answer_order_key, _load_decision_answer

        keys = [
            _answer_order_key(path, _load_decision_answer(path))
            for path in (first, second)
        ]
        assert keys[0] < keys[1]
        assert keys[0][1] == 0 and keys[1][1] == 1


class TestNewestAnswerWins:
    def test_an_old_reject_does_not_override_a_newer_approve(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """The live incident, reproduced: the counter-example is a tick that
        re-rejects the merge the operator approved four weeks later."""
        mid = _seed_merge(wiki_root)
        old = _answer_file(
            raw_root,
            when="2026-10-14T12:00:00Z",
            decision_id=mid,
            verdict="reject",
            note="not the same thing",
        )
        new = _answer_file(
            raw_root,
            when="2026-11-12T12:00:00Z",
            decision_id=mid,
            verdict="approve",
            note="same thing after all",
        )
        assert sorted(p.name for p in (raw_root / "answers").glob("*.md")) == [
            old.name,
            new.name,
        ]

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 1
        text = (wiki_root / "_pending_merges.md").read_text(encoding="utf-8")
        assert "**Decision**: approve" in text
        assert "**Decision**: reject" not in text
        assert (wiki_root / "merged-topic.md").exists()

    def test_the_superseded_file_is_recorded_and_kept(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        mid = _seed_merge(wiki_root)
        old = _answer_file(
            raw_root, when="2026-10-14T12:00:00Z", decision_id=mid, verdict="reject"
        )
        new = _answer_file(
            raw_root, when="2026-11-12T12:00:00Z", decision_id=mid, verdict="approve"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        by_path = {o.path: o for o in report.outcomes}
        assert by_path[new].applied is True
        superseded = by_path[old]
        assert superseded.applied is False
        assert superseded.error_code == "superseded"
        assert superseded.superseded_by == new.name
        assert superseded.decision_id == mid
        # Never deleted -- a skipped answer file is its own audit trail.
        assert old.exists()

    def test_only_the_newest_of_three_applies(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        mid = _seed_merge(wiki_root)
        for when, verdict in (
            ("2026-10-14T12:00:00Z", "reject"),
            ("2026-10-20T12:00:00Z", "approve"),
            ("2026-11-12T12:00:00Z", "reject"),
        ):
            _answer_file(raw_root, when=when, decision_id=mid, verdict=verdict)

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 2
        assert [o.error_code for o in report.outcomes if not o.applied] == [
            "superseded",
            "superseded",
        ]
        text = (wiki_root / "_pending_merges.md").read_text(encoding="utf-8")
        assert "**Decision**: reject" in text

    def test_answers_for_different_decisions_all_apply(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        """Supersession is per decision id -- an unrelated answer in the
        same batch is untouched."""
        first = _seed_merge(wiki_root)
        (wiki_root / "feedback_gamma.md").write_text(
            "---\nname: gamma\ntype: feedback\n---\nbody\n", encoding="utf-8"
        )
        (wiki_root / "feedback_delta.md").write_text(
            "---\nname: delta\ntype: feedback\n---\nbody\n", encoding="utf-8"
        )
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="other-topic",
            sources=[
                str(wiki_root / "feedback_gamma.md"),
                str(wiki_root / "feedback_delta.md"),
            ],
            rationale="also similar",
            draft_merged_body="other body\n",
            confidence=0.9,
        )
        second = next(
            pm.id for pm in parse_pending_merges(merges_path) if pm.id != first
        )
        _answer_file(
            raw_root, when="2026-11-12T12:00:00Z", decision_id=first, verdict="reject"
        )
        _answer_file(
            raw_root, when="2026-11-12T12:00:01Z", decision_id=second, verdict="reject"
        )

        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 2
        assert report.skipped == 0
