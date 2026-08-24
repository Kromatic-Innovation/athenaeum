# SPDX-License-Identifier: Apache-2.0
"""Off-corpus indexable storage — the purgeable surface (issue athenaeum#984).

Split (b) of the athenaeum#718 three-way re-scope
(``docs/whole-store-adapter-design.md`` §8), built on top of **S1**
(issue athenaeum#976 — the ``Store`` protocol + ``FilesystemStore``) and
**S3** (issue athenaeum#978 — ``snapshot()``/the ``versioned`` capability).
Both slices are closed; this module is the first real consumer of the
``purgeable``/``versioned`` capability distinction they shipped.

**Why this exists.** The wiki store is a git repository with history, clones
and remotes, so an in-git "erasure" survives in history on every clone until
a rewrite is force-pushed everywhere. Erasure-class data therefore lives only
in an off-corpus adapter — but unrecallable memory is useless memory, so
that adapter needs an index. This module gives it one: its own FTS5 +
vector index shard (:func:`build_off_corpus_index`), a single-store erasure
delete that prunes content and both index shards together
(:func:`erase_off_corpus_record`), and a ledger shard for verdicts that
touch off-corpus claims (:func:`append_verdict_off_corpus`) so they never
land in the in-git ledger athenaeum#712 (:mod:`athenaeum.verdicts`) built.

**Design-lock §8.5 compliance — no second storage abstraction.** Every
physical read/write in this module goes through the SAME :class:`athenaeum.store.Store`
protocol and :class:`athenaeum.store.FilesystemStore` implementation S1
shipped — :func:`off_corpus_store` builds a second *instance* of exactly
that class, addressing a second, physically distinct root, which is the
whole point of the store contract being an adapter seam (design note §1:
"a deployment may back... any excluded surface with encrypted storage, a
database, or a synced filesystem, and no caller can tell"). The purgeable
surface itself is declared through the EXISTING ``storage.mapping``/
``storage.adapters`` config layer (:mod:`athenaeum.storage`, issue
athenaeum#429/#532) — this module adds no new routing concept, only a new
config knob (``off_corpus.adapter``) naming WHICH already-configured
adapter is the off-corpus one. Its ``derived``/``operational`` artifacts
(the two index shards, the ledger shard) are declared in
:data:`athenaeum.store.ARTIFACT_REGISTRY` (R3), the same catalogue every
other store artifact in this repo declares through.

**Why a genuine erasure needs a SEPARATE root, not just a gitignored
subdirectory.** :class:`~athenaeum.store.FilesystemStore`'s ``versioned``
capability is declared from whether the *store's own* ``knowledge_root``
constructor argument has a ``.git`` directory (:mod:`athenaeum.store`'s
``FilesystemStore.__init__`` docstring). If the off-corpus surface's root
were merely a subdirectory of the operator's real (git-tracked)
``knowledge_root`` — even a ``.gitignore``'d one — a stray ``git add -A``
run against the KNOWLEDGE root (for example the wiki store's own
:meth:`~athenaeum.store.Store.snapshot`) could still sweep it into history,
silently defeating the whole point of ``purgeable``. So this module refuses
(loudly — D6) to build an off-corpus store whose resolved root sits inside
the configured ``knowledge_root`` at all (see :func:`off_corpus_root`) —
the physical isolation is enforced, not merely declared, and the
``FilesystemStore`` this module constructs is given ITS OWN root as its
``knowledge_root`` constructor argument (not the operator's real one), so
its ``versioned`` capability reads ``False`` (no ``.git`` there) and
``purgeable`` reads ``True`` for a genuinely, physically correct reason.

**Judgement call — where the purgeable store lives, and index-vs-ledger
scope.** The off-corpus INDEX shards (FTS5 db + vector collection) live
under ``<cache_dir>/off-corpus/`` — the SAME per-machine, never-git-tracked
cache dir every other derived index artifact already lives in (design note
§6.4's R3 scope note); they are reconstructible from the off-corpus store's
content by a rebuild, exactly like the main corpus index. The off-corpus
CONTENT and LEDGER SHARD (not reconstructible, per R3 "source"/"operational"
classes) live under the operator-configured, git-tree-external
``off_corpus.adapter`` root instead — never the cache dir, which is "the
one directory a user would feel safe deleting" (design note §6.4's R3
corollary, the same reasoning that relocated ``spend.jsonl`` et al. off the
cache dir in S5/athenaeum#980).

Layering: L3 domain module, a peer of :mod:`athenaeum.quarantine` and
:mod:`athenaeum.verdicts`. Imports :mod:`athenaeum.store` (L0/L1) and
:mod:`athenaeum.storage` (L2) upward, and :mod:`athenaeum.search` (L3)
function-locally (mirroring how :mod:`athenaeum.librarian` already imports
``search`` lazily, so importing this module costs nothing extra at process
start). Deliberately has NO import of :mod:`athenaeum.verdicts` — the
verdict ledger shard's caller (:func:`athenaeum.verdicts.record_pair_decision`)
imports THIS module, one direction only, and passes an already-serialized
``dict`` to :func:`append_verdict_off_corpus` rather than this module
importing :class:`~athenaeum.verdicts.VerdictEntry`, so the two modules can
never form an import cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from athenaeum.config import (
    resolve_off_corpus_adapter_name,
    resolve_off_corpus_enabled,
)
from athenaeum.storage import StorageAdapter, available_adapters
from athenaeum.store import FilesystemStore, Store, StoreKey

log = logging.getLogger(__name__)


class OffCorpusConfigError(ValueError):
    """Raised when ``off_corpus.*`` config is missing, unknown, or names a
    surface that resolves inside the git working tree (D6: fail closed,
    loudly — mirrors :class:`athenaeum.storage.StorageConfigError`)."""


#: Cache-dir subdirectory the off-corpus FTS5 db + vector collection live
#: under — a sibling of the main corpus index, never git-tracked (see
#: module docstring).
_OFF_CORPUS_CACHE_SUBDIR = "off-corpus"

#: Ledger-shard directory name, mirroring :data:`athenaeum.verdicts.LEDGER_DIRNAME`
#: (the in-git ledger's own directory name) so the two shards are
#: recognizable as the same concept at two locations, never confusable as
#: two different concepts.
LEDGER_DIRNAME = "_verdicts"


def off_corpus_adapter(config: dict[str, Any] | None) -> StorageAdapter | None:
    """Resolve the configured off-corpus :class:`~athenaeum.storage.StorageAdapter`.

    ``None`` when :func:`athenaeum.config.resolve_off_corpus_enabled` is
    ``False`` (the default) — every other function in this module treats
    that as "the off-corpus subsystem is dark," per the issue's Wiring AC.
    Raises :class:`OffCorpusConfigError` when enabled but
    ``off_corpus.adapter`` is unset or names an adapter
    ``storage.adapters``/``storage.mapping`` does not know about — never a
    silent fallback (D6).
    """
    if not resolve_off_corpus_enabled(config):
        return None
    name = resolve_off_corpus_adapter_name(config)
    if not name:
        raise OffCorpusConfigError(
            "off_corpus.enabled is true but off_corpus.adapter names no "
            "storage.adapters entry — set off_corpus.adapter to the name of "
            "the storage.adapters surface that backs the off-corpus store "
            "(see docs/configuration.md 'Off-corpus store')"
        )
    adapters = available_adapters(config)
    adapter = adapters.get(name)
    if adapter is None:
        raise OffCorpusConfigError(
            f"off_corpus.adapter names unknown storage adapter {name!r}; "
            f"known adapters: {sorted(adapters)}"
        )
    return adapter


def off_corpus_root(config: dict[str, Any] | None, knowledge_root: Path) -> Path | None:
    """Resolve and validate the off-corpus surface's on-disk root.

    ``None`` when off-corpus is disabled. Raises :class:`OffCorpusConfigError`
    (fail-closed, D6) when the configured adapter's ``surface_root`` resolves
    to ``knowledge_root`` itself or a path beneath it — see the module
    docstring's "why a genuine erasure needs a separate root" for why this is
    enforced rather than merely documented.
    """
    adapter = off_corpus_adapter(config)
    if adapter is None:
        return None
    resolved_knowledge_root = Path(knowledge_root).resolve()
    root = adapter.resolve_root(resolved_knowledge_root).resolve()
    if root == resolved_knowledge_root or resolved_knowledge_root in root.parents:
        raise OffCorpusConfigError(
            f"off_corpus adapter {adapter.name!r} surface_root {root} resolves "
            f"inside knowledge_root {resolved_knowledge_root} — an off-corpus "
            "surface must live OUTSIDE the git working tree for a delete to be "
            "a true erasure (docs/whole-store-adapter-design.md §4.4/§8); set "
            "storage.adapters.<name>.surface_root to an absolute path outside "
            "the knowledge root"
        )
    return root


def off_corpus_store(config: dict[str, Any] | None, knowledge_root: Path) -> Store | None:
    """Build the off-corpus :class:`~athenaeum.store.Store`. ``None`` when disabled.

    Constructed with the off-corpus root itself as the ``knowledge_root``
    constructor argument (not the operator's real knowledge root) — see the
    module docstring. This is what makes ``capabilities.versioned`` read
    ``False`` and ``capabilities.purgeable`` read ``True`` for a genuinely
    correct reason rather than a declared-but-unverified one.
    """
    adapter = off_corpus_adapter(config)
    if adapter is None:
        return None
    root = off_corpus_root(config, knowledge_root)
    assert root is not None  # off_corpus_adapter already returned non-None
    return FilesystemStore(root, {adapter.name: root})


def off_corpus_cache_dir(cache_dir: Path) -> Path:
    """The off-corpus index shards' cache-dir subdirectory (see module docstring)."""
    return Path(cache_dir) / _OFF_CORPUS_CACHE_SUBDIR


def build_off_corpus_index(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    cache_dir: Path,
    *,
    incremental: bool = True,
) -> dict[str, int] | None:
    """Build the off-corpus FTS5 AND vector index shards — both, always, so
    the federated recall merge (:mod:`athenaeum.mcp_server`) stays hybrid
    lexical+vector the same way the main corpus is (issue athenaeum#984
    judgement call: "hybrid lexical+vector layers stay both load-bearing
    across the federation").

    ``None`` (a strict no-op — the wiring the nightly librarian run, issue
    athenaeum#984 Wiring AC, calls unconditionally from
    :func:`athenaeum.librarian.reindex`) when off-corpus is disabled.
    ``config=None`` is passed to the underlying ``build_*_index`` calls
    deliberately: the off-corpus adapter's own ``corpus_policy.embedded`` is
    ``False`` (by design — see docs/configuration.md — so the MAIN corpus
    build skips this content), and that SAME global per-class flag would
    also skip it here if a real ``config`` were threaded through, since
    ``is_embedded`` has no notion of "which index build is asking." Passing
    ``None`` here is the same pattern the main corpus build already uses for
    the same reason.
    """
    root = off_corpus_root(config, knowledge_root)
    if root is None:
        return None

    from athenaeum.search import build_fts5_index, build_vector_index

    oc_cache = off_corpus_cache_dir(cache_dir)
    oc_cache.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {
        "fts5": build_fts5_index(root, oc_cache, incremental=incremental, config=None),
    }
    try:
        counts["vector"] = build_vector_index(
            root, oc_cache, incremental=incremental, config=None
        )
    except ImportError:
        log.info(
            "off_corpus: vector backend unavailable (install athenaeum[vector]) — "
            "off-corpus vector shard skipped, fts5 shard still built"
        )
    return counts


def query_off_corpus(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    cache_dir: Path,
    query: str,
    *,
    backend_name: str,
    top_k: int,
    caller_audience: set[str] | None = None,
    type_filter: Any = None,
) -> tuple[list[tuple[str, str, float]], Path] | None:
    """Query the off-corpus index shard. ``None`` when off-corpus is disabled.

    Returns ``(hits, off_corpus_root)`` — the caller (:mod:`athenaeum.mcp_server`)
    needs the root to resolve hit filenames back to on-disk paths, the same
    way it already does for ``extra_roots``. Degrades to an empty hit list
    (never raises) on an unbuilt/degraded off-corpus index or an
    unrecognized backend name — a caller must be able to merge this result
    into the primary corpus recall unconditionally without a try/except of
    its own; a bad off-corpus index must never fail an otherwise-good
    corpus recall.
    """
    root = off_corpus_root(config, knowledge_root)
    if root is None:
        return None

    from athenaeum.search import DegradedIndexError, get_backend

    oc_cache = off_corpus_cache_dir(cache_dir)
    try:
        backend = get_backend(backend_name)
    except KeyError:
        return [], root
    try:
        hits = backend.query(
            query,
            oc_cache,
            n=top_k,
            wiki_root=root,
            caller_audience=caller_audience,
            type_filter=type_filter,
        )
    except (DegradedIndexError, NotImplementedError):
        return [], root
    return hits, root


def merge_ranked_hits(
    primary: Sequence[tuple[str, str, float]],
    off_corpus_hits: Sequence[tuple[str, str, float]],
    top_k: int,
) -> list[tuple[str, str, float]]:
    """Merge two ranked hit lists from the SAME backend/scorer by score,
    descending (issue athenaeum#984 judgement call: "how federated recall
    merges two ranked result sets without either index silently
    dominating").

    Valid ONLY because both lists come from calling the identical backend's
    ``.query()`` — once against the corpus cache dir, once against the
    off-corpus cache dir — so their scores share one scale. (This is why
    federation always queries the off-corpus shard with the SAME
    ``backend_name`` the primary query used, never a different one — mixing
    an FTS5 BM25 score with a vector cosine-distance score would make this
    sort meaningless, the same reason the existing FTS5-then-vector shell
    hook concatenates instead of sorting across backend types.) A stable
    sort keeps ``primary`` before ``off_corpus_hits`` on an exact score tie
    — ties are rare for float scores and this never lets one side starve
    the other outright, since sort stability only breaks ties, not order.
    """
    merged = list(primary) + list(off_corpus_hits)
    merged.sort(key=lambda hit: hit[2], reverse=True)
    return merged[:top_k]


def erase_off_corpus_record(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    cache_dir: Path,
    relpath: str,
) -> bool:
    """Single-store erasure delete (issue athenaeum#984 AC2): content + both
    index shards + pointers, in one call.

    ``store.delete`` removes the physical file; the immediately-following
    incremental rebuild of both index shards sees (via the existing
    manifest add/changed/removed delta, issue athenaeum#348/#977) that the
    file is gone and prunes its FTS5 row and vector embedding — so a caller
    that calls this once and then calls :func:`query_off_corpus` again
    observes the record gone from content AND from the federated recall
    path, with no separate reindex step for the caller to remember (the
    "same operation" the AC's test asserts).

    Raises :class:`OffCorpusConfigError` if off-corpus is not
    enabled/configured — an erasure request against a surface that does not
    exist is a caller bug to surface loudly, never a silent no-op (D6).
    Returns whether *relpath* existed (``store.delete``'s own return value,
    design note §6.3 "no ``exists()``").
    """
    adapter = off_corpus_adapter(config)
    if adapter is None:
        raise OffCorpusConfigError("off_corpus is not enabled/configured")
    store = off_corpus_store(config, knowledge_root)
    assert store is not None
    key = StoreKey(surface=adapter.name, key=relpath)
    deleted = store.delete(key)
    build_off_corpus_index(config, knowledge_root, cache_dir, incremental=True)
    return deleted


def append_verdict_off_corpus(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    entry_dict: dict[str, Any],
    *,
    at: str,
) -> Path:
    """Append one verdict entry (already ``VerdictEntry.to_dict()``-shaped)
    to the off-corpus ledger shard — the SAME purgeable store as the
    off-corpus index, never the in-git ledger athenaeum#712 built (issue
    athenaeum#984 AC3).

    Takes a plain ``dict`` (not ``athenaeum.verdicts.VerdictEntry``)
    deliberately — see the module docstring's layering note on why this
    module must not import :mod:`athenaeum.verdicts`. *at* is the verdict's
    decision date (``entry.at``), used to pick the monthly partition exactly
    as :func:`athenaeum.verdicts.append_verdict` does for the in-git ledger.

    Durability matches the in-git ledger: ``O_APPEND`` + fsync via
    :meth:`~athenaeum.store.Store.append`. Unlike the in-git ledger, this
    shard has no ``RunLock`` single-appender requirement of its own — the
    caller (:func:`athenaeum.verdicts.record_pair_decision`) already runs
    under the same process-wide lock the in-git path requires, and this
    shard has no compaction/epoch-registry counterpart in this issue's
    scope (see the PR body's "left out" section).

    Raises :class:`OffCorpusConfigError` if off-corpus is not
    enabled/configured.
    """
    adapter = off_corpus_adapter(config)
    if adapter is None:
        raise OffCorpusConfigError("off_corpus is not enabled/configured")
    store = off_corpus_store(config, knowledge_root)
    assert store is not None
    month = (at or date.today().isoformat())[:7]
    key = StoreKey(surface=adapter.name, key=f"{LEDGER_DIRNAME}/{month}.jsonl")
    line = json.dumps(entry_dict, separators=(",", ":")) + "\n"
    store.append(key, line.encode("utf-8"))
    root = off_corpus_root(config, knowledge_root)
    assert root is not None
    return root / LEDGER_DIRNAME / f"{month}.jsonl"
