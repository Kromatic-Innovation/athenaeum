# SPDX-License-Identifier: Apache-2.0
"""The ``intake`` block and its pure accept check (athenaeum#2936)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    parse_store_declaration,
)
from athenaeum.store_intake import intake_accepts

BASE: dict[str, Any] = {"version": 2, "store_id": "st_q7m2xk4vwz5d"}


def _parse(intake: Any = ..., **over: Any) -> StoreDeclaration | StoreDeclarationInvalid:
    data = copy.deepcopy(BASE)
    data.update(over)
    if intake is not ...:
        data["intake"] = intake
    return parse_store_declaration(yaml.safe_dump(data))


def _ok(intake: Any, **over: Any) -> StoreDeclaration:
    result = _parse(intake, **over)
    assert isinstance(result, StoreDeclaration), getattr(result, "messages", None)
    return result


def _bad(intake: Any, **over: Any) -> StoreDeclarationInvalid:
    result = _parse(intake, **over)
    assert isinstance(result, StoreDeclarationInvalid)
    return result


POLICY = {
    "default": "refuse",
    "rules": [
        {"verdict": "accept", "type": "person"},
        {"verdict": "accept", "type": "policy", "tag": "hr"},
        {"verdict": "refuse", "tag": "personal"},
    ],
}


def test_parses_default_and_ordered_rules() -> None:
    decl = _ok(POLICY)
    assert decl.intake is not None
    assert decl.intake.default == "refuse"
    assert [(r.verdict, r.type, r.tag) for r in decl.intake.rules] == [
        ("accept", "person", None),
        ("accept", "policy", "hr"),
        ("refuse", None, "personal"),
    ]


def test_last_matching_rule_wins_and_default_applies() -> None:
    decl = _ok(POLICY)
    assert intake_accepts(decl, "person", []).accepted
    assert intake_accepts(decl, "person", []).reason == "rule"
    # person tagged personal: rule 3 matches last and refuses
    got = intake_accepts(decl, "person", ["personal"])
    assert not got.accepted and got.rule == 3
    assert intake_accepts(decl, "policy", ["hr"]).accepted
    got = intake_accepts(decl, "note", [])
    assert not got.accepted and got.reason == "default"


def test_tag_covers_slash_children_as_the_relevance_policy_matches() -> None:
    decl = _ok({"default": "accept", "rules": [{"verdict": "refuse", "tag": "personal"}]})
    assert not intake_accepts(decl, "note", ["personal/travel"]).accepted
    assert not intake_accepts(decl, "note", ["personal"]).accepted
    # a sibling sharing a prefix is not a child
    assert intake_accepts(decl, "note", ["personalities"]).accepted


@pytest.mark.parametrize(
    "intake",
    [
        {"default": "accept", "rules": [{"verdict": "accept", "type": "x", "subject": "u1"}]},
        {"default": "accept", "rules": [{"verdict": "route", "type": "x"}]},
        {"default": "accept", "rules": [{"verdict": "always", "type": "x"}]},
        {"default": "always"},
        {"default": "route"},
        {"rules": []},
        {"default": "accept", "rules": [{"verdict": "accept"}]},
        {"default": "accept", "rules": [{"verdict": "accept", "type": ""}]},
        {"default": "accept", "rules": "nope"},
        {"default": "accept", "extra": 1},
        "accept",
    ],
)
def test_validator_refuses(intake: Any) -> None:
    result = _bad(intake)
    assert any(k.startswith("intake") for k in result.keys), result.messages


def test_version_one_declaration_may_not_carry_an_intake_block() -> None:
    result = _bad(POLICY, version=1)
    assert "intake" in result.keys
    # the same block is fine at the version that introduces it
    assert isinstance(_parse(POLICY, version=2), StoreDeclaration)


def test_a_version_this_install_does_not_support_is_invalid_so_read_only() -> None:
    result = _bad(POLICY, version=3)
    assert "version" in result.keys
    got = intake_accepts(result, "person", [])
    assert not got.accepted and got.reason == "declaration-invalid"
    assert got.problems


def test_version_two_without_intake_is_valid() -> None:
    assert isinstance(_parse(), StoreDeclaration)


def test_unparseable_block_refuses_everything_and_is_reported() -> None:
    result = _bad({"default": "accept", "rules": [{"verdict": "route", "type": "person"}]})
    got = intake_accepts(result, "person", [])
    assert not got.accepted
    assert any(p.key.startswith("intake") for p in got.problems)


def test_no_intake_block_or_no_declaration_accepts_whatever_is_sent() -> None:
    assert intake_accepts(_parse(), "anything", ["x"]).accepted
    got = intake_accepts(None, "anything", [])
    assert got.accepted and got.reason == "no-declaration"
    got = intake_accepts(_parse(), "anything", [])
    assert got.accepted and got.reason == "no-intake-block"


def test_accept_check_is_pure_and_reaches_no_prompt() -> None:
    import athenaeum.store_intake as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    for banned in ("open(", "subprocess", "anthropic", "prompt ="):
        assert banned not in src
    decl = _ok(POLICY)
    assert intake_accepts(decl, "person", ["a"]) == intake_accepts(decl, "person", ["a"])
