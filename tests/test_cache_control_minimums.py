# SPDX-License-Identifier: Apache-2.0
"""Every ``cache_control`` breakpoint must be able to actually engage (athenaeum#927).

A ``cache_control`` block on a prefix shorter than the serving model's minimum
cacheable length is **accepted by the API and then silently ignored**. There is no
error, no warning, and no counter that distinguishes "this breakpoint is working"
from "this breakpoint is inert" other than ``cache_creation_input_tokens`` staying
0 forever — which reads identically to "this run made no calls".

That silence is why athenaeum#790's detector breakpoint shipped inert and survived two
days of metered runs: 630 tokens marked cacheable, sent to Haiku 4.5, whose floor
is 4,096. Nothing in the codebase could have caught it, because nothing checked the
prefix length against the model it was actually sent to.

These tests close that gap offline. They assert the property against the model each
prompt's knob ACTUALLY resolves to (via ``librarian._resolve_run_models``, the same
resolver athenaeum#783's spend preflight uses) rather than against a hardcoded
threshold — the floor is per-model and non-monotonic across generations, so a
constant would be wrong the moment a knob moves.

**No LLM calls and no ledger writes (athenaeum#776's regression class, AC 6).** Every
assertion here is a pure function of prompt bytes plus the two lookup tables; the
suite's session-scoped ``ATHENAEUM_SPEND_LEDGER`` redirect in ``conftest.py`` is
pinned explicitly by :func:`test_suite_ledger_is_redirected_away_from_operator`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from athenaeum.librarian import _resolve_run_models
from athenaeum.models import (
    _MIN_CACHEABLE_PREFIX_TOKENS,
    estimate_prompt_tokens,
    min_cacheable_prefix_tokens,
)
from athenaeum.prompt_registry import PROMPT_META, PROMPTS

# The knob -> model mapping for a default run. Resolved once: these are
# env/yaml/default lookups, and every test wants the same view of "what this run
# would actually serve traffic with".
_RUN_MODELS = dict(_resolve_run_models(None))

_SRC = Path(__file__).resolve().parents[1] / "src" / "athenaeum"

#: Matches an actual breakpoint in a request payload (``"cache_control": {...}``)
#: rather than any mention of the words. The modules below discuss cache_control
#: at length in comments explaining why they do NOT set one, so a bare substring
#: search would flag exactly the documentation these tests require.
_BREAKPOINT_RE = re.compile(r'"cache_control"\s*:')


def _sets_breakpoint(module: str) -> bool:
    return bool(_BREAKPOINT_RE.search((_SRC / f"{module}.py").read_text("utf-8")))


# --------------------------------------------------------------------------- #
# The core property (AC 2).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name", [n for n, m in PROMPT_META.items() if m.cacheable], ids=str
)
def test_cacheable_prompt_clears_its_models_minimum(name: str) -> None:
    """A prompt marked cacheable must clear the floor of the model it is sent to."""
    meta = PROMPT_META[name]
    model = _RUN_MODELS.get(meta.knob)
    assert model, f"{name}: knob {meta.knob!r} resolved to no model"

    minimum = min_cacheable_prefix_tokens(model)
    assert minimum is not None, (
        f"{name}: knob {meta.knob!r} resolves to {model!r}, which has no recorded "
        "minimum cacheable prefix. An UNKNOWN floor is not a permission slip — add "
        "the model to models._MIN_CACHEABLE_PREFIX_TOKENS (verify the figure against "
        "the claude-api skill, per hestia#1055) before marking a prompt cacheable "
        "for it."
    )

    estimated = estimate_prompt_tokens(PROMPTS[name])
    assert estimated >= minimum, (
        f"{name} carries a cache_control breakpoint but its prefix is ~{estimated} "
        f"tokens, below the {minimum}-token minimum for {model!r} (knob "
        f"{meta.knob!r}). The API would accept this breakpoint and silently ignore "
        "it — cache_creation_input_tokens stays 0 with no error (athenaeum#790/#927). "
        "Either lengthen the stable prefix past the floor, or drop the breakpoint "
        "and set cacheable=False in the prompt registry."
    )


def test_at_least_one_prompt_is_actually_cached() -> None:
    """Guard the guard: a registry with nothing cacheable would pass vacuously.

    ``resolutions.resolve_system`` is the one prompt long enough to clear its
    model's floor, and issue athenaeum#927's live probe confirmed it caches
    (create-then-read on two successive calls). If this fails, either that
    breakpoint was removed or the registry flag was flipped — both are real
    changes to athenaeum's cache posture and should be reviewed, not silently
    absorbed by a parametrized test that now has zero cases.
    """
    cacheable = [n for n, m in PROMPT_META.items() if m.cacheable]
    assert cacheable == ["resolutions.resolve_system"], (
        "the set of cache_control-marked prompts changed; confirm each entry "
        f"clears its model's minimum before updating this pin: {cacheable}"
    )


# --------------------------------------------------------------------------- #
# The registry cannot drift from the call sites.
# --------------------------------------------------------------------------- #


def test_source_cache_control_sites_match_the_registry() -> None:
    """Every ``cache_control`` in a prompt-owning module is a declared, cacheable row.

    Without this, a future call site could reintroduce an inert breakpoint and the
    parametrized property test above would never see it — it only iterates rows the
    registry already knows about.
    """
    owners = sorted({meta.module.split(".", 1)[1] for meta in PROMPT_META.values()})
    with_breakpoints = {module for module in owners if _sets_breakpoint(module)}
    declared = {
        meta.module.split(".", 1)[1]
        for meta in PROMPT_META.values()
        if meta.cacheable
    }
    assert with_breakpoints == declared, (
        "modules whose source sets cache_control do not match the modules with a "
        f"cacheable=True registry row (source={sorted(with_breakpoints)}, "
        f"registry={sorted(declared)}). A breakpoint that is not declared in the "
        "registry is not checked against its model's minimum — declare it."
    )


def test_detector_breakpoint_stays_removed() -> None:
    """Regression pin for athenaeum#927's actual fix.

    The detector prompt is 630 tokens and runs on Haiku 4.5 (floor 4,096), so it
    cannot be cached at any point in the foreseeable model lineup. Re-adding the
    breakpoint would restore a marking that reports success and does nothing.
    """
    assert not _sets_breakpoint("contradictions"), (
        "contradictions.py set a cache_control breakpoint again. _DETECT_SYSTEM is "
        "630 tokens against Haiku 4.5's 4,096-token floor — it cannot engage. See "
        "the DELIBERATELY UNCACHED note at the detector call site (athenaeum#927)."
    )


def test_entity_phase_prompts_are_documented_as_uncached() -> None:
    """AC 3: the entity phase's posture is recorded in-source, not merely absent."""
    source = (_SRC / "tiers.py").read_text(encoding="utf-8")
    assert not _sets_breakpoint("tiers")
    assert "DELIBERATELY UNCACHED" in source, (
        "tiers.py must keep the in-source note explaining why the entity-phase "
        "prompts carry no cache_control breakpoint (athenaeum#927 AC 3) — 'no "
        "breakpoint' and 'nobody considered a breakpoint' must stay distinguishable."
    )


# --------------------------------------------------------------------------- #
# The two lookup primitives.
# --------------------------------------------------------------------------- #


def test_haiku_45_floor_is_4096_not_2048() -> None:
    """The specific model fact athenaeum#927's own issue body got wrong.

    The issue reasoned against "Haiku's 2048-token minimum". 2,048 is Haiku *3.5*;
    Haiku *4.5* — the model the detector actually runs on — is 4,096. The verdict
    was unchanged (630 is below both), but the constant matters: a future prompt
    sized to clear 2,048 would still be silently inert.
    """
    assert min_cacheable_prefix_tokens("claude-haiku-4-5-20251001") == 4096
    assert min_cacheable_prefix_tokens("claude-3-5-haiku-20241022") == 2048


def test_floor_is_not_monotonic_across_generations() -> None:
    """Newer is not always lower — the reason this must be a table, not a formula."""
    assert min_cacheable_prefix_tokens("claude-opus-5") == 512
    assert min_cacheable_prefix_tokens("claude-opus-4-8") == 1024
    assert min_cacheable_prefix_tokens("claude-opus-4-7") == 2048
    assert min_cacheable_prefix_tokens("claude-opus-4-6") == 4096
    # The cheapest current model has the HIGHEST floor of any of them.
    assert min_cacheable_prefix_tokens("claude-haiku-4-5") == 4096


def test_floor_lookup_is_longest_prefix_and_fails_closed() -> None:
    assert min_cacheable_prefix_tokens("claude-sonnet-5-20260101") == 1024
    # Unknown / unset must read as UNKNOWN (None), never as "no minimum" (0).
    assert min_cacheable_prefix_tokens("some-other-vendor-model") is None
    assert min_cacheable_prefix_tokens(None) is None
    assert min_cacheable_prefix_tokens("") is None


def test_every_run_knob_model_has_a_recorded_floor() -> None:
    """Each model a default run serves traffic with must have a known floor.

    Mirrors athenaeum#783's unpriced-model preflight: a knob resolving to a model
    absent from the table means the cacheability of anything sent through it is
    un-assertable, which is how an inert breakpoint hides.
    """
    unknown = {
        knob: model
        for knob, model in _RUN_MODELS.items()
        if min_cacheable_prefix_tokens(model) is None
    }
    assert not unknown, (
        f"knobs resolving to models with no recorded minimum cacheable prefix: "
        f"{unknown}. Add them to models._MIN_CACHEABLE_PREFIX_TOKENS."
    )


@pytest.mark.parametrize(
    ("chars", "measured_tokens"),
    [
        # The two live count_tokens measurements recorded in issue athenaeum#927.
        (11728, 4395),  # resolutions._RESOLVE_SYSTEM, claude-sonnet-5
        (2344, 630),  # contradictions._DETECT_SYSTEM, claude-haiku-4-5
    ],
)
def test_estimate_is_a_lower_bound_against_live_measurements(
    chars: int, measured_tokens: int
) -> None:
    """The estimator must never over-count, or it could certify an inert breakpoint.

    Calibration against real tokenizer output: both prompts are DENSER than 4.0
    chars/token (2.67 and 3.72), so dividing by 4.0 under-counts both. Under-
    counting can only refuse to certify a breakpoint that would have worked; it can
    never certify one that is inert.
    """
    assert estimate_prompt_tokens("x" * chars) <= measured_tokens


def test_estimate_matches_live_measurement_for_the_real_prompts() -> None:
    """Pin the two real prompts, not just synthetic strings of the same length."""
    assert estimate_prompt_tokens(PROMPTS["resolutions.resolve_system"]) <= 4395
    assert estimate_prompt_tokens(PROMPTS["contradictions.detect_system"]) <= 630


def test_min_cacheable_table_covers_every_priced_model_family() -> None:
    """The three model-fact tables are maintained together (models.py's comment).

    A model priced but with no recorded cache floor is exactly the drift that
    athenaeum#777 hit between the pricing and sampling-parameter tables.
    """
    from athenaeum.models import _MODEL_RATES_USD_PER_MTOK

    missing = [
        prefix
        for prefix in _MODEL_RATES_USD_PER_MTOK
        if min_cacheable_prefix_tokens(prefix) is None
    ]
    assert not missing, (
        "priced model prefixes with no recorded minimum cacheable prefix: "
        f"{missing}. models.py's table comment requires the three tables be "
        "maintained together."
    )


def test_recorded_floors_are_plausible_powers_of_two() -> None:
    """Cheap shape check on hand-maintained model facts."""
    for prefix, minimum in _MIN_CACHEABLE_PREFIX_TOKENS.items():
        assert minimum in (512, 1024, 2048, 4096), (
            f"{prefix}: {minimum} is not one of the documented tiers"
        )


# --------------------------------------------------------------------------- #
# AC 6 — no ledger writes.
# --------------------------------------------------------------------------- #


def test_suite_ledger_is_redirected_away_from_operator(tmp_path: Path) -> None:
    """The spend ledger must never resolve to the operator's real file (athenaeum#776).

    ``conftest.py`` redirects ``ATHENAEUM_SPEND_LEDGER`` session-wide, before
    collection. Pinning it here means AC 6 is asserted rather than assumed: these
    tests make no LLM calls at all, so they write no records regardless, but the
    redirect is what makes that true for the suite as a whole.
    """
    ledger = os.environ.get("ATHENAEUM_SPEND_LEDGER")
    assert ledger, "conftest must set ATHENAEUM_SPEND_LEDGER for the whole suite"
    resolved = Path(ledger).expanduser()
    assert not resolved.is_relative_to(Path.home() / ".cache" / "athenaeum"), (
        f"spend ledger resolves into the operator's real cache dir: {resolved}"
    )
    assert re.search(r"(pytest|tmp)", str(resolved), re.IGNORECASE), (
        f"spend ledger is not under a pytest tmp dir: {resolved}"
    )
