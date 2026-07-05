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

import os
import re
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from athenaeum.models import (
    AUDIENCE_PUBLIC_TOKEN,
    audience_index_string,
    audience_string_authorized,
    is_inactive_memory,
    is_page_authorized,
    parse_frontmatter,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchBackend(Protocol):
    """Interface that all search backends must satisfy."""

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
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

        Returns the total number of pages indexed across all roots.
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
    ) -> list[tuple[str, str, float]]:
        """Search the index.

        ``wiki_root`` is used by scan-on-query backends (e.g. keyword) that
        don't maintain an on-disk index; indexed backends ignore it.

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


class FTS5Backend:
    """SQLite FTS5 full-text search with BM25 ranking and porter stemming."""

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
    ) -> int:
        """Scan wiki + extra intake roots and build an FTS5 index.

        See :meth:`SearchBackend.build_index` for the extra-root contract.
        Wiki entries are indexed with a bare filename; extra-root entries
        with ``<root_name>/<relpath>`` so the caller can disambiguate.
        """
        db_path = cache_dir / _DB_NAME
        cache_dir.mkdir(parents=True, exist_ok=True)

        if db_path.exists():
            db_path.unlink()

        conn = sqlite3.connect(str(db_path))
        # Issue #312: ``audience`` is UNINDEXED so it stays out of the BM25
        # term space (no ranking pollution) while remaining filterable in SQL.
        conn.execute(
            "CREATE VIRTUAL TABLE wiki USING fts5"
            "(filename, name, tags, aliases, description, audience UNINDEXED, "
            'tokenize="porter unicode61")'
        )

        rows: list[tuple[str, str, str, str, str, str]] = []
        for indexed_name, path in _scan_all_entries(wiki_root, extra_roots):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue

            # Issue #191: skip inactive members (superseded_by / deprecated)
            # so they never surface in recall. Frontmatter is at the top of
            # the file, so the truncated read above is sufficient.
            meta, _ = parse_frontmatter(text)
            if is_inactive_memory(meta):
                continue

            name, tags, aliases, description = _extract_frontmatter_fields(text)

            if not name:
                # For extra-root entries use the leaf stem (not the prefixed
                # indexed_name) so recall results show a clean title.
                name = path.stem

            # Issue #312: store each page's effective audience (delimited,
            # anchored) so Layer B can filter inside the query.
            audience = audience_index_string(meta)
            rows.append((indexed_name, name, tags, aliases, description, audience))

        conn.executemany("INSERT INTO wiki VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return len(rows)

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the FTS5 index. Returns ``(filename, name, score)`` triples."""
        del wiki_root  # FTS5 reads the pre-built index, not the wiki files
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
    Uses the default ``all-MiniLM-L6-v2`` embedding model.
    """

    def _get_chromadb(self) -> Any:
        try:
            import chromadb

            return chromadb
        except ImportError as exc:
            raise ImportError(
                "Vector backend requires chromadb. "
                "Install with: pip install athenaeum[vector]"
            ) from exc

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
    ) -> int:
        """Build a chromadb collection from wiki + extra intake roots.

        See :meth:`SearchBackend.build_index` for the extra-root contract.
        """
        chromadb = self._get_chromadb()

        vector_dir = cache_dir / _VECTOR_DIR
        # Nuke any prior on-disk state before opening a PersistentClient.
        # chromadb's SQLite metadata and the rust binding's collection store
        # can desync (stale UUIDs, corrupt sqlite, partial writes), causing
        # create_collection to return a Collection whose UUID the rust layer
        # then reports as non-existent on the first .add(). A full wipe on
        # each rebuild is the simplest robust reset — see issue #32.
        if vector_dir.exists():
            shutil.rmtree(vector_dir)
        vector_dir.mkdir(parents=True, exist_ok=True)

        # chromadb caches PersistentClient systems per-path at the module
        # level. When rebuild runs in a long-lived process (or the same
        # interpreter invoked it before) a cached system will still know
        # about the old "wiki" collection after we rmtree'd the dir,
        # causing create_collection to fail with "already exists". Clear
        # the cache so the new PersistentClient sees a fresh filesystem.
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()

        client = chromadb.PersistentClient(path=str(vector_dir))
        collection = client.create_collection(_VECTOR_COLLECTION)

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []

        for indexed_name, path in _scan_all_entries(wiki_root, extra_roots):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue

            # Issue #191: skip inactive members (superseded_by / deprecated).
            meta, _ = parse_frontmatter(text)
            if is_inactive_memory(meta):
                continue

            name, _tags, _aliases, _description = _extract_frontmatter_fields(text)

            if not name:
                name = path.stem

            # Issue #312 — Layer A: store the effective audience so the query
            # can pre-filter neighbors. chromadb metadata is scalar-only (no
            # list values), so the audience is stored as the same delimited
            # string as FTS5 and filtered in Python at query time (Layer B).
            ids.append(indexed_name)
            documents.append(text)
            metadatas.append(
                {
                    "name": name,
                    "filename": indexed_name,
                    "audience": audience_index_string(meta),
                }
            )

        # chromadb batches internally but has a max batch size
        batch_size = 5000
        for i in range(0, len(ids), batch_size):
            end = min(i + batch_size, len(ids))
            collection.add(
                ids=ids[i:end],
                documents=documents[i:end],
                metadatas=metadatas[i:end],
            )

        return len(ids)

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the chromadb collection with semantic search."""
        del wiki_root  # Vector reads the pre-built chromadb collection
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
            collection = client.get_collection(_VECTOR_COLLECTION)
        except Exception:
            return {}

        try:
            result = collection.get(ids=id_list, include=["embeddings"])
        except Exception:
            return {}

        out: dict[str, list[float]] = {}
        result_ids = result.get("ids") or []
        embeddings = result.get("embeddings") or []
        for i, doc_id in enumerate(result_ids):
            if i >= len(embeddings):
                continue
            vec = embeddings[i]
            if vec is None:
                continue
            # chromadb returns numpy arrays in some versions — coerce to list
            out[doc_id] = [float(x) for x in vec]
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
        if token in fm_text:
            score += 3.0
        if token in body_lower:
            score += 1.0
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
    ) -> int:
        """No-op: the keyword backend rescans on every query.

        Returns a count that includes wiki entries + extra-root entries
        (``MEMORY.md`` and non-``.md`` files excluded) so status checks
        see a comparable number to the indexed backends.
        """
        del cache_dir
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
    ) -> list[tuple[str, str, float]]:
        """Score every non-underscore wiki page and return the top-n hits."""
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
            if is_inactive_memory(fm):
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


def build_fts5_index(
    wiki_root: str | Path,
    cache_dir: str | Path,
    *,
    extra_roots: Iterable[str | Path] | None = None,
) -> int:
    """Build an FTS5 index. Callable from shell hooks via ``python3 -c``.

    ``extra_roots`` accepts the same list as
    :meth:`FTS5Backend.build_index` (additional intake directories
    scanned recursively, e.g. ``~/knowledge/raw/auto-memory``).
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return FTS5Backend().build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
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
) -> int:
    """Build a chromadb vector index. Callable from shell hooks.

    ``extra_roots`` accepts the same list as
    :meth:`VectorBackend.build_index`.
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return VectorBackend().build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
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
