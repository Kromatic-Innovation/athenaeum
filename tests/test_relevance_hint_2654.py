# SPDX-License-Identifier: Apache-2.0
"""Relevance policy: the classify gate covers hint-derived updates (athenaeum#2654).

A ``candidate_uid`` person-hint answer carries the same category + confidence
pair as an ordinary classify item, so a ``never`` rule can drop a hint-derived
fact, including one routed to the owner page, on both transports. All
fixtures are synthetic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_REASONING_T2_MODEL
from athenaeum.relevance_classify import RELEVANCE_ESCALATE_SYSTEM
from athenaeum.relevance_policy import load_relevance_policy
from athenaeum.tiers import (
    DEFAULT_CLASSIFY_MODEL,
    RELEVANCE_CATEGORY_HINT_PROMPT,
    Tier2ParseStats,
    _render_person_candidates_section,
    parse_tier2_entities,
)
from tests import test_batch_mode as tb
from tests.test_relevance_classify import TRANSPORTS, _raw, _reasons, _run
from tests.test_relevance_gate import _seed

OWNER_UID = "own00001"
OTHER_UID = "oth00001"
#: Scoped to the owner through the subject key (athenaeum#2643).
POLICY = (
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
    f"| never | | personal | {OWNER_UID} |\n"
)
OWNER_RAW = "I prefer window seats on long flights.\n"
OTHER_RAW = "Sam Rowe prefers window seats on long flights.\n"
MARKED = "---\nexplicit_remember: true\n---\n"
TYPES = ["person", "company"]
TAGS = ["active", "personal"]
ACCESS = ["internal"]


def _page(root: Path, uid: str, name: str) -> Path:
    page = root / "wiki" / f"{uid}-{name.lower().replace(' ', '-')}.md"
    page.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\naccess: internal\n"
        "created: '2024-01-01'\nupdated: '2024-01-01'\n---\n\n"
        f"# {name}\n\nOriginal body line.\n"
    )
    return page


def _world(tmp_path: Path, policy: str | None = POLICY) -> tuple[Path, Path, Path]:
    root = _seed(tmp_path, policy)
    owner = _page(root, OWNER_UID, "Riley Quill")
    other = _page(root, OTHER_UID, "Sam Rowe")
    (root / "athenaeum.yaml").write_text(
        f"owner:\n  uid: {OWNER_UID}\n  aliases: [Riley Quill]\n", encoding="utf-8"
    )
    return root, owner, other


class _Responder:
    """Fake classifier: answers with a ``candidate_uid`` hint item."""

    def __init__(self, category: Any, confidence: Any, reasoning: dict[str, Any] | None = None):
        self.category, self.confidence = category, confidence
        self.reasoning = reasoning or {"category": None, "confidence": "low"}
        self.escalations: list[dict[str, Any]] = []
        self.classify_msgs: list[str] = []

    def __call__(self, params: dict[str, Any]) -> str:
        if params["system"] == RELEVANCE_ESCALATE_SYSTEM:
            assert params["model"] == DEFAULT_REASONING_T2_MODEL
            self.escalations.append(params)
            return json.dumps(self.reasoning)
        if params["model"] == DEFAULT_CLASSIFY_MODEL:
            msg = params["messages"][0]["content"]
            self.classify_msgs.append(msg)
            uid = OTHER_UID if "Sam Rowe prefers" in msg else OWNER_UID
            item = {"candidate_uid": uid, "observations": "Prefers window seats."}
            if self.category != "<absent>":
                item["category"] = self.category
            if self.confidence != "<absent>":
                item["confidence"] = self.confidence
            return json.dumps([item])
        return tb._scripted_responder(params)


def _drop_reason(rule_tail: str = "never tag=personal subject=own00001 (rule 1, line 3)") -> str:
    return f"relevance-policy: {rule_tail}"


@TRANSPORTS
def test_never_rule_drops_an_owner_hint_fact_and_records_the_rule(tmp_path, monkeypatch, batch):
    root, owner, _ = _world(tmp_path)
    before = owner.read_text()
    raw = _raw(root, 1, OWNER_RAW)
    responder = _Responder("personal", "high")
    _run(root, monkeypatch, responder, batch=batch)
    assert owner.read_text() == before  # the fact never reached the owner page
    assert not raw.exists()
    assert responder.escalations == []
    assert _reasons(root) == [_drop_reason()]


@TRANSPORTS
def test_same_fact_for_a_different_subject_is_not_dropped(tmp_path, monkeypatch, batch):
    root, owner, other = _world(tmp_path)
    before = owner.read_text()
    _raw(root, 1, OTHER_RAW)
    _run(root, monkeypatch, _Responder("personal", "high"), batch=batch)
    assert "Merged note" in other.read_text()  # rule is keyed on the owner's uid
    assert owner.read_text() == before
    assert _reasons(root) == []


@TRANSPORTS
def test_explicit_remember_is_never_dropped_for_an_owner_hint_fact(tmp_path, monkeypatch, batch):
    root, owner, _ = _world(tmp_path)
    _raw(root, 1, MARKED + OWNER_RAW)
    responder = _Responder("personal", "low")
    _run(root, monkeypatch, responder, batch=batch)
    assert "Merged note" in owner.read_text()
    assert responder.escalations == []
    assert _reasons(root) == []


@TRANSPORTS
def test_unsure_hint_fact_escalates_then_fails_closed(tmp_path, monkeypatch, batch):
    root, owner, _ = _world(tmp_path)
    before = owner.read_text()
    _raw(root, 1, OWNER_RAW)
    responder = _Responder(None, "low", {"category": "personal", "confidence": "low"})
    _run(root, monkeypatch, responder, batch=batch)
    assert len(responder.escalations) == 1
    assert owner.read_text() == before
    assert _reasons(root) == [
        "relevance-unsure: never tag=personal subject=own00001 (rule 1, line 3)"
    ]


@TRANSPORTS
def test_unsure_hint_fact_resolved_by_reasoning_tier_is_kept(tmp_path, monkeypatch, batch):
    root, owner, _ = _world(tmp_path)
    _raw(root, 1, OWNER_RAW)
    responder = _Responder(
        "<absent>", "<absent>", {"category": None, "confidence": "high"}
    )
    _run(root, monkeypatch, responder, batch=batch)
    assert len(responder.escalations) == 1
    assert "Merged note" in owner.read_text()
    assert _reasons(root) == []


def test_batch_parity_with_librarian(tmp_path, monkeypatch):
    """Same inputs, both transports: identical pages, drop reasons, raw disposition."""
    outcomes = []
    for batch in (False, True):
        root, owner, other = _world(tmp_path / ("b" if batch else "s"))
        _raw(root, 1, OWNER_RAW)
        _raw(root, 2, OTHER_RAW)
        responder = _Responder("personal", "high")
        _run(root, monkeypatch, responder, batch=batch)
        outcomes.append(
            (
                "Merged note" in owner.read_text(),
                "Merged note" in other.read_text(),
                _reasons(root),
                sorted(p.name for p in root.joinpath("raw", "sessions").glob("*.md")),
                len(responder.escalations),
            )
        )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][:3] == (False, True, [_drop_reason()])


def test_hint_prompt_block_only_under_a_tag_keyed_never_rule(tmp_path):
    cands = [{"uid": OWNER_UID, "name": "Riley Quill", "role": "owner"}]
    plain = _render_person_candidates_section(cands)
    assert plain == _render_person_candidates_section(cands, None)
    always_only = _seed(
        tmp_path / "a",
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| always | | personal | |\n",
    )
    assert _render_person_candidates_section(
        cands, load_relevance_policy(always_only / "wiki")
    ) == plain
    assert RELEVANCE_CATEGORY_HINT_PROMPT not in plain
    policy = load_relevance_policy(_seed(tmp_path / "b") / "wiki")
    with_policy = _render_person_candidates_section(cands, policy)
    assert RELEVANCE_CATEGORY_HINT_PROMPT in with_policy
    assert "candidate_uid" in with_policy and '"category"' in with_policy


@TRANSPORTS
def test_no_policy_hint_prompt_has_no_category_fields(tmp_path, monkeypatch, batch):
    root, owner, _ = _world(tmp_path, None)
    _raw(root, 1, OWNER_RAW)
    responder = _Responder("<absent>", "<absent>")
    _run(root, monkeypatch, responder, batch=batch)
    assert responder.classify_msgs and "Relevance categories" not in responder.classify_msgs[0]
    assert RELEVANCE_CATEGORY_HINT_PROMPT not in responder.classify_msgs[0]
    assert "Merged note" in owner.read_text()


def test_parse_reads_category_and_confidence_on_a_candidate_uid_item():
    text = json.dumps(
        [
            {
                "candidate_uid": OWNER_UID,
                "observations": "Prefers window seats.",
                "category": "personal",
                "confidence": "HIGH",
            },
            {"candidate_uid": OWNER_UID, "observations": "Bad.", "category": "nope"},
        ]
    )
    cands = [{"uid": OWNER_UID, "name": "Riley Quill"}]
    a, b = parse_tier2_entities(
        text, "ref", TYPES, TAGS, ACCESS, stats=Tier2ParseStats(), person_candidates=cands
    )
    assert a.from_person_hint and (a.category, a.category_confidence) == ("personal", "high")
    assert (b.category, b.category_confidence) == (None, None)
    assert re.fullmatch(r"own\d+", a.existing_uid or "")
