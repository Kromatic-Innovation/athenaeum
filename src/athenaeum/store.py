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
(athenaeum#978) — see :meth:`FilesystemStore.snapshot`. Two protocol members
remain deliberately inert here and named as such at each call site: ``lease``
(the ``runlock`` migration lands in S4), and the R3 persistence-class
enforcement §5.2 defines (S5, athenaeum#980).

Like :mod:`athenaeum.storage` and :class:`athenaeum.search.SearchBackend`,
this is an INTERNAL seam: importable but not on the stable ``__all__`` surface
until S8 (athenaeum#983) publishes it alongside this slice's conformance suite
(``tests/test_store_conformance.py``) as a third-party adapter-authoring
harness (design note §6, preamble).

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
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter

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
    itself defined there, for the same reason as :class:`Record`. S1 stub
    type: no implementation in this slice grants a real lease —
    ``StoreCapabilities.leases`` is ``False`` on every store built here, and
    both implementations' ``lease()`` raise. S4 (athenaeum#979) implements this
    over ``runlock``'s ``flock`` + heartbeat.
    """

    name: str
    token: str


#: The four R3 persistence classes (design note §5.2). Declarative in S1 —
#: nothing here enforces that an artifact declares exactly one; that
#: enforcement is S5 (athenaeum#980).
PERSISTENCE_CLASSES = frozenset({"source", "derived", "operational", "config"})

#: The two R3 operational scopes (design note §5.2), declarative for the same
#: reason as :data:`PERSISTENCE_CLASSES`.
OPERATIONAL_SCOPES = frozenset({"store-durable", "machine-local"})


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
    access. ``leases=False`` for the same reason ``versioned`` used to be
    hardcoded: S4 (athenaeum#979) has not wired ``lease`` yet. Everything
    else this slice's conformance suite exercises for real: ``compare_and_swap``,
    ``append``, ``bulk_list``, ``bulk_read``, ``cheap_local_scan``.

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
            leases=False,  # S4 (athenaeum#979) wires runlock behind this
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
        """``O_APPEND`` + ``fsync`` (design note §4.6 / §6.2): the primitive the
        12 duplicated per-module ledger writers §2.4 counts collapse onto."""
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

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

    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]:
        raise NotImplementedError(
            "FilesystemStore.lease() is not implemented until S4 (athenaeum#979); "
            "capabilities.leases is False"
        )

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
