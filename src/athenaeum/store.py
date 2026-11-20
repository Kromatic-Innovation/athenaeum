# SPDX-License-Identifier: Apache-2.0
"""The whole-store adapter seam: ``StoreKey``/``ObjectMeta``/``StoreCapabilities``
and the ``Store`` protocol, plus ``FilesystemStore`` (issue athenaeum#976, slice S1
of the whole-store adapter design lock, issue athenaeum#911).

This is the physical layer :mod:`athenaeum.storage` has never had — that module
resolves an entity class to a **surface** (a ``backing_store`` name, a root, and a
corpus policy) and hands back a bare :class:`pathlib.Path`; every caller then
does its own filesystem arithmetic against it. This module generalizes the
*physical* half: a :class:`Store` addresses an object by :class:`StoreKey`
(surface + POSIX-style relative key, never an OS path) and reads/writes bytes
through the protocol, so a future non-filesystem adapter is a second
implementation of this module, not a second front door.

Full rationale: ``docs/extending/whole-store-adapter-design.md`` §6 (the published draft
contract this module implements verbatim from §6.2, plus the §6.1 design
decisions D1-D6). **No existing caller is migrated onto this seam in this
slice** — S2 (athenaeum#977), S3 (athenaeum#978), S4 (athenaeum#979) and S7
(athenaeum#982) do that. ``snapshot`` is implemented for real as of S3
(athenaeum#978) — see :meth:`FilesystemStore.snapshot`. ``lease`` is
implemented for real as of S4 (athenaeum#979) — see :meth:`FilesystemStore.lease`
and :class:`FileLease`. The R3 persistence-class enforcement §5.2 defines is
implemented as of S5 (athenaeum#980) — see :class:`ArtifactDeclaration` and
:data:`ARTIFACT_REGISTRY`.

**The lease primitive and its relationship to** :mod:`athenaeum.runlock`
**(issue athenaeum#979, S4).** The flock + heartbeat + inode-race hardening that
used to live entirely inside :class:`athenaeum.runlock.RunLock` (the CLI's
single-machine mutating-command mutex, issue athenaeum#309/#397/#526/#763) is
MOVED here — :func:`lease_open_fd`, :func:`lease_try_flock`,
:func:`lease_holds_current_inode`, :func:`lease_write_metadata`,
:func:`lease_refresh_heartbeat`, :func:`lease_break_lockfile`, plus the
holder-diagnostic trio :func:`read_holder`/:func:`is_stale`/
:func:`heartbeat_age_seconds` and :func:`_pid_alive` — generalized from
``RunLock``'s hardcoded ``knowledge_root/.athenaeum.lock`` to an arbitrary
``lockfile: Path``, since a lease name is caller-chosen (design note §6.2)
rather than fixed to one CLI convention. :class:`FileLease` (the
``AbstractContextManager[Lease]`` :meth:`FilesystemStore.lease` returns) is
built from exactly these functions — a single non-blocking attempt, with an
optional unconditional ``force`` break, and NO poll loop or
heartbeat-staleness auto-break of its own: those are ``RunLock``-specific
*policy* (the ``wait``/``force``/``break_stale_after``/``warn_stale_after``
knobs its CLI callers configure), layered on top by repeated,
force-escalating calls into this same engine. ``athenaeum.runlock`` imports
these names from here (a normal downward L0→L1-consumer edge — this module
never imports ``athenaeum.runlock`` back, so there is no cycle); see that
module's docstring for the full acquire()/release()/heartbeat() orchestration
that stayed there. Every byte written to the lockfile, every log message, and
every one of ``RunLock``'s existing exceptions/behaviors is unchanged — this
is a relocation of the primitive operations, not a redesign of the lock.

**Published, in part, as of S8 (issue athenaeum#983).** The ``Store`` protocol's
data/error types (:class:`StoreKey`, :class:`ObjectMeta`, :class:`Record`,
:class:`StoreCapabilities`, :class:`Lease`, :class:`Store` itself,
:class:`StoreKeyError`, :class:`StoreConflictError`, :class:`LeaseHeldError`,
:class:`UnknownSurfaceError`) and :class:`FilesystemStore` are now on the
package root's stable ``__all__`` surface (see ``src/athenaeum/__init__.py``
and ``docs/extending/store-contract.md``, the published form of this docstring's §6).
The rest of this module stays internal, same as :mod:`athenaeum.storage` and
:class:`athenaeum.search.SearchBackend`: the S4 lease-primitive internals
(:func:`lease_open_fd` and siblings, :class:`FileLease`), the S5
artifact-registry catalogue (:data:`ARTIFACT_REGISTRY`,
:class:`ArtifactDeclaration`, :data:`PERSISTENCE_CLASSES`,
:data:`OPERATIONAL_SCOPES`), and :func:`append_line_durable` are importable
but not part of the public contract; their signatures may change between
minor releases. The S1 conformance suite is published too, as a runnable
third-party adapter-authoring harness — see :mod:`athenaeum.store_conformance`
(design note §6, preamble) — separately from this module because it depends
on ``pytest``, a ``dev``-extra-only dependency this always-imported module
must not acquire.

Layering: L0/L1 (design note §6.4), and DELIBERATELY LOWER than the design
note's own layering paragraph describes. §6.4 says this module "inherits
storage.py's documented exception ... reach[ing] up to L2 config for adapter
and mapping resolution" and that ``resolve_store_for_class`` therefore lives
here, importing :mod:`athenaeum.storage`. That would create a real edge back
INTO this module from ``storage.py`` (which needs :class:`FilesystemStore` to
implement its side of the wrapper) — and this repo's import-graph guard
(``tests/test_import_graph_acyclic.py``, baseline pinned to ``[]`` since issue
athenaeum#640) counts function-local/deferred imports as graph edges too, so
not even a call-time-deferred import escapes it: any edge in either direction
between two mutually-importing modules is a 2-node SCC, a hard CI-blocking
regression. Concretely this means:

* This module has **no** import of :mod:`athenaeum.storage` — not even
  deferred. :class:`FilesystemStore` therefore knows nothing about
  ``storage.py``'s adapter/mapping config model; it takes an explicit
  ``roots: Mapping[str, Path]`` (surface name → resolved root) at
  construction instead (see :class:`FilesystemStore`), so surface → root
  resolution is supplied by the caller rather than looked up here.
* ``resolve_store_for_class`` is therefore defined in :mod:`athenaeum.storage`
  (module-level import of THIS module, one direction only — see that
  function's docstring), not here, so there is exactly one implementation
  (D5: "the existing seam is extended, never forked") and no second front
  door.

Only real imports: :mod:`athenaeum.atomic_io` (L0) for the atomic write
primitive, and :mod:`athenaeum.models` (L1) for
:func:`~athenaeum.models.parse_frontmatter` (``iter_records``' "list + read +
parse" convenience) — both stdlib/yaml-only, so this module stays strictly
import-light: no ``search``, ``librarian``, ``mcp_server``, or third-party
heavy deps (pydantic, anthropic, chromadb). ``tests/test_store_layering.py``
asserts both the import list and the one-directional edge mechanically.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

try:  # pragma: no cover - exercised via monkeypatch in athenaeum.runlock's tests
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter

# ---------------------------------------------------------------------------
# Shared durable-append primitive (design note §4.6 / §6.2, §2.4; issue
# athenaeum#980, slice S5)
# ---------------------------------------------------------------------------


def append_line_durable(path: Path, line: bytes) -> None:
    """``O_APPEND`` + ``fsync``: append *line* to *path*, creating the parent
    directory and the file itself if needed.

    THE single implementation of the primitive design note §2.4 found
    duplicated across "12 modules, no shared implementation" — a plain
    ``O_APPEND`` write of one small record is atomic on local filesystems, so
    a crash can at worst leave a torn TRAILING line (every reader in this
    codebase already tolerates that), never corrupt an already-written
    record. :meth:`FilesystemStore.append` is one caller; every per-module
    ``_append_line``/``_append_jsonl_line``/``_append_ledger_line`` helper
    this slice migrated (issue athenaeum#980 AC3) is another — collapsing
    what used to be 12+ independent copies of this exact body onto one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Shared UTC-ISO timestamp rendering (issue athenaeum#1348)
# ---------------------------------------------------------------------------


def now_iso(when: datetime | None = None) -> str:
    """Render *when* (or the current UTC instant, if omitted) as a UTC ISO-8601
    timestamp at **second precision**: ``"%Y-%m-%dT%H:%M:%SZ"`` (e.g.
    ``"2026-09-03T19:12:33Z"`` — no fractional seconds).

    THE single implementation of the UTC-ISO rendering rule issue athenaeum#1348
    found duplicated across 45 call sites and 13 independent module-private
    ``_now_iso()`` definitions, split between a microsecond-precision
    rendering (``datetime.isoformat()`` with its ``+00:00`` offset suffix
    swapped for a bare ``Z``) and a second-precision one (``datetime.strftime``
    with an explicit second-precision-then-``Z`` format string) — one of
    which, the second-precision form, is also the exact rendering every
    caller across the package that parses this value back with
    ``datetime.strptime("%Y-%m-%dT%H:%M:%SZ")`` expects. Second precision
    is the rendering kept here (not widened to also accept microseconds)
    because pinning the format in ONE place — rather than teaching every
    parser two shapes — is what makes a future drift back to microsecond
    rendering fail loudly instead of silently: any writer that reaches for
    this helper can no longer produce a timestamp a strict parser rejects.
    Mirrors :func:`append_line_durable` (issue athenaeum#980) as the
    package's shared-primitive home for this class of drift.

    A naive *when* (no ``tzinfo``) is assumed to already be UTC — the
    convention every existing caller in this package uses when it constructs
    one via ``datetime.now(timezone.utc)`` and later strips or never sets
    ``tzinfo``. An aware *when* is converted to UTC via
    :meth:`datetime.astimezone` rather than assumed.
    """
    dt = when if when is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreKeyError(ValueError):
    """Raised when a :class:`StoreKey`'s ``key`` is not a valid POSIX-relative key.

    ``key`` must never be an OS path (design note §6.2 D2): no leading ``/``,
    no ``\\``, and no ``.``/``..``/empty path segments, since those all carry
    filesystem-specific meaning a non-filesystem adapter cannot honor.
    """


class StoreConflictError(RuntimeError):
    """Raised when a ``put``/``delete`` compare-and-swap precondition (``expect=``)
    is not met.

    Not named by the design note's contract text, which specifies the CAS
    *semantics* (§2.5: ``put(..., expect=None)`` is exclusive create; §6.2 D3:
    versions are opaque tokens compared for equality) without naming an
    exception type. This is S1's conservative fill: one exception type, raised
    by both ``put`` and ``delete`` on any precondition mismatch, on both
    implementations in this slice.
    """


class LeaseHeldError(RuntimeError):
    """Raised by :class:`FileLease`/:meth:`FilesystemStore.lease` when a lease
    cannot be acquired non-blocking and ``force`` was not requested (issue
    athenaeum#979, S4).

    Carries the same holder metadata as :class:`athenaeum.runlock.LockHeld`
    (``holder``, parsed by :func:`read_holder`) without being that class —
    this module cannot import :mod:`athenaeum.runlock` (see the module
    docstring's layering note), so ``RunLock`` catches this at its own call
    site and re-raises its own ``LockHeld`` for its callers, unchanged.
    """

    def __init__(self, name: str, holder: dict[str, str] | None = None) -> None:
        self.name = name
        self.holder = holder or {}
        super().__init__(
            f"lease {name!r} is held"
            + (f" by {self.holder}" if self.holder else " (no holder metadata)")
        )


class StoreCapabilityError(RuntimeError):
    """Raised when an operation needs a capability the target surface does not
    declare (design note §4.4 R1 / §4.5 R2, issue athenaeum#2615).

    The case it exists for: a destructive operation (``delete``, or the source
    side of a ``move``) on a surface that is neither ``versioned`` (so the
    change is recoverable from git) nor ``purgeable`` (so erasure is the
    point). Doing it anyway would be the silent, unrecoverable delete R1
    forbids, so the store refuses, loudly (D6).
    """


class ReadOnlySurfaceError(StoreCapabilityError):
    """Raised when a write is attempted on a surface declared ``read_only``
    (athenaeum#2621).

    A read-only surface is a mount of content this install consumes but never
    changes (a shared git clone). Every write path refuses it, loudly, through
    this one named error. Re-exported by :mod:`athenaeum.storage`, which also
    owns the path-based guard (:func:`athenaeum.storage.require_writable_path`)
    for the writers that write through a ``Path`` rather than the store.
    """


class UnknownSurfaceError(KeyError):
    """Raised by :class:`FilesystemStore` for a ``StoreKey.surface`` absent
    from the ``roots`` mapping it was constructed with.

    The store contract itself has no concept of "known surfaces" — that is
    entirely up to whatever builds a :class:`FilesystemStore`'s ``roots``
    mapping (e.g. :func:`athenaeum.storage.resolve_store_for_class`, which
    resolves it from ``storage.mapping``/``storage.adapters`` and raises its
    own :class:`athenaeum.storage.StorageConfigError` earlier, at *class*
    resolution time, before a :class:`FilesystemStore` even exists). This is
    the later, *surface*-resolution-time counterpart for a ``StoreKey`` whose
    surface was never in the roots this store was given — not part of the
    design note's §6.2 contract text, and a local, honest substitute for a
    silent ``KeyError`` (this module has no import of :mod:`athenaeum.storage`
    to reuse that module's error type — see the module docstring's layering
    note).
    """


# ---------------------------------------------------------------------------
# Core value types (design note §6.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreKey:
    """A surface plus a POSIX-style relative key (design note §6.2 D2).

    ``surface`` is a storage-adapter surface name (an
    :class:`athenaeum.storage.StorageAdapter.name`, e.g.
    ``"wiki-markdown-embedded"`` or ``"excluded"``) — never an on-disk root.
    ``key`` is validated at construction so an OS path can never masquerade as
    a key.
    """

    surface: str
    key: str

    def __post_init__(self) -> None:
        _validate_relative_key(self.key)


def _validate_relative_key(key: str) -> None:
    if not key or key.startswith("/") or "\\" in key:
        raise StoreKeyError(
            f"invalid StoreKey.key {key!r}: must be a non-empty POSIX-relative key"
        )
    if any(segment in ("", ".", "..") for segment in key.split("/")):
        raise StoreKeyError(
            f"invalid StoreKey.key {key!r}: no empty, '.', or '..' path segments"
        )


@dataclass(frozen=True)
class ObjectMeta:
    """Listing metadata for one object — the unit :func:`Store.iter_meta` yields.

    ``version`` is an opaque token (design note §6.2 D3): compared for
    equality only, never parsed, never assumed to be a timestamp (§3.5 P2).
    """

    key: StoreKey
    version: str
    size: int


@dataclass(frozen=True)
class Record:
    """One decoded object: its metadata plus parsed frontmatter and body.

    Referenced by :func:`Store.iter_records`'s signature in design note §6.2
    but not itself defined there — the contract's prose is explicit about what
    it collapses ("``iter_meta`` + ``read_many`` + ``parse_frontmatter`` is
    what nearly every caller does", §6.2), so this is S1's conservative
    reading of that sentence into a concrete shape rather than invented scope.
    """

    meta: ObjectMeta
    frontmatter: dict[str, Any]
    body: str


@dataclass(frozen=True)
class Lease:
    """A held lease, returned (as a context manager value) by ``Store.lease()``.

    Referenced by :func:`Store.lease`'s signature in design note §6.2 but not
    itself defined there, for the same reason as :class:`Record`. ``token`` is
    the ISO-8601 acquire timestamp on :class:`FilesystemStore` (S4,
    athenaeum#979) — opaque per D3, like every other version/token this module
    hands out; do not parse it as a timestamp.
    """

    name: str
    token: str


# ---------------------------------------------------------------------------
# Lease primitive internals (design note §6.2 ``lease``; issue athenaeum#979,
# slice S4). MOVED (not copied) from ``athenaeum.runlock.RunLock`` — the
# flock-open, flock-attempt, inode-race guard, and lockfile metadata read/write
# bodies are exactly what backed ``RunLock.acquire``/``release``/``heartbeat``
# before this slice, generalized from a hardcoded ``knowledge_root/.athenaeum.lock``
# to an arbitrary ``lockfile: Path`` (see the module docstring's "lease
# primitive" section for the full relationship to ``athenaeum.runlock``, which
# imports every name below and supplies its own wait/force/staleness POLICY on
# top of them — none of that policy is duplicated here).
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True if *pid* names a live process on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still a live process.
        return True
    except OSError:
        return False
    return True


def _parse_iso_age_seconds(iso_ts: str | None) -> float | None:
    """Age in seconds of an ISO-8601 timestamp, or ``None`` if unparseable."""
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def read_holder(lockfile: Path) -> dict[str, str] | None:
    """Parse the ``key: value`` holder metadata from *lockfile*.

    Returns ``None`` when the file is absent or carries no parseable metadata.
    """
    try:
        text = lockfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    holder: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            holder[key.strip()] = value.strip()
    return holder or None


def is_stale(lockfile: Path) -> bool:
    """True if *lockfile* names a PID that is no longer alive.

    Diagnostic only — the kernel already released a dead holder's ``flock``,
    so this does not gate anything here; see ``athenaeum.runlock``'s module
    docstring ("Reading a residual lockfile") for the full reasoning this was
    moved from verbatim.
    """
    holder = read_holder(lockfile)
    if not holder:
        return False
    pid_raw = holder.get("pid")
    if not pid_raw:
        return False
    try:
        pid = int(pid_raw)
    except ValueError:
        return False
    return not _pid_alive(pid)


def heartbeat_age_seconds(lockfile: Path) -> float | None:
    """Age in seconds of the holder's effective heartbeat.

    Prefers the ``heartbeat:`` line; falls back to ``timestamp:`` when absent
    (lockfiles written before the heartbeat field existed).
    """
    holder = read_holder(lockfile)
    if not holder:
        return None
    iso_ts = holder.get("heartbeat") or holder.get("timestamp")
    return _parse_iso_age_seconds(iso_ts)


def lease_open_fd(lockfile: Path) -> int:
    """Open (creating if absent) the fd a lease attempt ``flock``s."""
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    return os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)


def lease_try_flock(fd: int) -> bool:
    """Non-blocking ``flock`` attempt on *fd*; ``True`` on success."""
    assert fcntl is not None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def lease_holds_current_inode(fd: int, lockfile: Path) -> bool:
    """True if *fd* refers to the inode currently at *lockfile*.

    After a break (``force=True``) unlinks and re-creates the lockfile, a
    descriptor opened *before* the break refers to an orphan inode: ``flock``
    on it still succeeds (nothing holds the orphan), but any metadata written
    lands in a file no longer at the lock path, so two holders could each
    believe they hold the lease on two different inodes (issue athenaeum#526,
    finding M6). Comparing ``fstat``/``stat`` ``st_ino`` catches that. Any
    ``OSError`` (e.g. the lockfile was unlinked mid-check) means the
    descriptor is not the current inode.
    """
    try:
        return os.fstat(fd).st_ino == os.stat(lockfile).st_ino
    except OSError:
        return False


def lease_write_metadata(fd: int) -> str:
    """Truncate *fd* and write this holder's diagnostics. Returns the ISO-8601
    acquire timestamp written (also the ``timestamp:`` line's value)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = (
        f"pid: {os.getpid()}\n"
        f"timestamp: {now_iso}\n"
        f"host: {socket.gethostname()}\n"
        f"heartbeat: {now_iso}\n"
    )
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
    os.fsync(fd)
    return now_iso


def lease_refresh_heartbeat(fd: int, acquired_at: str) -> None:
    """Rewrite only the ``heartbeat:`` line on *fd*, keeping ``pid``/``timestamp``/
    ``host`` (``timestamp`` pinned to *acquired_at*, the original acquire time).

    **Never shrinks the file, and never calls ``ftruncate`` (issue
    athenaeum#2229).** The previous implementation did ``ftruncate(fd, 0)``
    then ``write(payload)``, which opens a window where a contending reader
    with no flock (:func:`read_holder`, used to render a ``LockHeld``
    message) can land between the truncate and the write and see an empty
    file. Writing the new payload before any size change closes that
    specific window, but is not sufficient on its own: the ISO-8601
    ``heartbeat`` timestamp occasionally drops its fractional-second suffix
    when the microsecond happens to be exactly 0, so a later payload can be a
    few bytes SHORTER than what is already on disk. Overwriting in place
    with a shorter buffer leaves the old tail (bytes beyond the new
    payload's length) on disk until something truncates it off, so a
    truncate-after-write reopens the identical kind of window, just for the
    tail: a reader could land in it and see ``new_prefix + old_tail``, which
    is exactly as malformed as the all-empty case.

    So instead of ever truncating, a payload shorter than the file's
    CURRENT on-disk size is padded with trailing blank lines up to that
    size before the single ``os.write`` call. ``read_holder``'s line parser
    only treats a line with a ``:`` as a key/value pair, so trailing blank
    lines are inert and the lockfile format (the ``key: value`` lines
    themselves) is unaffected. A payload longer than the current size needs
    no special handling: ``os.write`` past the current end-of-file extends
    the file as part of the same call. Either way there is exactly one
    ``os.write`` and the file's size never decreases, so a reader's
    ``read_text()`` always lands either entirely before it (complete OLD
    payload, possibly with OLD padding) or entirely after it (complete NEW
    payload, possibly with NEW padding) — never a mix. This relies on a
    single small (well under one page) ``os.write`` being atomic with
    respect to a concurrent whole-file read on a local filesystem, the same
    assumption the issue's own suggested fix ("write the new content first")
    already makes.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = (
        f"pid: {os.getpid()}\n"
        f"timestamp: {acquired_at}\n"
        f"host: {socket.gethostname()}\n"
        f"heartbeat: {now_iso}\n"
    )
    data = payload.encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    current_size = os.fstat(fd).st_size
    if len(data) < current_size:
        data += b"\n" * (current_size - len(data))
    written = os.write(fd, data)
    if written != len(data):
        # A SHORT write (ENOSPC is the realistic cause) would leave exactly the
        # `new_prefix + old_tail` state the single-write design exists to make
        # impossible, so it must be loud rather than silently persisted. Not
        # retried: a second write would itself be a second window.
        raise OSError(
            f"partial heartbeat write: {written} of {len(data)} bytes "
            "reached the lockfile"
        )
    os.fsync(fd)


def lease_break_lockfile(lockfile: Path) -> None:
    """Unlink *lockfile* so a fresh ``flock`` inode can be acquired.

    No-op (not an error) when already absent.
    """
    try:
        os.unlink(lockfile)
    except FileNotFoundError:
        pass


class FileLease:
    """The concrete ``AbstractContextManager[Lease]`` :meth:`FilesystemStore.lease`
    returns (issue athenaeum#979, S4).

    A single acquire attempt per instance: non-blocking, with an optional
    unconditional ``force`` break first. No poll loop and no
    heartbeat-staleness auto-break — those are ``athenaeum.runlock.RunLock``'s
    policy, layered on top by calling this repeatedly (see the module
    docstring). Raises :class:`LeaseHeldError` from ``__enter__`` when
    contended. ``fd`` is exposed (read-only, set only while held) so a caller
    that needs the raw descriptor — ``RunLock`` mirrors it onto its own
    ``_fd`` for its existing test-observable surface — can get it without
    reaching into a private attribute.
    """

    def __init__(self, lockfile: Path, *, force: bool = False) -> None:
        self._lockfile = lockfile
        self._force = force
        self._fd: int | None = None
        self._acquired_at: str | None = None

    @property
    def fd(self) -> int | None:
        return self._fd

    def __enter__(self) -> Lease:
        if fcntl is None:
            raise RuntimeError(
                "FilesystemStore.lease() requires POSIX fcntl; unavailable on "
                "this platform"
            )
        if self._try_claim():
            return Lease(name=self._lockfile.name, token=self._acquired_at or "")
        if self._force and self._try_claim(force=True):
            return Lease(name=self._lockfile.name, token=self._acquired_at or "")
        raise LeaseHeldError(self._lockfile.name, read_holder(self._lockfile))

    def _try_claim(self, *, force: bool = False) -> bool:
        """One open+flock(+inode-check) attempt; optionally break first.

        Returns ``True`` (and sets ``self._fd``/``self._acquired_at``) only
        when this instance now genuinely holds the CURRENT inode's ``flock``.
        Any failure closes its own fd before returning ``False`` — never
        leaks a descriptor.
        """
        if force:
            lease_break_lockfile(self._lockfile)
        fd = lease_open_fd(self._lockfile)
        if lease_try_flock(fd) and lease_holds_current_inode(fd, self._lockfile):
            self._fd = fd
            self._acquired_at = lease_write_metadata(fd)
            return True
        os.close(fd)
        return False

    def heartbeat(self) -> None:
        """Refresh the lockfile's ``heartbeat`` line. No-op when not held."""
        if self._fd is None:
            return
        lease_refresh_heartbeat(self._fd, self._acquired_at or "")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


#: The four R3 persistence classes (design note §5.2).
PERSISTENCE_CLASSES = frozenset({"source", "derived", "operational", "config"})

#: The two R3 operational scopes (design note §5.2).
OPERATIONAL_SCOPES = frozenset({"store-durable", "machine-local"})


@dataclass(frozen=True)
class ArtifactDeclaration:
    """One store artifact's R3 persistence-class declaration (design note
    §5.2, issue athenaeum#980, slice S5).

    ``__post_init__`` is the enforcement S1 left inert (this module's
    docstring: "the R3 persistence-class enforcement §5.2 defines (S5,
    athenaeum#980)"): every declaration names exactly one
    :data:`PERSISTENCE_CLASSES` member (a required ``str`` field, so "more
    than one" is not representable), and an ``operational`` declaration
    additionally names exactly one :data:`OPERATIONAL_SCOPES` member — never
    zero, never both, and never a scope on a non-``operational`` artifact.

    Not itself part of the :class:`Store` protocol — a catalogue the
    conformance/enumeration test (``tests/test_artifact_registry.py``) reads,
    not something a caller constructs at runtime.
    """

    #: Short, stable identifier — the artifact's filename/constant where one
    #: exists, otherwise a descriptive slug (design note citation in
    #: ``source_ref`` disambiguates).
    name: str
    persistence_class: str
    operational_scope: str | None
    #: Human-readable root the artifact resolves under today: ``"wiki root"``,
    #: ``"raw root"``, ``"cache dir"``, ``"knowledge root"``, or
    #: ``"excluded surface root"``.
    location: str
    #: ``module.py:CONSTANT_NAME`` (or ``module.py:<literal>`` where no named
    #: constant exists) plus the design note table/paragraph this row
    #: transcribes.
    source_ref: str
    #: Base names the artifact is stored under, where it is one file with a fixed
    #: name. Empty when the artifact is a directory, a pattern or an operator-chosen
    #: name. Read by the S6b check that a machine-local file is not tracked
    #: (athenaeum#2994).
    file_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.persistence_class not in PERSISTENCE_CLASSES:
            raise ValueError(
                f"artifact {self.name!r}: persistence_class "
                f"{self.persistence_class!r} not in {sorted(PERSISTENCE_CLASSES)}"
            )
        if self.persistence_class == "operational":
            if self.operational_scope not in OPERATIONAL_SCOPES:
                raise ValueError(
                    f"artifact {self.name!r}: class 'operational' requires "
                    f"operational_scope in {sorted(OPERATIONAL_SCOPES)}, got "
                    f"{self.operational_scope!r}"
                )
        elif self.operational_scope is not None:
            raise ValueError(
                f"artifact {self.name!r}: class {self.persistence_class!r} must not "
                f"declare an operational_scope (got {self.operational_scope!r})"
            )


#: Every store artifact design note §5.1/§5.2/§2.3.1 names, declaring its R3
#: class (issue athenaeum#980, slice S5). This is the enumeration
#: ``tests/test_artifact_registry.py`` walks for AC1/AC2/AC6.
#:
#: ``source`` and ``derived`` membership is §5.1's confirmed boundary (raw +
#: wiki authoritative, indexes derived) plus the concrete cache-dir index
#: artifacts §5.2 names as the ``derived`` example. ``operational`` membership
#: is §5.2's table, one row per artifact rather than per table row so each
#: has its own declaration. ``config`` membership is the R3 box's own list
#: (§5.2: "operator-authored declarations (``rules/``, ``templates/``, the
#: authority manifest, ``athenaeum.yaml``)") — the narrower, authoritative
#: definition, not §2.3.1's wider sweep-finding paragraph that also names
#: ``registry.json``, ``compiled-exempt.json`` and the preserved-log
#: directory; those three are classified individually below against what
#: they actually are (see each entry's ``source_ref`` for the reasoning).
ARTIFACT_REGISTRY: tuple[ArtifactDeclaration, ...] = (
    # -- source (design note §5.1) ---------------------------------------
    ArtifactDeclaration(
        name="raw-intake",
        persistence_class="source",
        operational_scope=None,
        location="raw root",
        source_ref="intake.py (design note §5.1: 'raw ... authoritative')",
    ),
    ArtifactDeclaration(
        name="compiled-wiki-pages",
        persistence_class="source",
        operational_scope=None,
        location="wiki root",
        source_ref="librarian.py (design note §5.1: 'wiki authoritative')",
    ),
    ArtifactDeclaration(
        name="preserved-log-area",
        persistence_class="source",
        operational_scope=None,
        location="knowledge root (operator-configured subdirectory)",
        source_ref=(
            "rules.py:987-1023 preserved_log_source_pointer/move-into-preserved-area "
            "(design note §2.3.1 names the directory; classified 'source' here, not "
            "'config', because the directory HOLDS preserved raw log content — a "
            "retained source document, design note §5.1 — not an operator-authored "
            "behavioural declaration)"
        ),
    ),
    # -- derived (design note §5.1, §5.2 cache-dir 'yes' row) ------------
    ArtifactDeclaration(
        name="fts5-index-db",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="search.py:269 _DB_NAME (design note §5.1/§5.2 cache-dir 'yes' row)",
    ),
    ArtifactDeclaration(
        name="vector-collection",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="search.py:1335 _VECTOR_COLLECTION (design note §5.1)",
    ),
    ArtifactDeclaration(
        name="fts5-manifest",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="search.py:436 _FTS5_MANIFEST (design note §5.2 cache-dir 'yes' row)",
    ),
    ArtifactDeclaration(
        name="vector-manifest",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="search.py:437 _VECTOR_MANIFEST (design note §5.2 cache-dir 'yes' row)",
    ),
    ArtifactDeclaration(
        name="vector-generation-stamp",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="search.py:1342 _VECTOR_GENERATION (design note §5.1)",
    ),
    ArtifactDeclaration(
        name="ingest-manifest",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref=(
            "librarian.py:5878 INGEST_MANIFEST_NAME (design note §5.2 cache-dir "
            "'yes' row: 'ingest ... manifests ... yes — from a full rebuild')"
        ),
    ),
    ArtifactDeclaration(
        name="auto-memory-manifest",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir",
        source_ref="librarian.py:6076 AUTO_MEMORY_MANIFEST_NAME (design note §5.2 "
        "cache-dir 'yes' row)",
    ),
    # -- operational / store-durable (design note §5.2 table) -----------
    ArtifactDeclaration(
        name="pending-questions",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="answers.py '_pending_questions.md' (design note §5.2 table row 1)",
    ),
    ArtifactDeclaration(
        name="pending-questions-archive",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="answers.py:24 '_pending_questions_archive.md' (design note §5.2 "
        "table row 1, '+ archives')",
    ),
    ArtifactDeclaration(
        name="pending-merges",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="pending_merges.py '_pending_merges.md' (design note §5.2 table row 1)",
    ),
    ArtifactDeclaration(
        name="pending-merges-archive",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="pending_merges.py:63 '_pending_merges_archive.md' (design note §5.2 "
        "table row 1, '+ archives')",
    ),
    ArtifactDeclaration(
        name="quarantine-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="quarantine.py:81 QUARANTINE_LEDGER_FILENAME (design note §5.2 table row 2)",
    ),
    ArtifactDeclaration(
        name="merge-provenance-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="provenance.py:427 MERGE_PROVENANCE_FILENAME (design note §5.2 table row 3)",
    ),
    ArtifactDeclaration(
        name="pending-retractions-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="retraction_cascade.py:64 RETRACTION_REVIEW_FILENAME (design note "
        "§5.2 table row 4)",
    ),
    ArtifactDeclaration(
        name="calibration-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="calibration.py:61 CALIBRATION_LEDGER_FILENAME (design note §5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="reasoning-tier-decisions-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="reasoning_tiers.py:368 REASONING_TIER_LOG_FILENAME (design note "
        "§5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="axiom-governance-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="axiom_governance.py:92 AXIOM_LEDGER_FILENAME (design note §5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="corrections-applied-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="corrections.py:1843 CORRECTIONS_LEDGER_FILENAME (design note §5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="note-corrections-applied-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref=(
            "note_corrections.py NOTE_CORRECTIONS_LEDGER_FILENAME (issue athenaeum#1976; "
            "sibling of the corrections-applied-ledger row immediately above, same "
            "house-style duplicated appender §2.4 counts, not itself named in the "
            "design note §5.2 table)"
        ),
    ),
    ArtifactDeclaration(
        name="shape-rules-applied-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="rules.py:1143 SHAPE_RULES_LEDGER_FILENAME (design note §5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="shape-rule-dispositions-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref="rules.py:1183 SHAPE_RULE_DISPOSITIONS_FILENAME (design note §5.2 table row 5)",
    ),
    ArtifactDeclaration(
        name="rule-proposals-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root",
        source_ref=(
            "rule_proposals.py:140 RULE_PROPOSALS_LEDGER_FILENAME (sibling of the "
            "design note §5.2 table row 5 ledgers, same house-style duplicated "
            "appender §2.4 counts; not itself named in the table's prose but "
            "identical in shape and location)"
        ),
    ),
    ArtifactDeclaration(
        name="resolved-contradictions-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="raw root",
        source_ref="fingerprint.py:61 RESOLVED_CONTRADICTIONS_RELPATH (design note "
        "§5.2 table row 6)",
    ),
    ArtifactDeclaration(
        name="observations-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="excluded surface root",
        source_ref="pii.py:1306 OBSERVATION_LOG_FILENAME (design note §5.2 table row 7)",
    ),
    ArtifactDeclaration(
        name="observation-supersessions-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="excluded surface root",
        source_ref="pii.py:1312 SUPERSESSION_LOG_FILENAME (design note §5.2 table row 7)",
    ),
    # Issue athenaeum#2468 removed the "llm-schema-observations-ledger" entry
    # that used to sit here (``_llm_schema_observations.jsonl``): nothing
    # outside that module's own tests ever read the ledger, so the writer,
    # its readers, and this registry row were all removed together.
    ArtifactDeclaration(
        name="spend-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "spend.py LEDGER_FILENAME 'spend.jsonl' (design note §5.2 table row 8 "
            "'spend.jsonl'). Issue athenaeum#980 AC4 relocated this ledger to wiki "
            "root; issue athenaeum#1601 (operator decision, 2026-09-15) WITHDREW that "
            "relocation and returned it to the cache dir, the same fix issue "
            "athenaeum#1591 made for push-records-ledger. R3's class/scope declaration "
            "is unchanged — 'store-durable' never implied 'wiki root'. "
            "spend.durable_ledger_path() now always resolves to the cache dir and "
            "ignores its wiki_root argument entirely; issue athenaeum#2467 removed "
            "the one-time stranded-ledger warning that argument used to feed. "
            "Every production write AND read call site still passes wiki_root= "
            "(librarian.py x3, drain.py, _cmd_drain.py, _cmd_lifecycle.py, "
            "status.py, backlog_price_sheet.py, ordinary_night_table.py, "
            "answers.py, query_topics.py + _cmd_query.py, "
            "memory_class_backfill.py), purely for call-signature stability now — "
            "see tests/test_spend.py::TestDurableLedgerPath::test_matrix_wiki_path_populated"
        ),
    ),
    ArtifactDeclaration(
        name="push-records-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "push_metrics.py PUSH_RECORDS_FILENAME (design note §5.2 table row 8 "
            "'_push_records.jsonl'). Issue athenaeum#980 AC4 relocated this ledger to "
            "wiki root; issue athenaeum#1591 WITHDREW that relocation and returned it "
            "to the cache dir. R3's class/scope declaration is unchanged — what changed "
            "is that 'store-durable' never implied 'wiki root' (see "
            "push-references-ledger below, store-durable in the cache dir since "
            "athenaeum#980 "
            "itself), and issue athenaeum#749's acceptance ('outside the wiki corpus, "
            "so they never become claims and never enter the embedded index') was "
            "never retired — PR athenaeum#1080's own 'Ambiguities I resolved' §3 "
            "honoured the "
            "identical constraint for _decay_sweep_records.jsonl (athenaeum#969) and "
            "_push_references.jsonl. push_metrics.durable_push_records_path() now "
            "always resolves to the cache dir and ignores its wiki_root argument "
            "entirely; issue athenaeum#2467 removed the one-time stranded-ledger "
            "warning that argument used to feed (and, with it, this module's own "
            "_has_migrated_content() copy, which had no other caller) — see "
            "tests/test_push_metrics.py::TestDurablePushRecordsPath"
        ),
    ),
    ArtifactDeclaration(
        name="push-references-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "push_metrics.py:82 REFERENCE_RECORDS_FILENAME '_push_references.jsonl' — "
            "sibling of push-records-ledger (same module, same durable/not-reconstructible "
            "shape) but not itself named in design note §5.2's table, so classified "
            "without relocation"
        ),
    ),
    ArtifactDeclaration(
        name="raise-acceptance-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "raise_acceptance_ledger.py ACCEPTANCE_LEDGER_FILENAME "
            "'_raise_acceptance.jsonl' (issue athenaeum#2938; "
            "docs/design/federation-raise-retract.md §4.4, §6.2, slice S5) — one "
            "disposition row per raise message (accepted, rejected with a reason "
            "code, or held), ids and metadata only: no page text and no source "
            "page identifier. Durable and not reconstructible, the install's own "
            "state in the cache dir, outside the wiki and raw roots so it is "
            "never a page and never enters the recall index. Same shape as "
            "push-records-ledger and drop-ledger; keeping it on a bench is a "
            "later slice (S14)"
        ),
    ),
    ArtifactDeclaration(
        name="raise-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "raise_ledger.py RAISE_LEDGER_FILENAME '_raise_ledger.jsonl' (issue "
            "athenaeum#3001; docs/design/federation-raise-retract.md §4.3, §7.1, "
            "slice S4) — the raiser's record of each raise, keyed by raise_id and "
            "seq: ids and a payload hash, no page text. Durable and not "
            "reconstructible (the mapping from a raise_id to its source page "
            "exists nowhere else), the install's own state in the cache dir, "
            "outside the wiki and raw roots so it is never a page of any store "
            "and never enters the recall index. A record file merged by record, "
            "never as text; keeping it on a bench is a later slice (S14)"
        ),
    ),
    ArtifactDeclaration(
        name="store-review-state",
        persistence_class="operational",
        operational_scope="store-durable",
        location="knowledge root",
        source_ref=(
            "store_reviews.py:REVIEW_STATE_DIRNAME '_store_reviews/<store_id>.jsonl' "
            "(issue athenaeum#2992; docs/design/federation-store-access.md §8, slice "
            "S10) — one run record per completed cross-store review: review name, "
            "source store id, time, the source revision reviewed (the cursor) and "
            "integer counts. No source text. Durable and not reconstructible, the "
            "install's own state outside every store's wiki and raw roots; keeping "
            "it on a bench is a later slice (S8)"
        ),
    ),
    ArtifactDeclaration(
        name="drop-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "drops.py:DROP_LEDGER_FILENAME '_drops.jsonl' (issue athenaeum#2609) — "
            "one row per raw file the entity loop consumed without creating or "
            "updating anything (ref, capped+PII-masked excerpt, drop stage, "
            "reason, run id, timestamp). Durable and not reconstructible — once a "
            "dropped raw file is deleted (the default ``librarian.drop_action`` — "
            "or re-evaluated past the ``athenaeum.never_ingest`` manifest's own "
            "window), this ledger is the only record the content ever existed —"
            " same shape as push-records-ledger/push-references-ledger above, not"
            " itself named in design note §5.2's table, so classified without "
            "relocation"
        ),
    ),
    ArtifactDeclaration(
        name="drop-quarantine",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "drops.py:DROP_QUARANTINE_DIRNAME 'drop-quarantine' (issue "
            "athenaeum#2609) — holds a dropped raw file's ORIGINAL bytes "
            "verbatim when ``librarian.drop_action: quarantine`` is set (the "
            "non-default alternative to today's delete), so ``athenaeum drops "
            "restore`` can recover more than the drop ledger's own capped "
            "excerpt. Sibling of drop-ledger immediately above; same "
            "classification reasoning"
        ),
    ),
    # Issue athenaeum#2468 removed the "never-ingest-refusals-ledger" entry
    # that used to sit here (``_never_ingest_refusals.jsonl``): nothing
    # outside that module's own tests ever read the ledger, so the writer,
    # its reader, and this registry row were all removed together. The
    # never-ingest refusal DECISION itself is unchanged.
    ArtifactDeclaration(
        name="decay-sweep-records-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "decay_sweep.py:100 SWEEP_LEDGER_FILENAME — durable and not "
            "reconstructible, one of the §2.4 duplicated appenders, but outside "
            "§5.2's named table rows, so classified without relocation"
        ),
    ),
    ArtifactDeclaration(
        name="corrections-entity-registry",
        persistence_class="operational",
        operational_scope="store-durable",
        location="knowledge root",
        source_ref=(
            "corrections.py load_registry() 'registry.json' — named in design note "
            "§2.3.1's wider sweep paragraph as 'operator-authored', but its own module "
            "documents it as a machine-maintained entity-handle map built from applied "
            "corrections, not an operator declaration; classified 'operational' against "
            "R3's own box definition (§5.2) rather than 'config'"
        ),
    ),
    ArtifactDeclaration(
        name="compiled-exempt-manifest",
        persistence_class="operational",
        operational_scope="store-durable",
        location="knowledge root",
        source_ref=(
            "compiled_exempt.py:55 COMPILED_EXEMPT_FILENAME 'compiled-exempt.json' — "
            "named in design note §2.3.1's wider sweep paragraph, but its own module "
            "docstring calls it 'a durable decision' recording per-file retain "
            "dispositions, not an operator-authored behavioural declaration; classified "
            "'operational' against R3's own box definition (§5.2) rather than 'config'"
        ),
    ),
    # -- operational / machine-local (design note §5.2 table row 9) -----
    ArtifactDeclaration(
        name="zero-yield-state",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref="zero_yield.py:57 STATE_NAME 'zero_yield_state.json' (design note "
        "§5.2 table row 9)",
        file_names=("zero_yield_state.json",),
    ),
    ArtifactDeclaration(
        name="killswitch-state",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref="killswitch.py:100 state_path() 'disabled' (design note §5.2 table row 9)",
        file_names=("disabled",),
    ),
    ArtifactDeclaration(
        name="store-access-evidence",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref=(
            "store_access_evidence.py STATE_NAME 'store_access_evidence.json' "
            "(access S5, athenaeum#2991: this machine's last fetch and push outcome "
            "per location; describes this machine's access to a host, so it is "
            "machine-local)"
        ),
    ),
    # -- config (design note §5.2 R3 box) --------------------------------
    ArtifactDeclaration(
        name="shape-rules",
        persistence_class="config",
        operational_scope=None,
        location="knowledge root",
        source_ref="rules.py:740 'rules/*.yaml' (design note §5.2 R3 box, issue athenaeum#980 AC5)",
    ),
    ArtifactDeclaration(
        name="entity-templates",
        persistence_class="config",
        operational_scope=None,
        location="knowledge root",
        source_ref="init.py 'templates/' (design note §5.2 R3 box, issue athenaeum#980 AC5)",
    ),
    ArtifactDeclaration(
        name="authority-manifest",
        persistence_class="config",
        operational_scope=None,
        location="knowledge root",
        source_ref="authority.py:257,260 'authority-manifest.yaml' (design note §5.2 "
        "R3 box, issue athenaeum#980 AC5)",
    ),
    ArtifactDeclaration(
        name="athenaeum-config",
        persistence_class="config",
        operational_scope=None,
        location="knowledge root",
        source_ref="config.py:185 'athenaeum.yaml' (design note §5.2 R3 box, issue "
        "athenaeum#980 AC5)",
    ),
    # -- store declaration (issue athenaeum#2772) -------------------------
    # Operator-authored and versioned with the pages, so a copy changed on both
    # sides is held for a person and never merged by rule.
    ArtifactDeclaration(
        name="store-access-declaration",
        persistence_class="config",
        operational_scope=None,
        location="surface root (_store/access.yaml)",
        source_ref="store_access.py DECLARATION_RELPATH (design note "
        "federation-store-access §5, issue athenaeum#2772)",
    ),
    ArtifactDeclaration(
        name="store-review-criteria",
        persistence_class="config",
        operational_scope=None,
        location="surface root (_store/reviews/<name>.md)",
        source_ref="store_access.py (design note federation-store-access §8, "
        "issue athenaeum#2772)",
    ),
    # -- off-corpus purgeable surface (design note §8, issue athenaeum#984) --
    # AC4: the purgeable surface's derived/operational artifacts, declared
    # through this SAME R3 catalogue every other store artifact declares
    # through — not a second abstraction. Both index shards are reconstructible
    # from off-corpus content (a rebuild), matching the main corpus index
    # shards' 'derived' classification above; the ledger shard is durable and
    # not reconstructible, matching the other 'operational'/'store-durable'
    # ledgers above, just physically on the off-corpus purgeable store instead
    # of the wiki root.
    ArtifactDeclaration(
        name="off-corpus-fts5-index-db",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir (off-corpus/ subdirectory)",
        source_ref="off_corpus.py _OFF_CORPUS_CACHE_SUBDIR (issue athenaeum#984 AC1, "
        "design note §8 table row 1: 'a surface declaring purgeable, plus derived "
        "artifacts scoped to it')",
    ),
    ArtifactDeclaration(
        name="off-corpus-vector-collection",
        persistence_class="derived",
        operational_scope=None,
        location="cache dir (off-corpus/ subdirectory)",
        source_ref="off_corpus.py _OFF_CORPUS_CACHE_SUBDIR (issue athenaeum#984 AC1, "
        "design note §8 table row 1)",
    ),
    ArtifactDeclaration(
        name="off-corpus-ledger-shard",
        persistence_class="operational",
        operational_scope="store-durable",
        location="off-corpus purgeable surface "
        "(storage.adapters.<off_corpus.adapter>.surface_root)",
        source_ref="off_corpus.py LEDGER_DIRNAME (issue athenaeum#984 AC3, design note "
        "§8 table row 3: 'an operational/store-durable artifact on a purgeable "
        "surface (R3)')",
    ),
)


@dataclass(frozen=True)
class StoreCapabilities:
    """What a :class:`Store` (really: one of its surfaces) declares it can do.

    Declared, not probed (design note §6.1 D4): a caller checks a flag before
    relying on the capability rather than trying the operation and catching a
    failure. ``local_path_for`` is the one escape hatch (§6.2 D2) and is
    ``None`` on every non-filesystem adapter.
    """

    classes: frozenset[str]
    operational_scopes: frozenset[str]
    versioned: bool
    purgeable: bool
    compare_and_swap: bool
    leases: bool
    append: bool
    bulk_list: bool
    bulk_read: bool
    cheap_local_scan: bool
    local_path_for: Callable[[StoreKey], Path] | None = None
    # Multi-writer sync (athenaeum#2931, design note federation-multi-writer-sync
    # §3.3): the store is versioned by git AND has a remote, so several installs
    # may each write it and be integrated by a merge commit. ``has_remote`` is
    # the fact the invariant needs; it is declared alongside, never inferred.
    has_remote: bool = False
    multi_writer: bool = False

    def __post_init__(self) -> None:
        if self.multi_writer and not (self.versioned and self.has_remote):
            raise ValueError(
                "multi_writer requires a store that is versioned by git and has a remote "
                f"(versioned={self.versioned}, has_remote={self.has_remote})"
            )


# ---------------------------------------------------------------------------
# The protocol (design note §6.2, transcribed verbatim)
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """The whole-store adapter protocol (design note §6.2).

    D1: the unit is a record, not a path — ``iter_records`` fuses list + read
    + parse into one iteration. D2: keys, not paths. D3: versions are opaque
    tokens. D4: capabilities are declared, per surface, and checked. D6: fail
    closed, loudly — every implementation raises rather than degrading
    silently on a precondition mismatch or an unsupported capability.
    """

    capabilities: StoreCapabilities

    # --- reads ---------------------------------------------------------
    def read(self, key: StoreKey) -> bytes: ...

    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]: ...

    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]: ...

    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]: ...

    # --- writes ----------------------------------------------------------
    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str: ...

    def append(self, key: StoreKey, line: bytes) -> None: ...

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool: ...

    def move(self, src: StoreKey, dst: StoreKey) -> None: ...

    # --- recoverability (R1) --------------------------------------------
    def snapshot(self, label: str) -> str | None: ...

    # --- concurrency -----------------------------------------------------
    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]: ...

    # --- lifecycle ---------------------------------------------------------
    def bootstrap(self) -> None: ...


# ---------------------------------------------------------------------------
# FilesystemStore
# ---------------------------------------------------------------------------


def _repo_has_remote(repo: Path) -> bool:
    """True when the git repository at *repo* has at least one remote."""
    result = subprocess.run(
        ["git", "remote"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _commit_all(repo: Path, label: str) -> str | None:
    """``git add -A`` + ``git commit`` in *repo* iff it has changes; return the
    new HEAD SHA, or ``None`` when there was nothing to commit (or no ``.git``)."""
    if not (repo / ".git").exists():
        return None
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        return None
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", label], cwd=str(repo), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit_repo(repo: Path, label: str) -> str | None:
    """Public form of :func:`_commit_all`: commit every change in *repo*.

    Returns the new HEAD SHA, or ``None`` when there was nothing to commit (or
    no ``.git``). Used by the per-type store move (athenaeum#2620) to take its
    own restore points.
    """
    return _commit_all(repo, label)


def commit_paths(
    root: Path, paths: Sequence[Path], message: str, *, allow_empty: bool = False
) -> str | None:
    """Commit exactly *paths* (and nothing else that is staged or modified) in
    the git repository containing *root*; return the new HEAD SHA.

    ``None`` when *root* is not inside a git repository (nothing is committed).
    With *allow_empty* a commit is written even when *paths* are unchanged (a
    decision answered "keep what is published" still leaves a record).
    Raises ``subprocess.CalledProcessError`` when git refuses.
    """
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if top.returncode != 0 or not top.stdout.strip():
        return None
    repo = top.stdout.strip()
    rels = [str(p) for p in paths]
    subprocess.run(["git", "-C", repo, "add", "--", *rels], check=True, capture_output=True)
    try:
        subprocess.run(
            [
                "git", "-C", repo, "commit", "-m", message, "--only",
                *(["--allow-empty"] if allow_empty else []), "--", *rels,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Leave the index as it was (review on PR athenaeum#2984): a path this
        # call staged must not ride along in a later whole-index commit.
        subprocess.run(["git", "-C", repo, "reset", "-q", "--", *rels], capture_output=True)
        raise
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def blob_id(path: Path) -> str:
    """The git object id *path*'s bytes have (``git hash-object``), committed or not."""
    return subprocess.run(
        ["git", "hash-object", "--", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


def repo_head(repo: Path) -> str | None:
    """The HEAD commit SHA of the git repository at *repo*, or ``None`` (no repo / no commit)."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def repo_is_clean(repo: Path) -> bool:
    """Whether *repo* has no modified, staged or untracked files."""
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    )
    return status.returncode == 0 and not status.stdout.strip()


def restore_repo(repo: Path, sha: str) -> None:
    """Return *repo*'s working tree and HEAD to commit *sha* (``reset --hard`` + ``clean -fd``).

    Ignored files are left alone. Raises ``subprocess.CalledProcessError`` on failure.
    """
    subprocess.run(["git", "reset", "--hard", sha], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(repo), check=True, capture_output=True)


# -- Sync read side (athenaeum#2930, design note federation-multi-writer-sync §3.1) --
#
# Read-only git operations a multi-writer sync needs. None writes a commit, moves
# a branch, or touches the working tree; ``fetch_remote_branch`` only adds objects
# and remote-tracking refs. The seam holds no opinion about page content.


def _git_out(repo: Path, *args: str) -> bytes:
    """Run ``git *args`` in *repo*; return stdout bytes. Raises ``CalledProcessError``."""
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True).stdout


def remote_url(repo: Path, remote: str = "origin") -> str | None:
    """The URL of *remote* in *repo*, or ``None`` when it is not set (read-only)."""
    try:
        return _git_out(repo, "remote", "get-url", remote).decode().strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None


def fetch_remote_branch(repo: Path, branch: str, remote: str = "origin") -> str:
    """Fetch *branch* from *remote* and return the fetched tip commit SHA.

    The local branch and working tree are untouched. Raises
    ``subprocess.CalledProcessError`` when the fetch fails (no network, unknown
    remote or branch).
    """
    _git_out(repo, "fetch", "--quiet", remote, branch)
    return _git_out(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()


def common_ancestor(repo: Path, rev_a: str, rev_b: str) -> str | None:
    """The best common ancestor commit of two revisions, or ``None`` for unrelated histories."""
    result = subprocess.run(
        ["git", "merge-base", rev_a, rev_b], cwd=str(repo), capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode == 1:
        return None
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def read_file_at_revision(repo: Path, rev: str, path: str) -> bytes | None:
    """The bytes of *path* as committed at *rev*, or ``None`` if the path is absent there.

    Reads the object database only; the working tree and index are not touched.
    Raises ``subprocess.CalledProcessError`` for an unknown revision.
    """
    _git_out(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    entry = _git_out(repo, "ls-tree", "-z", rev, "--", path)
    if not entry:
        return None
    meta = entry.split(b"\t", 1)[0].split()
    if len(meta) != 3 or meta[1] != b"blob":
        return None
    return _git_out(repo, "cat-file", "blob", meta[2].decode())


def changed_paths(repo: Path, rev_a: str, rev_b: str) -> list[str]:
    """Sorted paths that differ between two revisions (added, modified or deleted).

    Renames are reported as a delete plus an add; pairing a page by uid is the
    caller's business, not the seam's.
    """
    raw = _git_out(repo, "diff", "--name-only", "-z", "--no-renames", rev_a, rev_b)
    return sorted(p.decode() for p in raw.split(b"\0") if p)


def merge_commits_between(repo: Path, rev_a: str, rev_b: str) -> list[str]:
    """The merge commits (two or more parents) reachable from *rev_b* and not from *rev_a*.

    Read-only; an empty list when the range holds none. Raises
    ``subprocess.CalledProcessError`` for an unknown revision.
    """
    raw = _git_out(repo, "rev-list", "--merges", f"{rev_a}..{rev_b}")
    return [line for line in raw.decode().split("\n") if line.strip()]


def remote_history_rewritten(repo: Path, last_seen: str | None, fetched_tip: str) -> bool:
    """Whether the fetched remote tip no longer contains the commit last seen there.

    ``True`` when *last_seen* is not an ancestor of (or equal to) *fetched_tip*.
    An ordinary fast-forward keeps *last_seen* in the history and is ``False``,
    as is a first sync (``last_seen`` is ``None``). Where "last seen" is kept is
    the caller's concern; this takes it as an argument and stores nothing.
    """
    if last_seen is None:
        return False
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", last_seen, fetched_tip],
        cwd=str(repo),
        capture_output=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


# -- Sync write side (athenaeum#2931, design note federation-multi-writer-sync §4.1/§4.2) --
#
# The merge commit and the push. Nothing here rewrites a commit that exists: the
# merge commit is a new object whose parents are the two tips, the branch only ever
# moves forward onto it, and a push names its refspec without the forcing prefix.


class WorkingTreeNotCleanError(RuntimeError):
    """Raised when a merge is asked of a checked-out branch with uncommitted changes."""


class PushFailureReason(str, Enum):
    """Why a push failed (design note §4.2): distinguishable, never merged into one."""

    REJECTED_REMOTE_MOVED = "rejected_remote_moved"  # remote branch moved: integrate, retry
    OTHER = "other"  # no network, no permission, unknown remote, a remote-side hook...


@dataclass(frozen=True)
class PushResult:
    """Outcome of :func:`push_branch`. ``reason`` is ``None`` exactly when ``ok``."""

    ok: bool
    reason: PushFailureReason | None = None
    detail: str = ""


def _git_env_c() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def build_merged_tree(repo: Path, base_rev: str, changes: Mapping[str, bytes | None]) -> str:
    """Write a tree object: *base_rev*'s tree with *changes* applied; return its SHA.

    *changes* maps a repository path to its merged bytes, or to ``None`` to delete
    it. Built in a throwaway index, so the repository's index, working tree and
    branches are not touched; the only effect is new objects in the object database.
    """
    with tempfile.TemporaryDirectory(prefix="athenaeum-merge-index-") as tmp:
        env = _git_env_c()
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")

        def run(*args: str, stdin: bytes | None = None) -> bytes:
            return subprocess.run(
                ["git", *args],
                cwd=str(repo),
                check=True,
                capture_output=True,
                input=stdin,
                env=env,
            ).stdout

        run("read-tree", base_rev)
        for path, data in sorted(changes.items()):
            if data is None:
                run("update-index", "--force-remove", "--", path)
                continue
            existing = run("ls-files", "-s", "--", path).split()
            mode = existing[0].decode() if existing else "100644"
            blob = run("hash-object", "-w", "--stdin", stdin=data).decode().strip()
            run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
        return run("write-tree").decode().strip()


def commit_merge(repo: Path, tree: str, local_rev: str, remote_rev: str, message: str) -> str:
    """Create (do not attach to any branch) a merge commit of *tree* with two parents.

    Parents are, in order, *local_rev* and *remote_rev*. Returns the commit SHA.
    """
    out = _git_out(
        repo, "commit-tree", tree, "-p", local_rev, "-p", remote_rev, "-m", message
    )
    return out.decode().strip()


def _move_branch(repo: Path, branch: str, old: str, new: str) -> None:
    """Move *branch* from *old* to *new*, a descendant of *old*. The last step of a merge."""
    head = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=str(repo), capture_output=True, text=True
    )
    if head.returncode == 0 and head.stdout.strip() == f"refs/heads/{branch}":
        # Checked out: advance branch, index and working tree together. A
        # fast-forward cannot leave a conflict marker, and git refuses it rather
        # than overwrite a local change.
        _git_out(repo, "merge", "--ff-only", "--quiet", new)
    else:
        _git_out(repo, "update-ref", f"refs/heads/{branch}", new, old)


def write_merge_commit(
    repo: Path,
    branch: str,
    remote_rev: str,
    changes: Mapping[str, bytes | None],
    message: str,
) -> str:
    """Integrate *remote_rev* into local *branch* as one two-parent merge commit.

    *changes* is the merged tree expressed against the branch's current tree: the
    path -> merged bytes (``None`` deletes) for every path whose merged content
    differs from the local one. All-or-nothing (design note §4.1): the tree and the
    commit are built first; the branch is moved to the commit last. A failure
    anywhere before that leaves the branch, index and working tree exactly as they
    were. Returns the new commit SHA.

    Raises :class:`WorkingTreeNotCleanError` if *branch* is checked out with
    uncommitted changes (snapshot them as a local commit first, design note §4
    step 2), and ``subprocess.CalledProcessError`` on any git failure.
    """
    local_rev = _git_out(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    local = local_rev.decode().strip()
    head = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=str(repo), capture_output=True, text=True
    )
    if head.stdout.strip() == f"refs/heads/{branch}" and not repo_is_clean(repo):
        raise WorkingTreeNotCleanError(
            f"refusing to merge into checked-out branch {branch!r}: uncommitted changes"
        )
    tree = build_merged_tree(repo, local, changes)
    commit = commit_merge(repo, tree, local, remote_rev, message)
    _move_branch(repo, branch, local, commit)
    return commit


def push_branch(repo: Path, branch: str, remote: str = "origin") -> PushResult:
    """Push *branch* to *remote* without force and report why it failed (design note §4.2).

    ``REJECTED_REMOTE_MOVED`` means the remote branch has commits this one lacks
    (fetch, integrate, push again); ``OTHER`` is anything else (no network, no
    permission, unknown remote). The refspec is a plain ``branch:branch``, so the
    remote can only be fast-forwarded.
    """
    result = subprocess.run(
        ["git", "push", "--porcelain", remote, f"refs/heads/{branch}:refs/heads/{branch}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_git_env_c(),
    )
    if result.returncode == 0:
        return PushResult(ok=True)
    detail = (result.stderr.strip() or result.stdout.strip())[:2000]
    for line in result.stdout.splitlines():
        if line.startswith("!") and "[rejected]" in line:
            if "(fetch first)" in line or "(non-fast-forward)" in line:
                return PushResult(False, PushFailureReason.REJECTED_REMOTE_MOVED, detail)
    return PushResult(False, PushFailureReason.OTHER, detail)


# -- Sync orchestration helpers (athenaeum#2935, design note §4) -----------
#
# Small read helpers and the fast-forward the integrate sequence needs, so the
# orchestrator in ``sync_integrate`` never shells out to git itself.


def current_branch(repo: Path) -> str | None:
    """The checked-out branch name of *repo*, or ``None`` (detached HEAD / no repo)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether commit *ancestor* is *descendant* or reachable from it."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo),
        capture_output=True,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def fast_forward_branch(repo: Path, branch: str, rev: str) -> None:
    """Move *branch* forward to *rev*, a descendant of its tip (design note §4.1 step 2).

    Never rewrites history: refuses (``CalledProcessError``) if *rev* does not
    descend from the branch tip, or would overwrite an uncommitted change.
    """
    tip = _git_out(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    old = tip.decode().strip()
    if not is_ancestor(repo, old, rev):
        raise subprocess.CalledProcessError(1, ["fast-forward", branch, rev])
    _move_branch(repo, branch, old, rev)


def blob_id_at_revision(repo: Path, rev: str, path: str) -> str | None:
    """The git blob id of *path* at *rev*, or ``None`` when absent there."""
    entry = _git_out(repo, "ls-tree", "-z", rev, "--", path)
    if not entry:
        return None
    meta = entry.split(b"\t", 1)[0].split()
    if len(meta) != 3 or meta[1] != b"blob":
        return None
    return meta[2].decode()


def read_blob(repo: Path, blob: str) -> bytes | None:
    """The bytes of the git blob *blob*, or ``None`` when *repo* has no such blob.

    Reads the object database only (athenaeum#2940: the last accepted version of
    a raised copy is read back by the blob id the acceptance ledger holds).
    *blob* must be a full 40- or 64-hex object id; anything else is ``None``.
    """
    if len(blob) not in (40, 64) or any(c not in "0123456789abcdef" for c in blob):
        return None
    result = subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=str(repo), capture_output=True
    )
    return result.stdout if result.returncode == 0 else None


def write_blob(repo: Path, data: bytes, pin_ref: str | None = None) -> str:
    """Write *data* into *repo*'s object database as a blob and return its id.

    With *pin_ref* (a ref name under ``refs/``) the blob is also referenced from
    that ref, so ``git gc`` never prunes a version that no commit holds
    (athenaeum#2940: the raiser's payload as last accepted, when the committed
    copy is a merge of it with the target's own edits). Raises
    ``subprocess.CalledProcessError`` when git refuses.
    """
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        input=data,
    ).stdout.decode().strip()
    if pin_ref is not None:
        subprocess.run(
            ["git", "update-ref", pin_ref, blob], cwd=str(repo), check=True, capture_output=True
        )
    return blob


def blob_id_of(repo: Path, data: bytes) -> str:
    """The blob id git would give *data*; nothing is written to the object database."""
    return subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        input=data,
    ).stdout.decode().strip()


def git_dir(repo: Path) -> Path:
    """The absolute git directory of *repo* (a linked worktree's own one included).

    Read-only. Raises ``subprocess.CalledProcessError`` outside a git repository.
    """
    return Path(_git_out(repo, "rev-parse", "--absolute-git-dir").decode().strip())


def list_tracked_files(repo: Path) -> list[str]:
    """Sorted paths git tracks in *repo* right now (its index), relative to the root.

    Read-only. A path that is only untracked or ignored in the working tree is not
    listed. Raises ``subprocess.CalledProcessError`` outside a git repository.
    """
    raw = _git_out(repo, "ls-files", "-z")
    # ``replace``: a filename that is not UTF-8 must not abort a sync; the caller
    # matches on ASCII base names and prints the path.
    return sorted(p.decode(errors="replace") for p in raw.split(b"\0") if p)


def list_files_at_revision(repo: Path, rev: str, prefix: str = "") -> list[str]:
    """Sorted blob paths at *rev* under *prefix* (the whole tree when empty)."""
    args = ["ls-tree", "-r", "-z", "--name-only", rev]
    if prefix:
        args += ["--", prefix.rstrip("/") + "/"]
    raw = _git_out(repo, *args)
    return sorted(p.decode() for p in raw.split(b"\0") if p)


class FilesystemStore:
    """``Store`` over :mod:`athenaeum.atomic_io` + :mod:`pathlib` (issue athenaeum#976).

    One instance can address every surface *roots* names, rooted wherever
    *roots* says — not one instance per surface. ``StoreKey.surface`` picks
    the surface per call, matching the protocol's "keys carry surface" shape
    (design note §6.2 D2).

    *roots* is an explicit ``{surface_name: absolute_root}`` mapping supplied
    by the caller — this class has NO knowledge of
    :mod:`athenaeum.storage`'s adapter/mapping config model (see the module
    docstring's layering note: that import would cycle back from
    ``storage.py``). :func:`athenaeum.storage.resolve_store_for_class` is the
    caller that builds *roots* from real config, one entry per configured
    adapter, each resolved via
    :meth:`athenaeum.storage.StorageAdapter.resolve_root`. A ``StoreKey``
    naming a surface absent from *roots* raises :class:`UnknownSurfaceError`
    (fail-closed — D6).

    ``versioned`` (design note §4.4 R1, wired in S3, athenaeum#978): ``True``
    iff ``knowledge_root/.git`` exists at construction time — the same
    precondition ``librarian.git_snapshot`` checked before this slice moved
    its body onto :meth:`snapshot`, now expressed as a declared capability
    rather than an ad-hoc check duplicated at every destructive call site. A
    caller that needs a fresh read re-constructs the store (e.g. via
    :func:`athenaeum.storage.resolve_store_for_class`) rather than expecting
    this instance to notice a ``git init`` that happened after it was built —
    consistent with D4 ("declared, not probed"): the declaration is a
    snapshot-in-time of the adapter's capability, not a live re-check on every
    access. ``leases`` is ``True`` unconditionally as of S4 (athenaeum#979) —
    see :meth:`lease` and :class:`FileLease`. Every capability this slice's
    conformance suite exercises is now real: ``compare_and_swap``, ``append``,
    ``bulk_list``, ``bulk_read``, ``cheap_local_scan``, ``leases``.

    Text-only in this slice: ``put``/``append`` route through
    :func:`athenaeum.atomic_io.atomic_write_text`, which is str-based (design
    note §6.4: "``FilesystemStore`` is implementable without weakening
    ``atomic_io.py``'s L0 rule ... ``atomic_io`` needs no change to serve as
    ``put``'s implementation"). *data*/*line* are therefore decoded as UTF-8
    before the write, matching every existing store artifact
    (markdown/JSONL); a caller writing non-UTF-8 bytes gets an honest
    ``UnicodeDecodeError`` rather than a silently truncated write.
    """

    def __init__(
        self,
        knowledge_root: Path,
        roots: Mapping[str, Path],
        *,
        non_purgeable_surfaces: Collection[str] = (),
        read_only_surfaces: Collection[str] = (),
        write_gate: Callable[[str], str | None] | None = None,
    ) -> None:
        self._knowledge_root = Path(knowledge_root)
        self._roots = dict(roots)
        # Surfaces whose adapter declares ``purgeable: false`` (R2, athenaeum#2615).
        # Empty by default, so every surface keeps the store-level
        # ``purgeable=True`` it always had.
        self._non_purgeable = frozenset(non_purgeable_surfaces)
        # Surfaces whose adapter declares ``read_only: true`` (athenaeum#2621):
        # every mutating method refuses a key on one of these.
        self._read_only = frozenset(read_only_surfaces)
        # Asked only when a mutating method runs (never on a read): returns a
        # refusal reason for a surface whose store declaration does not grant
        # this install compile (access S3, athenaeum#2988), else ``None``.
        self._write_gate = write_gate
        _versioned = (self._knowledge_root / ".git").exists()
        _has_remote = _versioned and _repo_has_remote(self._knowledge_root)
        self.capabilities = StoreCapabilities(
            classes=PERSISTENCE_CLASSES,
            operational_scopes=OPERATIONAL_SCOPES,
            # design note §4.4 R1 / issue athenaeum#978 (S3): declared from
            # whether *knowledge_root* is actually a git working tree, so a
            # Tier-A/Tier-B caller gating on this flag gets the identical
            # refusal the old ad-hoc ``(knowledge_root / ".git").exists()``
            # check gave — just expressed as a capability instead of a
            # duplicated filesystem probe.
            versioned=_versioned,
            has_remote=_has_remote,
            multi_writer=_versioned and _has_remote,
            purgeable=True,
            compare_and_swap=True,
            leases=True,  # S4 (athenaeum#979): FileLease over flock, see .lease()
            append=True,
            bulk_list=True,
            bulk_read=True,
            cheap_local_scan=True,
            local_path_for=self._local_path_for,
        )

    # -- per-surface capabilities (design note §4.5 R2, athenaeum#2615) ----

    def _surface_repo(self, surface: str) -> Path | None:
        """The git working-tree root that holds *surface*'s own root, or ``None``.

        Found by walking up from the surface root (or its nearest existing
        ancestor) to the first directory containing ``.git``. Two guards keep
        this from ever picking a repository the deployment did not mean:
        the knowledge root's own repo is returned as-is, and a repo that is a
        STRICT ANCESTOR of the knowledge root while the knowledge root is not
        itself a repo is ignored (an unrelated repo higher up the tree must
        never be treated as this store's, or ``snapshot`` would ``git add -A``
        it). Raises :class:`UnknownSurfaceError` for an unknown *surface*.
        """
        root = self._root_for_surface(surface)
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        found: Path | None = None
        for candidate in (probe, *probe.parents):
            if (candidate / ".git").exists():
                found = candidate
                break
        if found is None:
            return None
        knowledge = self._knowledge_root
        if found == knowledge:
            return found
        if found in knowledge.parents:
            return None
        return found

    def surface_repo(self, surface: str) -> Path | None:
        """Public form of :meth:`_surface_repo` (athenaeum#2620)."""
        return self._surface_repo(surface)

    def capabilities_for(self, surface: str) -> StoreCapabilities:
        """The capability profile of *surface* (design note §4.5 R2).

        ``versioned`` is true only when the surface's OWN root sits inside a
        git working tree; ``purgeable`` is true unless the surface's adapter
        declared otherwise. Every other flag is the store-level value. The
        store-level :attr:`capabilities` stays the default-surface (knowledge
        root) profile, unchanged, so existing callers keep working.
        ``has_remote`` and ``multi_writer`` follow the surface's own repository.
        """
        repo = self._surface_repo(surface)
        has_remote = repo is not None and _repo_has_remote(repo)
        return replace(
            self.capabilities,
            versioned=repo is not None,
            has_remote=has_remote,
            multi_writer=has_remote,
            purgeable=surface not in self._non_purgeable,
        )

    def versioned_surface_repos(self) -> dict[Path, tuple[str, ...]]:
        """``{git working-tree root: surfaces held in it}`` for every versioned
        surface, in stable (sorted) order."""
        grouped: dict[Path, list[str]] = {}
        for surface in sorted(self._roots):
            repo = self._surface_repo(surface)
            if repo is not None:
                grouped.setdefault(repo, []).append(surface)
        return {repo: tuple(names) for repo, names in grouped.items()}

    def own_repo_surfaces(self) -> dict[Path, tuple[str, ...]]:
        """The versioned surfaces that live in a git repository OTHER than the
        knowledge root's — the ones the run must pull and push on their own."""
        return {
            repo: names
            for repo, names in self.versioned_surface_repos().items()
            if repo != self._knowledge_root
        }

    def push_repos(self) -> dict[Path, tuple[str, ...]]:
        """:meth:`sync_repos` minus repositories held only by read-only surfaces.

        What the post-run push iterates (athenaeum#2621): a read-only mount is
        pulled but never pushed.
        """
        return {
            repo: names
            for repo, names in self.sync_repos().items()
            if not all(name in self._read_only for name in names)
        }

    def read_only_pull_repos(self) -> dict[Path, tuple[str, ...]]:
        """:meth:`sync_repos` narrowed to repositories held only by read-only
        surfaces: the mounts pulled before every index build (athenaeum#2621)."""
        return {
            repo: names
            for repo, names in self.sync_repos().items()
            if all(name in self._read_only for name in names)
        }

    def sync_repos(self) -> dict[Path, tuple[str, ...]]:
        """:meth:`own_repo_surfaces` narrowed to repos that have a remote."""
        return {
            repo: names
            for repo, names in self.own_repo_surfaces().items()
            if _repo_has_remote(repo)
        }

    def _require_writable(self, surface: str, op: str) -> None:
        if surface in self._read_only:
            raise ReadOnlySurfaceError(
                f"{op} refused on surface {surface!r}: it is declared read_only "
                "(a shared mount this install never writes)"
            )
        if self._write_gate is not None:
            reason = self._write_gate(surface)
            if reason:
                raise ReadOnlySurfaceError(f"{op} refused on surface {surface!r}: {reason}")

    def read_only_surfaces(self) -> frozenset[str]:
        """The surfaces this store refuses to write (athenaeum#2621)."""
        return self._read_only

    def _require_destructive_ok(self, surface: str, op: str) -> None:
        caps = self.capabilities_for(surface)
        if not caps.versioned and not caps.purgeable:
            raise StoreCapabilityError(
                f"{op} refused on surface {surface!r}: it is neither versioned "
                "(recoverable from git) nor purgeable (erasure is its point), so "
                "the change would be unrecoverable (design note §4.4 R1)"
            )

    # -- path / version resolution --------------------------------------

    def _root_for_surface(self, surface: str) -> Path:
        try:
            return self._roots[surface]
        except KeyError:
            raise UnknownSurfaceError(
                f"store operation on unknown surface {surface!r}; known surfaces: "
                f"{sorted(self._roots)}"
            ) from None

    def _path_for(self, key: StoreKey) -> Path:
        return self._root_for_surface(key.surface) / key.key

    def _local_path_for(self, key: StoreKey) -> Path:
        """The declared, nullable escape hatch (design note §6.2 D2)."""
        return self._path_for(key)

    @staticmethod
    def _version_for(path: Path) -> str | None:
        """``mtime_ns:size`` (design note §3.5 P2) — ``None`` when *path* is absent."""
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return f"{info.st_mtime_ns}:{info.st_size}"

    # -- reads ------------------------------------------------------------

    def read(self, key: StoreKey) -> bytes:
        return self._path_for(key).read_bytes()

    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]:
        """Missing keys are silently omitted (design note §6.3: "no ``exists()``")."""
        out: dict[StoreKey, bytes] = {}
        for key in keys:
            try:
                out[key] = self._path_for(key).read_bytes()
            except FileNotFoundError:
                continue
        return out

    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]:
        root = self._root_for_surface(surface)
        if not root.exists():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            # A surface rooted at a git repository's top level (athenaeum#2615)
            # must not list git's own metadata as stored objects.
            if ".git" in path.relative_to(root).parts:
                continue
            if not rel.startswith(prefix):
                continue
            info = path.stat()
            yield ObjectMeta(
                key=StoreKey(surface=surface, key=rel),
                version=f"{info.st_mtime_ns}:{info.st_size}",
                size=info.st_size,
            )

    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]:
        for meta in self.iter_meta(surface, prefix):
            frontmatter, body = parse_frontmatter(self.read(meta.key).decode("utf-8"))
            yield Record(meta=meta, frontmatter=frontmatter, body=body)

    # -- writes -------------------------------------------------------------

    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str:
        """Compare-and-swap write (design note §6.2 D3, §2.5).

        ``expect=None`` is exclusive create — it refuses when *key* already
        exists (design note §2.5: "a compare-and-swap against 'no existing
        version' *is* exclusive create"). ``expect=<token>`` refuses unless
        the current version equals *expect* exactly.
        """
        self._require_writable(key.surface, "put")
        path = self._path_for(key)
        current = self._version_for(path)
        if expect is None:
            if current is not None:
                raise StoreConflictError(
                    f"put({key.surface}:{key.key}, expect=None) refused: object "
                    "already exists (expect=None is exclusive create)"
                )
        elif current != expect:
            raise StoreConflictError(
                f"put({key.surface}:{key.key}, expect={expect!r}) refused: "
                f"current version is {current!r}"
            )
        atomic_write_text(path, data.decode("utf-8"))
        new_version = self._version_for(path)
        if new_version is None:  # pragma: no cover - defensive, atomic_write_text just wrote it
            raise RuntimeError(f"put({key.surface}:{key.key}) succeeded but the object vanished")
        return new_version

    def append(self, key: StoreKey, line: bytes) -> None:
        """``O_APPEND`` + ``fsync`` (design note §4.6 / §6.2) via
        :func:`append_line_durable` — the primitive the 12 duplicated
        per-module ledger writers §2.4 counts collapse onto."""
        self._require_writable(key.surface, "append")
        append_line_durable(self._path_for(key), line)

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool:
        """``expect=None`` deletes unconditionally (``False`` if already absent,
        matching "no ``exists()``", §6.3); ``expect=<token>`` is a CAS delete."""
        self._require_writable(key.surface, "delete")
        self._require_destructive_ok(key.surface, "delete")
        path = self._path_for(key)
        current = self._version_for(path)
        if current is None:
            if expect is not None:
                raise StoreConflictError(
                    f"delete({key.surface}:{key.key}, expect={expect!r}) refused: "
                    "object does not exist"
                )
            return False
        if expect is not None and current != expect:
            raise StoreConflictError(
                f"delete({key.surface}:{key.key}, expect={expect!r}) refused: "
                f"current version is {current!r}"
            )
        path.unlink()
        return True

    def move(self, src: StoreKey, dst: StoreKey) -> None:
        """A key change (design note §6.3: "no directory concept... quarantine's
        'move it so the walk stops finding it' is rewritten as a key change").

        Refuses rather than clobbering when *dst* already exists — the design
        note leaves ``move``'s overwrite behavior unstated, and D6 ("fail
        closed, loudly") is the conservative default absent an explicit
        ``expect=`` parameter on this method.
        """
        self._require_writable(src.surface, "move")
        self._require_writable(dst.surface, "move")
        self._require_destructive_ok(src.surface, "move")
        src_path = self._path_for(src)
        dst_path = self._path_for(dst)
        if not src_path.exists():
            raise FileNotFoundError(f"move source {src.surface}:{src.key} does not exist")
        if dst_path.exists():
            raise StoreConflictError(f"move destination {dst.surface}:{dst.key} already exists")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_path, dst_path)

    # -- recoverability / concurrency / lifecycle ------------------------

    def snapshot(self, label: str) -> str | None:
        """Stage every change under ``knowledge_root`` and commit if there is
        anything staged (design note §4.4 R1; issue athenaeum#978, slice S3).

        MOVED (not copied) from ``librarian.git_snapshot``
        (``librarian.py:492-518`` prior to this slice) — same three
        ``subprocess`` calls (``git status --porcelain`` / ``git add -A`` /
        ``git commit -m``), now against ``self._knowledge_root`` instead of a
        passed-in ``knowledge_root`` argument. ``librarian.py`` no longer
        defines ``git_snapshot`` at all; every former call site now goes
        through this method (see ``tests/test_no_git_shelling_outside_store.py``
        for the mechanical guard against a duplicate reappearing elsewhere).

        Returns the new commit SHA (``git rev-parse HEAD``) on a real commit,
        or ``None`` when there was nothing to commit — a legitimate outcome
        under the protocol's ``str | None`` return type, not a failure. A
        caller that has already checked ``capabilities.versioned`` (``True``
        only when ``knowledge_root/.git`` existed at construction, see
        ``__init__``) will not hit the "no ``.git``" branch below in normal
        operation; the check is kept anyway as the same defensive fail-closed
        posture ``git_snapshot`` always had, in case ``.git`` is removed
        out-of-band between construction and this call.
        """
        sha = _commit_all(self._knowledge_root, label)
        # R2 (athenaeum#2615): a surface that is its own git repository is a
        # restore point of its own. Committed here so every existing caller
        # gets it for free; the return value stays the knowledge root's SHA.
        for repo, surfaces in self.own_repo_surfaces().items():
            if all(name in self._read_only for name in surfaces):
                continue  # a read-only mount is never committed into (athenaeum#2621)
            _commit_all(repo, label)
        return sha

    def snapshot_surfaces(self, label: str) -> dict[str, str]:
        """Commit every versioned surface that has changes; return one restore
        point (commit SHA) per such surface (athenaeum#2615).

        Surfaces that share a repository share a commit and so a SHA. A
        versioned surface with nothing to commit is absent from the result.
        A failing ``git commit`` raises (never silently skipped).
        """
        points: dict[str, str] = {}
        for repo, surfaces in self.versioned_surface_repos().items():
            if all(name in self._read_only for name in surfaces):
                continue
            sha = _commit_all(repo, label)
            if sha is not None:
                for surface in surfaces:
                    points[surface] = sha
        return points

    def uncommitted_paths(self, paths: Sequence[Path]) -> list[Path]:
        """Return the subset of *paths* ``git status --porcelain`` reports as
        having any local change (modified, staged, or untracked) under
        ``knowledge_root`` (issue athenaeum#1944).

        Sibling of :meth:`snapshot` — same ``git status --porcelain``
        primitive this class already owns, scoped to specific paths rather
        than the whole tree, for a caller (``athenaeum subject-population
        --apply``) that must refuse to write a page with uncommitted local
        changes rather than silently mixing its own edit into them. Returns
        every element of *paths* unchanged (fails CLOSED, never silently
        clean) when ``knowledge_root`` is not a git repository, or the
        ``git`` invocation itself fails to run at all.
        """
        knowledge_root = self._knowledge_root
        existing = [p for p in paths if Path(p).exists()]
        if not existing:
            return []
        if not (knowledge_root / ".git").exists():
            return list(existing)

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "-z", "--", *[str(p) for p in existing]],
                cwd=str(knowledge_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return list(existing)
        if result.returncode != 0:
            return list(existing)

        dirty: set[Path] = set()
        # Porcelain v1 with ``-z``: NUL-separated records, each "XY " + path,
        # and a rename/copy emits the ORIGINAL path as its own following
        # record (destination first, which is the one that matters here).
        #
        # ``-z`` rather than the newline format on purpose. Under the default
        # ``core.quotePath=true``, plain ``--porcelain`` renders any path with
        # non-ASCII bytes or shell-special characters as a C-quoted,
        # backslash-escaped literal (``"caf\303\251.md"``). That literal does
        # not match the real filename, so a dirty page would read CLEAN and
        # this guard would fail OPEN -- the exact opposite of the fail-closed
        # contract the docstring above states, on precisely the accented and
        # punctuated page names a knowledge wiki is full of. ``-z`` never
        # quotes or escapes, so there is nothing to unescape and no
        # ``core.quotePath`` dependency. It also removes the newline format's
        # other ambiguity: a filename may legitimately begin or end with a
        # space, which the old ``.strip()`` silently corrupted.
        records = result.stdout.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            status, rel = record[:2], record[3:]
            if "R" in status or "C" in status:
                index += 1  # skip the rename/copy source path record
            if not rel:
                continue
            dirty.add((knowledge_root / rel).resolve())
        return [p for p in existing if Path(p).resolve() in dirty]

    def lease(
        self, name: str, ttl_seconds: float, *, force: bool = False
    ) -> AbstractContextManager[Lease]:
        """A ``flock``-backed lease at ``knowledge_root/name`` (design note
        §6.2; issue athenaeum#979, S4) — a single non-blocking attempt.

        ``ttl_seconds`` is accepted for protocol conformance and is not used
        by this adapter: the kernel already releases ``flock`` the instant a
        holder's process dies, which is a stronger guarantee than any
        application-level timer could give, so there is nothing here for a
        TTL to add (§4.6: "mapping onto flock for the filesystem adapter and
        onto a lease row / conditional put elsewhere" — this is the "onto
        flock" half; a database/lease-row adapter is where ``ttl_seconds``
        does real work, since IT has no kernel to release anything on death —
        see ``tests/store_fakes.py``'s ``InMemoryStore.lease`` for that other
        half). ``force`` is additive keyword-only surface beyond the
        ``Store`` protocol's two positional parameters (harmless to a
        ``runtime_checkable`` ``Protocol``, which checks attribute presence,
        not exact signatures) — :class:`athenaeum.runlock.RunLock` is the
        caller that uses it, to implement its own ``--force``/auto-break
        policy on top of this one primitive (see the module docstring).
        Raises :class:`LeaseHeldError` when contended and ``force`` is not
        set (or the forced break still loses the race to another holder).
        """
        return FileLease(self._knowledge_root / name, force=force)

    def bootstrap(self) -> None:
        """Create *knowledge_root* if absent. Non-destructive, matching
        ``mkdir(parents=True, exist_ok=True)`` — never touches existing content.
        ``athenaeum init``'s ``git init`` step is not part of this slice."""
        self._knowledge_root.mkdir(parents=True, exist_ok=True)


# ``resolve_store_for_class`` is defined in :mod:`athenaeum.storage`, not
# here — see this module's docstring ("Layering") for why: this module has
# no import of ``athenaeum.storage``, so the class → surface → roots
# resolution that function needs (``storage.available_adapters`` /
# ``storage.resolve_adapter_for_class``) can only live on that side of the
# one-directional edge without creating a 2-node import-graph SCC.
