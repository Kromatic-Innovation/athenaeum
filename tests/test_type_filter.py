# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``recall`` type filter (issue athenaeum#964).

Covers the ``SearchBackend.query(type_filter=...)`` contract across all three
backends, the ``normalize_type_filter`` helper, and the vector backend's
degenerate-index guard exemption for a legitimately type-narrowed result set.
``TestVectorBackendTypeFilter`` requires network access to download chromadb's
default embedding model on first use — same requirement every pre-existing
``TestVectorBackend``-style test in this suite already has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.search import (
    FTS5Backend,
    KeywordBackend,
    VectorBackend,
    _hits_from_query_results,
    normalize_type_filter,
)


class TestNormalizeTypeFilter:
    def test_none_is_none(self) -> None:
        assert normalize_type_filter(None) is None

    def test_empty_string_is_none(self) -> None:
        assert normalize_type_filter("") is None

    def test_blank_string_is_none(self) -> None:
        assert normalize_type_filter("   ") is None

    def test_empty_list_is_none(self) -> None:
        assert normalize_type_filter([]) is None

    def test_list_of_blanks_is_none(self) -> None:
        assert normalize_type_filter(["", "  "]) is None

    def test_single_string(self) -> None:
        assert normalize_type_filter("person") == ("person",)

    def test_single_string_trimmed(self) -> None:
        assert normalize_type_filter("  person  ") == ("person",)

    def test_list_of_strings(self) -> None:
        assert normalize_type_filter(["person", "company"]) == ("person", "company")

    def test_dedupes_preserving_order(self) -> None:
        assert normalize_type_filter(["person", "company", "person"]) == (
            "person",
            "company",
        )


@pytest.fixture
def typed_wiki(tmp_path: Path) -> Path:
    """A wiki with pages of several types, including a top-level/nested split."""
    # NOTE: the FTS5 backend indexes name/tags/aliases/description ONLY — not
    # the body — so every page's shared search phrase must appear in
    # `description:` (the body copy is for the keyword backend, which DOES
    # score body text).
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    (wiki / "alice.md").write_text(
        "---\nuid: u-alice\ntype: person\nname: Alice Person\n"
        "description: lean startup methodology\n---\n\n"
        "Alice works on lean startup methodology.\n"
    )
    (wiki / "bob.md").write_text(
        "---\nuid: u-bob\ntype: person\nname: Bob Person\n"
        "description: lean startup methodology\n---\n\n"
        "Bob also works on lean startup methodology.\n"
    )
    (wiki / "acme.md").write_text(
        "---\nuid: u-acme\ntype: company\nname: Acme Corp\n"
        "description: lean startup methodology\n---\n\n"
        "Acme Corp practices lean startup methodology.\n"
    )
    # Rare class -- exercises the "sparse type" acceptance criterion.
    (wiki / "incident-1.md").write_text(
        "---\nuid: u-inc1\ntype: incident\nname: Outage One\n"
        "description: lean startup methodology\n---\n\n"
        "An incident about lean startup deploy failures.\n"
    )
    # Nested metadata.type shape (issue athenaeum#964 precedence).
    (wiki / "nested.md").write_text(
        "---\nuid: u-nested\nname: Nested Type Page\n"
        "description: lean startup methodology\nmetadata:\n  type: concept\n---\n\n"
        "A concept page about lean startup that uses the nested type shape.\n"
    )
    # Untyped page must never match any type filter.
    (wiki / "untyped.md").write_text(
        "---\nuid: u-untyped\nname: Untyped Page\n"
        "description: lean startup methodology\n---\n\n"
        "An untyped page about lean startup methodology too.\n"
    )
    return wiki


class TestFTS5TypeFilter:
    def test_no_filter_is_byte_identical(self, typed_wiki: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        before = backend.query("lean startup methodology", cache, n=10)
        after = backend.query(
            "lean startup methodology", cache, n=10, type_filter=None
        )
        assert before == after

    def test_single_type_filters(self, typed_wiki: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter="company"
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"acme.md"}

    def test_list_of_types_is_or(self, typed_wiki: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter=["company", "incident"]
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"acme.md", "incident-1.md"}

    def test_unknown_type_returns_no_results(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter="no-such-type"
        )
        assert results == []

    def test_type_absent_from_registry_still_returns_results(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        # Issue athenaeum#964 AC: the filter is opaque and NOT validated against
        # `wiki/_schema/types.md`. A type present in the corpus but absent
        # from a real declared registry (`incident` here, mirroring the
        # issue's own `auto-memory`-not-in-types.md evidence) still matches.
        (typed_wiki / "_schema").mkdir()
        (typed_wiki / "_schema" / "types.md").write_text(
            "| Type |\n|---|\n| person |\n| company |\n"
        )
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter="incident"
        )
        assert [fn for fn, _n, _s in results] == ["incident-1.md"]


class TestFTS5CandidatesByType:
    """``candidates_by_type`` — the plain indexed WHERE issue athenaeum#965's
    enumeration primitive reads (AC amendment 3), as distinct from ``query``'s
    MATCH/BM25-ranked path above."""

    def test_returns_every_filename_of_the_type_sorted(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        assert backend.candidates_by_type(cache, "person") == ["alice.md", "bob.md"]

    def test_nested_metadata_type_shape_is_found(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        assert backend.candidates_by_type(cache, "concept") == ["nested.md"]

    def test_unknown_type_returns_empty(self, typed_wiki: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        assert backend.candidates_by_type(cache, "no-such-type") == []

    def test_missing_index_returns_empty_without_raising(self, tmp_path: Path) -> None:
        backend = FTS5Backend()
        assert backend.candidates_by_type(tmp_path / "no-such-cache", "person") == []

    def test_nested_metadata_type_is_filterable(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter="concept"
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"nested.md"}

    def test_type_filter_composes_with_exclude(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup",
            cache,
            n=10,
            type_filter="person",
            exclude={"alice.md"},
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"bob.md"}

    def test_type_filter_narrows_before_limit(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        # Both `person` pages match the query; a type filter to `company`
        # with n=1 must return the single real company match, not be
        # crowded out by the two person hits ranking above it.
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=1, type_filter="company"
        )
        assert len(results) == 1
        assert results[0][0] == "acme.md"


class TestKeywordTypeFilter:
    def test_no_filter_is_byte_identical(self, typed_wiki: Path, tmp_path: Path) -> None:
        backend = KeywordBackend()
        cache = tmp_path / "cache"
        before = backend.query(
            "lean startup methodology", cache, n=10, wiki_root=typed_wiki
        )
        after = backend.query(
            "lean startup methodology",
            cache,
            n=10,
            wiki_root=typed_wiki,
            type_filter=None,
        )
        assert before == after

    def test_single_type_filters(self, typed_wiki: Path, tmp_path: Path) -> None:
        backend = KeywordBackend()
        cache = tmp_path / "cache"
        results = backend.query(
            "lean startup",
            cache,
            n=10,
            wiki_root=typed_wiki,
            type_filter="person",
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"alice.md", "bob.md"}

    def test_unknown_type_returns_no_results(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        backend = KeywordBackend()
        cache = tmp_path / "cache"
        results = backend.query(
            "lean startup",
            cache,
            n=10,
            wiki_root=typed_wiki,
            type_filter="no-such-type",
        )
        assert results == []

    def test_nested_metadata_type_is_filterable(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        backend = KeywordBackend()
        cache = tmp_path / "cache"
        results = backend.query(
            "lean startup",
            cache,
            n=10,
            wiki_root=typed_wiki,
            type_filter="concept",
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"nested.md"}


class TestHitsFromQueryResultsTypeNarrowed:
    """``_hits_from_query_results``' degenerate-index guard exemption.

    Pure/deterministic (no chromadb required) — issue athenaeum#964's own AC:
    a type filter legitimately narrowing to a couple of pages must not trip
    the athenaeum#489 flat-score guard just because the tiny result set ties.
    """

    def _tied_results(self, n: int) -> dict:
        return {
            "ids": [[f"p{i}.md" for i in range(n)]],
            "metadatas": [[{"name": f"P{i}", "type": "incident"} for i in range(n)]],
            "distances": [[0.42] * n],
        }

    def test_unfiltered_tie_still_raises(self) -> None:
        from athenaeum.search import DegradedIndexError

        with pytest.raises(DegradedIndexError):
            _hits_from_query_results(self._tied_results(3), 3, None)

    def test_type_narrowed_tie_does_not_raise(self) -> None:
        hits = _hits_from_query_results(
            self._tied_results(2), 2, None, type_narrowed=True
        )
        assert len(hits) == 2
        assert {h[0] for h in hits} == {"p0.md", "p1.md"}


class TestVectorBackendTypeFilter:
    """Requires network egress to fetch chromadb's default embedding model."""

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_single_type_filters(self, typed_wiki: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup", cache, n=10, type_filter="company"
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"acme.md"}

    def test_sparse_type_returns_real_results_not_error(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(typed_wiki, cache)
        # `incident` has exactly one live page in the fixture -- must not
        # trip the degenerate-index guard.
        results = backend.query(
            "lean startup", cache, n=5, type_filter="incident"
        )
        assert [fn for fn, _n, _s in results] == ["incident-1.md"]

    def test_type_filter_composes_with_exclude(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(typed_wiki, cache)
        results = backend.query(
            "lean startup",
            cache,
            n=10,
            type_filter="person",
            exclude={"alice.md"},
        )
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"bob.md"}

    def test_metadata_schema_version_forces_full_rebuild(
        self, typed_wiki: Path, tmp_path: Path
    ) -> None:
        import json

        from athenaeum.search import _VECTOR_MANIFEST

        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(typed_wiki, cache)

        manifest_path = cache / _VECTOR_MANIFEST
        manifest = json.loads(manifest_path.read_text())
        assert manifest["metadata_schema_version"] == VectorBackend._METADATA_SCHEMA_VERSION

        # Simulate a pre-athenaeum#964 manifest (no metadata_schema_version key).
        del manifest["metadata_schema_version"]
        manifest_path.write_text(json.dumps(manifest))

        # An ordinary incremental build must force a full rebuild rather than
        # silently keeping the old (type-less) metadata on every untouched page.
        backend.build_index(typed_wiki, cache)
        results = backend.query("lean startup", cache, n=10, type_filter="company")
        filenames = {fn for fn, _n, _s in results}
        assert filenames == {"acme.md"}
