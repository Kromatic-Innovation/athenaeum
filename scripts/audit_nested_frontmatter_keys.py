#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Count pages whose frontmatter carries a NESTED key colliding with one of
the four fields ``_extract_frontmatter_fields`` reads. READ ONLY.

Issue athenaeum#1966: ``_extract_frontmatter_fields`` (``src/athenaeum/search.py``)
scanned frontmatter line by line, stripped leading whitespace, and THEN
tested ``line.startswith("name:")`` (and the same for ``tags:``/``aliases:``/
``description:``) -- so a NESTED key of the same name, for example a
``field_sources: {name: ...}`` mapping value or a ``name:`` inside a
``related:`` block-list entry, silently overwrote the page's own top-level
value in both index builders. The fix (same issue) adds a column-zero guard
so only a top-level key is ever read; this script counts how many pages in
a live corpus were affected by the bug BEFORE that fix, i.e. every page
whose frontmatter contains a nested ``name``/``tags``/``aliases``/
``description`` key at any depth below the top level.

Unlike the scanner this audits, this script parses frontmatter with the REAL
YAML loader (:func:`athenaeum.models.parse_frontmatter`) and walks the loaded
mapping recursively -- the audit wants ground truth about what is actually
nested in the YAML structure, not the hand-rolled scanner's deliberately
tolerant (and now patched) reading of it.

**This script never writes to the corpus and never mutates a page.** It opens
each file read-only and reports integers only -- no page content, titles, or
filenames, and no default embeds a path into this or any other personal
corpus (pass ``--wiki`` explicitly to point it at one).

Usage::

    python scripts/audit_nested_frontmatter_keys.py --wiki /path/to/wiki
    python scripts/audit_nested_frontmatter_keys.py --wiki /path/to/wiki --json

Exit status is 0 whenever the scan completed, whatever it found: this is a
measurement, not a gate. 2 means the tree could not be read. This is a
one-shot diagnostic for athenaeum#1966 -- deliberately NOT wired into
``athenaeum audit``, so the CLI surface and reference docs stay unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from athenaeum.models import parse_frontmatter  # noqa: E402

#: The four fields ``_extract_frontmatter_fields`` reads from the top level.
FIELDS: tuple[str, ...] = ("name", "tags", "aliases", "description")


def _iter_nested_field_hits(value: Any, *, top_level: bool) -> Iterator[str]:
    """Yield a field name each time it appears as a dict key BELOW the top
    level of *value* (the parsed frontmatter mapping).

    ``top_level=True`` only for the call on the page's own frontmatter dict
    itself -- a key there is the page's own legitimate value, never a
    collision. Every recursive call (into a nested dict's values or a list's
    items) passes ``top_level=False``, so a key matching one of ``FIELDS``
    at ANY depth below that is reported, covering both real-world shapes
    from the issue: an indented mapping value under a sibling top-level key
    (``field_sources: {name: ...}``) and a second key inside a block-list
    entry (``related: [{kind: x, name: ...}]``).
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            if not top_level and key in FIELDS:
                yield key
            yield from _iter_nested_field_hits(sub, top_level=False)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_field_hits(item, top_level=False)


def audit_page(meta: dict[str, Any]) -> dict[str, bool]:
    """Per-field nested-collision flags for one page's parsed frontmatter."""
    hits = set(_iter_nested_field_hits(meta, top_level=True))
    return {field: field in hits for field in FIELDS}


def audit_tree(wiki: Path) -> dict[str, Any]:
    """Scan every ``*.md`` under *wiki* read-only and aggregate the counts."""
    pages_scanned = 0
    pages_unreadable = 0
    pages_without_frontmatter = 0
    pages_affected = 0
    by_field = dict.fromkeys(FIELDS, 0)

    for path in sorted(wiki.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pages_unreadable += 1
            continue
        pages_scanned += 1
        meta, _body = parse_frontmatter(text)
        if not meta:
            pages_without_frontmatter += 1
            continue
        per_field = audit_page(meta)
        if any(per_field.values()):
            pages_affected += 1
        for field, hit in per_field.items():
            if hit:
                by_field[field] += 1

    return {
        "pages_scanned": pages_scanned,
        "pages_unreadable": pages_unreadable,
        "pages_without_frontmatter": pages_without_frontmatter,
        "pages_affected": pages_affected,
        "by_field": by_field,
    }


def render(report: dict[str, Any]) -> str:
    by_field = report["by_field"]
    lines = [
        "Nested frontmatter key collision audit (athenaeum#1966) -- read only",
        "",
        f"  pages scanned:                 {report['pages_scanned']}",
        f"  pages unreadable:              {report['pages_unreadable']}",
        f"  pages without frontmatter:     {report['pages_without_frontmatter']}",
        "",
        f"  pages affected (any field):    {report['pages_affected']}",
        "  by field:",
    ]
    for field in FIELDS:
        lines.append(f"    {field:<12}              {by_field[field]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Count pages whose frontmatter carries a nested key colliding "
            "with name/tags/aliases/description. Read only: nothing is "
            "written and no page is modified."
        )
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        default=Path.home() / "knowledge" / "wiki",
        help=(
            "Wiki directory to scan (scanned recursively, read-only). "
            "Defaults to the standard local knowledge root; pass an "
            "explicit path to scan anything else."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a text summary.",
    )
    args = parser.parse_args(argv)

    wiki = args.wiki.expanduser()
    if not wiki.is_dir():
        print(f"not a directory: {wiki}", file=sys.stderr)
        return 2

    report = audit_tree(wiki)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
