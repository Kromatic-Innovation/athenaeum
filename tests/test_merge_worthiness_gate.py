# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1172 — the deterministic merge-worthiness containment gate.

Does this raw file say anything new about this entity? If every fact the
file offers is already on the entity's page, the merge is pure cost — a
full-page echo that changes nothing. This gate answers that question with
**zero LLM calls**, and only suppresses a merge on overwhelming evidence of
containment (the AC4 asymmetry: a false suppression permanently destroys a
fact — raw files are unlinked after processing, with no re-derivation path
— while a false pass merely costs one merge call).

This suite covers:
  - the config resolver (``librarian.merge_worthiness_gate_enabled``),
    mirroring the athenaeum#1182 page-size resolvers' validation contract;
  - ``_normalize_for_containment`` and ``_merge_worthiness_fully_contained``
    directly (full containment, partial novelty, empty/vacuous inputs, the
    truncation boundary at ``_MAX_EXISTING_BODY_CHARS``);
  - ``check_merge_worthiness_gate`` itself, including the INFO log line;
  - the real dispatch site, ``tier3_derive_actions``'s "update" branch —
    proving the suppression is REAL (no LLM call) and that the knob being
    off leaves behaviour byte-identical to before this gate existed (AC6);
  - a held-out fixture set of known-good merges that must NEVER be
    suppressed (AC4).

No LLM, no network.
"""

from __future__ import annotations

import inspect
import json
import logging
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.config import resolve_merge_worthiness_gate_enabled
from athenaeum.models import EntityAction, EntityIndex, RawFile
from athenaeum.tiers import (
    _MAX_EXISTING_BODY_CHARS,
    _merge_worthiness_fully_contained,
    _normalize_for_containment,
    check_merge_worthiness_gate,
    tier3_derive_actions,
)


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _update_action(
    name: str = "Acme Corp",
    existing_uid: str = "a1b2c3d4",
    observations: str = "A brand-new observation to merge in.",
) -> EntityAction:
    return EntityAction(
        kind="update",
        name=name,
        entity_type="",
        tags=[],
        access="",
        existing_uid=existing_uid,
        observations=observations,
    )


# ---------------------------------------------------------------------------
# Config resolver
# ---------------------------------------------------------------------------


class TestResolveMergeWorthinessGateEnabled:
    def test_default_off(self) -> None:
        assert resolve_merge_worthiness_gate_enabled(None) is False
        assert resolve_merge_worthiness_gate_enabled({}) is False
        assert resolve_merge_worthiness_gate_enabled({"librarian": {}}) is False

    @pytest.mark.parametrize("token", ["1", "true", "True", "yes", "YES", "on", "On"])
    def test_truthy_env_tokens_enable(self, monkeypatch: pytest.MonkeyPatch, token: str) -> None:
        monkeypatch.setenv("ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED", token)
        assert resolve_merge_worthiness_gate_enabled(None) is True

    def test_env_false_explicit_overrides_yaml_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED", "false")
        cfg = {"librarian": {"merge_worthiness_gate_enabled": True}}
        assert resolve_merge_worthiness_gate_enabled(cfg) is False

    def test_yaml_true_enables(self) -> None:
        cfg = {"librarian": {"merge_worthiness_gate_enabled": True}}
        assert resolve_merge_worthiness_gate_enabled(cfg) is True

    def test_non_bool_yaml_falls_back_to_off(self) -> None:
        cfg = {"librarian": {"merge_worthiness_gate_enabled": "yes"}}
        assert resolve_merge_worthiness_gate_enabled(cfg) is False

    def test_missing_librarian_section_falls_back_to_off(self) -> None:
        assert resolve_merge_worthiness_gate_enabled({"other": {}}) is False

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "merge_worthiness_gate_enabled" not in _DEFAULTS.get("librarian", {})


# ---------------------------------------------------------------------------
# _normalize_for_containment
# ---------------------------------------------------------------------------


class TestNormalizeForContainment:
    def test_casefolds(self) -> None:
        assert _normalize_for_containment("Acme Corp") == "acme corp"

    def test_collapses_whitespace_runs(self) -> None:
        assert _normalize_for_containment("a   b\t\tc\n\nd") == "a b c d"

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert _normalize_for_containment("  hello world  ") == "hello world"

    def test_does_not_strip_markdown_syntax(self) -> None:
        # Deliberate: a unit that fails only on markdown formatting must be
        # dispatched, not suppressed (the safe direction). This function
        # must not remove the '**' or '[^1]'.
        text = "**Acme Corp** raised a round[^1]"
        assert _normalize_for_containment(text) == "**acme corp** raised a round[^1]"


# ---------------------------------------------------------------------------
# _merge_worthiness_fully_contained
# ---------------------------------------------------------------------------


class TestMergeWorthinessFullyContained:
    def test_full_containment(self) -> None:
        body = "Acme Corp is a fintech startup.\nFounded in 2019.\n"
        obs = "Acme Corp is a fintech startup.\nFounded in 2019."
        assert _merge_worthiness_fully_contained(obs, body) is True

    def test_partial_novelty_one_absent_line_fails_whole_check(self) -> None:
        body = "Acme Corp is a fintech startup.\n"
        obs = "Acme Corp is a fintech startup.\nRaised a Series B in 2026."
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_empty_body_never_contains_nonempty_observations(self) -> None:
        assert _merge_worthiness_fully_contained("New fact.", "") is False

    def test_vacuous_observations_returns_true(self) -> None:
        """An empty observation carries no fact to lose (boundary 7)."""
        assert _merge_worthiness_fully_contained("   \n\n  \n", "") is True
        assert _merge_worthiness_fully_contained("", "any body") is True

    def test_blank_lines_are_dropped_before_matching(self) -> None:
        body = "Acme Corp is a fintech startup."
        obs = "Acme Corp is a fintech startup.\n\n   \n"
        assert _merge_worthiness_fully_contained(obs, body) is True

    def test_case_and_whitespace_differences_still_contained(self) -> None:
        body = "Acme   Corp is a FINTECH startup."
        obs = "acme corp is a fintech startup."
        assert _merge_worthiness_fully_contained(obs, body) is True

    def test_no_reordering_credit_is_still_contiguous_substring(self) -> None:
        # Each unit independently checked as a contiguous substring of the
        # window -- reordering within a single line is not "contained"
        # unless it literally appears that way somewhere in the window.
        body = "Founded in 2019. Acme Corp is a fintech startup."
        obs = "Acme Corp is a fintech startup. Founded in 2019."
        assert _merge_worthiness_fully_contained(obs, body) is False

    # -- Truncation boundary (decisive tests for boundary 5) --------------

    def test_truncation_boundary_fact_beyond_window_is_not_contained(self) -> None:
        """The fact's ONLY occurrence starts at index >=
        _MAX_EXISTING_BODY_CHARS in the (hypothetical) untruncated body --
        proven here by simulating what the CALLER's window looks like after
        truncation: the fact is simply absent from the window handed in."""
        fact = "a brand new unique fact xyzzy123"
        padding = "x" * _MAX_EXISTING_BODY_CHARS
        full_body = padding + fact
        # The caller is responsible for truncating; this function only
        # checks what it is given. Confirm the fact is NOT in the
        # truncated window (mirrors what check_merge_worthiness_gate
        # would pass in).
        window = full_body[:_MAX_EXISTING_BODY_CHARS]
        assert fact not in window
        assert _merge_worthiness_fully_contained(fact, window) is False

    def test_truncation_boundary_fact_within_window_is_contained(self) -> None:
        """A fact ending at index <= _MAX_EXISTING_BODY_CHARS - 1 is found
        (proves no off-by-one)."""
        fact = "a brand new unique fact xyzzy123"
        padding = "x" * (_MAX_EXISTING_BODY_CHARS - len(fact))
        full_body = padding + fact
        assert len(full_body) == _MAX_EXISTING_BODY_CHARS
        window = full_body[:_MAX_EXISTING_BODY_CHARS]
        assert fact in window
        assert _merge_worthiness_fully_contained(fact, window) is True


# ---------------------------------------------------------------------------
# check_merge_worthiness_gate
# ---------------------------------------------------------------------------


class TestCheckMergeWorthinessGate:
    def test_full_containment_returns_true(self) -> None:
        action = _update_action(observations="Acme Corp is a fintech startup.")
        existing_body = "Acme Corp is a fintech startup. Founded 2019."
        assert check_merge_worthiness_gate(action, existing_body, "ref", None) is True

    def test_partial_novelty_returns_false(self) -> None:
        action = _update_action(observations="Acme Corp is a fintech startup.\nRaised a Series B.")
        existing_body = "Acme Corp is a fintech startup."
        assert check_merge_worthiness_gate(action, existing_body, "ref", None) is False

    def test_empty_new_page_returns_false(self) -> None:
        action = _update_action(observations="Something new.")
        assert check_merge_worthiness_gate(action, "", "ref", None) is False

    def test_no_client_parameter_at_all(self) -> None:
        """AC5(a): the function signature cannot accept a client --
        architecturally cannot make an LLM call."""
        params = inspect.signature(check_merge_worthiness_gate).parameters
        assert "client" not in params

    def test_reads_only_the_truncated_window_not_full_body(self) -> None:
        """The decisive test for boundary 5: a fact whose ONLY occurrence
        starts beyond _MAX_EXISTING_BODY_CHARS must NOT count as contained,
        even though it genuinely exists later in the full body."""
        fact_line = "a brand new unique fact xyzzy123"
        padding = "y" * _MAX_EXISTING_BODY_CHARS
        existing_body = padding + fact_line
        action = _update_action(observations=fact_line)
        assert check_merge_worthiness_gate(action, existing_body, "ref", None) is False

    def test_fires_info_log_with_expected_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        action = _update_action(name="Acme Corp", observations="Fintech startup.")
        existing_body = "Fintech startup. Founded 2019."
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            result = check_merge_worthiness_gate(action, existing_body, "sessions/x.md", None)
        assert result is True
        [record] = [r for r in caplog.records if "merge-worthiness gate" in r.message]
        assert record.levelno == logging.INFO
        assert "athenaeum#1172" in record.message
        assert "Acme Corp" in record.message
        assert "sessions/x.md" in record.message

    def test_no_log_when_gate_does_not_fire(self, caplog: pytest.LogCaptureFixture) -> None:
        action = _update_action(observations="Something completely absent.")
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            result = check_merge_worthiness_gate(action, "unrelated body", "ref", None)
        assert result is False
        assert not [r for r in caplog.records if "merge-worthiness gate" in r.message]


# ---------------------------------------------------------------------------
# The real dispatch site: tier3_derive_actions's "update" branch
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsMergeWorthinessGate:
    def test_full_containment_suppresses_no_llm_call(self, wiki_dir: Path) -> None:
        """AC2/AC5(b): the binding integration test, mirroring
        test_oversize_page_never_dispatches_a_merge_call. Real wiki_dir,
        page written to disk, MagicMock client with no configured
        response, gate armed -- any call would raise immediately and fail
        this test."""
        existing_body = "Acme Corp is a fintech startup based in Austin."
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(f"""\
                ---
                uid: a1b2c3d4
                type: company
                name: Acme Corp
                ---

                {existing_body}
            """)
        )
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Acme Corp is a fintech startup based in Austin.")
        actions = [_update_action(observations="Acme Corp is a fintech startup based in Austin.")]
        client = MagicMock()  # no return_value/side_effect configured
        config = {"librarian": {"merge_worthiness_gate_enabled": True}}

        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=config
        )

        client.messages.create.assert_not_called()
        assert new_entities == []
        assert pending_updates == []
        assert updated_uids == []
        assert escalations == []

    def test_partial_novelty_dispatches_merge_normally(self, wiki_dir: Path) -> None:
        existing_body = "Acme Corp is a fintech startup based in Austin."
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(f"""\
                ---
                uid: a1b2c3d4
                type: company
                name: Acme Corp
                ---

                {existing_body}
            """)
        )
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Acme Corp just raised a Series B led by Foo Ventures.")
        actions = [
            _update_action(observations="Acme Corp just raised a Series B led by Foo Ventures.")
        ]
        client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=json.dumps({"ops": [{"op": "append_section", "text": "Series B landed."}]})
            )
        ]
        response.stop_reason = "end_turn"
        client.messages.create.return_value = response
        config = {"librarian": {"merge_worthiness_gate_enabled": True}}

        _new, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=config
        )

        client.messages.create.assert_called_once()
        assert updated_uids == ["a1b2c3d4"]
        assert len(pending_updates) == 1
        assert escalations == []

    def test_knob_off_same_fixture_dispatches_normally(self, wiki_dir: Path) -> None:
        """AC6: the SAME fixture that suppresses when armed must dispatch
        normally when the knob is absent/False -- knob off is
        byte-identical to pre-athenaeum#1172 behaviour."""
        existing_body = "Acme Corp is a fintech startup based in Austin."
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(f"""\
                ---
                uid: a1b2c3d4
                type: company
                name: Acme Corp
                ---

                {existing_body}
            """)
        )
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Acme Corp is a fintech startup based in Austin.")
        actions = [_update_action(observations="Acme Corp is a fintech startup based in Austin.")]
        client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(text=json.dumps({"ops": [{"op": "append_section", "text": "Nothing new."}]}))
        ]
        response.stop_reason = "end_turn"
        client.messages.create.return_value = response

        # No config at all -- the default, knob-off path.
        _new, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=None
        )

        client.messages.create.assert_called_once()
        assert updated_uids == ["a1b2c3d4"]
        assert len(pending_updates) == 1


# ---------------------------------------------------------------------------
# AC4 — held-out known-good merges (must NEVER be suppressed)
# ---------------------------------------------------------------------------

_KNOWN_GOOD_MERGES: list[tuple[str, str]] = [
    # Trivially all-new content.
    (
        "Acme Corp announced a new CFO, Jane Smith, effective Q3.",
        "Acme Corp is a fintech startup based in Austin.",
    ),
    (
        "The board approved a $5M budget for the new product line.",
        "Acme Corp is a fintech startup based in Austin. Founded 2019.",
    ),
    (
        "Contact changed: new primary contact is Priya Patel, priya@acme.example.",
        "",
    ),
    # Hard near-duplicates: page already covers most lines, ONE new fact.
    (
        "Acme Corp is a fintech startup based in Austin.\nFounded in 2019.\n"
        "The CEO is now Sam Rivera, replacing the founder.",
        "Acme Corp is a fintech startup based in Austin.\nFounded in 2019.",
    ),
    (
        "Series B closed at $40M, led by Foo Ventures.\n"
        "Foo Ventures previously led the Series A too.",
        "Series B closed at $40M, led by Foo Ventures.",
    ),
    (
        "Headquarters moved to a new office in downtown Austin.\nThe old lease expired in June.",
        "Headquarters moved to a new office in downtown Austin.",
    ),
    (
        "Q3 revenue was $2.1M, up from $1.4M in Q2.\n"
        "Growth is attributed to the new enterprise tier.",
        "Q3 revenue was $2.1M, up from $1.4M in Q2.",
    ),
    (
        "Partnership with Beta Industries announced.\n"
        "The deal includes joint marketing through year-end.",
        "Partnership with Beta Industries announced.",
    ),
    # Line-wrapped sentence: one fact manually hard-wrapped across two
    # short lines -- line-level decomposition is sensitive to this.
    (
        "The company relocated its engineering\nteam to a new office in Denver.",
        "The company relocated its engineering team to somewhere else entirely.",
    ),
    (
        "Annual revenue grew by roughly forty\npercent year over year in 2026.",
        "Annual revenue grew by a different amount in 2025.",
    ),
    (
        "A new hire, Dana Lee, joined as VP of\nEngineering this month.",
        "The VP of Engineering role has been open since March.",
    ),
    # More trivially-new content, varied lengths/shapes.
    (
        "Legal counsel changed from Firm A to Firm B in August.",
        "Acme Corp is a fintech startup.",
    ),
    (
        "The product launched in three new countries: France, Germany, and Spain.",
        "The product launched in the US and UK last year.",
    ),
    (
        "A data breach was disclosed affecting 1,200 users; remediation completed within 48 hours.",
        "Acme Corp takes security seriously.",
    ),
    (
        "The company's Series C term sheet values it at $500M pre-money.",
        "The company closed a Series B at a $120M valuation.",
    ),
    (
        "Employee headcount reached 340 as of this month, up from 210 a year ago.",
        "Employee headcount was 210 a year ago.",
    ),
    (
        "A competitor, Rival Inc, was acquired by a larger player this week.",
        "Rival Inc is a direct competitor in the same market.",
    ),
    (
        "The CTO published a blog post outlining the new infrastructure roadmap.",
        "The CTO has led engineering since the company's founding.",
    ),
    (
        "Customer churn dropped to 2.1% this quarter after the onboarding redesign.",
        "Customer churn was a known problem last year.",
    ),
]


class TestKnownGoodMergesNeverSuppressed:
    @pytest.mark.parametrize("observations,existing_body", _KNOWN_GOOD_MERGES)
    def test_never_suppressed(self, observations: str, existing_body: str) -> None:
        action = _update_action(observations=observations)
        assert check_merge_worthiness_gate(action, existing_body, "ref", None) is False

    def test_zero_suppressions_across_whole_set(self) -> None:
        suppressed = [
            (obs, body)
            for obs, body in _KNOWN_GOOD_MERGES
            if check_merge_worthiness_gate(_update_action(observations=obs), body, "ref", None)
            is True
        ]
        assert suppressed == []
