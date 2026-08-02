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
)
from athenaeum.tiers import (
    _MERGE_MAX_TOKENS,
    _MERGE_PATCH_MAX_TOKENS,
    _TIER2_CLASSIFY_MAX_TOKENS,
    _TIER2_CLASSIFY_RETRY_MAX_TOKENS,
    _TIER3_CREATE_MAX_TOKENS,
    MERGE_FALLBACK_LOG_PREFIX,
    MERGE_PARSE_FAIL_AMBIGUOUS,
    MERGE_PARSE_FAIL_NO_JSON,
    MERGE_PARSE_FAIL_SHAPE,
    TIER2_DEGRADED_MARKER,
    TIER2_TRUNCATED_MARKER,
    MergeOpsError,
    Tier2ParseStats,
    apply_merge_ops,
    parse_merge_ops_response,
    parse_tier2_entities,
    tier1_programmatic_match,
    tier2_classify,
    tier2_reclassify_larger_budget,
    tier2_request_params,
    tier3_create,
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
            action, "# Acme Corp\n\nFintech startup, Series B.", "sessions/raw.md", client
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
            action, "# Acme Corp\n\nFintech startup, Series B.", "sessions/raw.md", client
        )
        assert body is not None
        assert esc is None


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
            apply_merge_ops(
                "aa aa", [{"op": "replace", "anchor": "aa", "text": "x"}]
            )

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
            apply_merge_ops(
                "body", [{"op": "delete", "anchor": "body", "text": ""}]
            )

    def test_missing_anchor_field_raises(self) -> None:
        with pytest.raises(MergeOpsError):
            apply_merge_ops("body", [{"op": "replace", "text": "x"}])

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

    # --- athenaeum#490 (slice A): each fallback names the page + a distinct cause ------

    def _fallback_warnings(
        self, caplog: pytest.LogCaptureFixture
    ) -> list[str]:
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
            '{"ops": [', self._action(), "sess-ref", "existing",
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
            json.dumps(
                {"operations": [{"op": "append_section", "text": "New.[^2]"}]}
            ),
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
                            {"op": "append_section", "text": "New partnership announced."}
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
        params = tier2_request_params(raw, [], ["person", "reference"], [], ["internal"])
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
            [{"name": "Fits", "entity_type": "reference", "access": "internal",
              "tags": [], "observations": "ok"}]
        )
        stats = Tier2ParseStats()
        results = parse_tier2_entities(
            payload, "sessions/x.md", self._TYPES, [], ["internal"],
            stats=stats, stop_reason="max_tokens",
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
            [{"name": "Recovered", "entity_type": "reference",
              "access": "internal", "tags": [], "observations": "second try"}]
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
            [{"name": "Bigger", "entity_type": "reference",
              "access": "internal", "tags": [], "observations": "fit now"}]
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
                raw, [], self._TYPES, [], ["internal"], client,
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
            [{"name": "Bigger", "entity_type": "reference",
              "access": "internal", "tags": [], "observations": "fit"}]
        )
        client = _sequenced_client([payload], stop_reasons=["end_turn"])
        raw = _make_raw("Entity-dense source text worth classifying.")
        entities, retry_stats = tier2_reclassify_larger_budget(
            raw, [], self._TYPES, [], ["internal"], client,
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
            raw, [], self._TYPES, [], ["internal"], client,
            config=self._CLI_CONFIG, stats=stats,
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
            [{"name": "Alpha", "entity_type": "reference",
              "access": "internal", "tags": [], "observations": "ok"}]
        )
        client = _sequenced_client(
            [_TRUNCATED_PAYLOAD, recovered],
            stop_reasons=["max_tokens", "end_turn"],
        )
        raw = _make_raw("Entity-dense source text worth classifying.")
        stats = Tier2ParseStats()
        tier2_classify(
            raw, [], self._TYPES, [], ["internal"], client,
            config={"llm": {"provider": "api"}}, stats=stats,
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

    def test_classify_default_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            self._raw(), [], self._TYPES, [], ["internal"],
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
