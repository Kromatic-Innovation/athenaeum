# SPDX-License-Identifier: Apache-2.0
"""Schema-amendment proposal ledger (issue athenaeum#2197, north-star §2.7/§2.8).

A deployment's page types and page structure are configuration, and the
librarian works within them (``docs/north-star.md`` §2.7). When
:mod:`athenaeum.structured_upsert` refuses a raw because its value for a
declared ``librarian.field_groups.<type>`` field collides with a different,
already-held value (``REASON_FIELD_GROUP_MISFIT``), that misfit is not
silently dropped -- it raises, or grows, ONE schema-amendment proposal in
the pending-decisions queue, naming the page type, the field group that
does not fit, a COUNT of affected pages, and a proposed amendment.

**Deduplicated by shape, not by occurrence.** The proposal's id is derived
from ``(page_type, field_group)`` alone (:func:`proposal_item_id`) -- every
misfit of the same shape, on any page, from any run, grows the SAME open
proposal's ``affected_uids`` set rather than minting a new one. This is the
one deliberate difference from this codebase's other proposal ledgers
(:mod:`athenaeum.tier_movement_proposals`, :mod:`athenaeum.dimension_proposals`):
those dedup by suppressing an exact-repeat re-draft; this one dedups by
*updating* the open proposal's record, because the issue's AC is an
observable, growing count an operator reads before deciding, not a silent
idempotency guard.

**Reuses the ``schema-amendment`` conflict-type taxonomy** that
:mod:`athenaeum.corrections` (the ``held-schema-proposal`` disposition, for
an attribute with no configured schema slot at all -- a different scenario,
see ``docs/design/field-corrections.md`` §7.2) already raises through
``athenaeum.librarian.EscalationItem``. This module does not touch that
path; it is a sibling rail for a different trigger (a slot that EXISTS but
is already held by a conflicting value), surfaced the same way: visible in
``list_pending_decisions``, never auto-adopted.

**Human adoption only.** This module never writes `athenaeum.yaml` or any
other config, never migrates a page, and defines no "adopt"/"apply" verb --
exactly :mod:`athenaeum.dimension_proposals`'s "visible, framed, but not yet
answerable" posture (an adopted amendment -- e.g. actually introducing a
``deal`` entity type -- is each its own follow-up, per the issue's own
"Out of scope"). An unresolved proposal changes nothing; see
``tests/test_schema_amendment_proposals.py``.

**Persistence**: one ledger, ``wiki/_schema_amendment_proposals.jsonl``, with
``proposal`` records only -- no ``approve``/``reject`` kind exists because
nothing in this module resolves a proposal; it is read verbatim by
:func:`athenaeum.decisions.schema_amendment_proposal_to_decision`.

Layering: L4 domain/pipeline module, a peer of
:mod:`athenaeum.tier_movement_proposals`. Imports :mod:`athenaeum.store`
(L3, for the shared durable-append/now_iso primitives) and, for typing only,
:mod:`athenaeum.structured_upsert` (L2); imported by the L4 librarian and
batch (to record a misfit) and by :mod:`athenaeum.decisions` (to list one).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from athenaeum.store import append_line_durable, now_iso

if TYPE_CHECKING:
    from athenaeum.structured_upsert import FieldGroupMisfit

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
SCHEMA_AMENDMENT_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_tier_movement_proposals.jsonl``.
SCHEMA_AMENDMENT_PROPOSALS_LEDGER_FILENAME = "_schema_amendment_proposals.jsonl"

#: The only record kind this module ever writes -- see module docstring's
#: "Human adoption only": there is no approve/reject verb here.
PROPOSAL_KIND = "proposal"


def default_schema_amendment_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_schema_amendment_proposals.jsonl``."""
    return Path(wiki_root) / SCHEMA_AMENDMENT_PROPOSALS_LEDGER_FILENAME


def proposal_item_id(page_type: str, field_group: str) -> str:
    """Deterministic id for one ``(page_type, field_group)`` SHAPE.

    Deliberately NOT value- or page-specific, and NOT per-event: every
    misfit of the same shape -- any page, any field pair, any run -- maps to
    the SAME id, which is exactly the "repeated misfits of the same shape
    add to the existing proposal" AC (issue athenaeum#2197).
    """
    payload = json.dumps(
        {"page_type": page_type, "field_group": field_group}, sort_keys=True
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_schema_amendment_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line."""
    target = (
        ledger_path
        if ledger_path is not None
        else default_schema_amendment_proposals_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
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
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _latest_proposal_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The most recently appended ``proposal`` record per id.

    The ledger is append-only: a repeat misfit of the same shape appends a
    FRESH record carrying the same ``id`` and a widened ``affected_uids``,
    rather than rewriting the earlier line in place. Reading the ledger
    therefore means folding to the latest record per id, not every record.
    """
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("kind") != PROPOSAL_KIND:
            continue
        rid = str(rec.get("id", ""))
        if not rid:
            continue
        latest[rid] = rec
    return latest


def list_pending_schema_amendment_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Every open schema-amendment proposal, latest state, oldest id-order first.

    "Pending" always -- this module writes no resolution kind (see module
    docstring), so every proposal the ledger has ever recorded is open.
    """
    records = read_schema_amendment_proposals_ledger(wiki_root, ledger_path=ledger_path)
    latest = _latest_proposal_by_id(records)
    # Stable order: first-appearance order of each id in the raw ledger.
    order: list[str] = []
    seen: set[str] = set()
    for rec in records:
        rid = str(rec.get("id", ""))
        if rid in latest and rid not in seen:
            seen.add(rid)
            order.append(rid)
    return [latest[rid] for rid in order]


def default_schema_amendment_text(page_type: str, field_group: str) -> str:
    """The default proposed-amendment text for one ``(page_type, field_group)``
    shape -- a REVERSIBLE default (issue athenaeum#2197's lane report): names
    the shape precisely but proposes no code, no migration, no entity-type
    implementation; a human reads and refines this before adopting anything.
    """
    return (
        f"Add a dedicated entity type for the {field_group!r} field group on "
        f"{page_type!r} pages (for example, a `deal` entity type that a "
        f"`{page_type}` page relates to), so each distinct record gets its "
        f"own page instead of competing for one page's fields."
    )


def record_schema_misfit(
    wiki_root: Path,
    *,
    page_type: str,
    field_group: str,
    uid: str,
    proposed_amendment: str,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one misfit occurrence for *uid*, creating or growing the ONE
    open proposal for ``(page_type, field_group)`` -- issue athenaeum#2197 AC
    "repeated misfits of the same shape add to the existing proposal instead
    of creating new ones".

    Idempotent per ``(shape, uid)``: re-recording the SAME page's misfit
    (for example, two different fields in the same group colliding on one
    raw) does not inflate ``affected_page_count`` -- that count is
    ``len(affected_uids)``, a count of pages, never an event tally. Returns
    the up-to-date record (post-write).
    """
    target = (
        ledger_path
        if ledger_path is not None
        else default_schema_amendment_proposals_ledger_path(wiki_root)
    )
    records = read_schema_amendment_proposals_ledger(wiki_root, ledger_path=target)
    item_id = proposal_item_id(page_type, field_group)
    current = _latest_proposal_by_id(records).get(item_id)

    affected_uids = set(current.get("affected_uids", [])) if current else set()
    affected_uids.add(uid)

    record = {
        "v": SCHEMA_AMENDMENT_PROPOSALS_LEDGER_VERSION,
        "kind": PROPOSAL_KIND,
        "id": item_id,
        "created_at": (current or {}).get("created_at") or now_iso(now),
        "updated_at": now_iso(now),
        "page_type": page_type,
        "field_group": field_group,
        "affected_uids": sorted(affected_uids),
        "affected_page_count": len(affected_uids),
        "proposed_amendment": proposed_amendment,
    }
    _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
    log.info(
        "schema-amendment proposal %s: page_type=%r field_group=%r now "
        "affects %d page(s)",
        item_id,
        page_type,
        field_group,
        record["affected_page_count"],
    )
    return record


def record_field_group_misfits(
    misfits: "Iterable[FieldGroupMisfit]",
    *,
    wiki_root: Path,
    uid: str,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Record every misfit in *misfits* for one page (*uid*) in one call.

    The thin end-to-end seam :mod:`athenaeum.librarian` and
    :mod:`athenaeum.batch` call after a
    :func:`athenaeum.structured_upsert.tier0_structured_upsert` refusal whose
    ``reason`` is ``REASON_FIELD_GROUP_MISFIT`` (its ``field_group_misfits``
    is *misfits*). Deduplicates by shape WITHIN this call so a raw whose
    misfit spans two fields of the SAME group (for example both
    ``crm_deal_name`` and ``crm_deal_amount``) only records that page once
    for that group -- :func:`record_schema_misfit` is already idempotent per
    ``(shape, uid)`` across calls; this just avoids two redundant ledger
    appends for the one call.
    """
    seen_shapes: set[tuple[str, str]] = set()
    records: list[dict[str, Any]] = []
    for misfit in misfits:
        shape = (misfit.page_type, misfit.field_group)
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        records.append(
            record_schema_misfit(
                wiki_root,
                page_type=misfit.page_type,
                field_group=misfit.field_group,
                uid=uid,
                proposed_amendment=default_schema_amendment_text(
                    misfit.page_type, misfit.field_group
                ),
                ledger_path=ledger_path,
                now=now,
            )
        )
    return records


__all__ = [
    "SCHEMA_AMENDMENT_PROPOSALS_LEDGER_VERSION",
    "SCHEMA_AMENDMENT_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "default_schema_amendment_proposals_ledger_path",
    "proposal_item_id",
    "read_schema_amendment_proposals_ledger",
    "list_pending_schema_amendment_proposals",
    "default_schema_amendment_text",
    "record_schema_misfit",
    "record_field_group_misfits",
]
