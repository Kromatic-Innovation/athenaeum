"""Search backend abstraction for athenaeum.

Provides pluggable search backends for wiki recall queries.  The default
``fts5`` backend uses SQLite FTS5 with BM25 ranking and porter stemming.
A ``vector`` backend stub is provided for issue #32.

Shell hook scripts can call the module-level convenience functions
(``build_fts5_index``, ``query_fts5_index``, etc.) without constructing
backend objects.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SearchBackend(Protocol):
    """Interface that all search backends must satisfy."""

    def build_index(self, wiki_root: Path, cache_dir: Path) -> int:
        """Build or rebuild the search index.

        Returns the number of wiki pages indexed.
        """
        ...

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Search the index.

        Returns a list of ``(filename, page_name, score)`` tuples,
        ordered by relevance (best first).
        """
        ...


# ---------------------------------------------------------------------------
# FTS5 backend
# ---------------------------------------------------------------------------

# Stopwords stripped before building an FTS5 query.
_STOPWORDS: frozenset[str] = frozenset(
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

_DB_NAME = "wiki-index.db"


class FTS5Backend:
    """SQLite FTS5 full-text search with BM25 ranking and porter stemming."""

    def build_index(self, wiki_root: Path, cache_dir: Path) -> int:
        """Scan wiki markdown files and build an FTS5 index."""
        db_path = cache_dir / _DB_NAME
        cache_dir.mkdir(parents=True, exist_ok=True)

        if db_path.exists():
            db_path.unlink()

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE VIRTUAL TABLE wiki USING fts5"
            "(filename, name, tags, aliases, description, "
            'tokenize="porter unicode61")'
        )

        rows: list[tuple[str, str, str, str, str]] = []
        for fname in sorted(os.listdir(wiki_root)):
            if not fname.endswith(".md") or fname.startswith("_"):
                continue
            path = wiki_root / fname
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue

            name, tags, aliases, description = "", "", "", ""
            if text.startswith("---"):
                end = text.find("---", 4)
                if end > 0:
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

            if not name:
                name = fname.replace(".md", "")

            rows.append((fname, name, tags, aliases, description))

        conn.executemany("INSERT INTO wiki VALUES (?,?,?,?,?)", rows)
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
    ) -> list[tuple[str, str, float]]:
        """Query the FTS5 index. Returns ``(filename, name, score)`` triples."""
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

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                f"SELECT filename, name, rank FROM wiki "
                f"WHERE wiki MATCH ? {exclude_clause} "
                f"ORDER BY rank LIMIT ?",
                [fts_query, *params, n],
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

    def build_index(self, wiki_root: Path, cache_dir: Path) -> int:
        """Build a chromadb collection from wiki markdown files."""
        chromadb = self._get_chromadb()

        vector_dir = cache_dir / _VECTOR_DIR
        vector_dir.mkdir(parents=True, exist_ok=True)

        client = chromadb.PersistentClient(path=str(vector_dir))
        # Delete and recreate to ensure a clean rebuild
        try:
            client.delete_collection(_VECTOR_COLLECTION)
        except Exception:
            pass
        collection = client.create_collection(_VECTOR_COLLECTION)

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []

        for fname in sorted(os.listdir(wiki_root)):
            if not fname.endswith(".md") or fname.startswith("_"):
                continue
            path = wiki_root / fname
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue

            name = ""
            if text.startswith("---"):
                end = text.find("---", 4)
                if end > 0:
                    fm = text[4:end]
                    for line in fm.splitlines():
                        line = line.strip()
                        if line.startswith("name:"):
                            name = line[5:].strip().strip("\"'")
                            break

            if not name:
                name = fname.replace(".md", "")

            ids.append(fname)
            documents.append(text)
            metadatas.append({"name": name, "filename": fname})

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
    ) -> list[tuple[str, str, float]]:
        """Query the chromadb collection with semantic search."""
        chromadb = self._get_chromadb()

        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return []

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(_VECTOR_COLLECTION)
        except Exception:
            return []

        if collection.count() == 0:
            return []

        # Build where filter for exclusions
        where: dict[str, Any] | None = None
        if exclude and len(exclude) == 1:
            where = {"filename": {"$ne": next(iter(exclude))}}
        elif exclude and len(exclude) > 1:
            where = {"filename": {"$nin": list(exclude)}}

        results = collection.query(
            query_texts=[query],
            n_results=min(n, collection.count()),
            where=where,
        )

        hits: list[tuple[str, str, float]] = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                name = meta.get("name", doc_id.replace(".md", ""))
                distance = results["distances"][0][i] if results["distances"] else 0.0
                hits.append((doc_id, name, distance))

        return hits


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[SearchBackend]] = {
    "fts5": FTS5Backend,  # type: ignore[dict-item]
    "vector": VectorBackend,  # type: ignore[dict-item]
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
    wiki_root: str | Path, cache_dir: str | Path
) -> int:
    """Build an FTS5 index. Callable from shell hooks via ``python3 -c``."""
    return FTS5Backend().build_index(Path(wiki_root), Path(cache_dir))


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
    wiki_root: str | Path, cache_dir: str | Path
) -> int:
    """Build a chromadb vector index. Callable from shell hooks."""
    return VectorBackend().build_index(Path(wiki_root), Path(cache_dir))


def query_vector_index(
    query: str,
    cache_dir: str | Path,
    *,
    n: int = 3,
    exclude: set[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Query the chromadb vector index. Callable from shell hooks."""
    return VectorBackend().query(query, Path(cache_dir), n=n, exclude=exclude)
