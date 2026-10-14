#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build host-side fixtures for the Maya collision-triage eval (issue athenaeum#2066).

Reads resolved name-collision proposals through the SANCTIONED ledger
module (:func:`athenaeum.pending_merges.parse_pending_merges` -- never a
hand-rolled parse of ``_pending_merges.md``), filters to collisions a human
already resolved, and writes ``{text_a, text_b, label}`` rows to a
host-side-only JSON-Lines file.

**Never committed, never printed.** This script never prints a fixture's
``text_a``/``text_b`` -- only aggregate class counts (issue athenaeum#2066
acceptance criteria). The output file is written with mode 600 inside a
0700 directory; nothing it writes is intended to leave this machine.

**Population.** :mod:`athenaeum.name_collisions` writes one proposal block
per collision via ``write_pending_merge``, with a fixed rationale prefix
(`"athenaeum#1170 nightly name-collision scan"`) that is the ONLY thing
distinguishing these blocks from wiki-dedupe or C3 merge proposals in the
same ``_pending_merges.md`` sidecar. This script reads BOTH the live
sidecar and its archive (``_pending_merges_archive.md`` --
:func:`athenaeum.pending_merges.ingest_resolved_merges` moves resolved
blocks there), since a resolved collision may live in either file
depending on whether the nightly archive-and-compact step has run since.

**Label source, and why frontmatter is never fed to the model.** A
resolved block's ``decision`` field is the label directly: ``"approve"``
means a human ruled the two (or more) colliding pages the SAME person
(label ``True``); ``"reject"`` means a human ruled them DIFFERENT people
(label ``False``). The alternative -- reading each page's post-resolution
frontmatter and inferring the label from it -- would leak the answer INTO
the model's input: an approved fold tombstones the source pages
(``status: folded``, ``folded_into: <slug>``) and adds the folded slugs to
the canonical page's ``aliases:``; a reject adds a ``merge_rejected_with:``
declaration to the first source. Feeding any of those keys to Maya would
let it read the ledger's own answer back to itself. This script therefore
builds each record's text from ONLY ``name`` + a body excerpt -- obtained
via :func:`athenaeum.models.parse_frontmatter`, discarding the metadata
dict entirely rather than selectively filtering it, so no future
frontmatter key (resolution-time or otherwise) can leak in by omission.

**Only auto_applied=False, len(sources)==2 blocks become fixtures.**
- ``auto_applied=True`` blocks (issue athenaeum#602) are the T2
  auto-finalize path or this module's own unambiguous auto-merge --
  neither is a human judgment, so neither belongs in a human-decision
  fixture set.
- A collision group with more than 2 sources shares one ``decision`` across
  every source pair, but a reject does not mean EVERY pair in the group
  differs from the canonical -- only that the group as a whole was not
  folded. Restricting to exactly 2 sources avoids assigning a label to a
  pair the human never separately judged. The excluded count is reported.
- Two blocks can name the SAME unordered source pair with different
  ``merge_target_name`` values (seen in the live ledger: a collision
  proposed once per candidate canonical). These are deduplicated by
  ``frozenset(sources)`` -- the SAME human judgment recorded twice is one
  fixture row, not two.

Usage::

    python scripts/build_collision_triage_fixtures.py \\
        --wiki-root ~/knowledge/wiki \\
        --output ~/.local/state/occam-lanes/2066/collision_pairs.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import PendingMerge, parse_pending_merges

#: The rationale prefix written by athenaeum.name_collisions.resolve_name_collisions.
#: The ONLY marker distinguishing a name-collision proposal from any other
#: pending-merge block in the same sidecar.
_NAME_COLLISION_RATIONALE_PREFIX = "athenaeum#1170 nightly name-collision scan"

#: Body excerpt length per record, independent of (and smaller than) the
#: eval script's own per-record truncation -- a short excerpt is enough for
#: "is this the same person" and keeps the fixture file itself small.
_BODY_EXCERPT_CHARS = 1200

#: Frontmatter keys this builder will NEVER read into a record's text, even
#: though parse_frontmatter + a dict-get pattern COULD see them. Recorded
#: as an explicit test assertion (test_build_collision_triage_fixtures.py)
#: rather than trusted from this comment alone.
_NEVER_LEAK_KEYS = frozenset(
    {"status", "folded_into", "embedded", "aliases", "merge_rejected_with"}
)


@dataclass(frozen=True)
class FixtureRow:
    text_a: str
    text_b: str
    label: bool


def _page_text(path: Path) -> str | None:
    """Build a record's text from ``name`` + a body excerpt only.

    Returns ``None`` if the page cannot be read or has no ``name:`` --
    skipped, never guessed at (same tolerance
    :func:`athenaeum.name_collisions.scan_name_collisions` already applies
    to malformed pages).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    name = meta.get("name") if isinstance(meta, dict) else None
    if not name:
        return None
    excerpt = body.strip()[:_BODY_EXCERPT_CHARS]
    return f"{name}\n\n{excerpt}"


def _is_name_collision_block(pm: PendingMerge) -> bool:
    return pm.rationale.startswith(_NAME_COLLISION_RATIONALE_PREFIX)


@dataclass(frozen=True)
class BuildReport:
    total_blocks_read: int
    name_collision_blocks: int
    resolved: int
    auto_applied_excluded: int
    multi_source_excluded: int
    unreadable_page_excluded: int
    duplicate_pair_excluded: int
    rows_written: int
    n_same_person: int
    n_different_person: int


def build_fixtures(wiki_root: Path) -> tuple[list[FixtureRow], BuildReport]:
    all_blocks: list[PendingMerge] = []
    for name in ("_pending_merges.md", "_pending_merges_archive.md"):
        path = wiki_root / name
        if path.exists():
            all_blocks.extend(parse_pending_merges(path))

    nc_blocks = [pm for pm in all_blocks if _is_name_collision_block(pm)]
    resolved = [pm for pm in nc_blocks if pm.resolved]

    auto_applied_excluded = 0
    multi_source_excluded = 0
    unreadable_page_excluded = 0
    duplicate_pair_excluded = 0

    # Single pass, preserving ledger (created_at) order of first occurrence
    # per unordered source pair -- the eval script's "current queue order"
    # baseline depends on fixtures being written in that same order.
    seen: set[frozenset[str]] = set()
    rows: list[FixtureRow] = []
    for pm in resolved:
        if pm.auto_applied:
            auto_applied_excluded += 1
            continue
        if pm.decision not in ("approve", "reject"):
            continue
        if len(pm.sources) != 2:
            multi_source_excluded += 1
            continue
        key = frozenset(pm.sources)
        if key in seen:
            duplicate_pair_excluded += 1
            continue
        text_a = _page_text(Path(pm.sources[0]))
        text_b = _page_text(Path(pm.sources[1]))
        if text_a is None or text_b is None:
            unreadable_page_excluded += 1
            continue
        seen.add(key)
        rows.append(FixtureRow(text_a=text_a, text_b=text_b, label=pm.decision == "approve"))

    report = BuildReport(
        total_blocks_read=len(all_blocks),
        name_collision_blocks=len(nc_blocks),
        resolved=len(resolved),
        auto_applied_excluded=auto_applied_excluded,
        multi_source_excluded=multi_source_excluded,
        unreadable_page_excluded=unreadable_page_excluded,
        duplicate_pair_excluded=duplicate_pair_excluded,
        rows_written=len(rows),
        n_same_person=sum(1 for r in rows if r.label),
        n_different_person=sum(1 for r in rows if not r.label),
    )
    return rows, report


def write_fixtures(rows: list[FixtureRow], output_path: Path) -> None:
    """Write ``rows`` to *output_path* as mode-600 JSON-Lines, inside a
    0700 parent directory. Never prints a row's text."""
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_path.parent, 0o700)

    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    fd = os.open(str(output_path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(
                json.dumps({"text_a": row.text_a, "text_b": row.text_b, "label": row.label}) + "\n"
            )
    os.chmod(output_path, 0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki-root",
        required=True,
        type=Path,
        help="Wiki root containing _pending_merges.md / _pending_merges_archive.md.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Host-side-only output path for the JSON-Lines fixture file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wiki_root = args.wiki_root.expanduser()
    rows, report = build_fixtures(wiki_root)
    write_fixtures(rows, args.output.expanduser())

    print(f"total pending-merge blocks read: {report.total_blocks_read}")
    print(f"name-collision blocks: {report.name_collision_blocks}")
    print(f"resolved name-collision blocks: {report.resolved}")
    print(f"excluded (auto_applied, not a human judgment): {report.auto_applied_excluded}")
    print(f"excluded (more than 2 sources in the group): {report.multi_source_excluded}")
    print(f"excluded (source page unreadable/no name:): {report.unreadable_page_excluded}")
    print(f"excluded (duplicate unordered source pair): {report.duplicate_pair_excluded}")
    print(f"fixture rows written: {report.rows_written}")
    print(f"  same-person (label=True): {report.n_same_person}")
    print(f"  different-person (label=False): {report.n_different_person}")
    print(f"wrote {args.output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
