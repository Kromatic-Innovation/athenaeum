#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline eval: Maya as an ORDERING signal for the name-collision queue (issue athenaeum#2066).

Vitruvius survey comment on athenaeum#2049 (name-collision section): the
only other yes/no-shaped decision with real queue volume (about 200 items
per nightly run) is name-collision triage --
:func:`athenaeum.name_collisions.resolve_name_collisions`'s ``ambiguous``
collisions, which sit in ``_pending_merges.md`` until a human approves
(same person, fold) or rejects (different people) them. This script asks
Maya, pairwise, "Are these two records about the same person?" and scores
whether P(yes) would put approvals ahead of rejections if the queue were
re-ordered by it.

**Ordering only. Maya never authorises a merge.** The identity-confirmation
poor-fit entry in the same survey comment is explicit: a wrong merge welds
two subjects' histories, and :func:`athenaeum.dimensions.compare_identity`
deliberately carries no confidence parameter for that reason. This script
produces no automatic decision of any kind -- it only measures whether
P(yes) is a good priority key for a human reviewer's existing queue.

**Zero spend, no implicit download**, same contract as the PII-hazard
sibling (``scripts/eval_maya_pii_hazard.py``, issue athenaeum#2049): Maya is
loaded from a local weights directory named by
``ATHENAEUM_MAYA_WEIGHTS_PATH`` (or ``--weights-path``); unset or missing is
a refusal, never a Hugging Face Hub fetch.

**No fixture strings in the output.** The emitted markdown carries only
aggregate counts/rates -- never a fixture's ``text_a``/``text_b``. Fixtures
are built host-side only by ``scripts/build_collision_triage_fixtures.py``
and read from a JSON-Lines file passed via ``--fixtures``; nothing
corpus-derived is committed to this public repo.

**Scorer seam.** athenaeum#2063 (the production ``YesNoScorer`` seam,
``src/athenaeum/yesno.py``) has since merged. Its out-of-process
:class:`~athenaeum.yesno.SubprocessYesNoScorer` is production's isolation
mechanism (token text never touches argv or a log); an offline host-side
eval script has no such exposure, so -- mirroring the PII-hazard sibling's
own choice after the same seam landed -- this script still loads Maya
in-process for direct per-pair latency control, but reuses the SAME
calibration primitives the seam ships: ``athenaeum._yesno_worker.
read_temperature`` and ``p_yes_from_logits`` (issue athenaeum#2063 AC9: "one
shared scoring function... eval and production can no longer disagree on
the weights' own ``maya_config.json`` temperature"). Concretely, this
script imports the PII-hazard sibling's own :class:`MayaAdapter` (via
``importlib.util.spec_from_file_location``, the same pattern
``tests/test_eval_maya_pii_hazard.py`` uses) unmodified -- it already
applies the shared temperature-aware transform -- and adds only a
``token_count`` method for this eval's over-length measurement.

Usage::

    ATHENAEUM_MAYA_WEIGHTS_PATH=/path/to/maya/weights \\
        python scripts/eval_maya_collision_triage.py \\
        --fixtures collision_pairs.jsonl \\
        --output docs/measurements/maya-collision-triage-eval-2026-10.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: The exact yes/no ordering question (issue athenaeum#2066).
QUESTION = "Are these two records about the same person?"

#: Same env var the PII-hazard sibling uses -- one Maya weights contract
#: across every eval script in this repo.
WEIGHTS_PATH_ENV = "ATHENAEUM_MAYA_WEIGHTS_PATH"

#: Each record's own character budget inside the combined pair text passed
#: to the scorer. Maya's cross-encoder input caps at 512 tokens
#: (``max_position_embeddings`` in its own config, per the athenaeum#2049
#: survey) -- concatenating two FULL records with no per-side budget would
#: let a long record A push record B out of the tokenizer's truncation
#: window entirely, silently scoring record A against nothing. ~800 chars
#: is a conservative English-text proxy for ~200 tokens, leaving headroom
#: for the question and the "Record A:"/"Record B:" framing under 512.
_PER_RECORD_CHAR_BUDGET = 800

#: Maya's own input cap (issue athenaeum#2049 survey: "Maya has a hard limit
#: as well. Its input caps at 512 tokens (``max_position_embeddings: 512``
#: in its config)"). Used only to measure the over-length fraction below --
#: never to gate scoring, which already truncates per record above.
MAYA_MAX_TOKENS = 512

_SCRIPT_DIR = Path(__file__).resolve().parent
_PII_HAZARD_SCRIPT = _SCRIPT_DIR / "eval_maya_pii_hazard.py"


def _load_pii_hazard_module() -> Any:
    """Import ``eval_maya_pii_hazard.py`` by path (it is not a package).

    Same ``importlib.util.spec_from_file_location`` pattern
    ``tests/test_eval_maya_pii_hazard.py`` already uses, and the same
    sys.modules pre-registration that module's own docstring explains is
    required for its frozen dataclasses to resolve their defining module.
    """
    spec = importlib.util.spec_from_file_location("eval_maya_pii_hazard", _PII_HAZARD_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_maya_pii_hazard"] = module
    spec.loader.exec_module(module)
    return module


class MayaWeightsUnavailable(RuntimeError):
    """Raised when the local Maya weights directory cannot be resolved."""


class PairScorer(Protocol):
    """Adapter seam the eval loop depends on, never the real model directly.

    A fake implementing this protocol lets the smoke test exercise the full
    script path with no weights, no ``transformers``, no ``torch``, and no
    network (mirrors issue athenaeum#2049 AC3 for this sibling eval).
    """

    def p_yes(self, question: str, text: str) -> float:
        """Return Maya's calibrated P(yes) in ``[0, 1]`` for *text*."""
        ...

    def token_count(self, question: str, text: str) -> int:
        """Return the UNTRUNCATED tokenized length of ``(question, text)``.

        Used only to measure the over-512-token fraction below -- never to
        gate or truncate a real scoring call (that truncation already
        happens via ``_PER_RECORD_CHAR_BUDGET`` before this is called).
        """
        ...


def _build_token_counting_adapter_class(pii_hazard_module: Any) -> type:
    """Thin subclass of the PII-hazard sibling's :class:`MayaAdapter`,
    parameterized by *pii_hazard_module* (the dynamically-loaded sibling
    script) adding only :meth:`token_count` -- used by this eval to measure
    the over-512-token fraction. ``p_yes`` is inherited UNCHANGED: the
    sibling's own ``p_yes`` already applies the shared, temperature-aware
    ``athenaeum._yesno_worker.p_yes_from_logits`` transform (issue
    athenaeum#2063 AC9), so re-deriving the transform here would risk the
    exact "eval and production can disagree on temperature" bug that AC
    was written to prevent.

    Monotonicity note, recorded for calibration honesty rather than
    because it changes anything this eval reports: dividing every logit by
    the same positive constant before a sigmoid/softmax is monotonic in
    the resulting P(yes) -- it changes HOW CONFIDENT the score looks,
    never the RELATIVE ORDER of two pairs' scores. Since this eval's
    headline metrics (AUROC, precision at k) are both rank-based, the
    temperature the sibling applies internally cannot move either number.
    """

    class TokenCountingMayaAdapter(pii_hazard_module.MayaAdapter):  # type: ignore[misc]
        def token_count(self, question: str, text: str) -> int:
            self._ensure_loaded()
            encoded = self._tokenizer(question, text, truncation=False)
            return len(encoded["input_ids"])

    return TokenCountingMayaAdapter


@dataclass(frozen=True)
class PairFixture:
    """One labelled name-collision pair.

    ``label=True`` means the ledger's human decision was ``approve`` (same
    person, folded); ``label=False`` means ``reject`` (different people).
    """

    text_a: str
    text_b: str
    label: bool
    source: str = "ledger"


def load_fixtures_jsonl(path: Path) -> list[PairFixture]:
    """Load ``{"text_a": ..., "text_b": ..., "label": true|false}`` rows."""
    fixtures: list[PairFixture] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: line {lineno}: invalid JSON -- {exc}") from exc
        if not isinstance(row, dict) or not {"text_a", "text_b", "label"} <= row.keys():
            raise ValueError(f"{path}: line {lineno}: row missing text_a/text_b/label")
        if not isinstance(row["label"], bool):
            raise ValueError(f"{path}: line {lineno}: 'label' must be a strict bool")
        fixtures.append(
            PairFixture(
                text_a=str(row["text_a"]),
                text_b=str(row["text_b"]),
                label=row["label"],
                source=str(row.get("source", "ledger")),
            )
        )
    return fixtures


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]


def build_pair_text(text_a: str, text_b: str) -> str:
    """Combine two records into one scorer-facing string.

    Each side is truncated to :data:`_PER_RECORD_CHAR_BUDGET` BEFORE
    concatenation specifically so a long record A cannot push record B out
    of Maya's own 512-token truncation window -- see this module's
    docstring and :data:`_PER_RECORD_CHAR_BUDGET`.
    """
    a = _truncate(text_a, _PER_RECORD_CHAR_BUDGET)
    b = _truncate(text_b, _PER_RECORD_CHAR_BUDGET)
    return f"Record A: {a}\n\nRecord B: {b}"


@dataclass(frozen=True)
class PairResult:
    label: bool
    p_yes: float
    latency_s: float
    over_length: bool


def run_eval(
    scorer: PairScorer,
    fixtures: Sequence[PairFixture],
    *,
    question: str = QUESTION,
) -> list[PairResult]:
    """Score every fixture pair with *scorer*.

    Timing covers only the ``p_yes`` inference call -- not fixture loading,
    not model construction. A warm-up call (result discarded, untimed)
    absorbs the real adapter's first-call lazy model load, mirroring the
    PII-hazard sibling's own ``run_eval``.
    """
    if fixtures:
        warm_text = build_pair_text(fixtures[0].text_a, fixtures[0].text_b)
        scorer.p_yes(question, warm_text)

    results: list[PairResult] = []
    for fx in fixtures:
        pair_text = build_pair_text(fx.text_a, fx.text_b)

        start = time.perf_counter()
        p_yes = scorer.p_yes(question, pair_text)
        elapsed = time.perf_counter() - start

        full_text = f"Record A: {fx.text_a}\n\nRecord B: {fx.text_b}"
        token_len = scorer.token_count(question, full_text)

        results.append(
            PairResult(
                label=fx.label,
                p_yes=p_yes,
                latency_s=elapsed,
                over_length=token_len > MAYA_MAX_TOKENS,
            )
        )
    return results


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[rank]


def compute_auroc(results: Sequence[PairResult]) -> float | None:
    """Mann-Whitney U / rank-sum AUROC, no sklearn dependency.

    ``None`` when either class is empty -- AUROC is undefined, not zero, in
    that case, and this script never fabricates a number for an
    undefined metric (see the measurement doc's "inconclusive" path).
    Ties are handled with mid-rank averaging (the standard AUROC
    tie-breaking rule), so a block of equal ``p_yes`` scores contributes
    its average rank to every member rather than an arbitrary order.
    """
    positives = [r.p_yes for r in results if r.label]
    negatives = [r.p_yes for r in results if not r.label]
    if not positives or not negatives:
        return None

    scored = sorted(results, key=lambda r: r.p_yes)
    ranks: dict[int, float] = {}
    i = 0
    n = len(scored)
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1].p_yes == scored[i].p_yes:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-indexed mid-rank for the tie block
        for k in range(i, j + 1):
            ranks[id(scored[k])] = avg_rank
        i = j + 1

    rank_sum_pos = sum(ranks[id(r)] for r in results if r.label)
    n_pos = len(positives)
    n_neg = len(negatives)
    u_statistic = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u_statistic / (n_pos * n_neg)


def precision_at_k(results: Sequence[PairResult], k: int) -> float | None:
    """Precision at *k* ordering the queue by descending P(yes).

    ``None`` when there are fewer than *k* rows total -- "precision at 25"
    on a 2-row queue is not a measurement, it is an artifact of the
    denominator, so this reports absence rather than a misleadingly small
    number.
    """
    if len(results) < k:
        return None
    ordered = sorted(results, key=lambda r: r.p_yes, reverse=True)
    top_k = ordered[:k]
    return sum(1 for r in top_k if r.label) / k


def precision_at_k_current_order(results: Sequence[PairResult], k: int) -> float | None:
    """Precision at *k* under the CURRENT queue order (ledger order, i.e.
    the order the fixtures were passed in -- see
    :func:`athenaeum.decisions.list_pending_decisions`, which sorts the
    unified decision queue oldest-first by ``created_at``;
    ``build_collision_triage_fixtures.py`` preserves that same ledger order
    when it writes the fixture file, so "first k fixture rows" IS "first k
    items a reviewer would see today").
    """
    if len(results) < k:
        return None
    top_k = results[:k]
    return sum(1 for r in top_k if r.label) / k


@dataclass(frozen=True)
class Metrics:
    n: int
    n_positive: int
    n_negative: int
    auroc: float | None
    precision_at_k: dict[int, float | None]
    precision_at_k_current_order: dict[int, float | None]
    mean_latency_s: float
    p95_latency_s: float
    n_over_length: int
    over_length_fraction: float


#: Reported together, since no fixed default page size was found on
#: `athenaeum.decisions.list_pending_decisions_page` or its MCP callers
#: (issue athenaeum#2066: "look for a default page size... if none exists,
#: report k = 10, 25, and 50").
DEFAULT_K_VALUES = (10, 25, 50)


def compute_metrics(
    results: Sequence[PairResult],
    *,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> Metrics:
    n = len(results)
    n_positive = sum(1 for r in results if r.label)
    n_negative = n - n_positive
    latencies = sorted(r.latency_s for r in results)
    n_over_length = sum(1 for r in results if r.over_length)
    return Metrics(
        n=n,
        n_positive=n_positive,
        n_negative=n_negative,
        auroc=compute_auroc(results),
        precision_at_k={k: precision_at_k(results, k) for k in k_values},
        precision_at_k_current_order={
            k: precision_at_k_current_order(results, k) for k in k_values
        },
        mean_latency_s=statistics.fmean(latencies) if latencies else 0.0,
        p95_latency_s=_percentile(latencies, 0.95),
        n_over_length=n_over_length,
        over_length_fraction=(n_over_length / n) if n else 0.0,
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(
    metrics: Metrics,
    *,
    temperature: float,
) -> str:
    """Render the aggregate measurement table. NEVER includes a fixture string."""
    lines = [
        "# Maya collision-triage ordering eval",
        "",
        "Measured against the local Maya yes/no cross-encoder "
        "(issue athenaeum#2066). No fixture strings appear below -- only "
        "aggregate counts and rates.",
        "",
        f"- Question: `{QUESTION}`",
        f"- Fixture count: {metrics.n} ({metrics.n_positive} same-person / "
        f"{metrics.n_negative} different-person)",
        f"- Temperature applied: {temperature} "
        "(athenaeum._yesno_worker.read_temperature -- the same value "
        "production's YesNoScorer worker applies)",
        "",
        "## Ranking (ordering only -- never a merge decision)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| AUROC | {_fmt(metrics.auroc)} |",
    ]
    for k, value in metrics.precision_at_k.items():
        lines.append(f"| precision@{k} (Maya P(yes) order) | {_fmt(value)} |")
    for k, value in metrics.precision_at_k_current_order.items():
        lines.append(f"| precision@{k} (current queue order) | {_fmt(value)} |")
    lines += [
        "",
        "## Latency",
        "",
        "| metric | value |",
        "|---|---|",
        f"| mean latency (s) | {metrics.mean_latency_s:.4f} |",
        f"| p95 latency (s) | {metrics.p95_latency_s:.4f} |",
        "",
        "## Input length",
        "",
        "| metric | value |",
        "|---|---|",
        f"| pairs exceeding Maya's {MAYA_MAX_TOKENS}-token input | "
        f"{metrics.n_over_length} / {metrics.n} ({_fmt(metrics.over_length_fraction)}) |",
        "",
    ]
    if metrics.auroc is None:
        lines.append(
            "**Inconclusive**: AUROC is undefined (at least one class is empty "
            "in the resolved-ledger population available at run time). See the "
            "measurement doc for the full counts and the recommendation this "
            "implies.\n"
        )
    return "\n".join(lines) + "\n"


def resolve_weights_path(explicit: str | None = None) -> Path:
    """Resolve the local Maya weights directory, refusing loudly on any gap.

    Same contract as the PII-hazard sibling's
    :func:`eval_maya_pii_hazard.resolve_weights_path` -- checked before any
    import of torch/transformers, so a missing-weights refusal never pays
    the import cost and never has a chance to reach the network.
    """
    import os

    raw = explicit if explicit is not None else os.environ.get(WEIGHTS_PATH_ENV)
    if not raw:
        raise MayaWeightsUnavailable(
            f"{WEIGHTS_PATH_ENV} is not set and --weights-path was not given. This "
            "eval refuses to run without a local weights directory -- no implicit "
            "download (issue athenaeum#2066, zero spend). Set "
            f"{WEIGHTS_PATH_ENV}=/path/to/maya/weights (or pass --weights-path) and retry."
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise MayaWeightsUnavailable(
            f"{WEIGHTS_PATH_ENV}={raw!r} is not a directory. Download Maya's weights "
            "(VishalMysore/maya on Hugging Face) to a local path first; this script "
            "never fetches them itself."
        )
    return path


def _build_scorer(weights_path: Path) -> PairScorer:
    """Factory seam: the only place :func:`main` constructs a real adapter.

    Tests override this (not the adapter class itself) to inject a fake
    :class:`PairScorer` while still exercising every other line of
    :func:`main`.
    """
    pii_hazard_module = _load_pii_hazard_module()
    adapter_class = _build_token_counting_adapter_class(pii_hazard_module)
    return adapter_class(weights_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        required=True,
        type=Path,
        help="JSON-Lines file of {text_a, text_b, label} rows (host-side; never committed).",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Path to write the markdown table."
    )
    parser.add_argument(
        "--weights-path",
        type=str,
        default=None,
        help=f"Override {WEIGHTS_PATH_ENV}.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        weights_path = resolve_weights_path(args.weights_path)
    except MayaWeightsUnavailable as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 1

    fixtures = load_fixtures_jsonl(args.fixtures)
    if not fixtures:
        print("refusing to run: no fixture rows loaded", file=sys.stderr)
        return 2

    from athenaeum._yesno_worker import read_temperature

    temperature = read_temperature(weights_path)
    scorer = _build_scorer(weights_path)
    results = run_eval(scorer, fixtures)
    metrics = compute_metrics(results)
    markdown = render_markdown(metrics, temperature=temperature)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
