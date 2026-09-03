# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {init,status,disable,enable,spend}`` — knowledge-base lifecycle.

Five small, independent subcommands grouped here because each is a thin,
single-purpose operator command over knowledge-base or process-level state
(create the directory, report status, flip the kill switch, report spend) —
none warrants its own module, and they share no code with the compile/index
pipeline (``_cmd_index.py``) or the ``run`` pipeline (``_cmd_run.py``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` (or a small same-domain group module like this
one) and registers via ``add_<name>_subparser`` — this is where a NEW
subcommand goes, not inline in ``cli.py``'s ``main()``. This module may
import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry points, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_lifecycle_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``init``, ``status``, ``disable``, ``enable``, ``spend``."""

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Initialize a new knowledge directory"
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Target directory (default: ~/knowledge)",
    )
    init_parser.add_argument(
        "--with-templates",
        action="store_true",
        help="Also copy bundled entity-author templates "
        "(person/company/project/concept/source) into <path>/templates/.",
    )
    init_parser.add_argument(
        "--templates-dest",
        type=Path,
        default=None,
        help="Override the templates destination directory "
        "(default: <path>/templates).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing template files at the destination; "
        "no backup is created (only applies with --with-templates and/or "
        "--with-rules).",
    )
    init_parser.add_argument(
        "--with-rules",
        action="store_true",
        help="Also copy bundled EXAMPLE shape rules (issue athenaeum#901) into "
        "<path>/rules/. Every example ships 'mode: observe' -- installing "
        "them changes nothing until you review wiki/_shape_rule_dispositions.jsonl "
        "and edit a copy to 'mode: live'. See docs/shape-rules.md.",
    )
    init_parser.add_argument(
        "--rules-dest",
        type=Path,
        default=None,
        help="Override the shape-rules destination directory "
        "(default: <path>/rules).",
    )
    init_parser.set_defaults(func=cmd_init)

    # status command
    status_parser = subparsers.add_parser("status", help="Show knowledge base status")
    status_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    status_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the kill-switch state "
        "(default: ~/.cache/athenaeum). Only affects the kill-switch line.",
    )
    status_parser.set_defaults(func=cmd_status)

    # disable / enable commands — the kill switch (issue athenaeum#379)
    disable_parser = subparsers.add_parser(
        "disable",
        help="Turn athenaeum's background work off (compile, detectors, "
        "recall, notifications). Reversible with 'athenaeum enable'.",
    )
    disable_parser.add_argument(
        "--compile",
        action="store_true",
        help="Granular: stop only the expensive compile/detect pass "
        "(session-end contradiction detection); leave recall on.",
    )
    disable_parser.add_argument(
        "--reason",
        type=str,
        default=None,
        help="Optional note recorded in the state file and shown by "
        "'athenaeum status'.",
    )
    disable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )
    disable_parser.set_defaults(func=cmd_disable)

    enable_parser = subparsers.add_parser(
        "enable",
        help="Undo 'athenaeum disable' — restore all background work.",
    )
    enable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )
    enable_parser.set_defaults(func=cmd_enable)

    # spend command — report the durable LLM-spend ledger (issue athenaeum#378)
    spend_parser = subparsers.add_parser(
        "spend",
        help="Report LLM spend from the durable ledger ($ for API, tokens for "
        "subscription — never blended)",
    )
    spend_parser.add_argument(
        "--since",
        type=str,
        default="7d",
        help="Lower bound: a window (7d / 24h / 30m / 2w) or an ISO date "
        "(2026-07-01). Default: 7d.",
    )
    spend_parser.add_argument(
        "--by-model", action="store_true", help="Break down per serving model."
    )
    spend_parser.add_argument(
        "--by-provider",
        action="store_true",
        help="Break down per run type within each cost path.",
    )
    spend_parser.add_argument(
        "--by-knob",
        action="store_true",
        help="Break down per model knob (classify/write/resolve/topic/"
        "reasoning_t1/reasoning_t2).",
    )
    spend_parser.add_argument(
        "--by-surface",
        action="store_true",
        help="Break down per declared non-batched surface (C4 contradiction "
        "detector/resolver, same-page multi-merge, the truncation retry, "
        "the tier-3 full-echo fallback) plus an unattributed remainder.",
    )
    spend_parser.add_argument(
        "--reprice",
        action="store_true",
        help="Recompute historical rows from their per-model token attribution "
        "at the CURRENT rates and report the delta against the stored figures. "
        "READ-ONLY — the ledger file is never modified.",
    )
    spend_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (what /good-morning consumes).",
    )
    spend_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory for config resolution (default: ~/knowledge).",
    )
    spend_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache dir holding spend.jsonl (default: ATHENAEUM_CACHE_DIR env, "
        "else ~/.cache/athenaeum).",
    )
    spend_parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Explicit ledger file path (overrides --cache-dir and config).",
    )
    spend_parser.set_defaults(func=cmd_spend)


def cmd_init(args: argparse.Namespace) -> int:
    from athenaeum.init import copy_example_rules, copy_templates, init_knowledge_dir

    target = init_knowledge_dir(args.path)
    print(f"Initialized knowledge directory at {target}")

    if getattr(args, "with_templates", False):
        dest = args.templates_dest if args.templates_dest else target / "templates"
        dest = dest.expanduser().resolve()
        written, skipped = copy_templates(dest, force=args.force)
        for fname in written:
            print(f"  wrote   {dest / fname}")
        for fname in skipped:
            print(f"  skipped {dest / fname} (exists; pass --force to overwrite)")
    elif args.templates_dest is not None:
        print(
            "warning: --templates-dest is ignored without --with-templates; "
            "no templates were copied.",
            file=sys.stderr,
        )

    if getattr(args, "with_rules", False):
        rules_dest = args.rules_dest if args.rules_dest else target / "rules"
        rules_dest = rules_dest.expanduser().resolve()
        written, skipped = copy_example_rules(rules_dest, force=args.force)
        for fname in written:
            print(f"  wrote   {rules_dest / fname} (mode: observe)")
        for fname in skipped:
            print(f"  skipped {rules_dest / fname} (exists; pass --force to overwrite)")
    elif getattr(args, "rules_dest", None) is not None:
        print(
            "warning: --rules-dest is ignored without --with-rules; "
            "no rules were copied.",
            file=sys.stderr,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from athenaeum.killswitch import format_status_line
    from athenaeum.status import format_status, status

    # Kill-switch state (issue athenaeum#379) is process/cache state, independent of the
    # knowledge base — report it first so 'athenaeum status' always answers
    # "is it on?" even when the knowledge directory is missing.
    print(format_status_line(getattr(args, "cache_dir", None)))
    print()

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1
    info = status(target)
    print(format_status(info))
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Kill switch (athenaeum#379): stop athenaeum background work, reversibly."""
    from athenaeum import killswitch

    scope = killswitch.SCOPE_COMPILE if args.compile else killswitch.SCOPE_ALL
    path = killswitch.disable(
        scope, reason=args.reason, cache_dir=getattr(args, "cache_dir", None)
    )
    if scope == killswitch.SCOPE_COMPILE:
        print(
            "athenaeum disabled (compile): the session-end compile/detect pass "
            "is off; recall stays on."
        )
    else:
        print(
            "athenaeum disabled: all background work is off — compile, "
            "contradiction detection, recall, and notifications."
        )
    print(f"State: {path}")
    print("Re-enable with: athenaeum enable")

    env_state = killswitch.current_state(getattr(args, "cache_dir", None))
    if env_state.source == "env" and env_state.scope != scope:
        print(
            f"note: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"overrides the state file (effective scope: {env_state.scope}).",
            file=sys.stderr,
        )
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Kill switch (athenaeum#379): undo 'athenaeum disable'."""
    from athenaeum import killswitch

    removed = killswitch.enable(cache_dir=getattr(args, "cache_dir", None))
    if removed:
        print("athenaeum enabled: background work restored.")
    else:
        print("athenaeum was already enabled (no state file to remove).")

    env = killswitch.current_state(getattr(args, "cache_dir", None))
    if env.source == "env":
        print(
            f"warning: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"still forces disabled (scope: {env.scope}); unset it to fully "
            "re-enable.",
            file=sys.stderr,
        )
        return 0
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    """Report LLM spend from the durable ledger (issue athenaeum#378).

    Separates real API DOLLARS from subscription TOKENS — never a blended
    figure. ``--json`` emits the machine-readable shape ``/good-morning``
    consumes; the human default renders the two paths as distinct rows.

    ``--reprice`` (issue athenaeum#788) switches the report to recompute each row
    from its stored per-model token attribution at the CURRENT rate table —
    including the operator's ``athenaeum.yaml`` ``pricing:`` overrides (issue
    athenaeum#783) — and show the delta against the stored figures. Strictly
    READ-ONLY: it opens the ledger for reading only, exactly as the default
    report does.
    """
    import json

    from athenaeum import spend
    from athenaeum.config import load_config, resolve_cache_dir
    from athenaeum.store import now_iso

    target = args.path.expanduser().resolve()
    config = load_config(target) if target.exists() else None

    if args.ledger is not None:
        ledger_path = args.ledger.expanduser().resolve()
    else:
        ledger_path = spend.resolve_ledger_path(
            config,
            cache_dir=resolve_cache_dir(args.cache_dir).resolve(),
            wiki_root=target / "wiki",
        )

    try:
        since_dt = spend.parse_since(args.since)
    except ValueError:
        print(
            f"Invalid --since value: {args.since!r} — use a window (7d / 24h / "
            f"30m / 2w) or an ISO date (2026-07-01).",
            file=sys.stderr,
        )
        return 2

    records = spend.read_ledger(ledger_path, since=since_dt)

    if getattr(args, "reprice", False):
        # Issue athenaeum#788: reprice against the rate table the operator is on
        # NOW, which means installing their `athenaeum.yaml` `pricing:` section
        # (issue athenaeum#783) first — same REPLACE-wholesale call `athenaeum run`
        # makes. Without this the reprice would silently read the code-default
        # table and report a "current" figure that is not the operator's.
        # Deliberately NOT gated behind preflight_model_rates: that preflight
        # fails a RUN loudly on an unpriced model it is about to use, whereas a
        # historical row naming a model the current table does not price is a
        # legitimate thing to report on, not a reason to refuse the report.
        from athenaeum.config import resolve_model_rates
        from athenaeum.models import configure_model_rates

        configure_model_rates(resolve_model_rates(config))
        repriced = spend.reprice(records)
        if args.json:
            print(
                json.dumps(
                    {
                        "since": now_iso(since_dt),
                        "ledger_path": str(ledger_path),
                        "reprice": repriced,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(spend.format_reprice(repriced, since_label=args.since))
        return 0

    summary = spend.summarize(
        records,
        by_model=args.by_model,
        by_provider=args.by_provider,
        by_knob=args.by_knob,
        by_surface=args.by_surface,
    )

    # Issue athenaeum#1135 (AC2/6): an ADDITIONAL day-scoped figure alongside the
    # ``--since``-window totals above — today's spend against the SAME
    # per-day ceiling ``athenaeum run``'s budget check (spend.ceiling_tripped)
    # actually enforces, so the report agrees with what would stop a run
    # right now. Never replaces the existing totals or narrows --since (see
    # spend.budget_window_status's docstring for why). Best-effort: a
    # failure here must not crash the whole report over an ADDITIVE section.
    try:
        budget_window = spend.budget_window_status(config, ledger_path=ledger_path)
    except Exception:  # noqa: BLE001 — additive section, never breaks the report
        budget_window = None

    # Issue athenaeum#1147 AC9: committed-but-unbilled batch spend. A batch is
    # paid for the moment it is submitted, but ``add_batch_tokens`` only books
    # it at COLLECT — so between a submit run and its collect run there is real
    # money the ledger above cannot see. Surfacing it here means an operator
    # can answer "what have I committed?" without hand-tracing the reservation
    # ledger. Additive and best-effort, exactly like the budget window above.
    try:
        outstanding = spend.outstanding_reservations(
            target / "wiki",
            cache_dir=resolve_cache_dir(args.cache_dir).resolve(),
        )
    except Exception:  # noqa: BLE001 — additive section, never breaks the report
        outstanding = []

    if args.json:
        payload = {
            "since": now_iso(since_dt),
            "ledger_path": str(ledger_path),
            **summary,
        }
        if budget_window is not None:
            payload["budget_window"] = budget_window
        payload["outstanding_reservations"] = {
            "count": len(outstanding),
            "est_usd": round(
                sum(float(r.get("est_usd") or 0.0) for r in outstanding), 6
            ),
            "batches": outstanding,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            spend.format_summary(
                summary,
                since_label=args.since,
                by_model=args.by_model,
                by_provider=args.by_provider,
                by_knob=args.by_knob,
                by_surface=args.by_surface,
            )
        )
        budget_window_line = (
            spend.format_budget_window(budget_window)
            if budget_window is not None
            else None
        )
        if budget_window_line:
            print(budget_window_line)
        if outstanding:
            total = sum(float(r.get("est_usd") or 0.0) for r in outstanding)
            print(
                f"in flight: {len(outstanding)} batch(es) committed but not yet "
                f"billed, est. ${total:.2f} (athenaeum#1147)"
            )
            for record in outstanding:
                print(
                    f"  {record.get('batch_id')} "
                    f"[{record.get('knob')}] "
                    f"reserved {record.get('day')} "
                    f"est. ${float(record.get('est_usd') or 0.0):.2f}"
                )
    return 0
