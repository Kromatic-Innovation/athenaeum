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
from athenaeum.verdict_effects import (
    apply_verdict_effect,
    parse_coordinate_batch_members,
    queue_coordinate_batch,
)
from athenaeum.verdicts import (
    challenge_coordinate_answer,
    iter_live_entries,
    make_pair_key,
    mark_pairs_stale,
)


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


def _seed_two_pair_coordinate_batch(wiki_root: Path, lock: RunLock) -> tuple:
    """Like :func:`_seed_underdetermined_coordinate_item`, but TWO
    independent pairs flushed as ONE batch -- mirroring how a real
    per-cluster caller (e.g. :mod:`athenaeum.wiki_dedupe`) aggregates many
    ``underdetermined`` pairs into a single queue item via a shared
    ``coordinate_sink`` (issue athenaeum#1991), rather than two separate
    batch-of-one items.

    Returns ``([(path_a, path_b, id_a, id_b, pair_key), (path_c, path_d,
    id_c, id_d, pair_key2)], pq)`` -- one ``pq`` covering both pairs.
    """
    sink: list[dict] = []
    pairs = []
    for suffix in ("1", "2"):
        path_a = wiki_root / f"page-{suffix}a.md"
        path_b = wiki_root / f"page-{suffix}b.md"
        _write_page(
            path_a, name=f"Page {suffix}A", body=f"A claim that birthday {suffix} is Jan 1."
        )
        _write_page(
            path_b, name=f"Page {suffix}B", body=f"A claim that birthday {suffix} is July 4."
        )

        page_a = page_from_path(path_a)
        page_b = page_from_path(path_b)
        client = _conflicting_client()

        outcome = compare_pages(page_a, page_b, client=client)
        assert outcome.verdict == VERDICT_UNDERDETERMINED
        assert outcome.missing == ["subject"]

        record_comparison(wiki_root, page_a, page_b, client=client, lock=lock)
        effect = apply_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            path_a=path_a,
            path_b=path_b,
            config=None,
            coordinate_sink=sink,
        )
        assert effect.verdict == VERDICT_UNDERDETERMINED
        assert effect.action == "coordinate-pending"

        pair_key = make_pair_key(page_a.id, page_b.id)
        pairs.append((path_a, path_b, page_a.id, page_b.id, pair_key))

    assert len(sink) == 2
    result = queue_coordinate_batch(sink, wiki_root=wiki_root, config=None)
    assert result.action == "queued"

    pending_path = wiki_root / "_pending_questions.md"
    candidates = [
        q
        for q in parse_pending_questions(pending_path)
        if q.decision_kind == "coordinate"
        and len(parse_coordinate_batch_members(q.description)) == 2
    ]
    assert len(candidates) == 1
    pq = candidates[0]
    return pairs, pq


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
        # Issue athenaeum#1994: coord_origins now carries the real mapping
        # -- the "subject" dimension this answer supplied, stamped with
        # the SAME answer id (pq.id == answer.decision_id for a
        # single-member batch) that a challenge keys on.
        assert recomputed.basis.coord_origins == {"subject": pq.id}

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


# ---------------------------------------------------------------------------
# coord_origins population + stale-mark blast radius (issue athenaeum#1994)
# ---------------------------------------------------------------------------


class TestCoordinateChallengeBlastRadius:
    def test_challenging_a_batch_answer_stale_marks_exactly_its_pairs(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: answer a two-pair coordinate BATCH, answer an
        UNRELATED pair's own batch-of-one separately, challenge only the
        first batch's answer id, and assert exactly the two pairs it
        decided are stale-marked -- no more (the unrelated pair stays
        fresh) and no fewer (both batch members, not just one)."""
        with RunLock(tmp_path) as lock:
            pairs, batch_pq = _seed_two_pair_coordinate_batch(tmp_path, lock)
            (path_1a, path_1b, id_1a, id_1b, pair_key_1), (
                path_2a,
                path_2b,
                id_2a,
                id_2b,
                pair_key_2,
            ) = pairs

            # A THIRD, wholly unrelated pair answered through its OWN
            # batch-of-one -- not via the shared helper above (which
            # asserts it is the ONLY pending question; two batches
            # already exist in this test by now).
            path_3a = tmp_path / "page-3a.md"
            path_3b = tmp_path / "page-3b.md"
            _write_page(path_3a, name="Page 3A", body="A claim that birthday 3 is Jan 1.")
            _write_page(path_3b, name="Page 3B", body="A claim that birthday 3 is July 4.")
            page_3a = page_from_path(path_3a)
            page_3b = page_from_path(path_3b)
            client_3 = _conflicting_client()
            outcome_3 = compare_pages(page_3a, page_3b, client=client_3)
            assert outcome_3.verdict == VERDICT_UNDERDETERMINED
            record_comparison(tmp_path, page_3a, page_3b, client=client_3, lock=lock)
            effect_3 = apply_verdict_effect(
                page_3a,
                page_3b,
                outcome_3,
                wiki_root=tmp_path,
                path_a=path_3a,
                path_b=path_3b,
                config=None,
            )
            assert effect_3.action == "queued"
            id_3a, id_3b = page_3a.id, page_3b.id
            pair_key_3 = make_pair_key(id_3a, id_3b)

            pending_path = tmp_path / "_pending_questions.md"
            [solo_pq] = [
                q
                for q in parse_pending_questions(pending_path)
                if q.decision_kind == "coordinate"
                and parse_coordinate_batch_members(q.description) == [
                    {"pair": pair_key_3, "dimensions": ["subject"]}
                ]
            ]

            raw_root = tmp_path / "raw"

            # Answer the TWO-pair batch with one decision-answer file.
            write_decision_answer(
                raw_root,
                decision_id=batch_pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {
                                "pair": pair_key_1,
                                "dimensions": {"subject": {id_1a: "alice", id_1b: "bob"}},
                            },
                            {
                                "pair": pair_key_2,
                                "dimensions": {"subject": {id_2a: "carol", id_2b: "dave"}},
                            },
                        ]
                    }
                ),
            )
            # Answer the UNRELATED solo pair with its OWN, different
            # decision id -- this must stay out of the batch's blast
            # radius entirely.
            write_decision_answer(
                raw_root,
                decision_id=solo_pq.id,
                decision_type="coordinate",
                verdict=json.dumps(
                    {
                        "answers": [
                            {
                                "pair": pair_key_3,
                                "dimensions": {"subject": {id_3a: "erin", id_3b: "frank"}},
                            }
                        ]
                    }
                ),
            )

            report = apply_decision_answers(tmp_path, raw_root, lock=lock)
            assert report.applied == 2
            assert all(o.error_code is None for o in report.outcomes)

            # coord_origins carries the SAME batch answer id for BOTH
            # members -- the identity this AC is "most easily faked" on.
            fresh_1 = [
                e
                for _, e in iter_live_entries(tmp_path)
                if e.pair == pair_key_1 and not e.stale
            ]
            fresh_2 = [
                e
                for _, e in iter_live_entries(tmp_path)
                if e.pair == pair_key_2 and not e.stale
            ]
            fresh_3 = [
                e
                for _, e in iter_live_entries(tmp_path)
                if e.pair == pair_key_3 and not e.stale
            ]
            assert len(fresh_1) == len(fresh_2) == len(fresh_3) == 1
            assert fresh_1[0].basis.coord_origins == {"subject": batch_pq.id}
            assert fresh_2[0].basis.coord_origins == {"subject": batch_pq.id}
            assert fresh_3[0].basis.coord_origins == {"subject": solo_pq.id}

            # Challenge ONLY the batch answer.
            result = challenge_coordinate_answer(tmp_path, batch_pq.id, lock=lock)

        assert result["ok"] is True
        assert result["marked_stale"] == 2
        assert set(result["pairs"]) == {pair_key_1, pair_key_2}

        # Positive side: BOTH batch members' fresh verdicts are now stale.
        entries_1 = [e for _, e in iter_live_entries(tmp_path) if e.pair == pair_key_1]
        entries_2 = [e for _, e in iter_live_entries(tmp_path) if e.pair == pair_key_2]
        assert all(e.stale for e in entries_1)
        assert all(e.stale for e in entries_2)

        # Negative side: the unrelated pair's fresh verdict is UNTOUCHED --
        # exactly one non-stale entry remains, the one just recomputed.
        entries_3 = [e for _, e in iter_live_entries(tmp_path) if e.pair == pair_key_3]
        still_fresh_3 = [e for e in entries_3 if not e.stale]
        assert len(still_fresh_3) == 1
        assert still_fresh_3[0].basis.coord_origins == {"subject": solo_pq.id}

        # Challenging an id with no coord_origins hits is a clean no-op,
        # not an error.
        with RunLock(tmp_path) as lock:
            empty = challenge_coordinate_answer(tmp_path, "no-such-answer-id", lock=lock)
        assert empty == {
            "ok": True,
            "answer_id": "no-such-answer-id",
            "marked_stale": 0,
            "pairs": [],
        }


class TestCoordinateChallengeIgnoresSupersededEntries:
    def test_challenging_a_superseded_answer_does_not_stale_mark_the_current_verdict(
        self, tmp_path: Path
    ) -> None:
        """Seer finding on PR athenaeum#2010: before compaction runs, a pair
        can carry MULTIPLE live entries -- an old, already-superseded entry
        decided by answer A, and the CURRENT one decided by a later, DIFFERENT
        answer B. Challenging A must stale-mark NOTHING for this pair: its
        current verdict was not decided by A. (Constructed directly via the
        same ledger primitives the coordinate-answer loop itself uses --
        `_write_coordinate` short-circuits Gate 1 on a second compare once a
        subject coordinate is already on disk, so a genuine third `coordinate`
        answer round-trip for the SAME pair cannot be driven through the
        full CLI/queue path a second time without re-deriving a third
        comparator scenario; this is the narrowest construction that still
        exercises the real `challenge_coordinate_answer` production path.)"""
        from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry

        with RunLock(tmp_path) as lock:
            # Pair decided by answer A.
            entry_a = build_verdict_entry(
                "page-x",
                "page-y",
                VERDICT_DISTINCT,
                basis=Basis(coord_origins={"subject": "answer-A"}),
                decided_by="comparator",
            )
            append_verdict(tmp_path, entry_a, lock=lock)

            # Re-decided later -- the A-era entry is stale-marked exactly as
            # `_apply_coordinate_answer` stale-marks a pair before its own
            # re-compare, and a FRESH entry for the SAME pair is appended,
            # decided by a DIFFERENT answer B.
            mark_pairs_stale(tmp_path, {"page-x+page-y": "re-answered"}, lock=lock)
            entry_b = build_verdict_entry(
                "page-x",
                "page-y",
                VERDICT_DISTINCT,
                basis=Basis(coord_origins={"subject": "answer-B"}),
                decided_by="comparator",
            )
            append_verdict(tmp_path, entry_b, lock=lock)

            # Challenge the SUPERSEDED answer A.
            result = challenge_coordinate_answer(tmp_path, "answer-A", lock=lock)

        # No more: the pair's CURRENT (B-decided) verdict is untouched, and
        # nothing is reported as matched or marked for the superseded answer.
        assert result == {
            "ok": True,
            "answer_id": "answer-A",
            "marked_stale": 0,
            "pairs": [],
        }
        entries = [e for _, e in iter_live_entries(tmp_path) if e.pair == "page-x+page-y"]
        current = [e for e in entries if e.basis.coord_origins == {"subject": "answer-B"}]
        assert len(current) == 1
        assert current[0].stale is False

        # No fewer, in the same breath: challenging the CURRENT answer B
        # DOES stale-mark it.
        with RunLock(tmp_path) as lock:
            result_b = challenge_coordinate_answer(tmp_path, "answer-B", lock=lock)
        assert result_b == {
            "ok": True,
            "answer_id": "answer-B",
            "marked_stale": 1,
            "pairs": ["page-x+page-y"],
        }
        entries_after = [
            e for _, e in iter_live_entries(tmp_path) if e.pair == "page-x+page-y"
        ]
        assert all(e.stale for e in entries_after)
