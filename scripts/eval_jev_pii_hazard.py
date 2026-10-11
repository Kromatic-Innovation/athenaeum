#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline eval: Jev (TypeSafe AI, hosted) vs the PII regex gate (issue athenaeum#2009).

Asks Jev's Noul question -- "is this string a way to contact a specific
human?" -- against a labelled fixture set, and records the SAME question's
answer from the existing regex gate (:func:`athenaeum.sensitivity.classify`)
for the same strings, so the two columns are comparable on one fixture set.
Hosted sibling of athenaeum#2049 (Maya, a local no-egress model evaluated
for the same question); this script imports that script's loaders, metrics
and markdown-rendering code directly (never copies them) via
:mod:`eval_maya_pii_hazard`, loaded the same way ``test_eval_maya_pii_hazard.py``
loads it so both scripts and both test modules share one ``Fixture``/
``RowResult``/``Metrics`` class identity.

**Unredacted by design.** The fixture string is sent to the live Jev API
exactly as typed, with no redaction first. Redacting a contact-shaped
string before asking "is this a way to contact a specific human?" would
erase the exact signal the question is about -- this is the deliberate
choice athenaeum#2009's acceptance criteria call for, not an oversight.
Unlike athenaeum#2049's Maya sibling (which never leaves the machine),
THIS IS REAL EGRESS: every fixture string -- true positives and the
adjudicated-allowlist negatives alike -- crosses the network to
api.typesafe.ai. There is no local/offline mode for this script.

**No fixture strings in any output.** Per-row progress prints counts and
indices only. A row whose Jev call raises records only the exception's
type name and the row index -- never the exception's message, which may
echo the request body (and therefore the fixture string) back. The
emitted markdown carries only aggregate counts and rates, exactly like the
Maya sibling.

**Real, billed spend -- capped.** Jev input tokens cost USD 0.042 per
million (output is free, per the operator's 2026-10-10 athenaeum#2009
disposition). ``--max-spend-usd`` (default 5.0, the disposition's lane
cap) stops the run and reports before placing a call that would push the
running total over the cap. Every response's ``usage.input_tokens`` is
summed into that running total; the script never estimates spend from
string length.

**Sequential, one call per string.** Jev's state/question shape supports
several questions over one state, but packing multiple fixture strings
into one state would change the question being measured (the model would
see neighboring fixtures as context) -- this script makes exactly one
``decide()`` call per fixture, with a constant ``question_id`` (never
derived from fixture content), and never runs calls concurrently so the
latency samples reflect one call at a time, not queueing behind siblings.

Usage::

    JEV_API_KEY="$(op read 'op://Agent Tools/Jev Athenaeum Key/credential')" \\
        python scripts/eval_jev_pii_hazard.py \\
        --fixtures /path/to/host-side/positives.jsonl \\
        --allowlist ~/knowledge/wiki/_pii-allowlist.yml \\
        --output /path/to/scratch/jev-eval-raw.json \\
        --progress-every 25
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The exact yes/no hazard question, mirroring athenaeum#2049's Maya sibling.
QUESTION = "Is this string a way to contact a specific human?"

#: Constant question id -- never derived from fixture content (would leak
#: the fixture into a log line naming the question).
QUESTION_ID = "hazard:noul"

#: Jev input-token price (USD per million tokens); output is free (2026-10-10
#: disposition, issue athenaeum#2009).
JEV_INPUT_USD_PER_MILLION = 0.042

#: Default spend cap for this lane (USD), per the 2026-10-10 disposition.
DEFAULT_MAX_SPEND_USD = 5.0

_SCRIPT_DIR = Path(__file__).resolve().parent
_MAYA_SCRIPT = _SCRIPT_DIR / "eval_maya_pii_hazard.py"


def _load_maya_module() -> Any:
    """Import ``eval_maya_pii_hazard`` by path, reusing a module already
    registered in ``sys.modules`` (e.g. by ``test_eval_maya_pii_hazard.py``)
    rather than re-``exec``-ing it under the same name -- a second exec
    would create a second, distinct ``Fixture``/``RowResult`` class so
    ``isinstance`` checks and dataclass equality across the two modules
    would silently stop matching.
    """
    existing = sys.modules.get("eval_maya_pii_hazard")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("eval_maya_pii_hazard", _MAYA_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_maya_pii_hazard"] = module
    spec.loader.exec_module(module)
    return module


maya = _load_maya_module()


class JevKeyMissing(RuntimeError):
    """Raised when ``JEV_API_KEY`` is not set; this script never falls
    back to a fake/offline path for the live run."""


class SpendCapExceeded(RuntimeError):
    """Raised when placing the next call would exceed ``--max-spend-usd``."""


@dataclass(frozen=True)
class JevRowOutcome:
    """One fixture's Jev outcome -- NEVER carries the fixture text."""

    index: int
    label: bool
    p_yes: float | None
    latency_s: float
    input_tokens: int
    output_tokens: int
    error_type: str | None
    attempts: int


class JevBackend:
    """Adapter seam the eval loop depends on, never
    :class:`athenaeum.decision_provider.JevDecisionBackend` directly -- a
    fake implementing the same ``p_yes_with_usage`` method lets the smoke
    test exercise the full script path with no network call (mirrors
    :class:`eval_maya_pii_hazard.MayaScorer`, issue athenaeum#2009 AC)."""

    def p_yes_with_usage(self, question: str, text: str) -> tuple[float, int, int]:
        """Return ``(p_yes, input_tokens, output_tokens)`` for one fixture."""
        raise NotImplementedError


class LiveJevBackend(JevBackend):
    """Wraps :class:`athenaeum.decision_provider.JevDecisionBackend` behind
    :func:`athenaeum._retry.with_retry` so a transient 429/5xx/connection
    error is retried exactly like every other backend call site
    (athenaeum#782), and backoff sleeps never count toward measured latency.
    """

    def __init__(self) -> None:
        from athenaeum.decision_provider import JevDecisionBackend

        self._backend = JevDecisionBackend()

    def p_yes_with_usage(self, question: str, text: str) -> tuple[float, int, int]:
        from athenaeum._retry import with_retry

        def _call() -> Any:
            return self._backend.decide(
                question_id=QUESTION_ID,
                kind="noul",
                instructions=question,
                state=text,
            )

        result = with_retry(_call, description="jev_pii_hazard_eval")
        if result.probability is None:
            raise ValueError("jev: noul answer carried no probability")
        return (
            float(result.probability),
            int(result.input_tokens or 0),
            int(result.output_tokens or 0),
        )


def _build_backend() -> JevBackend:
    """Factory seam: the only place :func:`main` constructs a real backend.
    Tests override this (not :class:`LiveJevBackend` itself) to inject a
    fake :class:`JevBackend`, mirroring :func:`eval_maya_pii_hazard._build_scorer`.
    """
    return LiveJevBackend()


def run_jev_eval(
    backend: JevBackend,
    fixtures: Sequence[Any],
    *,
    question: str = QUESTION,
    max_spend_usd: float = DEFAULT_MAX_SPEND_USD,
    progress_every: int = 0,
    progress_file: Any = sys.stderr,
) -> list[JevRowOutcome]:
    """Score every fixture with *backend*, one call per row, sequentially.

    Never raises on a single row's backend error -- records it as
    ``error_type`` (the exception's class name only, never its message)
    and continues, so one malformed/unreachable row cannot zero out the
    whole run. Raises :class:`SpendCapExceeded` as soon as the running
    spend total (summed from each call's actual ``usage.input_tokens``,
    never estimated from fixture-string length) exceeds *max_spend_usd* --
    checked before each call is placed, so at most ONE call beyond the cap
    ever runs (the call that pushes the total over the cap is already in
    flight when the overage becomes knowable; the row *after* it is the
    first one actually skipped). The real run in athenaeum#2058 spent USD
    0.0099 against a USD 5.0 cap, so this bound is immaterial in practice.
    """
    outcomes: list[JevRowOutcome] = []
    total_input_tokens = 0
    for i, fx in enumerate(fixtures):
        projected_cost = (total_input_tokens / 1_000_000) * JEV_INPUT_USD_PER_MILLION
        if projected_cost > max_spend_usd:
            print(
                f"jev eval: stopping at row {i}/{len(fixtures)} -- spend cap "
                f"USD {max_spend_usd:.2f} reached (running total "
                f"USD {projected_cost:.4f})",
                file=progress_file,
            )
            raise SpendCapExceeded(
                f"spend cap USD {max_spend_usd:.2f} reached after {i} row(s) "
                f"(running total USD {projected_cost:.4f})"
            )

        start = time.perf_counter()
        error_type: str | None = None
        p_yes: float | None = None
        input_tokens = 0
        output_tokens = 0
        try:
            p_yes, input_tokens, output_tokens = backend.p_yes_with_usage(question, fx.text)
        except Exception as exc:  # noqa: BLE001 -- recorded as type name only, never str(exc)
            error_type = type(exc).__name__
        latency_s = time.perf_counter() - start
        total_input_tokens += input_tokens

        outcomes.append(
            JevRowOutcome(
                index=i,
                label=fx.label,
                p_yes=p_yes,
                latency_s=latency_s,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_type=error_type,
                attempts=1,
            )
        )
        if progress_every and (i + 1) % progress_every == 0:
            print(
                f"jev eval: {i + 1}/{len(fixtures)} rows, "
                f"{sum(1 for o in outcomes if o.error_type)} error(s), "
                f"running input_tokens={total_input_tokens}",
                file=progress_file,
            )
    return outcomes


def retry_failed_rows(
    backend: JevBackend,
    fixtures: Sequence[Any],
    outcomes: list[JevRowOutcome],
    *,
    question: str = QUESTION,
    max_spend_usd: float = DEFAULT_MAX_SPEND_USD,
    max_rounds: int = 3,
) -> list[JevRowOutcome]:
    """Re-run every row still carrying an ``error_type`` in place, up to
    *max_rounds* passes, so a transient-run gap does not silently shrink
    the fixture set the regex column is compared against. Returns a new
    list with failed rows replaced by their retry outcome (success or a
    fresh failure) -- never drops a row.
    """
    current = list(outcomes)
    spent = sum((o.input_tokens / 1_000_000) * JEV_INPUT_USD_PER_MILLION for o in current)
    for _round in range(max_rounds):
        failed_indices = [i for i, o in enumerate(current) if o.error_type is not None]
        if not failed_indices:
            break
        for i in failed_indices:
            if spent > max_spend_usd:
                raise SpendCapExceeded(
                    f"spend cap USD {max_spend_usd:.2f} reached during retry pass "
                    f"(running total USD {spent:.4f})"
                )
            fx = fixtures[i]
            start = time.perf_counter()
            error_type: str | None = None
            p_yes = None
            input_tokens = 0
            output_tokens = 0
            try:
                p_yes, input_tokens, output_tokens = backend.p_yes_with_usage(question, fx.text)
            except Exception as exc:  # noqa: BLE001
                error_type = type(exc).__name__
            latency_s = time.perf_counter() - start
            spent += (input_tokens / 1_000_000) * JEV_INPUT_USD_PER_MILLION
            current[i] = JevRowOutcome(
                index=i,
                label=fx.label,
                p_yes=p_yes,
                latency_s=latency_s,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_type=error_type,
                attempts=current[i].attempts + 1,
            )
    return current


def outcomes_to_row_results(
    outcomes: Sequence[JevRowOutcome],
    fixtures: Sequence[Any],
    *,
    threshold: float,
) -> list[Any]:
    """Convert successful :class:`JevRowOutcome` rows into
    :class:`eval_maya_pii_hazard.RowResult` so the SAME
    :func:`eval_maya_pii_hazard.compute_metrics` /
    :func:`eval_maya_pii_hazard.render_markdown` code scores both Maya and
    Jev. Rows that never got a successful answer (``error_type`` still set
    after every retry round) are excluded -- callers should treat a
    non-empty remainder of errored rows as a run that did not reach the
    full fixture set and re-run before trusting the comparison.
    """
    RowResult = maya.RowResult
    results = []
    for outcome in outcomes:
        if outcome.error_type is not None or outcome.p_yes is None:
            continue
        fx = fixtures[outcome.index]
        regex_verdict = maya.regex_gate_verdict(fx.text)
        results.append(
            RowResult(
                label=outcome.label,
                p_yes=outcome.p_yes,
                predicted=outcome.p_yes >= threshold,
                regex_verdict=regex_verdict,
                maya_latency_s=outcome.latency_s,
                regex_latency_s=0.0,
            )
        )
    return results


def summarize_usage(outcomes: Sequence[JevRowOutcome]) -> dict[str, Any]:
    """Aggregate token/latency/error figures -- the numbers the write-up
    needs, computed once here so the doc update and the raw-output JSON
    agree on the same arithmetic."""
    input_tokens = sum(o.input_tokens for o in outcomes)
    output_tokens = sum(o.output_tokens for o in outcomes)
    latencies = sorted(o.latency_s for o in outcomes)
    n_errors = sum(1 for o in outcomes if o.error_type is not None)
    n_retried = sum(1 for o in outcomes if o.attempts > 1)
    cost_usd = (input_tokens / 1_000_000) * JEV_INPUT_USD_PER_MILLION
    return {
        "n_rows": len(outcomes),
        "n_errors_final": n_errors,
        "n_rows_retried": n_retried,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "estimated_cost_usd": cost_usd,
        "mean_latency_s": statistics.fmean(latencies) if latencies else 0.0,
        "median_latency_s": statistics.median(latencies) if latencies else 0.0,
        "p95_latency_s": maya._percentile(latencies, 0.95),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="JSON-Lines file of {text, label} rows (host-side; never committed).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Adjudicated PII allowlist YAML; exact-value entries become label=False rows.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path to write raw per-row results + summary as JSON (no fixture text).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="P(yes) threshold classifying Jev's verdict as positive (default: 0.5).",
    )
    parser.add_argument(
        "--abstain-low", type=float, default=None, help="Lower bound of an abstention band."
    )
    parser.add_argument(
        "--abstain-high", type=float, default=None, help="Upper bound of an abstention band."
    )
    parser.add_argument(
        "--max-spend-usd",
        type=float,
        default=DEFAULT_MAX_SPEND_USD,
        help=f"Spend cap in USD for this run (default: {DEFAULT_MAX_SPEND_USD}).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print a progress line every N rows (0 disables).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.fixtures is None and args.allowlist is None:
        print(
            "refusing to run: at least one of --fixtures/--allowlist is required", file=sys.stderr
        )
        return 2

    if not os.environ.get("JEV_API_KEY"):
        print(
            "jev eval: refusing to run -- JEV_API_KEY is not set. This script makes "
            "real, billed network calls and will not fall back to a fake backend.",
            file=sys.stderr,
        )
        return 1

    fixtures: list[Any] = []
    if args.fixtures is not None:
        fixtures.extend(maya.load_fixtures_jsonl(args.fixtures))
    if args.allowlist is not None:
        allow_fixtures, n_pattern_skipped, n_errors = maya.load_fixtures_allowlist(args.allowlist)
        fixtures.extend(allow_fixtures)
        if n_pattern_skipped:
            print(
                f"note: {n_pattern_skipped} pattern allowlist entr(y/ies) "
                "skipped (not a literal string)"
            )
        if n_errors:
            print(f"note: {n_errors} allowlist entr(y/ies) had load errors (skipped)")

    if not fixtures:
        print("refusing to run: no fixture rows loaded", file=sys.stderr)
        return 2

    backend = _build_backend()
    try:
        outcomes = run_jev_eval(
            backend,
            fixtures,
            max_spend_usd=args.max_spend_usd,
            progress_every=args.progress_every,
        )
    except SpendCapExceeded as exc:
        print(f"refusing to continue: {exc}", file=sys.stderr)
        return 3

    try:
        outcomes = retry_failed_rows(backend, fixtures, outcomes, max_spend_usd=args.max_spend_usd)
    except SpendCapExceeded as exc:
        print(f"refusing to continue retry pass: {exc}", file=sys.stderr)
        return 3

    results = outcomes_to_row_results(outcomes, fixtures, threshold=args.threshold)
    n_still_errored = sum(1 for o in outcomes if o.error_type is not None)
    if n_still_errored:
        print(
            f"warning: {n_still_errored} row(s) still errored after retries -- "
            "the regex comparison below is on a SUBSET of the fixture set, not "
            "the full set",
            file=sys.stderr,
        )

    metrics = maya.compute_metrics(
        results, abstain_low=args.abstain_low, abstain_high=args.abstain_high
    )
    usage = summarize_usage(outcomes)

    abstain_band = (
        (args.abstain_low, args.abstain_high)
        if args.abstain_low is not None and args.abstain_high is not None
        else None
    )
    markdown = maya.render_markdown(
        metrics,
        threshold=args.threshold,
        abstain_band=abstain_band,
        model_name="Jev",
        model_description="the hosted Jev API (TypeSafe AI, Noul classifier; issue athenaeum#2009)",
    )

    raw_rows = [
        {
            "index": o.index,
            "label": o.label,
            "p_yes": o.p_yes,
            "latency_s": o.latency_s,
            "input_tokens": o.input_tokens,
            "output_tokens": o.output_tokens,
            "error_type": o.error_type,
            "attempts": o.attempts,
        }
        for o in outcomes
    ]
    payload = {
        "question": QUESTION,
        "n_fixtures": len(fixtures),
        "n_scored_in_comparison": len(results),
        "n_still_errored": n_still_errored,
        "threshold": args.threshold,
        "usage": usage,
        "markdown": markdown,
        "rows": raw_rows,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        args.output.chmod(0o600)
    except OSError:
        pass
    print(f"wrote {args.output}")
    print(
        f"rows={len(fixtures)} scored={len(results)} errored={n_still_errored} "
        f"input_tokens={usage['total_input_tokens']} "
        f"est_cost_usd={usage['estimated_cost_usd']:.4f}"
    )
    return 0 if n_still_errored == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
