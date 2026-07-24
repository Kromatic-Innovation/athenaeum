# SPDX-License-Identifier: Apache-2.0
"""Tests for the memory-taxonomy data model (issue #424).

Covers:
- ``memory_class:`` validation on ``WikiBase`` / ``validate_wiki_meta``:
  each of the 7 recognized values accepted; an unknown value flagged via
  ``UserWarning`` (not silently accepted); an absent value tolerated and
  reported as "untyped" via ``schemas.is_untyped_memory_class`` /
  ``_lint.lint_untyped_memory_class``.
- The existing ``type:`` (entity schema, #93) and ``memory_type:`` (intake)
  axes are BYTE-IDENTICAL — unchanged by this issue.
- ``## Inference`` block schema + parser: valid blocks parse to addressable
  units exposing ``basis`` + ``confidence``; malformed blocks are flagged.
- ``observed_at`` staleness field: accepted, surfaced, and round-trips
  through parse/serialize.

Explicitly NOT covered here (out of scope for #424): merge/recall/embed
behavior changes (there are none), inference-block retraction machinery
(#433), axiom governance (#434).
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from athenaeum._lint import lint_untyped_memory_class
from athenaeum.inference_blocks import InferenceBlock, parse_inference_blocks
from athenaeum.models import parse_frontmatter, parse_observed_at, render_frontmatter
from athenaeum.schemas import (
    KNOWN_TYPES,
    MEMORY_CLASSES,
    PersonWiki,
    WikiBase,
    is_untyped_memory_class,
    validate_wiki_meta,
)

# ---------------------------------------------------------------------------
# memory_class validation
# ---------------------------------------------------------------------------


class TestMemoryClassValues:
    def test_all_seven_values_defined(self) -> None:
        assert MEMORY_CLASSES == {
            "fact",
            "guideline",
            "axiom",
            "reference",
            "entity",
            "decision",
            "procedure",
        }

    @pytest.mark.parametrize("value", sorted(MEMORY_CLASSES))
    def test_each_known_value_accepted_no_warning(self, value: str) -> None:
        meta = {"uid": "abc12345", "type": "concept", "name": "X", "memory_class": value}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)
        assert m.memory_class == value

    def test_unknown_value_flagged_via_warning(self) -> None:
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "opinion-ish",
        }
        with pytest.warns(UserWarning, match="unknown memory_class"):
            m = validate_wiki_meta(meta)
        # Flagged, not silently accepted-and-hidden: the value is still
        # visible on the model (recoverable, like the #93 KNOWN_TYPES path).
        assert m.memory_class == "opinion-ish"

    def test_unknown_value_does_not_raise(self) -> None:
        meta = {"uid": "abc1", "type": "concept", "name": "X", "memory_class": "bogus"}
        with pytest.warns(UserWarning):
            validate_wiki_meta(meta)  # must not raise ValidationError

    def test_absent_memory_class_is_tolerated(self) -> None:
        meta = {"uid": "abc12345", "type": "concept", "name": "X"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)  # must not warn or raise
        assert m.memory_class is None

    def test_empty_string_memory_class_is_tolerated(self) -> None:
        meta = {"uid": "abc12345", "type": "concept", "name": "X", "memory_class": ""}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)
        assert m.memory_class is None


class TestUntypedSurfacing:
    """Absent memory_class must be reported as 'untyped', not silently dropped."""

    def test_is_untyped_memory_class_true_when_absent(self) -> None:
        assert is_untyped_memory_class({"uid": "a", "type": "concept", "name": "X"}) is True

    def test_is_untyped_memory_class_true_when_empty(self) -> None:
        assert is_untyped_memory_class({"memory_class": ""}) is True

    def test_is_untyped_memory_class_false_when_present(self) -> None:
        assert is_untyped_memory_class({"memory_class": "fact"}) is False
        # Even an invalid-but-present value is not "untyped" — it's a
        # different lint condition (flagged separately via UserWarning).
        assert is_untyped_memory_class({"memory_class": "bogus"}) is False

    def test_lint_untyped_memory_class_reports_absent(self) -> None:
        msg = lint_untyped_memory_class({"uid": "a", "type": "concept", "name": "X"})
        assert msg is not None
        assert "untyped" in msg

    def test_lint_untyped_memory_class_names_file_when_given(self) -> None:
        from pathlib import Path

        msg = lint_untyped_memory_class({}, Path("/wiki/legacy-page.md"))
        assert msg is not None
        assert "legacy-page.md" in msg

    def test_lint_untyped_memory_class_silent_when_present(self) -> None:
        assert lint_untyped_memory_class({"memory_class": "fact"}) is None


# ---------------------------------------------------------------------------
# Existing axes unchanged (type: / memory_type:) — byte-identical behavior
# ---------------------------------------------------------------------------


class TestExistingAxesUnchanged:
    """Issue #424 layers a new axis; type:/memory_type: must not move at all."""

    def test_known_types_frozenset_unchanged(self) -> None:
        # Exact membership pinned — a regression here would mean #424
        # accidentally touched the #93 entity-schema axis.
        assert KNOWN_TYPES == {
            "person",
            "company",
            "project",
            "concept",
            "source",
            "auto-memory",
            "tool",
            "reference",
            "principle",
            "feedback",
            "preference",
            "user",
        }

    def test_unknown_type_still_warns_exactly_as_before(self) -> None:
        meta = {"uid": "abc12345", "type": "persn", "name": "X"}
        with pytest.warns(UserWarning, match="unknown wiki type"):
            validate_wiki_meta(meta)

    def test_known_type_with_no_memory_class_still_no_warning(self) -> None:
        meta = {"uid": "abc12345", "type": "person", "name": "X"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)
        assert isinstance(m, PersonWiki)

    def test_required_fields_still_enforced(self) -> None:
        with pytest.raises(ValidationError):
            WikiBase(type="concept", name="X")  # missing uid

    def test_memory_type_field_not_touched_by_this_module(self) -> None:
        # memory_type lives entirely on AutoMemoryFile / models.py and is
        # untouched by schemas.py's memory_class addition. WikiBase has no
        # memory_type field at all (extra="allow" still lets it round-trip
        # if present, same as any other unknown-to-WikiBase key).
        meta = {
            "uid": "abc12345",
            "type": "auto-memory",
            "name": "X",
            "memory_type": "feedback",
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)
        assert m.model_dump()["memory_type"] == "feedback"


# ---------------------------------------------------------------------------
# observed_at — staleness axis, validation + round-trip
# ---------------------------------------------------------------------------


class TestObservedAt:
    def test_accepted_on_wikibase(self) -> None:
        m = WikiBase(uid="a1", type="concept", name="X", observed_at="2026-05-01")
        assert m.observed_at == "2026-05-01"

    def test_absent_is_none(self) -> None:
        m = WikiBase(uid="a1", type="concept", name="X")
        assert m.observed_at is None

    def test_empty_string_normalizes_to_none(self) -> None:
        m = WikiBase(uid="a1", type="concept", name="X", observed_at="")
        assert m.observed_at is None

    def test_validate_wiki_meta_surfaces_observed_at(self) -> None:
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "fact",
            "observed_at": "2026-04-15",
        }
        m = validate_wiki_meta(meta)
        assert m.observed_at == "2026-04-15"

    def test_parse_observed_at_reads_frontmatter_dict(self) -> None:
        from datetime import date

        assert parse_observed_at({"observed_at": "2026-04-15"}) == date(2026, 4, 15)

    def test_parse_observed_at_absent_is_none(self) -> None:
        assert parse_observed_at(None) is None
        assert parse_observed_at({}) is None

    def test_parse_observed_at_malformed_fails_open(self) -> None:
        assert parse_observed_at({"observed_at": "not-a-date"}) is None

    def test_round_trips_through_parse_and_render_frontmatter(self) -> None:
        """observed_at must survive a full parse -> validate -> render cycle."""
        original = (
            "---\n"
            "uid: fact0001\n"
            "type: concept\n"
            "name: Acme headcount\n"
            "memory_class: fact\n"
            "observed_at: 2026-03-01\n"
            "---\n"
            "Acme has 40 employees.\n"
        )
        meta, body = parse_frontmatter(original)
        assert meta["observed_at"] == "2026-03-01" or str(meta["observed_at"]) == "2026-03-01"

        # Validate through the schema boundary too.
        validated = validate_wiki_meta(meta)
        assert validated.observed_at is not None
        assert str(validated.observed_at) == "2026-03-01"

        # Round-trip: render the ORIGINAL dict (as tier0_passthrough would)
        # and re-parse; the value must come back unchanged.
        rendered = render_frontmatter(meta) + body
        reparsed_meta, reparsed_body = parse_frontmatter(rendered)
        assert str(reparsed_meta["observed_at"]) == "2026-03-01"
        assert reparsed_body == body

    def test_round_trips_via_model_dump(self) -> None:
        """The validated pydantic model must re-render with observed_at intact."""
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "fact",
            "observed_at": "2026-06-30",
        }
        model = validate_wiki_meta(meta)
        dumped = model.model_dump(exclude_none=True)
        rendered = render_frontmatter(dumped)
        reparsed, _ = parse_frontmatter(rendered)
        assert str(reparsed["observed_at"]) == "2026-06-30"


# ---------------------------------------------------------------------------
# Inference blocks — schema + parser
# ---------------------------------------------------------------------------


VALID_BLOCK = (
    "## Inference\n"
    "**Basis**: [[fact-a]], [[fact-b|Fact B alias]]\n"
    "**Confidence**: 0.8\n"
    "Acme's growth rate implies profitability by Q4.\n"
)


class TestInferenceBlockParsing:
    def test_parses_single_valid_block(self) -> None:
        blocks = parse_inference_blocks(VALID_BLOCK)
        assert len(blocks) == 1
        block = blocks[0]
        assert isinstance(block, InferenceBlock)
        assert block.basis == ["fact-a", "fact-b"]
        assert block.confidence == 0.8
        assert not block.malformed
        assert block.errors == []
        assert "growth rate" in block.body

    def test_block_is_addressable_with_stable_id(self) -> None:
        b1 = parse_inference_blocks(VALID_BLOCK)[0]
        b2 = parse_inference_blocks(VALID_BLOCK)[0]
        assert b1.id == b2.id
        assert len(b1.id) == 12

    def test_id_changes_when_block_content_changes(self) -> None:
        other = VALID_BLOCK.replace("Q4", "Q3")
        b1 = parse_inference_blocks(VALID_BLOCK)[0]
        b2 = parse_inference_blocks(other)[0]
        assert b1.id != b2.id

    def test_ignores_non_inference_headers(self) -> None:
        text = "## Summary\nSome unrelated section.\n\n" + VALID_BLOCK
        blocks = parse_inference_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].basis == ["fact-a", "fact-b"]

    def test_parses_multiple_inference_blocks(self) -> None:
        second = (
            "\n## Inference\n"
            "**Basis**: [[fact-c]]\n"
            "**Confidence**: 0.5\n"
            "Another derived claim.\n"
        )
        text = VALID_BLOCK + second
        blocks = parse_inference_blocks(text)
        assert len(blocks) == 2
        assert blocks[1].basis == ["fact-c"]
        assert blocks[1].confidence == 0.5

    def test_stops_at_next_header(self) -> None:
        text = VALID_BLOCK + "## Another Section\nNot part of the inference block.\n"
        blocks = parse_inference_blocks(text)
        assert len(blocks) == 1
        assert "Another Section" not in blocks[0].body
        assert "Not part of the inference block" not in blocks[0].body

    def test_no_inference_blocks_returns_empty_list(self) -> None:
        assert parse_inference_blocks("## Summary\nJust prose.\n") == []
        assert parse_inference_blocks("") == []

    def test_works_on_full_page_with_frontmatter(self) -> None:
        # Frontmatter lines never start with "## ", so passing the whole
        # file (not just the body) is harmless.
        full = (
            "---\n"
            "uid: fact0001\n"
            "type: concept\n"
            "name: X\n"
            "memory_class: fact\n"
            "---\n" + VALID_BLOCK
        )
        blocks = parse_inference_blocks(full)
        assert len(blocks) == 1
        assert blocks[0].basis == ["fact-a", "fact-b"]


class TestInferenceBlockMalformed:
    def test_missing_basis_is_flagged(self) -> None:
        text = "## Inference\n**Confidence**: 0.6\nSome claim with no basis.\n"
        blocks = parse_inference_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].malformed is True
        assert any("Basis" in e for e in blocks[0].errors)

    def test_missing_confidence_is_flagged(self) -> None:
        text = "## Inference\n**Basis**: [[fact-a]]\nSome claim with no confidence.\n"
        blocks = parse_inference_blocks(text)
        assert blocks[0].malformed is True
        assert any("Confidence" in e for e in blocks[0].errors)

    def test_unparseable_confidence_is_flagged(self) -> None:
        text = "## Inference\n**Basis**: [[fact-a]]\n**Confidence**: high\nClaim.\n"
        blocks = parse_inference_blocks(text)
        assert blocks[0].malformed is True
        assert blocks[0].confidence is None
        assert any("Confidence" in e for e in blocks[0].errors)

    def test_out_of_range_confidence_is_flagged(self) -> None:
        text = "## Inference\n**Basis**: [[fact-a]]\n**Confidence**: 1.5\nClaim.\n"
        blocks = parse_inference_blocks(text)
        assert blocks[0].malformed is True
        assert blocks[0].confidence == 1.5  # still surfaced, just flagged
        assert any("out of range" in e for e in blocks[0].errors)

    def test_basis_with_no_wikilink_is_flagged(self) -> None:
        text = "## Inference\n**Basis**: fact-a (not a link)\n**Confidence**: 0.5\nClaim.\n"
        blocks = parse_inference_blocks(text)
        assert blocks[0].malformed is True
        assert blocks[0].basis == []
        assert any("no recoverable wikilink" in e for e in blocks[0].errors)

    def test_malformed_block_still_returned_not_dropped(self) -> None:
        """A malformed block must be visible to a linter, not silently skipped."""
        text = "## Inference\nJust prose, no metadata at all.\n"
        blocks = parse_inference_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].malformed is True
        assert len(blocks[0].errors) == 2  # missing basis + missing confidence


# ---------------------------------------------------------------------------
# Zero behavior change to merge/recall/embed (scope guard)
# ---------------------------------------------------------------------------


class TestScopeGuard:
    """Issue #424 is data-model + validation + parser + doc only."""

    def test_schemas_module_exports_new_names(self) -> None:
        import athenaeum.schemas as schemas_mod

        assert "MEMORY_CLASSES" in schemas_mod.__all__
        assert "is_untyped_memory_class" in schemas_mod.__all__

    def test_inference_blocks_module_has_no_retraction_machinery(self) -> None:
        import athenaeum.inference_blocks as ib_mod

        # #433 (retraction) is explicitly out of scope for #424 — this
        # module must expose only the schema + parser, not a retract/apply
        # function. A future #433 PR adding these is expected; this test
        # documents today's boundary.
        assert not hasattr(ib_mod, "retract_inference")
        assert not hasattr(ib_mod, "apply_retraction")
