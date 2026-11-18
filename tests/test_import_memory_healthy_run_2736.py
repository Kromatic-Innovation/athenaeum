# SPDX-License-Identifier: Apache-2.0
"""A healthy ``athenaeum import-memory`` run must not read as a failed one.

Issue athenaeum#2736: a run that moved four files into compiled pages still
printed a recovery-yield breach, a zero-yield alarm and an ``extra_intake_root
not found`` warning, recorded ``files_processed: 0`` on the spend ledger, and
(with no vector backend installed) a D3 chromadb message. Every test drives
the real code path with synthetic data under ``tmp_path``; no LLM call is made.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
from pathlib import Path

import pytest

from athenaeum.config import resolve_extra_intake_roots
from athenaeum.librarian import (
    ZERO_YIELD_PREFIX,
    _run_finalize_phase,
    run,
)
from athenaeum.models import TokenUsage
from athenaeum.spend import resolve_ledger_path
from tests.test_librarian_auto_memory import mock_anthropic  # noqa: F401
from tests.test_librarian_run_phases import _make_ctx

SCOPE = "-work-sample-project"


@pytest.fixture(autouse=True)
def _isolated_retire_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retire's live-session guard off the real ``~/.claude/projects``."""
    monkeypatch.setattr(
        "athenaeum.retire.default_projects_root",
        lambda: tmp_path / "_no_such_projects_root",
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)


def _staged_root(tmp_path: Path) -> Path:
    """A git-initialised knowledge root holding one staged memory file that
    declares no provenance (what ``import-memory`` stages: a verbatim copy)."""
    root = tmp_path / "knowledge"
    scope = root / "raw" / "auto-memory" / SCOPE
    scope.mkdir(parents=True)
    (scope / "user_bakery_hours.md").write_text(
        "---\nname: Bakery hours\ntype: user\n---\nThe bakery opens at six.\n",
        encoding="utf-8",
    )
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n", encoding="utf-8"
    )
    (root / "wiki").mkdir()
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def _run(root: Path, tmp_path: Path, **kw: object) -> int:
    return run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        projects_root=tmp_path / "_no_projects",
        live_session_guard=False,
        **kw,  # type: ignore[arg-type]
    )


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


class TestRecoveryYield:
    def test_staged_import_run_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mock_anthropic  # noqa: F811
    ) -> None:
        root = _staged_root(tmp_path)
        caplog.set_level(logging.INFO)
        assert _run(root, tmp_path, staged_import=True) == 0
        assert not any("recovery yield" in m for m in _messages(caplog))

    def test_ordinary_run_still_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mock_anthropic  # noqa: F811
    ) -> None:
        # Positive control: the alarm is intact for a non-import run.
        root = _staged_root(tmp_path)
        caplog.set_level(logging.INFO)
        assert _run(root, tmp_path) == 0
        assert any("recovery yield" in m for m in _messages(caplog))

    def test_import_memory_passes_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import _cmd_import_memory as cmd

        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "athenaeum.librarian.run", lambda **kw: seen.update(kw) or 0
        )
        cmd._default_compile_batch(tmp_path, max_files=1, max_api_calls=1)
        assert seen["staged_import"] is True


class TestFilesProcessed:
    def test_retired_auto_memory_files_count_as_processed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_anthropic,  # noqa: F811
    ) -> None:
        import athenaeum.librarian as lib

        root = _staged_root(tmp_path)
        seen: list[int] = []
        real = lib._zero_yield_tripped

        def _spy(ctx, previous):  # type: ignore[no-untyped-def]
            seen.append(ctx.files_processed_count)
            return real(ctx, previous)

        monkeypatch.setattr(lib, "_zero_yield_tripped", _spy)
        assert _run(root, tmp_path, staged_import=True) == 0
        assert seen == [1]

    def test_zero_yield_and_ledger_count_drained_auto_memory(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _make_ctx(
            tmp_path, push_after_run=False, cluster_only=False, strict_budget=False
        )
        ctx.wiki_root.mkdir(parents=True, exist_ok=True)
        ctx.usage = TokenUsage(api_calls=4, input_tokens=100, output_tokens=10)
        ctx.raw_files = []  # the entity queue is empty; intake was auto-memory
        ctx.auto_memory_drained_count = 4
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        _run_finalize_phase(ctx)
        assert ctx.files_processed_count == 4
        assert ctx.zero_yield_tripped is False
        assert not any(ZERO_YIELD_PREFIX in m for m in _messages(caplog))
        rows = [
            json.loads(line)
            for line in resolve_ledger_path(ctx.config).read_text().splitlines()
        ]
        assert rows[-1]["files_processed"] == 4


class TestIntakeRootSurvivesRetire:
    def test_root_still_exists_and_resolves_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mock_anthropic  # noqa: F811
    ) -> None:
        root = _staged_root(tmp_path)
        assert _run(root, tmp_path, staged_import=True) == 0
        assert not (root / "raw" / "auto-memory" / SCOPE / "user_bakery_hours.md").exists()
        assert (root / "raw" / "auto-memory").is_dir()
        caplog.clear()
        caplog.set_level(logging.WARNING)
        assert resolve_extra_intake_roots(root) != []
        assert not any("extra_intake_root not found" in m for m in _messages(caplog))


class TestD3Message:
    def _d3(self, tmp_path: Path) -> object:
        from athenaeum.delta import compute_affected_clusters
        from athenaeum.librarian import discover_auto_memory_files

        root = tmp_path / "k"
        d = root / "raw" / "auto-memory" / "alpha"
        d.mkdir(parents=True)
        p = d / "project_x.md"
        p.write_text("---\nname: project_x\ntype: auto-memory\n---\nhello\n")
        (root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
        )
        files = discover_auto_memory_files(root)
        prior = [
            {"cluster_id": "a", "member_paths": ["alpha/project_x.md"], "centroid_score": 1.0}
        ]
        return compute_affected_clusters(
            {p},
            prior,
            files,
            extra_roots=resolve_extra_intake_roots(root),
            cache_dir=root / ".cache-none",
            threshold=0.55,
            max_affected_clusters=8,
            max_affected_members=200,
        )

    def test_silent_without_vector_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, **k: None if name == "chromadb" else real(name, *a, **k),
        )
        caplog.set_level(logging.WARNING)
        assert self._d3(tmp_path) is None  # still falls back to a full compile
        assert not any("(D3)" in m for m in _messages(caplog))

    def test_still_warns_with_vector_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, **k: object() if name == "chromadb" else real(name, *a, **k),
        )
        caplog.set_level(logging.WARNING)
        assert self._d3(tmp_path) is None
        assert any("(D3)" in m for m in _messages(caplog))
