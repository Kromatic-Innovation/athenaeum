# SPDX-License-Identifier: Apache-2.0
"""``athenaeum audit`` — page audit pass CLI (issue athenaeum#1624).

Presentation for :mod:`athenaeum.audit`. Dry-run by default and ``--apply``
to write, matching ``decay-sweep`` / ``description backfill``.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _load_cache_config_env() -> None:
    """Load ``<cache_dir>/config.env`` into the process env (issue athenaeum#1667
    Plan item 10; issue athenaeum#1661 tracks the same gap for the hook adapter).

    ``KEY=VALUE`` lines only; blank lines and ``#``-comments are skipped.
    Existing process env always wins — a key already set (by the caller's
    shell, or set earlier this same process) is never overwritten. A
    missing file is a silent no-op. Values are NEVER printed or logged.
    """
    from athenaeum.config import resolve_cache_dir

    config_env_path = resolve_cache_dir() / "config.env"
    try:
        if not config_env_path.is_file():
            return
        lines = config_env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _read_uids_file(path: Path) -> list[str]:
    uids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            uids.append(line)
    return uids


def cmd_audit(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum audit``."""
    from athenaeum.audit import AUDIT_VERSION, apply_audit_report, build_audit_report

    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    if args.stale:
        return _cmd_audit_stale(args, wiki_root)

    uids: list[str] | None = None
    if args.uids is not None:
        uids_path = Path(args.uids)
        if not uids_path.is_file():
            print(f"error: --uids file not found: {uids_path}", file=sys.stderr)
            return 1
        uids = _read_uids_file(uids_path)

    from athenaeum.config import DEFAULT_CLASSIFY_MODEL, load_config, resolve_model

    config = load_config(knowledge_root)
    model = args.model or resolve_model(
        "classify", "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config
    )

    _load_cache_config_env()

    client: Any = None
    if not args.mechanical_dry_run:
        from athenaeum.provider import build_llm_client

        client = build_llm_client(config, knob="classify")
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
        report = build_audit_report(
            wiki_root,
            client=client,
            model=model,
            audit_version=AUDIT_VERSION,
            limit=args.limit,
            sample=args.sample,
            seed=args.seed,
            uids=uids,
            use_batch=args.batch,
            config=config,
        )

        changed = apply_audit_report(report, wiki_root) if args.apply else 0

        if args.json:
            payload = report.to_dict()
            payload["applied"] = args.apply
            payload["files_changed"] = changed
            sys.stdout.write(json.dumps(payload) + "\n")
            return 0

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== athenaeum audit ({mode}) ===")
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


def _cmd_audit_stale(args: argparse.Namespace, wiki_root: Path) -> int:
    """``athenaeum audit --stale`` — the read-only stale-page review report
    (issue athenaeum#1630 Plan item 1).

    A flag on the existing ``audit`` command rather than a nested ``audit
    stale`` subcommand: this CLI's own factoring rule (``cli.py``'s module
    docstring) is one ``add_*_subparser`` per command with NO precedent for
    nested/verb subcommands anywhere in the tree (every related-command
    group — e.g. ``_cmd_lifecycle``'s init/status/disable/enable/spend —
    registers each as its own FLAT top-level command, never nested under a
    shared parent token). A flag on ``audit`` also lets this report reuse
    ``--path``/``--json``/``--limit`` verbatim rather than re-declaring
    them on a second parser.

    Read-only: never builds an LLM client, never acquires the run lock,
    never touches ``--apply``/``--batch``/``--sample``/``--seed``/``--uids``.
    """
    from athenaeum.audit_queue import compute_stale_pages, render_stale_table
    from athenaeum.config import load_config, resolve_audit_stale_after_days

    config = load_config(_resolve_knowledge_root(args))
    stale_after_days = resolve_audit_stale_after_days(config)
    entries = compute_stale_pages(wiki_root, stale_after_days=stale_after_days)
    if args.limit is not None:
        entries = entries[: args.limit]

    if args.json:
        payload = {
            "stale_after_days": stale_after_days,
            "count": len(entries),
            "pages": [e.to_dict() for e in entries],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    print(f"=== athenaeum audit --stale (stale_after_days={stale_after_days}) ===")
    print(render_stale_table(entries))
    return 0


def add_audit_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``audit``."""
    parser = subparsers.add_parser(
        "audit",
        help="Read-and-reason audit pass over wiki pages: stamps last_audited/"
        "audit_version, fills determinable valid_from/valid_until/claimed_scope "
        "(issue athenaeum#1624). Dry-run by default; --apply writes.",
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
        help="Write last_audited/audit_version and any determinable coordinate "
        "fills/undeterminable markers. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Audit at most N pages this pass.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Audit a stratified (by type:) random sample of N pages instead "
        "of the whole corpus. Combine with --seed for reproducibility.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for --sample (default: 0). The same seed over the same "
        "corpus always selects the same pages.",
    )
    parser.add_argument(
        "--uids",
        type=Path,
        default=None,
        help="Path to a file listing one page uid per line; audit exactly those pages.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Route audit calls through the Batch API transport "
        "(athenaeum.batch.execute_batch) instead of one synchronous call per page.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the model (default: the 'classify' knob's resolved model).",
    )
    parser.add_argument(
        "--mechanical-dry-run",
        action="store_true",
        dest="mechanical_dry_run",
        help="Skip building an LLM client entirely (every page reported "
        "no-llm-client). For CI/offline smoke checks only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    parser.add_argument(
        "--stale",
        action="store_true",
        help="Report mode (issue athenaeum#1630): list pages whose last_audited "
        "is missing/older than audit.stale_after_days, or whose audit_version "
        "is behind the current version, oldest/never-audited first. Read-only "
        "-- ignores --apply/--batch/--sample/--seed/--uids/--mechanical-dry-run; "
        "honors --path/--json/--limit.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_audit)
