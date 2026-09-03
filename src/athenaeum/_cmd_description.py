# SPDX-License-Identifier: Apache-2.0
"""``athenaeum description backfill`` — issue athenaeum#1324.

Presentation for :mod:`athenaeum.page_description`. Dry-run by default and
``--apply`` to write, matching ``memory-class backfill`` / ``decay-sweep``.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def cmd_description(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum description backfill``."""
    if getattr(args, "description_target", None) != "backfill":
        print("usage: athenaeum description backfill [...]", file=sys.stderr)
        return 2

    from athenaeum.page_description import (
        apply_description_backfill,
        build_description_report,
    )

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    should_write = bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False))

    client: Any = None
    config: dict[str, Any] | None = None
    if not args.mechanical:
        from athenaeum.config import load_config
        from athenaeum.provider import build_llm_client

        config = load_config(knowledge_root)
        # ``knob="classify"`` — the describer is a Haiku-class sibling of the
        # Tier-2 classifier, so it inherits that knob's provider/model routing
        # instead of introducing a config key nothing else uses.
        client = build_llm_client(config, knob="classify")
        if client is None:
            print(
                "error: no LLM client is configured for the 'classify' knob "
                "(check llm.provider / ANTHROPIC_API_KEY), and --mechanical "
                "was not given",
                file=sys.stderr,
            )
            return 1

    report = build_description_report(
        wiki_root,
        mechanical=args.mechanical,
        client=client,
        config=config,
        include_retired=args.include_retired,
        batch_size=args.batch_size,
        limit=args.limit,
    )

    changed = apply_description_backfill(report) if should_write else 0

    if args.json:
        payload = report.to_dict()
        payload["applied"] = should_write
        payload["files_changed"] = changed
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    by_reason = report.counts_by_reason()
    print(f"scanned: {report.scanned} page(s) under {wiki_root}")
    print(f"assignable: {len(report.assignments)}")
    for reason in ("mechanical", "llm"):
        if reason in by_reason:
            print(f"  {reason}: {by_reason[reason]}")
    print("skipped:")
    for reason, count in by_reason.items():
        if reason in ("mechanical", "llm"):
            continue
        print(f"  {reason}: {count}")
    if not args.mechanical:
        print(f"llm: {report.llm_calls} call(s)")
    if args.sample:
        print("sample:")
        for outcome in report.assignments[: args.sample]:
            print(f"  {outcome.path.name}: {outcome.description}")
    print(
        f"applied: {changed} file(s) written"
        if should_write
        else "dry run: nothing written (pass --apply to write)"
    )
    return 0


def add_description_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum description`` and its ``backfill`` mode."""
    parser = subparsers.add_parser(
        "description",
        help=(
            "Page-summary maintenance: backfill the one-line description: "
            "frontmatter the recall hook injects (issue athenaeum#1324)."
        ),
    )
    parser.set_defaults(func=cmd_description)
    sub = parser.add_subparsers(dest="description_target")

    backfill = sub.add_parser(
        "backfill",
        help="Write description: onto pages that lack it — batched LLM "
        "summaries through the 'classify' knob, or --mechanical for a "
        "zero-LLM opening-paragraph derivation. Dry-run unless --apply. "
        "Never overwrites an existing value. Resumable: re-run to continue.",
    )
    backfill.set_defaults(func=cmd_description)
    backfill.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="Write the descriptions. Without this flag the command reports and writes nothing.",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing. Already the default; OVERRIDES --apply "
        "when both are given (safe mode wins).",
    )
    backfill.add_argument(
        "--mechanical",
        action="store_true",
        help="Derive each description from the page's opening paragraph "
        "instead of an LLM call. Free and instant; lower quality on pages "
        "whose first paragraph is not a summary.",
    )
    backfill.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Decide at most N pages this pass (the rest are reported "
        "'undecided'). Re-run to continue — already-described pages are "
        "skipped, so successive runs drain the backlog.",
    )
    backfill.add_argument(
        "--include-retired",
        action="store_true",
        help="Do not skip pages carrying 'retired: true'.",
    )
    backfill.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Pages per LLM call (default: 20).",
    )
    backfill.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Print the first N decided descriptions (dry-run review aid).",
    )
    backfill.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
