#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sweep the recall relevance floor and report the abstention/recall trade-off.

Issue athenaeum#1492, AC5: **this script does not choose a production
threshold.** Selecting what value production should run is a deliberate
product judgment, explicitly deferred to a follow-up. What this script does
is measure, for a swept range of floor values on EACH backend the floor
mechanism covers (FTS5, keyword), two numbers a human making that call needs
side by side:

* **abstention CLEAN count** -- of the 3 ``abstention``-class probes in
  ``tests/evals/data/corpus/probes/probes.yaml``, how many correctly return
  nothing (:func:`tests.evals.metrics.grade_abstention`) at this floor.
* **per-class MRR** -- for every OTHER probe class (single_hop, multi_hop,
  temporal, disambiguation, distractor_robustness), the mean reciprocal rank
  of the first expected page, at the same floor. This is the genuine-recall
  cost: a floor that clears every abstention probe but crushes single_hop MRR
  to 0 is exactly the trade-off a human should see before picking a number.

Both numbers are measured through the REAL mechanism this issue ships
(:func:`athenaeum.search.meets_relevance_floor`), applied to REAL backend
query results (:meth:`SearchBackend.query`) against the synthetic corpus
(:mod:`tests.evals.corpus`) -- not a simulation of the floor's effect.

Usage::

    .venv/bin/python scripts/sweep_recall_relevance_floor.py \\
        --out docs/measurements/recall-relevance-floor-sweep.md

Corpus scale defaults to ``small`` (the smallest scale that actually
generates a distractor tier -- see ``tests.evals.corpus.SCALES`` -- and the
scale athenaeum#1492's own committed test, ``tests/test_eval_recall_floor.py``,
uses to reproduce the defect). Override with ``--scale``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from athenaeum.search import get_backend, meets_relevance_floor  # noqa: E402
from tests.evals.corpus import Probe, build_corpus  # noqa: E402
from tests.evals.metrics import grade_abstention, mrr  # noqa: E402

BACKENDS = ("fts5", "keyword")
TOP_K = 5

# One sweep grid PER backend -- FTS5's rank and the keyword scorer's additive
# score are different scales with different "better" directions (see
# ``athenaeum.search.meets_relevance_floor``'s docstring), so a single shared
# grid would not mean the same thing on both. Endpoints are chosen wide
# enough to show the FULL trade-off curve: one end must be loose enough that
# every abstention probe still confabulates (0/3 CLEAN, floor is inert) and
# the other strict enough that every probe of every class abstains (a floor
# so strict it also destroys genuine recall) -- otherwise the sweep would not
# show the human making the tuning call where the curve actually bends.
FTS5_GRID: tuple[float | None, ...] = (
    None,  # inactive -- today's shipped default
    -20.0,
    -12.0,
    -9.0,
    -7.0,
    -5.0,
    -3.0,
    -1.0,
    0.0,
)
KEYWORD_GRID: tuple[float | None, ...] = (
    None,  # inactive -- today's shipped default
    1.0,
    5.0,
    10.0,
    15.0,
    20.0,
    30.0,
    50.0,
    80.0,
)


@dataclass(frozen=True)
class ProbeResult:
    probe: Probe
    ranked_uids: tuple[str, ...]


def _query_all(
    backend_name: str, wiki_root: Path, cache_dir: Path, probes: list[Probe]
) -> dict[str, list[tuple[str, str, float]]]:
    """Run every probe's raw query ONCE per backend, at an unfiltered top_k.

    Returns ``{probe.id: [(filename, name, score), ...]}``. The floor is
    applied afterward, in Python, once per swept value -- this is what makes
    a 9-point sweep cost one query per probe instead of nine.
    """
    backend = get_backend(backend_name)
    results: dict[str, list[tuple[str, str, float]]] = {}
    for probe in probes:
        hits = backend.query(probe.query, cache_dir, n=TOP_K, wiki_root=wiki_root)
        results[probe.id] = hits
    return results


def _uid_of(filename: str) -> str:
    """Corpus pages are materialized as ``<uid>.md`` (``Page.filename``) --
    the reverse mapping is exact string surgery, not a frontmatter re-read."""
    return filename[: -len(".md")] if filename.endswith(".md") else filename


def _ranked_uids_at_floor(
    hits: list[tuple[str, str, float]], backend_name: str, floor: float | None
) -> list[str]:
    return [
        _uid_of(filename)
        for filename, _name, score in hits
        if meets_relevance_floor(backend_name, score, floor)
    ]


def _sweep_backend(
    backend_name: str,
    grid: tuple[float | None, ...],
    raw: dict[str, list[tuple[str, str, float]]],
    probes: list[Probe],
) -> list[dict[str, object]]:
    abstention_probes = [p for p in probes if p.probe_class == "abstention"]
    other_classes = sorted({p.probe_class for p in probes if p.probe_class != "abstention"})

    rows: list[dict[str, object]] = []
    for floor in grid:
        ranked_by_id = {
            probe.id: _ranked_uids_at_floor(raw[probe.id], backend_name, floor) for probe in probes
        }

        clean = sum(
            1
            for probe in abstention_probes
            if grade_abstention(ranked_by_id[probe.id]).outcome.name == "CLEAN"
        )

        class_mrr: dict[str, float] = {}
        for probe_class in other_classes:
            class_probes = [p for p in probes if p.probe_class == probe_class]
            scores = [mrr(ranked_by_id[p.id], p.expected_uids) for p in class_probes]
            class_mrr[probe_class] = mean(scores) if scores else 0.0

        rows.append(
            {
                "floor": floor,
                "abstention_clean": clean,
                "abstention_total": len(abstention_probes),
                "class_mrr": class_mrr,
            }
        )
    return rows


def _format_floor(floor: float | None) -> str:
    return "inactive" if floor is None else f"{floor:g}"


def render_report(
    scale: str, class_order: list[str], by_backend: dict[str, list[dict[str, object]]]
) -> str:
    lines: list[str] = []
    lines.append("# Recall relevance floor -- abstention/recall sweep")
    lines.append("")
    lines.append(
        f"Generated by `scripts/sweep_recall_relevance_floor.py` against the "
        f"`{scale}` synthetic corpus (`tests/evals/data/corpus/`, issue "
        f"athenaeum#1492). Committed so the follow-up tuning decision this "
        f"issue defers has a starting artifact to read -- **no threshold is "
        f"chosen here.**"
    )
    lines.append("")
    lines.append(
        "`abstention CLEAN` is how many of the 3 `abstention`-class probes "
        "correctly return nothing at that floor (3/3 is the goal athenaeum#1492 "
        "reports as failing today, at the `inactive` row). The remaining "
        "columns are the mean reciprocal rank for every OTHER probe class at "
        "the SAME floor -- the genuine-recall cost of raising it. A floor "
        "that reaches 3/3 CLEAN while collapsing every MRR column to 0.000 "
        "has bought abstention by deleting recall entirely, which is exactly "
        "the trade-off this artifact exists to make visible before a human "
        "picks a number."
    )
    lines.append("")
    for backend_name, rows in by_backend.items():
        lines.append(f"## {backend_name}")
        lines.append("")
        header = ["floor", "abstention CLEAN"] + list(class_order)
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for row in rows:
            class_mrr = row["class_mrr"]
            assert isinstance(class_mrr, dict)
            cells = [
                _format_floor(row["floor"]),  # type: ignore[arg-type]
                f"{row['abstention_clean']}/{row['abstention_total']}",
            ] + [f"{class_mrr[c]:.3f}" for c in class_order]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale", default="small", help="tests.evals.corpus.SCALES key (default: small)"
    )
    parser.add_argument("--out", type=Path, default=None, help="write the markdown report here")
    args = parser.parse_args(argv)

    corpus = build_corpus(scale=args.scale)
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="athenaeum-1492-sweep-"))
    wiki_root = corpus.materialize(root)
    cache_dir = root / "cache"
    get_backend("fts5").build_index(wiki_root, cache_dir)

    probes = corpus.probes
    class_order = sorted({p.probe_class for p in probes if p.probe_class != "abstention"})

    by_backend: dict[str, list[dict[str, object]]] = {}
    for backend_name, grid in (("fts5", FTS5_GRID), ("keyword", KEYWORD_GRID)):
        raw = _query_all(backend_name, wiki_root, cache_dir, probes)
        by_backend[backend_name] = _sweep_backend(backend_name, grid, raw, probes)

    report = render_report(args.scale, class_order, by_backend)
    print(report)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
