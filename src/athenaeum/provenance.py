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
        m = _SCALAR_RE.match(value)
        if not m:
            raise ValueError(f"source scalar must match '<type>:<ref>', got {value!r}")
        return SourceRef(type=m.group(1), ref=m.group(2))
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


def validate_field_sources(value: Any) -> Any:
    """Validate a frontmatter ``field_sources`` value.

    Must be a dict with string keys; each value validates as a source.
    Returns the original shape unchanged on success.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"field_sources must be a dict, got {type(value).__name__}")
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"field_sources keys must be non-empty strings, got {k!r}")
        parse_source(v)  # raises on malformed
    return value


__all__ = [
    "SourceRef",
    "parse_source",
    "validate_source_value",
    "validate_field_sources",
]
