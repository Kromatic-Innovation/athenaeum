# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.schemas — Pydantic models for wiki frontmatter validation.

Covers:
- Required-field enforcement (uid, type, name) on every concrete model.
- Score-string -> float coercion via field validator (priority_score).
- Type-discriminated dispatch (validate_wiki_meta).
- Negative-path validation (empty uid, float on identity field, score=bool).
- Round-trip validation against the bundled ``tests/fixtures/wiki_sample/``
  fixture tree (CI gate).
- Optional smoke gate against the live wiki at ``~/knowledge/wiki/`` —
  local-only, opt-in via ``ATHENAEUM_LIVE_WIKI_TEST=1``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from athenaeum.models import parse_frontmatter
from athenaeum.schemas import (
    FALLBACK_TYPES,
    KNOWN_TYPES,
    CompanyWiki,
    ConceptWiki,
    PersonWiki,
    ProjectWiki,
    SourceWiki,
    WikiBase,
    validate_wiki_meta,
)

# --- Required-field enforcement ---


@pytest.mark.parametrize(
    "model_cls,etype",
    [
        (PersonWiki, "person"),
        (CompanyWiki, "company"),
        (ProjectWiki, "project"),
        (ConceptWiki, "concept"),
        (SourceWiki, "source"),
        (WikiBase, "anything"),
    ],
)
def test_required_fields_enforced(model_cls, etype):
    """uid, type, name are required on every model."""
    # Missing uid
    with pytest.raises(ValidationError):
        model_cls(type=etype, name="X")
    # Missing type
    with pytest.raises(ValidationError):
        model_cls(uid="abc12345", name="X")
    # Missing name
    with pytest.raises(ValidationError):
        model_cls(uid="abc12345", type=etype)
    # All present → ok
    m = model_cls(uid="abc12345", type=etype, name="X")
    assert m.uid == "abc12345"
    assert m.type == etype
    assert m.name == "X"


def test_extra_fields_allowed():
    """extra='allow' must round-trip arbitrary custom frontmatter."""
    m = PersonWiki(
        uid="abc12345",
        type="person",
        name="X",
        apollo_id="zzz",
        linkedin_url="https://example.com",
        custom_field={"nested": [1, 2]},
    )
    dumped = m.model_dump()
    assert dumped["apollo_id"] == "zzz"
    assert dumped["linkedin_url"] == "https://example.com"
    assert dumped["custom_field"] == {"nested": [1, 2]}


# --- Score coercion ---


def test_priority_score_coerces_string_to_float():
    m = PersonWiki(uid="a1", type="person", name="X", priority_score="0.4")
    assert m.priority_score == 0.4
    assert isinstance(m.priority_score, float)


def test_priority_score_accepts_float():
    m = PersonWiki(uid="a1", type="person", name="X", priority_score=0.7)
    assert m.priority_score == 0.7


def test_priority_score_optional():
    m = PersonWiki(uid="a1", type="person", name="X")
    assert m.priority_score is None


def test_priority_score_invalid_raises():
    with pytest.raises(ValidationError):
        PersonWiki(uid="a1", type="person", name="X", priority_score="not-a-number")


# --- Negative paths (Quine fix #4) ---


def test_empty_string_uid_raises():
    """uid="" must fail validation (whitespace-only also rejected)."""
    with pytest.raises(ValidationError):
        WikiBase(uid="", type="person", name="X")
    with pytest.raises(ValidationError):
        WikiBase(uid="   ", type="person", name="X")


def test_float_on_identity_field_raises():
    """Quine fix #5: a float arriving on uid is corruption, not coerced."""
    with pytest.raises(ValidationError):
        WikiBase(uid=1.5, type="person", name="X")


def test_score_as_bool_coerces_to_float():
    """``True`` is an int subclass in Python; documents current behavior:
    ``_coerce_score`` accepts it and coerces to ``1.0``. If we ever want to
    reject bools, change ``_coerce_score`` and flip this test."""
    m = PersonWiki(uid="a1", type="person", name="X", priority_score=True)
    assert m.priority_score == 1.0
    assert isinstance(m.priority_score, float)


def test_company_priority_score_optional():
    """CompanyWiki.priority_score is optional — missing is allowed."""
    m = CompanyWiki(uid="a1", type="company", name="C")
    assert m.priority_score is None


# --- Dispatcher ---


def test_validate_wiki_meta_dispatches_by_type():
    p = validate_wiki_meta({"uid": "a1", "type": "person", "name": "P"})
    assert isinstance(p, PersonWiki)
    c = validate_wiki_meta({"uid": "a1", "type": "company", "name": "C"})
    assert isinstance(c, CompanyWiki)
    proj = validate_wiki_meta({"uid": "a1", "type": "project", "name": "Pj"})
    assert isinstance(proj, ProjectWiki)
    con = validate_wiki_meta({"uid": "a1", "type": "concept", "name": "K"})
    assert isinstance(con, ConceptWiki)
    s = validate_wiki_meta({"uid": "a1", "type": "source", "name": "S"})
    assert isinstance(s, SourceWiki)


def test_validate_wiki_meta_unknown_type_falls_through_to_base():
    """type values not in the concrete-five fall through to WikiBase
    with extras preserved — documents the open-base contract."""
    m = validate_wiki_meta(
        {"uid": "a1", "type": "tool", "name": "T", "homepage": "https://x"}
    )
    assert isinstance(m, WikiBase)
    assert m.type == "tool"
    dumped = m.model_dump()
    assert dumped["homepage"] == "https://x"


def test_validate_wiki_meta_missing_required_raises():
    with pytest.raises(ValidationError):
        validate_wiki_meta({"type": "person", "name": "X"})  # no uid


# --- Fixture-tree round-trip (CI gate) ---

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wiki_sample"


def test_fixture_wiki_sample_validates():
    """Every fixture wiki under ``tests/fixtures/wiki_sample/`` must
    validate. This is the real CI gate for tier0_passthrough's
    byte-for-byte round-trip — the live-tree walk below is local-only."""
    failures: list[tuple[str, str]] = []
    checked = 0
    paths = sorted(FIXTURE_ROOT.glob("*.md"))
    assert paths, f"No fixture wikis found at {FIXTURE_ROOT}"
    for fpath in paths:
        text = fpath.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        assert meta, f"{fpath.name}: parse_frontmatter returned empty"
        checked += 1
        try:
            validate_wiki_meta(meta)
        except ValidationError as e:
            failures.append((fpath.name, str(e)[:200]))
    assert not failures, f"Fixture wiki validation failures: {failures}"
    assert checked >= 5, f"Expected ≥5 fixture wikis, got {checked}"


def test_fixture_int_uid_yaml_roundtrip():
    """Quine fix #1: a YAML uid that loads as int must reach the schema
    as str. Asserts the boundary coercion in ``parse_frontmatter``."""
    text = (
        "---\n" "uid: 19052\n" "type: person\n" "name: Numeric Uid\n" "---\n" "body\n"
    )
    meta, _ = parse_frontmatter(text)
    assert isinstance(meta["uid"], str)
    assert meta["uid"] == "19052"
    # And the schema accepts it without int-coercion in the validator.
    m = validate_wiki_meta(meta)
    assert isinstance(m, PersonWiki)
    assert m.uid == "19052"


# --- Live wiki round-trip (local-only smoke gate) ---

WIKI_ROOT = Path(os.path.expanduser("~/knowledge/wiki"))


@pytest.mark.skipif(
    os.environ.get("ATHENAEUM_LIVE_WIKI_TEST") != "1" or not WIKI_ROOT.exists(),
    reason=(
        "Live-wiki smoke gate is local-only. "
        "Set ATHENAEUM_LIVE_WIKI_TEST=1 with ~/knowledge/wiki/ present to run."
    ),
)
def test_live_wiki_roundtrip():
    """Local-only smoke gate: walk every wiki under ``~/knowledge/wiki/``
    and validate. Opt-in via ``ATHENAEUM_LIVE_WIKI_TEST=1``. CI relies on
    the fixture-tree test above; this catches drift in the developer's
    real corpus."""
    failures: list[tuple[str, str]] = []
    checked = 0
    for fpath in WIKI_ROOT.glob("*.md"):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            failures.append((fpath.name, f"read error: {e}"))
            continue
        meta, _ = parse_frontmatter(text)
        if not meta:
            continue
        if not meta.get("uid") or not meta.get("type") or not meta.get("name"):
            continue
        checked += 1
        try:
            validate_wiki_meta(meta)
        except ValidationError as e:
            failures.append((fpath.name, str(e)[:200]))

    assert not failures, (
        f"{len(failures)} wiki(s) failed validation out of {checked} checked. "
        f"First 5: {failures[:5]}"
    )
    assert checked > 0, "Expected at least one wiki to validate"


# --- Provenance (issue #90) ---


class TestProvenanceFields:
    """``source`` and ``field_sources`` validators on WikiBase."""

    def test_scalar_source_accepted(self) -> None:
        m = PersonWiki(
            uid="abc12345",
            type="person",
            name="X",
            source="api:apollo:2026-05-07",
        )
        # Round-trip fidelity — scalar must NOT be normalized to dict.
        assert m.source == "api:apollo:2026-05-07"

    def test_structured_source_accepted(self) -> None:
        src = {"type": "api", "ref": "apollo", "confidence": 0.9}
        m = PersonWiki(
            uid="abc12345",
            type="person",
            name="X",
            source=src,
        )
        assert m.source == src

    def test_field_sources_map_accepted(self) -> None:
        fs = {
            "emails": "api:apollo:2026-05-07",
            "current_title": "linkedin:nicole-segerer",
        }
        m = PersonWiki(uid="abc12345", type="person", name="X", field_sources=fs)
        assert m.field_sources == fs

    def test_both_fields_populated(self) -> None:
        m = CompanyWiki(
            uid="def67890",
            type="company",
            name="Acme",
            source="manual:initial-import",
            field_sources={"website": "scraped:homepage:2026-04-01"},
        )
        assert m.source == "manual:initial-import"
        assert m.field_sources == {"website": "scraped:homepage:2026-04-01"}

    def test_malformed_source_raises(self) -> None:
        # "Has-Uppercase" matches neither typed nor legacy form.
        with pytest.raises(ValidationError):
            PersonWiki(uid="abc12345", type="person", name="X", source="Has-Uppercase")

    def test_malformed_field_sources_raises(self) -> None:
        with pytest.raises(ValidationError):
            PersonWiki(
                uid="abc12345",
                type="person",
                name="X",
                field_sources={"emails": "Has-Uppercase"},
            )

    def test_legacy_bare_slug_source_rejected(self) -> None:
        # Post-#97: legacy bare-slug `source:` form is retired. The live
        # tree was migrated to `script:<slug>` on 2026-05-09; the schema
        # now rejects bare slugs and requires the typed `<type>:<ref>` form.
        with pytest.raises(ValueError):
            PersonWiki(
                uid="abc12345",
                type="person",
                name="X",
                source="extended-tier-build",
            )

    def test_typed_script_source_accepted(self) -> None:
        # Post-migration shape — `script:<slug>` is the typed equivalent.
        m = PersonWiki(
            uid="abc12345",
            type="person",
            name="X",
            source="script:extended-tier-build",
        )
        assert m.source == "script:extended-tier-build"

    def test_none_passes(self) -> None:
        m = PersonWiki(uid="abc12345", type="person", name="X")
        assert m.source is None
        assert m.field_sources is None

    def test_validate_wiki_meta_with_provenance(self) -> None:
        meta = {
            "uid": "abc12345",
            "type": "person",
            "name": "X",
            "source": "api:apollo:2026-05-07",
            "field_sources": {"emails": "api:apollo:2026-05-07"},
        }
        m = validate_wiki_meta(meta)
        assert m.source == "api:apollo:2026-05-07"
        assert m.field_sources == {"emails": "api:apollo:2026-05-07"}


# --- KNOWN_TYPES allowlist (issue #93) ---


class TestKnownTypes:
    """Issue #93: validate_wiki_meta warns on unknown types but does not raise."""

    def test_known_types_includes_concrete_schemas(self) -> None:
        for t in ("person", "company", "project", "concept", "source"):
            assert t in KNOWN_TYPES

    def test_known_types_includes_fallback_set(self) -> None:
        for t in (
            "auto-memory",
            "tool",
            "reference",
            "principle",
            "feedback",
            "preference",
            "user",
        ):
            assert t in KNOWN_TYPES
            assert t in FALLBACK_TYPES

    def test_unknown_type_emits_warning(self) -> None:
        meta = {"uid": "abc12345", "type": "persn", "name": "X"}
        with pytest.warns(UserWarning, match="unknown wiki type"):
            validate_wiki_meta(meta)

    def test_unknown_type_does_not_raise(self) -> None:
        # Falls through to WikiBase — uid/type/name still validated.
        meta = {"uid": "abc12345", "type": "novel-type", "name": "X"}
        with pytest.warns(UserWarning):
            m = validate_wiki_meta(meta)
        assert m.type == "novel-type"

    def test_known_type_emits_no_warning(self) -> None:
        import warnings as _w

        meta = {"uid": "abc12345", "type": "auto-memory", "name": "X"}
        with _w.catch_warnings():
            _w.simplefilter("error", UserWarning)
            # Should not raise — no warning expected for KNOWN_TYPES.
            validate_wiki_meta(meta)
