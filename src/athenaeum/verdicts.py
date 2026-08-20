# SPDX-License-Identifier: Apache-2.0
"""Verdict ledger with justification basis (issue athenaeum#712) — L2 domain/pipeline.

Child (b) of the memory-model v6 MVP (epic athenaeum#709). Ships **before** the
five-verdict comparator that will populate it (a separate, future child of
athenaeum#709) — the comparator's whole invalidation story depends on the basis
being right from entry one, so the store, schema, and invalidation rules land
first.

This is a truth-maintenance move: a verdict is a justified belief about a
pair of memories (``duplicate | contradiction | specialization | distinct |
underdetermined``), and the ``basis`` is the exact set of facts it was
justified by — content hashes, coordinates, epochs, authority. A change to
any basis element can invalidate exactly the verdicts that depended on it,
without invalidating everything else. That is what stops the nightly
rebuild-and-suppress loop this ledger replaces.

Contract: owns the append-only verdict ledger end to end — schema, content
hashing, per-basis-element stale-marking, per-month partitioning +
compaction, and the epoch registry. **A sanctioned reader/writer module is
the only path to it**, mirroring the repo's rule that the pending-merges
sidecar is only read via :func:`athenaeum.pending_merges.parse_pending_merges`
— hand-parsing ``wiki/_verdicts/*.jsonl`` is not a supported access pattern.

Store layout, under ``<wiki_root>/_verdicts/``:

- ``<YYYY-MM>.jsonl`` — one **live** partition per month, keyed by the month
  the verdict currently live for a pair was decided (``at``). Append-only
  JSONL, ``O_APPEND`` + fsync durability, same discipline as
  :mod:`athenaeum.provenance`'s merge-provenance ledger — a crash can at
  worst leave a torn trailing line, which every reader here skips.
- ``_verdicts_history.jsonl`` — verdicts superseded by a newer decision for
  the same pair, moved out of the live partitions by :func:`compact`.
  Invalidation waves (:func:`mark_pairs_stale`) scan the live partitions
  ONLY — a superseded verdict is already dead and needs no stale flag.
- ``_verdicts_epochs.json`` — the per-branch comparator-epoch registry
  (:class:`BranchEpochState`): current ``comparator_version``, whether a
  re-comparison wave is open, and the nights-in-wave / nights-total counters
  the duty-cycle report is computed from.

**Filename choice (issue athenaeum#712 ambiguity aperture):** the issue's "What
the ledger is" section names the concept ``wiki/_verdicts.jsonl`` (singular);
its own "Operational properties" section then requires per-month
partitioning + a live/history split, which a single file cannot satisfy
literally. This module resolves the tension by using a ``_verdicts/``
directory of dated partitions plus a history file — a reversible, purely
on-disk naming choice (no consumer outside this module and its CLI depends
on the literal filename), recorded here per the dispatch's ambiguity policy.

**Single-appender (issue athenaeum#712 AC), reusing** :mod:`athenaeum.runlock`
**rather than inventing a second lock:** every mutating function in this
module (:func:`append_verdict`, :func:`compact`, :func:`mark_pairs_stale`,
:func:`open_epoch`, :func:`close_wave`, :func:`note_run_night`) takes a
required ``lock: RunLock`` keyword argument and raises :class:`RuntimeError`
unless ``lock.acquired`` is True — it does not acquire the lock itself. This
is the SAME assumption :mod:`athenaeum.pending_merges` makes implicitly (its
writers trust the CLI's ``_acquire_or_exit`` to already hold
:class:`athenaeum.runlock.RunLock` around the whole mutating command); this
module states it explicitly and enforces it at the API boundary because a
second, independent ``RunLock(...).acquire()`` call from within an
already-locked run would deadlock on the same-process ``flock`` (a second
``open()`` of the lockfile is a distinct open-file-description and contends
with the first, even in the same process) — see runlock.py's own module
docstring for why that reentrancy trap exists. **Recorded next to the
recorded-time single-writer assumption** (:mod:`athenaeum.provenance` /
:mod:`athenaeum.reasoning_tiers`'s ``O_APPEND`` ledgers make the identical
assumption) so both fall together, visibly, whenever multi-writer support
arrives.

Layering: L2 (domain/pipeline). Imports :mod:`athenaeum.atomic_io` (L0),
:mod:`athenaeum.runlock` (L0), :mod:`athenaeum.models` (L1, for
:func:`~athenaeum.models.parse_frontmatter` / :func:`~athenaeum.models.slugify`),
and :mod:`athenaeum.pii` (L1, for the erasure-class refusal guard). Owns ONLY
the ledger's storage format, hashing, invalidation, and epoch bookkeeping —
it has no opinion on WHICH pairs should be compared or WHAT verdict a pair
deserves; that is the comparator's job (out of scope, a future child of
athenaeum#709).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, slugify
from athenaeum.runlock import RunLock

log = logging.getLogger(__name__)

#: Ledger entry schema version, stamped on every :class:`VerdictEntry` so a
#: future reader can migrate an older on-disk shape.
SCHEMA_VERSION = 1

#: The five verdict values this MVP's schema recognizes (issue athenaeum#712). The
#: comparator that decides between them does not exist yet (separate,
#: future child of athenaeum#709) — this module only stores and invalidates.
VERDICT_VALUES: tuple[str, ...] = (
    "duplicate",
    "contradiction",
    "specialization",
    "distinct",
    "underdetermined",
)

#: Directory (under ``wiki_root``) holding the live monthly partitions, the
#: history file, and the epoch registry.
LEDGER_DIRNAME = "_verdicts"

#: History file — superseded-by-compaction entries, one directory level
#: below ``wiki_root`` alongside the live partitions.
HISTORY_FILENAME = "_verdicts_history.jsonl"

#: Epoch registry filename, same directory.
EPOCH_REGISTRY_FILENAME = "_verdicts_epochs.json"

#: Frontmatter keys treated as SYSTEM-AUTHORED METADATA (issue athenaeum#712 AC):
#: coordinates, breadcrumbs, predicate annotations, and tier flags. These are
#: written BY the verdict/comparator system's own downstream consumers, never
#: by the human/source author, so they are excluded from :func:`content_hash`
#: — otherwise the verdict system would trigger its own re-comparison waves
#: by writing its own outputs onto a page (the exact loop AC this guards).
#: No dimension/coordinate registry exists yet (that is a separate, future
#: child of athenaeum#709; this issue only records ``registry_epoch``/``tree_epoch``
#: as opaque basis fields) — this is therefore a deliberately small, explicit,
#: reversible set of key names rather than a lookup into a registry that does
#: not exist. Recorded as a reversible default per the dispatch's ambiguity
#: policy; extend this set (never silently repurpose it) when the dimension
#: registry lands and defines the real key vocabulary.
SYSTEM_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "coords",
        "coordinates",
        "coord_origins",
        "breadcrumb",
        "breadcrumbs",
        "predicate",
        "predicates",
        "predicate_instrument",
        "tier",
        "tier_flags",
    }
)


class VerdictLedgerError(RuntimeError):
    """Base class for verdict-ledger errors."""


class LockNotHeld(VerdictLedgerError):
    """Raised when a mutating call is made without an acquired :class:`RunLock`.

    The single-appender guarantee (issue athenaeum#712 AC) is enforced HERE, at the
    API boundary, rather than left as an unstated convention — see the module
    docstring's "Single-appender" section.
    """


class EpochWaveInProgress(VerdictLedgerError):
    """Raised by :func:`open_epoch` when the branch's prior wave is still open.

    Issue athenaeum#712 AC: "a new comparator epoch cannot be opened while a
    re-comparison wave from the previous one is incomplete."
    """


def _require_lock(lock: RunLock) -> None:
    if not getattr(lock, "acquired", False):
        raise LockNotHeld(
            "verdicts: mutating call made without an acquired RunLock — "
            "every writer in this module reuses athenaeum.runlock.RunLock "
            "rather than inventing a second lock (issue athenaeum#712); the "
            "caller must hold the SAME lock the CLI's mutating commands "
            "already take (see module docstring's 'Single-appender' note)."
        )


# ---------------------------------------------------------------------------
# Content hashing — claim content only (issue athenaeum#712 AC)
# ---------------------------------------------------------------------------


def content_hash(page_text: str) -> str:
    """SHA-256 hash over a page's CLAIM CONTENT only.

    Excludes :data:`SYSTEM_METADATA_KEYS` from the frontmatter before
    hashing (coordinates, breadcrumbs, predicate annotations, tier flags —
    system-authored metadata that lives in the basis separately, never in
    the hash). The body text is always included verbatim.

    This is the exact guard the issue's AC names: "writing system metadata
    to a page leaves its content hash unchanged" — see
    ``tests/test_verdicts.py::test_content_hash_excludes_system_metadata``.
    """
    meta, body = parse_frontmatter(page_text)
    if not isinstance(meta, dict):
        meta = {}
    claim_meta = {k: v for k, v in meta.items() if k not in SYSTEM_METADATA_KEYS}
    canonical = json.dumps(claim_meta, sort_keys=True, ensure_ascii=False) + "\n" + body
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash_for_path(path: Path) -> str | None:
    """:func:`content_hash` for a file on disk. ``None`` if unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return content_hash(text)


def page_id_for_path(path: Path) -> str:
    """Canonical pair-member id for a wiki page path: its slug.

    Slugs are the durable identity handle this repo already keys aliases,
    wikilinks, and fold targets on (see
    :func:`athenaeum.pending_merges._source_slugs`) — reused here rather
    than inventing a second id space.
    """
    return slugify(Path(path).stem)


def make_pair_key(id_a: str, id_b: str) -> str:
    """Canonical ``"<idA>+<idB>"`` pair key, order-independent.

    Ids are sorted so ``(a, b)`` and ``(b, a)`` produce the identical key —
    a pair is compared once regardless of which side is named first.
    """
    a, b = sorted((id_a, id_b))
    return f"{a}+{b}"


# ---------------------------------------------------------------------------
# Schema: Basis + VerdictEntry
# ---------------------------------------------------------------------------


@dataclass
class Basis:
    """The exact set of facts a verdict was justified by (issue athenaeum#712).

    Every field is either populated or explicitly ``None`` with a reason
    recorded in :attr:`null_reasons` — "populated or explicitly null with a
    documented reason" per the AC. ``predicate_instrument`` is logged-only
    (consumed by nothing in v1, per the issue's Out-of-scope section).
    """

    content_hashes: list[str | None] = field(default_factory=lambda: [None, None])
    coords: list[Any] = field(default_factory=lambda: [None, None])
    coord_origins: dict[str, str] = field(default_factory=dict)
    registry_epoch: int | None = None
    tree_epoch: int | None = None
    authority_basis: str | None = None
    predicate_instrument: list[str | None] = field(default_factory=lambda: [None, None])
    comparator_version: str | None = None
    #: field name -> human-readable reason it is null. Additive beyond the
    #: issue's literal JSON example — every other field name there is
    #: preserved verbatim so an entry round-trips through the example shape.
    null_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hashes": list(self.content_hashes),
            "coords": list(self.coords),
            "coord_origins": dict(self.coord_origins),
            "registry_epoch": self.registry_epoch,
            "tree_epoch": self.tree_epoch,
            "authority_basis": self.authority_basis,
            "predicate_instrument": list(self.predicate_instrument),
            "comparator_version": self.comparator_version,
            "null_reasons": dict(self.null_reasons),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Basis:
        return cls(
            content_hashes=list(d.get("content_hashes") or [None, None]),
            coords=list(d.get("coords") or [None, None]),
            coord_origins=dict(d.get("coord_origins") or {}),
            registry_epoch=d.get("registry_epoch"),
            tree_epoch=d.get("tree_epoch"),
            authority_basis=d.get("authority_basis"),
            predicate_instrument=list(d.get("predicate_instrument") or [None, None]),
            comparator_version=d.get("comparator_version"),
            null_reasons=dict(d.get("null_reasons") or {}),
        )


@dataclass
class VerdictEntry:
    """One decided pairwise-comparison verdict (issue athenaeum#712)."""

    pair: str
    verdict: str
    basis: Basis
    separator: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    assumed: list[str] = field(default_factory=list)
    at: str = ""
    decided_by: str = ""
    stale: bool = False
    stale_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "verdict": self.verdict,
            "separator": list(self.separator),
            "missing": list(self.missing),
            "assumed": list(self.assumed),
            "at": self.at,
            "decided_by": self.decided_by,
            "basis": self.basis.to_dict(),
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VerdictEntry:
        return cls(
            pair=str(d.get("pair", "")),
            verdict=str(d.get("verdict", "")),
            basis=Basis.from_dict(d.get("basis") or {}),
            separator=list(d.get("separator") or []),
            missing=list(d.get("missing") or []),
            assumed=list(d.get("assumed") or []),
            at=str(d.get("at", "")),
            decided_by=str(d.get("decided_by", "")),
            stale=bool(d.get("stale", False)),
            stale_reason=d.get("stale_reason"),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


def build_verdict_entry(
    id_a: str,
    id_b: str,
    verdict: str,
    *,
    basis: Basis,
    separator: list[str] | None = None,
    missing: list[str] | None = None,
    assumed: list[str] | None = None,
    at: str | None = None,
    decided_by: str,
) -> VerdictEntry:
    """Build one :class:`VerdictEntry`, validating ``verdict`` and normalizing the pair."""
    if verdict not in VERDICT_VALUES:
        raise ValueError(f"verdict must be one of {VERDICT_VALUES!r}, got {verdict!r}")
    if not decided_by or not decided_by.strip():
        raise ValueError("decided_by is required (e.g. 'comparator', 'human:<ref>')")
    return VerdictEntry(
        pair=make_pair_key(id_a, id_b),
        verdict=verdict,
        basis=basis,
        separator=list(separator or []),
        missing=list(missing or []),
        assumed=list(assumed or []),
        at=at or date.today().isoformat(),
        decided_by=decided_by,
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def ledger_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / LEDGER_DIRNAME


def partition_path(wiki_root: Path, month: str) -> Path:
    """Live partition path for ``month`` (``"YYYY-MM"``)."""
    return ledger_dir(wiki_root) / f"{month}.jsonl"


def history_path(wiki_root: Path) -> Path:
    return ledger_dir(wiki_root) / HISTORY_FILENAME


def epoch_registry_path(wiki_root: Path) -> Path:
    return ledger_dir(wiki_root) / EPOCH_REGISTRY_FILENAME


def ledger_exists(wiki_root: Path) -> bool:
    """True if the ledger directory has been materialized at all."""
    return ledger_dir(wiki_root).is_dir()


# ---------------------------------------------------------------------------
# Durable JSONL append (mirrors athenaeum.provenance._append_jsonl_line)
# ---------------------------------------------------------------------------


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Same discipline as :func:`athenaeum.provenance._append_jsonl_line` /
    :mod:`athenaeum.spend`: a single small ``O_APPEND`` write is atomic on
    local filesystems, so a crash can at worst leave a torn TRAILING line
    (every reader below skips it), never corrupt an already-written record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_jsonl_tolerant(path: Path) -> list[dict[str, Any]]:
    """Read *path* as JSONL, skipping blank/malformed/torn lines. ``[]`` if absent."""
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Writer — append (single-appender enforced via RunLock)
# ---------------------------------------------------------------------------


def append_verdict(wiki_root: Path, entry: VerdictEntry, *, lock: RunLock) -> Path:
    """Append *entry* to its month's live partition. Returns the partition path.

    Requires an ALREADY-ACQUIRED ``lock`` (see module docstring's
    "Single-appender" section) — raises :class:`LockNotHeld` otherwise.
    """
    _require_lock(lock)
    month = (entry.at or date.today().isoformat())[:7]
    path = partition_path(wiki_root, month)
    line = json.dumps(entry.to_dict(), separators=(",", ":")) + "\n"
    _append_jsonl_line(path, line)
    return path


# ---------------------------------------------------------------------------
# Reader — live partitions, history, memoization
# ---------------------------------------------------------------------------


def iter_live_entries(wiki_root: Path) -> list[tuple[str, VerdictEntry]]:
    """Every entry across every live monthly partition, as ``(month, entry)``.

    Scans ``<wiki_root>/_verdicts/<YYYY-MM>.jsonl`` for every month present
    (``_verdicts_history.jsonl`` and ``_verdicts_epochs.json`` are NOT live
    partitions and are excluded). Invalidation waves and lookups read only
    from here — never from history (issue athenaeum#712 AC).
    """
    d = ledger_dir(wiki_root)
    if not d.is_dir():
        return []
    out: list[tuple[str, VerdictEntry]] = []
    for p in sorted(d.glob("*.jsonl")):
        if p.name == HISTORY_FILENAME:
            continue
        month = p.stem
        for record in _read_jsonl_tolerant(p):
            out.append((month, VerdictEntry.from_dict(record)))
    return out


def read_history_entries(wiki_root: Path) -> list[VerdictEntry]:
    return [VerdictEntry.from_dict(r) for r in _read_jsonl_tolerant(history_path(wiki_root))]


def lookup_pair(wiki_root: Path, pair_key: str) -> VerdictEntry | None:
    """The current LIVE verdict for *pair_key*, or ``None`` if never decided.

    If more than one live entry exists for the pair (compaction has not run
    yet), the most recently decided (``at``) wins — the same "keep the
    latest verdict per pair live" rule :func:`compact` enforces durably.
    """
    candidates = [e for _, e in iter_live_entries(wiki_root) if e.pair == pair_key]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.at)


def get_verdict_status(wiki_root: Path, pair_key: str) -> dict[str, Any]:
    """Answer "has this pair been decided, and is it fresh?" in one call.

    Issue athenaeum#712 AC (Pair memoization). Returns
    ``{"decided": bool, "fresh": bool, "verdict": str|None, "at": str|None,
    "stale_reason": str|None}``. "Fresh" means the pipeline does not need to
    re-compare this pair — i.e. decided AND not stale.
    """
    entry = lookup_pair(wiki_root, pair_key)
    if entry is None:
        return {"decided": False, "fresh": False, "verdict": None, "at": None, "stale_reason": None}
    return {
        "decided": True,
        "fresh": not entry.stale,
        "verdict": entry.verdict,
        "at": entry.at,
        "stale_reason": entry.stale_reason,
    }


def can_authorize_auto_operation(entry: VerdictEntry) -> bool:
    """True iff *entry* is fresh enough to authorize a NEW automatic operation.

    Issue athenaeum#712 AC: "a stale verdict cannot authorize a new automatic
    operation — auto-fold and auto-supersession require a fresh basis."
    Marking a verdict stale never touches — and this predicate never
    reasons about — operations already applied under an earlier, then-fresh
    verdict; those stand until a re-comparison confirms or proposes reversal
    (a decision this module does not make; see :func:`mark_pairs_stale`'s
    docstring for the corresponding side-effect-free guarantee).
    """
    return not entry.stale


# ---------------------------------------------------------------------------
# Compaction — keep the latest verdict per pair live; supersede the rest
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    moved_to_history: int
    kept_live: int
    partitions_rewritten: list[str]


def compact(wiki_root: Path, *, lock: RunLock) -> CompactionResult:
    """Keep the latest verdict per pair live; move superseded entries to history.

    Issue athenaeum#712 AC (Operational properties): "compaction on a schedule
    keeps the latest verdict per pair live and moves superseded entries to a
    history partition." Rewrites only the partitions whose contents actually
    changed; a corpus already compacted is a no-op (no files touched).
    """
    _require_lock(lock)
    all_entries = iter_live_entries(wiki_root)
    if not all_entries:
        return CompactionResult(moved_to_history=0, kept_live=0, partitions_rewritten=[])

    by_pair: dict[str, list[tuple[str, VerdictEntry]]] = {}
    for month, entry in all_entries:
        by_pair.setdefault(entry.pair, []).append((month, entry))

    keep: dict[str, tuple[str, VerdictEntry]] = {}
    superseded: list[VerdictEntry] = []
    for pair_key, versions in by_pair.items():
        versions_sorted = sorted(versions, key=lambda mv: mv[1].at)
        winner_month, winner = versions_sorted[-1]
        keep[pair_key] = (winner_month, winner)
        for _, loser in versions_sorted[:-1]:
            superseded.append(loser)

    # Rewrite each partition to hold only its kept winners.
    by_month: dict[str, list[VerdictEntry]] = {}
    for month, winner in keep.values():
        by_month.setdefault(month, []).append(winner)

    rewritten: list[str] = []
    d = ledger_dir(wiki_root)
    existing_months = {p.stem for p in d.glob("*.jsonl") if p.name != HISTORY_FILENAME} if d.is_dir() else set()
    for month in sorted(existing_months | set(by_month)):
        entries = sorted(by_month.get(month, []), key=lambda e: e.pair)
        text = "".join(json.dumps(e.to_dict(), separators=(",", ":")) + "\n" for e in entries)
        path = partition_path(wiki_root, month)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if text != current:
            if entries:
                atomic_write_text(path, text)
            elif path.exists():
                atomic_write_text(path, "")
            rewritten.append(month)

    if superseded:
        hpath = history_path(wiki_root)
        lines = "".join(json.dumps(e.to_dict(), separators=(",", ":")) + "\n" for e in superseded)
        hpath.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(hpath, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, lines.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    return CompactionResult(
        moved_to_history=len(superseded),
        kept_live=len(keep),
        partitions_rewritten=rewritten,
    )


# ---------------------------------------------------------------------------
# Targeted stale-marking — one selector function per rule (issue athenaeum#712 AC)
# ---------------------------------------------------------------------------
#
# Each ``select_stale_for_*`` function is a PURE selector: given the current
# live entries and a description of what changed, it returns
# ``{pair_key: reason}`` for exactly the pairs that rule invalidates — no
# I/O, so each has its own narrow, fast unit test. :func:`mark_pairs_stale`
# is the single persistence path shared by all six (and by any future rule),
# so "stale-marking" is targeted at the SELECTION layer while staying
# uniform at the WRITE layer.


def select_stale_for_changed_page(
    entries: list[VerdictEntry],
    page_id: str,
    *,
    new_content_hash: str | None = None,
    new_coords: Any = None,
) -> dict[str, str]:
    """Rule: content hash OR coordinates changed for *page_id* -> that pair re-compares."""
    out: dict[str, str] = {}
    for e in entries:
        sides = e.pair.split("+", 1)
        if page_id not in sides:
            continue
        idx = sides.index(page_id)
        hashes = e.basis.content_hashes
        coords = e.basis.coords
        side_hash = hashes[idx] if idx < len(hashes) else None
        side_coords = coords[idx] if idx < len(coords) else None
        if new_content_hash is not None and side_hash != new_content_hash:
            out[e.pair] = f"content hash changed for {page_id}"
        elif new_coords is not None and side_coords != new_coords:
            out[e.pair] = f"coordinates changed for {page_id}"
    return out


def select_stale_for_dimension_change(
    entries: list[VerdictEntry],
    dimension: str,
    *,
    changed_ids: set[str] | None = None,
) -> dict[str, str]:
    """Rule: a dimension flips to enforced/retired.

    Stale-marks a pair when a side named in ``changed_ids`` (a side that
    gained/lost the dimension's coordinate) is one of the pair's members, OR
    the verdict named *dimension* in ``assumed``/``missing``.
    """
    changed = changed_ids or set()
    out: dict[str, str] = {}
    for e in entries:
        sides = set(e.pair.split("+", 1))
        if dimension in e.assumed or dimension in e.missing:
            out[e.pair] = f"dimension {dimension!r} named in assumed/missing"
            continue
        if changed and sides & changed:
            out[e.pair] = f"dimension {dimension!r} coordinate changed for a pair member"
    return out


def select_stale_for_coordinate_challenged(
    entries: list[VerdictEntry], answer_id: str
) -> dict[str, str]:
    """Rule: a queue-answered coordinate is challenged -> stale-mark every verdict
    whose basis lists *answer_id* in ``coord_origins``."""
    out: dict[str, str] = {}
    for e in entries:
        if answer_id in e.basis.coord_origins.values():
            out[e.pair] = f"coord_origins answer {answer_id!r} challenged"
    return out


def select_stale_for_tree_epoch_bump(
    entries: list[VerdictEntry],
    new_tree_epoch: int,
    renamed_subtree_prefixes: list[str],
) -> dict[str, str]:
    """Rule: ``tree_epoch`` bumps -> stale-mark only verdicts whose basis
    coordinates touch a renamed subtree."""
    out: dict[str, str] = {}
    if not renamed_subtree_prefixes:
        return out
    for e in entries:
        if e.basis.tree_epoch == new_tree_epoch:
            continue
        for coord in e.basis.coords:
            if coord is None:
                continue
            coord_str = str(coord)
            if any(coord_str.startswith(prefix) for prefix in renamed_subtree_prefixes):
                out[e.pair] = f"tree_epoch bump touches renamed subtree in coords"
                break
    return out


def select_stale_for_authority_revoked(
    entries: list[VerdictEntry], authority_basis_value: str
) -> dict[str, str]:
    """Rule: ``authority_basis`` cites a now-false grant relation.

    Trivially ``implicit-superuser`` in single-operator mode (the field
    exists so a future multi-operator grant revision changes DATA, not
    schema — issue athenaeum#712 AC).
    """
    out: dict[str, str] = {}
    for e in entries:
        if e.basis.authority_basis == authority_basis_value:
            out[e.pair] = f"authority_basis {authority_basis_value!r} revoked"
    return out


def select_stale_for_comparator_epoch_bump(
    entries: list[VerdictEntry], branch: str, new_version: str
) -> dict[str, str]:
    """Rule: a comparator epoch bump stale-marks ONLY the branch it can affect.

    ``branch`` is a comparator_version PREFIX (e.g. ``"v1.gate2"``) — a
    ``content_relation`` prompt tweak on Gate 2 stale-marks Gate-2-decided
    verdicts, never Gate-1 typed exits (issue athenaeum#712 AC).
    """
    out: dict[str, str] = {}
    for e in entries:
        cv = e.basis.comparator_version
        if cv and cv.startswith(branch) and cv != new_version:
            out[e.pair] = f"comparator branch {branch!r} bumped to {new_version!r}"
    return out


def mark_pairs_stale(
    wiki_root: Path, pair_reasons: dict[str, str], *, lock: RunLock
) -> int:
    """Persist stale marks for ``{pair: reason}`` across the live partitions.

    Shared write path for every ``select_stale_for_*`` rule above. Only
    rewrites a partition whose contents actually change; a pair already
    marked stale (same reason or not) is left as-is on its FIRST stale
    reason — stale-marking never clears or replaces an existing reason, it
    only sets one the first time. Side-effect-free w.r.t. anything outside
    the ledger: this function touches only files under
    :func:`ledger_dir` and never wiki pages, provenance, or pending-merge
    state — the "operations already applied stand" half of the issue's AC
    (see :func:`can_authorize_auto_operation`'s docstring for the other half).
    Returns the count of entries newly marked stale.
    """
    _require_lock(lock)
    if not pair_reasons:
        return 0
    d = ledger_dir(wiki_root)
    if not d.is_dir():
        return 0
    marked = 0
    for p in sorted(d.glob("*.jsonl")):
        if p.name == HISTORY_FILENAME:
            continue
        records = _read_jsonl_tolerant(p)
        changed = False
        for record in records:
            pair = record.get("pair")
            if pair in pair_reasons and not record.get("stale"):
                record["stale"] = True
                record["stale_reason"] = pair_reasons[pair]
                changed = True
                marked += 1
        if changed:
            text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
            atomic_write_text(p, text)
    return marked


# ---------------------------------------------------------------------------
# Epoch registry — no-overlapping-wave guard + duty-cycle computation
# ---------------------------------------------------------------------------


@dataclass
class BranchEpochState:
    version: str = ""
    wave_open: bool = False
    wave_started_at: str | None = None
    nights_in_wave: int = 0
    nights_total: int = 0
    batch_interval_days: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "wave_open": self.wave_open,
            "wave_started_at": self.wave_started_at,
            "nights_in_wave": self.nights_in_wave,
            "nights_total": self.nights_total,
            "batch_interval_days": self.batch_interval_days,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BranchEpochState:
        return cls(
            version=str(d.get("version", "")),
            wave_open=bool(d.get("wave_open", False)),
            wave_started_at=d.get("wave_started_at"),
            nights_in_wave=int(d.get("nights_in_wave", 0)),
            nights_total=int(d.get("nights_total", 0)),
            batch_interval_days=int(d.get("batch_interval_days", 30)),
        )


def duty_cycle(state: BranchEpochState) -> float:
    """``nights_in_wave / nights_total`` (target <=0.25; reporting only — issue athenaeum#712 AC
    says enforcing the target is out of scope)."""
    if state.nights_total <= 0:
        return 0.0
    return state.nights_in_wave / state.nights_total


def load_epoch_registry(wiki_root: Path) -> dict[str, BranchEpochState]:
    path = epoch_registry_path(wiki_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        branch: BranchEpochState.from_dict(state)
        for branch, state in raw.items()
        if isinstance(state, dict)
    }


def save_epoch_registry(
    wiki_root: Path, registry: dict[str, BranchEpochState], *, lock: RunLock
) -> None:
    _require_lock(lock)
    path = epoch_registry_path(wiki_root)
    payload = {branch: state.to_dict() for branch, state in registry.items()}
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def open_epoch(
    wiki_root: Path,
    branch: str,
    new_version: str,
    *,
    batch_interval_days: int = 30,
    now: datetime | None = None,
    lock: RunLock,
) -> BranchEpochState:
    """Open a new comparator epoch on *branch*.

    Raises :class:`EpochWaveInProgress` if the branch's previous
    re-comparison wave has not been closed yet (issue athenaeum#712 AC: "no
    overlapping epochs").
    """
    _require_lock(lock)
    registry = load_epoch_registry(wiki_root)
    existing = registry.get(branch)
    if existing is not None and existing.wave_open:
        raise EpochWaveInProgress(
            f"branch {branch!r} already has an open wave (version "
            f"{existing.version!r}, started {existing.wave_started_at}); "
            "close it before opening a new epoch"
        )
    ts = (now or datetime.now(timezone.utc)).isoformat()
    nights_total = existing.nights_total if existing is not None else 0
    state = BranchEpochState(
        version=new_version,
        wave_open=True,
        wave_started_at=ts,
        nights_in_wave=0,
        nights_total=nights_total,
        batch_interval_days=batch_interval_days,
    )
    registry[branch] = state
    save_epoch_registry(wiki_root, registry, lock=lock)
    return state


def close_wave(wiki_root: Path, branch: str, *, lock: RunLock) -> BranchEpochState | None:
    """Close *branch*'s open re-comparison wave. No-op (returns ``None``) if absent."""
    _require_lock(lock)
    registry = load_epoch_registry(wiki_root)
    state = registry.get(branch)
    if state is None:
        return None
    state.wave_open = False
    registry[branch] = state
    save_epoch_registry(wiki_root, registry, lock=lock)
    return state


def note_run_night(wiki_root: Path, *, lock: RunLock) -> dict[str, float]:
    """Increment nights_total (and nights_in_wave for any open branch) by one night.

    Called once per ``athenaeum run`` when the verdict ledger is enabled
    (see ``librarian.run()``'s finalize step). Returns the resulting
    per-branch duty-cycle report. A registry with no branches yet is a
    well-formed no-op (returns ``{}``) — there is nothing to bump until
    :func:`open_epoch` has been called at least once.
    """
    _require_lock(lock)
    registry = load_epoch_registry(wiki_root)
    if not registry:
        return {}
    for state in registry.values():
        state.nights_total += 1
        if state.wave_open:
            state.nights_in_wave += 1
    save_epoch_registry(wiki_root, registry, lock=lock)
    return {branch: duty_cycle(state) for branch, state in registry.items()}


def duty_cycle_report(wiki_root: Path) -> dict[str, float]:
    """Read-only duty-cycle report per branch — the CLI/`athenaeum status` surface."""
    registry = load_epoch_registry(wiki_root)
    return {branch: duty_cycle(state) for branch, state in registry.items()}


def ensure_ledger_initialized(wiki_root: Path, *, lock: RunLock) -> Path:
    """Materialize the ledger directory (and epoch registry file) if absent.

    Idempotent — a second call on an already-initialized ledger touches
    nothing. This is the minimal, always-safe write a live ``athenaeum run``
    performs when the verdict ledger is enabled (see
    ``docs/configuration.md``'s "Verdict ledger" section for the Wiring
    decision this implements) so the store is genuinely live and queryable
    even before the comparator (a future, separate child of athenaeum#709)
    exists to populate it with real verdicts.
    """
    _require_lock(lock)
    d = ledger_dir(wiki_root)
    d.mkdir(parents=True, exist_ok=True)
    epath = epoch_registry_path(wiki_root)
    if not epath.exists():
        atomic_write_text(epath, json.dumps({}, indent=2) + "\n")
    return d


# ---------------------------------------------------------------------------
# Erasure-class refusal guard (issue athenaeum#712 Out of scope)
# ---------------------------------------------------------------------------


def refuse_if_erasure_class(source_path: Path) -> str | None:
    """Refuse a pair whose content is erasure-class / low-entropy personal data.

    Issue athenaeum#712's Out-of-scope section: "This issue must not write
    erasure-class content or plain hashes of short low-entropy personal
    facts into the in-git ledger; if such a pair would be decided, refuse
    and leave a TODO pointing at the off-corpus child." The off-corpus
    ledger shard + HMAC-keyed erasure-class hashing is a separate, future
    child of athenaeum#709 (not built here); this is the narrow, reversible
    refusal gate using the sensitivity signal that already exists in this
    repo (:func:`athenaeum.pii.is_pii_flagged` — the same ``pii:``
    frontmatter flag the T2 reasoning-tier safe-class check already gates
    auto-apply on), rather than inventing a second classifier.

    Returns a human-readable refusal reason, or ``None`` when the page is
    clear to hash/record.
    """
    from athenaeum.pii import is_pii_flagged

    try:
        text = Path(source_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _ = parse_frontmatter(text)
    if isinstance(meta, dict) and is_pii_flagged(meta):
        return (
            f"TODO(athenaeum#712 off-corpus child): {source_path} is "
            "erasure-class (pii-flagged) content — refusing to write its "
            "content hash into the in-git verdict ledger. This pair needs "
            "the off-corpus ledger shard + HMAC-keyed erasure-class hashing "
            "(out of scope of athenaeum#712)."
        )
    return None


# ---------------------------------------------------------------------------
# Wiring: record a verdict for a decision the CURRENT pipeline already makes
# ---------------------------------------------------------------------------


def record_pair_decision(
    wiki_root: Path,
    *,
    source_a: str,
    source_b: str,
    verdict: str,
    decided_by: str,
    lock: RunLock,
    separator: list[str] | None = None,
    missing: list[str] | None = None,
    assumed: list[str] | None = None,
    registry_epoch: int | None = None,
    tree_epoch: int | None = None,
    authority_basis: str = "implicit-superuser",
    comparator_version: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Record one verdict for a pair the pipeline just decided, end to end.

    This is the integration point the CURRENT pipeline (merge
    approve/reject via :func:`athenaeum.decision_answers._apply_merge_answer`)
    calls when the verdict ledger is enabled — the issue's Wiring AC option
    "consumed within this same issue by writing verdicts for the decisions
    the current pipeline already makes." Never enumerates pairs itself: the
    caller supplies exactly the two sources of a real, already-decided
    cluster disposition — this function never walks the corpus, so ledger
    growth stays linear in cluster-level dispositions (issue athenaeum#712 AC).

    Computes both sides' content hashes, refuses (via
    :func:`refuse_if_erasure_class`) if either source is erasure-class
    content, builds the :class:`Basis` + :class:`VerdictEntry`, and appends.

    Returns ``{"ok": bool, "error_code": str|None, "pair": str|None}``.
    Never raises for an ordinary refusal (erasure-class, unreadable source)
    — those are reported in the return value so a caller (which must not
    let a ledger write block the merge it is recording) can log and
    continue.
    """
    try:
        for src in (source_a, source_b):
            refusal = refuse_if_erasure_class(Path(src))
            if refusal is not None:
                log.warning("verdicts: refusing pair write — %s", refusal)
                return {"ok": False, "error_code": "erasure_class_refused", "pair": None}

        id_a = page_id_for_path(Path(source_a))
        id_b = page_id_for_path(Path(source_b))
        hash_a = content_hash_for_path(Path(source_a))
        hash_b = content_hash_for_path(Path(source_b))
        null_reasons: dict[str, str] = {}
        if hash_a is None:
            null_reasons["content_hashes[0]"] = f"{source_a} unreadable at decision time"
        if hash_b is None:
            null_reasons["content_hashes[1]"] = f"{source_b} unreadable at decision time"

        basis = Basis(
            content_hashes=[hash_a, hash_b],
            coords=[None, None],
            coord_origins={},
            registry_epoch=registry_epoch,
            tree_epoch=tree_epoch,
            authority_basis=authority_basis,
            predicate_instrument=[None, None],
            comparator_version=comparator_version,
            null_reasons=null_reasons,
        )
        entry = build_verdict_entry(
            id_a,
            id_b,
            verdict,
            basis=basis,
            separator=separator,
            missing=missing,
            assumed=assumed,
            at=at,
            decided_by=decided_by,
        )
        append_verdict(wiki_root, entry, lock=lock)
        return {"ok": True, "error_code": None, "pair": entry.pair}
    except Exception as exc:  # noqa: BLE001 — a ledger write must never break
        # the merge decision it is recording (same discipline as
        # athenaeum.provenance.record_merge_provenance's best-effort guard).
        log.debug("verdicts: record_pair_decision failed (%s): %s", type(exc).__name__, exc)
        return {"ok": False, "error_code": "ledger_write_failed", "pair": None}


# ---------------------------------------------------------------------------
# CLI-facing read helpers (sanctioned read path — issue athenaeum#712 AC)
# ---------------------------------------------------------------------------


def ledger_count(wiki_root: Path) -> int:
    return len(iter_live_entries(wiki_root))


def list_by_verdict(wiki_root: Path, verdict: str | None = None) -> list[dict[str, Any]]:
    entries = [e for _, e in iter_live_entries(wiki_root)]
    if verdict is not None:
        entries = [e for e in entries if e.verdict == verdict]
    return [e.to_dict() for e in sorted(entries, key=lambda e: e.pair)]


def show_one_pair(wiki_root: Path, pair_key: str) -> dict[str, Any] | None:
    entry = lookup_pair(wiki_root, pair_key)
    return entry.to_dict() if entry is not None else None


def show_stale(wiki_root: Path) -> list[dict[str, Any]]:
    entries = [e for _, e in iter_live_entries(wiki_root) if e.stale]
    return [e.to_dict() for e in sorted(entries, key=lambda e: e.pair)]


__all__ = [
    "SCHEMA_VERSION",
    "VERDICT_VALUES",
    "LEDGER_DIRNAME",
    "HISTORY_FILENAME",
    "EPOCH_REGISTRY_FILENAME",
    "SYSTEM_METADATA_KEYS",
    "VerdictLedgerError",
    "LockNotHeld",
    "EpochWaveInProgress",
    "content_hash",
    "content_hash_for_path",
    "page_id_for_path",
    "make_pair_key",
    "Basis",
    "VerdictEntry",
    "build_verdict_entry",
    "ledger_dir",
    "partition_path",
    "history_path",
    "epoch_registry_path",
    "ledger_exists",
    "append_verdict",
    "iter_live_entries",
    "read_history_entries",
    "lookup_pair",
    "get_verdict_status",
    "can_authorize_auto_operation",
    "CompactionResult",
    "compact",
    "select_stale_for_changed_page",
    "select_stale_for_dimension_change",
    "select_stale_for_coordinate_challenged",
    "select_stale_for_tree_epoch_bump",
    "select_stale_for_authority_revoked",
    "select_stale_for_comparator_epoch_bump",
    "mark_pairs_stale",
    "BranchEpochState",
    "duty_cycle",
    "load_epoch_registry",
    "save_epoch_registry",
    "open_epoch",
    "close_wave",
    "note_run_night",
    "duty_cycle_report",
    "ensure_ledger_initialized",
    "refuse_if_erasure_class",
    "record_pair_decision",
    "ledger_count",
    "list_by_verdict",
    "show_one_pair",
    "show_stale",
]
