# SPDX-License-Identifier: Apache-2.0
"""``athenaeum contact-surface-lint`` — read-only contact-surface report (issue athenaeum#2218).

Thin CLI dispatcher over :mod:`athenaeum.contact_surface_lint`, mirroring
``athenaeum fold-lint`` / ``athenaeum outbound-lint``'s shape (no detection
logic of its own). Child I of epic athenaeum#2192, design
``docs/design/attribution-verification.md`` §10.4. Prints COUNTS AND UIDS
ONLY — never a contact value: a test asserts no fixture value appears in
this command's output in either mode.

Three base reports, always produced (``pageless_records`` / ``shared_values``
/ ``observed_source_mismatch``), and two opt-in extensions:

- ``--attribution``: runs the seam in report mode over every (value, uid)
  pair on the surface, prints per-check-id and per-question-class counts,
  and persists one observe-report row (design §9.3) — see the library
  module's docstring for exactly what each writes.
- ``--attribution --escalate``: additionally runs athenaeum#2212's shadow
  escalation sweep over the suspects already parked on the surface, capped
  by :func:`athenaeum.librarian.librarian_max_api_calls` (override with
  ``--max-escalations``).

**Deliberately does NOT take the run lock (no `_acquire_or_exit` call).**
This is a considered choice, not an oversight: the command never writes a
contact record or any wiki page — the ONLY writes it can make (the
attribution-log rows and the one observe-report row, both append-only, both
only when ``--attribution`` is given) land on the excluded surface via
primitives (``append_line_durable``) already safe under concurrent
appenders, exactly like every real writer's own attribution-log row during
a live compile run. ``fold-lint`` and ``outbound-lint`` — this command's
two stated shape-mirrors — are both lock-free for the same reason: a
read-only (or append-only-evidence) report does not contend with a compile
run the way a record WRITE would. Consequently this command is not named in
``docs/reference/exit-codes.md`` (that doc's `75`/``EXIT_LOCK_HELD`` section
enumerates only `_acquire_or_exit` callers) and `tests/test_exit_codes_doc_parity.py`'s
counts are unaffected.

Exit codes (issue athenaeum#2218's own AC, not the repo's generic `run()`
table — this command keeps its own small contract, the same way
``athenaeum entity`` does, per ``docs/reference/exit-codes.md``'s own
"a caller checking one command's codes doesn't miss that another command's
`1` means something else" framing):

    0 -- clean: nothing in any of the base reports, and (when ``--attribution``
         ran) no suspect verdict.
    1 -- findings: the surface (or the attribution sweep) found something to
         report.
    2 -- error: the ``pii`` class is not mapped to an excluded surface, the
         resolved contacts root IS the wiki root, or any other setup/read
         failure.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config
from athenaeum.contact_surface_lint import (
    ContactSurfaceLintError,
    ContactSurfaceLintResult,
    run_contact_surface_lint,
)
from athenaeum.librarian import librarian_max_api_calls
from athenaeum.models import TokenUsage
from athenaeum.provider import build_llm_client

#: Exit code when a finding is reported (the issue's own AC5: "1 means
#: findings" — distinct from this repo's more common 0/2 "found something"
#: convention, because this command's own AC spells out all three codes).
EXIT_FINDINGS = 1
#: Exit code for a setup/read error (the issue's own AC5).
EXIT_ERROR = 2


def _surface_payload(result: ContactSurfaceLintResult) -> dict[str, Any]:
    surface = result.surface
    payload: dict[str, Any] = {
        "ok": result.ok,
        "counts": surface.counts(),
        "pageless_records": {"uids": list(surface.pageless_uids)},
        "shared_values": {
            "by_source_type": surface.shared_value_source_type_counts,
            "entries": [
                {
                    "value_key": entry.value_key,
                    "uids": list(entry.uids),
                    "source_types": dict(entry.source_types),
                }
                for entry in surface.shared_values
            ],
        },
        "observed_source_mismatch": [
            {
                "value_key": entry.value_key,
                "uid": entry.uid,
                "source_ref": entry.source_ref,
            }
            for entry in surface.observed_source_mismatch
        ],
    }
    if result.attribution is not None:
        payload["attribution"] = {
            "pairs_considered": result.attribution.pairs_considered,
            "by_disposition": result.attribution.by_disposition,
            "by_check": result.attribution.by_check,
            "by_question_class": result.attribution.by_question_class,
        }
    if result.observe_report is not None:
        payload["observe_report"] = {
            "report_id": result.observe_report.report_id,
            "ts": result.observe_report.ts,
            "per_class_shadow_counts": result.observe_report.per_class_shadow_counts,
            "model_by_class": result.observe_report.model_by_class,
            "prompt_sha256_by_class": result.observe_report.prompt_sha256_by_class,
        }
    if result.escalation is not None:
        esc = result.escalation
        payload["escalation"] = {
            "considered": esc.considered,
            "escalated": esc.escalated,
            "proposed": esc.proposed,
            "queued": esc.queued,
            "memo_hits": esc.memo_hits,
            "skipped_not_suspect": esc.skipped_not_suspect,
            "skipped_already_answered": esc.skipped_already_answered,
            "skipped_over_cap": esc.skipped_over_cap,
        }
    return payload


def _print_report(result: ContactSurfaceLintResult) -> None:
    surface = result.surface
    print("=== contact-surface-lint ===")
    for key, value in surface.counts().items():
        print(f"  {key}: {value}")
    if surface.pageless_uids:
        print("  PAGELESS UIDS (no live page, no merge record):")
        for uid in surface.pageless_uids:
            print(f"    {uid}")
    if surface.shared_values:
        print("  SHARED VALUES by source type:")
        for source_type, count in sorted(surface.shared_value_source_type_counts.items()):
            print(f"    {source_type}: {count}")
        print("  SHARED VALUES (value_key, uids):")
        for shared_entry in surface.shared_values:
            print(f"    {shared_entry.value_key}: {', '.join(shared_entry.uids)}")
    if surface.observed_source_mismatch:
        print("  OBSERVED SOURCE MISMATCH (value_key, uid, source):")
        for mismatch_entry in surface.observed_source_mismatch:
            print(
                f"    {mismatch_entry.value_key} uid={mismatch_entry.uid} "
                f"source={mismatch_entry.source_ref!r}"
            )

    if result.attribution is not None:
        print("=== attribution report (writer=lint) ===")
        print(f"  pairs considered: {result.attribution.pairs_considered}")
        print("  by disposition:")
        for disposition, count in sorted(result.attribution.by_disposition.items()):
            print(f"    {disposition}: {count}")
        print("  by check:")
        for check_id, count in sorted(result.attribution.by_check.items()):
            print(f"    {check_id}: {count}")
        print("  by question class:")
        for question_class, count in sorted(result.attribution.by_question_class.items()):
            print(f"    {question_class}: {count}")

    if result.observe_report is not None:
        report = result.observe_report
        print(f"=== observe report {report.report_id} ({report.ts}) ===")
        for question_class, count in sorted(report.per_class_shadow_counts.items()):
            model = report.model_by_class.get(question_class, "")
            sha = report.prompt_sha256_by_class.get(question_class, "")
            print(f"    {question_class}: shadow={count} model={model} prompt_sha256={sha}")

    if result.escalation is not None:
        esc = result.escalation
        print("=== shadow escalation ===")
        print(
            f"  considered={esc.considered} escalated={esc.escalated} "
            f"proposed={esc.proposed} queued={esc.queued} memo_hits={esc.memo_hits}"
        )
        print(
            f"  skipped_not_suspect={esc.skipped_not_suspect} "
            f"skipped_already_answered={esc.skipped_already_answered} "
            f"skipped_over_cap={esc.skipped_over_cap}"
        )

    print("OK — nothing to report" if result.ok else "FINDINGS — see report above")


def cmd_contact_surface_lint(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum contact-surface-lint``."""
    knowledge_root = Path(args.path).expanduser().resolve()
    config = load_config(knowledge_root)

    client = None
    usage = None
    escalate_limit = None
    if args.escalate:
        if not args.attribution:
            print("error: --escalate requires --attribution", file=sys.stderr)
            return EXIT_ERROR
        client = build_llm_client(config, knob="attribution")
        usage = TokenUsage()
        escalate_limit = (
            args.max_escalations
            if args.max_escalations is not None
            else librarian_max_api_calls(config)
        )

    try:
        result = run_contact_surface_lint(
            knowledge_root,
            config,
            contacts_root=args.contacts_root,
            run_attribution=args.attribution,
            escalate=args.escalate,
            client=client,
            usage=usage,
            escalate_limit=escalate_limit,
        )
    except ContactSurfaceLintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        sys.stdout.write(json.dumps(_surface_payload(result)) + "\n")
    else:
        _print_report(result)

    return 0 if result.ok else EXIT_FINDINGS


def add_contact_surface_lint_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum contact-surface-lint`` on *subparsers* (issue athenaeum#2218)."""
    parser = subparsers.add_parser(
        "contact-surface-lint",
        help=(
            "Read-only contact-surface report (issue athenaeum#2218): page-less "
            "records, shared values, and observed-source mismatches. "
            "--attribution adds a report-mode attribution backfill; "
            "--attribution --escalate adds the shadow escalation sweep. "
            "Prints counts and uids only, never a value."
        ),
    )
    parser.set_defaults(func=cmd_contact_surface_lint)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--contacts-root",
        type=Path,
        default=None,
        help="Override the resolved excluded contacts surface root.",
    )
    parser.add_argument(
        "--attribution",
        action="store_true",
        help="Run the attribution seam in report mode over every (value, uid) "
        "pair on the surface; writes attribution-log rows (writer=lint) and "
        "one observe-report record.",
    )
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="With --attribution: also run the shadow escalation sweep over "
        "already-parked suspects. Opt-in; makes model calls.",
    )
    parser.add_argument(
        "--max-escalations",
        type=int,
        default=None,
        help="Override the --escalate call cap (default: "
        "librarian_max_api_calls).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )


__all__ = [
    "EXIT_FINDINGS",
    "EXIT_ERROR",
    "cmd_contact_surface_lint",
    "add_contact_surface_lint_subparser",
]
