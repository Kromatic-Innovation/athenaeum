# SPDX-License-Identifier: Apache-2.0
"""Config-owned per-MTok pricing + startup preflight (issue athenaeum#783).

Covers the full slice: ``config.resolve_model_rates`` (yaml ``pricing:``
parsing, malformed-entry handling mirroring
``provider.resolve_max_tokens``'s convention), ``models.configure_model_rates``
/ ``model_has_price`` / ``default_model_rates`` (REPLACE, not overlay,
semantics — see the issue's "Design decision"), ``config.preflight_model_rates``
(the loud startup check), the ``athenaeum run`` wiring in
``librarian._run_preconditions`` / ``librarian._resolve_run_models``, and
``athenaeum init``'s default config shipping the current rate table.

No live API calls; every LLM interaction is stubbed or never reached (the
preflight fires before any client is built).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from athenaeum.config import (
    load_config,
    preflight_model_rates,
    resolve_model_rates,
    write_default_config,
)
from athenaeum.models import (
    TokenUsage,
    configure_model_rates,
    default_model_rates,
    model_has_price,
)

# ---------------------------------------------------------------------------
# resolve_model_rates -- yaml `pricing:` section parsing
# ---------------------------------------------------------------------------


class TestResolveModelRates:
    def test_none_config_returns_empty(self) -> None:
        assert resolve_model_rates(None) == {}

    def test_no_pricing_key_returns_empty(self) -> None:
        assert resolve_model_rates({"models": {"write": "x"}}) == {}

    def test_non_dict_pricing_section_returns_empty(self) -> None:
        assert resolve_model_rates({"pricing": "oops"}) == {}

    def test_valid_entry_parsed(self) -> None:
        rates = resolve_model_rates({"pricing": {"claude-opus-5": [7.0, 35.0]}})
        assert rates == {"claude-opus-5": (7.0, 35.0)}

    def test_int_values_coerced_to_float(self) -> None:
        rates = resolve_model_rates({"pricing": {"claude-opus-5": [7, 35]}})
        assert rates == {"claude-opus-5": (7.0, 35.0)}

    def test_multiple_entries_all_parsed(self) -> None:
        rates = resolve_model_rates(
            {
                "pricing": {
                    "claude-opus-5": [7.0, 35.0],
                    "claude-haiku-4-5": [1.0, 5.0],
                }
            }
        )
        assert rates == {
            "claude-opus-5": (7.0, 35.0),
            "claude-haiku-4-5": (1.0, 5.0),
        }

    def test_non_string_prefix_key_ignored(self) -> None:
        assert resolve_model_rates({"pricing": {1: [1.0, 2.0]}}) == {}

    @pytest.mark.parametrize(
        "bad_value",
        [
            [1.0],
            [1.0, 2.0, 3.0],
            ["a", 2.0],
            [True, 2.0],
            [1.0, False],
            "not-a-list",
            None,
        ],
        ids=[
            "too-few",
            "too-many",
            "non-numeric",
            "bool-first",
            "bool-second",
            "not-a-list",
            "none",
        ],
    )
    def test_malformed_entry_warns_and_is_rejected(
        self, bad_value: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="athenaeum")
        rates = resolve_model_rates({"pricing": {"claude-opus-5": bad_value}})
        assert rates == {}
        assert any("malformed" in r.getMessage().lower() for r in caplog.records)

    def test_negative_rate_warns_and_is_rejected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="athenaeum")
        rates = resolve_model_rates({"pricing": {"claude-opus-5": [-1.0, 5.0]}})
        assert rates == {}
        assert any("negative" in r.getMessage().lower() for r in caplog.records)

    def test_valid_and_malformed_entries_coexist(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One bad entry does not poison the rest of the table."""
        caplog.set_level(logging.WARNING, logger="athenaeum")
        rates = resolve_model_rates(
            {
                "pricing": {
                    "claude-opus-5": [7.0, 35.0],
                    "claude-broken": ["a", "b"],
                }
            }
        )
        assert rates == {"claude-opus-5": (7.0, 35.0)}


# ---------------------------------------------------------------------------
# models.configure_model_rates / model_has_price / default_model_rates --
# REPLACE (not overlay) semantics
# ---------------------------------------------------------------------------


class TestConfigureModelRates:
    def test_default_model_rates_returns_a_copy(self) -> None:
        rates = default_model_rates()
        rates["claude-opus-5"] = (999.0, 999.0)
        assert default_model_rates()["claude-opus-5"] == (5.0, 25.0)

    def test_out_of_the_box_active_table_is_code_default(self) -> None:
        """No configure_model_rates call in this test -- the conftest
        autouse fixture reset the active table after the PREVIOUS test, so
        this one starts from the code default, matching every existing
        caller that never loads config."""
        assert model_has_price("claude-opus-5")
        assert model_has_price("claude-sonnet-5")

    def test_ac1_yaml_override_changes_estimated_cost_usd(self) -> None:
        """athenaeum#783 AC1: setting ``pricing.claude-opus-5: [7.0, 35.0]``
        changes ``TokenUsage.estimated_cost_usd`` accordingly."""
        configure_model_rates(
            resolve_model_rates({"pricing": {"claude-opus-5": [7.0, 35.0]}})
        )
        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000, model="claude-opus-5")
        # $7/M input + $35/M output = $42, not the code default $30.
        assert abs(usage.estimated_cost_usd - 42.0) < 0.01

    def test_replace_not_overlay_other_prefixes_become_unpriced(self) -> None:
        """athenaeum#783 "Design decision": yaml REPLACES the whole table -- a
        prefix a non-empty yaml ``pricing:`` section omits is NOT backfilled
        from the code default (an overlay was explicitly rejected)."""
        configure_model_rates({"claude-opus-5": (7.0, 35.0)})
        assert model_has_price("claude-opus-5")
        assert not model_has_price("claude-sonnet-5")  # priced by default, not here

    def test_empty_rates_resets_to_code_default(self) -> None:
        configure_model_rates({"claude-opus-5": (7.0, 35.0)})
        assert not model_has_price("claude-sonnet-5")
        configure_model_rates(None)
        assert model_has_price("claude-sonnet-5")
        assert model_has_price("claude-opus-5")

    def test_configure_is_unconditional_no_stale_leak(self) -> None:
        """A call with an empty table always resets — it must never leave a
        PRIOR call's custom table in effect (the athenaeum#776 cross-test-leak
        failure mode, applied to this new global)."""
        configure_model_rates({"claude-opus-5": (7.0, 35.0)})
        configure_model_rates({})
        assert model_has_price("claude-sonnet-5")

    def test_untagged_tokens_still_blend_under_a_custom_table(self) -> None:
        """athenaeum#783 AC4: untagged tokens still price at the blended rate,
        unchanged — even once a custom pricing table is active."""
        configure_model_rates({"claude-opus-5": (7.0, 35.0)})
        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000)  # no model= kwarg
        assert abs(usage.estimated_cost_usd - 9.0) < 0.01  # unchanged blended rate


class TestModelHasPrice:
    def test_known_model_has_price(self) -> None:
        assert model_has_price("claude-opus-5")

    def test_dated_id_matches_by_longest_prefix(self) -> None:
        assert model_has_price("claude-haiku-4-5-20251001")

    def test_unknown_model_has_no_price(self) -> None:
        """athenaeum#783 AC3 support: a genuinely unmatched tagged model reports
        NO price — model_has_price never treats the blended fallback as a
        real price, which is exactly what lets the preflight catch it."""
        assert model_has_price("some-proxy-model-x") is False


# ---------------------------------------------------------------------------
# config.preflight_model_rates -- the loud startup check
# ---------------------------------------------------------------------------


class TestPreflightModelRates:
    def test_all_priced_returns_none(self) -> None:
        assert (
            preflight_model_rates(
                [
                    ("write", "claude-sonnet-5"),
                    ("classify", "claude-haiku-4-5-20251001"),
                ]
            )
            is None
        )

    def test_empty_resolved_models_returns_none(self) -> None:
        assert preflight_model_rates([]) is None

    def test_unpriced_model_names_model_and_config_key(self) -> None:
        msg = preflight_model_rates([("write", "claude-unreleased-9")])
        assert msg is not None
        assert "claude-unreleased-9" in msg
        assert "write" in msg
        assert "pricing." in msg

    def test_multiple_unpriced_all_listed(self) -> None:
        msg = preflight_model_rates(
            [("write", "claude-unreleased-9"), ("topic", "claude-another-x")]
        )
        assert msg is not None
        assert "claude-unreleased-9" in msg
        assert "claude-another-x" in msg

    def test_respects_the_active_table_not_just_code_default(self) -> None:
        """A model priced under the code default but dropped by a REPLACE
        (issue athenaeum#783 design decision) must trip the preflight too."""
        configure_model_rates({"claude-opus-5": (7.0, 35.0)})
        msg = preflight_model_rates([("write", "claude-sonnet-5")])
        assert msg is not None


# ---------------------------------------------------------------------------
# librarian._resolve_run_models -- drift guard against the knob registry
# ---------------------------------------------------------------------------


class TestResolveRunModelsMatchesKnobRegistry:
    def test_knob_set_matches_prompt_registry(self) -> None:
        """Drift guard: the athenaeum#783 preflight's knob coverage must match
        exactly the knob set athenaeum.prompt_registry.KNOBS defines (issue
        athenaeum#781's single source of truth for the knob set) -- a new knob
        registered there without a matching getter in _resolve_run_models
        would silently escape the preflight."""
        from athenaeum.librarian import _resolve_run_models
        from athenaeum.prompt_registry import KNOBS

        resolved = _resolve_run_models(None)
        knobs = [knob for knob, _ in resolved]
        assert sorted(knobs) == sorted(KNOBS)
        assert len(knobs) == len(set(knobs))  # no knob listed twice

    def test_every_resolved_model_is_priced_by_default(self) -> None:
        """The code-default table (shipped via athenaeum init) must cover
        every knob's default model, or every fresh install would fail the
        preflight out of the box."""
        from athenaeum.librarian import _resolve_run_models

        for knob, model in _resolve_run_models(None):
            assert model_has_price(model), f"{knob} default {model!r} is unpriced"


# ---------------------------------------------------------------------------
# librarian._LIBRARIAN_ROUTED_KNOBS -- drift guard for the athenaeum#1174 derivation
# ---------------------------------------------------------------------------


class TestLibrarianRoutedKnobsDerivation:
    """athenaeum#1174: ``_LIBRARIAN_ROUTED_KNOBS`` is derived from
    ``prompt_registry.KNOBS`` minus an EXPLICIT, NAMED exclusion set
    (``_LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS``), not hand-maintained. These
    tests pin the three-way relationship the issue's AC asks for: routed +
    excluded == the full registered knob set, with no overlap and no gap --
    so an eighth knob added to ``_META_ROWS`` without an explicit routing
    decision fails a test instead of silently landing (or not landing) in
    the client-cache pipeline.
    """

    def test_routed_plus_excluded_equals_registry_knobs(self) -> None:
        from athenaeum.librarian import (
            _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS,
            _LIBRARIAN_ROUTED_KNOBS,
        )
        from athenaeum.prompt_registry import KNOBS

        assert set(_LIBRARIAN_ROUTED_KNOBS) | _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS == set(
            KNOBS
        )

    def test_routed_and_excluded_are_disjoint(self) -> None:
        from athenaeum.librarian import (
            _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS,
            _LIBRARIAN_ROUTED_KNOBS,
        )

        assert not (set(_LIBRARIAN_ROUTED_KNOBS) & _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS)

    def test_no_knob_listed_twice_in_routed(self) -> None:
        from athenaeum.librarian import _LIBRARIAN_ROUTED_KNOBS

        assert len(_LIBRARIAN_ROUTED_KNOBS) == len(set(_LIBRARIAN_ROUTED_KNOBS))

    def test_topic_and_rule_proposals_are_the_excluded_knobs(self) -> None:
        """Pins the CURRENT exclusion set by name (not just its size) --
        the trap the issue's Occam pre-flight warned about was a naive
        ``_LIBRARIAN_ROUTED_KNOBS = prompt_registry.KNOBS`` silently
        folding ``topic`` (already deliberately excluded, issue athenaeum#786)
        and ``rule_proposals`` (default-off phase, issue athenaeum#1063) into
        the entity/merge pipeline's per-knob client cache."""
        from athenaeum.librarian import _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS

        assert _LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS == frozenset({"topic", "rule_proposals"})


# ---------------------------------------------------------------------------
# athenaeum init -- default config ships the current rate table
# ---------------------------------------------------------------------------


class TestDefaultConfigContainsPricingTable:
    def test_pricing_section_present(self, tmp_path: Path) -> None:
        path = write_default_config(tmp_path)
        assert "pricing:" in path.read_text()

    def test_pricing_section_round_trips_to_the_code_table(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#783 AC6: a fresh athenaeum init's config, parsed back through
        the REAL yaml loader and resolver, matches
        models.default_model_rates() exactly -- not string-matched, so this
        fails if the generated block is ever malformed or hand-edited out of
        sync with the code table."""
        write_default_config(tmp_path)
        cfg = load_config(tmp_path)
        assert resolve_model_rates(cfg) == default_model_rates()


# ---------------------------------------------------------------------------
# End-to-end: librarian.run() wiring (issue athenaeum#783 AC2)
# ---------------------------------------------------------------------------


def _seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "t"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


class TestRunPreflightsPricing:
    def test_unpriced_resolved_model_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """athenaeum#783 AC2: a run whose resolved models include an unpriced id
        exits non-zero at startup, with a message naming the model and the
        config key to set -- it does not start and then under-report."""
        from athenaeum.librarian import run

        root = _seed_root(tmp_path)
        monkeypatch.setenv("ATHENAEUM_WRITE_MODEL", "claude-unreleased-9")
        caplog.set_level(logging.ERROR, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
        )

        assert rc == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "claude-unreleased-9" in m and "pricing." in m for m in messages
        ), messages

    def test_priced_run_does_not_trip_the_pricing_preflight(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from athenaeum.librarian import run

        root = _seed_root(tmp_path)
        monkeypatch.delenv("ATHENAEUM_WRITE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
        )

        # dry_run waives the ANTHROPIC_API_KEY gate; no raw files means the
        # run completes cleanly. rc == 0 proves the pricing preflight did not
        # trip on the default-priced models.
        assert rc == 0

    def test_yaml_pricing_override_wires_through_a_real_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A full pricing: section written to athenaeum.yaml (as athenaeum
        init would ship it) must not trip the preflight for any default
        knob model -- the common, expected operator path."""
        from athenaeum.librarian import run

        root = _seed_root(tmp_path)
        write_default_config(root)
        monkeypatch.delenv("ATHENAEUM_WRITE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
        )

        assert rc == 0
