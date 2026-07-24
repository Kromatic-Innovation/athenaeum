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

Memory taxonomy (issue #424):
- ``memory_class:`` is a THIRD, orthogonal, LAYERED axis alongside ``type:``
  (this module's ``KNOWN_TYPES``) and intake ``memory_type:``
  (``models.py``: feedback/project/reference/user/recall). It is NOT a
  replacement for either — a person page keeps ``type: person`` and may
  additionally gain ``memory_class: entity``. See
  ``docs/memory-taxonomy.md`` for the full axis-reconciliation writeup and
  merge-vs-cite semantics (enforcement of those semantics is #433).
- Mirrors the #93 ``KNOWN_TYPES`` shape exactly: a recognized value is
  silent; an unrecognized non-empty value emits a :class:`UserWarning`
  (flagged, not silently accepted); an ABSENT ``memory_class`` is tolerated
  (legacy/untyped pages must not break) and is reported via
  :func:`is_untyped_memory_class` so a linter/report can surface it as
  "untyped" without that itself being a warning.
"""
from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from athenaeum.provenance import validate_field_sources, validate_source_value

#: The 7 recognized ``memory_class:`` values (issue #424). Deliberately does
#: NOT include ``open-question`` / ``hypothesis`` — the settled taxonomy
#: defers those rather than over-minting classes up front.
MEMORY_CLASSES: frozenset[str] = frozenset(
    {
        "fact",
        "guideline",
        "axiom",
        "reference",
        "entity",
        "decision",
        "procedure",
    }
)


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
    # ``field_sources`` per-field value is one of:
    # - ``str``/``dict`` (legacy single source for the whole field), or
    # - ``list[dict]`` of ``{"value", "source"}`` records (per-value
    #   attribution for list-typed fields, issue #102).
    field_sources: dict[str, str | dict | list] | None = None

    # Issue #424: the memory-taxonomy axis, layered on top of ``type:``.
    # ``None`` (absent) is tolerated — legacy/untyped pages must not break —
    # see :func:`is_untyped_memory_class`. A non-``None`` value outside
    # :data:`MEMORY_CLASSES` is flagged via ``UserWarning`` in
    # ``_validate_memory_class`` below (NOT silently accepted) but does not
    # raise, matching the #93 ``KNOWN_TYPES`` precedent this axis is layered
    # beside.
    memory_class: str | None = None

    # Issue #424 (staleness axis): standing-state facts carry ``observed_at``
    # — the date the fact was TRUE-WHEN-OBSERVED, as distinct from
    # ``created``/``updated`` (write-time bookkeeping) and from
    # ``valid_from``/``valid_until`` (the claim-validity window, #308).
    # Declared as an explicit field (rather than relying solely on
    # ``extra="allow"``) so it is a first-class, documented part of the
    # schema; stored as the on-disk scalar (str) for round-trip fidelity —
    # no date coercion here, mirroring how ``source``/``field_sources``
    # keep their on-disk shape rather than normalizing to a Python type.
    observed_at: str | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _validate_source(cls, v: Any) -> Any:
        return validate_source_value(v)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _validate_observed_at(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return str(v)

    @field_validator("memory_class", mode="before")
    @classmethod
    def _validate_memory_class(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if not isinstance(v, str) or v not in MEMORY_CLASSES:
            warnings.warn(
                f"unknown memory_class: {v!r} (not in MEMORY_CLASSES)",
                UserWarning,
                stacklevel=2,
            )
        return v

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


def is_untyped_memory_class(meta: dict[str, Any]) -> bool:
    """True when ``meta`` carries no (non-empty) ``memory_class:`` value.

    Issue #424: an absent ``memory_class`` is TOLERATED by validation (a
    legacy/untyped page must not fail to validate) but should still be
    SURFACED — e.g. by a lint/report pass counting untyped pages — rather
    than silently disappearing. This helper is the single predicate such a
    surfacing pass should call so "untyped" has one definition. Does not
    itself warn or raise; it is a pure read of the frontmatter dict, usable
    before or after :func:`validate_wiki_meta`.
    """
    value = meta.get("memory_class")
    return value is None or value == ""


__all__ = [
    "WikiBase",
    "PersonWiki",
    "CompanyWiki",
    "ProjectWiki",
    "ConceptWiki",
    "SourceWiki",
    "FALLBACK_TYPES",
    "KNOWN_TYPES",
    "MEMORY_CLASSES",
    "validate_wiki_meta",
    "is_untyped_memory_class",
]
