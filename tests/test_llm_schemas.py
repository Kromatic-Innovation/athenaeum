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
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from athenaeum import contradictions, llm_schemas, push_metrics, query_topics, resolutions, spend
from athenaeum.config import resolve_cache_dir
from athenaeum.llm_schemas import SCHEMA_MISMATCH_MARKER
from athenaeum.models import CLAIM_KINDS
from athenaeum.resolutions import _VALID_ACTIONS
from athenaeum.tiers import parse_tier2_entities
from tests.conftest import FakeLLMClient

_SCHEMA_LOGGER = "athenaeum.llm_schemas"


def _mismatch_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if SCHEMA_MISMATCH_MARKER in r.getMessage()]


@pytest.fixture(autouse=True)
def _isolate_observation_ledger(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the athenaeum#724 observation ledger at a per-test tmp cache dir, so
    observe() writes never touch the real ``~/.cache/athenaeum`` during the run.

    This module's tests specifically exercise the ledger (they assert
    observations ARE recorded), so — unlike the rest of the suite — it opts
    back IN to recording explicitly: the repo-wide ``tests/conftest.py``
    ``_isolate_cache_dir`` autouse fixture (athenaeum#750) defaults
    ``ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0`` for every test, so this
    re-enables it here. ``ATHENAEUM_CACHE_DIR`` is re-pointed too (own tmp dir
    per test via ``tmp_path_factory``, distinct from the global fixture's
    ``tmp_path``) so this module's ledger reads/writes stay self-contained.
    """
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path_factory.mktemp("obs-cache")))
    monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1")


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
    # Uses tiers.tier3-merge (not tiers.tier2) for this general "post-hoc
    # per-item extra key" path: athenaeum#1035 (M17 phase 2a) tightened
    # tiers.tier2 to extra="forbid", so an extra key there now fails
    # validation up front (see TestPhase2aStrictness) rather than reaching
    # the model_extra-based [].* reporting this test exercises.
    with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
        llm_schemas.observe_tier3_merge_ops(
            [{"op": "append_section", "text": "x", "surprise": 9}], call_site="t"
        )
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


# ---------------------------------------------------------------------------
# M17 phase 2a (athenaeum#1035) — per-contract strictness decision for
# tiers.tier2 (now extra="forbid") and tiers.tier3-merge (extra="allow"
# confirmed, unchanged). Every other contract's posture must be untouched.
# ---------------------------------------------------------------------------


class TestPhase2aStrictness:
    def test_strict_contracts_registry_matches_the_decision(self) -> None:
        assert llm_schemas.STRICT_CONTRACTS == frozenset(
            {"tiers.tier2", "tiers.tier3-merge"}
        )
        # the three starved contracts and query_topics must NOT be in the
        # decided set — AC4 (they stay observe-only, decision deferred to athenaeum#608)
        assert not llm_schemas.STRICT_CONTRACTS & {
            "query_topics",
            "claim_kind",
            "contradictions",
            "resolutions",
        }

    # --- tiers.tier2: strict-pass / strict-fail on extra="forbid" -----------

    def test_tier2_clean_payload_is_strict_pass(self) -> None:
        # Only documented keys present — validates cleanly under extra="forbid".
        llm_schemas.Tier2ClassifyResponse.model_validate(
            [{"name": "Acme", "entity_type": "org", "access": "internal"}]
        )

    def test_tier2_extra_key_is_strict_fail(self) -> None:
        # Before athenaeum#1035 this validated cleanly (extra="allow") and only the
        # key NAME was reported via model_extra; now it fails validation
        # outright — the decided "teeth where mismatch is ~0%" posture.
        with pytest.raises(ValidationError):
            llm_schemas.Tier2ClassifyResponse.model_validate(
                [{"name": "Acme", "surprise": 1}]
            )

    def test_tier2_extra_key_observed_as_validation_error_not_extra_keys(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # observe()'s WARNING classification changes shape (ValidationError
        # instead of the old post-hoc ExtraKeys report) but the ledger's
        # mismatch CLASS is still extra-keys either way — same signal, new path.
        with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
            llm_schemas.observe_tier2_classify(
                [{"name": "Acme", "surprise": 1}], call_site="t"
            )
        recs = _mismatch_records(caplog)
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "error_class=ValidationError" in msg
        assert "surprise" in msg
        agg = llm_schemas.aggregate_observations()
        assert agg["tiers.tier2"]["by_class"].get(llm_schemas.MISMATCH_EXTRA_KEYS) == 1

    def test_tier2_missing_name_is_still_strict_fail(self) -> None:
        # Unchanged: name was already required pre-athenaeum#1035, and it is the one
        # field with a confirmed missing-required posture (0 instances observed,
        # but the framework's own rule is that a required field STAYS required).
        with pytest.raises(ValidationError):
            llm_schemas.Tier2ClassifyResponse.model_validate([{"entity_type": "org"}])

    def test_tier2_pipeline_stays_behavior_neutral_under_the_tightened_schema(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # AC5: the schema tightened, but parse_tier2_entities' own hand-rolled
        # per-item tolerance is untouched — observe() still never gates the
        # pipeline, even for a contract in STRICT_CONTRACTS.
        text = '[{"name": "Acme", "surprise": 1}]'
        with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
            results = parse_tier2_entities(
                text, "sessions/x.md", ["person", "reference"], [], ["internal", "external"]
            )
        assert [e.name for e in results] == ["Acme"]
        assert _mismatch_records(caplog)  # still observed and logged

    # --- tiers.tier3-merge: strict-pass (extra tolerated) / strict-fail -----

    def test_tier3_merge_extra_key_shapes_from_the_window_are_strict_pass(self) -> None:
        # [].text2 and [].append_section are the two extra-key shapes actually
        # observed in the measured window — both must still validate cleanly
        # (extra="allow" confirmed, not tightened).
        llm_schemas.Tier3MergeOpsResponse.model_validate(
            [{"op": "append_section", "text": "New.", "text2": "extra"}]
        )
        llm_schemas.Tier3MergeOpsResponse.model_validate(
            [{"op": "append_section", "text": "New.", "append_section": True}]
        )

    def test_tier3_merge_missing_op_is_strict_fail(self) -> None:
        # The one missing-required hit in the measured window: "0.op: Field
        # required". Confirms `op` stays required.
        with pytest.raises(ValidationError):
            llm_schemas.Tier3MergeOpsResponse.model_validate(
                [{"anchor": "x", "text": "y"}]
            )

    def test_tier3_merge_missing_op_mismatch_class_is_missing_required(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_SCHEMA_LOGGER):
            llm_schemas.observe_tier3_merge_ops([{"anchor": "x", "text": "y"}], call_site="t")
        agg = llm_schemas.aggregate_observations()
        assert (
            agg["tiers.tier3-merge"]["by_class"].get(llm_schemas.MISMATCH_MISSING_REQUIRED) == 1
        )

    # --- regression guard: the other four contracts are untouched (AC4) -----

    @pytest.mark.parametrize(
        "model",
        [
            llm_schemas.ClaimKindResponse,
            llm_schemas.ContradictionResponse,
            llm_schemas.ResolutionResponse,
        ],
    )
    def test_other_contracts_extra_policy_is_unchanged(self, model: type) -> None:
        assert model.model_config.get("extra") == "allow"

    def test_query_topics_and_starved_contracts_still_tolerate_extra_shape(self) -> None:
        # query_topics is a RootModel[list[str]] with no extra-key concept;
        # the three starved BaseModel contracts must still accept an
        # unexpected key without raising (untouched by athenaeum#1035).
        llm_schemas.ClaimKindResponse.model_validate({"claim_kind": "fact", "new_field": 1})
        llm_schemas.ContradictionResponse.model_validate({"detected": False, "new_field": 1})
        llm_schemas.ResolutionResponse.model_validate(
            {"action": "keep_a", "new_field": 1}
        )


# ---------------------------------------------------------------------------
# Observation ledger — denominator, class-tagged mismatches, parse-fail
# counting, and the aggregation (issue athenaeum#724)
# ---------------------------------------------------------------------------
#
# Before athenaeum#724 the instrument logged only ON a mismatch, with no
# denominator, and a total parse failure returned BEFORE reaching observe() — so
# the athenaeum#608 measurement got "0 across every contract" and could not tell 0/0 from
# 0/400. These pin the three defects fixed: every observation is counted, every
# mismatch carries its class, and a parse failure is a countable record.


class TestObservationLedger:
    def test_clean_observation_is_counted_as_the_denominator(self) -> None:
        llm_schemas.observe_contradictions(
            {"detected": False, "rationale": "ok"}, call_site="t"
        )
        agg = llm_schemas.aggregate_observations()
        assert agg["contradictions"]["observations"] == 1
        assert agg["contradictions"]["mismatches"] == 0
        assert agg["contradictions"]["mismatch_rate"] == 0.0
        assert agg["contradictions"]["no_data"] is False

    def test_zero_over_zero_is_no_data_not_zero_percent(self) -> None:
        # AC2: a contract with 0 observations is reportable as *no data*,
        # distinctly from 0 mismatches over 400.
        agg = llm_schemas.aggregate_observations()
        assert agg["resolutions"]["no_data"] is True
        assert agg["resolutions"]["mismatch_rate"] is None
        # …and every instrumented contract is present (explicit, not silently
        # absent) — including claim_kind, which has no production caller.
        assert set(llm_schemas.INSTRUMENTED_CONTRACTS) <= set(agg)
        assert agg["claim_kind"]["no_data"] is True

    @pytest.mark.parametrize(
        "payload,expected_class",
        [
            ({}, llm_schemas.MISMATCH_MISSING_REQUIRED),
            ({"claim_kind": 123}, llm_schemas.MISMATCH_WRONG_TYPE),
            ({"claim_kind": "fact", "surprise": 1}, llm_schemas.MISMATCH_EXTRA_KEYS),
        ],
    )
    def test_mismatch_carries_its_class(self, payload: dict, expected_class: str) -> None:
        # AC3: extra-keys / missing-required / wrong-type each tagged.
        llm_schemas.observe_claim_kind(payload, call_site="t")
        agg = llm_schemas.aggregate_observations()
        assert agg["claim_kind"]["by_class"].get(expected_class) == 1
        assert agg["claim_kind"]["mismatches"] == 1

    def test_parse_failure_is_counted(self) -> None:
        # AC1: a total parse failure is a countable record with class parse-fail.
        llm_schemas.observe_parse_failure(
            contract="contradictions", call_site="t", detail="no-json"
        )
        agg = llm_schemas.aggregate_observations()
        assert agg["contradictions"]["observations"] == 1
        assert agg["contradictions"]["by_class"].get(llm_schemas.MISMATCH_PARSE_FAIL) == 1

    def test_aggregation_over_a_run_has_nonzero_denominators(self) -> None:
        # AC7: an aggregation over a run shows non-zero denominators per contract
        # and at least one deliberately-induced parse-fail counted.
        for _ in range(5):
            llm_schemas.observe_contradictions(
                {"detected": False, "rationale": "ok"}, call_site="t"
            )
        llm_schemas.observe_query_topics(["a", "b"], call_site="t")
        llm_schemas.observe_parse_failure(
            contract="contradictions", call_site="t", detail="no-json"
        )
        agg = llm_schemas.aggregate_observations()
        assert agg["contradictions"]["observations"] == 6  # 5 clean + 1 parse-fail
        assert agg["query_topics"]["observations"] == 1
        assert agg["contradictions"]["by_class"][llm_schemas.MISMATCH_PARSE_FAIL] == 1
        # every contract with traffic has a real denominator to divide by
        assert agg["contradictions"]["mismatch_rate"] == pytest.approx(1 / 6)

    def test_ledger_records_no_field_values_only_shape(self) -> None:
        # Redaction: the ledger carries field paths, error messages, and KEY
        # names — never a field VALUE, so no claim content or personal data.
        llm_schemas.observe_contradictions(
            {"detected": False, "rationale": "SECRET-CONTENT", "leaked_secret": "hunter2"},
            call_site="t",
        )
        rows = llm_schemas.read_observations()
        blob = "\n".join(str(r) for r in rows)
        assert "leaked_secret" in blob  # the KEY name is recorded (extra-keys)
        assert "hunter2" not in blob  # the VALUE is NOT
        assert "SECRET-CONTENT" not in blob

    def test_disabled_env_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "0")
        llm_schemas.observe_contradictions(
            {"detected": False, "rationale": "ok"}, call_site="t"
        )
        assert llm_schemas.read_observations() == []

    def test_observe_never_changes_behavior_or_raises(self) -> None:
        # Behavior neutrality preserved: observe returns None and never raises,
        # even when the ledger path is unwritable (best-effort).
        assert llm_schemas.observe_contradictions({"detected": True}, call_site="t") is None


class TestParseGuardCallSitesCountFailures:
    """AC1 pin: feeding UNPARSEABLE text to the real parse sites — which return
    ABOVE observe() — now produces a countable parse-fail record. Before
    athenaeum#724 these returns were structurally invisible to the instrument.
    """

    def test_contradictions_parse_response_counts_a_parse_failure(self) -> None:
        from athenaeum import contradictions

        result = contradictions._parse_response("this is not json at all", [])
        # Behavior unchanged: still the detector-returned-no-json fallback.
        assert result.detected is False
        assert result.rationale == "detector-returned-no-json"
        # …and it is now counted.
        agg = llm_schemas.aggregate_observations()
        assert agg["contradictions"]["by_class"].get(llm_schemas.MISMATCH_PARSE_FAIL) == 1

    def test_claim_kind_classify_counts_a_parse_failure(self) -> None:
        from unittest.mock import MagicMock

        from athenaeum import claim_kind

        client = MagicMock()
        response = MagicMock()
        response.content = [MagicMock(text="no json object here, just prose")]
        response.usage = MagicMock(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        client.messages.create.return_value = response

        # Behavior unchanged: unparseable → unclassified "".
        assert claim_kind.classify_claim_kind("Some memory body.", client) == ""
        # …and the parse failure is now counted.
        agg = llm_schemas.aggregate_observations()
        assert agg["claim_kind"]["by_class"].get(llm_schemas.MISMATCH_PARSE_FAIL) == 1


# ---------------------------------------------------------------------------
# Test-suite isolation from the production ledger (issue athenaeum#750)
# ---------------------------------------------------------------------------
#
# Before athenaeum#750, ``tests/conftest.py`` isolated nothing: any test that drove
# a parse site through ``observe()``/``record_observation()`` with no explicit
# ``cache_dir`` fell through ``resolve_cache_dir``'s ``arg > ATHENAEUM_CACHE_DIR
# env > default`` order to the real ``~/.cache/athenaeum``, appending to the
# operator's production ``_llm_schema_observations.jsonl`` on every test run.
# The two tests below pin the fix from opposite ends: the first proves the
# no-arg resolution lands under the test's own tmp dir (unit-level); the
# second proves it end-to-end by spawning a REAL, separate pytest process —
# deliberately with none of THIS test's env overrides inherited — and
# confirming the real ledger file is untouched by it.


class TestObservationsPathIsolation:
    def test_observations_path_no_arg_resolves_under_test_tmp_dir(self) -> None:
        """AC3 regression pin: ``observations_path()`` with NO argument must
        resolve under a test-owned tmp dir, never the real user cache dir,
        because an autouse fixture has already pointed ``ATHENAEUM_CACHE_DIR``
        at one — the repo-wide ``tests/conftest.py`` ``_isolate_cache_dir``
        fixture (athenaeum#750) for the suite generally, or this module's own
        ``_isolate_observation_ledger`` override here specifically. Either
        way, the property under test is "the env var wins over the real
        default", so the assertions read the env var pytest actually set
        rather than hardcoding either fixture's tmp path.
        """
        resolved = llm_schemas.observations_path()
        real_default = Path("~/.cache/athenaeum").expanduser()
        assert resolved != real_default / llm_schemas.OBSERVATIONS_FILENAME
        assert resolved.name == llm_schemas.OBSERVATIONS_FILENAME
        # It must live under whatever ATHENAEUM_CACHE_DIR an autouse fixture
        # pointed at (this module's own, or the repo-wide one in
        # tests/conftest.py — either way, the point is "the env var wins,
        # and the env var is a test tmp dir"), and that dir must NOT be the
        # real home cache dir.
        env_cache_dir = Path(os.environ["ATHENAEUM_CACHE_DIR"]).expanduser()
        assert env_cache_dir != real_default
        assert resolved == env_cache_dir / llm_schemas.OBSERVATIONS_FILENAME
        assert resolve_cache_dir(cache_dir=None) == env_cache_dir

    # AC1/AC2 (athenaeum#791): the pattern below was originally pinned to ONE
    # cache-dir artifact (``_llm_schema_observations.jsonl``, athenaeum#750).
    # "the count is the audit trail" applies just as much to the OTHER
    # artifacts a no-arg cache-dir resolution can escape into — ``spend.jsonl``
    # (athenaeum#776) and, per the evidence that opened athenaeum#791,
    # ``_push_records.jsonl`` / ``_push_references.jsonl``. Three independent
    # ledgers hitting the same escape is the signal that the invariant is
    # "no cache-dir artifact leaks", not "this one file doesn't leak" — so this
    # canary is parametrized over every artifact instead of naming one.
    _NESTED_POLLUTION_CASES = [
        pytest.param(
            llm_schemas.OBSERVATIONS_FILENAME,
            textwrap.dedent(
                """
                from athenaeum.llm_schemas import record_observation

                def test_calls_record_observation_with_no_explicit_cache_dir():
                    # No cache_dir kwarg — exercises the SAME no-arg resolution
                    # path production call sites use, so this only stays out of
                    # the real ledger if the child process's own conftest.py
                    # isolation (session AND per-test) fired.
                    record_observation(
                        contract="query_topics",
                        call_site="nested-pytest-pollution-probe-791",
                        outcome="ok",
                    )
                """
            ),
            id="llm_schema_observations",
        ),
        pytest.param(
            spend.LEDGER_FILENAME,
            textwrap.dedent(
                """
                from athenaeum.models import TokenUsage
                from athenaeum.spend import record_spend

                def test_calls_record_spend_with_no_explicit_cache_dir():
                    # Unlike observations, the spend ledger has no separate
                    # "disabled under test" flag — it is protected ONLY by the
                    # cache-dir redirect, so this genuinely exercises it.
                    wrote = record_spend(
                        TokenUsage(input_tokens=1, output_tokens=1, api_calls=1),
                        run_type="nested-pytest-pollution-probe-791",
                        provider="claude-cli",
                    )
                    assert wrote is True
                """
            ),
            id="spend",
        ),
        pytest.param(
            push_metrics.PUSH_RECORDS_FILENAME,
            textwrap.dedent(
                """
                from athenaeum.push_metrics import build_push_record, record_push

                def test_calls_record_push_with_no_explicit_cache_dir():
                    # No cache_dir kwarg — the exact no-arg resolution the
                    # athenaeum#791 defect took (75 synthetic rows in the
                    # real ~/.cache/athenaeum/_push_records.jsonl).
                    record = build_push_record(
                        session_id="nested-pytest-pollution-probe-791",
                        query="probe query",
                        backend="fts5",
                        hits=[
                            ("probe.md", {"uid": "probe0001"}, "probe snippet text"),
                        ],
                    )
                    assert record_push(record) is True
                """
            ),
            id="push_records",
        ),
        pytest.param(
            push_metrics.REFERENCE_RECORDS_FILENAME,
            textwrap.dedent(
                """
                from athenaeum.push_metrics import (
                    ReferenceResult,
                    record_reference_result,
                )

                def test_calls_record_reference_result_with_no_explicit_cache_dir():
                    result = ReferenceResult(
                        session_id="nested-pytest-pollution-probe-791",
                        ts="2026-01-01T00:00:00Z",
                        pushed_ids=["probe0001"],
                        referenced_ids=["probe0001"],
                    )
                    assert record_reference_result(result) is True
                """
            ),
            id="push_references",
        ),
    ]

    @pytest.mark.parametrize("artifact_filename, child_body", _NESTED_POLLUTION_CASES)
    def test_nested_pytest_run_does_not_pollute_real_cache_dir_artifact(
        self, artifact_filename: str, child_body: str
    ) -> None:
        """AC1/AC2: running the suite writes ZERO records into any REAL
        ``~/.cache/athenaeum`` cache-dir artifact — proven by spawning a
        genuinely separate ``pytest`` child process (not merely inspecting
        fixture code) for each artifact, snapshotting that artifact's real
        line count (or its absence) before and after, and asserting it is
        unchanged.

        Each child's test calls the artifact's own production write function
        with NO explicit ``cache_dir``, so if ``tests/conftest.py``'s
        isolation (the whole-suite ``pytest_configure`` redirect, and/or the
        per-test ``_isolate_cache_dir`` fixture) did not fire in the child
        process, the record would land in the real ledger — exactly the
        athenaeum#750/#776/#791 defect class. The child is launched with this
        test's own ``ATHENAEUM_CACHE_DIR`` / ``ATHENAEUM_SPEND_LEDGER`` /
        observations-enabled env overrides stripped (not just left as
        inherited monkeypatch state), so the child's OWN conftest.py is what
        has to do the isolating — proving the mechanism itself, not this
        test's env.
        """
        real_artifact = Path("~/.cache/athenaeum").expanduser() / artifact_filename

        def _line_count() -> int:
            if not real_artifact.exists():
                return 0
            return len(real_artifact.read_text(encoding="utf-8").splitlines())

        before = _line_count()

        # The child test file MUST live inside the real tests/ directory (not
        # under tmp_path) so that pytest's normal conftest.py auto-loading
        # picks up the REAL tests/conftest.py — that isolation is the exact
        # mechanism under test. tmp_path is a sibling directory pytest would
        # not associate with tests/conftest.py at all, which would prove
        # nothing. Cleaned up in `finally` regardless of outcome.
        repo_root = Path(__file__).resolve().parent.parent
        tests_dir = repo_root / "tests"
        case_slug = artifact_filename.strip("_").split(".")[0]
        child_test_path = tests_dir / f"test__nested_pollution_probe_791_{case_slug}.py"
        assert not child_test_path.exists(), (
            f"stray probe file already present at {child_test_path}; "
            "a previous run may have crashed before cleanup"
        )
        child_test_path.write_text(child_body, encoding="utf-8")

        child_env = dict(os.environ)
        child_env.pop("ATHENAEUM_CACHE_DIR", None)
        child_env.pop("ATHENAEUM_SPEND_LEDGER", None)
        child_env.pop("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", None)

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    str(child_test_path),
                ],
                cwd=repo_root,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            child_test_path.unlink(missing_ok=True)

        assert result.returncode == 0, (
            f"nested pytest child failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        after = _line_count()
        assert after == before, (
            "nested pytest run polluted the REAL cache-dir artifact "
            f"({real_artifact}): line count went from {before} to {after}"
        )


# ---------------------------------------------------------------------------
# durable_observations_path — issue athenaeum#980 AC4: the R3
# operational/store-durable relocation seam. NOT wired to observe()'s
# scattered call sites in this slice (see athenaeum.store.ARTIFACT_REGISTRY's
# "llm-schema-observations-ledger" entry) — this test covers the resolver
# capability itself.
# ---------------------------------------------------------------------------


class TestDurableObservationsPath:
    def test_fresh_store_resolves_to_wiki_root(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        resolved = llm_schemas.durable_observations_path(wiki_root, cache_dir=cache_dir)
        assert resolved == wiki_root / llm_schemas.OBSERVATIONS_FILENAME

    def test_legacy_store_falls_back_to_cache_dir(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        legacy = cache_dir / llm_schemas.OBSERVATIONS_FILENAME
        legacy.write_text('{"v":1}\n', encoding="utf-8")
        resolved = llm_schemas.durable_observations_path(wiki_root, cache_dir=cache_dir)
        assert resolved == legacy

    def test_no_split_brain_on_a_fresh_store(self, tmp_path: Path) -> None:
        """The production WRITE path (record_observation, as observe()/
        observe_parse_failure() call it) and the production READ path
        (read_observations) must agree on where a fresh store's ledger
        lives — issue athenaeum#980 AC4."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"

        llm_schemas.record_observation(
            contract="split-brain-probe",
            call_site="test",
            outcome="ok",
            cache_dir=cache_dir,
            wiki_root=wiki_root,
        )

        rows = llm_schemas.read_observations(cache_dir, wiki_root=wiki_root)
        assert any(r.get("contract") == "split-brain-probe" for r in rows)

        # A read that forgets wiki_root= must not silently see the same
        # records via the old cache-dir default.
        stale = llm_schemas.read_observations(cache_dir)
        assert stale == []
