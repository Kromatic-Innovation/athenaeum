# SPDX-License-Identifier: Apache-2.0
"""``athenaeum decompose-page`` — redistribute one aggregate page's facts
(issue athenaeum#1914).

Dry-run by default, exactly like ``paste-cleanup`` and ``retire-pages``: the
bare command classifies every bullet on the named page and writes a JSON
report to ``--report``; ``--apply`` attaches the ruled-and-resolved facts to
the pages they concern and rewrites the source page. The domain logic — and
the reasoning for why this is a new pass rather than a correction or a raw
intake note — lives in :mod:`athenaeum.page_decompose`.

**The report goes to a file, never to stdout.** It names subjects, uids and
page filenames; stdout gets counts only, so an unattended lane can print this
command's output without leaking corpus content into a ticket.

This command does NOT commit, tag or quiesce. Taking a rollback tag and
quiescing the corpus around the apply is the operator's step, matching every
other host-write job in this repo.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_decompose_page_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``decompose-page``."""

    parser = subparsers.add_parser(
        "decompose-page",
        help="Redistribute an aggregate page's per-entity facts onto the "
        "company/person pages they concern, then rewrite the page itself "
        "(issue athenaeum#1914). Default is dry-run: it writes a JSON "
        "report to --report and changes nothing. --apply attaches the "
        "facts and rewrites the source page, and refuses outright while "
        "any bullet is unresolved, sourceless or ambiguously sourced "
        "without a ruling in --resolutions. Makes no LLM call.",
    )
    parser.add_argument(
        "uid",
        help="uid of the aggregate page to decompose.",
    )
    parser.add_argument(
        "--subject-until",
        required=True,
        metavar="REGEX",
        help="Regex marking the end of each bullet's subject. The subject is "
        "the bullet's leading span up to the first match; a bullet the "
        "regex does not match is reported unresolved rather than guessed "
        "at. Supplied at run time so a page's sentence template never has "
        "to be recorded in this repo.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        metavar="PATH",
        help="Write the JSON report here. The report names subjects and "
        "page filenames and is never printed to stdout.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Attach the facts and rewrite the source page. Without this "
        "flag the command writes nothing but the report.",
    )
    parser.add_argument(
        "--resolutions",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON object of bullet-id -> uid | 'drop', ruling on the "
        "bullets the dry run could not place. A ruling whose id no longer "
        "matches its bullet's text is refused. --apply only.",
    )
    parser.add_argument(
        "--rewrite-body",
        type=Path,
        default=None,
        metavar="FILE",
        help="Markdown body the source page is rewritten to. Required with "
        "--apply. Refused if it exceeds 2 KB, links to a page that does "
        "not exist, or still names any decomposed subject.",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="New frontmatter description: for the source page. Required "
        "with --apply, and subject to the same no-subject-names check as "
        "the body (the current description is itself a list of companies).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_decompose_page)


def cmd_decompose_page(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum decompose-page``.

    Exit codes:
        0  - clean run (dry-run report written, or --apply succeeded).
        1  - refused: bad uid/regex, a missing or unruled bullet, an
             unusable rewrite, or an I/O failure. Nothing was written.
        75 - the run lock is held (``EXIT_LOCK_HELD``). --apply only;
             a dry run never takes the lock because it never writes.
    """
    from athenaeum.config import load_config
    from athenaeum.page_decompose import (
        DecomposeError,
        apply_report,
        build_report,
        load_resolutions,
        write_report,
    )

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    try:
        re.compile(args.subject_until)
    except re.error as exc:
        print(f"error: --subject-until is not a valid regex: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        missing = [
            flag
            for flag, value in (
                ("--rewrite-body", args.rewrite_body),
                ("--description", args.description),
            )
            if value is None
        ]
        if missing:
            print(f"error: --apply requires {', '.join(missing)}", file=sys.stderr)
            return 1

    try:
        report = build_report(wiki_root, args.uid, subject_until=args.subject_until)
    except DecomposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: reading the source page failed: {exc}", file=sys.stderr)
        return 1

    try:
        write_report(report, args.report)
    except OSError as exc:
        print(f"error: writing the report failed: {exc}", file=sys.stderr)
        return 1

    # Counts only. The per-bullet detail — subjects, uids, filenames — is in
    # the report file, deliberately out of anything a lane might paste.
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== decompose-page ({mode}) ===")
    print(f"  bullets:              {len(report.bullets)}")
    for disposition, count in report.counts.items():
        print(f"  {disposition + ':':22}{count}")
    print(f"  subjects resolved:    {report.subjects_resolved}")
    print(f"  subjects unresolved:  {report.subjects_unresolved}")
    print(f"  conflicting labels:   {report.conflicting_labels}")
    print(f"  orphan definitions:   {report.orphan_definitions}")
    print(f"  report:               {args.report}")

    if not args.apply:
        return 0

    try:
        resolutions = (
            load_resolutions(args.resolutions) if args.resolutions is not None else {}
        )
        rewrite_body = args.rewrite_body.read_text(encoding="utf-8")
    except DecomposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: reading --resolutions/--rewrite-body failed: {exc}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        result = apply_report(
            report,
            wiki_root,
            resolutions=resolutions,
            rewrite_body=rewrite_body,
            description=args.description,
        )
    except DecomposeError as exc:
        print(f"error: refusing to apply: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: applying failed: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()

    print(f"  attached:             {result.attached}")
    print(f"  skipped (present):    {result.skipped_already_present}")
    print(f"  dropped by ruling:    {result.dropped}")
    print(f"  source rewritten:     {'yes' if result.source_rewritten else 'no'}")
    return 0
