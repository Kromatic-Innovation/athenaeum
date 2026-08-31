"""Tests for athenaeum.tiers — tier1 matching, tier2 classification (mocked LLM),
tier3 create/merge/write (mocked LLM), tier4 escalation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.models import (
    EntityAction,
    EntityIndex,
    EscalationItem,
    RawFile,
    RawFileOverBudgetError,
    TokenUsage,
)
from athenaeum.tiers import (
    _MERGE_MAX_TOKENS,
    _MERGE_PATCH_MAX_TOKENS,
    _TIER2_CLASSIFY_MAX_TOKENS,
    _TIER2_CLASSIFY_RETRY_MAX_TOKENS,
    _TIER3_CREATE_MAX_TOKENS,
    ENTITY_LLM_CALL_MARKER,
    MERGE_FALLBACK_LOG_PREFIX,
    MERGE_PARSE_FAIL_AMBIGUOUS,
    MERGE_PARSE_FAIL_NO_JSON,
    MERGE_PARSE_FAIL_SHAPE,
    TIER2_DEGRADED_MARKER,
    TIER2_TRUNCATED_MARKER,
    MergeOpsError,
    PreambleOnlyResponseError,
    Tier2ParseStats,
    _timed_llm_call,
    apply_merge_ops,
    parse_merge_ops_response,
    parse_tier2_entities,
    resolve_type_gate_allowed_types,
    resolve_type_gate_excluded_keys,
    strip_planning_preamble,
    tier1_programmatic_match,
    tier2_classify,
    tier2_reclassify_larger_budget,
    tier2_request_params,
    tier3_create,
    tier3_derive_actions,
    tier3_merge,
    tier3_write,
    tier4_escalate,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_raw(content: str) -> RawFile:
    """Build a RawFile with pre-loaded content (no filesystem access needed)."""
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _mock_client(response_text: str) -> MagicMock:
    """Build a mock Anthropic client returning the given text."""
    client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = mock_response
    return client


def _sequenced_client(
    texts: list[str], stop_reasons: list[str] | None = None
) -> MagicMock:
    """Mock client returning ``texts`` on successive ``messages.create`` calls.

    Issue athenaeum#469: the patch-mode merge makes a patch attempt first and falls
    back to a full-echo retry on failure, so tests need distinct responses
    per call. ``stop_reasons`` (default ``"end_turn"``) parallels ``texts``.
    """
    client = MagicMock()
    responses = []
    for i, text in enumerate(texts):
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.stop_reason = stop_reasons[i] if stop_reasons else "end_turn"
        responses.append(resp)
    client.messages.create.side_effect = responses
    return client


# ---------------------------------------------------------------------------
# Tier 2 — owner-namespace routing (issue athenaeum#263)
# ---------------------------------------------------------------------------


class TestTier2OwnerRouting:
    """Drive ``parse_tier2_entities`` itself through the owner-routing branch.

    The underlying ``owner.route_owner_memory`` is unit-tested separately; this
    pins that the branch at ``tiers.py`` (~line 315) actually reclassifies an
    owner operational/exclusion memory to ``reference`` when the parser runs.
    """

    OWNER = {"uid": "a545c038", "google_contact": "", "aliases": ["Tristan Kromer"]}

    @staticmethod
    def _payload(name: str) -> str:
        return json.dumps(
            [{"name": name, "entity_type": "person", "access": "internal", "tags": []}]
        )

    def test_owner_operational_memory_reclassified_to_reference(self) -> None:
        # Classifier said "person"; owner routing steers an operational /
        # exclusion memory to a standalone reference page.
        results = parse_tier2_entities(
            self._payload("user_tristan_family_relationships"),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
            owner=self.OWNER,
        )
        assert len(results) == 1
        assert results[0].entity_type == "reference"

    def test_owner_bio_memory_stays_person(self) -> None:
        # An owner person-bio memory is left as the classifier set it.
        results = parse_tier2_entities(
            self._payload("user_tristan_career"),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
            owner=self.OWNER,
        )
        assert results[0].entity_type == "person"

    def test_inert_when_no_owner_configured(self) -> None:
        # Without an owner the routing branch never fires.
        results = parse_tier2_entities(
            self._payload("user_tristan_family_relationships"),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
            owner=None,
        )
        assert results[0].entity_type == "person"


class TestTier2PlaceholderLabelFilter:
    """Post-filter safety net (athenaeum#296): reject structural/placeholder labels
    ("Member N", "Member a", "Item 2") the classifier may hallucinate as
    entity names — these are internal disambiguators from
    contradictions.py/resolutions.py prompt-building, not real names.
    """

    @staticmethod
    def _payload(name: str) -> str:
        return json.dumps(
            [{"name": name, "entity_type": "person", "access": "internal", "tags": []}]
        )

    @pytest.mark.parametrize(
        "name",
        ["Member 19", "member 4", "Member a", "Member A", "Member b"],
    )
    def test_placeholder_labels_dropped(self, name: str) -> None:
        results = parse_tier2_entities(
            self._payload(name),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
        )
        assert results == []

    def test_real_name_containing_member_word_survives(self) -> None:
        # "Member" as part of a real proper name (e.g. a company/product)
        # must not be dropped — the filter is anchored to the exact
        # "<label> <alnum>" shape, not a loose substring match.
        results = parse_tier2_entities(
            self._payload("Member Corp International"),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
        )
        assert len(results) == 1
        assert results[0].name == "Member Corp International"

    def test_two_token_real_name_survives(self) -> None:
        # The false-positive boundary the regex actually risks: a genuine
        # two-token name (e.g. a credit-union-style "Member One") must not
        # be dropped just because it matches the "<word> <alnum>" shape.
        results = parse_tier2_entities(
            self._payload("Member One"),
            "sessions/x.md",
            ["person", "reference"],
            [],
            ["internal"],
        )
        assert len(results) == 1
        assert results[0].name == "Member One"

    def test_drop_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            results = parse_tier2_entities(
                self._payload("Member 4"),
                "sessions/x.md",
                ["person", "reference"],
                [],
                ["internal"],
            )
        assert results == []
        assert any("placeholder" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Tier 1 — Programmatic matching
# ---------------------------------------------------------------------------


class TestTier1:
    def test_matches_known_entity(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Met with the team at Acme Corp about their product.")
        matched = tier1_programmatic_match(raw, index)
        names = [name for name, _, _ in matched]
        assert any("acme" in n for n in names)

    def test_matches_alias(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Got an email from Acme Corporation today.")
        matched = tier1_programmatic_match(raw, index)
        assert len(matched) > 0

    def test_no_match(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Nothing relevant here about any known entities.")
        matched = tier1_programmatic_match(raw, index)
        assert len(matched) == 0

    def test_word_boundary(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        # "acme" appears as substring in "pharmacme" -- should NOT match
        raw = _make_raw("The pharmacme product line is interesting.")
        matched = tier1_programmatic_match(raw, index)
        acme_matches = [n for n, _, _ in matched if "acme" in n]
        assert len(acme_matches) == 0

    def test_short_names_skipped(self, wiki_dir: Path) -> None:
        """Names shorter than 3 chars should be skipped to avoid false positives."""
        index = EntityIndex(wiki_dir)
        # Register a short-name entity
        index._by_name["ai"] = ("short-uid", wiki_dir / "short.md")
        raw = _make_raw("AI is transforming the industry.")
        matched = tier1_programmatic_match(raw, index)
        ai_matches = [n for n, _, _ in matched if n == "ai"]
        assert len(ai_matches) == 0


# ---------------------------------------------------------------------------
# Issue athenaeum#1169: type gate for Tier-1 programmatic matching
# ---------------------------------------------------------------------------


def _type_gate_wiki(tmp_path: Path) -> Path:
    """A wiki with one `company`, one `project`, and one UNTYPED page --
    each name distinct and multi-word so the mention-density/junk gates
    never interfere with what the type gate itself is being tested for."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "co.md").write_text(
        "---\nuid: co1\ntype: company\nname: Widget Traders\n---\n\nA company.\n"
    )
    (wiki / "proj.md").write_text(
        "---\nuid: pr1\ntype: project\nname: Rocket Launcher\n---\n\nA project.\n"
    )
    (wiki / "untyped.md").write_text("---\nname: Silent Harbor\n---\n\nNo type field.\n")
    return wiki


_TYPE_GATE_CONTENT = (
    "Notes: talked to Widget Traders about the Rocket Launcher timeline, "
    "then visited Silent Harbor for lunch."
)


class TestTier1TypeGate:
    def test_default_no_configuration_matches_everything(self, tmp_path: Path) -> None:
        """No allowed_types/excluded_keys and no config: byte-identical to
        pre-athenaeum#1169 behavior -- every key that would have matched still
        matches, typed or untyped."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
        assert names == {"widget traders", "rocket launcher", "silent harbor"}

    def test_allowed_types_suppresses_other_types(self, tmp_path: Path) -> None:
        """Explicit allowed_types={'company'} drops the project match but
        keeps the company match."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {
            n
            for n, _, _ in tier1_programmatic_match(raw, index, allowed_types={"company"})
        }
        assert "widget traders" in names
        assert "rocket launcher" not in names

    def test_allowed_types_admits_configured_type(self, tmp_path: Path) -> None:
        """The flip side of the suppression test: a key whose type IS in the
        allow-list is not dropped."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {
            n
            for n, _, _ in tier1_programmatic_match(raw, index, allowed_types={"project"})
        }
        assert "rocket launcher" in names

    def test_untyped_page_kept_even_with_allowed_types_configured(
        self, tmp_path: Path
    ) -> None:
        """Binding decision (issue athenaeum#1169): an untyped page is always
        matchable, even when an allow-list is configured and the page's type
        (there isn't one) is not in it."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {
            n
            for n, _, _ in tier1_programmatic_match(raw, index, allowed_types={"company"})
        }
        assert "silent harbor" in names

    def test_excluded_keys_suppresses_specific_key(self, tmp_path: Path) -> None:
        """excluded_keys drops one specific key regardless of type -- the
        mechanism the issue's CORRECTION needs to express "exclude this
        particular inert key" rather than a whole type."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {
            n
            for n, _, _ in tier1_programmatic_match(
                raw, index, excluded_keys={"widget traders"}
            )
        }
        assert "widget traders" not in names
        # Unrelated keys are unaffected.
        assert "rocket launcher" in names
        assert "silent harbor" in names

    def test_excluded_keys_wins_over_allowed_types(self, tmp_path: Path) -> None:
        """A key can be excluded even when its own type IS in the allow-list --
        excluded_keys is a strictly stronger veto."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        names = {
            n
            for n, _, _ in tier1_programmatic_match(
                raw,
                index,
                allowed_types={"company"},
                excluded_keys={"widget traders"},
            )
        }
        assert "widget traders" not in names

    def test_suppressed_counts_report_what_each_gate_removed(self, tmp_path: Path) -> None:
        """The optional `suppressed` accumulator lets a caller (e.g. a
        host-side measurement run against the live corpus, issue athenaeum#1169 AC4)
        see how many candidate matches each half of the gate removed."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        suppressed: dict[str, int] = {}
        tier1_programmatic_match(
            raw,
            index,
            allowed_types={"company"},
            excluded_keys={"rocket launcher"},
            suppressed=suppressed,
        )
        # "rocket launcher" is caught by excluded_keys (checked first);
        # nothing else is dropped by the type gate since the only other
        # non-company, non-excluded key is "silent harbor", which is untyped
        # and therefore always kept.
        assert suppressed == {"excluded_key": 1}

    def test_config_wires_allowed_types_and_excluded_keys(self, tmp_path: Path) -> None:
        """librarian.type_gate_allowed_types / type_gate_excluded_keys in
        config, mirroring the librarian.junk_match_* / mention_density_*
        knob idiom, take effect with no explicit kwargs passed."""
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        cfg = {
            "librarian": {
                "type_gate_allowed_types": ["project"],
                "type_gate_excluded_keys": ["silent harbor"],
            }
        }
        names = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
        assert names == {"rocket launcher"}

    def test_explicit_kwarg_takes_precedence_over_config(self, tmp_path: Path) -> None:
        index = EntityIndex(_type_gate_wiki(tmp_path))
        raw = _make_raw(_TYPE_GATE_CONTENT)
        cfg = {"librarian": {"type_gate_allowed_types": ["project"]}}
        names = {
            n
            for n, _, _ in tier1_programmatic_match(
                raw, index, config=cfg, allowed_types={"company"}
            )
        }
        # Explicit kwarg (company) wins over config (project).
        assert "widget traders" in names
        assert "rocket launcher" not in names

    def test_resolve_type_gate_allowed_types_default_none(self) -> None:
        assert resolve_type_gate_allowed_types(None) is None
        assert resolve_type_gate_allowed_types({}) is None
        assert resolve_type_gate_allowed_types({"librarian": {}}) is None
        empty_cfg = {"librarian": {"type_gate_allowed_types": []}}
        assert resolve_type_gate_allowed_types(empty_cfg) is None

    def test_resolve_type_gate_allowed_types_from_config(self) -> None:
        cfg = {"librarian": {"type_gate_allowed_types": ["concept", "principle"]}}
        assert resolve_type_gate_allowed_types(cfg) == {"concept", "principle"}

    def test_resolve_type_gate_excluded_keys_default_none(self) -> None:
        assert resolve_type_gate_excluded_keys(None) is None
        empty_cfg = {"librarian": {"type_gate_excluded_keys": []}}
        assert resolve_type_gate_excluded_keys(empty_cfg) is None

    def test_resolve_type_gate_excluded_keys_from_config_lowercases(self) -> None:
        cfg = {"librarian": {"type_gate_excluded_keys": ["Widget Traders"]}}
        assert resolve_type_gate_excluded_keys(cfg) == {"widget traders"}


# ---------------------------------------------------------------------------
# Tier 2 — Classification (mocked LLM)
# ---------------------------------------------------------------------------


class TestTier2:
    """Mock-based tests for classification tier."""

    def test_classify_prompt_wraps_content_in_xml(self) -> None:
        """Issue #5: raw content must be wrapped in <user_document> tags."""
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some raw content with potential injection.")
        client = _mock_client("[]")

        tier2_classify(raw, [], ["person"], [], ["internal"], client)
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "<user_document>" in user_msg
        assert "</user_document>" in user_msg
        system_msg = call_args.kwargs["system"]
        assert "untrusted user data" in system_msg

    def test_classify_system_prompt_guards_against_placeholder_labels(self) -> None:
        """Issue athenaeum#296: the classify prompt must instruct the LLM not to
        extract structural/placeholder labels ("Member 1", "Item 2") as
        entities — the post-filter is defense in depth, not the only guard.
        """
        from athenaeum.tiers import CLASSIFY_SYSTEM

        assert "placeholder" in CLASSIFY_SYSTEM.lower()

    def test_classify_includes_observation_filter(
        self,
        wiki_dir: Path,
    ) -> None:
        """Issue athenaeum#17: observation-filter.md should be injected into classify prompt."""
        from athenaeum.tiers import tier2_classify

        schema_dir = wiki_dir / "_schema"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "observation-filter.md").write_text(
            "# Observation Filter\n\n## Always Capture\n- People\n"
        )

        raw = _make_raw("Some content about people.")
        client = _mock_client("[]")

        tier2_classify(
            raw,
            [],
            ["person"],
            [],
            ["internal"],
            client,
            wiki_root=wiki_dir,
        )
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "Observation filter" in user_msg
        assert "Always Capture" in user_msg

    def test_classify_records_token_usage(self) -> None:
        """Issue #9: token usage should be recorded from API responses."""
        from athenaeum.models import TokenUsage
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        mock_response.usage = MagicMock(
            input_tokens=150,
            output_tokens=20,
        )
        client.messages.create.return_value = mock_response

        usage = TokenUsage()
        tier2_classify(
            raw,
            [],
            ["person"],
            [],
            ["internal"],
            client,
            usage=usage,
        )
        assert usage.input_tokens == 150
        assert usage.output_tokens == 20
        assert usage.api_calls == 1
        # MagicMock auto-attrs for the cache fields are not ints — they
        # must coerce to 0, not blow up or accumulate mock objects.
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0

    def test_classify_records_cache_usage(self) -> None:
        """Issue athenaeum#230: cache creation/read tokens recorded when present."""
        from athenaeum.models import TokenUsage
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        mock_response.usage = MagicMock(
            input_tokens=150,
            output_tokens=20,
            cache_creation_input_tokens=2300,
            cache_read_input_tokens=4600,
        )
        client.messages.create.return_value = mock_response

        usage = TokenUsage()
        tier2_classify(
            raw,
            [],
            ["person"],
            [],
            ["internal"],
            client,
            usage=usage,
        )
        assert usage.input_tokens == 150
        assert usage.output_tokens == 20
        assert usage.cache_creation_input_tokens == 2300
        assert usage.cache_read_input_tokens == 4600
        assert usage.api_calls == 1

    def test_extracts_new_entity(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Had coffee with Alice Zhang, she runs product at Acme.")
        response_json = json.dumps(
            [
                {
                    "name": "Alice Zhang",
                    "entity_type": "person",
                    "tags": ["active", "client"],
                    "access": "internal",
                    "observations": "Runs product at Acme.",
                }
            ]
        )
        client = _mock_client(response_json)

        result = tier2_classify(
            raw,
            matched_names=["acme"],
            valid_types=["person", "company", "concept", "tool"],
            valid_tags=["active", "client"],
            valid_access=["open", "internal", "confidential", "personal"],
            client=client,
        )
        assert len(result) == 1
        assert result[0].name == "Alice Zhang"
        assert result[0].entity_type == "person"
        assert result[0].is_new is True

    def test_returns_empty_for_empty_content(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("   ")
        client = _mock_client("[]")

        result = tier2_classify(raw, [], ["person"], [], ["internal"], client)
        assert result == []
        # Should short-circuit before calling the API
        client.messages.create.assert_not_called()

    def test_invalid_type_falls_back_to_reference(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content about a widget.")
        response_json = json.dumps(
            [
                {
                    "name": "Widget",
                    "entity_type": "gadget",  # not in valid_types
                    "tags": [],
                    "access": "internal",
                    "observations": "A widget thing.",
                }
            ]
        )
        client = _mock_client(response_json)

        result = tier2_classify(
            raw,
            [],
            ["person", "company", "reference"],
            [],
            ["internal"],
            client,
        )
        assert len(result) == 1
        assert result[0].entity_type == "reference"

    def test_invalid_access_falls_back_to_internal(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        response_json = json.dumps(
            [
                {
                    "name": "Test Entity",
                    "entity_type": "person",
                    "tags": [],
                    "access": "top-secret",  # not in valid_access
                }
            ]
        )
        client = _mock_client(response_json)

        result = tier2_classify(
            raw,
            [],
            ["person"],
            [],
            ["open", "internal", "confidential", "personal"],
            client,
        )
        assert len(result) == 1
        assert result[0].access == "internal"

    def test_filters_invalid_tags(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        response_json = json.dumps(
            [
                {
                    "name": "Test",
                    "entity_type": "person",
                    "tags": ["active", "bogus-tag", "client"],
                    "access": "internal",
                }
            ]
        )
        client = _mock_client(response_json)

        result = tier2_classify(
            raw,
            [],
            ["person"],
            ["active", "client"],
            ["internal"],
            client,
        )
        assert result[0].tags == ["active", "client"]

    def test_handles_json_wrapped_in_code_fence(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Met Bob at the conference.")
        fenced = (
            "```json\n"
            '[{"name": "Bob", "entity_type": "person", "tags": [], "access": "internal"}]\n'
            "```"
        )
        client = _mock_client(fenced)

        result = tier2_classify(raw, [], ["person"], [], ["internal"], client)
        assert len(result) == 1
        assert result[0].name == "Bob"

    def test_handles_invalid_json(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        client = _mock_client("[{invalid json}]")

        result = tier2_classify(raw, [], ["person"], [], ["internal"], client)
        assert result == []

    def test_handles_no_json_in_response(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        client = _mock_client("I don't see any entities here.")

        result = tier2_classify(raw, [], ["person"], [], ["internal"], client)
        assert result == []

    def test_skips_items_without_name(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        response_json = json.dumps(
            [
                {"entity_type": "person", "tags": [], "access": "internal"},  # no name
                {
                    "name": "Valid",
                    "entity_type": "person",
                    "tags": [],
                    "access": "internal",
                },
            ]
        )
        client = _mock_client(response_json)

        result = tier2_classify(raw, [], ["person"], [], ["internal"], client)
        assert len(result) == 1
        assert result[0].name == "Valid"

    def test_api_error_propagates(self) -> None:
        """API errors must NOT be swallowed -- they must propagate so the caller
        can mark the file as failed and preserve it for retry."""
        import anthropic as anthropic_mod

        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        client = MagicMock()
        client.messages.create.side_effect = anthropic_mod.APIError(
            message="Server error",
            request=MagicMock(),
            body=None,
        )

        with pytest.raises(anthropic_mod.APIError):
            tier2_classify(raw, [], ["person"], [], ["internal"], client)

    def test_prompt_includes_matched_names(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Talked to Alice about Acme's roadmap.")
        client = _mock_client("[]")

        tier2_classify(
            raw,
            matched_names=["acme", "alice"],
            valid_types=["person"],
            valid_tags=[],
            valid_access=["internal"],
            client=client,
        )
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "acme, alice" in user_msg

    def test_caps_content_at_4000_chars(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("x" * 10000)
        client = _mock_client("[]")

        tier2_classify(raw, [], ["person"], [], ["internal"], client)
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        # The raw content portion should be capped
        assert "x" * 4001 not in user_msg


# ---------------------------------------------------------------------------
# Tier 3 — Create (mocked LLM)
# ---------------------------------------------------------------------------


class TestTier3Create:
    """Mock-based tests for entity creation tier."""

    def test_create_prompt_wraps_observations_in_xml(self) -> None:
        """Issue #5: observations must be wrapped in <user_document> tags."""
        action = EntityAction(
            kind="create",
            name="Test Entity",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="Untrusted observation text.",
        )
        client = _mock_client("# Test Entity\n\nContent.")

        tier3_create(action, "sessions/raw.md", client)
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "<user_document>" in user_msg
        assert "</user_document>" in user_msg
        assert "data only" in user_msg

    def test_create_includes_entity_template(
        self,
        wiki_dir: Path,
    ) -> None:
        """Issue athenaeum#17: _entity-template.md should be fed to Tier 3 create."""
        schema_dir = wiki_dir / "_schema"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "_entity-template.md").write_text(
            "# Entity Page Template\n\n## Template\nuid, type, name\n"
        )

        action = EntityAction(
            kind="create",
            name="Test",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="Some info.",
        )
        client = _mock_client("# Test\n\nContent.")

        tier3_create(
            action,
            "ref.md",
            client,
            wiki_root=wiki_dir,
        )
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "Entity template" in user_msg
        assert "Entity Page Template" in user_msg

    def test_creates_entity_from_action(self) -> None:
        action = EntityAction(
            kind="create",
            name="Alice Zhang",
            entity_type="person",
            tags=["active", "client"],
            access="internal",
            existing_uid=None,
            observations="Runs product at Acme Corp.",
        )
        client = _mock_client(
            "# Alice Zhang\n\nProduct lead at Acme Corp.[^1]\n\n[^1]: sessions/raw.md"
        )

        entity = tier3_create(action, "sessions/raw.md", client)
        assert entity is not None
        assert entity.name == "Alice Zhang"
        assert entity.type == "person"
        assert entity.access == "internal"
        assert entity.tags == ["active", "client"]
        assert len(entity.uid) == 8
        assert "Alice Zhang" in entity.body

    def test_api_error_propagates(self) -> None:
        import anthropic as anthropic_mod

        action = EntityAction(
            kind="create",
            name="Test",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="text",
        )
        client = MagicMock()
        client.messages.create.side_effect = anthropic_mod.APIError(
            message="Server error",
            request=MagicMock(),
            body=None,
        )

        with pytest.raises(anthropic_mod.APIError):
            tier3_create(action, "ref", client)

    def test_prompt_includes_entity_details(self) -> None:
        action = EntityAction(
            kind="create",
            name="Lean Startup",
            entity_type="concept",
            tags=["methodology"],
            access="open",
            existing_uid=None,
            observations="A methodology for validated learning.",
        )
        client = _mock_client("# Lean Startup\n\nA methodology.")

        tier3_create(action, "sessions/obs.md", client)
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "Lean Startup" in user_msg
        assert "concept" in user_msg
        assert "methodology" in user_msg
        assert "open" in user_msg


# ---------------------------------------------------------------------------
# Tier 3 — Create planning-preamble guard (issue athenaeum#1171)
# ---------------------------------------------------------------------------


class TestStripPlanningPreamble:
    """Unit tests for the standalone :func:`strip_planning_preamble` helper.

    Issue athenaeum#1171: this is the SAME detector the create path runs on every
    response — see :class:`TestTier3CreatePreambleGuard` below for the
    through-the-create-path regression coverage the issue's AC4 asks for.
    """

    def test_no_preamble_returns_body_unchanged(self) -> None:
        body = "# Alice Zhang\n\nProduct lead at Acme Corp.[^1]\n\n[^1]: sessions/raw.md"
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is False
        assert cleaned is body  # same object — not even re-stripped

    def test_leading_preamble_stripped_heading_boundary(self) -> None:
        body = (
            "Looking at the new observation, I need to write a page for "
            "Alice.\n\n# Alice Zhang\n\nProduct lead at Acme Corp.[^1]\n\n"
            "[^1]: sessions/raw.md"
        )
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned.startswith("# Alice Zhang")
        assert "Looking at" not in cleaned
        assert "Product lead at Acme Corp." in cleaned

    def test_leading_preamble_stripped_blank_line_boundary(self) -> None:
        body = "I'll draft this concisely.\n\nAcme Corp is a software vendor.[^1]"
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned == "Acme Corp is a software vendor.[^1]"

    def test_preamble_only_body_yields_empty_remainder(self) -> None:
        body = "Looking at the new observation, I need to think this through."
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned == ""

    def test_mid_body_first_person_sentence_not_stripped(self) -> None:
        body = (
            "# Alice Zhang\n\nAlice told the team, \"I need to leave early "
            "today.\" She works at Acme Corp.[^1]"
        )
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is False
        assert cleaned == body

    def test_trailing_first_person_sentence_not_stripped(self) -> None:
        body = "# Acme Corp\n\nA software vendor.[^1]\n\nI'll keep this updated."
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is False
        assert cleaned == body

    def test_let_me_opener_stripped(self) -> None:
        body = "Let me summarize what I found.\n\n# Bob Lee\n\nEngineer at Acme.[^1]"
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned.startswith("# Bob Lee")

    def test_based_on_lead_in_stripped(self) -> None:
        body = (
            "Based on the new observation, I need to create this entity.\n\n"
            "# Carol\n\nDesigner.[^1]"
        )
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned.startswith("# Carol")

    def test_ordinary_capitalized_i_sentence_not_treated_as_preamble(self) -> None:
        """A body that happens to start with 'I' but isn't a planning verb."""
        body = "I-beam Systems is a construction supplier.[^1]"
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is False
        assert cleaned == body

    def test_multi_line_no_blank_line_body_survives_past_first_line(self) -> None:
        """Gate-review should-fix (issue athenaeum#1171).

        No heading, no blank line — but a second line exists, so only the
        first line is preamble; everything after it is substantive content
        and must survive (rather than the whole body being classified as
        preamble-only).
        """
        body = (
            "I'll Be Back is a 1984 film catchphrase.\n"
            "Second line has real content that must survive.[^1]"
        )
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned == "Second line has real content that must survive.[^1]"

    def test_single_line_preamble_shaped_body_yields_empty_remainder(self) -> None:
        """No newline at all -- genuinely nothing to fall back to."""
        body = "I'll Be Back is a 1984 film catchphrase used by Schwarzenegger."
        cleaned, stripped = strip_planning_preamble(body)
        assert stripped is True
        assert cleaned == ""


class TestTier3CreatePreambleGuard:
    """Regression tests driven through :func:`tier3_create` (issue athenaeum#1171 AC4).

    Exercises the full create path (prompt build -> mocked LLM call ->
    :func:`tier3_entity_from_text`), not just the helper directly, per the
    issue's explicit instruction.
    """

    def _action(self) -> EntityAction:
        return EntityAction(
            kind="create",
            name="Alice Zhang",
            entity_type="person",
            tags=["active"],
            access="internal",
            existing_uid=None,
            observations="Runs product at Acme Corp.",
        )

    def test_create_strips_leading_planning_preamble(self) -> None:
        client = _mock_client(
            "Looking at the new observation, I need to write a page for "
            "Alice.\n\n# Alice Zhang\n\nProduct lead at Acme Corp.[^1]\n\n"
            "[^1]: sessions/raw.md"
        )
        entity = tier3_create(self._action(), "sessions/raw.md", client)
        assert "Looking at the new observation" not in entity.body
        assert "I need to write a page" not in entity.body
        assert entity.body.startswith("# Alice Zhang")
        assert "Product lead at Acme Corp." in entity.body

    def test_create_with_no_preamble_body_unchanged(self) -> None:
        raw_text = "# Alice Zhang\n\nProduct lead at Acme Corp.[^1]\n\n[^1]: sessions/raw.md"
        client = _mock_client(raw_text)
        entity = tier3_create(self._action(), "sessions/raw.md", client)
        assert entity.body == raw_text

    def test_create_preamble_only_response_is_rejected(self) -> None:
        client = _mock_client(
            "Looking at the new observation, I need to think about how to "
            "phrase this."
        )
        with pytest.raises(PreambleOnlyResponseError):
            tier3_create(self._action(), "sessions/raw.md", client)

    def test_create_mid_body_first_person_sentence_not_stripped(self) -> None:
        raw_text = (
            "# Alice Zhang\n\nAlice said, \"I need to leave early today.\" "
            "She works at Acme Corp.[^1]"
        )
        client = _mock_client(raw_text)
        entity = tier3_create(self._action(), "sessions/raw.md", client)
        assert entity.body == raw_text

    def test_create_stripped_preamble_increments_usage_counter(self) -> None:
        client = _mock_client(
            "I'll draft this now.\n\n# Alice Zhang\n\nProduct lead.[^1]"
        )
        usage = TokenUsage()
        tier3_create(self._action(), "sessions/raw.md", client, usage=usage)
        assert usage.preamble_stripped == 1
        assert usage.preamble_rejected == 0

    def test_create_rejected_preamble_increments_usage_counter(self) -> None:
        client = _mock_client("I'll think about this some more.")
        usage = TokenUsage()
        with pytest.raises(PreambleOnlyResponseError):
            tier3_create(self._action(), "sessions/raw.md", client, usage=usage)
        assert usage.preamble_rejected == 1
        assert usage.preamble_stripped == 0


class TestTier3DeriveActionsPreambleRejectionIsPerAction:
    """Gate-review must-fix (issue athenaeum#1171): rejecting a preamble-only
    create must be scoped to THAT action, not the whole raw file.

    Drives :func:`tier3_derive_actions` directly (the function whose
    per-action ``try/except`` is the fix) with TWO create actions for the
    SAME raw file — one whose response is preamble-only, one normal.
    Before the fix, ``PreambleOnlyResponseError`` propagated out of this
    function entirely (caught only by the generic ``except Exception`` that
    annotates and re-raises), discarding every action already derived for
    the file. After the fix, the function returns normally: the rejected
    action is simply absent from ``new_entities`` and its sibling still
    lands. See ``tests/test_batch_mode.py::TestBatchSyncEquivalence::
    test_preamble_only_sibling_create_is_skipped_not_file_aborted`` for the
    same behavior proved end-to-end through BOTH the sync and batch
    transports.
    """

    def test_preamble_only_action_skipped_sibling_still_lands(
        self, wiki_dir: Path
    ) -> None:
        raw = _make_raw("WidgetPreambleOnly and WidgetGood both mentioned.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="create",
                name="WidgetPreambleOnly",
                entity_type="concept",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="Facts about WidgetPreambleOnly.",
            ),
            EntityAction(
                kind="create",
                name="WidgetGood",
                entity_type="concept",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="Facts about WidgetGood.",
            ),
        ]

        preamble_response = MagicMock()
        preamble_response.content = [
            MagicMock(
                text="Looking at the new observation, I need to think this through."
            )
        ]
        good_response = MagicMock()
        good_response.content = [MagicMock(text="# WidgetGood\n\nFacts.[^1]")]

        client = MagicMock()
        # Ordered: the REJECTED action is first, so a bug that stops the
        # loop (rather than skipping just this one action) would never
        # even attempt the second call — the mock's 2-item queue makes that
        # failure mode a StopIteration instead of a silent pass.
        client.messages.create.side_effect = [preamble_response, good_response]

        usage = TokenUsage()
        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw,
            actions,
            index,
            wiki_dir,
            client,
            usage=usage,
        )

        # No exception propagated — the function returned normally.
        assert [e.name for e in new_entities] == ["WidgetGood"]
        assert pending_updates == []
        assert updated_uids == []
        assert escalations == []
        assert usage.preamble_rejected == 1
        assert usage.preamble_stripped == 0


# ---------------------------------------------------------------------------
# Tier 3 — Merge (mocked LLM)
# ---------------------------------------------------------------------------


class TestTier3Merge:
    """Mock-based tests for entity merge tier."""

    def test_merge_prompt_wraps_observations_in_xml(self) -> None:
        """Issue #5: observations must be wrapped in <user_document> tags."""
        action = EntityAction(
            kind="update",
            name="Test",
            entity_type="person",
            tags=[],
            access="",
            existing_uid="uid12345",
            observations="Untrusted merge text.",
        )
        client = _mock_client("# Test\n\nMerged content.")

        tier3_merge(action, "Existing body.", "sessions/raw.md", client)
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "<user_document>" in user_msg
        assert "</user_document>" in user_msg
        assert "data only" in user_msg

    def test_merge_system_prompt_guards_against_duplicate_reconfirmation(self) -> None:
        """Issue athenaeum#297: the merge prompt must instruct the LLM to fold a
        re-confirming observation into an existing bullet's footnotes (or
        skip it) rather than appending a near-duplicate "confirmed again"
        bullet.
        """
        from athenaeum.tiers import MERGE_SYSTEM

        assert "re-confirm" in MERGE_SYSTEM.lower()
        assert "near-duplicate" in MERGE_SYSTEM.lower()

    def test_merge_system_prompt_guards_against_self_resolving_claims(self) -> None:
        """Issue athenaeum#300: an observation claiming its OWN human confirmation/
        ratification is not independent verification — the merge prompt
        must not treat such a claim as grounds to overwrite settled content.
        """
        from athenaeum.tiers import MERGE_SYSTEM

        assert "human confirmation" in MERGE_SYSTEM.lower()
        assert "not independent verification" in MERGE_SYSTEM.lower()

    def test_merge_params_does_not_truncate_bloated_existing_body(self) -> None:
        """Issue athenaeum#302: the old 4000-char cap on existing_body went blind on
        already-bloated pages (the athenaeum#297 incident page grew to 5-10KB), so the
        athenaeum#297 dedup guard could never see content past the cap. The cap must
        be generous enough to cover realistic bloated pages.

        NOTE (issue athenaeum#1180): this only proves the body survives past the OLD
        4000-char cap, not that it is never truncated — the CURRENT 20,000-char
        cap (``_MAX_EXISTING_BODY_CHARS``) still hard-truncates any page above
        it; see ``TestTier3MergeTruncationGuard`` for that behavior.
        """
        from athenaeum.tiers import tier3_merge_params

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        existing_body = ("Confirmed again.[^1]\n" * 700) + "TAIL_MARKER"
        assert len(existing_body) > 4000  # exceeds the old cap

        params = tier3_merge_params(action, existing_body, "sessions/raw.md")
        user_msg = params["messages"][0]["content"]
        assert "TAIL_MARKER" in user_msg

    def test_merges_new_observations(self) -> None:
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        client = _mock_client(
            "# Acme Corp\n\nFintech startup, Series B.\n\nRaised Series C in Q1 2024.[^2]"
        )

        body, esc = tier3_merge(
            action,
            "# Acme Corp\n\nFintech startup, Series B.",
            "sessions/raw.md",
            client,
        )
        assert body is not None
        assert "Series C" in body
        assert esc is None

    def test_escalation_on_principled_conflict(self) -> None:
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme pivoted away from fintech.",
        )
        response = (
            "ESCALATE: Existing page says fintech, new observation says pivot away. "
            "This is a strategic direction conflict.\n"
            "---\n"
            "# Acme Corp\n\nFintech startup (disputed -- may have pivoted)."
        )
        client = _mock_client(response)

        body, esc = tier3_merge(
            action,
            "# Acme Corp\n\nFintech startup.",
            "sessions/raw.md",
            client,
        )
        assert esc is not None
        assert esc.conflict_type == "principled"
        # Both tokens appear in the mocked response — pin both so a
        # regression that swallows description content into empty string
        # or drops the conflict rationale cannot pass silently.
        desc = esc.description.lower()
        assert "fintech" in desc
        assert "pivot" in desc
        assert body is not None  # still returns merged body after separator

    def test_escalation_only_when_no_separator(self) -> None:
        action = EntityAction(
            kind="update",
            name="Test",
            entity_type="person",
            tags=[],
            access="",
            existing_uid="uid12345",
            observations="Contradictory info.",
        )
        response = "ESCALATE: Irreconcilable conflict between sources."
        client = _mock_client(response)

        body, esc = tier3_merge(action, "Existing body.", "ref", client)
        assert esc is not None
        assert body is None  # no body when no --- separator

    def test_api_error_propagates(self) -> None:
        import anthropic as anthropic_mod

        action = EntityAction(
            kind="update",
            name="Test",
            entity_type="person",
            tags=[],
            access="",
            existing_uid="uid12345",
            observations="text",
        )
        client = MagicMock()
        client.messages.create.side_effect = anthropic_mod.APIError(
            message="Error",
            request=MagicMock(),
            body=None,
        )

        with pytest.raises(anthropic_mod.APIError):
            tier3_merge(action, "body", "ref", client)

    def test_truncated_response_refuses_to_overwrite_and_escalates(self) -> None:
        """Issue athenaeum#302 (Quine follow-up): a response cut off by max_tokens is
        a truncated page body, not a complete one — MERGE_SYSTEM requires
        reproducing the WHOLE existing body, so writing a truncated response
        back would silently discard the tail of the page. Must refuse to
        overwrite and escalate instead.
        """
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        client = _mock_client("# Acme Corp\n\nFintech startup, Series B (cut off mid")
        client.messages.create.return_value.stop_reason = "max_tokens"

        body, esc = tier3_merge(
            action,
            "# Acme Corp\n\nFintech startup, Series B.",
            "sessions/raw.md",
            client,
        )
        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"
        assert "truncated" in esc.description.lower()

    def test_normal_stop_reason_is_not_treated_as_truncated(self) -> None:
        """A normal ``end_turn`` completion must not trip the truncation
        guard — only an actual max_tokens cutoff should refuse to write.
        """
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        client = _mock_client("# Acme Corp\n\nFintech startup, Series B, Series C.")
        client.messages.create.return_value.stop_reason = "end_turn"

        body, esc = tier3_merge(
            action,
            "# Acme Corp\n\nFintech startup, Series B.",
            "sessions/raw.md",
            client,
        )
        assert body is not None
        assert esc is None


class TestTier3MergeTruncationGuard:
    """Issue athenaeum#1180: ``fence_untrusted`` hard-slices ``existing_body`` to
    ``_MAX_EXISTING_BODY_CHARS`` with no truncation marker. A page past that
    window must not be silently amputated:

    - full-echo (the model's response REPLACES the whole file) must REFUSE
      to run at all and escalate instead of risking content loss.
    - patch mode (anchored ops apply against the real, untruncated body) may
      safely continue, but must record the resulting dedup blindness rather
      than pretend athenaeum#297's "empty ops = no-op" guarantee still holds
      past the window.
    """

    def _big_body(self, prefix: str, filler_lines: int = 2000) -> str:
        """A body whose *prefix* sits well inside the merge window, padded
        past ``_MAX_EXISTING_BODY_CHARS`` with repeated filler."""
        return prefix + ("Filler filler filler filler.\n" * filler_lines)

    def test_full_echo_refuses_on_truncated_existing_body(self) -> None:
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS, tier3_merge_full

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        existing_body = self._big_body("# Acme Corp\n\nFintech startup, Series B.\n\n")
        assert len(existing_body) > _MAX_EXISTING_BODY_CHARS

        client = MagicMock()

        body, esc = tier3_merge_full(action, existing_body, "sessions/raw.md", client)

        # No LLM call at all — the refusal happens before the request is built,
        # so an oversized page never pays for a doomed full-echo call.
        client.messages.create.assert_not_called()
        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"
        desc = esc.description.lower()
        assert "truncat" in desc or "window" in desc
        assert "1180" in esc.description

    def test_full_echo_runs_normally_when_body_within_window(self) -> None:
        """Sanity check the guard is scoped to oversized bodies — a normal-sized
        page must still go through full-echo as before."""
        from athenaeum.tiers import tier3_merge_full

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        client = _mock_client("# Acme Corp\n\nFintech startup, Series C.")

        body, esc = tier3_merge_full(
            action, "# Acme Corp\n\nFintech startup.", "sessions/raw.md", client
        )
        client.messages.create.assert_called_once()
        assert body is not None
        assert esc is None

    def test_patch_mode_continues_and_logs_dedup_blindness_on_truncated_body(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.tiers import (
            _MAX_EXISTING_BODY_CHARS,
            MERGE_TRUNCATED_INPUT_LOG_PREFIX,
        )

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        # Anchor lives well inside the window; the body is padded past it with
        # filler so the FULL body (which apply_merge_ops searches) exceeds
        # _MAX_EXISTING_BODY_CHARS.
        existing_body = self._big_body("# Acme Corp\n\nFintech startup, Series B.\n\n")
        assert len(existing_body) > _MAX_EXISTING_BODY_CHARS

        ops_response = json.dumps(
            {
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": "Fintech startup, Series B.",
                        "text": "Raised Series C in Q1 2024.[^1]",
                    }
                ]
            }
        )
        client = _mock_client(ops_response)

        with caplog.at_level("WARNING"):
            body, esc = tier3_merge(action, existing_body, "sessions/raw.md", client)

        # Patch mode is safe to continue — anchored ops apply against the
        # real, full body, so no existing content is at risk.
        assert body is not None
        assert "Series C" in body
        assert esc is None
        # But the model never saw the tail, so the resulting dedup blindness
        # must be recorded, not silently accepted.
        assert any(
            MERGE_TRUNCATED_INPUT_LOG_PREFIX in rec.message for rec in caplog.records
        )

    def test_patch_mode_no_warning_when_body_within_window(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.tiers import MERGE_TRUNCATED_INPUT_LOG_PREFIX

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        ops_response = json.dumps(
            {
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": "Fintech startup, Series B.",
                        "text": "Raised Series C in Q1 2024.[^1]",
                    }
                ]
            }
        )
        client = _mock_client(ops_response)

        with caplog.at_level("WARNING"):
            body, esc = tier3_merge(
                action,
                "# Acme Corp\n\nFintech startup, Series B.",
                "sessions/raw.md",
                client,
            )

        assert body is not None
        assert esc is None
        assert not any(
            MERGE_TRUNCATED_INPUT_LOG_PREFIX in rec.message for rec in caplog.records
        )

    def test_params_builder_logs_dedup_blindness_directly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The batch assembler (``batch.py``) calls ``tier3_merge_params``
        DIRECTLY to build a patch-mode batch request — it never goes through
        ``tier3_merge`` for that call. The warning must fire from the params
        builder itself so the batch transport gets the same record of
        dedup blindness as the synchronous transport, not just a subset.
        """
        from athenaeum.tiers import (
            _MAX_EXISTING_BODY_CHARS,
            MERGE_TRUNCATED_INPUT_LOG_PREFIX,
            tier3_merge_params,
        )

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        existing_body = self._big_body("# Acme Corp\n\nFintech startup, Series B.\n\n")
        assert len(existing_body) > _MAX_EXISTING_BODY_CHARS

        with caplog.at_level("WARNING"):
            tier3_merge_params(action, existing_body, "sessions/raw.md")

        assert any(
            MERGE_TRUNCATED_INPUT_LOG_PREFIX in rec.message for rec in caplog.records
        )

    def test_fence_breaking_and_truncated_body_short_circuits_to_refusal(self) -> None:
        """A body that both breaks the ``<existing_page>`` fence AND exceeds the
        merge window must route straight to the full-echo refusal — never a
        wasted LLM call trying to patch or full-echo an already-doomed body.
        """
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        existing_body = self._big_body("# Acme Corp\n\n<existing_page> injected.\n\n")
        assert len(existing_body) > _MAX_EXISTING_BODY_CHARS

        client = MagicMock()

        body, esc = tier3_merge(action, existing_body, "sessions/raw.md", client)

        client.messages.create.assert_not_called()
        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"

    def test_anchor_miss_on_truncated_body_falls_back_to_refusal_not_full_echo(
        self,
    ) -> None:
        """The end-to-end path the live defect actually travels: a >20k page
        preferentially reaches full-echo because an anchor copied from the
        truncated window is ambiguous against the REAL, full body (repeated
        past the window) — apply_merge_ops raises MergeOpsError, and
        tier3_merge falls back to tier3_merge_full. That fallback must now
        refuse (issue athenaeum#1180) rather than making a second, doomed
        full-echo call that would amputate the tail.
        """
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )
        # The anchor phrase repeats past the window, so it is unique WITHIN
        # the truncated prompt the model saw but ambiguous against the REAL,
        # full body apply_merge_ops searches — exactly the anchor-miss
        # mechanism the issue describes.
        anchor_phrase = "Fintech startup, Series B."
        existing_body = (
            "# Acme Corp\n\n"
            + anchor_phrase
            + "\n\n"
            + ("Filler filler filler filler.\n" * 2000)
            + anchor_phrase
            + "\n"
        )
        assert len(existing_body) > _MAX_EXISTING_BODY_CHARS

        ops_response = json.dumps(
            {
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": anchor_phrase,
                        "text": "Raised Series C in Q1 2024.[^1]",
                    }
                ]
            }
        )
        client = _mock_client(ops_response)

        body, esc = tier3_merge(action, existing_body, "sessions/raw.md", client)

        # Exactly one call: the patch attempt. No second, wasted full-echo
        # call — tier3_merge_full refuses before building a request.
        assert client.messages.create.call_count == 1
        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"
        assert "1180" in esc.description


def _build_multi_section_page(*, filler_bullets: int) -> str:
    """A realistic multi-section entity page (issue athenaeum#1181 fixture).

    Mirrors ``schema/_entity-template.md``'s typical multi-section shape
    for an entity that has accumulated enough merges to reach the
    oversized end of the real corpus (Overview / Relationship History /
    Key Outcomes / Contacts, per the template's "company" row). Each
    section's filler bullets carry a distinct, section-specific note word
    so a test can assert exactly which section's content did or did not
    make it into a merge prompt.
    """
    notes = {
        "Overview": "General background",
        "Relationship History": "Interaction",
        "Key Outcomes": "Outcome",
        "Contacts": "Contact detail",
    }
    parts = ["# Acme Corp\n\n"]
    for heading, note in notes.items():
        bullets = "\n".join(
            f"- {note} accumulated fact #{i}, recorded via routine merge.[^{i}]"
            for i in range(filler_bullets)
        )
        parts.append(f"## {heading}\n\n{bullets}\n\n")
    return "".join(parts)


class TestSectionScopedMerge:
    """Issue athenaeum#1181: section-scoped merging.

    The OUTPUT side of the ~84%-echo problem (anchored edit ops applied to
    the real file) was already solved by athenaeum#469 — see
    ``TestTier3MergePatchOps`` below. These tests cover the INPUT side:
    what ``tier3_merge_params`` fences into ``<existing_page>``.
    """

    def test_scopes_prompt_to_the_matching_section_not_the_whole_page(self) -> None:
        """AC1: a merge sends only the section(s) it targets."""
        from athenaeum.tiers import tier3_merge_params

        body = _build_multi_section_page(filler_bullets=30)
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Contacts update: support line moved to a toll-free number.",
        )
        params = tier3_merge_params(action, body, "sessions/raw.md")
        sent = params["messages"][0]["content"]

        assert "Contact detail accumulated fact #0" in sent
        # The other sections' own filler content must NOT be present — this
        # is genuinely scoped, not the whole page.
        assert "General background accumulated fact #0" not in sent
        assert "Interaction accumulated fact #0" not in sent
        assert "Outcome accumulated fact #0" not in sent
        # The lightweight outline still names every section, so the model
        # can see the page's structure without its full content.
        assert "Overview" in sent
        assert "Relationship History" in sent
        assert "Key Outcomes" in sent

    def test_no_matching_section_falls_back_to_last_section_with_outline(self) -> None:
        """AC1: 'a merge that has nothing to attach to must still work' —
        an observation sharing no vocabulary with any section still gets
        real section content (the last section) plus the outline, and a
        pointer to the anchor-free ``append_section`` op, never an empty
        fence."""
        from athenaeum.tiers import tier3_merge_params

        body = _build_multi_section_page(filler_bullets=10)
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Zzyzx qwerty plonk frobnicate wibble.",
        )
        params = tier3_merge_params(action, body, "sessions/raw.md")
        sent = params["messages"][0]["content"]

        assert "<existing_page>" in sent
        assert "Contact detail accumulated fact #0" in sent  # last section
        assert "Overview" in sent  # named in the outline
        assert "append_section" in sent  # scoping note steers to the anchor-free op

    def test_single_heading_body_is_not_scoped(self) -> None:
        """A body with fewer than two heading-delimited sections (the
        common freshly-created-entity shape — one ``# Entity Name``
        heading, no ``##`` substructure yet) is left completely unscoped —
        byte-identical to pre-athenaeum#1181 behavior."""
        from athenaeum.tiers import _select_merge_section

        body = "# Acme Corp\n\n" + ("Fact.\n" * 50)
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="New fact about Acme.",
        )
        selected, was_scoped = _select_merge_section(body, action)
        assert selected == body
        assert was_scoped is False

    def test_kill_switch_restores_full_body_echo(self) -> None:
        """The kill switch (``librarian.section_scoped_merge_enabled:
        false``) restores today's whole-body echo — no scoping note — for
        a page that would otherwise be scoped, with no code change."""
        from athenaeum.tiers import tier3_merge_params

        body = _build_multi_section_page(filler_bullets=5)
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Contacts update: support line moved to a toll-free number.",
        )
        config = {"librarian": {"section_scoped_merge_enabled": False}}
        params = tier3_merge_params(action, body, "sessions/raw.md", config=config)
        sent = params["messages"][0]["content"]

        # Every section's own filler is present — the whole body, not a
        # scoped excerpt.
        assert "General background accumulated fact #0" in sent
        assert "Interaction accumulated fact #0" in sent
        assert "Outcome accumulated fact #0" in sent
        assert "Contact detail accumulated fact #0" in sent
        # No scoping note — the Instructions section is byte-identical to
        # pre-athenaeum#1181.
        assert "is not the whole page" not in sent

    def test_ambiguous_anchor_across_sections_falls_back_not_misapplied(self) -> None:
        """THE correctness hazard (issue athenaeum#1181): an anchor unique
        WITHIN the section a merge is scoped to can still be ambiguous in
        the full page if the same text also occurs in a section that was
        NOT sent. The model, seeing only the Engineering section, has no
        way to know "Runs on Python 3.12." is not unique — but
        ``apply_merge_ops`` checks every anchor against the REAL, full,
        untruncated body regardless of what was sent, so it must refuse
        (found more than once) rather than silently editing one of the two
        occurrences. ``tier3_merge`` must fall back to full-echo, never
        guess.
        """
        shared_line = "Runs on Python 3.12."
        existing_body = (
            "# Acme Corp\n\n"
            "## Engineering\n\n"
            f"- {shared_line}\n"
            "- Uses Kubernetes for container orchestration.[^1]\n\n"
            "## Contact\n\n"
            f"- {shared_line}\n"
            "- Reach support via email at ACME-HELP.\n"
        )
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations=(
                "Engineering: services now run inside Kubernetes containers "
                "for better orchestration."
            ),
        )
        ops_response = json.dumps(
            {
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": shared_line,
                        "text": " Also uses Django.[^2]",
                    }
                ]
            }
        )
        full_echo_response = "# Acme Corp\n\n(full-echo fallback body)"
        client = _sequenced_client([ops_response, full_echo_response])

        body, esc = tier3_merge(action, existing_body, "sessions/raw.md", client)

        # TWO calls: the patch attempt (whose anchor turned out ambiguous
        # against the real full body) plus the full-echo fallback — never
        # a single call that silently applied the edit to one of the two
        # occurrences.
        assert client.messages.create.call_count == 2
        first_call_msg = client.messages.create.call_args_list[0].kwargs["messages"][
            0
        ]["content"]
        # Proves selection actually narrowed the prompt to Engineering —
        # otherwise this test would not be exercising athenaeum#1181 at all.
        assert "Uses Kubernetes" in first_call_msg
        assert "Reach support via email" not in first_call_msg
        # The ambiguous op was refused; the (mocked) full-echo fallback's
        # own output is what the merge actually returned.
        assert body is not None
        assert "full-echo fallback body" in body
        assert esc is None

    def test_echoed_chars_drop_materially_on_oversized_cohort(self) -> None:
        """AC2: measured echoed-chars-per-merge drops materially with
        section-scoping on vs off, on an oversized-page (20k+) fixture
        cohort. Uses the EXISTING ``merge_echoed_chars`` /
        ``echoed_chars_per_call`` instrumentation (issue athenaeum#1184) via
        the real params-building path (``tier3_merge_params``) — not a
        second, parallel measurement.

        This is a FIXTURE cohort, not the live 84-page real-corpus cohort
        — this container has no access to that corpus (see this issue's
        Honesty clause). See the lane's final report for the actual
        numbers measured here.
        """
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS, tier3_merge_params

        cohort = [
            (
                "Overview",
                "Overview update: the company rebranded its logo this quarter.",
            ),
            (
                "Contacts",
                "Contacts update: support line moved to a toll-free number.",
            ),
            (
                "Key Outcomes",
                "Key Outcomes update: Q3 renewal closed above target.",
            ),
        ]
        usage_off = TokenUsage()
        usage_on = TokenUsage()
        for _target_heading, obs_text in cohort:
            body = _build_multi_section_page(filler_bullets=120)
            assert len(body) > _MAX_EXISTING_BODY_CHARS
            action = EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations=obs_text,
            )
            tier3_merge_params(
                action,
                body,
                "sessions/raw.md",
                config={"librarian": {"section_scoped_merge_enabled": False}},
                usage=usage_off,
            )
            tier3_merge_params(
                action,
                body,
                "sessions/raw.md",
                config={"librarian": {"section_scoped_merge_enabled": True}},
                usage=usage_on,
            )

        assert usage_off.merge_calls == usage_on.merge_calls == len(cohort)
        # Material drop: scoped echo stays well under half of whole-body
        # echo on every page in this cohort.
        assert usage_on.merge_echoed_chars < usage_off.merge_echoed_chars * 0.5

    def test_relevant_content_beyond_20k_window_survives_scoping(self) -> None:
        """AC3: a merge on a >20,000-char page whose relevant content sits
        BEYOND the 20,000-char cut completes without the input window
        truncating it away — the case showing section-scoping supersedes
        raising ``_MAX_EXISTING_BODY_CHARS`` again."""
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS, tier3_merge_params

        # "Key Outcomes" is the LAST section, pushed well past char 20,000
        # by the three sections ahead of it.
        body = _build_multi_section_page(filler_bullets=200)
        key_outcomes_start = body.index("## Key Outcomes")
        assert key_outcomes_start > _MAX_EXISTING_BODY_CHARS, (
            "fixture must place the targeted section past the truncation window"
        )
        marker = "UNIQUE_OUTCOME_MARKER_XYZ"
        body = body.replace("## Key Outcomes\n\n", f"## Key Outcomes\n\n{marker}\n\n", 1)

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations=(
                "Key Outcomes update: outcome exceeded target this quarter, "
                "renewal closed above target."
            ),
        )
        params = tier3_merge_params(action, body, "sessions/raw.md")
        sent = params["messages"][0]["content"]
        assert marker in sent

    def test_splice_back_leaves_untargeted_sections_byte_identical(self) -> None:
        """AC4: driven through the real ``tier3_merge`` pipeline (not just
        ``apply_merge_ops`` in isolation) — a section-scoped merge
        targeting one section leaves every other section byte-for-byte
        unchanged."""
        from athenaeum.tiers import _split_into_sections

        body = _build_multi_section_page(filler_bullets=20)
        untargeted = {
            heading: text
            for heading, text in _split_into_sections(body)
            if heading != "Overview"
        }
        assert untargeted

        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Overview update: rebranded logo this quarter.",
        )
        ops_response = json.dumps(
            {
                "ops": [
                    {
                        "op": "insert_after",
                        "anchor": "## Overview\n\n",
                        "text": "- Rebranded logo this quarter.[^999]\n",
                    }
                ]
            }
        )
        client = _mock_client(ops_response)

        updated_body, esc = tier3_merge(action, body, "sessions/raw.md", client)

        assert esc is None
        assert updated_body is not None
        assert "Rebranded logo" in updated_body
        for heading, text in untargeted.items():
            assert text in updated_body, f"section {heading!r} was altered"


class TestTier3MergePatchOps:
    """Issue athenaeum#469: the tier-3 merge returns ANCHORED EDIT OPERATIONS that the
    librarian applies deterministically (cutting output ~80–90%), with a
    full-echo fallback that guarantees quality is never worse than before.

    These pin (a) the deterministic apply logic, (b) byte-identical
    equivalence to a full-echo merge on the acceptance-criteria fixture set,
    and (c) the fallback triggers (unparseable JSON, anchor miss, truncation).
    """

    @staticmethod
    def _action() -> EntityAction:
        return EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme raised Series C in Q1 2024.",
        )

    # --- apply_merge_ops: deterministic application (byte-identical fixtures) --

    def test_append_only_matches_full_echo(self) -> None:
        existing = "# Acme Corp\n\nFintech startup, Series B."
        ops = [{"op": "append_section", "text": "Raised Series C in Q1 2024.[^2]"}]
        # Byte-identical to what a full-echo merge would have produced.
        assert apply_merge_ops(existing, ops) == (
            "# Acme Corp\n\nFintech startup, Series B."
            "\n\nRaised Series C in Q1 2024.[^2]"
        )

    def test_mid_page_edit_replace(self) -> None:
        existing = "# Acme Corp\n\nHQ: SF.\n\nFounded 2019."
        ops = [{"op": "replace", "anchor": "HQ: SF.", "text": "HQ: NYC.[^2]"}]
        assert apply_merge_ops(existing, ops) == (
            "# Acme Corp\n\nHQ: NYC.[^2]\n\nFounded 2019."
        )

    def test_insert_after_anchor(self) -> None:
        existing = "# Acme\n\n- Series A[^1]\n- Series B[^1]"
        ops = [
            {
                "op": "insert_after",
                "anchor": "- Series B[^1]",
                "text": "\n- Series C[^2]",
            }
        ]
        assert apply_merge_ops(existing, ops) == (
            "# Acme\n\n- Series A[^1]\n- Series B[^1]\n- Series C[^2]"
        )

    def test_dedup_no_op_returns_body_unchanged(self) -> None:
        # athenaeum#297 dedup: a re-confirming observation with nothing new → empty
        # ops → body returned byte-for-byte unchanged.
        existing = "# Acme\n\nFintech.[^1]"
        assert apply_merge_ops(existing, []) == existing

    def test_multi_op_applied_in_single_pass(self) -> None:
        # replace + insert_after + append, given out of positional order —
        # applied over the ORIGINAL body so order in the list never matters.
        existing = "# Acme\n\nHQ: SF.\n\nStage: Series B."
        ops = [
            {
                "op": "replace",
                "anchor": "Stage: Series B.",
                "text": "Stage: Series C.[^2]",
            },
            {"op": "insert_after", "anchor": "HQ: SF.", "text": " (moved 2024)[^2]"},
            {"op": "append_section", "text": "See also: funding.[^2]"},
        ]
        assert apply_merge_ops(existing, ops) == (
            "# Acme\n\nHQ: SF. (moved 2024)[^2]\n\nStage: Series C.[^2]"
            "\n\nSee also: funding.[^2]"
        )

    def test_anchor_missing_raises(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops(
                "# Acme\n\nFintech.",
                [{"op": "replace", "anchor": "NOPE", "text": "x"}],
            )

    def test_ambiguous_anchor_raises(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops("aa aa", [{"op": "replace", "anchor": "aa", "text": "x"}])

    def test_overlapping_replaces_raise(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops(
                "abcdef",
                [
                    {"op": "replace", "anchor": "abcd", "text": "X"},
                    {"op": "replace", "anchor": "cdef", "text": "Y"},
                ],
            )

    def test_unknown_op_kind_raises(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops("body", [{"op": "delete", "anchor": "body", "text": ""}])

    def test_missing_anchor_field_raises(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops("body", [{"op": "replace", "text": "x"}])

    def test_missing_op_field_raises(self) -> None:
        # M17 phase 2a (athenaeum#1035): the one missing-required mismatch in the
        # measured window ("0.op: Field required") — pinning that a wholly
        # absent `op` key (not just an unrecognized value) is caught here,
        # same as test_unknown_op_kind_raises above.
        with pytest.raises(MergeOpsError):
            apply_merge_ops("body", [{"anchor": "body", "text": "x"}])

    def test_extra_key_on_an_op_is_tolerated(self) -> None:
        # athenaeum#1035: the two extra-key shapes actually observed in the
        # measured window ([].text2, [].append_section) must not block
        # application — extra="allow" is the decided (unchanged) posture for
        # tiers.tier3-merge.
        existing = "# Acme\n\nFintech."
        ops = [
            {"op": "append_section", "text": "Raised Series C.[^2]", "text2": "unused"}
        ]
        assert (
            apply_merge_ops(existing, ops)
            == "# Acme\n\nFintech.\n\nRaised Series C.[^2]"
        )

    # --- parse_merge_ops_response: the fallback-signalling contract ----------

    def test_parse_signals_fallback_on_unparseable(self) -> None:
        body, esc, needs_fallback = parse_merge_ops_response(
            "not json", self._action(), "ref", "existing"
        )
        assert (body, esc) == (None, None)
        assert needs_fallback is True

    def test_parse_signals_fallback_on_max_tokens(self) -> None:
        body, esc, needs_fallback = parse_merge_ops_response(
            '{"ops": [', self._action(), "ref", "existing", stop_reason="max_tokens"
        )
        assert needs_fallback is True

    def test_parse_applies_valid_ops(self) -> None:
        body, esc, needs_fallback = parse_merge_ops_response(
            json.dumps({"ops": [{"op": "append_section", "text": "New.[^2]"}]}),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is False
        assert esc is None
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    def test_parse_signals_fallback_on_op_missing_the_op_key(self) -> None:
        # M17 phase 2a (athenaeum#1035): a JSON-valid ops list whose single op is
        # missing "op" entirely — the measured window's one missing-required
        # mismatch. Reaches apply_merge_ops, which raises MergeOpsError; the
        # caller turns that into the same reject-and-degrade-to-full-echo
        # fallback as any other unusable ops list.
        body, esc, needs_fallback = parse_merge_ops_response(
            json.dumps({"ops": [{"anchor": "Old.", "text": "New.[^2]"}]}),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert (body, esc) == (None, None)
        assert needs_fallback is True

    def test_parse_applies_ops_with_extra_key_from_the_measured_window(self) -> None:
        # athenaeum#1035: [].append_section as an unexpected key (distinct from
        # the "append_section" op KIND) is one of the two extra-key shapes
        # actually observed — must still apply cleanly (extra="allow").
        body, esc, needs_fallback = parse_merge_ops_response(
            json.dumps(
                {
                    "ops": [
                        {"op": "append_section", "text": "New.[^2]", "text2": "unused"}
                    ]
                }
            ),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is False
        assert esc is None
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    # --- athenaeum#490 (slice A): each fallback names the page + a distinct cause ------

    def _fallback_warnings(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [
            rec.message
            for rec in caplog.records
            if rec.levelno == logging.WARNING
            and MERGE_FALLBACK_LOG_PREFIX in rec.message
        ]

    def test_max_tokens_fallback_warns_naming_page_and_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="athenaeum")
        parse_merge_ops_response(
            '{"ops": [',
            self._action(),
            "sess-ref",
            "existing",
            stop_reason="max_tokens",
        )
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert "cause=max_tokens" in warnings[0]
        assert "page=Acme Corp" in warnings[0]
        assert "source=sess-ref" in warnings[0]

    def test_parse_fail_fallback_warns_naming_page_and_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="athenaeum")
        parse_merge_ops_response("not json", self._action(), "sess-ref", "existing")
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert "cause=parse-fail" in warnings[0]
        assert "page=Acme Corp" in warnings[0]
        assert "source=sess-ref" in warnings[0]

    def test_anchor_miss_fallback_warns_naming_page_and_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="athenaeum")
        parse_merge_ops_response(
            json.dumps(
                {"ops": [{"op": "replace", "anchor": "NONEXISTENT", "text": "x"}]}
            ),
            self._action(),
            "sess-ref",
            "# Acme\n\nBody.",
        )
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert "cause=anchor-miss" in warnings[0]
        assert "page=Acme Corp" in warnings[0]
        assert "source=sess-ref" in warnings[0]

    def test_patch_mode_success_emits_no_fallback_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The signal must stay meaningful: a clean patch-mode apply (and the
        # dedup no-op / inline ESCALATE paths) emit NO fallback warning.
        caplog.set_level(logging.WARNING, logger="athenaeum")
        parse_merge_ops_response(
            json.dumps({"ops": [{"op": "append_section", "text": "New.[^2]"}]}),
            self._action(),
            "sess-ref",
            "# Acme\n\nOld.",
        )
        parse_merge_ops_response(
            json.dumps({"ops": []}), self._action(), "sess-ref", "# Acme\n\nOld."
        )
        parse_merge_ops_response(
            "ESCALATE: conflict\n---\n# Acme\n\nDisputed.",
            self._action(),
            "sess-ref",
            "# Acme\n\nOld.",
        )
        assert self._fallback_warnings(caplog) == []

    # --- athenaeum#496: parse-fail hardening (fix a/b) + discriminated sub-causes ------

    def test_fix_a_recovers_ops_object_from_ambiguous_response(self) -> None:
        # Two balanced top-level objects — a prose example object precedes the
        # real answer — so extract_json_object refuses (clause-4 ambiguity).
        # Fix (a) prefers the single ops-bearing candidate and applies it: no
        # fallback, no relaxing of the shared util's exactly-one rule.
        text = (
            'Here is an example: {"note": "ignore me"}\n'
            'Answer:\n{"ops": [{"op": "append_section", "text": "New.[^2]"}]}'
        )
        body, esc, needs_fallback = parse_merge_ops_response(
            text, self._action(), "ref", "# Acme\n\nOld."
        )
        assert needs_fallback is False
        assert esc is None
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    def test_fix_b_wraps_dict_valued_ops(self) -> None:
        # A single op emitted as a bare dict (not wrapped in a list). Fix (b)
        # coerces it to a one-element list; apply_merge_ops still validates it.
        body, esc, needs_fallback = parse_merge_ops_response(
            json.dumps({"ops": {"op": "append_section", "text": "New.[^2]"}}),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is False
        assert esc is None
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    def test_fix_b_accepts_operations_alternate_key(self) -> None:
        body, esc, needs_fallback = parse_merge_ops_response(
            json.dumps({"operations": [{"op": "append_section", "text": "New.[^2]"}]}),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is False
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    def test_ambiguous_no_ops_candidate_warns_sub_cause_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Multiple balanced objects, none carrying ops — genuinely ambiguous;
        # fix (a) must NOT guess. Distinct greppable sub-cause + fallback.
        caplog.set_level(logging.WARNING, logger="athenaeum")
        text = '{"foo": 1}\n{"bar": 2}'
        _b, _e, needs_fallback = parse_merge_ops_response(
            text, self._action(), "sess-ref", "existing"
        )
        assert needs_fallback is True
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert f"cause={MERGE_PARSE_FAIL_AMBIGUOUS}" in warnings[0]
        assert "page=Acme Corp" in warnings[0]

    def test_shape_failure_warns_sub_cause_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An object parsed, but ops is neither a list nor a dict nor under a
        # recognized alternate key — a shape failure, not ambiguity/no-json.
        caplog.set_level(logging.WARNING, logger="athenaeum")
        _b, _e, needs_fallback = parse_merge_ops_response(
            json.dumps({"ops": "append_section"}),
            self._action(),
            "sess-ref",
            "existing",
        )
        assert needs_fallback is True
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert f"cause={MERGE_PARSE_FAIL_SHAPE}" in warnings[0]

    def test_no_json_prose_reply_preserves_full_echo_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Sub-cause (c): the model emitted prose with no JSON object at all
        # (e.g. "No changes needed."). This MUST still signal a full-echo
        # fallback — never a silent no-op that would drop the merge.
        caplog.set_level(logging.WARNING, logger="athenaeum")
        body, esc, needs_fallback = parse_merge_ops_response(
            "No changes are needed for this page.",
            self._action(),
            "sess-ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is True
        assert (body, esc) == (None, None)
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert f"cause={MERGE_PARSE_FAIL_NO_JSON}" in warnings[0]

    def test_parse_fail_warning_carries_redacted_response_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The WARNING appends a truncated prefix of the response so the next
        # nightly can eyeball what came back — with PII (e.g. an email) redacted.
        caplog.set_level(logging.WARNING, logger="athenaeum")
        parse_merge_ops_response(
            "No JSON here. Contact alice@example.com for details.",
            self._action(),
            "sess-ref",
            "existing",
        )
        warnings = self._fallback_warnings(caplog)
        assert len(warnings) == 1
        assert "resp[:" in warnings[0]
        assert "alice@example.com" not in warnings[0]

    def test_single_ops_object_still_applies_directly(self) -> None:
        # Regression guard: a single, unambiguous ops object never enters the
        # fix-(a) recovery path — extract_json_object returns it outright.
        body, _esc, needs_fallback = parse_merge_ops_response(
            json.dumps({"ops": [{"op": "append_section", "text": "New.[^2]"}]}),
            self._action(),
            "ref",
            "# Acme\n\nOld.",
        )
        assert needs_fallback is False
        assert body == "# Acme\n\nOld.\n\nNew.[^2]"

    # --- tier3_merge: patch primary + full-echo fallback wiring --------------

    def test_patch_response_applied_without_fallback(self) -> None:
        client = _mock_client(
            json.dumps(
                {"ops": [{"op": "append_section", "text": "Raised Series C.[^2]"}]}
            )
        )
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
        )
        assert esc is None
        assert body == "# Acme Corp\n\nFintech, Series B.\n\nRaised Series C.[^2]"
        assert client.messages.create.call_count == 1  # patch only, no fallback

    def test_dedup_no_op_through_tier3_merge(self) -> None:
        client = _mock_client(json.dumps({"ops": []}))
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech.[^1]", "ref", client
        )
        assert esc is None
        assert body == "# Acme Corp\n\nFintech.[^1]"  # unchanged
        assert client.messages.create.call_count == 1

    def test_malformed_ops_json_falls_back_to_full_echo(self) -> None:
        client = _sequenced_client(
            ["not json at all", "# Acme Corp\n\nFintech, Series B, Series C.[^2]"]
        )
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
        )
        assert esc is None
        assert body == "# Acme Corp\n\nFintech, Series B, Series C.[^2]"
        assert client.messages.create.call_count == 2  # patch + full-echo retry

    def test_anchor_miss_falls_back_to_full_echo(self) -> None:
        client = _sequenced_client(
            [
                json.dumps(
                    {"ops": [{"op": "replace", "anchor": "NONEXISTENT", "text": "x"}]}
                ),
                "# Acme Corp\n\nFintech, Series B, Series C.[^2]",
            ]
        )
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
        )
        assert body == "# Acme Corp\n\nFintech, Series B, Series C.[^2]"
        assert client.messages.create.call_count == 2

    def test_patch_truncation_falls_back_and_succeeds(self) -> None:
        # A max_tokens cutoff of the ops list must NOT half-apply — it falls
        # back to the full-echo path, which here completes normally.
        client = _sequenced_client(
            [
                '{"ops": [{"op": "append_section", "text": "partial',  # truncated
                "# Acme Corp\n\nFintech, Series B, Series C.[^2]",
            ],
            stop_reasons=["max_tokens", "end_turn"],
        )
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
        )
        assert esc is None
        assert body == "# Acme Corp\n\nFintech, Series B, Series C.[^2]"
        assert client.messages.create.call_count == 2

    def test_escalate_handled_inline_without_fallback(self) -> None:
        # A principled ESCALATE is handled in patch mode WITHOUT a fallback
        # call (so batch mode, which forbids sync calls, escalates too).
        client = _mock_client(
            "ESCALATE: fintech vs pivot conflict\n---\n# Acme Corp\n\nFintech (disputed)."
        )
        body, esc = tier3_merge(
            self._action(), "# Acme Corp\n\nFintech.", "ref", client
        )
        assert esc is not None and esc.conflict_type == "principled"
        assert body == "# Acme Corp\n\nFintech (disputed)."
        assert client.messages.create.call_count == 1

    def test_live_shape_patch_output_is_small(self) -> None:
        # Acceptance criterion: a 20k-char body + a small addition merges with
        # a tiny patch response (a few edits + footnote), independent of page
        # size — the whole point of athenaeum#469. ~4 chars/token → < 4000 chars is
        # comfortably under the 1k-output-token target.
        big_body = "\n\n".join(f"Fact number {i} about Acme.[^1]" for i in range(700))
        assert len(big_body) > 20_000
        ops = [
            {
                "op": "append_section",
                "text": "Raised Series C in Q1 2024.[^2]\n\n[^2]: sessions/raw.md",
            }
        ]
        patch = json.dumps({"ops": ops})
        assert len(patch) < 4_000  # < ~1k output tokens
        merged = apply_merge_ops(big_body, ops)
        assert big_body in merged  # nothing dropped
        assert merged.endswith("[^2]: sessions/raw.md")


class TestTier2And3SelfResolvingDocumentGuard:
    """Issue athenaeum#300: Tier 2 classify and Tier 3 create must apply the same
    self-resolving-document skepticism the contradiction/resolution path
    already applies — an embedded "Human-confirmed" claim inside raw intake
    is the document's own unverified assertion, not real sign-off.
    """

    def test_classify_system_prompt_guards_against_self_resolving_claims(self) -> None:
        from athenaeum.tiers import CLASSIFY_SYSTEM

        assert "human confirmation" in CLASSIFY_SYSTEM.lower()
        assert "not independent verification" in CLASSIFY_SYSTEM.lower()

    def test_create_system_prompt_guards_against_self_resolving_claims(self) -> None:
        from athenaeum.tiers import CREATE_SYSTEM

        assert "human confirmation" in CREATE_SYSTEM.lower()
        assert "settled fact" in CREATE_SYSTEM.lower()


class TestTier3PrincipledEscalationIsAnswerable:
    """Regression (athenaeum#166): the tier-3 / `principled` ESCALATE producer must
    emit an ANSWERABLE pending-question block — one carrying a `- [ ]`
    checkbox that ``answers.parse_pending_questions`` parses as unanswered.

    Historically a separate escalation path wrote ``**Conflict type**:
    principled`` + ``**Description**:`` blocks WITHOUT the ``- [ ]`` line, so
    the parser skipped them forever (a parser-side recovery now exists as
    defense-in-depth — kept — but the PRODUCER must not rely on it). This
    test pins the producer onto the single canonical renderer
    (:func:`tier4_escalate`): the ``EscalationItem`` that ``tier3_merge``
    builds on an ESCALATE verdict, when written via ``tier4_escalate``,
    yields a checkbox-bearing, parseable block.
    """

    @pytest.mark.parametrize(
        "raw_ref",
        [
            "drive/2026-06-01-note.md",
            "claude-session/20260601T120000Z-aabb.md",
            "briefings/2026-06-01.md",
            "name-repairs/acme.md",
            "retros/sprint-12.md",
        ],
    )
    def test_principled_escalation_block_has_checkbox_and_parses(
        self, raw_ref: str, tmp_path: Path
    ) -> None:
        from athenaeum.answers import parse_pending_questions

        # The EscalationItem exactly as tier3_merge's ESCALATE handler builds
        # it (conflict_type="principled"), for raw files from each of the
        # source scopes that previously produced checkbox-less blocks.
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme pivoted away from fintech.",
        )
        response = (
            "ESCALATE: Existing page says fintech, new observation says "
            "pivot away. Strategic direction conflict.\n"
            "---\n"
            "# Acme Corp\n\nFintech startup (disputed)."
        )
        client = _mock_client(response)
        _body, esc = tier3_merge(action, "# Acme Corp\n\nFintech.", raw_ref, client)
        assert esc is not None
        assert esc.conflict_type == "principled"

        # Route the producer's EscalationItem through the single canonical
        # renderer — no config (legacy path), so no auto-apply interferes.
        pending = tmp_path / "_pending_questions.md"
        tier4_escalate([esc], pending)

        text = pending.read_text(encoding="utf-8")
        # The block carries the answerable checkbox AND the principled keys.
        assert "- [ ]" in text
        assert "**Conflict type**: principled" in text

        # And the canonical parser sees exactly one unanswered question — i.e.
        # the block is genuinely answerable, not silently skipped.
        pqs = parse_pending_questions(pending)
        assert len(pqs) == 1
        assert not pqs[0].answered
        assert pqs[0].conflict_type == "principled"
        assert pqs[0].source == raw_ref
        assert pqs[0].question  # a non-empty question line was emitted


# ---------------------------------------------------------------------------
# Tier 3 — Write (integration with mocked LLM)
# ---------------------------------------------------------------------------


class TestTier3Write:
    """Integration test for tier3_write with mocked LLM calls."""

    def test_create_and_update_actions(self, wiki_dir: Path) -> None:
        raw = _make_raw("New info about Alice and Acme.")
        index = EntityIndex(wiki_dir)

        actions = [
            EntityAction(
                kind="create",
                name="Alice Zhang",
                entity_type="person",
                tags=["active"],
                access="internal",
                existing_uid=None,
                observations="Product lead.",
            ),
            EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations="New partnership announced.",
            ),
        ]

        create_response = MagicMock()
        create_response.content = [
            MagicMock(text="# Alice Zhang\n\nProduct lead at Acme.")
        ]
        # Issue athenaeum#469: the merge returns anchored edit ops. An append_section
        # op folds in the new note without a full-echo fallback call.
        merge_response = MagicMock()
        merge_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "ops": [
                            {
                                "op": "append_section",
                                "text": "New partnership announced.",
                            }
                        ]
                    }
                )
            )
        ]
        merge_response.stop_reason = "end_turn"

        client = MagicMock()
        client.messages.create.side_effect = [create_response, merge_response]

        new_entities, updated_uids, escalations = tier3_write(
            raw,
            actions,
            index,
            wiki_dir,
            client,
        )

        assert len(new_entities) == 1
        assert new_entities[0].name == "Alice Zhang"
        assert updated_uids == ["a1b2c3d4"]
        assert len(escalations) == 0
        # Verify the update was written to the existing file
        acme_content = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert "New partnership announced" in acme_content

    def test_create_api_error_propagates_through_write(self, wiki_dir: Path) -> None:
        """APIError in tier3_create must bubble out of tier3_write."""
        import anthropic as anthropic_mod

        raw = _make_raw("Info about a new person.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="create",
                name="Unknown Person",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="text",
            ),
        ]

        client = MagicMock()
        client.messages.create.side_effect = anthropic_mod.APIError(
            message="Error",
            request=MagicMock(),
            body=None,
        )

        with pytest.raises(anthropic_mod.APIError):
            tier3_write(raw, actions, index, wiki_dir, client)

    def test_no_disk_write_on_partial_failure(self, wiki_dir: Path) -> None:
        """If the second action fails, the first action's update must not be written."""
        import anthropic as anthropic_mod

        raw = _make_raw("Info about Acme and a new person.")
        index = EntityIndex(wiki_dir)

        actions = [
            EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations="Should NOT be written.",
            ),
            EntityAction(
                kind="create",
                name="Crash Entity",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="text",
            ),
        ]

        # First call (merge) succeeds, second call (create) fails
        merge_response = MagicMock()
        merge_response.content = [MagicMock(text="# Acme Corp\n\nSHOULD NOT APPEAR")]
        client = MagicMock()
        client.messages.create.side_effect = [
            merge_response,
            anthropic_mod.APIError(message="Crash", request=MagicMock(), body=None),
        ]

        acme_before = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()

        with pytest.raises(anthropic_mod.APIError):
            tier3_write(raw, actions, index, wiki_dir, client)

        acme_after = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert (
            acme_after == acme_before
        ), "update was written despite subsequent failure"

    def test_uid_lookup_instead_of_glob(self, wiki_dir: Path) -> None:
        """tier3_write uses EntityIndex UID lookup, not filesystem glob."""
        raw = _make_raw("Update info about Acme.")
        index = EntityIndex(wiki_dir)

        actions = [
            EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations="New info.",
            ),
        ]

        client = _mock_client("# Acme Corp\n\nUpdated content.")
        new_entities, updated_uids, escalations = tier3_write(
            raw,
            actions,
            index,
            wiki_dir,
            client,
        )

        assert updated_uids == ["a1b2c3d4"]
        acme_content = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert "Updated content" in acme_content


# ---------------------------------------------------------------------------
# Tier 3 — per-file budget (issue athenaeum#994, revising athenaeum#898):
# tier3_derive_actions checks the LLM-call / wall-clock bound INCREMENTALLY,
# after each action, and a trip carries whatever completed so far as durable
# partial progress rather than discarding the whole file's work.
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsBudget:
    def test_llm_calls_bound_lands_completed_actions_and_discards_the_rest(
        self, wiki_dir: Path
    ) -> None:
        """Three create actions, a 1-call-over-budget bound: the action that
        pushes the running count over the bound (the 2nd) still lands, along
        with the 1st that completed before it; the 3rd is never attempted —
        proved by the mock's side_effect queue holding only two responses,
        so a third call would raise StopIteration and fail this test."""
        raw = _make_raw("Three new people mentioned.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="create",
                name=f"Person {i}",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations=f"Observation {i}.",
            )
            for i in range(3)
        ]

        responses = []
        for i in range(2):  # only 2 — a 3rd call would StopIteration
            r = MagicMock()
            r.content = [MagicMock(text=f"# Person {i}\n\nObservation {i}.")]
            responses.append(r)
        client = MagicMock()
        client.messages.create.side_effect = responses

        usage = TokenUsage()
        with pytest.raises(RawFileOverBudgetError) as excinfo:
            tier3_derive_actions(
                raw,
                actions,
                index,
                wiki_dir,
                client,
                usage=usage,
                max_api_calls_for_file=1,
                calls_before_file=0,
            )

        exc = excinfo.value
        assert exc.bound == "llm_calls"
        assert [e.name for e in exc.new_entities] == ["Person 0", "Person 1"]
        assert exc.pending_updates == []
        assert exc.updated_uids == []
        assert exc.escalations == []

    def test_wall_clock_bound_lands_completed_actions_and_discards_the_rest(
        self, wiki_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same shape as the llm_calls test above but for the wall-clock
        bound: the clock advances 20s per LLM call against a 10s bound, so
        the 1st action's completion already trips it — landing that one
        action and discarding the 2nd, unattempted (StopIteration-guarded
        by a single-response queue)."""
        raw = _make_raw("Two new people mentioned.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="create",
                name=f"Person {i}",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations=f"Observation {i}.",
            )
            for i in range(2)
        ]

        clock = {"n": 0.0}
        monkeypatch.setattr("athenaeum.tiers.time.monotonic", lambda: clock["n"])

        response = MagicMock()
        response.content = [MagicMock(text="# Person 0\n\nObservation 0.")]

        _calls_made = {"n": 0}

        def _side_effect(**kwargs):
            # Guards the 2nd action: raises if ever called more than once,
            # proving "Person 1" is never attempted.
            _calls_made["n"] += 1
            if _calls_made["n"] > 1:
                raise AssertionError(
                    "a 2nd LLM call means the bound did not stop the loop"
                )
            clock["n"] += 20.0
            return response

        client = MagicMock()
        client.messages.create.side_effect = _side_effect

        with pytest.raises(RawFileOverBudgetError) as excinfo:
            tier3_derive_actions(
                raw,
                actions,
                index,
                wiki_dir,
                client,
                max_runtime_for_file=10.0,
                started_at_file=0.0,
            )

        exc = excinfo.value
        assert exc.bound == "wall_clock"
        assert [e.name for e in exc.new_entities] == ["Person 0"]

    def test_partial_progress_carries_pending_updates_from_a_completed_action(
        self, wiki_dir: Path
    ) -> None:
        """A mixed update-then-create action list: the update completes
        first (landing in ``pending_updates``/``updated_uids``) and the
        create that immediately follows is what actually trips the bound —
        both ride along on the raised exception, proving the partial
        payload isn't limited to ``new_entities``."""
        raw = _make_raw("Acme update plus a new person.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations="New partnership announced.",
            ),
            EntityAction(
                kind="create",
                name="New Person",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="text",
            ),
        ]

        merge_response = MagicMock()
        merge_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "ops": [
                            {
                                "op": "append_section",
                                "text": "New partnership announced.",
                            }
                        ]
                    }
                )
            )
        ]
        merge_response.stop_reason = "end_turn"
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# New Person\n\ntext")]

        client = MagicMock()
        client.messages.create.side_effect = [merge_response, create_response]

        usage = TokenUsage()
        with pytest.raises(RawFileOverBudgetError) as excinfo:
            tier3_derive_actions(
                raw,
                actions,
                index,
                wiki_dir,
                client,
                usage=usage,
                max_api_calls_for_file=1,
                calls_before_file=0,
            )

        exc = excinfo.value
        assert exc.updated_uids == ["a1b2c3d4"]
        assert len(exc.pending_updates) == 1
        assert exc.pending_updates[0][0] == wiki_dir / "a1b2c3d4-acme-corp.md"
        assert [e.name for e in exc.new_entities] == ["New Person"]

        # Not yet written — tier3_derive_actions never writes; that is the
        # caller's (process_one's) job on catching the exception.
        acme_content = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert "New partnership announced" not in acme_content

    def test_no_bound_configured_behaves_exactly_as_before(
        self, wiki_dir: Path
    ) -> None:
        """The default (``max_api_calls_for_file=None``) is unbounded —
        every other caller of tier3_derive_actions (tier3_write, and every
        pre-athenaeum#994 test) must see byte-identical behaviour."""
        raw = _make_raw("New info about Alice.")
        index = EntityIndex(wiki_dir)
        actions = [
            EntityAction(
                kind="create",
                name="Alice Zhang",
                entity_type="person",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="Product lead.",
            ),
        ]
        client = _mock_client("# Alice Zhang\n\nProduct lead.")

        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client
        )
        assert [e.name for e in new_entities] == ["Alice Zhang"]
        assert pending_updates == []
        assert updated_uids == []
        assert escalations == []


# ---------------------------------------------------------------------------
# Tier 3 — Provenance (issue athenaeum#95)
# ---------------------------------------------------------------------------


class TestTier3Provenance:
    """Issue athenaeum#95: tier3 write paths must emit source / field_sources."""

    def test_create_stamps_source_on_entity(self) -> None:
        action = EntityAction(
            kind="create",
            name="Bob Test",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="A new person.",
        )
        client = _mock_client("# Bob Test\n\nbody.")
        entity = tier3_create(action, "sessions/raw.md", client)
        assert entity.source is not None
        assert isinstance(entity.source, str)
        assert entity.source.startswith("claude:tier3-create:")

    def test_create_render_emits_source_in_frontmatter(self) -> None:
        action = EntityAction(
            kind="create",
            name="Carol",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="text",
        )
        client = _mock_client("# Carol\n\nbody.")
        entity = tier3_create(action, "sessions/raw.md", client)
        rendered = entity.render()
        assert "source: claude:tier3-create:" in rendered

    def test_merge_sets_field_sources_for_overwritten_fields(
        self,
        wiki_dir: Path,
    ) -> None:
        """tier3_write merge attributes overwritten fields to the
        merge source and preserves prior field_sources entries."""
        import textwrap as _tw

        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            _tw.dedent(
                """\
            ---
            uid: a1b2c3d4
            type: company
            name: Acme Corp
            access: confidential
            tags:
              - client
            created: '2024-03-15'
            updated: '2024-04-06'
            source: api:apollo
            field_sources:
              name: api:apollo
              tags: manual:tristan
            ---

            # Acme Corp

            Fintech startup.
        """
            )
        )
        raw = _make_raw("New info about Acme.")
        index = EntityIndex(wiki_dir)

        actions = [
            EntityAction(
                kind="update",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="",
                existing_uid="a1b2c3d4",
                observations="Acme expanded ops.",
            ),
        ]
        client = _mock_client("# Acme Corp\n\nFintech startup. Expanded ops.")
        tier3_write(raw, actions, index, wiki_dir, client)

        from athenaeum.models import parse_frontmatter

        text = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        meta, _body = parse_frontmatter(text)
        fs = meta.get("field_sources")
        assert isinstance(fs, dict)
        # Overwritten fields attributed to merge source
        assert fs["body"].startswith("claude:tier3-merge:")
        assert fs["updated"].startswith("claude:tier3-merge:")
        # Non-overwritten fields preserved
        assert fs["name"] == "api:apollo"
        assert fs["tags"] == "manual:tristan"


# ---------------------------------------------------------------------------
# Tier 4 — Escalation
# ---------------------------------------------------------------------------


class TestTier4:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            EscalationItem(
                raw_ref="sessions/20240406T120000Z-aabb0011.md",
                entity_name="Acme Corp",
                conflict_type="principled",
                description="Conflicting info about Acme's Series status.",
            ),
        ]
        tier4_escalate(items, pending)
        assert pending.exists()
        content = pending.read_text()
        assert "Acme Corp" in content
        assert "principled" in content
        assert "# Pending Questions" in content

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        pending.write_text("# Pending Questions\n\nExisting content here.\n")

        items = [
            EscalationItem(
                raw_ref="sessions/test.md",
                entity_name="New Entity",
                conflict_type="ambiguous",
                description="Unclear classification.",
            ),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert "Existing content here." in content
        assert "New Entity" in content
        assert "ambiguous" in content

    def test_empty_items_noop(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        tier4_escalate([], pending)
        assert not pending.exists()

    def test_multiple_items(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        items = [
            EscalationItem("ref1", "Entity Alpha", "principled", "Conflict Alpha"),
            EscalationItem("ref2", "Entity Beta", "ambiguous", "Conflict Beta"),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert "Entity Alpha" in content
        assert "Entity Beta" in content

    def test_renders_checkbox_line_under_header(self, tmp_path: Path) -> None:
        """New schema (issue athenaeum#61): leading `- [ ]` line under each header.

        The checkbox is the anchor for `ingest_answers` + the MCP
        `resolve_question` tool. Without it, an answered block cannot
        round-trip back into raw intake.
        """
        pending = tmp_path / "_pending_questions.md"
        items = [
            EscalationItem(
                raw_ref="sessions/test.md",
                entity_name="Acme Corp",
                conflict_type="principled",
                description="Conflicting info about Acme's Series status.",
            ),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        lines = content.splitlines()

        # Find the header line and assert the next non-blank line is a `- [ ]`.
        header_idx = next(i for i, line in enumerate(lines) if line.startswith("## ["))
        # Checkbox may follow directly with no blank line between (per issue
        # schema: `directly under the header`).
        assert lines[header_idx + 1].startswith(
            "- [ ]"
        ), f"expected checkbox line directly after header; got {lines[header_idx + 1]!r}"
        # Question text should be present on the checkbox line (derived
        # from the description).
        assert "Conflicting info about Acme" in lines[header_idx + 1]
        # Conflict-type and description lines preserved below.
        assert "**Conflict type**: principled" in content
        assert "**Description**: Conflicting info about Acme" in content

    def test_checkbox_fallback_for_empty_description(self, tmp_path: Path) -> None:
        """If description is empty, the checkbox line still renders a prompt."""
        pending = tmp_path / "_pending_questions.md"
        items = [
            EscalationItem(
                raw_ref="sessions/test.md",
                entity_name="Silent Co",
                conflict_type="ambiguous",
                description="",
            ),
        ]
        tier4_escalate(items, pending)
        content = pending.read_text()
        assert "- [ ] Resolve ambiguous conflict for Silent Co" in content


# ---------------------------------------------------------------------------
# Issue athenaeum#472 — Tier-2 classify no longer silently drops all entities on a
# bare (unescaped) control character inside a JSON string value.
# ---------------------------------------------------------------------------


# The exact production failure signature: a raw newline (and a tab) inside the
# free-text observations value, which rejects the WHOLE array under json.loads.
_BARE_NEWLINE_PAYLOAD = (
    "[\n"
    '  {"name": "Paine", "entity_type": "reference", "access": "internal",\n'
    '   "tags": [], "observations": "Operator persona driving topic-run.mjs\n'
    '  spanning multiple paragraphs\twith a tab too"},\n'
    '  {"name": "Second Entity", "entity_type": "reference",\n'
    '   "access": "internal", "tags": [], "observations": "fine"}\n'
    "]"
)


class TestTier2JsonRepair:
    """The parse function repairs bare control chars before discarding."""

    _TYPES = ["person", "reference"]

    def test_bare_newline_response_recovered_not_dropped(self) -> None:
        stats = Tier2ParseStats()
        results = parse_tier2_entities(
            _BARE_NEWLINE_PAYLOAD,
            "sessions/x.md",
            self._TYPES,
            [],
            ["internal"],
            stats=stats,
        )
        # Both entities recovered (pre-fix this returned []).
        assert [r.name for r in results] == ["Paine", "Second Entity"]
        assert stats.repaired == 1
        assert stats.degraded == 0

    def test_repair_logs_recovery_not_degraded_marker(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            parse_tier2_entities(
                _BARE_NEWLINE_PAYLOAD, "sessions/x.md", self._TYPES, [], ["internal"]
            )
        text = "\n".join(rec.message for rec in caplog.records)
        assert "tier2-classify-repaired" in text
        assert TIER2_DEGRADED_MARKER not in text

    def test_unrepairable_json_degrades_and_marks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Truncated array: not something the control-char repair can fix.
        stats = Tier2ParseStats()
        with caplog.at_level("WARNING"):
            results = parse_tier2_entities(
                '[{"name": "X", "entity_type": "reference"',
                "sessions/x.md",
                self._TYPES,
                [],
                ["internal"],
                stats=stats,
            )
        assert results == []
        assert stats.degraded == 1
        assert any(TIER2_DEGRADED_MARKER in rec.message for rec in caplog.records)

    def test_no_json_array_degrades(self) -> None:
        stats = Tier2ParseStats()
        results = parse_tier2_entities(
            "I could not find any entities to classify.",
            "sessions/x.md",
            self._TYPES,
            [],
            ["internal"],
            stats=stats,
        )
        assert results == []
        assert stats.degraded == 1

    def test_stats_optional_backward_compatible(self) -> None:
        # No stats arg → behaves exactly as before (still recovers, no raise).
        results = parse_tier2_entities(
            _BARE_NEWLINE_PAYLOAD, "sessions/x.md", self._TYPES, [], ["internal"]
        )
        assert len(results) == 2


class TestTier2ClassifyRetry:
    """The sync transport retries once when the first response is unparseable
    even after repair, rather than discarding the file's entities."""

    _TYPES = ["person", "reference"]

    def _valid_payload(self) -> str:
        return json.dumps(
            [
                {
                    "name": "Recovered",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "second try parsed",
                }
            ]
        )

    def test_retry_recovers_and_clears_degrade(self) -> None:
        # First response is unrepairable (truncated); the retry returns valid
        # JSON. tier2_classify must make a SECOND call and return the entities.
        client = _sequenced_client(
            ['[{"name": "X", "entity_type": "reference"', self._valid_payload()]
        )
        raw = _make_raw("Some rich source text with entities to classify.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 2
        assert [r.name for r in results] == ["Recovered"]
        assert stats.degraded == 0

    def test_retry_still_degraded_counts_once(self) -> None:
        # Both attempts unparseable → one degrade recorded, empty result, and
        # exactly one retry (not an unbounded loop).
        client = _sequenced_client(
            [
                '[{"name": "X", "entity_type": "reference"',
                "still not valid json at all",
            ]
        )
        raw = _make_raw("Some rich source text with entities to classify.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 2
        assert results == []
        assert stats.degraded == 1

    def test_clean_response_does_not_retry(self) -> None:
        client = _sequenced_client([self._valid_payload()])
        raw = _make_raw("Some rich source text with entities to classify.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 1
        assert [r.name for r in results] == ["Recovered"]
        assert stats.degraded == 0


# ---------------------------------------------------------------------------
# Issue athenaeum#476 — Tier-2 classify no longer silently drops all entities when the
# response is TRUNCATED at max_tokens on entity-dense files. The raised output
# budget removes the trigger; a truncation-specific retry + a distinct
# ``truncated`` marker/counter keep it from being conflated with a athenaeum#472 parse
# failure ever again.
# ---------------------------------------------------------------------------


# A truncated array — exactly what a max_tokens cutoff mid-object looks like:
# valid JSON as far as it goes, but missing its closing brackets, which no
# control-char repair can fix.
_TRUNCATED_PAYLOAD = (
    '[{"name": "Alpha", "entity_type": "reference", "access": "internal", '
    '"tags": [], "observations": "some facts"}, '
    '{"name": "Beta", "entity_type": "reference", "access": "internal", '
    '"tags": [], "observ'
)


class TestTier2RequestBudget:
    """Issue athenaeum#476 fix 1: the classify output budget was raised off 1024."""

    def test_classify_max_tokens_raised_above_1024(self) -> None:
        raw = _make_raw("Some rich source text with entities.")
        params = tier2_request_params(
            raw, [], ["person", "reference"], [], ["internal"]
        )
        # The original 1024 truncated entity-dense files; the fix raises it.
        assert params["max_tokens"] > 1024


class TestTier2TruncationParse:
    """``parse_tier2_entities`` classes a max_tokens drop as truncated, not
    degraded, so the two failure modes are never conflated (athenaeum#472's mistake)."""

    _TYPES = ["person", "reference"]

    def test_truncated_response_counts_truncated_not_degraded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = Tier2ParseStats()
        with caplog.at_level("WARNING"):
            results = parse_tier2_entities(
                _TRUNCATED_PAYLOAD,
                "sessions/x.md",
                self._TYPES,
                [],
                ["internal"],
                stats=stats,
                stop_reason="max_tokens",
            )
        assert results == []
        assert stats.truncated == 1
        assert stats.degraded == 0
        text = "\n".join(rec.message for rec in caplog.records)
        assert TIER2_TRUNCATED_MARKER in text
        assert TIER2_DEGRADED_MARKER not in text

    def test_no_json_at_max_tokens_is_truncated(self) -> None:
        # A response that ran out of budget before emitting any array at all.
        stats = Tier2ParseStats()
        results = parse_tier2_entities(
            "Here are the entities I found:",
            "sessions/x.md",
            self._TYPES,
            [],
            ["internal"],
            stats=stats,
            stop_reason="max_tokens",
        )
        assert results == []
        assert stats.truncated == 1
        assert stats.degraded == 0

    def test_same_bad_json_without_max_tokens_still_degrades(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Identical unparseable text, but NOT a truncation (stop_reason absent)
        # → the pre-athenaeum#476 degraded path, unchanged. This is the exact ambiguity
        # athenaeum#472 tripped on: the JSON error alone cannot tell the two apart.
        stats = Tier2ParseStats()
        with caplog.at_level("WARNING"):
            results = parse_tier2_entities(
                _TRUNCATED_PAYLOAD,
                "sessions/x.md",
                self._TYPES,
                [],
                ["internal"],
                stats=stats,
            )
        assert results == []
        assert stats.degraded == 1
        assert stats.truncated == 0
        text = "\n".join(rec.message for rec in caplog.records)
        assert TIER2_DEGRADED_MARKER in text
        assert TIER2_TRUNCATED_MARKER not in text

    def test_complete_array_at_max_tokens_still_parses(self) -> None:
        # A response that happened to finish exactly at the budget still yields
        # its entities — truncation is only inferred on a PARSE failure.
        payload = json.dumps(
            [
                {
                    "name": "Fits",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "ok",
                }
            ]
        )
        stats = Tier2ParseStats()
        results = parse_tier2_entities(
            payload,
            "sessions/x.md",
            self._TYPES,
            [],
            ["internal"],
            stats=stats,
            stop_reason="max_tokens",
        )
        assert [r.name for r in results] == ["Fits"]
        assert stats.truncated == 0
        assert stats.degraded == 0


class TestTier2ClassifyTruncationRetry:
    """The sync transport retries a truncated response with a LARGER budget,
    not the athenaeum#472 escaping instruction (the wrong fix for a truncation)."""

    _TYPES = ["person", "reference"]

    def _valid_payload(self) -> str:
        return json.dumps(
            [
                {
                    "name": "Recovered",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "second try",
                }
            ]
        )

    def test_truncation_retry_uses_larger_budget_and_recovers(self) -> None:
        # First response truncated (stop_reason=max_tokens), retry succeeds.
        client = _sequenced_client(
            [_TRUNCATED_PAYLOAD, self._valid_payload()],
            stop_reasons=["max_tokens", "end_turn"],
        )
        raw = _make_raw("Entity-dense source text worth classifying.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 2
        assert [r.name for r in results] == ["Recovered"]
        # Recovered → the first-attempt truncation is cleared from the summary.
        assert stats.truncated == 0
        assert stats.degraded == 0
        # The retry carried a LARGER max_tokens than the first attempt.
        first_budget = client.messages.create.call_args_list[0].kwargs["max_tokens"]
        retry_budget = client.messages.create.call_args_list[1].kwargs["max_tokens"]
        assert retry_budget > first_budget
        # And it did NOT append the athenaeum#472 escaping-instruction turn — the retry
        # message list is unchanged from the first call's.
        retry_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        assert len(retry_messages) == 1

    def test_truncation_retry_still_truncated_counts_once(self) -> None:
        # Both attempts truncate → exactly one retry, one truncation recorded,
        # NOT counted as degraded, file preserved for the next run.
        client = _sequenced_client(
            [_TRUNCATED_PAYLOAD, _TRUNCATED_PAYLOAD],
            stop_reasons=["max_tokens", "max_tokens"],
        )
        raw = _make_raw("Entity-dense source text worth classifying.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 2
        assert results == []
        assert stats.truncated == 1
        assert stats.degraded == 0

    def test_non_truncation_parse_failure_still_takes_escaping_retry(self) -> None:
        # A NON-truncation parse failure (stop_reason end_turn, unrepairable)
        # must still take the athenaeum#472 escaping retry (append instruction turns),
        # not the bigger-budget path.
        client = _sequenced_client(
            ['[{"name": "X", "entity_type": "reference"', self._valid_payload()],
            stop_reasons=["end_turn", "end_turn"],
        )
        raw = _make_raw("Some rich source text with entities to classify.")
        stats = Tier2ParseStats()
        results = tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client, stats=stats
        )
        assert client.messages.create.call_count == 2
        assert [r.name for r in results] == ["Recovered"]
        assert stats.degraded == 0
        assert stats.truncated == 0
        retry_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        # Escaping retry appends assistant + user instruction turns.
        assert len(retry_messages) == 3
        # And the escaping retry keeps the ORIGINAL (raised) budget — it does
        # not bump max_tokens (that is the truncation path's fix, not this).
        first_budget = client.messages.create.call_args_list[0].kwargs["max_tokens"]
        retry_budget = client.messages.create.call_args_list[1].kwargs["max_tokens"]
        assert retry_budget == first_budget


class TestTier2ReclassifyLargerBudget:
    """The shared bigger-budget retry helper both transports delegate to
    (issue athenaeum#476) — so the batch path gets a real retry too, not just sync."""

    _TYPES = ["person", "reference"]

    def _valid_payload(self) -> str:
        return json.dumps(
            [
                {
                    "name": "Bigger",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "fit now",
                }
            ]
        )

    def test_uses_retry_budget_and_returns_stats(self) -> None:
        client = _sequenced_client([self._valid_payload()], stop_reasons=["end_turn"])
        raw = _make_raw("Entity-dense source text worth classifying.")
        entities, retry_stats = tier2_reclassify_larger_budget(
            raw, [], self._TYPES, [], ["internal"], client
        )
        assert [e.name for e in entities] == ["Bigger"]
        assert retry_stats.degraded == 0
        assert retry_stats.truncated == 0
        # Called once, with the larger retry budget (well above the old 1024).
        assert client.messages.create.call_count == 1
        assert client.messages.create.call_args.kwargs["max_tokens"] > 1024

    def test_retry_that_still_truncates_reports_it(self) -> None:
        client = _sequenced_client([_TRUNCATED_PAYLOAD], stop_reasons=["max_tokens"])
        raw = _make_raw("Entity-dense source text worth classifying.")
        entities, retry_stats = tier2_reclassify_larger_budget(
            raw, [], self._TYPES, [], ["internal"], client
        )
        assert entities == []
        assert retry_stats.truncated == 1
        assert retry_stats.degraded == 0


class TestClaudeCliParamDropGating:
    """athenaeum#574 (M15): the capability declaration surfaces the two claude-cli param
    drops — the max_tokens truncation retry (a no-op on the CLI) and the
    unreliable envelope stop_reason — instead of burning a byte-identical call
    or taking the wrong retry path on a spurious value."""

    _TYPES = ["person", "reference"]
    _CLI_CONFIG = {"llm": {"provider": "claude-cli"}}

    def test_truncation_retry_short_circuits_on_cli(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        client = _sequenced_client([_TRUNCATED_PAYLOAD], stop_reasons=["max_tokens"])
        raw = _make_raw("Entity-dense source text worth classifying.")
        with caplog.at_level("WARNING"):
            entities, retry_stats = tier2_reclassify_larger_budget(
                raw,
                [],
                self._TYPES,
                [],
                ["internal"],
                client,
                config=self._CLI_CONFIG,
            )
        # The retry's ONLY change is raising max_tokens, which claude-cli drops:
        # it would re-send a byte-identical request. Short-circuited, no call.
        assert client.messages.create.call_count == 0
        assert entities == []
        # A still-truncated stat signals NO recovery, so the caller preserves
        # the file and keeps the original truncation recorded.
        assert retry_stats.truncated == 1
        assert any(
            "does not honor max_tokens" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_truncation_retry_runs_on_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        payload = json.dumps(
            [
                {
                    "name": "Bigger",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "fit",
                }
            ]
        )
        client = _sequenced_client([payload], stop_reasons=["end_turn"])
        raw = _make_raw("Entity-dense source text worth classifying.")
        entities, retry_stats = tier2_reclassify_larger_budget(
            raw,
            [],
            self._TYPES,
            [],
            ["internal"],
            client,
            config={"llm": {"provider": "api"}},
        )
        # api honors max_tokens -> the retry proceeds with the larger budget.
        assert client.messages.create.call_count == 1
        assert (
            client.messages.create.call_args.kwargs["max_tokens"]
            == _TIER2_CLASSIFY_RETRY_MAX_TOKENS
        )
        assert [e.name for e in entities] == ["Bigger"]

    def test_cli_truncated_first_response_never_takes_max_tokens_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        # A first response truncated at the model cap (envelope stop_reason
        # "max_tokens") must NOT route to the bigger-budget retry on claude-cli:
        # the capability says stop_reason is unreliable, so the drop is classed
        # as a generic degrade and the futile 8192-budget retry never issues.
        client = _sequenced_client(
            [_TRUNCATED_PAYLOAD, _TRUNCATED_PAYLOAD],
            stop_reasons=["max_tokens", "max_tokens"],
        )
        raw = _make_raw("Entity-dense source text worth classifying.")
        stats = Tier2ParseStats()
        tier2_classify(
            raw,
            [],
            self._TYPES,
            [],
            ["internal"],
            client,
            config=self._CLI_CONFIG,
            stats=stats,
        )
        retry_budget_calls = [
            c
            for c in client.messages.create.call_args_list
            if c.kwargs.get("max_tokens") == _TIER2_CLASSIFY_RETRY_MAX_TOKENS
        ]
        assert retry_budget_calls == []
        # Classed as a generic degrade, not a truncation (stop_reason suppressed).
        assert stats.truncated == 0
        assert stats.degraded >= 1

    def test_api_truncated_first_response_does_take_max_tokens_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Control: on api, the same truncated first response DOES route to the
        # bigger-budget retry (stop_reason is trusted). Proves the gating is
        # provider-specific, not a blanket disable.
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        recovered = json.dumps(
            [
                {
                    "name": "Alpha",
                    "entity_type": "reference",
                    "access": "internal",
                    "tags": [],
                    "observations": "ok",
                }
            ]
        )
        client = _sequenced_client(
            [_TRUNCATED_PAYLOAD, recovered],
            stop_reasons=["max_tokens", "end_turn"],
        )
        raw = _make_raw("Entity-dense source text worth classifying.")
        stats = Tier2ParseStats()
        tier2_classify(
            raw,
            [],
            self._TYPES,
            [],
            ["internal"],
            client,
            config={"llm": {"provider": "api"}},
            stats=stats,
        )
        retry_budget_calls = [
            c
            for c in client.messages.create.call_args_list
            if c.kwargs.get("max_tokens") == _TIER2_CLASSIFY_RETRY_MAX_TOKENS
        ]
        assert len(retry_budget_calls) == 1


class TestPerStageMaxTokensThroughSeam:
    """athenaeum#575: each stage's max_tokens is resolved through the provider seam
    (env > yaml > default), moving the value out of a baked-in call-site
    literal. Today's values are preserved as the defaults."""

    _TYPES = ["person", "reference"]

    def _raw(self):
        return _make_raw("Some rich source text with entities.")

    def test_classify_default_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CLASSIFY_MAX_TOKENS", raising=False)
        params = tier2_request_params(self._raw(), [], self._TYPES, [], ["internal"])
        assert params["max_tokens"] == _TIER2_CLASSIFY_MAX_TOKENS

    def test_env_override_flows_through_builder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CLASSIFY_MAX_TOKENS", "1234")
        params = tier2_request_params(self._raw(), [], self._TYPES, [], ["internal"])
        assert params["max_tokens"] == 1234

    def test_yaml_override_flows_through_builder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CLASSIFY_MAX_TOKENS", raising=False)
        params = tier2_request_params(
            self._raw(),
            [],
            self._TYPES,
            [],
            ["internal"],
            config={"max_tokens": {"classify": 3210}},
        )
        assert params["max_tokens"] == 3210

    def test_merge_stage_defaults_unchanged(self) -> None:
        # Value preservation guard for the tier-3 merge budgets: the classify
        # budgets are untouched by athenaeum#578 (Haiku, disabled thinking); the
        # write-knob budgets (create/merge_patch/merge_full) were RAISED by
        # issue athenaeum#578's re-baseline ahead of the Sonnet-5 bump (athenaeum#580) — see
        # TestThinkingReBaseline below for the never-shrinks assertion.
        assert _MERGE_MAX_TOKENS == 12288
        assert _MERGE_PATCH_MAX_TOKENS == 6144
        assert _TIER3_CREATE_MAX_TOKENS == 6144
        assert _TIER2_CLASSIFY_MAX_TOKENS == 4096
        assert _TIER2_CLASSIFY_RETRY_MAX_TOKENS == 8192


# ---------------------------------------------------------------------------
# Entity-phase per-call wall-clock logging (issue athenaeum#800)
# ---------------------------------------------------------------------------


class TestEntityLLMCallTiming:
    """The entity run-summary's ``entity secs=... calls=...`` line is an
    aggregate over the WHOLE phase — it cannot tell "few slow calls" apart
    from "many fast calls" that sum to the same total. Every entity-phase LLM
    call site (tier2_classify, its two retries, tier3_create, tier3_merge,
    tier3_merge_full) routes through ``_timed_llm_call``, which times and logs
    each call individually under the stable :data:`ENTITY_LLM_CALL_MARKER`.
    """

    def _call_timing_lines(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [
            rec.message
            for rec in caplog.records
            if ENTITY_LLM_CALL_MARKER in rec.message
        ]

    def test_tier2_classify_logs_call_wall_clock(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        raw = _make_raw("Had coffee with Alice Zhang, she runs product at Acme.")
        client = _mock_client("[]")

        tier2_classify(raw, [], ["person"], [], ["internal"], client)

        lines = self._call_timing_lines(caplog)
        assert len(lines) == 1, "exactly one LLM call → exactly one timing line"
        assert f"desc=tier2_classify {raw.ref}" in lines[0]
        assert "elapsed=" in lines[0]

    def test_tier3_create_logs_call_wall_clock(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        action = EntityAction(
            kind="create",
            name="Test Entity",
            entity_type="person",
            tags=[],
            access="internal",
            existing_uid=None,
            observations="Some observation.",
        )
        client = _mock_client("# Test Entity\n\nContent.")

        tier3_create(action, "sessions/raw.md", client)

        lines = self._call_timing_lines(caplog)
        assert len(lines) == 1
        assert "desc=tier3_create sessions/raw.md" in lines[0]
        assert "elapsed=" in lines[0]

    def test_two_classify_calls_produce_two_distinct_timing_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two files → two per-call lines — the per-call granularity the
        aggregate ``entity secs``/``calls`` figure cannot provide."""
        caplog.set_level(logging.INFO, logger="athenaeum")
        client = _mock_client("[]")

        tier2_classify(
            _make_raw("First file."), [], ["person"], [], ["internal"], client
        )
        tier2_classify(
            _make_raw("Second file."), [], ["person"], [], ["internal"], client
        )

        lines = self._call_timing_lines(caplog)
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# _timed_llm_call attempt-counting (issue athenaeum#1177)
# ---------------------------------------------------------------------------


class TestTimedLLMCallRecordsAttempts:
    """``_timed_llm_call`` is the single choke point every tier2/tier3
    entity-phase LLM call site passes through (see
    ``TestEntityLLMCallTiming`` above). Before athenaeum#1177, a call site's
    ``usage.api_calls`` only ever incremented on a SUCCESSFUL response (via
    ``_record_usage``, called after ``_timed_llm_call`` returns) — a
    persistently-failing call (retries exhausted, or a non-transient error
    ``with_retry`` never retries at all) left ``api_calls`` at 0, making an
    all-failing run indistinguishable from a genuinely idle one. These
    tests cover the fix directly at its source, independent of the full
    end-to-end regression test in ``tests/test_librarian_zero_yield.py``.
    """

    def test_successful_call_records_one_attempt(self) -> None:
        usage = TokenUsage()
        result = _timed_llm_call(lambda: "ok", "desc", usage=usage)
        assert result == "ok"
        assert usage.attempted_calls == 1

    def test_failing_call_still_records_the_attempt(self) -> None:
        """The load-bearing case: the attempt is recorded BEFORE the call
        runs, so a raised exception does not erase it."""
        usage = TokenUsage()

        def _boom() -> None:
            raise RuntimeError("simulated non-transient failure")

        with pytest.raises(RuntimeError):
            _timed_llm_call(_boom, "desc", usage=usage)
        assert usage.attempted_calls == 1

    def test_multiple_failing_calls_each_record_an_attempt(self) -> None:
        """The exact shape an all-failing run produces: N attempts, N
        failures, ``attempted_calls == N`` even though nothing succeeded."""
        usage = TokenUsage()

        def _boom() -> None:
            raise RuntimeError("simulated non-transient failure")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                _timed_llm_call(_boom, "desc", usage=usage)

        assert usage.attempted_calls == 3
        assert usage.api_calls == 0
        assert usage.succeeded_calls == 0

    def test_no_usage_given_is_a_pure_no_op_for_attempt_counting(self) -> None:
        """``usage=None`` (the default) must not raise -- callers that do
        not track usage (e.g. some test/CLI paths) are unaffected."""
        result = _timed_llm_call(lambda: "ok", "desc")
        assert result == "ok"

    def test_tier2_classify_records_an_attempt_via_the_real_call_site(
        self,
    ) -> None:
        """End-to-end through the REAL call site (not calling
        ``_timed_llm_call`` directly) -- proves the wiring at
        ``tier2_classify``'s call site actually threads ``usage`` through."""
        usage = TokenUsage()
        raw = _make_raw("Had coffee with Alice Zhang, she runs product at Acme.")
        client = _mock_client("[]")

        tier2_classify(raw, [], ["person"], [], ["internal"], client, usage=usage)

        assert usage.attempted_calls == 1
        assert usage.api_calls == 1  # this call succeeded too
