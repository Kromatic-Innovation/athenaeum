# SPDX-License-Identifier: Apache-2.0
"""``athenaeum calibration {summary,review}`` — tier-audit calibration CLI (issue #438).

The calibration loop for the tiered reasoning pass: a random audit share of
T1 rejects and T2 approvals is surfaced (as ``type: "audit"`` items in the
``decisions`` queue) for a human to confirm or overturn. This CLI is the
human's side of that loop:

- ``summary``  per-tier counts of ``sampled`` / ``reviewed`` / ``overturned``
                 — the calibration signal at a glance.
- ``review``   record a human's confirm/overturn of one audit item by id
                 (``--id``, ``--verdict``, optional ``--note``). Overturning
                 records a calibration signal only; it does NOT re-execute or
                 unwind the tier's merge decision.

A thin dispatcher over :mod:`athenaeum.calibration`, mirroring
:mod:`athenaeum._cmd_axiom`'s shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.calibration import calibration_summary, record_audit_review


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def cmd_calibration(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum calibration {summary,review}``."""
    sub = getattr(args, "calibration_target", None)
    if sub not in ("summary", "review"):
        print("usage: athenaeum calibration {summary,review} [...]", file=sys.stderr)
        return 2

    wiki_root = _resolve_wiki_root(args)

    if sub == "summary":
        summary = calibration_summary(wiki_root)
        if args.json:
            sys.stdout.write(json.dumps(summary) + "\n")
            return 0
        for tier, counts in summary.items():
            print(
                f"{tier}: sampled {counts['sampled']}, "
                f"reviewed {counts['reviewed']}, overturned {counts['overturned']}"
            )
        return 0

    # sub == "review"
    try:
        record = record_audit_review(
            wiki_root,
            audit_id=args.id,
            human_verdict=args.verdict,
            note=getattr(args, "note", "") or "",
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        sys.stdout.write(json.dumps(record) + "\n")
    else:
        outcome = "overturned" if record["overturned"] else "confirmed"
        print(
            f"{outcome} audit item {record['id']} "
            f"(tier {record['tier']}: {record['original_verdict']!r} "
            f"-> human {record['human_verdict']!r})"
        )
    return 0


def add_calibration_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum calibration`` and its modes on ``subparsers``."""
    c_parser = subparsers.add_parser(
        "calibration",
        help=(
            "Tier-audit calibration: per-tier sampled/reviewed/overturned "
            "summary, and record a human confirm/overturn of an audit item "
            "(issue #438)."
        ),
    )
    c_sub = c_parser.add_subparsers(dest="calibration_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=Path("~/knowledge"),
            help="Knowledge directory (default: ~/knowledge)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    summary_p = c_sub.add_parser(
        "summary",
        help="Per-tier calibration counts (sampled / reviewed / overturned).",
    )
    _add_common(summary_p)

    review_p = c_sub.add_parser(
        "review",
        help="Record a human confirm/overturn of a sampled audit item.",
    )
    _add_common(review_p)
    review_p.add_argument(
        "--id", required=True, help="The audit item id (from `decisions list`)."
    )
    review_p.add_argument(
        "--verdict",
        required=True,
        help="The human's verdict. Equal to the tier's original verdict = "
        "confirm; different = overturn (a calibration signal only).",
    )
    review_p.add_argument(
        "--note", default="", help="Optional free-text note on the review."
    )
