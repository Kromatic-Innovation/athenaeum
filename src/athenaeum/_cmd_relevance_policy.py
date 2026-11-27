# SPDX-License-Identifier: Apache-2.0
"""``athenaeum relevance-policy`` — read back the effective relevance rules.

Issue athenaeum#2643 (slice 1 of athenaeum#2598). Answers "what are you set to
remember?" by listing the rules from ``wiki/_schema/relevance-policy.md`` in
evaluation order (:mod:`athenaeum.relevance_policy`). Read-only: no run lock,
no writes, no LLM call. Exits 1 on an invalid file (the error names each bad
rule) and 0 otherwise — including when no policy file exists.

Factoring rule (L5 presentation): see ``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def cmd_relevance_policy(args: argparse.Namespace) -> int:
    from athenaeum.relevance_policy import (
        RelevancePolicyError,
        load_relevance_policy,
        render_policy,
    )

    wiki_root = _resolve_knowledge_root(args) / "wiki"
    try:
        policy = load_relevance_policy(wiki_root)
    except RelevancePolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        sys.stdout.write(json.dumps(policy.to_dict()) + "\n")
    else:
        sys.stdout.write(render_policy(policy))
    return 0


def cmd_relevance_policy_report(args: argparse.Namespace) -> int:
    """List existing wiki pages each ``never`` rule covers. Changes nothing."""
    from athenaeum.relevance_policy import (
        RelevancePolicyError,
        load_relevance_policy,
        report_always_matches,
        report_never_matches,
    )

    wiki_root = _resolve_knowledge_root(args) / "wiki"
    try:
        policy = load_relevance_policy(wiki_root)
    except RelevancePolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    matches = report_never_matches(policy, wiki_root)
    protected = report_always_matches(policy, wiki_root)
    if args.json:
        counts: dict[int, int] = {}
        for p in protected:
            counts[p.rule.number] = counts.get(p.rule.number, 0) + 1
        sys.stdout.write(
            json.dumps(
                {
                    "matches": [m.to_dict() for m in matches],
                    "protected": [p.to_dict() for p in protected],
                    "protected_counts": {str(k): v for k, v in sorted(counts.items())},
                }
            )
            + "\n"
        )
        return 0
    if not matches:
        sys.stdout.write("No existing pages match a `never` rule.\n")
    else:
        sys.stdout.write(
            f"{len(matches)} existing page(s) match a `never` rule "
            "(report only; nothing was changed):\n"
        )
        _write_by_rule(matches)
    if not protected:
        sys.stdout.write("No existing pages are protected by an `always` rule.\n")
    else:
        sys.stdout.write(
            f"\n{len(protected)} existing page(s) are protected from retirement by an "
            "`always` rule:\n"
        )
        _write_by_rule(protected)
    return 0


def _write_by_rule(matches: list) -> None:
    current = None
    for m in matches:
        if m.rule.number != current:
            current = m.rule.number
            n = sum(1 for x in matches if x.rule.number == current)
            sys.stdout.write(
                f"\nrule {m.rule.number} (line {m.rule.line}): {m.rule.describe()} - {n} page(s)\n"
            )
        sys.stdout.write(f"  {m.path}\n")


def add_relevance_policy_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum relevance-policy``."""
    parser = subparsers.add_parser(
        "relevance-policy",
        help="List the effective relevance-policy rules (always/never capture "
        "rules from wiki/_schema/relevance-policy.md) in evaluation order, "
        "last match wins (issue athenaeum#2643). An `always` rule also protects "
        "matching pages from retire, decay-sweep and audit retirement "
        "candidates (athenaeum#3152). Read-only.",
    )
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
    parser.set_defaults(func=cmd_relevance_policy)
    sub = parser.add_subparsers(dest="relevance_policy_target")
    report = sub.add_parser(
        "report",
        help="List existing wiki pages that match each `never` rule "
        "(read-only; a new `never` rule applies forward only) and how many "
        "live pages each `always` rule protects from retirement.",
    )
    report.add_argument(
        "--path",
        type=Path,
        default=argparse.SUPPRESS,
        help="Knowledge directory (default: ~/knowledge)",
    )
    report.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON instead of plain text.",
    )
    report.set_defaults(func=cmd_relevance_policy_report)
