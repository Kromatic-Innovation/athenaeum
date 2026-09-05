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
  auto-memory, incident, preference, …) and Lane A is not retyping
  them.

Out of scope here (Lane B / athenaeum#90, Lane G / athenaeum#91):
- Per-claim ``source`` / ``field_sources`` provenance.
- Conflict-resolution semantics on update.

Memory taxonomy (issue athenaeum#424):
- ``memory_class:`` is a THIRD, orthogonal, LAYERED axis alongside ``type:``
  (this module's ``KNOWN_TYPES``) and intake ``memory_type:``
  (``models.py``: feedback/project/reference/user/recall). It is NOT a
  replacement for either — a person page keeps ``type: person`` and may
  additionally gain ``memory_class: entity``. See
  ``docs/design/memory-taxonomy.md`` for the full axis-reconciliation writeup and
  merge-vs-cite semantics (enforcement of those semantics is athenaeum#433).

Axiom governance (issue athenaeum#434):
- ``memory_class: axiom`` additionally requires an explicit, recorded,
  human-approved PROMOTION on file — a page carrying that value with no
  active promotion record is flagged (see
  :func:`athenaeum.axiom_governance.warn_if_unbacked_axiom`). That check
  needs ledger I/O keyed by slug, which a ``field_validator`` here cannot
  do (no filesystem access, no cross-page context) — see
  :mod:`athenaeum.axiom_governance`'s module docstring for the full design
  and why the check lives there instead of in ``_validate_memory_class``
  below. This module's role in athenaeum#434 is limited to the ``scope:`` field
  (below) — the axiom-governance ledger, promotion/demotion, and audit
  listing all live in :mod:`athenaeum.axiom_governance`.
- Mirrors the athenaeum#93 ``KNOWN_TYPES`` shape exactly: a recognized value is
  silent; an unrecognized non-empty value emits a :class:`UserWarning`
  (flagged, not silently accepted); an ABSENT ``memory_class`` is tolerated
  (legacy/untyped pages must not break) and is reported via
  :func:`is_untyped_memory_class` so a linter/report can surface it as
  "untyped" without that itself being a warning.

PII off-corpus (issue athenaeum#427):
- Entity pages carry durable identifiers only (name, LinkedIn, record id,
  Google-Contact id); inline archival contact data (``emails:`` / ``phones:``
  frontmatter) does not belong on a page that stays in the embedded/recalled
  corpus. ``WikiBase`` flags this via a :class:`UserWarning` — mirroring the
  athenaeum#93 ``KNOWN_TYPES`` / athenaeum#424 ``memory_class`` precedent exactly (recoverable,
  not a hard failure, since migrating pre-existing pages is athenaeum#437's operator
  task, not this validator's job). A page carrying a truthy ``pii:`` flag
  (:func:`athenaeum.pii.is_pii_flagged`) is EXEMPT from this particular
  warning — that flag is the explicit "yes, I know, and every corpus
  consumer already excludes this page" acknowledgment (see
  :mod:`athenaeum.pii`'s module docstring, point 3). See
  :mod:`athenaeum.pii` for the full off-corpus design (contacts surface,
  observation log, supersession fold); this module only hosts the
  frontmatter-boundary half of the lint (body-text inline detection needs
  the page body, which is out of scope for a frontmatter-only validator —
  see :func:`athenaeum.pii.lint_inline_contact_fields` for the body-aware
  batch-lint counterpart).

Layering: L1 (data model — pydantic validation boundary alongside
:mod:`athenaeum.models`). Imports :mod:`athenaeum.provenance` (L1, sibling)
and :mod:`athenaeum.pii` (a higher-layer service module) for one validator's
exemption check — a deliberate, narrow upward reach for a single flag lookup,
not a general license to import service-layer policy here. Factoring rule:
this module owns ONLY frontmatter SHAPE validation (required fields, type
coercion, the flagged-vs-tolerated axes above); it must never decide merge/
resolution/conflict outcomes — that is out of scope per the notes above.
"""
from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# The ``memory_class`` vocabulary + rule map live in the leaf module
# :mod:`athenaeum.memory_class` (issue athenaeum#996) so the WRITE model
# (``models.WikiEntity``) can share them without closing an import cycle
# through config/pii/storage. Re-exported here because ``MEMORY_CLASSES`` has
# been importable from this module since athenaeum#424 and consumers
# (merge_type_gate, reasoning_tiers, axiom_governance, _lint) import it that
# way — moving it must not break them.
from athenaeum.memory_class import (  # noqa: F401 — re-exported, see above
    MACHINE_ASSIGNABLE_MEMORY_CLASSES,
    MEMORY_CLASSES,
    TYPE_TO_MEMORY_CLASS,
    memory_class_for_type,
)
from athenaeum.pii import CONTACT_FRONTMATTER_FIELDS, is_pii_flagged
from athenaeum.provenance import validate_field_sources, validate_source_value


class WikiBase(BaseModel):
    """Base model for any wiki frontmatter. Open by design.

    Required: uid, type, name. Everything else passes through via
    ``extra="allow"`` so custom-namespace fields survive round-trip.

    Provenance (issue athenaeum#90):
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

    # Per-claim provenance (issue athenaeum#90). Stored as the on-disk shape
    # (str OR dict) — round-trip fidelity beats normalization here.
    source: str | dict | None = None
    # ``field_sources`` per-field value is one of:
    # - ``str``/``dict`` (legacy single source for the whole field), or
    # - ``list[dict]`` of ``{"value", "source"}`` records (per-value
    #   attribution for list-typed fields, issue athenaeum#102).
    field_sources: dict[str, str | dict | list] | None = None

    # Issue athenaeum#424: the memory-taxonomy axis, layered on top of ``type:``.
    # ``None`` (absent) is tolerated — legacy/untyped pages must not break —
    # see :func:`is_untyped_memory_class`. A non-``None`` value outside
    # :data:`MEMORY_CLASSES` is flagged via ``UserWarning`` in
    # ``_validate_memory_class`` below (NOT silently accepted) but does not
    # raise, matching the athenaeum#93 ``KNOWN_TYPES`` precedent this axis is layered
    # beside.
    memory_class: str | None = None

    # Issue athenaeum#424 (staleness axis): standing-state facts carry ``observed_at``
    # — the date the fact was TRUE-WHEN-OBSERVED, as distinct from
    # ``created``/``updated`` (write-time bookkeeping) and from
    # ``valid_from``/``valid_until`` (the claim-validity window, athenaeum#308).
    # Declared as an explicit field (rather than relying solely on
    # ``extra="allow"``) so it is a first-class, documented part of the
    # schema; stored as the on-disk scalar (str) for round-trip fidelity —
    # no date coercion here, mirroring how ``source``/``field_sources``
    # keep their on-disk shape rather than normalizing to a Python type.
    observed_at: str | None = None

    # Issue athenaeum#434 (context scoping): an axiom (or any memory_class value) may
    # carry a SCOPE narrowing where it should be treated as authoritative —
    # e.g. "applies to resume work" is axiomatic there, noise elsewhere.
    # Stored as the on-disk scalar (str), same round-trip-fidelity discipline
    # as ``observed_at``/``source`` — no normalization, no enum. ENFORCEMENT
    # (a consumer deciding whether the current context matches the scope) is
    # explicitly out of scope for athenaeum#434; this field only stores and surfaces
    # it. Not restricted to axioms at the schema level — a ``fact`` or
    # ``guideline`` scoped to a context is equally legible — but athenaeum#434's
    # governance (promotion/demotion/ledger) only concerns ``axiom``.
    scope: str | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def _validate_scope(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return str(v)

    # Issue athenaeum#714 (dimension registry): four NEW fields, read-side
    # counterparts to the write-side fields added to ``models.WikiEntity``.
    # None collide with any existing key — see ``athenaeum/dimensions.py``'s
    # module docstring for why the kernel ``scope`` dimension's coordinate is
    # ``claimed_scope`` rather than reusing THIS class's existing ``scope``
    # field (which a different subsystem, ``scoped_claims.py``/athenaeum#329,
    # already reads as an incompatible nested ``{org, locale}`` shape).
    recorded_at: str | None = None
    provenance_scope: str | None = None
    claimed_scope: str | None = None
    subject: str | None = None

    @field_validator("recorded_at", "provenance_scope", "claimed_scope", "subject", mode="before")
    @classmethod
    def _validate_dimension_coordinate_str(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        return str(v)

    @model_validator(mode="after")
    def _validate_intake_temporal(self) -> "WikiBase":
        """Enforce the athenaeum#714 intake temporal-validation AC at the schema
        boundary (the same choke point every other intake path already
        validates through — see :func:`athenaeum.intake.tier0_passthrough`'s
        ``validate_wiki_meta`` call).

        ``recorded_at`` is a NEW field (see above), so most frontmatter this
        validates today carries none — and this validator does NOT only run
        at intake: ``validate_wiki_meta`` is also the gate on read/merge
        paths over pages already on disk (``librarian.merge``,
        ``corrections``, ``batch``). Rejecting a page that simply lacks the
        new coordinate would therefore break existing data, and would break
        it on the very paths that could repair it (``corrections`` re-
        validates a merged read). It would also contradict athenaeum#714's own
        "a claim missing a coordinate is not rejected" AC.

        So the hard reject
        (:class:`athenaeum.dimensions.ObservedAfterRecordedError`, a
        :class:`ValueError` subclass pydantic wraps into
        :class:`pydantic.ValidationError`) fires here only when the page
        carries a REAL ``recorded_at`` anchor of its own. A page without one
        gets a soft :class:`UserWarning` instead — the signal is kept, not
        dropped. The AC's intake-side rejection is enforced where the AC
        scopes it, at the intake boundary itself: see
        :func:`athenaeum.intake.tier0_passthrough`, which passes an explicit
        now-anchor to :func:`athenaeum.dimensions.validate_intake_temporal`.
        Deep back-dates soft-flag in both cases. All tested — see
        ``tests/test_dimensions.py``.
        """
        from athenaeum.dimensions import validate_intake_temporal

        validate_intake_temporal(observed_at=self.observed_at, recorded_at=self.recorded_at)
        return self

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

    @model_validator(mode="after")
    def _warn_inline_contact_fields(self) -> "WikiBase":
        """Flag durable-identifier-only entity pages carrying inline PII (athenaeum#427).

        Recoverable — a :class:`UserWarning`, not a raise — mirroring the athenaeum#93
        ``KNOWN_TYPES`` / athenaeum#424 ``memory_class`` precedent. Skips the check
        entirely when the page is ``pii: true``-flagged: that flag is the
        explicit acknowledgment every corpus consumer already keys off of
        (:func:`athenaeum.pii.is_pii_flagged`), so warning here too would be
        noise, not signal. Frontmatter-only (the ``emails``/``phones`` fields)
        — inline body text is checked separately by
        :func:`athenaeum.pii.lint_inline_contact_fields`, which has access to
        the page body this schema-boundary validator does not.
        """
        meta = self.model_dump(exclude_none=True)
        if is_pii_flagged(meta):
            return self
        present = [f for f in CONTACT_FRONTMATTER_FIELDS if meta.get(f)]
        if present:
            warnings.warn(
                f"inline contact data on entity page: frontmatter field(s) "
                f"{sorted(present)!r} (durable identifiers only — see athenaeum#427)",
                UserWarning,
                stacklevel=2,
            )
        return self

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
# tree as of 2026-05-09 (issue athenaeum#93 audit). These fall through to
# :class:`WikiBase` for validation; the allowlist exists so unknown
# types (typos, drift) emit a warning instead of being silently
# accepted. See issue athenaeum#93.
#
# Issue athenaeum#971 (follow-up to the ``_schema/types.md`` reconciliation in
# athenaeum#970): ``incident`` added — the 10th declared type per athenaeum#970's audit, absent
# here meant every incident page warned as "unknown". ``user`` and
# ``feedback`` REMOVED — athenaeum#970 folds them (``user`` -> ``preference``); they
# stay non-raising (fall through to the ordinary "unknown wiki type"
# :class:`UserWarning` below, same as any other out-of-registry value, never
# an exception — a page in the wild with ``type: user`` keeps validating) but
# no longer count as a currently-valid type for a NEW write, which is what
# lets :func:`athenaeum.corrections.process_correction_record` gate a create
# against the fold (see that module's ``valid_types`` check).
FALLBACK_TYPES: frozenset[str] = frozenset(
    {
        "auto-memory",
        "tool",
        "reference",
        "principle",
        "preference",
        "incident",
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

    Issue athenaeum#93: emits a :class:`UserWarning` (NOT an exception) when
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

    Issue athenaeum#424: an absent ``memory_class`` is TOLERATED by validation (a
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
