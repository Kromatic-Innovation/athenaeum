# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.llm_schemas — observe-only LLM response validation (athenaeum#570, M17).

The load-bearing guarantee of phase 1 is **behavior neutrality**: schema
validation observes and logs, but NEVER changes what a call site does with a
response. The regression tests below feed a schema-violating response through
each in-scope parse site and assert (a) the downstream result is exactly what
the site produced before this issue, and (b) a mismatch WARNING was emitted —
so validation is provably running AND provably inert.
"""

from __future__ import annotations

import logging
from typing import get_args

import pytest

from athenaeum import contradictions, llm_schemas, query_topics, resolutions
from athenaeum.llm_schemas import SCHEMA_MISMATCH_MARKER
from athenaeum.models import CLAIM_KINDS
from athenaeum.resolutions import _VALID_ACTIONS
from athenaeum.tiers import parse_tier2_entities
from tests.conftest import FakeLLMClient

_SCHEMA_LOGGER = "athenaeum.llm_schemas"


def _mismatch_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if SCHEMA_MISMATCH_MARKER in r.getMessage()]


# ---------------------------------------------------------------------------
# Vocabulary equivalence — the two Literals are stated locally (import hygiene)
# but MUST equal their live source sets. A drift fails CI here, not silently in
# production.
# ---------------------------------------------------------------------------


def test_claim_kind_literal_matches_live_set() -> None:
    field = llm_schemas.ClaimKindResponse.model_fields["claim_kind"]
    assert set(get_args(field.annotation)) == set(CLAIM_KINDS)


def test_resolver_action_literal_matches_live_set() -> None:
    field = llm_schemas.ResolutionResponse.model_fields["action"]
    assert set(get_args(field.annotation)) == set(_VALID_ACTIONS)


# ---------------------------------------------------------------------------
# observe() — the entry point: logs on mismatch/extra-keys, silent on a clean
# match, and NEVER raises.
# ---------------------------------------------------------------------------


def test_observe_silent_on_valid_payload(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_claim_kind({"claim_kind": "fact"}, call_site="t")
    assert _mismatch_records(caplog) == []


def test_observe_logs_on_invalid_value(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_claim_kind({"claim_kind": "not-a-kind"}, call_site="t")
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert "contract=claim_kind" in msg
    assert "error_class=ValidationError" in msg
    assert "claim_kind" in msg  # the failing field path is present


def test_observe_reports_unexpected_top_level_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_claim_kind({"claim_kind": "fact", "brand_new_field": 1}, call_site="t")
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert "error_class=ExtraKeys" in msg
    assert "brand_new_field" in msg


def test_observe_reports_per_item_extra_key_in_array(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_tier2_classify([{"name": "Acme", "surprise": 9}], call_site="t")
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    assert "[].surprise" in recs[0].getMessage()


@pytest.mark.parametrize("payload", [None, object(), 42, "a string", {"x": object()}])
def test_observe_never_raises_on_garbage(payload: object) -> None:
    # Every observe_* wrapper must swallow anything — observation cannot become
    # a new failure mode on the pipeline.
    llm_schemas.observe_query_topics(payload, call_site="t")
    llm_schemas.observe_claim_kind(payload, call_site="t")
    llm_schemas.observe_contradictions(payload, call_site="t")
    llm_schemas.observe_resolutions(payload, call_site="t")
    llm_schemas.observe_tier2_classify(payload, call_site="t")
    llm_schemas.observe_tier3_merge_ops(payload, call_site="t")


def test_valid_merge_op_with_anchor_and_text_is_not_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # anchor/text are documented op fields, not "unexpected" keys — a valid op
    # must produce NO mismatch, or every successful merge would flood the log.
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_tier3_merge_ops(
            [{"op": "insert_after", "anchor": "x", "text": "y"}], call_site="t"
        )
    assert _mismatch_records(caplog) == []


# ---------------------------------------------------------------------------
# Behavior neutrality — a schema-violating response STILL produces the identical
# downstream result the site produced before athenaeum#570, AND logs a mismatch. This is
# the regression guard for the no-behavior-change acceptance criterion.
# ---------------------------------------------------------------------------


def test_query_topics_schema_violation_is_behavior_neutral(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # A non-string element (123) and an empty string violate the list[str]
    # contract; today's site keeps "ok" and drops the other two.
    fake = FakeLLMClient(text='["ok", 123, ""]')
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", fake)

    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        topics = query_topics.extract_topics("Tell me about the Acme deal")

    assert topics == ["ok"]  # identical to pre-athenaeum#570 behavior
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    assert "contract=query_topics" in recs[0].getMessage()


def test_contradictions_schema_violation_is_behavior_neutral(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Missing the required `detected` field violates the schema; today's site
    # reads a falsy `detected` and returns detected=False with the rationale.
    text = '{"rationale": "no clear conflict"}'
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        result = contradictions._parse_response(text, members=[])

    assert result.detected is False
    assert result.rationale == "no clear conflict"
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    assert "contract=contradictions" in recs[0].getMessage()


def test_resolutions_schema_violation_is_behavior_neutral(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An out-of-vocabulary action violates the Literal; today's site falls back
    # to retain_both_with_context @ confidence 0.0.
    text = '{"action": "totally_invalid", "confidence": 0.9}'
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        result = resolutions._parse_response(text)

    assert result.action == "retain_both_with_context"
    assert result.recommended_winner == "neither"
    assert result.confidence == 0.0
    recs = _mismatch_records(caplog)
    assert len(recs) == 1
    assert "contract=resolutions" in recs[0].getMessage()


def test_tier2_schema_violation_is_behavior_neutral(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # First entity is missing the required `name` (schema violation → today's
    # site skips it); second is valid but carries an unexpected field. The
    # returned entities must be exactly the valid one, unchanged.
    text = '[{"entity_type": "person"}, {"name": "Acme", "brand_new": 1}]'
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        results = parse_tier2_entities(
            text,
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal", "external"],
        )

    assert [e.name for e in results] == ["Acme"]
    assert results[0].entity_type == "reference"  # defaulted, as today
    recs = _mismatch_records(caplog)
    assert recs, "expected at least one schema-mismatch WARNING"
    assert all("contract=tiers.tier2" in r.getMessage() for r in recs)
