#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Re-score the qualified-name signal against a real corpus (issue athenaeum#1577 AC3).

Why this file exists at all
---------------------------

Issue athenaeum#1251 measured the (since-deleted) merge-worthiness gate on
28,951 live candidate pairs, and the measurement was excellent — but the
harness was an ad-hoc run against a deployed build and was never committed.
Issue athenaeum#1582's lane searched ``src/``, ``tests/``, ``scripts/``,
``docs/`` and ``git log --all --grep=1251`` and found no replay artifact, so
the next signal change had nothing to re-run and the number could not be
reproduced. AC3 was corrected on 2026-09-10 to make building this script part
of the work rather than a precondition inherited from athenaeum#1251.

The whole lesson is that an uncommitted harness cannot be reused. So: keep
this committed, and re-run it whenever the signal in
:mod:`athenaeum.name_structure` changes.

READ-ONLY, and structurally so
-------------------------------

This script opens files for reading and prints. It never calls
``write_pending_merge``, ``resolve_merge``, or the librarian; it never writes
under the corpus it is pointed at. Measuring a signal against the live corpus
must not mutate the live corpus — that is why the measurement lives here and
not behind a ``--dry-run`` flag on the write path, where a missing flag would
enact.

Usage
-----

    python scripts/rescore_name_structure.py --wiki-root ~/knowledge/wiki
    python scripts/rescore_name_structure.py --wiki-root <root> --json

``--sample N`` prints N hits (deterministically, evenly spread across the
sorted hit list rather than the first N, which would be alphabetical by slug
and therefore skewed) for the hand review AC3 asks for. Hand review is the
POINT of the sample, not a formality: the rule cannot distinguish an
expansion (``JTBD (Jobs to Be Done)`` — one entity, twice) from a scope
qualifier (``Fujitsu (2nd Contract)`` — one company, several engagements),
and the proportion between those two populations is what a reviewer of a
signal change needs to see.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from athenaeum.config import load_config  # noqa: E402
from athenaeum.name_structure import (  # noqa: E402
    NAME_STRUCTURE_CANDIDATE_TYPES,
    scan_qualified_name_splits,
    summarize,
)


def _evenly_spread(items: list, count: int) -> list:
    """*count* items spread across *items*, not the first *count*.

    The hit list is sorted by path, i.e. by slug, i.e. effectively by the
    random hex prefix the compiler mints. A head slice is not obviously
    biased, but it is not obviously UNbiased either, and a reviewer should
    not have to reason about that. An even stride is defensible without
    argument and is reproducible, which ``random.sample`` would not be.
    """
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    stride = len(items) / count
    return [items[int(i * stride)] for i in range(count)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--wiki-root",
        type=Path,
        required=True,
        help="Path to a compiled wiki/ directory. READ ONLY -- nothing is written.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Print N hits, evenly spread, for hand review (0 = none; -1 = all).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    wiki_root = args.wiki_root.expanduser().resolve()
    if not wiki_root.is_dir():
        parser.error(f"not a directory: {wiki_root}")

    total_pages = sum(1 for p in wiki_root.glob("*.md") if not p.name.startswith(("_", "auto-")))

    # Load the corpus's own config so the count matches what the librarian
    # phase would actually queue -- the storage-adapter merge-eligibility
    # policy (issue athenaeum#429) drops classes an operator routed to an
    # excluded surface, and a measurement taken WITHOUT it would over-report
    # against a corpus that has any. Read-only: load_config only reads.
    config = load_config(wiki_root.parent)
    splits = scan_qualified_name_splits(wiki_root, config=config)
    stats = summarize(splits)

    sample_n = len(splits) if args.sample == -1 else args.sample
    sample = _evenly_spread(splits, sample_n)
    sample_rows = [
        {
            "type": s.page_type,
            "qualified": s.qualified_name,
            "bare": s.bare_name,
            "qualifier": s.qualifier,
            "qualified_page": s.qualified_path.name,
            "bare_page": s.bare_path.name,
        }
        for s in sample
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "wiki_root": str(wiki_root),
                    "pages_scanned": total_pages,
                    "candidate_types": sorted(NAME_STRUCTURE_CANDIDATE_TYPES),
                    "proposals_gained": stats["total"],
                    "by_type": stats["by_type"],
                    "sample": sample_rows,
                },
                indent=2,
            )
        )
        return 0

    print(f"wiki root         : {wiki_root}")
    print(f"pages scanned     : {total_pages}")
    print(f"candidate types   : {', '.join(sorted(NAME_STRUCTURE_CANDIDATE_TYPES))}")
    print(f"proposals gained  : {stats['total']}")
    print(
        "  by type         : " + (", ".join(f"{k}={v}" for k, v in stats["by_type"].items()) or "-")
    )
    if sample_rows:
        print(f"\nhand-review sample ({len(sample_rows)} of {stats['total']}):")
        for row in sample_rows:
            print(f"  [{row['type']}] {row['qualified']}  <->  {row['bare']}")
            print(f"      {row['qualified_page']}  <->  {row['bare_page']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
