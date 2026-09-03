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

Full rationale: ``docs/whole-store-adapter-design.md`` §6 (the published draft
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
and ``docs/store-contract.md``, the published form of this docstring's §6).
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    which, the second-precision form, is also the exact rendering
    :data:`athenaeum.fingerprint._RESOLVED_AT_FORMAT` parses back with
    ``datetime.strptime``. Second precision is the rendering kept here (not
    widened to also accept microseconds) because that parser is the sole
    consumer that round-trips this value, and pinning the format in ONE
    place — rather than teaching the parser two shapes — is what makes a
    future drift back to microsecond rendering fail loudly instead of
    silently: any writer that reaches for this helper can no longer produce
    a timestamp the parser rejects. Mirrors :func:`append_line_durable`
    (issue athenaeum#980) as the package's shared-primitive home for this
    class of drift.

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
    ``host`` (``timestamp`` pinned to *acquired_at*, the original acquire time)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = (
        f"pid: {os.getpid()}\n"
        f"timestamp: {acquired_at}\n"
        f"host: {socket.gethostname()}\n"
        f"heartbeat: {now_iso}\n"
    )
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
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
    ArtifactDeclaration(
        name="llm-schema-observations-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root, with a legacy-store cache-dir fallback (see source_ref)",
        source_ref=(
            "llm_schemas.py:134 OBSERVATIONS_FILENAME (design note §5.2 table row 8 "
            "'observations.jsonl'). Issue athenaeum#980 AC4: "
            "llm_schemas.durable_observations_path() resolves behind the seam with "
            "the same legacy-store fallback as the spend ledger. observe()/"
            "observe_parse_failure() and all five wrapper functions "
            "(observe_query_topics/observe_claim_kind/observe_contradictions/"
            "observe_resolutions/observe_tier2_classify/observe_tier3_merge_ops) now "
            "accept wiki_root=, threaded from every call chain up to its available "
            "root (query_topics.py, claim_kind.py -> librarian.py's ctx.wiki_root, "
            "contradictions.py/resolutions.py -> merge.py's wiki_root, tiers.py -> "
            "batch.py/merge.py's wiki_root)"
        ),
    ),
    ArtifactDeclaration(
        name="spend-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root, with a legacy-store cache-dir fallback (see source_ref)",
        source_ref=(
            "spend.py:129 LEDGER_FILENAME (design note §5.2 table row 8 'spend.jsonl'). "
            "Issue athenaeum#980 AC4: spend.durable_ledger_path() resolves behind the "
            "seam (an existing installation's populated cache-dir ledger keeps "
            "resolving there until migrated; a fresh or already-migrated store resolves "
            "to wiki_root). Every production write AND read call site now passes "
            "wiki_root= (librarian.py x3, drain.py, _cmd_drain.py, _cmd_lifecycle.py, "
            "status.py, backlog_price_sheet.py, ordinary_night_table.py, answers.py, "
            "query_topics.py + _cmd_query.py, memory_class_backfill.py) — see "
            "tests/test_spend.py::TestDurableLedgerPath::test_no_split_brain_on_a_fresh_store"
        ),
    ),
    ArtifactDeclaration(
        name="push-records-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="wiki root, with a legacy-store cache-dir fallback (see source_ref)",
        source_ref=(
            "push_metrics.py:80 PUSH_RECORDS_FILENAME (design note §5.2 table row 8 "
            "'_push_records.jsonl'). Issue athenaeum#980 AC4: "
            "push_metrics.durable_push_records_path() resolves behind the seam with the "
            "same legacy-store fallback as the spend ledger. Every production write AND "
            "read call site now passes wiki_root= (mcp_server.py's record_push, "
            "compute_baseline/sample_sessions/build_coverage_worksheet/"
            "determine_references/run_reference_determination and their "
            "_cmd_push_metrics.py/librarian.py callers, usage_report.py + a new "
            "--path/--knowledge-root flag on `athenaeum usage-report`) — see "
            "tests/test_push_metrics.py::TestDurablePushRecordsPath::test_no_split_brain_on_a_fresh_store"
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
            "without relocation, same reasoning as never-ingest-refusals-ledger below"
        ),
    ),
    ArtifactDeclaration(
        name="never-ingest-refusals-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "never_ingest.py:122 REFUSALS_FILENAME — same durable, not-reconstructible "
            "shape as design note §5.2 table row 8 (and one of the duplicated §2.4 "
            "appenders AC3 collapses), but NOT named in §5.2's table, so its physical "
            "location is left as-is per 'do not invent a classification the note does "
            "not state'; only classification is declared here, not relocated"
        ),
    ),
    ArtifactDeclaration(
        name="decay-sweep-records-ledger",
        persistence_class="operational",
        operational_scope="store-durable",
        location="cache dir",
        source_ref=(
            "decay_sweep.py:100 SWEEP_LEDGER_FILENAME — same reasoning as "
            "never-ingest-refusals-ledger above: durable and not reconstructible, one "
            "of the §2.4 duplicated appenders, but outside §5.2's named table rows, so "
            "classified without relocation"
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
        name="detection-incomplete-state",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref="detection_state.py:57 _STORE_NAME 'detection_incomplete.json' "
        "(design note §5.2 table row 9)",
    ),
    ArtifactDeclaration(
        name="zero-yield-state",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref="zero_yield.py:57 STATE_NAME 'zero_yield_state.json' (design note "
        "§5.2 table row 9)",
    ),
    ArtifactDeclaration(
        name="killswitch-state",
        persistence_class="operational",
        operational_scope="machine-local",
        location="cache dir",
        source_ref="killswitch.py:100 state_path() 'disabled' (design note §5.2 table row 9)",
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

    def __init__(self, knowledge_root: Path, roots: Mapping[str, Path]) -> None:
        self._knowledge_root = Path(knowledge_root)
        self._roots = dict(roots)
        self.capabilities = StoreCapabilities(
            classes=PERSISTENCE_CLASSES,
            operational_scopes=OPERATIONAL_SCOPES,
            # design note §4.4 R1 / issue athenaeum#978 (S3): declared from
            # whether *knowledge_root* is actually a git working tree, so a
            # Tier-A/Tier-B caller gating on this flag gets the identical
            # refusal the old ad-hoc ``(knowledge_root / ".git").exists()``
            # check gave — just expressed as a capability instead of a
            # duplicated filesystem probe.
            versioned=(self._knowledge_root / ".git").exists(),
            purgeable=True,
            compare_and_swap=True,
            leases=True,  # S4 (athenaeum#979): FileLease over flock, see .lease()
            append=True,
            bulk_list=True,
            bulk_read=True,
            cheap_local_scan=True,
            local_path_for=self._local_path_for,
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
        append_line_durable(self._path_for(key), line)

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool:
        """``expect=None`` deletes unconditionally (``False`` if already absent,
        matching "no ``exists()``", §6.3); ``expect=<token>`` is a CAS delete."""
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
        knowledge_root = self._knowledge_root
        if not (knowledge_root / ".git").exists():
            return None

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return None

        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(knowledge_root),
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", label],
            cwd=str(knowledge_root),
            check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return sha

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
