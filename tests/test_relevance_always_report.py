# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 8: ``always`` prompt hint and the existing-pages report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import RawFile
from athenaeum.relevance_policy import load_relevance_policy, report_never_matches
from athenaeum.tiers import (
    RELEVANCE_ALWAYS_PROMPT,
    render_relevance_always_section,
    tier2_request_params,
)
from tests.test_relevance_gate import POLICY, _seed

NEVER_ONLY = "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | | personal | |\n"


def _prompt(root: Path, policy_text: str | None) -> str:
    if policy_text is not None:
        (root / "wiki" / "_schema" / "relevance-policy.md").write_text(policy_text)
    policy = load_relevance_policy(root / "wiki")
    raw_path = root / "raw" / "sessions" / "20260101T000000Z-aaaaaaaa.md"
    raw_path.write_text("Some note.\n")
    raw = RawFile(raw_path, "sessions", "20260101T000000Z", "aaaaaaaa")
    params = tier2_request_params(
        raw, [], ["person"], ["personal"], ["internal"], root / "wiki",
        relevance_policy=policy,
    )
    return str(params["messages"][0]["content"])


def test_no_always_rule_renders_nothing(tmp_path):
    assert render_relevance_always_section(None) == ""
    root = _seed(tmp_path, NEVER_ONLY)
    assert render_relevance_always_section(load_relevance_policy(root / "wiki")) == ""


def test_prompt_without_always_rule_is_byte_identical_to_no_policy(tmp_path):
    # A type-keyed `never` rule renders neither the always block nor the
    # category block, so the prompt must equal the no-policy prompt exactly.
    baseline = _prompt(_seed(tmp_path / "a", None), None)
    type_never = "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | company | | |\n"
    assert _prompt(_seed(tmp_path / "b", type_never), None) == baseline
    assert "Relevance hints" not in baseline


def test_always_rule_is_rendered_beside_the_observation_filter(tmp_path):
    root = _seed(tmp_path)
    (root / "wiki" / "_schema" / "observation-filter.md").write_text("Capture decisions.\n")
    prompt = _prompt(root, POLICY)
    assert "## Observation filter (what to capture)" in prompt
    assert "## Relevance hints (what this deployment wants kept)" in prompt
    assert "- tag `personal/travel`" in prompt
    assert "- tag `personal`\n" not in prompt.split("Relevance hints")[1].split("## ")[0]
    assert prompt.index("Observation filter") < prompt.index("Relevance hints")
    assert "does not override the observation filter" in RELEVANCE_ALWAYS_PROMPT


def _wiki_pages(root: Path) -> None:
    wiki = root / "wiki"
    (wiki / "ada.md").write_text(
        "---\ntype: person\nuid: aa11bb22\nname: Ada Example\ntags: [personal]\n---\nBody.\n"
    )
    (wiki / "ben.md").write_text(
        "---\ntype: person\nuid: cc33dd44\nname: Ben Example\ntags: [personal/travel]\n---\nBody.\n"
    )
    (wiki / "cy.md").write_text(
        "---\ntype: person\nuid: ee55ff66\nname: Cy Example\ntags: [active]\n---\nBody.\n"
    )
    (wiki / "_pending_questions.md").write_text(
        "---\ntype: person\ntags: [personal]\n---\nqueue\n"
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def test_report_lists_only_pages_a_never_rule_still_decides(tmp_path):
    root = _seed(tmp_path)
    _wiki_pages(root)
    matches = report_never_matches(load_relevance_policy(root / "wiki"), root / "wiki")
    assert [m.path for m in matches] == ["ada.md"]
    assert matches[0].rule.number == 1


def test_report_cli_text_and_json_change_nothing(tmp_path, capsys):
    root = _seed(tmp_path)
    _wiki_pages(root)
    before = _snapshot(root)
    assert main(["relevance-policy", "report", "--path", str(root)]) == 0
    out = capsys.readouterr().out
    assert "ada.md" in out and "ben.md" not in out and "cy.md" not in out
    assert main(["relevance-policy", "report", "--path", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["path"] for m in data["matches"]] == ["ada.md"]
    assert data["matches"][0]["uid"] == "aa11bb22"
    assert _snapshot(root) == before


def test_report_without_policy_or_matches(tmp_path, capsys):
    root = _seed(tmp_path, None)
    _wiki_pages(root)
    assert main(["relevance-policy", "report", "--path", str(root)]) == 0
    assert "No existing pages" in capsys.readouterr().out


def test_report_exits_1_on_invalid_policy(tmp_path, capsys):
    bad = "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| maybe | | x | |\n"
    root = _seed(tmp_path, bad)
    assert main(["relevance-policy", "report", "--path", str(root)]) == 1
    assert "error" in capsys.readouterr().err


def test_bare_relevance_policy_still_lists_rules(tmp_path, capsys):
    root = _seed(tmp_path)
    assert main(["relevance-policy", "--path", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["present"] is True


@pytest.mark.parametrize("args", (["--json"], []))
def test_json_flag_before_subcommand_is_honoured(tmp_path, capsys, args):
    root = _seed(tmp_path)
    assert main(["relevance-policy", *args, "report", "--path", str(root)]) == 0
    out = capsys.readouterr().out
    assert (out.startswith("{")) == bool(args)
