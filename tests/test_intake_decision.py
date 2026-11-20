# SPDX-License-Identifier: Apache-2.0
"""The pure intake decision (athenaeum#2797, slice S2 of athenaeum#2625)."""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest

from athenaeum import intake_decision, relevance_classify, relevance_policy
from athenaeum.drops import iter_drops
from athenaeum.intake_decision import (
    CAPTURE_DROP,
    CAPTURE_KEEP,
    CAPTURE_UNSURE,
    REASONS,
    Description,
    StoreInfo,
    decide,
)
from athenaeum.models import ClassifiedEntity
from athenaeum.relevance_policy import parse_relevance_policy
from tests.test_relevance_gate import PERSONAL, TRAVEL, WORK, _pages, _run, _seed

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"
STORE_A = "st_h4r5n2kd6wq3"
STORE_B = "st_q7m2xk4vwz5d"
STORE_C = "st_aaaabbbbcccc"
TYPES = {"person", "policy", "company"}
TAGS = {"personal", "personal/travel", "active", "hr", "hr/health"}

HEAD = "| Verdict | Type | Tag | Subject | Store |\n|---|---|---|---|---|\n"


def _policy(*rows: str):
    return parse_relevance_policy(HEAD + "".join(r + "\n" for r in rows), TYPES, TAGS)


NEVER_PERSONAL = "| never | | personal | | |"
ALWAYS_TRAVEL = "| always | | personal/travel | | |"
ROUTE_HR = f"| route | | hr | | {STORE_A} |"
ROUTE_POLICY = f"| route | policy | | | {STORE_B} |"
ROUTE_HEALTH = "| route | | hr/health | | personal |"
ROUTES = (ROUTE_HR, ROUTE_POLICY, ROUTE_HEALTH, f"| route | | personal | | {STORE_A} |")

POLICIES = {
    "never": (NEVER_PERSONAL,),
    "always": (ALWAYS_TRAVEL,),
    "both": (NEVER_PERSONAL, ALWAYS_TRAVEL),
    "subject-never": ("| never | person | | uid-1 | |",),
    "neither": ("| never | company | | | |",),
}


# -- the pre-change logic, frozen for the equivalence test --------------------


def _old_confident(confidence):
    return confidence == "high"


def _old_reachable_never(policy, type_, tags, subject):
    for rule in policy.rules:
        if (
            rule.verdict == "never"
            and rule.tag is not None
            and rule.matches(type_, tags + [rule.tag], subject)
        ):
            return rule
    return None


def _old_decide(policy, type_, tags, subject, category, confidence, *, can_be_unsure):
    base = policy.match(type_, tags, subject)
    full = policy.match(type_, tags + [category], subject) if category else base
    if full is not None and full.verdict == "never":
        if base is not None and base.number == full.number:
            return "drop", full
        return ("drop", full) if _old_confident(confidence) else ("escalate", full)
    if can_be_unsure and not _old_confident(confidence):
        rule = _old_reachable_never(policy, type_, tags, subject)
        if rule is not None:
            return "escalate", rule
    return "keep", None


def _old_capture(policy, d: Description) -> tuple[str, int | None]:
    """The capture answer the gates computed before the change."""
    if d.classified:
        verdict, rule = _old_decide(
            policy, d.type, list(d.tags), d.subject, d.category, d.confidence, can_be_unsure=True
        )
    else:  # gate_raw_content / gate_update_actions: declared keys, no category
        rule = policy.match(d.type, list(d.tags), d.subject)
        verdict = "drop" if rule is not None and rule.verdict == "never" else "keep"
        rule = rule if verdict == "drop" else None
    if verdict == "keep" or rule is None:
        return CAPTURE_KEEP, None
    if d.explicit:
        return CAPTURE_KEEP, None
    return (CAPTURE_UNSURE if verdict == "escalate" else CAPTURE_DROP), rule.number


def _grid():
    for classified, explicit in itertools.product((False, True), repeat=2):
        categories = (None, "personal", "personal/travel", "active") if classified else (None,)
        confidences = (None, "high", "low") if classified else (None,)
        for type_, tags, subject, category, confidence in itertools.product(
            (None, "person", "policy"),
            ((), ("personal",), ("personal/travel",), ("active",), ("hr",), ("hr/health",)),
            (None, "uid-1"),
            categories,
            confidences,
        ):
            yield Description(
                type=type_,
                tags=tags,
                subject=subject,
                category=category,
                confidence=confidence,
                explicit=explicit,
                classified=classified,
            )


GRID = list(_grid())


# -- purity -------------------------------------------------------------------


def test_intake_decision_imports_nothing_above_it():
    tree = ast.parse((SRC / "intake_decision.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {
        "athenaeum.librarian",
        "athenaeum.batch",
        "athenaeum.mcp_server",
        "athenaeum.storage",
        "athenaeum.provider",
    }
    assert not imported & forbidden
    assert not {m for m in imported if m.startswith("athenaeum")}


# -- equivalence with the pre-change capture logic ----------------------------


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_capture_equals_the_pre_change_logic(name):
    policy = _policy(*POLICIES[name])
    for d in GRID:
        got = decide(d, policy)
        want, number = _old_capture(policy, d)
        assert got.capture == want, d
        if want != CAPTURE_KEEP:
            assert got.rule is not None and got.rule.number == number, d


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_route_rows_do_not_change_capture(name):
    plain = _policy(*POLICIES[name])
    routed = _policy(*POLICIES[name], *ROUTES)
    for d in GRID:
        assert decide(d, routed).capture == decide(d, plain).capture, d


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_empty_stores_always_means_personal(name):
    policy = _policy(*POLICIES[name], *ROUTES)
    for d in GRID:
        for stores in (None, {}):
            got = decide(d, policy, stores, {"company": STORE_A})
            if got.capture == CAPTURE_KEEP:
                assert got.store == "personal", d
            else:
                assert got.store is None


def test_never_beats_route_and_reports_no_store():
    policy = _policy(NEVER_PERSONAL, f"| route | | personal | | {STORE_A} |")
    stores = {STORE_A: StoreInfo(frozenset({"compile"}))}
    got = decide(Description(tags=("personal",)), policy, stores)
    assert got.capture == CAPTURE_DROP
    assert got.store is None and got.type is None


def test_explicit_marker_keeps_and_names_the_overridden_rule():
    policy = _policy(NEVER_PERSONAL)
    got = decide(Description(tags=("personal",), explicit=True), policy)
    assert got.capture == CAPTURE_KEEP and got.overridden is not None
    assert got.overridden.number == 1


# -- unsure capture: escalated once, then dropped ------------------------------


def test_low_confidence_never_is_unsure_and_the_gate_escalates_once(monkeypatch):
    policy = _policy(NEVER_PERSONAL)
    entity = ClassifiedEntity(
        "Ada Example", "person", [], "internal", True, observations="x",
        category="personal", category_confidence="low",
    )
    assert decide(
        Description("person", (), None, "personal", "low", classified=True), policy
    ).capture == CAPTURE_UNSURE
    calls = []

    def fake_escalate(ent, categories, **kwargs):
        calls.append(ent.name)
        return None, False  # the reasoning tier is unsure too

    monkeypatch.setattr(relevance_classify, "escalate_category", fake_escalate)
    out = relevance_classify.gate_classified_entities(
        [entity], policy, raw_ref="r", raw_content="note", client=object()
    )
    assert calls == ["Ada Example"] and out.escalated == 1
    assert out.kept == []
    (drop,) = out.drops
    assert drop.reason.startswith("relevance-unsure: never tag=personal")


# -- the store answer (section 6) ----------------------------------------------


def _stores(**kw):
    return {
        STORE_A: StoreInfo(frozenset({"compile"})),
        STORE_B: StoreInfo(frozenset({"read"})),
        STORE_C: StoreInfo(frozenset({"compile"}), accepts=lambda t, tags: False),
        **kw,
    }


def test_store_default_when_nothing_matches():
    got = decide(Description(tags=("active",)), _policy(ROUTE_HR), _stores())
    assert (got.store, got.reason, got.rule) == ("personal", "default", None)


def test_store_rule_on_declared_keys_with_compile():
    got = decide(Description(tags=("hr",)), _policy(ROUTE_HR), _stores())
    assert (got.store, got.reason) == (STORE_A, "rule")
    assert got.rule is not None and got.rule.number == 1
    assert got.type is None


def test_store_propose_alone_also_suffices():
    stores = {STORE_A: StoreInfo(frozenset({"propose"}))}
    assert decide(Description(tags=("hr",)), _policy(ROUTE_HR), stores).store == STORE_A


def test_store_unsure_when_only_a_low_confidence_category_matches():
    d = Description(category="hr", confidence="low", classified=True)
    got = decide(d, _policy(ROUTE_HR), _stores())
    assert (got.store, got.reason) == ("personal", "unsure")
    sure = Description(category="hr", confidence="high", classified=True)
    assert decide(sure, _policy(ROUTE_HR), _stores()).store == STORE_A


def test_store_no_grant_without_compile_or_propose_or_when_absent():
    policy = _policy(ROUTE_HR)
    held = {STORE_A: StoreInfo(frozenset({"read", "answer"}))}
    for stores in (held, {STORE_A: StoreInfo()}, {}):
        got = decide(Description(tags=("hr",)), policy, stores)
        assert (got.store, got.reason) == ("personal", "no-grant")


def test_store_refuses_when_compiling_and_the_accept_predicate_says_no():
    policy = _policy(f"| route | | hr | | {STORE_C} |")
    got = decide(Description(tags=("hr",)), policy, _stores())
    assert (got.store, got.reason) == ("personal", "store-refuses")


def test_a_failing_accept_predicate_refuses():
    def boom(t, tags):
        raise ValueError("unreadable")

    stores = {STORE_A: StoreInfo(frozenset({"compile"}), accepts=boom)}
    got = decide(Description(tags=("hr",)), _policy(ROUTE_HR), stores)
    assert got.reason == "store-refuses"


def test_mapping_places_a_type_and_a_route_row_wins_over_it():
    mapping = {"policy": STORE_B}
    compile_ = StoreInfo(frozenset({"compile"}))
    stores = {STORE_B: compile_, STORE_A: compile_}
    got = decide(Description(type="policy"), _policy(NEVER_PERSONAL), stores, mapping)
    assert (got.store, got.reason) == (STORE_B, "mapping")
    got = decide(Description(type="policy", tags=("hr",)), _policy(ROUTE_HR), stores, mapping)
    assert (got.store, got.reason) == (STORE_A, "rule")


def test_the_entry_path_can_pin_the_personal_store():
    d = Description(tags=("hr",), personal_only=True)
    got = decide(d, _policy(ROUTE_HR), _stores())
    assert (got.store, got.reason) == ("personal", "path")


def test_an_owner_named_store_is_explicit():
    got = decide(Description(store=STORE_A), _policy(NEVER_PERSONAL), _stores())
    assert (got.store, got.reason) == (STORE_A, "explicit")
    assert decide(Description(store="personal"), _policy(ROUTE_HR), _stores()).reason == "explicit"


def test_a_confident_match_alone_is_not_enough():
    got = decide(Description(tags=("hr",)), _policy(ROUTE_HR), {})
    assert got.store == "personal"


# -- decisions carry no fact text; reason codes are one closed set --------------


def test_a_decision_carries_no_fact_text():
    marker = "ZZ-MARKER-5f1c"
    note = f"Ada Example likes oat milk {marker}"
    d = Description(type="person", tags=("personal",), subject="uid-1", explicit=len(note) > 0)
    got = decide(d, _policy(NEVER_PERSONAL, ROUTE_HR))
    assert marker not in repr(got) and marker not in repr(d)
    assert not any(isinstance(v, str) and marker in v for v in vars(got).values())


def test_reason_codes_are_one_closed_set():
    policy = _policy(NEVER_PERSONAL, *ROUTES)
    stores = _stores()
    mapping = {"person": STORE_A}
    seen = set()
    for d in GRID:
        for override, pinned in itertools.product((None, "personal"), (False, True)):
            variant = Description(
                d.type, d.tags, d.subject, d.category, d.confidence, override,
                d.explicit, d.classified, pinned,
            )
            seen.add(decide(variant, policy, stores, mapping).reason)
    refused = _policy(f"| route | | hr | | {STORE_C} |")
    seen.add(decide(Description(tags=("hr",)), refused, stores).reason)
    seen.add(decide(Description(tags=("hr",)), _policy(ROUTE_HR), {}).reason)
    assert seen <= set(REASONS)
    assert set(REASONS) <= seen
    assert len(REASONS) == len(set(REASONS))


# -- one implementation of the capture logic ------------------------------------


def test_policy_match_is_not_called_for_capture_outside_decide():
    for name in ("relevance_policy.py", "relevance_classify.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert not re.search(r"\bpolicy\.match\(", text), name
    assert re.search(r"\bpolicy\.match\(", (SRC / "intake_decision.py").read_text())


def test_the_gates_call_decide():
    assert relevance_policy.decide is intake_decision.decide
    assert relevance_classify.decide is intake_decision.decide


# -- both transports -------------------------------------------------------------


def test_transport_parity_and_equal_descriptions(tmp_path, monkeypatch):
    results = []
    for batch in (False, True):
        calls: list[Description] = []

        def recorder(description, policy, *args, _calls=calls, **kwargs):
            _calls.append(description)
            return decide(description, policy, *args, **kwargs)

        monkeypatch.setattr(relevance_policy, "decide", recorder)
        monkeypatch.setattr(relevance_classify, "decide", recorder)
        root = _seed(tmp_path / ("b" if batch else "s"))
        sess = root / "raw" / "sessions"
        (sess / "20260101T120000Z-aaaaaaa1.md").write_text(PERSONAL)
        (sess / "20260101T120000Z-aaaaaaa2.md").write_text(TRAVEL)
        (sess / "20260101T120000Z-aaaaaaa3.md").write_text(PERSONAL + WORK)
        rc, _ = _run(root, monkeypatch, batch=batch)
        assert rc == 0
        results.append(
            (
                sorted(p.name for p in _pages(root)),
                sorted(r.reason for r in iter_drops(root / "cache")),
                sorted(repr(c) for c in calls),
            )
        )
    assert results[0] == results[1]
    assert results[0][2], "the gate never called decide"
    assert len(results[0][0]) == 2 and len(results[0][1]) == 2
