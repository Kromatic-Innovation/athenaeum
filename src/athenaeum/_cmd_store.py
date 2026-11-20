# SPDX-License-Identifier: Apache-2.0
"""``athenaeum store access check`` -- declared rights against local config and evidence.

Thin CLI dispatcher over :mod:`athenaeum.store_access_check`; read-only (access S5,
design note ``federation-store-access.md`` §9). Exit codes: ``0`` -- no difference
and no refusal; :data:`EXIT_FINDINGS` (2) -- at least one difference or refusal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config, resolve_owner_asserter
from athenaeum.store_access_check import AccessReport, check_access
from athenaeum.store_access_evidence import load_evidence

EXIT_FINDINGS = 2


def _render(report: AccessReport) -> str:
    if not report.stores:
        return "no mounted store has a declaration\n"
    lines: list[str] = []
    for s in report.stores:
        lines.append(f"store {s.store} ({s.store_id or 'no valid declaration'})")
        lines.append(f"  rights: {', '.join(s.rights) or 'none'}")
        lines.append(f"  read_only: {str(s.read_only).lower()}")
        for loc in s.locations:
            detail = f" ({loc.push_detail.splitlines()[0]})" if loc.push_detail else ""
            lines.append(
                f"  {loc.role} {loc.where}: fetch: {loc.fetch}; push: {loc.push}{detail}"
            )
        lines.extend(f"  DIFFERENCE: {d}" for d in s.differences)
        lines.extend(f"  REFUSED: {r}" for r in s.refusals)
    lines.append("OK" if report.ok else "differences or refusals found")
    return "\n".join(lines) + "\n"


def cmd_store_access_check(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum store access check``."""
    root = _resolve_knowledge_root(args)
    config = load_config(root)
    report = check_access(root, config, resolve_owner_asserter(config), load_evidence())
    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
    else:
        sys.stdout.write(_render(report))
    return 0 if report.ok else EXIT_FINDINGS


def cmd_store(args: argparse.Namespace) -> int:
    parser: argparse.ArgumentParser = args._store_parser
    parser.print_help()
    return 1


def add_store_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum store`` and ``store access check`` on *subparsers*."""
    store = subparsers.add_parser(
        "store",
        help=(
            "Store access: 'store access check' compares each declared store's rights "
            "with local config and this install's last fetch and push. Read-only."
        ),
    )
    store.set_defaults(func=cmd_store, _store_parser=store)
    access = store.add_subparsers(dest="store_target").add_parser(
        "access", help="Access to declared stores."
    )
    access.set_defaults(func=cmd_store, _store_parser=access)
    check = access.add_subparsers(dest="access_target").add_parser(
        "check",
        help=(
            "Read-only: report declared rights, local read_only, the recorded last fetch "
            "and push per location, and any difference or refused declaration. Exits 2 "
            "on any finding."
        ),
    )
    check.set_defaults(func=cmd_store_access_check)
    check.add_argument(
        "--path", type=Path, default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    check.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of plain text."
    )
