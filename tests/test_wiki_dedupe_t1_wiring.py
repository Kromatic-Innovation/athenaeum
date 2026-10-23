# SPDX-License-Identifier: Apache-2.0
"""T1 is wired into the wiki-dedupe pair loop (athenaeum#2093, option a).

T1 is consulted only for a ``duplicate`` verdict that survived the prior-ruling
check (athenaeum#2077) and the confidence floor (athenaeum#2080), is armed only
by its own default-OFF knob, and is advisory + suppress-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.reasoning_tiers import read_reasoning_tier_decisions
from athenaeum.verdict_effects import FOLD_EVIDENCE_DIRNAME
from tests.test_wiki_dedupe import (
    _content_payload,
    _low_cosine_embed,
    _run_pass,
    _seed_identical_pages,
    _seed_low_cosine_pair,
)

_T1_ON = {
    "librarian": {
        "comparator_enabled": True,
        "reasoning_tier_auditing_enabled": True,
        "audit_sample_rate_t1_rejects": 1.0,
    }
}
_T1_OFF = {"librarian": {"comparator_enabled": True}}


def _resp(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _client(t1_verdict: str = "pass_up") -> MagicMock:
    """Call 1 is comparator Gate 2 (``equivalent``); later calls are T1."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _resp(_content_payload("equivalent")),
        _resp(json.dumps({"verdict": t1_verdict, "reason": "fixture"})),
    ] + [_resp(json.dumps({"verdict": t1_verdict, "reason": "fixture"}))] * 4
    return client


def _t1_log(knowledge_root: Path) -> list[dict]:
    return read_reasoning_tier_decisions(knowledge_root / "wiki", tier="T1")


def test_t1_records_a_decision_for_a_pair_that_reaches_the_pass(tmp_path: Path) -> None:
    root = _seed_identical_pages(tmp_path, n=2)
    client = _client("pass_up")
    results = _run_pass(root, config=_T1_ON, client=client)
    assert client.messages.create.call_count == 2  # Gate 2 + one T1 call
    assert len(_t1_log(root)) == 1
    # Advisory: a pass-up changes nothing about the normal enact.
    assert results[0]["action"] == "fold-proposal"
    assert (root / "wiki" / FOLD_EVIDENCE_DIRNAME).is_dir()


def test_t1_reject_suppresses_the_queue_item_and_is_not_rescreened(tmp_path: Path) -> None:
    root = _seed_identical_pages(tmp_path, n=2)
    results = _run_pass(root, config=_T1_ON, client=_client("reject"))
    assert results[0]["action"] == "t1-rejected"
    assert not (root / "wiki" / FOLD_EVIDENCE_DIRNAME).exists()
    assert [r["decision"] for r in _t1_log(root)] == ["reject"]
    # Next run: no Gate 2 redo and no second T1 call for the rejected pair.
    again = _client("reject")
    assert _run_pass(root, config=_T1_ON, client=again) == []
    assert again.messages.create.call_count == 0
    assert len(_t1_log(root)) == 1


def test_t1_is_off_unless_its_own_knob_is_on(tmp_path: Path) -> None:
    root = _seed_identical_pages(tmp_path, n=2)
    client = _client("reject")
    results = _run_pass(root, config=_T1_OFF, client=client)
    assert client.messages.create.call_count == 1
    assert _t1_log(root) == []
    assert results[0]["action"] == "fold-proposal"


def test_t1_spends_nothing_on_a_pair_below_the_confidence_floor(tmp_path: Path) -> None:
    root = _seed_low_cosine_pair(tmp_path)
    client = _client("reject")
    results = _run_pass(
        root,
        config=_T1_ON,
        client=client,
        embedding_provider=_low_cosine_embed,
        threshold=0.55,
    )
    assert results[0]["action"] == "archived-below-confidence-floor"
    assert client.messages.create.call_count == 1  # Gate 2 only
    assert _t1_log(root) == []


def test_t1_spends_nothing_on_a_pair_with_a_prior_ruling(tmp_path: Path) -> None:
    from tests.test_prior_rulings_2077 import RECENT, _rule

    root = _seed_identical_pages(tmp_path, n=2)
    wiki = root / "wiki"
    a, b = sorted(wiki.glob("dup-*.md"))
    _rule(wiki, a, b, "reject", answered_at=RECENT, base_name="dup-0")
    client = _client("reject")
    assert _run_pass(root, config=_T1_ON, client=client) == []
    client.messages.create.assert_not_called()
    assert _t1_log(root) == []


def test_t1_not_run_in_dry_run(tmp_path: Path) -> None:
    root = _seed_identical_pages(tmp_path, n=2)
    client = _client("reject")
    _run_pass(root, config=_T1_ON, client=client, dry_run=True)
    assert _t1_log(root) == []


def test_t1_error_degrades_to_unscreened_and_does_not_abort(tmp_path: Path) -> None:
    root = _seed_identical_pages(tmp_path, n=2)
    client = MagicMock()
    client.messages.create.side_effect = [
        _resp(_content_payload("equivalent")),
        RuntimeError("boom"),
    ]
    results = _run_pass(root, config=_T1_ON, client=client)
    assert results[0]["action"] == "fold-proposal"
