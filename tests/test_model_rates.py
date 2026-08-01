# SPDX-License-Identifier: Apache-2.0
"""Model rate-table + sampling-capability guards (issue #577, epic #516 B1).

``_MODEL_RATES_USD_PER_MTOK`` is the single site pricing model tokens; a
default that resolves to no prefix falls through to the blended fallback and
silently under-reports spend into the ceilings, drain's dollar guard, and the
published ledger. These tests are the forward guard: they fail loudly if a
future ``DEFAULT_*_MODEL`` is added or moved to an unpriced (or
un-sampling-classified) id, BEFORE the mispricing reaches a financial consumer.
"""

from __future__ import annotations

import importlib

import pytest

from athenaeum.models import (
    _BLENDED_INPUT_USD_PER_MTOK,
    _BLENDED_OUTPUT_USD_PER_MTOK,
    _rates_for_model,
    _sampling_params_rejected,
)

_BLENDED = (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)

# Every model-priced default in the codebase, named explicitly (module,
# constant) so a reader sees at a glance which defaults are covered and an
# un-aliasing later cannot silently drop coverage. DEFAULT_EMBEDDING_MODEL is
# deliberately excluded — it is a local sentence-transformer, not an
# Anthropic-API-priced model.
_API_MODEL_DEFAULTS = [
    ("athenaeum.resolutions", "DEFAULT_RESOLVE_MODEL"),
    ("athenaeum.tiers", "DEFAULT_CLASSIFY_MODEL"),
    ("athenaeum.tiers", "DEFAULT_WRITE_MODEL"),
    ("athenaeum.query_topics", "DEFAULT_TOPIC_MODEL"),
    ("athenaeum.reasoning_tiers", "DEFAULT_T1_MODEL"),
    ("athenaeum.reasoning_tiers", "DEFAULT_T2_MODEL"),
    ("athenaeum.contradictions", "DEFAULT_CONTRADICTION_MODEL"),
]


def _resolve_default(module_name: str, const_name: str) -> str:
    module = importlib.import_module(module_name)
    return getattr(module, const_name)


# ---------------------------------------------------------------------------
# Rate table
# ---------------------------------------------------------------------------


def test_claude_5_family_prefixes_priced() -> None:
    # The two additions this issue lands. Opus 5 at the standard Opus-tier
    # $5/$25; Sonnet 5 at the STANDARD $3/$15 (not the introductory $2/$10 —
    # the table has no time dimension and standard errs toward over-reporting).
    assert _rates_for_model("claude-opus-5") == (5.0, 25.0)
    assert _rates_for_model("claude-sonnet-5") == (3.0, 15.0)


def test_dated_5_family_ids_resolve_via_prefix() -> None:
    # A future dated snapshot still resolves through the prefix match.
    assert _rates_for_model("claude-opus-5-20260901") == (5.0, 25.0)
    assert _rates_for_model("claude-sonnet-5-20260815") == (3.0, 15.0)


@pytest.mark.parametrize("module_name,const_name", _API_MODEL_DEFAULTS)
def test_every_default_resolves_to_non_blended_rate(
    module_name: str, const_name: str
) -> None:
    # THE GUARD: every default must price to a real prefix rate, never the
    # blended fallback. Asserts != blended (not a hardcoded rate) so the test
    # survives a legitimate future price change.
    model = _resolve_default(module_name, const_name)
    rates = _rates_for_model(model)
    assert rates != _BLENDED, (
        f"{module_name}.{const_name} = {model!r} falls through to the blended "
        f"fallback {_BLENDED} — add a prefix to _MODEL_RATES_USD_PER_MTOK or it "
        "will silently under-report spend."
    )


def test_unknown_id_still_returns_blended_fallback() -> None:
    # The fallback must not be removed — an id matching no prefix (proxy route,
    # a model we have not priced yet) is priced at the blended pair, not crashed.
    assert _rates_for_model("some-unknown-model") == _BLENDED
    assert _rates_for_model(None) == _BLENDED


def test_longest_prefix_precedence_holds() -> None:
    # claude-opus-5 and claude-opus-4 are disjoint prefixes — a 5-family id must
    # not be captured by the 4-family prefix (or vice versa).
    assert _rates_for_model("claude-opus-5") == (5.0, 25.0)
    assert _rates_for_model("claude-opus-4-8") == (5.0, 25.0)
    assert _rates_for_model("claude-sonnet-5") == (3.0, 15.0)
    assert _rates_for_model("claude-sonnet-4-6") == (3.0, 15.0)
    assert _rates_for_model("claude-haiku-4-5-20251001") == (1.0, 5.0)


# ---------------------------------------------------------------------------
# Sampling-parameter capability (epic #515 deliverable 4; #573 reads this)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,rejected",
    [
        ("claude-opus-5", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-7", True),
        ("claude-sonnet-5", True),
        ("claude-fable-5", True),
        ("claude-haiku-4-5", False),
        ("claude-haiku-4-5-20251001", False),
        ("claude-sonnet-4-6", False),
    ],
)
def test_sampling_capability_classification(model: str, rejected: bool) -> None:
    # temperature/top_p/top_k return 400 on the 4.7+/5-family surface and are
    # accepted on Haiku 4.5 / Sonnet 4.6. This is a declaration only — athenaeum
    # sends no sampling parameters.
    assert _sampling_params_rejected(model) is rejected


@pytest.mark.parametrize("module_name,const_name", _API_MODEL_DEFAULTS)
def test_every_default_has_a_sampling_classification(
    module_name: str, const_name: str
) -> None:
    # Forward guard mirroring the rate guard: every current default must be
    # classified by the sampling table (not None) so a future default cannot
    # silently fall outside the recorded request-surface knowledge.
    model = _resolve_default(module_name, const_name)
    assert _sampling_params_rejected(model) is not None, (
        f"{module_name}.{const_name} = {model!r} is not classified by "
        "_SAMPLING_PARAMS_REJECTED_PREFIXES — add its prefix."
    )


def test_sampling_unknown_id_is_none() -> None:
    assert _sampling_params_rejected("some-unknown-model") is None
    assert _sampling_params_rejected(None) is None
