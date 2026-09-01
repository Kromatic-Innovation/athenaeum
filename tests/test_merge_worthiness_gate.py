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
    suppressed (AC4), including the coincidental-short-line and
    seam-stitch shapes named in QA round 2;
  - the two false-suppression regressions QA round 2 reproduced directly
    against ``_merge_worthiness_fully_contained`` (a coincidental match on
    short/generic units; a match stitched across a line boundary the page
    deliberately drew), and the two conservative fixes that close them
    (per-line containment; a minimum verifiable-unit length that FAILS a
    too-short unit rather than excluding it from the check).

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
    _MERGE_WORTHINESS_MIN_UNIT_CHARS,
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
        # Both lines are >= _MERGE_WORTHINESS_MIN_UNIT_CHARS normalized
        # chars, so this exercises real containment, not the length gate.
        body = "Acme Corp is a fintech startup.\nThe company was founded back in 2019.\n"
        obs = "Acme Corp is a fintech startup.\nThe company was founded back in 2019."
        assert _merge_worthiness_fully_contained(obs, body) is True

    def test_partial_novelty_one_absent_line_fails_whole_check(self) -> None:
        body = "Acme Corp is a fintech startup.\n"
        obs = "Acme Corp is a fintech startup.\nRaised a Series B in 2026."
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_empty_body_never_contains_nonempty_observations(self) -> None:
        # Long enough to isolate "empty body" as the reason, not shortness.
        assert (
            _merge_worthiness_fully_contained("This is a brand-new fact worth noting.", "") is False
        )

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
# QA round 2 (post-d8939d1): two reproduced false-suppression defects and
# their conservative fixes.
#
# (1a) Short/generic units matched by coincidence -- fixed by
#      _MERGE_WORTHINESS_MIN_UNIT_CHARS: a unit shorter than that FAILS the
#      check (dispatches the merge) rather than being excluded from it.
# (1b) Whitespace-collapse stitched a match across a line boundary the page
#      deliberately drew -- fixed by checking containment against a single
#      normalized body LINE, never the whole body flattened into one
#      string.
# ---------------------------------------------------------------------------


class TestQARound2FalseSuppressionRegressions:
    def test_1a_short_generic_units_no_longer_coincidentally_match(self) -> None:
        """Verbatim reproduction from QA round 2: three short, generic
        units ("2023", "50", "Austin") each happen to appear somewhere in
        an ordinary ~350-char page, but none is >= 24 normalized chars, so
        none is VERIFIABLE evidence of containment -- the whole check must
        fail (dispatch), not silently treat coincidence as coverage."""
        body = (
            "Acme Corp is a fintech startup founded in 2019. It reported revenue "
            "growth in 2023, driven by international expansion. Employee count "
            "reached 50 by year end. The main office is located in Austin, with "
            "a satellite office opened later. The founder previously worked at a bank."
        )
        obs = "2023\n50\nAustin"
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_1b_a_match_stitched_across_a_line_boundary_is_not_contained(self) -> None:
        """Reconstruction of QA round 2's second reproduction. The page
        draws a real boundary between two unrelated claims (a genuine line
        break -- a blank line, a list item, or, as here, two separate
        observation lines merged onto the page as two separate lines): one
        line says the report was filed on time, a DIFFERENT line says the
        deadline was missed. The unit under test is only a contiguous
        substring of the OLD flattened whole-body string, stitched across
        that boundary via whitespace-collapse -- it is not a substring of
        either line alone, so per-line containment must reject it."""
        body = "Filed the Q1 report on time.\nMissed the Q1 deadline for the follow-up."
        obs = "time. Missed the Q1 deadline"
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_fix_a_boundary_unit_spans_two_body_lines_is_not_contained(self) -> None:
        """Dedicated Fix A boundary test (distinct fixture from the 1b
        regression above): an observation unit whose text exists in the
        body ONLY by spanning two lines must return False, even though it
        WOULD be a contiguous substring of the old flattened whole-body
        string."""
        body = (
            "The office lease was renewed last month.\n"
            "Expansion plans were also announced for next year."
        )
        obs = "last month. Expansion plans were also announced"
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_fix_a_does_not_affect_a_single_line_markdown_paragraph(self) -> None:
        """Regression guard for Fix A's own stated safety property: an
        ordinary markdown paragraph is stored as ONE line, so a unit fully
        contained within that one line is unaffected by the line-boundary
        restriction."""
        body = "Acme Corp is a fintech startup based in Austin, founded in 2019."
        obs = "a fintech startup based in Austin, founded in 2019."
        assert _merge_worthiness_fully_contained(obs, body) is True

    def test_fix_b_direction_long_contained_line_plus_short_novel_line(self) -> None:
        """THE decisive test for Fix B's direction (QA round 2's own
        framing): an observation of one long line that IS contained plus
        one short line that is NOT on the page must return False. If the
        short unit were EXCLUDED from the check (the dangerous direction
        the module docstring warns against) rather than made to FAIL it,
        this would wrongly return True and silently destroy the novel
        short fact."""
        long_line = "This is a sufficiently long observation line already on the page verbatim."
        body = long_line
        obs = long_line + "\nBrand new short fact"
        assert _merge_worthiness_fully_contained(obs, body) is False

    def test_unit_at_exact_min_length_threshold_is_verifiable(self) -> None:
        """Boundary, no off-by-one: a unit whose NORMALIZED length is
        exactly _MERGE_WORTHINESS_MIN_UNIT_CHARS is treated as verifiable
        (checked for containment normally, and found here)."""
        unit = "x" * _MERGE_WORTHINESS_MIN_UNIT_CHARS
        assert len(unit) == _MERGE_WORTHINESS_MIN_UNIT_CHARS
        assert _merge_worthiness_fully_contained(unit, unit) is True

    def test_unit_one_under_min_length_threshold_is_unverifiable_even_when_present(
        self,
    ) -> None:
        """Boundary, no off-by-one: a unit one char SHORT of the threshold
        is NOT verifiable -- the check fails even though the unit is
        genuinely, literally present in the body."""
        unit = "x" * (_MERGE_WORTHINESS_MIN_UNIT_CHARS - 1)
        assert len(unit) == _MERGE_WORTHINESS_MIN_UNIT_CHARS - 1
        assert _merge_worthiness_fully_contained(unit, unit) is False


# ---------------------------------------------------------------------------
# check_merge_worthiness_gate
# ---------------------------------------------------------------------------


class TestCheckMergeWorthinessGate:
    def test_full_containment_returns_true(self) -> None:
        action = _update_action(observations="Acme Corp is a fintech startup.")
        existing_body = "Acme Corp is a fintech startup. Founded 2019."
        assert check_merge_worthiness_gate(action, existing_body, "ref") is True

    def test_partial_novelty_returns_false(self) -> None:
        # Second line is >= 24 normalized chars and genuinely absent, so
        # this exercises real novelty, not the length gate.
        action = _update_action(
            observations="Acme Corp is a fintech startup.\nRaised a Series B funding round."
        )
        existing_body = "Acme Corp is a fintech startup."
        assert check_merge_worthiness_gate(action, existing_body, "ref") is False

    def test_empty_new_page_returns_false(self) -> None:
        # Long enough to isolate "empty page" as the reason, not shortness.
        action = _update_action(observations="Something completely new happened here.")
        assert check_merge_worthiness_gate(action, "", "ref") is False

    def test_no_client_parameter_at_all(self) -> None:
        """AC5(a): the function signature cannot accept a client --
        architecturally cannot make an LLM call."""
        params = inspect.signature(check_merge_worthiness_gate).parameters
        assert "client" not in params

    def test_no_config_parameter_at_all(self) -> None:
        """QA round 2 (Fix C): this gate resolves no knobs of its own --
        enablement is decided at the call site -- so a ``config`` parameter
        would have nothing to do, unlike ``check_page_size_gate``, which
        genuinely resolves two of its own knobs."""
        params = inspect.signature(check_merge_worthiness_gate).parameters
        assert "config" not in params

    def test_reads_only_the_truncated_window_not_full_body(self) -> None:
        """The decisive test for boundary 5: a fact whose ONLY occurrence
        starts beyond _MAX_EXISTING_BODY_CHARS must NOT count as contained,
        even though it genuinely exists later in the full body."""
        fact_line = "a brand new unique fact xyzzy123"
        padding = "y" * _MAX_EXISTING_BODY_CHARS
        existing_body = padding + fact_line
        action = _update_action(observations=fact_line)
        assert check_merge_worthiness_gate(action, existing_body, "ref") is False

    def test_fires_info_log_with_expected_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        # >= 24 normalized chars, so this exercises the log path via real
        # containment, not the length gate.
        action = _update_action(
            name="Acme Corp", observations="A fintech startup founded in Austin."
        )
        existing_body = "A fintech startup founded in Austin. Founded 2019."
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            result = check_merge_worthiness_gate(action, existing_body, "sessions/x.md")
        assert result is True
        [record] = [r for r in caplog.records if "merge-worthiness gate" in r.message]
        assert record.levelno == logging.INFO
        assert "athenaeum#1172" in record.message
        assert "Acme Corp" in record.message
        assert "sessions/x.md" in record.message

    def test_no_log_when_gate_does_not_fire(self, caplog: pytest.LogCaptureFixture) -> None:
        action = _update_action(observations="Something completely absent.")
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            result = check_merge_worthiness_gate(action, "unrelated body", "ref")
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
    # QA round 2 -- coincidental-short-line shape: every observation line
    # is short/generic enough that it could coincidentally appear on any
    # ordinary page; none is >= _MERGE_WORTHINESS_MIN_UNIT_CHARS, so none
    # is verifiable evidence, and the merge must dispatch.
    (
        "2024\n42\nDenver",
        "The company was founded in 2024. It has 42 employees, based in Denver, and growing fast.",
    ),
    (
        "Q3\n$5M\nTrue",
        "Q3 results are in. The company raised $5M. True north for the "
        "team remains customer retention.",
    ),
    # QA round 2 -- seam-stitch shape: the observation is only a
    # contiguous substring of the page once two DIFFERENT lines are
    # flattened together; it is not a substring of either line alone, so
    # the merge must dispatch.
    (
        "on time. Missed the deadline for the quarterly filing",
        "Filed the annual report right on time.\nMissed the deadline for the quarterly filing.",
    ),
    (
        "for the launch. The rollout was delayed by two weeks",
        "The team finished all prep work for the launch.\n"
        "The rollout was delayed by two weeks due to a vendor issue.",
    ),
]


class TestKnownGoodMergesNeverSuppressed:
    @pytest.mark.parametrize("observations,existing_body", _KNOWN_GOOD_MERGES)
    def test_never_suppressed(self, observations: str, existing_body: str) -> None:
        action = _update_action(observations=observations)
        assert check_merge_worthiness_gate(action, existing_body, "ref") is False

    def test_zero_suppressions_across_whole_set(self) -> None:
        suppressed = [
            (obs, body)
            for obs, body in _KNOWN_GOOD_MERGES
            if check_merge_worthiness_gate(_update_action(observations=obs), body, "ref") is True
        ]
        assert suppressed == []
