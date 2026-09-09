#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure the SHAPE of a local knowledge tree, so synthetic fixtures match it.

Emits distribution parameters only -- page-length histogram, links per page,
entity-type frequencies, supersession rate. **No page content, no names, no
titles, no excerpts.**

The output is DELIBERATELY NOT COMMITTED (``.gitignore`` excludes it). A
frequency table over a personal corpus is not reliably content-free, and
committing it would place the derived artifact inside the very directory the
eval-corpus leakage lint scans -- a safety artifact living in the namespace of
the thing it certifies, which this workspace has been bitten by before.

Read the report locally while hand-authoring ``tests/evals/data/corpus/core``;
do not add it to git.

Usage::

    python scripts/measure_corpus_shape.py [--wiki ~/knowledge/wiki] \\
        --out tests/evals/data/corpus/corpus-shape.local.md
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from pathlib import Path

import yaml


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        parsed = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def measure(wiki: Path) -> dict[str, object]:
    lengths: list[int] = []
    link_counts: list[int] = []
    types: Counter[str] = Counter()
    claim_kinds: Counter[str] = Counter()
    superseded = 0
    described = 0
    total = 0

    for path in sorted(wiki.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        front = _frontmatter(text)
        if front is None:
            continue
        total += 1
        body = text[text.find("\n---", 3) + 4 :]
        lengths.append(len(body))
        link_counts.append(body.count("[["))
        types[str(front.get("type", "unknown"))] += 1
        if front.get("claim_kind"):
            claim_kinds[str(front["claim_kind"])] += 1
        if front.get("description"):
            described += 1
        lowered = body.lower()
        if "supersede" in lowered or "superseded" in lowered:
            superseded += 1

    def _pct(n: int) -> float:
        return round(100.0 * n / total, 2) if total else 0.0

    return {
        "pages": total,
        "body_chars_mean": round(statistics.mean(lengths), 1) if lengths else 0,
        "body_chars_median": round(statistics.median(lengths), 1) if lengths else 0,
        "body_chars_p90": (round(sorted(lengths)[int(len(lengths) * 0.9)], 1) if lengths else 0),
        "links_per_page_mean": (round(statistics.mean(link_counts), 2) if link_counts else 0),
        "pages_with_no_links_pct": _pct(sum(1 for c in link_counts if c == 0)),
        "described_pct": _pct(described),
        "supersession_pct": _pct(superseded),
        "type_frequencies": dict(types.most_common(25)),
        "claim_kind_frequencies": dict(claim_kinds.most_common()),
    }


def render(stats: dict[str, object], wiki: Path) -> str:
    lines = [
        "# Corpus shape (LOCAL ONLY -- DO NOT COMMIT)",
        "",
        "Distribution parameters measured from a local knowledge tree, for use",
        "while hand-authoring the synthetic eval corpus. Numbers only: this file",
        "carries no page content, names, or titles -- and is gitignored anyway,",
        "because a frequency table over a personal corpus is not reliably",
        "content-free.",
        "",
        # Deliberately NOT the absolute path. The default --out lands inside
        # tests/evals/data/corpus/, which the eval-corpus path lint scans, and
        # writing a home directory into a file in that directory would put the
        # exact string the lint exists to catch inside the tree it guards.
        f"Source tree: {len(list(wiki.rglob('*.md')))} pages (path omitted by",
        "design -- see the note in this script).",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key, value in stats.items():
        if isinstance(value, dict):
            continue
        lines.append(f"| {key} | {value} |")
    for key in ("type_frequencies", "claim_kind_frequencies"):
        table = stats.get(key) or {}
        if not isinstance(table, dict) or not table:
            continue
        lines += ["", f"## {key}", "", "| value | count |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki",
        type=Path,
        default=Path.home() / "knowledge" / "wiki",
        help="knowledge tree to measure (default: ~/knowledge/wiki)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/evals/data/corpus/corpus-shape.local.md"),
        help="where to write the report (gitignored by convention)",
    )
    args = parser.parse_args()

    if not args.wiki.is_dir():
        print(f"no knowledge tree at {args.wiki}")
        return 1

    stats = measure(args.wiki)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(stats, args.wiki), encoding="utf-8")
    print(f"wrote {args.out} ({stats['pages']} pages measured)")
    print("REMINDER: this file is local-only. Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
