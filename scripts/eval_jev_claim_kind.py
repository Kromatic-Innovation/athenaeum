#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline-harness eval: Jev vs. the current model on the `claim_kind` knob (athenaeum#1998).

athenaeum#1987's desk comparison deliberately excluded accuracy ("a cheaper
wrong answer is not a saving") and found no latency instrumentation anywhere
in the ledger; this script is the first point in the #1987/#1997/#1998 chain
that measures both directly. It runs the SAME 50 (or fewer, if the labelled
corpus is smaller) memory snippets through:

- **Arm A (current model):** :func:`athenaeum.claim_kind.classify_claim_kind`
  unchanged -- the live classify-knob call path, resolved through
  :func:`athenaeum.provider.build_llm_client` exactly as production does.
- **Arm B (Jev):** :class:`athenaeum.decision_provider.JevDecisionBackend`,
  asked a ``choice`` question over the same six `claim_kind` labels, with
  criteria descriptions paraphrased from :data:`athenaeum.claim_kind.CLAIM_KIND_SYSTEM`
  (never copied verbatim).

Both arms see the SAME frontmatter-stripped snippet
(:func:`athenaeum.claim_kind._snippet`) -- the Anthropic arm strips it
internally inside `classify_claim_kind`; the Jev arm's `state` is built with
the identical helper so neither arm's prompt leaks the answer key's own
`claim_kind:` line.

**No corpus mutation.** This script NEVER calls
:func:`athenaeum.claim_kind.stamp_claim_kind` (the write path) -- only the
read-only `classify_claim_kind`. It never writes into the sample directory.
`wiki_root` is passed as ``None`` throughout, so the module's own
observe-schema telemetry (``llm_schemas.record_observation``) lands in the
local cache dir, never under the knowledge root.

**No fixture content in the output.** The emitted markdown and the per-case
log carry only aggregate counts, labels, predictions, token counts, and
latencies -- never a snippet's text, its filename, or any path under the
sample directory. Case identifiers are opaque (`case-01`, `case-02`, ...).

**Answer key.** Sampled from files under ``--sample-dir`` (default
``~/knowledge/raw/auto-memory/``) that already carry an author-supplied
``claim_kind:`` in their frontmatter -- these are treated as human-labelled
per the dispatching issue's framing; see the measurement doc this script
feeds for the stated limitation (no independent provenance check separates
an author-chosen label from one a prior nightly backfill pass may have
stamped before this eval ran; both leave an identical final frontmatter
value, so this script cannot distinguish them from file content alone).
Stratified by kind with a fixed seed (``--seed``, default 42) -- see
:func:`sample_stratified` for the sampling method and its limits when a kind
has fewer available cases than its proportional share.

Usage::

    JEV_API_KEY="$(op read 'op://Agent Tools/Jev Athenaeum Key/credential')" \\
        python scripts/eval_jev_claim_kind.py \\
        --sample-dir ~/knowledge/raw/auto-memory \\
        --network-note "measured from a US home connection, 2026-10-11" \\
        --output docs/measurements/jev-claim-kind-eval-2026-10.md
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: The closed label vocabulary, in a fixed canonical order (mirrors
#: ``athenaeum.models.CLAIM_KINDS`` -- duplicated as a literal tuple, not
#: imported, so the Jev criteria list this script sends has a script-local,
#: order-stable identity independent of set iteration order).
CLAIM_KINDS: tuple[str, ...] = (
    "fact",
    "observation",
    "opinion",
    "decision",
    "policy",
    "definition",
)

#: Jev `choice` instructions -- paraphrased from
#: ``athenaeum.claim_kind.CLAIM_KIND_SYSTEM``, never copied verbatim.
JEV_INSTRUCTIONS = (
    "Classify this memory snippet by its epistemic KIND -- not its topic, and "
    "not whether it is true. Pick exactly one of the listed criteria. Prefer "
    "opinion for any evaluative, preference, or judgment claim."
)

#: Jev `choice` criteria -- a label -> paraphrased-description mapping (the
#: live API rejects a bare list of ids for `choice` with a 422 "criteria must
#: be a dictionary"; verified against the live endpoint 2026-10-11, issue
#: athenaeum#1998 -- a shape gap in `JevDecisionBackend`'s own byte-pinned
#: unit test, which pins only the list form; reported upward, not fixed here).
#: Descriptions paraphrased from ``CLAIM_KIND_SYSTEM``, never copied verbatim.
JEV_CRITERIA: dict[str, str] = {
    "fact": "a verifiable state of the world, true or false independently of who says it",
    "observation": "a first-hand report of something seen, measured, or logged",
    "opinion": "an evaluative stance or preference where reasonable people may disagree",
    "decision": "a timestamped choice with audit value",
    "policy": "a durable prescriptive rule or standing instruction",
    "definition": "fixes the meaning of a name or term",
}

#: Jev question id for this knob.
JEV_QUESTION_ID = "claim_kind"


class ClassifyBackend(Protocol):
    """Adapter seam for Arm A, injected so the smoke test needs no network.

    A fake satisfying this protocol lets the unit test exercise the whole
    script path -- sampling, interleaving, metrics, markdown -- with no
    `ANTHROPIC_API_KEY` and no `anthropic` import.
    """

    def classify(self, text: str) -> tuple[str, int, int]:
        """Return ``(predicted_kind_or_"", input_tokens, output_tokens)``."""
        ...


class DecideBackend(Protocol):
    """Adapter seam for Arm B, injected so the smoke test needs no network."""

    def classify(self, snippet: str) -> tuple[str, int, int]:
        """Return ``(predicted_kind_or_"", input_tokens, output_tokens)``."""
        ...


class AnthropicClassifyAdapter:
    """Real Arm A: calls the live `classify_claim_kind` path unchanged."""

    def __init__(self, client: Any, config: dict[str, Any] | None) -> None:
        self._client = client
        self._config = config

    def classify(self, text: str) -> tuple[str, int, int]:
        from athenaeum.claim_kind import classify_claim_kind
        from athenaeum.models import TokenUsage

        usage = TokenUsage()
        kind = classify_claim_kind(
            text, self._client, config=self._config, usage=usage, wiki_root=None
        )
        return kind, usage.input_tokens, usage.output_tokens


class JevClassifyAdapter:
    """Real Arm B: one Jev `choice` call per case."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def classify(self, snippet: str) -> tuple[str, int, int]:
        result = self._backend.decide(
            question_id=JEV_QUESTION_ID,
            kind="choice",
            instructions=JEV_INSTRUCTIONS,
            state=snippet,
            criteria=JEV_CRITERIA,
        )
        choice = result.choice if isinstance(result.choice, str) else ""
        if choice not in CLAIM_KINDS:
            choice = ""
        return choice, result.input_tokens or 0, result.output_tokens or 0


@dataclass(frozen=True)
class Case:
    """One answer-key row. Carries NO filename -- `case_id` is opaque."""

    case_id: str
    text: str
    label: str


def discover_cases(sample_dir: Path) -> dict[str, list[Case]]:
    """Scan *sample_dir* for files with an author-supplied `claim_kind:`.

    Returns cases grouped by label. Follows symlinks (`rglob` does not, by
    itself, descend into a symlinked subdirectory -- callers on a corpus
    that symlinks every memory directory in, as this project's does, MUST
    pass a directory that itself resolves the symlink, or use
    :func:`_iter_markdown_files`, which does descend through them).
    """
    from athenaeum.models import parse_claim_kind, parse_frontmatter

    by_label: dict[str, list[Case]] = {kind: [] for kind in CLAIM_KINDS}
    for index, path in enumerate(sorted(_iter_markdown_files(sample_dir))):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(content)
        label = parse_claim_kind(meta if meta else None)
        if label not in CLAIM_KINDS:
            continue
        case_id = f"case-src-{index:04d}"
        by_label[label].append(Case(case_id=case_id, text=content, label=label))
    return by_label


def _iter_markdown_files(root: Path) -> list[Path]:
    """``*.md`` files under *root*, descending through symlinked subdirectories.

    Plain ``Path.rglob`` does not follow a symlink it meets partway down, so
    a corpus that symlinks whole memory directories into one root (this
    project's does) undercounts with a bare ``rglob("*.md")``. Cycle-safe via
    a visited-realpath set.
    """
    out: list[Path] = []
    seen_dirs: set[str] = set()

    def _walk(directory: Path) -> None:
        real = str(directory.resolve())
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                _walk(entry)
            elif entry.suffix == ".md":
                out.append(entry)

    _walk(root)
    return out


def sample_stratified(
    by_label: dict[str, list[Case]], n: int, seed: int
) -> tuple[list[Case], dict[str, int]]:
    """Stratify-sample up to *n* cases across the six kinds with a fixed seed.

    Round-robins across kinds in :data:`CLAIM_KINDS` order, one case per kind
    per round, each kind's own pool pre-shuffled with ``random.Random(seed)``
    so the draw is deterministic and reproducible. A kind with fewer cases
    than its even share is exhausted and skipped in later rounds -- the
    shortfall is NOT redistributed to inflate another kind's count beyond
    what rotation would naturally give it, so the returned sample can be,
    and here is, smaller than *n* when the labelled corpus itself is smaller
    than *n*. Returns ``(sample, available_counts)`` -- `available_counts`
    is the per-kind pool size BEFORE sampling, for the doc's limitations
    section.
    """
    rng = random.Random(seed)
    available_counts = {kind: len(cases) for kind, cases in by_label.items()}
    pools: dict[str, list[Case]] = {}
    for kind, cases in by_label.items():
        pool = sorted(cases, key=lambda c: c.case_id)
        rng.shuffle(pool)
        pools[kind] = pool

    sample: list[Case] = []
    cursor = {kind: 0 for kind in CLAIM_KINDS}
    progressed = True
    while len(sample) < n and progressed:
        progressed = False
        for kind in CLAIM_KINDS:
            if len(sample) >= n:
                break
            pool = pools[kind]
            idx = cursor[kind]
            if idx < len(pool):
                sample.append(pool[idx])
                cursor[kind] = idx + 1
                progressed = True
    rng.shuffle(sample)
    return sample, available_counts


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    label: str
    first_arm: str  # "anthropic" | "jev" -- which ran first for this case
    anthropic_pred: str
    anthropic_latency_s: float
    anthropic_input_tokens: int
    anthropic_output_tokens: int
    jev_pred: str
    jev_latency_s: float
    jev_input_tokens: int
    jev_output_tokens: int


def run_eval(
    cases: Sequence[Case],
    anthropic: ClassifyBackend,
    jev: DecideBackend,
) -> list[CaseResult]:
    """Run both arms over *cases*, alternating call order per case.

    Alternating which arm runs first (even index: Anthropic first; odd: Jev
    first) spreads any one-sided connection-warm-up or ordering effect across
    both arms rather than letting it bias one. One untimed warm-up call per
    arm precedes the loop, using the first case's input, discarded before
    timing starts -- covers TLS/connection setup so it does not skew the
    first scored case's latency.
    """
    results: list[CaseResult] = []
    if cases:
        anthropic.classify(cases[0].text)
        from athenaeum.claim_kind import _snippet

        jev.classify(_snippet(cases[0].text))

    from athenaeum.claim_kind import _snippet

    for i, case in enumerate(cases):
        snippet = _snippet(case.text)
        first_arm = "anthropic" if i % 2 == 0 else "jev"

        def _call_anthropic() -> tuple[str, float, int, int]:
            start = time.perf_counter()
            pred, in_tok, out_tok = anthropic.classify(case.text)
            return pred, time.perf_counter() - start, in_tok, out_tok

        def _call_jev() -> tuple[str, float, int, int]:
            start = time.perf_counter()
            pred, in_tok, out_tok = jev.classify(snippet)
            return pred, time.perf_counter() - start, in_tok, out_tok

        if first_arm == "anthropic":
            a_pred, a_lat, a_in, a_out = _call_anthropic()
            j_pred, j_lat, j_in, j_out = _call_jev()
        else:
            j_pred, j_lat, j_in, j_out = _call_jev()
            a_pred, a_lat, a_in, a_out = _call_anthropic()

        results.append(
            CaseResult(
                case_id=case.case_id,
                label=case.label,
                first_arm=first_arm,
                anthropic_pred=a_pred,
                anthropic_latency_s=a_lat,
                anthropic_input_tokens=a_in,
                anthropic_output_tokens=a_out,
                jev_pred=j_pred,
                jev_latency_s=j_lat,
                jev_input_tokens=j_in,
                jev_output_tokens=j_out,
            )
        )
    return results


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile -- well-behaved at n=1."""
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[rank]


@dataclass(frozen=True)
class ArmMetrics:
    n: int
    accuracy: float
    n_unclassified: int
    confusion: dict[tuple[str, str], int]
    mean_latency_s: float
    median_latency_s: float
    p95_latency_s: float
    min_latency_s: float
    max_latency_s: float
    total_input_tokens: int
    total_output_tokens: int


def _arm_metrics(
    results: Sequence[CaseResult],
    *,
    pred_field: str,
    latency_field: str,
    in_tok_field: str,
    out_tok_field: str,
) -> ArmMetrics:
    n = len(results)
    correct = 0
    n_unclassified = 0
    confusion: dict[tuple[str, str], int] = {}
    for r in results:
        pred = getattr(r, pred_field)
        if not pred:
            n_unclassified += 1
        if pred == r.label:
            correct += 1
        key = (r.label, pred or "(unclassified)")
        confusion[key] = confusion.get(key, 0) + 1
    latencies = sorted(getattr(r, latency_field) for r in results)
    return ArmMetrics(
        n=n,
        accuracy=correct / n if n else 0.0,
        n_unclassified=n_unclassified,
        confusion=confusion,
        mean_latency_s=statistics.fmean(latencies) if latencies else 0.0,
        median_latency_s=statistics.median(latencies) if latencies else 0.0,
        p95_latency_s=_percentile(latencies, 0.95),
        min_latency_s=latencies[0] if latencies else 0.0,
        max_latency_s=latencies[-1] if latencies else 0.0,
        total_input_tokens=sum(getattr(r, in_tok_field) for r in results),
        total_output_tokens=sum(getattr(r, out_tok_field) for r in results),
    )


@dataclass(frozen=True)
class EvalSummary:
    n: int
    available_counts: dict[str, int]
    anthropic: ArmMetrics
    jev: ArmMetrics
    agreement_rate: float
    majority_class: str
    majority_class_baseline: float
    anthropic_cost_usd: float
    jev_cost_usd: float


#: Jev's published rate (athenaeum#1987/#1998): USD per million INPUT tokens;
#: output tokens are free.
JEV_USD_PER_MTOK_INPUT = 0.042


def summarize(
    results: Sequence[CaseResult], available_counts: dict[str, int], anthropic_cost_usd: float
) -> EvalSummary:
    n = len(results)
    anthropic = _arm_metrics(
        results,
        pred_field="anthropic_pred",
        latency_field="anthropic_latency_s",
        in_tok_field="anthropic_input_tokens",
        out_tok_field="anthropic_output_tokens",
    )
    jev = _arm_metrics(
        results,
        pred_field="jev_pred",
        latency_field="jev_latency_s",
        in_tok_field="jev_input_tokens",
        out_tok_field="jev_output_tokens",
    )
    agree = sum(1 for r in results if r.anthropic_pred == r.jev_pred and r.anthropic_pred)
    agreement_rate = agree / n if n else 0.0

    label_counts: dict[str, int] = {}
    for r in results:
        label_counts[r.label] = label_counts.get(r.label, 0) + 1
    majority_class = max(label_counts, key=lambda k: label_counts[k]) if label_counts else ""
    majority_class_baseline = (label_counts.get(majority_class, 0) / n) if n else 0.0

    jev_cost_usd = jev.total_input_tokens * JEV_USD_PER_MTOK_INPUT / 1_000_000

    return EvalSummary(
        n=n,
        available_counts=available_counts,
        anthropic=anthropic,
        jev=jev,
        agreement_rate=agreement_rate,
        majority_class=majority_class,
        majority_class_baseline=majority_class_baseline,
        anthropic_cost_usd=anthropic_cost_usd,
        jev_cost_usd=jev_cost_usd,
    )


def _fmt(v: float, digits: int = 3) -> str:
    return f"{v:.{digits}f}"


def _render_confusion(confusion: dict[tuple[str, str], int]) -> list[str]:
    labels = list(CLAIM_KINDS) + ["(unclassified)"]
    lines = ["| true \\ pred | " + " | ".join(labels) + " |", "|" + "---|" * (len(labels) + 1)]
    for true_label in CLAIM_KINDS:
        row = [str(confusion.get((true_label, p), 0)) for p in labels]
        lines.append(f"| {true_label} | " + " | ".join(row) + " |")
    return lines


def render_markdown(
    summary: EvalSummary,
    *,
    network_note: str,
    sample_size_requested: int,
    seed: int,
) -> str:
    """Render the measurement doc body. Never includes a case's text, label
    source filename, or any ``~/knowledge`` path -- aggregate counts only.
    """
    lines = [
        "# Jev vs. current-model eval: `claim_kind` (athenaeum#1998)",
        "",
        "Offline comparison of Jev (TypeSafe AI) against athenaeum's current "
        "`classify` knob on the `claim_kind` classification task -- the "
        "knob this issue's dispatch brief named as the expected highest-volume "
        "candidate, pending confirmation against athenaeum#1987's own table (see "
        '"Volume confirmation" below). No fixture text, filename, or '
        "`~/knowledge` path appears below -- only aggregate counts.",
        "",
        "## Method",
        "",
        f"- Sample: {summary.n} case(s), requested {sample_size_requested}, "
        f"stratified across the six `claim_kind` labels with a fixed seed "
        f"(`--seed {seed}`).",
        "- Answer key: memory files under the sample directory that already "
        "carry an author-supplied `claim_kind:` in frontmatter, treated as "
        "human-labelled per this issue's dispatch framing.",
        "- Both arms see the identical frontmatter-stripped snippet "
        "(`athenaeum.claim_kind._snippet`); Arm A strips it internally, "
        "Arm B's `state` is built with the same helper.",
        "- Arm A (current model): `athenaeum.claim_kind.classify_claim_kind` "
        "unchanged, via `athenaeum.provider.build_llm_client` -- the live "
        "classify-knob call path, never `stamp_claim_kind` (no corpus write).",
        '- Arm B (Jev): `JevDecisionBackend.decide(kind="choice")` with '
        "the six labels as criteria and instructions paraphrased from "
        "`CLAIM_KIND_SYSTEM` (not copied verbatim).",
        "- Call order alternates per case (even index: Anthropic first; odd: "
        "Jev first) to spread any one-sided warm-up/ordering effect across "
        "both arms. One untimed warm-up call precedes each arm's timed loop.",
        f"- Network context: {network_note}",
        "",
        "### Answer-key limitations",
        "",
        "- Available labelled pool by kind: "
        + ", ".join(f"{k}={v}" for k, v in summary.available_counts.items())
        + ".",
    ]
    if summary.n < sample_size_requested:
        lines.append(
            f"- Only {summary.n} labelled case(s) were available against a "
            f"request for {sample_size_requested} -- the sample below is "
            "every eligible case, not a 50-case draw from a larger pool."
        )
    zero_kinds = [k for k, v in summary.available_counts.items() if v == 0]
    if zero_kinds:
        lines.append(
            f"- Zero labelled cases exist for: {', '.join(zero_kinds)}. The "
            "`claim_kind` prompt's own stated load-bearing distinction "
            "(`opinion` must never be overridden by another opinion on "
            "authority alone) is UNTESTED by this run wherever `opinion` is "
            "among the zero-count kinds."
        )
    lines.append(
        '- Provenance of the "human-labelled" framing is not independently '
        "verified here: a file carrying `claim_kind:` in frontmatter is "
        "indistinguishable, from file content alone, between a value an "
        "authoring session chose at write time and one a prior nightly "
        "`stamp_claim_kind` backfill pass wrote onto an originally-unclassified "
        "file -- both leave the identical final frontmatter. This run treats "
        "every such file as the dispatching issue instructed; a case drawn "
        "from the second category would make the Anthropic arm's accuracy on "
        "that case partly self-agreement rather than agreement with an "
        "independent human judgment."
    )
    lines += [
        "",
        "### Volume confirmation",
        "",
        "athenaeum#1987's inventory bundles `claim_kind` inside the `classify` "
        "knob's `librarian` run_type rows and marks per-knob volume **not "
        "separable** from that table -- it does not itself state a `claim_kind`-"
        "specific call count. The dispatching issue named `claim_kind` as the "
        "*expected* highest-volume knob pending this confirmation; this run "
        "does not independently establish that ranking and reports it as "
        "unconfirmed from #1987's table, not as verified.",
        "",
        "## Results",
        "",
        f"- Majority-class baseline: `{summary.majority_class}` is "
        f"{_fmt(summary.majority_class_baseline)} of the sample -- a "
        "classifier scoring at or below this rate is not beating a "
        "single-label guess.",
        f"- Agreement between arms (both non-empty, equal): {_fmt(summary.agreement_rate)}",
        "",
        "### Arm A: current model (classify knob)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| accuracy | {_fmt(summary.anthropic.accuracy)} |",
        f'| unclassified (`""`) | {summary.anthropic.n_unclassified} / {summary.anthropic.n} |',
        f"| mean latency (s) | {_fmt(summary.anthropic.mean_latency_s, 4)} |",
        f"| median latency (s) | {_fmt(summary.anthropic.median_latency_s, 4)} |",
        f"| p95 latency (s) | {_fmt(summary.anthropic.p95_latency_s, 4)} |",
        f"| min / max latency (s) | {_fmt(summary.anthropic.min_latency_s, 4)} / "
        f"{_fmt(summary.anthropic.max_latency_s, 4)} |",
        f"| input / output tokens | {summary.anthropic.total_input_tokens} / "
        f"{summary.anthropic.total_output_tokens} |",
        f"| estimated cost (USD, configured model price) | {_fmt(summary.anthropic_cost_usd, 5)} |",
        "",
        "Confusion matrix (rows: true label, cols: prediction):",
        "",
        *_render_confusion(summary.anthropic.confusion),
        "",
        "### Arm B: Jev (choice)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| accuracy | {_fmt(summary.jev.accuracy)} |",
        f"| unclassified / out-of-vocabulary | {summary.jev.n_unclassified} / {summary.jev.n} |",
        f"| mean latency (s) | {_fmt(summary.jev.mean_latency_s, 4)} |",
        f"| median latency (s) | {_fmt(summary.jev.median_latency_s, 4)} |",
        f"| p95 latency (s) | {_fmt(summary.jev.p95_latency_s, 4)} |",
        f"| min / max latency (s) | {_fmt(summary.jev.min_latency_s, 4)} / "
        f"{_fmt(summary.jev.max_latency_s, 4)} |",
        f"| input / output tokens | {summary.jev.total_input_tokens} / "
        f"{summary.jev.total_output_tokens} |",
        f"| estimated cost (USD, $0.042/MTok input, output free) | "
        f"{_fmt(summary.jev_cost_usd, 5)} |",
        "",
        "Confusion matrix (rows: true label, cols: prediction):",
        "",
        *_render_confusion(summary.jev.confusion),
        "",
    ]
    return "\n".join(lines) + "\n"


def _build_anthropic_backend(config: dict[str, Any] | None) -> ClassifyBackend:
    """Factory seam: the only place `main` constructs the real Arm A client.

    Tests override this (not the client class itself) to inject a fake
    :class:`ClassifyBackend`.
    """
    from athenaeum.provider import build_llm_client

    client = build_llm_client(config, knob="classify")
    if client is None:
        raise RuntimeError(
            "build_llm_client(knob='classify') returned None -- no "
            "ANTHROPIC_API_KEY resolved for the 'api' provider. Refusing to "
            "run Arm A silently against a None client (every case would "
            "return '' with no error)."
        )
    return AnthropicClassifyAdapter(client, config)


def _build_jev_backend() -> DecideBackend:
    """Factory seam: the only place `main` constructs the real Arm B client."""
    import os

    from athenaeum.decision_provider import JevDecisionBackend

    if not os.environ.get("JEV_API_KEY"):
        raise RuntimeError("JEV_API_KEY is not set; refusing to run Arm B silently.")
    return JevClassifyAdapter(JevDecisionBackend())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=Path("~/knowledge/raw/auto-memory").expanduser(),
        help="Directory of memory files to sample the answer key from.",
    )
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output", required=True, type=Path, help="Path to write the markdown report."
    )
    parser.add_argument(
        "--network-note",
        required=True,
        help="One sentence stating the network context latency was measured under.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=50,
        help="Hard cap on calls PER ARM (safety net against a sample-size bug).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    by_label = discover_cases(args.sample_dir)
    sample, available_counts = sample_stratified(by_label, args.sample_size, args.seed)
    if not sample:
        print("refusing to run: no labelled cases found", file=sys.stderr)
        return 2
    if len(sample) > args.max_calls:
        print(
            f"refusing to run: sample size {len(sample)} exceeds --max-calls {args.max_calls}",
            file=sys.stderr,
        )
        return 2

    config: dict[str, Any] | None = None
    try:
        anthropic_backend = _build_anthropic_backend(config)
        jev_backend = _build_jev_backend()
    except RuntimeError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 1

    results = run_eval(sample, anthropic_backend, jev_backend)

    from athenaeum.models import TokenUsage

    anthropic_usage = TokenUsage()
    for r in results:
        anthropic_usage.add(
            r.anthropic_input_tokens,
            r.anthropic_output_tokens,
            model="claude-haiku-4-5-20251001",
            knob="classify",
        )
    anthropic_cost_usd = anthropic_usage.estimated_cost_usd

    summary = summarize(results, available_counts, anthropic_cost_usd)
    markdown = render_markdown(
        summary,
        network_note=args.network_note,
        sample_size_requested=args.sample_size,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
