# SPDX-License-Identifier: Apache-2.0
"""COMPILATION writes the edges, not merely the algorithm (issue athenaeum#1576).

``tests/test_relatedness.py`` proves the writer decides correctly and
``tests/evals/test_relatedness_writer_eval.py`` grades what it decides against
issue athenaeum#1570's ground truth -- but both reach
:mod:`athenaeum.relatedness` directly. AC1's wording is "carry edges AFTER
COMPILATION", and nothing above actually asserts that the librarian's write
boundary invokes the writer at all: unwire both call sites, or make
``run_index`` return ``None`` unconditionally, and every one of those tests
stays green.

``TestBatchSyncEquivalence::test_wiki_output_identical`` does not close that
gap either -- it only catches a ONE-SIDED removal, since two paths that both
write nothing are still identical.

So this file drives the real ``librarian._apply_tier3_results`` against a real
wiki directory and reads the bytes that land on disk.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import EntityIndex, ProcessingResult, RawFile, WikiEntity, parse_frontmatter
from athenaeum.relatedness import DEFAULT_MIN_INDEX_PAGES, ROLE_TERM_OVERLAP, reset_run_index

# Two distinctive vocabularies. Pages built from the first should be mutual
# neighbours of the entity compiled below; pages built from the second share
# nothing with it and exist to give the corpus enough documents for the term
# weighting to have something to weight against.
_BERTH = "berth scheduling quayside harbour rota manifest cutover"
_LEDGER = "invoice reconciliation ledger remittance quarterly depreciation"


def _seed_wiki(root: Path, *, related_pages: int, filler_pages: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Kept few and non-identical. Six byte-identical pages would be each
    # other's whole top-k and would crowd the compiled page out of their
    # neighbour lists -- the mutuality test would then correctly refuse an
    # edge, and this file would be asserting on a fixture accident.
    for i in range(related_pages):
        _page(root, f"rel{i:04d}", f"Berth note {i}", f"{_BERTH} variant{i}")
    for i in range(filler_pages):
        _page(root, f"fil{i:04d}", f"Ledger note {i}", f"{_LEDGER} entry{i}")


def _page(root: Path, uid: str, name: str, body: str) -> None:
    (root / f"{uid}.md").write_text(
        f"---\nuid: {uid}\ntype: concept\nname: {name!r}\naccess: internal\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def _compile_one(tmp_path: Path, entity: WikiEntity, config: dict | None = None) -> str:
    """Run the real write boundary and return the page's bytes on disk."""
    reset_run_index()
    wiki = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "20260201T000000Z-aabbccdd.md"
    raw_path.write_text("source\n", encoding="utf-8")
    raw = RawFile(
        path=raw_path,
        source="sessions",
        timestamp="20260201T000000Z",
        uuid8="aabbccdd",
        _content="source\n",
    )
    result = ProcessingResult(raw_file=raw)
    _apply_tier3_results(
        result,
        new_entities=[entity],
        pending_updates=[],
        updated_uids=[],
        escalations=[],
        wiki_root=wiki,
        index=EntityIndex(wiki),
        config=config,
        raw=raw,
    )
    reset_run_index()
    return (wiki / entity.filename).read_text(encoding="utf-8")


def _entity(uid: str = "newpage1") -> WikiEntity:
    return WikiEntity(
        uid=uid,
        type="concept",
        name="Berth cutover note",
        access="internal",
        created="2026-02-01",
        updated="2026-02-01",
        body=f"# Berth cutover note\n\n{_BERTH}\n",
        source="script:relatedness-compile-integration-test",
    )


def test_compilation_writes_a_related_edge(tmp_path):
    """The AC1 claim, end to end: a compiled page lands with edges on disk."""
    _seed_wiki(tmp_path / "wiki", related_pages=2, filler_pages=DEFAULT_MIN_INDEX_PAGES)
    text = _compile_one(tmp_path, _entity())

    meta, _body = parse_frontmatter(text)
    related = meta.get("related") or []
    assert related, f"compilation wrote no related: block\n{text}"
    assert all(row["role"] == ROLE_TERM_OVERLAP for row in related), related
    assert all(row["uid"].startswith("rel") for row in related), related


def test_compilation_writes_nothing_when_the_knob_is_off(tmp_path):
    _seed_wiki(tmp_path / "wiki", related_pages=2, filler_pages=DEFAULT_MIN_INDEX_PAGES)
    text = _compile_one(tmp_path, _entity(), config={"librarian": {"relatedness_writer": False}})

    meta, _body = parse_frontmatter(text)
    assert not (meta.get("related") or [])


def test_compilation_writes_nothing_on_a_wiki_below_the_index_floor(tmp_path):
    """A small wiki compiles exactly as it did before athenaeum#1576.

    The corpus weighting is relative, so on a handful of pages every cosine is
    large and the writer would link everything -- see
    ``DEFAULT_MIN_INDEX_PAGES``. These pages are deliberately near-identical,
    the worst case for that failure.
    """
    _seed_wiki(tmp_path / "wiki", related_pages=3, filler_pages=0)
    text = _compile_one(tmp_path, _entity())

    meta, _body = parse_frontmatter(text)
    assert not (meta.get("related") or [])


def test_edges_land_inside_the_bytes_the_schema_gate_validates(tmp_path):
    """The stamp must precede ``entity.render()``, not follow the write.

    Asserted through the on-disk page rather than the in-memory entity: if the
    edges were appended after rendering, the object would carry them and the
    file would not.
    """
    _seed_wiki(tmp_path / "wiki", related_pages=2, filler_pages=DEFAULT_MIN_INDEX_PAGES)
    entity = _entity()
    text = _compile_one(tmp_path, entity)

    assert ROLE_TERM_OVERLAP in text
    assert entity.related
