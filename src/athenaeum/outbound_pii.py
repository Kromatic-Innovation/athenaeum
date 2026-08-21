# SPDX-License-Identifier: Apache-2.0
"""Outbound-draft PII lint (emails/phones) — interim mitigation split from athenaeum#428.

**Contract:** given text about to leave the system (email draft, Buffer post,
public issue), return every email/phone-shaped finding (location + class), or
a redacted copy — a pure, offline, deterministic text lint. It never decides
whether to actually block/send the outbound surface; that policy call is the
caller's.

**Factoring rule:** the cheap, mechanical half of athenaeum#428's "no PII in outbound
drafts" idea. This module owns DETECTION + optional REDACTION of the two named
classes (email, phone) in already-composed text. It does NOT own egress
*refusal* — an agent declining to reveal PII even when directly asked — which
stays parked on athenaeum#428 and is deliberately NOT attempted here: no policy
judgment, no network, no live-store access, no LLM call.

**Layering:** L3 service. Imports :mod:`athenaeum.sensitivity` (a sibling L3
module) at module scope for :func:`~athenaeum.sensitivity.classify` — detection
is obtained through the registry rather than importing
:mod:`athenaeum.pii`'s private compiled patterns directly (issue athenaeum#992;
see "Migrated onto the sensitivity registry" below). Consumed by
:mod:`athenaeum.provider` (redacting a CLI error envelope before it reaches a
log line) and the ``athenaeum outbound-lint`` CLI.

Relationship to :mod:`athenaeum.pii` (the athenaeum#427 corpus-hygiene slice): that
module lints *entity pages that stay in the corpus* for inline contact data.
This module lints *outbound-destined text about to leave the system*. They are
different surfaces with different lifecycles, so they are separate modules —
but detection shares ONE definition of "what an email/phone looks like":
:mod:`athenaeum.sensitivity`'s built-in ``email``/``phone`` recognisers, which
themselves iterate :mod:`athenaeum.pii`'s compiled ``_EMAIL_RE`` / ``_PHONE_RE``
patterns. If the detection patterns ever need to change, they change in one
place.

**Migrated onto the sensitivity registry (issue athenaeum#992).** This module used
to import :mod:`athenaeum.pii`'s private ``_EMAIL_RE`` / ``_PHONE_RE`` /
``_has_enough_digits`` / ``_is_excluded_phone_shape`` directly — never
:func:`~athenaeum.pii.find_inline_emails`/:func:`~athenaeum.pii.find_inline_phones`
(``docs/sensitivity-class-vocabulary.md`` §2.1/§9 previously claimed
otherwise; that claim is corrected in this PR). Migration was viable because
:mod:`athenaeum.sensitivity`'s built-in recognisers populate
``SensitivityMatch.span`` (the design note's S1a span decision), which
redaction requires. :func:`scan_outbound_text` now calls
:func:`athenaeum.sensitivity.classify` with ``config=None`` (this module has
no config surface of its own — every call site is unconditional) and applies
the SAME two policies it always applied on top of raw detection: (1) a phone
match whose span overlaps an email match is dropped
(``test_email_containing_digits_is_not_double_counted_as_phone``), and (2)
the allowlist filter. One deliberate, enumerated behavioural
difference: the built-in ``phone`` recogniser additionally suppresses a
digit run the surrounding prose already types as a labeled record id
(:func:`athenaeum.pii._has_labeled_identifier_prefix`, issue athenaeum#732) — a
suppression :func:`scan_outbound_text` did not previously apply. No existing
fixture exercises a labeled-identifier-prefixed phone-shaped token in outbound
text, so ``redact_outbound_text``'s output is byte-identical to pre-change on
the full existing fixture corpus (see ``TestOutboundPiiUnchangedOnFixtures``
in ``tests/test_outbound_pii.py``); the difference is a strict convergence
with the corpus-lint's already-shipped athenaeum#732 fix, not a new suppression
invented for this module.

Allowlist / fail-safe (the "isn't already known to the recipient" qualifier in
athenaeum#428): a caller that can establish an address is already known to the recipient
passes it in the ``allowlist`` — such findings are dropped (not flagged, not
redacted). Where the caller CANNOT establish that, the default is to flag: an
empty/absent allowlist means every match is reported. Failing safe (flag) is the
default, never silent-pass.

Two entry points, mirroring the issue's "callable API **and** a CLI":

- API — :func:`scan_outbound_text` (flag-only: returns findings),
  :func:`redact_outbound_text` (strip mode: returns sanitized text + findings),
  and :func:`lint_outbound_text` (convenience wrapper returning both).
- CLI — ``athenaeum outbound-lint`` (see :mod:`athenaeum._cmd_outbound`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Single source of truth for detection: the sensitivity registry's shipped
# `email`/`phone` recognisers (issue athenaeum#992). Importing sensitivity here
# creates no cycle — it does not import this module.
from athenaeum.sensitivity import classify

#: The two PII classes this lint recognizes (the two athenaeum#428 names). Exposed as
#: constants so callers/tests match on a symbol rather than a bare string.
PII_KIND_EMAIL = "email"
PII_KIND_PHONE = "phone"

#: Default redaction placeholder template. ``{kind}`` is substituted with the
#: finding's class (``email``/``phone``). Callers can override via the
#: ``placeholder`` argument to :func:`redact_outbound_text`.
DEFAULT_PLACEHOLDER = "[redacted-{kind}]"


@dataclass(frozen=True)
class PiiFinding:
    """One PII match in outbound text: its class, value, and location.

    ``start``/``end`` are 0-based character offsets into the scanned text
    (half-open, so ``text[start:end] == value``). ``line`` is 1-based and
    ``column`` is the 1-based character position within that line — the shape
    editors and humans expect when a lint points at "line 3, column 12".
    """

    kind: str
    value: str
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class Allowlist:
    """Normalized set of addresses already known to the recipient.

    Built via :meth:`from_entries` so email casing and phone separators are
    normalized once. A finding is allowlisted (dropped) when its normalized
    form is present here. Emails compare case-insensitively; phones compare on
    their digit sequence only (so ``(555) 010-0100`` and ``555-010-0100``
    match), which means an allowlist entry must carry the same digits as the
    address as it appears in the text — a country-code prefix present in one
    but not the other will not match (intentionally conservative: better to
    over-flag a known address than to silently pass an unknown one).
    """

    emails: frozenset[str] = field(default_factory=frozenset)
    phones: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_entries(cls, entries: object) -> Allowlist:
        """Build an :class:`Allowlist` from an iterable of raw strings.

        Each entry is classified by shape: anything containing ``@`` is an
        email (normalized lowercase), otherwise it is treated as a phone
        (normalized to digits only). Blank entries are ignored. ``None`` yields
        an empty allowlist — the fail-safe default where everything is flagged.
        """
        emails: set[str] = set()
        phones: set[str] = set()
        if entries is None:
            return cls()
        # entries is typed ``object`` so callers may pass any iterable of
        # raw values (each is str()-coerced below); narrow just enough for
        # mypy while preserving the original "whatever's iterable" runtime
        # behavior (a non-iterable truthy value still raises TypeError).
        assert isinstance(entries, Iterable)
        for raw in entries:
            token = str(raw).strip()
            if not token:
                continue
            if "@" in token:
                emails.add(_normalize_email(token))
            else:
                digits = _normalize_phone(token)
                if digits:
                    phones.add(digits)
        return cls(emails=frozenset(emails), phones=frozenset(phones))

    def contains(self, finding: PiiFinding) -> bool:
        """True when *finding* names an address already known to the recipient."""
        if finding.kind == PII_KIND_EMAIL:
            return _normalize_email(finding.value) in self.emails
        if finding.kind == PII_KIND_PHONE:
            return _normalize_phone(finding.value) in self.phones
        return False


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def _coerce_allowlist(allowlist: Allowlist | object) -> Allowlist:
    """Accept either a prebuilt :class:`Allowlist` or a raw iterable of strings."""
    if isinstance(allowlist, Allowlist):
        return allowlist
    return Allowlist.from_entries(allowlist)


def _line_starts(text: str) -> list[int]:
    """Offsets at which each line begins (index 0 is offset 0)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _locate(offset: int, line_starts: list[int]) -> tuple[int, int]:
    """Map a 0-based *offset* to a (1-based line, 1-based column) pair."""
    # Rightmost line start that is <= offset. Linear scan from the end is
    # simple and plenty fast for outbound-draft-sized text (a few KB).
    line_index = 0
    for i, start in enumerate(line_starts):
        if start <= offset:
            line_index = i
        else:
            break
    return line_index + 1, offset - line_starts[line_index] + 1


def scan_outbound_text(text: str, *, allowlist: Allowlist | object = None) -> list[PiiFinding]:
    """Scan *text* for PII, returning findings in document order.

    Detects emails first, then phone numbers that do NOT overlap an email match
    (so ``5551234567`` inside ``jo.5551234567@x.com`` is reported once, as the
    email, not twice). Detection is obtained through
    :func:`athenaeum.sensitivity.classify` (issue athenaeum#992) — the shipped
    ``email``/``phone`` recognisers, which already apply the digit-count floor
    and the provably-not-a-phone exclusion (ISO dates, year ranges, bare id
    fragments — issue athenaeum#683) that this module always relied on, plus one
    additional suppression this module did not previously have (a
    labeled-identifier-prefixed digit run, issue athenaeum#732 — see this
    module's docstring for why that is a deliberate, harmless convergence).
    Email detection requires a dotted TLD so ``@handle`` mentions and
    ``@decorator`` names do not false-positive.

    Findings naming an address in *allowlist* (already known to the recipient)
    are omitted. An empty/absent allowlist flags everything — the fail-safe
    default.
    """
    allow = _coerce_allowlist(allowlist)
    source = text or ""
    line_starts = _line_starts(source)

    classified = classify(text=source, config=None)

    findings: list[PiiFinding] = []
    email_spans: list[tuple[int, int]] = []

    for cm in classified:
        if cm.match.recognizer != PII_KIND_EMAIL:
            continue
        assert cm.match.span is not None  # built-in recognisers always set span
        start, end = cm.match.span
        email_spans.append((start, end))
        line, column = _locate(start, line_starts)
        findings.append(
            PiiFinding(PII_KIND_EMAIL, cm.match.value, start, end, line, column)
        )

    for cm in classified:
        if cm.match.recognizer != PII_KIND_PHONE:
            continue
        assert cm.match.span is not None  # built-in recognisers always set span
        start, end = cm.match.span
        if any(start < e and s < end for s, e in email_spans):
            continue  # digits living inside an email match; already reported
        line, column = _locate(start, line_starts)
        findings.append(
            PiiFinding(PII_KIND_PHONE, cm.match.value, start, end, line, column)
        )

    findings.sort(key=lambda f: f.start)
    return [f for f in findings if not allow.contains(f)]


def redact_outbound_text(
    text: str,
    *,
    allowlist: Allowlist | object = None,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> tuple[str, list[PiiFinding]]:
    """Strip mode: return ``(redacted_text, findings)``.

    Every reported finding (i.e. every non-allowlisted match) is replaced in
    the returned text by *placeholder*, with ``{kind}`` substituted for the
    finding's class. Allowlisted addresses are left intact. The returned
    findings carry offsets into the ORIGINAL text (not the redacted output) so
    a caller can still report where each redaction happened.
    """
    findings = scan_outbound_text(text, allowlist=allowlist)
    source = text or ""
    # Replace right-to-left so earlier offsets stay valid as we mutate.
    out = source
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        replacement = placeholder.format(kind=finding.kind)
        out = out[: finding.start] + replacement + out[finding.end :]
    return out, findings


@dataclass(frozen=True)
class OutboundLintResult:
    """Combined result of :func:`lint_outbound_text`.

    ``redacted`` is ``None`` in flag-only mode and the sanitized string in
    strip mode, keeping the two modes distinguishable in one return type.
    """

    findings: list[PiiFinding]
    redacted: str | None = None

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


def lint_outbound_text(
    text: str,
    *,
    allowlist: Allowlist | object = None,
    redact: bool = False,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> OutboundLintResult:
    """Convenience wrapper over the two modes.

    ``redact=False`` (default) is flag-only: ``result.redacted is None``.
    ``redact=True`` is strip mode: ``result.redacted`` is the sanitized text.
    Either way ``result.findings`` lists every non-allowlisted PII match.
    """
    if redact:
        cleaned, findings = redact_outbound_text(
            text, allowlist=allowlist, placeholder=placeholder
        )
        return OutboundLintResult(findings=findings, redacted=cleaned)
    return OutboundLintResult(findings=scan_outbound_text(text, allowlist=allowlist))


__all__ = [
    "PII_KIND_EMAIL",
    "PII_KIND_PHONE",
    "DEFAULT_PLACEHOLDER",
    "PiiFinding",
    "Allowlist",
    "OutboundLintResult",
    "scan_outbound_text",
    "redact_outbound_text",
    "lint_outbound_text",
]
