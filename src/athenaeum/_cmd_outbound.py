# SPDX-License-Identifier: Apache-2.0
"""``athenaeum outbound-lint`` — outbound-draft PII lint CLI (issue #455).

Mirrors ``athenaeum authority`` / ``athenaeum merges`` in shape: a thin CLI
dispatcher over the library functions in :mod:`athenaeum.outbound_pii`, with no
detection logic of its own.

Reads the outbound-destined text from ``--text``, ``--file``, or stdin (the
default), scans it for emails/phones, and either reports the findings
(flag-only, the default) or prints the redacted text (``--redact``, strip
mode). ``--allow`` / ``--allowlist-file`` supply addresses already known to the
recipient, which are dropped from the report. Offline and deterministic — no
network, no live-store access, no LLM call.

Exit codes (flag mode): ``0`` — no PII found; :data:`EXIT_PII_FOUND` (2) — PII
found. The non-zero-on-found convention lets a shell gate a send on a clean
scan (``athenaeum outbound-lint --file draft.txt && send draft.txt``). In
``--redact`` mode the command always exits ``0`` on success — it produced
sanitized output, which is the whole point — and reports the redaction count on
stderr.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.outbound_pii import (
    Allowlist,
    lint_outbound_text,
)

#: Exit code when flag-only mode finds PII (mirrors the repo's dry-run-found
#: (2) convention — a "found something to act on" signal distinct from the
#: generic error code 1).
EXIT_PII_FOUND = 2


def _read_input(args: argparse.Namespace) -> str:
    """Resolve the text to scan from --text, --file, or stdin (in that order)."""
    if args.text is not None:
        return args.text
    if args.file is not None:
        return args.file.read_text(encoding="utf-8")
    return sys.stdin.read()


def _build_allowlist(args: argparse.Namespace) -> Allowlist:
    """Combine --allow entries and an --allowlist-file into one Allowlist."""
    entries: list[str] = list(args.allow or [])
    if args.allowlist_file is not None:
        entries.extend(args.allowlist_file.read_text(encoding="utf-8").splitlines())
    return Allowlist.from_entries(entries)


def cmd_outbound(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum outbound-lint``."""
    text = _read_input(args)
    allowlist = _build_allowlist(args)
    result = lint_outbound_text(text, allowlist=allowlist, redact=args.redact)

    if args.redact:
        # Strip mode: sanitized text is the product. Emit it on stdout; report
        # the count on stderr so a pipe consuming stdout gets clean text only.
        sys.stdout.write(result.redacted)
        print(
            f"redacted {len(result.findings)} PII finding(s)",
            file=sys.stderr,
        )
        return 0

    if args.json:
        payload = [
            {
                "kind": f.kind,
                "value": f.value,
                "start": f.start,
                "end": f.end,
                "line": f.line,
                "column": f.column,
            }
            for f in result.findings
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return EXIT_PII_FOUND if result.has_findings else 0

    if not result.has_findings:
        print("0 PII findings")
        return 0

    print(f"{len(result.findings)} PII finding(s):")
    for f in result.findings:
        print(f"  {f.line}:{f.column} [{f.kind}] {f.value}")
    return EXIT_PII_FOUND


def add_outbound_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum outbound-lint`` on ``subparsers`` (issue #455)."""
    parser = subparsers.add_parser(
        "outbound-lint",
        help="Scan outbound-destined text for PII (emails/phones) before it "
        "ships; flag findings (default) or --redact them. Offline, "
        "deterministic (issue #455).",
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--text",
        default=None,
        help="Text to scan, given inline. Mutually exclusive with --file; if "
        "neither is given, text is read from stdin.",
    )
    src.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to a file whose contents are scanned. Mutually exclusive "
        "with --text.",
    )

    parser.add_argument(
        "--allow",
        action="append",
        metavar="ADDRESS",
        help="An email or phone number already known to the recipient, which "
        "is dropped from the report. Repeatable.",
    )
    parser.add_argument(
        "--allowlist-file",
        dest="allowlist_file",
        type=Path,
        default=None,
        help="Path to a file with one allowlisted address per line.",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Strip mode: print the text with each finding replaced by a "
        "redaction placeholder (to stdout) instead of reporting findings.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON findings instead of plain text "
        "(ignored in --redact mode).",
    )
    parser.set_defaults(func=cmd_outbound)
