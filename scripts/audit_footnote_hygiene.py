#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Audit footnote and list-item hygiene across a wiki tree. READ ONLY.

Issue athenaeum#1942 AC3. The patch-mode merge applier used to splice each
night's new clause into whichever list item the model had anchored on, and
never allocated footnote labels against the page it was writing to, so an
aggregate page receiving many merges drifted into run-on list items carrying
many citations each, with labels reused across unrelated subjects. The applier
is fixed (``athenaeum.tiers.apply_merge_ops``); this script measures how far
the damage already spread, so the blast radius is a number rather than a
guess.

Two counts per page, both named by that acceptance criterion:

* ``multi_cited_items`` — list items carrying MORE THAN ONE footnote
  reference. One item, many citations is the run-on signature: each merge
  concatenated its clause onto the previous one instead of adding a sibling.
* ``reused_labels`` — labels with MORE THAN ONE definition on the page. Every
  reference to such a label is ambiguous, so provenance for the facts citing
  it cannot be resolved.

Two further counts come free from the same scan and say whether a page's
footnotes resolve at all: ``dangling_refs`` (referenced, never defined) and
``orphan_defs`` (defined, never referenced).

**This script never writes to the corpus and never mutates a page.** It opens
each file read-only and reports integers.

**Output carries no page content, titles, or filenames by default** — only
corpus totals and distributions, which is the shape this measurement is
recorded in. ``--per-page`` additionally emits one row per affected page keyed
by its frontmatter ``uid`` (an opaque handle, not a name) for an operator
repairing specific pages locally; ``--json`` emits the same data as JSON.

Usage::

    python scripts/audit_footnote_hygiene.py --wiki ~/knowledge/wiki
    python scripts/audit_footnote_hygiene.py --wiki ~/knowledge/wiki --json
    python scripts/audit_footnote_hygiene.py --wiki ~/knowledge/wiki --per-page

Exit status is 0 whenever the scan completed, whatever it found: this is a
measurement, not a gate. 2 means the tree could not be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: An inline footnote REFERENCE (``[^2]``), never a definition (``[^2]:``).
#: The negative lookahead is the only thing that tells the two apart.
REFERENCE_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")

#: A footnote DEFINITION line (``[^2]: ...``).
DEFINITION_RE = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)

#: A markdown list-item line.
LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")


def iter_list_items(body: str) -> list[str]:
    """Every list item in *body*, each folded into a single string.

    An item runs from its marker line through any following indented
    continuation lines, so a clause spliced into the middle of an item — or
    wrapped onto the next line — is counted as part of that item rather than
    as a line of its own. Getting this wrong would under-count exactly the
    shape being measured.
    """
    items: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if LIST_ITEM_RE.match(line):
            if current is not None:
                items.append("\n".join(current))
            current = [line]
            continue
        if current is not None and line.strip() and line[:1].isspace():
            current.append(line)
            continue
        if current is not None:
            items.append("\n".join(current))
            current = None
    if current is not None:
        items.append("\n".join(current))
    return items


def audit_body(body: str) -> dict[str, int]:
    """The four hygiene counts for one page body."""
    items = iter_list_items(body)
    references = REFERENCE_RE.findall(body)
    definitions = DEFINITION_RE.findall(body)
    return {
        "list_items": len(items),
        "multi_cited_items": sum(
            1 for item in items if len(REFERENCE_RE.findall(item)) > 1
        ),
        "max_refs_in_one_item": max(
            (len(REFERENCE_RE.findall(item)) for item in items), default=0
        ),
        "reused_labels": sum(
            1 for label in set(definitions) if definitions.count(label) > 1
        ),
        "dangling_refs": len(set(references) - set(definitions)),
        "orphan_defs": len(set(definitions) - set(references)),
    }


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)``; frontmatter is ``""`` when absent.

    Footnote definitions are a BODY construct, and a page's frontmatter can
    legitimately carry bracketed YAML, so the two are separated before any
    counting rather than scanning the whole file.
    """
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    return text[3:end], text[end + 4 :]


_UID_RE = re.compile(r"^uid:\s*[\"']?([A-Za-z0-9_-]+)[\"']?\s*$", re.MULTILINE)


def _uid(frontmatter: str) -> str | None:
    match = _UID_RE.search(frontmatter)
    return match.group(1) if match else None


def audit_tree(wiki: Path) -> dict[str, Any]:
    """Scan every ``*.md`` under *wiki* read-only and aggregate the counts."""
    pages: list[dict[str, Any]] = []
    unreadable = 0
    for path in sorted(wiki.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            unreadable += 1
            continue
        frontmatter, body = _split_frontmatter(text)
        counts = audit_body(body)
        counts["uid"] = _uid(frontmatter)
        pages.append(counts)

    def total(key: str) -> int:
        return sum(int(page[key]) for page in pages)

    affected = [
        page
        for page in pages
        if page["multi_cited_items"] or page["reused_labels"]
    ]
    worst = max((int(p["max_refs_in_one_item"]) for p in pages), default=0)
    return {
        "pages_scanned": len(pages),
        "pages_unreadable": unreadable,
        "list_items": total("list_items"),
        "multi_cited_items": total("multi_cited_items"),
        "reused_labels": total("reused_labels"),
        "dangling_refs": total("dangling_refs"),
        "orphan_defs": total("orphan_defs"),
        "pages_with_multi_cited_items": sum(
            1 for page in pages if page["multi_cited_items"]
        ),
        "pages_with_reused_labels": sum(1 for page in pages if page["reused_labels"]),
        "max_refs_in_one_item": worst,
        "per_page": sorted(
            affected,
            key=lambda page: (
                -int(page["max_refs_in_one_item"]),
                -int(page["reused_labels"]),
                str(page["uid"] or ""),
            ),
        ),
    }


def render(report: dict[str, Any], *, per_page: bool) -> str:
    lines = [
        "Footnote / list-item hygiene audit (athenaeum#1942) -- read only",
        "",
        f"  pages scanned:                 {report['pages_scanned']}",
        f"  pages unreadable:              {report['pages_unreadable']}",
        f"  list items:                    {report['list_items']}",
        "",
        f"  list items with >1 citation:   {report['multi_cited_items']}"
        f"  (on {report['pages_with_multi_cited_items']} page(s))",
        f"  reused footnote labels:        {report['reused_labels']}"
        f"  (on {report['pages_with_reused_labels']} page(s))",
        f"  worst item (citations in one): {report['max_refs_in_one_item']}",
        "",
        f"  dangling references:           {report['dangling_refs']}",
        f"  orphan definitions:            {report['orphan_defs']}",
    ]
    if per_page and report["per_page"]:
        lines += [
            "",
            "  uid        items  >1-cited  worst  reused  dangling  orphan",
        ]
        for page in report["per_page"]:
            lines.append(
                "  {:<10} {:>5} {:>9} {:>6} {:>7} {:>9} {:>7}".format(
                    page["uid"] or "-",
                    page["list_items"],
                    page["multi_cited_items"],
                    page["max_refs_in_one_item"],
                    page["reused_labels"],
                    page["dangling_refs"],
                    page["orphan_defs"],
                )
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Count run-on list items and reused footnote labels across a wiki "
            "tree. Read only: nothing is written and no page is modified."
        )
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        required=True,
        help="Wiki directory to scan (scanned recursively, read-only).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a text summary.",
    )
    parser.add_argument(
        "--per-page",
        action="store_true",
        help=(
            "Also list affected pages, keyed by frontmatter uid. Omitted by "
            "default so the summary carries no per-page handles at all."
        ),
    )
    args = parser.parse_args(argv)

    wiki = args.wiki.expanduser()
    if not wiki.is_dir():
        print(f"not a directory: {wiki}", file=sys.stderr)
        return 2

    report = audit_tree(wiki)
    if not args.per_page:
        report = {key: value for key, value in report.items() if key != "per_page"}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render({**report, "per_page": report.get("per_page", [])}, per_page=args.per_page))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
