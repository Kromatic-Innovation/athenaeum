# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.schemas — Pydantic models for wiki frontmatter validation.

Covers:
- Required-field enforcement (uid, type, name) on every concrete model.
- Score-string -> float coercion via field validator (priority_score).
- Type-discriminated dispatch (validate_wiki_meta).
- Round-trip validation against every existing wiki under ~/knowledge/wiki/.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from athenaeum.models import parse_frontmatter
from athenaeum.schemas import (
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
    with pytest.raises(Exception):
        model_cls(type=etype, name="X")
    # Missing type
    with pytest.raises(Exception):
        model_cls(uid="abc12345", name="X")
    # Missing name
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        PersonWiki(uid="a1", type="person", name="X", priority_score="not-a-number")


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
    """type values not in the concrete-five fall through to WikiBase."""
    m = validate_wiki_meta({"uid": "a1", "type": "tool", "name": "T"})
    assert isinstance(m, WikiBase)
    assert m.type == "tool"


def test_validate_wiki_meta_missing_required_raises():
    with pytest.raises(Exception):
        validate_wiki_meta({"type": "person", "name": "X"})  # no uid


# --- Live wiki round-trip (gate test) ---

WIKI_ROOT = Path(os.path.expanduser("~/knowledge/wiki"))


@pytest.mark.skipif(
    not WIKI_ROOT.exists(),
    reason="No live wiki at ~/knowledge/wiki — skipping round-trip gate.",
)
def test_every_live_wiki_validates():
    """Every wiki page under ~/knowledge/wiki/ must validate against its schema.

    This is the contract that protects tier0_passthrough's byte-for-byte
    round-trip — any schema change that breaks a real wiki is caught here.
    """
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
            continue  # non-frontmatter wiki page — index loader skips these too
        if not meta.get("uid") or not meta.get("type") or not meta.get("name"):
            continue  # non-entity-format page (legacy notes); index skips these
        checked += 1
        try:
            validate_wiki_meta(meta)
        except Exception as e:
            failures.append((fpath.name, str(e)[:200]))

    assert not failures, (
        f"{len(failures)} wiki(s) failed validation out of {checked} checked. "
        f"First 5: {failures[:5]}"
    )
    assert checked > 0, "Expected at least one wiki to validate"
