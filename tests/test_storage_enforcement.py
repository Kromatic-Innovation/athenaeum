# SPDX-License-Identifier: Apache-2.0
"""Enforcement tests for the storage-adapter corpus policy (issue athenaeum#532, H4/M34).

``tests/test_storage.py`` already unit-tests the ``is_embedded`` /
``is_recallable`` predicates in isolation. Those predicates existed but were
*unenforced* — no index builder consulted ``is_embedded`` and no recall path
consulted ``is_recallable``, so ``docs/extending/storage-adapter-contract.md`` sold three
fail-closed guarantees while two did nothing (a class configured
``recallable: false`` was still returned by ``recall``).

These tests pin the enforcement that closes that gap:

* ``is_embedded`` is honored at index BUILD (a non-embedded class never enters
  the FTS5 / vector store), mirroring the athenaeum#427 PII drop.
* ``is_recallable`` is honored at recall RENDER (a non-recallable class is never
  returned even if it slipped into the index), mirroring the athenaeum#312 audience
  Layer-C re-check.
* Both are strict no-ops for the default (unconfigured) knowledge base.

The M34 contract test drives a custom adapter registered through the in-process
``register_adapter`` extension point all the way through resolve → index →
recall, giving the previously-unexercised extension point an in-tree consumer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import storage
from athenaeum.mcp_server import recall_search
from athenaeum.search import FTS5Backend, build_fts5_index, query_fts5_index
from athenaeum.storage import CorpusPolicy, StorageAdapter, register_adapter


@pytest.fixture(autouse=True)
def _reset_registered_adapters():
    """Snapshot/restore the in-process adapter registry around each test."""
    snapshot = dict(storage._REGISTERED_ADAPTERS)
    try:
        yield
    finally:
        storage._REGISTERED_ADAPTERS.clear()
        storage._REGISTERED_ADAPTERS.update(snapshot)


def _write_page(wiki: Path, filename: str, entity_type: str, marker: str) -> None:
    """A minimal wiki page of ``entity_type`` containing a unique ``marker``."""
    (wiki / filename).write_text(
        f"---\nname: {filename[:-3]}\ntype: {entity_type}\n"
        f"description: {marker} page\n---\n\n"
        f"This page mentions {marker} exactly once.\n"
    )


@pytest.fixture
def mixed_wiki(tmp_path: Path) -> Path:
    """A wiki with a default-surface page and a restricted-class page."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_page(wiki, "alice.md", "person", "publicmarker")
    _write_page(wiki, "ledger.md", "secret", "hushmarker")
    return wiki


# ---------------------------------------------------------------------------
# is_embedded — enforced at index BUILD
# ---------------------------------------------------------------------------


class TestEmbeddedEnforcedAtBuild:
    def test_non_embedded_class_dropped_from_fts5_index(
        self, mixed_wiki: Path, tmp_path: Path
    ) -> None:
        # Route the ``secret`` class onto the built-in all-false ``excluded``
        # surface. A page of that class sits physically in wiki/, so ONLY the
        # build-time policy consult can keep it out of the index.
        config = {"storage": {"mapping": {"secret": "excluded"}}}
        cache = tmp_path / "cache"

        count = build_fts5_index(mixed_wiki, cache, config=config)

        # Only the default-surface page is indexed.
        assert count == 1
        assert query_fts5_index("publicmarker", cache)  # person page present
        assert not query_fts5_index("hushmarker", cache)  # secret page dropped

    def test_no_config_indexes_every_page(
        self, mixed_wiki: Path, tmp_path: Path
    ) -> None:
        # Control: without a config the policy consult short-circuits and both
        # pages are indexed — proving the drop above is the policy, not a fluke.
        cache = tmp_path / "cache"
        count = build_fts5_index(mixed_wiki, cache, config=None)
        assert count == 2
        assert query_fts5_index("hushmarker", cache)

    def test_default_config_is_a_no_op(
        self, mixed_wiki: Path, tmp_path: Path
    ) -> None:
        # An empty/default storage config maps every class to the all-true wiki
        # surface — identical to config=None.
        cache = tmp_path / "cache"
        count = build_fts5_index(mixed_wiki, cache, config={"search_backend": "fts5"})
        assert count == 2
        assert query_fts5_index("hushmarker", cache)


# ---------------------------------------------------------------------------
# is_recallable — enforced at recall RENDER
# ---------------------------------------------------------------------------


class TestRecallableEnforcedAtRecall:
    def _embedded_not_recallable_config(self) -> dict:
        # A surface that IS embedded (so it reaches the index / a keyword scan)
        # but is NOT recallable — the case that isolates the recall-render
        # filter from the build-time embed filter.
        return {
            "storage": {
                "adapters": {
                    "index-only": {
                        "backing_store": "wiki-markdown",
                        "surface_root": "wiki",
                        "corpus_policy": {
                            "embedded": True,
                            "recallable": False,
                            "merge_eligible": False,
                        },
                    }
                },
                "mapping": {"secret": "index-only"},
            }
        }

    def test_non_recallable_class_withheld_from_recall(self, mixed_wiki: Path) -> None:
        config = self._embedded_not_recallable_config()
        # Keyword backend scans the wiki directly at query time, so the hidden
        # page IS a raw hit; only the recall-render policy consult can drop it.
        out = recall_search(
            mixed_wiki, "hushmarker", top_k=5, search_backend="keyword", config=config
        )
        assert "ledger" not in out.lower()
        assert "no wiki pages matched" in out.lower()

    def test_recallable_class_still_returned(self, mixed_wiki: Path) -> None:
        config = self._embedded_not_recallable_config()
        out = recall_search(
            mixed_wiki, "publicmarker", top_k=5, search_backend="keyword", config=config
        )
        assert "alice" in out.lower()

    def test_no_config_returns_every_hit(self, mixed_wiki: Path) -> None:
        # Control: without a storage policy the hidden page is recalled normally.
        out = recall_search(
            mixed_wiki, "hushmarker", top_k=5, search_backend="keyword", config=None
        )
        assert "ledger" in out.lower()


# ---------------------------------------------------------------------------
# M34 — the in-process extension point, exercised end to end
# ---------------------------------------------------------------------------


class TestAdapterExtensionPointContract:
    """A custom adapter registered via ``register_adapter`` driven through the
    full resolve → index → recall pipeline — the contract test the audit asks
    for, giving the extension point its first in-tree consumer."""

    def test_custom_registered_adapter_honored_end_to_end(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "readme.md", "person", "keepmarker")
        _write_page(wiki, "vault.md", "skillnote", "dropmarker")

        # Register a code-defined adapter with a no-corpus policy and map a
        # class to it — the athenaeum#426 skill-sync seam shape.
        register_adapter(
            StorageAdapter(
                name="skill-sync-test",
                backing_store="sqlite",
                surface_root="skills-ext",
                corpus_policy=CorpusPolicy.none(),
            )
        )
        config = {"storage": {"mapping": {"skillnote": "skill-sync-test"}}}

        # resolve → the registered adapter is what the class maps to.
        assert (
            storage.resolve_adapter_for_class("skillnote", config).name
            == "skill-sync-test"
        )

        # index → the skillnote page never enters the FTS5 store (embedded=False).
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(wiki, cache, config=config)
        assert count == 1
        assert query_fts5_index("keepmarker", cache)
        assert not query_fts5_index("dropmarker", cache)

        # recall → and it is never returned even by a direct keyword scan
        # (recallable=False), while the default-surface page is.
        hidden = recall_search(
            wiki, "dropmarker", top_k=5, search_backend="keyword", config=config
        )
        assert "no wiki pages matched" in hidden.lower()
        shown = recall_search(
            wiki, "keepmarker", top_k=5, search_backend="keyword", config=config
        )
        assert "readme" in shown.lower()


# ---------------------------------------------------------------------------
# Per-page ``embedded`` override (issue athenaeum#716) — the per-page
# COUNTERPART to ``is_embedded``'s class-level policy exercised above. Unit
# coverage of the predicate itself lives in ``tests/test_storage.py``;
# per-backend index-build exclusion lives in ``tests/test_search.py``. This
# class covers what neither of those can: the actual MCP recall ENTRY POINT
# (not a raw backend call) and the negative-only asymmetry proven through a
# real index build rather than the predicate in isolation.
# ---------------------------------------------------------------------------


class TestPerPageEmbeddedOverrideEnforcement:
    def _write_tombstone(self, wiki: Path, fname: str, *, name: str, marker: str) -> None:
        (wiki / fname).write_text(
            f"---\nname: {name}\nstatus: folded\nfolded_into: winner\n"
            f"embedded: false\n---\n\n{marker} content now lives elsewhere.\n"
        )

    def test_tombstone_invisible_via_recall_search_entry_point(
        self, tmp_path: Path
    ) -> None:
        """"Invisible to recall": driven through ``mcp_server.recall_search``
        itself (the entry point the MCP tool and ``_cmd_query.py`` both sit
        on top of), not the raw ``KeywordBackend.query`` call — a page-level
        override proven only at the raw backend would not discharge this
        AC, per the lane brief's own framing."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write_tombstone(
            wiki, "folded-source.md", name="Folded Source", marker="tombstonemarker"
        )
        out = recall_search(
            wiki, "tombstonemarker", top_k=5, search_backend="keyword", config=None
        )
        assert "folded-source" not in out.lower()
        assert "no wiki pages matched" in out.lower()

    def test_page_embedded_true_cannot_escape_excluded_class(
        self, tmp_path: Path
    ) -> None:
        """The negative-only asymmetry, proven end to end through a REAL FTS5
        index build rather than the predicate in isolation: a page of a
        class mapped to the all-false ``excluded`` adapter sets its OWN
        ``embedded: true`` and must still never enter the index."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "escapee.md").write_text(
            "---\nname: Escapee\ntype: secret\nembedded: true\n"
            "description: escapemarker page\n---\n\n"
            "This page mentions escapemarker exactly once.\n"
        )
        config = {"storage": {"mapping": {"secret": "excluded"}}}
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(wiki, cache, config=config)
        assert count == 0
        assert not query_fts5_index("escapemarker", cache)

    def test_page_embedded_false_excludes_from_an_otherwise_embedded_class(
        self, tmp_path: Path
    ) -> None:
        """Control for the test above: with NO storage config at all (the
        class is fully embedded by default), the page's own ``embedded:
        false`` still suppresses it — proving the drop above is the
        page-level override, not an artifact of the ``excluded`` adapter."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write_tombstone(
            wiki, "folded-source.md", name="Folded Source", marker="hushmarker"
        )
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(wiki, cache)
        assert count == 0
        assert not query_fts5_index("hushmarker", cache)
