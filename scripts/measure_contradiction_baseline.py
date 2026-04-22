#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Baseline metric harness for the C4 contradiction detector (#198).

Runs the auto-memory discovery → cluster → merge → contradiction pipeline
against a live ``~/knowledge/`` tree (or a user-specified knowledge root)
and prints aggregate + per-cluster metrics. Nothing is committed and
nothing is written back to the wiki — this is a read-only dry-run style
harness operators execute locally.

Usage:

    ANTHROPIC_API_KEY=sk-... \\
        python scripts/measure_contradiction_baseline.py \\
        --knowledge-root ~/knowledge

    # Without an API key: the detector falls back to detected=False for
    # every cluster. Useful to measure the cluster-count side alone.
    python scripts/measure_contradiction_baseline.py

Expected output shape (do NOT commit a real run log):

    clusters:         N
    flagged:          M   (of which F factual, P prescriptive)
    members total:    K
    llm-unavailable:  U   (clusters where detector was skipped)

    sample (first 10 clusters):
      cluster_id=...  members=2  flagged=true  conflict_type=prescriptive
          rationale=...
          member 0: scope/filename.md
          member 1: scope/filename.md
      ...

Arguments:

    --knowledge-root PATH   Default: ~/knowledge
    --sample-size N         Default: 10. How many clusters to print in the
                            sample for manual false-positive review.
    --dry-run               Force skipping wiki writes (default True — this
                            script is always non-destructive).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

# Ensure the in-repo source is on sys.path when invoked without install.
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from athenaeum.clusters import (  # noqa: E402
    cluster_auto_memory_files,
    resolve_cluster_threshold,
)
from athenaeum.config import load_config, resolve_extra_intake_roots  # noqa: E402
from athenaeum.contradictions import detect_contradictions  # noqa: E402
from athenaeum.librarian import discover_auto_memory_files  # noqa: E402
from athenaeum.merge import (  # noqa: E402
    _collect_am_by_path,
    merge_cluster_row,
    read_cluster_rows,
    resolve_cluster_output_path,
)

log = logging.getLogger("measure_contradiction_baseline")


def _build_client():
    """Return a live Anthropic client or ``None`` when no key is set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-root", type=Path,
        default=Path.home() / "knowledge",
        help="Path to a knowledge root (default: ~/knowledge)",
    )
    parser.add_argument(
        "--sample-size", type=int, default=10,
        help="How many clusters to print verbatim for manual review",
    )
    parser.add_argument(
        "--cluster-threshold", type=float, default=None,
        help=(
            "Override cluster cosine threshold (default: resolved from config)"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    knowledge_root: Path = args.knowledge_root.expanduser()
    if not knowledge_root.is_dir():
        print(f"ERROR: knowledge root not found: {knowledge_root}", file=sys.stderr)
        return 1

    config = load_config(knowledge_root)
    extra_roots = resolve_extra_intake_roots(knowledge_root, config=config)

    # Re-cluster live rather than relying on a possibly-stale canonical JSONL
    # so the baseline reflects the detector behavior on the current corpus.
    auto_memory_files = discover_auto_memory_files(knowledge_root, config=config)
    if not auto_memory_files:
        print("No auto-memory files discovered; nothing to measure.")
        return 0

    threshold = (
        args.cluster_threshold
        if args.cluster_threshold is not None
        else resolve_cluster_threshold(knowledge_root, config=config)
    )
    cache_dir = Path(
        os.environ.get("ATHENAEUM_CACHE_DIR")
        or (Path.home() / ".cache" / "athenaeum")
    )

    clusters = cluster_auto_memory_files(
        auto_memory_files,
        extra_roots=extra_roots,
        cache_dir=cache_dir,
        threshold=threshold,
    )

    # Fold the in-memory clusters into the same "cluster JSONL row" shape
    # merge_cluster_row() consumes so the code path matches the real
    # librarian pipeline.
    rows = [
        {
            "cluster_id": c.cluster_id,
            "member_paths": list(c.member_paths),
            "centroid_score": c.centroid_score,
            "rationale": "",
        }
        for c in clusters
    ]
    # Also read any canonical JSONL rows so future users who want to
    # measure against a frozen run can still do so (not used here but
    # kept for parity with merge_clusters_to_wiki).
    _ = read_cluster_rows(
        resolve_cluster_output_path(knowledge_root, config=config)
    )

    am_by_path = _collect_am_by_path(auto_memory_files)

    entries = []
    for row in rows:
        entry = merge_cluster_row(
            row, extra_roots=extra_roots, am_by_path=am_by_path,
        )
        if entry is not None:
            entries.append(entry)

    client = _build_client()

    type_counter: Counter[str] = Counter()
    unavailable = 0
    flagged_samples: list[tuple[str, object]] = []
    for entry in entries:
        result = detect_contradictions(entry.resolved_members, client)
        entry.contradiction = result
        entry.contradictions_detected = bool(result.detected)
        if result.detected:
            if result.conflict_type:
                type_counter[result.conflict_type] += 1
            flagged_samples.append((entry.cluster_id, result))
        if result.rationale == "llm-unavailable":
            unavailable += 1

    total = len(entries)
    flagged = sum(1 for e in entries if e.contradictions_detected)
    members_total = sum(len(e.resolved_members) for e in entries)

    print(f"clusters:         {total}")
    print(
        f"flagged:          {flagged}"
        + (
            "   (of which "
            + ", ".join(f"{v} {k}" for k, v in type_counter.items())
            + ")"
            if type_counter
            else ""
        )
    )
    print(f"members total:    {members_total}")
    print(f"llm-unavailable:  {unavailable}")
    print()
    print(f"sample (first {args.sample_size} clusters):")
    shown = 0
    for entry in entries:
        if shown >= args.sample_size:
            break
        shown += 1
        r = entry.contradiction
        print(
            f"  cluster_id={entry.cluster_id}  "
            f"members={len(entry.resolved_members)}  "
            f"flagged={entry.contradictions_detected}"
            + (
                f"  conflict_type={r.conflict_type}"
                if r is not None and r.conflict_type
                else ""
            )
        )
        if r is not None and r.rationale:
            print(f"      rationale={r.rationale[:160]}")
        for i, am in enumerate(entry.resolved_members[:3]):
            print(f"      member {i}: {am.origin_scope}/{am.path.name}")
        if len(entry.resolved_members) > 3:
            print(f"      ... and {len(entry.resolved_members) - 3} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
