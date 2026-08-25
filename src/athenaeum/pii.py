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

6. **One-call entity read** (:func:`read_entity`, issue athenaeum#883/#886) — the
   single sanctioned entry point for reading any entity's wiki page together
   with its excluded/contact data by uid. It is the concrete realization of
   the egress half of the two-path invariant
   (``docs/one-way-in-one-way-out.md`` §3) for the excluded-data surface: it
   resolves the surface root itself, so a caller supplies only a ``uid`` and a
   boolean, never a path. With inclusion off (the default), each withheld
   field is reported as a :class:`RedactionMarker` naming the field and that a
   value exists — never the value — so a caller can tell "redacted" from
   "absent" instead of both collapsing to the same missing key. Reachable
   from the MCP server (``read_entity`` tool) and ``athenaeum query entity``.

   :func:`read_entities` is the BATCH form of the same read (issue
   athenaeum#877), and the one to reach for whenever more than one uid is
   resolved in a single process. ``read_entity`` rebuilds two O(corpus)
   indexes per call — the wiki :class:`~athenaeum.models.EntityIndex` and a
   full :func:`iter_contact_records` scan — which is fine for the occasional
   single lookup and quadratic-in-practice for a loop: ~28s per uid against
   the live 16,928-page store, or ~37 hours for the 4,696-person population
   ``apollo-enrich``'s weekly job resolves. ``read_entities`` builds each
   index exactly once per batch (:func:`build_contact_record_uid_index` is
   the contacts half) and returns identical values, so the fix is a cost
   change and not a semantic one. Both entry points share one assembly body
   (``_person_read_from_indexes``) so they cannot drift.

   The original person-shaped entry points (``read_person``/``read_people``,
   issue athenaeum#864/#877) were deprecated in athenaeum#887 and removed in
   athenaeum#888 once every known consumer had migrated to the generic form
   above; ``PersonRead`` (a back-compat alias of :class:`EntityRead`) was
   removed in the same change, having no remaining referrers.

7. **Facts, not verdicts** (issue athenaeum#851) — athenaeum returns what it
   knows and how it knows it; the consumer decides what to do about it.
   Concretely, an authorized reader gets, for every contact value: the value,
   its usage/provenance classification (:class:`ContactClassification`), and
   its validity state including any valid-time close
   (:class:`IdentifierValidity`) — plus, per record, the do-not-email mark
   with provenance (:class:`DoNotEmailState`). Those ride the EXISTING read
   path: ``recall(with_pii=True)`` and :func:`read_entity`, plus
   :func:`read_identifier_facts` for the bulk by-address case.

   **Explicit non-goal: no suppression/eligibility predicate ships here, and
   the next lane should not re-derive one.** "May I email this person" folds
   deliverability, an operator's do-not-email mark, provenance and campaign
   policy into one boolean, and that boolean is an ACTION decision belonging
   to the caller — putting it in the memory layer both moves policy into
   storage and forks the read seam athenaeum#888 is consolidating. The
   originally-filed ``suppression_state()`` was cancelled for exactly this
   reason; see athenaeum#851's decision comment and
   ``docs/authorized-reader-contract.md``. (:func:`is_outreach_eligible` is
   NOT that predicate and is not a precedent for one: it reports a single
   value's usage class and deliberately does not consult bounce state — its
   own docstring says so.)

   Two properties of these facts are load-bearing and easy to lose. **Unknown
   is stated, never inferred**: :attr:`IdentifierFacts.known` positively
   distinguishes "we have never heard of this address" from "we know it and
   hold nothing against it", so a consumer cannot silently treat strangers as
   safe. And the read is **fail-closed**: an unreachable surface raises
   :class:`ExcludedSurfaceUnavailable` rather than reporting an empty result
   a caller would read as "nothing suppressed" — a false skip is recoverable
   by a human, a false send is not.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import (
    EntityIndex,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    valid_until_expired,
)
from athenaeum.storage import surface_root_for_class
from athenaeum.store import append_line_durable

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


def excluded_surface_root(
    entity_class: str,
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> Path:
    """Resolve the on-disk root for ANY surface class (issue athenaeum#883).

    The class-parameterized generalization of :func:`contacts_surface_root`,
    which hardcoded :data:`PII_ENTITY_CLASS`. It delegates to
    :func:`athenaeum.storage.surface_root_for_class` exactly as that function
    always has — the adapter layer was already fully class-parameterized
    (athenaeum#427/#429); the caller simply never got to say which class.

    *entity_class* is the **surface class** — the ``storage.mapping`` key,
    e.g. ``"pii"`` — and NOT the wiki page's ``type:``. The distinction is
    load-bearing: a person page is ``type: person`` while its excluded record
    lives on the ``pii`` surface. This function is TOLD the surface class and
    never guesses; mapping a page class onto a surface class is a separate
    concern (issue athenaeum#885's ``storage.excluded_read_mapping``).

    Absent any ``storage.mapping`` entry for *entity_class*, this resolves to
    the default wiki surface — a no-op convenience, not a silent leak. The
    operator must explicitly map a class to the ``excluded`` adapter for this
    root to land outside the corpus.
    """
    return surface_root_for_class(entity_class, config, knowledge_root)


#: Built-in page-class → surface-class table (issue athenaeum#885). The default is
#: IDENTITY — every wiki ``type:`` joins a same-named surface — with exactly
#: one shipped non-identity entry: a ``type: person`` page's excluded record
#: lives on the ``pii`` surface, which is the whole reason the page class and
#: the surface class cannot be collapsed into one name. Operator-overridable
#: via ``storage.excluded_read_mapping``.
DEFAULT_EXCLUDED_READ_MAPPING: dict[str, str] = {"person": PII_ENTITY_CLASS}


def surface_class_for_page_class(
    page_class: str | None,
    config: dict[str, Any] | None,
) -> str:
    """The surface class whose excluded record holds *page_class*'s excluded fields.

    Resolution: the operator's ``storage.excluded_read_mapping`` first, then
    :data:`DEFAULT_EXCLUDED_READ_MAPPING`, then IDENTITY (the page class joins a
    same-named surface). An operator entry wins over the built-in, so
    ``person`` can be pointed elsewhere — or back at identity — without a code
    change.

    Answering this question is NOT the same as deciding a join should happen.
    A page class whose mapped surface class is not actually excluded (every
    class on a base that maps only ``pii: excluded``) resolves fine here and
    must then be refused by :func:`athenaeum.storage.is_excluded` at the call
    site — otherwise a read would scan the WIKI ROOT as if it were an excluded
    surface. This function maps; the gate decides.
    """
    cls = (page_class or "").strip()
    if not cls:
        return ""
    from athenaeum.config import resolve_excluded_read_mapping

    configured = resolve_excluded_read_mapping(config)
    if cls in configured:
        return configured[cls]
    return DEFAULT_EXCLUDED_READ_MAPPING.get(cls, cls)


def contacts_surface_root(
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> Path:
    """Resolve the on-disk root for the ``pii`` entity class.

    A one-line wrapper over :func:`excluded_surface_root` passing
    :data:`PII_ENTITY_CLASS`. Kept (not deleted) because it is the shape every
    existing caller in this repo and in ``apollo-enrich`` already calls; the
    generalization added a parameter, it did not move the entry point.
    """
    return excluded_surface_root(PII_ENTITY_CLASS, knowledge_root, config)


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


def iter_corpus_files(wiki_root: Path, *, exclude: Iterable[Path] = ()) -> list[Path]:
    """Return every regular file under *wiki_root*, recursively, sorted.

    Unlike the entity-page scans (:func:`athenaeum.storage_migrate.iter_entity_pages`,
    :func:`athenaeum.search._iter_wiki_entries`) this deliberately does NOT skip
    ``_``-prefixed files, does NOT restrict to ``*.md``, and DOES descend into
    subdirectories — the whole point of athenaeum#495 is that the excluded surface is
    only worth as much as the completeness of the sweep, so ``_``-prefixed queue
    files, ``.bak`` backups and anything else living in the corpus are all in
    scope. Missing root yields ``[]`` (never raises).

    *exclude* is the ONE narrow escape hatch (athenaeum#936): the adjudicated
    allowlist (:func:`load_pii_allowlist`) is by construction a file containing
    one verbatim contact value per entry, so scanning it would make every
    adjudicated value a fresh finding and put exit 0 permanently out of reach.
    Paths are compared after ``resolve()`` so a relative/symlinked spelling of
    the same file still matches. Nothing else is ever excluded — the sweep's
    completeness is the whole point of athenaeum#495.
    """
    if not wiki_root.is_dir():
        return []
    skip = set()
    for p in exclude:
        try:
            skip.add(p.resolve())
        except OSError:  # pragma: no cover - defensive (unresolvable path)
            continue
    out: list[Path] = []
    for p in sorted(wiki_root.rglob("*")):
        if not p.is_file():
            continue
        try:
            resolved = p.resolve()
        except OSError:  # pragma: no cover - defensive
            resolved = p
        if resolved in skip:
            continue
        out.append(p)
    return out


def scan_corpus_pii(wiki_root: Path, *, exclude: Iterable[Path] = ()) -> list[CorpusPiiFinding]:
    """Scan every file under *wiki_root* for inline email/phone tokens.

    Returns one :class:`CorpusPiiFinding` per file that carries any
    email/phone-shaped token in its text, in sorted path order. Files that
    cannot be read as UTF-8 text (binary assets) are skipped rather than
    treated as findings — the lint is about text-visible contact data, not
    byte-level scanning. A clean corpus returns ``[]``.

    *exclude* is forwarded to :func:`iter_corpus_files` — see its docstring for
    why the adjudicated allowlist must not scan itself (athenaeum#936).
    """
    findings: list[CorpusPiiFinding] = []
    for path in iter_corpus_files(wiki_root, exclude=exclude):
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
# 2c. Adjudicated allowlist for the corpus lint (athenaeum#936)
# ---------------------------------------------------------------------------
#
# athenaeum#437's acceptance criterion is "lint-pii exits 0, OR every remaining
# finding appears in a committed allowlist, one entry per distinct value, each
# carrying a one-line reason". Both branches were unreachable before this:
# nothing read an allowlist, and — because :func:`iter_corpus_files` scans
# every file under ``wiki/`` — authoring one would have RAISED the finding
# count, since the artifact is by construction a list of verbatim contact
# values. The self-exclusion in :func:`iter_corpus_files` is what makes exit 0
# reachable at all; without it this feature cannot do its job.
#
# Why adjudication rather than deletion: athenaeum#437's residue splits into data that
# is genuinely a person's (operator migration work) and data that is not a
# person at all — service accounts, tagged test addresses, example-domain
# placeholders, digit runs that are identifiers or timestamps misread by the
# phone axis. Deleting the second class destroys true, non-personal facts;
# that is the action that cost athenaeum#691 two restore passes. Adjudication
# records a human's "this is not PII, and here is why" instead.
#
# The gate stays honest in both directions: an unexplained finding still fails
# (a value is never tolerated by OMISSION — silence is not adjudication), and
# an entry matching nothing is surfaced as STALE so the artifact cannot rot
# into a permanent blanket over values that have since left the corpus.

#: Conventional filename of the adjudicated allowlist, resolved under the wiki
#: root. `_`-prefixed so it sorts with the corpus's other bookkeeping files;
#: overridable via ``lint-pii --allowlist`` (athenaeum#936 / athenaeum#437).
PII_ALLOWLIST_FILENAME = "_pii-allowlist.yml"


@dataclass(frozen=True)
class PiiAllowlistEntry:
    """One adjudicated value: "this token is not PII, and here is why"."""

    value: str
    reason: str


@dataclass(frozen=True)
class PiiAdjudicatedFinding:
    """A corpus finding split into its adjudicated and unexplained halves.

    ``allowlisted`` are tokens covered by an allowlist entry; the two
    ``unexplained_*`` lists are what still fails the gate. A finding is fully
    adjudicated when both unexplained lists are empty.
    """

    path: Path
    allowlisted: list[str]
    unexplained_emails: list[str]
    unexplained_phones: list[str]

    @property
    def is_adjudicated(self) -> bool:
        """True when nothing on this file remains unexplained."""
        return not self.unexplained_emails and not self.unexplained_phones


@dataclass(frozen=True)
class PiiAdjudication:
    """The whole corpus scan, adjudicated against the allowlist."""

    findings: list[PiiAdjudicatedFinding]
    stale: list[PiiAllowlistEntry]
    errors: list[str]

    @property
    def unexplained_count(self) -> int:
        """Number of tokens no allowlist entry explains — the gate's subject."""
        return sum(len(f.unexplained_emails) + len(f.unexplained_phones) for f in self.findings)

    @property
    def adjudicated_count(self) -> int:
        """Number of tokens an allowlist entry explains (reported, not failed)."""
        return sum(len(f.allowlisted) for f in self.findings)

    @property
    def is_clean(self) -> bool:
        """True when the gate should exit 0: nothing unexplained anywhere."""
        return self.unexplained_count == 0


def load_pii_allowlist(path: Path) -> tuple[list[PiiAllowlistEntry], list[str]]:
    """Load the adjudicated allowlist at *path*.

    Returns ``(entries, errors)``. A MISSING FILE IS NOT AN ERROR — it means
    "nothing adjudicated", so behaviour is exactly as it was before athenaeum#936
    (``([], [])``).

    Schema — one entry per distinct value, each carrying a required non-empty
    ``reason``::

        - value: "noreply@example.com"
          reason: "service account, not a person"

    Malformed input is REPORTED AND SKIPPED, never raised and never partially
    trusted (mirroring :func:`athenaeum.rules.load_shape_rules`). This fails
    CLOSED by construction: a skipped entry adjudicates nothing, so whatever it
    would have covered stays unexplained and the gate still fails. `yaml.safe_load`
    only — an allowlist is DATA, never executed.
    """
    import yaml

    errors: list[str] = []
    if not path.is_file():
        return [], errors
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        return [], [f"{path}: unreadable or malformed YAML -- {exc}"]
    if raw is None:
        return [], errors
    if not isinstance(raw, list):
        return [], [f"{path}: top-level YAML must be a list of entries, got {type(raw).__name__}"]

    entries: list[PiiAllowlistEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"{path}: entry {i}"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be a mapping, got {type(item).__name__}")
            continue
        value = item.get("value")
        reason = item.get("reason")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{where}: missing a non-empty 'value'")
            continue
        if not isinstance(reason, str) or not reason.strip():
            # A value cannot be tolerated by omission: an entry with no stated
            # reason adjudicates nothing, so its value stays unexplained.
            errors.append(f"{where} ({value!r}): missing a non-empty 'reason'")
            continue
        if value in seen:
            errors.append(f"{where} ({value!r}): duplicate value")
            continue
        seen.add(value)
        entries.append(PiiAllowlistEntry(value=value, reason=reason.strip()))
    return entries, errors


def adjudicate_corpus_pii(
    findings: Iterable[CorpusPiiFinding],
    entries: Iterable[PiiAllowlistEntry],
    *,
    errors: Iterable[str] = (),
) -> PiiAdjudication:
    """Split *findings* into adjudicated vs unexplained against *entries*.

    A token is adjudicated when an entry's ``value`` matches it exactly. Every
    file keeps an entry in the result (so the two populations stay countable
    per file) — read :attr:`PiiAdjudicatedFinding.is_adjudicated` rather than
    the presence of the record.

    Any entry that matched nothing anywhere in the corpus is returned in
    ``stale``, so the artifact is kept honest as the corpus changes instead of
    quietly becoming a blanket over values that are no longer there.
    """
    allowed = {e.value: e for e in entries}
    matched: set[str] = set()
    out: list[PiiAdjudicatedFinding] = []

    for f in findings:
        allowlisted: list[str] = []
        unexplained_emails: list[str] = []
        unexplained_phones: list[str] = []
        for token in f.emails:
            if token in allowed:
                allowlisted.append(token)
                matched.add(token)
            else:
                unexplained_emails.append(token)
        for token in f.phones:
            if token in allowed:
                allowlisted.append(token)
                matched.add(token)
            else:
                unexplained_phones.append(token)
        out.append(
            PiiAdjudicatedFinding(
                path=f.path,
                allowlisted=allowlisted,
                unexplained_emails=unexplained_emails,
                unexplained_phones=unexplained_phones,
            )
        )

    stale = [e for v, e in allowed.items() if v not in matched]
    return PiiAdjudication(findings=out, stale=stale, errors=list(errors))


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
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


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
    target = log_path if log_path is not None else default_supersession_log_path(contacts_root)
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


def read_observations(contacts_root: Path, *, log_path: Path | None = None) -> list[Observation]:
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


def read_supersessions(contacts_root: Path, *, log_path: Path | None = None) -> list[Supersession]:
    """Read every well-formed supersession record, in file order."""
    target = log_path if log_path is not None else default_supersession_log_path(contacts_root)
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


def find_hard_bounce_code(text: str) -> str | None:
    """Return the first ``5.x.x`` hard-failure code in *text*, or ``None``.

    The public half of :data:`_HARD_BOUNCE_CODE_RE`, split out (issue athenaeum#854)
    so a caller that needs to report *which* Tier-0 condition a candidate note
    failed — :mod:`athenaeum.bounce_contract` — can ask this question with the
    SAME predicate :func:`detect_hard_bounce_fact` gates on, rather than
    re-deriving the code shape and drifting from it.
    """
    match = _HARD_BOUNCE_CODE_RE.search(text or "")
    return match.group(0) if match is not None else None


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
    code = find_hard_bounce_code(text or "")
    if code is None:
        return None
    diagnostic = next(
        (line.strip() for line in (text or "").splitlines() if code in line),
        code,
    )
    return HardBounceFact(identifier=emails[0], diagnostic=diagnostic)


#: Direct-instruction shape: "do not email <address>", "don't email X",
#: "must not be emailed", "should not be contacted by email". Matches the
#: maecenas opt-out migration's exact wording (issue athenaeum#1121).
_DO_NOT_EMAIL_INSTRUCTION_RE = re.compile(
    r"\b(?:do not|don'?t|must not|should not) (?:e-?mail\b|contact\b.{0,40}\bemail)",
    re.I,
)

#: Reported-opt-out shape: the statement attributes the "stop emailing me"
#: request to the person themselves, rather than issuing it as an
#: instruction. Issue athenaeum#1121 treats the two as equivalent — the
#: difference is provenance (who said it), not whether the person opted out.
_DO_NOT_EMAIL_OPTOUT_RE = re.compile(
    r"\basked (?:not to be (?:emailed|contacted)|to (?:stop receiving email|opt out|unsubscribe))\b"
    r"|\bopted out\b",
    re.I,
)


@dataclass(frozen=True)
class DoNotEmailFact:
    """A do-not-email fact recognized in ordinary free-text raw intake.

    ``identifier`` is the single email-shaped token the note names;
    ``reason`` is the verbatim sentence the instruction/opt-out phrase was
    found in (falling back to the first line of the note), used to populate
    ``do_not_email_reason`` on the target wiki page (issue athenaeum#1121).
    """

    identifier: str
    reason: str


def detect_do_not_email_fact(text: str) -> DoNotEmailFact | None:
    """Recognize a do-not-email fact in free text, or ``None`` — never guesses.

    Issue athenaeum#1121: an intake statement sets ``do_not_email: true`` when
    its subject is an address or a person and its predicate is that they must
    not be emailed — covering both the direct instruction ("Do not email
    ``<address>``") and the reported opt-out ("X asked to stop receiving
    email"). Deliberately conservative, mirroring :func:`detect_hard_bounce_fact`'s
    shape:

    - exactly ONE email-shaped token (:func:`find_inline_emails`) — a note
      naming zero or several addresses is ambiguous and left to reasoning,
      not guessed at;
    - a recognized instruction or reported-opt-out phrase
      (:data:`_DO_NOT_EMAIL_INSTRUCTION_RE` / :data:`_DO_NOT_EMAIL_OPTOUT_RE`)
      somewhere in the text;
    - NOT a hard-bounce report (:func:`find_hard_bounce_code`) — a ``5.x.x``
      diagnostic is a deliverability fact, never consent (maecenas#95's
      operator ruling), and :func:`tier0_bounce_mark` owns that shape
      exclusively.

    Asymmetry governs the two thresholds above: a false positive here costs
    one un-emailed contact; a false negative emails someone who asked not to
    be. The single-address and bounce-exclusion gates keep false positives
    rare without giving up on recall for the shapes this issue names.
    """
    emails = find_inline_emails(text or "")
    if len(emails) != 1:
        return None
    if find_hard_bounce_code(text or "") is not None:
        # Deliverability, not consent — leave it to tier0_bounce_mark.
        return None
    instruction_match = _DO_NOT_EMAIL_INSTRUCTION_RE.search(text or "")
    optout_match = _DO_NOT_EMAIL_OPTOUT_RE.search(text or "")
    if instruction_match is None and optout_match is None:
        return None

    # Reason = the sentence the matched phrase appears in, trimmed. Sentence
    # boundaries require a following whitespace/end-of-string (not just any
    # ``.``) so a domain period inside the matched email address itself
    # (e.g. "namwil.com") is never mistaken for a sentence break. Falls back
    # to the first non-empty line if sentence-splitting finds nothing (cannot
    # happen for input that already matched above, but mirrors
    # detect_hard_bounce_fact's defensive fallback shape).
    match = instruction_match or optout_match
    assert match is not None
    body = text or ""
    _sentence_break_re = re.compile(r"[.!?](?=\s|$)")
    sentence_start = 0
    for boundary in _sentence_break_re.finditer(body, 0, match.start()):
        sentence_start = boundary.end()
    end_boundary = _sentence_break_re.search(body, match.end())
    sentence_end = end_boundary.end() if end_boundary is not None else len(body)
    reason = body[sentence_start:sentence_end].strip()
    if not reason:
        reason = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return DoNotEmailFact(identifier=emails[0], reason=reason)


def default_bounce_record_path(contacts_root: Path, identifier: str) -> Path:
    """Per-identifier contact-record path under the (excluded) contacts surface.

    The FALLBACK placement, used only when no existing record already lists
    the address (issue athenaeum#850 — :func:`resolve_contact_record` is asked first).
    One record per IDENTIFIER, so the bounce is recorded even when nothing on
    the surface knows whose address it is.
    """
    return Path(contacts_root) / f"contact-{slugify(identifier)}.md"


#: List-valued frontmatter fields on a contacts-surface record that hold the
#: ADDRESSES a person is known by. :func:`resolve_contact_record` scans these
#: to answer "does a record for this address already exist?" before
#: :func:`mark_bounced` falls back to minting a slug-keyed record (issue athenaeum#850).
#: ``emails`` is what the athenaeum#479/#502 migrator writes onto every record it
#: creates (see :func:`athenaeum.storage_migrate._render_excluded_record`);
#: ``former_emails`` / ``alt_emails`` are folded INTO ``emails`` by that
#: migrator but a hand-authored or earlier-migrated record can still carry
#: them, and they hold the same kind of value — an address this person is
#: known by — so resolution reads all three.
CONTACT_IDENTIFIER_FIELDS: tuple[str, ...] = ("emails", "former_emails", "alt_emails")

#: Frontmatter key holding PER-IDENTIFIER valid-time closes on a record that
#: lists several addresses (issue athenaeum#850). See :func:`mark_bounced` for why a
#: person record cannot carry the mark as a bare top-level ``valid_until``.
IDENTIFIER_VALIDITY_FIELD = "identifier_validity"

#: Frontmatter key holding PER-VALUE provenance + usage classification on a
#: contact record (issue athenaeum#866). Deliberately the same shape as
#: :data:`IDENTIFIER_VALIDITY_FIELD` — a list of dicts keyed by
#: ``identifier`` — because it answers the same *kind* of question ("what do
#: we know about THIS address, not this record") and a record listing several
#: addresses cannot carry either mark as a bare top-level field without
#: asserting it of the person.
CONTACT_CLASSIFICATION_FIELD = "contact_classification"

#: Usage class: the address was seen in real communication with this person —
#: they wrote to us from it, or we have corresponded with it before. Evidence
#: of use, which is what makes outreach to it a continuation rather than an
#: initiation.
USAGE_CLASS_OBSERVED = "observed"

#: Usage class: the address was supplied by a data provider / vendor. Storable
#: and syncable to an address book, NOT eligible for outreach absent prior
#: communication — the distinction issue athenaeum#866 exists to make
#: representable.
USAGE_CLASS_PROVIDER = "provider"

#: Usage class for a value carrying no classification entry at all — every
#: contact value written before issue athenaeum#866. NOT a synonym for
#: "usable": an unclassified value is one whose provenance was never recorded,
#: so nothing is known about how it was obtained. It is reported as
#: ``unclassified`` and is NOT outreach-eligible (AC: "never silently
#: defaulted to usable").
USAGE_CLASS_UNCLASSIFIED = "unclassified"

#: Every usage class, most-authoritative first. Order is load-bearing: it IS
#: the no-downgrade ladder :func:`_classification_outranks` reads.
USAGE_CLASSES: tuple[str, ...] = (
    USAGE_CLASS_OBSERVED,
    USAGE_CLASS_PROVIDER,
    USAGE_CLASS_UNCLASSIFIED,
)

#: The usage classes a value may be used to INITIATE contact from. Only
#: :data:`USAGE_CLASS_OBSERVED` qualifies — address-book population and
#: outreach eligibility are different permissions (issue athenaeum#866), and
#: this tuple is the machine-readable statement of the narrower one. Storage
#: and address-book sync are gated by neither: every class may be stored.
OUTREACH_ELIGIBLE_CLASSES: tuple[str, ...] = (USAGE_CLASS_OBSERVED,)


def _classification_rank(usage_class: str) -> int:
    """Position of *usage_class* in :data:`USAGE_CLASSES`; unknown ranks last.

    An unrecognized class read off a hand-edited record must never outrank a
    known one — it sorts with ``unclassified``, the weakest position, so a
    typo can neither win a no-downgrade comparison nor confer eligibility.
    """
    try:
        return USAGE_CLASSES.index(usage_class)
    except ValueError:
        return len(USAGE_CLASSES)


def _classification_outranks(incoming: str, existing: str) -> bool:
    """True when *incoming* is a strictly stronger claim than *existing*.

    "Evidence of use outranks purchase" (issue athenaeum#866): ``observed``
    beats ``provider`` beats ``unclassified``. Equal classes do NOT outrank —
    a re-assertion of the same class refreshes provenance in place rather than
    counting as a change, which is what keeps a repeated write idempotent.
    """
    return _classification_rank(incoming) < _classification_rank(existing)


def normalize_identifier(identifier: str) -> str:
    """Casefold + strip an email identifier for COMPARISON only.

    Never used to rewrite a stored value: an address is recorded exactly as
    it was observed, and matched case-insensitively (the domain half is
    case-insensitive per RFC 5321, and no real-world mail store this module
    talks to treats the local part as case-sensitive either — matching
    case-sensitively would silently mint a duplicate record for
    ``Alex@example.org``, which is the very failure athenaeum#850 is about).
    """
    return (identifier or "").strip().lower()


def identifiers_on_record(meta: dict[str, Any] | None) -> list[str]:
    """Every address *meta* lists across :data:`CONTACT_IDENTIFIER_FIELDS`.

    Returns the values verbatim (not normalized), in field then list order,
    skipping non-string and blank entries. A record that lists no address at
    all — including a slug-keyed bounce record, whose address lives in
    ``identifier:`` rather than in a list — returns ``[]``.
    """
    if not isinstance(meta, dict):
        return []
    found: list[str] = []
    for field_name in CONTACT_IDENTIFIER_FIELDS:
        value = meta.get(field_name)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        found += [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return found


def record_lists_identifier(meta: dict[str, Any] | None, identifier: str) -> bool:
    """True when *meta* already lists *identifier* among its addresses."""
    wanted = normalize_identifier(identifier)
    if not wanted:
        return False
    return any(normalize_identifier(item) == wanted for item in identifiers_on_record(meta))


def iter_contact_records(contacts_root: Path) -> list[Path]:
    """Every ``*.md`` record under the contacts surface, recursively, sorted.

    Sorted so resolution is DETERMINISTIC — the same store always resolves an
    address to the same record, which is what makes a re-reported bounce a
    true no-op rather than a coin flip between two records. Missing root
    yields ``[]`` (never raises), mirroring :func:`iter_corpus_files`. The
    ``_``-prefixed JSONL ledgers are excluded by the ``*.md`` glob itself.
    """
    root = Path(contacts_root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


class ExcludedRecordIndex:
    """Every record on one excluded surface, indexed by ``uid`` AND by address.

    Built by a SINGLE :func:`iter_contact_records` pass (issue athenaeum#883), so a
    caller resolving N records pays the O(corpus) scan once rather than N
    times. It is the by-ADDRESS sibling of the by-uid fix athenaeum#877/#879 already
    landed: :func:`resolve_contact_record` still paid a full scan **per call**,
    and it has three callers — :func:`athenaeum.bounce_join.join_identifier`,
    :func:`classify_contact_value`, and :func:`mark_bounced` (reached from
    ``librarian``'s compile loop via ``tier0_bounce_mark``) — each paying it
    independently.

    **Resolution discipline is IDENTICAL to the unindexed functions**, so
    moving a caller onto the index never changes WHICH record it resolves:

    - the FIRST match in :func:`iter_contact_records` order wins (that
      function sorts, so the same store always yields the same mapping);
    - ``log.warning`` (never raise) when a key resolves to several records;
    - keys are ``str(...).strip()``-coerced exactly as
      :func:`resolve_contact_record_for_uid` compares them, and addresses are
      :func:`normalize_identifier`-keyed exactly as
      :func:`record_lists_identifier` compares them;
    - a missing root yields an EMPTY index, never a raise.

    **Staleness (issue athenaeum#850).** :func:`mark_bounced` MINTS a record when
    resolution returns ``None``, and merges new identifiers onto existing
    records. A long-lived index that is never invalidated would answer a
    stale ``None`` for the second lookup of one address in a batch and mint a
    DUPLICATE record — athenaeum#850's exact failure, reintroduced. So this index
    takes :class:`~athenaeum.models.EntityIndex`'s architectural answer: a
    ONE-SHOT load, plus an explicit :meth:`register` that the WRITER calls
    after every write. (The analogy is architectural, not a signature match —
    ``EntityIndex.register`` takes a page object; this takes a path.) Nothing
    else invalidates it: after the load, only ``register`` mutates it, which
    is what makes resolution stable across a batch of interleaved writes.

    **The load is LAZY** — deferred to the first lookup rather than done in
    ``__init__``, the same shape :func:`read_entities` uses for its indexes and
    for the same reason. The compile loop constructs one of these per run
    ABOVE ``process_one``, but only a conforming hard-bounce note ever
    resolves through it — "a handful per compile run, not the full candidate
    population". An eager load would charge every run an O(corpus) scan to
    answer zero lookups, turning a cost fix into a cost regression for the
    common case. One-shot and lazy are not in tension: the scan still happens
    at most once per index.

    ``by_identifier`` retains ALL matches for a key internally and exposes
    first-match-wins as the default read, so an all-matches accessor is a
    different read of the same index rather than a second scan (issue
    athenaeum#884 adds exactly that).
    """

    def __init__(self, contacts_root: Path) -> None:
        self.contacts_root = Path(contacts_root)
        self._by_uid: dict[str, list[Path]] = {}
        self._by_identifier: dict[str, list[Path]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Scan the surface once, on first use. Idempotent."""
        if self._loaded:
            return
        # Set BEFORE indexing so a re-entrant call (a log handler that reads
        # the index, say) cannot trigger a second scan mid-load.
        self._loaded = True
        for path in iter_contact_records(self.contacts_root):
            self._index_record(path)
        self._warn_collisions()

    def _index_record(self, path: Path) -> None:
        """Add one record's uid and every address it lists to both maps.

        Append-only per key: the first path seen for a key stays first, which
        is what makes first-match-wins survive a mid-batch :meth:`register`.
        """
        meta = read_bounce_record(path)
        uid = str(meta.get("uid", "")).strip()
        if uid:
            self._append(self._by_uid, uid, path)
        for identifier in identifiers_on_record(meta):
            key = normalize_identifier(identifier)
            if key:
                self._append(self._by_identifier, key, path)
        # A slug-keyed bounce record carries its address in `identifier:`
        # rather than in a list, so `identifiers_on_record` returns [] for it
        # (documented there). `record_lists_identifier` agrees — it reads the
        # same helper — so indexing it here would resolve an address the
        # unindexed function does not, which is the one divergence this class
        # must never have.

    @staticmethod
    def _append(target: dict[str, list[Path]], key: str, path: Path) -> None:
        paths = target.setdefault(key, [])
        if path not in paths:
            paths.append(path)

    def _warn_collisions(self) -> None:
        """One line per collided key — proportional to the problem, not the corpus."""
        for uid, paths in self._by_uid.items():
            if len(paths) > 1:
                log.warning(
                    "uid %r resolves to %d contact records; indexing the first "
                    "(%s). See resolve_contact_record's docstring for the same "
                    "shared-value posture.",
                    uid,
                    len(paths),
                    paths[0].name,
                )
        for identifier, paths in self._by_identifier.items():
            if len(paths) > 1:
                log.warning(
                    "identifier resolves to %d contact records; annotating the "
                    "first (%s). A shared address is legitimate — see the "
                    "observation log.",
                    len(paths),
                    paths[0].name,
                )

    def by_uid(self, uid: str) -> Path | None:
        """The record whose frontmatter ``uid`` equals *uid*, or ``None``.

        An empty/blank *uid* never matches — otherwise it would match every
        record carrying no ``uid:`` at all, which is never a lookup's intent.
        """
        wanted = str(uid).strip()
        if not wanted:
            return None
        self._ensure_loaded()
        paths = self._by_uid.get(wanted)
        return paths[0] if paths else None

    def by_identifier(self, identifier: str) -> Path | None:
        """The first record listing *identifier*, or ``None`` — first-wins."""
        paths = self.all_by_identifier(identifier)
        return paths[0] if paths else None

    def all_by_identifier(self, identifier: str) -> list[Path]:
        """EVERY record listing *identifier*, in scan order (may be empty).

        The all-matches read athenaeum#884 needs, and the reason this index was
        committed to retaining every match for a key internally rather than
        only the first: an all-matches lookup is a different ACCESSOR on the
        same index, not a second scan.

        Why both exist rather than one: first-match-wins is the right posture
        for :func:`mark_bounced` (a deliverability fact has to land SOMEWHERE,
        deterministically, and a shared address is legitimate) and the WRONG
        posture for identity resolution (silently picking one of several
        people an address might belong to is exactly the guess the correction
        applier must refuse). Same data, two questions.
        """
        key = normalize_identifier(identifier)
        if not key:
            return []
        self._ensure_loaded()
        return list(self._by_identifier.get(key, ()))

    def uid_map(self) -> dict[str, Path]:
        """``uid -> first record`` — the shape :func:`build_contact_record_uid_index` returns."""
        self._ensure_loaded()
        return {uid: paths[0] for uid, paths in self._by_uid.items() if paths}

    def register(self, path: Path) -> None:
        """Re-index *path* WHOLESALE after a write — uid and its full address list.

        Wholesale, not a single-key insert, because :func:`mark_bounced` both
        mints new records and MERGES new identifiers onto existing ones: an
        insert keyed only on the address just written would miss the merge
        case and leave the index disagreeing with disk.

        Registering never changes which record an already-indexed key resolves
        to — :meth:`_index_record` appends — so batch resolution stays stable
        no matter how writes interleave with reads. Re-registering the same
        path is idempotent.

        Loads first, deliberately: registering into a not-yet-loaded index
        would place the just-written record FIRST for its keys, ahead of
        records already on disk, and first-wins would then resolve differently
        depending on whether a write happened before the first read.
        """
        self._ensure_loaded()
        self._index_record(Path(path))


def resolve_contact_record(
    contacts_root: Path,
    identifier: str,
    *,
    index: "ExcludedRecordIndex | None" = None,
) -> Path | None:
    """Find the existing record that already lists *identifier*, or ``None``.

    The join athenaeum#850 exists to create: an incoming address is resolved against
    what the surface ALREADY knows before a new record is minted, so the
    deliverability fact lands on the record other consumers read rather than
    on a slug-keyed sibling nothing points at.

    Returns the FIRST match in :func:`iter_contact_records` order when more
    than one record lists the address. A shared address legitimately maps to
    several persons (see the observation log's ``identifier -> person`` note
    in point 4 of the module docstring), so several matches is not an error —
    but this function deliberately does not guess which is "the" person: it
    takes the deterministic first and logs the ambiguity, leaving a real
    resolution to the observation ledger, which models it properly.

    Args:
        index: An already-built :class:`ExcludedRecordIndex` over
            *contacts_root* to answer from, instead of paying a fresh
            :func:`iter_contact_records` scan (issue athenaeum#883). Optional and
            defaulting to ``None`` so every existing caller keeps today's
            behaviour AND today's cost exactly — a caller with no natural
            batch scope has nothing to amortize an index against, and
            supplying one it built itself for a single lookup would be
            strictly slower. Resolution is identical either way.
    """
    if index is not None:
        return index.by_identifier(identifier)
    matches = [
        path
        for path in iter_contact_records(contacts_root)
        if record_lists_identifier(read_bounce_record(path), identifier)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        log.warning(
            "identifier resolves to %d contact records; annotating the first "
            "(%s). A shared address is legitimate — see the observation log.",
            len(matches),
            matches[0].name,
        )
    return matches[0]


def resolve_contact_records(
    contacts_root: Path,
    identifier: str,
    *,
    index: "ExcludedRecordIndex | None" = None,
) -> list[Path]:
    """EVERY record listing *identifier*, in :func:`iter_contact_records` order.

    The all-matches sibling of :func:`resolve_contact_record` (issue
    athenaeum#884). That function returns the deterministic FIRST match and logs
    the ambiguity — the right posture for :func:`mark_bounced`, where a
    deliverability fact must land somewhere and a shared address is
    legitimate, and the wrong posture for IDENTITY resolution, where quietly
    picking one of several people an address might belong to is precisely the
    guess a caller must refuse to make. This function hands the ambiguity back
    to the caller instead of resolving it.

    An empty/blank identifier, or a missing root, yields ``[]`` — never a
    raise, mirroring its sibling.

    Args:
        index: An already-built :class:`ExcludedRecordIndex` to answer from,
            instead of a fresh scan. When supplied this is
            :meth:`ExcludedRecordIndex.all_by_identifier` — a different read of
            an index the caller already has, not a second scan of the surface.
    """
    if index is not None:
        return index.all_by_identifier(identifier)
    if not normalize_identifier(identifier):
        return []
    return [
        path
        for path in iter_contact_records(contacts_root)
        if record_lists_identifier(read_bounce_record(path), identifier)
    ]


def uid_on_record(record_path: Path) -> str | None:
    """The ``uid`` an excluded record carries, or ``None`` (issue athenaeum#884).

    The `record -> uid` half of the ``identifier -> record -> uid -> wiki
    page`` chain, kept in this module because this module is the only one that
    knows the surface layout (``docs/one-way-in-one-way-out.md`` §3) — a
    caller doing its own ``read_bounce_record(...).get("uid")`` would be
    reaching past that seam for one field.

    Coerces with ``str(...).strip()`` exactly as
    :func:`resolve_contact_record_for_uid` compares, so a uid resolved here
    and a uid looked up there always mean the same string. A record with no
    ``uid``, or a blank one, yields ``None``.
    """
    uid = str(read_bounce_record(record_path).get("uid", "")).strip()
    return uid or None


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


def identifier_validity_entries(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The per-identifier valid-time closes on *meta*, or ``[]``.

    Tolerant reader: a missing, non-list or malformed
    :data:`IDENTIFIER_VALIDITY_FIELD` yields ``[]`` rather than raising, and
    non-dict list entries are skipped — a hand-edited record must degrade to
    "no mark recorded", never to a crash in a consumer.
    """
    if not isinstance(meta, dict):
        return []
    entries = meta.get(IDENTIFIER_VALIDITY_FIELD)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def is_bounced_identifier(
    meta: dict[str, Any] | None, identifier: str, as_of: date | None = None
) -> bool:
    """True when *identifier* is closed on *meta* — the ADDRESS-level predicate.

    :func:`is_bounced` asks "is this RECORD closed", which is the right
    question for a slug-keyed record (one record, one address) and the wrong
    one for a person record listing several addresses — there, a bare
    top-level ``valid_until`` would say the PERSON expired. This is the
    predicate a consumer calls when it holds an address (issue athenaeum#850), and it
    reads both shapes:

    - a per-identifier entry in :data:`IDENTIFIER_VALIDITY_FIELD`, matched
      case-insensitively; else
    - the record's own top-level close, but ONLY when the record's
      ``identifier:`` IS the address asked about — so a slug-keyed record can
      never answer for a neighbouring address.

    Absent either, ``False`` (never seen ~ not bounced), mirroring
    :func:`is_bounced`'s open-upper-bound posture.
    """
    wanted = normalize_identifier(identifier)
    if not wanted:
        return False
    for entry in identifier_validity_entries(meta):
        if normalize_identifier(str(entry.get("identifier", ""))) == wanted:
            return valid_until_expired(entry, as_of)
    if isinstance(meta, dict) and normalize_identifier(str(meta.get("identifier", ""))) == wanted:
        return is_bounced(meta, as_of)
    return False


@dataclass(frozen=True)
class ContactClassification:
    """How one contact value was obtained, and what it may be used for.

    The per-VALUE unit issue athenaeum#866 introduces: an address obtained
    from a data vendor and an address someone used to write to you are
    different facts with different permissions, and before this they were
    stored identically. ``usage_class`` is the permission-bearing half;
    ``source``/``observed_at`` are the provenance that justifies it (which
    system asserted this, and when).

    A value with no stored entry is reported as
    :data:`USAGE_CLASS_UNCLASSIFIED` with ``source``/``observed_at`` of
    ``None`` — the "we never recorded how this was obtained" case, which is
    distinct from (and never silently promoted to) either real class.
    """

    identifier: str
    usage_class: str
    source: str | dict[str, Any] | None = None
    observed_at: str | None = None

    @property
    def outreach_eligible(self) -> bool:
        """True when this value may be used to INITIATE contact.

        Only :data:`OUTREACH_ELIGIBLE_CLASSES` qualifies. Storage and
        address-book population are NOT gated by this — they are a different,
        broader permission (issue athenaeum#866).
        """
        return self.usage_class in OUTREACH_ELIGIBLE_CLASSES

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape, mirroring :meth:`RedactionMarker.to_dict`."""
        return {
            "identifier": self.identifier,
            "usage_class": self.usage_class,
            "source": self.source,
            "observed_at": self.observed_at,
            "outreach_eligible": self.outreach_eligible,
        }


def contact_classification_entries(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The per-value classification entries on *meta*, or ``[]``.

    Tolerant reader, exactly mirroring :func:`identifier_validity_entries`: a
    missing, non-list or malformed :data:`CONTACT_CLASSIFICATION_FIELD` yields
    ``[]`` rather than raising, and non-dict entries are skipped. A
    hand-edited record must degrade to "no classification recorded" — which
    reads as ``unclassified``, and therefore NOT outreach-eligible — never to
    a crash in a consumer.
    """
    if not isinstance(meta, dict):
        return []
    entries = meta.get(CONTACT_CLASSIFICATION_FIELD)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def classification_for_value(meta: dict[str, Any] | None, identifier: str) -> ContactClassification:
    """The classification recorded for *identifier* on *meta*.

    Always returns a :class:`ContactClassification` — never ``None``. A value
    with no stored entry comes back as :data:`USAGE_CLASS_UNCLASSIFIED` with
    no provenance, which is the AC's "existing contact records without the
    marker are treated as unclassified and reported as such, never silently
    defaulted to usable": the caller receives a positive statement that the
    provenance is unknown, and :attr:`~ContactClassification.outreach_eligible`
    is ``False`` for it.

    Matches case-insensitively via :func:`normalize_identifier`, the same
    comparison :func:`is_bounced_identifier` uses — a classification that
    missed ``Alex@example.org`` because it was stored as ``alex@example.org``
    would silently read as unclassified, losing a permission that was in fact
    recorded.
    """
    wanted = normalize_identifier(identifier)
    if wanted:
        for entry in contact_classification_entries(meta):
            if normalize_identifier(str(entry.get("identifier", ""))) == wanted:
                usage_class = str(entry.get("usage_class", "") or "").strip()
                return ContactClassification(
                    identifier=identifier,
                    usage_class=usage_class or USAGE_CLASS_UNCLASSIFIED,
                    source=entry.get("source"),
                    observed_at=(
                        str(entry["observed_at"]) if entry.get("observed_at") is not None else None
                    ),
                )
    return ContactClassification(identifier=identifier, usage_class=USAGE_CLASS_UNCLASSIFIED)


def is_outreach_eligible(meta: dict[str, Any] | None, identifier: str) -> bool:
    """True when *identifier* may be used to INITIATE contact (issue athenaeum#866).

    The single predicate a consumer calls, in the shape of
    :func:`is_pii_flagged` / :func:`is_bounced_identifier` — so the outreach
    rule lives in the store and is not reimplemented (and eventually not
    implemented) per consumer. False for a provider-supplied value, false for
    an unclassified one, false for an address the record has never heard of.

    Deliberately does NOT consult bounce state: "may we initiate contact with
    this address" and "is this address still deliverable" are separate
    questions with separate predicates (:func:`is_bounced_identifier`). A
    caller about to send needs BOTH — this one does not silently answer the
    other, which would make a bounced-but-observed address read as sendable.
    """
    return classification_for_value(meta, identifier).outreach_eligible


#: Frontmatter key carrying a do-not-email mark on a contact record (issue
#: athenaeum#851). It exists on live records today and was, until this issue,
#: absent from the API surface **entirely** — so a consumer could only reach it
#: by reading the store's files, which is the pattern the excluded surface
#: exists to remove.
#:
#: Deliberately NOT added to :data:`EXCLUDED_RECORD_BOOKKEEPING_FIELDS`: it is
#: a FACT about the person, not bookkeeping about the record, and a
#: bookkeeping key is invisible to :func:`resolve_excluded_fields`'s rule-3
#: denylist-complement. It is surfaced through :func:`do_not_email_state`
#: rather than as a contact *value* because it has no value to redact — it is
#: a mark, not an address.
DO_NOT_EMAIL_FIELD = "do_not_email"

#: Strings a ``do_not_email:`` value may carry that mean "no mark". Anything
#: else non-empty means the mark IS set: the failure direction of a typo must
#: be a false SKIP (recoverable by a human), never a false SEND (not).
_DO_NOT_EMAIL_FALSEY: frozenset[str] = frozenset({"", "false", "no", "none", "null", "0", "off"})


@dataclass(frozen=True)
class DoNotEmailState:
    """Whether a contact record carries a do-not-email mark, and on whose word.

    The first-class exposure of :data:`DO_NOT_EMAIL_FIELD` (issue
    athenaeum#851). Provenance is carried alongside the mark rather than
    reduced out of it because issue athenaeum#77 requires an OPERATOR mark and a
    PLATFORM unsubscribe to stay distinguishable — a bare boolean cannot say
    which of the two it is, and a consumer that must honour one differently
    from the other would have to go back to the files to find out.

    ``marked`` is the fact; ``source`` / ``observed_at`` / ``reason`` are how
    the store knows it. A record with no mark at all returns ``marked=False``
    with no provenance — which is a positive statement ("nothing recorded"),
    NOT an assertion that the person may be emailed. Athenaeum does not answer
    that question; see this module's note on eligibility being the consumer's
    policy.

    ``surface`` (issue athenaeum#960) names WHICH of the two surfaces
    :func:`do_not_email_state` read the mark from — ``"wiki"`` or
    ``"excluded"`` — and is ``None`` exactly when ``marked`` is ``False``. It
    exists because the wiki page frontmatter and the excluded-record meta are
    two independently-authored surfaces (issue athenaeum#851 shipped reading
    only the latter, which is inert on live data — every hand-authored mark
    lives on the former); a caller auditing where a mark came from should not
    have to re-derive it from which argument was non-``None``.
    """

    marked: bool
    source: str | dict[str, Any] | None = None
    observed_at: str | None = None
    reason: str | None = None
    surface: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape, mirroring :meth:`ContactClassification.to_dict`."""
        return {
            "marked": self.marked,
            "source": self.source,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "surface": self.surface,
        }


def _coerce_do_not_email_flag(raw: Any) -> bool:
    """Read the truthiness of a raw ``do_not_email:`` scalar, fail-closed.

    ``True``/``False`` answer for themselves. A string is compared against
    :data:`_DO_NOT_EMAIL_FALSEY` case-insensitively, so a hand-written
    ``do_not_email: "unsubscribed 2026-02-01"`` reads as MARKED (and carries
    its text as the reason) rather than being silently discarded as
    unparseable. ``None`` — which is what a bare ``do_not_email:`` with no
    value parses to — reads as NOT marked: the key was written with nothing
    after it, which asserts nothing.
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() not in _DO_NOT_EMAIL_FALSEY


def _do_not_email_from_record_meta(meta: dict[str, Any] | None) -> DoNotEmailState:
    """The do-not-email mark recorded on ONE excluded-record *meta*, with provenance.

    ``surface`` is left ``None`` here — it is the caller's job
    (:func:`do_not_email_state`) to stamp it, since this helper does not know
    which of the two surfaces it was asked to parse.

    **Tolerant reader**, mirroring :func:`identifier_validity_entries` and
    :func:`contact_classification_entries`, because live records were written
    by hand and by three different code paths. Four shapes are accepted:

    - absent → ``marked=False``, no provenance;
    - a scalar (``true`` / ``"unsubscribed"`` / …) → :func:`_coerce_do_not_email_flag`
      decides, and a non-boolean scalar is kept as ``reason`` so the operator's
      words are not thrown away;
    - a mapping → ``marked`` comes from whichever of ``value`` / ``marked`` /
      ``set`` / ``do_not_email`` is present (defaulting to ``True``: a mapping
      was written AT ALL, which is an assertion), with ``source`` /
      ``observed_at`` / ``reason`` read alongside;
    - a list → the LAST entry wins, read by the rules above, matching the
      last-writer-wins posture :func:`mark_bounced` already has. An empty list
      is "no mark".

    Nothing here raises. A record whose ``do_not_email:`` is malformed degrades
    to a readable answer, never to a crash inside a consumer's send loop.
    """
    if not isinstance(meta, dict) or DO_NOT_EMAIL_FIELD not in meta:
        return DoNotEmailState(marked=False)
    raw = meta[DO_NOT_EMAIL_FIELD]
    if isinstance(raw, list):
        entries = [item for item in raw if item is not None]
        if not entries:
            return DoNotEmailState(marked=False)
        raw = entries[-1]
    if isinstance(raw, Mapping):
        marked = True
        for key in ("value", "marked", "set", DO_NOT_EMAIL_FIELD):
            if key in raw:
                marked = _coerce_do_not_email_flag(raw[key])
                break
        reason = raw.get("reason")
        observed_at = raw.get("observed_at") or raw.get("date")
        return DoNotEmailState(
            marked=marked,
            source=raw.get("source"),
            observed_at=str(observed_at) if observed_at is not None else None,
            reason=str(reason) if reason is not None else None,
        )
    marked = _coerce_do_not_email_flag(raw)
    # A non-boolean scalar carries the operator's own words; keep them as the
    # reason rather than reducing the mark to a bare bit.
    reason = str(raw).strip() if marked and not isinstance(raw, bool) else None
    return DoNotEmailState(marked=marked, reason=reason or None)


def _do_not_email_from_page(page_frontmatter: dict[str, Any] | None) -> DoNotEmailState:
    """The do-not-email mark recorded on ONE wiki page's frontmatter, with provenance.

    The page's shape is FLAT, not the excluded-record surface's nested
    mapping: a bare ``do_not_email:`` scalar, with provenance (if any) on the
    sibling keys ``do_not_email_reason`` and ``do_not_email_date`` (issue
    athenaeum#960's plan). ``source`` is always reported as ``"operator"`` for
    a page-originated mark — this repo's own live-store evidence is that
    every ``do_not_email:`` mark on this surface is hand-authored by the
    operator (there is no automated writer of this field anywhere in
    ``src/``, on either surface) — which keeps the operator-vs-platform
    distinction issue athenaeum#77 requires true by construction for this
    surface, without inventing an unrequested ``do_not_email_source:`` key.

    Coercion is the SAME fail-closed rule :func:`_coerce_do_not_email_flag`
    applies to the excluded-record surface: a malformed or unparseable scalar
    reads as MARKED, never silently as "no mark".
    """
    if not isinstance(page_frontmatter, dict) or DO_NOT_EMAIL_FIELD not in page_frontmatter:
        return DoNotEmailState(marked=False)
    raw = page_frontmatter[DO_NOT_EMAIL_FIELD]
    marked = _coerce_do_not_email_flag(raw)
    if not marked:
        return DoNotEmailState(marked=False)
    reason = page_frontmatter.get("do_not_email_reason")
    if reason is None and not isinstance(raw, bool):
        # A hand-written scalar like `do_not_email: "family request"` carries
        # the operator's own words even with no separate reason key —
        # mirroring the excluded-record scalar shape's behaviour.
        reason = raw
    observed_at = page_frontmatter.get("do_not_email_date")
    return DoNotEmailState(
        marked=True,
        source="operator",
        observed_at=str(observed_at) if observed_at is not None else None,
        reason=str(reason).strip() if reason is not None else None,
    )


def do_not_email_state(
    record_meta: dict[str, Any] | None,
    page_frontmatter: dict[str, Any] | None = None,
) -> DoNotEmailState:
    """The do-not-email mark for one contact, read across BOTH surfaces.

    Always returns a :class:`DoNotEmailState` — never ``None`` — in the shape
    of :func:`classification_for_value`, so a consumer never distinguishes
    "no mark" from "no answer" by testing for ``None``.

    **Converges the two surfaces (issue athenaeum#960).** Issue athenaeum#851
    shipped this reading ONLY *record_meta* — the excluded-record surface —
    which holds zero live ``do_not_email`` marks; every hand-authored mark
    lives on the wiki page's frontmatter instead, so the field was inert on
    live data. *page_frontmatter* is the new, optional second surface.

    **Precedence, not merge** (2026-08-20 AC amendment, operator-ratified):
    the wiki page is checked FIRST. If it carries the mark, its own
    ``source`` / ``observed_at`` / ``reason`` are returned exactly as read
    from the page — ``surface="wiki"`` — and *record_meta* is never
    consulted. Only when the page carries no mark does *record_meta*'s own
    mark answer (``surface="excluded"``), preserving the exact shape issue
    athenaeum#851 shipped so no existing record-side caller regresses. The
    two are never blended: a caller reading ``.source``/``.reason`` never
    receives a value assembled from both surfaces. Neither is backfilled onto
    the other — the wiki page remains the sole authoring surface, and a
    future divergence (the excluded surface newly carrying the field) is
    athenaeum#963's guard's job to flag, not this function's to resolve.

    A contact with no mark on EITHER surface returns ``marked=False`` with
    every provenance field (including ``surface``) ``None`` — "nothing
    recorded" stays a distinguishable, positive answer, not an assertion that
    the person may be emailed. Athenaeum does not answer that question; see
    this module's note on eligibility being the consumer's policy.

    Nothing here raises. A record whose ``do_not_email:`` is malformed on
    either surface degrades to a readable answer, never to a crash inside a
    consumer's send loop.
    """
    page_state = _do_not_email_from_page(page_frontmatter)
    if page_state.marked:
        return replace(page_state, surface="wiki")
    record_state = _do_not_email_from_record_meta(record_meta)
    if record_state.marked:
        return replace(record_state, surface="excluded")
    return DoNotEmailState(marked=False)


@dataclass(frozen=True)
class IdentifierValidity:
    """The validity state of ONE contact value, with its valid-time close.

    **This type is the answer to the representation trap** (issue
    athenaeum#851). A hard bounce is recorded as a valid-time CLOSE — a
    ``valid_until`` on the identifier, per :func:`mark_bounced` — and *not* as a
    ``bounced:`` enum field. So ``grep '^bounced:'`` over the excluded contacts
    surface returns 0 even after a fully successful mark, which has already
    misled one verification lane (observed during maecenas#73's verification).

    Before verifying a mark by hand, read ``docs/deprecated-email-tracking.md``
    § "How to verify a mark — and the two greps that lie" — the canonical
    account, covering BOTH failing greps. The one above is only the first; the
    mirror-image error is grepping the WIKI surface for ``bounced:``, where the
    field genuinely exists (a different surface, written outside athenaeum —
    ``docs/bounce-surface-convergence.md``) and a ``0`` means the glob missed.
    ``docs/authorized-reader-contract.md`` covers the caller-facing contract.

    A consumer holding one of these never has to know that. It reads
    ``closed`` (is this value closed as of the date I asked about), ``valid_until``
    (when), ``reason`` (why — the SMTP diagnostic for a bounce), and ``source``
    (who says so). Which frontmatter key encodes that is athenaeum's problem,
    and changing it later is a non-event for callers. Making the representation
    irrelevant to callers is most of what this type is for.

    ``recorded`` distinguishes "the store holds a validity entry for this value
    and it is still open" from "the store holds NO entry at all". Both have
    ``closed=False``, and collapsing them would hide the difference between a
    value someone has vouched for and one nobody has ever looked at.

    Athenaeum states none of this as a verdict on whether the value may be
    USED. ``closed`` is a fact about deliverability; eligibility is the
    consumer's policy over these fields.
    """

    identifier: str
    closed: bool
    valid_until: str | None = None
    reason: str | None = None
    source: str | dict[str, Any] | None = None
    observed_at: str | None = None
    recorded: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape, mirroring :meth:`ContactClassification.to_dict`."""
        return {
            "identifier": self.identifier,
            "closed": self.closed,
            "valid_until": self.valid_until,
            "reason": self.reason,
            "source": self.source,
            "observed_at": self.observed_at,
            "recorded": self.recorded,
        }


def _validity_from_entry(
    identifier: str, entry: dict[str, Any], as_of: date | None
) -> IdentifierValidity:
    """Build an :class:`IdentifierValidity` from one stored validity mapping.

    Reads the keys :func:`_merge_identifier_validity` writes — ``valid_until``,
    ``bounce_diagnostic``, ``source``, ``observed_at`` — and falls back to a
    generic ``reason`` key so a hand-written close (an operator closing an
    address for a reason that is not a bounce) is readable too.
    """
    valid_until = entry.get("valid_until")
    reason = entry.get("bounce_diagnostic")
    if reason is None:
        reason = entry.get("reason")
    observed_at = entry.get("observed_at")
    return IdentifierValidity(
        identifier=identifier,
        closed=valid_until_expired(entry, as_of),
        valid_until=str(valid_until) if valid_until is not None else None,
        reason=str(reason) if reason is not None else None,
        source=entry.get("source"),
        observed_at=str(observed_at) if observed_at is not None else None,
        recorded=True,
    )


def validity_for_value(
    meta: dict[str, Any] | None, identifier: str, as_of: date | None = None
) -> IdentifierValidity:
    """The validity state recorded for *identifier* on *meta*, with provenance.

    The structured counterpart to :func:`is_bounced_identifier` — the same two
    shapes, the same case-insensitive matching, the same "absent means no mark
    recorded" posture — returning the *facts* (when, why, on whose word) rather
    than reducing them to a bare boolean:

    - a per-identifier entry in :data:`IDENTIFIER_VALIDITY_FIELD` (a person
      record listing several addresses); else
    - the record's own top-level close, but ONLY when the record's
      ``identifier:`` IS the address asked about — so a slug-keyed record can
      never answer for a neighbouring address.

    Always returns an :class:`IdentifierValidity`, never ``None``. A value with
    no stored entry comes back ``closed=False, recorded=False`` — "nothing
    recorded", which is not the same claim as "verified deliverable" and must
    not be read as one.

    ``closed`` is evaluated through :func:`athenaeum.models.valid_until_expired`
    — the SAME upper-bound predicate :func:`is_bounced` and
    :func:`is_bounced_identifier` use — rather than a second comparison that
    could drift from them.
    """
    wanted = normalize_identifier(identifier)
    if not wanted:
        return IdentifierValidity(identifier=identifier, closed=False)
    for entry in identifier_validity_entries(meta):
        if normalize_identifier(str(entry.get("identifier", ""))) == wanted:
            return _validity_from_entry(identifier, entry, as_of)
    if isinstance(meta, dict) and normalize_identifier(str(meta.get("identifier", ""))) == wanted:
        return _validity_from_entry(identifier, meta, as_of)
    return IdentifierValidity(identifier=identifier, closed=False)


def assemble_excluded_validity(
    record_meta: dict[str, Any] | None,
    fields: Mapping[str, list[str]],
    *,
    as_of: date | None = None,
) -> dict[str, list[IdentifierValidity]]:
    """Validity state for every value in *fields*, co-indexed with it.

    The validity sibling of the ``classifications`` map
    :func:`assemble_excluded_read` returns: ``validity[field][i]`` describes
    ``fields[field][i]``, so a caller holding a value always holds its validity
    without a second lookup — the same co-indexing contract, for the same
    reason.

    Kept as a SEPARATE function rather than a fourth element of
    :func:`assemble_excluded_read`'s tuple so that seam's arity — public, and
    already consumed by ``recall``'s render join and by
    :func:`_entity_read_from_indexes` — does not change. Additive here, and
    breaking there.

    Empty in exactly the case ``fields`` is empty: a redacted read exposes no
    values, so it describes none. That is deliberate — a redaction marker
    reports that values EXIST and how many, and attaching validity to values
    the caller was not given would leak the shape of what was withheld.
    """
    if not fields:
        return {}
    return {
        field_name: [validity_for_value(record_meta, value, as_of) for value in values]
        for field_name, values in fields.items()
    }


def _merge_contact_classification(
    existing_meta: dict[str, Any],
    identifier: str,
    *,
    usage_class: str,
    source: str | dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Upsert one per-value classification into a copy of *existing_meta*.

    Mirrors :func:`_merge_identifier_validity`'s discipline — in-place update
    preserving list position (so a re-assertion compares byte-identical and a
    record's history stays readable in file order), append otherwise.

    **Enforces the no-downgrade rule (issue athenaeum#866).** A ``provider``
    assertion of an address already recorded ``observed`` leaves the entry
    untouched, provenance included: evidence of use outranks purchase, and the
    observed provenance is precisely what justifies the surviving permission,
    so overwriting it with the vendor's would keep the class while destroying
    its basis. An UPGRADE (``provider`` -> ``observed``, or either over
    ``unclassified``) applies and takes the new provenance with it. A
    re-assertion of the SAME class refreshes provenance in place — that is a
    fresher statement of the same fact, not a downgrade.

    This is the store enforcing the rule for every writer, rather than each
    writer enforcing it for itself: the marker is the authority (issue
    athenaeum#866's motivation), so a consumer-side check is defense in depth,
    never the mechanism.
    """
    merged = dict(existing_meta)
    wanted = normalize_identifier(identifier)
    entries = [dict(item) for item in contact_classification_entries(existing_meta)]
    entry = {
        "identifier": identifier,
        "usage_class": usage_class,
        "source": source,
        "observed_at": observed_at,
    }
    for position, item in enumerate(entries):
        if normalize_identifier(str(item.get("identifier", ""))) != wanted:
            continue
        current = str(item.get("usage_class", "") or USAGE_CLASS_UNCLASSIFIED)
        if _classification_outranks(current, usage_class):
            return merged  # no-downgrade: the stronger existing claim stands
        entries[position] = entry
        break
    else:
        entries.append(entry)
    merged[CONTACT_CLASSIFICATION_FIELD] = entries
    merged[PII_FLAG] = True
    return merged


def classify_contact_value(
    contacts_root: Path,
    identifier: str,
    *,
    usage_class: str,
    source: str | dict[str, Any],
    observed_at: str,
    index: "ExcludedRecordIndex | None" = None,
) -> Path | None:
    """Record how *identifier* was obtained, on the record that already lists it.

    The writer half of issue athenaeum#866, shaped like :func:`mark_bounced`:
    resolves the existing record via :func:`resolve_contact_record`, merges the
    classification onto its frontmatter under
    :data:`CONTACT_CLASSIFICATION_FIELD`, and writes atomically. Returns the
    record path, or ``None`` when no record lists the address.

    Unlike :func:`mark_bounced` this never MINTS a record: a classification is
    a statement about a value the store already holds, and minting one here
    would let a provider assertion conjure a contact record for an address the
    store had deliberately never been given. Re-asserting an identical
    classification rewrites identical bytes (idempotent), and a downgrade is
    refused in :func:`_merge_contact_classification` — in both cases the file
    is left byte-identical.

    Args:
        index: An already-built :class:`ExcludedRecordIndex` to resolve
            through instead of a fresh scan (issue athenaeum#883). Optional, and
            unindexed by default: this function has no in-tree caller with a
            natural batch scope to amortize an index against, so it gains the
            parameter for symmetry with :func:`mark_bounced` rather than a new
            default. Unlike ``mark_bounced`` it never MINTS, so it never has
            to :meth:`~ExcludedRecordIndex.register` anything back — a
            classification only ever rewrites a record the index already holds,
            and rewriting cannot change which addresses that record lists.

    Raises:
        ValueError: if *usage_class* is not one of :data:`USAGE_CLASSES`.
            A misspelled class must fail loudly at the WRITE, where the caller
            can see it — silently storing an unrecognized class would read
            back as ``unclassified`` and quietly strip a permission.
    """
    if usage_class not in USAGE_CLASSES:
        raise ValueError(
            f"unknown usage_class {usage_class!r}; expected one of {list(USAGE_CLASSES)}"
        )
    record_path = resolve_contact_record(contacts_root, identifier, index=index)
    if record_path is None:
        return None
    existing_text = record_path.read_text(encoding="utf-8")
    existing_meta, existing_body = parse_frontmatter(existing_text)
    merged = _merge_contact_classification(
        existing_meta if isinstance(existing_meta, dict) else {},
        identifier,
        usage_class=usage_class,
        source=source,
        observed_at=observed_at,
    )
    new_text = render_frontmatter(merged) + "\n" + existing_body
    if new_text != existing_text:
        atomic_write_text(record_path, new_text)
    return record_path


def _merge_identifier_validity(
    existing_meta: dict[str, Any],
    identifier: str,
    *,
    diagnostic: str,
    observed_at: str,
    source: str | dict[str, Any],
) -> dict[str, Any]:
    """Upsert one per-identifier close into a copy of *existing_meta*.

    Updates the entry for *identifier* IN PLACE (preserving its position in
    the list) when one is already there, else appends. Position stability is
    what makes a re-report of the identical fact compare byte-identical, and
    keeps a record's history readable in file order.
    """
    entry = {
        "identifier": identifier,
        "bounce_diagnostic": diagnostic,
        "observed_at": observed_at,
        "valid_until": observed_at,
        "source": source,
    }
    merged = dict(existing_meta)
    wanted = normalize_identifier(identifier)
    entries = [dict(item) for item in identifier_validity_entries(existing_meta)]
    for position, item in enumerate(entries):
        if normalize_identifier(str(item.get("identifier", ""))) == wanted:
            entries[position] = entry
            break
    else:
        entries.append(entry)
    merged[IDENTIFIER_VALIDITY_FIELD] = entries
    merged[PII_FLAG] = True
    return merged


def mark_bounced(
    contacts_root: Path,
    identifier: str,
    *,
    diagnostic: str,
    observed_at: str,
    source: str | dict[str, Any],
    record_path: Path | None = None,
    index: "ExcludedRecordIndex | None" = None,
) -> tuple[Path, bool]:
    """Upsert the hard-bounce mark onto *identifier*'s contact record. Idempotent.

    **Resolution (issue athenaeum#850).** The record is found by asking
    :func:`resolve_contact_record` which EXISTING record already lists this
    address, and only minting :func:`default_bounce_record_path`'s slug-keyed
    record when none does. Resolving by slug alone (the original athenaeum#765
    behaviour) put the mark on a sibling of the person record that already
    listed the address — so a consumer reading deliverability off the person
    record got a confident "not bounced" for an address that demonstrably
    bounced. An explicit *record_path* still overrides both.

    **Shape.** Decided by what the target record IS, not by how it was found:

    - a record that LISTS addresses (:func:`identifiers_on_record` — a person
      record migrated by athenaeum#479/#502) gets a PER-IDENTIFIER close appended to
      :data:`IDENTIFIER_VALIDITY_FIELD`. A bare top-level ``valid_until``
      cannot be used there: the record holds several addresses, so closing
      the record would assert the whole PERSON expired — a second
      silent-wrong-answer in place of the one being fixed.
    - any other record (the slug-keyed fallback, one record = one address)
      keeps athenaeum#765's top-level fields verbatim, which is what
      :func:`is_bounced` reads.

    Both shapes are the SAME representation — a valid-time close, athenaeum#308's
    existing mechanism, per the module docstring's point 5 — differing only in
    what they are scoped to. Neither introduces a ``bounced``/``deprecated``
    status enum. :func:`is_bounced_identifier` reads both.

    Creates the record if absent; otherwise merges onto the EXISTING
    frontmatter (any other field already on the record survives byte-for-byte
    — this never overwrites the whole file, only sets its own keys) and
    rewrites atomically. Never deletes the record or the identifier.

    Returns ``(record_path, changed)``. ``changed`` is ``False`` — no write —
    when the merged frontmatter is byte-for-byte identical to what is
    already on disk (the delta gate :func:`athenaeum.librarian.tier0_handle_upsert`
    already uses for the same reason: re-reporting the identical fact must be
    a true no-op, never a duplicate mark). Re-reporting a LATER bounce (a
    different *observed_at* / *diagnostic*) updates the same record — and,
    on a person record, the same list entry — in place rather than
    duplicating it: last-writer-wins.

    Args:
        index: An already-built :class:`ExcludedRecordIndex` to resolve
            through, and to KEEP CURRENT (issue athenaeum#883). When supplied,
            resolution is answered from the index and the written record is
            :meth:`~ExcludedRecordIndex.register`-ed back onto it before
            returning — so a second call for the same address in one batch
            resolves to the record the first call just minted, instead of
            answering a stale ``None`` and minting a duplicate (athenaeum#850's
            exact failure). When omitted, behaviour and cost are exactly as
            before: a fresh scan per call, and no staleness surface at all.
    """
    target = record_path
    if target is None:
        target = resolve_contact_record(contacts_root, identifier, index=index) or (
            default_bounce_record_path(contacts_root, identifier)
        )
    existing_meta = read_bounce_record(target)
    existing_body = ""
    if target.exists():
        _, existing_body = parse_frontmatter(target.read_text(encoding="utf-8"))

    if identifiers_on_record(existing_meta):
        merged_meta = _merge_identifier_validity(
            existing_meta,
            identifier,
            diagnostic=diagnostic,
            observed_at=observed_at,
            source=source,
        )
    else:
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
    if index is not None:
        # Register AFTER the write, so the re-read sees the merged frontmatter
        # — this is both the mint case (a record the index had never seen) and
        # the merge case (an existing record that just gained an identifier).
        index.register(target)
    return target, True


#: Frontmatter key stamped on a slug-keyed bounce record whose mark has been
#: folded onto the person record that lists the same address (issue athenaeum#850).
#: Presence is what makes :func:`find_orphaned_bounce_marks` skip the record on
#: a re-run, so the repair is idempotent WITHOUT deleting anything — the
#: original record stays exactly where it was, having only gained a field, per
#: the "never deleted, only ever gains fields" posture the mark itself has.
FOLDED_INTO_FIELD = "folded_into"


@dataclass(frozen=True)
class OrphanedBounceMark:
    """A bounce mark stranded on a slug-keyed record (issue athenaeum#850).

    ``bounce_record`` carries the deliverability fact; ``person_record`` is
    the record that lists the same ``identifier`` among its addresses and is
    therefore the one consumers actually read.
    """

    identifier: str
    bounce_record: Path
    person_record: Path


@dataclass(frozen=True)
class BounceFoldReport:
    """Outcome of :func:`fold_orphaned_bounce_marks` — what was folded, and how many."""

    folded: list[OrphanedBounceMark]
    dry_run: bool

    @property
    def count(self) -> int:
        """How many stranded marks were folded (or would be, under *dry_run*)."""
        return len(self.folded)


def find_orphaned_bounce_marks(contacts_root: Path) -> list[OrphanedBounceMark]:
    """Find slug-keyed bounce records whose address a person record already lists.

    The repairable population athenaeum#850's resolve-then-annotate fix stops
    CREATING, reported here so the pairs the previous behaviour already left
    behind can be folded too. A record qualifies when all of:

    - it carries a top-level ``identifier:`` (the slug-keyed bounce shape —
      a person record's addresses live in a list, so it never matches);
    - it carries a bounce mark (a top-level ``valid_until``);
    - it has not already been folded (:data:`FOLDED_INTO_FIELD` absent); and
    - some OTHER record lists that identifier among its addresses.

    Pure — reads only. Returns pairs in :func:`iter_contact_records` order.
    """
    records = [(path, read_bounce_record(path)) for path in iter_contact_records(contacts_root)]
    orphaned: list[OrphanedBounceMark] = []
    for path, meta in records:
        identifier = str(meta.get("identifier", "") or "").strip()
        if not identifier or FOLDED_INTO_FIELD in meta or "valid_until" not in meta:
            continue
        person = next(
            (
                other_path
                for other_path, other_meta in records
                if other_path != path and record_lists_identifier(other_meta, identifier)
            ),
            None,
        )
        if person is not None:
            orphaned.append(
                OrphanedBounceMark(identifier=identifier, bounce_record=path, person_record=person)
            )
    return orphaned


def fold_orphaned_bounce_marks(contacts_root: Path, *, dry_run: bool = False) -> BounceFoldReport:
    """Fold every stranded mark onto the person record, reporting the count.

    For each pair :func:`find_orphaned_bounce_marks` reports, replays the mark
    onto the person record through the SAME :func:`mark_bounced` path a live
    bounce takes (so a folded mark and a freshly-resolved one are byte-identical,
    rather than two writers drifting apart), then stamps
    :data:`FOLDED_INTO_FIELD` on the slug-keyed record so a re-run is a no-op.

    Non-destructive by construction: nothing is deleted, and the slug-keyed
    record keeps its own mark — folding ADDS the fact to the record consumers
    read, it does not move it. Idempotent: running twice folds nothing the
    second time. Under *dry_run* nothing is written and the report describes
    what would have been.
    """
    orphaned = find_orphaned_bounce_marks(contacts_root)
    if dry_run:
        return BounceFoldReport(folded=orphaned, dry_run=True)

    for pair in orphaned:
        meta = read_bounce_record(pair.bounce_record)
        mark_bounced(
            contacts_root,
            pair.identifier,
            diagnostic=str(meta.get("bounce_diagnostic", "") or ""),
            observed_at=str(meta.get("valid_until", "") or ""),
            source=meta.get("source", ""),
            record_path=pair.person_record,
        )
        person_meta = read_bounce_record(pair.person_record)
        stamped = dict(meta)
        stamped[FOLDED_INTO_FIELD] = str(
            person_meta.get("uid") or pair.person_record.relative_to(Path(contacts_root))
        )
        _, body = parse_frontmatter(pair.bounce_record.read_text(encoding="utf-8"))
        atomic_write_text(pair.bounce_record, render_frontmatter(stamped) + "\n" + body)

    return BounceFoldReport(folded=orphaned, dry_run=False)


# ---------------------------------------------------------------------------
# 6. One-call entity read (issue athenaeum#883/#886)
# ---------------------------------------------------------------------------
#
# The egress-half realization of the two-path invariant
# (``docs/one-way-in-one-way-out.md`` §3) for the excluded-data surface:
# :func:`read_entity` is the ONE sanctioned way to read an entity's page
# together with its excluded data. It resolves the surface root itself — a
# caller supplies a ``uid`` and a boolean, never a path — and, with inclusion
# off, reports each withheld field as a :class:`RedactionMarker` rather than
# silently omitting it, so "redacted" and "absent" never collapse to the same
# shape. (The person-shaped ``read_person`` predecessor was removed in
# athenaeum#888.)

#: The union of :data:`CONTACT_FRONTMATTER_FIELDS` and
#: :data:`CONTACT_IDENTIFIER_FIELDS`, in that stable order — every frontmatter
#: field on a contacts-surface record that can hold an address or number a
#: person is reachable by (issue athenaeum#864). A record migrated by an earlier
#: pass of the athenaeum#479/#502 tooling can still carry ``former_emails`` /
#: ``alt_emails`` alongside (or instead of) the ``emails`` the current migrator
#: writes (see :data:`CONTACT_IDENTIFIER_FIELDS`'s docstring) — a person read
#: that only withheld/returned ``emails``/``phones`` would silently hand a
#: caller an address through whichever side field it forgot to check, which is
#: exactly the leak the redaction marker exists to prevent. The two source
#: tuples share ``"emails"``, so a plain concatenation would iterate it twice
#: (double-reporting one field's redaction marker, or overwriting-but-not-quite
#: a caller's ``contact["emails"]`` with an equal value) — ``dict.fromkeys``
#: dedupes while preserving first-seen order, which is what keeps this the
#: single ``("emails", "phones", "former_emails", "alt_emails")`` iteration
#: order :func:`read_entity` relies on.
CONTACT_DATA_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(CONTACT_FRONTMATTER_FIELDS + CONTACT_IDENTIFIER_FIELDS)
)


#: Frontmatter keys on an excluded record that are BOOKKEEPING — the record's
#: own machinery — and are therefore never reported as data fields by the
#: denylist-complement default in :func:`resolve_excluded_fields` (issue
#: athenaeum#883). Every one of these is written by this module about a record or
#: about one of its values, not by an observer about the entity:
#: ``uid``/``type`` are the join and the class; ``pii`` is the flag; the rest
#: are :func:`mark_bounced`'s and :func:`classify_contact_value`'s own marks.
#:
#: ``contact_classification`` sits here deliberately: it is metadata ABOUT a
#: field (which usage class each value carries), not a contact field itself.
#: It is CONSULTED to classify returned values and never returned as one.
EXCLUDED_RECORD_BOOKKEEPING_FIELDS: frozenset[str] = frozenset(
    {
        "uid",
        "type",
        PII_FLAG,
        "identifier",
        IDENTIFIER_VALIDITY_FIELD,
        CONTACT_CLASSIFICATION_FIELD,
        FOLDED_INTO_FIELD,
        "source",
        "observed_at",
        "valid_until",
        "bounce_diagnostic",
    }
)


def resolve_excluded_fields(
    surface_class: str,
    config: dict[str, Any] | None,
    record_meta: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Which frontmatter fields on an excluded record are DATA, for *surface_class*.

    The per-surface-class field policy that replaces the fixed
    :data:`CONTACT_DATA_FIELDS` allowlist (issue athenaeum#883), resolved in this
    order:

    1. explicit operator config — ``storage.excluded_fields.<surface_class>``,
       a list of frontmatter field names (see
       :func:`athenaeum.config.resolve_excluded_fields_config`);
    2. surface class ``pii`` — the built-in default stays
       :data:`CONTACT_DATA_FIELDS` VERBATIM, so a person read is byte-identical
       to what it returned before this function existed;
    3. any other surface class — every frontmatter field on the record MINUS
       :data:`EXCLUDED_RECORD_BOOKKEEPING_FIELDS`.

    **Why a denylist-complement and not an allowlist for the unknown class.**
    An allowlist for a class nobody has enumerated makes the redaction marker
    *dishonest by omission*: a field the allowlist forgot is reported neither
    as a value nor as a marker, so "withheld" and "absent" collapse into the
    same shape — precisely the failure :class:`RedactionMarker` exists to
    prevent. A denylist-complement is honest by construction, and its failure
    direction is only noise (a bookkeeping key surfaced as a field), never a
    silent hole. Noise is visible and correctable; a hole is neither.

    Rule 3 reads the record, so the returned tuple is per-record for an
    unconfigured class — a record with no fields yields ``()``. Field order is
    the record's own frontmatter order, which keeps a read's field iteration
    stable for a given record. Rules 1 and 2 return the configured/built-in
    order and ignore *record_meta* entirely.
    """
    from athenaeum.config import resolve_excluded_fields_config

    configured = resolve_excluded_fields_config(config).get(surface_class)
    if configured is not None:
        return tuple(configured)
    if surface_class == PII_ENTITY_CLASS:
        return CONTACT_DATA_FIELDS
    if not isinstance(record_meta, Mapping):
        return ()
    return tuple(
        str(name) for name in record_meta if str(name) not in EXCLUDED_RECORD_BOOKKEEPING_FIELDS
    )


def _normalize_frontmatter_uid(raw: Any) -> str:
    """Coerce a raw frontmatter ``uid`` value to the string both uid-keyed
    paths compare against — ``""`` means "absent" for both.

    Shared by :func:`resolve_contact_record_for_uid` and
    :func:`build_contact_record_uid_index` so a single definition governs
    both (issue athenaeum#878). ``uid:`` with no value parses to YAML
    ``None``, and ``str(None)`` is the literal string ``"None"`` — before
    this helper existed, each function independently did ``str(...).strip()``
    on the raw value, so a valueless ``uid:`` indexed/matched under the
    literal ``"None"`` and a lookup for the string ``"None"`` would find it.
    That was reproduced deliberately in athenaeum#877 to keep the two paths
    from diverging, but it was never correct: a valueless uid is absent, not
    a uid whose value happens to be the four characters ``N``, ``o``, ``n``,
    ``e``. Mapping ``None`` to ``""`` here — the same sentinel an explicit
    ``uid: ""`` already normalizes to — makes both paths skip it the same
    way they already skip an explicit empty string.
    """
    if raw is None:
        return ""
    return str(raw).strip()


def resolve_contact_record_for_uid(contacts_root: Path, uid: str) -> Path | None:
    """Find the existing contact record whose frontmatter ``uid`` equals *uid*.

    The ``uid``-keyed sibling of :func:`resolve_contact_record` (which
    resolves by ADDRESS instead) — this is how :func:`read_entity` finds an
    entity's contact record without the caller ever constructing the surface
    path itself (issue athenaeum#864). Mirrors :func:`resolve_contact_record`'s
    discipline exactly:

    - the FIRST match in :func:`iter_contact_records` order when more than one
      record carries the same ``uid`` — deterministic, so the same store
      always resolves a person to the same record;
    - ``log.warning`` (never raise) when more than one record matches;
    - a missing *contacts_root* yields ``None``, same as
      :func:`resolve_contact_record`.

    Compares :func:`_normalize_frontmatter_uid` on both sides. An
    empty/blank *uid* never matches anything — otherwise it would match
    every record with no ``uid:`` field at all, which is never the intent
    of a uid lookup. That includes a record whose ``uid:`` key is present
    but VALUELESS (YAML ``None``): normalized to ``""``, same as an
    explicit ``uid: ""``, so it is treated as absent rather than matching a
    lookup for the literal string ``"None"`` (issue athenaeum#878).
    """
    wanted = _normalize_frontmatter_uid(uid)
    if not wanted:
        return None
    matches = [
        path
        for path in iter_contact_records(contacts_root)
        if _normalize_frontmatter_uid(read_bounce_record(path).get("uid")) == wanted
    ]
    if not matches:
        return None
    if len(matches) > 1:
        log.warning(
            "uid %r resolves to %d contact records; annotating the first "
            "(%s). See resolve_contact_record's docstring for the same "
            "shared-value posture.",
            wanted,
            len(matches),
            matches[0].name,
        )
    return matches[0]


def build_contact_record_uid_index(contacts_root: Path) -> dict[str, Path]:
    """Map every ``uid`` on the contacts surface to its record, in ONE scan.

    The batch-shaped counterpart to :func:`resolve_contact_record_for_uid`
    (issue athenaeum#877). That function answers one uid and costs a full
    :func:`iter_contact_records` scan to do it; a caller resolving N uids
    therefore paid that scan N times — 4,696 uids against the live corpus
    projected to ~37 hours. This builds the whole ``uid -> record`` mapping
    in a single pass, so the scan is paid O(1) times for any number of
    lookups. :func:`read_entities` is the caller that exists to use it.

    Resolution discipline is IDENTICAL to
    :func:`resolve_contact_record_for_uid`, so a batch read never resolves a
    uid to a different record than a single read would:

    - the FIRST match in :func:`iter_contact_records` order wins (that
      function sorts, so the same store always yields the same mapping);
    - ``log.warning`` (never raise) when more than one record carries a uid;
    - the key is :func:`_normalize_frontmatter_uid` of the record's ``uid``
      frontmatter, the SAME normalizer the single-lookup function compares
      with. A record with no ``uid`` key, ``uid: ""``, or a VALUELESS
      ``uid:`` (YAML ``None``) is skipped — an empty/absent uid must never
      match anything, mirroring that function's empty-uid guard. Before
      issue athenaeum#878, a valueless ``uid:`` indexed under the literal
      string ``"None"`` (from ``str(None)``) and so matched a lookup for
      that string; both paths now treat it as absent instead, via the
      shared normalizer.

    A missing *contacts_root* yields ``{}`` (:func:`iter_contact_records`
    returns ``[]`` for one), never a raise.
    """
    index: dict[str, Path] = {}
    duplicates: dict[str, int] = {}
    for path in iter_contact_records(contacts_root):
        uid = _normalize_frontmatter_uid(read_bounce_record(path).get("uid"))
        if not uid:
            continue
        if uid in index:
            # Count rather than warn per occurrence: one line per duplicated
            # uid, not one per extra record, keeps a corpus-wide scan's log
            # proportional to the problem instead of to the corpus.
            duplicates[uid] = duplicates.get(uid, 1) + 1
            continue
        index[uid] = path
    for uid, count in duplicates.items():
        log.warning(
            "uid %r resolves to %d contact records; indexing the first "
            "(%s). See resolve_contact_record's docstring for the same "
            "shared-value posture.",
            uid,
            count,
            index[uid].name,
        )
    return index


@dataclass(frozen=True)
class RedactionMarker:
    """One withheld contact field on an :class:`EntityRead` (issue athenaeum#864).

    Names the field and that a value EXISTS, without the value itself — the
    property that lets a caller distinguish "this person has an email on
    file, withheld" from "this person has no email at all". Without this
    marker both cases present identically (no ``contact[field]`` key), and a
    caller reading "no email" cannot tell whether to route around the person
    or whether they simply were not given the address they need.
    """

    field: str
    value_count: int
    redacted: bool = True

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape (``dict``, all scalar values)."""
        return {
            "field": self.field,
            "value_count": self.value_count,
            "redacted": self.redacted,
        }


def json_date_default(obj: object) -> str:
    """``default=`` callback for ``json.dumps``: coerce dates to ISO-8601 (issue athenaeum#1002).

    The ONE coercion point for every JSON-emitting read surface built over the
    sanctioned excluded-field read path — ``read_entity``
    (:meth:`EntityRead.to_dict` carries ``frontmatter`` unconverted) and
    ``recall(with_pii=True)`` (:mod:`athenaeum.mcp_server`'s excluded-facts
    render and handle-resolution join, both of which can carry a
    :class:`ContactClassification`'s ``source`` straight from record
    frontmatter). Passed as ``default=`` at each call site rather than
    reimplemented per site, so the three surfaces cannot drift in HOW they
    coerce a date and a caller never has to walk its own payload first —
    ``json.dumps`` already recurses into nested dicts/lists on its own and
    calls this function for exactly the values it cannot otherwise encode.

    :mod:`yaml`'s safe loader (:func:`athenaeum.models.parse_frontmatter`)
    parses a bare frontmatter date (``dob: 1990-01-01``) into
    ``datetime.date`` and a timestamp into ``datetime.datetime`` — neither of
    which the stdlib ``json`` module can serialize on its own, so an affected
    page previously crashed every read tool with ``Object of type date is not
    JSON serializable`` before this existed.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


@dataclass(frozen=True)
class EntityRead:
    """Result of :func:`read_entity` — an entity's page plus excluded-data view.

    Renamed field-for-field from ``PersonRead`` (issue athenaeum#883). A
    ``PersonRead = EntityRead`` back-compat alias existed from athenaeum#883
    through athenaeum#887's deprecation window; it was removed in athenaeum#888
    alongside ``read_person``/``read_people``, its only remaining referrers.

    The ``contact`` / ``contact_included`` / ``contact_record_path`` field
    names stay as-is on the generic type. Renaming them would change the JSON
    keys live consumers already read (``to_dict`` is the MCP and CLI payload),
    which is a breaking change this generalization has no reason to make. An
    accepted naming wart; renaming the payload keys is a separate decision.

    ``contact`` holds real values only when the read was made with
    ``include_contact=True``; ``redactions`` is non-empty only when contact
    data was withheld (``include_contact=False``) AND the person's contact
    record actually carries a non-empty value for at least one field. The
    four cells (issue athenaeum#864 AC):

    - include off, record present with values: ``contact == {}``,
      ``redactions`` non-empty.
    - include off, no record / no values: ``contact == {}``,
      ``redactions == ()``.
    - include on, record present with values: ``contact`` holds the values,
      ``redactions == ()``.
    - include on, no record / no values: ``contact == {}``,
      ``redactions == ()``.

    ``contact_record_path`` is ``None`` exactly when no contact record was
    found — never an error, per the issue's "person whose contact record does
    not exist returns the page with no redaction markers" criterion.

    ``classifications`` (issue athenaeum#866) carries the usage classification
    of every value present in ``contact``, keyed by field and co-indexed with
    it — ``classifications[field][i]`` classifies ``contact[field][i]``, so a
    caller receiving an address always knows which kind it is and never has to
    make a second call to find out. It is empty exactly when ``contact`` is: a
    redacted read exposes no values, so it classifies none.

    ``validity`` (issue athenaeum#851) is the third co-indexed map, on the same
    contract: ``validity[field][i]`` is the :class:`IdentifierValidity` of
    ``contact[field][i]``. With it, a caller holding a value holds all three
    things the store knows about that value — what it is, how it was obtained,
    and whether it is still open — in ONE read. It is likewise empty exactly
    when ``contact`` is.

    ``do_not_email`` (issue athenaeum#851) is a per-RECORD fact rather than a
    per-value one, so it is a single :class:`DoNotEmailState` rather than a
    co-indexed map. Unlike the two maps above it is populated even on a
    REDACTED read: it carries no contact value to withhold — it is a mark, not
    an address — and withholding the mark while returning the page would leave
    a consumer unable to learn the one thing that most constrains what it may
    do. An entity with no excluded record gets ``marked=False`` with no
    provenance, the same "nothing recorded" answer :func:`do_not_email_state`
    gives for a record without the key.

    None of these three fields states whether the entity may be contacted.
    They are facts; eligibility is the consumer's policy over them.
    """

    uid: str
    page_path: Path
    frontmatter: dict[str, Any]
    body: str
    contact: dict[str, list[str]]
    redactions: tuple[RedactionMarker, ...]
    contact_included: bool
    contact_record_path: Path | None
    # `dataclass_field`, not `field` — two long-standing functions in this
    # module use `field` as a loop variable, and importing the bare name would
    # shadow them (ruff F402).
    classifications: dict[str, list[ContactClassification]] = dataclass_field(default_factory=dict)
    validity: dict[str, list[IdentifierValidity]] = dataclass_field(default_factory=dict)
    do_not_email: DoNotEmailState = dataclass_field(
        default_factory=lambda: DoNotEmailState(marked=False)
    )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict: paths as ``str``, redactions as a list of dicts."""
        return {
            "uid": self.uid,
            "page_path": str(self.page_path),
            "frontmatter": self.frontmatter,
            "body": self.body,
            "contact": self.contact,
            "redactions": [marker.to_dict() for marker in self.redactions],
            "contact_included": self.contact_included,
            "contact_record_path": (
                str(self.contact_record_path) if self.contact_record_path is not None else None
            ),
            "classifications": {
                name: [item.to_dict() for item in values]
                for name, values in self.classifications.items()
            },
            "validity": {
                name: [item.to_dict() for item in values] for name, values in self.validity.items()
            },
            "do_not_email": self.do_not_email.to_dict(),
        }


def assemble_excluded_read(
    page_path: Path,
    page_frontmatter: Mapping[str, Any],
    record_meta: dict[str, Any] | None,
    *,
    surface_class: str,
    config: dict[str, Any] | None = None,
    include_excluded: bool = False,
    usage_classes: Collection[str] | None = None,
) -> tuple[
    dict[str, list[str]],
    tuple[RedactionMarker, ...],
    dict[str, list[ContactClassification]],
]:
    """Assemble ``(fields, redactions, classifications)`` from RESOLVED inputs.

    The marker/field logic that used to be private inside
    ``_person_read_from_indexes``, made public and — critically —
    :class:`~athenaeum.models.EntityIndex`-FREE (issue athenaeum#883). That function
    required an ``EntityIndex`` it needed only to find the page; this half of
    the work needs no index at all, just the already-resolved page and the
    matched excluded record's frontmatter.

    That is the seam ``recall`` needs (issue athenaeum#885): recall already holds the
    hit's fresh frontmatter from its own Layer-C re-read, and must not pay to
    rebuild an ``EntityIndex`` to reach this logic — 25.2s of the measured
    28.1s single-call cost is ``EntityIndex`` construction, not the contacts
    scan. :func:`read_entity` and :func:`read_entities` are callers of this
    function, not the only way to reach it.

    Args:
        page_path: The entity's already-resolved wiki page. Not read here —
            the caller has already parsed it — but part of the call contract
            so the assembly seam takes a COMPLETE resolved read, and so a
            uid disagreement between the page and the record it was joined to
            can be reported against a concrete file.
        page_frontmatter: That page's parsed frontmatter, likewise already in
            the caller's hand. Consulted only for the uid cross-check.
        record_meta: The matched excluded record's frontmatter, or ``None``
            when no record matched — which is not an error: the entity simply
            has no excluded data, and the read returns three empty containers.
        surface_class: The **surface class** (``storage.mapping`` key, e.g.
            ``"pii"``) whose field policy applies — never a page ``type:``.
        config: Resolved ``athenaeum.yaml``, for the
            ``storage.excluded_fields`` override.
        include_excluded: When ``False``, every non-empty field yields a
            :class:`RedactionMarker` instead of its values.
        usage_classes: Restrict returned values to these usage classes (issue
            athenaeum#866). ``None`` returns every value. Only meaningful with
            *include_excluded* — a redacted read exposes no values to filter.

    Returns:
        ``(fields, redactions, classifications)`` — the three payload pieces
        of an :class:`EntityRead`, with the same four-cell semantics that type
        documents.
    """
    if not record_meta:
        return {}, (), {}

    record_uid = str(record_meta.get("uid", "")).strip()
    page_uid = str(page_frontmatter.get("uid", "")).strip() if page_frontmatter else ""
    if record_uid and page_uid and record_uid != page_uid:
        # Never raises: the join is the caller's, and a mismatch is a
        # store-consistency signal, not a reason to deny a read. Loud because
        # returning one entity's excluded values on another's page is the
        # worst failure this module has.
        log.warning(
            "excluded record uid %r does not match page uid %r (%s); "
            "assembling the read as joined by the caller.",
            record_uid,
            page_uid,
            page_path.name,
        )

    wanted_classes = frozenset(usage_classes) if usage_classes is not None else None
    fields: dict[str, list[str]] = {}
    classifications: dict[str, list[ContactClassification]] = {}
    redactions: list[RedactionMarker] = []
    for field_name in resolve_excluded_fields(surface_class, config, record_meta):
        raw = record_meta.get(field_name)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v).strip() for v in values if str(v).strip()]
        if not values:
            continue
        if not include_excluded:
            # Count BEFORE class filtering: the marker reports what the record
            # holds for this field, and a caller who asked for no excluded data
            # never named a class to filter by (issue athenaeum#866).
            redactions.append(RedactionMarker(field=field_name, value_count=len(values)))
            continue
        classified = [classification_for_value(record_meta, value) for value in values]
        if wanted_classes is not None:
            kept = [
                (value, item)
                for value, item in zip(values, classified, strict=True)
                if item.usage_class in wanted_classes
            ]
            if not kept:
                # Every value of this field was filtered out. Drop the field
                # entirely rather than emitting an empty list, so "this field
                # has no value of the class you asked for" and "this field has
                # no value at all" present identically to a caller that must
                # not see the other class — the whole point of the filter.
                continue
            values = [value for value, _ in kept]
            classified = [item for _, item in kept]
        fields[field_name] = values
        classifications[field_name] = classified

    return fields, tuple(redactions), classifications


def _entity_read_from_indexes(
    uid: str,
    *,
    entity_index: EntityIndex,
    contact_records: Mapping[str, Path],
    include_excluded: bool,
    wanted_classes: frozenset[str] | None,
    surface_class: str,
    config: dict[str, Any] | None,
) -> EntityRead | None:
    """Assemble one :class:`EntityRead` from ALREADY-BUILT indexes.

    The shared body of :func:`read_entity` (one uid, indexes built for the
    call) and :func:`read_entities` (N uids, indexes built once) — extracted so
    the two entry points cannot drift in what they return (issue athenaeum#877).
    Both index arguments are read-only here; this function builds neither, and
    so is the only part of the read that is genuinely O(1) per uid.

    *wanted_classes* is the pre-``frozenset``-ed form of ``usage_classes`` —
    normalized once by the caller rather than per uid, which is the same "pay
    it once for the batch" property the indexes have.

    The field/marker work itself is :func:`assemble_excluded_read`'s; this
    function is only the index-shaped half (page lookup, record lookup) that
    a caller holding a resolved page — ``recall`` — must be able to skip.
    """
    page_path = entity_index.get_by_uid(uid)
    if page_path is None:
        return None

    frontmatter, body = parse_frontmatter(page_path.read_text(encoding="utf-8"))

    record_path = contact_records.get(str(uid).strip()) if str(uid).strip() else None
    record_meta = read_bounce_record(record_path) if record_path is not None else {}

    fields, redactions, classifications = assemble_excluded_read(
        page_path,
        frontmatter,
        record_meta,
        surface_class=surface_class,
        config=config,
        include_excluded=include_excluded,
        usage_classes=wanted_classes,
    )

    return EntityRead(
        uid=uid,
        page_path=page_path,
        frontmatter=frontmatter,
        body=body,
        contact=fields,
        redactions=redactions,
        contact_included=include_excluded,
        contact_record_path=record_path,
        classifications=classifications,
        validity=assemble_excluded_validity(record_meta, fields),
        # Populated even when the read is redacted — see `EntityRead`: the mark
        # carries no contact value to withhold. Reads BOTH surfaces (issue
        # athenaeum#960): the page's own frontmatter is already in hand as
        # `frontmatter`, no extra read needed.
        do_not_email=do_not_email_state(record_meta, frontmatter),
    )


def read_entity(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    uid: str,
    *,
    surface_class: str,
    include_excluded: bool = False,
    usage_classes: Collection[str] | None = None,
    excluded_index: "ExcludedRecordIndex | None" = None,
    entity_index: EntityIndex | None = None,
) -> EntityRead | None:
    """Read one entity's wiki page, with excluded data gated by *include_excluded*.

    The entity-class-generic form of the person-shaped read that preceded it
    (issue athenaeum#883; the ``read_person`` wrapper itself was removed in
    athenaeum#888 once every known consumer had migrated). It resolves an
    entity of ANY wiki ``type:`` through :class:`~athenaeum.models.EntityIndex`
    (which has always indexed every type by uid, not just persons) and joins
    it to the excluded record for *surface_class* — so the read that was
    person-shaped by accident of how it was built now works for any class the
    operator has routed to an excluded surface.

    Semantics are the former person-shaped read's, unchanged, with two
    generalizations: the surface is *surface_class*'s rather than always
    ``pii``'s, and which frontmatter fields count as data comes from
    :func:`resolve_excluded_fields`'s per-class policy rather than the fixed
    :data:`CONTACT_DATA_FIELDS` allowlist. The four inclusion/record cells, the
    :class:`RedactionMarker` per withheld non-empty field, and the co-indexed
    ``classifications`` map are all identical — see :class:`EntityRead`.

    Args:
        knowledge_root: Root of the knowledge base (parent of ``wiki/``).
        config: Resolved ``athenaeum.yaml``.
        uid: The entity's durable identifier.
        surface_class: The **surface class** (``storage.mapping`` key) whose
            excluded surface this entity's record lives on — NOT the page's
            ``type:``. A ``type: person`` page's record lives on the ``pii``
            surface, which is why the two names cannot be collapsed. This
            function is told the surface class and never guesses; mapping a
            page class onto one is issue athenaeum#885's job. Required, with no
            default: a generic read that quietly fell back to ``pii`` would
            join an entity of any class to the person surface and report the
            result as authoritative.
        include_excluded: Inclusion flag, default ``False``. Who may set it
            remains the deferred athenaeum#864 question — this function neither
            widens nor narrows it.
        usage_classes: Restrict returned values to these usage classes
            (issue athenaeum#866), exactly as the former person-shaped read.
        excluded_index: An already-built :class:`ExcludedRecordIndex` over
            *surface_class*'s surface (issue athenaeum#1124), for a caller
            resolving several uids on the ambiguous-handle branch — each
            call otherwise pays :func:`resolve_contact_record_for_uid`'s full
            :func:`iter_contact_records` scan on its own (97.8% of one
            profiled call; see the issue). When supplied, its
            :meth:`~ExcludedRecordIndex.by_uid` replaces that scan; the
            caller is responsible for building it over the SAME
            *surface_class* root (:func:`excluded_surface_root`) this call
            would otherwise resolve — a mismatched surface is not detected
            here (unlike :func:`athenaeum.identity_resolution._assemble_contact_values`,
            which can compare roots because it also computes them; this
            function is simply told the index to use). ``None`` (the
            default, and every pre-athenaeum#1124 caller) resolves the
            single uid itself, exactly as before. **Staleness
            (athenaeum#850):** valid only for a read-only window —
            :func:`mark_bounced` mints records and merges identifiers onto
            existing ones, so a caller interleaving bounce writes with reads
            in the same process must rebuild or invalidate this index at
            that boundary.
        entity_index: An already-built
            :class:`~athenaeum.models.EntityIndex` over the compiled wiki,
            for the same batch-of-uids reason. ``None`` (the default) builds
            one per call. Prefer :func:`read_entities` when resolving many
            uids at once — it builds both indexes for the whole batch
            without the caller assembling them by hand; these parameters
            exist for a caller (like the ambiguous-handle branch, which
            calls this once per candidate uid rather than once for all of
            them) that cannot restructure itself into a single
            :func:`read_entities` call but can still hold one pair of
            indexes across those calls.

    Returns:
        An :class:`EntityRead`, or ``None`` when *uid* resolves to no wiki
        page — the only ``None`` case. An entity with no excluded record is
        NOT one: the page is returned with empty data and no markers.

    Cost:
        With neither *excluded_index* nor *entity_index* supplied, builds
        both O(corpus) indexes per call, as the former person-shaped read
        always has (~28s against the live 16,928-page store, of which 25.2s
        is ``EntityIndex``). **Resolving more than one uid: use
        :func:`read_entities`, or hold prepared indexes across repeated
        calls via the parameters above.** A caller that already HAS the page
        and its frontmatter — ``recall`` — should call
        :func:`assemble_excluded_read` directly and build no index at all.
    """
    if excluded_index is not None:
        record_path = excluded_index.by_uid(uid)
    else:
        contacts_root = excluded_surface_root(surface_class, knowledge_root, config)
        # Single lookup: resolve just this uid rather than indexing the whole
        # surface. Same one-scan cost, but it warns only about a collision on
        # the uid actually asked for — the batch builder's corpus-wide
        # warning would be noise in a one-uid read.
        record_path = resolve_contact_record_for_uid(contacts_root, uid)
    contact_records = {} if record_path is None else {str(uid).strip(): record_path}
    return _entity_read_from_indexes(
        uid,
        entity_index=(
            entity_index if entity_index is not None else EntityIndex(knowledge_root / "wiki")
        ),
        contact_records=contact_records,
        include_excluded=include_excluded,
        wanted_classes=frozenset(usage_classes) if usage_classes is not None else None,
        surface_class=surface_class,
        config=config,
    )


class ExcludedSurfaceUnavailable(RuntimeError):
    """The excluded surface could not be read — raised by the FAIL-CLOSED read path.

    The failure mode this exists to prevent (issue athenaeum#851): a store that
    cannot be reached returning an empty result, which a consumer reasonably
    reads as "nothing suppressed" and acts on by sending. **A false skip is
    recoverable by a human; a false send is not.** So the read path for
    suppression facts raises rather than answering emptily.

    Note the asymmetry with :func:`iter_contact_records`, which returns ``[]``
    for a missing root and does NOT raise. That is correct for its callers —
    :func:`mark_bounced` MINTS the first record on a surface that does not
    exist yet, and a write path that refused to start on an empty store could
    never bootstrap one. Reading and writing genuinely want opposite defaults
    here, which is why this contract lives on the read entry point rather than
    being pushed down into the shared scan.
    """


@dataclass(frozen=True)
class IdentifierFacts:
    """Everything the excluded surface knows about ONE contact value.

    The per-identifier result of :func:`read_identifier_facts` (issue
    athenaeum#851). It is a FACTS record, not a verdict: there is deliberately
    no ``suppressed`` / ``may_email`` / ``eligible`` field on it, because
    whether a value may be used for outreach is the consumer's policy, not
    athenaeum's. See the module note on eligibility.

    ``known`` is the load-bearing field, and it is stated POSITIVELY rather
    than left to be inferred from empty containers. "We have never heard of
    this address" and "we know this address and hold no mark against it" are
    different answers with different consequences, and a consumer that infers
    a stranger from an absence silently treats strangers as safe — the exact
    conflation athenaeum#851 (and `maecenas#97`, which joins on it) exists to
    make impossible. When ``known`` is ``False`` every other fact field is
    ``None``/unset, and that is not an answer of "nothing against them".

    ``ambiguous`` marks an address listed by MORE THAN ONE record — legitimate
    (a shared family or role address) but not resolvable to a single person.
    The facts returned are the FIRST matching record's, matching
    :meth:`ExcludedRecordIndex.by_identifier`'s first-wins posture; the flag is
    what lets a caller that must not guess (identity resolution) refuse, while
    a caller that only needs deliverability proceeds.
    """

    identifier: str
    known: bool
    uid: str | None = None
    record_path: Path | None = None
    classification: ContactClassification | None = None
    validity: IdentifierValidity | None = None
    do_not_email: DoNotEmailState = dataclass_field(
        default_factory=lambda: DoNotEmailState(marked=False)
    )
    ambiguous: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape; ``None`` facts stay ``None`` (see ``known``)."""
        return {
            "identifier": self.identifier,
            "known": self.known,
            "uid": self.uid,
            "record_path": (str(self.record_path) if self.record_path is not None else None),
            "classification": (
                self.classification.to_dict() if self.classification is not None else None
            ),
            "validity": self.validity.to_dict() if self.validity is not None else None,
            "do_not_email": self.do_not_email.to_dict(),
            "ambiguous": self.ambiguous,
        }


def read_identifier_facts(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    identifiers: Iterable[str],
    *,
    surface_class: str = PII_ENTITY_CLASS,
    as_of: date | None = None,
    index: "ExcludedRecordIndex | None" = None,
) -> Iterator[tuple[str, IdentifierFacts]]:
    """Read the excluded surface's facts for MANY addresses, on ONE corpus scan.

    The bulk read issue athenaeum#851 needs, in the shape of
    :func:`read_entities` — the by-ADDRESS sibling of that by-uid batch. A
    campaign evaluating ~16.9k contacts calls this once and pays the O(corpus)
    scan once; the per-identifier work is a dict lookup plus reading that
    record's own file.

    It is built on :class:`ExcludedRecordIndex` (athenaeum#883), which already
    indexes a surface by uid AND by address in a single
    :func:`iter_contact_records` pass — deliberately NOT a second index, so
    there is one definition of how an address resolves to a record and the two
    cannot drift.

    **This returns facts and no verdict.** There is no eligibility predicate
    here and none is coming: athenaeum states what it knows and how it knows
    it; the consumer decides what to do about it. See the module note.

    **Fail-closed** (the athenaeum#851 AC): a surface root that does not exist,
    is not a directory, or cannot be listed raises
    :class:`ExcludedSurfaceUnavailable` rather than yielding
    ``known=False`` for every identifier — which is indistinguishable from a
    clean store in which nobody is suppressed, and would be acted on by
    sending. The check runs ONCE, on the first identifier pulled, alongside the
    index build.

    Args:
        knowledge_root: Root of the knowledge base (parent of ``wiki/``). The
            caller supplies this and never constructs a surface path — the
            two-path invariant (``docs/one-way-in-one-way-out.md`` §3), which
            is why this is an entry point rather than an exported index.
        config: Resolved ``athenaeum.yaml``.
        identifiers: The addresses to look up. Consumed lazily.
        surface_class: The **surface class** whose excluded surface holds these
            records; defaults to :data:`PII_ENTITY_CLASS`, the only surface
            that carries contact values today.
        as_of: Evaluate every valid-time close as of this date (default:
            today), so a campaign can ask "was this closed when the segment was
            cut" rather than only "is it closed now".
        index: An already-built :class:`ExcludedRecordIndex` to resolve
            through — for a caller interleaving reads with
            :func:`mark_bounced` writes on the same batch, so both see one
            index and one scan.

    Each yielded fact's ``do_not_email`` reads BOTH surfaces (issue
    athenaeum#960): the excluded record AND, when the record carries a
    ``uid``, that uid's wiki page — the surface every live mark actually
    lives on. Resolving uid to page needs
    :class:`~athenaeum.models.EntityIndex`, built lazily on the first
    identifier that resolves to a record (same "pay once for the batch, pay
    nothing for an empty one" posture as the excluded-surface index above,
    and the same index :func:`read_entities` already builds once per batch).

    Yields:
        ``(identifier, IdentifierFacts)`` pairs in the order *identifiers*
        supplies them. Pairs (not a bare sequence) so a caller always knows
        which address a result belongs to; input order (not a dict) so
        duplicates are neither collapsed nor reordered. EVERY identifier
        yields a pair — an unknown address yields ``known=False``, never a
        skipped entry, because a silently missing row is exactly the absence a
        consumer would misread.

    Raises:
        ExcludedSurfaceUnavailable: The surface could not be read. Never
            swallowed, never downgraded to an empty answer.
    """
    resolved_index = index
    entity_index: EntityIndex | None = None
    for identifier in identifiers:
        if resolved_index is None:
            # Built on the FIRST identifier, not at call time, exactly as
            # `read_entities` builds its indexes: a caller whose candidate list
            # came back empty pays nothing rather than a full O(corpus) pass to
            # read zero facts. The fail-closed check rides along for the same
            # reason — an empty batch asks the store nothing, so it has nothing
            # to be wrong about.
            contacts_root = excluded_surface_root(surface_class, knowledge_root, config)
            _require_readable_surface(contacts_root, surface_class)
            resolved_index = ExcludedRecordIndex(contacts_root)
        if entity_index is None:
            # Same lazy-on-first-use posture as `resolved_index` above, and
            # the same index `read_entities` builds once per batch — never
            # rebuilt per identifier (issue athenaeum#883's cost note on
            # `EntityIndex` construction applies here too).
            entity_index = EntityIndex(knowledge_root / "wiki")
        yield (
            identifier,
            _facts_for_identifier(identifier, resolved_index, as_of, entity_index=entity_index),
        )


def _require_readable_surface(contacts_root: Path, surface_class: str) -> None:
    """Raise :class:`ExcludedSurfaceUnavailable` unless *contacts_root* is listable.

    Deliberately probes with an actual directory listing rather than only
    :meth:`~pathlib.Path.is_dir`: a surface that exists but cannot be read
    (permissions, an unmounted volume that still has a mount point, a
    decryption layer that is not up) is precisely the "unreachable store" the
    fail-closed contract is about, and ``is_dir()`` returns ``True`` for it.
    """
    try:
        if not contacts_root.is_dir():
            raise ExcludedSurfaceUnavailable(
                f"excluded surface for {surface_class!r} is not readable at "
                f"{contacts_root} (no such directory). Refusing to report "
                "'nothing recorded' for a store that was never read — a false "
                "skip is recoverable, a false send is not."
            )
        next(iter(contacts_root.iterdir()), None)
    except ExcludedSurfaceUnavailable:
        raise
    except OSError as exc:
        raise ExcludedSurfaceUnavailable(
            f"excluded surface for {surface_class!r} at {contacts_root} could "
            f"not be listed: {exc}. Refusing to report 'nothing recorded' for "
            "a store that was never read."
        ) from exc


def _facts_for_identifier(
    identifier: str,
    index: "ExcludedRecordIndex",
    as_of: date | None,
    *,
    entity_index: EntityIndex | None = None,
) -> IdentifierFacts:
    """Assemble one :class:`IdentifierFacts` from an ALREADY-BUILT index.

    The per-identifier half of :func:`read_identifier_facts`, extracted for the
    same reason :func:`_entity_read_from_indexes` was: it is the only genuinely
    O(1)-per-key part of the read, and keeping it separate makes the one-scan
    property visible rather than buried in a loop.

    *entity_index*, when supplied, resolves the matched record's ``uid`` back
    to its wiki page (issue athenaeum#960) so ``do_not_email`` can read that
    surface too — ``None`` (the default, and every pre-existing caller) skips
    the resolution and reads only the excluded-record surface, exactly as
    issue athenaeum#851 shipped.
    """
    matches = index.all_by_identifier(identifier)
    if not matches:
        # Stated, not inferred: `known=False` with every fact field unset. This
        # is NOT "no marks against them".
        return IdentifierFacts(identifier=identifier, known=False)
    record_path = matches[0]
    meta = read_bounce_record(record_path)
    uid = str(meta.get("uid", "")).strip() or None
    page_frontmatter: dict[str, Any] | None = None
    if uid is not None and entity_index is not None:
        page_path = entity_index.get_by_uid(uid)
        if page_path is not None:
            page_frontmatter = read_bounce_record(page_path)
    return IdentifierFacts(
        identifier=identifier,
        known=True,
        uid=uid,
        record_path=record_path,
        classification=classification_for_value(meta, identifier),
        validity=validity_for_value(meta, identifier, as_of),
        do_not_email=do_not_email_state(meta, page_frontmatter),
        ambiguous=len(matches) > 1,
    )


def read_entities(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    uids: Iterable[str],
    *,
    surface_class: str,
    include_excluded: bool = False,
    usage_classes: Collection[str] | None = None,
) -> Iterator[tuple[str, EntityRead | None]]:
    """Read MANY entities, paying each O(corpus) scan once (issues athenaeum#877/#883).

    The batch counterpart to :func:`read_entity` (the primitive the removed
    ``read_people`` wrapper was built over — see athenaeum#888). Every yielded
    :class:`EntityRead` is exactly what :func:`read_entity` would have returned
    for that uid — the batch differs ONLY in what it costs.

    Both O(corpus) indexes (the wiki :class:`~athenaeum.models.EntityIndex` and
    the surface's ``uid -> record`` mapping) are built exactly ONCE for the
    whole batch, lazily on the FIRST uid rather than at call time — so an empty
    batch costs nothing rather than one full pass to read zero entities. The
    stream is lazy for the same reason it always was: an
    :class:`EntityRead` carries the entity's full page body, so materializing
    thousands at once would hold much of the corpus in memory at peak.

    Yields:
        ``(uid, EntityRead | None)`` pairs in the order *uids* supplies them —
        ``None`` for a uid resolving to no wiki page. Pairs (not a bare
        sequence) so a caller always knows WHICH uid a result belongs to;
        input order (not a dict) so duplicate uids are neither collapsed nor
        reordered.

    The two-path invariant is preserved (``docs/one-way-in-one-way-out.md``
    §3): the caller supplies uids and flags, and THIS function resolves the
    surface root and the records within it. A caller still never constructs a
    surface path — which is what made a batch entry point the right fix rather
    than exporting the index for callers to scan themselves.
    """
    wanted_classes = frozenset(usage_classes) if usage_classes is not None else None
    entity_index: EntityIndex | None = None
    contact_records: Mapping[str, Path] = {}
    for uid in uids:
        if entity_index is None:
            # Built on the FIRST uid, not at call time: a caller whose
            # candidate list came back empty (a quiet week for the weekly
            # enrichment job) then pays nothing at all rather than a full
            # O(corpus) pass to read zero entities.
            entity_index = EntityIndex(knowledge_root / "wiki")
            contact_records = build_contact_record_uid_index(
                excluded_surface_root(surface_class, knowledge_root, config)
            )
        yield (
            uid,
            _entity_read_from_indexes(
                uid,
                entity_index=entity_index,
                contact_records=contact_records,
                include_excluded=include_excluded,
                wanted_classes=wanted_classes,
                surface_class=surface_class,
                config=config,
            ),
        )


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
    "find_hard_bounce_code",
    "detect_hard_bounce_fact",
    "default_bounce_record_path",
    "read_bounce_record",
    "is_bounced",
    "mark_bounced",
    "CONTACT_IDENTIFIER_FIELDS",
    "IDENTIFIER_VALIDITY_FIELD",
    "FOLDED_INTO_FIELD",
    "normalize_identifier",
    "identifiers_on_record",
    "record_lists_identifier",
    "iter_contact_records",
    "resolve_contact_record",
    "resolve_contact_records",
    "uid_on_record",
    "identifier_validity_entries",
    "is_bounced_identifier",
    "OrphanedBounceMark",
    "BounceFoldReport",
    "find_orphaned_bounce_marks",
    "fold_orphaned_bounce_marks",
    "CONTACT_DATA_FIELDS",
    "EXCLUDED_RECORD_BOOKKEEPING_FIELDS",
    "ExcludedRecordIndex",
    "excluded_surface_root",
    "resolve_excluded_fields",
    "assemble_excluded_read",
    "resolve_contact_record_for_uid",
    "build_contact_record_uid_index",
    "RedactionMarker",
    "EntityRead",
    "read_entity",
    "read_entities",
    "CONTACT_CLASSIFICATION_FIELD",
    "USAGE_CLASS_OBSERVED",
    "USAGE_CLASS_PROVIDER",
    "USAGE_CLASS_UNCLASSIFIED",
    "USAGE_CLASSES",
    "OUTREACH_ELIGIBLE_CLASSES",
    "ContactClassification",
    "contact_classification_entries",
    "classification_for_value",
    "is_outreach_eligible",
    "classify_contact_value",
    "DO_NOT_EMAIL_FIELD",
    "DoNotEmailState",
    "do_not_email_state",
    "IdentifierValidity",
    "validity_for_value",
    "assemble_excluded_validity",
    "ExcludedSurfaceUnavailable",
    "IdentifierFacts",
    "read_identifier_facts",
]
