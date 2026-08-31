# SPDX-License-Identifier: Apache-2.0
"""Write-boundary type guard for wiki entity pages (issue athenaeum#1196).

athenaeum#1196's audit found the intended clamp chain (``intake.tier0_passthrough``'s
``valid_types`` eligibility gate, ``tiers.parse_tier2_entities``'s
``entity_type not in valid_types -> "reference"`` fold, and every downstream
tier-3 write that consumes an already-clamped ``entity_type``) working
correctly on every path traced, plus one undeclared type (``type: issue``)
live in the corpus whose origin could not be pinned to a bypass in any of
them. ``schemas.validate_wiki_meta`` already flags an unrecognized type —
but only via a :class:`UserWarning`, which nothing in the write path reads
or acts on. This module is the backstop: a guard that sits at the actual
disk-write call site for a NEW entity page (not a mid-pipeline clamp that a
future path could route around) and REFUSES the write outright when the
type is not admitted, rather than trusting that every upstream clamp was
exercised correctly.

Admitted set: ``declared_entity_classes(wiki_root) | KNOWN_TYPES`` —
deliberately BROADER than the ``valid_types`` list the tier0/tier2 clamps
enforce (which is ``types.md``'s declared rows, falling back to
``KNOWN_TYPES`` only when ``types.md`` is absent/empty — see
``athenaeum.entity_schema.declared_entity_classes``). The tier clamps stay
narrow on purpose, so entity intake cannot mint a page of a reserved,
non-entity-intake type (``auto-memory``, written exclusively by the
auto-memory compiler — see ``wiki/_schema/types.md``'s "Reserved
(non-entity-intake) types" section and this issue's own note on why that
must not change). This guard's job is different: it is the LAST line, after
every upstream decision has already been made, so it must not reject a
legitimately-already-known type just because a NEW-page clamp would have
excluded it — it only rejects a type nothing in the corpus recognizes at
all.

Refuse-and-surface, never destroy: a rejected write is never applied to
``wiki_root`` — the caller must not create the page — and the write is
NEVER an update to an already-existing page (this module can only fire from
a NEW-entity write path; the tier-3 merge path never sets ``type`` at all,
see ``tiers.stamp_merge_provenance``). The rendered content that would have
been written is instead parked at
``<wiki_root>/_type_rejected/<filename>`` (a brand-new file — nothing pre
-existing is ever overwritten there) and a record is appended to
``<wiki_root>/_type_rejected.jsonl`` so an operator can find and disposition
it later. Both live under a leading-underscore name, so they are invisible
to every existing shallow, underscore-excluding wiki scan
(``models.EntityIndex._load``, ``entity_schema.resolve_entity_classes``,
``librarian.rebuild_index``) without this module having to touch that
exclusion convention itself.

Layering: L2 leaf. Imports only :mod:`athenaeum.entity_schema` (declared
types), :mod:`athenaeum.schemas` (``KNOWN_TYPES``), :mod:`athenaeum.store`
(the shared durable JSONL-append primitive), and :mod:`athenaeum.atomic_io`
— none of which import back into :mod:`athenaeum.intake`,
:mod:`athenaeum.tiers`, :mod:`athenaeum.librarian`, or :mod:`athenaeum.batch`,
so this module can be imported at top level from any of those without
reintroducing the librarian-centered import cycle athenaeum#545/athenaeum#640 dissolved
(see ``tests/test_import_graph_acyclic.py``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.entity_schema import declared_entity_classes
from athenaeum.schemas import KNOWN_TYPES
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)

#: Sidecar directory (under ``wiki_root``) a rejected write's rendered
#: content is parked in, mirroring ``athenaeum.quarantine``'s
#: ``<wiki_root>/_quarantine/`` shape but kept as its OWN, differently-named
#: directory — this guards a different object (a page that was never
#: written) from that module's (a raw intake file moved off disk), and
#: reusing the same directory would risk a future reader conflating the two
#: ledgers' record shapes.
TYPE_REJECTED_DIR_NAME = "_type_rejected"

#: Durable, append-only audit ledger of every rejected write this guard has
#: ever refused, one JSON record per line (schema mirrors
#: ``athenaeum.quarantine``'s ledger convention: tolerant reader, torn
#: trailing line survivable).
TYPE_REJECTED_LEDGER_NAME = "_type_rejected.jsonl"


def resolve_admitted_wiki_types(wiki_root: Path) -> frozenset[str]:
    """The write-boundary admitted ``type`` set for *wiki_root*.

    ``declared_entity_classes(wiki_root) | KNOWN_TYPES`` — see this module's
    docstring for why the union (not either set alone) is the correct
    boundary-guard admission rule.
    """
    return declared_entity_classes(wiki_root) | KNOWN_TYPES


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def guard_entity_write_type(
    wiki_root: Path,
    filename: str,
    rendered: str,
    meta: dict[str, Any],
    *,
    source: str = "unknown",
) -> bool:
    """Admit or refuse one NEW entity page write, by its ``type``.

    Returns ``True`` when ``meta["type"]`` is in
    :func:`resolve_admitted_wiki_types` for *wiki_root* — the caller should
    proceed with its normal ``atomic_write_text`` to ``wiki_root``.

    Returns ``False`` when it is not: the write must be REFUSED. This
    function itself parks *rendered* byte-for-byte at
    ``<wiki_root>/_type_rejected/<filename>`` and appends an audit record to
    ``<wiki_root>/_type_rejected.jsonl`` before returning — the caller's only
    remaining job is to skip the normal ``atomic_write_text`` call and count
    the rejection (e.g. in its own run-summary accumulator). *source* is a
    free-text tag (e.g. ``"tier3-create"`` / ``"batch"``) recorded on the
    ledger entry for traceability; it never affects the admission decision.

    Never touches an existing page: this guard only runs at a NEW-entity
    write call site, and ``filename`` under ``_type_rejected/`` is a fresh
    path each call (a repeated rejection of the same uid/name simply
    overwrites its own prior parked copy — nothing else can collide with
    it), so there is no data-loss risk here even on a repeated run.
    """
    etype = str(meta.get("type", "") or "").strip()
    admitted = resolve_admitted_wiki_types(wiki_root)
    if etype in admitted:
        return True

    rejected_dir = wiki_root / TYPE_REJECTED_DIR_NAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(rejected_dir / filename, rendered)

    record = {
        "ts": _now_iso(),
        "filename": filename,
        "type": etype,
        "uid": str(meta.get("uid", "") or ""),
        "name": str(meta.get("name", "") or ""),
        "source": source,
    }
    append_line_durable(
        wiki_root / TYPE_REJECTED_LEDGER_NAME,
        (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    log.warning(
        "wiki write REJECTED (issue athenaeum#1196): type=%r not in declared "
        "∪ KNOWN_TYPES for %s -- parked at %s/%s, recorded in %s",
        etype,
        wiki_root,
        TYPE_REJECTED_DIR_NAME,
        filename,
        TYPE_REJECTED_LEDGER_NAME,
    )
    return False


def list_type_rejected(wiki_root: Path) -> list[dict[str, Any]]:
    """Read every well-formed record from the type-rejected ledger.

    Returns ``[]`` when the ledger does not exist. Tolerates a torn trailing
    line (a crash mid-append) by skipping it — the same tolerant-reader
    convention every other ledger in this codebase follows (see e.g.
    ``athenaeum.calibration.read_calibration_ledger``).
    """
    path = wiki_root / TYPE_REJECTED_LEDGER_NAME
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


__all__ = [
    "TYPE_REJECTED_DIR_NAME",
    "TYPE_REJECTED_LEDGER_NAME",
    "resolve_admitted_wiki_types",
    "guard_entity_write_type",
    "list_type_rejected",
]
