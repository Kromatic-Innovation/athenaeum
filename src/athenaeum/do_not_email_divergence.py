# SPDX-License-Identifier: Apache-2.0
"""``do_not_email`` divergence across both surfaces (issue athenaeum#960).

The anti-recurrence half of athenaeum#960. The reader fix
(:func:`athenaeum.pii.do_not_email_state`) makes the wiki page authoritative
and the excluded record a fallback — it does not, and must not, backfill or
merge the two. This module answers the DIFFERENT question of whether the two
surfaces still hold the same set of marked entities, so a drift can be caught
rather than silently masked by the reader's own precedence.

Modelled on :mod:`athenaeum.bounce_divergence` (issue athenaeum#853), reusing
its :class:`~athenaeum.bounce_divergence.SurfaceStatus`,
:class:`~athenaeum.bounce_divergence.SurfaceScan` and
:class:`~athenaeum.bounce_divergence.DivergenceEntry` — the same
empty-vs-unreadable distinction and opaque-handle shape apply unchanged,
and importing rather than re-deriving them keeps there from being two
definitions of "a surface could not be read".

**Why this field's residual is zero, unlike bounce's — but only in ONE
direction (issue athenaeum#1039).** `docs/bounce-surface-convergence.md`
documents that a bounce-surface difference is often expected (a
list-verification verdict is not a hard bounce, so it correctly never mints
a wiki page tag). `do_not_email` carries no such evidence-class asymmetry
for the direction the design actually forbids: the wiki page is the sole
authoring surface (athenaeum#960's Out-of-scope rejects any backfill onto
the excluded surface), so ``marked_on_wiki_not_excluded`` is the design's
ONLY legal steady state, not a residual to tolerate away — it is simply not
a divergence. The tolerated residual is exactly zero only for
``marked_on_excluded_not_wiki``, the excluded surface newly carrying the
field. That is why, unlike ``bounce-divergence``, this module's CLI command
(:mod:`athenaeum._cmd_do_not_email_divergence`) exits non-zero on that ONE
direction of divergence, not only on an unreadable surface — the mistake
athenaeum#853 shipped and athenaeum#960's issue names explicitly (a check
that only ever exits 0 or 2 is silent about the number moving). Before
athenaeum#1039, the CLI command (and the equivalent `surface-divergence
--field do_not_email` predicate) alerted on EITHER direction, which meant
alerting on the design's only legal state.

**Both surfaces are keyed by ``uid``, not by address.** Unlike bounce (which
marks individual email ADDRESSES), ``do_not_email`` is a per-RECORD /
per-PAGE fact on both surfaces — so the join here is a plain uid-set
difference, with no address-level complexity and no opaque digest handles;
a wiki page's own ``uid`` is already an opaque identifier.

Marked-ness on each surface is read through
:func:`athenaeum.pii.do_not_email_state` — the SAME function the reader
uses — rather than re-deriving the coercion rules here, so this check and
the reader can never drift on what counts as "marked".

**Read-only.** Nothing here writes to either surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.bounce_divergence import DivergenceEntry, SurfaceScan, SurfaceStatus
from athenaeum.bounce_join import read_page_meta
from athenaeum.pii import do_not_email_state, iter_contact_records, read_bounce_record
from athenaeum.storage_migrate import iter_entity_pages


def _scan_wiki_for_do_not_email(wiki_root: Path) -> tuple[SurfaceScan, set[str]]:
    """Scan the wiki surface for pages carrying a ``do_not_email`` mark.

    Returns the scan and the set of marked pages' ``uid``s. A page with the
    field present but coerced to "not marked" (``do_not_email: false``, an
    explicit falsey string) is NOT counted — this mirrors
    :func:`athenaeum.pii.do_not_email_state`'s own fail-closed-but-not-
    fail-loud reading exactly, so the divergence check and the reader agree
    on what "marked" means. A marked page with no ``uid`` cannot be joined
    and is reported separately rather than silently dropped.
    """
    root = Path(wiki_root)
    if not root.is_dir():
        return (
            SurfaceScan(
                status=SurfaceStatus.MISSING,
                detail=f"wiki surface is not a readable directory: {root.name}",
            ),
            set(),
        )

    try:
        pages = list(iter_entity_pages(root))
    except OSError as exc:
        return (
            SurfaceScan(status=SurfaceStatus.UNREADABLE, detail=f"could not list pages: {exc}"),
            set(),
        )

    marked_uids: set[str] = set()
    unreadable = 0
    unjoinable = 0
    count = 0
    for page in pages:
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        meta = read_page_meta(page) if text else {}
        state = do_not_email_state(None, meta)
        if not state.marked:
            continue
        count += 1
        uid = str(meta.get("uid", "") or "").strip()
        if uid:
            marked_uids.add(uid)
        else:
            unjoinable += 1

    return (
        SurfaceScan(
            status=SurfaceStatus.READ,
            count=count,
            unreadable_paths=unreadable,
            detail=(
                (
                    f"{unreadable} page(s) could not be read; counts are a lower bound"
                    if unreadable
                    else None
                )
                if unjoinable == 0
                else (
                    f"{unjoinable} marked page(s) with no uid, not comparable"
                    + (f"; {unreadable} page(s) could not be read" if unreadable else "")
                )
            ),
        ),
        marked_uids,
    )


def _scan_excluded_for_do_not_email(contacts_root: Path) -> tuple[SurfaceScan, set[str]]:
    """Scan the excluded surface for records carrying a ``do_not_email`` mark.

    Returns the scan and the set of marked records' ``uid``s, on the same
    "empty is not unreadable" contract as :func:`_scan_wiki_for_do_not_email`.
    """
    root = Path(contacts_root)
    if not root.is_dir():
        return (
            SurfaceScan(
                status=SurfaceStatus.MISSING,
                detail="excluded surface is not a readable directory "
                "(is `storage.mapping: {pii: excluded}` configured?)",
            ),
            set(),
        )

    try:
        records = iter_contact_records(root)
    except OSError as exc:
        return (
            SurfaceScan(
                status=SurfaceStatus.UNREADABLE, detail=f"could not list records: {exc}"
            ),
            set(),
        )

    marked_uids: set[str] = set()
    unreadable = 0
    unjoinable = 0
    count = 0
    for record in records:
        try:
            record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        meta = read_bounce_record(record)
        state = do_not_email_state(meta, None)
        if not state.marked:
            continue
        count += 1
        uid = str(meta.get("uid", "") or "").strip()
        if uid:
            marked_uids.add(uid)
        else:
            unjoinable += 1

    return (
        SurfaceScan(
            status=SurfaceStatus.READ,
            count=count,
            unreadable_paths=unreadable,
            detail=(
                (
                    f"{unreadable} record(s) could not be read; counts are a lower bound"
                    if unreadable
                    else None
                )
                if unjoinable == 0
                else (
                    f"{unjoinable} marked record(s) with no uid, not comparable"
                    + (f"; {unreadable} record(s) could not be read" if unreadable else "")
                )
            ),
        ),
        marked_uids,
    )


@dataclass(frozen=True)
class DoNotEmailDivergenceReport:
    """The divergence between the two ``do_not_email`` surfaces, for one store.

    ``wiki`` and ``excluded`` carry each surface's status and count; the two
    lists are the uid-set symmetric difference. Both surfaces are keyed by
    ``uid`` directly — there is no address-level join here, unlike bounce.
    """

    wiki: SurfaceScan
    excluded: SurfaceScan
    marked_on_wiki_not_excluded: list[DivergenceEntry] = field(default_factory=list)
    marked_on_excluded_not_wiki: list[DivergenceEntry] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when BOTH surfaces were fully read — the difference is trustworthy."""
        return self.wiki.reliable and self.excluded.reliable

    @property
    def diverged(self) -> bool:
        """True when either direction of the difference is non-empty.

        Purely descriptive of the two surfaces' set difference — it does
        NOT by itself say whether that difference is a defect. As of issue
        athenaeum#1039, ``marked_on_wiki_not_excluded`` alone is the
        design's legal steady state (athenaeum#960 forbids backfill onto
        the excluded surface), so this property is ``True`` in that case
        without it being anything to alert on. The CLI commands' exit-code
        decisions key off ``marked_on_excluded_not_wiki`` specifically, not
        this property — see :mod:`athenaeum._cmd_do_not_email_divergence`
        and :mod:`athenaeum.surface_divergence`.
        """
        return bool(self.marked_on_wiki_not_excluded or self.marked_on_excluded_not_wiki)

    @property
    def clean_zero(self) -> bool:
        """True when both surfaces were read and neither holds any mark."""
        return self.complete and self.wiki.count == 0 and self.excluded.count == 0


def compute_do_not_email_divergence(
    wiki_root: Path, contacts_root: Path
) -> DoNotEmailDivergenceReport:
    """Compare both ``do_not_email`` surfaces for one store. Read-only; never raises.

    A missing or unreadable surface yields a report that SAYS SO rather than
    a zero. A store where both surfaces are readable and hold nothing yields
    a clean zero report, not an error.
    """
    wiki_scan, wiki_uids = _scan_wiki_for_do_not_email(wiki_root)
    excluded_scan, excluded_uids = _scan_excluded_for_do_not_email(contacts_root)

    return DoNotEmailDivergenceReport(
        wiki=wiki_scan,
        excluded=excluded_scan,
        marked_on_wiki_not_excluded=[
            DivergenceEntry(handle=uid, kind="uid")
            for uid in sorted(wiki_uids - excluded_uids)
        ],
        marked_on_excluded_not_wiki=[
            DivergenceEntry(handle=uid, kind="uid")
            for uid in sorted(excluded_uids - wiki_uids)
        ],
    )


def render_report(report: DoNotEmailDivergenceReport) -> str:
    """Render *report* as plain text that is safe to paste into a public issue.

    A page ``uid`` is already an opaque identifier, so the handles below carry
    no address or name — same public-safety posture as
    :func:`athenaeum.bounce_divergence.render_report`.
    """
    lines: list[str] = ["do_not_email divergence across both surfaces", ""]

    surfaces = (
        ("wiki `do_not_email:` pages", report.wiki),
        ("excluded-surface records", report.excluded),
    )
    for label, scan in surfaces:
        if scan.status is SurfaceStatus.READ:
            lines.append(f"  {label}: {scan.count}")
        else:
            lines.append(f"  {label}: NOT READ ({scan.status.value})")
        if scan.detail:
            lines.append(f"      {scan.detail}")
    lines.append("")

    if not report.complete:
        lines.append(
            "  INCOMPLETE — at least one surface could not be read. The "
            "difference below is NOT a divergence measurement; it is what "
            "was visible."
        )
        lines.append("")

    lines.append(f"  marked on wiki, not on excluded: {len(report.marked_on_wiki_not_excluded)}")
    for entry in report.marked_on_wiki_not_excluded:
        lines.append(f"      {entry.kind}:{entry.handle}")
    lines.append(f"  marked on excluded, not on wiki: {len(report.marked_on_excluded_not_wiki)}")
    for entry in report.marked_on_excluded_not_wiki:
        lines.append(f"      {entry.kind}:{entry.handle}")

    if report.clean_zero:
        lines.append("")
        lines.append("  Both surfaces read; neither holds a do_not_email mark.")
    elif report.complete and not report.diverged:
        lines.append("")
        lines.append("  Both surfaces read and agree — no divergence.")

    return "\n".join(lines) + "\n"


def report_as_dict(report: DoNotEmailDivergenceReport) -> dict[str, Any]:
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
        "excluded": {
            "status": report.excluded.status.value,
            "count": report.excluded.count,
            "unreadable_paths": report.excluded.unreadable_paths,
            "detail": report.excluded.detail,
        },
        "marked_on_wiki_not_excluded": [
            {"handle": e.handle, "kind": e.kind} for e in report.marked_on_wiki_not_excluded
        ],
        "marked_on_excluded_not_wiki": [
            {"handle": e.handle, "kind": e.kind} for e in report.marked_on_excluded_not_wiki
        ],
    }


__all__ = [
    "DoNotEmailDivergenceReport",
    "compute_do_not_email_divergence",
    "render_report",
    "report_as_dict",
]
