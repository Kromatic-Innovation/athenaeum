# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for wiki frontmatter validation.

These models guard the write path so malformed frontmatter cannot reach
``wiki/``. They sit alongside the dataclasses in :mod:`athenaeum.models`
(``WikiEntity`` etc.) — those remain the in-memory pipeline shape; these
validate frontmatter dicts at the schema boundary.

Design:
- ``WikiBase`` is the open base. Required: ``uid``, ``type``, ``name``.
  ``model_config = ConfigDict(extra="allow")`` so non-core fields
  (``apollo_*``, ``linkedin_url``, ``relationship``, ``current_title``, …)
  round-trip byte-for-byte through tier0_passthrough.
- Concrete subclasses (PersonWiki / CompanyWiki / ProjectWiki / ConceptWiki
  / SourceWiki) exist for type-discriminated dispatch and to host
  type-specific validators (e.g. ``priority_score`` string→float coercion
  on PersonWiki).
- ``validate_wiki_meta`` dispatches a frontmatter dict to the right model
  by ``type``. Unknown types fall through to ``WikiBase`` rather than
  raising — the live wiki has 13+ types (tool, reference, principle,
  auto-memory, feedback, preference, user, …) and Lane A is not retyping
  them.

Out of scope here (Lane B / #90, Lane G / #91):
- Per-claim ``source`` / ``field_sources`` provenance.
- Conflict-resolution semantics on update.
"""
from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from athenaeum.provenance import validate_field_sources, validate_source_value


class WikiBase(BaseModel):
    """Base model for any wiki frontmatter. Open by design.

    Required: uid, type, name. Everything else passes through via
    ``extra="allow"`` so custom-namespace fields survive round-trip.

    Provenance (issue #90):
    - ``source`` is the wiki-level default source for any frontmatter
      field that does not have a ``field_sources`` override.
    - ``field_sources`` is a per-claim map ``{<field>: <source>}``.
    Both accept either a scalar ``"<type>:<ref>"`` or a structured
    object ``{type, ref, ts?, confidence?, notes?}``.
    """

    model_config = ConfigDict(extra="allow")

    uid: str
    type: str
    name: str

    # Per-claim provenance (issue #90). Stored as the on-disk shape
    # (str OR dict) — round-trip fidelity beats normalization here.
    source: str | dict | None = None
    field_sources: dict[str, str | dict] | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _validate_source(cls, v: Any) -> Any:
        return validate_source_value(v)

    @field_validator("field_sources", mode="before")
    @classmethod
    def _validate_field_sources(cls, v: Any) -> Any:
        return validate_field_sources(v)

    @field_validator("uid", "type", "name", mode="before")
    @classmethod
    def _require_nonempty_str(cls, v: Any) -> str:
        # Identity fields must be non-empty strings. YAML int-coercion
        # (bare all-decimal hex uids loading as int) is handled at the
        # YAML boundary in ``models.parse_frontmatter`` — by the time we
        # see the dict here, those have been stringified. A ``float``
        # arriving on uid/type/name is a corruption signal (mis-quoted
        # YAML scalar), not something to silently coerce.
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


def _coerce_score(v: Any) -> float | None:
    """Coerce a frontmatter score-ish value to float. None passes through."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError as e:
            raise ValueError(f"score must parse as float: {v!r}") from e
    raise ValueError(f"unsupported score type: {type(v).__name__}")


class PersonWiki(WikiBase):
    """type: person — contact-wiki entries."""

    priority_score: float | None = None

    @field_validator("priority_score", mode="before")
    @classmethod
    def _coerce_priority_score(cls, v: Any) -> float | None:
        return _coerce_score(v)


class CompanyWiki(WikiBase):
    """type: company — organizations."""

    priority_score: float | None = None

    @field_validator("priority_score", mode="before")
    @classmethod
    def _coerce_priority_score(cls, v: Any) -> float | None:
        return _coerce_score(v)


class ProjectWiki(WikiBase):
    """type: project — initiatives, codebases, products."""


class ConceptWiki(WikiBase):
    """type: concept — abstract ideas, principles, methods."""


class SourceWiki(WikiBase):
    """type: source — citation/reference origins."""


# --- Dispatcher ---

_BY_TYPE: dict[str, type[WikiBase]] = {
    "person": PersonWiki,
    "company": CompanyWiki,
    "project": ProjectWiki,
    "concept": ConceptWiki,
    "source": SourceWiki,
}

# Types that are not in ``_BY_TYPE`` but are present in the live wiki
# tree as of 2026-05-09 (issue #93 audit). These fall through to
# :class:`WikiBase` for validation; the allowlist exists so unknown
# types (typos, drift) emit a warning instead of being silently
# accepted. See issue #93.
FALLBACK_TYPES: frozenset[str] = frozenset(
    {
        "auto-memory",
        "tool",
        "reference",
        "principle",
        "feedback",
        "preference",
        "user",
    }
)

#: All wiki ``type`` values currently recognized — concrete schemas
#: plus the live-tree fallback set. Anything outside this set triggers
#: a :class:`UserWarning` from :func:`validate_wiki_meta`.
KNOWN_TYPES: frozenset[str] = frozenset(_BY_TYPE) | FALLBACK_TYPES


def validate_wiki_meta(meta: dict[str, Any]) -> WikiBase:
    """Validate a frontmatter dict against the appropriate schema.

    Dispatches by ``meta["type"]``. Unknown types fall through to
    :class:`WikiBase` (still enforces uid/type/name). Raises
    :class:`pydantic.ValidationError` on malformed input.

    Issue #93: emits a :class:`UserWarning` (NOT an exception) when
    ``meta["type"]`` is outside :data:`KNOWN_TYPES`. Recoverable —
    strict mode is out of scope.
    """
    etype = meta.get("type", "")
    if etype and etype not in KNOWN_TYPES:
        warnings.warn(
            f"unknown wiki type: {etype!r} (not in KNOWN_TYPES)",
            UserWarning,
            stacklevel=2,
        )
    model_cls = _BY_TYPE.get(etype, WikiBase)
    return model_cls.model_validate(meta)


__all__ = [
    "WikiBase",
    "PersonWiki",
    "CompanyWiki",
    "ProjectWiki",
    "ConceptWiki",
    "SourceWiki",
    "FALLBACK_TYPES",
    "KNOWN_TYPES",
    "validate_wiki_meta",
]
