# SPDX-License-Identifier: Apache-2.0
"""``athenaeum questions {list,next,count}`` — surface unresolved pending questions.

Reads ``~/knowledge/wiki/_pending_questions.md`` via the existing
:mod:`athenaeum.answers` parser. Outputs are deterministic and JSON-friendly
so the example SessionStart hook (``pending-questions-surface.sh``) and the
shipped ``resolve-questions`` skill can rely on stable shapes.

Three modes:

- ``list``     all unresolved questions (optionally ``--with-proposal``,
                 ``--limit``, ``--json``)
- ``next``     the OLDEST unresolved question (one block)
- ``count``    ``N unresolved (oldest: <iso-date>)`` summary

The proposal block (``**Proposed resolution**`` etc., shipped in #126) is
extracted from the raw block tail when present. Entries without a proposal
remain valid and just emit an empty ``proposal`` field in JSON output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.answers import PendingQuestion, parse_pending_questions

# Keys the resolver appends to a block tail (locked by issue #126 and the
# resolutions.py module docstring). Order matters for re-extraction.
_PROPOSAL_KEYS = (
    "**Proposed resolution**:",
    "**Confidence**:",
    "**Rationale**:",
    "**Source precedence**:",
)


def _extract_proposal_block(raw_block: str) -> str:
    """Pull the trailing 4-key proposal block out of ``raw_block``.

    Returns the verbatim proposal text (joined with ``\\n``) or ``""`` when
    the block has no proposal. Tolerant of blank lines and ordering — we
    just collect any line that starts with one of the four keys.
    """
    lines = raw_block.splitlines()
    proposal_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(key) for key in _PROPOSAL_KEYS):
            proposal_lines.append(stripped)
    return "\n".join(proposal_lines)


def _question_to_dict(pq: PendingQuestion, *, with_proposal: bool) -> dict:
    out: dict = {
        "id": pq.id,
        "entity": pq.entity,
        "source": pq.source,
        "question": pq.question,
        "conflict_type": pq.conflict_type,
        "description": pq.description,
        "created_at": pq.created_at,
    }
    if with_proposal:
        out["proposal"] = _extract_proposal_block(pq.raw_block)
    return out


def _format_block(pq: PendingQuestion, *, with_proposal: bool) -> str:
    """Human-readable rendering for stdout (non-JSON path)."""
    lines = [
        f"## [{pq.created_at}] Entity: {pq.entity!r} (from {pq.source})",
        f"  id: {pq.id}",
        f"  question: {pq.question}",
    ]
    if pq.conflict_type:
        lines.append(f"  conflict_type: {pq.conflict_type}")
    if pq.description:
        lines.append(f"  description: {pq.description}")
    if with_proposal:
        proposal = _extract_proposal_block(pq.raw_block)
        if proposal:
            lines.append("  proposal:")
            for p_line in proposal.splitlines():
                lines.append(f"    {p_line}")
    return "\n".join(lines)


def _resolve_pending_path(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki" / "_pending_questions.md"


def _unanswered(pending_path: Path) -> list[PendingQuestion]:
    return [pq for pq in parse_pending_questions(pending_path) if not pq.answered]


def cmd_questions(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum questions {list,next,count}``.

    The hook surface contract: this never raises on a missing or empty
    ``_pending_questions.md`` — count returns 0 / null oldest, list/next
    print nothing and exit 0. Lets the SessionStart hook fail-silent.
    """
    sub = getattr(args, "questions_target", None)
    if sub not in ("list", "next", "count"):
        print(
            "usage: athenaeum questions {list,next,count} [...]",
            file=sys.stderr,
        )
        return 2

    pending_path = _resolve_pending_path(args)
    questions = _unanswered(pending_path)

    if sub == "count":
        oldest = questions[0].created_at if questions else None
        if args.json:
            sys.stdout.write(
                json.dumps({"count": len(questions), "oldest": oldest}) + "\n"
            )
        else:
            if not questions:
                print("0 unresolved")
            else:
                print(f"{len(questions)} unresolved (oldest: {oldest})")
        return 0

    if sub == "next":
        if not questions:
            if args.json:
                sys.stdout.write("null\n")
            return 0
        pq = questions[0]
        if args.json:
            sys.stdout.write(
                json.dumps(_question_to_dict(pq, with_proposal=args.with_proposal))
                + "\n"
            )
        else:
            print(_format_block(pq, with_proposal=args.with_proposal))
        return 0

    # sub == "list"
    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        questions = questions[:limit]

    if args.json:
        payload = [
            _question_to_dict(pq, with_proposal=args.with_proposal) for pq in questions
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    if not questions:
        print("0 unresolved")
        return 0

    for idx, pq in enumerate(questions):
        if idx > 0:
            print()
        print(_format_block(pq, with_proposal=args.with_proposal))
    return 0


def add_questions_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum questions`` and its three modes on ``subparsers``."""
    q_parser = subparsers.add_parser(
        "questions",
        help=(
            "Inspect unresolved entries in `_pending_questions.md`. "
            "Three modes: list, next, count. Used by the example "
            "SessionStart hook and the resolve-questions skill."
        ),
    )
    q_sub = q_parser.add_subparsers(dest="questions_target")

    common = {
        "path": (
            "--path",
            {
                "type": Path,
                "default": Path("~/knowledge"),
                "help": "Knowledge directory (default: ~/knowledge)",
            },
        ),
        "json": (
            "--json",
            {
                "action": "store_true",
                "help": "Emit machine-readable JSON instead of plain text.",
            },
        ),
        "with_proposal": (
            "--with-proposal",
            {
                "action": "store_true",
                "help": (
                    "Include the (optional) `**Proposed resolution**` "
                    "block from the resolver (#126)."
                ),
            },
        ),
    }

    list_p = q_sub.add_parser("list", help="List all unresolved questions.")
    list_p.add_argument(*((common["path"][0],)), **common["path"][1])
    list_p.add_argument(*((common["json"][0],)), **common["json"][1])
    list_p.add_argument(*((common["with_proposal"][0],)), **common["with_proposal"][1])
    list_p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Truncate to first N (default: 0 = unlimited).",
    )

    next_p = q_sub.add_parser(
        "next", help="Show the oldest unresolved question (single block)."
    )
    next_p.add_argument(*((common["path"][0],)), **common["path"][1])
    next_p.add_argument(*((common["json"][0],)), **common["json"][1])
    next_p.add_argument(*((common["with_proposal"][0],)), **common["with_proposal"][1])

    count_p = q_sub.add_parser(
        "count", help="Print `N unresolved (oldest: <iso-date>)`."
    )
    count_p.add_argument(*((common["path"][0],)), **common["path"][1])
    count_p.add_argument(*((common["json"][0],)), **common["json"][1])
