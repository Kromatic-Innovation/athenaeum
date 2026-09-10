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

from athenaeum._cli_shared import _resolve_wiki_root
from athenaeum.calibration import calibration_summary, record_audit_review
from athenaeum.config import (
    DEFAULT_KNOWLEDGE_ROOT,
    load_config,
    resolve_reasoning_tier_any_screen_enabled,
    resolve_reasoning_tier_auditing_enabled,
    resolve_reasoning_tier_t2_auto_apply_enabled,
)
from athenaeum.reasoning_tiers import (
    T1_TIER_NAME,
    T2_TIER_NAME,
    read_reasoning_tier_decisions,
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

#: Per-tier (armed flag resolver, env/yaml hint) used by
#: :func:`_reasoning_tier_activity` to build the "armed but silent" warning
#: (issue athenaeum#1487). ``calibration_summary`` alone cannot distinguish this
#: state: it is fed ONLY by :func:`athenaeum.calibration.sample_tier_decision`,
#: which is invoked on a T1 REJECT (sampled at
#: ``audit_sample_rate_t1_rejects``, default 7.5%) or a T2 auto-applied
#: APPROVE (``audit_sample_rate_t2_approvals``) — never on a T1 pass-up. A
#: tier that is armed and running correctly but only ever passes proposals
#: up (the common case for "these are genuinely different entities") shows
#: the exact same permanent ``sampled: 0`` as a tier that was never invoked
#: at all. The UNSAMPLED, unconditional decision log
#: (``_reasoning_tier_decisions.jsonl``, written by
#: :func:`athenaeum.reasoning_tiers.record_reasoning_tier_decision` for
#: EVERY decision — reject or pass-up — via
#: :func:`athenaeum.reasoning_tiers.run_reasoning_pipeline`) is the only
#: source that can tell the two apart, which is why this reads it via
#: :func:`athenaeum.reasoning_tiers.read_reasoning_tier_decisions` instead
#: of extending the sampled ledger.
_TIER_ARM_RESOLVERS = {
    T1_TIER_NAME: (
        resolve_reasoning_tier_auditing_enabled,
        "librarian.reasoning_tier_auditing_enabled",
    ),
    T2_TIER_NAME: (
        resolve_reasoning_tier_t2_auto_apply_enabled,
        "librarian.reasoning_tier_t2_auto_apply_enabled",
    ),
}


def _reasoning_tier_activity(
    wiki_root: Path, config: dict | None
) -> dict[str, dict[str, object]]:
    """Per-tier raw activity from the UNSAMPLED decision log (issue athenaeum#1487).

    Returns ``{tier: {"decisions_logged": int, "last_decision_at": str |
    None, "armed": bool, "armed_but_silent": bool}}`` for T1 and T2 (plus
    any other tier tag present in the log). ``armed_but_silent`` is true
    only when that tier's OWN flag resolves true AND
    ``decisions_logged == 0`` — the state a config-only ``calibration
    summary`` cannot show (see :data:`_TIER_ARM_RESOLVERS`).
    """
    records = read_reasoning_tier_decisions(wiki_root)
    armed: dict[str, bool] = {
        tier: resolver(config) for tier, (resolver, _hint) in _TIER_ARM_RESOLVERS.items()
    }
    counts: dict[str, int] = dict.fromkeys(armed, 0)
    last_at: dict[str, str | None] = dict.fromkeys(armed)
    for record in records:
        tier = str(record.get("tier", ""))
        if not tier:
            continue
        counts[tier] = counts.get(tier, 0) + 1
        armed.setdefault(tier, False)
        ts = record.get("ts")
        if isinstance(ts, str) and (
            last_at.get(tier) is None or ts > str(last_at.get(tier))
        ):
            last_at[tier] = ts
        else:
            last_at.setdefault(tier, None)

    activity: dict[str, dict[str, object]] = {}
    for tier in armed:
        decisions_logged = counts.get(tier, 0)
        activity[tier] = {
            "decisions_logged": decisions_logged,
            "last_decision_at": last_at.get(tier),
            "armed": armed[tier],
            "armed_but_silent": bool(armed[tier] and decisions_logged == 0),
        }
    return activity


def cmd_calibration(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum calibration {summary,review}``."""
    sub = getattr(args, "calibration_target", None)
    if sub not in ("summary", "review"):
        print("usage: athenaeum calibration {summary,review} [...]", file=sys.stderr)
        return 2

    wiki_root = _resolve_wiki_root(args)
    config = load_config(wiki_root.parent)

    # Issue athenaeum#518: gate the calibration surface behind the explicit opt-in.
    # When off, report the not-enabled state rather than an empty-but-"green"
    # summary that lies about a subsystem that never ran.
    if not resolve_reasoning_tier_any_screen_enabled(config):
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"enabled": False, "error": _NOT_ENABLED_MSG}) + "\n"
            )
        else:
            print(_NOT_ENABLED_MSG, file=sys.stderr)
        return 0 if sub == "summary" else 1

    if sub == "summary":
        summary = calibration_summary(wiki_root)
        # Issue athenaeum#1487: layer the UNSAMPLED decision-log activity (count +
        # last-decision timestamp, per tier) on top of the sampled calibration
        # counts above — see `_reasoning_tier_activity`'s docstring for why
        # `sampled`/`reviewed` alone cannot tell "armed and quietly passing
        # everything up" apart from "armed but never actually invoked".
        activity = _reasoning_tier_activity(wiki_root, config)
        if args.json:
            merged = {
                tier: {**counts, **activity.get(tier, {})}
                for tier, counts in summary.items()
            }
            sys.stdout.write(json.dumps(merged) + "\n")
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
            tier_activity = activity.get(tier)
            if tier_activity is not None:
                last_at = tier_activity["last_decision_at"] or "never"
                print(
                    f"{tier}: {tier_activity['decisions_logged']} decision(s) "
                    f"logged (unsampled), last {last_at}"
                )
                if tier_activity["armed_but_silent"]:
                    print(
                        f" *** {tier} is ARMED but has recorded ZERO decisions "
                        "— it may never actually be reaching a proposal. Check "
                        f"the reasoning_{tier.lower()} LLM client (provider/API "
                        "key) and that merge proposals are reaching the "
                        "screen. ***"
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
