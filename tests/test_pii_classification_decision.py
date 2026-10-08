# SPDX-License-Identifier: Apache-2.0
"""Tests for the pii-classification framed-decision schema (issue athenaeum#689 AC2)."""

from __future__ import annotations

from athenaeum import decision_framing
from athenaeum.pii_classification_decision import (
    CLASS_IS_PII,
    CLASS_NOT_PII_ALIAS,
    CLASS_NOT_PII_CALENDAR_ID,
    CLASS_NOT_PII_PAGE_PURPOSE,
    CLASS_NOT_PII_TEST_ACCOUNT,
    DECISION_TYPE,
    PII_CLASSES,
    build_context_bundle,
    is_not_pii_verdict,
    is_pii_verdict,
    response_schema,
    validate_answer,
)


def test_conforming_not_pii_answer_validates() -> None:
    assert validate_answer({"verdict": CLASS_NOT_PII_ALIAS}) == []


def test_conforming_is_pii_answer_with_note_validates() -> None:
    errors = validate_answer({"verdict": CLASS_IS_PII, "note": "migrate it"})
    assert errors == []


def test_every_policy_class_validates() -> None:
    for verdict_class in PII_CLASSES:
        assert validate_answer({"verdict": verdict_class}) == []


def test_missing_verdict_is_rejected() -> None:
    errors = validate_answer({"note": "no verdict given"})
    assert errors
    assert any("required" in e for e in errors)


def test_unknown_verdict_value_is_rejected() -> None:
    errors = validate_answer({"verdict": "maybe-pii"})
    assert errors


def test_additional_property_is_rejected() -> None:
    errors = validate_answer({"verdict": CLASS_IS_PII, "extra": "nope"})
    assert errors


def test_verdict_must_be_a_string_not_an_object() -> None:
    # Mirrors athenaeum.decision_framing's coordinate-schema constraint: the
    # inbound applier only ever forwards str(parsed["verdict"]), so a
    # structured verdict must never validate.
    errors = validate_answer({"verdict": {"class": CLASS_IS_PII}})
    assert errors


def test_response_schema_is_deep_copied() -> None:
    schema_a = response_schema()
    schema_a["properties"]["verdict"]["enum"].append("poisoned")
    schema_b = response_schema()
    assert "poisoned" not in schema_b["properties"]["verdict"]["enum"]


def test_class_membership_partitions_not_pii_from_pii() -> None:
    for cls in (
        CLASS_NOT_PII_ALIAS,
        CLASS_NOT_PII_CALENDAR_ID,
        CLASS_NOT_PII_PAGE_PURPOSE,
        CLASS_NOT_PII_TEST_ACCOUNT,
    ):
        assert is_not_pii_verdict(cls)
        assert not is_pii_verdict(cls)
    assert is_pii_verdict(CLASS_IS_PII)
    assert not is_not_pii_verdict(CLASS_IS_PII)


def test_build_context_bundle_is_structured_not_free_text() -> None:
    bundle = build_context_bundle(
        value="user@example-host.test",
        page_id="auto-example-page",
        sentence="The remote is configured as user@example-host.test.",
        page_purpose="automation setup notes",
    )
    assert bundle == {
        "value": "user@example-host.test",
        "page_id": "auto-example-page",
        "sentence": "The remote is configured as user@example-host.test.",
        "page_purpose": "automation setup notes",
    }


def test_build_context_bundle_omits_page_purpose_when_unknown() -> None:
    bundle = build_context_bundle(
        value="user@example-host.test",
        page_id="auto-example-page",
        sentence="some sentence",
    )
    assert "page_purpose" not in bundle


def test_decision_type_is_not_registered_in_type_framing() -> None:
    # AC2's scoping note: authoring the schema discharges this AC; nothing
    # imports it into the decision-queue path. If a future change registers
    # "pii-classification" in _TYPE_FRAMING, that is deliberate wiring that
    # belongs to issue athenaeum#717, not a silent regression here.
    assert DECISION_TYPE not in decision_framing._TYPE_FRAMING


def test_module_is_not_imported_by_decision_framing() -> None:
    import athenaeum.decision_framing as module

    assert "pii_classification_decision" not in module.__dict__
