# SPDX-License-Identifier: Apache-2.0
"""An ``always`` relevance rule protects pages from every removal path (athenaeum#3152)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum import cli
from athenaeum.audit import build_audit_report
from athenaeum.decay_sweep import apply_sweep, build_sweep_report
from athenaeum.relevance_policy import (
    RelevanceRule,
    load_relevance_policy,
    protecting_rule,
    render_policy,
    report_always_matches,
)
from athenaeum.retire_pages import (
    apply_retirement,
    find_policy_protected,
    resolve_uids,
)
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage

HEADER = "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
ALWAYS = HEADER + "| always | person | x/y | |\n"
ALWAYS_THEN_NEVER = ALWAYS + "| never | person | x/y | zzzz9999 |\n"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def _page(uid: str, name: str, *, tags: str | None, extra: str = "") -> str:
    fm = [f"uid: {uid}", f"name: {name}", "type: person"]
    if tags:
        fm.append(f"tags: [{tags}]")
    return "---\n" + "\n".join(fm) + f"\n{extra}---\n\nbody of {name}\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
    (wiki / "_schema" / "tags.md").write_text("| Tag |\n|---|\n| x/y |\n| other |\n")
    # Protected (person + x/y), expired daily so decay-sweep would kill it.
    (wiki / "aaaa1111-ann.md").write_text(
        _page("aaaa1111", "Ann", tags="x/y", extra="bucket: daily\nvalid_until: '2020-01-01'\n")
    )
    # Unprotected: wrong tag, also expired daily.
    (wiki / "bbbb2222-bob.md").write_text(
        _page("bbbb2222", "Bob", tags="other", extra="bucket: daily\nvalid_until: '2020-01-01'\n")
    )
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def _policy(root: Path, text: str | None) -> None:
    path = root / "wiki" / "_schema" / "relevance-policy.md"
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text)


def _retire(root: Path, *uids: str, apply: bool = True, force: bool = False) -> int:
    argv = ["retire-pages", "--path", str(root), "--uids", *uids, "--reason", "t"]
    if apply:
        argv.append("--apply")
    if force:
        argv.append("--force-retire")
    return cli.main(argv)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _protect_all(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    rule = RelevanceRule(1, 1, "always", type="person")

    def fake(policy: Any, meta: Any) -> RelevanceRule:
        seen.append(meta)
        return rule

    monkeypatch.setattr("athenaeum.relevance_policy.protecting_rule", fake)
    return seen


class TestSharedHelper:
    def test_always_protects_with_rule(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        policy = load_relevance_policy(repo / "wiki")
        rule = protecting_rule(policy, {"uid": "u", "type": "person", "tags": ["x/y/z"]})
        assert rule is not None and rule.number == 1
        assert protecting_rule(policy, {"type": "person", "tags": ["other"]}) is None

    def test_later_never_unprotects(self, repo: Path) -> None:
        _policy(repo, ALWAYS_THEN_NEVER)
        policy = load_relevance_policy(repo / "wiki")
        meta = {"uid": "zzzz9999", "type": "person", "tags": ["x/y"]}
        assert protecting_rule(policy, meta) is None

    def test_no_policy_protects_nothing(self) -> None:
        assert protecting_rule(None, {"type": "person"}) is None
        empty = load_relevance_policy(Path("/nonexistent"))
        assert protecting_rule(empty, {"type": "person"}) is None


class TestRetirePages:
    def test_protected_uid_refuses_whole_run_and_writes_nothing(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _policy(repo, ALWAYS)
        head = _head(repo)
        assert _retire(repo, "aaaa1111", "bbbb2222") == 1
        err = capsys.readouterr().err
        assert "aaaa1111" in err and "rule 1" in err and "always type=person tag=x/y" in err
        assert "bbbb2222" not in err.split("refusing")[0]
        assert _head(repo) == head
        assert (repo / "wiki" / "aaaa1111-ann.md").exists()
        assert (repo / "wiki" / "bbbb2222-bob.md").exists()

    def test_dry_run_also_reports_it(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _policy(repo, ALWAYS)
        assert _retire(repo, "aaaa1111", apply=False) == 1
        assert "aaaa1111: protected by rule 1" in capsys.readouterr().err

    def test_force_retires_and_records_override(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "policy")
        assert _retire(repo, "aaaa1111", force=True) == 0
        assert not (repo / "wiki" / "aaaa1111-ann.md").exists()
        message = _git(repo, "log", "-1", "--format=%B").stdout
        assert "Forced past relevance-policy protection" in message
        assert "aaaa1111: protected by rule 1" in message
        ledger = (repo / "wiki" / "_relevance_policy_audit.jsonl").read_text().splitlines()
        rec = json.loads(ledger[-1])
        assert rec["kind"] == "removal-override" and rec["uid"] == "aaaa1111"
        assert rec["command"] == "retire-pages" and rec["rule_number"] == 1

    def test_force_without_protection_adds_no_override_record(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        assert _retire(repo, "bbbb2222", force=True) == 0
        assert "Forced past" not in _git(repo, "log", "-1", "--format=%B").stdout
        assert not (repo / "wiki" / "_relevance_policy_audit.jsonl").exists()

    def test_no_policy_file_behaves_as_before(self, repo: Path) -> None:
        _policy(repo, None)
        assert _retire(repo, "aaaa1111", "bbbb2222") == 0
        assert not (repo / "wiki" / "aaaa1111-ann.md").exists()
        assert "Forced past" not in _git(repo, "log", "-1", "--format=%B").stdout

    def test_invalid_policy_fails_closed(self, repo: Path) -> None:
        _policy(repo, HEADER + "| bogus | person | | |\n")
        head = _head(repo)
        assert _retire(repo, "bbbb2222") == 1
        assert _head(repo) == head

    def test_command_consults_the_shared_helper(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _policy(repo, ALWAYS)  # a policy must exist for the helper to be asked
        seen = _protect_all(monkeypatch)
        assert _retire(repo, "bbbb2222") == 1  # bbbb2222 is unprotected by the real rule
        assert seen

    def test_find_policy_protected_direct(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        cands, _ = resolve_uids(repo / "wiki", ["aaaa1111", "bbbb2222"])
        found = find_policy_protected(repo, cands)
        assert [c.uid for c, _ in found] == ["aaaa1111"]
        _policy(repo, None)
        assert find_policy_protected(repo, cands) == []

    def test_apply_retirement_unforced_has_no_override_note(self, repo: Path) -> None:
        cands, _ = resolve_uids(repo / "wiki", ["bbbb2222"])
        apply_retirement(repo, cands, reason="r")
        assert "Forced past" not in _git(repo, "log", "-1", "--format=%B").stdout


class TestDecaySweep:
    def test_protected_page_is_skipped_and_counted(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        report = build_sweep_report(repo / "wiki", as_of=None, knowledge_root=repo)
        assert [p.name for c in report.kill for p in [c.path]] == ["bbbb2222-bob.md"]
        assert [p.name for p, _ in report.protected] == ["aaaa1111-ann.md"]
        apply_sweep(repo, report)
        assert (repo / "wiki" / "aaaa1111-ann.md").exists()
        assert not (repo / "wiki" / "bbbb2222-bob.md").exists()

    def test_cli_reports_count(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _policy(repo, ALWAYS)
        cli.main(["decay-sweep", "--path", str(repo)])
        assert "protected by policy: 1" in capsys.readouterr().out

    def test_no_policy_file_behaves_as_before(self, repo: Path) -> None:
        _policy(repo, None)
        report = build_sweep_report(repo / "wiki", knowledge_root=repo)
        assert len(report.kill) == 2 and report.protected == []

    def test_sweep_consults_the_shared_helper(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _policy(repo, ALWAYS)
        seen = _protect_all(monkeypatch)
        report = build_sweep_report(repo / "wiki", knowledge_root=repo)
        assert seen and report.kill == [] and len(report.protected) == 2

    def test_invalid_policy_fails_closed(self, repo: Path) -> None:
        _policy(repo, HEADER + "| bogus | person | | |\n")
        report = build_sweep_report(repo / "wiki", knowledge_root=repo)
        assert report.kill == [] and report.errors


def _flag_everything(params: dict[str, Any]) -> str:
    return json.dumps({"retirement_candidate": True, "retirement_reason": "stub"})


def _audit(repo: Path):
    client = FakeLLMClient(
        responder=lambda **p: make_llm_response(_flag_everything(p), usage=make_llm_usage(1, 1))
    )
    return build_audit_report(repo / "wiki", client=client, model="m")


class TestAuditCandidates:
    def test_protected_never_a_candidate_and_counted(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        report = _audit(repo)
        flagged = {v.path.name for v in report.retirement_candidates}
        assert flagged == {"bbbb2222-bob.md"}
        assert report.protected_by_policy == 1
        assert report.to_dict()["protected_by_policy"] == 1
        assert "protected by policy: 1" in report.render_text()

    def test_no_policy_file_behaves_as_before(self, repo: Path) -> None:
        _policy(repo, None)
        report = _audit(repo)
        assert len(report.retirement_candidates) == 2 and report.protected_by_policy == 0

    def test_audit_consults_the_shared_helper(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _policy(repo, ALWAYS)
        seen = _protect_all(monkeypatch)
        report = _audit(repo)
        assert seen and report.retirement_candidates == []
        assert report.protected_by_policy == 2


class TestReadBack:
    def test_always_counterpart_report(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        matches = report_always_matches(load_relevance_policy(repo / "wiki"), repo / "wiki")
        assert [(m.uid, m.rule.number) for m in matches] == [("aaaa1111", 1)]

    def test_cli_report_json_counts_per_rule(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _policy(repo, ALWAYS)
        assert cli.main(["relevance-policy", "report", "--path", str(repo), "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["protected_counts"] == {"1": 1}
        assert out["protected"][0]["uid"] == "aaaa1111"

    def test_cli_report_text(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _policy(repo, ALWAYS)
        cli.main(["relevance-policy", "report", "--path", str(repo)])
        assert "protected from retirement" in capsys.readouterr().out

    def test_render_and_to_dict_state_removal_protection(self, repo: Path) -> None:
        _policy(repo, ALWAYS)
        policy = load_relevance_policy(repo / "wiki")
        assert "protects every page it resolves to from retirement" in render_policy(policy)
        assert "protects every page" in policy.to_dict()["removal_protection"]

    def test_mcp_tool_description_states_it(self) -> None:
        import inspect

        from athenaeum import mcp_server

        src = inspect.getsource(mcp_server)
        assert "protects every page it matches from retirement" in src
