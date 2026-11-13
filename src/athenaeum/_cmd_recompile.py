# SPDX-License-Identifier: Apache-2.0
"""``athenaeum recompile {candidates,plan,show}`` -- the page-recompile
operator surface (issues athenaeum#2534 and athenaeum#2537, slices S1 and
S3 of athenaeum#2525).

* ``candidates`` (athenaeum#2534) runs
  :func:`athenaeum.page_recompile.find_recompile_candidates` and prints
  COUNTS ONLY (candidates, flagged, over ceiling, blocked, with a count per
  blocked reason) -- never a page name, uid, or filename (that issue's
  AC8). Makes no LLM call, writes nothing, never takes the run lock.
* ``plan --uid U`` (athenaeum#2537 AC11) runs
  :func:`athenaeum.page_recompile_plan.plan_page` for ONE page the operator
  names and appends the resulting plan to ``wiki/_page_recompiles.jsonl``
  as a shadow record, pinned to the page's content hash. It changes no
  page: the ledger is the only file it can write. ``--dry-run`` suppresses
  even that.
* ``show <plan id>`` (athenaeum#2537 AC11) prints a recorded plan.

``plan`` is the only subcommand here that makes an LLM call. Neither ``plan``
nor ``show`` writes a page, raises a decision item, or has an undo --
applying a plan is a later slice's job.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_recompile_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum recompile`` and its ``candidates`` subcommand."""
    parser = subparsers.add_parser(
        "recompile",
        help="Page-recompile maintenance pass (issues athenaeum#2534, "
        "athenaeum#2537): a read-only candidate scan, a shadow-only "
        "planner, and a plan viewer. Writes no page.",
    )
    parser.set_defaults(func=_cmd_recompile_dispatch)
    sub = parser.add_subparsers(dest="recompile_target")

    candidates_p = sub.add_parser(
        "candidates",
        help="Scan for active pages over librarian.page_size_threshold_chars "
        "or flagged needs_split: true, and report counts only -- no page "
        "name, uid, or filename is ever printed. Read-only: no run lock, "
        "no writes, no LLM call.",
    )
    candidates_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    candidates_p.set_defaults(func=_cmd_candidates)

    plan_p = sub.add_parser(
        "plan",
        help="Plan the recompilation of ONE page (--uid) and append the plan "
        "to wiki/_page_recompiles.jsonl as a shadow record. Writes no page.",
    )
    plan_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    plan_p.add_argument("--uid", required=True, help="uid of the page to plan")
    plan_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the planner but append nothing to the ledger.",
    )
    plan_p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the verify-knob check of the parent summary.",
    )
    plan_p.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    plan_p.set_defaults(func=_cmd_plan)

    show_p = sub.add_parser(
        "show",
        help="Print a recorded plan by its plan id.",
    )
    show_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    show_p.add_argument("plan_id", help="plan id, as printed by `recompile plan`")
    show_p.add_argument("--json", action="store_true", help="Print the record as JSON.")
    show_p.set_defaults(func=_cmd_show)


def _cmd_recompile_dispatch(args: argparse.Namespace) -> int:
    print("usage: athenaeum recompile {candidates,plan,show} [...]", file=sys.stderr)
    return 2


def _cmd_candidates(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum recompile candidates``.

    Exit codes: 0 clean run; 1 wiki root missing.
    """
    from athenaeum.config import load_config
    from athenaeum.page_recompile import find_recompile_candidates

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    scan = find_recompile_candidates(wiki_root, cfg)

    reason_counts: dict[str, int] = {}
    for blocked in scan.blocked:
        reason_counts[blocked.reason] = reason_counts.get(blocked.reason, 0) + 1

    # Counts only (AC8) -- no path, uid, or filename from `scan` is ever
    # formatted into a print() call below.
    print("=== recompile candidates ===")
    print(f"  candidates:           {len(scan.candidates)}")
    print(f"    flagged:            {sum(c.flagged for c in scan.candidates)}")
    print(f"    over hard ceiling:  {sum(c.over_ceiling for c in scan.candidates)}")
    print(f"  blocked:              {len(scan.blocked)}")
    for reason, count in sorted(reason_counts.items()):
        print(f"    {reason}:{' ' * max(1, 20 - len(reason))}{count}")
    return 0


def _wiki_root_or_error(args: argparse.Namespace) -> Path | None:
    """``<knowledge>/wiki``, or ``None`` after printing the error (exit 1)."""
    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return None
    return wiki_root


def _cmd_plan(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum recompile plan --uid U`` (issue athenaeum#2537 AC11).

    Exit codes: 0 a plan was produced (valid or not -- an invalid plan is a
    reportable outcome, not a command failure); 1 wiki root missing, no page
    with that uid, no LLM client, or no plan returned.
    """
    import json as _json

    from athenaeum.config import load_config
    from athenaeum.page_recompile_plan import plan_page
    from athenaeum.provider import build_llm_client

    wiki_root = _wiki_root_or_error(args)
    if wiki_root is None:
        return 1
    knowledge_root = args.path.expanduser().resolve()
    config = load_config(knowledge_root)

    client = build_llm_client(config, knob="page_recompile")
    if client is None:
        print(
            "error: no LLM client is configured for the 'page_recompile' knob "
            "(check llm.provider / ANTHROPIC_API_KEY)",
            file=sys.stderr,
        )
        return 1

    result = plan_page(
        wiki_root=wiki_root,
        uid=args.uid,
        client=client,
        config=config,
        record=not args.dry_run,
        verify=not args.no_verify,
    )
    if result.plan is None:
        print(f"error: {result.error or 'no plan produced'}", file=sys.stderr)
        return 1

    if args.json:
        payload = result.plan.to_dict()
        payload["plan_id"] = result.plan_id
        payload["recorded"] = not args.dry_run
        sys.stdout.write(_json.dumps(payload, sort_keys=True) + "\n")
        return 0

    _print_plan(result.plan.to_dict(), plan_id=result.plan_id)
    print(f"  recorded:  {'no (dry run)' if args.dry_run else 'yes'}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum recompile show <plan id>`` (issue athenaeum#2537 AC11).

    Exit codes: 0 found and printed; 1 wiki root missing or no such plan id.
    """
    import json as _json

    from athenaeum.page_recompile_plan import find_plan_record

    wiki_root = _wiki_root_or_error(args)
    if wiki_root is None:
        return 1

    record = find_plan_record(args.plan_id, wiki_root)
    if record is None:
        print(f"error: no plan with id {args.plan_id}", file=sys.stderr)
        return 1
    if args.json:
        sys.stdout.write(_json.dumps(record, sort_keys=True) + "\n")
        return 0
    plan = record.get("plan")
    _print_plan(plan if isinstance(plan, dict) else {}, plan_id=str(record.get("id") or ""))
    print(f"  recorded:  {record.get('ts', '')}")
    return 0


def _print_plan(plan: dict[str, object], *, plan_id: str) -> None:
    """Render one plan for a terminal. Shared by ``plan`` and ``show`` so the
    two surfaces cannot drift into two different renderings of one record."""
    print("=== recompile plan ===")
    print(f"  plan id:   {plan_id}")
    print(f"  uid:       {plan.get('uid', '')}")
    print(f"  pinned to: {plan.get('content_hash', '')}")
    print(f"  scope:     {plan.get('scope', '')}")
    print(f"  valid:     {plan.get('valid', False)}")
    print(f"  verified:  {plan.get('verified')}")

    groups = plan.get("groups")
    if isinstance(groups, list) and groups:
        print("  groups:")
        for group in groups:
            if not isinstance(group, dict):
                continue
            print(
                f"    {group.get('key', '')}: {group.get('name', '')} "
                f"(confidence {group.get('confidence', 0)})"
            )
            print(f"      {group.get('description', '')}")

    dispositions = plan.get("dispositions")
    if isinstance(dispositions, list) and dispositions:
        print("  dispositions:")
        for entry in dispositions:
            if isinstance(entry, dict):
                print(f"    {entry.get('unit', '')} -> {entry.get('disposition', '')}")

    merges = plan.get("merges")
    if isinstance(merges, list) and merges:
        print("  merges:")
        for merge in merges:
            if isinstance(merge, dict):
                units = merge.get("units")
                count = len(units) if isinstance(units, list) else 0
                print(f"    {merge.get('group', '')}: {count} units")

    actions = plan.get("page_actions")
    if isinstance(actions, list) and actions:
        print("  page actions:")
        for action in actions:
            print(f"    {action}")

    summary = plan.get("summary")
    if isinstance(summary, str) and summary:
        print("  summary:")
        for line in summary.splitlines():
            print(f"    {line}")

    issues = plan.get("issues")
    if isinstance(issues, list) and issues:
        print("  issues:")
        for issue in issues:
            if isinstance(issue, dict):
                print(f"    {issue.get('code', '')}: {issue.get('detail', '')}")
