# SPDX-License-Identifier: Apache-2.0
"""Item contract v1 for the decision queue (athenaeum#2528, design athenaeum#2489 section 2).

Every item gains ``contract_version``, ``state``/``hold_reason``,
``answerable``, a stored ``presentation``, ``fingerprint`` and ``priority``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum.decision_framing import (
    _JOB_UNKNOWN,
    _TYPE_FRAMING,
    CONTRACT_VERSION,
    answerable_as,
    fingerprint_for,
    frame_decision,
    response_schema_for_item,
    state_digest_for,
    template_presentation,
)
from athenaeum.decision_presentations import PRESENTATIONS_FILENAME, load_presentations

ALL_TYPES = tuple(_TYPE_FRAMING)

_WIDEN = 'Pick one. Candidate values (answer with exactly one): ["alpha", "beta"]'


def _item(decision_type: str, **payload: Any) -> dict:
    return {
        "type": decision_type,
        "id": f"id-{decision_type}",
        "created_at": "2026-10-01",
        "summary": f"A plainly-phrased {decision_type} question?",
        "confidence": None,
        "payload": dict(payload),
    }


def test_every_framed_type_has_a_job_template() -> None:
    # The design counted 13 types; page-recompile (athenaeum#2547) made it 14.
    from athenaeum.decision_framing import _JOB

    assert len(ALL_TYPES) >= 13
    assert set(ALL_TYPES) == set(_JOB)


@pytest.mark.parametrize("decision_type", ALL_TYPES)
class TestEveryTypeCarriesTheContract:
    def test_fields_present_alongside_todays(self, decision_type: str) -> None:
        framed = frame_decision(_item(decision_type), max_context_tokens=1500)

        assert framed["contract_version"] == CONTRACT_VERSION == 1
        assert framed["state"] == "pending"
        assert framed["hold_reason"] is None
        assert isinstance(framed["answerable"], bool)
        assert framed["fingerprint"].startswith("sha256:")
        assert 0.0 <= framed["priority"]["score"] <= 1.0
        assert isinstance(framed["priority"]["reasons"], list)
        # nothing in today's framing is dropped
        for key in (
            "summary",
            "context_bundle",
            "reversibility",
            "routing",
            "proposed_default",
            "response_schema",
            "escalation_rationale",
        ):
            assert key in framed

    def test_answerable_is_exactly_answerable_as(self, decision_type: str) -> None:
        framed = frame_decision(_item(decision_type), max_context_tokens=1500)
        assert framed["answerable"] is (answerable_as(decision_type) is not None)

    def test_presentation_template(self, decision_type: str) -> None:
        item = _item(decision_type)
        pres = frame_decision(item, max_context_tokens=1500)["presentation"]

        assert pres["context"].strip()
        assert pres["job"].strip()
        assert pres["job"] != _JOB_UNKNOWN  # a real per-type job, not the fallback
        assert pres["framed_by"]["method"] == "template"
        assert pres["framed_by"]["model"] is None
        assert pres["framed_by"]["framed_at"]
        # options come from the response schema's verdict enum; free text has none
        enum = response_schema_for_item(item)["properties"]["verdict"].get("enum")
        assert [o["value"] for o in pres["options"]] == (enum or [])
        for opt in pres["options"]:
            assert opt["label"].strip()
            assert opt["pros"] and opt["cons"]
        values = {o["value"] for o in pres["options"]}
        rec = pres["recommendation"]
        assert rec is None or rec["value"] in values

    def test_no_basis_means_no_recommendation(self, decision_type: str) -> None:
        pres = frame_decision(_item(decision_type), max_context_tokens=1500)["presentation"]
        assert pres["recommendation"] is None


def test_answerable_and_unanswerable_types_differ() -> None:
    assert frame_decision(_item("merge"), max_context_tokens=1500)["answerable"] is True
    for t in ("retraction", "quarantine", "tier-movement-proposal", "policy-pack-edit"):
        assert frame_decision(_item(t), max_context_tokens=1500)["answerable"] is False


def test_recommendation_is_taken_from_an_item_signal_and_stays_an_option() -> None:
    good = frame_decision(_item("merge", recommended_verdict="reject"), max_context_tokens=1500)
    assert good["presentation"]["recommendation"]["value"] == "reject"
    # a signal that is not one of the options is never promoted
    bad = frame_decision(_item("merge", recommended_verdict="nonsense"), max_context_tokens=1500)
    assert bad["presentation"]["recommendation"] is None
    # free-text items have no options, so no recommendation can exist
    free = frame_decision(_item("audit", recommended_verdict="approve"), max_context_tokens=1500)
    assert free["presentation"]["recommendation"] is None


def test_widen_question_options_are_its_candidates() -> None:
    item = _item("question", description=_WIDEN, conflict_type="widen")
    pres = frame_decision(item, max_context_tokens=1500)["presentation"]
    assert [o["value"] for o in pres["options"]] == ["alpha", "beta"]


def test_fingerprint_is_stable_and_ignores_framed_at() -> None:
    item = _item("merge")
    a = template_presentation(item, framed_at="2026-01-01T00:00:00Z")
    b = template_presentation(item, framed_at="2027-01-01T00:00:00Z")
    digest = state_digest_for(item)
    assert fingerprint_for(item, a, digest) == fingerprint_for(item, b, digest)


def test_decision_relevant_change_changes_digest_and_fingerprint() -> None:
    base = _item("merge", rationale="r1")
    changed = _item("merge", rationale="r2")
    assert state_digest_for(base) != state_digest_for(changed)
    f1 = frame_decision(base, max_context_tokens=1500)["fingerprint"]
    f2 = frame_decision(changed, max_context_tokens=1500)["fingerprint"]
    assert f1 != f2
    # created_at is not decision-relevant
    moved = dict(base, created_at="2020-01-01")
    assert state_digest_for(moved) == state_digest_for(base)


def test_framing_makes_no_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every backend/provider seam is patched to raise; framing must not touch one."""
    import athenaeum.decision_provider as dp

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an LLM/backend call was made while framing")

    for name in dir(dp):
        obj = getattr(dp, name)
        if callable(obj) and not name.startswith("_") and name[0].islower():
            monkeypatch.setattr(dp, name, boom)
    import anthropic

    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    for t in ALL_TYPES:
        pres = frame_decision(_item(t), max_context_tokens=1500)["presentation"]
        assert pres["framed_by"] == {
            "method": "template",
            "model": None,
            "framed_at": pres["framed_by"]["framed_at"],
        }


class TestStoredThroughTheReadPath:
    def _wiki(self, tmp_path: Path) -> Path:
        from athenaeum.answers import raise_pending_question

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        source = wiki / "feedback_subject.md"
        source.write_text("---\nname: Subject\n---\n\nbody\n", encoding="utf-8")
        raise_pending_question(
            wiki / "_pending_questions.md",
            "Which figure holds?",
            "The two notes disagree.",
            entity="Subject",
            source=str(source),
        )
        return wiki

    def test_unframed_item_is_templated_in_process_and_persisted(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)
        assert not (wiki / PRESENTATIONS_FILENAME).exists()

        (item,) = list_pending_decisions(wiki)

        assert item["contract_version"] == 1
        assert item["presentation"]["framed_by"]["method"] == "template"
        rows = [json.loads(x) for x in (wiki / PRESENTATIONS_FILENAME).read_text().splitlines()]
        assert [r["id"] for r in rows] == [item["id"]]
        assert rows[0]["state_digest"].startswith("sha256:")
        assert rows[0]["presentation"] == item["presentation"]

    def test_stored_framing_is_reused_verbatim(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)
        (first,) = list_pending_decisions(wiki)
        path = wiki / PRESENTATIONS_FILENAME
        before = path.read_text()
        (second,) = list_pending_decisions(wiki)

        assert second["presentation"] == first["presentation"]  # same framed_at too
        assert second["fingerprint"] == first["fingerprint"]
        assert path.read_text() == before  # unchanged reads do not rewrite

    def test_a_stored_presentation_is_what_is_served(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)
        (first,) = list_pending_decisions(wiki)
        path = wiki / PRESENTATIONS_FILENAME
        row = json.loads(path.read_text())
        row["presentation"]["job"] = "Stored wording that hosts must not re-word."
        path.write_text(json.dumps(row) + "\n")

        (second,) = list_pending_decisions(wiki)

        assert second["presentation"]["job"] == "Stored wording that hosts must not re-word."
        assert second["fingerprint"] != first["fingerprint"]

    def test_change_to_a_decision_relevant_field_invalidates_stored_framing(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)
        (first,) = list_pending_decisions(wiki)
        path = wiki / PRESENTATIONS_FILENAME
        row = json.loads(path.read_text())
        row["presentation"]["job"] = "STALE"
        path.write_text(json.dumps(row) + "\n")
        # change the question text of the underlying block
        pq = wiki / "_pending_questions.md"
        pq.write_text(pq.read_text().replace("Which figure holds?", "Which figure is right?"))

        (second,) = list_pending_decisions(wiki)

        assert second["summary"] != first["summary"]
        assert second["presentation"]["job"] != "STALE"
        assert second["fingerprint"] != first["fingerprint"]
        stored = load_presentations(wiki)
        assert stored[second["id"]]["presentation"]["job"] != "STALE"

    def test_resolved_items_are_pruned_from_the_store(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)
        list_pending_decisions(wiki)
        (wiki / "_pending_questions.md").write_text("")
        assert list_pending_decisions(wiki) == []
        assert load_presentations(wiki) == {}

    def test_unwritable_store_degrades_persistence_not_the_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import decision_presentations as dpres
        from athenaeum.decisions import list_pending_decisions

        wiki = self._wiki(tmp_path)

        def deny(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(dpres, "atomic_write_text", deny)
        (item,) = list_pending_decisions(wiki)
        assert item["presentation"]["job"].strip()

    def test_list_pending_decisions_page_items_carry_the_contract(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions_page

        wiki = self._wiki(tmp_path)
        page = list_pending_decisions_page(wiki)
        for item in page["items"]:
            for key in (
                "contract_version",
                "state",
                "hold_reason",
                "answerable",
                "presentation",
                "fingerprint",
                "priority",
            ):
                assert key in item
