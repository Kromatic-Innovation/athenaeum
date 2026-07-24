# SPDX-License-Identifier: Apache-2.0
"""Search backend abstraction for athenaeum.

Provides pluggable search backends for wiki recall queries. The default
``fts5`` backend uses SQLite FTS5 with BM25 ranking and porter stemming.
The ``vector`` backend uses chromadb with ``all-MiniLM-L6-v2``. When the
vector backend is configured, the example recall hook performs a hybrid
FTS5+vector merge so that short proper-noun queries still resolve
cleanly — see ``docs/recall-architecture.md`` for why each backend is
load-bearing.

Shell hook scripts can call the module-level convenience functions
(``build_fts5_index``, ``query_fts5_index``, ``build_vector_index``,
``query_vector_index``) without constructing backend objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from athenaeum.models import (
    AUDIENCE_PUBLIC_TOKEN,
    audience_index_string,
    audience_string_authorized,
    is_inactive_memory,
    is_page_authorized,
    parse_frontmatter,
    valid_until_expired,
    validity_bound_str,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

# Issue #373: default age (days) for the periodic full-re-hash backstop that
# heals the #370 stat pre-filter's blind spot (a content edit preserving both
# mtime and size). Referenced by every ``build_index`` signature, so it is
# defined here before the Protocol; the full rationale lives beside the manifest
# helpers below. Resolved from config by
# ``config.resolve_reindex_full_rehash_max_age_days``.
_DEFAULT_FULL_REHASH_MAX_AGE_DAYS = 7.0


@runtime_checkable
class SearchBackend(Protocol):
    """Interface that all search backends must satisfy."""

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    ) -> int:
        """Build or rebuild the search index.

        Args:
            wiki_root: The primary wiki directory (shallow ``*.md`` scan,
                underscore-prefixed files excluded). Entries indexed with a
                bare filename (e.g. ``lean-startup.md``).
            cache_dir: Where the index is persisted.
            extra_roots: Additional intake roots (recursive scan). Each
                root's entries are indexed with a path of the form
                ``<root_name>/<relpath>`` so recall results disambiguate
                wiki entries from raw intake entries. Intended for the
                ``raw/auto-memory/`` intake tree, but accepts any
                directory. Files named ``MEMORY.md`` (per-scope index
                files) and non-``.md`` files are skipped.
            incremental: When ``True`` (default) and a prior manifest
                exists, diff each page's whole-file content hash against
                the stored manifest and apply only the delta — add new
                pages, re-index changed pages, delete removed pages
                (issue #348). A no-op rebuild then touches nothing and
                returns in sub-second time regardless of corpus size.
                When ``False`` (seeding, ``reindex --full``), wipe and
                rebuild from scratch. No prior manifest also forces a full
                build. Setting ``as_of`` (below) also forces a full build.
            include_globs / exclude_globs: Optional corpus-scoping globs
                matched against the indexed name (issue #348 COULD). The
                default (``None`` / ``None``) indexes everything — the
                Apollo contact wikis are legitimate name-recall targets and
                must stay indexed by default. This is a footprint/relevance
                knob, not the CPU fix.
            as_of: Issue #308 slice 3 — the date the index reflects. The
                inactive filter drops pages outside their
                ``[valid_from, valid_until]`` window relative to THIS date.
                ``None`` (default) means today, so the live index is
                unchanged. Pass a past date to build an as-of *rewind*
                index: a page whose ``valid_until`` had not yet passed on
                that date is included even if it has expired since. An as-of
                build is always a FULL build (a historical snapshot has no
                stable manifest to diff against), written into whatever
                ``cache_dir`` the caller chose (a scratch dir, so the live
                index is untouched).
            full_rehash_max_age_days: Issue #373 — the self-healing backstop for
                the #370 stat pre-filter. On an INCREMENTAL build, when the
                manifest has not recorded a full re-hash within this many days,
                the stat fast-path is skipped for ONE build: every file is
                re-read and re-hashed so a content edit that preserved both
                ``mtime`` and ``size`` is finally caught. The change delta is
                STILL applied incrementally (no full re-embed / FTS5 rebuild).
                ``0`` / negative = always re-hash; a very large value =
                effectively never. Ignored on a full or as-of build (both
                already re-hash everything).

        Returns the total number of pages in the index across all roots.
        """
        ...

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
    ) -> list[tuple[str, str, float]]:
        """Search the index.

        ``wiki_root`` is used by scan-on-query backends (e.g. keyword) that
        don't maintain an on-disk index; indexed backends ignore it.

        ``as_of`` (issue #308 slice 3) pins the temporal view. Indexed
        backends (fts5 / vector) filter at BUILD time, so they IGNORE this
        parameter — an as-of view for them is a matching as-of index (see
        ``build_index``). The scan-on-query ``keyword`` backend honors it
        directly, filtering each page against its validity window at query
        time. ``None`` (default) means today.

        ``caller_audience`` (issue #312) pins the query to a restricted read
        scope. ``None`` is the owner / default caller: no filtering, every
        page (untagged included) is eligible. A non-None set restricts the
        result to pages the caller is authorized for, with the audience
        predicate pushed INSIDE the backend query so BM25/kNN top-k is
        computed over permitted rows only — a forbidden page can neither
        occupy a slot nor push a permitted page past the limit. Fail-closed:
        untagged / malformed pages are withheld from a restricted caller.

        Returns a list of ``(filename, page_name, score)`` tuples,
        ordered by relevance (best first). The ``filename`` may be a
        bare name (wiki entry) or ``<root_name>/<relpath>`` (extra-root
        entry) — callers resolving to a filesystem path must try each
        configured root in turn.
        """
        ...


# ---------------------------------------------------------------------------
# FTS5 backend
# ---------------------------------------------------------------------------

# Public stopword list — sorted tuple for deterministic CLI output.
# Exposed as the single source of truth so shell hooks and downstream
# callers don't re-hardcode their own copy. See `athenaeum stopwords`
# CLI subcommand and examples/claude-code/user-prompt-recall.sh.
STOPWORDS: tuple[str, ...] = tuple(
    sorted(
        set(
            "the and for are but not you all can had her was one our out has his how "
            "its let may new now old see way who did get got him she too use with from "
            "have this that they will been call come each find give help here just know "
            "like long look make many more most much must next only over said same some "
            "such take tell than them then very want well went were what when which "
            "while work also back been being both came does done down even goes going "
            "good keep last left life line made need never part place point right show "
            "small still think those turn used using where would about after again "
            "could every great might often other shall should since start state still "
            "there these thing think three through under until which while world would "
            "years your into just like made over said some than them then time very "
            "want what when will with year does really right going being looking "
            "trying running check please sure okay yeah thanks".split()
        )
    )
)

# Stopwords stripped before building an FTS5 query.
_STOPWORDS: frozenset[str] = frozenset(STOPWORDS)

_DB_NAME = "wiki-index.db"

# Filenames excluded from the intake scan. ``MEMORY.md`` is the per-scope
# curated index file generated by ``scripts/build-per-scope-memory-index.py``
# — we don't want it appearing as a recall hit because it's a table of
# contents, not a memory. Callers who want to search index files directly
# can do so with a filename-targeted query outside recall.
_INTAKE_SKIP_NAMES: frozenset[str] = frozenset({"MEMORY.md"})


def _like_escape(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards in an audience role id (issue #312).

    Role ids are operator-controlled, but a stray ``%`` / ``_`` in a role would
    turn the delimiter-anchored ``LIKE`` predicate into an unintended wildcard.
    Escaped with a backslash to pair with ``ESCAPE '\\'`` in the query.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_frontmatter_fields(text: str) -> tuple[str, str, str, str]:
    """Parse ``name / tags / aliases / description`` from YAML frontmatter.

    Returns a 4-tuple of strings (empty when not present). Mirrors the
    hand-rolled parser FTS5 used inline — factored out so the intake-root
    scanner shares one implementation.
    """
    name, tags, aliases, description = "", "", "", ""
    if not text.startswith("---"):
        return name, tags, aliases, description
    end = text.find("---", 4)
    if end <= 0:
        return name, tags, aliases, description
    fm = text[4:end]
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[5:].strip().strip("\"'")
        elif line.startswith("tags:"):
            tags = line[5:].strip().strip("[]")
        elif line.startswith("aliases:"):
            aliases = line[8:].strip().strip("[]")
        elif line.startswith("description:"):
            description = line[12:].strip().strip("\"'")
    return name, tags, aliases, description


def _iter_wiki_entries(wiki_root: Path) -> Iterable[tuple[str, Path]]:
    """Yield ``(filename, full_path)`` for wiki markdown pages.

    Wiki is a flat shallow scan — underscore-prefixed files are excluded
    (``_index.md``, ``_pending_questions.md``, etc.).
    """
    try:
        names = sorted(os.listdir(wiki_root))
    except OSError:
        return
    for fname in names:
        if not fname.endswith(".md") or fname.startswith("_"):
            continue
        if fname in _INTAKE_SKIP_NAMES:
            continue
        yield fname, wiki_root / fname


def _iter_extra_root_entries(
    extra_roots: Iterable[Path] | None,
) -> Iterable[tuple[str, Path]]:
    """Yield ``(indexed_filename, full_path)`` for extra intake roots.

    Each extra root is scanned recursively. ``indexed_filename`` is
    ``<root_name>/<relpath_posix>`` so wiki entries (bare name) and extra-
    root entries never collide and remain distinguishable to the recall
    formatter. Non-``.md`` files and ``MEMORY.md`` are excluded; the
    ``_unscoped/`` subdirectory is included (its files are first-class
    memories, not metadata). Missing roots are silently skipped — this
    is intake code and shouldn't crash on an unconfigured knowledge base.
    """
    if not extra_roots:
        return
    for root in extra_roots:
        if not root.is_dir():
            continue
        root_name = root.name
        for path in sorted(root.rglob("*.md")):
            if path.name in _INTAKE_SKIP_NAMES:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            yield f"{root_name}/{rel}", path


def _scan_all_entries(
    wiki_root: Path,
    extra_roots: Iterable[Path] | None,
) -> Iterable[tuple[str, Path]]:
    """Yield every indexable ``(filename, full_path)`` pair.

    Wiki entries come first (bare filename) followed by extra-root
    entries (``<root_name>/<relpath>``). Callers ingest whatever order
    this yields — ordering within each source is alphabetical so index
    rebuilds are deterministic for test assertions.
    """
    yield from _iter_wiki_entries(wiki_root)
    yield from _iter_extra_root_entries(extra_roots)


# ---------------------------------------------------------------------------
# Incremental indexing helpers (issue #348)
# ---------------------------------------------------------------------------
#
# Both indexed backends persist a per-page WHOLE-FILE content hash in a JSON
# sidecar manifest next to their index artifact. On rebuild they diff the
# current files against the stored hashes and apply only the delta — add
# new, re-index changed, delete removed. Hashing the whole file (frontmatter
# + body) means a frontmatter-only change (e.g. issue #312 audience) is
# caught just as a body edit is; a body-only hash would miss it. Inactive
# memories (issue #191) are filtered out BEFORE hashing, so a page that flips
# to inactive drops out of the manifest and is treated as a deletion.

# Manifest sidecar filenames (co-located with each backend's index artifact).
_FTS5_MANIFEST = "fts5-manifest.json"
_VECTOR_MANIFEST = "vector-manifest.json"

# Issue #373: ``_DEFAULT_FULL_REHASH_MAX_AGE_DAYS`` (defined above the Protocol)
# bounds the stat pre-filter's blind window — an incremental build that has not
# re-hashed everything within that many days ignores the stat fast-path for ONE
# build (re-reads + re-hashes every file) while still applying the change delta
# incrementally, so a content edit preserving both mtime and size is caught
# without paying for a full re-embed.

# Top-level manifest key recording the epoch-seconds timestamp of the last build
# that re-hashed every file (a full rebuild or a stale-triggered incremental
# re-hash). Absent (a pre-#373 manifest) => treated as infinitely stale, so the
# first build after this ships does one full re-hash and stamps it.
_MANIFEST_REHASH_KEY = "last_full_rehash_at"


def _now() -> float:
    """Return the current epoch seconds.

    A module-level indirection (not cached) so tests can monkeypatch the clock
    to age a manifest past the full-re-hash staleness window (issue #373).
    """
    return time.time()


# Default embedding model (issue #315 slice). Kept as the documented default;
# the one-time seed re-embed that incremental seeding requires is the natural
# opportunity to evaluate a stronger model (see VectorBackend).
# TODO(#315): when seeding the hash-indexed collection from scratch, evaluate a
# stronger embedding model here and record the eval result before changing the
# default — the seed re-embed is paid once, so it is the cheap moment to swap.
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _passes_globs(
    indexed_name: str,
    include_globs: Iterable[str] | None,
    exclude_globs: Iterable[str] | None,
) -> bool:
    """Return True if ``indexed_name`` survives the include/exclude globs.

    Default (both ``None``) indexes everything. ``include_globs`` is an
    allow-list (the name must match at least one); ``exclude_globs`` is a
    deny-list applied after. Globs match the indexed name — the bare
    filename for wiki entries or ``<root_name>/<relpath>`` for extra roots.
    """
    if include_globs:
        include = list(include_globs)
        if include and not any(fnmatch(indexed_name, g) for g in include):
            return False
    if exclude_globs:
        for g in exclude_globs:
            if fnmatch(indexed_name, g):
                return False
    return True


def _scan_indexed_records(
    wiki_root: Path,
    extra_roots: Iterable[Path] | None,
    *,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    as_of: date | None = None,
    prior: dict[str, tuple[int, int, str, str]] | None = None,
) -> Iterator[tuple[str, Path, str, str, dict[str, Any], tuple[int, int, str]]]:
    """Yield ``(indexed_name, path, hash, text, meta, statrec)`` per active page.

    ``content_hash`` is the sha256 of the whole file (frontmatter + body).
    ``text`` is the full decoded file (callers truncate as needed for the
    index document). ``statrec`` is ``(mtime_ns, size, valid_until_iso)`` for
    the manifest's stat pre-filter. Inactive memories are filtered here so they
    are absent from both index and manifest — the incremental differ then treats
    an active→inactive flip as a deletion. Unreadable files are skipped.

    ``as_of`` (issue #308 slice 3) pins the temporal view: a page outside its
    validity window relative to ``as_of`` (default today) is filtered out here,
    exactly like a #191 tombstone. Only an as-of BUILD passes this (and an as-of
    build is always full), so the manifest a normal live rebuild diffs against is
    never contaminated by a historical view.

    ``prior`` (issue #370) enables the stat pre-filter: a map ``indexed_name ->
    (mtime_ns, size, valid_until_iso, hash)`` from the last manifest. When a
    file's ``(mtime_ns, size)`` matches its prior entry, its body is NOT read or
    re-hashed — the stored hash is reused (rsync-style heuristic). The page was
    active last build (only active pages are in the manifest) and its content is
    unchanged, so it stays active EXCEPT if its ``valid_until`` has since expired
    relative to ``as_of`` — that time-varying bound is re-checked from the stored
    date without a read, preserving the #308 date-expiry semantics. Stat-matched
    rows yield placeholder ``text=""``/``meta={}``: callers only consume those
    for the add/change delta, whose members always fail the stat match and are
    freshly read. ``prior`` MUST be ``None`` for a full (re)build — a full build
    inserts every scanned record, so the placeholders would corrupt it. Callers
    pass ``prior`` only on the incremental-apply path.
    """
    include = list(include_globs) if include_globs else None
    exclude = list(exclude_globs) if exclude_globs else None
    for indexed_name, path in _scan_all_entries(wiki_root, extra_roots):
        if not _passes_globs(indexed_name, include, exclude):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        mtime_ns, size = st.st_mtime_ns, st.st_size
        prior_rec = prior.get(indexed_name) if prior else None
        if prior_rec is not None and prior_rec[0] == mtime_ns and prior_rec[1] == size:
            # Stat fast-path: content unchanged since the last active build.
            # Re-check ONLY the time-varying upper bound (superseded_by /
            # deprecated are content-based and cannot change without a stat
            # change). ``valid_until`` may have crossed ``as_of`` (default
            # today) with no content edit — drop the page then so it becomes a
            # manifest ``removed`` and leaves the index (issue #308).
            stored_vu = prior_rec[2]
            if stored_vu and valid_until_expired({"valid_until": stored_vu}, as_of):
                continue
            yield indexed_name, path, prior_rec[3], "", {}, (mtime_ns, size, stored_vu)
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        content_hash = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", errors="replace")
        meta, _ = parse_frontmatter(text)
        # Issue #191: inactive members never enter the index or the manifest.
        # Issue #308: an as-of build additionally drops pages outside their
        # validity window relative to ``as_of`` (default today).
        if is_inactive_memory(meta, as_of):
            continue
        vu = validity_bound_str(meta, "valid_until")
        yield indexed_name, path, content_hash, text, meta, (mtime_ns, size, vu)


def _compute_delta(
    current_hashes: dict[str, str],
    stored_hashes: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(added, changed, removed)`` indexed-name lists.

    ``added``: present now, absent from the manifest.
    ``changed``: present in both, whole-file hash differs.
    ``removed``: in the manifest, absent now (deleted or gone inactive).
    """
    added = [k for k in current_hashes if k not in stored_hashes]
    changed = [
        k
        for k, h in current_hashes.items()
        if k in stored_hashes and stored_hashes[k] != h
    ]
    removed = [k for k in stored_hashes if k not in current_hashes]
    return added, changed, removed


def _load_manifest(path: Path) -> dict[str, Any] | None:
    """Load a manifest sidecar, or ``None`` when absent/unreadable/malformed."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _manifest_hashes(manifest: dict[str, Any] | None) -> dict[str, str]:
    """Extract the ``{indexed_name: hash}`` map from a loaded manifest."""
    if not manifest:
        return {}
    hashes = manifest.get("hashes")
    if isinstance(hashes, dict):
        return {str(k): str(v) for k, v in hashes.items()}
    return {}


def _manifest_stats(manifest: dict[str, Any] | None) -> dict[str, tuple[int, int, str]]:
    """Extract the ``{indexed_name: (mtime_ns, size, valid_until)}`` stat map.

    Issue #370's stat pre-filter. Absent (a v1 manifest predating stats) or
    malformed => ``{}``, which forces a one-time full hash of every file and the
    manifest upgrades to v2 on the next write. Each stored entry is a
    ``[mtime_ns, size, valid_until]`` list (JSON has no tuples); rows that do not
    parse are skipped (fail to a re-hash), never crashing the build.
    """
    if not manifest:
        return {}
    stats = manifest.get("stats")
    if not isinstance(stats, dict):
        return {}
    out: dict[str, tuple[int, int, str]] = {}
    for name, rec in stats.items():
        try:
            mtime_ns, size, vu = rec[0], rec[1], rec[2]
            out[str(name)] = (int(mtime_ns), int(size), str(vu or ""))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _manifest_last_full_rehash(manifest: dict[str, Any] | None) -> float | None:
    """Read the manifest's ``last_full_rehash_at`` epoch seconds (issue #373).

    ``None`` when absent (a pre-#373 manifest) or malformed — the caller treats
    that as infinitely stale and forces one full re-hash. A ``bool`` (a subclass
    of ``int``) is rejected so a stray ``true`` cannot read as ``1.0``.
    """
    if not manifest:
        return None
    raw = manifest.get(_MANIFEST_REHASH_KEY)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _scan_prior(
    manifest: dict[str, Any] | None,
) -> dict[str, tuple[int, int, str, str]]:
    """Join a manifest's hashes + stats into the scan's ``prior`` map (#370).

    Returns ``{indexed_name: (mtime_ns, size, valid_until, hash)}`` for names
    that have BOTH a hash and a stat entry. A name missing either (e.g. every
    name in a v1 manifest, which has no ``stats``) is omitted, so it is read and
    re-hashed exactly once — after which the v2 write records its stat.
    """
    hashes = _manifest_hashes(manifest)
    stats = _manifest_stats(manifest)
    out: dict[str, tuple[int, int, str, str]] = {}
    for name, (mtime_ns, size, vu) in stats.items():
        h = hashes.get(name)
        if h is not None:
            out[name] = (mtime_ns, size, vu, h)
    return out


def _write_manifest(
    path: Path,
    hashes: dict[str, str],
    extra: dict[str, Any] | None = None,
    stats: dict[str, tuple[int, int, str]] | None = None,
    last_full_rehash_at: float | None = None,
) -> None:
    """Atomically write the manifest sidecar (temp file + rename).

    ``stats`` (issue #370) persists the per-file ``(mtime_ns, size,
    valid_until)`` alongside the hash so the next build's stat pre-filter can
    skip re-reading unchanged files. Bumped to ``version: 2`` when stats are
    written; a reader that only knows ``hashes`` is unaffected (still present).

    ``last_full_rehash_at`` (issue #373) records the epoch seconds of the most
    recent build that re-hashed every file (a full rebuild or a stale-triggered
    incremental re-hash). The stale-detection backstop reads it to decide when
    to force the next full re-hash; a fresh incremental build PRESERVES the prior
    value by passing it back unchanged. ``None`` omits the key.
    """
    version = 2 if stats is not None else 1
    payload: dict[str, Any] = {"version": version, "hashes": hashes}
    if stats is not None:
        payload["stats"] = {k: [v[0], v[1], v[2]] for k, v in stats.items()}
    if last_full_rehash_at is not None:
        payload[_MANIFEST_REHASH_KEY] = last_full_rehash_at
    if extra:
        payload.update(extra)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


class FTS5Backend:
    """SQLite FTS5 full-text search with BM25 ranking and porter stemming."""

    # SQL fragments shared by the full and incremental build paths.
    _CREATE_SQL = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS wiki USING fts5"
        "(filename, name, tags, aliases, description, audience UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    _INSERT_SQL = "INSERT INTO wiki VALUES (?,?,?,?,?,?)"

    @staticmethod
    def _row_for(
        indexed_name: str, path: Path, text: str, meta: dict[str, Any]
    ) -> tuple[str, str, str, str, str, str]:
        """Build the FTS5 row tuple for one page."""
        name, tags, aliases, description = _extract_frontmatter_fields(text)
        if not name:
            # For extra-root entries use the leaf stem (not the prefixed
            # indexed_name) so recall results show a clean title.
            name = path.stem
        # Issue #312: store each page's effective audience (delimited,
        # anchored) so Layer B can filter inside the query.
        audience = audience_index_string(meta)
        return (indexed_name, name, tags, aliases, description, audience)

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    ) -> int:
        """Scan wiki + extra intake roots and build an FTS5 index.

        See :meth:`SearchBackend.build_index` for the full contract. Wiki
        entries are indexed with a bare filename; extra-root entries with
        ``<root_name>/<relpath>``. Incremental by default (issue #348):
        only added/changed/removed pages are touched, keyed off a whole-file
        content-hash manifest sidecar. An as-of build (``as_of`` set, issue
        #308) is always a full build reflecting that date's validity windows.
        ``full_rehash_max_age_days`` (issue #373) periodically forces a full
        re-hash on the incremental path — see :meth:`SearchBackend.build_index`.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = cache_dir / _DB_NAME
        manifest_path = cache_dir / _FTS5_MANIFEST

        # Issue #308: an as-of view is a historical snapshot — never diff it
        # against (or seed) the live manifest, so force a full build.
        stored = (
            _load_manifest(manifest_path) if incremental and as_of is None else None
        )
        # Incremental only when we have BOTH a prior manifest and a live DB;
        # otherwise seed with a clean full rebuild.
        do_incremental = (
            incremental and as_of is None and stored is not None and db_path.is_file()
        )

        # Issue #373: self-healing full-re-hash backstop. On the incremental
        # path, if the manifest has not recorded a full re-hash within the max
        # age, force one this build (``prior=None`` => every file re-read and
        # re-hashed) while STILL applying the change delta incrementally. A fresh
        # manifest preserves its stored timestamp; a full rebuild always stamps.
        now = _now()
        last_rehash = _manifest_last_full_rehash(stored)
        stale = last_rehash is None or (now - last_rehash) > (
            full_rehash_max_age_days * 86400.0
        )
        rehash_at = now if (not do_incremental or stale) else last_rehash

        # Issue #370: feed the prior manifest's stats into the scan so unchanged
        # files are stat-matched instead of re-read. A full build inserts every
        # scanned record, so it must read every file — ``prior=None`` there. A
        # stale incremental build (#373) likewise passes ``prior=None`` to force
        # a re-hash of every file.
        prior = _scan_prior(stored) if (do_incremental and not stale) else None
        current = list(
            _scan_indexed_records(
                wiki_root,
                extra_roots,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                as_of=as_of,
                prior=prior,
            )
        )
        current_hashes = {name: h for name, _p, h, _t, _m, _s in current}
        current_stats = {name: s for name, _p, _h, _t, _m, s in current}

        if not do_incremental:
            if db_path.exists():
                db_path.unlink()
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(self._CREATE_SQL)
                rows = [
                    self._row_for(name, path, text, meta)
                    for name, path, _h, text, meta, _s in current
                ]
                conn.executemany(self._INSERT_SQL, rows)
                conn.commit()
            finally:
                conn.close()
            _write_manifest(
                manifest_path,
                current_hashes,
                stats=current_stats,
                last_full_rehash_at=rehash_at,
            )
            return len(rows)

        # Incremental path — diff and apply only the delta.
        stored_hashes = _manifest_hashes(stored)
        added, changed, removed = _compute_delta(current_hashes, stored_hashes)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(self._CREATE_SQL)  # defensive: table may predate a wipe
            to_delete = removed + changed
            if to_delete:
                conn.executemany(
                    "DELETE FROM wiki WHERE filename = ?",
                    [(k,) for k in to_delete],
                )
            reindex = set(added) | set(changed)
            if reindex:
                rows = [
                    self._row_for(name, path, text, meta)
                    for name, path, _h, text, meta, _s in current
                    if name in reindex
                ]
                conn.executemany(self._INSERT_SQL, rows)
            conn.commit()
            total = int(conn.execute("SELECT count(*) FROM wiki").fetchone()[0])
        finally:
            conn.close()
        _write_manifest(
            manifest_path,
            current_hashes,
            stats=current_stats,
            last_full_rehash_at=rehash_at,
        )
        return total

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the FTS5 index. Returns ``(filename, name, score)`` triples."""
        del wiki_root  # FTS5 reads the pre-built index, not the wiki files
        del as_of  # #308: FTS5 filters at build time; as-of view = as-of index
        db_path = cache_dir / _DB_NAME
        if not db_path.is_file():
            return []

        # Tokenize and filter stopwords
        terms = [
            t
            for t in re.split(r"\W+", query.lower())
            if len(t) >= 3 and t not in _STOPWORDS
        ]
        if not terms:
            return []

        # Build FTS5 MATCH expression: "word1" OR "word2" ...
        fts_query = " OR ".join(f'"{t}"' for t in terms[:8])

        # Build exclusion clause
        exclude_clause = ""
        params: list[str] = []
        if exclude:
            placeholders = ", ".join("?" for _ in exclude)
            exclude_clause = f" AND filename NOT IN ({placeholders})"
            params = list(exclude)

        # Issue #312 — Layer B: push the audience predicate INTO the WHERE,
        # BEFORE ``ORDER BY rank LIMIT``, so the BM25 top-k is selected from
        # permitted rows only. A forbidden page can neither occupy a slot nor
        # push a permitted page past the LIMIT. ``caller_audience=None`` (owner)
        # adds no predicate — every page is eligible. Each role is a
        # delimiter-anchored, LIKE-escaped, parameterized clause so ``|ops|``
        # never matches ``|opsadmin|`` and role ids can't inject SQL.
        audience_clause = ""
        audience_params: list[str] = []
        if caller_audience is not None:
            # Public marker first (the internal sentinel, escaped so its
            # underscores aren't treated as LIKE wildcards), then one anchored,
            # escaped, parameterized clause per caller role.
            like_clauses = [r"audience LIKE ? ESCAPE '\'"]
            audience_params.append(f"%|{_like_escape(AUDIENCE_PUBLIC_TOKEN)}|%")
            for role in sorted(caller_audience):
                like_clauses.append(r"audience LIKE ? ESCAPE '\'")
                audience_params.append(f"%|{_like_escape(role)}|%")
            audience_clause = " AND (" + " OR ".join(like_clauses) + ")"

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                f"SELECT filename, name, rank FROM wiki "
                f"WHERE wiki MATCH ? {exclude_clause}{audience_clause} "
                f"ORDER BY rank LIMIT ?",
                [fts_query, *params, *audience_params, n],
            )
            return [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Vector backend (chromadb)
# ---------------------------------------------------------------------------

_VECTOR_DIR = "wiki-vectors"
_VECTOR_COLLECTION = "wiki"


class VectorBackend:
    """Semantic search via chromadb with local embeddings.

    Requires ``pip install athenaeum[vector]`` (chromadb).
    Uses the default ``all-MiniLM-L6-v2`` embedding model unless an
    alternate model name is passed (issue #315 config seam).
    """

    # Text length used both as the embedded document and as the batch cap.
    _DOC_LIMIT = 4000
    _BATCH_SIZE = 5000

    def __init__(self, embedding_model: str | None = None) -> None:
        """Construct the backend.

        ``embedding_model`` (issue #315 seam) selects the sentence-transformer
        model. ``None`` and the documented default ``all-MiniLM-L6-v2`` both
        use chromadb's built-in default embedding function unchanged — the
        default is NOT changed here. A non-default name is only honored if
        chromadb's sentence-transformer EF can load it; the manifest records
        the model so swapping it forces a one-time full re-embed (the eval
        opportunity noted at DEFAULT_EMBEDDING_MODEL).
        """
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL

    def _get_chromadb(self) -> Any:
        try:
            import chromadb

            return chromadb
        except ImportError as exc:
            raise ImportError(
                "Vector backend requires chromadb. "
                "Install with: pip install athenaeum[vector]"
            ) from exc

    def _embedding_function(self) -> Any | None:
        """Return the chromadb embedding function, or ``None`` for the default.

        The default model uses chromadb's built-in EF (``None``) so behavior
        is byte-for-byte unchanged. A non-default model constructs a
        SentenceTransformer EF; if that import/construction fails we fall
        back to the default rather than crashing the rebuild.
        """
        if self.embedding_model == DEFAULT_EMBEDDING_MODEL:
            return None
        try:
            from chromadb.utils import embedding_functions

            return embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self.embedding_model
            )
        except Exception:  # pragma: no cover - optional-model fallback
            import logging

            logging.getLogger(__name__).warning(
                "embedding model %r unavailable; falling back to default %r",
                self.embedding_model,
                DEFAULT_EMBEDDING_MODEL,
            )
            return None

    def _add_records(
        self,
        collection: Any,
        records: list[tuple[str, Path, str, str, dict[str, Any], tuple[int, int, str]]],
    ) -> None:
        """Embed and add a batch of scanned records to the collection."""
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        for indexed_name, path, _h, text, meta, _s in records:
            name, _tags, _aliases, _description = _extract_frontmatter_fields(text)
            if not name:
                name = path.stem
            # Issue #312 — Layer A: store the effective audience so the query
            # can pre-filter neighbors. chromadb metadata is scalar-only, so
            # the audience is stored as the same delimited string as FTS5 and
            # filtered in Python at query time (Layer B).
            ids.append(indexed_name)
            documents.append(text[: self._DOC_LIMIT])
            metadatas.append(
                {
                    "name": name,
                    "filename": indexed_name,
                    "audience": audience_index_string(meta),
                }
            )
        for i in range(0, len(ids), self._BATCH_SIZE):
            end = min(i + self._BATCH_SIZE, len(ids))
            collection.add(
                ids=ids[i:end],
                documents=documents[i:end],
                metadatas=metadatas[i:end],
            )

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    ) -> int:
        """Build a chromadb collection from wiki + extra intake roots.

        See :meth:`SearchBackend.build_index` for the full contract.
        Incremental by default (issue #348): only added/changed/removed
        pages are (re-)embedded, keyed off a whole-file content-hash
        manifest sidecar. A no-op rebuild re-embeds nothing. An as-of build
        (``as_of`` set, issue #308) is always a full build reflecting that
        date's validity windows. ``full_rehash_max_age_days`` (issue #373)
        periodically forces a full re-hash on the incremental path — the change
        delta is still applied incrementally (no full re-embed).
        """
        chromadb = self._get_chromadb()
        cache_dir.mkdir(parents=True, exist_ok=True)
        vector_dir = cache_dir / _VECTOR_DIR
        manifest_path = cache_dir / _VECTOR_MANIFEST

        # Issue #308: an as-of view is a historical snapshot — never diff it
        # against (or seed) the live manifest, so force a full build.
        stored = (
            _load_manifest(manifest_path) if incremental and as_of is None else None
        )
        stored_model = stored.get("embedding_model") if stored else None
        # Incremental only when we have a prior manifest, a live collection
        # dir, AND the SAME embedding model — a model swap must re-embed all.
        do_incremental = (
            incremental
            and as_of is None
            and stored is not None
            and vector_dir.is_dir()
            and stored_model == self.embedding_model
        )

        # Issue #373: self-healing full-re-hash backstop (identical to FTS5).
        # On the incremental path, force a full re-hash of every file when the
        # manifest has not recorded one within the max age — the change delta is
        # still applied incrementally (no rmtree / full re-embed).
        now = _now()
        last_rehash = _manifest_last_full_rehash(stored)
        stale = last_rehash is None or (now - last_rehash) > (
            full_rehash_max_age_days * 86400.0
        )

        # Issue #370: stat pre-filter the scan on the incremental path only —
        # a full (re)build embeds every scanned record and cannot use the
        # placeholder text/meta that stat-matched rows carry. A stale incremental
        # build (#373) also passes ``prior=None`` to force a re-hash of all.
        prior = _scan_prior(stored) if (do_incremental and not stale) else None

        def _scan(with_prior: dict[str, tuple[int, int, str, str]] | None) -> tuple[
            list[tuple[str, Path, str, str, dict[str, Any], tuple[int, int, str]]],
            dict[str, str],
            dict[str, tuple[int, int, str]],
        ]:
            recs = list(
                _scan_indexed_records(
                    wiki_root,
                    extra_roots,
                    include_globs=include_globs,
                    exclude_globs=exclude_globs,
                    as_of=as_of,
                    prior=with_prior,
                )
            )
            return (
                recs,
                {name: h for name, _p, h, _t, _m, _s in recs},
                {name: s for name, _p, _h, _t, _m, s in recs},
            )

        current, current_hashes, current_stats = _scan(prior)

        # chromadb caches PersistentClient systems per-path at the module
        # level. Clear it so a fresh client sees the true on-disk state
        # (avoids stale-collection "already exists" desync — see issue #32).
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()

        if do_incremental:
            try:
                client = chromadb.PersistentClient(path=str(vector_dir))
                collection = client.get_collection(
                    _VECTOR_COLLECTION,
                    embedding_function=self._embedding_function(),
                )
            except Exception as exc:
                # Corrupt / missing collection despite a manifest — fall back
                # to a clean full rebuild rather than accreting a bad delta.
                # Issue #370: log it — a silent full rmtree+re-embed of a 21k
                # corpus was indistinguishable from a hang. WARNING so a real
                # (expensive) full rebuild is diagnosable, not silent.
                import logging

                logging.getLogger(__name__).warning(
                    "vector incremental open failed (%s: %s); "
                    "falling back to FULL rebuild (rmtree + re-embed all)",
                    type(exc).__name__,
                    exc,
                )
                do_incremental = False

        if not do_incremental:
            # A stat pre-filtered scan yields placeholder bodies for unchanged
            # rows; a full rebuild embeds every row, so re-scan with full reads
            # first (only when we took the fast-path).
            if prior is not None:
                current, current_hashes, current_stats = _scan(None)
            # Full (re)build — nuke any prior on-disk state before opening a
            # PersistentClient. chromadb's SQLite metadata and the rust
            # binding's collection store can desync; a full wipe is the
            # simplest robust reset (issue #32).
            if vector_dir.exists():
                shutil.rmtree(vector_dir)
            vector_dir.mkdir(parents=True, exist_ok=True)
            SharedSystemClient.clear_system_cache()
            client = chromadb.PersistentClient(path=str(vector_dir))
            collection = client.create_collection(
                _VECTOR_COLLECTION,
                embedding_function=self._embedding_function(),
            )
            self._add_records(collection, current)
            _write_manifest(
                manifest_path,
                current_hashes,
                {"embedding_model": self.embedding_model},
                stats=current_stats,
                last_full_rehash_at=now,
            )
            return len(current)

        # Incremental path — diff and apply only the delta.
        stored_hashes = _manifest_hashes(stored)
        added, changed, removed = _compute_delta(current_hashes, stored_hashes)

        to_delete = removed + changed
        if to_delete:
            collection.delete(ids=to_delete)
        reindex = set(added) | set(changed)
        if reindex:
            self._add_records(
                collection,
                [rec for rec in current if rec[0] in reindex],
            )
        total = int(collection.count())
        _write_manifest(
            manifest_path,
            current_hashes,
            {"embedding_model": self.embedding_model},
            stats=current_stats,
            last_full_rehash_at=(now if stale else last_rehash),
        )
        return total

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the chromadb collection with semantic search."""
        del wiki_root  # Vector reads the pre-built chromadb collection
        del as_of  # #308: vector filters at build time; as-of view = as-of index
        chromadb = self._get_chromadb()

        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return []

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(_VECTOR_COLLECTION)
        except Exception as exc:
            # chromadb raises an InvalidCollectionException (and occasionally
            # bare ValueError from the rust binding) when the collection is
            # absent or its metadata is corrupt. We can't import the exception
            # class directly because chromadb reorganises it between releases,
            # so we catch broadly but log the class name so a real bug
            # doesn't sit silent — "vector returns nothing" was the top
            # first-adopter confusion in the v0.2.0 review.
            import logging

            logging.getLogger(__name__).warning(
                "vector get_collection(%s) failed with %s: %s; " "returning empty hits",
                _VECTOR_COLLECTION,
                type(exc).__name__,
                exc,
            )
            return []

        count = collection.count()
        if count == 0:
            return []

        # Build where filter for exclusions
        where: dict[str, Any] | None = None
        if exclude and len(exclude) == 1:
            where = {"filename": {"$ne": next(iter(exclude))}}
        elif exclude and len(exclude) > 1:
            where = {"filename": {"$nin": list(exclude)}}

        # Issue #312 — Layer B (vector): chromadb metadata is scalar-only, so
        # there is no native substring/list-membership operator to express the
        # audience predicate as a ``where``. Instead OVER-FETCH — for a
        # restricted caller fetch the full ordered neighbor list — then filter
        # in Python and re-truncate to ``n``. Because we fetch every neighbor,
        # no permitted page can be starved out of the top-k by forbidden
        # neighbors ranking above it. ``caller_audience=None`` (owner) keeps the
        # original cheap ``min(n, count)`` fetch. Layer C (mcp_server) re-checks
        # fresh on-disk frontmatter as the backstop.
        # NOTE (perf): for a restricted caller this is a full-collection kNN
        # (n_results == count) — O(collection) per query — deliberately, so a
        # forbidden-heavy corpus can never starve a permitted page out of the
        # returned top-k. Fine for a personal knowledge base; revisit with a
        # bounded over-fetch + retry only if this ever dominates recall latency.
        fetch_n = count if caller_audience is not None else min(n, count)

        results = collection.query(
            query_texts=[query],
            n_results=fetch_n,
            where=where,
        )

        hits: list[tuple[str, str, float]] = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                if caller_audience is not None:
                    audience_str = str(meta.get("audience", "|"))
                    if not audience_string_authorized(audience_str, caller_audience):
                        continue
                name = meta.get("name", doc_id.replace(".md", ""))
                distance = results["distances"][0][i] if results["distances"] else 0.0
                hits.append((doc_id, name, distance))
                if len(hits) >= n:
                    break

        return hits

    def fetch_embeddings(
        self,
        ids: Iterable[str],
        cache_dir: Path,
    ) -> dict[str, list[float]]:
        """Return ``{id: embedding_vector}`` for the given indexed filenames.

        Narrow accessor for clustering (issue #196). Reuses the collection
        built by :meth:`build_index` — does NOT invoke a second embedding
        provider. Missing ids are silently omitted so callers can cluster
        over the intersection of "requested" and "actually indexed".
        Returns ``{}`` when the collection does not exist or is empty.

        Issue #370: this is a pure READ of stored embeddings (``get`` with
        ``include=["embeddings"]`` never embeds), so the collection is opened
        with ``embedding_function=None`` — the default arg is a module-level
        ``DefaultEmbeddingFunction()`` that (in a future chromadb) could pull in
        the ONNX model on this read-only path. Passing ``None`` guarantees the
        embedding backend is never constructed here.
        """
        chromadb = self._get_chromadb()
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return {}

        id_list = list(ids)
        if not id_list:
            return {}

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
        except Exception:
            return {}

        try:
            result = collection.get(ids=id_list, include=["embeddings"])
        except Exception:
            return {}

        out: dict[str, list[float]] = {}
        # chromadb returns embeddings as a numpy array — ``x or []`` raises
        # "truth value ambiguous" on it, so normalize with an explicit None
        # check instead of truthiness (issue #370: this read path must work).
        result_ids = result.get("ids")
        if result_ids is None:
            result_ids = []
        embeddings = result.get("embeddings")
        if embeddings is None:
            embeddings = []
        for i, doc_id in enumerate(result_ids):
            if i >= len(embeddings):
                continue
            vec = embeddings[i]
            if vec is None:
                continue
            # chromadb returns numpy arrays in some versions — coerce to list
            out[doc_id] = [float(x) for x in vec]
        return out

    def purge_ids(
        self,
        ids: Iterable[str],
        cache_dir: Path,
    ) -> int:
        """Delete the given indexed filenames from the collection (issue #425).

        Embedding hygiene for a fold-into-existing merge: when the resolver
        deletes old-slug wiki files after folding them into a canonical page,
        their stale vectors must not linger and surface as near-duplicate
        recall hits. Mirrors :meth:`fetch_embeddings`'s open pattern (a pure
        mutation of the existing collection — never constructs an embedding
        function, since a delete needs no embedding). Returns the number of
        ids requested that were plausibly present (best-effort — chromadb's
        ``delete`` does not report which ids actually existed); returns 0
        when the collection/vector dir does not exist, chromadb is not
        installed, or the input is empty. Never raises — a purge failure
        must not block the merge's file-level side effects, which have
        already happened by the time this runs.
        """
        try:
            chromadb = self._get_chromadb()
        except ImportError:
            return 0
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return 0

        id_list = list(ids)
        if not id_list:
            return 0

        try:
            client = chromadb.PersistentClient(path=str(vector_dir))
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
            collection.delete(ids=id_list)
        except Exception:
            return 0
        return len(id_list)

    def query_neighbors(
        self,
        embedding: Sequence[float],
        cache_dir: Path,
        *,
        k: int = 200,
        exclude_ids: Iterable[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``[(id, distance)]`` for the ``k`` nearest stored neighbors.

        Issue #370 (delta compile): a by-VECTOR nearest-neighbor accessor for
        the delta-scoped cluster pass. Unlike :meth:`query` (which embeds a
        query *string*), this queries by an already-resolved embedding vector,
        so the collection is opened with ``embedding_function=None`` — this is
        a pure read that never constructs the ONNX embedder.

        The caller OVER-FETCHES (large ``k``) because chromadb's default HNSW
        space is L2, which only approximates cosine ranking; the delta closure
        re-confirms every returned candidate with an exact cosine check before
        treating it as a true single-linkage edge, so ANN ranking noise cannot
        introduce a spurious edge. Returns fewer than ``k`` when the collection
        is smaller. ``exclude_ids`` drops the query file itself (and any known
        non-candidates) via a chromadb ``where`` filter. Returns ``[]`` when the
        collection does not exist or is empty.
        """
        chromadb = self._get_chromadb()
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return []

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
        except Exception:
            return []

        count = collection.count()
        if count == 0:
            return []

        where: dict[str, Any] | None = None
        excluded = [e for e in (exclude_ids or [])]
        if len(excluded) == 1:
            where = {"filename": {"$ne": excluded[0]}}
        elif len(excluded) > 1:
            where = {"filename": {"$nin": excluded}}

        try:
            results = collection.query(
                query_embeddings=[list(embedding)],
                n_results=min(k, count),
                where=where,
            )
        except Exception:
            return []

        out: list[tuple[str, float]] = []
        ids = results.get("ids") or []
        if ids and ids[0]:
            distances = results.get("distances") or [[]]
            dist_row = distances[0] if distances else []
            for i, doc_id in enumerate(ids[0]):
                dist = float(dist_row[i]) if i < len(dist_row) else 0.0
                out.append((doc_id, dist))
        return out


# ---------------------------------------------------------------------------
# Keyword backend (in-memory, scan-on-query)
# ---------------------------------------------------------------------------


def tokenize_keyword_query(query: str) -> list[str]:
    """Split a query into lowercase tokens of length >=2."""
    return [t for t in re.split(r"\W+", query.lower()) if len(t) >= 2]


def score_keyword_page(tokens: list[str], frontmatter: dict, body: str) -> float:
    """Score a wiki page against keyword tokens.

    Frontmatter fields (``name``, ``aliases``, ``tags``, ``description``,
    ``title``) match at 3x the weight of body hits.
    """
    if not tokens:
        return 0.0

    fm_parts: list[str] = []
    for key in ("name", "aliases", "tags", "description", "title"):
        val = frontmatter.get(key, "")
        if isinstance(val, list):
            fm_parts.append(" ".join(str(v) for v in val))
        else:
            fm_parts.append(str(val))
    fm_text = " ".join(fm_parts).lower()
    body_lower = body.lower()

    score = 0.0
    for token in tokens:
        score += fm_text.count(token) * 3.0
        score += body_lower.count(token) * 1.0
    return score


class KeywordBackend:
    """Scan-on-query keyword scoring over wiki frontmatter + body.

    No pre-built index: every query rereads the wiki. Frontmatter hits
    are weighted 3x body hits. Intended as a zero-setup fallback for
    small wikis or tests — FTS5 is the recommended default for real use.
    """

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    ) -> int:
        """No-op: the keyword backend rescans on every query.

        Returns a count that includes wiki entries + extra-root entries
        (``MEMORY.md`` and non-``.md`` files excluded) so status checks
        see a comparable number to the indexed backends. The ``incremental``
        / glob knobs (issue #348), ``as_of`` (issue #308), and
        ``full_rehash_max_age_days`` (issue #373) are accepted for Protocol
        parity but inert here — there is no persisted manifest to diff, scope,
        or re-hash, and the temporal filter is applied at QUERY time
        (:meth:`query`), not here.
        """
        del cache_dir, incremental, include_globs, exclude_globs, as_of
        del full_rehash_max_age_days
        count = sum(
            1
            for p in wiki_root.rglob("*.md")
            if not p.name.startswith("_") and p.name not in _INTAKE_SKIP_NAMES
        )
        if extra_roots:
            for root in extra_roots:
                if not root.is_dir():
                    continue
                count += sum(
                    1 for p in root.rglob("*.md") if p.name not in _INTAKE_SKIP_NAMES
                )
        return count

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
    ) -> list[tuple[str, str, float]]:
        """Score every non-underscore wiki page and return the top-n hits.

        ``as_of`` (issue #308 slice 3) filters each page against its validity
        window at query time — the keyword backend scans on query, so it honors
        an as-of *rewind* directly (no as-of index build needed). ``None`` =
        today.
        """
        del cache_dir
        if wiki_root is None or not wiki_root.is_dir():
            return []

        tokens = tokenize_keyword_query(query)
        if not tokens:
            return []

        excluded = exclude or set()
        scored: list[tuple[float, str, str]] = []
        for md_file in wiki_root.rglob("*.md"):
            if md_file.name.startswith("_"):
                continue
            try:
                rel = md_file.relative_to(wiki_root).as_posix()
            except ValueError:
                rel = md_file.name
            if rel in excluded:
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            fm, body = parse_frontmatter(text)
            # Issue #191: skip inactive members (superseded_by / deprecated).
            # Issue #308 slice 3: also skip pages outside their validity window
            # relative to ``as_of`` (default today) — the query-time as-of view.
            if is_inactive_memory(fm, as_of):
                continue
            # Issue #312 — Layer B (keyword): authorize BEFORE scoring so a
            # forbidden page never enters ``scored`` and cannot occupy a top-n
            # slot. Owner (caller_audience=None) is authorized for everything.
            if not is_page_authorized(fm, caller_audience):
                continue
            score = score_keyword_page(tokens, fm, body)
            if score > 0:
                name = fm.get("name") or md_file.stem
                scored.append((score, rel, str(name)))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(fname, name, score) for score, fname, name in scored[:n]]


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[SearchBackend]] = {
    "fts5": FTS5Backend,  # type: ignore[dict-item]
    "vector": VectorBackend,  # type: ignore[dict-item]
    "keyword": KeywordBackend,  # type: ignore[dict-item]
}


def get_backend(name: str) -> SearchBackend:
    """Return a backend instance by name. Raises ``KeyError`` for unknown names."""
    cls = _BACKENDS.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown search backend {name!r}. "
            f"Available: {', '.join(sorted(_BACKENDS))}"
        )
    return cls()


# ---------------------------------------------------------------------------
# Convenience functions for shell hook scripts
# ---------------------------------------------------------------------------


def _coerce_as_of(as_of: date | str | None) -> date | None:
    """Coerce an ``as_of`` argument (ISO string or ``date``) to a ``date``.

    Convenience for shell-hook / CLI callers that pass ``as_of`` as an
    ISO-8601 ``YYYY-MM-DD`` string. ``None`` and ``date`` pass through. A
    non-empty unparseable string raises ``ValueError`` — unlike the fail-open
    frontmatter parse, an operator explicitly asking for an as-of view with a
    bad date should get a loud error, not a silent today-view.
    """
    if as_of is None or isinstance(as_of, date):
        return as_of
    return date.fromisoformat(as_of.strip())


def build_fts5_index(
    wiki_root: str | Path,
    cache_dir: str | Path,
    *,
    extra_roots: Iterable[str | Path] | None = None,
    incremental: bool = True,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    as_of: date | str | None = None,
    full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
) -> int:
    """Build an FTS5 index. Callable from shell hooks via ``python3 -c``.

    ``extra_roots`` accepts the same list as
    :meth:`FTS5Backend.build_index` (additional intake directories
    scanned recursively, e.g. ``~/knowledge/raw/auto-memory``).
    ``incremental`` (default ``True``, issue #348) applies only the
    add/change/delete delta; pass ``False`` to force a full rebuild.

    ``as_of`` (issue #308) builds an as-of *rewind* index: pass an ISO date
    string or a ``date`` to reflect the knowledge base as it stood then
    (always a full build). ``None`` (default) means today.
    ``full_rehash_max_age_days`` (issue #373) sets the periodic full-re-hash
    backstop for the stat pre-filter.
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return FTS5Backend().build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
        incremental=incremental,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        as_of=_coerce_as_of(as_of),
        full_rehash_max_age_days=full_rehash_max_age_days,
    )


def query_fts5_index(
    query: str,
    cache_dir: str | Path,
    *,
    n: int = 3,
    exclude: set[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Query the FTS5 index. Callable from shell hooks via ``python3 -c``."""
    return FTS5Backend().query(query, Path(cache_dir), n=n, exclude=exclude)


def build_vector_index(
    wiki_root: str | Path,
    cache_dir: str | Path,
    *,
    extra_roots: Iterable[str | Path] | None = None,
    incremental: bool = True,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    embedding_model: str | None = None,
    as_of: date | str | None = None,
    full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
) -> int:
    """Build a chromadb vector index. Callable from shell hooks.

    ``extra_roots`` accepts the same list as
    :meth:`VectorBackend.build_index`. ``incremental`` (default ``True``,
    issue #348) re-embeds only the delta. ``embedding_model`` (issue #315
    seam) defaults to ``all-MiniLM-L6-v2`` — the documented default is not
    changed here; swapping it forces a one-time full re-embed. ``as_of``
    (issue #308) builds an as-of *rewind* index — see :func:`build_fts5_index`.
    ``full_rehash_max_age_days`` (issue #373) sets the periodic full-re-hash
    backstop for the stat pre-filter.
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return VectorBackend(embedding_model=embedding_model).build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
        incremental=incremental,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        as_of=_coerce_as_of(as_of),
        full_rehash_max_age_days=full_rehash_max_age_days,
    )


def query_vector_index(
    query: str,
    cache_dir: str | Path,
    *,
    n: int = 3,
    exclude: set[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Query the chromadb vector index. Callable from shell hooks."""
    return VectorBackend().query(query, Path(cache_dir), n=n, exclude=exclude)


# ---------------------------------------------------------------------------
# Embedding helpers (issue #211 — decision-log semantic matching)
# ---------------------------------------------------------------------------

# Module-level memoized chromadb embedding function instance.  Loaded lazily
# so the module can be imported when chromadb is absent (it is an optional
# ``[vector]`` dependency).  When chromadb is not installed this stays ``None``
# and all callers gracefully degrade.
_EF: Any | None = None
_EF_LOADED: bool = False  # True once we have tried to load (even if None)


def _get_ef() -> Any | None:
    """Return a memoized chromadb DefaultEmbeddingFunction, or None."""
    global _EF, _EF_LOADED
    if _EF_LOADED:
        return _EF
    _EF_LOADED = True
    try:
        from chromadb.utils import embedding_functions  # type: ignore[import]

        _EF = embedding_functions.DefaultEmbeddingFunction()
    except Exception:  # ImportError, any chromadb init error
        _EF = None
    return _EF


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a list of texts using chromadb's default EF.

    Returns a list of float vectors (one per input string), or ``None`` when
    chromadb is not installed or the embedding call fails.  This function is
    the injectable default used by :func:`athenaeum.fingerprint.find_resolved_record`
    for the embedding similarity strategy.  Tests MUST inject a stub embedder —
    never rely on real chromadb in the test suite.
    """
    ef = _get_ef()
    if ef is None:
        return None
    try:
        result = ef(texts)
        # chromadb EF returns a list-like of list-likes; normalise to list[list[float]]
        return [list(map(float, vec)) for vec in result]
    except Exception:
        return None


def embed_text(text: str) -> list[float] | None:
    """Convenience wrapper: embed a single string.  Returns None on failure."""
    result = embed_texts([text])
    if result is None:
        return None
    return result[0]
