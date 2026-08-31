# SPDX-License-Identifier: Apache-2.0
"""``athenaeum explain-routing`` — read-only preview of the resolved
provider, model, batch eligibility, and per-MTok price for every model
knob (issue athenaeum#1176).

Docs were the actual gap this closes, not routing logic: provider
(:func:`athenaeum.provider.resolve_provider`) and model
(:func:`athenaeum.config.resolve_model`) are both already genuinely
per-knob and threaded end to end (issue athenaeum#841). What was missing was a
single place that assembled all three axes -- provider, model, batch
eligibility -- per knob for a given ``athenaeum.yaml`` + environment, and a
command that prints it. This module changes NO routing behaviour: every
value below comes from calling the SAME resolvers a real ``athenaeum run``
calls (:func:`athenaeum.librarian._resolve_run_models` for models,
:func:`athenaeum.provider.resolve_provider` for providers,
:func:`athenaeum.librarian.librarian_batch_knob` for batch eligibility) --
see ``tests/test_cmd_explain_routing.py`` for the test asserting this
command's resolved values equal those resolvers' own return values for the
same config, which is what makes "matches what a real run actually uses"
checkable rather than merely asserted.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` -- see
``cli.py``'s module docstring.

Knob list: iterates :data:`athenaeum.prompt_registry.KNOBS` at runtime
rather than hard-coding the six knobs that exist today, so a future knob
(for example issue athenaeum#1174's concurrently-landing ``rule_proposals``)
appears in this table automatically once it is in ``_META_ROWS`` -- nothing
here needs editing when the knob set grows. A knob this module has no
model-resolution getter wired for (only possible for a knob whose model
resolution was not added to :func:`athenaeum.librarian._resolve_run_models`
in the same change that added it to the registry) shows ``model: None``
rather than raising -- provider and batch eligibility are still resolved
for it, since those two axes are generic across every knob.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config


def add_explain_routing_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``explain-routing``."""
    parser = subparsers.add_parser(
        "explain-routing",
        help="Read-only preview: resolved provider/model/batch/price per "
        "model knob (issue athenaeum#1176). Prints what 'athenaeum run' would "
        "actually use for this athenaeum.yaml + environment -- no LLM call, "
        "no file processed, no routing behavior changed.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a formatted table.",
    )
    parser.set_defaults(func=cmd_explain_routing)


def resolve_routing_table(config: dict[str, object] | None) -> list[dict[str, object]]:
    """Return one row per knob in :data:`athenaeum.prompt_registry.KNOBS`.

    Each row: ``knob``, ``provider``, ``model``, ``batch_eligible``,
    ``batched_this_run``, ``price_input_usd_per_mtok``,
    ``price_output_usd_per_mtok``, ``price_is_blended_fallback``.

    Calls the SAME functions a real run calls, for the SAME config -- see
    the module docstring. This is assembly and printing, not new resolution
    logic (issue athenaeum#1176).
    """
    from athenaeum.config import resolve_model_rates
    from athenaeum.librarian import (
        BATCHABLE_KNOBS,
        _resolve_run_models,
        librarian_batch_knob,
        librarian_batch_mode,
    )
    from athenaeum.models import _rates_for_model, configure_model_rates, model_has_price
    from athenaeum.prompt_registry import KNOBS
    from athenaeum.provider import resolve_provider

    models_by_knob = dict(_resolve_run_models(config))
    # Mirrors the athenaeum#783 run-startup preflight: install the operator's
    # resolved pricing table as ACTIVE before pricing any model below, so a
    # `pricing:` override in athenaeum.yaml is reflected here exactly as it
    # would be for a real run. Reset by tests/conftest.py's autouse
    # `_reset_model_rates` fixture; a real `athenaeum run` invocation is a
    # fresh process, same as this command's own invocation.
    configure_model_rates(resolve_model_rates(config))
    run_batch_default = librarian_batch_mode(config)

    rows: list[dict[str, object]] = []
    for knob in KNOBS:
        provider = resolve_provider(config, knob=knob)
        model = models_by_knob.get(knob)
        batch_eligible = knob in BATCHABLE_KNOBS
        batched_this_run = (
            librarian_batch_knob(config, knob, default=run_batch_default)
            if batch_eligible
            else False
        )
        row: dict[str, object] = {
            "knob": knob,
            "provider": provider,
            "model": model,
            "batch_eligible": batch_eligible,
            "batched_this_run": batched_this_run,
        }
        if model:
            input_rate, output_rate = _rates_for_model(model)
            row["price_input_usd_per_mtok"] = input_rate
            row["price_output_usd_per_mtok"] = output_rate
            row["price_is_blended_fallback"] = not model_has_price(model)
        else:
            row["price_input_usd_per_mtok"] = None
            row["price_output_usd_per_mtok"] = None
            row["price_is_blended_fallback"] = None
        rows.append(row)
    return rows


def _format_table(rows: list[dict[str, object]]) -> str:
    headers = ["knob", "provider", "model", "batch", "price ($/MTok in,out)"]
    lines = [" | ".join(headers)]
    for row in rows:
        model = row["model"] or "(no model resolver wired for this knob)"
        if row["price_input_usd_per_mtok"] is None:
            price = "-"
        else:
            fallback = " (blended fallback)" if row["price_is_blended_fallback"] else ""
            price = (
                f"{row['price_input_usd_per_mtok']}/{row['price_output_usd_per_mtok']}"
                f"{fallback}"
            )
        if row["batch_eligible"]:
            batch = "batched" if row["batched_this_run"] else "not batched (eligible)"
        else:
            batch = "never batched"
        lines.append(" | ".join([str(row["knob"]), str(row["provider"]), str(model), batch, price]))
    return "\n".join(lines)


def cmd_explain_routing(args: argparse.Namespace) -> int:
    knowledge_root = args.path.expanduser().resolve()
    config = load_config(knowledge_root)
    rows = resolve_routing_table(config)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(_format_table(rows))
    return 0
