# SPDX-License-Identifier: Apache-2.0
"""Framed, effort-capped decision items (issue athenaeum#717).

The acceptance criterion these tests exist for is not "the fields are
present" — it is that the per-item context cap is **enforced in code, not
aspirationally**. So the cap tests below assert the refusal paths (decompose,
then escalate-as-scheduled-review) and the invariant that no item can enter
the queue carrying an over-cap bundle, including through the real
``list_pending_decisions`` read path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.decision_framing import (
    _TYPE_FRAMING,
    REVERSIBILITY_CLASSES,
    ROUTING_AUTHORITY,
    ROUTING_CLASSES,
    ROUTING_COMPETENCE,
    ROUTING_SCHEDULED_REVIEW,
    build_context_bundle,
    bundle_tokens,
    escalation_rationale_for,
    frame_decision,
    proposed_default_for,
    response_schema_for,
    reversibility_for,
    routing_for,
    shape_errors_only,
    validate_answer,
)

#: Every ``type`` tag :mod:`athenaeum.decisions` stamps on a unified item.
LIVE_DECISION_TYPES = (
    "question",
    "confirmation",
    "merge",
    "retraction",
    "audit",
    "quarantine",
    "proposed-rule",
)


def _item(decision_type: str, **payload: object) -> dict:
    """A minimal unified-queue item of ``decision_type``."""
    return {
        "type": decision_type,
        "id": f"id-{decision_type}",
        "created_at": "2026-10-01",
        "summary": f"A plainly-phrased {decision_type} question?",
        "confidence": None,
        "payload": dict(payload),
    }


class TestEveryLiveTypeIsFramed:
    """Framing is stated per type, so a live type with no entry is a bug."""

    @pytest.mark.parametrize("decision_type", LIVE_DECISION_TYPES)
    def test_live_type_has_a_real_framing_entry(self, decision_type: str) -> None:
        # Not just "framing resolves" -- the unknown-type fallback resolves for
        # anything. This asserts the type is actually IN the table, which is
        # what stops a new decision type from being silently mis-framed as
        # cheap and reversible.
        assert decision_type in _TYPE_FRAMING

    @pytest.mark.parametrize("decision_type", LIVE_DECISION_TYPES)
    def test_framed_item_carries_every_required_field(self, decision_type: str) -> None:
        framed = frame_decision(_item(decision_type), max_context_tokens=1500)

        # The plain-language question is the existing `summary` field.
        assert framed["summary"].strip()
        assert isinstance(framed["context_bundle"], dict)
        assert framed["reversibility"] in REVERSIBILITY_CLASSES
        assert framed["routing"] in ROUTING_CLASSES
        assert set(framed["proposed_default"]) == {"action", "consequences"}
        assert framed["proposed_default"]["action"].strip()
        assert framed["proposed_default"]["consequences"].strip()
        assert framed["response_schema"]["type"] == "object"
        assert framed["escalation_rationale"].strip()

    @pytest.mark.parametrize("decision_type", LIVE_DECISION_TYPES)
    def test_framing_never_mutates_the_input_item(self, decision_type: str) -> None:
        item = _item(decision_type, body="x")
        before = json.dumps(item, sort_keys=True)

        frame_decision(item, max_context_tokens=1500)

        assert json.dumps(item, sort_keys=True) == before

    def test_an_unknown_type_fails_closed(self) -> None:
        """A type with no entry is routed to the owner as irreversible.

        The unsafe direction would be defaulting a new decision type to
        'reversible, competence' -- that quietly makes it eligible for
        anything the design lets an agent absorb unreviewed.
        """
        framed = frame_decision(_item("brand-new-kind"), max_context_tokens=1500)

        assert framed["reversibility"] == "irreversible"
        assert framed["routing"] == ROUTING_AUTHORITY

    def test_reversibility_classes_are_actually_discriminated(self) -> None:
        """The classes must distinguish types, or the field informs nothing.

        A triage agent is meant to absorb the reversible items and hand the
        irreversible ones to a human. A table that collapsed every type onto
        one class would satisfy the field and make that split impossible.
        """
        classes = {reversibility_for(t) for t in LIVE_DECISION_TYPES}

        assert len(classes) >= 2
        assert classes <= set(REVERSIBILITY_CLASSES)

    def test_the_irreversible_class_is_reserved_for_a_real_one(self) -> None:
        """`retraction` is the irreversible one: derived content cannot be un-derived.

        `merge` is deliberately NOT irreversible any more -- athenaeum#716
        made a fold reversible by leaving the source as a tombstone instead of
        deleting it, which is exactly the kind of change this field has to
        track or it goes stale.
        """
        assert reversibility_for("retraction") == "irreversible"
        assert reversibility_for("merge") == "reversible-with-work"

    def test_authority_and_competence_are_both_actually_used(self) -> None:
        """Both routings are populated, so the split is real rather than nominal.

        Authority items reaching a human are the queue working as designed and
        must be countable separately from competence escalations; a table that
        only ever emitted one value would make that count meaningless.
        """
        routings = {routing_for(t) for t in LIVE_DECISION_TYPES}

        assert routings == {ROUTING_AUTHORITY, ROUTING_COMPETENCE}


class TestProposedDefaultsAreConservative:
    #: The only verbs a no-answer default may OPEN with. Each denotes leaving
    #: the corpus as it is: ``leave``/``reject``/``no`` decline to act, and
    #: ``accept`` appears only on ``confirmation``, where the narrowing being
    #: accepted has already shipped, so accepting it changes nothing.
    INACTION_VERBS = frozenset({"leave", "reject", "no", "accept"})

    def test_no_default_enacts_a_change(self) -> None:
        """An unanswered item must not be able to change the corpus by timing out.

        Checked on the action's LEADING verb rather than by searching the
        whole string for acting verbs: 'reject (do not adopt the rule)' is an
        inaction that mentions one, and a substring sweep calls it an action.
        A new table entry reading 'approve the merge' still fails.
        """
        for decision_type in LIVE_DECISION_TYPES:
            action = proposed_default_for(decision_type)["action"].lower()
            verb = action.split()[0]
            assert verb in self.INACTION_VERBS, (
                f"{decision_type}'s no-answer default opens with {verb!r}, "
                f"which is not one of {sorted(self.INACTION_VERBS)}: {action!r}"
            )


class TestEscalationRationale:
    def test_rationale_carries_the_item_specific_detail(self) -> None:
        """A constant string would satisfy the field and inform nobody."""
        plain = escalation_rationale_for(_item("question"))
        with_detail = escalation_rationale_for(
            _item("question", conflict_type="same-time-contradiction")
        )

        assert "same-time-contradiction" in with_detail
        assert with_detail != plain

    def test_confidence_is_reported_when_present(self) -> None:
        item = _item("merge")
        item["confidence"] = 0.84

        assert "0.84" in escalation_rationale_for(item)

    def test_a_bool_confidence_is_not_reported_as_a_number(self) -> None:
        """`bool` is an `int` subclass -- a True would read as 'confidence: True'."""
        item = _item("merge")
        item["confidence"] = True

        assert "True" not in escalation_rationale_for(item)


class TestPerItemContextCap:
    """The cap is the acceptance criterion: enforced in code, refusal tested."""

    def test_a_small_item_is_admitted_untouched(self) -> None:
        framed = frame_decision(_item("merge", rationale="short"), max_context_tokens=1500)

        assert framed["context_decomposed"] is False
        assert "context_overflow" not in framed
        assert framed["routing"] == ROUTING_COMPETENCE
        assert framed["context_bundle"]["rationale"] == "short"

    def test_an_oversized_item_is_decomposed_rather_than_admitted_oversized(self) -> None:
        item = _item(
            "merge",
            merge_target_name="Keep me",
            draft_merged_body="word " * 4000,
            rationale="short",
        )

        framed = frame_decision(item, max_context_tokens=1500)

        assert framed["context_tokens"] <= 1500
        assert framed["context_decomposed"] is True
        assert "draft_merged_body" in framed["context_dropped"]
        # Decomposition drops bulk, not signal: the identifying fields and the
        # question itself survive.
        assert framed["context_bundle"]["merge_target_name"] == "Keep me"
        assert framed["summary"] == item["summary"]
        # Still a normal answerable item -- decomposing is not escalating.
        assert framed["routing"] == ROUTING_COMPETENCE
        assert "context_overflow" not in framed

    def test_decomposition_stops_at_the_cheapest_sufficient_drop(self) -> None:
        """Dropping the body is enough, so the rationale must survive."""
        item = _item("merge", draft_merged_body="word " * 4000, rationale="keep this")

        framed = frame_decision(item, max_context_tokens=1500)

        assert framed["context_dropped"] == ["draft_merged_body"]
        assert framed["context_bundle"]["rationale"] == "keep this"

    def test_an_irreducible_oversized_item_escalates_to_scheduled_review(self) -> None:
        """The refusal path: it may not enter the queue as an oversized item.

        Every shrinkable key is dropped and the bundle is STILL over cap, so
        the item is admitted as scheduled work rather than presented as a
        one-screen decision it demonstrably is not.
        """
        item = _item(
            "merge",
            # Not in _SHRINK_ORDER, so decomposition cannot reach it.
            unshrinkable_bulk="word " * 4000,
        )

        framed = frame_decision(item, max_context_tokens=1500)

        assert framed["routing"] == ROUTING_SCHEDULED_REVIEW
        assert framed["context_overflow"]["cap"] == 1500
        assert framed["context_tokens"] <= 1500
        # The pointer bundle still identifies WHICH item needs scheduling.
        assert framed["context_bundle"]["id"] == item["id"]
        assert "unshrinkable_bulk" not in framed["context_bundle"]

    def test_a_scheduled_review_item_still_carries_its_full_framing(self) -> None:
        """An over-cap item is still answerable once scheduled, so it keeps its schema."""
        framed = frame_decision(
            _item("merge", unshrinkable_bulk="word " * 4000),
            max_context_tokens=1500,
        )

        assert framed["response_schema"]["type"] == "object"
        assert framed["escalation_rationale"].strip()
        assert framed["reversibility"] in REVERSIBILITY_CLASSES

    def test_no_framed_item_can_exceed_the_cap(self) -> None:
        """The invariant, swept over every type and several bundle shapes."""
        shapes: tuple[dict, ...] = (
            {},
            {"rationale": "word " * 3000},
            {"draft_merged_body": "word " * 9000},
            {"unshrinkable_bulk": "word " * 9000},
            {"sources": [{"body": "word " * 2000} for _ in range(40)]},
        )
        for decision_type in (*LIVE_DECISION_TYPES, "brand-new-kind"):
            for shape in shapes:
                framed = frame_decision(
                    _item(decision_type, **shape), max_context_tokens=1500
                )
                assert framed["context_tokens"] <= 1500, (
                    f"{decision_type} with {sorted(shape)} entered the queue over cap"
                )

    def test_a_non_positive_cap_disables_the_cap_rather_than_emptying_items(self) -> None:
        """A misconfigured 0 must not silently strip every item's context.

        `config._resolve_positive_int_knob` already coerces a bad value to the
        default, so this is defence in depth for a direct caller.
        """
        framed = frame_decision(
            _item("merge", draft_merged_body="word " * 4000), max_context_tokens=0
        )

        assert framed["context_decomposed"] is False
        assert "draft_merged_body" in framed["context_bundle"]

    def test_bundle_tokens_measures_an_unserializable_value_instead_of_raising(
        self,
    ) -> None:
        assert bundle_tokens({"weird": object()}) > 0

    def test_the_question_is_never_counted_against_the_context_cap(self) -> None:
        """`summary` is the question, not context -- and is never shrunk."""
        bundle = build_context_bundle(_item("merge"))

        assert "summary" not in bundle


class TestResponseSchemaValidation:
    def test_a_conforming_answer_validates(self) -> None:
        assert validate_answer("merge", {"verdict": "approve"}) == []
        assert validate_answer("merge", {"verdict": "reject", "note": "why"}) == []

    def test_an_out_of_vocabulary_verdict_is_reported(self) -> None:
        assert validate_answer("merge", {"verdict": "maybe"}) != []

    def test_a_missing_verdict_is_reported(self) -> None:
        errors = validate_answer("merge", {"note": "no verdict"})

        assert any("verdict" in error for error in errors)

    def test_an_unknown_key_is_reported(self) -> None:
        assert validate_answer("merge", {"verdict": "approve", "oops": 1}) != []

    def test_a_free_text_type_accepts_prose_but_not_emptiness(self) -> None:
        assert validate_answer("question", {"verdict": "The 2026 figure holds."}) == []
        assert validate_answer("question", {"verdict": ""}) != []

    def test_every_live_type_publishes_a_usable_schema(self) -> None:
        for decision_type in LIVE_DECISION_TYPES:
            schema = response_schema_for(decision_type)
            assert schema["required"] == ["verdict"]
            assert schema["additionalProperties"] is False

    def test_the_published_schema_is_a_copy_not_the_shared_table_entry(self) -> None:
        """A caller mutating a returned schema must not poison the next item."""
        response_schema_for("merge")["properties"].clear()

        assert response_schema_for("merge")["properties"]


class TestShapeVersusValueDivisionOfLabour:
    """One condition must not acquire two error codes."""

    def test_a_verdict_value_error_is_deferred_to_the_per_type_resolver(self) -> None:
        errors = validate_answer("merge", {"verdict": "maybe"})

        assert errors != []
        assert shape_errors_only(errors) == []

    def test_a_shape_error_is_kept(self) -> None:
        errors = validate_answer("merge", {"verdict": "approve", "oops": 1})

        assert shape_errors_only(errors) != []

    def test_a_missing_verdict_counts_as_a_shape_error(self) -> None:
        errors = validate_answer("merge", {"note": "nothing"})

        assert shape_errors_only(errors) != []


class TestConfigParity:
    def test_the_literal_default_matches_the_config_resolver(self) -> None:
        """`decisions.py` holds a literal to avoid an import cycle; keep them equal."""
        from athenaeum.config import resolve_decisions_max_item_context_tokens
        from athenaeum.decisions import _DECISIONS_MAX_ITEM_CONTEXT_TOKENS_DEFAULT

        assert (
            resolve_decisions_max_item_context_tokens(None)
            == _DECISIONS_MAX_ITEM_CONTEXT_TOKENS_DEFAULT
        )

    def test_the_documented_one_screen_default_is_1500(self) -> None:
        from athenaeum.config import resolve_decisions_max_item_context_tokens

        assert resolve_decisions_max_item_context_tokens(None) == 1500

    def test_yaml_and_env_override_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import resolve_decisions_max_item_context_tokens

        config = {"librarian": {"decisions_max_item_context_tokens": 900}}
        assert resolve_decisions_max_item_context_tokens(config) == 900

        monkeypatch.setenv("ATHENAEUM_DECISIONS_MAX_ITEM_CONTEXT_TOKENS", "700")
        assert resolve_decisions_max_item_context_tokens(config) == 700


class TestFramingReachesTheRealReadPath:
    """Framing must be applied by the queue itself, not only by a unit call."""

    def test_list_pending_decisions_frames_and_caps_every_item(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.answers import raise_pending_question
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        source = wiki_root / "feedback_subject.md"
        source.write_text("---\nname: Subject\n---\n\nbody\n", encoding="utf-8")
        raise_pending_question(
            wiki_root / "_pending_questions.md",
            "Which figure holds?",
            # Deliberately over the cap, so this exercises the real read path's
            # capping rather than only its field-stamping.
            "word " * 4000,
            entity="Subject",
            source=str(source),
        )

        items = list_pending_decisions(wiki_root, max_item_context_tokens=1500)

        assert items, "expected the raised question to reach the unified queue"
        for item in items:
            assert item["context_tokens"] <= 1500
            assert item["reversibility"] in REVERSIBILITY_CLASSES
            assert item["routing"] in ROUTING_CLASSES
            assert item["response_schema"]["type"] == "object"
            assert item["escalation_rationale"].strip()
            assert item["proposed_default"]["action"].strip()
