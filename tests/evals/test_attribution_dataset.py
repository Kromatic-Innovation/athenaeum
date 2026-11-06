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

import pytest
import yaml

from athenaeum.config import resolve_attribution_accuracy_floor
from athenaeum.pii_attribution import QUESTION_CLASSES
from tests.evals.data.attribution.generate_cases import (
    _PATTERNS,
    OUTPUT_PATH,
    PATTERN_CASE_COUNTS,
    assign_splits,
    generate_cases,
)
from tests.evals.metrics import wilson_lower_bound_one_sided

#: athenaeum#2344: the computed minimum for the one-sided Wilson lower
#: bound (z = 1.6449) to clear the 0.95 accuracy floor at zero held-out
#: errors -- 52/52 gives ~0.9505 (passes), 51/52 gives ~0.918 (fails). At
#: this corpus's 60/40 fit/holdout split that is 130 cases per class.
MIN_HOLDOUT_PER_CLASS = 52

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


def test_corpus_has_at_least_the_one_sided_bound_s_floor_of_cases() -> None:
    """athenaeum#2344: the one-sided Wilson lower bound (z = 1.6449) only
    clears the 0.95 accuracy floor at zero held-out errors when n_holdout
    >= 52, i.e. >= 130 cases per class at this corpus's 60/40 split -- see
    ``test_every_question_class_meets_the_holdout_floor`` for the per-class
    check. Five question classes x 130 is the floor on the total."""
    cases = _load_cases()
    assert len(cases) >= 650, f"expected at least 650 cases (5 classes x 130), got {len(cases)}"


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


def test_every_question_class_meets_the_holdout_floor() -> None:
    """athenaeum#2344: the computed minimum for the pass bar to be
    reachable at all. For every question class in the committed
    ``cases.yaml``, ``n_holdout`` must be >= 52 (the issue's computed
    minimum: zero held-out errors needs n = 52 for the one-sided Wilson
    lower bound, z = 1.6449, to clear the 0.95 accuracy floor -- 52/52
    gives ~0.9505, 51/52 gives ~0.918), i.e. >= 130 cases per class at this
    corpus's 60/40 fit/holdout split -- and the one-sided bound computed
    with all n_holdout cases correct must itself be at least
    ``resolve_attribution_accuracy_floor(None)``, so the test tracks the
    live floor rather than a hand-copied 0.95.
    """
    cases = _load_cases()
    holdout_counts: dict[str, int] = {}
    for case in cases:
        if case.get("split") == "holdout":
            holdout_counts[case["question_class"]] = (
                holdout_counts.get(case["question_class"], 0) + 1
            )
    floor = resolve_attribution_accuracy_floor(None)
    for question_class in QUESTION_CLASSES:
        n_holdout = holdout_counts.get(question_class, 0)
        assert n_holdout >= MIN_HOLDOUT_PER_CLASS, (
            f"question class {question_class!r} has only {n_holdout} held-out "
            f"cases, need >= {MIN_HOLDOUT_PER_CLASS}"
        )
        best_possible = wilson_lower_bound_one_sided(n_holdout, n_holdout)
        assert best_possible >= floor, (
            f"question class {question_class!r}: n_holdout={n_holdout} gives a "
            f"best-possible one-sided lower bound of {best_possible!r}, below "
            f"the {floor!r} floor even with zero held-out errors"
        )


def test_counter_example_48_holdout_cases_fails_the_holdout_floor() -> None:
    """Counter-example for ``test_every_question_class_meets_the_holdout_floor``:
    a synthetic class with 48 held-out cases (below the 52-case computed
    minimum) must fail, even at zero held-out errors."""
    floor = resolve_attribution_accuracy_floor(None)
    n_holdout = 48
    assert n_holdout < MIN_HOLDOUT_PER_CLASS
    best_possible = wilson_lower_bound_one_sided(n_holdout, n_holdout)
    assert best_possible < floor


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


def test_each_pattern_contributes_its_configured_case_count() -> None:
    """athenaeum#2344: replaced the old flat ``N_PER_PATTERN`` with
    per-pattern counts (:data:`PATTERN_CASE_COUNTS`) sized so every
    question class totals >= 130 cases -- patterns no longer all
    contribute the same count, so each is checked against its own
    configured target rather than one shared constant."""
    cases = _load_cases()
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["pattern"]] = counts.get(case["pattern"], 0) + 1
    expected = {fn.__name__[len("_pattern_") :]: n for fn, n in PATTERN_CASE_COUNTS.items()}
    assert counts == expected


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


# ---------------------------------------------------------------------------
# Gold-label validity (athenaeum#2341): four structural checks over every
# case's gold label, each with its own deliberately-broken counter-example
# fixture so the check is proven to actually reject what it claims to.
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+")


def _case_names(case: dict[str, Any]) -> set[str]:
    names = {case["target"]["name"]}
    for record in (*case["holders"], *case["candidates"]):
        names.add(record["name"])
    return names


def _normalize_text(text: str, names: set[str]) -> str:
    """Erase names and numeric suffixes/uids so two cases that differ only
    by which name/number the generator drew compare equal."""
    normalized = text
    for name in sorted(names, key=len, reverse=True):
        normalized = normalized.replace(name, "<NAME>")
    return _NUMBER_RE.sub("<N>", normalized)


def _case_shape_signature(case: dict[str, Any]) -> tuple[Any, ...]:
    """A case's structural shape with names/uids/numeric suffixes erased.

    Two cases sharing a signature are "identical apart from names, uids
    and numeric suffixes" -- the phrasing of the AC this backs. A parity
    (``i % 2``) split that hands two such cases different gold labels,
    with nothing in the actual inputs to tell them apart, is exactly the
    ``_pattern_role_inbox_unrelated_domain`` defect this exists to catch
    (athenaeum#2341).
    """
    names = _case_names(case)
    checks_shape = tuple(
        (c["check_id"], _normalize_text(c.get("label") or "", names)) for c in case["checks"]
    )
    return (
        case["pattern"],
        bool(case["holders"]),
        bool(case["candidates"]),
        checks_shape,
        _normalize_text(case["value"], names),
        _normalize_text(case["source_excerpt"], names),
    )


def _assert_owner_uid_is_a_case_uid(cases: list[dict[str, Any]]) -> None:
    """(a) ``same-entity``/``other-known`` gold must name one of THIS case's
    own holder/candidate uids -- never the target's own uid, and never a
    uid borrowed from elsewhere."""
    bad = []
    for case in cases:
        if case["gold_verdict"] not in ("same-entity", "other-known"):
            continue
        case_uids = {r["uid"] for r in case["holders"]} | {r["uid"] for r in case["candidates"]}
        if case["gold_owner_uid"] not in case_uids:
            bad.append(case["id"])
    assert not bad, f"gold_owner_uid is not one of the case's own holder/candidate uids: {bad}"


def _assert_identically_shaped_cases_share_a_gold_label(cases: list[dict[str, Any]]) -> None:
    """(b) No two cases that are identical apart from names, uids and
    numeric suffixes may carry different gold labels."""
    by_shape: dict[tuple[Any, ...], set[tuple[str, str | None]]] = {}
    for case in cases:
        shape = _case_shape_signature(case)
        by_shape.setdefault(shape, set()).add((case["gold_verdict"], case["gold_alias_relation"]))
    conflicts = {shape: labels for shape, labels in by_shape.items() if len(labels) > 1}
    assert not conflicts, (
        f"cases identical apart from names/uids/numeric suffixes carry different "
        f"gold labels: {conflicts}"
    )


def _assert_alias_gold_has_a_relation(cases: list[dict[str, Any]]) -> None:
    """(c) No ``alias`` gold may be missing ``gold_alias_relation``."""
    bad = [
        c["id"]
        for c in cases
        if c["gold_verdict"] == "alias" and not c.get("gold_alias_relation")
    ]
    assert not bad, f"alias gold case(s) missing gold_alias_relation: {bad}"


#: Substrings whose presence in an ``own`` case's value/excerpt means the
#: case is actually describing an alias -- a personal alias, a nickname
#: address, or a secondary address -- and so cannot be gold ``own``.
_ALIAS_DESCRIBING_RE = re.compile(
    r"nickname|personal alias|personal nickname|secondary address|shortened surname",
    re.IGNORECASE,
)


def _assert_own_gold_never_describes_an_alias(cases: list[dict[str, Any]]) -> None:
    """(d) No gold ``own`` case's value or excerpt may describe a personal
    alias, nickname address, or secondary address."""
    bad = []
    for case in cases:
        if case["gold_verdict"] != "own":
            continue
        text = f"{case['value']} {case['source_excerpt']}"
        if _ALIAS_DESCRIBING_RE.search(text):
            bad.append(case["id"])
    assert not bad, f"gold own case value/excerpt describes an alias: {bad}"


def test_same_entity_and_other_known_owner_uid_is_a_case_uid() -> None:
    _assert_owner_uid_is_a_case_uid(_load_cases())


def test_identically_shaped_cases_carry_the_same_gold_label() -> None:
    _assert_identically_shaped_cases_share_a_gold_label(_load_cases())


def test_alias_gold_always_carries_an_alias_relation() -> None:
    _assert_alias_gold_has_a_relation(_load_cases())


def test_own_gold_never_describes_an_alias() -> None:
    _assert_own_gold_never_describes_an_alias(_load_cases())


def _broken_case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "broken-000",
        "question_class": "names-other",
        "pattern": "broken",
        "value": "alex.vexmoor@example.org",
        "target": {
            "uid": "attr-eval-9001",
            "name": "Alex Thistlewood",
            "type": "person",
            "aliases": [],
            "employer_domain_hints": [],
        },
        "holders": [],
        "candidates": [],
        "checks": [{"check_id": "C2", "uids": [], "label": "broken"}],
        "source_excerpt": "Alex Thistlewood's broken fixture excerpt.",
        "gold_verdict": "own",
        "gold_owner_uid": None,
        "gold_alias_relation": None,
        "split": "fit",
    }
    base.update(overrides)
    return base


def test_counter_example_inverted_former_name_shape_is_rejected() -> None:
    """The original ``_pattern_two_surnames_former_name`` defect: a
    ``same-entity`` case with no holder/candidate at all, and
    ``gold_owner_uid`` set to the TARGET's own uid instead of a holder's --
    so the owner uid can never be one of the case's own holder/candidate
    uids."""
    broken = _broken_case(
        id="broken-same-entity-inverted",
        gold_verdict="same-entity",
        gold_owner_uid="attr-eval-9001",  # the target's own uid, not a holder's
        holders=[],
        candidates=[],
    )
    try:
        _assert_owner_uid_is_a_case_uid([broken])
    except AssertionError:
        pass
    else:
        raise AssertionError("check (a) failed to reject an inverted same-entity shape")


def test_counter_example_owner_uid_outside_case_uids_is_rejected() -> None:
    """``other-known`` gold pointing at a uid this case never mentions."""
    broken = _broken_case(
        id="broken-other-known-stray-uid",
        gold_verdict="other-known",
        gold_owner_uid="attr-eval-9999",
        holders=[
            {
                "uid": "attr-eval-9002",
                "name": "Jordan Vexmoor",
                "type": "person",
                "aliases": [],
                "employer_domain_hints": [],
            }
        ],
        candidates=[
            {
                "uid": "attr-eval-9002",
                "name": "Jordan Vexmoor",
                "type": "person",
                "aliases": [],
                "employer_domain_hints": [],
            }
        ],
    )
    try:
        _assert_owner_uid_is_a_case_uid([broken])
    except AssertionError:
        pass
    else:
        raise AssertionError("check (a) failed to reject an owner uid outside the case's uids")


def test_counter_example_parity_labelled_pair_is_rejected() -> None:
    """Two cases whose inputs are identical apart from name/uid/number --
    the ``_pattern_role_inbox_unrelated_domain`` defect -- must not carry
    different gold labels."""
    case_a = _broken_case(
        id="broken-parity-000",
        pattern="broken_parity",
        value="info@othercorp-0.example.org",
        target={
            "uid": "attr-eval-9101",
            "name": "Alex Thistlewood",
            "type": "person",
            "aliases": [],
            "employer_domain_hints": [],
        },
        checks=[{"check_id": "D1-role", "uids": [], "label": "role-local-part:info"}],
        source_excerpt="Alex Thistlewood's page cites info@othercorp-0.example.org, a role inbox.",
        gold_verdict="other-unknown",
    )
    case_b = _broken_case(
        id="broken-parity-001",
        pattern="broken_parity",
        value="info@othercorp-1.example.org",
        target={
            "uid": "attr-eval-9102",
            "name": "Jordan Vexmoor",
            "type": "person",
            "aliases": [],
            "employer_domain_hints": [],
        },
        checks=[{"check_id": "D1-role", "uids": [], "label": "role-local-part:info"}],
        source_excerpt="Jordan Vexmoor's page cites info@othercorp-1.example.org, a role inbox.",
        gold_verdict="non-personal",
    )
    try:
        _assert_identically_shaped_cases_share_a_gold_label([case_a, case_b])
    except AssertionError:
        pass
    else:
        raise AssertionError("check (b) failed to reject a parity-labelled identical-shape pair")


def test_counter_example_alias_missing_relation_is_rejected() -> None:
    broken = _broken_case(
        id="broken-alias-no-relation", gold_verdict="alias", gold_alias_relation=None
    )
    try:
        _assert_alias_gold_has_a_relation([broken])
    except AssertionError:
        pass
    else:
        raise AssertionError("check (c) failed to reject an alias case missing gold_alias_relation")


def test_counter_example_own_case_describing_a_nickname_is_rejected() -> None:
    broken = _broken_case(
        id="broken-own-describes-nickname",
        gold_verdict="own",
        source_excerpt=(
            "Alex Thistlewood prefers their personal nickname address, ace0@example.org."
        ),
    )
    try:
        _assert_own_gold_never_describes_an_alias([broken])
    except AssertionError:
        pass
    else:
        raise AssertionError("check (d) failed to reject an own case describing a nickname address")


# ---------------------------------------------------------------------------
# Design §12.4 example record (athenaeum#2344)
# ---------------------------------------------------------------------------

DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "design" / "attribution-verification.md"
)


def _section_12_4_yaml_block() -> str:
    text = DESIGN_DOC_PATH.read_text(encoding="utf-8")
    after_12_4 = text.split("### 12.4 Choosing the tier and storing the result", 1)[1]
    fenced = after_12_4.split("```yaml", 1)[1]
    return fenced.split("```", 1)[0]


def _yaml_number(block: str, key: str) -> float:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([0-9.]+)", block, re.M)
    assert match, f"{key!r} not found in the §12.4 example block"
    return float(match.group(1))


def test_design_doc_example_holdout_wilson_lb_matches_its_own_numbers() -> None:
    """athenaeum#2344: the design doc's §12.4 example record must be
    arithmetically possible under the one-sided Wilson lower bound --
    recompute ``holdout_wilson_lb`` from the example's own ``n_holdout`` and
    ``holdout_accuracy_above`` rather than trusting the literal in the doc,
    so a future hand-edit that drifts the example back out of reach fails
    here instead of shipping silently (the original defect this issue
    fixed: §12.4's prior example, n_holdout=40 / accuracy=0.98, could not
    occur under any Wilson bound at all)."""
    block = _section_12_4_yaml_block()
    n_holdout = int(_yaml_number(block, "n_holdout"))
    accuracy = _yaml_number(block, "holdout_accuracy_above")
    documented_lb = _yaml_number(block, "holdout_wilson_lb")

    successes = round(n_holdout * accuracy)
    recomputed = wilson_lower_bound_one_sided(successes, n_holdout)

    floor = resolve_attribution_accuracy_floor(None)
    assert recomputed >= floor, (
        f"§12.4 example (n_holdout={n_holdout}, accuracy={accuracy}) recomputes to a "
        f"one-sided lower bound of {recomputed!r}, below the {floor!r} floor"
    )
    assert recomputed == pytest.approx(documented_lb, abs=5e-3), (
        f"§12.4's documented holdout_wilson_lb ({documented_lb!r}) does not match the "
        f"recomputed value ({recomputed!r}) for n_holdout={n_holdout}, accuracy={accuracy}"
    )
