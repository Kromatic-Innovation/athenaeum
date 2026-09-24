# SPDX-License-Identifier: Apache-2.0
"""``athenaeum paste-cleanup`` — tier-0 attributed-paste cleanup pass CLI
(issue athenaeum#1717).

Presentation for :mod:`athenaeum.paste_cleanup`. Dry-run by default and
``--apply`` to write, mirroring ``audit``/``_cmd_audit.py``.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _read_uids_file(path: Path) -> list[str]:
    uids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            uids.append(line)
    return uids


def cmd_paste_cleanup(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum paste-cleanup``."""
    from athenaeum.paste_cleanup import (
        PASTE_CLEANUP_VERSION,
        apply_paste_cleanup_report,
        build_paste_cleanup_report,
    )

    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    uids: list[str] | None = None
    if args.uids is not None:
        uids_path = Path(args.uids)
        if not uids_path.is_file():
            print(f"error: --uids file not found: {uids_path}", file=sys.stderr)
            return 1
        uids = _read_uids_file(uids_path)

    from athenaeum.config import (
        DEFAULT_CLASSIFY_MODEL,
        DEFAULT_VERIFY_MODEL,
        load_config,
        resolve_model,
    )

    config = load_config(knowledge_root)
    model = args.model or resolve_model(
        "classify", "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config
    )
    verify_model = args.verify_model or resolve_model(
        "verify", "ATHENAEUM_VERIFY_MODEL", DEFAULT_VERIFY_MODEL, config
    )

    client: Any = None
    verify_client: Any = None
    if not args.mechanical_dry_run:
        from athenaeum.provider import build_llm_client

        client = build_llm_client(config, knob="classify")
        verify_client = build_llm_client(config, knob="verify") or client
        if client is None:
            print(
                "error: no LLM client is configured for the 'classify' knob "
                "(check llm.provider / ANTHROPIC_API_KEY)",
                file=sys.stderr,
            )
            return 1

    lock: "RunLock | None" = None
    if args.apply:
        acquired = _acquire_or_exit(knowledge_root, args, config)
        if isinstance(acquired, int):
            return acquired
        lock = acquired

    try:
        report = build_paste_cleanup_report(
            wiki_root,
            client=client,
            verify_client=verify_client,
            model=model,
            verify_model=verify_model,
            verify_rule=args.verify_rule,
            limit=args.limit,
            sample=args.sample,
            seed=args.seed,
            uids=uids,
            config=config,
        )

        changed = apply_paste_cleanup_report(report, wiki_root) if args.apply else 0

        if args.json:
            payload = report.to_dict()
            payload["applied"] = args.apply
            payload["files_changed"] = changed
            sys.stdout.write(json.dumps(payload) + "\n")
            return 0

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== athenaeum paste-cleanup ({mode}, {PASTE_CLEANUP_VERSION}) ===")
        print(report.render_text())
        print(
            f"applied: {changed} file(s) written"
            if args.apply
            else "dry run: nothing written (pass --apply to write)"
        )
        return 0
    finally:
        if lock is not None:
            lock.release()


def add_paste_cleanup_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``paste-cleanup``."""
    parser = subparsers.add_parser(
        "paste-cleanup",
        help="Tier-0 attributed-paste cleanup pass over person pages' ## Notes "
        "bullets (issue athenaeum#1717): a cheap-model proposer classifies "
        "keep/rewrite/remove, a stronger model verifies a sample. Dry-run by "
        "default; --apply writes.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write remove/rewrite verdicts. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Consider at most N pages this pass.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Consider a stratified random sample of N pages instead of the "
        "whole corpus. Combine with --seed for reproducibility.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for --sample (default: 0).",
    )
    parser.add_argument(
        "--uids",
        type=Path,
        default=None,
        help="Path to a file listing one page uid per line; consider exactly those pages.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the proposer model (default: the 'classify' knob's resolved model).",
    )
    parser.add_argument(
        "--verify-model",
        type=str,
        default=None,
        dest="verify_model",
        help="Override the verifier model (default: the 'verify' knob's resolved model).",
    )
    parser.add_argument(
        "--verify-rule",
        type=str,
        choices=("sampled", "all"),
        default="sampled",
        dest="verify_rule",
        help="'sampled' verifies every low-confidence proposal plus a fixed "
        "10%% stable sample of the rest; 'all' verifies every proposal "
        "(issue athenaeum#1717's own AC: use 'all' when measured agreement "
        "is below 90%%).",
    )
    parser.add_argument(
        "--mechanical-dry-run",
        action="store_true",
        dest="mechanical_dry_run",
        help="Skip building an LLM client entirely. For CI/offline smoke checks only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_paste_cleanup)
