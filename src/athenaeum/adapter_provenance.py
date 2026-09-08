# SPDX-License-Identifier: Apache-2.0
"""Source-object <-> compiled-page provenance ledger (issue athenaeum#1462).

**The gap this closes.** A Lane A adapter (see
``docs/extending/adapter-contract.md``) writes rich, source-specific
frontmatter onto its raw-intake records -- for ``mural-board-summary``, a
board id, room, workspace, created/updated timestamps, a fragment count, an
``archive_path``, and a ``template_only`` flag. Tier 2/3 compile that raw
record into a wiki page via :class:`athenaeum.models.ClassifiedEntity` /
:class:`athenaeum.models.EntityAction`, both of which carry a FIXED field set
(``name``/``entity_type``/``tags``/``access``/``observations``), and
:func:`athenaeum.tiers.tier3_create` hands the model only those fields plus a
free-text observation blob. None of the adapter's extra frontmatter ever
reaches the model or the finished :class:`athenaeum.models.WikiEntity`, so a
compiled page cannot be joined back to the source object it came from --
measured over the live corpus at 16 of 1,774 pages retaining even the board
id (`grep -rl`, issue filing). Raw records are unlinked once consumed, so
after that the join is gone for good.

**Decision (AC1): mechanism (b), a provenance ledger -- NOT (a), preserving
selected frontmatter onto the compiled page.** The issue explicitly leaves
this choice to the implementer, defaulting to (a) unless the compile path
structurally cannot carry the keys or doing so would leak adapter-internal
fields onto the corpus-visible surface. Both apply here:

1. **Structural.** :class:`~athenaeum.models.WikiEntity` has no general
   "extra frontmatter" escape hatch -- ``WikiEntity.render()`` builds its
   ``meta`` dict from an explicit, hardcoded list of dataclass fields, and
   :func:`athenaeum.tiers.tier3_create_params` builds its model prompt from
   :class:`~athenaeum.models.EntityAction`'s equally fixed field set. Landing
   even one adapter key on the page would mean adding a new dataclass field
   to ``WikiEntity``, a new render branch, and new plumbing through
   :class:`~athenaeum.models.ClassifiedEntity` / ``EntityAction`` /
   :func:`athenaeum.tiers.tier3_derive_actions`'s create AND merge branches
   -- a materially larger, more invasive change than this module.
2. **Leakage.** ``archive_path`` is a local absolute filesystem path on the
   machine that ran the adapter
   (``/Users/<user>/knowledge/raw/mural/<board>.json`` in the live corpus);
   ``room``/``workspace`` are Mural's own internal workspace taxonomy, not a
   fact about the entity the page describes. Recall and the MCP ``recall``
   tool surface compiled-page frontmatter directly into an agent's context
   -- stamping operator-machine paths and a third-party tool's internal
   categories onto every one of (currently) 1,774 pages, forever, is a
   corpus-hygiene regression a ledger avoids entirely: the ledger is queried
   only by something that already asks "where did this page come from",
   never rendered into a recall hit.

**Rejected: (a), preserving keys on the page.** Rejected for the two reasons
above -- not workable without extending ``WikiEntity``'s schema and the
tier-3 create/merge plumbing, and it would push adapter-internal metadata
into the corpus-visible surface every compiled reader/recall hit sees.

**The bounded set (AC2).** :data:`ADAPTER_PROVENANCE_VALUE_KEYS` and
:data:`SOURCE_OBJECT_ID_KEYS` are closed, hardcoded collections -- not
"whatever the adapter sent". A source absent from
:data:`SOURCE_OBJECT_ID_KEYS`, or a frontmatter key absent from
:data:`ADAPTER_PROVENANCE_VALUE_KEYS`, is never captured, no matter what an
adapter emits; widening either requires an in-repo code change, never an
adapter-side one. The literal key names below are measured directly against
a live ``mural-board-summary`` record (``/knowledge/raw/mural-board-summary/``,
2026-09-08):

```yaml
mural_board_id: kromatic5164.1730753845696   # -> SOURCE_OBJECT_ID_KEYS["mural-board-summary"]
room: KIT Open Enrollment Workspaces
workspace: Kromatic
created_on: '1730753845696'
updated_on: '1732116400407'
text_fragment_count: 137
archive_path: <operator-home>/knowledge/raw/mural/kromatic5164....json
template_only: false
participants:                                 # NOT captured -- not in the allowlist
- Nebiyou
```

**Where the ledger is written.** Not literally inside
:mod:`athenaeum.intake` -- the join between a raw record and the page(s) it
produced is only known once Tier 3 has actually written its create/update
result, which happens in :func:`athenaeum.librarian._apply_tier3_results`.
That is the earliest point in the pipeline where both halves of the join
(the raw record's frontmatter, and the uid(s) of the page(s) it just landed
on) are simultaneously available, which is what "written at intake" (the
issue's phrasing) means operationally. :func:`record_adapter_provenance_for_pages`
is the single call this module expects a compile-time caller to make; this
module itself never reads ``raw/`` or ``wiki/`` on its own.

**Fail-open, like every other observability write in this pipeline**
(mirrors :mod:`athenaeum.recovery_yield` and
:func:`athenaeum.intake._record_recovery_yield`): a ledger write failure is
logged and swallowed, never raised -- this is an audit trail, not a gate,
and it must not be able to break the compile it observes.

Layering: L3 service. Imports :mod:`athenaeum.config` (cache dir
resolution), :mod:`athenaeum.models` (``parse_frontmatter``), and
:mod:`athenaeum.store` (``append_line_durable``/``now_iso``) -- all L1/L2 or
below. Mirrors :mod:`athenaeum.push_metrics`'s layering exactly. Must never
import :mod:`athenaeum.librarian` or :mod:`athenaeum.tiers` (that would
close a cycle back to this module's own caller).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.models import parse_frontmatter
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Ledger filename, under the resolved cache dir -- outside the wiki/raw
#: corpus by construction, mirroring
#: :data:`athenaeum.decay_sweep.SWEEP_LEDGER_FILENAME` /
#: :data:`athenaeum.push_metrics.PUSH_RECORDS_FILENAME`.
PROVENANCE_LEDGER_FILENAME = "_adapter_provenance_records.jsonl"

#: Schema version stamped on every ledger record.
PROVENANCE_LEDGER_SCHEMA_VERSION = 1

#: Closed map of Lane A ``source`` (the ``raw/<source>/`` directory name,
#: i.e. ``RawFile.source``) -> the ONE frontmatter key that names that
#: source's own object id. A source absent here is never ledgered -- see
#: the module docstring's AC2 discussion. Extending this for a future
#: adapter is an in-repo, reviewed change, never something an adapter can
#: trigger by simply emitting a new key.
SOURCE_OBJECT_ID_KEYS: dict[str, str] = {
    "mural-board-summary": "mural_board_id",
}

#: Closed set of adapter-supplied descriptive frontmatter keys captured
#: alongside the source object id, for every source in
#: :data:`SOURCE_OBJECT_ID_KEYS`. A key not listed here is NEVER captured --
#: this is what keeps the set "explicit and bounded" per AC2 regardless of
#: what else a raw record's frontmatter carries (e.g. the live corpus's own
#: ``participants:`` list is deliberately excluded).
ADAPTER_PROVENANCE_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "room",
        "workspace",
        "created_on",
        "updated_on",
        "text_fragment_count",
        "archive_path",
        "template_only",
    }
)


@dataclass(frozen=True)
class AdapterProvenanceRecord:
    """One ledger row: one raw record's join to one compiled page.

    ``fields`` is the bounded subset of :data:`ADAPTER_PROVENANCE_VALUE_KEYS`
    that *raw_ref*'s frontmatter actually carried -- never a superset, and
    never a key outside that set, regardless of what the raw frontmatter
    contained.
    """

    source: str
    raw_ref: str
    page_uid: str
    source_object_id: str
    fields: dict[str, Any]
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROVENANCE_LEDGER_SCHEMA_VERSION,
            "source": self.source,
            "raw_ref": self.raw_ref,
            "page_uid": self.page_uid,
            "source_object_id": self.source_object_id,
            "fields": self.fields,
            "recorded_at": self.recorded_at,
        }


def extract_adapter_provenance(
    source: str, raw_content: str
) -> tuple[str, dict[str, Any]] | None:
    """Pull ``(source_object_id, fields)`` out of one raw record's frontmatter.

    Returns ``None`` (nothing to ledger) when:

    - *source* has no entry in :data:`SOURCE_OBJECT_ID_KEYS` -- an adapter
      this module does not know the id-key convention for.
    - *raw_content* has no parseable frontmatter, or the frontmatter is
      missing/blank for that source's declared id key.

    ``fields`` is built by intersecting the frontmatter's own keys with
    :data:`ADAPTER_PROVENANCE_VALUE_KEYS` -- a key present in the raw
    frontmatter but absent from that allowlist (e.g. the live corpus's
    ``participants:``) is silently excluded, by construction, every time.
    """
    id_key = SOURCE_OBJECT_ID_KEYS.get(source)
    if id_key is None:
        return None
    meta, _body = parse_frontmatter(raw_content)
    if not isinstance(meta, dict):
        return None
    raw_id = meta.get(id_key)
    if raw_id is None:
        return None
    source_object_id = str(raw_id).strip()
    if not source_object_id:
        return None
    fields = {k: meta[k] for k in ADAPTER_PROVENANCE_VALUE_KEYS if k in meta}
    return source_object_id, fields


def provenance_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the ledger path: ``<cache_dir>/_adapter_provenance_records.jsonl``.

    Same resolver precedence as every other cache-dir ledger in this
    codebase (:func:`athenaeum.config.resolve_cache_dir`: ``arg >
    ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum``).
    """
    return resolve_cache_dir(cache_dir) / PROVENANCE_LEDGER_FILENAME


def write_adapter_provenance(
    records: list[AdapterProvenanceRecord], *, cache_dir: Path | None = None
) -> None:
    """Append *records* to the durable provenance ledger.

    Fail-open (issue athenaeum#1462, mirrors
    :func:`athenaeum.intake._record_recovery_yield`): a write failure is
    logged and swallowed rather than raised -- this ledger is an audit
    trail, not a gate, and must never be able to break the compile pass
    that calls it. A no-op for an empty *records* list (no file is even
    touched).
    """
    if not records:
        return
    path = provenance_ledger_path(cache_dir)
    lines = "".join(
        json.dumps(rec.to_dict(), separators=(",", ":")) + "\n" for rec in records
    )
    try:
        append_line_durable(path, lines.encode("utf-8"))
    except OSError:
        log.warning(
            "adapter-provenance: failed to append %d record(s) to %s "
            "(issue athenaeum#1462)",
            len(records),
            path,
            exc_info=True,
        )


def record_adapter_provenance_for_pages(
    source: str,
    raw_ref: str,
    raw_content: str,
    page_uids: list[str],
    *,
    cache_dir: Path | None = None,
) -> list[AdapterProvenanceRecord]:
    """Ledger one row per page uid *raw_ref* just compiled onto (issue athenaeum#1462).

    The single call a compile-time caller (:func:`athenaeum.librarian._apply_tier3_results`)
    makes once it knows which uid(s) a raw file's Tier-3 create/update
    actually landed. A no-op (returns ``[]``, writes nothing) when
    *page_uids* is empty or :func:`extract_adapter_provenance` finds nothing
    to record for *source*/*raw_content*.

    Returns the records written (even though the ledger itself is
    append-only and this return value is not re-read by the caller) so a
    test can assert on them directly without a round trip through disk.
    """
    if not page_uids:
        return []
    extracted = extract_adapter_provenance(source, raw_content)
    if extracted is None:
        return []
    source_object_id, fields = extracted
    when = now_iso()
    records = [
        AdapterProvenanceRecord(
            source=source,
            raw_ref=raw_ref,
            page_uid=uid,
            source_object_id=source_object_id,
            fields=dict(fields),
            recorded_at=when,
        )
        for uid in page_uids
    ]
    write_adapter_provenance(records, cache_dir=cache_dir)
    return records


def read_adapter_provenance(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every provenance-ledger record. Never raises.

    Tolerates a torn trailing line (the same crash-safety contract every
    ``append_line_durable``-backed ledger in this codebase documents --
    see :func:`athenaeum.decay_sweep.read_sweep_ledger`) and an absent file
    (fresh install, or a knowledge base with no adapter provenance yet).
    """
    path = provenance_ledger_path(cache_dir)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def resolve_source_for_page(
    page_uid: str, cache_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Join *page_uid* back to every ledgered adapter source object (AC4).

    Reads ONLY the provenance ledger -- never ``raw/``, which may already be
    unlinked by the time a caller asks this question. Returns every matching
    row (oldest first, ledger order) since a page can, in principle, be
    touched by more than one raw record over its lifetime (create, then one
    or more merges); an empty list means "no adapter provenance recorded for
    this page" (either it was never linked, or it was compiled before this
    mechanism existed).
    """
    return [
        row for row in read_adapter_provenance(cache_dir) if row.get("page_uid") == page_uid
    ]
