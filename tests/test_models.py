"""Tests for athenaeum.models -- frontmatter, slugify, UID, WikiEntity, EntityIndex, schema."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.models import (
    AI_ATTRIBUTED_SOURCE_TYPES,
    SOURCE_TYPES,
    AutoMemoryFile,
    EntityAction,
    EntityIndex,
    WikiEntity,
    asserter_identity_key,
    coerce_source_type,
    compare_asserters,
    generate_uid,
    is_inactive_memory,
    load_schema_list,
    parse_asserter,
    parse_deprecated,
    parse_frontmatter,
    parse_model,
    parse_on_behalf_of,
    parse_superseded_by,
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

    (schema / "types.md").write_text(
        textwrap.dedent(
            """\
        # Entity Types

        | Type | Description | Example |
        |------|------------|---------|
        | person | A human being | Alice |
        | company | An organization | Acme Corp |
        | concept | A framework or idea | Lean startup |
        | tool | Software or service | Code editor |
    """
        )
    )

    (schema / "tags.md").write_text(
        textwrap.dedent(
            """\
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
    """
        )
    )

    (schema / "access-levels.md").write_text(
        textwrap.dedent(
            """\
        # Access Levels

        | Level | Who can see | Example |
        |-------|------------|---------|
        | open | Anyone | Public docs |
        | internal | Team members | Workflow notes |
        | confidential | Specific context | Client details |
        | personal | Restricted | Home address |
    """
        )
    )

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
        text = textwrap.dedent(
            """\
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
        """
        )
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

    def test_int_uid_coerced_to_str_at_boundary(self) -> None:
        """PyYAML loads bare all-decimal uids as int (e.g. ``19052``).
        ``parse_frontmatter`` must stringify uid/type/name at the YAML
        boundary so downstream schema validation, index lookup, and
        filename rendering can treat them as strings unconditionally."""
        text = "---\nuid: 19052\ntype: person\nname: 42\n---\n\nBody."
        meta, _ = parse_frontmatter(text)
        assert meta["uid"] == "19052"
        assert isinstance(meta["uid"], str)
        assert meta["name"] == "42"
        assert isinstance(meta["name"], str)
        assert meta["type"] == "person"


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

    def test_render_frontmatter_preserves_key_order(self) -> None:
        """tier0 byte-for-byte round-trip requires sort_keys=False.

        Quine regression: alphabetizing keys would break tier0_passthrough's
        contract that custom frontmatter round-trips byte-for-byte.
        """
        # Non-alpha order — would re-sort to field_sources, name, source,
        # type, uid if sort_keys=True crept in.
        meta = {
            "type": "person",
            "name": "Zed",
            "uid": "12345",
            "source": "api:apollo",
            "field_sources": {"emails": "api:apollo"},
        }
        rendered = render_frontmatter(meta)
        # Strip leading/trailing fences; remaining lines are key: ... rows
        # at top level (the field_sources nested block follows its key).
        lines = rendered.splitlines()
        top_keys = []
        for line in lines:
            if line in ("---",):
                continue
            if line.startswith(" ") or line.startswith("\t"):
                continue
            if ":" in line:
                top_keys.append(line.split(":", 1)[0])
        assert top_keys == [
            "type",
            "name",
            "uid",
            "source",
            "field_sources",
        ], f"Key order not preserved; got {top_keys}"


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


class TestTokenUsage:
    def test_add_accumulates(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(100, 50)
        usage.add(200, 75)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 125
        assert usage.api_calls == 2
        assert usage.total_tokens == 425

    def test_estimated_cost(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000)
        # $1.50/M input + $7.50/M output = $9.00
        assert abs(usage.estimated_cost_usd - 9.0) < 0.01

    def test_empty_usage(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        assert usage.api_calls == 0
        assert usage.total_tokens == 0
        assert usage.estimated_cost_usd == 0.0

    def test_add_tokens_accumulates_without_counting_a_call(self) -> None:
        """#239: token-only accumulation for call sites that count attempts
        separately (merge.py's detector/resolver loop)."""
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add_tokens(100, 50, 10, 20)
        usage.add_tokens(200, 75)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 125
        assert usage.cache_creation_input_tokens == 10
        assert usage.cache_read_input_tokens == 20
        assert usage.api_calls == 0

    def test_estimated_cost_includes_cache_terms(self) -> None:
        """#239: input_tokens excludes cached tokens, so the estimate must
        fold in cache writes at 1.25x and cache reads at 0.1x input rate."""
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(
            1_000_000,
            0,
            cache_creation_input_tokens=1_000_000,
            cache_read_input_tokens=1_000_000,
        )
        # $1.50 input + $1.875 cache-write (1.25x) + $0.15 cache-read (0.1x)
        assert abs(usage.estimated_cost_usd - 3.525) < 0.001

    def test_estimated_cost_cache_read_only(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(0, 0, cache_read_input_tokens=10_000_000)
        # 10M cache-read tokens at 0.1 * $1.50/M = $1.50
        assert abs(usage.estimated_cost_usd - 1.50) < 0.001


class TestPerModelCostAttribution:
    """#247: per-model cost attribution in ``estimated_cost_usd``.

    Tokens tagged with a known model price at that model's rate; untagged
    or unknown-model tokens fall back to the blended rate. The existing
    cache and batch multipliers compose per model.
    """

    def test_opus_tagged_traffic_uses_opus_rates(self) -> None:
        """RED on blended-only code: resolver traffic tagged
        ``claude-opus-4-7`` must price at Opus rates (~3.3x blended)."""
        from athenaeum.models import TokenUsage

        tagged = TokenUsage()
        tagged.add(1_000_000, 1_000_000, model="claude-opus-4-7")
        # Opus: $5/M input + $25/M output = $30.00
        assert abs(tagged.estimated_cost_usd - 30.0) < 0.01

        blended = TokenUsage()
        blended.add(1_000_000, 1_000_000)
        # Blended: $1.50 + $7.50 = $9.00; Opus is ~3.33x.
        assert (
            abs(tagged.estimated_cost_usd / blended.estimated_cost_usd - 3.333) < 0.01
        )

    def test_sonnet_and_haiku_dated_ids_resolve_by_prefix(self) -> None:
        from athenaeum.models import TokenUsage

        sonnet = TokenUsage()
        sonnet.add(1_000_000, 1_000_000, model="claude-sonnet-4-6")
        # Sonnet: $3/M input + $15/M output = $18.00
        assert abs(sonnet.estimated_cost_usd - 18.0) < 0.01

        haiku = TokenUsage()
        haiku.add(1_000_000, 1_000_000, model="claude-haiku-4-5-20251001")
        # Haiku: $1/M input + $5/M output = $6.00
        assert abs(haiku.estimated_cost_usd - 6.0) < 0.01

    def test_unknown_model_falls_back_to_blended(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000, model="some-proxy-model-x")
        # Unknown id => blended $9.00
        assert abs(usage.estimated_cost_usd - 9.0) < 0.01

    def test_untagged_falls_back_to_blended(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000)  # no model kwarg
        assert abs(usage.estimated_cost_usd - 9.0) < 0.01

    def test_mixed_tagged_and_untagged(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(1_000_000, 1_000_000, model="claude-opus-4-7")  # $30
        usage.add(1_000_000, 1_000_000)  # blended $9
        assert abs(usage.estimated_cost_usd - 39.0) < 0.01

    def test_cache_multipliers_compose_with_per_model_rates(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add(
            1_000_000,
            0,
            cache_creation_input_tokens=1_000_000,
            cache_read_input_tokens=1_000_000,
            model="claude-opus-4-7",
        )
        # Opus input $5/M: $5 input + $6.25 cache-write (1.25x)
        # + $0.50 cache-read (0.1x) = $11.75
        assert abs(usage.estimated_cost_usd - 11.75) < 0.001

    def test_batch_discount_composes_with_per_model_rates(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add_batch_tokens(1_000_000, 1_000_000, model="claude-opus-4-7")
        # Opus $30 at 50% batch discount = $15.00
        assert abs(usage.estimated_cost_usd - 15.0) < 0.01

    def test_add_tokens_threads_model(self) -> None:
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        usage.add_tokens(1_000_000, 1_000_000, model="claude-opus-4-7")
        assert usage.api_calls == 0
        assert abs(usage.estimated_cost_usd - 30.0) < 0.01

    def test_default_constructors_stay_valid(self) -> None:
        """Additive change: existing positional constructions unchanged."""
        from athenaeum.models import TokenUsage

        usage = TokenUsage(input_tokens=10, output_tokens=5, api_calls=1)
        assert usage.input_tokens == 10
        assert usage.estimated_cost_usd > 0


class TestCacheUsageCounts:
    """Direct unit coverage for ``cache_usage_counts`` (#239 nit b)."""

    def test_full_usage_extracted(self) -> None:
        from athenaeum.models import cache_usage_counts

        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=2300,
                cache_read_input_tokens=4600,
            )
        )
        assert cache_usage_counts(response) == (10, 5, 2300, 4600)

    def test_missing_usage_attribute_returns_zeros(self) -> None:
        from athenaeum.models import cache_usage_counts

        assert cache_usage_counts(object()) == (0, 0, 0, 0)

    def test_missing_cache_fields_default_to_zero(self) -> None:
        from athenaeum.models import cache_usage_counts

        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=7, output_tokens=3)
        )
        assert cache_usage_counts(response) == (7, 3, 0, 0)

    def test_bool_fields_coerce_to_zero(self) -> None:
        """bool is an int subclass — ``True`` must not count as 1 token."""
        from athenaeum.models import cache_usage_counts

        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=True,
                output_tokens=5,
                cache_creation_input_tokens=True,
                cache_read_input_tokens=False,
            )
        )
        assert cache_usage_counts(response) == (0, 5, 0, 0)

    def test_non_int_fields_coerce_to_zero(self) -> None:
        from athenaeum.models import cache_usage_counts

        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens="12", output_tokens=None)
        )
        assert cache_usage_counts(response) == (0, 0, 0, 0)


class TestEntityAction:
    def test_kind_literal_annotation(self) -> None:
        annotation = EntityAction.__dataclass_fields__["kind"].type
        assert "Literal" in annotation


# ---------------------------------------------------------------------------
# Issue #191: inactive-member predicate + parse helpers
# ---------------------------------------------------------------------------


class TestParseSupersededBy:
    def test_missing_returns_empty(self) -> None:
        assert parse_superseded_by(None) == ""
        assert parse_superseded_by({}) == ""
        assert parse_superseded_by({"name": "x"}) == ""

    def test_value_stripped(self) -> None:
        assert parse_superseded_by({"superseded_by": "  Winner A  "}) == "Winner A"

    def test_non_string_coerced(self) -> None:
        assert parse_superseded_by({"superseded_by": 42}) == "42"

    def test_none_value(self) -> None:
        assert parse_superseded_by({"superseded_by": None}) == ""


class TestParseDeprecated:
    def test_missing_is_false(self) -> None:
        assert parse_deprecated(None) is False
        assert parse_deprecated({}) is False

    def test_bool_true(self) -> None:
        assert parse_deprecated({"deprecated": True}) is True

    def test_bool_false(self) -> None:
        assert parse_deprecated({"deprecated": False}) is False

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", " Yes "])
    def test_string_truthy_variants(self, val: str) -> None:
        assert parse_deprecated({"deprecated": val}) is True

    @pytest.mark.parametrize("val", ["false", "0", "no", ""])
    def test_string_falsey_variants(self, val: str) -> None:
        assert parse_deprecated({"deprecated": val}) is False


class TestIsInactiveMemory:
    def test_empty(self) -> None:
        assert is_inactive_memory(None) is False
        assert is_inactive_memory({}) is False

    def test_superseded_by(self) -> None:
        assert is_inactive_memory({"superseded_by": "Winner"}) is True

    def test_deprecated_flag(self) -> None:
        assert is_inactive_memory({"deprecated": True}) is True
        assert is_inactive_memory({"deprecated": "yes"}) is True

    def test_active_keys_only(self) -> None:
        assert is_inactive_memory({"name": "x", "deprecated": False}) is False


class TestAutoMemoryFileInactive:
    def test_defaults_active(self, tmp_path: Path) -> None:
        am = AutoMemoryFile(
            path=tmp_path / "m.md", origin_scope="s", memory_type="feedback"
        )
        assert am.superseded_by == ""
        assert am.deprecated is False
        assert am.is_inactive() is False

    def test_superseded_by_makes_inactive(self, tmp_path: Path) -> None:
        am = AutoMemoryFile(
            path=tmp_path / "m.md",
            origin_scope="s",
            memory_type="feedback",
            superseded_by="Winner",
        )
        assert am.is_inactive() is True

    def test_deprecated_makes_inactive(self, tmp_path: Path) -> None:
        am = AutoMemoryFile(
            path=tmp_path / "m.md",
            origin_scope="s",
            memory_type="feedback",
            deprecated=True,
        )
        assert am.is_inactive() is True


# ---------------------------------------------------------------------------
# Issue #326 — channel split, model recording, IdP-compatible asserter identity
# ---------------------------------------------------------------------------
#
# Locks `docs/provenance-shape.md` §10 — the two new source_type values and
# the new claim-level frontmatter fields (`model`, `on_behalf_of`,
# `asserter`) round-trip through AutoMemoryFile / WikiEntity, and the
# asserter identity key is derived from (iss, sub) — with the Microsoft
# Entra pairwise-sub trap handled via (iss, "entra", tid, oid). ``email``
# is a display snapshot; changing it never orphans the identity.


class TestChannelSplitSourceTypes:
    def test_new_channel_values_are_legal(self) -> None:
        # #326: two new values on the coarse source_type vocabulary.
        assert "agent-observed" in SOURCE_TYPES
        assert "model-prior" in SOURCE_TYPES

    def test_existing_source_types_preserved(self) -> None:
        # Locked #260 values must survive the extension.
        assert "user-stated" in SOURCE_TYPES
        assert "inferred" in SOURCE_TYPES
        assert "external" in SOURCE_TYPES
        assert "document" in SOURCE_TYPES

    def test_coerce_source_type_accepts_new_values(self) -> None:
        assert coerce_source_type("agent-observed") == "agent-observed"
        assert coerce_source_type("model-prior") == "model-prior"

    def test_ai_attributed_set_lists_the_three_ai_channels(self) -> None:
        # `AI_ATTRIBUTED_SOURCE_TYPES` is the set write-paths consult to
        # decide whether a `model:` annotation is expected.
        assert AI_ATTRIBUTED_SOURCE_TYPES == frozenset(
            {"agent-observed", "inferred", "model-prior"}
        )

    def test_coerce_unknown_still_downgrades_to_inferred(self) -> None:
        # Fail-open contract preserved: an out-of-vocab value → inferred.
        assert coerce_source_type("wishful-thinking") == "inferred"


class TestParseModel:
    def test_missing_returns_empty_string(self) -> None:
        assert parse_model(None) == ""
        assert parse_model({}) == ""
        assert parse_model({"model": None}) == ""

    def test_string_value_returned_stripped(self) -> None:
        assert parse_model({"model": "  claude-opus-4-7  "}) == "claude-opus-4-7"

    def test_non_string_returns_empty(self) -> None:
        # Fail-open: a typo'd list or int returns "" rather than raising.
        assert parse_model({"model": 42}) == ""
        assert parse_model({"model": ["claude-opus-4-7"]}) == ""


class TestParseOnBehalfOf:
    def test_missing_returns_empty(self) -> None:
        assert parse_on_behalf_of(None) == ""
        assert parse_on_behalf_of({}) == ""

    def test_string_value_stripped(self) -> None:
        assert parse_on_behalf_of({"on_behalf_of": "  alice  "}) == "alice"

    def test_non_string_returns_empty(self) -> None:
        assert parse_on_behalf_of({"on_behalf_of": 42}) == ""


class TestParseAsserter:
    def test_missing_returns_empty_dict(self) -> None:
        assert parse_asserter(None) == {}
        assert parse_asserter({}) == {}
        assert parse_asserter({"asserter": None}) == {}

    def test_non_dict_returns_empty_dict(self) -> None:
        # Fail-open on a corruption signal.
        assert parse_asserter({"asserter": "not-a-dict"}) == {}
        assert parse_asserter({"asserter": 42}) == {}

    def test_round_trip_verbatim(self) -> None:
        # The parser normalizes for read-side consumers but must NOT drop
        # correctly-typed fields — round-trip fidelity beats normalization.
        block = {
            "type": "person",
            "iss": "https://accounts.google.com",
            "sub": "1076...",
            "email": "alice@example.com",
            "name": "Alice Example",
            "provider_ids": {"entra_oid": "o1", "entra_tid": "t1"},
        }
        assert parse_asserter({"asserter": block}) == block

    def test_non_string_keys_dropped(self) -> None:
        # YAML shouldn't produce non-string keys, but if it does they're
        # dropped rather than raising — same fail-open discipline.
        block = {"type": "person", "iss": "x", "sub": "y"}
        block_with_bad_key: dict = {**block, 42: "junk"}  # type: ignore[dict-item]
        assert parse_asserter({"asserter": block_with_bad_key}) == block


class TestAsserterIdentityKey:
    def test_empty_returns_empty_tuple(self) -> None:
        assert asserter_identity_key(None) == ()
        assert asserter_identity_key({}) == ()

    def test_standard_google_key_is_iss_and_sub(self) -> None:
        # Locked: Google/Okta/most OIDC providers key on (iss, sub).
        assert asserter_identity_key(
            {"iss": "https://accounts.google.com", "sub": "1076..."}
        ) == ("https://accounts.google.com", "1076...")

    def test_email_change_does_not_orphan_identity(self) -> None:
        # ACCEPTANCE CRITERION 2 (issue #326): an asserter block keyed on
        # iss+sub round-trips; email change does not orphan the identity.
        before = {
            "iss": "https://accounts.google.com",
            "sub": "1076...",
            "email": "alice@example.com",
        }
        after = {
            "iss": "https://accounts.google.com",
            "sub": "1076...",
            "email": "alice.new@example.com",  # renamed
            "name": "Alice New",
        }
        assert asserter_identity_key(before) == asserter_identity_key(after)

    def test_entra_pairwise_sub_ignored_when_tid_oid_present(self) -> None:
        # Microsoft Entra's `sub` is pairwise per app (OIDC-Core §8.1);
        # the identity key must NOT use it when the stable per-tenant
        # (tid, oid) pair is available. Two apps' pairwise `sub` values
        # for the same user must map to the same key.
        app_a = {
            "iss": "https://login.microsoftonline.com/tenant/",
            "sub": "pairwise-A",
            "provider_ids": {"entra_tid": "t1", "entra_oid": "o1"},
        }
        app_b = {
            "iss": "https://login.microsoftonline.com/tenant/",
            "sub": "pairwise-B",  # different app, different sub
            "provider_ids": {"entra_tid": "t1", "entra_oid": "o1"},
        }
        assert asserter_identity_key(app_a) == asserter_identity_key(app_b)
        assert asserter_identity_key(app_a) == (
            "https://login.microsoftonline.com/tenant/",
            "entra",
            "t1",
            "o1",
        )

    def test_entra_falls_back_to_sub_when_provider_ids_missing(self) -> None:
        # A caller who didn't populate provider_ids gets the standard
        # (iss, sub) key — degrades gracefully rather than returning ().
        got = asserter_identity_key(
            {"iss": "https://login.microsoftonline.com/t/", "sub": "s"}
        )
        assert got == ("https://login.microsoftonline.com/t/", "s")

    def test_no_iss_returns_empty(self) -> None:
        # A key requires iss. Without one there's no durable anchor.
        assert asserter_identity_key({"sub": "1076"}) == ()

    def test_no_sub_and_no_provider_ids_returns_empty(self) -> None:
        assert asserter_identity_key({"iss": "https://example.com"}) == ()

    def test_single_user_local_issuer(self) -> None:
        # Single-user mode degrades to `iss: local, sub: <owner>` per
        # design lock §10.3.
        assert asserter_identity_key({"iss": "local", "sub": "tristan"}) == (
            "local",
            "tristan",
        )


class TestWikiEntityChannelSplitRoundTrip:
    def test_render_emits_new_fields_when_set(self) -> None:
        # #326: WikiEntity's `model`, `on_behalf_of`, `asserter` render
        # into frontmatter only when set — legacy entities without them
        # produce byte-identical output.
        we = WikiEntity(
            uid="mp001",
            type="concept",
            name="training-prior-guess",
            source_type="model-prior",
            source_ref="claude-opus-4-7:prior",
            model="claude-opus-4-7",
            on_behalf_of="alice",
            asserter={"iss": "local", "sub": "alice"},
        )
        rendered = we.render()
        assert "source_type: model-prior" in rendered
        assert "model: claude-opus-4-7" in rendered
        assert "on_behalf_of: alice" in rendered
        assert "asserter:" in rendered
        assert "iss: local" in rendered

    def test_render_omits_new_fields_when_absent(self) -> None:
        # Absence must produce a clean render — legacy entities have no
        # `model` / `on_behalf_of` / `asserter` frontmatter keys.
        we = WikiEntity(uid="p001", type="person", name="Alice")
        rendered = we.render()
        assert "model:" not in rendered
        assert "on_behalf_of:" not in rendered
        assert "asserter:" not in rendered


class TestAutoMemoryFileChannelSplitDefaults:
    def test_defaults_are_empty(self, tmp_path: Path) -> None:
        # Legacy files without the new fields must construct cleanly.
        am = AutoMemoryFile(
            path=tmp_path / "m.md", origin_scope="s", memory_type="feedback"
        )
        assert am.model == ""
        assert am.on_behalf_of == ""
        assert am.asserter == {}

    def test_carries_new_fields(self, tmp_path: Path) -> None:
        am = AutoMemoryFile(
            path=tmp_path / "m.md",
            origin_scope="s",
            memory_type="feedback",
            source_type="model-prior",
            model="claude-opus-4-7",
            on_behalf_of="alice",
            asserter={"iss": "local", "sub": "alice"},
        )
        assert am.source_type == "model-prior"
        assert am.model == "claude-opus-4-7"
        assert am.on_behalf_of == "alice"
        assert asserter_identity_key(am.asserter) == ("local", "alice")


# ---------------------------------------------------------------------------
# Issue #327 — asserter comparison helper (same / different / unknown)
# ---------------------------------------------------------------------------


class TestCompareAsserters:
    _ALICE = {"iss": "https://accounts.google.com", "sub": "alice-1", "name": "Alice"}
    _ALICE_NEW_EMAIL = {
        "iss": "https://accounts.google.com",
        "sub": "alice-1",
        "email": "alice@new.example",
    }
    _BOB = {"iss": "https://accounts.google.com", "sub": "bob-2", "name": "Bob"}

    def test_same_identity_key(self) -> None:
        # Same (iss, sub) → "same" even though email/name differ (email is
        # never part of the key).
        assert compare_asserters(self._ALICE, self._ALICE_NEW_EMAIL) == "same"

    def test_different_identity_keys(self) -> None:
        assert compare_asserters(self._ALICE, self._BOB) == "different"

    def test_missing_identity_is_unknown(self) -> None:
        # Either side with no durable key → "unknown" (the common
        # Claude-session case — no OIDC identity).
        assert compare_asserters(self._ALICE, {}) == "unknown"
        assert compare_asserters(None, self._BOB) == "unknown"
        assert compare_asserters({}, {}) == "unknown"
        # An asserter with only a display name (no iss/sub) has no key.
        assert compare_asserters(self._ALICE, {"name": "Carol"}) == "unknown"

    def test_entra_pairwise_sub_keyed_on_oid(self) -> None:
        entra = {
            "iss": "https://login.microsoftonline.com/t/v2.0",
            "sub": "app-scoped-1",
            "provider_ids": {"entra_tid": "tid-9", "entra_oid": "oid-9"},
        }
        entra_other_app = {
            "iss": "https://login.microsoftonline.com/t/v2.0",
            "sub": "app-scoped-2",  # different pairwise sub, same person
            "provider_ids": {"entra_tid": "tid-9", "entra_oid": "oid-9"},
        }
        assert compare_asserters(entra, entra_other_app) == "same"
