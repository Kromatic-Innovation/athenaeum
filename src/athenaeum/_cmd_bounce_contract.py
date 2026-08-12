# SPDX-License-Identifier: Apache-2.0
"""``athenaeum bounce-contract`` — Tier-0 bounce-note conformance CLI (issue athenaeum#854).

Mirrors ``athenaeum outbound-lint`` in shape and purpose: a thin CLI dispatcher
over a library function (:func:`athenaeum.bounce_contract.check_tier0_bounce_conformance`)
with no detection logic of its own, run by a PRODUCER before it ships content —
there, before a send; here, before a bulk raw-intake submission.

Reads a candidate note from ``--text``, ``--file``, or stdin (the default) and
answers "would Tier 0 recognize this as a hard-bounce fact?" **without writing
anything** — no mark, no intake submission, no store mutation, no network, no
LLM call. On a decline it names every unmet condition, so a producer can fix a
whole batch rather than learn only that it failed.

Exit codes: ``0`` — the note conforms; :data:`EXIT_NONCONFORMING` (2) — it does
not (it would fall through to the reasoning tiers, which is not an error, just
not a bounce mark). The non-zero-on-decline convention lets a producer gate a
submission on a clean check (``athenaeum bounce-contract --file note.md &&
submit note.md``), the same shell-gate shape ``outbound-lint`` uses. Code 1
stays the generic error.

Why a CLI is the shipped surface (the acceptance criterion lets the lane pick):
the consumer of this contract is a producer in ANOTHER repository, and the
nearest one (voltaire#117's backfill) is TypeScript. A Python function is not
callable from there; a subprocess with ``--json`` is. The underlying function
stays importable for a Python producer, but the CLI is the contract's portable
surface and the one the contract document points at.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in its
own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this module
may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.bounce_contract import check_tier0_bounce_conformance

#: Exit code when the candidate note would NOT be recognized by Tier 0
#: (mirrors ``outbound-lint``'s EXIT_PII_FOUND — a "found something to act on"
#: signal distinct from the generic error code 1).
EXIT_NONCONFORMING = 2


def _read_input(args: argparse.Namespace) -> str:
    """Resolve the note to check from --text, --file, or stdin (in that order)."""
    if args.text is not None:
        return args.text
    if args.file is not None:
        return args.file.read_text(encoding="utf-8")
    return sys.stdin.read()


def cmd_bounce_contract(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum bounce-contract``."""
    result = check_tier0_bounce_conformance(_read_input(args))

    if args.json:
        payload = {
            "conforms": result.conforms,
            "identifier": result.identifier,
            "diagnostic": result.diagnostic,
            "observed_at": result.observed_at,
            "declines": [
                {"reason": d.reason, "where": d.where, "detail": d.detail}
                for d in result.declines
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if result.conforms else EXIT_NONCONFORMING

    if result.conforms:
        print("conforms: Tier 0 would mark this note as a hard bounce")
        print(f"  identifier: {result.identifier}")
        print(f"  diagnostic: {result.diagnostic}")
        print(f"  observed_at: {result.observed_at}")
        return 0

    print(
        f"does NOT conform: {len(result.declines)} unmet condition(s). "
        "Tier 0 would leave this note to the reasoning tiers (not an error, "
        "and not a bounce mark):"
    )
    for d in result.declines:
        print(f"  [{d.where}] {d.reason}: {d.detail}")
    return EXIT_NONCONFORMING


def add_bounce_contract_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum bounce-contract`` on ``subparsers`` (issue athenaeum#854)."""
    parser = subparsers.add_parser(
        "bounce-contract",
        help="Check whether a candidate raw-intake note would be recognized "
        "by the Tier-0 hard-bounce gate, before submitting it. Read-only, "
        "offline, deterministic (issue athenaeum#854).",
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--text",
        default=None,
        help="The candidate note (frontmatter + body), given inline. Mutually "
        "exclusive with --file; if neither is given, the note is read from "
        "stdin.",
    )
    src.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to a file holding the candidate note. Mutually exclusive "
        "with --text.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON verdict instead of plain text.",
    )
    parser.set_defaults(func=cmd_bounce_contract)
