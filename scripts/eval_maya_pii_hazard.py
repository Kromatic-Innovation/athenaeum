#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline eval: Maya (local yes/no cross-encoder) vs the PII regex gate (issue athenaeum#2049).

Asks Maya the hazard question -- "is this string a way to contact a specific
human?" -- against a labelled fixture set, and records the same question's
answer from the EXISTING regex gate (:func:`athenaeum.sensitivity.classify`,
the ``email``/``phone``/``street-address`` recognisers :mod:`athenaeum.pii`
backs) for the same strings, so the two columns are comparable on one
fixture set rather than two separately-drawn samples. Sibling of
athenaeum#2009 (Jev, a hosted classifier); this one is local and
no-egress -- Maya's weights run entirely on this machine, so the
contact-shaped string never leaves it even though it reaches the model
unredacted (redaction would erase the exact signal the question is about,
same rationale as athenaeum#2009).

**Zero spend, no implicit download.** Maya is loaded from a local weights
directory named by ``ATHENAEUM_MAYA_WEIGHTS_PATH`` (or ``--weights-path``).
Unset or missing is a REFUSAL, never a Hugging Face Hub fetch -- this script
never reads or sets any API key and never touches the network.

**No fixture strings in the output.** The emitted markdown carries only
aggregate counts/rates -- never a fixture's ``text``. Fixtures themselves
stay host-side; nothing corpus-derived is committed to this public repo.

Fixture sources (either or both; rows from each are concatenated):

- ``--fixtures PATH`` -- a JSON-Lines file of ``{"text": ..., "label":
  true|false}`` rows (``label`` true = a way to contact a specific human).
  Used for known true positives and synthetic/smoke rows.
- ``--allowlist PATH`` -- the adjudicated PII allowlist
  (:func:`athenaeum.pii.load_pii_allowlist`), read through the SAME
  sanctioned loader every other allowlist consumer uses. Every exact-``value``
  entry becomes a ``label=False`` row (a human has already ruled it is NOT a
  way to contact a specific human). A ``pattern`` entry names a SHAPE, not a
  string, and cannot be turned into a fixture row -- it is counted and
  skipped, never guessed at.

The regex column is computed by calling :func:`athenaeum.sensitivity.classify`
directly on each fixture's raw text, with **no allowlist applied** -- the
allowlist is exactly what adjudicated the negatives in the first place, so
consulting it here would make the regex column score 100% on negatives by
construction. The comparison is "does the regex recognise a contact-shaped
token at all", the same question Maya is asked.

Usage::

    ATHENAEUM_MAYA_WEIGHTS_PATH=/path/to/maya/weights \\
        python scripts/eval_maya_pii_hazard.py \\
        --fixtures positives.jsonl --allowlist ~/knowledge/pii-allowlist.yaml \\
        --output docs/measurements/maya-pii-hazard-eval-2026-10.md
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: The exact yes/no hazard question, mirroring athenaeum#2009's Jev sibling.
QUESTION = "Is this string a way to contact a specific human?"

#: Env var naming the local Maya weights directory. No implicit download:
#: unset or missing is a refusal (zero spend, athenaeum#2049).
WEIGHTS_PATH_ENV = "ATHENAEUM_MAYA_WEIGHTS_PATH"


class MayaWeightsUnavailable(RuntimeError):
    """Raised when the local Maya weights directory cannot be resolved."""


class MayaScorer(Protocol):
    """Adapter seam the eval loop depends on, never the real model directly.

    A fake implementing this protocol lets the smoke test exercise the full
    script path with no weights, no ``transformers``, no ``torch``, and no
    network (issue athenaeum#2049 AC3).
    """

    def p_yes(self, question: str, text: str) -> float:
        """Return Maya's calibrated P(yes) in ``[0, 1]`` for *text*."""
        ...


class MayaAdapter:
    """Loads Maya (a DeBERTa-v3-large-based yes/no cross-encoder) from a
    local weights directory and scores ``(question, text)`` pairs.

    Lazy-imports ``transformers``/``torch`` only inside :meth:`_ensure_loaded`
    -- never at module import time -- so importing this script, or
    constructing this class, never requires either package until the first
    real score. Both live behind the optional ``maya-eval`` extra
    (``pyproject.toml``), never the default install.
    """

    def __init__(self, weights_path: Path) -> None:
        self._weights_path = weights_path
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._yes_index: int | None = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise RuntimeError(
                "transformers and torch are required to load Maya locally; "
                "install the optional extra: pip install 'athenaeum[maya-eval]'"
            ) from exc
        self._torch = torch
        # local_files_only=True: this must never fall through to a Hugging
        # Face Hub fetch (zero spend, athenaeum#2049) even if the local
        # directory is incomplete -- a missing file should raise, not download.
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self._weights_path), local_files_only=True
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            str(self._weights_path), local_files_only=True
        )
        self._model.eval()
        self._yes_index = self._resolve_yes_index()

    def _resolve_yes_index(self) -> int:
        """Find the "yes" class index from the model's own ``id2label``.

        Never hardcoded -- a cross-encoder's label order is a property of
        its own config, not an assumption this script gets to make. Fails
        loudly if no label case-insensitively named "yes" is declared,
        rather than silently guessing index 1.
        """
        id2label = getattr(self._model.config, "id2label", None) or {}
        for idx, label in id2label.items():
            if str(label).strip().lower() == "yes":
                return int(idx)
        raise RuntimeError(
            f"Maya weights at {self._weights_path} declare no 'yes' label in "
            f"config.id2label ({id2label!r}); refusing to guess which output "
            "index is the yes-class."
        )

    def p_yes(self, question: str, text: str) -> float:
        self._ensure_loaded()
        torch = self._torch
        inputs = self._tokenizer(question, text, return_tensors="pt", truncation=True)
        with torch.no_grad():
            logits = self._model(**inputs).logits
        num_labels = logits.shape[-1]
        if num_labels == 1:
            # Single-logit head: the logit itself is the yes-score.
            return float(torch.sigmoid(logits)[0, 0])
        probs = torch.softmax(logits, dim=-1)
        assert self._yes_index is not None
        return float(probs[0, self._yes_index])


@dataclass(frozen=True)
class Fixture:
    text: str
    label: bool
    source: str


def load_fixtures_jsonl(path: Path) -> list[Fixture]:
    """Load ``{"text": ..., "label": true|false}`` rows from a JSON-Lines file."""
    fixtures: list[Fixture] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: line {lineno}: invalid JSON -- {exc}") from exc
        if not isinstance(row, dict) or "text" not in row or "label" not in row:
            raise ValueError(f"{path}: line {lineno}: row missing 'text'/'label'")
        if not isinstance(row["label"], bool):
            raise ValueError(f"{path}: line {lineno}: 'label' must be a strict bool")
        fixtures.append(Fixture(text=str(row["text"]), label=row["label"], source="jsonl"))
    return fixtures


def load_fixtures_allowlist(path: Path) -> tuple[list[Fixture], int, int]:
    """Adjudicated allowlist entries -> ``label=False`` fixture rows.

    Reads through the SAME sanctioned loader every other allowlist consumer
    uses (:func:`athenaeum.pii.load_pii_allowlist`) -- never a hand-rolled
    YAML parse. Returns ``(fixtures, n_pattern_skipped, n_errors)``: a
    ``pattern`` entry names a shape, not a string, so it cannot become a
    fixture row and is counted rather than guessed at; loader errors are
    counted only -- never surfaced as strings.
    """
    from athenaeum.pii import load_pii_allowlist

    entries, errors = load_pii_allowlist(path)
    fixtures: list[Fixture] = []
    n_pattern_skipped = 0
    for entry in entries:
        if entry.value is not None:
            fixtures.append(Fixture(text=entry.value, label=False, source="allowlist"))
        else:
            n_pattern_skipped += 1
    return fixtures, n_pattern_skipped, len(errors)


def regex_gate_verdict(text: str, config: dict[str, Any] | None = None) -> bool:
    """True iff the existing regex sensitivity gate flags *text* as a
    contact-shaped match (issue athenaeum#2049's live comparison baseline).

    Calls :func:`athenaeum.sensitivity.classify` directly -- no allowlist
    consulted here; see this module's docstring for why that would make the
    regex column circular on the allowlist-derived negatives.
    """
    from athenaeum.sensitivity import classify

    return bool(classify(text=text, config=config))


@dataclass(frozen=True)
class RowResult:
    label: bool
    p_yes: float
    predicted: bool
    regex_verdict: bool
    maya_latency_s: float
    regex_latency_s: float


def run_eval(
    scorer: MayaScorer,
    fixtures: Sequence[Fixture],
    *,
    question: str = QUESTION,
    threshold: float = 0.5,
    config: dict[str, Any] | None = None,
) -> list[RowResult]:
    """Score every fixture with *scorer* and the regex gate.

    Timing covers only the inference call for each path -- not fixture
    loading, not model construction.
    """
    results: list[RowResult] = []
    for fx in fixtures:
        start = time.perf_counter()
        p_yes = scorer.p_yes(question, fx.text)
        maya_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        verdict = regex_gate_verdict(fx.text, config)
        regex_elapsed = time.perf_counter() - start

        results.append(
            RowResult(
                label=fx.label,
                p_yes=p_yes,
                predicted=p_yes >= threshold,
                regex_verdict=verdict,
                maya_latency_s=maya_elapsed,
                regex_latency_s=regex_elapsed,
            )
        )
    return results


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile -- defined and well-behaved even at n=1."""
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[rank]


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class Metrics:
    n: int
    n_positive: int
    n_negative: int
    n_scored: int
    n_abstained: int
    tp: int
    fp: int
    tn: int
    fn: int
    accuracy: float | None
    precision: float | None
    recall: float | None
    brier: float
    regex_tp: int
    regex_fp: int
    regex_tn: int
    regex_fn: int
    regex_accuracy: float | None
    regex_precision: float | None
    regex_recall: float | None
    mean_latency_s: float
    p95_latency_s: float
    regex_mean_latency_s: float
    regex_p95_latency_s: float


def compute_metrics(
    results: Sequence[RowResult],
    *,
    abstain_low: float | None = None,
    abstain_high: float | None = None,
) -> Metrics:
    n = len(results)
    n_positive = sum(1 for r in results if r.label)
    n_negative = n - n_positive

    in_band = (
        (lambda p: abstain_low <= p <= abstain_high)
        if abstain_low is not None and abstain_high is not None
        else (lambda p: False)
    )

    tp = fp = tn = fn = 0
    n_abstained = 0
    for r in results:
        if in_band(r.p_yes):
            n_abstained += 1
            continue
        if r.predicted and r.label:
            tp += 1
        elif r.predicted and not r.label:
            fp += 1
        elif not r.predicted and not r.label:
            tn += 1
        else:
            fn += 1
    n_scored = tp + fp + tn + fn

    regex_tp = sum(1 for r in results if r.regex_verdict and r.label)
    regex_fp = sum(1 for r in results if r.regex_verdict and not r.label)
    regex_tn = sum(1 for r in results if not r.regex_verdict and not r.label)
    regex_fn = sum(1 for r in results if not r.regex_verdict and r.label)

    brier = (
        statistics.fmean((r.p_yes - (1.0 if r.label else 0.0)) ** 2 for r in results)
        if results
        else 0.0
    )

    maya_latencies = sorted(r.maya_latency_s for r in results)
    regex_latencies = sorted(r.regex_latency_s for r in results)

    return Metrics(
        n=n,
        n_positive=n_positive,
        n_negative=n_negative,
        n_scored=n_scored,
        n_abstained=n_abstained,
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        accuracy=_safe_div(tp + tn, n_scored),
        precision=_safe_div(tp, tp + fp),
        recall=_safe_div(tp, tp + fn),
        brier=brier,
        regex_tp=regex_tp,
        regex_fp=regex_fp,
        regex_tn=regex_tn,
        regex_fn=regex_fn,
        regex_accuracy=_safe_div(regex_tp + regex_tn, n),
        regex_precision=_safe_div(regex_tp, regex_tp + regex_fp),
        regex_recall=_safe_div(regex_tp, regex_tp + regex_fn),
        mean_latency_s=statistics.fmean(maya_latencies) if maya_latencies else 0.0,
        p95_latency_s=_percentile(maya_latencies, 0.95),
        regex_mean_latency_s=statistics.fmean(regex_latencies) if regex_latencies else 0.0,
        regex_p95_latency_s=_percentile(regex_latencies, 0.95),
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(
    metrics: Metrics,
    *,
    threshold: float,
    abstain_band: tuple[float, float] | None,
) -> str:
    """Render the aggregate measurement table. NEVER includes a fixture string."""
    lines = [
        "# Maya PII-hazard eval",
        "",
        "Measured against the local Maya yes/no cross-encoder (issue athenaeum#2049). "
        "No fixture strings appear below -- only aggregate counts and rates.",
        "",
        f"- Question: `{QUESTION}`",
        f"- Threshold: {threshold}",
        f"- Fixture count: {metrics.n} ({metrics.n_positive} positive / "
        f"{metrics.n_negative} negative)",
    ]
    if abstain_band is not None:
        abstain_rate = _fmt(_safe_div(metrics.n_abstained, metrics.n))
        lines.append(
            f"- Abstention band: [{abstain_band[0]}, {abstain_band[1]}] -- "
            f"{metrics.n_abstained} row(s) abstained ({abstain_rate} of fixtures), "
            "excluded from the confusion counts below"
        )
    lines += [
        "",
        "## Maya",
        "",
        "| metric | value |",
        "|---|---|",
        f"| scored rows | {metrics.n_scored} / {metrics.n} |",
        f"| accuracy | {_fmt(metrics.accuracy)} |",
        f"| precision | {_fmt(metrics.precision)} |",
        f"| recall | {_fmt(metrics.recall)} |",
        f"| TP / FP / TN / FN | {metrics.tp} / {metrics.fp} / {metrics.tn} / {metrics.fn} |",
        f"| Brier score | {metrics.brier:.4f} |",
        f"| mean latency (s) | {metrics.mean_latency_s:.4f} |",
        f"| p95 latency (s) | {metrics.p95_latency_s:.4f} |",
        "",
        "## Regex gate (`athenaeum.sensitivity.classify`, no allowlist applied)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| accuracy | {_fmt(metrics.regex_accuracy)} |",
        f"| precision | {_fmt(metrics.regex_precision)} |",
        f"| recall | {_fmt(metrics.regex_recall)} |",
        f"| TP / FP / TN / FN | {metrics.regex_tp} / {metrics.regex_fp} / "
        f"{metrics.regex_tn} / {metrics.regex_fn} |",
        f"| mean latency (s) | {metrics.regex_mean_latency_s:.4f} |",
        f"| p95 latency (s) | {metrics.regex_p95_latency_s:.4f} |",
        "",
    ]
    return "\n".join(lines) + "\n"


def resolve_weights_path(explicit: str | None = None) -> Path:
    """Resolve the local Maya weights directory, refusing loudly on any gap.

    Checked BEFORE importing torch/transformers (so a missing-weights
    refusal never pays the import cost, and never has a chance to reach the
    network either way -- zero spend, athenaeum#2049).
    """
    raw = explicit if explicit is not None else os.environ.get(WEIGHTS_PATH_ENV)
    if not raw:
        raise MayaWeightsUnavailable(
            f"{WEIGHTS_PATH_ENV} is not set and --weights-path was not given. This "
            "eval refuses to run without a local weights directory -- no implicit "
            f"download (zero spend, athenaeum#2049). Set {WEIGHTS_PATH_ENV}=/path/to/"
            "maya/weights (or pass --weights-path) and retry."
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise MayaWeightsUnavailable(
            f"{WEIGHTS_PATH_ENV}={raw!r} is not a directory. Download Maya's weights "
            "(VishalMysore/maya on Hugging Face) to a local path first; this script "
            "never fetches them itself."
        )
    return path


def _build_scorer(weights_path: Path) -> MayaScorer:
    """Factory seam: the only place :class:`main` constructs a real adapter.

    Tests override this (not :class:`MayaAdapter` itself) to inject a fake
    :class:`MayaScorer` while still exercising every other line of
    :func:`main` -- arg parsing, fixture loading, the eval loop, metrics,
    and the markdown write (issue athenaeum#2049 AC3).
    """
    return MayaAdapter(weights_path)


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
        "--output", required=True, type=Path, help="Path to write the markdown table."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="P(yes) threshold classifying Maya's verdict as positive (default: 0.5).",
    )
    parser.add_argument(
        "--weights-path",
        type=str,
        default=None,
        help=f"Override {WEIGHTS_PATH_ENV}.",
    )
    parser.add_argument("--abstain-low", type=float, default=None)
    parser.add_argument("--abstain-high", type=float, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.fixtures is None and args.allowlist is None:
        print(
            "refusing to run: at least one of --fixtures/--allowlist is required", file=sys.stderr
        )
        return 2

    try:
        weights_path = resolve_weights_path(args.weights_path)
    except MayaWeightsUnavailable as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 1

    fixtures: list[Fixture] = []
    if args.fixtures is not None:
        fixtures.extend(load_fixtures_jsonl(args.fixtures))
    if args.allowlist is not None:
        allow_fixtures, n_pattern_skipped, n_errors = load_fixtures_allowlist(args.allowlist)
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

    scorer = _build_scorer(weights_path)
    results = run_eval(scorer, fixtures, threshold=args.threshold)
    abstain_band = None
    if args.abstain_low is not None and args.abstain_high is not None:
        abstain_band = (args.abstain_low, args.abstain_high)
    metrics = compute_metrics(results, abstain_low=args.abstain_low, abstain_high=args.abstain_high)
    markdown = render_markdown(metrics, threshold=args.threshold, abstain_band=abstain_band)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
