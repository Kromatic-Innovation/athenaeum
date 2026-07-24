# SPDX-License-Identifier: Apache-2.0
"""``athenaeum decisions {list,next,count}`` — the one "human decisions needed" list.

Unifies pending **questions** (contradiction detector) and pending **merges**
(resolver proposals) into a single queue (issue #401). Each item is tagged
``type: "question" | "merge"`` and shares the common fields ``id``,
``created_at``, ``summary`` (a one-line, answerable question) and
``confidence`` (present for merges, ``null`` for questions), plus a
type-specific ``payload``.

The human doesn't think in "questions vs merges" — both are "athenaeum needs
me to decide something." One queue means one place to look, one age metric,
one briefing section, and no second surface to forget to build next time a
decision type is added.

Three modes mirror ``questions`` / ``merges``:

- ``list``   all pending decisions, oldest first (optionally ``--limit``,
               ``--with-proposal``, ``--json``)
- ``next``   the OLDEST pending decision (one block)
- ``count``  ``N decisions pending (Q questions, M merges; oldest Xd)``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import load_config, resolve_decisions_max_sources_per_merge
from athenaeum.decisions import age_days, list_pending_decisions


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    return (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()


def _format_block(decision: dict) -> str:
    """Human-readable rendering for stdout (non-JSON path)."""
    conf = decision.get("confidence")
    conf_str = f", confidence {conf:.2f}" if isinstance(conf, (int, float)) else ""
    lines = [
        f"## [{decision['created_at']}] {decision['type']}{conf_str}",
        f"  id: {decision['id']}",
        f"  question: {decision['summary']}",
    ]
    payload = decision.get("payload", {})
    if decision["type"] == "merge":
        for src in payload.get("sources", []):
            gist = f" — {src['gist']}" if src["gist"] else ""
            lines.append(f"    - {src['title']}{gist}")
        omitted = payload.get("sources_omitted", 0)
        if omitted:
            lines.append(f"    - … and {omitted} more")
    elif decision["type"] == "retraction":
        lines.append(f"  merge: {payload.get('merge_id', '')}")
        lines.append(f"  retracted source: {payload.get('retracted_ref', '')}")
        if payload.get("reason"):
            lines.append(f"  reason: {payload['reason']}")
    elif decision["type"] == "audit":
        lines.append(
            f"  tier {payload.get('tier', '')} verdict: {payload.get('verdict', '')}"
        )
        lines.append(f"  proposal: {payload.get('proposal_id', '')}")
        if payload.get("reason"):
            lines.append(f"  reason: {payload['reason']}")
    else:
        if payload.get("description"):
            lines.append(f"  description: {payload['description']}")
        proposal = payload.get("proposal")
        if proposal:
            lines.append("  proposal:")
            for p_line in proposal.splitlines():
                lines.append(f"    {p_line}")
    return "\n".join(lines)


def _counts(decisions: list[dict]) -> tuple[int, int, int, int, int, str | None]:
    """Return ``(total, questions, merges, retractions, audits, oldest_created_at)``."""
    questions = sum(1 for d in decisions if d["type"] == "question")
    merges = sum(1 for d in decisions if d["type"] == "merge")
    retractions = sum(1 for d in decisions if d["type"] == "retraction")
    audits = sum(1 for d in decisions if d["type"] == "audit")
    # ``list_pending_decisions`` returns oldest-first, so the first item's
    # created_at is the oldest across all queues.
    oldest = decisions[0]["created_at"] if decisions else None
    return len(decisions), questions, merges, retractions, audits, oldest


def _cmd_scan_retractions(args: argparse.Namespace) -> int:
    """Run the retraction cascade (issue #435): flag dependent merges for review.

    Reads the merge-provenance ledger (under ``wiki/``) and the observation
    supersession log (under the contacts/excluded surface) and appends a
    review item for every completed merge that relied on a now-retracted
    source. Idempotent — a re-scan emits only newly-flagged pairs. Never
    unmerges anything.
    """
    from athenaeum.pii import contacts_surface_root
    from athenaeum.retraction_cascade import scan_retraction_cascade

    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    config = load_config(knowledge_root)
    contacts_root = contacts_surface_root(knowledge_root, config)
    newly = scan_retraction_cascade(wiki_root, contacts_root)
    if args.json:
        sys.stdout.write(json.dumps({"flagged": len(newly), "items": newly}) + "\n")
    elif not newly:
        print("0 merges newly flagged for retraction review")
    else:
        print(f"{len(newly)} merge(s) newly flagged for retraction review:")
        for rec in newly:
            print(
                f"  - merge {rec['merge_id']} into "
                f'"{rec["canonical_slug"]}" (retracted source {rec["retracted_ref"]})'
            )
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum decisions {list,next,count}``.

    Never raises on missing/empty sidecars: count returns zeros, list/next
    print nothing (or ``null`` JSON for ``next``) and exit 0.
    """
    sub = getattr(args, "decisions_target", None)
    if sub not in ("list", "next", "count", "scan-retractions"):
        print(
            "usage: athenaeum decisions {list,next,count,scan-retractions} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "scan-retractions":
        return _cmd_scan_retractions(args)

    wiki_root = _resolve_wiki_root(args)
    with_proposal = getattr(args, "with_proposal", False)
    config = load_config(_resolve_knowledge_root(args))
    max_sources_per_merge = resolve_decisions_max_sources_per_merge(config)
    decisions = list_pending_decisions(
        wiki_root,
        with_proposal=with_proposal,
        max_sources_per_merge=max_sources_per_merge,
    )

    if sub == "count":
        total, questions, merges, retractions, audits, oldest = _counts(decisions)
        oldest_age = age_days(oldest) if oldest else None
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "count": total,
                        "questions": questions,
                        "merges": merges,
                        "retractions": retractions,
                        "audits": audits,
                        "oldest": oldest,
                        "oldest_age_days": oldest_age,
                    }
                )
                + "\n"
            )
        elif total == 0:
            print("0 decisions pending")
        else:
            age_str = f"; oldest {oldest_age}d" if oldest_age is not None else ""
            breakdown = f"{questions} questions, {merges} merges"
            if retractions:
                breakdown += f", {retractions} retractions"
            if audits:
                breakdown += f", {audits} audits"
            print(f"{total} decisions pending ({breakdown}{age_str})")
        return 0

    if sub == "next":
        if not decisions:
            if args.json:
                sys.stdout.write("null\n")
            return 0
        decision = decisions[0]
        if args.json:
            sys.stdout.write(json.dumps(decision) + "\n")
        else:
            print(_format_block(decision))
        return 0

    # sub == "list"
    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        decisions = decisions[:limit]

    if args.json:
        sys.stdout.write(json.dumps(decisions) + "\n")
        return 0

    if not decisions:
        print("0 decisions pending")
        return 0

    for idx, decision in enumerate(decisions):
        if idx > 0:
            print()
        print(_format_block(decision))
    return 0


def add_decisions_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum decisions`` and its three modes on ``subparsers``."""
    d_parser = subparsers.add_parser(
        "decisions",
        help=(
            "One unified 'human decisions needed' list — pending questions "
            "AND merges, each tagged by type. Three modes: list, next, count."
        ),
    )
    d_sub = d_parser.add_subparsers(dest="decisions_target")

    def _add_common(parser: argparse.ArgumentParser, *, with_proposal: bool) -> None:
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
        if with_proposal:
            parser.add_argument(
                "--with-proposal",
                action="store_true",
                help=(
                    "Include the (optional) `**Proposed resolution**` block "
                    "on question items (#126)."
                ),
            )

    list_p = d_sub.add_parser("list", help="List all pending decisions, oldest first.")
    _add_common(list_p, with_proposal=True)
    list_p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Truncate to first N (default: 0 = unlimited).",
    )

    next_p = d_sub.add_parser(
        "next", help="Show the oldest pending decision (single block)."
    )
    _add_common(next_p, with_proposal=True)

    count_p = d_sub.add_parser(
        "count",
        help="Print `N decisions pending (Q questions, M merges; oldest Xd)`.",
    )
    _add_common(count_p, with_proposal=False)

    scan_p = d_sub.add_parser(
        "scan-retractions",
        help=(
            "Flag any completed merge that relied on a now-retracted source "
            "for human review (issue #435). Idempotent; never unmerges."
        ),
    )
    _add_common(scan_p, with_proposal=False)
