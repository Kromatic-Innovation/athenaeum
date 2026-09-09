# SPDX-License-Identifier: Apache-2.0
"""Regenerate ``golden_hits.json`` for the retrieval golden-set suite (athenaeum#1420).

The golden set is FROZEN by design (AC1/AC5): ``tests/test_retrieval_golden_1420.py``
asserts hit identity and rank order against this file, not against whatever
the backends happen to return on a given run. When a later, legitimate
ranking change (e.g. athenaeum#1493) makes the current goldens stale, regenerate
them here and review the diff like any other code change — never hand-edit
the JSON.

Usage (from the repo root, with the project's own venv active):

    python -m tests.fixtures.retrieval_golden.update_goldens

This is the same idiom ``src/athenaeum/prompt_registry.py --write`` uses for
its own frozen goldens: a small script co-located with the fixture, run
explicitly, that overwrites the golden file from the CURRENT behavior of the
code under test. It intentionally does NOT run under pytest or via a
``--update-goldens`` pytest flag — the golden file this writes must never be
regenerated implicitly by a normal test run.

Regenerating does NOT change the fixture corpus itself (``corpus.py`` is the
source of truth for the pages); it only re-records what FTS5Backend and
VectorBackend currently return against that corpus.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (base, *base.parents):
            if (parent / "pyproject.toml").is_file() and (parent / "src" / "athenaeum").is_dir():
                return parent
    raise RuntimeError("could not locate repo root (pyproject.toml + src/athenaeum)")


GOLDEN_FILENAME = "golden_hits.json"


def _patch_offline_embeddings() -> None:
    """Route chromadb's default embedding function through the SAME
    deterministic, offline lexical stand-in ``tests/conftest.py``'s autouse
    ``_offline_embedding_function`` fixture installs for every test run.

    This script runs OUTSIDE pytest, so that fixture never fires. Without
    this, the vector backend would embed with the real ONNX MiniLM model
    (offline-capable in this container per the dispatch brief, but not
    bit-identical to the lexical stand-in) and the golden file would record
    hits the actual pytest run — which DOES apply the stand-in — could never
    reproduce. Mirrors ``tests/conftest.py::_offline_embedding_function``
    exactly, just via plain attribute assignment instead of
    ``monkeypatch.setattr`` (no pytest fixture machinery available here).
    """
    import chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 as onnx_module

    from tests.offline_embeddings import OfflineONNXMiniLMStub

    onnx_module.ONNXMiniLM_L6_V2 = OfflineONNXMiniLMStub  # type: ignore[misc]


def build_goldens() -> dict[str, object]:
    """Build both backends' indices over the fixture corpus and record top-3
    hit identity + rank order for every query in ``corpus.QUERIES`` (AC1)."""
    from athenaeum.search import get_backend
    from tests.fixtures.retrieval_golden import corpus as golden_corpus

    _patch_offline_embeddings()

    with tempfile.TemporaryDirectory(prefix="athenaeum-retrieval-golden-") as tmp:
        tmp_path = Path(tmp)
        wiki_root = tmp_path / "wiki"
        golden_corpus.build_corpus(wiki_root)

        backends = {}
        for name in ("fts5", "vector"):
            backend = get_backend(name)
            cache_dir = tmp_path / f"cache-{name}"
            backend.build_index(wiki_root, cache_dir)
            backends[name] = (backend, cache_dir)

        goldens: dict[str, object] = {"queries": {}}
        for backend_name, (backend, cache_dir) in backends.items():
            per_query: dict[str, list[dict[str, object]]] = {}
            for query in golden_corpus.QUERIES:
                hits = backend.query(query, cache_dir, n=3, wiki_root=wiki_root)
                per_query[query] = [
                    {"filename": fname, "name": name} for fname, name, _score in hits
                ]
            goldens["queries"][backend_name] = per_query  # type: ignore[index]
        return goldens


def write_goldens() -> Path:
    root = _repo_root()
    out_path = root / "tests" / "fixtures" / "retrieval_golden" / GOLDEN_FILENAME
    goldens = build_goldens()
    out_path.write_text(json.dumps(goldens, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    del argv
    path = write_goldens()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
