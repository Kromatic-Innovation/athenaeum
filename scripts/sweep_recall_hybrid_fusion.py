#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sweep the hybrid RRF fusion knobs on the REAL embedder (issue athenaeum#1800).

Follows the shape of ``scripts/sweep_recall_relevance_floor.py``: build each
corpus scale's indexes ONCE, query each backend's raw candidate list ONCE per
probe, then sweep the swept parameter over the CACHED raw results -- the
sweep touches nothing on disk and makes no further backend query, so the
96-combination grid costs one query pass, not 96.

Run as a plain script (``.venv/bin/python scripts/...``, never through
pytest): no test module is imported for its *tests*, only for its probe
data and pass/fail definitions (``tests.evals.test_recall_covers_grep``'s
``_PROBE_IDS`` / ``_PERSON_REPO_PROBE_IDS`` / ``_probe_by_id`` /
``_grep_hits``) -- reused so this script's notion of "coverage" and
"disambiguation" pass/fail is IDENTICAL to the committed tests', never a
second definition that could quietly drift from them. Running outside
pytest means ``tests/conftest.py``'s autouse offline-embedding stand-in
never loads at all (it is a pytest fixture) -- this script always sees the
REAL ``all-MiniLM-L6-v2`` model chromadb resolves by default, which is the
whole point of re-sweeping here rather than trusting the mismatched
in-pytest instrument the issue's Motivation names.

Knobs swept (the issue's own grid, 6 x 4 x 4 = 96 combinations):

* ``recall.hybrid.fts5_weight`` in {0.5, 0.75, 1.0, 1.5, 2.0, 3.0}
* ``recall.hybrid.guard_rank`` in {0, 1, 2, 3}
* ``recall.hybrid.k`` in {10, 20, 30, 60}

For each combination, against EVERY (scale, probe) pair in both vector
families (coverage: ``tests_recall_covers_grep_reachable_expected_pages``'s
own invariant: every grep-reachable expected page must be in the fused top-5;
disambiguation: ``person_not_repo``/``repo_not_person``'s ``must_not_rank``
page must NOT be in the fused top-5), this script recomputes
:func:`athenaeum.search.reciprocal_rank_fusion` -- the REAL production
function, never reimplemented -- over the cached raw candidate lists and
reports the real-model failure count and the failing (family, scale, probe)
set.

Eligibility (a) ("every vector case that passes on the real model at
baseline (1.0/0/60) still passes") is computed and printed per row; this
script does NOT apply the winner rule's tie-break or run check (b) (the
default-selection pytest run) -- both are inherently pytest-side / need a
config-default edit to check against, and are the calling lane's job per
the issue's Plan step 4, not this script's.

Usage::

    .venv/bin/python scripts/sweep_recall_hybrid_fusion.py \\
        --out docs/measurements/recall-hybrid-fusion-sweep.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from athenaeum.search import get_backend, reciprocal_rank_fusion  # noqa: E402
from tests.evals.corpus import Corpus, Probe, build_corpus  # noqa: E402
from tests.evals.test_recall_covers_grep import (  # noqa: E402
    _PERSON_REPO_PROBE_IDS,
    _PROBE_IDS,
    _grep_hits,
    _probe_by_id,
)

_SCALES: tuple[str, ...] = ("core", "medium")
_TOP_K = 5
#: Matches ``src.athenaeum.mcp_server._HYBRID_CANDIDATE_POOL`` -- the width
#: BOTH backends are re-queried at before fusion in the real hybrid dispatch.
_HYBRID_CANDIDATE_POOL = 15

_BASELINE_FTS5_WEIGHT = 1.0
_BASELINE_GUARD_RANK = 0
_BASELINE_K = 60

FTS5_WEIGHT_GRID: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
GUARD_RANK_GRID: tuple[int, ...] = (0, 1, 2, 3)
K_GRID: tuple[int, ...] = (10, 20, 30, 60)

#: One entry per (family, scale, probe_id) case this script scores.
_COVERAGE_FAMILY = "reachable_expected_pages"
_DISAMBIGUATION_FAMILY = "person_repo_disambiguation"


def _uid_of(filename: str) -> str:
    return filename[: -len(".md")] if filename.endswith(".md") else filename


@dataclass(frozen=True)
class _ProbeCandidates:
    probe: Probe
    grep_reachable_expected: frozenset[str]
    wide_vector_hits: list[tuple[str, str, float]]
    wide_fts5_hits: list[tuple[str, str, float]]


def _collect_candidates(
    scale: str, corpus: Corpus, wiki_root: Path, cache_dir: Path
) -> dict[str, _ProbeCandidates]:
    """One real query pass per probe id in ``_PROBE_IDS`` (a superset of
    ``_PERSON_REPO_PROBE_IDS`` -- both disambiguation probes are also
    coverage-invariant probes), at the SAME widened pool width and
    ``metadata_only`` FTS5 restriction the real hybrid dispatch uses
    (``athenaeum.mcp_server``'s vector block, ``_HYBRID_CANDIDATE_POOL``).
    """
    vector_backend = get_backend("vector")
    fts5_backend = get_backend("fts5")
    out: dict[str, _ProbeCandidates] = {}
    for probe_id in _PROBE_IDS:
        probe = _probe_by_id(corpus, probe_id)
        grep_reachable = frozenset(probe.expected_uids) & _grep_hits(corpus, probe.query).keys()
        wide_vector_hits = vector_backend.query(
            probe.query,
            cache_dir,
            n=max(_TOP_K, _HYBRID_CANDIDATE_POOL),
            wiki_root=wiki_root,
        )
        wide_fts5_hits = fts5_backend.query(
            probe.query,
            cache_dir,
            n=max(_TOP_K, _HYBRID_CANDIDATE_POOL),
            wiki_root=wiki_root,
            metadata_only=True,
        )
        out[probe_id] = _ProbeCandidates(
            probe=probe,
            grep_reachable_expected=grep_reachable,
            wide_vector_hits=wide_vector_hits,
            wide_fts5_hits=wide_fts5_hits,
        )
    return out


@dataclass(frozen=True)
class ComboResult:
    fts5_weight: float
    guard_rank: int
    k: int
    failing: frozenset[tuple[str, str, str]]  # (family, scale, probe_id)

    @property
    def is_baseline(self) -> bool:
        return (
            self.fts5_weight == _BASELINE_FTS5_WEIGHT
            and self.guard_rank == _BASELINE_GUARD_RANK
            and self.k == _BASELINE_K
        )

    @property
    def knobs_changed(self) -> int:
        return (
            (self.fts5_weight != _BASELINE_FTS5_WEIGHT)
            + (self.guard_rank != _BASELINE_GUARD_RANK)
            + (self.k != _BASELINE_K)
        )


def _score_combo(
    *,
    fts5_weight: float,
    guard_rank: int,
    k: int,
    by_scale: dict[str, dict[str, _ProbeCandidates]],
) -> ComboResult:
    failing: set[tuple[str, str, str]] = set()
    for scale, candidates_by_probe in by_scale.items():
        for probe_id, cand in candidates_by_probe.items():
            fused = reciprocal_rank_fusion(
                cand.wide_vector_hits,
                cand.wide_fts5_hits,
                n=_TOP_K,
                k=k,
                secondary_weight=fts5_weight,
                guard_rank=guard_rank,
            )
            ranked_uids = [_uid_of(filename) for filename, _name, _score in fused]
            missing = cand.grep_reachable_expected - set(ranked_uids)
            if missing:
                failing.add((_COVERAGE_FAMILY, scale, probe_id))
            if probe_id in _PERSON_REPO_PROBE_IDS:
                wrong_uid = cand.probe.must_not_rank[0]
                if wrong_uid in ranked_uids:
                    failing.add((_DISAMBIGUATION_FAMILY, scale, probe_id))
    return ComboResult(
        fts5_weight=fts5_weight, guard_rank=guard_rank, k=k, failing=frozenset(failing)
    )


#: Winner selected by the mechanical rule in issue athenaeum#1800's
#: acceptance criteria (fewest real-model failures among eligible
#: combinations, tie-broken fewest-knobs-changed / larger k / fts5_weight
#: closer to 1.0 / smaller guard_rank) -- the only knob that moves from
#: baseline is ``guard_rank`` (``0`` -> ``1``). Shipped in
#: ``src/athenaeum/config.py``'s ``RECALL_HYBRID_*_DEFAULT`` constants.
_WINNER_FTS5_WEIGHT = 1.0
_WINNER_GUARD_RANK = 1
_WINNER_K = 60


def _native_rank(hits: list[tuple[str, str, float]], uid: str) -> str:
    """1-indexed rank of *uid* within one backend's own widened candidate
    list (``_HYBRID_CANDIDATE_POOL`` wide), or ``"not in top 15"`` if
    absent -- this is each arm's OWN ranking, never the fused one."""
    for rank, (filename, _name, _score) in enumerate(hits, start=1):
        if _uid_of(filename) == uid:
            return str(rank)
    return "not in top 15"


def _render_remaining_failures(
    winner: ComboResult, by_scale: dict[str, dict[str, _ProbeCandidates]]
) -> str:
    lines: list[str] = []
    lines.append(
        f"## Remaining real-model failures under the shipped defaults "
        f"(fts5_weight={_WINNER_FTS5_WEIGHT:g}, guard_rank={_WINNER_GUARD_RANK}, "
        f"k={_WINNER_K})"
    )
    lines.append("")
    lines.append(
        "Each arm's OWN native rank (1-indexed within its own widened "
        "15-candidate list, before fusion) for the expected page (coverage "
        "cases) or the `must_not_rank` page (disambiguation cases) -- "
        "`not in top 15` means fusion cannot reach it no matter how the "
        "three knobs are set, since RRF only ever reorders candidates "
        "already present in at least one input list."
    )
    lines.append("")
    header = ["family", "scale", "probe", "page", "vector rank", "fts5 rank"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for family, scale, probe_id in sorted(winner.failing):
        cand = by_scale[scale][probe_id]
        if family == _COVERAGE_FAMILY:
            missing_pages = sorted(
                cand.grep_reachable_expected
                - {
                    _uid_of(f)
                    for f, _n, _s in reciprocal_rank_fusion(
                        cand.wide_vector_hits,
                        cand.wide_fts5_hits,
                        n=_TOP_K,
                        k=_WINNER_K,
                        secondary_weight=_WINNER_FTS5_WEIGHT,
                        guard_rank=_WINNER_GUARD_RANK,
                    )
                }
            )
            for page in missing_pages:
                vrank = _native_rank(cand.wide_vector_hits, page)
                frank = _native_rank(cand.wide_fts5_hits, page)
                lines.append(
                    f"| {family} | {scale} | {probe_id} | {page} | {vrank} | {frank} |"
                )
        else:
            page = cand.probe.must_not_rank[0]
            vrank = _native_rank(cand.wide_vector_hits, page)
            frank = _native_rank(cand.wide_fts5_hits, page)
            lines.append(f"| {family} | {scale} | {probe_id} | {page} | {vrank} | {frank} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def render_report(
    results: list[ComboResult], baseline: ComboResult, *, git_sha: str | None, wall_time_s: float
) -> str:
    lines: list[str] = []
    lines.append("# Recall hybrid RRF fusion -- real-embedder sweep")
    lines.append("")
    sha_note = f"git sha `{git_sha}`" if git_sha is not None else "git sha unavailable"
    lines.append(
        f"Generated by `scripts/sweep_recall_hybrid_fusion.py` against the "
        f"`core`+`medium` synthetic corpus (`tests/evals/data/corpus/`), "
        f"issue athenaeum#1800, at {sha_note}. Reproduce with "
        f"`.venv/bin/python scripts/sweep_recall_hybrid_fusion.py --out "
        f"docs/measurements/recall-hybrid-fusion-sweep.md`. Wall time for "
        f"this run: {wall_time_s:.1f}s."
    )
    lines.append("")
    lines.append(
        f"Baseline (`fts5_weight=1.0, guard_rank=0, k=60`, today's shipped "
        f"default): **{len(baseline.failing)} real-model failures**: "
        + ", ".join(f"{fam}/{scale}/{probe}" for fam, scale, probe in sorted(baseline.failing))
    )
    lines.append("")
    lines.append(
        "Eligibility (a): every case that PASSES at baseline on the real "
        "model must still pass. `eligible` is `yes` only when this "
        "combination's failing set is a SUBSET of baseline's failing set "
        "(introduces no new failure relative to baseline)."
    )
    lines.append("")
    header = [
        "fts5_weight",
        "guard_rank",
        "k",
        "real_failures",
        "eligible(a)",
        "knobs_changed",
        "failing_set",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for r in results:
        eligible = r.failing <= baseline.failing
        failing_str = (
            ", ".join(f"{fam}/{scale}/{probe}" for fam, scale, probe in sorted(r.failing))
            or "(none)"
        )
        cells = [
            f"{r.fts5_weight:g}",
            str(r.guard_rank),
            str(r.k),
            str(len(r.failing)),
            "yes" if eligible else "no",
            str(r.knobs_changed),
            failing_str,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write the markdown report here")
    args = parser.parse_args(argv)

    start = time.monotonic()

    by_scale: dict[str, dict[str, _ProbeCandidates]] = {}
    for scale in _SCALES:
        corpus = build_corpus(scale)
        root = Path(tempfile.mkdtemp(prefix=f"athenaeum-1800-sweep-{scale}-"))
        wiki_root = corpus.materialize(root)
        cache_dir = root / "cache"
        get_backend("fts5").build_index(wiki_root, cache_dir)
        get_backend("vector").build_index(wiki_root, cache_dir)
        by_scale[scale] = _collect_candidates(scale, corpus, wiki_root, cache_dir)

    baseline = _score_combo(
        fts5_weight=_BASELINE_FTS5_WEIGHT,
        guard_rank=_BASELINE_GUARD_RANK,
        k=_BASELINE_K,
        by_scale=by_scale,
    )

    results: list[ComboResult] = []
    for weight in FTS5_WEIGHT_GRID:
        for guard_rank in GUARD_RANK_GRID:
            for k in K_GRID:
                results.append(
                    _score_combo(
                        fts5_weight=weight, guard_rank=guard_rank, k=k, by_scale=by_scale
                    )
                )

    winner = _score_combo(
        fts5_weight=_WINNER_FTS5_WEIGHT,
        guard_rank=_WINNER_GUARD_RANK,
        k=_WINNER_K,
        by_scale=by_scale,
    )

    wall_time_s = time.monotonic() - start
    report = render_report(results, baseline, git_sha=_git_sha(), wall_time_s=wall_time_s)
    report += "\n" + _render_remaining_failures(winner, by_scale)
    print(report)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
