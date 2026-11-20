# SPDX-License-Identifier: Apache-2.0
"""The tracked machine-local file check (athenaeum#2994). Repositories live under tmp_path."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from athenaeum import cli
from athenaeum._cmd_machine_local_check import EXIT_TRACKED_FOUND, cmd_machine_local_check
from athenaeum.store import ARTIFACT_REGISTRY
from athenaeum.sync_integrate import SyncSettings, sync_before_compile
from athenaeum.sync_machine_local import find_tracked_machine_local, machine_local_file_names


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One tracked machine-local file, one untracked, one tracked store-durable file."""
    r = tmp_path / "pages"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "cache").mkdir()
    (r / "cache" / "zero_yield_state.json").write_text("{}")  # tracked, machine-local
    (r / "registry.json").write_text("{}")  # tracked, operational store-durable
    (r / "page.md").write_text("x")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    (r / "disabled").write_text("")  # machine-local, but untracked
    return r


def test_registry_names_come_from_machine_local_entries() -> None:
    assert machine_local_file_names() == {"zero_yield_state.json", "disabled"}
    durable = {
        n for d in ARTIFACT_REGISTRY if d.operational_scope == "store-durable" for n in d.file_names
    }
    assert not durable & machine_local_file_names()


def test_lists_tracked_machine_local_only(repo: Path) -> None:
    assert find_tracked_machine_local(repo) == ("cache/zero_yield_state.json",)


def test_untracked_and_ignored_machine_local_not_reported(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "cache/zero_yield_state.json")
    (repo / ".gitignore").write_text("zero_yield_state.json\n")
    assert find_tracked_machine_local(repo) == ()  # also: `disabled` is untracked


def test_tracked_store_durable_not_reported(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "cache/zero_yield_state.json")
    assert (repo / "registry.json").exists()
    assert find_tracked_machine_local(repo) == ()


def test_check_changes_nothing(repo: Path) -> None:
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    find_tracked_machine_local(repo)
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert before == after
    assert (repo / "cache" / "zero_yield_state.json").exists()
    assert (
        "cache/zero_yield_state.json"
        in subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout
    )


def test_sync_before_compile_reports_finding(repo: Path) -> None:
    report = sync_before_compile(repo, "main", SyncSettings())
    rows = {s.step: s for s in report.steps}
    assert rows["machine-local-files"].outcome == "failed"
    assert rows["machine-local-files"].reason == "tracked=1"
    # the sync still ran after the finding
    assert "snapshot" in rows and "fetch" in rows
    # and the file stays tracked (the snapshot commit-all also adds the untracked one)
    assert "cache/zero_yield_state.json" in find_tracked_machine_local(repo)


def test_sync_before_compile_clean_repo_ok(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "cache/zero_yield_state.json")
    report = sync_before_compile(repo, "main", SyncSettings())
    row = next(s for s in report.steps if s.step == "machine-local-files")
    assert row.outcome == "ok"


def test_not_a_git_repo_is_skipped_not_raised(tmp_path: Path) -> None:
    report = sync_before_compile(tmp_path, "main", SyncSettings())
    row = next(s for s in report.steps if s.step == "machine-local-files")
    assert row.outcome == "skipped"


def _args(repo: Path, as_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(repo=repo, path=None, json=as_json)


def test_cli_exits_nonzero_on_finding(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_machine_local_check(_args(repo, True)) == EXIT_TRACKED_FOUND
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["repos"][0]["tracked"] == ["cache/zero_yield_state.json"]


def test_cli_exits_zero_when_clean(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "cache/zero_yield_state.json")
    assert cmd_machine_local_check(_args(repo)) == 0


def test_cli_registered(repo: Path) -> None:
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    assert parser is not None
    ns = parser.parse_args(["machine-local-check", "--repo", str(repo)])
    assert ns.func is cmd_machine_local_check


def test_non_utf8_tracked_filename_does_not_abort_the_check(repo: Path) -> None:
    bad = repo.joinpath(b"caf\xe9.txt".decode("utf-8", errors="surrogateescape"))
    bad.write_text("x")
    _git(repo, "add", "--", bad.name)
    assert find_tracked_machine_local(repo) == ("cache/zero_yield_state.json",)
    report = sync_before_compile(repo, "main", SyncSettings())
    row = next(s for s in report.steps if s.step == "machine-local-files")
    assert row.outcome == "failed" and row.reason == "tracked=1"


def test_cli_json_ok_is_false_when_a_check_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_machine_local_check(_args(tmp_path, True)) == 1  # not a git repository
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["repos"][0]["error"]
