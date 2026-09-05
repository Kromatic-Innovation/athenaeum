# The whole-store `Store` contract

> **Status:** published extension point (issue athenaeum#983, S8 of the
> whole-store adapter design lock, issue athenaeum#911). `athenaeum.store`'s
> `Store` protocol, its data/error types, and the shipped `FilesystemStore`
> adapter are on the stable `__all__` surface as of this contract's
> publication — see `src/athenaeum/__init__.py`. The rest of
> `athenaeum.store` (the lease-primitive internals, the artifact-registry
> catalogue, `append_line_durable`) stays internal; see that module's
> docstring for the exact split.
>
> **Scope note.** This is the *physical* layer
> [`docs/extending/storage-adapter-contract.md`](storage-adapter-contract.md) has never
> had: that contract resolves an entity class to a **surface** (a root plus a
> corpus policy) and hands back a bare `pathlib.Path`; every caller then does
> its own filesystem arithmetic against it. `Store` generalizes the physical
> half — a caller addresses an object by `StoreKey` (surface + POSIX-style
> relative key, never an OS path) and reads/writes bytes through the
> protocol, so a non-filesystem adapter is a second implementation of this
> module, not a second front door. This contract **extends** the
> storage-adapter seam; it does not replace it — see "Where this sits", below.

Full design rationale: [`docs/extending/whole-store-adapter-design.md`](whole-store-adapter-design.md)
§6 (the draft this contract publishes) and §9.2 (the implementation slices
S1–S8 that built and published it).

## Design decisions

- **D1 — the unit is a record, not a path.** Every consumer wants frontmatter
  plus body, and the seam hands it over in one call:
  [`Store.iter_records`](#the-protocol) fuses list, read, and parse into one
  iteration. Handing back a `Path` for the caller to open is what made the
  physical layer unviable as an adapter seam in the first place (design note
  §3.5, P1/P3).
- **D2 — keys, not paths.** A `StoreKey` is a surface plus a POSIX-style
  relative key. `Path` is an implementation detail of the filesystem adapter,
  exposed only through the explicitly-nullable `local_path_for` escape hatch,
  which every caller must be able to do without.
- **D3 — versions are opaque tokens.** Compared for equality, never parsed,
  never assumed to be a timestamp (design note §3.5, P2).
  `FilesystemStore` uses `mtime_ns:size`; a non-filesystem adapter is free to
  use anything else (a content hash, a database row version, an ETag) — a
  caller that parses a version token instead of comparing it is not honoring
  the contract.
- **D4 — capabilities are declared, per surface, and checked.** An adapter
  that omits a capability does not get a best-effort emulation; callers take
  the declared alternative, or refuse (design note §7, the honest-refusal
  rule).
- **D5 — the existing seam is extended, never forked.**
  `resolve_adapter_for_class` (in `athenaeum.storage`) keeps routing classes
  to surfaces; the store hangs off the resolved adapter. See "Where this
  sits", below, for exactly how.
- **D6 — fail closed, loudly.** Inherited verbatim from the existing
  storage-adapter layer: an omitted capability defaults to absent, and a
  misconfiguration raises rather than falling back to a default surface.

## The protocol

This is the shipped shape, in `src/athenaeum/store.py`:

```python
@dataclass(frozen=True)
class StoreKey:
    surface: str          # a storage-adapter surface name
    key: str              # POSIX-style relative key; never an OS path

@dataclass(frozen=True)
class ObjectMeta:
    key: StoreKey
    version: str          # opaque; equality only (D3)
    size: int

@dataclass(frozen=True)
class Record:
    meta: ObjectMeta
    frontmatter: dict[str, Any]
    body: str

@dataclass(frozen=True)
class Lease:
    name: str
    token: str            # opaque; equality only (D3)

@dataclass(frozen=True)
class StoreCapabilities:
    # persistence class support (design note R3)
    classes: frozenset[str]              # {"source","derived","operational","config"}
    operational_scopes: frozenset[str]   # {"store-durable","machine-local"}
    # recoverability (R1/R2)
    versioned: bool                # can produce a restore point
    purgeable: bool                # a delete is a true erasure
    # concurrency + atomicity
    compare_and_swap: bool
    leases: bool
    append: bool
    # performance shape (P1/P3/P4)
    bulk_list: bool
    bulk_read: bool
    cheap_local_scan: bool
    # escape hatch (D2) — None on every non-filesystem adapter
    local_path_for: Callable[[StoreKey], Path] | None = None

class Store(Protocol):
    capabilities: StoreCapabilities

    # --- reads -------------------------------------------------------
    def read(self, key: StoreKey) -> bytes: ...
    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]: ...
    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]: ...
    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]: ...

    # --- writes ------------------------------------------------------
    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str: ...
    def append(self, key: StoreKey, line: bytes) -> None: ...
    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool: ...
    def move(self, src: StoreKey, dst: StoreKey) -> None: ...

    # --- recoverability (R1) ------------------------------------------
    def snapshot(self, label: str) -> str | None: ...

    # --- concurrency ---------------------------------------------------
    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]: ...

    # --- lifecycle -------------------------------------------------------
    def bootstrap(self) -> None: ...
```

Four of these earn their place against a specific finding rather than by
symmetry with a filesystem: `iter_meta` replaces N `stat()` calls with one
listing (P1); `read_many` replaces N reads with one batch (P3); `put(...,
expect=...)` is the portable form of temp-plus-rename (design note §4.6); and
`snapshot` is what a recoverability gate asks instead of "does `.git` exist"
(R1). `iter_records` is the one convenience: `iter_meta` + `read_many` +
`parse_frontmatter` is what nearly every caller does, and offering it as one
call is what stops callers rebuilding a slower version themselves.

`put`'s CAS semantics: `expect=None` is exclusive create — it refuses when
the key already exists. `expect=<token>` refuses unless the object's current
version equals `expect` exactly. `delete` mirrors this: `expect=None` deletes
unconditionally (and returns `False`, not an error, if the object was already
absent); `expect=<token>` is a CAS delete. Both raise `StoreConflictError` on
a precondition mismatch — never a silent no-op.

Errors this module raises, all importable from `athenaeum` alongside the
types above: `StoreKeyError` (an invalid `StoreKey.key`, at construction),
`StoreConflictError` (a `put`/`delete`/`move` precondition mismatch),
`LeaseHeldError` (`lease()` contended without `force`), and
`UnknownSurfaceError` (`FilesystemStore`-specific: a `StoreKey.surface` this
instance's `roots` mapping doesn't know).

## What the contract deliberately does not have

- **No transactions across keys.** Nothing in the design's seam inventory
  needs one, and requiring it would exclude object stores as adapters.
  Multi-object consistency is achieved with `snapshot` plus the
  restore-together rule (design note R3), not a distributed transaction.
- **No directory concept.** Prefixes only (`iter_meta`'s `prefix=`
  parameter). The tree shape is a filesystem detail — quarantine's "move it
  so the walk stops finding it" is rewritten as a key change via `move`, not
  a directory move.
- **No search or query.** Indexes are derived (design note §5.1); a store
  that answered queries would be a second read seam.
- **No `exists()`.** `iter_meta` and a missing-key error from `read` answer
  it; a standalone existence check is the round trip callers most easily
  scatter.

## Where this sits

**Scope: `knowledge_root`, not the cache dir.** The `Store` contract governs
the knowledge root and nothing else. Cache-dir artifacts (the FTS5 database,
the vector-store collection, index manifests, ingest/auto-memory manifests,
the vector generation stamp) stay on the local filesystem whatever adapter
backs the store — they are `derived` or `operational`/`machine-local`
(design note R3), per-machine by construction, and requiring an adapter to
serve them would drag every backend into the local index libraries' own
file-handle assumptions for no benefit.

**Layering: `athenaeum.store` is L0/L1**, and it inherits none of
`athenaeum.storage`'s upward reach. The whole-store design note's own §6.4
originally described the opposite arrangement; that was a staleness in the
note, not a divergence in the shipped code, and issue athenaeum#1087 corrected
§6.4 to match what is recorded here. The account is repeated in this section
because it is the published contract's own layering claim, not only the
design note's:

- Design note §6.4 originally described `resolve_store_for_class` as living
  IN `athenaeum.store`, reaching up to `athenaeum.storage` (L2) for adapter
  and mapping resolution — the same upward exception `storage.py` documents
  for itself. (Corrected by issue athenaeum#1087; see below for what actually
  shipped.)
- **What actually shipped (S1, issue athenaeum#976) is the reverse.**
  `resolve_store_for_class` lives in **`athenaeum.storage`**, which imports
  `athenaeum.store` (one direction only) to build a `FilesystemStore`.
  `athenaeum.store` itself imports only `athenaeum.atomic_io` (L0) and
  `athenaeum.models` (L1) — it has **no** import of `athenaeum.storage`, not
  even a function-local/deferred one.
- **Why:** the note's original split would create a real edge back INTO
  `athenaeum.store` from `athenaeum.storage` (which independently needs
  `FilesystemStore` to implement its own side of the wrapper) — a
  two-module strongly-connected component. This repo's import-graph guard
  (`tests/test_import_graph_acyclic.py`, pinned to an empty baseline since
  issue athenaeum#640) counts function-local/deferred imports as graph edges too,
  so not even a call-time-deferred import would have escaped it. Putting
  `resolve_store_for_class` on the `storage.py` side of the edge keeps the
  dependency one-directional and avoids the cycle, while still satisfying
  D5 ("the existing seam is extended, never forked" — `resolve_store_for_class`
  sits beside `surface_root_for_class` in the same module, doing the
  equivalent resolution for the store).
- This is checked mechanically, twice: the repo-wide guard above, and a
  narrower, store-specific Tarjan check in `tests/test_store_layering.py`.

A caller building a `FilesystemStore` from real config uses
`athenaeum.storage.resolve_store_for_class(cls, config, knowledge_root)`,
the store-returning sibling of `storage.surface_root_for_class`. `Store`
itself is otherwise adapter-agnostic: `FilesystemStore` takes an explicit
`roots: Mapping[str, Path]` (surface name → resolved root) at construction,
so surface → root resolution is supplied by the caller rather than looked up
inside `athenaeum.store`.

**Not to be confused with the storage-adapter layer.** `Store` is the
physical read/write seam; [`docs/extending/storage-adapter-contract.md`](storage-adapter-contract.md)
is the routing seam (entity class → surface + corpus policy) one level above
it. They compose: `resolve_store_for_class` resolves BOTH — the surface an
entity class maps to (storage-adapter layer) and the `Store` instance that
can actually read and write it (this contract).

## Running the conformance harness against your own implementation

The S1 conformance suite ships as a runnable, importable harness —
`athenaeum.store_conformance` — so a third-party adapter author can prove
their own `Store` implementation is conformant without editing any athenaeum
source or test file:

```python
# test_my_adapter.py — lives in YOUR project, not athenaeum's
import pytest
from athenaeum.store import Store
from athenaeum.store_conformance import StoreConformanceTests

class TestMyAdapterConformance(StoreConformanceTests):
    @pytest.fixture
    def store(self) -> Store:
        return MyStore(...)  # your Store implementation
```

```bash
pip install athenaeum[dev]   # StoreConformanceTests needs pytest
pytest test_my_adapter.py -v
```

Every inherited `test_*` method then runs against `MyStore`, exercising
`put`/`read`/`read_many`/`iter_meta`/`iter_records`/`append`/`delete`/`move`/
`snapshot`/`lease`/`bootstrap` and the declared-capabilities shape — a
pass/fail conformance report, the same run that gates athenaeum's own two
shipped implementations (`FilesystemStore` and the in-memory test fixture,
see `tests/test_store_conformance.py`). `athenaeum.store_conformance`'s
module docstring documents exactly which parts of the S1 suite are portable
this way versus athenaeum-internal-only (fixture-specific capability values,
`athenaeum.storage.resolve_store_for_class`, and the in-memory fixture's
genuinely-TTL-expiring lease semantics). `tests/test_store_conformance_harness.py`
in the athenaeum repo is a complete worked example: a from-scratch, minimal
dict-backed `Store` built using nothing but the public harness and protocol
types, run through the same suite.

The bundled [`adapter-authoring`](../../skills/adapter-authoring/SKILL.md) skill
references this harness alongside its intake-adapter counterpart.
