# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 4 (athenaeum#2647): classify category, reasoning-tier
escalation, fail closed, and the Tier-3 update gate -- on both transports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from athenaeum.config import DEFAULT_REASONING_T2_MODEL
from athenaeum.drops import iter_drops
from athenaeum.models import ClassifiedEntity
from athenaeum.relevance_classify import (
    RELEVANCE_ESCALATE_SYSTEM,
    gate_classified_entities,
    parse_escalation_response,
)
from athenaeum.relevance_policy import load_relevance_policy
from athenaeum.tiers import (
    DEFAULT_CLASSIFY_MODEL,
    Tier2ParseStats,
    parse_tier2_entities,
    render_relevance_category_section,
)
from tests import test_batch_mode as tb
from tests.test_relevance_gate import _pages, _seed

SESSIONS = ("raw", "sessions")
TYPES = ["person", "company"]
TAGS = ["active", "personal", "personal/travel"]
ACCESS = ["internal"]


def _classify_item(name: str, category: Any, confidence: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "entity_type": "company",
        "tags": [],
        "access": "internal",
        "observations": f"Facts about {name}.",
    }
    if category != "<absent>":
        item["category"] = category
    if confidence != "<absent>":
        item["confidence"] = confidence
    return item


#: What the (fake) classifier says per entity name, and what the (fake)
#: reasoning tier says when asked. Keyed on the ``Widget<Name>`` in the raw.
CLASSIFY = {
    "WidgetSure": ("personal", "high"),  # confident match
    "WidgetClear": (None, "high"),  # confident non-match
    "WidgetResolves": ("personal", "low"),  # unsure -> reasoning decides
    "WidgetStuck": (None, "low"),  # unsure -> reasoning unsure
    "WidgetUnsaid": ("<absent>", "<absent>"),  # model emitted neither field
}
REASONING = {
    "WidgetResolves": {"category": None, "confidence": "high"},
    "WidgetUnsaid": {"category": "personal", "confidence": "high"},
    "WidgetStuck": {"category": "personal", "confidence": "low"},
}


class _Responder:
    def __init__(self, reasoning: dict[str, Any] | None = None) -> None:
        self.reasoning = REASONING if reasoning is None else reasoning
        self.escalations: list[dict[str, Any]] = []

    def __call__(self, params: dict[str, Any]) -> str:
        msg = params["messages"][0]["content"]
        if params["system"] == RELEVANCE_ESCALATE_SYSTEM:
            self.escalations.append(params)
            name = re.search(r"Widget\w+", msg).group(0)  # type: ignore[union-attr]
            return json.dumps(self.reasoning[name])
        if params["model"] == DEFAULT_CLASSIFY_MODEL:
            m = re.search(r"Widget\w+", msg)
            if not m:
                return "[]"
            name = m.group(0)
            cat, conf = CLASSIFY[name]
            return json.dumps([_classify_item(name, cat, conf)])
        return tb._scripted_responder(params)


def _run(root: Path, monkeypatch, responder, *, batch: bool):
    import anthropic as anthropic_mod

    from athenaeum.librarian import run

    tb._clean_env(monkeypatch)
    tb._patch_uids(monkeypatch)
    tb._freeze_recorded_at(monkeypatch)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(root / "cache"))
    client = tb._FakeClient(responder, allow_sync=True)
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        batch_mode=batch,
    )
    assert rc == 0
    return client


def _raw(root: Path, n: int, text: str) -> Path:
    path = root.joinpath(*SESSIONS) / f"20260101T120000Z-aaaaaaa{n}.md"
    path.write_text(text)
    return path


def _reasons(root: Path) -> list[str]:
    return sorted(r.reason for r in iter_drops(root / "cache"))


TRANSPORTS = pytest.mark.parametrize("batch", [False, True], ids=["sync", "batch"])


@TRANSPORTS
def test_confident_match_is_dropped_without_escalation(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    raw = _raw(root, 1, "Note about WidgetSure.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    assert not raw.exists() and _pages(root) == []
    assert responder.escalations == []
    assert _reasons(root) == ["relevance-policy: never tag=personal (rule 1, line 3)"]


@TRANSPORTS
def test_confident_non_match_is_kept_without_escalation(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    _raw(root, 1, "Note about WidgetClear.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    assert len(_pages(root)) == 1
    assert responder.escalations == []
    assert _reasons(root) == []


@TRANSPORTS
def test_unsure_resolved_by_reasoning_tier_is_kept(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    _raw(root, 1, "Note about WidgetResolves.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    assert len(_pages(root)) == 1 and _reasons(root) == []
    (call,) = responder.escalations
    assert call["model"] == DEFAULT_REASONING_T2_MODEL  # the existing reasoning knob


@TRANSPORTS
def test_unsure_resolved_to_a_never_category_is_dropped(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    raw = _raw(root, 1, "Note about WidgetUnsaid.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    assert not raw.exists() and _pages(root) == []
    assert len(responder.escalations) == 1
    assert _reasons(root) == ["relevance-policy: never tag=personal (rule 1, line 3)"]


@TRANSPORTS
def test_unsure_then_reasoning_unsure_fails_closed(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    raw = _raw(root, 1, "Note about WidgetStuck.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    # Dropped, recorded, and not held: no page, raw consumed.
    assert not raw.exists() and _pages(root) == []
    assert len(responder.escalations) == 1
    (reason,) = _reasons(root)
    assert reason.startswith("relevance-unsure: never tag=personal")


@TRANSPORTS
def test_explicit_remember_marker_is_never_dropped(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    marked = "---\nexplicit_remember: true\n---\n"
    _raw(root, 1, marked + "Note about WidgetSure.\n")
    _raw(root, 2, marked + "Note about WidgetStuck.\n")
    responder = _Responder()
    _run(root, monkeypatch, responder, batch=batch)
    assert len(_pages(root)) == 2
    assert responder.escalations == []  # the override short-circuits the escalation
    assert _reasons(root) == []


def _seed_personal_page(root: Path) -> Path:
    page = root / "wiki" / "acme1234-acme-corp.md"
    page.write_text(
        "---\nuid: acme1234\ntype: company\nname: Acme Corp\naccess: internal\n"
        "tags: [personal]\ncreated: '2024-01-01'\nupdated: '2024-01-01'\n---\n\n"
        "# Acme Corp\n\nOriginal body line.\n"
    )
    return page


@TRANSPORTS
def test_update_onto_a_covered_page_is_dropped(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    page = _seed_personal_page(root)
    before = page.read_text()
    raw = _raw(root, 1, "Acme Corp shipped a new product.\n")
    client = _run(root, monkeypatch, _Responder(), batch=batch)
    assert page.read_text() == before  # no merge reached the page
    assert not raw.exists()
    assert not any("## Existing page content" in str(c) for c in client.sync_calls)
    assert _reasons(root) == ["relevance-policy: never tag=personal (rule 1, line 3)"]


@TRANSPORTS
def test_update_onto_a_covered_page_with_marker_proceeds(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    page = _seed_personal_page(root)
    _raw(root, 1, "---\nexplicit_remember: true\n---\nAcme Corp shipped a new product.\n")
    _run(root, monkeypatch, _Responder(), batch=batch)
    assert "Merged note" in page.read_text()
    assert _reasons(root) == []


def test_batch_parity_with_librarian(tmp_path, monkeypatch):
    """Same inputs, both transports: identical pages, drop reasons and raw disposition."""
    outcomes = []
    for batch in (False, True):
        root = _seed(tmp_path / ("b" if batch else "s"))
        _seed_personal_page(root)
        for n, name in enumerate(
            ["WidgetSure", "WidgetClear", "WidgetResolves", "WidgetUnsaid", "WidgetStuck"],
            start=1,
        ):
            _raw(root, n, f"Note about {name}.\n")
        _raw(root, 6, "Acme Corp shipped a new product.\n")
        responder = _Responder()
        _run(root, monkeypatch, responder, batch=batch)
        outcomes.append(
            (
                sorted(p.name for p in _pages(root)),
                _reasons(root),
                sorted(p.name for p in root.joinpath(*SESSIONS).glob("*.md")),
                len(responder.escalations),
            )
        )
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0][0]) == 3 and len(outcomes[0][1]) == 4 and outcomes[0][3] == 3


def test_no_policy_behaviour_and_prompt_bytes_unchanged(tmp_path, monkeypatch):
    root = _seed(tmp_path, None)
    _raw(root, 1, "Note about WidgetSure.\n")
    responder = _Responder()
    client = _run(root, monkeypatch, responder, batch=False)
    assert len(_pages(root)) == 1 and responder.escalations == []
    classify = next(c for c in client.sync_calls if c["model"] == DEFAULT_CLASSIFY_MODEL)
    assert "Relevance categories" not in classify["messages"][0]["content"]


def test_category_section_only_when_a_tag_keyed_never_rule_exists(tmp_path):
    assert render_relevance_category_section(None) == ""
    always_only = (
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| always | | personal | |\n"
    )
    only_always = _seed(tmp_path / "a", always_only)
    assert render_relevance_category_section(load_relevance_policy(only_always / "wiki")) == ""
    policy = load_relevance_policy(_seed(tmp_path / "b") / "wiki")
    section = render_relevance_category_section(policy)
    assert "- personal\n- personal/travel\n" in section
    assert '"category"' in section and '"confidence"' in section


def test_classify_prompt_carries_the_category_block_under_a_policy(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    _raw(root, 1, "Note about WidgetClear.\n")
    client = _run(root, monkeypatch, _Responder(), batch=False)
    classify = next(c for c in client.sync_calls if c["model"] == DEFAULT_CLASSIFY_MODEL)
    assert "Relevance categories" in classify["messages"][0]["content"]


def test_parse_reads_category_and_confidence_against_the_vocabulary():
    text = json.dumps(
        [
            _classify_item("A One", "personal/travel", "HIGH"),
            _classify_item("B Two", "not-a-tag", "maybe"),
            _classify_item("C Three", "<absent>", "<absent>"),
        ]
    )
    a, b, c = parse_tier2_entities(
        text, "ref", TYPES, TAGS, ACCESS, stats=Tier2ParseStats()
    )
    assert (a.category, a.category_confidence) == ("personal/travel", "high")
    assert (b.category, b.category_confidence) == (None, None)
    assert (c.category, c.category_confidence) == (None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"category": "personal", "confidence": "high"}', ("personal", True)),
        ('{"category": "personal", "confidence": "low"}', ("personal", False)),
        ('{"category": null, "confidence": "high"}', (None, True)),
        ('{"category": "bogus", "confidence": "high"}', (None, False)),
        ("no json at all", (None, False)),
    ],
)
def test_parse_escalation_response(text, expected):
    assert parse_escalation_response(text, ["personal", "active"]) == expected


def test_escalation_call_failure_propagates_rather_than_dropping(tmp_path):
    policy = load_relevance_policy(_seed(tmp_path) / "wiki")

    class Boom:
        class messages:
            @staticmethod
            def create(**_: Any) -> Any:
                raise ValueError("backend exploded")

    entity = ClassifiedEntity(
        "WidgetX", "company", [], "internal", True, observations="x",
        category=None, category_confidence="low",
    )
    with pytest.raises(ValueError, match="backend exploded"):
        gate_classified_entities(
            [entity], policy, raw_ref="r", raw_content="x", client=Boom, config=None
        )


def test_no_policy_gate_is_a_noop():
    entity = ClassifiedEntity("W", "company", [], "internal", True)
    out = gate_classified_entities(
        [entity], None, raw_ref="r", raw_content="x", client=None
    )
    assert out.kept == [entity] and out.drops == []


def test_reasoning_tier_may_resolve_to_an_always_exception(tmp_path):
    # never personal; always personal/travel
    policy = load_relevance_policy(_seed(tmp_path) / "wiki")
    seen: list[dict[str, Any]] = []

    class Client:
        class messages:
            @staticmethod
            def create(**params: Any) -> Any:
                seen.append(params)
                return tb._msg('{"category": "personal/travel", "confidence": "high"}')

    entity = ClassifiedEntity(
        "WidgetTrip", "company", [], "internal", True, observations="prefers aisle",
        category="personal", category_confidence="low",
    )
    out = gate_classified_entities(
        [entity], policy, raw_ref="r", raw_content="x", client=Client, config=None
    )
    assert out.kept == [entity] and out.drops == [] and out.escalated == 1 and len(seen) == 1


def test_a_confident_exception_category_is_kept_without_escalation(tmp_path):
    policy = load_relevance_policy(_seed(tmp_path) / "wiki")
    entity = ClassifiedEntity(
        "WidgetTrip", "company", [], "internal", True,
        category="personal/travel", category_confidence="high",
    )
    out = gate_classified_entities(
        [entity], policy, raw_ref="r", raw_content="x", client=None, config=None
    )
    assert out.kept == [entity] and out.escalated == 0
