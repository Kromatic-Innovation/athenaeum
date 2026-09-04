# SPDX-License-Identifier: Apache-2.0
"""Versioned schema for the sidecar context envelope (issue athenaeum#1359).

:func:`athenaeum.context.build_context` returns a plain dict — "the
envelope". This module is that dict's SCHEMA, pinned and versioned
separately from the core that produces it (:mod:`athenaeum.context`,
issue athenaeum#1358), so a schema change is a deliberate, visible act (bump
:data:`SCHEMA_VERSION`, add a migration note below) rather than a silent
drift between what the core emits and what an adapter was written against.

See ``docs/sidecar-adapter-contract.md`` for the full contract this
schema is one half of (the other half is the may/may-not rules for what
an adapter may do with the envelope).

Layering: L3 service, same tier as :mod:`athenaeum.context`. Deliberately
has NO import of :mod:`athenaeum.context` itself (this module only
describes the shape of that module's output; it does not call it), so a
schema-only consumer (a test, or a future non-Claude adapter that wants to
validate a live envelope) never pays the FTS5-core's own import cost.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Field contract, v1
# ---------------------------------------------------------------------------
#
# `required` fields are the ones a caller may depend on being present, typed,
# and stable across a schema VERSION (not across every value — `candidates`
# is naturally empty on a no-hit turn). Adding a field here is a public
# contract change: bump SCHEMA_VERSION and add a row to MIGRATIONS below,
# even for an additive change — issue athenaeum#1359's own counter-example
# is "adding a required field without bumping v".
#
# `diagnostic` fields (currently just `elapsed_ms`) are real output, but are
# explicitly OUTSIDE the versioned contract: their value is non-reproducible
# by nature (wall-clock timing) and no adapter may depend on their presence,
# type, or meaning staying stable. They exist for local debugging only. A
# schema-conformance check validates required fields' types; it does not —
# and structurally cannot — pin a diagnostic field's value, so it is kept
# out of `REQUIRED_FIELDS` rather than given a false stability guarantee.

REQUIRED_FIELDS: dict[str, type] = {
    "v": int,
    "query": str,
    "session_id": str,
    "candidates": list,
    "budget": dict,
    "render": dict,
    "backend": str,
}

DIAGNOSTIC_FIELDS: dict[str, type] = {
    "elapsed_ms": float,
}

CANDIDATE_REQUIRED_FIELDS: dict[str, type] = {
    "filename": str,
    "name": str,
    "description": str,
    "backend": str,
    # `relevance` is `float | None` (None for a vector-backend hit — see
    # athenaeum.context.Candidate's own docstring on why a vector
    # similarity score is never recorded as `relevance`) — checked
    # separately below rather than via this type map.
    "memory_tier": str,
    "audience": str,
    "token_cost": int,
}

BUDGET_REQUIRED_FIELDS: dict[str, type] = {
    "tokens": int,
    "used": int,
}

RENDER_REQUIRED_FIELDS: dict[str, type] = {
    "text": str,
    "preamble": str,
}

# ---------------------------------------------------------------------------
# Migration log
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, str] = {
    1: "Initial version. Fields: v, query, session_id, candidates[], budget, "
    "render, backend (required); elapsed_ms (diagnostic, unversioned).",
}

# A hand-maintained SNAPSHOT of each version's required field sets —
# top-level AND the three nested objects (candidates[] entries, budget,
# render) — deliberately NOT derived from the REQUIRED_FIELDS dicts above
# at runtime (`frozenset(REQUIRED_FIELDS)` would just mirror whatever
# REQUIRED_FIELDS currently says, which can never disagree with itself and
# so could never catch anything). This is the mechanism
# `test_sidecar_envelope_schema.py` uses to enforce the issue's own
# counter-example — "adding a required field without bumping v" — for EVERY
# level of the envelope, not just the top level: add a field to any of the
# four REQUIRED_FIELDS dicts above without adding it to the matching entry
# here (and bumping SCHEMA_VERSION + MIGRATIONS), and
# `test_schema_history_matches_current_required_fields` fails immediately —
# the two must be edited together, on purpose, every time.
SCHEMA_HISTORY: dict[int, dict[str, frozenset[str]]] = {
    1: {
        "envelope": frozenset(
            {"v", "query", "session_id", "candidates", "budget", "render", "backend"}
        ),
        "candidate": frozenset(
            {
                "filename",
                "name",
                "description",
                "backend",
                "relevance",
                "memory_tier",
                "audience",
                "token_cost",
            }
        ),
        "budget": frozenset({"tokens", "used"}),
        "render": frozenset({"text", "preamble"}),
    },
}


class EnvelopeValidationError(ValueError):
    """Raised by :func:`validate_envelope` with the specific field/type that
    failed, so a caller integrating against this schema gets an actionable
    error rather than a bare KeyError/TypeError."""


def _check_fields(obj: dict[str, Any], required: dict[str, type], where: str) -> list[str]:
    errors = []
    for field, typ in required.items():
        if field not in obj:
            errors.append(f"{where}: missing required field {field!r}")
        elif not isinstance(obj[field], typ):
            errors.append(
                f"{where}: field {field!r} has type {type(obj[field]).__name__}, "
                f"expected {typ.__name__}"
            )
    return errors


def validate_envelope(envelope: dict[str, Any]) -> None:
    """Validate *envelope* against :data:`SCHEMA_VERSION`'s required-field
    contract. Raises :class:`EnvelopeValidationError` listing every
    violation found (not just the first), so a schema-drift failure is
    diagnosable in one pass.

    This checks REQUIRED fields only — it does not reject an envelope for
    carrying an extra, undocumented field (that is a looser check than "no
    extra fields allowed"), because :data:`DIAGNOSTIC_FIELDS` and a future
    adapter-specific extension both legitimately add fields the schema
    does not enumerate. What it DOES enforce is the inverse: every
    required field must be present with the right type, on every
    envelope this schema version claims to describe.
    """
    errors = _check_fields(envelope, REQUIRED_FIELDS, "envelope")

    if envelope.get("v") != SCHEMA_VERSION:
        errors.append(f"envelope: v={envelope.get('v')!r}, expected {SCHEMA_VERSION!r}")

    budget = envelope.get("budget")
    if isinstance(budget, dict):
        errors.extend(_check_fields(budget, BUDGET_REQUIRED_FIELDS, "envelope.budget"))

    render = envelope.get("render")
    if isinstance(render, dict):
        errors.extend(_check_fields(render, RENDER_REQUIRED_FIELDS, "envelope.render"))

    candidates = envelope.get("candidates")
    if isinstance(candidates, list):
        for i, candidate in enumerate(candidates):
            where = f"envelope.candidates[{i}]"
            if not isinstance(candidate, dict):
                errors.append(f"{where}: expected an object, got {type(candidate).__name__}")
                continue
            errors.extend(_check_fields(candidate, CANDIDATE_REQUIRED_FIELDS, where))
            if "relevance" in candidate and not isinstance(
                candidate["relevance"], (float, int, type(None))
            ):
                errors.append(
                    f"{where}: field 'relevance' has type "
                    f"{type(candidate['relevance']).__name__}, expected float | None"
                )
            elif "relevance" not in candidate:
                errors.append(f"{where}: missing required field 'relevance'")

    if errors:
        raise EnvelopeValidationError("; ".join(errors))
