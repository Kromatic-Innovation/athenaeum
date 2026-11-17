# SPDX-License-Identifier: Apache-2.0
"""Offline checks for the per-type guidance eval harness (issue athenaeum#2715).

Unmarked, so it runs in the default suite with no credential: the live half
(``test_merge_eval.py`` and ``python -m tests.evals.type_guidance_eval``) is
deselected by default, and without this file CI would only prove the module
imports. It pins the harness invariants a live run depends on.
"""

from __future__ import annotations

from athenaeum import tiers, type_guidance
from tests.conftest import FakeLLMClient
from tests.evals import type_guidance_eval as T


def test_planted_phrases_are_verbatim_in_the_page() -> None:
    assert all(p in T.PAGE_BODY for p in T.RULE_PHRASES)
    assert all(T.retained(T.PAGE_BODY))


def test_guidance_reaches_the_prompt_only_when_the_fragment_exists(tmp_path) -> None:
    action = T._action("page", "obs")
    for arm, expect in (("off", False), ("benign", True)):
        root = T.make_wiki(tmp_path / arm, T.ARMS[arm]["guidance"])
        params = tiers.tier3_merge_params(
            action, T.PAGE_BODY, "s.md", wiki_root=root, target_type=T.ENTITY_TYPE
        )
        assert ("Guidance for `preference` pages" in params["messages"][0]["content"]) is expect


def test_noclause_arm_blanks_the_precedence_clause_and_restores_it() -> None:
    original = type_guidance.TYPE_GUIDANCE_PROMPT
    seen: list[str] = []

    def responder(**params):
        seen.append(params["messages"][0]["content"])
        return "ADDS_NEW_CLAIM: false"

    client = FakeLLMClient(responder=responder)
    T.run_batch(
        client,
        arms=["benign", "benign_noclause"],
        modes=["full"],
        chain_reps=0,
        escalate_reps=1,
        workers=1,
    )
    assert type_guidance.TYPE_GUIDANCE_PROMPT == original
    with_clause = [s for s in seen if "subordinate to the rules above" in s]
    assert with_clause and len(with_clause) < len(seen)


def test_empty_full_echo_body_does_not_wipe_the_chain_state() -> None:
    # A full-echo reply whose ADDS_NEW_CLAIM line carries trailing prose and no
    # body parses to "" (callers treat a falsy body as "no write"); the chain
    # must keep the previous body, not replace the page with "".
    client = FakeLLMClient(text="ADDS_NEW_CLAIM: false -- re-confirmation only")
    rec = T.run_chain(client, "full", "off", 0)
    assert all(all(s["retained"]) for s in rec["steps"])
    assert all(s["empty_body"] for s in rec["steps"])


def test_wilson_interval_is_clamped_and_ordered() -> None:
    lo, hi = T.wilson(0, 20)
    assert lo == 0.0 and 0.0 < hi < 0.2
    lo, hi = T.wilson(20, 20)
    assert hi > 0.999 and 0.8 < lo < 1.0
    assert T.wilson(0, 0) == (0.0, 0.0)
