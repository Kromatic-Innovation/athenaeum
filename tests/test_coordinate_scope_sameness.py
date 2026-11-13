# SPDX-License-Identifier: Apache-2.0
"""Tests for the scope-sameness judge issue athenaeum#2515 adds to the
coordinate-answer apply path (:func:`athenaeum.decision_answers._apply_coordinate_answer`).

Operator ruling: answering a coordinate item's ``subject`` dimension with the
SAME value on both sides used to leave ``scope`` ambiguous for the pair --
resolved only by a LATER, LLM-backed comparator sweep, which then raised a
FRESH "same scope?" coordinate item for the SAME pair a day after the human
already answered once. This module tests the fix: one model judgement of
scope sameness, made in the SAME apply pass.

Deliberately a separate test module from ``tests/test_coordinate_answer_loop.py``
-- issue athenaeum#2544 is a sibling lane refactoring that file's VALIDATION-half
tests of the same apply function; keeping this fix's tests in their own file
avoids a merge collision over shared lines.

No live LLM call anywhere in this file (issue athenaeum#2515 AC5): every
client is either ``None`` or a ``MagicMock`` mirroring the Anthropic SDK's
response shape, matching ``tests/test_comparator.py``'s own convention.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.answers import parse_pending_questions
from athenaeum.comparator import (
    VERDICT_UNDERDETERMINED,
    CompareOutcome,
    page_from_path,
)
from athenaeum.decision_answers import (
    ScopeSameness,
    apply_decision_answers,
    write_decision_answer,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.runlock import RunLock
from athenaeum.verdict_effects import build_coordinate_request, queue_coordinate_batch
from athenaeum.verdicts import get_verdict_status, make_pair_key


def _write_page(
    path: Path,
    *,
    name: str,
    body: str,
    claimed_scope: str | None = None,
    recorded_at: str | None = None,
) -> None:
    meta: dict[str, object] = {"name": name, "type": "feedback"}
    if claimed_scope is not None:
        meta["claimed_scope"] = claimed_scope
    if recorded_at is not None:
        meta["recorded_at"] = recorded_at
    path.write_text(render_frontmatter(meta) + f"\n{body}\n", encoding="utf-8")


def _seed_same_subject_scope_pending(
    wiki_root: Path,
    *,
    scope_a: str | None,
    scope_b: str | None,
    recorded_a: str = "2026-01-01",
    recorded_b: str = "2026-06-01",
) -> tuple:
    """Two pages, no ``subject`` yet, each with its own ``claimed_scope`` --
    and ONE already-queued coordinate item asking about ``subject`` only
    (mirroring the real bug: the human is asked about subject, answers it,
    and scope was never part of what they were asked)."""
    path_a = wiki_root / "page-a.md"
    path_b = wiki_root / "page-b.md"
    _write_page(
        path_a,
        name="Page A",
        body="A claim about the thing under discussion.",
        claimed_scope=scope_a,
        recorded_at=recorded_a,
    )
    _write_page(
        path_b,
        name="Page B",
        body="Another claim about the thing under discussion.",
        claimed_scope=scope_b,
        recorded_at=recorded_b,
    )

    page_a = page_from_path(path_a)
    page_b = page_from_path(path_b)
    pair_key = make_pair_key(page_a.id, page_b.id)

    member = {
        "pair_key": pair_key,
        "request": build_coordinate_request(
            page_a, page_b, CompareOutcome(verdict=VERDICT_UNDERDETERMINED, missing=["subject"])
        ),
        "conflict_type": "ambiguous",
    }
    result = queue_coordinate_batch([member], wiki_root=wiki_root, config=None)
    assert result.action == "queued"

    pending_path = wiki_root / "_pending_questions.md"
    questions = parse_pending_questions(pending_path)
    coordinate_items = [q for q in questions if q.decision_kind == "coordinate"]
    assert len(coordinate_items) == 1
    pq = coordinate_items[0]
    return path_a, path_b, page_a.id, page_b.id, pair_key, pq


def _same_subject_verdict(pair_key: str, id_a: str, id_b: str) -> str:
    return json.dumps(
        {
            "answers": [
                {
                    "pair": pair_key,
                    "dimensions": {"subject": {id_a: "alice", id_b: "alice"}},
                }
            ]
        }
    )


def _scope_client(sameness: str) -> MagicMock:
    """A MagicMock mirroring the Anthropic SDK's response shape, reporting
    the scope-sameness judge's verdict -- same convention
    ``tests/test_comparator.py``'s ``_fake_client`` uses for ``content_relation``."""
    client = MagicMock()
    response = MagicMock()
    response.content = [
        MagicMock(text=json.dumps({"scope_sameness": sameness, "rationale": "test"}))
    ]
    client.messages.create.return_value = response
    return client


def _raising_client() -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    return client


# ---------------------------------------------------------------------------
# AC1: a clear "same" judgement writes scope, raises no item.
# ---------------------------------------------------------------------------


class TestClearSameJudgementWrites:
    def test_same_subject_same_scope_judgement_writes_scope_and_raises_no_item(
        self, tmp_path: Path
    ) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="job hunt automation, redesign phase two",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            client = _scope_client(ScopeSameness.SAME)
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

        assert report.applied == 1
        client.messages.create.assert_called_once()

        # Both pages now carry the SAME claimed_scope -- the OLDER page's
        # (page A, earlier recorded_at) existing value, per the operator's
        # own evidence ("the human chose the older page's scope").
        meta_a, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        assert meta_a["claimed_scope"] == "the job-hunt automation redesign"
        assert meta_b["claimed_scope"] == "the job-hunt automation redesign"

        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        # Only the ORIGINAL subject item exists -- no new scope item raised.
        assert len(coordinate_items) == 1
        assert coordinate_items[0].id == pq.id
        assert coordinate_items[0].answered is True


# ---------------------------------------------------------------------------
# AC2: an unclear judgement raises a scope item NOW; the next tick raises
# no further item for the pair.
# ---------------------------------------------------------------------------


class TestUnclearJudgementRaisesOnceOnly:
    def test_unclear_judgement_raises_scope_item_same_pass_and_next_tick_is_a_noop(
        self, tmp_path: Path
    ) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="a newsletter subject-line calibration pass",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            client = _scope_client(ScopeSameness.UNCLEAR)
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

            full_pair_key = make_pair_key(id_a, id_b)
            status_right_after = get_verdict_status(tmp_path, full_pair_key)

        assert report.applied == 1
        client.messages.create.assert_called_once()

        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        # The original subject item, now answered, PLUS exactly one fresh
        # scope item -- raised in THIS SAME pass, not on a later tick.
        assert len(coordinate_items) == 2
        original = next(q for q in coordinate_items if q.id == pq.id)
        assert original.answered is True
        scope_item = next(q for q in coordinate_items if q.id != pq.id)
        assert scope_item.answered is False

        # The pair's own verdict is DECIDED and FRESH immediately -- this is
        # exactly what makes "the next tick raises no further item" true:
        # a later LLM-backed comparator sweep's own `record_comparison`
        # checks `get_verdict_status` FIRST and skips re-comparing (and
        # therefore re-queueing) a fresh pair.
        assert status_right_after["decided"] is True
        assert status_right_after["fresh"] is True
        assert status_right_after["verdict"] == VERDICT_UNDERDETERMINED

        # Simulate the next tick: a REAL comparator sweep, with a real
        # (conflicting) client, over the SAME pair. It must be a no-op --
        # no second scope item, no second ledger entry.
        from athenaeum.comparator import record_comparison

        conflicting_client = MagicMock()
        conflicting_response = MagicMock()
        conflicting_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "content_relation": "conflicting",
                        "conflicting_passages": ["x", "y"],
                        "predicate_a": "p-a",
                        "predicate_b": "p-b",
                        "rationale": "test",
                    }
                )
            )
        ]
        conflicting_client.messages.create.return_value = conflicting_response

        with RunLock(tmp_path) as lock2:
            next_tick = record_comparison(
                tmp_path,
                page_from_path(path_a),
                page_from_path(path_b),
                client=conflicting_client,
                lock=lock2,
            )
        assert next_tick["skipped"] == "fresh"
        conflicting_client.messages.create.assert_not_called()

        coordinate_items_after = [
            q
            for q in parse_pending_questions(pending_path)
            if q.decision_kind == "coordinate"
        ]
        assert len(coordinate_items_after) == 2  # unchanged -- no new item


# ---------------------------------------------------------------------------
# AC3: each written scope value records MODEL vs HUMAN provenance.
# ---------------------------------------------------------------------------


class TestScopeProvenanceRecordsDecider:
    """Issue athenaeum#2515 AC3 is about what gets PASSED to the ledger as
    this pair's ``coord_origins`` -- :func:`athenaeum.comparator.record_comparison`'s
    own handling of that mapping (writing it into the ledger whenever it
    successfully ledgers a verdict) is already covered by
    ``tests/test_coordinate_answer_loop.py::test_full_loop_writes_coordinate_and_mechanically_recomputes``.
    These two tests capture ``record_comparison``'s ``coord_origins`` kwarg
    directly (while still calling through to the REAL function) rather than
    asserting on a ledger entry, because a same-subject-and-scope pair with
    no other disjoint dimension needs a genuine Gate-2 call to reach a FINAL
    pair verdict -- ``client=None`` at that call site (unchanged by this
    issue) means no entry is ledgered in THIS fixture either way; the
    coordinate (and its provenance) is still correctly written and
    forwarded regardless."""

    def test_model_decided_scope_is_tagged_in_coord_origins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.comparator as comparator_mod

        real_record_comparison = comparator_mod.record_comparison
        captured: dict = {}

        def _capture(*args: object, **kwargs: object) -> object:
            captured["coord_origins"] = kwargs.get("coord_origins")
            return real_record_comparison(*args, **kwargs)

        monkeypatch.setattr(comparator_mod, "record_comparison", _capture)

        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="job hunt automation, redesign phase two",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            client = _scope_client(ScopeSameness.SAME)
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

        assert report.applied == 1
        assert captured["coord_origins"]["subject"] == pq.id
        assert captured["coord_origins"]["scope"] == f"model:{pq.id}"
        assert not captured["coord_origins"]["scope"].startswith(pq.id)

    def test_human_decided_scope_is_tagged_with_the_bare_answer_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the human answers BOTH subject and scope in the SAME
        submitted payload, the pre-existing coordinate-answer machinery
        (unchanged by this issue) stamps ``coord_origins["scope"]`` with the
        bare answer id -- never the ``model:`` prefix, since no judge ran at
        all (scope was already in the answered dims, so the new branch's
        ``"scope" not in dims_for_pair`` guard keeps it out of scope)."""
        import athenaeum.comparator as comparator_mod

        real_record_comparison = comparator_mod.record_comparison
        captured: dict = {}

        def _capture(*args: object, **kwargs: object) -> object:
            captured["coord_origins"] = kwargs.get("coord_origins")
            return real_record_comparison(*args, **kwargs)

        monkeypatch.setattr(comparator_mod, "record_comparison", _capture)

        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path, scope_a=None, scope_b=None
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {
                                "pair": pair_key,
                                "dimensions": {
                                    "subject": {id_a: "alice", id_b: "alice"},
                                    "scope": {id_a: "shared/scope", id_b: "shared/scope"},
                                },
                            }
                        ]
                    }
                ),
            )
            # No client at all -- proves the human-answered path never
            # touches the judge (the judge would short-circuit on
            # ``client=None`` anyway, but this proves it is never even
            # reached: ``"scope" in dims_for_pair`` already excludes it).
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 1
        assert captured["coord_origins"]["scope"] == pq.id
        assert captured["coord_origins"]["subject"] == pq.id
        assert not captured["coord_origins"]["scope"].startswith("model:")

        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        # No extra scope item -- the human already answered it directly.
        assert len(coordinate_items) == 1


# ---------------------------------------------------------------------------
# AC4: a failed/unavailable model call raises the scope item instead of
# guessing.
# ---------------------------------------------------------------------------


class TestUnavailableJudgeRaisesInsteadOfGuessing:
    def test_no_client_raises_scope_item_instead_of_guessing(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="a newsletter subject-line calibration pass",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            # No `client=` at all -- the default, matching every
            # pre-athenaeum#2515 caller.
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 1
        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        assert len(coordinate_items) == 2
        scope_item = next(q for q in coordinate_items if q.id != pq.id)
        assert scope_item.answered is False

        # Nothing was guessed: both pages keep their ORIGINAL, divergent
        # claimed_scope values.
        meta_a, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        assert meta_a["claimed_scope"] == "the job-hunt automation redesign"
        assert meta_b["claimed_scope"] == "a newsletter subject-line calibration pass"

    def test_failed_call_raises_scope_item_instead_of_guessing(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="a newsletter subject-line calibration pass",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            client = _raising_client()
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

        assert report.applied == 1
        client.messages.create.assert_called_once()
        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        assert len(coordinate_items) == 2


# ---------------------------------------------------------------------------
# A clear "different" judgement also raises (does not fabricate a DISJOINT
# scope) -- documented reversible default, see _apply_coordinate_answer's
# docstring.
# ---------------------------------------------------------------------------


class TestClearDifferentJudgementAlsoRaises:
    def test_different_judgement_raises_scope_item_too(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="a newsletter subject-line calibration pass",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=_same_subject_verdict(pair_key, id_a, id_b),
            )
            client = _scope_client(ScopeSameness.DIFFERENT)
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

        assert report.applied == 1
        pending_path = tmp_path / "_pending_questions.md"
        coordinate_items = [
            q for q in parse_pending_questions(pending_path) if q.decision_kind == "coordinate"
        ]
        assert len(coordinate_items) == 2


# ---------------------------------------------------------------------------
# The judge is never invoked outside its narrow trigger shape.
# ---------------------------------------------------------------------------


class TestJudgeNeverRunsOutsideItsTriggerShape:
    def test_differing_subject_answer_never_calls_the_judge(self, tmp_path: Path) -> None:
        """A DIFFERING subject answer (e.g. 'alice' vs 'bob') is the
        pre-existing distinct-on-subject path -- never this issue's
        trigger. The judge must not be called even though a client is
        supplied."""
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_same_subject_scope_pending(
                tmp_path,
                scope_a="the job-hunt automation redesign",
                scope_b="a newsletter subject-line calibration pass",
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {
                                "pair": pair_key,
                                "dimensions": {"subject": {id_a: "alice", id_b: "bob"}},
                            }
                        ]
                    }
                ),
            )
            client = _scope_client(ScopeSameness.SAME)
            report = apply_decision_answers(tmp_path, raw_root, lock=lock, client=client)

        assert report.applied == 1
        client.messages.create.assert_not_called()
