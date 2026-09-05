# SPDX-License-Identifier: Apache-2.0
"""``athenaeum surface-divergence`` — the generalized per-field guard (issue athenaeum#963).

Generalizes ``bounce-divergence`` (issue athenaeum#853) and
``do-not-email-divergence`` (issue athenaeum#960) into ONE command,
parameterized by ``--field``, driven by the :mod:`athenaeum.surface_divergence`
registry. The two prior commands were removed once this command replaced
them (issue athenaeum#1111) — this is now the ONLY divergence-guard entry
point; ``bounce-divergence``'s historical exit-0-unless-unreadable contract
lives on only as this command's opt-in ``--report-only`` mode. The two
underlying library modules (:mod:`athenaeum.bounce_divergence`,
:mod:`athenaeum.do_not_email_divergence`) stay in place — this command still
wraps their unchanged compute/report/render/dict implementations — only
their CLI wiring (``_cmd_bounce_divergence.py`` /
``_cmd_do_not_email_divergence.py``) was removed.

**The JSON ``diverged`` field is redefined at this layer (issue
athenaeum#1111).** Each wrapped module's own ``report_as_dict`` reports
``diverged`` as true whenever EITHER direction of the two-surface
comparison differs — a broader notion than this command's exit code, which
only fails on the field's declared NOT-tolerated direction
(:func:`athenaeum.surface_divergence.get_field`'s
``exceeds_allowance``). Left unmodified, a caller reading ``diverged``
instead of the exit code could see ``"diverged": true`` on exit 0 (the
design's own legal steady state for ``do_not_email``) — a live trap. This
command's JSON output overrides ``diverged`` to track ``exceeds_allowance``
so it can never contradict the exit code; a direct caller of the wrapped
modules' own ``report_as_dict`` still gets their original, broader
both-directions ``diverged`` semantics unchanged.

**Why this is the "runs unattended and fails" half of the issue.** Every
prior divergence command either never failed on divergence at all
(``bounce-divergence``) or hard-coded its own single field's allowance
(``do-not-email-divergence``, correctly zero-tolerance for ITS field, but
not reusable for a field with a different, documented tolerance). This
command's default mode fails — a registered field diverging beyond its
declared allowance (:data:`athenaeum.surface_divergence.EXIT_DIVERGED`) is a
non-zero exit, full stop — and the pre-existing "just show me the numbers"
use case is preserved explicitly via ``--report-only`` rather than silently,
so an operator inspecting a store interactively is never forced into a
failure exit, and an unattended caller that does NOT pass ``--report-only``
can never mistake exit 0 for "ran successfully" irrespective of the result.

**Where and how often to run it.** Wired into ``ci.yml``'s ``test`` job via
the pytest suite (``tests/test_surface_divergence.py``) against fixture
stores on every push/PR, for both registered fields — the fixture-based
protection issue athenaeum#963's CI acceptance criterion (as amended
2026-08-20) asks for, since GitHub Actions has no access to a live store.
Against the LIVE store, this repository ships no in-repo scheduler (see
"There is no shipped nightly cron wrapper in this repo" in
``docs/reference/configuration.md``) — the in-repo registration this issue's AC asks
for is this command's documented contract itself (this docstring, the
``docs/reference/configuration.md`` operator-invocation note, and the exit-code table
in ``docs/reference/exit-codes.md``): an operator's own external cron/launchd (or the
Hestia nightly sweep in a different repo) invokes
``athenaeum surface-divergence --field <name> --path ~/knowledge`` once per
registered field and treats a non-zero, non-``EXIT_SURFACE_UNREADABLE`` exit
as an alert. Whether that external wrapper has actually been updated to add
these invocation lines is state on the operator's host, outside what this
repository's tests can observe or assert.

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


def cmd_surface_divergence(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum surface-divergence``."""
    from athenaeum.pii import contacts_surface_root
    from athenaeum.surface_divergence import EXIT_DIVERGED, EXIT_SURFACE_UNREADABLE, get_field

    try:
        spec = get_field(args.field)
    except KeyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    wiki_root = args.wiki_root or (knowledge_root / "wiki")
    contacts_root = args.contacts_root or contacts_surface_root(knowledge_root, config)

    report = spec.compute(wiki_root, contacts_root, None)

    if args.json:
        # athenaeum#1111: "diverged" is overridden to track exceeds_allowance
        # (what actually drives the exit code below), not the wrapped
        # module's own broader both-directions notion — see the module
        # docstring's "The JSON diverged field is redefined at this layer".
        payload = dict(spec.report_as_dict(report))
        payload["diverged"] = spec.exceeds_allowance(report)
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        sys.stdout.write(spec.render_report(report))

    if not spec.is_complete(report):
        return EXIT_SURFACE_UNREADABLE
    if args.report_only:
        # Pre-athenaeum#963 interactive-inspection contract: a readable
        # store always exits 0, divergence or not. Never the mode an
        # unattended caller should pass.
        return 0
    if spec.exceeds_allowance(report):
        return EXIT_DIVERGED
    return 0


def add_surface_divergence_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum surface-divergence`` on *subparsers* (issue athenaeum#963)."""
    from athenaeum.surface_divergence import field_names

    parser = subparsers.add_parser(
        "surface-divergence",
        help="Report the two-surface divergence for a REGISTERED field "
        "(wiki frontmatter vs. the contacts/excluded surface) and, by "
        "default, exit non-zero when it exceeds the field's declared "
        "allowance. Generalizes bounce-divergence / "
        "do-not-email-divergence into one per-field guard (issue "
        "athenaeum#963). Read-only; output is safe to paste publicly.",
    )
    parser.add_argument(
        "--field",
        required=True,
        choices=field_names(),
        help="Registered field to check.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge-base root to report on. Both surfaces are resolved "
        "from it (the contacts/excluded surface through the configured "
        "storage mapping).",
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
        help="Override the contacts/excluded surface root (defaults to the "
        "configured `pii` entity-class surface).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of plain text. Carries the "
        "same opaque handles — no addresses or names in either form.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Preserve the pre-athenaeum#963 exit-0-unless-unreadable "
        "contract for interactive inspection: never fail on divergence, "
        "only on an unreadable surface. Do not pass this from an "
        "unattended caller — it is exactly the inert behavior athenaeum#963 "
        "generalizes past.",
    )
    parser.set_defaults(func=cmd_surface_divergence)
