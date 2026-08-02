# SPDX-License-Identifier: Apache-2.0
"""``athenaeum drain`` — one-command supervised API+batch backlog drain (issue athenaeum#470).

A thin CLI wrapper over :func:`athenaeum.drain.run_drain`: it runs the pre-flight
guards (API key present, no finite deadline, cost confirmation), prints an
up-front cost ESTIMATE, acquires the run lock, and loops intake windows through
the forced API+batch path until the raw backlog empties or the cumulative
``--max-usd`` ceiling trips. The orchestration logic (and all estimators) live
in :mod:`athenaeum.drain`; this module only parses args and gates on them.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _positive_float(value: str) -> float:
    """Argparse type for ``--max-usd``: a strictly positive dollar amount."""
    try:
        fvalue = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid positive number: {value!r}"
        ) from None
    if fvalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number (got {value!r})")
    return fvalue


def add_drain_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum drain`` and its flags on *subparsers* (issue athenaeum#470)."""
    from athenaeum._cli_shared import _add_lock_args, _positive_int

    parser = subparsers.add_parser(
        "drain",
        help="Supervised API+batch drain of the raw-intake backlog (cost-guarded)",
        description=(
            "Loop intake windows through the API + Batch path (issue athenaeum#236, 50% "
            "token discount) until the raw backlog empties or the cumulative "
            "--max-usd ceiling trips. Forces provider=api, batch mode, and an "
            "unbounded run (batch block-polls; a finite deadline is the cwc#615 "
            "failure mode). Requires ANTHROPIC_API_KEY in the environment "
            "(athenaeum performs no credential handling)."
        ),
    )
    parser.add_argument(
        "--max-usd",
        type=_positive_float,
        required=True,
        metavar="N",
        help="Mandatory cost ceiling in USD applied CUMULATIVELY across the whole "
        "drain (not per window). Maps onto the athenaeum#378 spend.max_usd_per_run ceiling "
        "for each window as the remaining budget.",
    )
    parser.add_argument(
        "--max-files",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Intake window size — files compiled per window (default: "
        "librarian.max_files / 50). The drain loops windows until the backlog "
        "empties or the cost ceiling trips.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Proceed without the interactive cost confirmation (required to run "
        "non-interactively — the drain incurs real API spend).",
    )
    parser.add_argument(
        "--knowledge-root",
        "--path",
        type=Path,
        default=None,
        dest="knowledge_root",
        help="Knowledge directory (default: ~/knowledge).",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=None,
        help="Raw intake directory (default: <knowledge-root>/raw).",
    )
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory (default: <knowledge-root>/wiki).",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_drain)


def cmd_drain(args: argparse.Namespace) -> int:
    """Handle ``athenaeum drain`` (issue athenaeum#470). Returns a process exit code."""
    from athenaeum import drain, drain_advisor, spend
    from athenaeum._cli_shared import _acquire_or_exit
    from athenaeum.config import load_config
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, discover_raw_files

    knowledge_root = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser()
    raw_root = (args.raw_root or (knowledge_root / "raw")).expanduser()
    wiki_root = (args.wiki_root or (knowledge_root / "wiki")).expanduser()

    if not knowledge_root.exists():
        print(f"Knowledge directory not found: {knowledge_root}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {knowledge_root}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    config = load_config(knowledge_root)

    # Pre-flight guard 1: the API key is required — athenaeum handles no creds.
    err = drain.check_api_key()
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    # Pre-flight guard 2: batch mode + a finite deadline is the cwc#615 failure.
    runtime = drain.resolve_drain_runtime(config)
    err = drain.check_batch_deadline(max_runtime=runtime)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    # Backlog + up-front cost estimate.
    backlog = len(discover_raw_files(raw_root))
    if backlog == 0:
        print("Raw backlog is empty — nothing to drain.")
        return 0

    records = spend.read_ledger(spend.resolve_ledger_path(config))
    tokens = drain_advisor.observed_tokens_per_file(records)
    if tokens is None:
        avg_input = drain_advisor.DEFAULT_AVG_INPUT_TOKENS_PER_FILE
        avg_output = drain_advisor.DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE
        rate_note = "no ledger history — coarse token defaults"
    else:
        avg_input, avg_output = tokens
        rate_note = "observed ledger tokens/file"
    estimate = drain_advisor.estimate_drain_cost_usd(
        backlog=backlog,
        avg_input_per_file=avg_input,
        avg_output_per_file=avg_output,
        config=config,
        batch=True,
    )

    print("athenaeum drain — supervised API+batch backlog drain")
    print(f"  raw backlog:      {backlog} file(s)")
    print(f"  estimated cost:   ~${estimate:.2f} (API+batch; {rate_note})")
    print(f"  cost ceiling:     ${args.max_usd:g} (cumulative across the whole drain)")
    if estimate > args.max_usd:
        print(
            f"  NOTE: the estimate exceeds the ceiling — the drain will stop at "
            f"${args.max_usd:g} before the backlog is fully drained."
        )

    # Pre-flight guard 3: cost confirmation. --yes proceeds; otherwise prompt on
    # a TTY, and refuse loudly when neither is available.
    if not args.yes:
        if sys.stdin.isatty():
            reply = input("Proceed with the drain? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted.")
                return 1
        else:
            print(
                "error: refusing to start a cost-incurring drain "
                "non-interactively without --yes (pass --yes to confirm the "
                "estimated cost).",
                file=sys.stderr,
            )
            return 1

    lock = _acquire_or_exit(knowledge_root, args, config)
    if isinstance(lock, int):
        return lock
    try:
        return drain.run_drain(
            knowledge_root=knowledge_root,
            raw_root=raw_root,
            wiki_root=wiki_root,
            max_usd=args.max_usd,
            max_files=args.max_files,
            config=config,
        )
    finally:
        lock.release()
