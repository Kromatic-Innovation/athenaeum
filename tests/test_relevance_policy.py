# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 1 (athenaeum#2643): file, loader, validator, read-back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.librarian import _render_run_summary
from athenaeum.relevance_policy import (
    POLICY_FILENAME,
    RelevancePolicyError,
    load_relevance_policy,
    policy_hash_token,
    render_policy,
)

TAGS = (
    "| Tag | Usage |\n|---|---|\n| active | x |\n| personal | x |\n"
    "| personal/travel | x |\n| food | x |\n"
)
TYPES = "| Type | D |\n|---|---|\n| person | x |\n| preference | x |\n"


def _wiki(tmp_path: Path, policy: str | None = None) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "tags.md").write_text(TAGS)
    (wiki / "_schema" / "types.md").write_text(TYPES)
    if policy is not None:
        (wiki / "_schema" / POLICY_FILENAME).write_text(policy)
    return wiki


def _table(*rows: str) -> str:
    return (
        "# Relevance policy\n\nprose is ignored.\n\n"
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
        + "".join(r + "\n" for r in rows)
    )


EXAMPLE = _table("| never | | personal | |", "| always | | personal/travel | |")


def test_exception_after_general_rule_wins(tmp_path):
    pol = load_relevance_policy(_wiki(tmp_path, EXAMPLE))
    assert pol.resolve(tags=["personal/travel"]) == "always"
    assert pol.resolve(tags=["personal"]) == "never"
    assert pol.resolve(tags=["personal", "personal/travel"]) == "always"
    assert pol.resolve(tags=["food"]) is None
    assert pol.resolve() is None


def test_order_is_decisive(tmp_path):
    reversed_ = _table("| always | | personal/travel | |", "| never | | personal | |")
    pol = load_relevance_policy(_wiki(tmp_path, reversed_))
    assert pol.resolve(tags=["personal/travel"]) == "never"


def test_type_and_subject_keys_all_must_match(tmp_path):
    pol = load_relevance_policy(
        _wiki(
            tmp_path,
            _table(
                "| never | preference | food | |",
                "| always | preference | food | ab12cd34 |",
                "| never | - | - | zz99 |",
            ),
        )
    )
    assert pol.resolve("preference", ["food"], None) == "never"
    assert pol.resolve("preference", ["food"], "ab12cd34") == "always"
    assert pol.resolve("person", ["food"], None) is None
    assert pol.resolve("person", [], "zz99") == "never"


@pytest.mark.parametrize(
    "row, needle",
    [
        ("| never | | bogus-tag | |", "rule 1 (line 7): unknown tag 'bogus-tag'"),
        ("| never | bogus-type | | |", "rule 1 (line 7): unknown type 'bogus-type'"),
        ("| maybe | | food | |", "rule 1 (line 7): unknown verdict 'maybe'"),
        ("| never | | | |", "rule 1 (line 7): names no type, tag or subject"),
    ],
)
def test_validation_errors_name_the_rule(tmp_path, row, needle):
    with pytest.raises(RelevancePolicyError) as exc:
        load_relevance_policy(_wiki(tmp_path, _table(row)))
    assert needle in str(exc.value)


def test_all_bad_rules_are_reported(tmp_path):
    text = _table("| never | | food | |", "| never | | nope | |", "| always | x | | |")
    with pytest.raises(RelevancePolicyError) as exc:
        load_relevance_policy(_wiki(tmp_path, text))
    msg = str(exc.value)
    assert "rule 2" in msg and "rule 3" in msg and "rule 1" not in msg


def test_wrong_header_is_an_error(tmp_path):
    with pytest.raises(RelevancePolicyError, match="header"):
        load_relevance_policy(_wiki(tmp_path, "| A | B |\n|---|---|\n| x | y |\n"))


def test_missing_file_is_a_noop(tmp_path):
    wiki = _wiki(tmp_path)
    pol = load_relevance_policy(wiki)
    assert not pol.present and pol.rules == ()
    assert pol.resolve("person", ["personal"], "x") is None
    assert policy_hash_token(wiki) is None
    assert "relevance_policy" not in _render_run_summary(
        [("entity", 1.0, {})], relevance_policy_hash=policy_hash_token(wiki)
    )
    # no wiki/_schema dir at all
    assert not load_relevance_policy(tmp_path / "nowhere").present


def test_vocabulary_falls_back_when_schema_files_absent(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / POLICY_FILENAME).write_text(_table("| never | person | active | |"))
    assert load_relevance_policy(wiki).resolve("person", ["active"]) == "never"


def test_run_summary_reports_hash_next_to_schema_fragments(tmp_path):
    wiki = _wiki(tmp_path, EXAMPLE)
    token = policy_hash_token(wiki)
    assert token and len(token) == 8
    line = _render_run_summary(
        [("entity", 1.0, {})],
        schema_fragments={"observation-filter.md": ("a" * 64, True)},
        relevance_policy_hash=token,
    )
    assert f"schema_fragments=observation-filter:default relevance_policy={token}" in line
    # comment/whitespace edits do not change the hash; a rule change does
    (wiki / "_schema" / POLICY_FILENAME).write_text("note\n" + EXAMPLE + "\nmore prose\n")
    assert policy_hash_token(wiki) == token
    (wiki / "_schema" / POLICY_FILENAME).write_text(_table("| never | | personal | |"))
    assert policy_hash_token(wiki) != token


def test_invalid_file_hash_token_does_not_raise(tmp_path):
    assert policy_hash_token(_wiki(tmp_path, _table("| never | | nope | |"))) == "invalid"


def test_read_back_text_lists_rules_in_order(tmp_path):
    out = render_policy(load_relevance_policy(_wiki(tmp_path, EXAMPLE)))
    assert out.index("1. never tag=personal") < out.index("2. always tag=personal/travel")
    assert "last match wins" in out
    assert "No relevance policy" in render_policy(load_relevance_policy(tmp_path / "x"))


def test_cli_read_back(tmp_path, capsys):
    _wiki(tmp_path, EXAMPLE)
    assert main(["relevance-policy", "--path", str(tmp_path)]) == 0
    assert "2. always tag=personal/travel" in capsys.readouterr().out
    assert main(["relevance-policy", "--path", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["present"] is True and data["semantics"] == "last-match-wins"
    assert [(r["rule"], r["verdict"], r["tag"]) for r in data["rules"]] == [
        (1, "never", "personal"),
        (2, "always", "personal/travel"),
    ]


def test_cli_missing_and_invalid(tmp_path, capsys):
    assert main(["relevance-policy", "--path", str(tmp_path)]) == 0
    assert "No relevance policy" in capsys.readouterr().out
    _wiki(tmp_path, _table("| never | | nope | |"))
    assert main(["relevance-policy", "--path", str(tmp_path)]) == 1
    assert "rule 1" in capsys.readouterr().err


def test_mcp_tool_read_back(tmp_path):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    wiki = _wiki(tmp_path, EXAMPLE)
    raw = tmp_path / "raw"
    raw.mkdir()
    server = create_server(raw_root=raw, wiki_root=wiki)
    import asyncio

    tool = asyncio.run(server.get_tool("relevance_policy"))
    result = tool.fn()
    assert [r["verdict"] for r in result["rules"]] == ["never", "always"]
    assert result["hash"] == policy_hash_token(wiki)
    assert tool.annotations is not None
    (wiki / "_schema" / POLICY_FILENAME).write_text(_table("| never | | nope | |"))
    assert "rule 1" in tool.fn()["error"]
