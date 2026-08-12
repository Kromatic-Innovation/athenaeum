# SPDX-License-Identifier: Apache-2.0
"""``athenaeum bounce-divergence`` — bounce-mark divergence report (issue athenaeum#853).

Why a CLI is the shipped surface (the acceptance criterion lets the lane pick,
and asks it to say why): this is an **operator** check run against a store —
including a private store this repository's tests can never touch — so it must
take a store root as a parameter and be runnable without writing Python. That
is the same reasoning ``athenaeum bounce-contract`` records for its own CLI,
and it lands next to that command where an operator already looks for
bounce-adjacent tooling. The underlying
:func:`athenaeum.bounce_divergence.compute_divergence` stays importable for a
Python caller.

**Where and how often to run it.** Against the live store, after any change to
either bounce surface — a backfill, a batch of raw-intake bounce notes, or a
change to the mark or the join itself — and periodically (a monthly operator
pass is enough for a surface that moves this slowly). What it is defending is
a *moving number*: a regression in the report path should show up as a
divergence that changes, not as silence. Nothing here alerts or thresholds —
deciding what to do when the number moves is deliberately out of scope.

Exit codes: ``0`` — the report was produced (divergence is **not** an error;
this issue makes it visible, it does not judge it);
:data:`EXIT_SURFACE_UNREADABLE` (2) — at least one surface could not be read,
so the difference is not a divergence measurement. That non-zero is the shell-
level half of "an empty result and a failed scan must never render
identically". Code 1 stays the generic error.

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

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config

#: Exit code when a surface could not be read — distinct from "read it, found
#: nothing" (0) and from the generic error (1).
EXIT_SURFACE_UNREADABLE = 2


def cmd_bounce_divergence(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum bounce-divergence``."""
    from athenaeum.bounce_divergence import compute_divergence, render_report, report_as_dict
    from athenaeum.pii import contacts_surface_root

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    wiki_root = args.wiki_root or (knowledge_root / "wiki")
    contacts_root = args.contacts_root or contacts_surface_root(knowledge_root, config)

    report = compute_divergence(wiki_root, contacts_root)

    if args.json:
        sys.stdout.write(json.dumps(report_as_dict(report)) + "\n")
    else:
        sys.stdout.write(render_report(report))

    return 0 if report.complete else EXIT_SURFACE_UNREADABLE


def add_bounce_divergence_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum bounce-divergence`` on *subparsers* (issue athenaeum#853)."""
    parser = subparsers.add_parser(
        "bounce-divergence",
        help="Report the difference between the two bounce surfaces (wiki "
        "`bounced:` frontmatter and the contacts-surface mark) for a store. "
        "Read-only; output is safe to paste publicly (issue athenaeum#853).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge-base root to report on. Both surfaces are resolved "
        "from it (the contacts surface through the configured storage "
        "mapping).",
    )
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Override the wiki surface root (defaults to <path>/wiki).",
    )
    parser.add_argument(
        "--contacts-root",
        type=Path,
        default=None,
        help="Override the contacts surface root (defaults to the configured "
        "`pii` entity-class surface).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of plain text. Carries the same "
        "opaque handles — no addresses or names in either form.",
    )
    parser.set_defaults(func=cmd_bounce_divergence)
