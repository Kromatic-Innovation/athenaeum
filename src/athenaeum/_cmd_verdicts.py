# SPDX-License-Identifier: Apache-2.0
"""``athenaeum verdicts {count,list-by-verdict,show-one-pair,show-stale}``.

The sanctioned CLI read path for the verdict ledger (:mod:`athenaeum.verdicts`,
issue athenaeum#712) — mirroring the repo's ``athenaeum merges`` precedent
(:mod:`athenaeum._cmd_merges`). Hand-parsing ``wiki/_verdicts/*.jsonl`` is
not a supported access pattern; this (or the library functions it calls) is.

Four modes, matching the issue's AC ("A CLI surface exists to inspect the
ledger: at minimum count, list-by-verdict, show-one-pair, and show-stale"):

- ``count``          total live (non-superseded) verdict count.
- ``list-by-verdict`` all live verdicts, optionally filtered to one
                      ``--verdict`` value.
- ``show-one-pair``  the current live verdict for ``--pair <idA>+<idB>``
                      (order-independent — either id order resolves the
                      same pair).
- ``show-stale``     every live verdict currently flagged stale.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def _format_entry(entry: dict) -> str:
    basis = entry.get("basis") or {}
    lines = [
        f"## {entry.get('pair')} — {entry.get('verdict')}"
        f"{' [STALE]' if entry.get('stale') else ''}",
        f"  at: {entry.get('at')}  decided_by: {entry.get('decided_by')}",
        f"  comparator_version: {basis.get('comparator_version')}",
    ]
    if entry.get("stale"):
        lines.append(f"  stale_reason: {entry.get('stale_reason')}")
    return "\n".join(lines)


def cmd_verdicts(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum verdicts {count,list-by-verdict,show-one-pair,show-stale}``."""
    from athenaeum.verdicts import ledger_count, list_by_verdict, show_one_pair, show_stale

    sub = getattr(args, "verdicts_target", None)
    if sub not in ("count", "list-by-verdict", "show-one-pair", "show-stale"):
        print(
            "usage: athenaeum verdicts "
            "{count,list-by-verdict,show-one-pair,show-stale} [...]",
            file=sys.stderr,
        )
        return 2

    wiki_root = _resolve_wiki_root(args)

    if sub == "count":
        count = ledger_count(wiki_root)
        if args.json:
            sys.stdout.write(json.dumps({"count": count}) + "\n")
        else:
            print(f"{count} live verdict(s)")
        return 0

    if sub == "list-by-verdict":
        verdict = getattr(args, "verdict", None)
        entries = list_by_verdict(wiki_root, verdict=verdict)
        if args.json:
            sys.stdout.write(json.dumps(entries) + "\n")
            return 0
        if not entries:
            print("0 live verdicts")
            return 0
        for idx, entry in enumerate(entries):
            if idx > 0:
                print()
            print(_format_entry(entry))
        return 0

    if sub == "show-one-pair":
        pair = args.pair
        entry = show_one_pair(wiki_root, pair)
        if args.json:
            sys.stdout.write((json.dumps(entry) if entry else "null") + "\n")
            return 0
        if entry is None:
            print(f"no decided verdict for pair {pair!r}")
            return 0
        print(_format_entry(entry))
        return 0

    # sub == "show-stale"
    entries = show_stale(wiki_root)
    if args.json:
        sys.stdout.write(json.dumps(entries) + "\n")
        return 0
    if not entries:
        print("0 stale verdicts")
        return 0
    for idx, entry in enumerate(entries):
        if idx > 0:
            print()
        print(_format_entry(entry))
    return 0


def add_verdicts_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum verdicts`` and its four modes on ``subparsers``."""
    v_parser = subparsers.add_parser(
        "verdicts",
        help=(
            "Inspect the verdict ledger (`wiki/_verdicts/`, issue athenaeum#712) — "
            "pairwise comparison verdicts with their justification basis. "
            "Four modes: count, list-by-verdict, show-one-pair, show-stale."
        ),
    )
    v_parser.set_defaults(func=cmd_verdicts)
    v_sub = v_parser.add_subparsers(dest="verdicts_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=DEFAULT_KNOWLEDGE_ROOT,
            help="Knowledge directory (default: ~/knowledge)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    count_p = v_sub.add_parser("count", help="Print the live verdict count.")
    _add_common(count_p)

    list_p = v_sub.add_parser(
        "list-by-verdict", help="List all live verdicts, optionally filtered by --verdict."
    )
    _add_common(list_p)
    list_p.add_argument(
        "--verdict",
        default=None,
        choices=("duplicate", "contradiction", "specialization", "distinct", "underdetermined"),
        help="Filter to only this verdict value.",
    )

    show_p = v_sub.add_parser(
        "show-one-pair", help="Show the current live verdict for one pair."
    )
    _add_common(show_p)
    show_p.add_argument(
        "--pair",
        required=True,
        help="Pair key, e.g. 'id-a+id-b' (order-independent).",
    )

    stale_p = v_sub.add_parser(
        "show-stale", help="List every live verdict currently flagged stale."
    )
    _add_common(stale_p)
