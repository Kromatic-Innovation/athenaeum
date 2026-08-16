<!-- SPDX-License-Identifier: Apache-2.0 -->

# Generalising the storage-adapter seam to the whole store

**Status:** DESIGN LOCK. Issue athenaeum#911. Not yet implemented — the
implementation slices this design locks are listed in §9.

From the 2026-08-14 intake-architecture review (Vitruvius Specify), which
ratified [`docs/north-star.md`](north-star.md) §2.11:

> **Storage is logically fixed, physically pluggable.** The logical model —
> raw + wiki as source of truth, indexes derived — is a fixed boundary. The
> physical layer is an adapter seam: a deployment may back the wiki or any
> excluded surface with encrypted storage, a database, or a synced filesystem,
> and no caller can tell, because callers only ever touch intake and recall.

Companion to [`docs/storage-adapter-contract.md`](storage-adapter-contract.md)
(the seam as it exists today — entity class → surface + corpus policy),
[`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md) (the two-path
invariant this design is the physical half of), and
[`docs/recall-architecture.md`](recall-architecture.md) (the read path whose
latency budget bounds everything here).

---

## 1. What is already true, and what is missing

The storage-adapter layer (athenaeum#427 / athenaeum#429 / athenaeum#532)
resolves an **entity class** to a **surface**: a `backing_store` name, a
`surface_root`, and a `corpus_policy` of three enforced capability bits. It
works, it ships two built-in adapters, and it has a live contract test.

It is not, however, a storage abstraction. Its writer-facing entry point is:

```python
def surface_root_for_class(entity_class, config, knowledge_root) -> Path: ...
```

(`src/athenaeum/storage.py:362`). It returns **a `pathlib.Path`**. Every
caller then performs ordinary filesystem operations against that path. The
seam answers *where a class lives* and *what corpus it joins*; it does not
mediate *how bytes are read and written*. `backing_store` is today a
descriptive string — `"wiki-markdown"`, `"markdown"` — that no code dispatches
on, and the documented `backing_store: "sqlite"` example
(`docs/storage-adapter-contract.md:207`) changes no behaviour whatsoever.

That is the whole gap. **The class → surface routing generalises already; the
physical layer does not exist yet.** Reaching north-star §2.11 means
introducing a *store* abstraction underneath the existing adapter and routing
the filesystem touches inventoried in §2 through it.

The size of that job is not obvious from the outside, so it is worth stating
up front: the knowledge store is not only `raw/` and `wiki/`. It is also a git
working tree whose history is the only backup athenaeum has (§4), a POSIX
`flock` target (§4.6), a set of append-only ledgers (§5.2), and — for the
keyword backend — a corpus that is re-read in full on every query (§3.1).
Each of those is a filesystem assumption that a database or object store does
not satisfy for free.

### 1.1 Three things this design settles

Stated in athenaeum#911, answered here:

1. **A complete seam inventory** — every direct filesystem touch that would
   have to route through the store (§2).
2. **The performance constraints** — index builds walk the whole store, so
   adapter latency is not free (§3).
3. **The interaction with git versioning**, currently load-bearing for
   recoverability (§4).

## 2. Seam inventory

### 2.1 Method

Every direct filesystem touch in `src/athenaeum/**.py` — read, write, list,
stat, delete, move, mkdir, and `sqlite3.connect` — enumerated with:

```sh
rg -c 'read_text|write_text|read_bytes|write_bytes|\.glob\(|\.rglob\(|\.iterdir\(|os\.walk|\.unlink\(|\.mkdir\(|\.stat\(|shutil\.|sqlite3\.connect|atomic_write_text|\.exists\(|\.is_file\(|\.is_dir\(|os\.replace|os\.rename' \
   src/athenaeum/*.py | sort -t: -k2 -rn
```

That sweep was then hand-reviewed site by site to drop false positives and
assign each remaining touch an artifact bucket. §2.3 carries the per-module
line references; the full per-site list is regenerable from the command above
and is not transcribed in full, because what the design needs from the
inventory is the *shape* — how many modules, how concentrated, how much
already routes through an abstraction, and which filesystem semantics have no
portable equivalent. Counts are against `develop` at the time of writing and
will drift; the shape will not.

### 2.2 The headline numbers

Two counts are given because they answer different questions. The
**mechanical** count is what the command above returns and is reproducible to
the digit; the **classified** count is that sweep hand-reviewed per site to
drop non-I/O `Path()` constructors, `datetime.replace`/`str.replace` false
positives, and in-memory `yaml.safe_load` on already-read text.

| Measure | Count |
|---|---|
| Modules in `src/athenaeum/` | 104 |
| Modules with at least one raw pattern hit (mechanical) | 73–76 |
| Raw pattern hits (mechanical) | ~613–667 |
| **Modules with a genuine store touch (classified)** | **~65** *(76 modules had hits; ~9 proved to touch only CLI-arbitrary paths, temp fixtures or packaged resources, and `atomic_io.py` is the chokepoint itself rather than a consumer)* |
| **Genuine store-touch sites (classified)** | **~421** *(131 wiki + 90 raw + 145 state + 33 index + 22 excluded; the ~83 non-store and ~43 ambiguous sites are counted separately in §2.2.1)* |
| Modules routing writes through `atomic_io.atomic_write_text` | 28 |
| Hand-rolled re-implementations of that same temp + `os.replace` pattern | 4 — `search.py:684-686`, `repair.py:91-100`, `clusters.py:643-659`, plus a plain `write_text` at `search.py:1032` |
| Modules implementing their own `O_APPEND` ledger writer | 12 |
| Modules consulting `storage.surface_root_for_class` — **the existing seam** | **5** |

The last row is the finding. Five modules ask the storage layer where a class
lives (`storage.py`, `_cmd_storage.py`, `storage_migrate.py`, `corrections.py`,
`pii.py`); every other store-touching module receives a `wiki_root` /
`raw_root` / `knowledge_root` / `cache_dir` as a parameter threaded down the
call graph and does path arithmetic on it. **The seam exists and is almost
entirely bypassed** — not by anyone reaching around it, but because it never
offered the thing they needed. This is what "the class → surface routing
generalises already; the physical layer does not exist yet" (§1) looks like
when counted.

### 2.2.1 Touches by artifact class

Every classified site falls into one of these. The classes here are the
*physical* buckets the sweep found; §5.2 maps them onto the *persistence*
classes the contract needs.

| Bucket | Sites | Modules | What it is |
|---|---|---|---|
| `store-state` | ~145 | 27 | ledgers, queues, sidecars, manifests, stamps |
| `store-wiki` | ~131 | 30 | compiled entity pages |
| `store-raw` | ~90 | 26 | raw intake |
| `store-index` | ~33 | 7 | FTS5 db, chromadb, manifests, generation stamp |
| `store-excluded` | ~22 | 6 | off-corpus records |
| ambiguous / cross-surface | ~43 | 14 | content- or config-dependent destination |
| `non-store` | ~83 | 30 | CLI-arbitrary paths, temp fixtures, packaged resources, `pyproject.toml` |

**`store-state` is the largest bucket** — larger than compiled wiki pages, and
nearly twice raw intake. A design that treats the store as "raw plus wiki plus
some indexes" is describing under half of it. This is the evidence behind R3
(§5.2), and it is why the classes R3 adds are something the model cannot do
without rather than a tidy-up.

The ~43 ambiguous sites are not a classification failure; they are a real
property of the code. `models.py:979`, `pii.py:3153`, `cross_scope.py:520` and
`decisions.py:149` each read a path whose surface depends on the entity class
or the merge history of the record — precisely the resolution the store
contract has to perform, and precisely why callers cannot keep receiving bare
roots.

### 2.3 Where the touches concentrate

Ranked by classified store-touch count, with representative line references:

| Module | Sites | Buckets | Representative sites |
|---|---|---|---|
| `librarian.py` | 40 | raw, wiki, state | wiki list/read `456,460`; raw intake scan `5388-5417`; `_stuck_files.json` `2131-2158`; `_quarantine_candidates.json` `2289-2316`; ingest/auto-memory/full-compile manifests under the cache dir `5437-5647` |
| `search.py` | 38 | wiki, raw, index | `os.listdir(wiki_root)` `323`; per-page `stat`/`read` `503,521`; manifests `576-686`; FTS5 db `719-971`; chromadb `1258-1617`; `.generation` `1016,1032` |
| `pending_merges.py` | 33 | wiki, state | sidecars `460-1515`; source/target pages `676-1288`, delete at **`1001`**; inbound-wikilink rewrite `754-839` |
| `corrections.py` | 28 | raw, wiki, excluded, state | correction batches `1516-1931`; excluded record `826-846`; `_corrections_applied.jsonl` `1685-1760`; `.git` check `1806` |
| `pii.py` | 25 | wiki, excluded, state | corpus scan `999-1038`; excluded records `1680-2674`; observation ledgers `1312-1448` |
| `rules.py` | 14 | raw, state, **config** | `knowledge_root/rules/*.yaml` `651-655`; preserved-log area `885-939`; raw retirement `963,989`; `_shape_rules_applied.jsonl` `1011,1012` |
| `answers.py` | 14 | wiki, state, ambiguous | `_pending_questions.md` `430-1177`; source-ref resolution tries three roots in order `602,607,736,783` |
| `quarantine.py` | 13 | raw, state | ledger `75-128`; cross-surface `shutil.move` both directions `219-244`, `303-305` |
| `resolutions.py` | 12 | raw | member reads `923-2374`; delete `2672,2680` |
| `tiers.py` | 12 | wiki, state | `wiki/_schema/` `226-272`; entity pages `2202,2208`; pending sidecars `2655-3450` |
| `repair.py` | 12 | wiki, raw | wiki glob/read `85-695`; hand-rolled atomic write `99,100` |
| `init.py` | 12 | raw, state, **config** | scaffold `107`; `_schema` + `_index.md` `114-122`; `templates/` `167-177`; `rules/` `193-203` |
| `intake.py` | 11 | raw, wiki | raw discovery `156-381`; wiki collision check `473` |
| `merge.py` | 11 | raw, wiki, ambiguous | member reads `568-1116`; wiki glob `2219,2501,2502`; cluster report `530,533` |
| `dedupe.py` | 10 | wiki | person load `290,294`; reference rewrite `706-836` |
| `storage_migrate.py` | 10 | wiki, excluded | wiki reads `330-766`; excluded write `643,644` |
| `mcp_server.py` | — | wiki, excluded, state | the read/write seam itself: 46 `wiki_root`, 54 excluded-surface, 13 `cache_dir` references |

Three structural facts follow:

- **The store is not concentrated behind a few doors.** The top five modules
  hold 164 of ~421 sites — under two-fifths; the tail runs long across command
  modules, detectors, and ledger writers. There is no small set of files whose
  migration would generalise the store — which is the whole argument for §9's
  slice-by-slice plan over a cutover (§10).
- **Most modules touch more than one bucket**, usually without saying so.
  `retire.py` moves source data, rewrites derived indexes, and appends to an
  operational ledger in one pass. This is why R3 (§5.2) must be a declaration
  on the *artifact* rather than an attribute of the module.
- **Operational artifacts are physically nested inside the source surfaces.**
  `_pending_questions.md`, `_pending_merges.md`, `_calibration.jsonl`,
  `_reasoning_tier_decisions.jsonl`, `_schema/` and `_index.md` all live
  inside `wiki/`; `_resolved_contradictions.jsonl`, `_librarian-clusters.jsonl`,
  `answers/` and the per-scope `MEMORY.md` all live inside `raw/`. A
  wiki-vs-not-wiki split cannot address them. The contract therefore needs
  **prefixes within a surface**, not just surfaces — which is why `StoreKey`
  (§6.2) is `(surface, key)` with prefix-scoped listing rather than a flat
  per-surface namespace.

### 2.3.1 The artifacts the current three-surface model cannot name

The sweep surfaced a category that is neither raw, wiki, excluded, index, nor
an in-surface sidecar: **knowledge-root-level, operator-facing files.**

`knowledge_root/rules/*.yaml` (`rules.py:651-655`),
`knowledge_root/templates/` (`init.py:167-177`), the configurable preserved-log
directory (`rules.py:885-939`), `registry.json` (`corrections.py:270`),
`authority-manifest.yaml` (`authority.py:257,260`), `compiled-exempt.json`,
and `athenaeum.yaml` itself — which, note, lives *inside* the knowledge root
(`config.py:188,190,2212,2216`), so "the config file is outside the store" is
not true today.

These are authoritative, operator-authored, not knowledge, and not
reconstructible. north-star §2.7 already gives them a name — *"Rules are data;
humans adopt them"* — and §5.2's R3 gives them a class.

### 2.4 What already routes through an abstraction

Three chokepoints exist, and they are the right foundations to build on rather
than replace:

| Abstraction | Location | Coverage |
|---|---|---|
| `atomic_io.atomic_write_text` — same-dir temp + `fsync` + `os.replace` | `src/athenaeum/atomic_io.py:32` | 28 modules. Its docstring states the invariant as codebase-wide: *"Every store-path write anywhere in the codebase must route through `atomic_write_text`"* (`atomic_io.py:18-20`) |
| `storage.surface_root_for_class` — class → surface root | `src/athenaeum/storage.py:362` | 5 modules (§2.2) |
| Per-module `O_APPEND` + `fsync` ledger append | 12 modules, duplicated by explicit house style (`quarantine.py:102-104`) | 12 modules, no shared implementation |

The write path is therefore in good shape and the read path is not: there is no
read equivalent of `atomic_write_text`. Every read is an ad-hoc
`path.read_text()`, which is exactly why the walk costs in §3 are invisible to
anyone reading a single module.

The 12 duplicated ledger writers are the second finding: a durability
primitive copied twelve times is one that cannot be changed once, which is
precisely what a new backing store would require. §6.2's `append` exists to
collapse them.

Four sites re-implement the temp-file-plus-rename pattern by hand rather than
calling the helper — `search.py:684-686` (both index manifests),
`repair.py:91-100` (`_atomic_write`, used by three of four write paths),
`clusters.py:643-659` (`_atomic_replace`, the cluster report), and a plain
`write_text` for the vector generation stamp at `search.py:1032`. Together
with the 12 duplicated appenders that is **16 independent implementations of
two primitives**, none of which can be changed in one place. A remaining
handful of plain `write_text` calls are legitimate: a temp smoke-test store
(`_cmd_query.py:932`), fresh-store bootstrap (`init.py:117-203`), generated
docs (`prompt_registry.py:297-298`), and a user-specified report path
(`_cmd_curate.py:283`). None of the sixteen is a live defect today; every one
is a site a store migration has to account for.

### 2.5 Filesystem semantics with no portable equivalent

The inventory's conclusion. Each is load-bearing somewhere, and none has a
free equivalent in a database or object store:

| Semantic | Established at | Depends on it |
|---|---|---|
| Same-mount atomic rename, with permission-bit preservation | `atomic_io.py:53-63` | every whole-file write, plus the 4 hand-rolled copies |
| Single small `O_APPEND` write is atomic; a crash tears only the trailing line | the 12 appenders; readers are written to tolerate exactly that | every ledger |
| POSIX advisory `flock`; kernel releases on process death | `runlock.py:11-13`, `runlock.py:375-381` (absent on Windows — the run proceeds unguarded) | all mutual exclusion |
| **fd/inode identity** — `os.fstat(fd).st_ino == os.stat(path).st_ino` | `runlock.py:361` | the orphan-inode race guard after a `--force` lock break |
| **Inode equality as file identity** — `Path.samefile` | `pending_merges.py:897-901` (`_same_file`) | the athenaeum#748 guard against deleting the page being folded into |
| **Hardlinks across the store boundary** — a per-scope `MEMORY.md` inside `raw/` is hardlinked to a file *outside* the knowledge root | `memory_index.py` docstring; `retire.py` stats the sibling before dropping a pointer | index-pointer retirement |
| Seekable rewrite-in-place — `lseek` + `ftruncate` + `write` + `fsync` | `runlock.py` (`_write_metadata` / `heartbeat`) | lock heartbeats |
| Cheap same-tree `shutil.move`, after which a directory walk stops finding the file | `quarantine.py:244`, `quarantine.py:181-183` | quarantine and release |
| `mtime_ns` + `size` as a change token | `search.py:506`, `librarian.py:5404` | both incremental manifests |
| `sqlite3.connect` on a real file; chromadb's `PersistentClient` caching clients per path | `search.py:719`, `search.py:1258` | both persisted indexes |
| Exclusive-create — **absent**: filename minting probes `exists()` then writes, so collision avoidance is probabilistic, not `O_EXCL`-enforced | `storage.py:377-407` | raw-intake writes |
| `shutil.rmtree` + recreate as schema-mismatch repair | `search.py:1358-1360` | vector rebuild |

Two of these deserve to be called out as more than porting work. **The
hardlink** is a store artifact whose identity is shared with a file outside
the store — a relationship no non-filesystem adapter can represent at all, so
the pointer-retirement path needs a different mechanism rather than a
different backend. And the **missing `O_EXCL`** is a pre-existing weakness
that a concurrent backend would expose rather than introduce: `write_raw_intake`
retries on `exists()` (`storage.py:393-405`) and then hands off to an
unconditional `os.replace`. The contract's `put(..., expect=None)` is the
place to fix it, since a compare-and-swap against "no existing version" *is*
exclusive create.

The change-token row is the one that costs the most, and §3 is about it.

## 3. Performance: the index build is the binding constraint

### 3.1 The whole-store walks

The store is walked end-to-end by more paths than the index build, and only
three of them are incremental:

| # | Walk | Entry point | Incremental? |
|---|---|---|---|
| W1 | **Search index build** (FTS5 and vector share it) | `search._scan_indexed_records`, `src/athenaeum/search.py:450`, driven by `_scan_all_entries`, `search.py:363` | **Yes** — manifest + stat pre-filter (§3.3) |
| W2 | **Keyword scan-on-query** | `KeywordBackend.query`, `search.py:1753`; re-globs and re-reads every page on **every query** (`search.py:1781`, `search.py:1795`) | No — by design, no persisted index |
| W3 | **Raw-intake delta snapshot** | `librarian._raw_hash_snapshot`, `src/athenaeum/librarian.py:5362` | **Yes** — a *second*, independent stat+hash manifest |
| W4 | **Delta-scoped clustering** | `delta.compute_affected_clusters`, `src/athenaeum/delta.py:159` | **Yes** — but see the caveat below |
| W5 | **Auto-memory intake discovery** | `intake.discover_auto_memory_files`, `src/athenaeum/intake.py:115`; unconditional `read_text` per file at `intake.py:177` | No |
| W6 | **`_index.md` rebuild** | `librarian.rebuild_index`, `librarian.py:451`; full read of every page at `librarian.py:460` | No |
| W7 | **Dedupe person load** | `dedupe._load_persons`, `src/athenaeum/dedupe.py:288` | No |
| W8 | **~14 further full walks** — duplicate-topic lint (`authority.py:385`), status (`status.py:140`), registry build (`registry.py:147`), wiki-dedupe candidates (`wiki_dedupe.py:168`), repair (`repair.py:85`), entity load (`models.py:2286`), excluded-surface sweeps (`pii.py:1008`, `pii.py:1823`), bounce divergence (`bounce_divergence.py:288`), pending merges (`pending_merges.py:754`), corrections (`corrections.py:1518`), auto-memory prune (`auto_memory_prune.py:68`) | No |

**The caveat on W4 is the important one.** `delta.py` narrows the *expensive*
(LLM) work to affected clusters, but the discovery walk that produces its
input — W5 — has no stat or hash pre-filter at all and reads every raw
auto-memory file on every run (`intake.py:177`). A "delta" compile therefore
still pays a full O(N) read before the delta logic gets to narrow anything.

### 3.2 Per-page cost, counted

For one page during a **full** FTS5 or vector index build, exactly two
filesystem operations occur:

1. `path.stat()` — `search.py:503`
2. `path.read_bytes()` — `search.py:521`

Nothing else re-opens that page. `_row_for` (`search.py:737`) and the vector
`_add_records` path (`search.py:1187`) both reuse the already-decoded text and
frontmatter; the manifest is read once and written once per build, not per
page. Hashing and frontmatter parsing are CPU on bytes already in memory.

For an **incremental** build, an unchanged page costs **one** operation — the
`stat()` alone, because a `(mtime_ns, size)` match against the stored manifest
lets the stored hash be reused with no read (`search.py:507-519`). A changed
or added page costs the same two operations as the full-build case.

### 3.3 The finding: incrementality is what an adapter breaks

athenaeum#370's stat pre-filter is a *filesystem* optimisation. It trades an
expensive operation (read + hash) for a cheap one (`stat`) — and that trade is
only a win because a local `stat` is effectively free. It is exactly this
trade that a non-filesystem adapter inverts:

> A no-op incremental build — nothing changed at all — still issues **one
> store operation per page**. That cost does not shrink when the corpus is
> idle, and under an adapter with per-operation latency `L` it becomes `N · L`
> of pure round-trip on every single build.

Writing the arithmetic out, with `N` pages and `c` changed pages:

```
full build           X = 2N               →  Δt ≈ 2N · L
incremental build    X = N + c            →  Δt ≈ (N + c) · L
incremental, c ≈ 0   X = N                →  Δt ≈ N · L     ← does not shrink
```

`L` is adapter-dependent and this design deliberately states no value for it:
bounding `L` is the adapter author's job, and bounding `X` is athenaeum's.
**The operation-count multiplier is the part athenaeum controls, and it is
therefore the part the contract must fix.**

### 3.4 What is actually measured today

Only three figures exist in the tree. They are quoted rather than
extrapolated, because no others were found in `docs/`, `CHANGELOG.md` or
`tests/`:

- **Full index build:** *"FTS5 rebuild is ~1s on a 3k-page wiki, cheap next to
  vector's ~45s"* — `docs/recall-architecture.md:43`.
- **Query latency p95**, 200 synthetic pages, Apple Silicon:
  `keyword: 260.0` ms, `fts5: 1.2` ms —
  `tests/benchmarks/test_search_bench.py:39-46`. The keyword figure is high
  *because* it is W2: a whole-store re-read per query.
- **O(corpus)-per-call, in production:** resolving one excluded record per
  `uid` cost *"~28s each against the live corpus, ~37 hours for the 4,696
  people the weekly enrichment job resolves"* —
  `docs/one-way-in-one-way-out.md:118-121`. That number is why the batch
  `read_entities` shape exists at all.

The third is the cautionary one, and it did not need an adapter to happen. A
seam that is far too slow for a real workload is one a caller eventually
routes around (`docs/one-way-in-one-way-out.md:122-125`) — which is a
*correctness* failure of the two-path invariant, arriving dressed as a
performance problem. Adapter latency makes that failure mode cheaper to reach,
not new.

`tests/benchmarks/` measures queries only; `build_index()` is called inside
the benchmark tests but is never itself wrapped in `benchmark(...)`, so **no
build-time number is asserted anywhere in CI today**. Nothing would currently
notice a build-cost regression.

### 3.5 Constraints this locks

- **P1 — Bulk listing is mandatory.** The contract MUST expose a single call
  that returns `(key, version, size)` for every object under a prefix. An
  index build may issue O(1) listing calls, never O(N) individual `stat`s.
  This is the primitive that makes W1 viable at all over an adapter.
- **P2 — The change token is adapter-defined and opaque.** The manifest must
  store whatever the adapter says identifies a version (`mtime_ns:size` for a
  filesystem, an ETag, a row version, a content hash), and must never assume
  mtime semantics. Callers compare tokens for equality and nothing else.
- **P3 — Bulk read is mandatory.** The contract MUST expose a batched
  multi-key read so the `c` changed pages cost O(1) round trips, not O(c).
- **P4 — A per-query whole-store walk is not portable.** W2 (`KeywordBackend`)
  is viable only against an adapter that declares cheap local scanning. On any
  other adapter it must refuse loudly rather than run at 200×.
- **P5 — Op-count budgets are tested, wall-clock is not.** Adapter-latency
  regressions are invisible to a wall-clock benchmark run on a local disk. The
  guard is an operation-count assertion against a latency-injecting fake
  adapter, in `tests/benchmarks/` alongside the existing p95 harness.
- **P6 — The unincremental walks (W5–W8) are in scope for the inventory but
  not for the first slices.** They are correct today because local reads are
  cheap. Each one becomes an O(N)-round-trip liability under an adapter, and
  §9's S2 fixes only W1. The rest are named, tracked, and explicitly deferred
  rather than silently inherited.

## 4. Git versioning and recoverability

### 4.1 Git is not optional today — it is a precondition

The librarian refuses to start a mutating run when `knowledge_root/.git` does
not exist:

> *"No .git in … — refusing to run without a writable git repo. The
> librarian's pre-processing snapshot is load-bearing for raw-file recovery."*
> — `src/athenaeum/librarian.py:3067-3077`

Every run takes a whole-tree snapshot (`git status --porcelain` → `git add -A`
→ `git commit`, `librarian.py:495-521`). The documented recovery story is
explicit that this is the *only* backup:

> *"**Recovery is git-only.** The pass refuses to run when `knowledge_root` is
> not a git repo, and it never hard-`unlink`s."* — `README.md:735-786`, which
> also warns that `git gc`, a squash/rebase, or never pushing can lose retired
> raw permanently, and that *"the git-only recovery story only holds on the
> machine that ran the librarian"* absent the opt-in push.

So "the store is a git working tree" is not an operator convention athenaeum
happens to benefit from. It is a load-bearing assumption wired into a hard
precondition, and generalising the store to a database or object store
invalidates it outright.

### 4.2 Two distinct git users — do not conflate them

Only the first is in scope:

| | Git on the **knowledge store** | Git on **athenaeum's own repo** |
|---|---|---|
| What | snapshot, push/pull, retire/prune commits, `init` bootstrap | deploy-SHA stamping, version reporting |
| Where | `librarian.py:495`, `librarian.py:621`, `rules.py:957`, `retire.py:568`, `auto_memory_prune.py:113`, `filename_entity_prune.py:130`, `memory_index.py:180`, `corrections.py:1780`, `init.py:129`, `status.py:201` | `push_metrics.py:637`, `scripts/write_build_sha.py`, `deploy_check.py` |
| Takes | an explicit `knowledge_root` argument | resolves `Path(__file__).parents[N]` or a deploy tree |

They never intersect in code, and this design touches only the left column.
All of it is `subprocess.run(["git", …])`; there is no GitPython dependency.

### 4.3 Three tiers of destructive path, and one hole

Every operation that removes user data falls into one of three tiers:

- **Tier A — hard-gated on `.git`, refuses without it.** The move-then-retire
  pass (`retire.py:568-583`, which downgrades every `MOVE` to `SKIP`),
  `auto-memory prune` (`auto_memory_prune.py:136-143`),
  `prune-filename-entities` (`filename_entity_prune.py:130`), and `prune-index`
  (`memory_index.py:196-199`). These fail safe.
- **Tier B — silently degrades to a plain `unlink()` off-git.** Shape-rule
  retirement (`rules.py:992-993`) and field-correction batch retirement
  (`corrections.py:1830-1833`). Documented as a best-effort fallback for test
  fixtures — which is fine while "no git" is an edge case, and is exactly the
  wrong default the moment a non-filesystem adapter makes it the normal case.
- **Tier C — no gate at all.** `pending_merges._apply_fold_into_existing`
  deletes the folded-away source wiki page with a bare `src_path.unlink()`
  (`src/athenaeum/pending_merges.py:1001`). The module contains no git
  reference of any kind, and `resolve_merge` is an **MCP write tool**
  (`src/athenaeum/mcp_server.py:10`) invoked outside any `athenaeum run`, so
  the librarian's pre-run snapshot does not bracket it. Against the
  documented guarantee — *"a bad merge is a `git revert` away"*
  (`README.md:59-61`, `docs/why-athenaeum.md:239-240`) — this is a gap, and it
  is filed separately rather than folded into this design (§9.1).

Tier B and Tier C are the shape of the problem in miniature: **when the
recoverability substrate is absent, athenaeum currently deletes anyway.**

### 4.4 The decision: git is a capability of one adapter, not a property of the store

The `.git` existence check is the right *intent* expressed against the wrong
*subject*. What each Tier-A gate actually needs to know is "can this store give
me the data back?" — a question about capability, not about a directory name.

> **R1 — the recoverability rule.** A destructive store operation MUST either
> (i) run against a surface whose adapter declares `versioned`, having taken a
> restore point, or (ii) refuse. Silent degradation to an unrecoverable delete
> is prohibited on every adapter, including the filesystem one.

Under R1 the filesystem adapter implements `versioned` with exactly the git
code that exists today, moved behind the seam; the four Tier-A gates become
`store.capabilities.versioned` checks; and Tier B's silent fallbacks are
removed rather than generalised.

### 4.5 The tension R1 creates with athenaeum#718 — and why it is the design

Git's durability is precisely what makes erasure impossible. athenaeum#718
states it plainly: the wiki store is a git repository with history, clones and
remotes, *"so an in-git 'erasure' survives in history on every clone until a
rewrite is force-pushed everywhere"*. Erasure-class data therefore cannot live
on a versioned surface at all.

So `versioned` and `purgeable` are opposed capabilities, and a real deployment
needs both **at the same time**. That settles a question this design would
otherwise have had to guess at:

> **R2 — capabilities are declared per SURFACE, not per store.** A deployment's
> store is a *set* of surfaces with different capability profiles — a
> git-backed, versioned, non-purgeable `wiki` surface alongside an encrypted,
> purgeable, non-versioned `excluded` surface — not one uniform backend.

R2 falls out of the existing design rather than fighting it: `storage.mapping`
already routes a class to a surface, and `corpus_policy` already declares
per-surface capability bits. The store contract extends that table; it does not
introduce a second one.

R1 and R2 together also answer what "recoverable" means on a purgeable
surface: nothing on it is recoverable, by construction, and a destructive
operation there is permitted precisely because erasure is the point. What R1
forbids is the *silent* case — a delete on a surface that declares neither.

### 4.6 What else breaks, and what the contract owes each one

| Mechanism | Filesystem semantic it rests on | What the contract owes |
|---|---|---|
| `atomic_write_text` — same-dir temp + `fsync` + `os.replace` (`src/athenaeum/atomic_io.py:32-69`) | single-mount atomic rename | a portable atomic primitive: conditional write / compare-and-swap on an opaque version token. The filesystem adapter keeps the existing implementation. (Note: there is no directory `fsync` today, so the rename's own durability across a host crash is already unguaranteed — this design does not close that gap, it inherits it.) |
| Append-only ledgers — `O_APPEND` + `fsync`, duplicated per module by house style (`quarantine.py:95-111`, `pii.py:1319-1334`) | atomicity of a single small append | an explicit append primitive; without one, every ledger silently becomes a read-modify-write race |
| `RunLock` — advisory `fcntl.flock` on `<knowledge_root>/.athenaeum.lock` (`src/athenaeum/runlock.py:11-13`, `runlock.py:128`) | POSIX advisory locking, single machine; already documented as unreliable on NFS/SMB, and absent entirely on Windows (`runlock.py:375-381`) | a lease primitive with a TTL and renewal, mapping onto flock for the filesystem adapter and onto a lease row / conditional put elsewhere |
| Quarantine — `shutil.move` into `<wiki_root>/_quarantine/` (`quarantine.py:244`) | a cheap same-tree rename, plus the fact that a directory walk stops finding the moved file | a `move`/`rekey` primitive; "moved so discovery stops finding it" is a tree-shaped argument that a flat keyspace does not make for free |
| `athenaeum init` — `git init` + initial commit (`init.py:129-154`) | a fresh directory tree is a fresh repo | an adapter-supplied bootstrap; "create an empty store" has no filesystem-independent meaning today |

### 4.7 Provenance survives a restore today by accident

Every durable ledger except one lives *inside* `knowledge_root`, and none is
gitignored — `_quarantine.jsonl` under the wiki root (`quarantine.py:59-75`),
`_merge_provenance.jsonl` (`provenance.py:427-431`), `_pending_retractions.jsonl`
(`retraction_cascade.py:26-28`), `_resolved_contradictions.jsonl` under `raw/`
(`fingerprint.py:61`), and the contact-observation ledgers under the excluded
surface root (`pii.py:1309-1314`). So `git add -A` sweeps them up, and a
store-level restore restores the audit trail together with the content it
attests to.

That coupling is **incidental — same directory, same commit — not designed.**
Under an adapter where content and ledger could be separate stores, "restore
the content" and "restore the ledger that explains it" become two operations
that can diverge. The contract must make the coupling explicit (§5.3).

The one exception is the LLM-observation ledger, which resolves under the
*cache* dir rather than the store (`llm_schemas.py:391`, via
`config.resolve_cache_dir`) — a durable, non-reconstructible record living in
a directory whose name promises the opposite. §5.2 classifies it.

## 5. The logical model boundary — confirmed, with one addition

### 5.1 Confirmed: raw + wiki authoritative, indexes derived

The boundary north-star §2.11 fixes is **confirmed unchanged**, and it already
holds physically as well as logically: every derived index lives under the
cache root (`config.resolve_cache_dir`, `src/athenaeum/config.py:128`; default
`~/.cache/athenaeum`, `docs/configuration.md:71`), outside `knowledge_root`
entirely, and is explicitly framed as *"not recovery-critical (recovery is
git-based)"* (`docs/configuration.md:56`). The FTS5 database, the chromadb
collection, both manifests, and the vector `.generation` stamp
(`search.py:990-1037`) are all reconstructible from raw + wiki by definition.

Nothing in this design changes that, and three adjacent rejections in
north-star §3 stay rejected: raw is still not indexed directly, duration is
still frontmatter rather than a second store, and there is still one read seam.

### 5.2 The addition: the two classes the model is silent about

The two-way split — *authoritative source* and *derived index* — has no place
for the largest bucket in the inventory. `store-state` is ~145 sites across 27
modules (§2.2.1), larger than compiled wiki pages, and every one of those
artifacts is authoritative (nothing else records what it records) and **not
reconstructible from raw + wiki**:

| Artifact | Where it resolves | Reconstructible? |
|---|---|---|
| `_pending_questions.md` / `_pending_merges.md` + archives (`atomic_io.py:2-8`) | wiki root | no |
| `_quarantine.jsonl` (`quarantine.py:59-75`) | wiki root | no |
| `_merge_provenance.jsonl` (`provenance.py:427-431`) | wiki root | no |
| `_pending_retractions.jsonl` (`retraction_cascade.py:26-28`) | wiki root | no |
| `_calibration.jsonl`, `_reasoning_tier_decisions.jsonl`, `_axiom_governance.jsonl`, `_corrections_applied.jsonl`, `_shape_rules_applied.jsonl` | wiki root | no |
| `_resolved_contradictions.jsonl` (`fingerprint.py:61`) | `raw/` | no |
| `_observations.jsonl` / `_observation_supersessions.jsonl` (`pii.py:1309-1314`) | excluded surface root | no |
| `observations.jsonl`, `spend.jsonl`, `_push_records.jsonl` (`llm_schemas.py:391`, `config.py:1449`, `push_metrics.py:132`) | **cache dir** | no |
| ingest / auto-memory / full-compile manifests (`librarian.py:5437-5647`), FTS5 + vector manifests, `.generation` (`search.py:576-686`, `search.py:1016`) | **cache dir** | yes — from a full rebuild |
| `detection_incomplete.json`, `zero_yield_state.json`, killswitch `disabled` | **cache dir** | no, but machine-scoped |
| the off-corpus ledger shard athenaeum#718 adds | purgeable off-corpus store | no |

And a second unnamed group, from §2.3.1: `rules/*.yaml`, `templates/`, the
preserved-log directory, `registry.json`, `authority-manifest.yaml`,
`compiled-exempt.json`, and `athenaeum.yaml` — operator-authored declarations
that live inside the knowledge root and shape behaviour.

Calling any of these "derived" licenses a rebuild to destroy them. Calling
them "source of truth" puts a spend ledger on the same footing as a compiled
entity page. Neither is right, and the ambiguity is already visible in the
table: several sit in a cache directory, which is the classification mistake
made concrete.

> **R3 — every store artifact declares exactly one class:
> `source` | `derived` | `operational` | `config`,** and every `operational`
> artifact additionally declares a scope of `store-durable` or
> `machine-local`.
>
> - **`source`** — raw intake and compiled wiki pages. Versioned; never
>   destroyed by a rebuild. Unchanged from north-star §2.11.
> - **`derived`** — indexes, manifests, generation stamps, the cluster report.
>   May be destroyed and rebuilt at any time; needs no durability guarantee
>   beyond its own generation stamp.
> - **`operational`** — ledgers, queues and decision state. Durable,
>   append-only, **not** rebuildable. A `store-durable` one must be restored
>   together with the `source` it attests to; a `machine-local` one
>   (manifests' stat cache, killswitch, detection state) is per-machine by
>   design and must **not** travel with a restore or a sync.
> - **`config`** — operator-authored declarations (`rules/`, `templates/`,
>   the authority manifest, `athenaeum.yaml`). Authoritative, durable, not
>   knowledge, and never LLM-written. north-star §2.7 already names the
>   principle — *"Rules are data; humans adopt them"* — R3 gives it a
>   persistence class.

The `operational` scope split answers the question the cache dir raises
directly: **machine-local state stays machine-local and outside the adapter;
store-durable state moves behind it.** That is why a spend ledger and an FTS5
manifest currently sitting in the same directory belong on opposite sides of
the seam — and why "does the whole-store adapter cover the cache dir?" has an
answer rather than a shrug.

This is an **addition to** the logical model, not a revision of it. The
raw + wiki / index boundary north-star §2.11 fixes is untouched; R3 names the
classes that boundary was silent about. If north-star §2.11 is ever restated
it should read *"raw + wiki as source of truth, indexes derived, operational
state durable, config authored"* — a wording change, not a design change.

### 5.3 Consequence for the contract

R3 gives §4.7's incidental coupling a name: an `operational` artifact and the
`source` it attests to must share a restore point. On the filesystem adapter
that is free, because both are inside the one git tree. On any other adapter
it is a requirement the adapter must satisfy or explicitly refuse.

## 6. The published store contract (draft)

This is the extension point athenaeum#911 asks to have drafted. It is a
**draft for publication**, not a published surface: like the rest of
`athenaeum.storage` it stays off the stable `__all__` surface until §9's S7
promotes it, backed by the conformance suite S1 ships (the same staging the
existing seam used — `docs/storage-adapter-contract.md:242-253`).

### 6.1 Design decisions

- **D1 — the unit is a record, not a path.** Every consumer wants frontmatter
  plus body, and the seam should hand it over in one call. Fusing list, read
  and parse into one iteration is what makes an adapter viable (§3.5, P1/P3);
  handing back a `Path` for the caller to open is what makes it unviable.
- **D2 — keys, not paths.** A `StoreKey` is a surface plus a POSIX-style
  relative key. `Path` becomes an implementation detail of the filesystem
  adapter, exposed only through the explicitly-nullable `local_path_for`
  escape hatch, which every caller must be able to do without.
- **D3 — versions are opaque tokens.** Compared for equality, never parsed,
  never assumed to be a time (§3.5, P2).
- **D4 — capabilities are declared, per surface, and checked** (§4.4 R1,
  §4.5 R2). An adapter that omits a capability does not get a best-effort
  emulation; callers take the declared alternative, or refuse (§7).
- **D5 — the existing seam is extended, never forked.** `resolve_adapter_for_class`
  keeps routing classes to surfaces; the store hangs off the resolved adapter.
  A parallel API alongside `surface_root_for_class` would be exactly the forked
  seam `docs/one-way-in-one-way-out.md:56-60` rules out.
- **D6 — fail closed, loudly.** Inherited verbatim from the existing layer: an
  omitted capability defaults to absent, and a misconfiguration raises rather
  than falling back to the default surface (`storage.py:72-80`).

### 6.2 The protocol

```python
@dataclass(frozen=True)
class StoreKey:
    surface: str          # the storage-adapter surface name
    key: str              # POSIX-style relative key; never an OS path

@dataclass(frozen=True)
class ObjectMeta:
    key: StoreKey
    version: str          # opaque; equality only (D3)
    size: int

@dataclass(frozen=True)
class StoreCapabilities:
    # persistence class support (R3)
    classes: frozenset[str]        # {"source","derived","operational","config"}
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
    local_path_for: Callable[[StoreKey], Path] | None

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

    # --- recoverability (R1) ----------------------------------------
    def snapshot(self, label: str) -> str | None: ...

    # --- concurrency -------------------------------------------------
    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]: ...

    # --- lifecycle ---------------------------------------------------
    def bootstrap(self) -> None: ...
```

Four of these earn their place against a specific finding rather than by
symmetry with a filesystem: `iter_meta` replaces N `stat()` calls with one
listing (P1); `read_many` replaces N reads with one batch (P3); `put(...,
expect=...)` is the portable form of temp-plus-rename (§4.6); and `snapshot`
is what a Tier-A gate asks instead of "does `.git` exist" (R1).

`iter_records` is the one convenience: `iter_meta` + `read_many` +
`parse_frontmatter` is what nearly every caller does, and offering it as one
call is what stops callers rebuilding a slower version themselves.

### 6.3 What the contract deliberately does not have

- **No transactions across keys.** Nothing in the inventory needs one, and
  requiring it would exclude object stores as adapters. Multi-object
  consistency is achieved with `snapshot` plus the R3 restore-together rule,
  not with a distributed transaction.
- **No directory concept.** Prefixes only. The tree shape is a filesystem
  detail, and quarantine's "move it so the walk stops finding it"
  (`quarantine.py:181-183`) is rewritten as a key change, not a directory move.
- **No search or query.** Indexes are derived (§5.1); a store that answered
  queries would be a second read seam.
- **No `exists()`.** `iter_meta` and a `None` from a read answer it, and a
  standalone existence check is the round trip callers most easily scatter.

## 7. Capability degradation and the honest-refusal rule

> **R4 — no silent degradation.** A caller that needs an absent capability has
> exactly three permitted responses: use the declared alternative primitive;
> raise a loud, named error; or run in an explicitly documented reduced mode
> that the operator opted into. Doing the thing anyway, less safely, is not one
> of them.

R4 is not new policy — it is the existing layer's rule
(`StorageConfigError`'s *"never silently falls back to the default surface"*,
`storage.py:72-80`; the fail-closed `corpus_policy`,
`docs/storage-adapter-contract.md:175-181`) applied to the physical layer.
Concretely:

- A destructive path on a surface declaring neither `versioned` nor
  `purgeable` **refuses** (R1). This removes Tier B's silent `unlink`.
- `KeywordBackend` on a surface without `cheap_local_scan` **refuses** with a
  message naming FTS5 as the supported backend (P4). It does not quietly pay
  `N · L` of round-trip on **every query** — which, unlike the build path, no
  amount of incrementality can amortise, because the walk is the query
  (§3.1 W2).
- An index build against a store without `bulk_list` **refuses**, rather than
  falling back to per-page `iter_meta` calls and turning a 3k-page corpus into
  3,000 round trips (§3.3).
- A caller needing `local_path_for` on an adapter that returns `None` **must**
  have a non-path route. This is the check that keeps D2 honest: any caller
  that cannot be written without a real path is a caller the seam has not
  actually generalised, and it should be reported as such rather than papered
  over with a temp-file materialisation.

## 8. Re-scoping athenaeum#718

athenaeum#718 ("Memory model v6") is on hold and must be re-scoped against
this design. Its two surfaces have **opposite** relationships to this work, and
the re-scope is to stop treating them as one deliverable:

**Surface 1 — tiering and push budget.** Retrieval-cost tiers, a
token-denominated push budget, coordinate-fit ranking, and the recall-hit
header. None of it touches storage physics; the one storage-adjacent element
is its explicit reuse of the existing `embedded: false` adapter flag as the
cold-tier mechanism, which this design leaves untouched. **Unaffected —
proceeds on its existing dependency chain** (athenaeum#714, itself gated on
athenaeum#711's baseline snapshot).

**Surface 2 — off-corpus indexable mode, ledger shard, erasure taint.** Every
one of its acceptance criteria is a store-contract capability under this
design, and building it before the contract exists means building a bespoke
second store:

| athenaeum#718 acceptance criterion | Becomes, under this design |
|---|---|
| off-corpus adapter gains indexable mode, its own vector collection and index shard outside git | a surface declaring `purgeable` + `derived`-class support (R2, R3) |
| erasure is a single-store delete of content + index shard + pointers | `delete` on a `purgeable` surface, with the R3 restore-together rule inverted into an erase-together rule |
| ledger shard in the purgeable store, never the in-git ledger | an `operational`-class artifact on a `purgeable` surface (R3) |
| HMAC-keyed erasure-class hashes | unchanged — a content-policy decision above the store, not a storage capability |
| recall federates across corpus and off-corpus indexes | unchanged — a read-path change; the store contract supplies the surfaces, not the federation |

**The re-scope, stated explicitly:**

1. **Split athenaeum#718 along its own two surfaces**, into two issues. It
   currently requires both to be green to merge, on the reasoning that
   splitting them *"would leave a half-wired store"* — that reasoning is what
   this design invalidates, because the store wiring becomes the contract's
   job rather than athenaeum#718's.
2. **Surface 1 keeps athenaeum#718's existing gates** and is not blocked by
   this design.
3. **Surface 2 becomes `blocked_by` slices S1 and S3** (§9), and its scope
   narrows from "build a purgeable indexed off-corpus store with a ledger
   shard" to "declare and consume `purgeable` surfaces, and put the ledger
   shard on one." The HMAC-keying and erasure-taint criteria stay with it
   unchanged.
4. **What must NOT happen:** athenaeum#718 implementing its own storage
   abstraction for the off-corpus half. Two storage abstractions is the forked
   seam D5 exists to prevent, and it is the specific failure this re-scope is
   filed to avoid.

athenaeum#764 (the librarian's wall-clock exhaustion) is **not** re-scoped by
this design, but it is the reason P5 and P6 are stated as constraints rather
than aspirations: a nightly run that already cannot finish inside its deadline
has no headroom to absorb `N · L` of new round-trip latency (§3.3).

## 9. Implementation slices

Each slice is one session's work, in dependency order. `blocked_by` edges are
native, not prose.

### 9.1 Filed separately, not a slice

The Tier-C gap found while inventorying §4.3 —
`pending_merges._apply_fold_into_existing` deleting a wiki page with an
ungated `unlink()` (`pending_merges.py:1001`) — is a **live defect against
today's documented guarantee**, independent of this design. It is filed as its
own issue rather than folded into a slice, because it should be fixed whether
or not the store generalisation ever ships.

### 9.2 The slices

| Slice | Scope | MoSCoW | `blocked_by` |
|---|---|---|---|
| **S1 — store protocol + filesystem adapter** | New `athenaeum/store.py` (L0/L1) carrying `StoreKey`, `ObjectMeta`, `StoreCapabilities`, `Store`, and a `FilesystemStore` implementing it over `atomic_io` + `pathlib`. `resolve_store_for_class()` alongside the existing `surface_root_for_class()`. **No callers migrated.** Acceptance: a conformance suite that an in-memory fake and `FilesystemStore` both pass, modelled on `tests/test_storage_enforcement.py::TestAdapterExtensionPointContract`. | must | — |
| **S2 — index build on the bulk primitives** | `search._scan_indexed_records` consumes `iter_meta` instead of per-page `stat()`, and `read_many` for the changed set. Manifest stores the opaque `version` token (P2) with a schema stamp; a stamp mismatch forces one full re-hash, which athenaeum#373's staleness backstop already models. Acceptance: an op-count assertion against a latency-injecting fake adapter (P5) — one listing call plus `c` batched reads, for any `N`. | must | S1 |
| **S3 — recoverability capability replaces the `.git` gate** | `snapshot()` on the protocol; `FilesystemStore` implements it with the existing `librarian.git_snapshot` code moved behind the seam. The four Tier-A gates become `capabilities.versioned` checks; Tier B's two silent `unlink` fallbacks are removed (R1). Acceptance: a destructive operation against a fake adapter declaring neither `versioned` nor `purgeable` refuses, with a test per Tier-A and Tier-B site. | must | S1 |
| **S4 — lease primitive** | `Store.lease()`; `FilesystemStore` implements it over the existing `flock` + heartbeat + inode-race hardening; `runlock` migrates onto it. Acceptance: the existing `runlock` test suite passes unchanged against the filesystem adapter, plus a fake-adapter lease-expiry test. | should | S1 |
| **S5 — artifact classification** | Every artifact in §5.2's tables declares one of the four R3 classes, and every `operational` one declares `store-durable` or `machine-local`. The 12 duplicated `O_APPEND` appenders collapse onto the contract's `append`; the cache-dir-resident artifacts are split by scope — durable ledgers move behind the seam, machine-local state stays out of it. The `config` class picks up `rules/`, `templates/`, the authority manifest and `athenaeum.yaml` (§2.3.1). Acceptance: a test enumerating every store artifact and asserting each declares exactly one class, and exactly one scope where the class is `operational`. | should | S1 |
| **S6 — keyword backend declares its requirement** | `KeywordBackend` refuses on a surface without `cheap_local_scan`, naming FTS5 (P4, R4). Small. | should | S1 |
| **S7 — publish the contract** | Promote `athenaeum.store` onto the stable `__all__` surface; publish §6 as `docs/store-contract.md`; ship the S1 conformance suite as a runnable third-party adapter-authoring harness, alongside the existing `adapter-authoring` skill's intake-adapter counterpart. | could | S1, S2, S3, S4, S5, S6 |

**Coverage of the walks in §3.1:** S2 migrates W1 (the search index build);
S6 gates W2 (keyword scan-on-query). **W3 through W8 are deliberately not in
these slices** — the raw-intake manifest, delta clustering, auto-memory
discovery, the `_index.md` rebuild, dedupe's person load, and the ~14 further
full walks in W8. Every one is a real O(N)-round-trip liability under an
adapter and every one is named here so it is tracked rather than silently
inherited; each becomes its own follow-up once S2 has demonstrated the
pattern on W1. Sequencing them behind a proven pattern is the point —
converting all of them at once is the cutover §10 rejects.

## 10. Rejected alternatives

- **Migrate every module to a `Store` handle in one change.** About 65 of 104
  modules touch the store across roughly 420 classified sites (§2). A single
  cutover has no intermediate state that is both shippable and verifiable, and
  no slice of it delivers value on its own. S1's "no callers migrated" is the
  deliberate opposite: the seam lands, proven by a conformance suite, before
  anything moves onto it.
- **Add a parallel store API and leave `surface_root_for_class` in place
  permanently.** Two ways to reach the store is the forked seam
  `docs/one-way-in-one-way-out.md:56-60` names as the failure mode, and the
  fork would be invisible in tests because both paths work on a filesystem.
  D5's "extend, never fork" is the alternative, with `local_path_for` as a
  declared, nullable escape hatch rather than a second front door.
- **A FUSE / virtual-filesystem layer, keeping every caller path-based.** It
  would make encrypted and cloud-synced backends work with no code change at
  all — and it fixes nothing this design is about. Round-trip counts stay
  O(N) (§3.3), there is no capability declaration so R1 and R4 have nothing to
  check, and it imports a platform dependency athenaeum does not otherwise
  have.
- **Require every adapter to be git-backed.** It preserves the recoverability
  story exactly and is the smallest possible change to §4 — and it forecloses
  athenaeum#718's erasure requirement outright (§4.5), which is a shipped
  design commitment, not a hypothetical.
- **Classify the §5.2 ledgers as `derived`.** Tempting because most already sit
  beside the indexes in the cache dir. Rejected: a rebuild is licensed to
  destroy `derived` artifacts, and these are not reconstructible from raw +
  wiki. The classification mistake is the *reason* two of them are in a cache
  directory, not a justification for it.
- **Delete the keyword backend rather than gate it.** Out of scope, and it is
  the documented zero-setup fallback
  (`tests/benchmarks/test_search_bench.py:41-44`). S6 constrains where it may
  run; it does not remove it.
