# SPDX-License-Identifier: Apache-2.0
"""Adaptive-thinking model-capability table + write-knob downgrade rule
(issue athenaeum#1336).

The ``write`` knob's three call sites (``tiers.tier3_create_params`` /
``tier3_merge_params`` / ``tier3_merge_full_params``) requested
``thinking: {"type": "adaptive"}`` unconditionally, so pointing ``write`` at a
model that cannot honour ``adaptive`` (e.g. ``claude-haiku-4-5``) produced an
HTTP 400 on every single call — see athenaeum#1262's live eval. This module covers,
one class per acceptance criterion:

1. ``models._ADAPTIVE_THINKING_SUPPORTED_PREFIXES`` / ``adaptive_thinking_supported``
   (AC1).
2. ``provider.resolve_thinking``'s code-default downgrade (AC2).
3. ``provider.resolve_thinking``'s explicit-override passthrough (AC3).
4. The anti-recurrence guard across all three write call sites (AC4).
5. No collateral damage to any other ``resolve_thinking`` call site (AC5).
6. An unrecorded model leaves the code default completely unchanged.
"""

from __future__ import annotations

import logging

import pytest

from athenaeum.claim_kind import _get_classify_model as _claim_kind_model
from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from athenaeum.contradictions import DEFAULT_CONTRADICTION_MODEL
from athenaeum.models import (
    _ADAPTIVE_THINKING_SUPPORTED_PREFIXES,
    EntityAction,
    _longest_prefix_value,
    adaptive_thinking_supported,
)
from athenaeum.provider import resolve_thinking
from athenaeum.query_topics import DEFAULT_TOPIC_MODEL
from athenaeum.reasoning_tiers import DEFAULT_T1_MODEL, DEFAULT_T2_MODEL
from athenaeum.resolutions import DEFAULT_RESOLVE_MODEL
from athenaeum.rule_proposals import DEFAULT_RULE_PROPOSALS_MODEL
from athenaeum.tiers import tier3_create_params, tier3_merge_full_params, tier3_merge_params

# ---------------------------------------------------------------------------
# AC1 — the table + its lookup helper.
# ---------------------------------------------------------------------------


class TestAdaptiveThinkingSupportedTable:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-fable-5",
            "claude-fable-5-1",
            "claude-mythos-5",
            "claude-mythos-5-1",
        ],
    )
    def test_recorded_supported_id_is_true(self, model: str) -> None:
        assert adaptive_thinking_supported(model) is True

    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-sonnet-4-0",
            "claude-opus-4-5",
            "claude-opus-4-1",
            "claude-opus-4-0",
            "claude-3-5-haiku",
            "claude-haiku-4",  # family fallback: exactly one member, Haiku 4.5.
            "claude-haiku-3",  # family fallback.
        ],
    )
    def test_recorded_unsupported_id_is_false(self, model: str) -> None:
        assert adaptive_thinking_supported(model) is False

    @pytest.mark.parametrize(
        "model",
        [
            "claude-zzz-9",
            None,
            # claude-opus-4 / claude-sonnet-4 straddle the boundary (some
            # members support adaptive, some don't) so NO family-level entry
            # is recorded for them — the bare family prefix must stay unknown.
            "claude-opus-4",
            "claude-sonnet-4",
        ],
    )
    def test_unrecorded_id_is_none(self, model: str | None) -> None:
        assert adaptive_thinking_supported(model) is None

    def test_longest_prefix_disagreement_resolves_to_the_longer_match(self) -> None:
        table = {"claude-x": False, "claude-x-9": True}
        assert _longest_prefix_value("claude-x-9-foo", table) is True
        assert _longest_prefix_value("claude-x-1", table) is False

    def test_fable_and_mythos_are_true_because_disabled_400s_there(self) -> None:
        # Mirror image of this issue's defect (see the table's header comment):
        # {"type": "disabled"} is REJECTED with a 400 on these four ids, so a
        # False entry would make the downgrade path emit the one value they
        # refuse. Assert directly against the real table, not just the helper.
        for model in (
            "claude-fable-5",
            "claude-fable-5-1",
            "claude-mythos-5",
            "claude-mythos-5-1",
        ):
            assert _ADAPTIVE_THINKING_SUPPORTED_PREFIXES[model] is True


# ---------------------------------------------------------------------------
# AC2 — code-default `adaptive` downgrades to `disabled` on an unsupported
# model, with a WARN naming the knob and the model.
# ---------------------------------------------------------------------------


class TestCodeDefaultDowngrade:
    def test_downgrades_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_MERGE_CREATE_THINKING", raising=False)
        with caplog.at_level(logging.WARNING):
            result = resolve_thinking(
                "merge_create",
                "ATHENAEUM_MERGE_CREATE_THINKING",
                "adaptive",
                model="claude-haiku-4-5",
            )
        assert result == {"type": "disabled"}
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "merge_create" in m and "claude-haiku-4-5" in m and "downgrad" in m for m in messages
        ), messages


# ---------------------------------------------------------------------------
# AC3 — an explicit operator `adaptive` (env or yaml) is NOT downgraded, and
# the two WARN paths are distinguishable.
# ---------------------------------------------------------------------------


class TestExplicitOverrideNeverDowngraded:
    def test_explicit_env_adaptive_is_not_downgraded(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MERGE_CREATE_THINKING", "adaptive")
        with caplog.at_level(logging.WARNING):
            result = resolve_thinking(
                "merge_create",
                "ATHENAEUM_MERGE_CREATE_THINKING",
                "adaptive",
                model="claude-haiku-4-5",
            )
        assert result == {"type": "adaptive"}
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "explicitly requested" in m and "ATHENAEUM_MERGE_CREATE_THINKING" in m for m in messages
        ), messages

    def test_explicit_yaml_adaptive_is_not_downgraded(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_MERGE_CREATE_THINKING", raising=False)
        config = {"thinking": {"merge_create": "adaptive"}}
        with caplog.at_level(logging.WARNING):
            result = resolve_thinking(
                "merge_create",
                "ATHENAEUM_MERGE_CREATE_THINKING",
                "adaptive",
                config,
                model="claude-haiku-4-5",
            )
        assert result == {"type": "adaptive"}
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "explicitly requested" in m and "thinking.merge_create" in m for m in messages
        ), messages

    def test_operator_path_and_default_path_warnings_are_distinguishable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_MERGE_CREATE_THINKING", raising=False)
        with caplog.at_level(logging.WARNING):
            resolve_thinking(
                "merge_create",
                "ATHENAEUM_MERGE_CREATE_THINKING",
                "adaptive",
                model="claude-haiku-4-5",
            )
        default_path_messages = [r.getMessage() for r in caplog.records]
        caplog.clear()

        monkeypatch.setenv("ATHENAEUM_MERGE_CREATE_THINKING", "adaptive")
        with caplog.at_level(logging.WARNING):
            resolve_thinking(
                "merge_create",
                "ATHENAEUM_MERGE_CREATE_THINKING",
                "adaptive",
                model="claude-haiku-4-5",
            )
        operator_path_messages = [r.getMessage() for r in caplog.records]

        assert default_path_messages != operator_path_messages
        assert any("downgrading to 'disabled'" in m for m in default_path_messages)
        assert not any("downgrading to 'disabled'" in m for m in operator_path_messages)
        assert any("explicitly requested" in m for m in operator_path_messages)
        assert not any("explicitly requested" in m for m in default_path_messages)


# ---------------------------------------------------------------------------
# AC4 — the anti-recurrence test: with ATHENAEUM_WRITE_MODEL pointed at a
# model that cannot honour adaptive thinking, all three write call sites must
# build a request carrying {"type": "disabled"}, not "adaptive".
#
# Verified BY HAND to fail on unmodified origin/develop. Two distinct
# pre-fix failures, depending on how much of the change is reverted, and both
# are load-bearing:
#
# - reverting ONLY ``src/athenaeum/tiers.py`` (the model threading) leaves this
#   module importable and fails all three assertions below with
#   `{'type': 'adaptive'} != {'type': 'disabled'}` — the precise defect
#   athenaeum#1262 hit;
# - reverting the whole change fails EARLIER still, at collection, with
#   `ImportError: cannot import name '_ADAPTIVE_THINKING_SUPPORTED_PREFIXES'`.
#
# An anti-recurrence test that would have passed before the fix is worthless;
# this one fails either way.
# ---------------------------------------------------------------------------


def _entity_action(kind: str = "create") -> EntityAction:
    return EntityAction(
        kind=kind,  # type: ignore[arg-type]
        name="Acme Corp",
        entity_type="company",
        tags=[],
        access="internal",
        existing_uid="a1b2c3d4" if kind == "update" else None,
        observations="Acme raised Series C in Q1 2024.",
    )


class TestWriteKnobAntiRecurrence:
    def test_tier3_create_params_downgrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_WRITE_MODEL", "claude-haiku-4-5")
        params = tier3_create_params(_entity_action("create"), "sessions/raw.md")
        assert params["thinking"] == {"type": "disabled"}

    def test_tier3_merge_params_downgrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_WRITE_MODEL", "claude-haiku-4-5")
        params = tier3_merge_params(_entity_action("update"), "Existing body.", "sessions/raw.md")
        assert params["thinking"] == {"type": "disabled"}

    def test_tier3_merge_full_params_downgrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_WRITE_MODEL", "claude-haiku-4-5")
        params = tier3_merge_full_params(
            _entity_action("update"), "Existing body.", "sessions/raw.md"
        )
        assert params["thinking"] == {"type": "disabled"}


# ---------------------------------------------------------------------------
# AC5 — no collateral damage: every OTHER resolve_thinking call site still
# resolves to its declared posture on its own default model.
# ---------------------------------------------------------------------------

# (knob, env_var, code-default posture, the stage's real default model)
_OTHER_STAGES: list[tuple[str, str, str, str]] = [
    ("classify", "ATHENAEUM_CLASSIFY_THINKING", "disabled", DEFAULT_CLASSIFY_MODEL),
    ("topic", "ATHENAEUM_TOPIC_THINKING", "disabled", DEFAULT_TOPIC_MODEL),
    ("claim_kind", "ATHENAEUM_CLAIM_KIND_THINKING", "disabled", _claim_kind_model()),
    ("reasoning_t1", "ATHENAEUM_REASONING_T1_THINKING", "disabled", DEFAULT_T1_MODEL),
    (
        "comparator_content_relation",
        "ATHENAEUM_COMPARATOR_CONTENT_RELATION_THINKING",
        "disabled",
        DEFAULT_CLASSIFY_MODEL,
    ),
    (
        "contradiction_detect",
        "ATHENAEUM_CONTRADICTION_DETECT_THINKING",
        "disabled",
        DEFAULT_CONTRADICTION_MODEL,
    ),
    ("resolve", "ATHENAEUM_RESOLVE_THINKING", "adaptive", DEFAULT_RESOLVE_MODEL),
    ("freetext_edit", "ATHENAEUM_FREETEXT_EDIT_THINKING", "adaptive", DEFAULT_RESOLVE_MODEL),
    ("reasoning_t2", "ATHENAEUM_REASONING_T2_THINKING", "adaptive", DEFAULT_T2_MODEL),
    (
        "rule_proposals",
        "ATHENAEUM_RULE_PROPOSALS_THINKING",
        "adaptive",
        DEFAULT_RULE_PROPOSALS_MODEL,
    ),
]


class TestOtherStagesUnaffected:
    @pytest.mark.parametrize(
        "knob,env_var,default,model", _OTHER_STAGES, ids=[row[0] for row in _OTHER_STAGES]
    )
    def test_stage_resolves_to_its_declared_default_posture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        knob: str,
        env_var: str,
        default: str,
        model: str,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert resolve_thinking(knob, env_var, default, model=model) == {"type": default}


# ---------------------------------------------------------------------------
# 6. An unrecorded model + code-default `adaptive` -> unchanged, no downgrade
#    WARN.
# ---------------------------------------------------------------------------


class TestUnrecordedModelPassthrough:
    def test_unrecorded_model_leaves_adaptive_default_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_THINKING", raising=False)
        with caplog.at_level(logging.WARNING):
            result = resolve_thinking(
                "resolve", "ATHENAEUM_RESOLVE_THINKING", "adaptive", model="claude-zzz-9"
            )
        assert result == {"type": "adaptive"}
        assert not any("athenaeum#1336" in r.getMessage() for r in caplog.records)

    def test_model_none_leaves_adaptive_default_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_THINKING", raising=False)
        with caplog.at_level(logging.WARNING):
            result = resolve_thinking("resolve", "ATHENAEUM_RESOLVE_THINKING", "adaptive")
        assert result == {"type": "adaptive"}
        assert not any("athenaeum#1336" in r.getMessage() for r in caplog.records)
