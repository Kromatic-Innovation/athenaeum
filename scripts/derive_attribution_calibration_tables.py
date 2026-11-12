#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Derive the per-(model, class, signal) tables in
``docs/measurements/attribution-calibration-2026-11-11.md`` (athenaeum#2260).

Offline: reads the ``calibration-summary.json`` and the
``calibration-recorded-fixtures`` artifact a live ``evals.yml``
``suite=calibration`` run uploaded, and rebuilds the harness's own
:class:`tests.evals.attribution_eval.CaseRun` lists from them. Every metric is
computed by the repo's own code (``tests.evals.metrics``,
``tests.evals.attribution_eval``, :class:`athenaeum.models.TokenUsage`), never
reimplemented here, so the numbers are the ones the pass bar used. It also
cross-checks the re-selected per-class report against the committed record and
exits non-zero on any mismatch.

    python scripts/derive_attribution_calibration_tables.py \\
        <calibration-summary.json> <recorded-fixtures-dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from athenaeum import config as config_module  # noqa: E402
from athenaeum.models import TokenUsage  # noqa: E402
from athenaeum.pii_attribution import (  # noqa: E402
    QUESTION_CLASSES,
    SIGNAL_AGREEMENT_K3,
    SIGNAL_CLASS_LEVEL,
    SIGNAL_SELF_REPORTED,
)
from tests.evals import attribution_eval as ae  # noqa: E402
from tests.evals.metrics import (  # noqa: E402
    cost_per_case,
    wilson_interval,
)

RECORD = REPO_ROOT / "src" / "athenaeum" / "calibration_data" / "contact_attribution.yaml"


def _kv(detail: str) -> dict[str, str]:
    """Parse the leading ``key=value`` tokens of a case ``detail`` string
    (stops at the first token that is not ``key=value``)."""
    out: dict[str, str] = {}
    for tok in detail.split():
        k, sep, v = tok.partition("=")
        if sep:
            out.setdefault(k, v)
    return out


def load_runs(summary: dict) -> dict[tuple[str, str, str], list[ae.CaseRun]]:
    """(model, class, signal) -> CaseRuns, signal in {self-reported, agreement-k3}."""
    runs: dict[tuple[str, str, str], list[ae.CaseRun]] = defaultdict(list)
    for row in summary["cases"]:
        cid = row["case_id"]
        if cid.startswith("report::") or "::agreement::run" in cid:
            continue
        model, _, rest = cid.partition("::")
        is_agree = rest.endswith("::agreement")
        case_id = rest.removesuffix("::agreement")
        kv = _kv(row["detail"])
        score = float(kv["agreement_score" if is_agree else "score"])
        run = ae.CaseRun(
            case_id=case_id,
            question_class=kv["question_class"],
            model=model,
            signal=SIGNAL_AGREEMENT_K3 if is_agree else SIGNAL_SELF_REPORTED,
            gold_verdict=row["expected"],
            judgement_verdict=row["observed"],
            score=score,
            correct=bool(row["passed"]),
            split=kv["split"],
        )
        runs[(model, run.question_class, run.signal)].append(run)
    return runs


def load_usage(fixtures: Path, class_of: dict[str, str]) -> dict[tuple[str, str, str], TokenUsage]:
    """(model, class, 'self'|'agree') -> TokenUsage from per-call fixture usage."""
    usage: dict[tuple[str, str, str], TokenUsage] = defaultdict(TokenUsage)
    for path in sorted((fixtures / "attribution").glob("*.json")):
        stem = path.stem.split("__")
        kind, model, case_id = stem[0], stem[1], stem[2]
        u = json.loads(path.read_text(encoding="utf-8"))["usage"]
        usage[(model, class_of[case_id], kind)].add_tokens(
            u["input_tokens"],
            u["output_tokens"],
            u.get("cache_creation_input_tokens", 0),
            u.get("cache_read_input_tokens", 0),
            model=model,
        )
    return usage


def _acc(rs: list[ae.CaseRun]) -> tuple[int, int]:
    return sum(r.correct for r in rs), len(rs)


def _short(model: str) -> str:
    return model.removeprefix("claude-").removesuffix("-20251001")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("summary", type=Path)
    ap.add_argument("fixtures", type=Path)
    args = ap.parse_args()

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    floor = config_module.resolve_attribution_accuracy_floor(None)
    runs = load_runs(summary)
    class_of = {r.case_id: r.question_class for rs in runs.values() for r in rs}
    usage = load_usage(args.fixtures, class_of)
    models = sorted({m for m, _, _ in runs})

    print(
        "| model | class | signal | n | accuracy | Wilson 95% | AUROC (fit) | threshold (fit) "
        "| holdout acc above | Wilson LB (1-sided) | abstain | ECE (holdout) | $/case |"
    )
    print("| --- " * 13 + "|")
    reliability: list[str] = []
    mismatches: list[str] = []
    recorded = {e["question_class"]: e for e in yaml.safe_load(RECORD.read_text())}
    selected: dict[str, list[ae.ClassReport]] = defaultdict(list)

    for qc in QUESTION_CLASSES:
        for model in models:
            self_all = runs[(model, qc, SIGNAL_SELF_REPORTED)]
            agree_all = runs.get((model, qc, SIGNAL_AGREEMENT_K3), [])
            sf = [r for r in self_all if r.split == "fit"]
            sh = [r for r in self_all if r.split == "holdout"]
            af = [r for r in agree_all if r.split == "fit"]
            ah = [r for r in agree_all if r.split == "holdout"]
            self_cost = cost_per_case(usage[(model, qc, "self")], len(self_all))
            agree_cost = cost_per_case(usage[(model, qc, "agree")], len(agree_all))

            variants = [(SIGNAL_SELF_REPORTED, sf, sh, self_all, self_cost)]
            if agree_all:
                variants.append((SIGNAL_AGREEMENT_K3, af, ah, agree_all, agree_cost))
            cl_f = [ae.dataclasses.replace(r, score=1.0) for r in sf]
            cl_h = [ae.dataclasses.replace(r, score=1.0) for r in sh]
            variants.append((SIGNAL_CLASS_LEVEL, cl_f, cl_h, self_all, self_cost))

            for signal, fit, hold, allr, cost in variants:
                k, n = _acc(allr)
                lo, hi = wilson_interval(k, n)
                if signal == SIGNAL_CLASS_LEVEL:
                    au, au_txt = None, "n/a (constant score)"
                else:
                    au, why = ae.self_reported_auroc_or_reason(fit)
                    au_txt = f"{au:.3f}" if au is not None else f"undefined ({why})"
                rep = ae._report_for_signal(
                    qc,
                    model,
                    signal,
                    fit,
                    hold,
                    accuracy_floor=floor,
                    auroc_value=au,
                    has_ece=signal != SIGNAL_CLASS_LEVEL,
                )
                if rep.threshold is None:
                    thr, hacc, wlb, ab = (
                        "none reachable",
                        "n/a",
                        "0 (no threshold)",
                        "1.0 (no threshold)",
                    )
                else:
                    thr = f"{rep.threshold:.4g}"
                    hacc, wlb, ab = (
                        f"{rep.holdout_accuracy_above:.3f}",
                        f"{rep.holdout_wilson_lb:.4f}",
                        f"{rep.abstain_share:.3f}",
                    )
                if signal == SIGNAL_CLASS_LEVEL:
                    ece = "n/a (class-level, §12.3)"
                elif rep.threshold is None:
                    ece = "n/a (no threshold)"
                else:
                    ece = f"{rep.ece:.4f}"
                    reliability.append(
                        f"| {_short(model)} | {qc} | {signal} | "
                        + " ; ".join(f"[{b[0]:.2f}, {b[1]:.2f}] {b[2]:.3f}" for b in rep.bins)
                        + " |"
                    )
                print(
                    f"| {_short(model)} | {qc} | {signal} | {n} | {k / n:.3f} | "
                    f"[{lo:.3f}, {hi:.3f}] | {au_txt} | {thr} | {hacc} | {wlb} | {ab} | {ece} | "
                    f"{cost:.5f} |"
                )

            # The harness's own selection, cross-checked against the record.
            selected[qc].append(
                ae.select_signal_and_report(
                    qc,
                    model,
                    accuracy_floor=floor,
                    self_reported_fit=sf,
                    self_reported_holdout=sh,
                    agreement_fit=af or None,
                    agreement_holdout=ah or None,
                )
            )

    print("\nReliability bins (holdout, equal-count): [lo, hi] observed accuracy\n")
    print("| model | class | signal | bins |\n| --- | --- | --- | --- |")
    print("\n".join(reliability))

    print("\nRe-selected record vs committed record\n")
    entries = ae.to_calibration_entries(
        selected, dataset_sha256="", prompt_sha256="", measured_at=""
    )
    for e in entries:
        r = recorded[e["question_class"]]
        for key in (
            "model",
            "signal",
            "threshold",
            "n_fit",
            "n_holdout",
            "holdout_accuracy_above",
            "holdout_wilson_lb",
            "abstain_share",
            "passes",
        ):
            a, b = e[key], r[key]
            same = abs(a - b) < 1e-9 if isinstance(a, float) else a == b
            if not same:
                mismatches.append(f"{e['question_class']}.{key}: rederived={a!r} record={b!r}")
        print(
            f"- {e['question_class']}: {e['model']} {e['signal']} thr={e['threshold']} "
            f"lb={e['holdout_wilson_lb']:.4f} abstain={e['abstain_share']} passes={e['passes']}"
        )
    if mismatches:
        print("\nMISMATCH:\n" + "\n".join(mismatches))
        return 1
    print(
        "\nOK: threshold, held-out Wilson LB, abstain share (and model/signal/passes) "
        "match the committed record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
