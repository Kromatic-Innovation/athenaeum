# SPDX-License-Identifier: Apache-2.0
"""``athenaeum do-not-email-divergence`` — do_not_email divergence report (issue athenaeum#960).

The anti-recurrence criterion of issue athenaeum#960: a check that reports the
two-surface difference for ``do_not_email`` (wiki page frontmatter vs. the
excluded-record surface) and, unlike ``bounce-divergence`` (issue
athenaeum#853), **exits non-zero on ANY divergence** — the tolerated residual
for this field is zero, not just "the surfaces were readable". That fixes the
exact defect athenaeum#960's issue names in ``bounce-divergence``: a check
whose exit code never reflects the number it reports is indistinguishable
from no check at all.

Same reasoning as ``athenaeum bounce-divergence`` for why this is a CLI: an
**operator** check run against a store — including a private store this
repository's tests can never touch — so it must take a store root as a
parameter and be runnable without writing Python.

**Where and how often to run it.** Wired into ``ci.yml``'s ``test`` job via
the pytest suite (``tests/test_do_not_email_divergence.py::TestCli``) against
a fixture store on every push/PR, so a regression in the check itself is
caught before merge. Against the LIVE store, this repository ships no
in-repo scheduler (``docs/configuration.md`` — "There is no shipped nightly
cron wrapper in this repo"); an operator's own external cron/launchd should
invoke ``athenaeum do-not-email-divergence --path ~/knowledge`` alongside
the existing ``athenaeum run`` / ``athenaeum ingest`` entries, and treat a
non-zero exit as an alert (the residual is zero, so any exit above 0 is
actionable, not merely informational).

Exit codes: ``0`` — both surfaces were read and agree;
:data:`EXIT_SURFACE_UNREADABLE` (2) — at least one surface could not be
read, so the difference is not a divergence measurement;
:data:`EXIT_DIVERGED` (3) — both surfaces were read and they diverge. Code 1
stays the generic error.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config

#: Exit code when a surface could not be read — distinct from "read it, found
#: an agreement" (0) and from a genuine divergence (3).
EXIT_SURFACE_UNREADABLE = 2

#: Exit code when both surfaces were read and they diverge at all. The
#: tolerated residual for `do_not_email` is zero (issue athenaeum#960), so —
#: unlike `bounce-divergence` — this is a distinct non-zero code, not folded
#: into a bare "report was produced" 0.
EXIT_DIVERGED = 3


def cmd_do_not_email_divergence(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum do-not-email-divergence``."""
    from athenaeum.do_not_email_divergence import (
        compute_do_not_email_divergence,
        render_report,
        report_as_dict,
    )
    from athenaeum.pii import contacts_surface_root

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    wiki_root = args.wiki_root or (knowledge_root / "wiki")
    contacts_root = args.contacts_root or contacts_surface_root(knowledge_root, config)

    report = compute_do_not_email_divergence(wiki_root, contacts_root)

    if args.json:
        sys.stdout.write(json.dumps(report_as_dict(report)) + "\n")
    else:
        sys.stdout.write(render_report(report))

    if not report.complete:
        return EXIT_SURFACE_UNREADABLE
    if report.diverged:
        return EXIT_DIVERGED
    return 0


def add_do_not_email_divergence_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum do-not-email-divergence`` on *subparsers* (issue athenaeum#960)."""
    parser = subparsers.add_parser(
        "do-not-email-divergence",
        help="Report the difference between the two do_not_email surfaces "
        "(wiki page frontmatter and the excluded-record mark) for a store, "
        "and exit non-zero on ANY divergence — the tolerated residual is "
        "zero. Read-only; output is safe to paste publicly (issue athenaeum#960).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge-base root to report on. Both surfaces are resolved "
        "from it (the excluded surface through the configured storage "
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
        help="Override the excluded surface root (defaults to the configured "
        "`pii` entity-class surface).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of plain text. Carries the same "
        "opaque handles — no addresses or names in either form.",
    )
    parser.set_defaults(func=cmd_do_not_email_divergence)
