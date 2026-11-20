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


# ---------------------------------------------------------------------------
# athenaeum#2796: the ``Store`` column and the ``route`` verdict
# ---------------------------------------------------------------------------

from athenaeum.relevance_policy import (  # noqa: E402
    parse_relevance_policy,
    policy_categories,
)

STORE_A = "st_h4r5n2kd6wq3"
STORE_B = "st_q7m2xk4vwz5d"
ROUTE_VOCAB_TYPES = {"person", "policy"}
ROUTE_VOCAB_TAGS = {"personal", "personal/travel", "hr", "hr/health"}

# The table of docs/design/federation-intake-routing.md section 5.1.
SECTION_5_1 = (
    "| Verdict | Type   | Tag             | Subject | Store           |\n"
    "|---------|--------|-----------------|---------|-----------------|\n"
    "| never   |        | personal        |         |                 |\n"
    "| always  |        | personal/travel |         |                 |\n"
    f"| route   |        | hr              |         | {STORE_A} |\n"
    f"| route   | policy |                 |         | {STORE_B} |\n"
    "| route   |        | hr/health       |         | personal        |\n"
)

FOUR_COL = (
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
    "| never | | personal | |\n| always | | personal/travel | |\n"
    "| never | person | | uid-1 |\n"
)
#: ``canonical_hash()`` of FOUR_COL, computed by the code before athenaeum#2796.
FOUR_COL_HASH = "c45e9fa2bd94ccdf6e6b27e4ec59fb6c34d0e6ab0241b144ceae1dcc1808f34e"
FOUR_COL_RENDER = (
    "Relevance policy hash=c45e9fa2; 3 rule(s), evaluated top to bottom, last match wins:\n"
    "  1. never tag=personal\n  2. always tag=personal/travel\n"
    "  3. never type=person subject=uid-1\n"
)


def _parse(text: str, stores=frozenset({STORE_A, STORE_B})):
    return parse_relevance_policy(text, ROUTE_VOCAB_TYPES, ROUTE_VOCAB_TAGS, stores)


def _route_table(*rows: str) -> str:
    return (
        "| Verdict | Type | Tag | Subject | Store |\n|---|---|---|---|---|\n"
        + "".join(r + "\n" for r in rows)
    )


def test_route_row_does_not_shadow_a_never_row():
    policy = _parse(
        _route_table("| never | | personal | |", f"| route | | personal | | {STORE_A} |")
    )
    assert policy.resolve(tags=["personal"]) == "never"
    assert policy.match(tags=["personal"]).verdict == "never"
    # The destination question still sees the route row.
    assert policy.match_route(tags=["personal"]).store == STORE_A


def test_four_column_file_parses_byte_identically():
    policy = parse_relevance_policy(FOUR_COL, ROUTE_VOCAB_TYPES, ROUTE_VOCAB_TAGS)
    assert [(r.number, r.line, r.verdict, r.type, r.tag, r.subject) for r in policy.rules] == [
        (1, 3, "never", None, "personal", None),
        (2, 4, "always", None, "personal/travel", None),
        (3, 5, "never", "person", None, "uid-1"),
    ]
    assert all(r.store is None for r in policy.rules)
    assert policy.canonical_hash() == FOUR_COL_HASH
    assert render_policy(policy) == FOUR_COL_RENDER


def test_five_column_file_parses_section_5_1_table():
    policy = _parse(SECTION_5_1)
    assert [r.verdict for r in policy.rules] == ["never", "always", "route", "route", "route"]
    assert [r.store for r in policy.rules] == [None, None, STORE_A, STORE_B, "personal"]
    assert policy.rules[3].type == "policy"


def test_match_route_is_last_match_wins_among_route_rows():
    policy = _parse(SECTION_5_1)
    assert policy.match_route(tags=["hr/health"]).store == "personal"
    assert policy.match_route(tags=["hr"]).store == STORE_A
    assert policy.match_route(type_="policy").store == STORE_B
    assert policy.match_route(tags=["food"]) is None
    capture_only = parse_relevance_policy(FOUR_COL, ROUTE_VOCAB_TYPES, ROUTE_VOCAB_TAGS)
    assert capture_only.match_route(tags=["personal"]) is None


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("| route | | hr | | |", "rule 1 (line 3): a route rule needs a Store"),
        (f"| never | | personal | | {STORE_A} |", "rule 1 (line 3): a never rule must leave Store"),
        (f"| always | | personal | | {STORE_A} |", "rule 1 (line 3): a always rule must leave"),
        (
            "| route | | hr | | st_aaaaaaaaaaaa |",
            "rule 1 (line 3): Store 'st_aaaaaaaaaaaa' is not a store this install maps",
        ),
        (f"| route | | | | {STORE_A} |", "rule 1 (line 3): names no type, tag or subject"),
    ],
)
def test_validation_errors_name_the_rule_and_line(row, fragment):
    with pytest.raises(RelevancePolicyError) as exc:
        _parse(_route_table(row))
    assert fragment in str(exc.value)


def test_personal_is_valid_with_an_empty_valid_store_set():
    policy = _parse(_route_table("| route | | hr | | personal |"), stores=frozenset())
    assert policy.match_route(tags=["hr"]).store == "personal"


@pytest.mark.parametrize("stores", [frozenset({STORE_A}), frozenset(), None])
def test_an_adapter_name_is_never_a_store(stores):
    with pytest.raises(RelevancePolicyError, match=r"rule 1 \(line 3\): Store 'hr' is neither"):
        _parse(_route_table("| route | | hr | | hr |"), stores=stores)


def test_without_a_valid_store_set_only_the_shape_is_checked():
    policy = _parse(_route_table("| route | | hr | | st_aaaaaaaaaaaa |"), stores=None)
    assert policy.rules[0].store == "st_aaaaaaaaaaaa"
    with pytest.raises(RelevancePolicyError, match="neither 'personal' nor shaped"):
        _parse(_route_table("| route | | hr | | st_h4r8n2kd6wq3 |"), stores=None)  # 8 is not base32


def test_policy_categories_skips_route_rows():
    assert policy_categories(_parse(SECTION_5_1)) == ["personal", "personal/travel"]


def test_tier2_request_is_byte_identical_with_route_rows_removed(tmp_path):
    from athenaeum.models import RawFile
    from athenaeum.tiers import tier2_request_params

    raw_path = tmp_path / "20260101T000000Z-aaaaaaaa.md"
    raw_path.write_text("Some note.\n")
    raw = RawFile(raw_path, "sessions", "20260101T000000Z", "aaaaaaaa")
    with_routes = _parse(SECTION_5_1)
    without = _parse(
        "".join(
            ln + "\n" for ln in SECTION_5_1.splitlines() if not ln.startswith("| route")
        )
    )

    def build(policy):
        return tier2_request_params(
            raw, [], ["person"], ["personal", "hr"], ["internal"], relevance_policy=policy
        )

    assert build(with_routes) == build(without)
    assert "Relevance categories" in str(build(with_routes)["messages"][0]["content"])


def test_hash_sees_a_routing_change():
    a = _parse(_route_table(f"| route | | hr | | {STORE_A} |"))
    b = _parse(_route_table(f"| route | | hr | | {STORE_B} |"))
    c = _parse(_route_table("| route | | hr | | personal |"))
    assert len({a.canonical_hash(), b.canonical_hash(), c.canonical_hash()}) == 3


def test_read_back_shows_the_store():
    policy = _parse(SECTION_5_1)
    rules = policy.to_dict()["rules"]
    assert [r["store"] for r in rules] == [None, None, STORE_A, STORE_B, "personal"]
    text = render_policy(policy)
    assert f"route tag=hr store={STORE_A}" in text
    assert "route tag=hr/health store=personal" in text
    assert "never tag=personal\n" in text


def test_match_route_has_one_production_caller():
    # athenaeum#2796 shipped with no caller; athenaeum#2797's ``decide`` is the
    # only one, and no gate reads a route row to place a fact.
    src = Path(__file__).resolve().parent.parent / "src" / "athenaeum"
    users = sorted(
        p.name for p in src.rglob("*.py") if "match_route" in p.read_text(encoding="utf-8")
    )
    assert users == ["intake_decision.py", "relevance_policy.py"]
