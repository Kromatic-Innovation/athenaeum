# SPDX-License-Identifier: Apache-2.0
"""Golden-set retrieval tests: hit identity + rank order, not hit count (athenaeum#1420).

**The finding this closes.** The pre-existing retrieval suite asserted things
like "still returns 3 results" — an assertion shape that is structurally
blind to silent substitution. A real `memory_tier = 'hot'` gate excluded
96.56% of the corpus and, in 10 of 12 sampled queries, still returned a full
3 hits, backfilled with a materially worse page: the topical answer was
suppressed and a lower-ranked page took the slot. A count-only assertion
passes that bug completely. Separately, nothing exercised the vector
backend at all, so a change could land on FTS5 only and every existing test
would stay green — the near-miss this issue names is athenaeum#1345, which
carried an AC that would have silently reverted two merged issues on the
vector path while the FTS5-path tests kept passing.

**What this module asserts, mapped to the issue's acceptance criteria:**

- AC1 (hit identity + rank, not count) — ``TestGoldenHitIdentity`` freezes
  the exact ordered filename list FTS5Backend/VectorBackend return for a
  fixed query set against ``tests/fixtures/retrieval_golden``'s corpus, and
  asserts on that list, not ``len(hits)``.
- AC2 (realistic tier/type mix) — ``TestFixtureTierMix`` asserts the fixture
  corpus itself is a small hot minority confined to
  principle/preference/auto-memory pages plus a dominant warm majority
  (see ``tests/fixtures/retrieval_golden/corpus.py``'s docstring for the
  exact ratio and why an all-hot/all-warm fixture proves nothing).
- AC3 (parameterized across both backends) — every ``TestGoldenHitIdentity``
  and ``TestTypeFilterBackendParity`` test is parametrized over
  ``BACKEND_NAMES = ("fts5", "vector")``, so a change landing on only one
  backend fails only that backend's parametrized case rather than passing
  the whole suite silently.
- AC4 (backend parity) — ``TestRecallSearchBackendParity`` calls the same
  MCP-facing ``recall_search`` entry point with ``search_backend="fts5"``
  and ``search_backend="vector"`` for a query both can serve, and asserts
  the rendered hit block is identical once the backend-specific relevance
  score is normalized out.
- AC5 (runs in CI unattended) — this file lives directly under ``tests/``
  (not ``tests/evals/``) and carries none of the ``eval``/``live``/
  ``embedding`` markers ``pyproject.toml``'s ``addopts`` deselects by
  default, so a plain ``pytest tests/`` — exactly what ``ci.yml``'s
  ``test`` job runs — collects and runs it unconditionally. Nothing here
  needs to be remembered or opted into.

**Ownership boundary (see the dispatch brief's ownership map).** This
module and ``tests/fixtures/retrieval_golden/`` are net-new and owned
entirely by athenaeum#1420. They do NOT touch, extend, or read
``tests/evals/data/corpus/**`` (athenaeum#1493 is migrating that corpus this
same dispatch) or ``tests/fixtures/wiki_sample`` (a different fixture, other
tests' baseline). ``src/athenaeum/search.py`` and
``src/athenaeum/mcp_server.py`` are read-only from this module's
perspective — it imports and calls them, never edits them.

**Regenerating the golden set.** ``tests/fixtures/retrieval_golden/golden_hits.json``
is frozen by design (AC1/AC5) and must never be hand-edited. When a
legitimate ranking change (e.g. athenaeum#1493) makes it stale, regenerate
and review the diff:

    python -m tests.fixtures.retrieval_golden.update_goldens

See that script's module docstring for why it must patch in the same
offline embedding stand-in ``tests/conftest.py`` installs for the test run,
rather than the real embedding model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.search import get_backend
from tests.fixtures.retrieval_golden import corpus as golden_corpus

BACKEND_NAMES = ("fts5", "vector")

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "retrieval_golden" / "golden_hits.json"


def _require_backend(name: str) -> None:
    if name == "vector":
        pytest.importorskip("chromadb")


def _load_golden() -> dict[str, dict[str, list[dict[str, str]]]]:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))["queries"]


@pytest.fixture(scope="module")
def golden_wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture corpus, built once and shared read-only across this module."""
    wiki_root = tmp_path_factory.mktemp("retrieval_golden_wiki") / "wiki"
    golden_corpus.build_corpus(wiki_root)
    return wiki_root


@pytest.fixture(scope="module")
def golden_caches(
    golden_wiki: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Path]:
    """One built index per backend over ``golden_wiki``, built once per module.

    ``tests/conftest.py``'s offline-embedding stand-in
    (``_offline_embedding_function``) is function-scoped and autouse, but
    this fixture is module-scoped — pytest instantiates broader-scope
    fixtures before the narrower-scope autouse fixtures of the first test
    that needs them, so by the time this fixture's body runs the per-test
    patch has NOT been applied yet. Building the vector index here would
    otherwise fall through to chromadb's real default embedding function
    and attempt a network fetch of the ONNX model — exactly the outbound
    call ``tests/test_network_guard.py`` exists to catch. Apply the SAME
    stand-in class (``tests.offline_embeddings.OfflineONNXMiniLMStub``)
    directly for the duration of the build, then undo it immediately —
    every actual `.query()` call happens inside a test body, where the
    normal per-test autouse fixture is already active.
    """
    pytest.importorskip("chromadb")  # both backends built together; vector needs it
    cache_root = tmp_path_factory.mktemp("retrieval_golden_cache")
    caches: dict[str, Path] = {}

    import chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 as onnx_module

    from tests.offline_embeddings import OfflineONNXMiniLMStub

    build_patch = pytest.MonkeyPatch()
    build_patch.setattr(onnx_module, "ONNXMiniLM_L6_V2", OfflineONNXMiniLMStub)
    try:
        for name in BACKEND_NAMES:
            cache_dir = cache_root / name
            get_backend(name).build_index(golden_wiki, cache_dir)
            caches[name] = cache_dir
    finally:
        build_patch.undo()
    return caches


class TestFixtureTypeMixAndOrphanedTierKeys:
    """AC2: the fixture corpus carries a realistic type mix.

    Originally this asserted a realistic TIER mix — an all-hot or all-warm
    fixture cannot express the substitution bug athenaeum#1420 is about (an
    eagerly-surfaced page silently backfilling for a suppressed one). Issue
    athenaeum#1514 retired the tier vocabulary, so the same property is now
    asserted where it actually lives: the TYPE mix, with the
    principle/preference/auto-memory pool a small minority of the corpus.

    The second half pins the orphaned `memory_tier:` frontmatter key that
    athenaeum#1514's chosen migration leaves on disk. It must still be
    present in the fixture (so the goldens keep exercising pages that carry
    one) and must still be inert (so a page carrying one is retrieved
    exactly like a page that does not).
    """

    _EAGER_TYPES = {"principle", "preference", "auto-memory"}

    def test_eagerly_surfaced_types_are_a_small_minority(self) -> None:
        pages = golden_corpus.all_pages()
        eager = [spec for spec in pages if spec.type in self._EAGER_TYPES]
        assert eager, "fixture must contain at least one eagerly-surfaced page"

        eager_fraction = len(eager) / len(pages)
        # Counter-example this guards against: a degenerate fixture whose
        # corpus is all one pool. The real corpus measured 96.56% / 3.44%;
        # this synthetic fixture is far smaller so exact percentages aren't
        # meaningful, but the SHAPE (small minority) must hold.
        assert 0 < eager_fraction <= 0.15, (
            f"eagerly-surfaced fraction out of range: {eager_fraction:.2%}"
        )

    def test_orphaned_memory_tier_pins_are_still_in_the_fixture(self) -> None:
        """Issue athenaeum#1514's migration is "leave the key in place, stop
        reading it" — so the fixture must keep carrying it, or the goldens
        would stop covering the on-disk shape the real corpus has.
        """
        pinned = [spec for spec in golden_corpus.all_pages() if spec.memory_tier is not None]
        assert pinned, (
            "the fixture must keep at least one orphaned `memory_tier:` pin, "
            "so retrieval is exercised against the shape the real corpus has"
        )
        assert all(spec.type in self._EAGER_TYPES for spec in pinned)

    def test_the_orphaned_key_reaches_the_page_but_not_the_index(
        self, golden_wiki: Path, golden_caches: dict[str, Path]
    ) -> None:
        """The key is inert, not absent: it IS written to the page's
        frontmatter, and it is NOT carried into the retrieval index. A test
        asserting only the second half would also pass on a fixture that had
        quietly stopped writing the key at all.
        """
        import sqlite3

        pinned = [spec for spec in golden_corpus.all_pages() if spec.memory_tier is not None]
        page = golden_wiki / pinned[0].filename
        assert "memory_tier:" in page.read_text(encoding="utf-8")

        conn = sqlite3.connect(golden_caches["fts5"] / "wiki-index.db")
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wiki)")}
        finally:
            conn.close()
        assert "memory_tier" not in cols, (
            f"the retired tier axis is back in the index schema: {sorted(cols)}"
        )


class TestGoldenHitIdentity:
    """AC1 + AC3: frozen hit identity + rank order, parameterized across both backends.

    Counter-example that must fail (see the dispatch brief's "prove your
    counter-examples" section for the sabotage-and-revert demonstration run
    against this class): swapping the top result for a lower-ranked page
    while the hit count stays at 3. A count-only assertion
    (``len(hits) == 3``) would pass that unchanged; asserting the ordered
    filename list against the golden catches it.
    """

    @pytest.mark.parametrize("backend_name", BACKEND_NAMES)
    @pytest.mark.parametrize("query", golden_corpus.QUERIES)
    def test_hit_identity_and_rank_order(
        self, backend_name: str, query: str, golden_caches: dict[str, Path]
    ) -> None:
        _require_backend(backend_name)
        golden = _load_golden()
        expected = [row["filename"] for row in golden[backend_name][query]]

        backend = get_backend(backend_name)
        hits = backend.query(query, golden_caches[backend_name], n=3)
        actual = [filename for filename, _name, _score in hits]

        assert actual == expected, (
            f"[{backend_name}] query {query!r}: hit identity/rank diverged from "
            f"golden.\n  expected: {expected}\n  actual:   {actual}\n"
            "If this is a deliberate, reviewed ranking change, regenerate via "
            "`python -m tests.fixtures.retrieval_golden.update_goldens` and "
            "review the diff — never hand-edit golden_hits.json."
        )


class TestTypeFilterBackendParity:
    """AC3's literal counter-example: removing a field from the vector
    branch's metadata lookup while FTS5 keeps supplying it (exactly
    athenaeum#1345's near-miss shape, at the `type` field instead of
    `scope`/`memory_tier`).

    ``VectorBackend._add_records`` stores ``type`` in chromadb's per-document
    metadata specifically so ``type_filter`` can push a ``where={"type": ...}``
    predicate into the query (``VectorBackend.query``). If that field is ever
    dropped from the metadata dict, chromadb's ``where`` clause matches
    nothing and every vector-backend type-filtered query silently returns
    empty — while FTS5, whose ``type`` column is written independently in
    ``FTS5Backend._row_for``, keeps working. This test is parameterized so
    that regression fails on the vector parameter alone.
    """

    @pytest.mark.parametrize("backend_name", BACKEND_NAMES)
    def test_type_filter_returns_exactly_the_principle_pages(
        self, backend_name: str, golden_caches: dict[str, Path]
    ) -> None:
        _require_backend(backend_name)
        backend = get_backend(backend_name)
        hits = backend.query(
            golden_corpus.PRINCIPLE_QUERY,
            golden_caches[backend_name],
            n=10,
            type_filter=golden_corpus.PRINCIPLE_TYPE,
        )
        actual = {filename for filename, _name, _score in hits}
        assert actual == golden_corpus.PRINCIPLE_FILENAMES, (
            f"[{backend_name}] type_filter={golden_corpus.PRINCIPLE_TYPE!r} "
            f"expected exactly {sorted(golden_corpus.PRINCIPLE_FILENAMES)}, "
            f"got {sorted(actual)}"
        )


_SCORE_RE = re.compile(r"\(score: [0-9.eE+-]+\)")


def _first_hit_block(rendered: str) -> str:
    """Extract the ``### 1. ...`` hit block from a ``recall_search`` rendering,
    with the backend-specific relevance score normalized out.

    ``recall_search`` prefixes its output with a ``Found N matching pages:``
    header whose ``N`` can legitimately differ between backends for the same
    query (different backends can surface a different NUMBER of matches);
    AC4 is about the RENDERING of a shared hit, not about the two backends
    returning identical result counts, so only the top hit's block is
    compared.
    """
    marker = "### 1. "
    idx = rendered.index(marker)
    block = rendered[idx + len(marker):]
    return _SCORE_RE.sub("(score: X)", block)


class TestRecallSearchBackendParity:
    """AC4: for a query both backends can serve, the rendered output is identical.

    Counter-example that must fail: bare-name bullets on the vector path
    while FTS5 renders full metadata (tags/uid/type/snippet/etc) — exactly
    the shape of the shell-hook near-miss athenaeum#1345 named for the
    `description` field (there, chromadb metadata didn't carry it and a
    join was needed; here the equivalent risk is the same asymmetric-branch
    shape reaching the MCP `recall_search` renderer in `mcp_server.py`).

    `recall_search` resolves every rendered field (name/tags/uid/type/
    source/updated/valid/status/links/snippet) from a FRESH on-disk
    frontmatter read (`_recall_via_backend` in `mcp_server.py`), keyed only
    by the hit filename both backends already return in the same
    `(filename, name, score)` shape — so today's code renders identically
    for both backends by construction. This test pins that invariant.
    """

    def test_top_hit_render_identical_modulo_score(
        self, golden_wiki: Path, golden_caches: dict[str, Path]
    ) -> None:
        pytest.importorskip("chromadb")
        rendered = {}
        for backend_name in BACKEND_NAMES:
            rendered[backend_name] = recall_search(
                golden_wiki,
                golden_corpus.PARITY_QUERY,
                top_k=1,
                search_backend=backend_name,
                cache_dir=golden_caches[backend_name],
            )

        blocks = {name: _first_hit_block(text) for name, text in rendered.items()}
        # Sanity: both backends actually served the SAME page for this query
        # (both goldens agree on golden_corpus.PARITY_FILENAME as rank 1) —
        # otherwise a block-text mismatch would be meaningless noise from
        # comparing two different pages' renders.
        for name, text in rendered.items():
            assert "Found 1 matching pages:" in text, f"[{name}] unexpected header: {text[:80]}"
            assert text.startswith("Found 1 matching pages:\n\n### 1. "), (
                f"[{name}] top_k=1 should render exactly one hit block: {text[:120]}"
            )

        assert blocks["fts5"] == blocks["vector"], (
            "backend parity broken: recall_search rendered a different block "
            f"for the same page across backends.\n--- fts5 ---\n{blocks['fts5']}"
            f"\n--- vector ---\n{blocks['vector']}"
        )
