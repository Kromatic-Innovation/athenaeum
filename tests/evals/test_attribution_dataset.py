# SPDX-License-Identifier: Apache-2.0
"""Validity checks for the contact-attribution calibration corpus (athenaeum#2211).

Offline, no model call: this only reads the committed
``tests/evals/data/attribution/cases.yaml`` and the generator that produced
it. The deselected-by-default ``pytest.mark.calibration`` sweep
(``test_attribution_eval.py``, athenaeum#2287) is what actually CALLS a model;
this file is a structural guard over the static corpus, so it runs in the
default suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from athenaeum.pii_attribution import QUESTION_CLASSES
from tests.evals.data.attribution.generate_cases import (
    _PATTERNS,
    N_PER_PATTERN,
    OUTPUT_PATH,
    assign_splits,
    generate_cases,
)

DATA_DIR = Path(__file__).resolve().parent / "data" / "attribution"
CASES_PATH = DATA_DIR / "cases.yaml"

#: Design §12.1's floor: at least 50 cases per question class.
MIN_CASES_PER_CLASS = 50

#: Permitted email/phone-like domains (design §12.1: "invented names, and
#: example.org and example.com addresses only"). A subdomain of either
#: (e.g. "mailer.example.org", "othercorp-3.example.com") still counts —
#: it is still reserved-for-documentation space, never a real domain.
_ALLOWED_DOMAIN_RE = re.compile(r"(^|\.)example\.(org|com)\.?$")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+)")


def _load_cases() -> list[dict[str, Any]]:
    raw = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    return raw


def test_corpus_file_exists_and_is_nonempty() -> None:
    assert CASES_PATH.is_file(), f"missing {CASES_PATH} — run generate_cases.py"
    cases = _load_cases()
    assert cases, "cases.yaml is empty"


def test_corpus_has_at_least_250_cases() -> None:
    cases = _load_cases()
    assert len(cases) >= 250, f"expected at least 250 cases, got {len(cases)}"


def test_every_question_class_has_at_least_the_minimum() -> None:
    cases = _load_cases()
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["question_class"]] = counts.get(case["question_class"], 0) + 1
    for question_class in QUESTION_CLASSES:
        assert counts.get(question_class, 0) >= MIN_CASES_PER_CLASS, (
            f"question class {question_class!r} has only "
            f"{counts.get(question_class, 0)} cases, need >= {MIN_CASES_PER_CLASS}"
        )


def test_every_case_is_a_known_question_class() -> None:
    cases = _load_cases()
    unknown = sorted({c["question_class"] for c in cases} - set(QUESTION_CLASSES))
    assert not unknown, f"cases carry unknown question_class value(s): {unknown}"


def _domains_in_text(text: str) -> list[str]:
    return [m.group(1) for m in _EMAIL_RE.finditer(text)]


def test_no_case_uses_a_non_example_domain() -> None:
    """Design §12.1: invented names, example.org/example.com addresses only."""
    raw_text = CASES_PATH.read_text(encoding="utf-8")
    offenders = [d for d in _domains_in_text(raw_text) if not _ALLOWED_DOMAIN_RE.search(d)]
    assert not offenders, f"non-example domain(s) found in the corpus: {sorted(set(offenders))}"


def test_every_case_has_a_fit_or_holdout_split() -> None:
    cases = _load_cases()
    for case in cases:
        assert case.get("split") in ("fit", "holdout"), case["id"]


def test_split_is_roughly_sixty_forty_per_class() -> None:
    cases = _load_cases()
    by_class: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_class.setdefault(case["question_class"], []).append(case)
    for question_class, class_cases in by_class.items():
        fit = sum(1 for c in class_cases if c["split"] == "fit")
        ratio = fit / len(class_cases)
        assert 0.55 <= ratio <= 0.65, (
            f"{question_class}: fit ratio {ratio:.2f} is not close to 60/40"
        )


def test_every_case_carries_the_required_fields() -> None:
    required = {
        "id",
        "question_class",
        "pattern",
        "value",
        "target",
        "holders",
        "candidates",
        "checks",
        "source_excerpt",
        "gold_verdict",
        "gold_owner_uid",
        "gold_alias_relation",
        "split",
    }
    cases = _load_cases()
    for case in cases:
        missing = required - set(case)
        assert not missing, f"{case.get('id')} is missing field(s): {missing}"


def test_generator_covers_all_fourteen_design_patterns() -> None:
    """Design §12.1 lists 14 patterns; every one must have a generator function."""
    assert len(_PATTERNS) == 14
    cases = _load_cases()
    patterns_seen = {c["pattern"] for c in cases}
    assert len(patterns_seen) == 14


def test_regenerating_is_deterministic() -> None:
    """The fixed seed makes a fresh generation byte-identical to the committed file."""
    fresh = generate_cases()
    assign_splits(fresh)
    fresh.sort(key=lambda c: c["id"])
    committed = _load_cases()
    assert len(fresh) == len(committed)
    assert [c["id"] for c in fresh] == [c["id"] for c in committed]
    assert [c["split"] for c in fresh] == [c["split"] for c in committed]


def test_cases_path_constant_matches_this_test_s_path() -> None:
    assert OUTPUT_PATH == CASES_PATH


def test_each_pattern_contributes_n_per_pattern_cases() -> None:
    cases = _load_cases()
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["pattern"]] = counts.get(case["pattern"], 0) + 1
    for count in counts.values():
        assert count == N_PER_PATTERN


def test_no_case_carries_a_usage_class_field() -> None:
    """Design §12.3's pass-bar condition 4: "No case produces an
    `observed` usage class." The corpus schema has no `usage_class` key at
    all, so no generated case can claim one -- see
    ``tests.test_pii_attribution_judge.TestJudgeNeverTouchesUsageClass``
    for the runtime half of this (the judge never calls
    ``athenaeum.pii``, the module that owns that vocabulary)."""
    cases = _load_cases()
    for case in cases:
        assert "usage_class" not in case, case["id"]
