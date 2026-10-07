# SPDX-License-Identifier: Apache-2.0
"""``athenaeum subject-population --include-raw-members`` -- issue athenaeum#1946.

CLI-level coverage over the real ``main()`` dispatch, mirroring
``tests/test_cmd_subject_population.py``'s own stub-injection contract: no
real chromadb/claude-cli, every LLM-adjacent seam patched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import parse_frontmatter


def _raw_file(root: Path, scope: str, filename: str, *, name: str, body: str = "claim") -> Path:
    scope_dir = root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\n{body}\n", encoding="utf-8")
    return path


def _clusters_file(path: Path, member_refs: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"cluster_id": "c1", "member_paths": member_refs}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "develop"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def claude_cli_classify(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATHENAEUM_CLASSIFY_LLM_PROVIDER", "claude-cli")


def _stub_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.provider as provider_mod

    monkeypatch.setattr(provider_mod, "build_llm_client", lambda config, **kwargs: object())


def _stub_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.search as search_mod

    monkeypatch.setattr(search_mod, "embed_texts", lambda texts: None)


class TestIncludeRawMembersDryRun:
    def test_dry_run_scans_raw_members_leaves_everything_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_file(knowledge, "scope-a", "feedback_one.md", name="widget")
        before = raw.read_bytes()
        clusters_path = knowledge / "raw" / "_librarian-clusters-fixture.jsonl"
        _clusters_file(clusters_path, [f"scope-a/{raw.name}"])

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch)

        report_path = tmp_path / "report.jsonl"
        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--report",
                str(report_path),
                "--include-raw-members",
                str(clusters_path),
            ]
        )
        assert rc == 0
        assert raw.read_bytes() == before
        assert not (wiki / "_subject_registry.json").exists()

        rows = _read_rows(report_path)
        raw_rows = [r for r in rows if r["type"].startswith("raw:")]
        assert len(raw_rows) == 1
        assert raw_rows[0]["prior_subject_state"] == "absent"


class TestIncludeRawMembersApply:
    def test_apply_from_report_stamps_raw_frontmatter_and_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_file(knowledge, "scope-a", "feedback_one.md", name="widget")
        clusters_path = knowledge / "raw" / "_librarian-clusters-fixture.jsonl"
        _clusters_file(clusters_path, [f"scope-a/{raw.name}"])
        _init_git_repo(knowledge)
        _commit_all(knowledge)

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch)

        report_path = tmp_path / "report.jsonl"
        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--report",
                str(report_path),
                "--include-raw-members",
                str(clusters_path),
            ]
        )
        assert rc == 0

        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
                "--json",
            ]
        )
        assert rc == 0

        meta, _body = parse_frontmatter(raw.read_text(encoding="utf-8"))
        assert meta["subject"]
        assert meta["subject"] != "undeterminable"
        assert (wiki / "_subject_registry.json").is_file()

    def test_reapplying_same_report_is_idempotent_no_second_mint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_file(knowledge, "scope-a", "feedback_one.md", name="widget")
        clusters_path = knowledge / "raw" / "_librarian-clusters-fixture.jsonl"
        _clusters_file(clusters_path, [f"scope-a/{raw.name}"])
        _init_git_repo(knowledge)
        _commit_all(knowledge)

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch)

        report_path = tmp_path / "report.jsonl"
        main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--report",
                str(report_path),
                "--include-raw-members",
                str(clusters_path),
            ]
        )
        main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        meta_1, _ = parse_frontmatter(raw.read_text(encoding="utf-8"))
        first_id = meta_1["subject"]

        # Re-apply the SAME report a second time -- must not raise, must not
        # change the id, and the second apply's own git-dirty guard would
        # refuse on the wiki side if the raw stamp had dirtied anything
        # inside knowledge_root's tracked tree it shouldn't have.
        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        assert rc == 0
        meta_2, _ = parse_frontmatter(raw.read_text(encoding="utf-8"))
        assert meta_2["subject"] == first_id
