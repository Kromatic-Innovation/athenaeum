# SPDX-License-Identifier: Apache-2.0
"""``athenaeum subject backfill`` — issue athenaeum#1244.

Presentation for :mod:`athenaeum.subject_backfill`. Dry-run by default and
``--apply`` to write, matching ``memory-class backfill`` / ``description
backfill``.

**Read :mod:`athenaeum.subject_backfill`'s module docstring before running
``--apply`` against a live store.** The derivation (``subject := uid``) is
mechanically safe and 100%-coverage, but whether it is the RIGHT value for
the ``subject`` kernel dimension's whole-page-claim use is an open question
this command does not decide — it is an entity-linking semantics question,
not a formatting one. ``--apply`` is provided for an operator who has made
that call deliberately, not as this issue's own default action.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def cmd_subject(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum subject backfill``."""
    if getattr(args, "subject_target", None) != "backfill":
        print("usage: athenaeum subject backfill [...]", file=sys.stderr)
        return 2

    from athenaeum.subject_backfill import apply_subject_backfill, build_subject_report

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    should_write = bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False))

    report = build_subject_report(wiki_root)
    changed = apply_subject_backfill(report) if should_write else 0

    if args.json:
        payload = report.to_dict()
        payload["applied"] = should_write
        payload["files_changed"] = changed
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    by_reason = report.counts_by_reason()
    print(f"scanned: {report.scanned} page(s) under {wiki_root}")
    print(f"derivable: {len(report.assignments)}")
    print("skipped:")
    for reason, count in by_reason.items():
        if reason == "derivable":
            continue
        print(f"  {reason}: {count}")
    print(
        f"applied: {changed} file(s) written"
        if should_write
        else "dry run: nothing written (pass --apply to write)"
    )
    if should_write and changed:
        print(
            "NOTE: this stamped subject := uid on the pages above. See "
            "athenaeum.subject_backfill's module docstring for the "
            "entity-linking hazard this write carries once a ratified-"
            "identity resolver ships (athenaeum#715 follow-up)."
        )
    return 0


def add_subject_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum subject`` and its ``backfill`` mode."""
    parser = subparsers.add_parser(
        "subject",
        help=(
            "Subject-coordinate maintenance: backfill the subject: "
            "frontmatter axis (issue athenaeum#1244)."
        ),
    )
    parser.set_defaults(func=cmd_subject)
    sub = parser.add_subparsers(dest="subject_target")

    backfill = sub.add_parser(
        "backfill",
        help="Write subject: (= uid) onto comparator-relevant pages that "
        "lack it. Zero-LLM, deterministic. Dry-run unless --apply. Never "
        "overwrites an existing value. Read the module docstring before "
        "using --apply on a live store.",
    )
    backfill.set_defaults(func=cmd_subject)
    backfill.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="Write the assignments. Without this flag the command reports and writes nothing.",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing. Already the default; OVERRIDES --apply "
        "when both are given (safe mode wins).",
    )
    backfill.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
