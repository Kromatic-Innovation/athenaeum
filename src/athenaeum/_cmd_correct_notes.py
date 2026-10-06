# SPDX-License-Identifier: Apache-2.0
"""``athenaeum correct-notes`` — move or drop individual Notes lines on a
person/company page (issue athenaeum#1976).

Apply-by-default, UNLIKE ``decompose-page``/``paste-cleanup``/
``retire-pages``: the bare command resolves every record in ``--batch``
against one snapshot of the named page and, when every id and target
resolve, writes the moves/drops and records one ledger line.
``--dry-run`` reports counts only and changes nothing. The domain logic —
and the fuller rationale for a new pass rather than an existing write
path — lives in :mod:`athenaeum.note_corrections` and
``docs/design/note-corrections.md``.

**Counts only, same posture as ``decompose-page``'s report.** Neither mode
prints a bullet's text, subject or target uid to stdout — a ``--dry-run``
report is numbers only (records by action, refusals, body chars/bytes
against the size thresholds), so an unattended lane can print this
command's output without leaking corpus content into a ticket.

This command does NOT commit, tag or quiesce. Taking a rollback tag around
the apply is the operator's step, matching every other host-write job in
this repo.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_correct_notes_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``correct-notes``."""

    parser = subparsers.add_parser(
        "correct-notes",
        help="Move or drop individual Notes lines on a person/company page "
        "(issue athenaeum#1976), e.g. lines misfiled onto a first-name "
        "match-magnet page. Default is APPLY: every bullet id and move "
        "target in --batch is resolved against one snapshot of the page, "
        "and the whole batch is written only when every record resolves. "
        "--dry-run reports counts only and changes nothing. Makes no LLM "
        "call.",
    )
    parser.add_argument(
        "uid",
        help="uid of the page the batch's Notes lines are corrected on. Must "
        "match the batch file's own source_uid.",
    )
    parser.add_argument(
        "--batch",
        type=Path,
        required=True,
        metavar="PATH",
        help="Host-path JSON batch file: {source_uid, batch_id, created_at, "
        "records: [{bullet_id, action: 'move'|'drop', target_uid?, "
        "note?}]}. Never read from raw/ -- see the module docstring for "
        "why that boundary matters here. A batch_id already present in "
        "the ledger makes the whole batch a no-op.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts only (records by action, refusals, body "
        "characters and file bytes against their respective thresholds) "
        "and write nothing. Without this flag the command applies.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_correct_notes)


def cmd_correct_notes(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum correct-notes``.

    Exit codes:
        0  - clean run (--dry-run report written, or the batch applied —
             including a no-op replay of an already-applied batch_id).
        1  - refused: bad uid/batch shape, an unknown bullet id or move
             target, or an I/O failure. Nothing was written.
        75 - the run lock is held (``EXIT_LOCK_HELD``). Apply only; a dry
             run never takes the lock because it never writes.
    """
    from athenaeum.config import load_config
    from athenaeum.note_corrections import (
        NoteCorrectionError,
        apply_batch,
        dry_run_report,
        load_batch,
    )

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    try:
        envelope = load_batch(args.batch)
    except NoteCorrectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if envelope.source_uid != args.uid:
        print(
            f"error: batch source_uid {envelope.source_uid!r} does not match "
            f"the uid argument {args.uid!r}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        try:
            report = dry_run_report(wiki_root, envelope)
        except NoteCorrectionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: reading the page failed: {exc}", file=sys.stderr)
            return 1

        print("=== correct-notes (DRY RUN) ===")
        print(f"  batch_id:             {report.batch_id}")
        print(f"  records total:        {report.records_total}")
        print(f"  replay (already applied): {'yes' if report.replay else 'no'}")
        print(f"  moved:                {report.moved}")
        print(f"  dropped:              {report.dropped}")
        print(f"  refused (unknown id):     {report.refused_unknown_id}")
        print(f"  refused (unknown target): {report.refused_unknown_target}")
        print(
            f"  body chars before/after: {report.body_chars_before}/"
            f"{report.body_chars_after} (threshold: {report.page_size_threshold_chars})"
        )
        print(
            f"  file bytes before/after: {report.file_bytes_before}/"
            f"{report.file_bytes_after} (flag threshold: {report.page_flag_bytes})"
        )
        return 0

    cfg = load_config(knowledge_root)
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        outcome = apply_batch(wiki_root, envelope)
    except NoteCorrectionError as exc:
        print(f"error: refusing to apply: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: applying failed: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()

    counts: dict[str, int] = {}
    for result in outcome.results:
        counts[result.disposition] = counts.get(result.disposition, 0) + 1

    print("=== correct-notes (APPLY) ===")
    print(f"  batch_id:             {outcome.batch_id}")
    print(f"  records total:        {outcome.records_total}")
    print(f"  replay (already applied): {'yes' if outcome.replay else 'no'}")
    print(f"  moved:                {counts.get('moved', 0)}")
    print(f"  dropped:              {counts.get('dropped', 0)}")
    print(f"  noop:                 {counts.get('noop', 0)}")
    print(f"  body chars before/after: {outcome.body_chars_before}/{outcome.body_chars_after}")
    return 0
