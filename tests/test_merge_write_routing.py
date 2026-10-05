# SPDX-License-Identifier: Apache-2.0
"""Write-loop routing contracts for the C3 merge pass.

Issue athenaeum#1256 retired the C4 contradiction detector, which collapsed
``merge_clusters_to_wiki``'s two-phase write (write-every-page-unflagged, then
re-write only the pages whose C4 flag changed — issue athenaeum#462) down to a
SINGLE write loop. The file that used to pin that ordering,
``tests/test_merge_write_before_detect.py``, went away with the detector.

Two of its assertions were never about C4 at all, and nothing else in the suite
covers them end-to-end on disk — ``tests/test_delta.py`` only checks that
``only_cluster_ids`` is PASSED to a mocked ``merge_clusters_to_wiki``, never
that the write loop honours it. Since the surviving write loop is precisely the
code issue athenaeum#1256 restructured, those two contracts are re-homed here
rather than dropped:

- ``out_wiki_root`` redirect (issue athenaeum#359) — a compile-as-of run writes
  to its scratch dir and must never leak a recompiled snapshot into the live
  ``wiki/`` tree.
- ``only_cluster_ids`` delta scoping (issue athenaeum#370) — a delta run writes
  exactly the affected cluster's page and leaves every unaffected page
  untouched.

The fixture is carried over unchanged except for its C4-specific parts: the
members still carry no validity windows and declare no relationship, but there
is no longer any detector to run on them, and the ``cross_scope_mode`` knob the
old fixture set was retired with the detector it chunked for.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.merge import merge_clusters_to_wiki

_SCOPE = "-Users-tristankromer-Code"


def _write_am(root: Path, name: str, body: str) -> None:
    d = root / "raw" / "auto-memory" / _SCOPE
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )


def _write_cluster(root: Path, rows: list[dict]) -> None:
    out = root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )


def _seed_root(tmp_path: Path, n_clusters: int = 1) -> Path:
    """A knowledge root with ``n_clusters`` two-member clusters."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    rows = []
    for i in range(n_clusters):
        a, b = f"feedback_a{i}.md", f"feedback_b{i}.md"
        _write_am(root, a, f"Cluster {i} says the price is $50 per month.")
        _write_am(root, b, f"Cluster {i} says the price is $70 per month.")
        rows.append(
            {
                "cluster_id": f"pricing-{i:04d}",
                "member_paths": [f"{_SCOPE}/{a}", f"{_SCOPE}/{b}"],
                "centroid_score": 0.62,
                "rationale": "cosine >= 0.55; shares tokens: price, per, month",
            }
        )
    _write_cluster(root, rows)
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return root


def test_out_wiki_root_redirect_is_honored_by_the_write_loop(tmp_path: Path) -> None:
    """The write loop must target ``out_wiki_root`` (the compile-as-of scratch
    dir), never the live wiki — otherwise a recompiled snapshot leaks into the
    live tree (issue athenaeum#359)."""
    root = _seed_root(tmp_path, n_clusters=1)
    scratch = tmp_path / "scratch-wiki"

    entries = merge_clusters_to_wiki(root, out_wiki_root=scratch)

    assert len(entries) == 1
    assert sorted(scratch.glob("auto-*.md")), "the write loop must honor out_wiki_root"
    assert sorted((root / "wiki").glob("auto-*.md")) == [], (
        "an out_wiki_root run must leave the live wiki untouched"
    )


def test_only_cluster_ids_scopes_the_write_loop(tmp_path: Path) -> None:
    """Delta scope (issue athenaeum#370) must write ONLY the affected cluster's
    page: the write loop iterates the already-delta-filtered ``entries``, so an
    unaffected cluster is never written."""
    root = _seed_root(tmp_path, n_clusters=2)

    entries = merge_clusters_to_wiki(root, only_cluster_ids={"pricing-0000"})

    assert len(entries) == 1
    assert entries[0].cluster_id == "pricing-0000"
    pages = sorted((root / "wiki").glob("auto-*.md"))
    assert len(pages) == 1, "delta scope must write exactly the one affected page"
