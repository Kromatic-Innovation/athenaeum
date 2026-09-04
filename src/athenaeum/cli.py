# SPDX-License-Identifier: Apache-2.0
"""Athenaeum CLI entry point — L5 presentation layer.

Contract: :func:`build_parser` builds one ``argparse.ArgumentParser``, wiring
EVERY subcommand's subparser via a sibling ``_cmd_*.py`` module's
``add_*_subparser(subparsers)`` function; :func:`main` calls it, parses
``argv``, then dispatches with a single ``return args.func(args)``. Every
subparser calls
``set_defaults(func=<handler>)`` at declaration time (in its
``add_*_subparser``), binding parser → handler once, up front — ``main()``
itself contains no per-command branching. A missing subcommand prints help
and returns 0; an unrecognized one is rejected by argparse itself before
``main()`` ever sees ``args.command``.

Sixteen sibling ``_cmd_*.py`` modules each own one or a few related
subcommands' argparse setup plus their handler(s):
``_cmd_lifecycle`` (init/status/disable/enable/spend), ``_cmd_serve``
(serve), ``_cmd_run`` (run), ``_cmd_index`` (reindex/rebuild-index/compile/
registry/ingest/session-end), ``_cmd_query`` (recall/query-topics/
stopwords/test-mcp), ``_cmd_pending`` (ingest-answers/ingest-merges/
reresolve-questions), ``_cmd_curate`` (dedupe/claims/auto-memory),
``_cmd_decay`` (decay-sweep, issue athenaeum#904),
``_cmd_reconcile`` (reconcile, issue athenaeum#1143), ``_cmd_repair`` (repair),
``_cmd_questions``, ``_cmd_merges``,
``_cmd_decisions``, ``_cmd_authority``, ``_cmd_axiom``, ``_cmd_calibration``,
``_cmd_outbound``, ``_cmd_drain``, ``_cmd_storage``, ``_cmd_push_metrics``
(push-metrics baseline/coverage-audit, issue athenaeum#711),
``_cmd_memory_class`` (memory-class backfill, issue athenaeum#996),
``_cmd_verdicts`` (verdict ledger inspection, issue athenaeum#712),
``_cmd_explain_routing`` (explain-routing, issue athenaeum#1176).

FACTORING RULE: **every subcommand lives in its own ``_cmd_<name>.py`` module
(or a small same-domain group module) with an ``add_<name>_subparser(subparsers)``
function that both builds the parser AND calls ``set_defaults(func=...)``,
wired into ``main()`` via a lazy import + one call — never inline in
``main()`` and never as an ``elif`` branch.** The ``_cmd_*`` modules are the
only place new subcommand wiring goes.

Layering: L5 (presentation, top of the stack). May import anything below
it (L0-L4) plus the ``_cmd_*.py`` sibling modules; nothing below may import
this module. Owns process-level concerns only: argv parsing, exit codes,
and top-level dispatch. Must NOT contain merge/tier/resolution business
logic itself — that belongs in the L3/L4 module a command's handler
delegates to, or in the owning ``_cmd_*.py`` module.

Non-obvious invariant: subcommand modules are imported LAZILY (inside
``main()``, not at module top) — this keeps `import athenaeum.cli` cheap (no
transitive pull of the whole L1-L4 graph, e.g. ``anthropic``/embedding
backends) for callers that only need argv-parsing helpers like
``_iso_date`` (re-exported here from ``_cli_shared`` for backward
compatibility).

Second non-obvious invariant (issue athenaeum#1360 residual, see
``_SUBCOMMAND_LOADERS`` below): :func:`build_parser` itself stays fully
eager — it is a documented introspection contract (``tests/test_cli.py``
walks its ENTIRE tree, every leaf, to assert every subcommand binds
``func`` and every subcommand's ``--help`` renders without crashing) — but
``main()`` does not call it for the common case. ``main()`` peeks the raw
argv for the subcommand token *before* building any parser and, when that
token names a known subcommand, imports only that subcommand's owning
``_cmd_*`` module via ``_SUBCOMMAND_LOADERS`` and builds a parser containing
just it. Every other ``_cmd_*`` module — 30 of the current 31 — is never
imported. ``athenaeum --version`` needs no subcommand module at all and
short-circuits before any lazy-loader lookup. Anything the peek can't
resolve (bare ``athenaeum``, top-level ``-h``/``--help``, an unrecognized
command) falls back to the fully eager :func:`build_parser`, so a wrong or
absent guess costs performance, never correctness — dispatch always goes
through the real ``add_*_subparser`` function, so a single-command run
parses with the exact same argument definitions ``build_parser()`` would
have given it.
"""

import argparse
import importlib
import sys

from athenaeum._cli_shared import (
    EXIT_LOCK_HELD,  # noqa: F401 — re-exported for `from athenaeum.cli import EXIT_LOCK_HELD`
    _acquire_or_exit,  # noqa: F401 — re-exported; some tests/tools import via cli
    _add_lock_args,  # noqa: F401 — re-exported; some tests/tools import via cli
    _iso_date,  # noqa: F401 — re-exported for `from athenaeum.cli import _iso_date`
    _positive_int,  # noqa: F401 — re-exported; some tests/tools import via cli
)

# Subcommand name -> (owning module, its `add_*_subparser` loader function
# name). Keys are every name `build_parser()` registers on the top-level
# subparsers action, aliases included (`reindex`'s `rebuild-index` alias is
# both a key here, same target). This is intentionally a literal table, not
# something extracted from each module at runtime — extracting it without
# importing the module is exactly the thing we're avoiding. Kept honest by
# `TestSubcommandLoaderRegistryStaysInSync` in `tests/test_cli.py`, which
# builds the real (eager) parser and asserts this table's key set matches
# its top-level `choices` exactly, so a subcommand added without a registry
# entry fails CI instead of silently losing its fast path.
_SUBCOMMAND_LOADERS: dict[str, tuple[str, str]] = {
    "init": ("athenaeum._cmd_lifecycle", "add_lifecycle_subparsers"),
    "status": ("athenaeum._cmd_lifecycle", "add_lifecycle_subparsers"),
    "disable": ("athenaeum._cmd_lifecycle", "add_lifecycle_subparsers"),
    "enable": ("athenaeum._cmd_lifecycle", "add_lifecycle_subparsers"),
    "spend": ("athenaeum._cmd_lifecycle", "add_lifecycle_subparsers"),
    "serve": ("athenaeum._cmd_serve", "add_serve_subparser"),
    "run": ("athenaeum._cmd_run", "add_run_subparser"),
    "test-mcp": ("athenaeum._cmd_query", "add_query_subparsers"),
    "entity": ("athenaeum._cmd_query", "add_query_subparsers"),
    "query-topics": ("athenaeum._cmd_query", "add_query_subparsers"),
    "stopwords": ("athenaeum._cmd_query", "add_query_subparsers"),
    "recall": ("athenaeum._cmd_query", "add_query_subparsers"),
    "enumerate": ("athenaeum._cmd_enumerate", "add_enumerate_subparser"),
    "explain-routing": (
        "athenaeum._cmd_explain_routing",
        "add_explain_routing_subparser",
    ),
    "ingest-answers": ("athenaeum._cmd_pending", "add_pending_subparsers"),
    "ingest-merges": ("athenaeum._cmd_pending", "add_pending_subparsers"),
    "reresolve-questions": ("athenaeum._cmd_pending", "add_pending_subparsers"),
    "dedupe": ("athenaeum._cmd_curate", "add_curate_subparsers"),
    "claims": ("athenaeum._cmd_curate", "add_curate_subparsers"),
    "auto-memory": ("athenaeum._cmd_curate", "add_curate_subparsers"),
    "decay-sweep": ("athenaeum._cmd_decay", "add_decay_subparser"),
    "reconcile": ("athenaeum._cmd_reconcile", "add_reconcile_subparser"),
    "repair": ("athenaeum._cmd_repair", "add_repair_subparser"),
    "pii-restore": ("athenaeum._cmd_pii_restore", "add_pii_restore_subparser"),
    "questions": ("athenaeum._cmd_questions", "add_questions_subparser"),
    "merges": ("athenaeum._cmd_merges", "add_merges_subparser"),
    "decisions": ("athenaeum._cmd_decisions", "add_decisions_subparser"),
    "authority": ("athenaeum._cmd_authority", "add_authority_subparser"),
    "axiom": ("athenaeum._cmd_axiom", "add_axiom_subparser"),
    "calibration": ("athenaeum._cmd_calibration", "add_calibration_subparser"),
    "outbound-lint": ("athenaeum._cmd_outbound", "add_outbound_subparser"),
    "bounce-contract": (
        "athenaeum._cmd_bounce_contract",
        "add_bounce_contract_subparser",
    ),
    "surface-divergence": (
        "athenaeum._cmd_surface_divergence",
        "add_surface_divergence_subparser",
    ),
    "drain": ("athenaeum._cmd_drain", "add_drain_subparser"),
    "storage": ("athenaeum._cmd_storage", "add_storage_subparser"),
    "push-metrics": ("athenaeum._cmd_push_metrics", "add_push_metrics_subparser"),
    "usage-report": ("athenaeum._cmd_usage_report", "add_usage_report_subparser"),
    "measure": ("athenaeum._cmd_measure", "add_measure_subparser"),
    "memory-class": ("athenaeum._cmd_memory_class", "add_memory_class_subparser"),
    "description": ("athenaeum._cmd_description", "add_description_subparser"),
    "verdicts": ("athenaeum._cmd_verdicts", "add_verdicts_subparser"),
    "dimensions": ("athenaeum._cmd_dimensions", "add_dimensions_subparser"),
    "reindex": ("athenaeum._cmd_index", "add_index_subparsers"),
    "rebuild-index": ("athenaeum._cmd_index", "add_index_subparsers"),
    "compile": ("athenaeum._cmd_index", "add_index_subparsers"),
    "registry": ("athenaeum._cmd_index", "add_index_subparsers"),
    "ingest": ("athenaeum._cmd_index", "add_index_subparsers"),
    "session-end": ("athenaeum._cmd_index", "add_index_subparsers"),
}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``athenaeum`` parser with every subcommand wired.

    Factored out of :func:`main` so a test can introspect the assembled
    parser tree (e.g. walking every registered subparser to assert it has a
    ``func`` default) without going through argv parsing + dispatch. ``main``
    is the only intended caller in production code.
    """
    from athenaeum._cmd_authority import add_authority_subparser
    from athenaeum._cmd_axiom import add_axiom_subparser
    from athenaeum._cmd_bounce_contract import add_bounce_contract_subparser
    from athenaeum._cmd_calibration import add_calibration_subparser
    from athenaeum._cmd_curate import add_curate_subparsers
    from athenaeum._cmd_decay import add_decay_subparser
    from athenaeum._cmd_decisions import add_decisions_subparser
    from athenaeum._cmd_description import add_description_subparser
    from athenaeum._cmd_dimensions import add_dimensions_subparser
    from athenaeum._cmd_drain import add_drain_subparser
    from athenaeum._cmd_enumerate import add_enumerate_subparser
    from athenaeum._cmd_explain_routing import add_explain_routing_subparser
    from athenaeum._cmd_index import add_index_subparsers
    from athenaeum._cmd_lifecycle import add_lifecycle_subparsers
    from athenaeum._cmd_measure import add_measure_subparser
    from athenaeum._cmd_memory_class import add_memory_class_subparser
    from athenaeum._cmd_merges import add_merges_subparser
    from athenaeum._cmd_outbound import add_outbound_subparser
    from athenaeum._cmd_pending import add_pending_subparsers
    from athenaeum._cmd_pii_restore import add_pii_restore_subparser
    from athenaeum._cmd_push_metrics import add_push_metrics_subparser
    from athenaeum._cmd_query import add_query_subparsers
    from athenaeum._cmd_questions import add_questions_subparser
    from athenaeum._cmd_reconcile import add_reconcile_subparser
    from athenaeum._cmd_repair import add_repair_subparser
    from athenaeum._cmd_run import add_run_subparser
    from athenaeum._cmd_serve import add_serve_subparser
    from athenaeum._cmd_storage import add_storage_subparser
    from athenaeum._cmd_subject import add_subject_subparser
    from athenaeum._cmd_surface_divergence import add_surface_divergence_subparser
    from athenaeum._cmd_usage_report import add_usage_report_subparser
    from athenaeum._cmd_verdicts import add_verdicts_subparser

    parser = argparse.ArgumentParser(
        prog="athenaeum",
        description="Knowledge management pipeline — append-only intake, tiered compilation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
    subparsers = parser.add_subparsers(dest="command")

    # Issue athenaeum#553: each add_*_subparser both builds its subparser(s) AND calls
    # set_defaults(func=<handler>) on them, so dispatch below is uniform.
    add_lifecycle_subparsers(subparsers)  # init, status, disable, enable, spend
    add_serve_subparser(subparsers)  # serve
    add_run_subparser(subparsers)  # run
    add_query_subparsers(subparsers)  # test-mcp, entity, query-topics, stopwords, recall
    add_enumerate_subparser(subparsers)  # enumerate (issue athenaeum#965)
    add_explain_routing_subparser(subparsers)  # explain-routing (issue athenaeum#1176)
    add_pending_subparsers(subparsers)  # ingest-answers, ingest-merges, reresolve-questions
    add_curate_subparsers(subparsers)  # dedupe, claims, auto-memory
    add_decay_subparser(subparsers)  # decay-sweep
    add_reconcile_subparser(subparsers)  # reconcile (issue athenaeum#1143)
    add_repair_subparser(subparsers)  # repair
    add_pii_restore_subparser(subparsers)  # pii-restore (issue athenaeum#1037)
    add_questions_subparser(subparsers)  # questions
    add_merges_subparser(subparsers)  # merges
    add_decisions_subparser(subparsers)  # decisions
    add_authority_subparser(subparsers)  # authority
    add_axiom_subparser(subparsers)  # axiom
    add_calibration_subparser(subparsers)  # calibration
    add_outbound_subparser(subparsers)  # outbound-lint
    add_bounce_contract_subparser(subparsers)  # bounce-contract
    add_surface_divergence_subparser(subparsers)  # surface-divergence
    # bounce-divergence / do-not-email-divergence removed (issue athenaeum#1111):
    # surface-divergence --field {bounced,do_not_email} replaced both;
    # the removed pair had zero remaining callers (see the issue for the
    # verified caller audit, including the cron-fleet correction).
    add_drain_subparser(subparsers)  # drain
    add_storage_subparser(subparsers)  # storage
    add_push_metrics_subparser(subparsers)  # push-metrics
    add_usage_report_subparser(subparsers)  # usage-report (issue athenaeum#968)
    add_measure_subparser(subparsers)  # measure (shadow-linkage, backlog-price, ordinary-night)
    add_memory_class_subparser(subparsers)  # memory-class backfill
    add_description_subparser(subparsers)  # description backfill (issue athenaeum#1324)
    add_verdicts_subparser(subparsers)  # verdicts (issue athenaeum#712)
    add_dimensions_subparser(subparsers)  # dimensions show|compare (issue athenaeum#714)
    add_subject_subparser(subparsers)  # subject backfill (issue athenaeum#1244)
    add_index_subparsers(
        subparsers
    )  # reindex/rebuild-index, compile, registry, ingest, session-end

    return parser


def _peek_subcommand(argv: list[str]) -> str | None:
    """Return the first non-option token in ``argv``, or ``None``.

    A cheap, regex-free pre-parse used only to decide which single
    ``_cmd_*`` module (if any) ``main()`` needs to import — never used to
    validate or dispatch. The top-level parser has exactly one positional
    (the subcommand) and no options that consume a value (``--version`` and
    ``-h``/``--help`` are both zero-arg), so "first token not starting with
    ``-``" is unambiguous. A wrong guess (there isn't one today, but a
    future top-level value-taking option would produce one) just means
    ``main()`` falls through to the fully eager :func:`build_parser`.
    """
    for token in argv:
        if not token.startswith("-"):
            return token
    return None


def _build_lazy_parser(command: str) -> argparse.ArgumentParser:
    """Build a parser carrying ``--version`` plus only ``command``'s group.

    ``command`` must already be a known key of ``_SUBCOMMAND_LOADERS`` —
    callers check that before calling this. Imports exactly one ``_cmd_*``
    module and calls its real ``add_*_subparser`` loader, so the resulting
    subparser (arguments, nested subcommands, ``func`` binding, help text)
    is identical to what :func:`build_parser` would have produced for that
    same command — only the other ~30 modules are never imported.
    """
    module_path, loader_name = _SUBCOMMAND_LOADERS[command]
    module = importlib.import_module(module_path)
    loader = getattr(module, loader_name)

    parser = argparse.ArgumentParser(
        prog="athenaeum",
        description="Knowledge management pipeline — append-only intake, tiered compilation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
    subparsers = parser.add_subparsers(dest="command")
    loader(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = _peek_subcommand(argv)

    if command is None and "--version" in argv:
        # No subcommand token at all -- e.g. `athenaeum --version`. The
        # version action fires and exits before any subcommand is ever
        # consulted, so building even one `_cmd_*` module for it is pure
        # waste (issue athenaeum#1360 residual: this was the single biggest
        # remaining cost on the `--version`/`--help` path).
        parser = argparse.ArgumentParser(
            prog="athenaeum",
            description="Knowledge management pipeline — append-only intake, tiered compilation",
        )
        parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
        parser.parse_args(argv)
        # Unreachable in practice: the version action above always exits.
        # Kept only so this branch has a well-defined return if argparse's
        # exit-on-version behavior is ever weakened.
        return 0

    if command in _SUBCOMMAND_LOADERS:
        parser = _build_lazy_parser(command)
    else:
        # Bare `athenaeum`, top-level -h/--help, or an unrecognized command
        # -- all need the full subcommand listing (names + help text) to
        # answer correctly, which only the eager build provides. Pays the
        # known ~31-module cost, same as before this change.
        parser = build_parser()

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
