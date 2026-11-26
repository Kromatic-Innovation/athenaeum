# SPDX-License-Identifier: Apache-2.0
"""Retirement under a declared ``storage.history`` (athenaeum#3164).

Under ``none`` the three retirement paths remove the file with no git call and
log once that no restore point exists; under ``git`` with no repository they
refuse with the messages pinned here; under ``git`` with a repository nothing
changes (the existing retirement tests cover the byte-identical commits).
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from athenaeum import store as store_mod
from athenaeum.corrections import retire_batch
from athenaeum.rules import retire_compiled_raw_file

_NO_RESTORE = "removing with no restore point"


@pytest.fixture(autouse=True)
def _reset_once_log() -> None:
    store_mod._NO_RESTORE_POINT_NOTED.clear()


@pytest.fixture
def git_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `git` shim that records every call and fails, so any git use is visible."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "git-calls.log"
    exe = bindir / "git"
    exe.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _kb(tmp_path: Path, history: str) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "athenaeum.yaml").write_text(
        f"storage:\n  history: {history}\nrecall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return root


def _file(root: Path, name: str) -> Path:
    path = root / "raw" / "graph-writer" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    return path


class TestHistoryNone:
    def test_batch_retired_without_git(
        self, tmp_path: Path, git_calls: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = _kb(tmp_path, "none")
        first, second = _file(root, "a.jsonl"), _file(root, "b.jsonl")
        with caplog.at_level(logging.INFO):
            assert retire_batch(root, first) is True
            assert retire_batch(root, second) is True
        assert not first.exists() and not second.exists()
        assert not git_calls.exists()
        assert sum(_NO_RESTORE in r.getMessage() for r in caplog.records) == 1

    def test_batch_retired_even_when_a_repository_exists(self, tmp_path: Path) -> None:
        """Branches on the declared value, not on whether `.git` exists."""
        root = _kb(tmp_path, "none")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        batch = _file(root, "a.jsonl")
        assert retire_batch(root, batch) is True
        assert not batch.exists()
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True
        )
        assert head.returncode != 0  # nothing was committed

    def test_rules_retire_without_git(
        self, tmp_path: Path, git_calls: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = _kb(tmp_path, "none")
        first, second = _file(root, "a.jsonl"), _file(root, "b.jsonl")
        with caplog.at_level(logging.INFO):
            assert retire_compiled_raw_file(root, first, rule_tag="t@1") is True
            assert retire_compiled_raw_file(root, second, rule_tag="t@1") is True
        assert not first.exists() and not second.exists()
        assert not git_calls.exists()
        assert sum(_NO_RESTORE in r.getMessage() for r in caplog.records) == 1

    def test_raw_retirement_without_git(
        self, tmp_path: Path, git_calls: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import run_retire_pass
        from tests.test_librarian_auto_memory import _raw_file, _write_am, _write_clusters

        root = _kb(tmp_path, "none")
        scope = root / "raw" / "auto-memory" / "-Users-tristankromer-Code-home"
        _write_am(
            scope,
            "user_tristan_berlin_address.md",
            name="Tristan Berlin address",
            session="sess-berlin",
            turn=3,
            body="Tristan lives in Berlin, Germany.",
        )
        _write_clusters(
            root,
            [
                {
                    "cluster_id": "home-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code-home/user_tristan_berlin_address.md"
                    ],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        (root / "wiki").mkdir(parents=True, exist_ok=True)
        entries = merge_clusters_to_wiki(root)
        with caplog.at_level(logging.INFO):
            report = run_retire_pass(entries, root)
        assert not _raw_file(root).exists()
        assert len(report.moved) == 1
        assert not git_calls.exists()
        assert sum(_NO_RESTORE in r.getMessage() for r in caplog.records) == 1


class TestHistoryGitWithoutRepository:
    """Today's refusals, pinned so a predicate change cannot loosen them."""

    def test_batch_refuses(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        root = _kb(tmp_path, "git")
        batch = _file(root, "a.jsonl")
        with caplog.at_level(logging.WARNING):
            assert retire_batch(root, batch) is False
        assert batch.exists()
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            m
            == f"corrections: store at {root} is not versioned — refusing to retire "
            f"batch {batch} (recovery is git-only)"
            for m in msgs
        ), msgs

    def test_rules_refuses(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        root = _kb(tmp_path, "git")
        raw = _file(root, "a.jsonl")
        with caplog.at_level(logging.WARNING):
            assert retire_compiled_raw_file(root, raw, rule_tag="t@1") is False
        assert raw.exists()
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            m
            == f"shape-rules: store at {root} is not versioned — refusing to retire "
            f"{raw} (recovery is git-only)"
            for m in msgs
        ), msgs

    def test_raw_retirement_refuses(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import SKIP, run_retire_pass
        from tests.test_librarian_auto_memory import _raw_file, _write_am, _write_clusters

        root = _kb(tmp_path, "git")
        scope = root / "raw" / "auto-memory" / "-Users-tristankromer-Code-home"
        _write_am(
            scope,
            "user_tristan_berlin_address.md",
            name="Tristan Berlin address",
            session="sess-berlin",
            turn=3,
            body="Tristan lives in Berlin, Germany.",
        )
        _write_clusters(
            root,
            [
                {
                    "cluster_id": "home-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code-home/user_tristan_berlin_address.md"
                    ],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        (root / "wiki").mkdir(parents=True, exist_ok=True)
        entries = merge_clusters_to_wiki(root)
        with caplog.at_level(logging.WARNING):
            report = run_retire_pass(entries, root)
        assert _raw_file(root).exists()
        assert report.moved == []
        assert any(d.disposition == SKIP for d in report.dispositions)
        assert any(
            f"retire: store at {root} is not versioned — refusing to retire raw "
            "(recovery is git-only); leaving 1 candidate file(s) in place"
            == r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
        assert any(
            d.reason == "no git repo — not retired (recovery is git-only)"
            for d in report.dispositions
        )


class TestHistoryReadingCommands:
    @pytest.mark.parametrize(
        "argv",
        [
            ["storage", "move-type", "--undo", "no-such-record"],
            ["recover-parked", "restore", "--manifest", "m.jsonl"],
            ["pii-restore"],
        ],
    )
    def test_reports_no_history_and_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        from athenaeum.cli import main

        root = _kb(tmp_path, "none")
        (root / "wiki").mkdir()
        flag = "--knowledge-root" if argv[0] == "pii-restore" else "--path"
        assert main([*argv, flag, str(root)]) == 0
        assert "no history" in capsys.readouterr().out

    def test_undo_under_git_is_unchanged(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        root = _kb(tmp_path, "git")
        assert main(["storage", "move-type", "--undo", "no-such-record", "--path", str(root)]) != 0
