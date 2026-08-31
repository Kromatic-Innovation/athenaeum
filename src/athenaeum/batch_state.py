# SPDX-License-Identifier: Apache-2.0
"""Pending-batch handle store and raw-file lease (issue athenaeum#1143).

Foundation for the async submit/collect split (parent epic athenaeum#1138).
Under any design where a run SUBMITS an Anthropic batch and exits, leaving
collection to a later run, the raw files that batch was built from are still
sitting in ``raw/``: :func:`athenaeum.intake.discover_raw_files` has no
in-flight or claim concept, and raw files are only unlinked on finalize
success. So run N submits 300 files and exits; run N+1 rediscovers the same
300 and **resubmits them at full price**. That failure is silent — it looks
exactly like normal progress while double-billing.

This module is the sidecar that closes it. For each submitted-but-uncollected
batch it records a HANDLE — the batch id, the knob (``classify`` | ``write``),
when it was submitted, its phase, and the ``custom_id -> raw ref`` mapping with
each ref's absolute path and a content hash taken at claim time — and, over
those refs, a **lease**. The entity-phase claim loop
(:func:`athenaeum.librarian._run_entity_tier_phase`) honours the lease when it
assembles a new cohort, so a leased file is not re-claimed while its batch is
still in flight. An expired lease is released on the next claim pass and its
refs become claimable again, so a lost or abandoned batch cannot strand its
intake forever.

**Store location: the cache dir, not ``wiki_root``.** The handle is
machine-local, in-flight, and mutable. Committing it into the wiki's git
snapshot every run would produce churn and make a collect resumed elsewhere
look like a legitimate corpus change. :mod:`athenaeum.detection_state` and
:mod:`athenaeum.zero_yield` both chose the cache dir for this reason, and this
module follows ``detection_state``'s shape precedent exactly: a single JSON
document at ``<cache_dir>/pending_batches.json``, written through
:func:`athenaeum.atomic_io.atomic_write_text`, read fail-open.

**Fail-open is load-bearing, not politeness.** A missing, empty, or unparseable
store loads as ``{}`` and logs at WARNING, never raises — a marker store must
never break the run it guards. The cost of a lost store is one cohort
re-claimed early (at worst a duplicate submit, which is what the operator would
have got with no lease at all); the cost of a raising one would be a wedged
nightly run.

**Filtering happens in the claim loop, never inside ``discover_raw_files``.**
That function is pure filesystem discovery with a carefully documented contract
across four issues' worth of semantics; pushing run-state awareness into it
would couple L1 discovery to L3 batch state and break every caller that expects
it to enumerate what is on disk. Its signature and behaviour are untouched by
this module — it still returns leased files, and the claim loop drops them.

**Not behind the :mod:`athenaeum.storage` seam.** That module is the
entity-class -> storage-surface adapter (``resolve_adapter_for_class``,
``corpus_policy_for_class``). A pending batch handle is run state with no
entity class; routing it there would be a category error.

**Layering:** L3 service. Module scope imports :mod:`athenaeum.config` and
:mod:`athenaeum.atomic_io` (both L2) and nothing else from the package.
Consumed by the L4 :mod:`athenaeum.librarian` claim pass and clean-exit sweep —
never imports ``librarian`` back.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum import config
from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

#: Sidecar filename under the cache dir.
_STORE_NAME = "pending_batches.json"

#: On-disk document version. Bumped only for a shape change that an older
#: reader could misread; an unknown version loads as empty (fail-open).
STORE_VERSION = 1

#: The two batch knobs a handle can belong to, mirroring
#: :func:`athenaeum.batch.execute_batch`'s own ``knob`` argument.
KNOBS = ("classify", "write")

#: Phase a handle is in. ``submitted`` is the only phase this foundation
#: issue writes; the collect side (a later child of athenaeum#1138) advances it.
DEFAULT_PHASE = "submitted"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(ts: datetime) -> datetime:
    """Coerce *ts* to an aware UTC datetime.

    Every ``now=`` argument this module accepts crosses here first. A NAIVE
    datetime is read as UTC rather than left to :meth:`datetime.astimezone`,
    which would silently reinterpret it as LOCAL time — a lease written on a
    UTC+13 machine would then be hours off, and a naive/aware comparison in
    :meth:`PendingBatch.lease_active` would raise outright. The collect phase
    this module is the foundation for will pass its own timestamps in; this is
    where that stops being a footgun.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _fmt(ts: datetime) -> str:
    return _as_utc(ts).strftime(_TS_FMT)


def _parse(ts: str) -> datetime | None:
    """Parse a stored ISO-8601 stamp, or ``None`` if it is unusable.

    Fail-open: an unparseable ``leased_until`` is treated as no lease at all
    (the refs become claimable) rather than as an infinite one, because a
    corrupt stamp must never strand intake permanently.
    """
    try:
        parsed = datetime.strptime(ts, _TS_FMT)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RefRecord:
    """One ``custom_id``'s raw file, as claimed at submit time (AC3).

    ``ref`` is the :attr:`athenaeum.models.RawFile.ref` short reference
    (``<source>/<filename>``) — the same string the claim loop filters on.
    ``path`` is the raw file's ABSOLUTE path, kept so a collect resumed later
    can find the file without re-deriving it from ``raw_root``. ``content_hash``
    is taken **at claim time**, so a collect can tell a file that was rewritten
    under it apart from the one it actually submitted.
    """

    ref: str
    path: str
    content_hash: str
    #: Issue athenaeum#1145: the serving model id this request was submitted
    #: with, read from its ``params["model"]`` at claim time. A later run's
    #: collect books the batch's token usage against THIS model, preserving the
    #: athenaeum#247 per-model attribution ``execute_batch`` performs within a run via
    #: ``model_by_cid`` — which is otherwise unrecoverable once the submitting
    #: run has exited. ``None`` on a handle recorded before athenaeum#1145.
    model: str | None = None


@dataclass(frozen=True)
class PendingBatch:
    """A submitted-but-uncollected batch, and the lease over its raw files."""

    batch_id: str
    knob: str
    submitted_at: str
    phase: str = DEFAULT_PHASE
    #: ISO-8601 ``Z`` stamp, or ``None`` when leasing is disabled
    #: (``resolve_batch_lease_seconds`` returned ``None``) or the lease has
    #: been explicitly released.
    leased_until: str | None = None
    #: ``custom_id -> RefRecord``.
    refs: dict[str, RefRecord] = field(default_factory=dict)
    #: Issue athenaeum#1145: an OPAQUE, JSON-serializable document carrying whatever
    #: the collecting run needs to apply this batch's results through the
    #: normal finalize path. This module round-trips it and never interprets
    #: it: the schema belongs to :mod:`athenaeum.batch`, which is the only
    #: module that knows what a tier-3 merge needs in order to be applied.
    #: Putting it here rather than inventing a second sidecar keeps ONE
    #: atomically-written document per batch, so a handle and the work it
    #: describes can never disagree. ``None`` on a handle recorded before
    #: athenaeum#1145, and on any handle whose knob needs no extra context.
    work: dict[str, Any] | None = None

    def lease_active(self, now: datetime | None = None) -> bool:
        """Whether this handle's lease is still holding its refs."""
        if not self.leased_until:
            return False
        until = _parse(self.leased_until)
        if until is None:
            return False
        # Strictly ``>``: a lease held exactly to ``leased_until`` has expired
        # AT that instant, not one tick after. Pinned by a boundary test —
        # the tie-break must not flip silently.
        return until > _as_utc(now or _now())


def resolve_cache_dir() -> Path:
    """The cache dir the pending-batch store lives under.

    Mirrors :mod:`athenaeum.detection_state`'s resolution exactly
    (``ATHENAEUM_CACHE_DIR`` env, else ``~/.cache/athenaeum``) so the WRITE side
    (submit, via :func:`record_handle`) and the READ side (the next run's claim
    pass, via :func:`leased_refs`) always agree on the same file across runs.
    """
    return config.resolve_cache_dir()


def store_path(cache_dir: Path) -> Path:
    """Absolute path of the sidecar under *cache_dir*."""
    return Path(cache_dir) / _STORE_NAME


def content_hash(path: Path) -> str:
    """Stable short hash of a raw file's bytes, or ``""`` when unreadable.

    Mirrors :func:`athenaeum.librarian._stuck_content_hash`'s fail-open
    contract: any read error hashes to the empty string, which simply means a
    later collect cannot prove the file is unchanged — never a raised error at
    claim time.
    """
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(payload).hexdigest()[:16]


def _ref_from_json(raw: Any) -> RefRecord | None:
    if not isinstance(raw, dict):
        return None
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref:
        return None
    path = raw.get("path")
    chash = raw.get("content_hash")
    model = raw.get("model")
    return RefRecord(
        ref=ref,
        path=path if isinstance(path, str) else "",
        content_hash=chash if isinstance(chash, str) else "",
        model=model if isinstance(model, str) else None,
    )


def _handle_from_json(batch_id: str, raw: Any) -> PendingBatch | None:
    if not isinstance(raw, dict):
        return None
    knob = raw.get("knob")
    refs: dict[str, RefRecord] = {}
    raw_refs = raw.get("refs")
    if isinstance(raw_refs, dict):
        for custom_id, entry in raw_refs.items():
            record = _ref_from_json(entry)
            if isinstance(custom_id, str) and record is not None:
                refs[custom_id] = record
    leased_until = raw.get("leased_until")
    submitted_at = raw.get("submitted_at")
    phase = raw.get("phase")
    work = raw.get("work")
    return PendingBatch(
        batch_id=batch_id,
        knob=knob if isinstance(knob, str) else "",
        submitted_at=submitted_at if isinstance(submitted_at, str) else "",
        phase=phase if isinstance(phase, str) else DEFAULT_PHASE,
        leased_until=leased_until if isinstance(leased_until, str) else None,
        refs=refs,
        # Fail-open on shape, like every other field here: a ``work`` document
        # that is not an object reads as absent, which degrades the collect to
        # "retire and re-claim" rather than raising.
        work=work if isinstance(work, dict) else None,
    )


def _handle_to_json(handle: PendingBatch) -> dict[str, Any]:
    out: dict[str, Any] = {
        "knob": handle.knob,
        "submitted_at": handle.submitted_at,
        "phase": handle.phase,
        "leased_until": handle.leased_until,
        "refs": {
            custom_id: {
                "ref": record.ref,
                "path": record.path,
                "content_hash": record.content_hash,
                "model": record.model,
            }
            for custom_id, record in sorted(handle.refs.items())
        },
    }
    if handle.work is not None:
        out["work"] = handle.work
    return out


def load(cache_dir: Path) -> dict[str, PendingBatch]:
    """Return ``batch_id -> PendingBatch`` for every recorded handle, or ``{}``.

    Fail-open (AC2): a missing, empty, unparseable, wrong-shaped, or
    unknown-version store reads as no handles and logs at WARNING rather than
    raising. The worst consequence is a cohort claimed earlier than its lease
    intended; a raising store would wedge the run this sidecar exists to
    protect.
    """
    path = store_path(cache_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning(
            "pending-batch store unreadable (%s: %s) — treating as empty; "
            "leased raw files may be re-claimed early",
            type(exc).__name__,
            exc,
        )
        return {}
    if not isinstance(raw, dict):
        log.warning("pending-batch store is not an object — treating as empty")
        return {}
    version = raw.get("version")
    if version != STORE_VERSION:
        log.warning(
            "pending-batch store version %r is not %d — treating as empty",
            version,
            STORE_VERSION,
        )
        return {}
    handles_raw = raw.get("handles")
    if not isinstance(handles_raw, dict):
        return {}
    out: dict[str, PendingBatch] = {}
    for batch_id, entry in handles_raw.items():
        if not isinstance(batch_id, str) or not batch_id:
            continue
        handle = _handle_from_json(batch_id, entry)
        if handle is not None:
            out[batch_id] = handle
    return out


def _save(cache_dir: Path, handles: Mapping[str, PendingBatch]) -> None:
    """Persist *handles*, or remove the store when it is empty.

    Best-effort, matching :mod:`athenaeum.detection_state`: a write failure
    warns and returns rather than breaking the run. An empty store is REMOVED
    so a fully-collected machine leaves no stale residue behind (AC7).
    """
    path = store_path(cache_dir)
    if not handles:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                "pending-batch store removal failed (%s: %s)", type(exc).__name__, exc
            )
        return
    payload = {
        "version": STORE_VERSION,
        "handles": {
            batch_id: _handle_to_json(handle)
            for batch_id, handle in sorted(handles.items())
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:  # never break a run over a marker write
        log.warning(
            "pending-batch store write failed (%s: %s) — an in-flight batch's "
            "raw files are unleased and may be re-submitted next run",
            type(exc).__name__,
            exc,
        )


def record_handle(
    cache_dir: Path,
    *,
    batch_id: str,
    knob: str,
    refs: Mapping[str, Any],
    config: dict[str, Any] | None = None,
    phase: str = DEFAULT_PHASE,
    work: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> PendingBatch | None:
    """Record a submitted batch and take a lease over its raw files.

    *refs* maps each request's ``custom_id`` to the
    :class:`~athenaeum.models.RawFile` it was built from — anything exposing
    ``.ref`` and ``.path`` is accepted, matching the duck-typed ``raw: Any``
    convention the librarian's own ledgers use. A :class:`RefRecord` is
    accepted verbatim, so a caller that already hashed its files does not
    re-read them.

    The lease length comes from
    :func:`athenaeum.config.resolve_batch_lease_seconds`; a resolved ``None``
    (the operator set the knob ``<= 0``) records the handle with NO lease —
    the explicit opt-out, not a silent default. Returns the stored handle, or
    ``None`` when *batch_id* is empty (nothing to key on).

    *work* (issue athenaeum#1145) is an opaque JSON document stored verbatim on the
    handle — see :attr:`PendingBatch.work`. This module neither reads nor
    validates it.

    Idempotent: re-recording the same *batch_id* replaces its handle and
    re-takes the lease from *now*.
    """
    if not batch_id:
        return None
    stamp = _as_utc(now or _now())
    lease_seconds = resolve_lease_seconds(config)
    leased_until = (
        _fmt(stamp + timedelta(seconds=lease_seconds))
        if lease_seconds is not None
        else None
    )
    records: dict[str, RefRecord] = {}
    for custom_id, raw in refs.items():
        if not isinstance(custom_id, str) or not custom_id:
            continue
        if isinstance(raw, RefRecord):
            records[custom_id] = raw
            continue
        ref = getattr(raw, "ref", None)
        path = getattr(raw, "path", None)
        if not isinstance(ref, str) or not ref or not path:
            # A falsy ``path`` (not just ``None``) is rejected: ``Path("")``
            # resolves to the process CWD, which would record a lease against
            # a file that has nothing to do with this batch.
            continue
        absolute = Path(path).resolve()
        records[custom_id] = RefRecord(
            ref=ref, path=str(absolute), content_hash=content_hash(absolute)
        )
    handle = PendingBatch(
        batch_id=batch_id,
        knob=knob,
        submitted_at=_fmt(stamp),
        phase=phase,
        leased_until=leased_until,
        refs=records,
        work=work,
    )
    handles = load(cache_dir)
    handles[batch_id] = handle
    _save(cache_dir, handles)
    return handle


def retire_handle(cache_dir: Path, batch_id: str) -> None:
    """Drop *batch_id*'s handle entirely — it was collected. No-op if absent.

    This is the collect-side terminal: the batch's results have landed, so
    neither the handle nor its lease has anything left to protect. Use
    :func:`release_lease` instead to free the refs while KEEPING the handle.
    """
    if not batch_id:
        return
    handles = load(cache_dir)
    if batch_id in handles:
        del handles[batch_id]
        _save(cache_dir, handles)


def release_lease(cache_dir: Path, batch_id: str) -> None:
    """Clear *batch_id*'s lease, keeping the handle. No-op if absent.

    The refs become claimable again on the next claim pass while the handle
    stays recorded — the shape an abandoned-but-not-yet-reconciled batch
    needs, and what :func:`release_expired_leases` applies automatically.
    """
    if not batch_id:
        return
    handles = load(cache_dir)
    handle = handles.get(batch_id)
    if handle is None or handle.leased_until is None:
        return
    handles[batch_id] = PendingBatch(
        batch_id=handle.batch_id,
        knob=handle.knob,
        submitted_at=handle.submitted_at,
        phase=handle.phase,
        leased_until=None,
        refs=handle.refs,
    )
    _save(cache_dir, handles)


def leased_refs(cache_dir: Path, *, now: datetime | None = None) -> set[str]:
    """Every :attr:`~athenaeum.models.RawFile.ref` held by a LIVE lease (AC5).

    An expired lease contributes nothing, so this is safe to call from a
    ``--dry-run`` claim pass that must not write: the answer is identical to
    the one a writing run would compute, it just leaves the expired entries on
    disk for :func:`release_expired_leases` to clear.
    """
    at = _as_utc(now or _now())
    refs: set[str] = set()
    for handle in load(cache_dir).values():
        if handle.lease_active(at):
            refs.update(record.ref for record in handle.refs.values())
    return refs


def release_expired_leases(cache_dir: Path, *, now: datetime | None = None) -> list[str]:
    """Release every lease whose ``leased_until`` has passed (AC6/AC7).

    Called at the top of the claim pass — so an abandoned batch's refs become
    claimable again on the next run rather than being stranded forever — and at
    every clean non-dry-run exit path, so no expired residue outlives the run
    that observed it. The handles themselves are KEPT (a batch whose results
    are still retrievable can still be collected); only the lease is dropped.

    Returns the batch ids released, newest-store-order-independent (sorted), so
    the caller can log exactly what it freed. Never writes when nothing expired.
    """
    at = _as_utc(now or _now())
    handles = load(cache_dir)
    released: list[str] = []
    for batch_id, handle in sorted(handles.items()):
        if handle.leased_until is None:
            continue
        if handle.lease_active(at):
            continue
        released.append(batch_id)
        handles[batch_id] = PendingBatch(
            batch_id=handle.batch_id,
            knob=handle.knob,
            submitted_at=handle.submitted_at,
            phase=handle.phase,
            leased_until=None,
            refs=handle.refs,
        )
    if released:
        _save(cache_dir, handles)
    return released


def resolve_lease_seconds(config_data: dict[str, Any] | None) -> float | None:
    """Thin passthrough to :func:`athenaeum.config.resolve_batch_lease_seconds`.

    Kept so callers inside this module (and its tests) name the knob once,
    without shadowing the ``config`` parameter name used by the public
    functions above.
    """
    return config.resolve_batch_lease_seconds(config_data)
