# SPDX-License-Identifier: Apache-2.0
"""``athenaeum memory-class backfill`` — issue athenaeum#996.

Presentation for :mod:`athenaeum.memory_class_backfill`. Dry-run by default
and ``--apply`` to write, matching ``decay-sweep`` / ``auto-memory prune``
rather than inventing a third convention for a destructive-ish sweep; the
explicit ``--dry-run`` flag is accepted too so a caller can state the safe
mode rather than rely on the default.

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


def cmd_memory_class(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum memory-class backfill``."""
    if getattr(args, "memory_class_target", None) != "backfill":
        print("usage: athenaeum memory-class backfill [...]", file=sys.stderr)
        return 2

    from athenaeum.memory_class_backfill import apply_backfill, build_backfill_report

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    should_write = bool(getattr(args, "apply", False)) and not bool(
        getattr(args, "dry_run", False)
    )

    client: Any = None
    config: dict[str, Any] | None = None
    if args.classifier:
        from athenaeum.config import load_config
        from athenaeum.provider import build_llm_client

        config = load_config(knowledge_root)
        # ``knob="classify"`` — the residual pass is entity classification's
        # sibling, so it inherits that knob's provider/model routing instead
        # of introducing a config key nothing else uses.
        client = build_llm_client(config, knob="classify")
        if client is None:
            print(
                "error: --classifier requested but no LLM client is "
                "configured for the 'classify' knob (check llm.provider / "
                "ANTHROPIC_API_KEY)",
                file=sys.stderr,
            )
            return 1

    report = build_backfill_report(
        wiki_root,
        use_classifier=args.classifier,
        client=client,
        config=config,
        include_retired=args.include_retired,
        batch_size=args.batch_size,
    )

    changed = apply_backfill(report) if should_write else 0

    if args.json:
        payload = report.to_dict()
        payload["applied"] = should_write
        payload["files_changed"] = changed
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    by_class = report.counts_by_class()
    by_reason = report.counts_by_reason()
    print(f"scanned: {report.scanned} page(s) under {wiki_root}")
    print(f"assignable: {len(report.assignments)}")
    for name, count in by_class.items():
        print(f"  {name}: {count}")
    print("skipped:")
    for reason, count in by_reason.items():
        if reason in ("mechanical", "classifier"):
            continue
        print(f"  {reason}: {count}")
    if args.classifier:
        print(
            f"classifier: {report.classifier_calls} call(s), "
            f"{report.classifier_rejected} answer(s) rejected "
            "(axiom or out-of-taxonomy)"
        )
    print(
        f"applied: {changed} file(s) written"
        if should_write
        else "dry run: nothing written (pass --apply to write)"
    )
    return 0


def add_memory_class_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum memory-class`` and its ``backfill`` mode."""
    parser = subparsers.add_parser(
        "memory-class",
        help=(
            "Memory-taxonomy class maintenance: backfill the memory_class: "
            "frontmatter axis across the wiki (issue athenaeum#996)."
        ),
    )
    parser.set_defaults(func=cmd_memory_class)
    sub = parser.add_subparsers(dest="memory_class_target")

    backfill = sub.add_parser(
        "backfill",
        help="Assign memory_class: to pages that lack it — deterministic "
        "type-rule map, plus an optional classifier pass over the residual. "
        "Dry-run unless --apply. Never overwrites an existing value and "
        "never mints 'axiom'.",
    )
    backfill.set_defaults(func=cmd_memory_class)
    backfill.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="Write the assignments. Without this flag the command reports "
        "and writes nothing.",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing. This is already the default; the flag "
        "exists so a caller can state it, and it OVERRIDES --apply when both "
        "are given (safe mode wins).",
    )
    backfill.add_argument(
        "--classifier",
        action="store_true",
        help="Also class the residual (auto-memory/preference/feedback/"
        "incident/issue and untyped-but-frontmattered pages) with batched "
        "LLM calls routed through the 'classify' knob. Off by default: the "
        "deterministic rule map needs no model and covers ~97% of pages.",
    )
    backfill.add_argument(
        "--include-retired",
        action="store_true",
        help="Do not skip pages carrying 'retired: true'. Off by default — "
        "a retired page is on its way out and does not merit a classifier "
        "call.",
    )
    backfill.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Pages per classifier call (default: 20).",
    )
    backfill.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
