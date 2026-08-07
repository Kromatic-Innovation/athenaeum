# SPDX-License-Identifier: Apache-2.0
"""PII off-corpus — contacts surface, entity-page lint, observation log (athenaeum#427).

**Contract:** keep PII OUT of the recall-visible corpus (or, if it must live
inline, keep it flagged so every corpus consumer excludes it) — this module
never encrypts or redacts content in place. Corpus hygiene + ambient-egress
reduction, NOT encryption (see the issue's threat model: recall injects pages
into arbitrary agent prompts, so the retrieval-layer exclusion is the
cheapest egress reduction — at-rest encryption is ~pointless when the
librarian itself needs the keys). This module is the **code-only slice**:
migrating live entity pages to durable-IDs-only is operator task athenaeum#437 (out of
scope); wiring "retracting an observation flags a dependent merge" is the
retraction cascade, athenaeum#435 (out of scope, blocked on athenaeum#425's merge-provenance
model).

**Relationship to :mod:`athenaeum.outbound_pii`** (read that module's
docstring for its half): this module screens content that STAYS in the
corpus (entity pages, the observation ledger); ``outbound_pii`` screens text
about to LEAVE the system (an email draft, a Buffer post). Different
surfaces, different lifecycles — kept as separate modules on purpose, though
``outbound_pii`` imports this module's compiled detection regexes so there is
one definition of "what an email/phone looks like" shared by both.

**Layering:** L3 service. Imports :mod:`athenaeum.storage` (L2),
:mod:`athenaeum.models` (L1), and :mod:`athenaeum.atomic_io` (L0) at module
scope — all strictly lower layers, so this remains a one-way edge. Consumed
by :mod:`athenaeum.search` (the ``is_pii_flagged`` corpus-exclusion
predicate), by the merge-candidate discovery in :mod:`athenaeum.wiki_dedupe`,
and by :mod:`athenaeum.librarian`'s ``tier0_bounce_mark`` (issue athenaeum#765) —
never the reverse.

Five pieces, in the order the issue settles them:

1. **Contacts surface** (:func:`contacts_surface_root` / :func:`is_pii_class`)
   — a thin convenience wrapper over :mod:`athenaeum.storage`'s athenaeum#429 adapter
   layer. This module does NOT hardcode ``~/knowledge/contacts/`` in any
   corpus consumer: the path is an adapter-config choice (see
   ``athenaeum.yaml``'s ``storage.mapping: {pii: excluded}`` example), and
   every embed/recall/merge consumer excludes the resolved surface root **by
   construction** because it lives outside ``wiki/`` + the configured
   ``recall.extra_intake_roots`` (the same by-construction property
   :mod:`tests.test_storage`'s ``TestByConstructionExclusion`` already proves
   for athenaeum#429's adapter layer in general). This module's ``contacts_surface_root``
   is just the writer-facing convenience that resolves to that same excluded
   root under the conventional ``pii`` entity class.

2. **Entity-page lint** (:data:`PII_FLAG` / :func:`has_inline_contact_fields` /
   :func:`lint_inline_contact_fields`) — flags durable/archival-contact
   confusion on a page that stays IN the corpus: an entity page should carry
   only durable identifiers (name, LinkedIn, record id, Google-Contact id);
   inline ``emails:`` / ``phones:`` frontmatter (or an email/phone-shaped
   string in the body) is flagged as a validation warning, mirroring the
   athenaeum#424 ``memory_class`` precedent (:mod:`athenaeum.schemas` / :mod:`athenaeum._lint`)
   — recoverable, not a hard failure, because migrating existing pages is
   athenaeum#437. ``pii: true`` is the belt-and-suspenders flag an operator can set on
   a page that legitimately carries PII inline in narrative; every corpus
   consumer additionally excludes a ``pii: true`` page even when it is NOT on
   the excluded surface (see point 3).

3. **Corpus-consumer wiring for ``pii: true``** — :func:`is_pii_flagged`
   is the single predicate (mirrors :func:`athenaeum.authority.is_pointer_stub`)
   consulted by :mod:`athenaeum.search` (embed index build + keyword
   scan-on-query) and :mod:`athenaeum.wiki_dedupe` (merge-candidate
   discovery) so a flagged page is excluded from ALL THREE corpus
   capabilities without needing to move it off the default wiki surface.

4. **Observation log + supersession fold** — an append-only JSONL ledger
   recording ``(identifier, person_id, observed_at, source_msg_id)``
   mirroring :mod:`athenaeum.provenance`'s merge-provenance ledger (JSONL,
   ``O_APPEND`` + fsync, tolerant reader that skips a torn trailing line).
   ``identifier -> person`` is ~1:1 (a taken-over inbox still identifies the
   ORIGINAL person — routing is not identity) but several persons are
   allowed for a genuinely shared address, so a read returns ALL live
   attributions. Corrections are supersession records
   ``(retracts: obs_id, reason, at)`` — never edits/tombstones of the
   original observation. :func:`fold_observations` resolves "latest
   uncontradicted" per identifier via a DETERMINISTIC FOLD (sort by
   ``observed_at`` then ``obs_id`` for a stable tie-break; drop any
   observation a supersession record retracts) — deliberately NO
   clustering/similarity step, so this does not recreate the wiki-dedup
   merge problem the rest of the codebase works hard to keep separate.

5. **Hard-bounce recognition + mark** (:func:`detect_hard_bounce_fact` /
   :func:`mark_bounced`, issue athenaeum#765). A hard-bounce fact (identifier +
   diagnostic + observed date + source) arrives as an ORDINARY free-text
   raw-intake note — no new intake schema, no ``type:`` field, no dedicated
   code path (see :func:`athenaeum.librarian.tier0_bounce_mark`, which is
   just one more deterministic decline-or-apply branch in the SAME tier
   dispatch every raw file already goes through). Recognition keys on a
   RFC 3463 enhanced-status-code of class ``5.x.x`` — the hard-bounce class
   — so a ``4.x`` transient diagnostic (voltaire#81's "potentially stale"
   case) never matches and is left untouched; this issue is scoped to hard
   bounces only. The mark itself is a VALID-TIME close
   (``valid_until``, reusing athenaeum#308's existing claim-validity mechanism —
   see :func:`athenaeum.models.valid_until_expired`) rather than a new status
   enum: "deliverable until the observed date" is exactly what that
   mechanism already expresses. The mark lives on the identifier's own
   contact-record frontmatter (upserted in place — idempotent, never
   deleted, no new ledger file); :func:`is_bounced` is the single read-side
   predicate a consumer (recall, lint) calls to tell present-but-
   non-deliverable apart from absent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, render_frontmatter, slugify, valid_until_expired
from athenaeum.storage import surface_root_for_class

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Contacts surface (thin convenience over the athenaeum#429 adapter layer)
# ---------------------------------------------------------------------------

#: Conventional entity-class name this module's callers route through the
#: storage-adapter layer. NOT special-cased in :mod:`athenaeum.storage` —
#: it is just a class name like any other; an operator maps it to the
#: built-in ``excluded`` adapter (or a custom one) via ``storage.mapping``
#: in ``athenaeum.yaml``. See the module docstring's point 1.
PII_ENTITY_CLASS = "pii"


def contacts_surface_root(
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> Path:
    """Resolve the on-disk root for the ``pii`` entity class.

    Delegates entirely to :func:`athenaeum.storage.surface_root_for_class` —
    no hardcoded ``contacts/`` path here. Absent any ``storage.mapping``
    entry for ``pii``, this resolves to the default wiki surface (so calling
    this with an unconfigured knowledge base is a no-op convenience, not a
    silent PII leak — the operator must explicitly map ``pii`` to the
    ``excluded`` adapter, exactly as ``athenaeum.yaml``'s shipped example
    comment shows, for this root to actually land outside the corpus).
    """
    return surface_root_for_class(PII_ENTITY_CLASS, config, knowledge_root)


def is_pii_class_excluded(config: dict[str, Any] | None) -> bool:
    """True when the ``pii`` entity class currently resolves out of the corpus.

    Convenience predicate for callers (e.g. a writer deciding where to place
    a new contact record) that want to confirm the operator has actually
    wired ``pii`` to an excluded-policy adapter before writing there.
    """
    from athenaeum.storage import is_excluded

    return is_excluded(PII_ENTITY_CLASS, config)


# ---------------------------------------------------------------------------
# 2. Entity-page lint — inline email/phone flag
# ---------------------------------------------------------------------------

#: Frontmatter flag mirroring :data:`athenaeum.authority.POINTER_STUB_FLAG`'s
#: pattern: a real bool or truthy string variant marks a page as carrying
#: PII inline in its narrative on purpose (belt-and-suspenders — the page
#: still stays in the corpus unless ALSO routed to an excluded surface).
PII_FLAG = "pii"

#: Frontmatter (list-valued) fields that hold archival contact data directly,
#: per the issue's entity-page rule: entity pages carry durable identifiers
#: only (name, LinkedIn, record id, Google-Contact id); ``emails``/``phones``
#: are the two contact-data fields that must not live inline going forward.
#: Migrating pre-existing pages that already carry these is athenaeum#437 (out of
#: scope) — this module only flags, never rewrites, a page.
CONTACT_FRONTMATTER_FIELDS: tuple[str, ...] = ("emails", "phones")

#: Frontmatter fields whose values are DURABLE IDENTIFIERS (athenaeum#427) and are
#: PRESERVED VERBATIM on an entity page even when a value is email/phone-shaped
#: — they are identity, not archival contact data. The athenaeum#479 migrator originally
#: read only ``emails:`` / ``phones:``; athenaeum#502 found the live residual lives
#: mostly in *other* keys (``aliases:``, ``former_emails:``, ``source:``, …),
#: so the migrator now detector-scans EVERY frontmatter value — but must NOT
#: rewrite these identity fields. Two distinct reasons:
#:
#: * ``uid`` / ``type`` / ``linkedin_url`` / ``google_contact*`` /
#:   ``handles_verified`` are durable identifiers (athenaeum#427). An email that has
#:   landed in one of these (a data-quality anomaly — athenaeum#502 saw one page each in
#:   ``google_contact_kromatic`` / ``linkedin_connected_on``) is NOT auto-
#:   migrated: rewriting an identity field is not mechanically safe, so it is
#:   left for the corpus-wide lint to surface and an operator to hand-fix.
#: * ``name`` / ``preferred_name`` are the ~80 pages NAMED after an email
#:   address (athenaeum#502, from the Streak email-only import). Renaming an entity page
#:   changes its slug and breaks inbound ``related:`` edges + alias resolution,
#:   so the name-is-an-email population is EXCLUDED from this automatic path and
#:   handled in its own slice. Preserving these here is exactly what keeps the
#:   migrator from silently renaming those live pages.
DURABLE_IDENTIFIER_FIELDS: frozenset[str] = frozenset(
    {
        "uid",
        "type",
        "name",
        "preferred_name",
        "linkedin_url",
        "linkedin",
        "handles_verified",
        "google_contact",
        "google_contact_kromatic",
    }
)

#: The two frontmatter fields that hold an entity page's NAME. When one of
#: these IS an email address the page is excluded from the athenaeum#502 automatic
#: migration path (renaming is unsafe — see :data:`DURABLE_IDENTIFIER_FIELDS`);
#: :func:`name_field_holds_pii` reports the population so the separate slice can
#: pick it up.
NAME_FIELDS: tuple[str, ...] = ("name", "preferred_name")

# A conservative email-shaped token — good enough to flag a body/narrative
# line as "looks like inline contact data" without trying to be a fully
# RFC 5322-correct validator (a lint, not a hard gate).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A conservative phone-shaped token: 7+ digits allowing common separators
# (spaces, dashes, dots, parens) and an optional leading '+' or '('.
# Deliberately permissive about separators (so "+1-555-0100" and
# "(555) 010-0100" both match) but requires enough digits that ordinary
# numbers (years, page counts, issue numbers) don't false-positive.
_PHONE_RE = re.compile(r"(?<!\w)([+(]?\d[\d\-.\s()]{6,}\d)(?!\w)")

# The phone regex above is intentionally permissive, which means digit runs
# that are NOT phone numbers slip through: dates (in any ordering), year
# ranges, bare analytics/uid id fragments, and — because the character class
# admits spaces, parens and (via `\s`) newlines — a match that RUNS PAST its
# number into an adjacent one across a separator (issue athenaeum#720: `256-257-280`
# issue-number lists, `02-08-2018` non-ISO dates, `2026-04-27)\n\n1` dates
# bleeding into the next line, `1778 (2026-08-01` version-and-date pairs). The
# classifier below narrows the match by NORMALIZATION + STRUCTURAL
# CLASSIFICATION rather than a growing literal blocklist: it segments the
# capture into digit groups and the separator runs between them and asks
# whether that structure can be a phone at all. A new separator style needs no
# new rule — it is just another separator run. Every genuine fixture phone
# carries a '+', a balanced `(area)`, or a >=10-digit national run, so none is
# mistaken for a list, a date, or a bled capture.

# One date component (1–4 digits) separated by a single `-`, `.` or `/`. Used
# order-agnostically by :func:`_looks_like_date` — which of the three numbers
# is the year is decided by value, not position, so `2015-12-03` (ISO),
# `02-08-2018` (D-M-Y / M-D-Y) and `2018.08.02` (dotted) all classify as dates.
_DATE_RE = re.compile(r"^(\d{1,4})[-./](\d{1,4})[-./](\d{1,4})$")

# A plausible 4-digit calendar year (19xx / 20xx). Single source of truth for
# "is this component a year" shared by the date and year-range classifiers.
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Year range: two plausible calendar years joined by ANY run of `-`/`.`/space
# separators — `2019-2020`, `2020--2021` (en-dash-style double hyphen), or
# `2019.2020`. Restricting both halves to real-year prefixes keeps a local
# number like `2015-9988` (2nd half not a year) matchable.
_YEAR_RANGE_RE = re.compile(r"^(?:19|20)\d{2}[-.\s]+(?:19|20)\d{2}$")

# A bare digit run (no '+', no separators) is only phone-shaped when its digit
# count falls in the E.164-plausible band: a national number is >= 10 digits
# (NANP) and the international maximum is 15. The corpus's bare-run false
# positives (`00075741` = 8 digits, GA4 property id `387473359` = 9 digits)
# fall below that band, while a genuine bare-typed phone number (>= 10 digits)
# still matches. Runs with a '+' or separators bypass this entirely.
_BARE_PHONE_MIN_DIGITS = 10
_BARE_PHONE_MAX_DIGITS = 15


def _has_enough_digits(candidate: str, *, minimum: int = 7) -> bool:
    return sum(ch.isdigit() for ch in candidate) >= minimum


def _looks_like_date(candidate: str) -> bool:
    """True when *candidate* is a calendar date (any ordering) or a year range.

    Excludes the date shapes the phone regex matches in the live corpus —
    ISO dates (`2015-12-03`, athenaeum#500), non-ISO orderings (`02-08-2018`, athenaeum#720),
    dotted dates (`2018.08.02`, athenaeum#720) and year ranges (`2019-2020` /
    `2020--2021`) — without excluding phone-shaped tokens that merely resemble
    them. Ordering-agnostic: of the three numeric components exactly one must
    be a 4-digit calendar year (at either end), and the other two a plausible
    month/day pair (each 1–31, at least one 1–12, in either order). A phone
    like `917-231-6130` has no year component; `5551-23-4567` has a would-be
    year (`5551`) outside 19xx/20xx — neither is dropped.
    """
    m = _DATE_RE.match(candidate)
    if m:
        a, b, c = m.groups()
        if _YEAR_RE.match(a) and not _YEAR_RE.match(c):
            d1, d2 = int(b), int(c)
        elif _YEAR_RE.match(c) and not _YEAR_RE.match(a):
            d1, d2 = int(a), int(b)
        else:
            return False  # no single unambiguous year component — not a date
        return 1 <= d1 <= 31 and 1 <= d2 <= 31 and (d1 <= 12 or d2 <= 12)
    return bool(_YEAR_RANGE_RE.match(candidate))


def _is_bare_id_fragment(candidate: str) -> bool:
    """True when *candidate* is a separator-free digit run too short to be a phone.

    Page uid prefixes and analytics/property ids (issue athenaeum#500's `00075741`,
    `387473359`) are bare digit runs below the E.164-plausible length band; a
    genuine bare-typed phone number (>= 10 digits) is kept. Any '+' or
    separator character means it is not a bare run, so real fixtures like
    `+1-555-0100` and `(555) 010-0100` are never treated as id fragments.
    """
    if not candidate.isdigit():
        return False
    return not (_BARE_PHONE_MIN_DIGITS <= len(candidate) <= _BARE_PHONE_MAX_DIGITS)


#: Preceding-token labels that TYPE the digit run that follows as a labeled
#: identifier, not a phone (issue athenaeum#732). A value the surrounding prose
#: already names — ``QBO realm 1008563730``, GA4 ``stream 5139685489``,
#: ``ISBN 978…`` — is a self-identifying record id; a preceding-token match
#: retires it with no model call. This is a DATA list: adding a new label is an
#: entry here, never a new code path. Matched case-insensitively against the
#: word immediately before the run, tolerating the quote / backtick / paren /
#: colon punctuation that commonly sits between a label and its value
#: (``stream `5139685489```, ``realm: 1008563730``).
LABELED_IDENTIFIER_PREFIXES: tuple[str, ...] = (
    "qbo realm",
    "realm",
    "stream",
    "isbn",
)

#: Anchored at the END of the text preceding a candidate: an optional run of
#: separator punctuation, then one of the labels above, bounded on its left by a
#: non-alphanumeric (so ``realm`` matches but ``overwhelm`` does not). The gap
#: class includes ``-`` so ``ISBN-13 9798…`` matches (the ``13 `` the phone
#: regex bleeds off ``ISBN-13`` sits between the label and the value).
_LABELED_PREFIX_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:"
    + "|".join(re.escape(label) for label in LABELED_IDENTIFIER_PREFIXES)
    + r")[\s`'\"(:=\-]*$",
    re.IGNORECASE,
)


def _has_labeled_identifier_prefix(preceding_text: str) -> bool:
    """True when *preceding_text* ends with a labeled-identifier prefix.

    *preceding_text* is the corpus text immediately before a phone-shaped run.
    Only the tail is relevant, so this bounds the scan to the last 64 chars —
    long enough to hold ``QBO realm `` and its punctuation, short enough to stay
    cheap on a large page (issue athenaeum#732).
    """
    return bool(_LABELED_PREFIX_RE.search(preceding_text[-64:]))


def _is_isbn13(candidate: str) -> bool:
    """True when *candidate* is a bare ISBN-13 — 13 digits with a 978/979 prefix.

    An ISBN-13's Bookland prefix (``978``/``979``) at exactly 13 digits is a
    structural tell independent of any adjacent ``ISBN`` label (issue
    athenaeum#732), so an unlabeled ISBN is retired without relying on
    surrounding prose. A 13-digit run sits INSIDE the E.164-plausible band, so
    :func:`_is_bare_id_fragment` does not catch it — this rule does. No genuine
    phone is a bare 13-digit run beginning 978/979 (there is no such country
    code), so nothing real is dropped.
    """
    return len(candidate) == 13 and candidate.isdigit() and candidate[:3] in ("978", "979")


def _normalize_phone_token(token: str) -> str:
    """Strip a single leading ``+``/``(`` and a single trailing ``)`` from *token*.

    ``_PHONE_RE`` captures its optional leading ``[+(]`` delimiter **inside** the
    capture group, so a parenthesized run like ``(2026-07-29)`` is captured as
    ``(2026-07-29`` (issue athenaeum#683). The date/id-fragment exclusion helpers below
    test the raw token and are defeated by that leading punctuation —
    ``_DATE_RE`` is anchored on ``^\\d`` so the ``(`` never matches, and
    ``_is_bare_id_fragment`` short-circuits because ``'(2026-07-29'.isdigit()``
    is ``False``. Stripping the surrounding
    delimiter for the exclusion *check only* (never the returned value) restores
    the intended date/id-fragment exclusions while leaving genuine phone tokens
    like ``(555) 010-0100`` — whose interior separators mean they are never a bare
    run or a date — matched and returned verbatim.
    """
    normalized = token
    if normalized[:1] in "+(":
        normalized = normalized[1:]
    if normalized[-1:] == ")":
        normalized = normalized[:-1]
    return normalized


def _is_excluded_phone_shape(token: str) -> bool:
    """True when *token* is a provably-non-phone shape.

    Shared by :func:`find_inline_phones` (corpus-page lint) and
    :func:`athenaeum.outbound_pii.scan_outbound_text` (egress lint) so the
    "what is provably not a phone" rule has exactly one definition. The
    classification is structural — it segments the capture into digit groups
    and the separator runs between them and asks whether that structure can be
    a phone at all — so a new separator style needs no new rule (issue athenaeum#720):

    * **Line-spanning** (``\\n``/``\\r``/``\\t``) — the permissive character
      class lets a match run across whitespace into the next line's number
      (``2026-04-27)\\n\\n1``). A phone never wraps a line.
    * **Unbalanced parentheses** — ``(555) 010-0100`` is a balanced area code;
      an unmatched ``(`` or ``)`` means the match bled across a paren boundary
      into adjacent text (``1778 (2026-08-01``, ``2026-04-27)``).
    * **Date / year range** in any ordering or separator style
      (:func:`_looks_like_date`) — ``02-08-2018``, ``2020--2021``.
    * **Bare id/analytics fragment** — a separator-free run outside the
      E.164-plausible length band (:func:`_is_bare_id_fragment`, athenaeum#500).
    * **Bare ISBN-13** — 13 digits with a ``978``/``979`` Bookland prefix
      (:func:`_is_isbn13`, athenaeum#732), caught structurally so an unlabeled
      ISBN needs no adjacent ``ISBN`` prose.
    * **Multi-character separator run** between digit groups (``--``, ``..``) —
      list or range punctuation, never phone grouping (``445--436--435--374``).
    * **Four or more groups without a ``+``** — a phone has at most four digit
      groups (country/area/prefix/line) and a real four-group run carries the
      ``+`` a country code implies; a bare four-group run is an issue list, a
      four-part date, or a datetime that bled past its date across a space
      (athenaeum#732: ``410-414-416-412``, ``2018-05-06-07``, ``2026-04-23 05``).
      A lower bound with no upper limit, so a 5-/6-group list stays closed.
    * **More than four groups** — a list even with a ``+`` (athenaeum#720).
    * **Short unprefixed grouped run** — a 2-3 group separator-joined sequence
      with no ``+`` and fewer than a full national number's digits (10) is an
      issue-number / reference list, not a phone (``256-257-280`` = 9 digits).
      ``+1-555-0100`` (``+`` prefix) and ``917-231-6130`` (10 digits) are kept.

    None of these can be a genuine phone, so applying the rule on the egress
    path removes false positives without dropping any real number.
    """
    if any(ch in token for ch in "\r\n\t"):
        return True
    if token.count("(") != token.count(")"):
        return True

    candidate = _normalize_phone_token(token)
    if _looks_like_date(candidate) or _is_bare_id_fragment(candidate) or _is_isbn13(candidate):
        return True

    # Structural segmentation on the paren-free candidate: balanced parens wrap
    # an area code, they are not grouping separators.
    stripped = candidate.replace("(", "").replace(")", "")
    pieces = re.findall(r"\d+|\D+", stripped)
    groups = [p for p in pieces if p.isdigit()]
    internal_seps = [
        p
        for i, p in enumerate(pieces)
        if not p.isdigit()
        and 0 < i < len(pieces) - 1
        and pieces[i - 1].isdigit()
        and pieces[i + 1].isdigit()
    ]

    if any(len(sep) > 1 for sep in internal_seps):
        return True
    has_plus = token.startswith("+")
    total_digits = sum(len(g) for g in groups)
    # A phone has at most four digit groups (country/area/prefix/line), and a
    # genuine four-group run carries the international '+' a country code
    # implies. So a run of four OR MORE groups without a '+' is a list, a
    # four-part date, or a datetime that bled past its date across a space —
    # never a phone (issue athenaeum#732: `410-414-416-412`, `2018-05-06-07`,
    # `2026-04-23 05`). Expressed as a lower bound with NO upper limit, so a 5-
    # or 6-group list cannot reopen the class (athenaeum#732 AC3). `917-231-6130`
    # (3 groups) and `+1-555-234-5678` (has '+') are kept.
    if len(groups) >= 4 and not has_plus:
        return True
    # More than four groups is a list even WITH a '+' — a phone never exceeds
    # four groups (retains athenaeum#720's upper-count guard for the +-prefixed case).
    if len(groups) > 4:
        return True
    # A short separator-joined run with no '+' and fewer than a national
    # number's digits is a 2-3 group issue/reference list (athenaeum#720:
    # `256-257-280` = 9 digits). `917-231-6130` (10 digits) is kept.
    if len(groups) >= 2 and not has_plus and total_digits < 10:
        return True
    return False


def is_pii_flagged(meta: dict[str, Any] | None) -> bool:
    """True when frontmatter carries a truthy ``pii`` flag (belt-and-suspenders).

    Same coercion contract as :func:`athenaeum.authority.is_pointer_stub` /
    :func:`athenaeum.models.parse_deprecated`: a real bool or a truthy string
    variant; missing/falsey => False. Single source of truth consulted by
    every corpus consumer (:mod:`athenaeum.search`, :mod:`athenaeum.wiki_dedupe`)
    so a page an operator has hand-flagged is excluded from embed/recall/merge
    even when it has not been moved to the excluded surface.
    """
    if not meta:
        return False
    value = meta.get(PII_FLAG)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def find_inline_emails(text: str) -> list[str]:
    """Return every email-shaped token found in *text*, in order, deduped."""
    seen: list[str] = []
    for m in _EMAIL_RE.finditer(text or ""):
        token = m.group(0)
        if token not in seen:
            seen.append(token)
    return seen


def find_inline_phones(text: str) -> list[str]:
    """Return every phone-shaped token found in *text*, in order, deduped.

    Excludes the corpus false positives issue athenaeum#500 documented — ISO dates,
    year ranges, and bare id/analytics fragments — via
    :func:`_is_excluded_phone_shape`, which normalizes a leading ``+``/``(`` or
    trailing ``)`` the regex folds into its group (issue athenaeum#683) before applying
    the checks; every genuine phone fixture (carrying a '+', parens, or
    separators) still matches and is returned verbatim.

    Additionally excludes a run the surrounding prose already TYPES as a record
    id via a preceding-token label (:func:`_has_labeled_identifier_prefix`,
    issue athenaeum#732) — ``QBO realm 1008563730``, GA4 ``stream 5139685489``,
    ``ISBN 978…`` — which needs the match position and so is applied here rather
    than in the token-only :func:`_is_excluded_phone_shape`.
    """
    source = text or ""
    seen: list[str] = []
    for m in _PHONE_RE.finditer(source):
        token = m.group(1)
        if not _has_enough_digits(token):
            continue
        if _is_excluded_phone_shape(token):
            continue
        # A run the surrounding prose already labels as a record id — `QBO realm
        # 1008563730`, GA4 `stream 5139685489`, `ISBN 978…` — is a self-
        # identifying identifier, not a phone (issue athenaeum#732).
        if _has_labeled_identifier_prefix(source[: m.start(1)]):
            continue
        if token not in seen:
            seen.append(token)
    return seen


#: Domains whose addresses are ALWAYS service identifiers, regardless of
#: localpart (issue athenaeum#507). A ``…@group.calendar.google.com`` address is a Google
#: Calendar group id, not a person's contact address — migrating it off the page
#: (redacting the leaf, archiving it as "contact data") would corrupt a calendar
#: reference and lose no real PII. Matched by domain because the localpart is an
#: opaque calendar id.
SERVICE_ADDRESS_DOMAINS: frozenset[str] = frozenset(
    {
        "group.calendar.google.com",
    }
)

#: Exact ``localpart@domain`` pseudo-addresses that are service identifiers, not
#: contact data (issue athenaeum#507). ``git@github.com`` (and its GitLab/Bitbucket
#: siblings) is the SSH pseudo-user in a clone URL — email-*shaped* but a
#: transport identifier. Redacting it out of an entity page would damage a repo
#: reference; it is not a person's address, so nothing archival is lost by
#: leaving it in place.
SERVICE_ADDRESSES: frozenset[str] = frozenset(
    {
        "git@github.com",
        "git@gitlab.com",
        "git@bitbucket.org",
    }
)


def is_service_address(token: str) -> bool:
    """True when an email-shaped *token* is a SERVICE identifier, not contact data.

    The email/phone detectors (:func:`find_inline_emails`) are deliberately
    permissive — they match anything email-*shaped*. Some matches are transport
    or service identifiers rather than a person's address: an SSH clone-URL
    pseudo-user (``git@github.com``) or a Google Calendar group id
    (``…@group.calendar.google.com``). Migrating one would damage the page (a
    broken clone URL / calendar ref) while archiving no real PII. This is the
    EXPLICIT, auditable predicate the athenaeum#507 recursive frontmatter sweep consults
    before treating a detected address as migratable — the excluded set is the
    two named sources above, not a silent heuristic. Matching is
    case-insensitive on the whole address and on the domain.
    """
    normalized = token.strip().lower()
    if normalized in SERVICE_ADDRESSES:
        return True
    domain = normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
    return domain in SERVICE_ADDRESS_DOMAINS


def name_field_holds_pii(meta: dict[str, Any]) -> bool:
    """True when a ``name:`` / ``preferred_name:`` value is email/phone-shaped.

    The athenaeum#502 name-is-an-email population: ~80 live pages whose NAME is a raw
    contact address (Streak email-only import). These are deliberately EXCLUDED
    from the automatic migration path — renaming a page changes its slug and
    breaks inbound edges (:data:`DURABLE_IDENTIFIER_FIELDS`) — and handled in a
    separate slice. This predicate lets the bulk migrator COUNT and surface the
    excluded population so the operator sees exactly how many pages the
    follow-up slice must cover, rather than the class silently vanishing.
    """
    if not meta:
        return False
    for field in NAME_FIELDS:
        raw = meta.get(field)
        if raw is None:
            continue
        for value in raw if isinstance(raw, list) else [raw]:
            s = str(value)
            if find_inline_emails(s) or find_inline_phones(s):
                return True
    return False


# ---------------------------------------------------------------------------
# 2c. Name-is-an-email local-part -> display-name derivation (issue athenaeum#505)
# ---------------------------------------------------------------------------
#
# athenaeum#502 found ~80 live pages whose ``name:`` / ``preferred_name:`` IS an email
# address (Streak email-only import) and deliberately left them unmigrated —
# renaming a page changes its slug and breaks inbound ``related:``/alias
# edges, so it needed its own slice (this one). The operator's decision
# (athenaeum#505, APPROACH 1): derive a human-readable display name from the
# local-part when possible (e.g. ``jane.doe@acme.com`` -> ``Jane Doe``), move
# the address to the excluded contact record, and rewrite inbound edges — the
# page stays in the corpus under a human-readable name. When a confident name
# cannot be derived, LEAVE the page for manual naming (never guess).

#: Local-parts that are ALWAYS role/service addresses, never a person's name —
#: matched case-insensitively against the WHOLE local-part (not a substring
#: check, so ``information@`` is not mistaken for ``info@``). Deliberately a
#: closed, auditable list rather than a heuristic: renaming ``sales@acme.com``
#: to "Sales" would invent a fictitious person.
ROLE_LOCALPARTS: frozenset[str] = frozenset(
    {
        "info",
        "information",
        "sales",
        "support",
        "admin",
        "administrator",
        "noreply",
        "no-reply",
        "donotreply",
        "contact",
        "hello",
        "help",
        "office",
        "team",
        "billing",
        "accounts",
        "hr",
        "jobs",
        "careers",
        "press",
        "media",
        "marketing",
        "webmaster",
        "postmaster",
        "abuse",
        "security",
        "privacy",
        "legal",
        "enquiries",
        "inquiries",
        "general",
        "mail",
        "email",
        "newsletter",
        "subscribe",
        "unsubscribe",
        "notifications",
        "alerts",
        "feedback",
        "service",
        "services",
        "orders",
        "shop",
        "store",
    }
)

#: Separators that plausibly join name PARTS in a human local-part
#: (``jane.doe``, ``jane_doe``, ``jane-doe``). A local-part with none of
#: these (a single unbroken token) is only confident when it independently
#: looks like a whole first name — see :func:`derive_display_name_from_email`.
_NAME_PART_SEPARATORS = re.compile(r"[._-]")

#: A local-part containing a digit is treated as opaque/system-generated
#: (``jdoe123``, ``a12345``, ``2026report``) rather than a human name — a
#: conservative exclusion, since a real name-part essentially never carries a
#: digit in this corpus's data (the Streak import's numeric/opaque local-parts
#: are exactly the shape the issue calls out to defer).
_HAS_DIGIT_RE = re.compile(r"\d")

#: A ``+tag`` suffix (``jane.doe+work@...``) is a routing tag, not part of the
#: name — but its PRESENCE at all marks the address as one a human deliberately
#: annotated for filtering, which the issue calls out as ambiguous; deferred
#: rather than derived-from-the-part-before-the-plus, so an operator confirms.
_PLUS_TAG_RE = re.compile(r"\+")

#: Minimum length for each name-part segment once split on separators. A
#: 1-2 character segment (``j.doe``, initials) is exactly the "initial-blob"
#: shape the issue says to defer, not guess at.
_MIN_NAME_PART_LEN = 3


def derive_display_name_from_email(email: str) -> str | None:
    """Derive a human-readable display name from an email local-part, or ``None``.

    Implements the athenaeum#505 CONFIDENCE GATE for approach 1. Returns a title-cased
    display name (separators -> spaces) when the local-part looks like a
    dotted/underscored/hyphenated human name (``jane.doe`` -> ``"Jane Doe"``,
    ``jane_doe`` -> ``"Jane Doe"``). Returns ``None`` (DEFER — do not guess)
    for:

    - a role/service local-part (:data:`ROLE_LOCALPARTS`, e.g. ``info@``,
      ``sales@``, ``noreply@``) — reused via whole-local-part membership;
    - a ``+tag`` address (``first.last+tag@``) — the tag itself is a
      routing/filter annotation a human added, which the issue treats as
      ambiguous rather than something to strip-and-guess through;
    - a local-part containing any digit (opaque or numeric local-parts,
      e.g. ``jdoe123``, ``2016import``);
    - a BARE (no ``.``/``_``/``-`` separator) local-part, e.g. ``jdoe``,
      ``mjs``, or even ``jane`` — with no separator there is no reliable,
      dictionary-free way to tell a genuine first name from an initials
      blob (issue athenaeum#505 names ``jdoe``/``mjs`` explicitly; a bare token is
      conservatively deferred across the board rather than guessing which
      unseparated tokens happen to be real first names);
    - an initial-blob EVEN WITH separators (``j.doe``) — any part shorter
      than :data:`_MIN_NAME_PART_LEN` defers the whole address;
    - a malformed/empty local-part (no ``@``, or nothing before it).

    A service address (``git@github.com``) is not specially handled here —
    it is filtered upstream by :func:`is_service_address` before this is ever
    called on a genuine contact address, but calling this directly on one
    would still defer (a bare token, no separator) rather than mis-derive a
    name.
    """
    if "@" not in email:
        return None
    local = email.split("@", 1)[0].strip()
    if not local:
        return None

    if _PLUS_TAG_RE.search(local):
        return None  # `first.last+tag@...` — ambiguous, defer.

    if local.lower() in ROLE_LOCALPARTS:
        return None  # role/service address — never invent a person.

    if _HAS_DIGIT_RE.search(local):
        return None  # opaque or numeric local-part — defer.

    parts = [p for p in _NAME_PART_SEPARATORS.split(local) if p]
    if len(parts) < 2:
        # No separator (or nothing but separators): conservatively defer —
        # a bare token is exactly the initials-blob shape (`jdoe`, `mjs`)
        # the issue calls out, and there is no reliable way to distinguish
        # it from a genuine short first name without a dictionary.
        return None

    # Separator-joined: every part must be alphabetic and long enough — a
    # part like `j` or `mc` (initials) defers the whole address rather than
    # guessing a partial name.
    for part in parts:
        if not part.isalpha() or len(part) < _MIN_NAME_PART_LEN:
            return None

    return " ".join(p.capitalize() for p in parts)


def _frontmatter_contact_values(meta: dict[str, Any]) -> dict[str, list[str]]:
    """Return ``{field: [values]}`` for any non-empty contact field present."""
    found: dict[str, list[str]] = {}
    for field in CONTACT_FRONTMATTER_FIELDS:
        raw = meta.get(field)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v).strip() for v in values if str(v).strip()]
        if values:
            found[field] = values
    return found


def has_inline_contact_fields(meta: dict[str, Any], body: str = "") -> bool:
    """True when *meta*/*body* carry archival contact data on an entity page.

    Checks (a) the ``emails``/``phones`` frontmatter fields for any non-empty
    value, and (b) the body text for an email- or phone-shaped token. Does
    NOT consult :data:`PII_FLAG` — that is a separate belt-and-suspenders
    exclusion signal (point 3), not a suppressor of this lint. A page that is
    flagged ``pii: true`` AND still carries inline contact data is arguably
    doing the right corpus-exclusion thing already, but the lint still
    reports the shape so an operator auditing entity pages sees it (the flag
    changes what the CORPUS does with the page, not whether the page's
    shape is worth flagging).
    """
    if _frontmatter_contact_values(meta):
        return True
    return bool(find_inline_emails(body) or find_inline_phones(body))


def lint_inline_contact_fields(
    meta: dict[str, Any], body: str = "", fpath: Path | None = None
) -> str | None:
    """Return a lint message when an entity page carries inline contact data.

    Mirrors :func:`athenaeum._lint.lint_untyped_memory_class`'s shape: a pure
    function returning ``None`` (nothing to report) or a human-readable
    message naming the file when *fpath* is given. Intended for a batch
    lint pass over a wiki tree; :func:`has_inline_contact_fields` is the
    underlying boolean predicate for callers (e.g. a pydantic validator)
    that want a ``UserWarning`` instead of a collected message — see
    :class:`athenaeum.schemas.PersonWiki`'s ``_warn_inline_contact_fields``.
    """
    if not has_inline_contact_fields(meta, body):
        return None
    fields = sorted(_frontmatter_contact_values(meta))
    reasons: list[str] = []
    if fields:
        reasons.append(f"frontmatter field(s) {fields!r}")
    if find_inline_emails(body):
        reasons.append("email-shaped text in body")
    if find_inline_phones(body):
        reasons.append("phone-shaped text in body")
    detail = "; ".join(reasons)
    msg = f"inline contact data on entity page ({detail})"
    return f"{fpath}: {msg}" if fpath else msg


# ---------------------------------------------------------------------------
# 2b. Corpus-wide PII lint — any file under wiki/, not only entity pages (athenaeum#495)
# ---------------------------------------------------------------------------
#
# The entity-page lint above (:func:`lint_inline_contact_fields`) only ever
# looks at entity pages — the pydantic boundary (:class:`athenaeum.schemas.PersonWiki`)
# runs it per-page, and athenaeum#479's migration walks the same ``wiki/*.md`` entity
# set. athenaeum#495 measured the gap that leaves: 790 pages carry an email in *body
# text with no ``emails:`` frontmatter*, and the sample is dominated by the
# corpus's own ``_``-prefixed queue/index/archive files and a stale ``.bak`` —
# none of which the entity-page lint or the entity-page migration ever open.
# ``_pending_merges_archive.md`` in particular embeds full draft bodies (every
# contact datum copied verbatim) inside ``wiki/``, so it is in the corpus and
# recallable while sitting in the *least* obvious place anyone would look.
#
# This lint closes that by scanning the WHOLE text of EVERY file under
# ``wiki/`` (recursively — ``_``-prefixed files, ``.bak`` files, nested dirs),
# reusing the same :func:`find_inline_emails` / :func:`find_inline_phones`
# detectors so there is one definition of "looks like contact data". It is a
# hard gate (the CLI exits non-zero on any finding — see
# :mod:`athenaeum._cmd_storage`'s ``storage lint-pii``) so a body-text email
# cannot silently regrow after the sweep. The excluded surface lives OUTSIDE
# ``wiki/`` by construction (athenaeum#427/#429), so migrated contact records are never
# scanned here — exactly the property that makes the exclusion worth its cost.


@dataclass(frozen=True)
class CorpusPiiFinding:
    """Inline contact data found in one corpus file (issue athenaeum#495).

    ``emails``/``phones`` are the deduped, order-preserving tokens
    :func:`find_inline_emails` / :func:`find_inline_phones` matched anywhere in
    the file's text (frontmatter or body — the corpus-wide sweep does not
    distinguish, since AC is "zero files under ``wiki/`` contain an inline
    email or phone").
    """

    path: Path
    emails: list[str]
    phones: list[str]


def iter_corpus_files(wiki_root: Path) -> list[Path]:
    """Return every regular file under *wiki_root*, recursively, sorted.

    Unlike the entity-page scans (:func:`athenaeum.storage_migrate.iter_entity_pages`,
    :func:`athenaeum.search._iter_wiki_entries`) this deliberately does NOT skip
    ``_``-prefixed files, does NOT restrict to ``*.md``, and DOES descend into
    subdirectories — the whole point of athenaeum#495 is that the excluded surface is
    only worth as much as the completeness of the sweep, so ``_``-prefixed queue
    files, ``.bak`` backups and anything else living in the corpus are all in
    scope. Missing root yields ``[]`` (never raises).
    """
    if not wiki_root.is_dir():
        return []
    return sorted(p for p in wiki_root.rglob("*") if p.is_file())


def scan_corpus_pii(wiki_root: Path) -> list[CorpusPiiFinding]:
    """Scan every file under *wiki_root* for inline email/phone tokens.

    Returns one :class:`CorpusPiiFinding` per file that carries any
    email/phone-shaped token in its text, in sorted path order. Files that
    cannot be read as UTF-8 text (binary assets) are skipped rather than
    treated as findings — the lint is about text-visible contact data, not
    byte-level scanning. A clean corpus returns ``[]``.
    """
    findings: list[CorpusPiiFinding] = []
    for path in iter_corpus_files(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable / binary asset — not a text PII finding
        emails = find_inline_emails(text)
        phones = find_inline_phones(text)
        if emails or phones:
            findings.append(CorpusPiiFinding(path=path, emails=emails, phones=phones))
    return findings


# ---------------------------------------------------------------------------
# 4. Observation log (append-only JSONL) + supersession + deterministic fold
# ---------------------------------------------------------------------------
#
# Schema (per the issue, settled — not re-litigated here):
#   observation: (identifier, person_id, observed_at, source_msg_id)
#   supersession: (retracts: obs_id, reason, at)
#
# ``identifier -> person`` is ~1:1 in the common case (a taken-over inbox
# still identifies the ORIGINAL person — routing is not identity) but SEVERAL
# persons are allowed for a genuinely shared address (a read returns ALL live
# attributions for that identifier, not just the newest). Temporality
# reconstructs from ``observed_at`` — there is deliberately no pre-modeled
# validity window (``valid_from``/``valid_until``) on an observation; that
# would require deciding IN ADVANCE how long an attribution holds, which is
# exactly the kind of clustering-shaped machinery the deterministic fold
# below is designed to avoid.

#: Schema version stamped on every record (mirrors
#: :data:`athenaeum.provenance.MERGE_PROVENANCE_VERSION`) so a future reader
#: can migrate.
OBSERVATION_LOG_VERSION = 1

#: Ledger filename, written under the contacts (excluded) surface root.
OBSERVATION_LOG_FILENAME = "_observations.jsonl"

#: Sidecar filename for supersession (correction) records — kept separate
#: from the observation ledger itself so an observation file is pure
#: "what was asserted, when" and never needs an in-place rewrite; a
#: correction is always a NEW record in its own append-only file.
SUPERSESSION_LOG_FILENAME = "_observation_supersessions.jsonl"


@dataclass(frozen=True)
class Observation:
    """One append-only observation record.

    ``obs_id`` is caller-supplied (the writer mints it, e.g. a ULID or a
    content hash) — this module does not invent an ID scheme, matching
    :mod:`athenaeum.provenance`'s merge-provenance ledger (which likewise
    takes ``merge_id`` from the caller rather than generating one).
    """

    obs_id: str
    identifier: str
    person_id: str
    observed_at: str
    source_msg_id: str


@dataclass(frozen=True)
class Supersession:
    """One append-only supersession (correction) record.

    Retracts a prior :class:`Observation` by ``obs_id`` — never edits or
    deletes it. ``reason`` is free text (e.g. "inbox reassigned to Janice
    2026-06-01"); ``at`` is the ISO-8601 timestamp the correction itself was
    recorded (distinct from ``observed_at`` on the observation it retracts).
    """

    retracts: str
    reason: str
    at: str


def default_observation_log_path(contacts_root: Path) -> Path:
    """Default observation ledger path: ``<contacts_root>/_observations.jsonl``."""
    return Path(contacts_root) / OBSERVATION_LOG_FILENAME


def default_supersession_log_path(contacts_root: Path) -> Path:
    """Default supersession ledger path: ``<contacts_root>/_observation_supersessions.jsonl``."""
    return Path(contacts_root) / SUPERSESSION_LOG_FILENAME


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Identical discipline to :func:`athenaeum.provenance._append_jsonl_line` /
    :mod:`athenaeum.spend`'s ledger writer: a single small ``O_APPEND`` write
    is atomic on local filesystems, so a crash can at worst leave a torn
    TRAILING line (which the reader skips), never corrupt an
    already-written record. Duplicated (not imported) because
    ``provenance._append_jsonl_line`` is a private helper of that module and
    this ledger is a conceptually separate log — mirroring the pattern is
    the explicit brief, not reusing the private symbol across modules.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_observation_record(
    *,
    obs_id: str,
    identifier: str,
    person_id: str,
    observed_at: str,
    source_msg_id: str,
) -> dict[str, Any]:
    """Build one observation record dict (the on-disk JSONL shape)."""
    return {
        "v": OBSERVATION_LOG_VERSION,
        "obs_id": obs_id,
        "identifier": identifier,
        "person_id": person_id,
        "observed_at": observed_at,
        "source_msg_id": source_msg_id,
    }


def append_observation(
    contacts_root: Path,
    *,
    obs_id: str,
    identifier: str,
    person_id: str,
    observed_at: str,
    source_msg_id: str,
    log_path: Path | None = None,
) -> Observation:
    """Append one observation record. Raises on a write failure (not best-effort).

    Unlike :func:`athenaeum.provenance.record_merge_provenance` (which
    swallows write failures because the merge's file-level side effects have
    already happened by the time it runs), an observation append IS the
    entire side effect here — there is nothing else to protect, so a failure
    must surface to the caller rather than be silently dropped.
    """
    record = build_observation_record(
        obs_id=obs_id,
        identifier=identifier,
        person_id=person_id,
        observed_at=observed_at,
        source_msg_id=source_msg_id,
    )
    target = log_path if log_path is not None else default_observation_log_path(contacts_root)
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return Observation(
        obs_id=obs_id,
        identifier=identifier,
        person_id=person_id,
        observed_at=observed_at,
        source_msg_id=source_msg_id,
    )


def build_supersession_record(
    *, retracts: str, reason: str, at: str | None = None
) -> dict[str, Any]:
    """Build one supersession record dict (the on-disk JSONL shape)."""
    return {
        "v": OBSERVATION_LOG_VERSION,
        "retracts": retracts,
        "reason": reason,
        "at": at if at is not None else _now_iso(),
    }


def append_supersession(
    contacts_root: Path,
    *,
    retracts: str,
    reason: str,
    at: str | None = None,
    log_path: Path | None = None,
) -> Supersession:
    """Append one supersession (correction) record. Raises on write failure.

    ``retracts`` is the ``obs_id`` of the observation being corrected.
    Correction-of-a-correction is expressible (a later supersession can
    retract an earlier one's ``obs_id`` too, since supersessions are not
    addressable independently here — see :func:`fold_observations` for how
    the fold resolves the resulting chain) but the common case is retracting
    an :class:`Observation`.
    """
    record = build_supersession_record(retracts=retracts, reason=reason, at=at)
    target = (
        log_path if log_path is not None else default_supersession_log_path(contacts_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return Supersession(retracts=record["retracts"], reason=record["reason"], at=record["at"])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every well-formed JSON object line from *path*.

    Tolerates a torn/partial trailing line (a crash mid-write) or a hand-edit
    — such lines are skipped, not fatal, mirroring
    :func:`athenaeum.provenance.read_merge_provenance`. Returns ``[]`` when
    the file does not exist.
    """
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            records.append(record)
    return records


def read_observations(
    contacts_root: Path, *, log_path: Path | None = None
) -> list[Observation]:
    """Read every well-formed observation record, in file order."""
    target = log_path if log_path is not None else default_observation_log_path(contacts_root)
    out: list[Observation] = []
    for rec in _read_jsonl(target):
        try:
            out.append(
                Observation(
                    obs_id=str(rec["obs_id"]),
                    identifier=str(rec["identifier"]),
                    person_id=str(rec["person_id"]),
                    observed_at=str(rec["observed_at"]),
                    source_msg_id=str(rec["source_msg_id"]),
                )
            )
        except KeyError:
            continue  # malformed record (missing a required key); skip
    return out


def read_supersessions(
    contacts_root: Path, *, log_path: Path | None = None
) -> list[Supersession]:
    """Read every well-formed supersession record, in file order."""
    target = (
        log_path if log_path is not None else default_supersession_log_path(contacts_root)
    )
    out: list[Supersession] = []
    for rec in _read_jsonl(target):
        try:
            out.append(
                Supersession(
                    retracts=str(rec["retracts"]),
                    reason=str(rec["reason"]),
                    at=str(rec["at"]),
                )
            )
        except KeyError:
            continue
    return out


def fold_observations(
    observations: list[Observation],
    supersessions: list[Supersession] | None = None,
) -> dict[str, list[Observation]]:
    """Deterministically fold observations into ``{identifier: [live obs]}``.

    The read-side "latest uncontradicted" resolution the issue specifies:

    1. Drop every observation whose ``obs_id`` is named by ANY supersession's
       ``retracts`` — a corrected observation is gone, permanently (the
       supersession record itself is never deleted; it just removes its
       target from every future fold).
    2. Group the SURVIVING observations by ``identifier``.
    3. Within each identifier's group, keep one entry per DISTINCT
       ``person_id`` — the most recent survives (by ``observed_at``, then
       ``obs_id`` as a stable tie-break when two observations share a
       timestamp) — but a genuinely different ``person_id`` is NEVER
       collapsed into the same slot. This is what makes a shared-address
       read return ALL currently-attributed persons rather than only the
       newest write for that identifier.

    Deliberately no similarity/clustering step: two observations are "the
    same claim" if and only if they share both ``identifier`` AND
    ``person_id`` — string equality, nothing fuzzier. Two different people
    sharing one address is not a conflict to resolve; it is exactly the
    shared-address case the issue calls out, so both survive. A correction
    (Jason -> Janice) works by RETRACTING the Jason observation via a
    supersession, not by the fold guessing which of two same-identifier
    writes is more authoritative.

    Returns ``{identifier: [Observation, ...]}`` — each identifier's list is
    sorted by ``observed_at`` (then ``obs_id``) for deterministic output;
    an identifier with no surviving observations is simply absent (never an
    empty list).
    """
    retracted = {s.retracts for s in (supersessions or [])}
    live = [o for o in observations if o.obs_id not in retracted]

    by_identifier: dict[str, list[Observation]] = {}
    for obs in live:
        by_identifier.setdefault(obs.identifier, []).append(obs)

    folded: dict[str, list[Observation]] = {}
    for identifier, obs_list in by_identifier.items():
        # Keep the latest observation per distinct person_id (deterministic
        # tie-break on obs_id so two same-timestamp writes fold predictably
        # regardless of input order).
        latest_by_person: dict[str, Observation] = {}
        for obs in obs_list:
            current = latest_by_person.get(obs.person_id)
            if current is None or (obs.observed_at, obs.obs_id) > (
                current.observed_at,
                current.obs_id,
            ):
                latest_by_person[obs.person_id] = obs
        folded[identifier] = sorted(
            latest_by_person.values(), key=lambda o: (o.observed_at, o.obs_id)
        )
    return folded


def resolve_identifier(
    identifier: str,
    observations: list[Observation],
    supersessions: list[Supersession] | None = None,
) -> list[Observation]:
    """Convenience: fold, then return the live observations for one identifier.

    Returns ``[]`` when the identifier has no surviving observations (never
    seen, or every observation for it was retracted).
    """
    return fold_observations(observations, supersessions).get(identifier, [])


# ---------------------------------------------------------------------------
# 5. Hard-bounce recognition + mark (issue athenaeum#765)
# ---------------------------------------------------------------------------
#
# Voltaire's ``bounce.ts`` (see ``docs/deprecated-email-tracking.md``,
# superseded by this issue) gates a HARD bounce on an RFC 3463 enhanced
# delivery-status code of the PERMANENT-FAILURE class, ``5.x.x``; a ``4.x``
# code is a transient give-up (voltaire#81's "potentially stale" case, e.g. a
# live address behind a temporary Office 365 routing misconfiguration) and is
# deliberately OUT OF SCOPE — marking a ``4.x``-diagnosed address bounced
# would be wrong. :func:`detect_hard_bounce_fact` keys on that same ``5.x.x``
# signal, so a ``4.x`` report (or an ambiguous one naming more than one
# address) simply never matches and falls through to the ordinary
# prose-classification tiers untouched — "nothing is rejected, a
# non-conformant note just climbs a tier," the same posture every other
# raw-intake shape in this pipeline already has.
#
# The mark is a VALID-TIME close (``valid_until``), reusing the EXISTING
# claim-validity mechanism (issue athenaeum#308, :func:`athenaeum.models.valid_until_expired`)
# rather than inventing a ``bounced``/``deprecated`` status enum — "deliverable
# until the observed date" is exactly what that mechanism already expresses.
# No new ledger file: the mark is upserted onto the identifier's own
# contact-record frontmatter on the excluded contacts surface, in place —
# idempotent (re-reporting the identical fact is a no-op), and the identifier
# is never deleted, only ever gains fields.

#: An RFC 3463 enhanced delivery-status code restricted to the ``5.x.x``
#: permanent-failure class. Matches ``550 5.1.1 user unknown`` and bare
#: ``5.1.1`` alike; never matches a ``4.x.x`` transient code.
_HARD_BOUNCE_CODE_RE = re.compile(r"\b5\.\d{1,3}\.\d{1,3}\b")


@dataclass(frozen=True)
class HardBounceFact:
    """A hard-bounce fact recognized in ordinary free-text raw intake.

    ``identifier`` is the single email-shaped token the note names;
    ``diagnostic`` is the verbatim line the ``5.x.x`` status code was found
    on (falling back to the bare matched code if line-splitting somehow
    yields nothing, which cannot happen for the input :func:`detect_hard_bounce_fact`
    itself already required to match).
    """

    identifier: str
    diagnostic: str


def detect_hard_bounce_fact(text: str) -> HardBounceFact | None:
    """Recognize a hard-bounce fact in free text, or ``None`` — never guesses.

    Deliberately conservative: BOTH of these must hold, or this declines so
    the note falls through to ordinary prose classification exactly like any
    other raw intake file (issue athenaeum#765 — "nothing is rejected"):

    - exactly ONE email-shaped token (:func:`find_inline_emails`) — a note
      naming zero or several addresses is ambiguous and left to reasoning,
      not guessed at;
    - a ``5.x.x`` enhanced-status-code diagnostic
      (:data:`_HARD_BOUNCE_CODE_RE`) somewhere in the text — a ``4.x`` (or
      no) diagnostic never matches, which is what keeps voltaire#81's
      transient case out of scope by construction rather than by a separate
      status check.
    """
    emails = find_inline_emails(text or "")
    if len(emails) != 1:
        return None
    match = _HARD_BOUNCE_CODE_RE.search(text or "")
    if match is None:
        return None
    diagnostic = next(
        (line.strip() for line in (text or "").splitlines() if match.group(0) in line),
        match.group(0),
    )
    return HardBounceFact(identifier=emails[0], diagnostic=diagnostic)


def default_bounce_record_path(contacts_root: Path, identifier: str) -> Path:
    """Per-identifier contact-record path under the (excluded) contacts surface.

    One record per IDENTIFIER (not per person) — the bounce is a fact about
    the address's own deliverability, independent of whose it is or whether
    that attribution is even known.
    """
    return Path(contacts_root) / f"contact-{slugify(identifier)}.md"


def read_bounce_record(record_path: Path) -> dict[str, Any]:
    """Read an existing contact record's frontmatter, or ``{}`` if absent."""
    if not record_path.exists():
        return {}
    meta, _ = parse_frontmatter(record_path.read_text(encoding="utf-8"))
    return meta if isinstance(meta, dict) else {}


def is_bounced(meta: dict[str, Any] | None, as_of: date | None = None) -> bool:
    """True when a contact record's ``valid_until`` has passed — present, non-deliverable.

    The single read-side predicate a consumer (recall, lint) calls to tell a
    bounced-but-still-present identifier apart from one that was never seen
    at all (an absent/missing record, or a record with no ``valid_until`` —
    both read as ``False`` here, mirroring :func:`is_pii_flagged`'s
    "single predicate every consumer calls" shape). Delegates entirely to
    :func:`athenaeum.models.valid_until_expired` — the SAME upper-bound
    predicate already wired into recall/compile for every other claim's
    ``valid_until`` — rather than a second, drifting implementation.
    """
    return valid_until_expired(meta, as_of)


def mark_bounced(
    contacts_root: Path,
    identifier: str,
    *,
    diagnostic: str,
    observed_at: str,
    source: str | dict[str, Any],
    record_path: Path | None = None,
) -> tuple[Path, bool]:
    """Upsert the hard-bounce mark onto *identifier*'s contact record. Idempotent.

    Creates the record if absent; otherwise merges onto the EXISTING
    frontmatter (any other field already on the record survives byte-for-byte
    — this never overwrites the whole file, only sets its own keys) and
    rewrites atomically. Never deletes the record or the identifier.

    Returns ``(record_path, changed)``. ``changed`` is ``False`` — no write —
    when the merged frontmatter is byte-for-byte identical to what is
    already on disk (the delta gate :func:`athenaeum.librarian.tier0_handle_upsert`
    already uses for the same reason: re-reporting the identical fact must be
    a true no-op, never a duplicate mark).

    Encodes deliverability as a VALID-TIME close: ``valid_until`` is set to
    *observed_at* (see the module-docstring's point 5 for why this is not a
    ``bounced``/``deprecated`` status enum). Re-reporting a LATER bounce
    (a different *observed_at* / *diagnostic*) updates the same record in
    place rather than duplicating it.
    """
    target = (
        record_path
        if record_path is not None
        else default_bounce_record_path(contacts_root, identifier)
    )
    existing_meta = read_bounce_record(target)
    existing_body = ""
    if target.exists():
        _, existing_body = parse_frontmatter(target.read_text(encoding="utf-8"))

    merged_meta = dict(existing_meta)
    merged_meta["identifier"] = identifier
    merged_meta[PII_FLAG] = True
    merged_meta["bounce_diagnostic"] = diagnostic
    merged_meta["observed_at"] = observed_at
    merged_meta["valid_until"] = observed_at
    merged_meta["source"] = source

    if merged_meta == existing_meta:
        return target, False

    body = existing_body or (
        f"Contact record for {identifier!r} — marked non-deliverable (hard "
        "bounce, issue athenaeum#765). A historical identifier: present, not "
        "deleted.\n"
    )
    atomic_write_text(target, render_frontmatter(merged_meta) + "\n" + body)
    return target, True


__all__ = [
    "PII_ENTITY_CLASS",
    "PII_FLAG",
    "CONTACT_FRONTMATTER_FIELDS",
    "DURABLE_IDENTIFIER_FIELDS",
    "NAME_FIELDS",
    "name_field_holds_pii",
    "ROLE_LOCALPARTS",
    "derive_display_name_from_email",
    "OBSERVATION_LOG_VERSION",
    "OBSERVATION_LOG_FILENAME",
    "SUPERSESSION_LOG_FILENAME",
    "Observation",
    "Supersession",
    "contacts_surface_root",
    "is_pii_class_excluded",
    "is_pii_flagged",
    "find_inline_emails",
    "find_inline_phones",
    "SERVICE_ADDRESS_DOMAINS",
    "SERVICE_ADDRESSES",
    "is_service_address",
    "has_inline_contact_fields",
    "lint_inline_contact_fields",
    "CorpusPiiFinding",
    "iter_corpus_files",
    "scan_corpus_pii",
    "default_observation_log_path",
    "default_supersession_log_path",
    "build_observation_record",
    "build_supersession_record",
    "append_observation",
    "append_supersession",
    "read_observations",
    "read_supersessions",
    "fold_observations",
    "resolve_identifier",
    "HardBounceFact",
    "detect_hard_bounce_fact",
    "default_bounce_record_path",
    "read_bounce_record",
    "is_bounced",
    "mark_bounced",
]
