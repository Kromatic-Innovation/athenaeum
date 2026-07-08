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

# Note: the legacy single-token (bare-slug) ``source:`` form was retired
# after issue #97 migrated 15,403 live-tree wikis from `<slug>` to
# `script:<slug>` on 2026-05-09 via
# ``athenaeum repair --legacy-source-slugs --apply``. New wikis MUST use
# the typed ``<type>:<ref>`` form. The migration tool itself
# (`repair.migrate_legacy_source_slugs`) keeps its own internal slug
# regex and ships unchanged for any future tree that needs it.


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
        # Legacy bare-slug form retired post-#97 migration. Callers that
        # still emit `source: <slug>` must switch to the typed
        # `<type>:<ref>` form (e.g. `script:<slug>`).
        raise ValueError(
            f"source scalar must be typed '<type>:<ref>' (e.g. "
            f"'script:extended-tier-build'); legacy bare-slug form retired "
            f"in #97, got {value!r}"
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


# Wrapper keys accepted by :func:`resolve_remember_sources` /
# :func:`resolve_remember_extras`. The originals (`_source`, `_field_sources`)
# hit the SourceRef surface; the extras (issue #326) inject frontmatter
# keys on the raw file that carry channel/model/asserter provenance beside
# the SourceRef.
_REMEMBER_WIKI_SOURCE_KEYS = frozenset({"_source", "_field_sources"})
_REMEMBER_EXTRA_KEYS = frozenset(
    {"_source_type", "_source_ref", "_model", "_on_behalf_of", "_asserter"}
)
_REMEMBER_ALLOWED_KEYS = _REMEMBER_WIKI_SOURCE_KEYS | _REMEMBER_EXTRA_KEYS

# Maps a wrapper key (e.g. ``"_source_type"``) to the frontmatter key it
# writes (e.g. ``"source_type"``). Extras are injected under these names
# by :func:`athenaeum.mcp_server._inject_provenance_frontmatter` so the
# read-side parsers (:func:`athenaeum.models.parse_asserter` etc.) find
# them.
_REMEMBER_EXTRA_KEY_MAP: dict[str, str] = {
    "_source_type": "source_type",
    "_source_ref": "source_ref",
    "_model": "model",
    "_on_behalf_of": "on_behalf_of",
    "_asserter": "asserter",
}


def _reject_bad_remember_dict(keys: set[str]) -> None:
    """Raise the standard ValueError for a malformed remember(sources=...) dict."""
    raise ValueError(
        "MCP remember(sources=...) bare-dict shape removed. "
        'Use {"_source": ...} for wiki-level, '
        '{"_field_sources": {<field>: <source>}} for per-field, or '
        "one of the channel-split extras "
        '(`_source_type`, `_source_ref`, `_model`, `_on_behalf_of`, `_asserter`). '
        "See docs/provenance-shape.md §4 / §10. "
        f"Got keys: {sorted(keys)!r}"
    )


def resolve_remember_sources(
    sources: Any,
) -> tuple[Any, dict | None]:
    """Disambiguate the ``remember(sources=...)`` argument shape.

    Settles design-lock §4 (``docs/provenance-shape.md``): the bare-dict
    heuristic that previously inspected ``{type, ref}`` keys to guess
    "structured single-source" vs "per-field map" is REMOVED. Callers
    must use explicit wrapper keys for structured input.

    Accepted shapes:

    - ``None`` → ``(None, None)``.
    - ``str`` (e.g. ``"api:apollo:2026-05-09"``) → wiki-level scalar;
      returns ``(<scalar>, None)``.
    - ``dict`` containing any combination of the following wrapper keys:

      * ``_source`` — wiki-level scalar/structured SourceRef.
      * ``_field_sources`` — per-field ``{<field>: <source>}`` map.
      * ``_source_type`` / ``_source_ref`` — origin-traced provenance
        (issue #260 channel classification + ultimate reference).
      * ``_model`` / ``_on_behalf_of`` / ``_asserter`` — channel-split
        extras (issue #326; see :func:`resolve_remember_extras`).

    Any bare dict (no wrapper keys) or unknown key raises
    :class:`ValueError`. Any other type raises :class:`TypeError`.

    Returns:
        ``(wiki_source, field_sources_map)`` — either entry may be
        ``None``. ``wiki_source`` is the validated scalar/structured
        source; ``field_sources_map`` is the validated per-field dict.

    Note: the channel-split extras validated here are NOT returned in
    this tuple — callers wanting them use :func:`resolve_remember_extras`
    on the same input. Split from this function to preserve the pre-#326
    two-tuple return.
    """
    if sources is None:
        return None, None
    if isinstance(sources, str):
        validate_source_value(sources)
        return sources, None
    if isinstance(sources, dict):
        keys = set(sources.keys())
        unknown = keys - _REMEMBER_ALLOWED_KEYS
        if unknown or not keys:
            _reject_bad_remember_dict(keys)
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
        # Validate extras here too so a malformed asserter is caught at
        # the boundary. The values themselves are returned by
        # :func:`resolve_remember_extras`; this call is validation-only.
        _validate_remember_extras(sources)
        return wiki_source, field_sources_map
    raise TypeError(
        f"`sources` must be str, dict, or None; got {type(sources).__name__}. "
        'Use {"_source": ...}, {"_field_sources": {...}}, or the channel-split '
        "extras (`_source_type`/`_source_ref`/`_model`/`_on_behalf_of`/`_asserter`) "
        "for structured input. See docs/provenance-shape.md §4 / §10."
    )


def _validate_remember_extras(sources: dict[str, Any]) -> None:
    """Validate the channel-split extras on a ``remember(sources=...)`` dict.

    Fail-open at the schema layer (matches ``coerce_source_type``): a
    non-string ``_source_type`` is passed through — read-side
    :func:`athenaeum.models.coerce_source_type` will downgrade it to
    ``inferred``. The tight-schema check is on ``_asserter`` which must
    be a dict when present (or ``None`` to explicitly clear).
    """
    asserter = sources.get("_asserter")
    if asserter is not None and not isinstance(asserter, dict):
        raise ValueError(
            f"_asserter must be a dict, got {type(asserter).__name__}"
        )


def resolve_remember_extras(sources: Any) -> dict[str, Any]:
    """Return the channel-split extras to inject as frontmatter (issue #326).

    Extracts the ``_source_type`` / ``_source_ref`` / ``_model`` /
    ``_on_behalf_of`` / ``_asserter`` wrapper keys from a
    ``remember(sources=...)`` dict and returns them keyed by the
    frontmatter names they write into (``source_type``, ``source_ref``,
    ``model``, ``on_behalf_of``, ``asserter``).

    - ``sources is None`` → ``{}``.
    - ``sources`` is a bare scalar (str) → ``{}`` — the scalar
      shorthand is a SourceRef, not an extras carrier.
    - ``sources`` is a dict → the extras subset, keyed for frontmatter
      injection.

    Malformed values raise :class:`ValueError` — same discipline as
    :func:`resolve_remember_sources`. Callers who want to be robust
    against a malformed extras block should catch and log; the MCP
    server surfaces the error to the caller.
    """
    if sources is None or isinstance(sources, str):
        return {}
    if not isinstance(sources, dict):
        raise TypeError(
            f"`sources` must be str, dict, or None; got {type(sources).__name__}"
        )
    # Re-validate here so a caller that skips resolve_remember_sources
    # still gets the asserter-must-be-dict check.
    _validate_remember_extras(sources)
    extras: dict[str, Any] = {}
    for wrapper_key, fm_key in _REMEMBER_EXTRA_KEY_MAP.items():
        if wrapper_key in sources:
            extras[fm_key] = sources[wrapper_key]
    return extras


__all__ = [
    "SourceRef",
    "parse_source",
    "parse_per_value_field_sources",
    "validate_source_value",
    "validate_field_sources",
    "resolve_remember_sources",
    "resolve_remember_extras",
]
