# SPDX-License-Identifier: Apache-2.0
"""``athenaeum corrections-trust-dryrun`` — the trust-graph flip report.

Issue athenaeum#2331 (TG3): a READ-ONLY command that lists every tier-0 field
correction whose verdict would change if the trust edges already declared
in ``librarian.source_trust_edges`` were consulted (`docs/design/
trust-graph.md`, the TG3 slice). It works whether or not
``librarian.source_trust_enabled`` is currently on, and never writes to
the store — see :func:`athenaeum.corrections.run_trust_dryrun`, which does
all the actual work via two ``dry_run=True`` passes. This module is
presentation only: argv parsing, formatting, exit code.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring. No nested sub-subcommand scheme is invented
here; ``corrections-trust-dryrun`` is one flat top-level verb, the same
shape as ``decay-sweep`` or ``correct-notes``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import load_config

if TYPE_CHECKING:
    from athenaeum.corrections import TrustDryRunReport


def _render_text(report: "TrustDryRunReport") -> str:
    lines: list[str] = []
    if report.has_evidence_kind_scoped_edge:
        lines.append(
            "NOTE: one or more configured edges name a `domain.evidence_kind` "
            "(e.g. `document`). Per docs/design/trust-graph.md §2.3.1, neither "
            "tier-0 field corrections (seam 1) nor tier-0 list removal (seam 2) "
            "thread an evidence kind through today -- every claim's evidence "
            "kind is `unknown` at these seams, and an edge that NAMES a kind "
            "never matches an unknown-kind claim. Such edges WILL report zero "
            "flips below even if correctly written -- that is not the same as "
            "\"this edge has no effect on your data\". See the TG2/TG3 "
            "follow-up named in that section before concluding an edge is "
            "inert."
        )
        lines.append("")
    if not report.edges:
        lines.append("0 trust edges configured (librarian.source_trust_edges is empty).")
    else:
        lines.append(f"{len(report.edges)} trust edge(s) configured:")
        for i, edge in enumerate(report.edges):
            lines.append(f"  edge[{i}]: {edge.describe()}")
    lines.append("")
    if not report.flips:
        lines.append("0 verdicts would change with these edges enabled.")
        return "\n".join(lines)
    lines.append(f"{len(report.flips)} verdict(s) would change:")
    for flip in report.flips:
        lines.append("")
        lines.append(f"- {flip.batch_path}: correction_id={flip.correction_id}")
        lines.append(f"  target={flip.target!r} op={flip.op!r} field={flip.field!r}")
        lines.append(f"  today:      {flip.baseline_disposition} ({flip.baseline_reason})")
        lines.append(f"  if enabled: {flip.candidate_disposition} ({flip.candidate_reason})")
    return "\n".join(lines)


def _render_json(report: "TrustDryRunReport") -> str:
    payload = {
        "edges": [edge.describe() for edge in report.edges],
        "has_evidence_kind_scoped_edge": report.has_evidence_kind_scoped_edge,
        "flips": [
            {
                "batch_path": str(flip.batch_path),
                "correction_id": flip.correction_id,
                "target": flip.target,
                "op": flip.op,
                "field": flip.field,
                "baseline_disposition": flip.baseline_disposition,
                "baseline_reason": flip.baseline_reason,
                "candidate_disposition": flip.candidate_disposition,
                "candidate_reason": flip.candidate_reason,
            }
            for flip in report.flips
        ],
    }
    return json.dumps(payload)


def cmd_corrections_trust_dryrun(args: argparse.Namespace) -> int:
    """Handler for ``athenaeum corrections-trust-dryrun``."""
    from athenaeum.corrections import run_trust_dryrun

    knowledge_root = _resolve_knowledge_root(args)
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    config = load_config(knowledge_root)

    report = run_trust_dryrun(
        raw_root=raw_root, wiki_root=wiki_root, knowledge_root=knowledge_root, config=config
    )

    if args.json:
        sys.stdout.write(_render_json(report) + "\n")
    else:
        print(_render_text(report))
    return 0


def add_corrections_trust_dryrun_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum corrections-trust-dryrun``."""
    parser = subparsers.add_parser(
        "corrections-trust-dryrun",
        help=(
            "Read-only: list every tier-0 field correction whose verdict would "
            "change if librarian.source_trust_edges were enabled (issue "
            "athenaeum#2331, TG3). Never mutates the store; works with the flag "
            "off."
        ),
    )
    parser.set_defaults(func=cmd_corrections_trust_dryrun)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
