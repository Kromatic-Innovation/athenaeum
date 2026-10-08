# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for the coordinate-answer loop (issue athenaeum#1993).

Closes the loop issue athenaeum#1991 (batching) left open: an
``underdetermined`` comparator verdict queues a ``coordinate`` decision-queue
item (:func:`athenaeum.verdict_effects.queue_coordinate_batch`); answering it
through ``athenaeum decisions answer`` writes the coordinate(s) to the
affected page(s)' frontmatter (reusing
:func:`athenaeum.pending_merges._write_coordinate` verbatim) and mechanically
re-compares the pair via the EXISTING comparator entry point
(:func:`athenaeum.comparator.record_comparison`) -- never a second, ad hoc
comparison path.

No LLM client anywhere in this file: the mandatory end-to-end fixture
resolves purely on Gate 1 (a human-ratified ``subject`` identity answer),
matching :mod:`athenaeum.decision_answers`'s own "tier 0 -- no LLM call,
ever" contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.answers import parse_pending_questions
from athenaeum.comparator import (
    VERDICT_DISTINCT,
    VERDICT_UNDERDETERMINED,
    compare_pages,
    page_from_path,
    record_comparison,
)
from athenaeum.decision_answers import apply_decision_answers, write_decision_answer
from athenaeum.decision_framing import (
    answerable_as,
    response_schema_for,
    validate_answer,
    validate_coordinate_payload,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.runlock import RunLock
from athenaeum.verdict_effects import apply_verdict_effect, parse_coordinate_batch_members
from athenaeum.verdicts import iter_live_entries, make_pair_key


def _write_page(path: Path, *, name: str, body: str) -> None:
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\n\n{body}\n", encoding="utf-8")


def _conflicting_client() -> MagicMock:
    """A MagicMock mirroring the Anthropic SDK, reporting Gate 2 CONFLICTING."""
    client = MagicMock()
    response = MagicMock()
    response.content = [
        MagicMock(
            text=json.dumps(
                {
                    "content_relation": "conflicting",
                    "conflicting_passages": ["Jan 1 vs July 4"],
                    "predicate_a": "born-jan-1",
                    "predicate_b": "born-july-4",
                    "rationale": "test",
                }
            )
        )
    ]
    client.messages.create.return_value = response
    return client


def _seed_underdetermined_coordinate_item(wiki_root: Path, lock: RunLock) -> tuple:
    """Build two pages with no ``subject``, ledger an ``underdetermined``
    verdict for them, and queue it as a ``coordinate`` item.

    Returns ``(path_a, path_b, id_a, id_b, pair_key, pq)``.
    """
    path_a = wiki_root / "page-a.md"
    path_b = wiki_root / "page-b.md"
    _write_page(path_a, name="Page A", body="A claim that the birthday is Jan 1.")
    _write_page(path_b, name="Page B", body="A claim that the birthday is July 4.")

    page_a = page_from_path(path_a)
    page_b = page_from_path(path_b)
    client = _conflicting_client()

    outcome = compare_pages(page_a, page_b, client=client)
    assert outcome.verdict == VERDICT_UNDERDETERMINED
    assert outcome.missing == ["subject"]

    record_comparison(wiki_root, page_a, page_b, client=client, lock=lock)
    effect = apply_verdict_effect(
        page_a, page_b, outcome, wiki_root=wiki_root, path_a=path_a, path_b=path_b, config=None
    )
    assert effect.verdict == VERDICT_UNDERDETERMINED
    assert effect.action == "queued"

    pending_path = wiki_root / "_pending_questions.md"
    questions = parse_pending_questions(pending_path)
    assert len(questions) == 1
    pq = questions[0]
    assert pq.decision_kind == "coordinate"

    members = parse_coordinate_batch_members(pq.description)
    assert len(members) == 1
    pair_key = members[0]["pair"]
    id_a, id_b = pair_key.split("+", 1)
    assert {id_a, id_b} == {page_a.id, page_b.id}

    return path_a, path_b, id_a, id_b, pair_key, pq


# ---------------------------------------------------------------------------
# Framing + schema (AC1)
# ---------------------------------------------------------------------------


class TestCoordinateFraming:
    def test_coordinate_is_answerable(self) -> None:
        assert answerable_as("coordinate") == "coordinate"

    def test_response_schema_is_a_non_empty_string_verdict(self) -> None:
        schema = response_schema_for("coordinate")
        assert schema["type"] == "object"
        assert schema["properties"]["verdict"]["type"] == "string"
        assert schema["properties"]["verdict"]["minLength"] == 1
        assert "note" in schema["properties"]
        assert schema["required"] == ["verdict"]

    def test_outer_schema_accepts_a_json_encoded_payload_string(self) -> None:
        payload = {"answers": [{"pair": "a+b", "dimensions": {"subject": {"a": "x"}}}]}
        assert validate_answer("coordinate", {"verdict": json.dumps(payload)}) == []

    def test_outer_schema_refuses_an_empty_verdict(self) -> None:
        assert validate_answer("coordinate", {"verdict": ""}) != []

    def test_outer_schema_refuses_a_missing_verdict(self) -> None:
        assert validate_answer("coordinate", {"note": "no verdict here"}) != []

    def test_payload_schema_accepts_a_well_formed_answer(self) -> None:
        payload = {"answers": [{"pair": "a+b", "dimensions": {"subject": {"a": "x"}}}]}
        assert validate_coordinate_payload(payload) == []

    def test_payload_schema_refuses_a_missing_pair_key(self) -> None:
        payload = {"answers": [{"dimensions": {"subject": {"a": "x"}}}]}
        assert validate_coordinate_payload(payload) != []

    def test_payload_schema_refuses_an_empty_dimensions_map(self) -> None:
        payload = {"answers": [{"pair": "a+b", "dimensions": {}}]}
        assert validate_coordinate_payload(payload) != []

    def test_payload_schema_refuses_a_non_dict(self) -> None:
        assert validate_coordinate_payload(["not", "a", "dict"]) != []
        assert validate_coordinate_payload("also not a dict") != []


# ---------------------------------------------------------------------------
# Queueing + unified-view surfacing (AC2)
# ---------------------------------------------------------------------------


class TestCoordinateQueueing:
    def test_underdetermined_queues_a_coordinate_type_item(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )

        decisions = list_pending_decisions(tmp_path)
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision["type"] == "coordinate"
        assert decision["id"] == pq.id
        assert decision["payload"]["members"] == [
            {"pair": pair_key, "dimensions": ["subject"]}
        ]
        assert decision["payload"]["batch_ref"] == pq.source


# ---------------------------------------------------------------------------
# Inbound applier refusals — "nothing half-lands" (AC3)
# ---------------------------------------------------------------------------


class TestCoordinateAnswerApplierRefusals:
    def test_malformed_verdict_json_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, _, _, _, pq = _seed_underdetermined_coordinate_item(tmp_path, lock)
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root, decision_id=pq.id, decision_type="coordinate", verdict="not json"
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "malformed_verdict_json"

    def test_schema_invalid_payload_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, _, _, _, pq = _seed_underdetermined_coordinate_item(tmp_path, lock)
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps({"no_answers_key": True}),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "schema_invalid"

    def test_unknown_decision_id_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _seed_underdetermined_coordinate_item(tmp_path, lock)
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id="not-a-real-batch-ref",
                decision_type="coordinate",
                verdict=json.dumps(
                    {"answers": [{"pair": "a+b", "dimensions": {"subject": {"a": "x"}}}]}
                ),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "id_not_found"

    def test_pair_not_in_batch_is_refused_before_writing(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )
            raw_root = tmp_path / "raw"
            before_a = path_a.read_text(encoding="utf-8")
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {
                                "pair": "some-other-page+another-page",
                                "dimensions": {"subject": {"some-other-page": "x"}},
                            }
                        ]
                    }
                ),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)
            # Nothing half-lands: the page is untouched on a refusal.
            assert path_a.read_text(encoding="utf-8") == before_a

        assert report.applied == 0
        assert report.outcomes[0].error_code == "invalid_coordinate_answer"
        assert "not a member of batch" in report.outcomes[0].message

    def test_unknown_dimension_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {"pair": pair_key, "dimensions": {"not-a-real-dimension": {id_a: "x"}}}
                        ]
                    }
                ),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "invalid_coordinate_answer"
        assert "unknown dimension" in report.outcomes[0].message

    def test_page_id_not_a_member_of_its_own_pair_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
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
                                "dimensions": {"subject": {"not-a-member-id": "x"}},
                            }
                        ]
                    }
                ),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "invalid_coordinate_answer"
        assert "not members of this pair" in report.outcomes[0].message

    def test_no_lock_is_refused_without_calling_the_comparator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.comparator as comparator_mod

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("must not reach the comparator without a lock")

        with RunLock(tmp_path) as lock:
            _, _, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )

        monkeypatch.setattr(comparator_mod, "record_comparison", _boom)
        raw_root = tmp_path / "raw"
        write_decision_answer(
            raw_root,
            decision_id=pq.id,
            decision_type="coordinate",
            verdict=json.dumps(
                {"answers": [{"pair": pair_key, "dimensions": {"subject": {id_a: "x"}}}]}
            ),
        )
        # No lock= passed at all -- the applier's own code must refuse,
        # never silently proceed without one.
        report = apply_decision_answers(tmp_path, raw_root)

        assert report.applied == 0
        assert report.outcomes[0].error_code == "lock_required"

    def test_already_answered_item_is_refused(self, tmp_path: Path) -> None:
        with RunLock(tmp_path) as lock:
            _, _, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )
            raw_root = tmp_path / "raw"
            verdict = json.dumps(
                {"answers": [{"pair": pair_key, "dimensions": {"subject": {id_a: "x"}}}]}
            )
            write_decision_answer(
                raw_root, decision_id=pq.id, decision_type="coordinate", verdict=verdict
            )
            first = apply_decision_answers(tmp_path, raw_root, lock=lock)
            assert first.applied == 1

            write_decision_answer(
                raw_root, decision_id=pq.id, decision_type="coordinate", verdict=verdict
            )
            second = apply_decision_answers(tmp_path, raw_root, lock=lock)

        # Every outcome on this SECOND pass is now "already_resolved" --
        # both the just-reprocessed first file and the fresh second file
        # for the now-answered id, since `apply_decision_answers` walks
        # every file in `raw_root/answers/` on each call, not just new
        # ones (same posture as every other applier: never deletes an
        # answer file, so it is its own audit trail and gets re-seen).
        assert second.applied == 0
        assert all(o.error_code == "already_resolved" for o in second.outcomes)
        assert len(second.outcomes) == 2


# ---------------------------------------------------------------------------
# The mandatory end-to-end fixture (AC "Test:")
# ---------------------------------------------------------------------------


class TestCoordinateAnswerEndToEnd:
    def test_full_loop_writes_coordinate_and_mechanically_recomputes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """underdetermined -> queued coordinate -> answered -> coordinate
        written -> mechanical re-compare -> a NEW, non-underdetermined
        verdict for the same pair. No LLM call anywhere in the re-compare."""
        import athenaeum.comparator as comparator_mod

        def _gate2_must_not_be_called(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                "the mechanical re-compare must resolve on Gate 1 alone "
                "(a ratified subject answer) -- Gate 2 must never be reached"
            )

        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )

            # Only now, AFTER the item is queued (mirroring that the
            # ORIGINAL compare legitimately needed Gate 2), forbid it —
            # the coordinate-answer's own re-compare must never reach it.
            monkeypatch.setattr(
                comparator_mod, "content_relation", _gate2_must_not_be_called
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
                                    "subject": {id_a: "alice", id_b: "bob"}
                                },
                            }
                        ]
                    }
                ),
            )

            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

        assert report.applied == 1
        outcome = report.outcomes[0]
        assert outcome.error_code is None
        assert "distinct" in outcome.message

        # The coordinate was actually WRITTEN to both pages' frontmatter.
        meta_a_text = path_a.read_text(encoding="utf-8")
        meta_b_text = path_b.read_text(encoding="utf-8")
        assert "subject: alice" in meta_a_text
        assert "subject: bob" in meta_b_text

        # The queue item is now answered.
        pending_path = tmp_path / "_pending_questions.md"
        [answered_pq] = [
            q for q in parse_pending_questions(pending_path) if q.id == pq.id
        ]
        assert answered_pq.answered is True

        # The mechanical re-compare ledgered a NEW, non-underdetermined
        # verdict for the SAME pair -- via iter_live_entries (append
        # order), not lookup_pair: the ledger's `at` field is DATE-only
        # (athenaeum.verdicts.build_verdict_entry), so two same-day
        # entries for one pair tie on lookup_pair's "most recent `at`
        # wins" rule before compaction runs; append order is unambiguous.
        full_pair_key = make_pair_key(id_a, id_b)
        entries = [e for _, e in iter_live_entries(tmp_path) if e.pair == full_pair_key]
        assert len(entries) == 2
        original, recomputed = entries
        assert original.verdict == VERDICT_UNDERDETERMINED
        assert original.stale is True
        assert recomputed.verdict == VERDICT_DISTINCT
        assert recomputed.stale is False
        assert recomputed.decided_by == "comparator"
        assert f"human-batch:{pq.id}" == recomputed.basis.authority_basis
        # Out of scope for athenaeum#1993 (slice d2, athenaeum#1994's job):
        # coord_origins stays exactly the EXISTING {} literal every fresh
        # compare writes (comparator.py / verdicts.py) -- this issue must
        # not populate it.
        assert recomputed.basis.coord_origins == {}

    def test_partial_answer_defers_to_the_next_llm_backed_pass(
        self, tmp_path: Path
    ) -> None:
        """Answering only ONE side of a two-sided missing dimension still
        writes the coordinate and still flips the item answered, but the
        pair stays undecided (no Gate 1 relation yet) -- not a crash, and
        no second ledger entry is written for a verdict that was never
        actually decided."""
        with RunLock(tmp_path) as lock:
            path_a, path_b, id_a, id_b, pair_key, pq = _seed_underdetermined_coordinate_item(
                tmp_path, lock
            )
            raw_root = tmp_path / "raw"
            write_decision_answer(
                raw_root,
                decision_id=pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {"answers": [{"pair": pair_key, "dimensions": {"subject": {id_a: "alice"}}}]}
                ),
            )
            report = apply_decision_answers(tmp_path, raw_root, lock=lock)

            full_pair_key = make_pair_key(id_a, id_b)
            entries = [e for _, e in iter_live_entries(tmp_path) if e.pair == full_pair_key]

        assert report.applied == 1
        assert "not yet decided" in report.outcomes[0].message
        assert "subject: alice" in path_a.read_text(encoding="utf-8")
        assert "subject:" not in path_b.read_text(encoding="utf-8")
        # Only the ORIGINAL entry -- the re-compare correctly could not
        # decide (one side still has no subject), so nothing new ledgers.
        assert len(entries) == 1
        assert entries[0].verdict == VERDICT_UNDERDETERMINED
