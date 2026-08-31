# SPDX-License-Identifier: Apache-2.0
"""``athenaeum calibration {summary,review}`` — tier-audit calibration CLI (issue athenaeum#438).

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

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.calibration import calibration_summary, record_audit_review
from athenaeum.config import (
    DEFAULT_KNOWLEDGE_ROOT,
    load_config,
    resolve_reasoning_tier_any_screen_enabled,
)

# Issue athenaeum#518: the message shown when the reasoning-tier subsystem is not
# enabled — an explicit state so an operator never mistakes a permanent
# 0/0/0 all-clear for "the tiers ran and are well calibrated". Checked via
# resolve_reasoning_tier_any_screen_enabled (issue athenaeum#1200: T1 and T2 are
# now independently armed, so this must be OR, not just T1's flag — a T2-only
# config must still see its own sampled audit items here, not a false
# "not enabled").
_NOT_ENABLED_MSG = (
    "tier auditing not enabled "
    "(set librarian.reasoning_tier_auditing_enabled: true for T1, and/or "
    "librarian.reasoning_tier_t2_auto_apply_enabled: true for T2, to enable "
    "the reasoning tiers and their calibration loop)"
)


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def cmd_calibration(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum calibration {summary,review}``."""
    sub = getattr(args, "calibration_target", None)
    if sub not in ("summary", "review"):
        print("usage: athenaeum calibration {summary,review} [...]", file=sys.stderr)
        return 2

    wiki_root = _resolve_wiki_root(args)

    # Issue athenaeum#518: gate the calibration surface behind the explicit opt-in.
    # When off, report the not-enabled state rather than an empty-but-"green"
    # summary that lies about a subsystem that never ran.
    if not resolve_reasoning_tier_any_screen_enabled(load_config(wiki_root.parent)):
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"enabled": False, "error": _NOT_ENABLED_MSG}) + "\n"
            )
        else:
            print(_NOT_ENABLED_MSG, file=sys.stderr)
        return 0 if sub == "summary" else 1

    if sub == "summary":
        summary = calibration_summary(wiki_root)
        if args.json:
            sys.stdout.write(json.dumps(summary) + "\n")
            return 0
        for tier, counts in summary.items():
            line = (
                f"{tier}: sampled {counts['sampled']}, "
                f"reviewed {counts['reviewed']}, overturned {counts['overturned']}"
            )
            if counts.get("applied"):
                line += f", applied {counts['applied']}"
            # Issue athenaeum#602: surface an overturn of an ALREADY-APPLIED (auto-
            # finalized, live-in-the-wiki) merge prominently — this is the
            # one number that means "a human caught a bad write that
            # already happened", never buried inside the plain
            # ``overturned`` count.
            if counts.get("overturned_applied"):
                line += (
                    f" *** {counts['overturned_applied']} OVERTURN(S) OF AN "
                    "APPLIED MERGE — already live in the wiki ***"
                )
            print(line)
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
        line = (
            f"{outcome} audit item {record['id']} "
            f"(tier {record['tier']}: {record['original_verdict']!r} "
            f"-> human {record['human_verdict']!r})"
        )
        if record.get("overturned_applied"):
            line += (
                " *** THIS MERGE WAS ALREADY AUTO-APPLIED (live in the wiki) "
                "— automated unwinding is out of scope; a human must "
                "manually correct the wiki page ***"
            )
        print(line)
    return 0


def add_calibration_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum calibration`` and its modes on ``subparsers``."""
    c_parser = subparsers.add_parser(
        "calibration",
        help=(
            "Tier-audit calibration: per-tier sampled/reviewed/overturned "
            "summary, and record a human confirm/overturn of an audit item "
            "(issue athenaeum#438)."
        ),
    )
    c_parser.set_defaults(func=cmd_calibration)
    c_sub = c_parser.add_subparsers(dest="calibration_target")

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
