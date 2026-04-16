"""Tests for athenaeum.tiers — tier1 matching, tier2 classification (mocked LLM),
tier3 create/merge/write (mocked LLM), tier4 escalation."""

from __future__ import annotations

import json
import textwrap
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


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a minimal wiki directory with sample entity pages."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    (wiki / "feedback_keychain_auth.md").write_text(textwrap.dedent("""\
        ---
        name: Auth tokens must use system keychain
        description: Never store auth tokens as plaintext env vars.
        type: feedback
        ---

        Always use the system keychain for storing auth tokens.
    """))

    (wiki / "a1b2c3d4-acme-corp.md").write_text(textwrap.dedent("""\
        ---
        uid: a1b2c3d4
        type: company
        name: Acme Corp
        aliases:
          - Acme
          - Acme Corporation
        access: confidential
        tags:
          - client
          - fintech
        created: '2024-03-15'
        updated: '2024-04-06'
        ---

        # Acme Corp

        Fintech startup, Series B.
    """))

    (wiki / "project_knowledge_architecture.md").write_text(textwrap.dedent("""\
        ---
        name: Knowledge architecture project
        description: Unified knowledge system.
        type: project
        ---

        The knowledge architecture unifies fragmented memory scopes.
    """))

    (wiki / "_index.md").write_text("# Index\n")
    (wiki / "MEMORY.md").write_text("# Memory Index\n")

    return wiki


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
        matched, _ = tier1_programmatic_match(raw, index)
        names = [name for name, _, _ in matched]
        assert any("acme" in n for n in names)

    def test_matches_alias(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Got an email from Acme Corporation today.")
        matched, _ = tier1_programmatic_match(raw, index)
        assert len(matched) > 0

    def test_no_match(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        raw = _make_raw("Nothing relevant here about any known entities.")
        matched, _ = tier1_programmatic_match(raw, index)
        assert len(matched) == 0

    def test_word_boundary(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        # "acme" appears as substring in "pharmacme" -- should NOT match
        raw = _make_raw("The pharmacme product line is interesting.")
        matched, _ = tier1_programmatic_match(raw, index)
        acme_matches = [n for n, _, _ in matched if "acme" in n]
        assert len(acme_matches) == 0

    def test_short_names_skipped(self, wiki_dir: Path) -> None:
        """Names shorter than 3 chars should be skipped to avoid false positives."""
        index = EntityIndex(wiki_dir)
        # Register a short-name entity
        index._by_name["ai"] = ("short-uid", wiki_dir / "short.md")
        raw = _make_raw("AI is transforming the industry.")
        matched, _ = tier1_programmatic_match(raw, index)
        ai_matches = [n for n, _, _ in matched if n == "ai"]
        assert len(ai_matches) == 0


# ---------------------------------------------------------------------------
# Tier 2 — Classification (mocked LLM)
# ---------------------------------------------------------------------------


class TestTier2:
    """Mock-based tests for classification tier."""

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
        assert "fintech" in esc.description.lower() or "pivot" in esc.description.lower()
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

        new_entities, escalations = tier3_write(raw, actions, index, wiki_dir, client)

        assert len(new_entities) == 1
        assert new_entities[0].name == "Alice Zhang"
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
