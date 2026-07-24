# SPDX-License-Identifier: Apache-2.0
"""``athenaeum axiom {promote,demote,list}`` — axiom governance CLI (issue #434).

Axiom assignment (``memory_class: axiom``) must be an explicit,
human-approved act, never something the librarian/LLM write path mints
silently. This CLI is the sanctioned authorization surface (see
:mod:`athenaeum.axiom_governance`'s module docstring for why a direct CLI
acknowledgement was chosen over routing through the #401 decisions-queue):

- ``promote``  record a promotion: ``--slug``, ``--reason``, ``--by``,
                 optional ``--scope``. Appends to the append-only ledger
                 (``wiki/_axiom_governance.jsonl``); never mutates the page
                 itself (setting ``memory_class: axiom`` on the frontmatter
                 is a separate, ordinary edit — this command only records
                 the human authorization for it).
- ``demote``   symmetric: record a demotion with ``--slug``, ``--reason``,
                 ``--by``. No dogma without an exit.
- ``list``     the assignment audit — every slug's current status (active
                 promotion or not) plus its full promote/demote history
                 (when/why/by-whom), oldest-first.

Mirrors ``athenaeum merges provenance`` in shape (a thin CLI dispatcher over
:mod:`athenaeum.axiom_governance`, no logic of its own).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.axiom_governance import list_axiom_audit, record_demotion, record_promotion


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def _format_audit_entry(entry: dict) -> str:
    status = "ACTIVE" if entry["active"] else "inactive"
    lines = [f"## {entry['slug']} ({status})"]
    for record in entry["history"]:
        scope = f", scope={record['scope']!r}" if record.get("scope") else ""
        lines.append(
            f"  [{record.get('at', '?')}] {record.get('action', '?')} "
            f"by {record.get('by', '?')}: {record.get('reason', '?')}{scope}"
        )
    return "\n".join(lines)


def cmd_axiom(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum axiom {promote,demote,list}``."""
    sub = getattr(args, "axiom_target", None)
    if sub not in ("promote", "demote", "list"):
        print("usage: athenaeum axiom {promote,demote,list} [...]", file=sys.stderr)
        return 2

    wiki_root = _resolve_wiki_root(args)

    if sub == "promote":
        record = record_promotion(
            wiki_root,
            slug=args.slug,
            reason=args.reason,
            by=args.by,
            scope=getattr(args, "scope", None),
        )
        if args.json:
            sys.stdout.write(json.dumps(record) + "\n")
        else:
            print(f"promoted {record['slug']!r} (by {record['by']}: {record['reason']})")
        return 0

    if sub == "demote":
        record = record_demotion(
            wiki_root,
            slug=args.slug,
            reason=args.reason,
            by=args.by,
        )
        if args.json:
            sys.stdout.write(json.dumps(record) + "\n")
        else:
            print(f"demoted {record['slug']!r} (by {record['by']}: {record['reason']})")
        return 0

    # sub == "list"
    audit = list_axiom_audit(wiki_root)
    if args.json:
        sys.stdout.write(json.dumps(audit) + "\n")
        return 0
    if not audit:
        print("0 axioms on record")
        return 0
    for idx, entry in enumerate(audit):
        if idx > 0:
            print()
        print(_format_audit_entry(entry))
    return 0


def add_axiom_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum axiom`` and its three modes on ``subparsers``."""
    a_parser = subparsers.add_parser(
        "axiom",
        help=(
            "Axiom governance: explicit human-approved promotion/demotion "
            "of memory_class: axiom pages, plus the assignment audit "
            "(issue #434)."
        ),
    )
    a_sub = a_parser.add_subparsers(dest="axiom_target")

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

    promote_p = a_sub.add_parser(
        "promote",
        help="Record a human-approved axiom promotion for a wiki page slug.",
    )
    _add_common(promote_p)
    promote_p.add_argument("--slug", required=True, help="The wiki page slug being promoted.")
    promote_p.add_argument(
        "--reason", required=True, help="Why this page is being promoted to axiom."
    )
    promote_p.add_argument("--by", required=True, help="Who is authorizing the promotion.")
    promote_p.add_argument(
        "--scope",
        default=None,
        help='Optional context scope (e.g. "applies to resume work"). '
        "Stored + surfaced; enforcement is a consumer's concern (out of "
        "scope for #434).",
    )

    demote_p = a_sub.add_parser(
        "demote",
        help="Record a human-approved axiom demotion for a wiki page slug.",
    )
    _add_common(demote_p)
    demote_p.add_argument("--slug", required=True, help="The wiki page slug being demoted.")
    demote_p.add_argument("--reason", required=True, help="Why this axiom is being demoted.")
    demote_p.add_argument("--by", required=True, help="Who is authorizing the demotion.")

    list_p = a_sub.add_parser(
        "list",
        help="Assignment audit: every slug's current status + full "
        "promote/demote history (when/why/by-whom).",
    )
    _add_common(list_p)
