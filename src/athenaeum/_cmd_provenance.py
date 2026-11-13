# SPDX-License-Identifier: Apache-2.0
"""``athenaeum provenance adapter`` — read command for the adapter-provenance
ledger (issue athenaeum#2468).

Operator disposition, athenaeum#2468 (cleanup audit item B8, parent epic
athenaeum#2447): of the five observability ledgers that audit reviewed,
adapter provenance is the one kept and, by this module, exposed; the other
four (compile routes, LLM schema observations, the wiki-dedupe attribution
report, never-ingest refusals) had their writers and readers removed
entirely — see ``CHANGELOG.md`` and ``docs/guides/upgrading.md``.

This module is a thin CLI wrapper over
:func:`athenaeum.adapter_provenance.resolve_source_for_page` — the join a
Lane A adapter's own frontmatter leaves behind once Tier 3 compiles a raw
record onto a wiki page (see that module's docstring, and
``docs/design/provenance-shape.md`` §13, for the full mechanism). This
command adds no new read logic of its own.

Read-only: takes no run lock, writes nothing. A page with no recorded
provenance is not an error — most pages have none, either because they were
compiled before this mechanism existed or because their source has no
declared id-key convention (:data:`athenaeum.adapter_provenance.SOURCE_OBJECT_ID_KEYS`)
— so the command exits 0 either way and says so plainly.

Exit codes (see ``docs/reference/exit-codes.md``):

    0  -- success, whether or not any provenance was found for the uid.
    2  -- usage (no subcommand given).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_provenance(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum provenance {adapter}``."""
    target = getattr(args, "provenance_target", None)
    if target == "adapter":
        return _cmd_adapter(args)
    print("usage: athenaeum provenance adapter --uid <uid>", file=sys.stderr)
    return 2


def _cmd_adapter(args: argparse.Namespace) -> int:
    from athenaeum.adapter_provenance import resolve_source_for_page
    from athenaeum.config import resolve_cache_dir

    cache_dir = resolve_cache_dir(args.cache_dir)
    rows = resolve_source_for_page(args.uid, cache_dir=cache_dir)

    if args.json:
        sys.stdout.write(json.dumps({"uid": args.uid, "records": rows}) + "\n")
        return 0

    if not rows:
        print(f"No adapter provenance recorded for page uid={args.uid!r}.")
        return 0

    for row in rows:
        print(f"page_uid:         {row.get('page_uid', '')}")
        print(f"  source:           {row.get('source', '')}")
        print(f"  source_object_id: {row.get('source_object_id', '')}")
        print(f"  raw_ref:          {row.get('raw_ref', '')}")
        print(f"  recorded_at:      {row.get('recorded_at', '')}")
        fields = row.get("fields") or {}
        if fields:
            print("  fields:")
            for key in sorted(fields):
                print(f"    {key}: {fields[key]}")
    return 0


def add_provenance_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum provenance`` and its ``adapter`` mode."""
    parser = subparsers.add_parser(
        "provenance",
        help=(
            "Read-only provenance-ledger lookups. `adapter --uid <uid>` "
            "prints which adapter/source produced a compiled page, from the "
            "adapter-provenance ledger (issue athenaeum#2468)."
        ),
    )
    parser.set_defaults(func=cmd_provenance)
    sub = parser.add_subparsers(dest="provenance_target")

    adapter_p = sub.add_parser(
        "adapter",
        help=(
            "Print the adapter-provenance ledger row(s) recorded for one "
            "compiled page uid -- source, source_object_id, raw_ref, and "
            "the captured adapter fields -- or say none were recorded."
        ),
    )
    adapter_p.add_argument(
        "--uid",
        required=True,
        help="The compiled page's durable uid.",
    )
    adapter_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory the ledger lives under (default: ~/.cache/athenaeum).",
    )
    adapter_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
