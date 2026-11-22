# SPDX-License-Identifier: Apache-2.0
"""Time ``athenaeum run``'s post-compile search-index refresh (athenaeum#3124).

Builds a synthetic store of PAGES wiki pages, indexes it once, then times
``_refresh_index_after_run`` three times with nothing changed and three times
after rewriting CHANGED pages. No model calls are made; ``run``'s compile is
not part of the timing.

    python scripts/bench_run_index_refresh.py fts5 1000 5 /tmp/bench
    python scripts/bench_run_index_refresh.py vector 1000 5 /tmp/bench

The ``vector`` backend needs the ``vector`` extra and chromadb's MiniLM model.
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import time
from pathlib import Path


def main(backend: str, n_pages: int, n_changed: int, root: Path) -> None:
    import athenaeum.librarian as lib
    from athenaeum._cmd_run import _refresh_index_after_run
    from athenaeum.init import init_knowledge_dir

    rng = random.Random(7)
    words = (
        "launch code project budget meeting partner review board contract "
        "hiring roadmap customer invoice travel"
    ).split()

    def body(i: int, rev: int = 0) -> str:
        text = " ".join(rng.choice(words) for _ in range(180))
        return f"---\nname: Entity {i}\ntype: project\n---\n\n{text} rev{rev}\n"

    knowledge = root / "knowledge"
    init_knowledge_dir(knowledge)
    (knowledge / "athenaeum.yaml").write_text(f"search_backend: {backend}\n", encoding="utf-8")
    wiki = knowledge / "wiki"
    for i in range(n_pages):
        (wiki / f"{i:08x}-entity-{i}.md").write_text(body(i), encoding="utf-8")
    cache = root / "cache"
    os.environ["ATHENAEUM_CACHE_DIR"] = str(cache)

    start = time.perf_counter()
    lib.reindex(knowledge_root=knowledge, cache_dir=cache, backend=backend)
    full = time.perf_counter() - start

    def timed() -> float:
        start = time.perf_counter()
        _refresh_index_after_run(knowledge, wiki, cache)
        return time.perf_counter() - start

    unchanged = [timed() for _ in range(3)]
    changed = []
    for rep in range(3):
        for i in rng.sample(range(n_pages), n_changed):
            (wiki / f"{i:08x}-entity-{i}.md").write_text(body(i, rep + 1), encoding="utf-8")
        changed.append(timed())
    print(
        f"backend={backend} pages={n_pages} changed={n_changed} full_reindex={full:.2f}s "
        f"refresh_unchanged_median={statistics.median(unchanged):.2f}s "
        f"refresh_changed_median={statistics.median(changed):.2f}s "
        f"changed_runs={[round(x, 2) for x in changed]}"
    )


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]))
