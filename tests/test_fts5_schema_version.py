# SPDX-License-Identifier: Apache-2.0
"""FTS5 index schema-version stamping and rebuild-on-mismatch (issue #530, M7).

Before this fix the FTS5 index carried no ``PRAGMA user_version`` stamp, so a
DB built by an older athenaeum (a pre-``audience`` shape) survived an
incremental build: ``CREATE VIRTUAL TABLE IF NOT EXISTS`` is a no-op against
the old shape, the positional INSERT mismatches, and every audience-filtered
query hits the missing ``audience`` column, raises ``OperationalError``, and is
silently turned into an empty result set — the memory system answering "I don't
know" while looking healthy.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from athenaeum.search import _DB_NAME, _FTS5_MANIFEST, FTS5Backend

# The pre-#312 FTS5 shape: no ``audience`` column, no user_version stamp.
_LEGACY_CREATE_SQL = (
    "CREATE VIRTUAL TABLE wiki USING fts5"
    '(filename, name, tags, aliases, description, tokenize="porter unicode61")'
)


def _write_wiki_page(
    wiki: Path, filename: str, name: str, body: str, audience: str = "eng"
) -> None:
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / filename).write_text(
        f"---\nname: {name}\naudience: [{audience}]\n---\n{body}\n", encoding="utf-8"
    )


def _seed_legacy_index(cache: Path) -> Path:
    """Write a pre-audience, unstamped FTS5 DB plus a matching manifest."""
    cache.mkdir(parents=True, exist_ok=True)
    db_path = cache / _DB_NAME
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_LEGACY_CREATE_SQL)
        conn.execute(
            "INSERT INTO wiki VALUES (?,?,?,?,?)",
            ("stale.md", "Stale", "", "", "old body"),
        )
        conn.commit()
    finally:
        conn.close()
    # A manifest so the incremental gate is armed (`stored is not None`).
    (cache / _FTS5_MANIFEST).write_text(
        json.dumps({"version": 1, "hashes": {"stale.md": "deadbeef"}}),
        encoding="utf-8",
    )
    return db_path


def test_full_build_stamps_schema_version(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    cache = tmp_path / "cache"
    _write_wiki_page(wiki, "alpha.md", "alphaunique marker", "some body text")

    FTS5Backend().build_index(wiki, cache, incremental=False)

    db_path = cache / _DB_NAME
    assert FTS5Backend._db_schema_version(db_path) == FTS5Backend._SCHEMA_VERSION


def test_incremental_build_keeps_schema_version(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    cache = tmp_path / "cache"
    _write_wiki_page(wiki, "alpha.md", "alphaunique marker", "some body text")
    backend = FTS5Backend()
    backend.build_index(wiki, cache, incremental=False)
    # Add a page and rebuild incrementally — the stamp must remain current.
    _write_wiki_page(wiki, "beta.md", "betaunique marker", "more body text")
    backend.build_index(wiki, cache, incremental=True)
    assert (
        FTS5Backend._db_schema_version(cache / _DB_NAME)
        == FTS5Backend._SCHEMA_VERSION
    )


def test_legacy_db_has_zero_schema_version(tmp_path: Path) -> None:
    db_path = _seed_legacy_index(tmp_path / "cache")
    assert FTS5Backend._db_schema_version(db_path) == 0


def test_pre_audience_db_triggers_rebuild_not_empty_recall(tmp_path: Path) -> None:
    # The core M7 reproduction: a legacy, unstamped, pre-``audience`` DB plus a
    # manifest would otherwise be reused for an incremental build. The
    # schema-version mismatch must instead force a full rebuild, so an
    # audience-filtered recall returns the real hit rather than a silent [].
    wiki = tmp_path / "wiki"
    cache = tmp_path / "cache"
    db_path = _seed_legacy_index(cache)
    assert FTS5Backend._db_schema_version(db_path) == 0

    _write_wiki_page(wiki, "alpha.md", "alphaunique marker", "searchable body text")
    backend = FTS5Backend()
    backend.build_index(wiki, cache, incremental=True)

    # The mismatched schema forced a full rebuild → the DB is now audience-aware.
    assert FTS5Backend._db_schema_version(db_path) == FTS5Backend._SCHEMA_VERSION

    # And an audience-filtered recall (the missing-``audience``-column path that
    # used to raise OperationalError → []) returns the public page.
    results = backend.query("alphaunique", cache, caller_audience={"eng"})
    assert any(fn == "alpha.md" for fn, _name, _score in results)
    # The stale legacy row is gone (full rebuild replaced the whole table).
    assert not any(fn == "stale.md" for fn, _name, _score in results)
