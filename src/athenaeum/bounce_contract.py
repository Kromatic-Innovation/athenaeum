# SPDX-License-Identifier: Apache-2.0
"""The Tier-0 bounce-note contract, and a read-only conformance check (issue athenaeum#854).

`librarian.tier0_bounce_mark` recognizes a hard-bounce fact in an ORDINARY
free-text raw-intake note and marks the address non-deliverable on the
contacts surface, LLM-free (issue athenaeum#765). The gate is deliberately narrow, and
a note that misses any of its conditions is **not rejected** — nothing is
rejected. It simply falls through to the Tier 1/2/3 reasoning path and is
compiled as an ordinary free-text memory, with no error and no bounce mark.

For one note that is correct and harmless. At backfill volume it is a
contamination vector: a producer emitting a few hundred near-miss notes
compiles a few hundred addresses-plus-diagnostics into the corpus as ordinary
prose. Until this module existed the required shape was discoverable only by
reading the gate and inferring it, which a producer in another repository
cannot depend on.

This module is that contract, expressed as code:

- :func:`check_tier0_bounce_conformance` answers "would Tier 0 recognize this
  note?" for a candidate note **without writing anything** — no mark, no
  intake submission, no store mutation, no network, no LLM call. It is a pure
  function of the note text.
- On a decline it reports **which** condition failed
  (:class:`BounceDecline`, one per unmet condition, ALL of them — a producer
  fixing a batch needs the full list, not the first failure), not a bare
  boolean.
- ``athenaeum bounce-contract`` (:mod:`athenaeum._cmd_bounce_contract`) is the
  same check as a CLI, which is the surface a producer in another language
  can actually call.

**Drift is prevented structurally, not by convention.**
:func:`librarian.tier0_bounce_mark` calls :func:`check_tier0_bounce_conformance`
for its whole recognition decision and does nothing but write the mark on top
of it, so the check cannot answer differently from the gate — they are the
same code path. The body predicates are the production ones
(:func:`athenaeum.sensitivity.classify`,
:func:`athenaeum.pii.find_hard_bounce_code`,
:func:`athenaeum.pii.detect_hard_bounce_fact`) called directly, never
re-derived here. The prose contract in ``docs/extending/tier0-bounce-note-contract.md``
is pinned to :data:`DECLINE_REASONS` by a test, so the documented reason list
cannot silently fall behind this module either.

**Migrated onto the sensitivity registry (issue athenaeum#992).** This module used
to import :func:`athenaeum.pii.find_inline_emails` directly — the note at
``docs/design/sensitivity-class-vocabulary.md`` §2.1/§9 originally omitted this
module from the S3 call-site inventory entirely. The email-identifier count
this module needs (exactly one, for :data:`NO_EMAIL_IDENTIFIER` /
:data:`SEVERAL_EMAIL_IDENTIFIERS`) is now obtained via
:func:`athenaeum.sensitivity.classify` with ``config=None`` — this module
takes no ``config`` parameter of its own (it is "a pure function of the note
text", per this docstring's opening line), so only the shipped ``pii`` class's
``email`` recogniser is ever consulted; a deployment's own
``sensitivity:`` config block is out of reach here, matching this module's
pre-existing no-config-surface contract. :func:`_conforming_emails` preserves
:func:`~athenaeum.pii.find_inline_emails`'s order-preserving dedup exactly, so
a body repeating the same address is still counted once — behaviour is
unchanged on every existing fixture.

This module adds **no** rejection path and changes no runtime behaviour on the
intake path: it makes the existing boundary legible and checkable in advance.

Layering: L2-ish — depends on the L1 :mod:`athenaeum.models` frontmatter
parser, the L2 :mod:`athenaeum.pii` predicates, and the L3
:mod:`athenaeum.sensitivity` registry, and on nothing above it, so the L5
librarian gate and the L5 CLI can both call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athenaeum.models import parse_frontmatter
from athenaeum.pii import (
    BounceVerdictFact,
    HardBounceFact,
    detect_bounce_verdict_fact,
    detect_hard_bounce_fact,
    find_bare_smtp_5xx_code,
    find_hard_bounce_code,
    find_verified_bounce_verdict_token,
)
from athenaeum.sensitivity import classify


def _conforming_emails(body: str) -> list[str]:
    """Email-shaped values the body names, in order, deduped.

    The migrated replacement for a direct ``find_inline_emails(body)`` call
    (issue athenaeum#992): routes through :func:`athenaeum.sensitivity.classify`
    instead of importing the detector function by name. ``config=None`` —
    this module has no config surface of its own, so only the shipped
    ``email`` recogniser (bound to the built-in ``pii`` class) is ever
    consulted, exactly as the direct import always was. Dedup is
    order-preserving, matching :func:`~athenaeum.pii.find_inline_emails`'s
    contract byte-for-byte (``classify`` itself reports one match per
    occurrence with no dedup — this wrapper is what keeps this module's
    caller-visible behaviour unchanged).
    """
    seen: list[str] = []
    for classified in classify(text=body, config=None):
        if classified.match.recognizer != "email":
            continue
        if classified.match.value not in seen:
            seen.append(classified.match.value)
    return seen


#: Frontmatter parsed to something other than a YAML mapping (a list, a bare
#: scalar), so the per-claim fields cannot be read from it at all.
FRONTMATTER_NOT_A_MAPPING = "frontmatter_not_a_mapping"

#: No non-empty ``observed_at`` on the note's OWN frontmatter.
MISSING_OBSERVED_AT = "missing_observed_at"

#: No non-empty ``source`` on the note's OWN frontmatter.
MISSING_SOURCE = "missing_source"

#: ``source`` is present but is neither the bare shorthand string nor the
#: per-value mapping shape, so the mark has no source to attribute itself to.
UNSUPPORTED_SOURCE_TYPE = "unsupported_source_type"

#: The body names no email-shaped token, so there is no identifier to mark.
NO_EMAIL_IDENTIFIER = "no_email_identifier"

#: The body names more than one email-shaped token. Which one bounced is
#: ambiguous, and Tier 0 never guesses.
SEVERAL_EMAIL_IDENTIFIERS = "several_email_identifiers"

#: The body carries no RFC 3463 ``5.x.x`` permanent-failure code. This is also
#: the decline a ``4.x`` transient note gets: a transient give-up is not a hard
#: bounce, and marking it bounced would be wrong.
MISSING_HARD_BOUNCE_CODE = "missing_hard_bounce_code"

#: Every reason :func:`check_tier0_bounce_conformance` can report, in the order
#: the contract document lists them. The documented table is pinned to this
#: tuple by ``tests/test_bounce_contract.py`` so the two cannot drift.
DECLINE_REASONS: tuple[str, ...] = (
    FRONTMATTER_NOT_A_MAPPING,
    MISSING_OBSERVED_AT,
    MISSING_SOURCE,
    UNSUPPORTED_SOURCE_TYPE,
    NO_EMAIL_IDENTIFIER,
    SEVERAL_EMAIL_IDENTIFIERS,
    MISSING_HARD_BOUNCE_CODE,
)

#: Where a decline's fix belongs — the note's own YAML frontmatter, or its
#: body text. A producer batch-fixing declines needs to know which half of the
#: note to edit.
WHERE_FRONTMATTER = "frontmatter"
WHERE_BODY = "body"


@dataclass(frozen=True)
class BounceDecline:
    """One unmet Tier-0 condition, and what would satisfy it.

    ``reason`` is one of :data:`DECLINE_REASONS` (a stable machine token — a
    producer may branch on it); ``where`` is :data:`WHERE_FRONTMATTER` or
    :data:`WHERE_BODY`; ``detail`` is a human-readable remedy.
    """

    reason: str
    where: str
    detail: str


@dataclass(frozen=True)
class Tier0BounceConformance:
    """Whether Tier 0 would recognize a candidate note, and why not if not.

    ``conforms`` is ``True`` exactly when :func:`librarian.tier0_bounce_mark`
    would recognize this note and mark the address. When it is ``True``,
    ``fact``, ``observed_at`` and ``source`` carry the values the mark would be
    written from, and ``declines`` is empty. When it is ``False``, ``fact`` is
    ``None`` and ``declines`` holds EVERY unmet condition.

    A declining note is not an error: it falls through to the Tier 1/2/3
    reasoning path and is compiled as an ordinary free-text memory.
    """

    conforms: bool
    declines: tuple[BounceDecline, ...] = ()
    fact: HardBounceFact | None = None
    observed_at: str | None = None
    source: str | dict[str, Any] | None = None

    @property
    def identifier(self) -> str | None:
        """The single email-shaped token Tier 0 would mark, if it conforms."""
        return self.fact.identifier if self.fact is not None else None

    @property
    def diagnostic(self) -> str | None:
        """The verbatim diagnostic line the ``5.x.x`` code was found on."""
        return self.fact.diagnostic if self.fact is not None else None

    @property
    def reasons(self) -> tuple[str, ...]:
        """Just the machine tokens, for a caller that only wants to branch."""
        return tuple(d.reason for d in self.declines)


def check_tier0_bounce_conformance(note_text: str) -> Tier0BounceConformance:
    """Would Tier 0 recognize *note_text*? Read-only — writes nothing (issue athenaeum#854).

    *note_text* is the FULL raw-intake note: its own YAML frontmatter followed
    by the body, exactly as a producer would submit it through ``remember()``.
    Both halves are checked, because the gate reads both — the per-claim
    ``observed_at`` / ``source`` fields from the frontmatter, the identifier
    and the ``5.x.x`` diagnostic from the body.

    This is the whole recognition half of :func:`librarian.tier0_bounce_mark`,
    which calls it; the gate adds only the write. Being the same code path is
    what makes "the check agrees with the gate" a structural property rather
    than a promise.

    Returns a :class:`Tier0BounceConformance`. Never raises for malformed
    input, never touches the filesystem, and never submits anything.
    """
    meta, body = parse_frontmatter(note_text or "")

    declines: list[BounceDecline] = []
    observed_at: str | None = None
    source: str | dict[str, Any] | None = None

    if not isinstance(meta, dict):
        declines.append(
            BounceDecline(
                FRONTMATTER_NOT_A_MAPPING,
                WHERE_FRONTMATTER,
                "The note's frontmatter must parse to a YAML mapping carrying "
                "`observed_at:` and `source:`.",
            )
        )
    else:
        observed_at = str(meta.get("observed_at", "") or "").strip() or None
        if observed_at is None:
            declines.append(
                BounceDecline(
                    MISSING_OBSERVED_AT,
                    WHERE_FRONTMATTER,
                    "Add a non-empty `observed_at:` (the date the bounce was "
                    "observed) to the note's own frontmatter.",
                )
            )

        raw_source = meta.get("source")
        if not raw_source:
            declines.append(
                BounceDecline(
                    MISSING_SOURCE,
                    WHERE_FRONTMATTER,
                    "Add a non-empty `source:` (per-claim provenance) to the "
                    "note's own frontmatter. Through `remember()` this is the "
                    "`sources` parameter, not `source` — that one picks the "
                    "raw/<session>/ landing directory.",
                )
            )
        elif not isinstance(raw_source, (str, dict)):
            declines.append(
                BounceDecline(
                    UNSUPPORTED_SOURCE_TYPE,
                    WHERE_FRONTMATTER,
                    "`source:` must be the bare shorthand string or the "
                    "per-value mapping shape; the mark cannot be attributed to "
                    "any other type.",
                )
            )
        else:
            source = raw_source

    # Body: the production predicates, called directly. `detect_hard_bounce_fact`
    # collapses both body conditions into one None, so the two are asked
    # separately here to report WHICH failed — with the same functions, not a
    # re-derivation of them.
    emails = _conforming_emails(body)
    if len(emails) == 0:
        declines.append(
            BounceDecline(
                NO_EMAIL_IDENTIFIER,
                WHERE_BODY,
                "The body must name exactly one email-shaped token — the "
                "address that bounced.",
            )
        )
    elif len(emails) > 1:
        declines.append(
            BounceDecline(
                SEVERAL_EMAIL_IDENTIFIERS,
                WHERE_BODY,
                f"The body names {len(emails)} email-shaped tokens; which one "
                "bounced is ambiguous. Emit one note per address.",
            )
        )

    if find_hard_bounce_code(body) is None:
        declines.append(
            BounceDecline(
                MISSING_HARD_BOUNCE_CODE,
                WHERE_BODY,
                "The body must carry an RFC 3463 `5.x.x` permanent-failure "
                "code. A `4.x` transient give-up is not a hard bounce and is "
                "deliberately out of scope.",
            )
        )

    if declines:
        return Tier0BounceConformance(conforms=False, declines=tuple(declines))

    fact = detect_hard_bounce_fact(body)
    if fact is None:  # pragma: no cover - unreachable: both body conditions held
        return Tier0BounceConformance(
            conforms=False,
            declines=(
                BounceDecline(
                    MISSING_HARD_BOUNCE_CODE,
                    WHERE_BODY,
                    "The body must carry an RFC 3463 `5.x.x` permanent-failure "
                    "code naming exactly one address.",
                ),
            ),
        )

    return Tier0BounceConformance(
        conforms=True,
        fact=fact,
        observed_at=observed_at,
        source=source,
    )


# ---------------------------------------------------------------------------
# The verified-undeliverable, non-RFC-3463 verdict contract (reversal of
# athenaeum#852's read-only stance for this narrow class — see
# athenaeum#1341). Sibling of the ``5.x.x`` contract above: SAME shape
# (frontmatter + exactly-one-email checks), DIFFERENT body predicate — a bare
# 550-559 SMTP reply code or a verified list-verification verdict token, not
# an RFC 3463 enhanced code. ``librarian.tier0_bounce_verdict_mark`` calls
# :func:`check_tier0_bounce_verdict_conformance` for its whole recognition
# decision, mirroring how ``tier0_bounce_mark`` shares its own check, so the
# two cannot drift apart either.
# ---------------------------------------------------------------------------

#: The body already carries an RFC 3463 ``5.x.x`` code — that shape belongs
#: to :func:`check_tier0_bounce_conformance` / ``tier0_bounce_mark``
#: exclusively (dispatched first in ``process_one``, so in practice this
#: branch never reaches a note like that at all — this decline is what keeps
#: the check correct standalone, independent of dispatch order).
RFC_CODE_ALREADY_PRESENT = "rfc_code_already_present"

#: Neither a bare 550-559 SMTP reply code nor a
#: :data:`~athenaeum.pii.VERIFIED_NON_RFC_BOUNCE_VERDICTS` token appears in
#: the body. This is also the decline an unrecognized or transient diagnostic
#: (e.g. ``SmtpConnectionTimeout``) gets: it is not a verified permanent
#: failure, and marking it would be wrong.
MISSING_VERDICT_SIGNAL = "missing_verdict_signal"

#: Every reason :func:`check_tier0_bounce_verdict_conformance` can report.
#: Kept separate from :data:`DECLINE_REASONS` — a distinct contract with its
#: own reason vocabulary, not an extension of the ``5.x.x`` one.
VERDICT_DECLINE_REASONS: tuple[str, ...] = (
    FRONTMATTER_NOT_A_MAPPING,
    MISSING_OBSERVED_AT,
    MISSING_SOURCE,
    UNSUPPORTED_SOURCE_TYPE,
    NO_EMAIL_IDENTIFIER,
    SEVERAL_EMAIL_IDENTIFIERS,
    RFC_CODE_ALREADY_PRESENT,
    MISSING_VERDICT_SIGNAL,
)


@dataclass(frozen=True)
class Tier0BounceVerdictConformance:
    """Whether the verdict Tier-0 branch would recognize a candidate note.

    Mirrors :class:`Tier0BounceConformance` exactly, but ``fact`` is a
    :class:`~athenaeum.pii.BounceVerdictFact` and this contract's
    :attr:`fact` is written to the WIKI ``bounced:`` field only — never the
    PII/contacts surface.
    """

    conforms: bool
    declines: tuple[BounceDecline, ...] = ()
    fact: BounceVerdictFact | None = None
    observed_at: str | None = None
    source: str | dict[str, Any] | None = None

    @property
    def identifier(self) -> str | None:
        """The single email-shaped token this branch would mark, if it conforms."""
        return self.fact.identifier if self.fact is not None else None

    @property
    def diagnostic(self) -> str | None:
        """The verbatim diagnostic line the verdict was found on."""
        return self.fact.diagnostic if self.fact is not None else None

    @property
    def reasons(self) -> tuple[str, ...]:
        """Just the machine tokens, for a caller that only wants to branch."""
        return tuple(d.reason for d in self.declines)


def check_tier0_bounce_verdict_conformance(
    note_text: str,
) -> Tier0BounceVerdictConformance:
    """Would the verdict Tier-0 branch recognize *note_text*? Read-only.

    Same frontmatter and single-email-identifier checks as
    :func:`check_tier0_bounce_conformance`; the body predicate differs — it
    recognizes a bare 550-559 SMTP reply code or a
    :data:`~athenaeum.pii.VERIFIED_NON_RFC_BOUNCE_VERDICTS` token instead of
    an RFC 3463 ``5.x.x`` code, and explicitly DECLINES when an RFC code is
    already present (that note belongs to the ``5.x.x`` contract above,
    checked here directly rather than assumed from dispatch order).

    This is the whole recognition half of
    :func:`librarian.tier0_bounce_verdict_mark`, which calls it and then does
    nothing but write the verdict onto the WIKI page's ``bounced:`` field —
    never the PII/contacts surface — on top of it.

    Never raises for malformed input, never touches the filesystem, and
    never submits or writes anything.
    """
    meta, body = parse_frontmatter(note_text or "")

    declines: list[BounceDecline] = []
    observed_at: str | None = None
    source: str | dict[str, Any] | None = None

    if not isinstance(meta, dict):
        declines.append(
            BounceDecline(
                FRONTMATTER_NOT_A_MAPPING,
                WHERE_FRONTMATTER,
                "The note's frontmatter must parse to a YAML mapping carrying "
                "`observed_at:` and `source:`.",
            )
        )
    else:
        observed_at = str(meta.get("observed_at", "") or "").strip() or None
        if observed_at is None:
            declines.append(
                BounceDecline(
                    MISSING_OBSERVED_AT,
                    WHERE_FRONTMATTER,
                    "Add a non-empty `observed_at:` (the date the verdict was "
                    "observed) to the note's own frontmatter.",
                )
            )

        raw_source = meta.get("source")
        if not raw_source:
            declines.append(
                BounceDecline(
                    MISSING_SOURCE,
                    WHERE_FRONTMATTER,
                    "Add a non-empty `source:` (per-claim provenance) to the "
                    "note's own frontmatter. Through `remember()` this is the "
                    "`sources` parameter, not `source` — that one picks the "
                    "raw/<session>/ landing directory.",
                )
            )
        elif not isinstance(raw_source, (str, dict)):
            declines.append(
                BounceDecline(
                    UNSUPPORTED_SOURCE_TYPE,
                    WHERE_FRONTMATTER,
                    "`source:` must be the bare shorthand string or the "
                    "per-value mapping shape; the mark cannot be attributed to "
                    "any other type.",
                )
            )
        else:
            source = raw_source

    emails = _conforming_emails(body)
    if len(emails) == 0:
        declines.append(
            BounceDecline(
                NO_EMAIL_IDENTIFIER,
                WHERE_BODY,
                "The body must name exactly one email-shaped token — the "
                "address that was verified undeliverable.",
            )
        )
    elif len(emails) > 1:
        declines.append(
            BounceDecline(
                SEVERAL_EMAIL_IDENTIFIERS,
                WHERE_BODY,
                f"The body names {len(emails)} email-shaped tokens; which one "
                "is undeliverable is ambiguous. Emit one note per address.",
            )
        )

    if find_hard_bounce_code(body) is not None:
        declines.append(
            BounceDecline(
                RFC_CODE_ALREADY_PRESENT,
                WHERE_BODY,
                "The body already carries an RFC 3463 `5.x.x` code — that "
                "note conforms to the `5.x.x` contract instead (see "
                "`check_tier0_bounce_conformance`), not this one.",
            )
        )
    elif (
        find_bare_smtp_5xx_code(body) is None
        and find_verified_bounce_verdict_token(body) is None
    ):
        declines.append(
            BounceDecline(
                MISSING_VERDICT_SIGNAL,
                WHERE_BODY,
                "The body must carry a bare SMTP reply code in the 550-559 "
                "range, or one of the verified non-RFC verdict tokens "
                "(`athenaeum.pii.VERIFIED_NON_RFC_BOUNCE_VERDICTS`). A "
                "transient or unrecognized diagnostic (e.g. "
                "`SmtpConnectionTimeout`) is deliberately out of scope.",
            )
        )

    if declines:
        return Tier0BounceVerdictConformance(conforms=False, declines=tuple(declines))

    fact = detect_bounce_verdict_fact(body)
    if fact is None:  # pragma: no cover - unreachable: both body conditions held
        return Tier0BounceVerdictConformance(
            conforms=False,
            declines=(
                BounceDecline(
                    MISSING_VERDICT_SIGNAL,
                    WHERE_BODY,
                    "The body must carry a bare 550-559 SMTP code or a "
                    "verified non-RFC verdict token naming exactly one "
                    "address.",
                ),
            ),
        )

    return Tier0BounceVerdictConformance(
        conforms=True,
        fact=fact,
        observed_at=observed_at,
        source=source,
    )


__all__ = [
    "DECLINE_REASONS",
    "FRONTMATTER_NOT_A_MAPPING",
    "MISSING_OBSERVED_AT",
    "MISSING_SOURCE",
    "UNSUPPORTED_SOURCE_TYPE",
    "NO_EMAIL_IDENTIFIER",
    "SEVERAL_EMAIL_IDENTIFIERS",
    "MISSING_HARD_BOUNCE_CODE",
    "WHERE_FRONTMATTER",
    "WHERE_BODY",
    "BounceDecline",
    "Tier0BounceConformance",
    "check_tier0_bounce_conformance",
    "VERDICT_DECLINE_REASONS",
    "RFC_CODE_ALREADY_PRESENT",
    "MISSING_VERDICT_SIGNAL",
    "Tier0BounceVerdictConformance",
    "check_tier0_bounce_verdict_conformance",
]
