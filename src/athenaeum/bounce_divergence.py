# SPDX-License-Identifier: Apache-2.0
"""Bounce-mark divergence across BOTH surfaces (issue athenaeum#853).

A count of one surface cannot tell a healthy system from a broken one here,
and this feature has already been misread twice by exactly that shape of
check — once in each direction (see
``docs/deprecated-email-tracking.md``'s "How to verify a mark"). A metric that
reads only one surface inherits the same class of error. So this module
reports the **difference between the two surfaces**, computed over the join
key athenaeum#852 defines, rather than a count of either alone.

Three properties are deliberate, and each defends against a failure this
feature has actually had:

1. **Both surfaces, both directions.** Marked-but-not-on-the-wiki-surface AND
   on-the-wiki-surface-but-unmarked. A regression in the report path shows up
   as a moving number rather than as silence.
2. **"Empty" and "could not be read" never render identically.** Each surface
   carries a :class:`SurfaceStatus` — ``read`` / ``missing`` / ``unreadable``
   — alongside its count, and a partially-read surface reports how many paths
   it could not read. That conflation is the root of both prior false
   negatives; a report that cannot tell them apart is not a check.
3. **Output is safe to paste into a public issue.** Aggregate counts, and at
   most OPAQUE handles: a page ``uid``, or a truncated digest of an address
   (:func:`opaque_handle`). No address, no name, and no record path — a
   contacts-surface filename embeds a slugified address or person name, so
   paths are never emitted either. There is deliberately no ``--verbose`` mode
   that would emit identifying detail.

Every number is re-derived at run time from the store passed in. Nothing from
athenaeum#849 or athenaeum#853 is hard-coded as an expected value; the figures
quoted in those issues are as-of-2026-08-12 observations of one private store
(see ``docs/bounce-surface-convergence.md``), and this module neither asserts
nor reproduces them.

**Read-only.** Nothing here writes to either surface.

**Layering:** L4, alongside :mod:`athenaeum.bounce_join`, whose join key and
surface definitions it takes rather than re-deriving — two implementations of
the same join is exactly the drift this epic is cleaning up.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from athenaeum.bounce_join import (
    UID_FIELD,
    read_page_meta,
    wiki_bounced_value,
)
from athenaeum.pii import (
    identifier_validity_entries,
    identifiers_on_record,
    is_bounced_identifier,
    normalize_identifier,
    read_bounce_record,
)
from athenaeum.storage_migrate import iter_entity_pages

log = logging.getLogger(__name__)

#: How many hex characters of the identifier digest an opaque handle carries.
#: Long enough that a collision within one store is implausible, short enough
#: to read in a pasted report. The digest is one-way — it is a stable handle
#: for correlating two lines of the same report, never a recoverable address.
HANDLE_DIGEST_CHARS = 12


class SurfaceStatus(str, Enum):
    """Whether a surface was read at all — distinct from what was found on it.

    ``READ`` with ``count == 0`` means "this surface is empty"; ``MISSING`` and
    ``UNREADABLE`` mean "this report does not know what is on it". Conflating
    those is the root of both false negatives athenaeum#853 defends against, so
    they are separate states rather than a count of zero.
    """

    READ = "read"
    MISSING = "missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SurfaceScan:
    """What one surface reported: its status, its count, and what it could not read."""

    status: SurfaceStatus
    count: int = 0
    unreadable_paths: int = 0
    detail: str | None = None

    @property
    def reliable(self) -> bool:
        """True only when the surface was fully read — a count is meaningful."""
        return self.status is SurfaceStatus.READ and self.unreadable_paths == 0


@dataclass(frozen=True)
class DivergenceEntry:
    """One divergent item, identified OPAQUELY.

    ``handle`` is a page ``uid`` (already an opaque identifier) or a truncated
    digest of an address — never the address itself, never a name, never a
    path. ``kind`` says which, so a reader knows whether two entries carrying
    the same handle are the same page.
    """

    handle: str
    kind: str


def opaque_handle(identifier: str) -> str:
    """A stable, one-way handle for an email identifier.

    Case-normalized first (:func:`athenaeum.pii.normalize_identifier`) so the
    same address always yields the same handle regardless of how it was
    recorded. SHA-256 truncated to :data:`HANDLE_DIGEST_CHARS` — enough to
    correlate lines within one report, and not reversible to the address,
    which is what makes the output safe to paste into a public issue.
    """
    digest = hashlib.sha256(normalize_identifier(identifier).encode("utf-8")).hexdigest()
    return digest[:HANDLE_DIGEST_CHARS]


def record_has_bounce_mark(meta: dict[str, Any] | None) -> bool:
    """True when *meta* carries a bounce mark in EITHER shape the mark writes.

    Identified by the field the mark **actually writes** — a per-identifier
    close under ``identifier_validity:`` (athenaeum#850) or a top-level
    ``valid_until`` on a slug-keyed record (athenaeum#765) — and never by a
    ``bounced:`` key, which is never written on this surface. Asking for
    ``bounced:`` here is precisely the false negative this report exists to
    stop repeating.
    """
    if not isinstance(meta, dict):
        return False
    if identifier_validity_entries(meta):
        return True
    return bool(str(meta.get("valid_until", "") or "").strip())


def marked_identifiers(meta: dict[str, Any] | None, as_of: date | None = None) -> list[str]:
    """Every address on *meta* whose close has passed, in record order.

    Reads through :func:`athenaeum.pii.is_bounced_identifier`, so a person
    record answers per ADDRESS rather than as a whole, and a slug-keyed record
    answers only for its own identifier.
    """
    if not isinstance(meta, dict):
        return []
    candidates = list(identifiers_on_record(meta))
    own = str(meta.get("identifier", "") or "").strip()
    if own and not any(normalize_identifier(c) == normalize_identifier(own) for c in candidates):
        candidates.append(own)
    return [
        identifier for identifier in candidates if is_bounced_identifier(meta, identifier, as_of)
    ]


@dataclass(frozen=True)
class DivergenceReport:
    """The divergence between the two bounce surfaces, for one store.

    ``wiki`` and ``contacts`` carry each surface's status and count;
    the two lists are the set difference in both directions, computed over
    the ``uid`` join key. ``unjoinable_*`` count items that carry a bounce
    fact but cannot be joined at all (a wiki page with no ``uid``, a marked
    record with no ``uid``) — they are neither agreement nor divergence, and
    hiding them would overstate how much of the store the report actually
    compared.
    """

    wiki: SurfaceScan
    contacts: SurfaceScan
    marked_not_on_wiki: list[DivergenceEntry] = field(default_factory=list)
    on_wiki_not_marked: list[DivergenceEntry] = field(default_factory=list)
    marks: int = 0
    unjoinable_wiki_pages: int = 0
    unjoinable_marks: int = 0

    @property
    def complete(self) -> bool:
        """True when BOTH surfaces were fully read — the difference is trustworthy."""
        return self.wiki.reliable and self.contacts.reliable

    @property
    def diverged(self) -> bool:
        """True when either direction of the difference is non-empty."""
        return bool(self.marked_not_on_wiki or self.on_wiki_not_marked)

    @property
    def clean_zero(self) -> bool:
        """True when both surfaces were read and neither holds any bounce fact."""
        return self.complete and self.wiki.count == 0 and self.contacts.count == 0


def _scan_wiki(wiki_root: Path) -> tuple[SurfaceScan, dict[str, Path], int]:
    """Scan the wiki surface for pages carrying ``bounced:``.

    Returns the scan, ``{uid: page}`` for the pages that carry the field AND a
    ``uid``, and the count of those that carry the field but no ``uid`` (so
    cannot be joined).
    """
    root = Path(wiki_root)
    if not root.is_dir():
        return (
            SurfaceScan(
                status=SurfaceStatus.MISSING,
                detail=f"wiki surface is not a readable directory: {root.name}",
            ),
            {},
            0,
        )

    try:
        pages = list(iter_entity_pages(root))
    except OSError as exc:
        return (
            SurfaceScan(status=SurfaceStatus.UNREADABLE, detail=f"could not list pages: {exc}"),
            {},
            0,
        )

    by_uid: dict[str, Path] = {}
    unjoinable = 0
    unreadable = 0
    count = 0
    for page in pages:
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        meta = read_page_meta(page) if text else {}
        if wiki_bounced_value(meta) is None:
            continue
        count += 1
        uid = str(meta.get(UID_FIELD, "") or "").strip()
        if uid:
            by_uid.setdefault(uid, page)
        else:
            unjoinable += 1

    return (
        SurfaceScan(
            status=SurfaceStatus.READ,
            count=count,
            unreadable_paths=unreadable,
            detail=(
                f"{unreadable} page(s) could not be read; counts are a lower bound"
                if unreadable
                else None
            ),
        ),
        by_uid,
        unjoinable,
    )


def _scan_contacts(
    contacts_root: Path, as_of: date | None
) -> tuple[SurfaceScan, list[tuple[str, str | None]], int]:
    """Scan the contacts surface for records carrying a bounce mark.

    Returns the scan, one ``(identifier, uid)`` per marked address, and the
    count of marked addresses whose record carries no ``uid`` (unjoinable).
    """
    root = Path(contacts_root)
    if not root.is_dir():
        return (
            SurfaceScan(
                status=SurfaceStatus.MISSING,
                detail="contacts surface is not a readable directory "
                "(is `storage.mapping: {pii: excluded}` configured?)",
            ),
            [],
            0,
        )

    try:
        records = sorted(p for p in root.rglob("*.md") if p.is_file())
    except OSError as exc:
        return (
            SurfaceScan(
                status=SurfaceStatus.UNREADABLE, detail=f"could not list records: {exc}"
            ),
            [],
            0,
        )

    marks: list[tuple[str, str | None]] = []
    unjoinable = 0
    unreadable = 0
    count = 0
    for record in records:
        try:
            record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        meta = read_bounce_record(record)
        if not record_has_bounce_mark(meta):
            continue
        count += 1
        uid = str(meta.get(UID_FIELD, "") or "").strip() or None
        for identifier in marked_identifiers(meta, as_of):
            marks.append((identifier, uid))
            if uid is None:
                unjoinable += 1

    return (
        SurfaceScan(
            status=SurfaceStatus.READ,
            count=count,
            unreadable_paths=unreadable,
            detail=(
                f"{unreadable} record(s) could not be read; counts are a lower bound"
                if unreadable
                else None
            ),
        ),
        marks,
        unjoinable,
    )


def compute_divergence(
    wiki_root: Path,
    contacts_root: Path,
    *,
    as_of: date | None = None,
) -> DivergenceReport:
    """Compare both bounce surfaces for one store. Read-only; never raises on a bad store.

    A missing or unreadable surface yields a report that SAYS SO rather than a
    zero — the whole point of athenaeum#853. A store where both surfaces are
    readable and hold nothing yields a clean zero report, not an error.
    """
    wiki_scan, wiki_by_uid, unjoinable_pages = _scan_wiki(wiki_root)
    contacts_scan, marks, unjoinable_marks = _scan_contacts(contacts_root, as_of)

    marked_uids = {uid for _, uid in marks if uid}
    marked_not_on_wiki = [
        DivergenceEntry(handle=opaque_handle(identifier), kind="digest")
        for identifier, uid in marks
        if uid is not None and uid not in wiki_by_uid
    ]
    on_wiki_not_marked = [
        DivergenceEntry(handle=uid, kind="uid")
        for uid in sorted(wiki_by_uid)
        if uid not in marked_uids
    ]

    return DivergenceReport(
        wiki=wiki_scan,
        contacts=contacts_scan,
        marked_not_on_wiki=marked_not_on_wiki,
        on_wiki_not_marked=on_wiki_not_marked,
        marks=len(marks),
        unjoinable_wiki_pages=unjoinable_pages,
        unjoinable_marks=unjoinable_marks,
    )


def render_report(report: DivergenceReport) -> str:
    """Render *report* as plain text that is safe to paste into a public issue.

    Aggregate counts and opaque handles only. Each surface's line states its
    status explicitly, so "empty" and "could not be read" can never be
    mistaken for each other by a reader skimming for a number.
    """
    lines: list[str] = ["bounce-mark divergence across both surfaces", ""]

    surfaces = (
        ("wiki `bounced:` pages", report.wiki),
        ("contacts-surface records", report.contacts),
    )
    for label, scan in surfaces:
        if scan.status is SurfaceStatus.READ:
            lines.append(f"  {label}: {scan.count}")
        else:
            lines.append(f"  {label}: NOT READ ({scan.status.value})")
        if scan.detail:
            lines.append(f"      {scan.detail}")

    lines.append(f"  bounce marks (addresses): {report.marks}")
    lines.append("")

    if not report.complete:
        lines.append(
            "  INCOMPLETE — at least one surface could not be read. The "
            "difference below is NOT a divergence measurement; it is what "
            "was visible."
        )
        lines.append("")

    lines.append(f"  marked, not on the wiki surface: {len(report.marked_not_on_wiki)}")
    for entry in report.marked_not_on_wiki:
        lines.append(f"      {entry.kind}:{entry.handle}")
    lines.append(f"  on the wiki surface, unmarked: {len(report.on_wiki_not_marked)}")
    for entry in report.on_wiki_not_marked:
        lines.append(f"      {entry.kind}:{entry.handle}")

    if report.unjoinable_wiki_pages or report.unjoinable_marks:
        lines.append("")
        lines.append(
            f"  not comparable (no join key): {report.unjoinable_wiki_pages} wiki "
            f"page(s) with no uid, {report.unjoinable_marks} mark(s) on a record "
            "with no uid"
        )

    if report.clean_zero:
        lines.append("")
        lines.append("  Both surfaces read; neither holds a bounce fact.")

    return "\n".join(lines) + "\n"


def report_as_dict(report: DivergenceReport) -> dict[str, Any]:
    """Machine-readable form of *report*, carrying the same opaque handles."""
    return {
        "complete": report.complete,
        "diverged": report.diverged,
        "wiki": {
            "status": report.wiki.status.value,
            "count": report.wiki.count,
            "unreadable_paths": report.wiki.unreadable_paths,
            "detail": report.wiki.detail,
        },
        "contacts": {
            "status": report.contacts.status.value,
            "count": report.contacts.count,
            "unreadable_paths": report.contacts.unreadable_paths,
            "detail": report.contacts.detail,
        },
        "marks": report.marks,
        "marked_not_on_wiki": [
            {"handle": e.handle, "kind": e.kind} for e in report.marked_not_on_wiki
        ],
        "on_wiki_not_marked": [
            {"handle": e.handle, "kind": e.kind} for e in report.on_wiki_not_marked
        ],
        "unjoinable_wiki_pages": report.unjoinable_wiki_pages,
        "unjoinable_marks": report.unjoinable_marks,
    }


__all__ = [
    "HANDLE_DIGEST_CHARS",
    "SurfaceStatus",
    "SurfaceScan",
    "DivergenceEntry",
    "DivergenceReport",
    "opaque_handle",
    "record_has_bounce_mark",
    "marked_identifiers",
    "compute_divergence",
    "render_report",
    "report_as_dict",
]
