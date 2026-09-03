# SPDX-License-Identifier: Apache-2.0
"""``athenaeum decisions {list,next,count}`` — the one "human decisions needed" list.

Unifies pending **questions** (contradiction detector) and pending **merges**
(resolver proposals) into a single queue (issue athenaeum#401). Each item is tagged
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

A fourth item type, ``confirmation`` (issue athenaeum#1290), is included in
all three above — an agent-raiseable "implemented X without Y, confirm?"
flag. ``raise-confirmation`` is the CLI's WRITE path for it (mirroring the
MCP ``raise_decision`` tool's ``kind="confirmation"``); resolving one is
still ``resolve_question`` (MCP) since storage-wise it is a block in
``_pending_questions.md`` like any other question.

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

from athenaeum._cli_shared import _resolve_knowledge_root, _resolve_wiki_root
from athenaeum.answers import raise_pending_question
from athenaeum.config import (
    DEFAULT_KNOWLEDGE_ROOT,
    load_config,
    resolve_decisions_max_sources_per_merge,
)
from athenaeum.decisions import age_days, list_pending_decisions


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
    elif decision["type"] == "confirmation":
        # Issue athenaeum#1290: an agent-raised "implemented X without Y,
        # confirm?" flag — render the structured fields the MCP payload
        # carries (see ``athenaeum.decisions.confirmation_to_decision``).
        lines.append(f"  raiser: {payload.get('raiser', '')}")
        lines.append(f"  repo: {payload.get('repo', '')}")
        lines.append(f"  issue/PR: {payload.get('issue_ref', '')}")
        lines.append(f"  narrowed scope: {payload.get('narrowed_scope', '')}")
        lines.append(
            f"  implemented behaviour: {payload.get('implemented_behavior', '')}"
        )
        lines.append(f"  alternative: {payload.get('alternative', '')}")
        if payload.get("raised_at"):
            lines.append(f"  raised at: {payload['raised_at']}")
    else:
        if payload.get("description"):
            lines.append(f"  description: {payload['description']}")
        proposal = payload.get("proposal")
        if proposal:
            lines.append("  proposal:")
            for p_line in proposal.splitlines():
                lines.append(f"    {p_line}")
    return "\n".join(lines)


def _counts(decisions: list[dict]) -> tuple[int, int, int, int, int, int, str | None]:
    """Return ``(total, questions, merges, retractions, audits, confirmations,
    oldest_created_at)``."""
    questions = sum(1 for d in decisions if d["type"] == "question")
    merges = sum(1 for d in decisions if d["type"] == "merge")
    retractions = sum(1 for d in decisions if d["type"] == "retraction")
    audits = sum(1 for d in decisions if d["type"] == "audit")
    # Issue athenaeum#1290.
    confirmations = sum(1 for d in decisions if d["type"] == "confirmation")
    # ``list_pending_decisions`` returns oldest-first, so the first item's
    # created_at is the oldest across all queues.
    oldest = decisions[0]["created_at"] if decisions else None
    return (
        len(decisions),
        questions,
        merges,
        retractions,
        audits,
        confirmations,
        oldest,
    )


def _cmd_scan_retractions(args: argparse.Namespace) -> int:
    """Run the retraction cascade (issue athenaeum#435): flag dependent merges for review.

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


def _cmd_raise_confirmation(args: argparse.Namespace) -> int:
    """``athenaeum decisions raise-confirmation`` — the CLI half of AC1 (athenaeum#1290).

    The CLI counterpart to the MCP ``raise_decision`` tool's
    ``kind="confirmation"`` path — same underlying function
    (:func:`athenaeum.answers.raise_pending_question`), same validation, same
    on-disk block. Exists so an agent WITHOUT MCP access (a plain shell
    context) can still raise a durable "implemented X without Y, confirm?"
    flag, exactly per AC1's "through the MCP server and the CLI".
    """
    wiki_root = _resolve_wiki_root(args)
    pending_path = wiki_root / "_pending_questions.md"
    result = raise_pending_question(
        pending_path,
        args.question or "",
        args.context or "",
        entity=args.entity or "",
        kind="confirmation",
        raiser=args.raiser,
        repo=args.repo,
        issue_ref=args.issue_ref,
        narrowed_scope=args.narrowed_scope,
        implemented_behavior=args.implemented_behavior,
        alternative=args.alternative,
    )
    if args.json:
        sys.stdout.write(json.dumps(result) + "\n")
    elif result["ok"]:
        print(f"confirmation raised: id={result['decision_id']}")
    else:
        print(f"error [{result['error_code']}]: {result['message']}", file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_decisions(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum decisions {list,next,count}``.

    Never raises on missing/empty sidecars: count returns zeros, list/next
    print nothing (or ``null`` JSON for ``next``) and exit 0.
    """
    sub = getattr(args, "decisions_target", None)
    if sub not in (
        "list",
        "next",
        "count",
        "scan-retractions",
        "raise-confirmation",
    ):
        print(
            "usage: athenaeum decisions "
            "{list,next,count,scan-retractions,raise-confirmation} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "scan-retractions":
        return _cmd_scan_retractions(args)

    if sub == "raise-confirmation":
        return _cmd_raise_confirmation(args)

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
        total, questions, merges, retractions, audits, confirmations, oldest = _counts(
            decisions
        )
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
                        "confirmations": confirmations,
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
            if confirmations:
                breakdown += f", {confirmations} confirmations"
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
    d_parser.set_defaults(func=cmd_decisions)
    d_sub = d_parser.add_subparsers(dest="decisions_target")

    def _add_common(parser: argparse.ArgumentParser, *, with_proposal: bool) -> None:
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
        if with_proposal:
            parser.add_argument(
                "--with-proposal",
                action="store_true",
                help=(
                    "Include the (optional) `**Proposed resolution**` block "
                    "on question items (athenaeum#126)."
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
            "for human review (issue athenaeum#435). Idempotent; never unmerges."
        ),
    )
    _add_common(scan_p, with_proposal=False)

    raise_p = d_sub.add_parser(
        "raise-confirmation",
        help=(
            "File a NEW agent-raised 'implemented X without Y, confirm?' "
            "item into the pending-decisions queue (issue athenaeum#1290) — "
            "the CLI counterpart of the MCP raise_decision tool's "
            "kind=\"confirmation\" path."
        ),
    )
    raise_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    raise_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    raise_p.add_argument(
        "--raiser", required=True, help="Who/what narrowed scope (agent name, lane id, ...)."
    )
    raise_p.add_argument("--repo", required=True, help="The owner/repo narrowed in.")
    raise_p.add_argument(
        "--issue-ref",
        dest="issue_ref",
        required=True,
        help="The issue or PR the narrowing relates to.",
    )
    raise_p.add_argument(
        "--narrowed-scope",
        dest="narrowed_scope",
        required=True,
        help="What was narrowed — the scope NOT covered.",
    )
    raise_p.add_argument(
        "--implemented-behavior",
        dest="implemented_behavior",
        required=True,
        help="What was actually built instead.",
    )
    raise_p.add_argument(
        "--alternative",
        required=True,
        help="The road not taken — what a human might have wanted instead.",
    )
    raise_p.add_argument(
        "--question",
        default="",
        help=(
            "Optional checkbox question text. Auto-phrased from the "
            "structured fields above when omitted."
        ),
    )
    raise_p.add_argument(
        "--context",
        default="",
        help=(
            "Optional standalone context. Auto-phrased from the structured "
            "fields above when omitted."
        ),
    )
    raise_p.add_argument(
        "--entity",
        default="",
        help="Optional short human-readable header label. Cosmetic only.",
    )
