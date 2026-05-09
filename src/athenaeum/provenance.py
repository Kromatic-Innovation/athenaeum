# SPDX-License-Identifier: Apache-2.0
"""Per-claim provenance primitives.

Every CLAIM in a wiki page (a frontmatter field's value, or — eventually —
a body assertion) should be traceable to a SOURCE. This module defines:

- :class:`SourceRef` — structured source descriptor.
- :func:`parse_source` — accept either the scalar shorthand
  ``"<type>:<ref>"`` or the structured form
  ``{"type": ..., "ref": ..., "ts": ..., "confidence": ..., "notes": ...}``.
- :func:`validate_source_value` — schema-side validator helper used by
  :class:`athenaeum.schemas.WikiBase` to gate the ``source`` and
  ``field_sources`` frontmatter keys.

Format contract (issue #90):

- Scalar: ``"<type>:<ref>"``. ``type`` is ``[a-z][a-z0-9_-]*``, ``ref`` is
  any non-empty string with no embedded newlines. Examples:
  ``"api:apollo:2026-05-07"``, ``"claude:session-2026-05-08"``,
  ``"linkedin:nicole-segerer-5209921b"``.
- Structured: a dict with required ``type`` + ``ref`` and optional ``ts``
  (ISO-8601 string), ``confidence`` (float in [0, 1]), ``notes`` (free
  text). Extra keys are rejected so we don't silently absorb typos.

The wiki-level ``source`` is the default for any field whose key is not
present in ``field_sources``. ``field_sources`` is a dict
``{<field_name>: <scalar-or-structured>}`` of per-claim overrides.

Conflict resolution behavior (which source wins on update) is NOT defined
here — that is Lane G / #91. This module only parses, validates, and
round-trips.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# ``<type>:<ref>``. Type segment is restrictive (lowercase + dash + digit
# + underscore); ref is permissive but cannot contain newlines and cannot
# be empty. We anchor with ``\Z`` so a trailing newline doesn't sneak in.
_SCALAR_RE = re.compile(r"^([a-z][a-z0-9_-]*):([^\n]+)\Z")

# Legacy single-token form (no colon) — for pre-#90 wikis whose ``source:``
# is a bare slug like ``extended-tier-build`` or ``warm-network-detect``.
# ~15k live wikis use this shape; the validator MUST accept them so the
# schema doesn't break the live tree. New wikis SHOULD use the typed
# ``<type>:<ref>`` form above. Migration of legacy → typed is tracked
# separately in issue #97; once complete, this regex + branch can go.
_LEGACY_SCALAR_RE = re.compile(r"^[a-z][a-z0-9_-]*\Z")


class SourceRef(BaseModel):
    """Structured source descriptor for a CLAIM.

    Required: ``type`` + ``ref``. Optional: ``ts`` (when the source was
    observed), ``confidence`` (0..1, source-quality estimate),
    ``notes`` (free text).
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    ref: str
    ts: str | None = None
    confidence: float | None = None
    notes: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _validate_type(cls, v: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source type must be a non-empty string")
        if not re.match(r"^[a-z][a-z0-9_-]*\Z", v):
            raise ValueError(f"source type must match [a-z][a-z0-9_-]*, got {v!r}")
        return v

    @field_validator("ref", mode="before")
    @classmethod
    def _validate_ref(cls, v: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source ref must be a non-empty string")
        if "\n" in v:
            raise ValueError("source ref must not contain newlines")
        return v

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        return v

    def to_scalar(self) -> str:
        """Render as the scalar shorthand ``<type>:<ref>``.

        Lossy when ``ts``/``confidence``/``notes`` are set — callers that
        need round-trip fidelity should keep the structured form.
        """
        return f"{self.type}:{self.ref}"


def parse_source(value: Any) -> SourceRef | None:
    """Parse a scalar or structured source value into a :class:`SourceRef`.

    Accepts:

    - ``None`` → returns ``None`` (no source attached).
    - ``str`` of form ``"<type>:<ref>"`` → split and validate.
    - ``dict`` with ``type``/``ref`` (+ optional fields) → validate.

    Raises :class:`ValueError` on malformed input (bad scalar shape,
    missing required keys, unknown extra keys, out-of-range confidence).
    """
    if value is None:
        return None
    if isinstance(value, SourceRef):
        return value
    if isinstance(value, str):
        if value == "" or value != value.strip():
            # Empty / leading / trailing whitespace = corruption signal,
            # not normal input. Reject loudly.
            raise ValueError(
                f"source scalar must be non-empty and trimmed, got {value!r}"
            )
        m = _SCALAR_RE.match(value)
        if m:
            ref_part = m.group(2)
            if ref_part != ref_part.strip():
                raise ValueError(
                    f"source ref must not have leading/trailing whitespace, got {value!r}"
                )
            return SourceRef(type=m.group(1), ref=ref_part)
        # Legacy single-token form (pre-#90 wikis). Accept but don't
        # synthesize a structured SourceRef — the on-disk shape stays the
        # bare slug; we model it as type="legacy", ref=<slug> for callers
        # that need a SourceRef object. Validator preserves on-disk shape
        # via validate_source_value (returns value unchanged).
        if _LEGACY_SCALAR_RE.match(value):
            return SourceRef(type="legacy", ref=value)
        raise ValueError(
            f"source scalar must match '<type>:<ref>' or legacy '[a-z][a-z0-9_-]*', got {value!r}"
        )
    if isinstance(value, dict):
        return SourceRef.model_validate(value)
    raise ValueError(f"source must be str, dict, or None; got {type(value).__name__}")


def validate_source_value(value: Any) -> Any:
    """Validate a frontmatter ``source`` value, return the original shape.

    Used by :class:`athenaeum.schemas.WikiBase` validators. Round-trip
    fidelity matters here: we MUST NOT replace the on-disk scalar with a
    structured dict (or vice versa) just because we parsed it. Parsing
    raises on malformed input; on success we return ``value`` unchanged
    so :func:`render_frontmatter` re-emits the same bytes.
    """
    if value is None:
        return None
    parse_source(value)  # raises on malformed
    return value


def _is_per_value_list(value: Any) -> bool:
    """Return True if ``value`` is the per-value list-of-records shape.

    Per-value shape (issue #102, design lock §2.1):

        [{"value": <any>, "source": <str|dict>}, ...]

    A non-list, or a list that doesn't look like records of that shape,
    is legacy. Empty list counts as per-value (vacuously valid — and
    distinguishes intent from an absent value).
    """
    if not isinstance(value, list):
        return False
    if not value:
        return True
    for entry in value:
        if not isinstance(entry, dict):
            return False
        if "value" not in entry or "source" not in entry:
            return False
    return True


def parse_per_value_field_sources(value: Any) -> list[dict[str, Any]]:
    """Parse a per-value ``field_sources.<list_field>`` entry.

    Accepts a list of ``{"value": <any>, "source": <str|dict>}`` records.
    Each record's ``source`` must validate via :func:`parse_source`;
    ``value`` is type-unconstrained (matches the underlying list field's
    element type). Extra keys on a record are rejected.

    Returns the original list unchanged. Raises :class:`ValueError` on
    malformed input.

    Co-indexing alignment between this list and the underlying field's
    list is NOT enforced here (per design-lock §2.4 — stale entries are
    pruned at write time, not at validation).
    """
    if not isinstance(value, list):
        raise ValueError(
            f"per-value field_sources must be a list, got {type(value).__name__}"
        )
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(
                f"per-value field_sources[{i}] must be a dict, got {type(entry).__name__}"
            )
        if "value" not in entry:
            raise ValueError(f"per-value field_sources[{i}] missing 'value' key")
        if "source" not in entry:
            raise ValueError(f"per-value field_sources[{i}] missing 'source' key")
        extra = set(entry) - {"value", "source"}
        if extra:
            raise ValueError(
                f"per-value field_sources[{i}] has unknown keys: {sorted(extra)!r}"
            )
        parse_source(entry["source"])  # raises on malformed
    return value


def validate_field_sources(value: Any) -> Any:
    """Validate a frontmatter ``field_sources`` value.

    Must be a dict with string keys. Each value is one of:

    - ``str`` or ``dict`` → legacy single-source-for-the-whole-field,
      validated via :func:`parse_source`.
    - ``list`` of ``{"value", "source"}`` records → per-value
      attribution (issue #102), validated via
      :func:`parse_per_value_field_sources`.

    Returns the original shape unchanged on success.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"field_sources must be a dict, got {type(value).__name__}")
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"field_sources keys must be non-empty strings, got {k!r}")
        if isinstance(v, list):
            parse_per_value_field_sources(v)
        else:
            parse_source(v)  # raises on malformed
    return value


def resolve_remember_sources(
    sources: Any,
) -> tuple[Any, dict | None]:
    """Disambiguate the ``remember(sources=...)`` argument shape.

    Settles design-lock §4 (``docs/provenance-shape.md``): the bare-dict
    heuristic that previously inspected ``{type, ref}`` keys to guess
    "structured single-source" vs "per-field map" is REMOVED. Callers
    must use explicit wrapper keys for structured input.

    Three accepted shapes:

    - ``None`` → ``(None, None)``.
    - ``str`` (e.g. ``"api:apollo:2026-05-09"``) → wiki-level scalar;
      returns ``(<scalar>, None)``.
    - ``dict`` containing only ``_source`` and/or ``_field_sources`` →
      returns ``(<wiki_source>, <field_sources_map>)``.

    Any bare dict (no wrapper keys) raises :class:`ValueError` directing
    the caller to wrap. Any other type raises :class:`TypeError`.

    Returns:
        ``(wiki_source, field_sources_map)`` — either entry may be
        ``None``. ``wiki_source`` is the validated scalar/structured
        source; ``field_sources_map`` is the validated per-field dict.
    """
    if sources is None:
        return None, None
    if isinstance(sources, str):
        validate_source_value(sources)
        return sources, None
    if isinstance(sources, dict):
        allowed = {"_source", "_field_sources"}
        keys = set(sources.keys())
        unknown = keys - allowed
        if unknown or not keys:
            raise ValueError(
                "MCP remember(sources=...) bare-dict shape removed. "
                'Use {"_source": ...} for wiki-level or '
                '{"_field_sources": {<field>: <source>}} for per-field. '
                "See docs/provenance-shape.md §4."
            )
        wiki_source: Any = None
        field_sources_map: dict | None = None
        if "_source" in sources:
            wiki_source = sources["_source"]
            validate_source_value(wiki_source)
        if "_field_sources" in sources:
            fs = sources["_field_sources"]
            if not isinstance(fs, dict):
                raise ValueError(
                    f"_field_sources must be a dict, got {type(fs).__name__}"
                )
            validate_field_sources(fs)
            field_sources_map = fs
        return wiki_source, field_sources_map
    raise TypeError(
        f"`sources` must be str, dict, or None; got {type(sources).__name__}. "
        'Use {"_source": ...} or {"_field_sources": {...}} for structured input. '
        "See docs/provenance-shape.md §4."
    )


__all__ = [
    "SourceRef",
    "parse_source",
    "parse_per_value_field_sources",
    "validate_source_value",
    "validate_field_sources",
    "resolve_remember_sources",
]
