# SPDX-License-Identifier: Apache-2.0
"""Joining the pii bounce mark to the wiki ``bounced:`` field (issue athenaeum#852).

Two surfaces record bounce facts, and until this module they recorded them
independently of one another:

- the **contacts surface** — a valid-time close written by
  :func:`athenaeum.pii.mark_bounced` (issue athenaeum#765), keyed by email identifier;
- the **wiki `bounced:` frontmatter field** — what downstream consumers
  actually read at segment time, keyed by page ``uid``.

Neither could see the other, because they shared no key: a wiki page carries a
``uid`` and no address, and the athenaeum#765 slug-keyed bounce record carried an
address and no ``uid``. The **excluded person record** is the only object
holding both halves — which is why athenaeum#850 (resolving a mark onto that record
rather than a slug-keyed sibling) is what makes this module possible at all.

**The chain**, re-derived from the record shapes on disk rather than assumed:

    identifier                                    (an email address)
      -> person record on the contacts surface    (lists it under `emails:`)
      -> that record's `uid:`                     (written by the athenaeum#427/#437 migrator)
      -> the wiki page carrying the same `uid:`   (what consumers hold)

**Direction (P6).** Wiki frontmatter remains consumer truth, so the question
this module answers is "what does a consumer holding a wiki page know about
deliverability" — and the pii mark reaches that answer by the chain above,
**at read time**. This module deliberately does NOT write ``bounced:`` onto
wiki pages; see ``docs/bounce-surface-convergence.md`` for why that direction
was chosen over propagating the mark into the corpus, and what the alternative
would have cost.

**Evidence classes stay separate — this is the load-bearing property.** The
two surfaces do not hold the same KIND of evidence. The pii mark exists only
where :func:`athenaeum.pii.detect_hard_bounce_fact` matched, which requires an
RFC 3463 ``5.x.x`` permanent-failure code. The wiki field is a union surface,
strictly broader: list-verification verdicts, DSN-derived replies carrying a
bare SMTP code with no enhanced code, transient ``4.x`` observations, and
CRM-notes markers all appear there. So :class:`Deliverability` reports the two
halves SEPARATELY and never collapses them into one boolean — collapsing them
would promote a transient or a list-verification verdict to a hard bounce,
which no code path here is allowed to do. The wiki value is carried verbatim
and opaque: this module never parses it, classifies it, or feeds it back into
a pii mark.

**Layering:** L4. Imports :mod:`athenaeum.pii` (L3), :mod:`athenaeum.models`
(L1) and :mod:`athenaeum.storage_migrate`'s public entity-page scan — all
lower, so this stays a one-way edge. Nothing in :mod:`athenaeum.pii` imports
back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter
from athenaeum.pii import (
    identifiers_on_record,
    is_bounced_identifier,
    iter_contact_records,
    read_bounce_record,
    resolve_contact_record,
)
from athenaeum.storage_migrate import iter_entity_pages

log = logging.getLogger(__name__)

#: The wiki frontmatter field consumers read at segment time. Written today by
#: a producer OUTSIDE athenaeum (see ``docs/deprecated-email-tracking.md``'s Q3
#: and maecenas#42); athenaeum reads it and never writes it (issue athenaeum#852).
WIKI_BOUNCED_FIELD = "bounced"

#: The frontmatter field carrying a page's durable id on BOTH surfaces — the
#: join key itself. On a wiki page it is the page's own identity; on a contacts
#: -surface person record it is the linkage back to the origin entity page,
#: written by the athenaeum#427/#437 migrator.
UID_FIELD = "uid"


def _uid_of(meta: dict[str, Any] | None) -> str | None:
    """The non-empty ``uid`` on *meta*, or ``None``."""
    if not isinstance(meta, dict):
        return None
    uid = str(meta.get(UID_FIELD, "") or "").strip()
    return uid or None


def read_page_meta(path: Path) -> dict[str, Any]:
    """Frontmatter of *path*, or ``{}`` when absent or unreadable.

    Unreadable is distinct from empty for a REPORT (see athenaeum#853), but for a
    single-page read a caller holding the path already knows the page exists —
    so this keeps :func:`athenaeum.pii.read_bounce_record`'s tolerant posture.
    """
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def wiki_bounced_value(meta: dict[str, Any] | None) -> str | None:
    """The page's ``bounced:`` value verbatim, or ``None`` when it carries none.

    Returned **opaque**: whatever the producer wrote, unparsed and
    unclassified. The value may be a list-verification verdict
    (``MailboxDoesNotExist``), a bare SMTP reply (``550 …``), an enhanced
    status code, a transient ``4.x`` observation, or a CRM marker — this
    module draws no conclusion from which, because doing so is exactly the
    promotion athenaeum#852 forbids. A present-but-empty field reads as ``None``
    (nothing recorded), never as a bounce.
    """
    if not isinstance(meta, dict):
        return None
    value = meta.get(WIKI_BOUNCED_FIELD)
    if value is None or isinstance(value, bool):
        # A bare `bounced: true` carries no evidence at all; treat the flag
        # itself as the value so a consumer can still see the field is set.
        return "true" if value is True else None
    text = str(value).strip()
    return text or None


def wiki_page_for_uid(wiki_root: Path, uid: str) -> Path | None:
    """The entity page carrying *uid*, or ``None``.

    Scans the same flat ``wiki/*.md`` entity-page set the rest of the codebase
    uses (:func:`athenaeum.storage_migrate.iter_entity_pages`) rather than
    building an :class:`~athenaeum.models.EntityIndex`, so a join costs one
    frontmatter read per page and pulls in no LLM-adjacent machinery. Returns
    the first match in sorted order; a duplicate ``uid`` across two pages is a
    corpus-integrity problem the dedupe path owns, not something to guess at
    here.
    """
    wanted = (uid or "").strip()
    if not wanted:
        return None
    for path in iter_entity_pages(wiki_root):
        if _uid_of(read_page_meta(path)) == wanted:
            return path
    return None


@dataclass(frozen=True)
class BounceJoin:
    """One identifier resolved along the full chain, however far it reaches.

    Every link is optional because a real store breaks the chain at every
    point: an address with no person record (nothing knows whose it is), a
    person record with no ``uid`` (an unmigrated or hand-authored record), a
    ``uid`` with no wiki page (the entity page was merged away or never
    existed). :attr:`reached` says how far it got, and each attribute is
    ``None`` from the first break onward.
    """

    identifier: str
    person_record: Path | None = None
    uid: str | None = None
    wiki_page: Path | None = None
    pii_marked: bool = False
    wiki_bounced: str | None = None

    @property
    def reached(self) -> str:
        """How far the chain got: ``wiki-page`` / ``uid`` / ``person-record`` / ``identifier``."""
        if self.wiki_page is not None:
            return "wiki-page"
        if self.uid is not None:
            return "uid"
        if self.person_record is not None:
            return "person-record"
        return "identifier"

    @property
    def joined(self) -> bool:
        """True when the chain reached a wiki page — the two surfaces are joinable."""
        return self.wiki_page is not None


def join_identifier(
    contacts_root: Path,
    wiki_root: Path,
    identifier: str,
    *,
    as_of: date | None = None,
) -> BounceJoin:
    """Walk the chain from an email identifier to the wiki page consumers hold.

    Follows ``identifier -> person record -> uid -> wiki page``, stopping at
    the first missing link and reporting how far it got rather than raising:
    a broken chain is the ordinary case on a real store, not an error.

    ``pii_marked`` is :func:`athenaeum.pii.is_bounced_identifier` on whichever
    record was resolved — the ADDRESS-level predicate, so a person record
    listing several addresses answers only for the one asked about.

    **Cost (issue athenaeum#883).** This deliberately keeps the UNINDEXED
    :func:`~athenaeum.pii.resolve_contact_record` call. That issue moved the
    callers that have a natural batch scope onto a shared
    :class:`~athenaeum.pii.ExcludedRecordIndex` — the librarian's compile loop
    builds one for its whole ``ctx.raw_files`` pass — but this function is a
    single-identifier entry point with no batch above it (it has no in-tree
    caller at all; it is the joined-chain question a consumer asks about ONE
    address). Building an index to answer one lookup is strictly slower than
    the scan it replaces, so the honest choice here is to keep the scan. A
    future batch-shaped caller should build the index at ITS loop level and
    resolve through :meth:`~athenaeum.pii.ExcludedRecordIndex.by_identifier`
    directly, exactly as the compile loop does.
    """
    person = resolve_contact_record(contacts_root, identifier)
    if person is None:
        return BounceJoin(identifier=identifier)

    meta = read_bounce_record(person)
    marked = is_bounced_identifier(meta, identifier, as_of)
    uid = _uid_of(meta)
    if uid is None:
        return BounceJoin(identifier=identifier, person_record=person, pii_marked=marked)

    page = wiki_page_for_uid(wiki_root, uid)
    if page is None:
        return BounceJoin(
            identifier=identifier, person_record=person, uid=uid, pii_marked=marked
        )

    return BounceJoin(
        identifier=identifier,
        person_record=person,
        uid=uid,
        wiki_page=page,
        pii_marked=marked,
        wiki_bounced=wiki_bounced_value(read_page_meta(page)),
    )


@dataclass(frozen=True)
class Deliverability:
    """What a consumer holding a wiki page knows about an address's deliverability.

    The two evidence classes are reported SEPARATELY and deliberately not
    collapsed into a single boolean (issue athenaeum#852):

    - :attr:`hard_bounced` is the pii mark, which exists only where a
      ``5.x.x`` permanent-failure code was observed;
    - :attr:`wiki_verdict` is whatever the wiki field says, verbatim and
      unclassified — a strictly broader union that includes transients and
      list-verification verdicts.

    A consumer that wants "do not send" may reasonably act on either. A
    consumer that wants "this address is permanently dead" may act only on
    :attr:`hard_bounced`. Collapsing the two here would make that distinction
    unavailable and would promote a transient to a hard bounce — so the
    decision is left where the evidence can still be told apart.
    """

    identifier: str
    hard_bounced: bool
    wiki_verdict: str | None
    person_record: Path | None = None
    wiki_page: Path | None = None

    @property
    def any_evidence(self) -> bool:
        """True when EITHER surface records something about this address.

        A convenience for a caller that only needs "is there anything here at
        all" — it says nothing about which kind of evidence, and is not a
        hard-bounce determination.
        """
        return self.hard_bounced or self.wiki_verdict is not None


def deliverability_for_page(
    page: Path,
    contacts_root: Path,
    *,
    as_of: date | None = None,
) -> list[Deliverability]:
    """The consumer entry point: what does THIS wiki page know about deliverability?

    Walks the chain backwards — page ``uid`` -> the person record carrying the
    same ``uid`` -> that record's addresses -> each address's pii mark — and
    pairs each address with the page's own ``bounced:`` value. This is the
    direction P6 dictates: the consumer holds a wiki page, and the pii mark
    reaches it here.

    Returns one :class:`Deliverability` per address the person record lists,
    in record order. A page with no ``uid``, or a ``uid`` no person record
    carries, returns ``[]`` — a consumer learns "nothing is recorded about
    this page's addresses", which is different from "its addresses are
    deliverable" only in that there is nothing to report either way.
    """
    page_meta = read_page_meta(page)
    verdict = wiki_bounced_value(page_meta)
    uid = _uid_of(page_meta)
    if uid is None:
        return []

    record = next(
        (
            path
            for path in iter_contact_records(contacts_root)
            if _uid_of(read_bounce_record(path)) == uid
        ),
        None,
    )
    if record is None:
        return []

    meta = read_bounce_record(record)
    return [
        Deliverability(
            identifier=identifier,
            hard_bounced=is_bounced_identifier(meta, identifier, as_of),
            wiki_verdict=verdict,
            person_record=record,
            wiki_page=page,
        )
        for identifier in identifiers_on_record(meta)
    ]


__all__ = [
    "WIKI_BOUNCED_FIELD",
    "UID_FIELD",
    "BounceJoin",
    "Deliverability",
    "read_page_meta",
    "wiki_bounced_value",
    "wiki_page_for_uid",
    "join_identifier",
    "deliverability_for_page",
]
