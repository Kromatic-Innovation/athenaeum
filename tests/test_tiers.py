"""Tests for athenaeum.tiers — tier1 matching, tier2 classification (mocked LLM),
tier3 create/merge/write (mocked LLM), tier4 escalation."""

from __future__ import annotations

import json
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
    tier1_programmatic_match,
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

    def test_classify_includes_observation_filter(
        self, wiki_dir: Path,
    ) -> None:
        """Issue #17: observation-filter.md should be injected into classify prompt."""
        from athenaeum.tiers import tier2_classify

        schema_dir = wiki_dir / "_schema"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "observation-filter.md").write_text(
            "# Observation Filter\n\n## Always Capture\n- People\n"
        )

        raw = _make_raw("Some content about people.")
        client = _mock_client("[]")

        tier2_classify(
            raw, [], ["person"], [], ["internal"], client,
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
            input_tokens=150, output_tokens=20,
        )
        client.messages.create.return_value = mock_response

        usage = TokenUsage()
        tier2_classify(
            raw, [], ["person"], [], ["internal"], client,
            usage=usage,
        )
        assert usage.input_tokens == 150
        assert usage.output_tokens == 20
        assert usage.api_calls == 1

    def test_extracts_new_entity(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Had coffee with Alice Zhang, she runs product at Acme.")
        response_json = json.dumps([{
            "name": "Alice Zhang",
            "entity_type": "person",
            "tags": ["active", "client"],
            "access": "internal",
            "observations": "Runs product at Acme.",
        }])
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
        response_json = json.dumps([{
            "name": "Widget",
            "entity_type": "gadget",  # not in valid_types
            "tags": [],
            "access": "internal",
            "observations": "A widget thing.",
        }])
        client = _mock_client(response_json)

        result = tier2_classify(
            raw, [], ["person", "company", "reference"], [], ["internal"], client,
        )
        assert len(result) == 1
        assert result[0].entity_type == "reference"

    def test_invalid_access_falls_back_to_internal(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Some content.")
        response_json = json.dumps([{
            "name": "Test Entity",
            "entity_type": "person",
            "tags": [],
            "access": "top-secret",  # not in valid_access
        }])
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
        response_json = json.dumps([{
            "name": "Test",
            "entity_type": "person",
            "tags": ["active", "bogus-tag", "client"],
            "access": "internal",
        }])
        client = _mock_client(response_json)

        result = tier2_classify(
            raw, [], ["person"], ["active", "client"], ["internal"], client,
        )
        assert result[0].tags == ["active", "client"]

    def test_handles_json_wrapped_in_code_fence(self) -> None:
        from athenaeum.tiers import tier2_classify

        raw = _make_raw("Met Bob at the conference.")
        fenced = (
            '```json\n'
            '[{"name": "Bob", "entity_type": "person", "tags": [], "access": "internal"}]\n'
            '```'
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
        response_json = json.dumps([
            {"entity_type": "person", "tags": [], "access": "internal"},  # no name
            {"name": "Valid", "entity_type": "person", "tags": [], "access": "internal"},
        ])
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
        self, wiki_dir: Path,
    ) -> None:
        """Issue #17: _entity-template.md should be fed to Tier 3 create."""
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
            action, "ref.md", client, wiki_root=wiki_dir,
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
        create_response.content = [MagicMock(text="# Alice Zhang\n\nProduct lead at Acme.")]
        merge_response = MagicMock()
        merge_response.content = [
            MagicMock(
                text="# Acme Corp\n\nFintech startup, Series B.\n\nNew partnership announced."
            )
        ]

        client = MagicMock()
        client.messages.create.side_effect = [create_response, merge_response]

        new_entities, updated_uids, escalations = tier3_write(
            raw, actions, index, wiki_dir, client,
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
        assert acme_after == acme_before, "update was written despite subsequent failure"

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
            raw, actions, index, wiki_dir, client,
        )

        assert updated_uids == ["a1b2c3d4"]
        acme_content = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert "Updated content" in acme_content


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
