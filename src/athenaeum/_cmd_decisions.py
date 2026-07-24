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
    else:
        if payload.get("description"):
            lines.append(f"  description: {payload['description']}")
        proposal = payload.get("proposal")
        if proposal:
            lines.append("  proposal:")
            for p_line in proposal.splitlines():
                lines.append(f"    {p_line}")
    return "\n".join(lines)


def _counts(decisions: list[dict]) -> tuple[int, int, int, str | None]:
    """Return ``(total, questions, merges, oldest_created_at)``."""
    questions = sum(1 for d in decisions if d["type"] == "question")
    merges = sum(1 for d in decisions if d["type"] == "merge")
    # ``list_pending_decisions`` returns oldest-first, so the first item's
    # created_at is the oldest across both queues.
    oldest = decisions[0]["created_at"] if decisions else None
    return len(decisions), questions, merges, oldest


def cmd_decisions(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum decisions {list,next,count}``.

    Never raises on missing/empty sidecars: count returns zeros, list/next
    print nothing (or ``null`` JSON for ``next``) and exit 0.
    """
    sub = getattr(args, "decisions_target", None)
    if sub not in ("list", "next", "count"):
        print("usage: athenaeum decisions {list,next,count} [...]", file=sys.stderr)
        return 2

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
        total, questions, merges, oldest = _counts(decisions)
        oldest_age = age_days(oldest) if oldest else None
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "count": total,
                        "questions": questions,
                        "merges": merges,
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
            print(
                f"{total} decisions pending "
                f"({questions} questions, {merges} merges{age_str})"
            )
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
