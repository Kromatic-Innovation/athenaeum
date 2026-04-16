"""Tests for athenaeum.models -- frontmatter, slugify, UID, WikiEntity, EntityIndex, schema."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from athenaeum.models import (
    EntityAction,
    EntityIndex,
    WikiEntity,
    generate_uid,
    load_schema_list,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Create a minimal _schema directory with markdown tables."""
    schema = tmp_path / "_schema"
    schema.mkdir()

    (schema / "types.md").write_text(textwrap.dedent("""\
        # Entity Types

        | Type | Description | Example |
        |------|------------|---------|
        | person | A human being | Alice |
        | company | An organization | Acme Corp |
        | concept | A framework or idea | Lean startup |
        | tool | Software or service | Code editor |
    """))

    (schema / "tags.md").write_text(textwrap.dedent("""\
        # Tags

        ## Status
        - `active`
        - `archived`

        ## Domain
        - `client`
        - `internal`

        | Tag | Usage |
        |-----|-------|
        | fintech | Industry vertical |
        | devops | Technical domain |
    """))

    (schema / "access-levels.md").write_text(textwrap.dedent("""\
        # Access Levels

        | Level | Who can see | Example |
        |-------|------------|---------|
        | open | Anyone | Public docs |
        | internal | Team members | Workflow notes |
        | confidential | Specific context | Client details |
        | personal | Restricted | Home address |
    """))

    return schema


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self) -> None:
        text = "---\nname: Test\ntype: person\n---\n\nBody content here."
        meta, body = parse_frontmatter(text)
        assert meta == {"name": "Test", "type": "person"}
        assert body.strip() == "Body content here."

    def test_no_frontmatter(self) -> None:
        text = "Just plain text without frontmatter."
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_empty_frontmatter(self) -> None:
        text = "---\n\n---\n\nBody."
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body.strip() == "Body."

    def test_complex_frontmatter(self) -> None:
        text = textwrap.dedent("""\
            ---
            uid: abc12345
            type: company
            name: Acme Corp
            aliases:
              - AC
              - AcmeCo
            tags:
              - active
              - client
            ---

            # Acme Corp
        """)
        meta, body = parse_frontmatter(text)
        assert meta["uid"] == "abc12345"
        assert meta["aliases"] == ["AC", "AcmeCo"]
        assert "# Acme Corp" in body

    def test_crlf_frontmatter(self) -> None:
        text = "---\r\nname: Test\r\ntype: person\r\n---\r\n\r\nBody content."
        meta, body = parse_frontmatter(text)
        assert meta == {"name": "Test", "type": "person"}
        assert "Body content." in body

    def test_invalid_yaml(self) -> None:
        text = "---\n: bad: yaml: [unclosed\n---\n\nBody."
        meta, body = parse_frontmatter(text)
        # Should fall back gracefully
        assert meta == {}


# ---------------------------------------------------------------------------
# render_frontmatter
# ---------------------------------------------------------------------------


class TestRenderFrontmatter:
    def test_basic_render(self) -> None:
        meta = {"uid": "abc123", "type": "person", "name": "Alice"}
        result = render_frontmatter(meta)
        assert result.startswith("---\n")
        assert result.endswith("---\n")
        assert "uid: abc123" in result
        assert "name: Alice" in result

    def test_roundtrip(self) -> None:
        original = {"uid": "x1y2z3", "type": "tool", "name": "Code Editor"}
        rendered = render_frontmatter(original)
        parsed, _ = parse_frontmatter(rendered + "\nBody.")
        assert parsed["uid"] == original["uid"]
        assert parsed["type"] == original["type"]
        assert parsed["name"] == original["name"]


# ---------------------------------------------------------------------------
# slugify / generate_uid
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Acme Corp") == "acme-corp"

    def test_special_chars(self) -> None:
        assert slugify("Alice's Cool Tool!") == "alice-s-cool-tool"

    def test_leading_trailing(self) -> None:
        assert slugify("  --hello--  ") == "hello"

    def test_long_name(self) -> None:
        long_name = "A" * 100
        assert len(slugify(long_name)) <= 60

    def test_empty(self) -> None:
        assert slugify("") == ""


class TestGenerateUid:
    def test_length(self) -> None:
        uid = generate_uid()
        assert len(uid) == 8

    def test_hex_chars(self) -> None:
        uid = generate_uid()
        assert all(c in "0123456789abcdef" for c in uid)

    def test_unique(self) -> None:
        uids = {generate_uid() for _ in range(100)}
        assert len(uids) == 100  # no collisions in 100 tries


# ---------------------------------------------------------------------------
# WikiEntity
# ---------------------------------------------------------------------------


class TestWikiEntity:
    def test_filename(self) -> None:
        entity = WikiEntity(uid="abcd1234", type="person", name="Alice Smith")
        assert entity.filename == "abcd1234-alice-smith.md"

    def test_render_minimal(self) -> None:
        entity = WikiEntity(
            uid="abc123",
            type="person",
            name="Alice",
            body="# Alice\n\nA person.",
        )
        rendered = entity.render()
        assert "uid: abc123" in rendered
        assert "type: person" in rendered
        assert "name: Alice" in rendered
        assert "# Alice" in rendered
        assert "aliases" not in rendered  # empty, should be omitted

    def test_render_full(self) -> None:
        entity = WikiEntity(
            uid="abc123",
            type="company",
            name="Acme",
            aliases=["Acme Corp"],
            access="confidential",
            tags=["client"],
            created="2024-01-01",
            updated="2024-04-06",
            body="# Acme\n\nA company.",
        )
        rendered = entity.render()
        assert "aliases:" in rendered
        assert "- Acme Corp" in rendered
        assert "access: confidential" in rendered
        assert "tags:" in rendered


# ---------------------------------------------------------------------------
# EntityIndex
# ---------------------------------------------------------------------------


class TestEntityIndex:
    def test_loads_pages(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        # Should have loaded 3 pages (skipping _index.md and MEMORY.md)
        assert len(index._by_name) >= 3  # names + aliases

    def test_lookup_by_name(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        result = index.lookup("Acme Corp")
        assert result is not None
        uid, path = result
        assert uid == "a1b2c3d4"

    def test_lookup_by_alias(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        result = index.lookup("Acme")
        assert result is not None
        uid, _ = result
        assert uid == "a1b2c3d4"

    def test_lookup_case_insensitive(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        result = index.lookup("acme corp")
        assert result is not None

    def test_lookup_old_format(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        result = index.lookup("Auth tokens must use system keychain")
        assert result is not None
        uid_or_name, _ = result
        # Old format has no uid, so it returns the name
        assert uid_or_name == "Auth tokens must use system keychain"

    def test_lookup_missing(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        assert index.lookup("Nonexistent Entity") is None

    def test_has_entity_format(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        entity_page = wiki_dir / "a1b2c3d4-acme-corp.md"
        old_page = wiki_dir / "feedback_keychain_auth.md"
        assert index.has_entity_format(entity_page) is True
        assert index.has_entity_format(old_page) is False

    def test_get_by_uid(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        path = index.get_by_uid("a1b2c3d4")
        assert path is not None
        assert path.name == "a1b2c3d4-acme-corp.md"

    def test_get_by_uid_missing(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        assert index.get_by_uid("nonexistent") is None

    def test_get_by_uid_after_register(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        entity = WikiEntity(uid="newuid01", type="tool", name="New Tool")
        index.register(entity)
        path = index.get_by_uid("newuid01")
        assert path is not None
        assert path.name == entity.filename

    def test_has_entity_format_after_register(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        entity = WikiEntity(uid="reg12345", type="tool", name="New Tool")
        index.register(entity)
        assert index.has_entity_format(wiki_dir / entity.filename) is True

    def test_register(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        entity = WikiEntity(
            uid="new12345",
            type="person",
            name="New Person",
            aliases=["NP"],
        )
        index.register(entity)
        assert index.lookup("New Person") is not None
        assert index.lookup("NP") is not None

    def test_skips_underscore_and_memory(self, wiki_dir: Path) -> None:
        index = EntityIndex(wiki_dir)
        # _index.md and MEMORY.md should not be indexed
        assert index.lookup("Index") is None
        assert index.lookup("Memory Index") is None


# ---------------------------------------------------------------------------
# load_schema_list
# ---------------------------------------------------------------------------


class TestLoadSchemaList:
    def test_load_types(self, schema_dir: Path) -> None:
        types = load_schema_list(schema_dir, "types.md")
        assert "person" in types
        assert "company" in types
        assert "concept" in types
        assert "tool" in types

    def test_load_access(self, schema_dir: Path) -> None:
        levels = load_schema_list(schema_dir, "access-levels.md")
        assert "open" in levels
        assert "internal" in levels
        assert "confidential" in levels
        assert "personal" in levels

    def test_load_tags(self, schema_dir: Path) -> None:
        tags = load_schema_list(schema_dir, "tags.md")
        assert "fintech" in tags
        assert "devops" in tags

    def test_missing_file(self, tmp_path: Path) -> None:
        result = load_schema_list(tmp_path, "nonexistent.md")
        assert result == []


# ---------------------------------------------------------------------------
# EntityAction type annotation
# ---------------------------------------------------------------------------


class TestEntityAction:
    def test_kind_literal_annotation(self) -> None:
        annotation = EntityAction.__dataclass_fields__["kind"].type
        assert "Literal" in annotation
