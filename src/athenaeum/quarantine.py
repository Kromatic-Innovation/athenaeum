# SPDX-License-Identifier: Apache-2.0
"""Per-file quarantine for poison raw-intake files (issue athenaeum#898).

A raw intake file that keeps exceeding its per-file bound (byte size,
LLM-call count, or wall-clock — the consecutive-count accounting lives in
:mod:`athenaeum.librarian`, mirroring the athenaeum#663 stuck-file ledger's shape) is
QUARANTINED: physically moved out of ``raw_root`` into
``<wiki_root>/_quarantine/<source>/<filename>`` so it drops out of
:func:`athenaeum.intake.discover_raw_files`'s discovery set without any
change to that function itself, and recorded in a durable, append-only audit
ledger naming the file and the bound it exceeded.

This is deliberately a HEAVIER, more visible action than the athenaeum#663 stuck-file
skip: a stuck file stays on disk and is merely skipped in place (an
unbounded-content processing FAILURE may be transient, so leaving it for
inspection costs nothing); a bound violation is a measured RESOURCE fact, and
crossing the consecutive-run threshold means the file has already cost
:data:`athenaeum.librarian.DEFAULT_QUARANTINE_THRESHOLD` (or its configured
override) nights of budget for zero durable progress. Removing it from the
discovery set is what actually stops the bleed; the athenaeum#663 skip alone would
not, since a skip-in-place file is still discovered and checked every run.

Persistence mirrors the other librarian ledgers in shape (JSONL, one record
per line, tolerant reader that skips a torn trailing line). As of issue
athenaeum#982 (slice S7 of the whole-store adapter design lock, athenaeum#911) every
read/write routes through :mod:`athenaeum.store` — the ledger append and both
directions of the physical move go through :meth:`~athenaeum.store.Store.append`
and :meth:`~athenaeum.store.Store.move` rather than a local ``O_APPEND`` +
fsync helper and the standard library's recursive move utility.
:mod:`athenaeum.calibration` /
:mod:`athenaeum.retraction_cascade` still carry their own per-module copy of
the pre-migration ``_append_jsonl_line`` pattern, pending their own migration
slices. One ledger, ``<wiki_root>/_quarantine.jsonl``, carries two record
kinds: ``quarantine`` (the file was moved out) and ``release`` (an operator's
reversing decision — AC6: quarantine is reversible, and an operator decision
is the ONLY way back; there is no automatic un-quarantine, by design).
Surfaced through :func:`athenaeum.decisions.list_pending_decisions` as
``type: "quarantine"`` items via :func:`athenaeum.decisions.quarantine_to_decision`.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.calibration`
and :mod:`athenaeum.retraction_cascade`. Imports only stdlib plus
:mod:`athenaeum.store` (L0/L1, issue athenaeum#976) at module scope — no
filesystem-path-object module, no recursive-move-utility module (issue
athenaeum#982). :mod:`athenaeum.librarian`
(the entity phase runner) is the one caller that resolves the
consecutive-violation threshold and decides WHEN to call
:func:`quarantine_file` — this module only executes the mechanical action
(move + ledger write) and the reversal, and never imports ``librarian`` back.

Store injection (issue athenaeum#982): every public function below still takes
bare ``wiki_root``/``raw_root`` roots — unchanged from before this migration,
so no existing caller needs to change — plus a new keyword-only ``store=``
parameter. When omitted, a private :class:`~athenaeum.store.FilesystemStore`
scoped to exactly those two roots is built per call (see :func:`_default_store`);
a caller that already holds a resolved :class:`~athenaeum.store.Store` (for
example from :func:`athenaeum.storage.resolve_store_for_class`) may pass it
explicitly instead. ``wiki_root``/``raw_root``/``ledger_path`` are typed
``Any`` (matching this module's existing ``raw: Any`` convention) rather than
a filesystem path-object type: this module cannot import that standard
library module at all (see above), so it cannot name the type either.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from athenaeum.store import FilesystemStore, Store, StoreKey

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record so a future reader can migrate.
QUARANTINE_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_calibration.jsonl`` and
#: ``_pending_retractions.jsonl``.
QUARANTINE_LEDGER_FILENAME = "_quarantine.jsonl"

#: Holding directory under ``wiki_root`` a quarantined file is moved into,
#: preserving its ``<source>/<filename>`` shape so a release can restore it
#: to the exact ``raw_root``-relative path it came from.
QUARANTINE_DIR_NAME = "_quarantine"

#: Record kinds in the single quarantine ledger.
QUARANTINE_KIND = "quarantine"
RELEASE_KIND = "release"

#: Surface names for the private, per-call default store (issue athenaeum#982).
#: Internal to this module only — never registered with :mod:`athenaeum.storage`
#: — so they must not collide with a real adapter name; see
#: :func:`_default_store`.
_WIKI_SURFACE = "quarantine-wiki"
_RAW_SURFACE = "quarantine-raw"


def default_quarantine_ledger_path(wiki_root: Any) -> str:
    """Default quarantine ledger path: ``<wiki_root>/_quarantine.jsonl``.

    Returns a plain ``str``, not a filesystem path-object — issue athenaeum#982
    removed this module's path-object import entirely (module docstring), and
    nothing else in the repo consumes this function's return value as such an
    object (checked at migration time).
    """
    return os.path.join(os.fspath(wiki_root), QUARANTINE_LEDGER_FILENAME)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def quarantine_item_id(ref: str, created_at: str) -> str:
    """Deterministic idempotency key for one quarantine EVENT.

    Keyed on ``(ref, created_at)`` rather than ``ref`` alone: the same ref can
    legitimately be quarantined, released, and quarantined again later (a
    fresh run of consecutive bound violations on edited-then-still-bad
    content), and each event needs its own id for the ledger's
    quarantine/release pairing to stay unambiguous.
    """
    digest = hashlib.sha1(f"{ref}\x00{created_at}".encode("utf-8")).hexdigest()
    return digest[:16]


def _relative_key(path: Any, root: Any) -> str | None:
    """POSIX-style store key for *path* relative to *root*, or ``None`` when
    *path* is not actually rooted under *root*.

    Mirrors the standard library path-object's ``relative_to``'s
    ``ValueError`` on an unrelated path, without calling any method of that
    module (module docstring, issue athenaeum#982: every read/write routes
    through :mod:`athenaeum.store` instead, and this module no longer
    imports it).
    """
    rel = os.path.relpath(os.fspath(path), os.fspath(root))
    if rel.split(os.sep, 1)[0] == os.pardir:
        return None
    return rel if os.sep == "/" else rel.replace(os.sep, "/")


def _ledger_key(wiki_root: Any, ledger_path: Any) -> str:
    """The ledger's store key: ``ledger_path`` made relative to ``wiki_root``
    when given, else the default ``_quarantine.jsonl`` filename.

    Known limitation introduced by the store migration (issue athenaeum#982,
    not exercised by any test or caller in this repo): the store addresses
    objects by a surface-relative :class:`~athenaeum.store.StoreKey`, so a
    ``ledger_path`` override that is NOT under ``wiki_root`` is no longer
    representable and raises ``ValueError`` here — previously (pre-migration)
    an arbitrary absolute path worked unconditionally.
    """
    if ledger_path is None:
        return QUARANTINE_LEDGER_FILENAME
    key = _relative_key(ledger_path, wiki_root)
    if key is None:
        raise ValueError(
            f"ledger_path={ledger_path!r} must be located under "
            f"wiki_root={wiki_root!r}: the store (issue athenaeum#982) addresses "
            "objects by a surface-relative key, so an override outside "
            "wiki_root is not representable"
        )
    return key


def _default_store(wiki_root: Any, raw_root: Any = None) -> Store:
    """Build a :class:`~athenaeum.store.Store` scoped to *wiki_root* (and
    *raw_root*, when given) for a caller that does not inject one (issue
    athenaeum#982).

    :func:`athenaeum.storage.resolve_store_for_class` is the canonical
    resolver, but it needs an *entity_class* + *config* + *knowledge_root*
    this module's public functions were never given — they take bare
    ``wiki_root``/``raw_root`` roots, and changing that would break every
    existing caller (out of scope for this migration). This is the
    documented workaround: a private :class:`~athenaeum.store.FilesystemStore`
    whose two surface names (:data:`_WIKI_SURFACE`/:data:`_RAW_SURFACE`) are
    internal to this module only, covering exactly the roots the caller
    already passed. A caller that already holds a real resolved store (for
    example from ``resolve_store_for_class``) should pass it via *store=*
    instead — this function is only the fallback.
    """
    roots: dict[str, Any] = {_WIKI_SURFACE: wiki_root}
    if raw_root is not None:
        roots[_RAW_SURFACE] = raw_root
    return FilesystemStore(wiki_root, roots)


def read_quarantine_ledger(
    wiki_root: Any, *, ledger_path: Any = None, store: Store | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Returns ``[]`` when the ledger does not exist. Malformed lines (a crash
    mid-write, or a hand-edit) are skipped, not fatal.
    """
    store = store if store is not None else _default_store(wiki_root)
    key = StoreKey(surface=_WIKI_SURFACE, key=_ledger_key(wiki_root, ledger_path))
    try:
        raw_bytes = store.read(key)
    except (OSError, KeyError):
        # OSError covers FilesystemStore (FileNotFoundError, PermissionError,
        # ...); KeyError covers a store-fake backend with no on-disk concept
        # of a missing object (design note §6.3: "no exists()";
        # tests/test_store_conformance.py pins ``read`` on a missing key to
        # ``(FileNotFoundError, KeyError)`` across backends).
        return []
    records: list[dict[str, Any]] = []
    for line in raw_bytes.decode("utf-8").splitlines():
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


def _released_ids(records: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") == RELEASE_KIND}


def list_pending_quarantine(
    wiki_root: Any, *, ledger_path: Any = None, store: Store | None = None
) -> list[dict[str, Any]]:
    """Quarantined files awaiting an operator decision (issue athenaeum#898, AC 4/5).

    Excludes any quarantine event that already has a matching ``release``
    record — the same "unreviewed" filter shape as
    :func:`athenaeum.calibration.list_pending_audit`.
    """
    records = read_quarantine_ledger(wiki_root, ledger_path=ledger_path, store=store)
    released = _released_ids(records)
    return [
        r
        for r in records
        if r.get("kind") == QUARANTINE_KIND and str(r.get("id")) not in released
    ]


def quarantine_file(
    raw: Any,
    *,
    wiki_root: Any,
    raw_root: Any,
    bound: str,
    detail: str,
    violations: int,
    ledger_path: Any = None,
    store: Store | None = None,
) -> dict[str, Any]:
    """Move *raw* out of the discovery set and record the quarantine (issue athenaeum#898, AC 4).

    ``raw`` is an :class:`athenaeum.models.RawFile` (or anything duck-typing
    its ``path``/``source``/``ref``). The file is moved — not copied — from
    its ``raw_root``-relative location to the mirrored key under
    ``<wiki_root>/_quarantine/`` via :meth:`~athenaeum.store.Store.move`
    (issue athenaeum#982), so a subsequent
    :func:`athenaeum.intake.discover_raw_files` call over ``raw_root`` no
    longer finds it (AC 4: "moves the file out of the discovery set"). A
    ``quarantine`` record is appended to the ledger naming the ref, the
    ``bound`` that was exceeded (``"bytes"`` / ``"llm_calls"`` /
    ``"wall_clock"``), a human-readable ``detail`` string, and the
    consecutive-violation count that triggered this — the audit trail AC 4
    requires. Both the ``original_path`` (for :func:`release_quarantine`) and
    the ``quarantine_path`` are recorded, ``wiki_root``-relative and
    ``raw_root``-relative respectively.

    Returns the appended record (also the shape :func:`list_pending_quarantine`
    and :func:`athenaeum.decisions.quarantine_to_decision` consume).

    Ordering (code-review finding, athenaeum#898): the ledger record is written
    **before** the file is moved, deliberately. If the move raises partway
    (disk-full, permission error, or the SIGTERM this run's per-file loop
    installs a handler for) after the ledger write already landed, the
    failure mode is a ledger entry pointing at a file that is still in its
    original place — DETECTABLE (the caller can log it, and the record is
    visible to anyone reading the ledger or ``list_pending_quarantine``) —
    rather than the reverse ordering's failure mode, a file silently moved
    with no ledger record at all: invisible to AC 4/5's listing surface,
    findable only by a manual filesystem search. Neither ordering makes the
    two-step sequence atomic; this one fails toward visibility. The caller
    (the entity loop in ``librarian.py``) wraps this call in a try/except so
    a raised exception here does not crash the run — it logs and leaves the
    file's bound-violation ledger entry retry-eligible for the next run.

    Note (issue athenaeum#982): :meth:`~athenaeum.store.Store.move` refuses
    rather than clobbering when the destination key already exists — a
    slightly stricter failure mode than the pre-migration recursive move
    utility, which would have silently overwritten an existing file at the
    destination. Not exercised by any existing test (a quarantine
    destination is only ever occupied by a file this same ``ref``'s prior,
    still-unreleased quarantine already owns).
    """
    store = store if store is not None else _default_store(wiki_root, raw_root)

    original_relpath = _relative_key(raw.path, raw_root)
    if original_relpath is None:
        # Defensive: a raw file not actually rooted under raw_root (e.g. a
        # hand-built test double) still gets a sensible, reconstructable
        # relative path rather than raising here.
        original_relpath = f"{raw.source}/{os.path.basename(os.fspath(raw.path))}"

    quarantine_key = f"{QUARANTINE_DIR_NAME}/{original_relpath}"

    created_at = _now_iso()
    record = {
        "v": QUARANTINE_LEDGER_VERSION,
        "kind": QUARANTINE_KIND,
        "id": quarantine_item_id(raw.ref, created_at),
        "created_at": created_at,
        "ref": raw.ref,
        "source": raw.source,
        "bound": bound,
        "detail": detail,
        "violations": violations,
        "original_path": original_relpath,
        "quarantine_path": quarantine_key,
    }
    ledger_key = _ledger_key(wiki_root, ledger_path)
    store.append(
        StoreKey(surface=_WIKI_SURFACE, key=ledger_key),
        (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
    )

    store.move(
        StoreKey(surface=_RAW_SURFACE, key=original_relpath),
        StoreKey(surface=_WIKI_SURFACE, key=quarantine_key),
    )

    log.info(
        "athenaeum#898: quarantined %s -> %s (bound=%s violations=%d)",
        raw.ref,
        quarantine_key,
        bound,
        violations,
    )
    return record


def release_quarantine(
    wiki_root: Any,
    raw_root: Any,
    *,
    quarantine_id: str,
    note: str = "",
    ledger_path: Any = None,
    store: Store | None = None,
) -> dict[str, Any]:
    """Reverse a quarantine: move the file back into the discovery set (issue athenaeum#898, AC 6).

    Looks up the quarantine event by ``quarantine_id`` (the id
    :func:`quarantine_file` returned/recorded); moves the file from its
    quarantine holding key back to its original ``raw_root``-relative
    location via :meth:`~athenaeum.store.Store.move` (issue athenaeum#982), so
    the NEXT :func:`athenaeum.intake.discover_raw_files` call finds it again —
    the only path back into the discovery set (no automatic un-quarantine
    exists anywhere in this module, by design: AC 6 requires an operator
    decision). Appends a ``release`` record to the ledger.

    Raises ``ValueError`` if ``quarantine_id`` is unknown or already
    released — each quarantine event is released at most once, mirroring
    :func:`athenaeum.calibration.record_audit_review`'s guard.

    If the quarantined file is no longer present at release time (manually
    deleted, moved by an operator, etc. — surfaced as
    :class:`FileNotFoundError` from :meth:`~athenaeum.store.Store.move`), the
    release record is still written — a decision that can never be marked
    resolved because its file evaporated would be a worse failure mode than
    a release record that describes a file the operator must restore some
    other way — but a WARNING is logged naming exactly what happened.
    """
    store = store if store is not None else _default_store(wiki_root, raw_root)
    records = read_quarantine_ledger(wiki_root, ledger_path=ledger_path, store=store)
    quarantined = next(
        (
            r
            for r in records
            if r.get("kind") == QUARANTINE_KIND and str(r.get("id")) == quarantine_id
        ),
        None,
    )
    if quarantined is None:
        raise ValueError(f"unknown quarantine item id: {quarantine_id!r}")
    if quarantine_id in _released_ids(records):
        raise ValueError(f"quarantine item already released: {quarantine_id!r}")

    quarantine_key = str(quarantined.get("quarantine_path", ""))
    original_key = str(quarantined.get("original_path", ""))
    try:
        store.move(
            StoreKey(surface=_WIKI_SURFACE, key=quarantine_key),
            StoreKey(surface=_RAW_SURFACE, key=original_key),
        )
    except FileNotFoundError:
        log.warning(
            "athenaeum#898: quarantined file missing on disk at release time: %s "
            "(ledger record %s) — releasing the ledger record anyway so the "
            "decision does not stay stuck forever; the file itself cannot be "
            "restored by this call.",
            quarantine_key,
            quarantine_id,
        )

    record = {
        "v": QUARANTINE_LEDGER_VERSION,
        "kind": RELEASE_KIND,
        "id": quarantine_id,
        "created_at": _now_iso(),
        "ref": quarantined.get("ref"),
        "note": note,
    }
    ledger_key = _ledger_key(wiki_root, ledger_path)
    store.append(
        StoreKey(surface=_WIKI_SURFACE, key=ledger_key),
        (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    log.info(
        "athenaeum#898: released quarantine %s (ref=%s)",
        quarantine_id,
        quarantined.get("ref"),
    )
    return record


__all__ = [
    "QUARANTINE_LEDGER_VERSION",
    "QUARANTINE_LEDGER_FILENAME",
    "QUARANTINE_DIR_NAME",
    "QUARANTINE_KIND",
    "RELEASE_KIND",
    "default_quarantine_ledger_path",
    "quarantine_item_id",
    "read_quarantine_ledger",
    "list_pending_quarantine",
    "quarantine_file",
    "release_quarantine",
]
