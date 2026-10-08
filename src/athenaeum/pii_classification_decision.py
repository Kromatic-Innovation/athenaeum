# SPDX-License-Identifier: Apache-2.0
"""Framed-decision contract for a PII classification verdict (issue athenaeum#689).

Issue athenaeum#689's policy document (``docs/design/pii-classification-policy.md``)
is the domain judgment that separates a non-contact token (an SSH host
alias, a calendar id, a page that exists to hold addresses, a test/role
account) from a genuine personal address. This module is that judgment's
**wire format**: the response schema a valid answer must satisfy, and the
minimal per-item context a human or agent needs to apply the policy,
expressed in exactly the shape issue athenaeum#717's framed-decision
contract (:mod:`athenaeum.decision_framing`) already defines for every
other decision type (``response_schema``, and a context-bundle builder
matching :func:`athenaeum.decision_framing.build_context_bundle`'s
contract).

**Scoped 2026-09-03 (Occam ruling), reaffirmed by the 2026-10-07
occam:disposition on athenaeum#689: this module discharges AC2 by
AUTHORING the schema, not by wiring it.** Nothing here is imported by
:mod:`athenaeum.decision_framing`, :mod:`athenaeum.decisions`, or any
queue path — ``"pii-classification"`` is deliberately **absent** from
:data:`athenaeum.decision_framing._TYPE_FRAMING`, so `athenaeum run`
behaviour is byte-identical with or without this module present. Issue
athenaeum#717 adopts this module when it lands, by registering a
``_TYPE_FRAMING["pii-classification"]`` entry whose ``"schema"`` key is
:data:`RESPONSE_SCHEMA` — a one-line registration, not a rewrite, because
the shape below already matches every existing entry's ``schema`` value
and the same ``verdict``-is-a-plain-string constraint
:mod:`athenaeum.decision_framing`'s coordinate-schema docstring documents
(the inbound applier only ever forwards ``str(parsed["verdict"])``).

Layering: L4 domain/pipeline module, same tier as :mod:`athenaeum.decision_framing`.
Imports only stdlib; ``jsonschema`` is imported lazily inside
:func:`validate_answer`, mirroring :mod:`athenaeum.decision_framing`'s own
lazy import so a caller that never validates an answer never pays for the
dependency. Owns no storage and mutates nothing — this module is a pure
description of a shape.
"""

from __future__ import annotations

import copy
from typing import Any

#: The decision-type tag this schema is authored for. Not registered
#: anywhere in :mod:`athenaeum.decision_framing` (see module docstring) —
#: named here only so a future caller (issue athenaeum#717's wiring) has one
#: place to read the exact string to register under.
DECISION_TYPE = "pii-classification"

#: The five classes the policy document
#: (``docs/design/pii-classification-policy.md``) resolves a PII-shaped
#: token into. The first four are judged NOT PII; the fifth is PII. A
#: verdict names the class, not a bare true/false, so the answer carries
#: which rule applied — the detail :func:`athenaeum.pii_verdicts` records
#: as the justification basis.
CLASS_NOT_PII_ALIAS = "not-pii-alias"
CLASS_NOT_PII_CALENDAR_ID = "not-pii-calendar-id"
CLASS_NOT_PII_PAGE_PURPOSE = "not-pii-page-purpose"
CLASS_NOT_PII_TEST_ACCOUNT = "not-pii-test-account"
CLASS_IS_PII = "is-pii"

#: Every valid verdict value, in the policy document's own order.
PII_CLASSES: tuple[str, ...] = (
    CLASS_NOT_PII_ALIAS,
    CLASS_NOT_PII_CALENDAR_ID,
    CLASS_NOT_PII_PAGE_PURPOSE,
    CLASS_NOT_PII_TEST_ACCOUNT,
    CLASS_IS_PII,
)

#: Classes 1-4 of the policy document — judged NOT PII.
NOT_PII_CLASSES: frozenset[str] = frozenset(
    {
        CLASS_NOT_PII_ALIAS,
        CLASS_NOT_PII_CALENDAR_ID,
        CLASS_NOT_PII_PAGE_PURPOSE,
        CLASS_NOT_PII_TEST_ACCOUNT,
    }
)

#: Class 5 — judged PII. A singleton set (not a bare string) so callers can
#: use the same ``value in {NOT,IS}_PII_CLASSES`` shape for both checks.
IS_PII_CLASSES: frozenset[str] = frozenset({CLASS_IS_PII})


def is_not_pii_verdict(verdict_class: str) -> bool:
    """True when *verdict_class* is one of the four not-PII classes."""
    return verdict_class in NOT_PII_CLASSES


def is_pii_verdict(verdict_class: str) -> bool:
    """True when *verdict_class* is the is-PII class."""
    return verdict_class in IS_PII_CLASSES


#: The response schema a valid answer to a ``pii-classification`` item must
#: satisfy. Shaped exactly like every entry in
#: :mod:`athenaeum.decision_framing`'s ``_TYPE_FRAMING`` table: a plain
#: ``verdict`` string (never a nested object — see this module's docstring)
#: restricted to :data:`PII_CLASSES`, plus the existing free-text ``note``
#: property every other schema in that module also carries.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(PII_CLASSES),
            "description": (
                "Which of the five policy classes the value belongs to: "
                "not-pii-alias, not-pii-calendar-id, not-pii-page-purpose, "
                "not-pii-test-account, or is-pii."
            ),
        },
        "note": {
            "type": "string",
            "description": "Optional free text recorded with the answer.",
        },
    },
    "required": ["verdict"],
    "additionalProperties": False,
}


def response_schema() -> dict[str, Any]:
    """Deep copy of :data:`RESPONSE_SCHEMA`.

    Deep-copied on the way out for the same reason
    :func:`athenaeum.decision_framing.response_schema_for` copies its
    schemas: the object is shared module state, and a caller that mutates
    the dict it gets back (e.g. serializing then editing ``properties``)
    must not poison every later caller.
    """
    return copy.deepcopy(RESPONSE_SCHEMA)


def build_context_bundle(
    *,
    value: str,
    page_id: str,
    sentence: str,
    page_purpose: str | None = None,
) -> dict[str, Any]:
    """The minimal structured context a classifier needs to answer.

    Mirrors :func:`athenaeum.decision_framing.build_context_bundle`'s
    contract: structured data, never a free-text blob, and nothing beyond
    what the policy document's rules actually consult.

    ``value``
        The PII-shaped token being classified.
    ``page_id``
        The page the token was found on (a slug, same id space
        :func:`athenaeum.verdicts.page_id_for_path` uses).
    ``sentence``
        The sentence (or smallest surrounding context) the token appears
        in — sentence context is what the policy document's page-purpose
        rule and class distinctions turn on, never the token in isolation.
    ``page_purpose``
        The page's stated purpose if known (its frontmatter ``type``,
        title, or opening sentence) — the page-purpose rule's input.
        Omitted (``None``) when the page carries no such declared purpose.
    """
    bundle: dict[str, Any] = {
        "value": value,
        "page_id": page_id,
        "sentence": sentence,
    }
    if page_purpose is not None:
        bundle["page_purpose"] = page_purpose
    return bundle


def validate_answer(answer: dict[str, Any]) -> list[str]:
    """Validate *answer* against :data:`RESPONSE_SCHEMA`.

    Returns a list of human-readable validation errors — empty when the
    answer is valid. Mirrors
    :func:`athenaeum.decision_framing.validate_answer`'s contract exactly
    (same error-string shape: ``"<path>: <message>"``) so a future caller
    that already knows that contract needs nothing new to read these
    errors.
    """
    import jsonschema

    validator = jsonschema.Draft202012Validator(RESPONSE_SCHEMA)
    errors = sorted(validator.iter_errors(answer), key=lambda e: list(e.path))
    return [
        f"{'/'.join(str(p) for p in error.path) or '<answer>'}: {error.message}" for error in errors
    ]


__all__ = [
    "DECISION_TYPE",
    "CLASS_NOT_PII_ALIAS",
    "CLASS_NOT_PII_CALENDAR_ID",
    "CLASS_NOT_PII_PAGE_PURPOSE",
    "CLASS_NOT_PII_TEST_ACCOUNT",
    "CLASS_IS_PII",
    "PII_CLASSES",
    "NOT_PII_CLASSES",
    "IS_PII_CLASSES",
    "is_not_pii_verdict",
    "is_pii_verdict",
    "RESPONSE_SCHEMA",
    "response_schema",
    "build_context_bundle",
    "validate_answer",
]
