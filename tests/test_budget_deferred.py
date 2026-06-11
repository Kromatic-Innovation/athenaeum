# SPDX-License-Identifier: Apache-2.0
"""Issue #220 — run-level budget exhaustion must leave a paper trail.

Covers:
- Budget trip mid-run → ``wiki/_deferred_work.md`` manifest written listing
  the raw files NOT processed, and the end-of-run summary line is visibly
  DEGRADED (machine-greppable) while the process still exits 0.
- Clean run (no trip) → no manifest, and a stale manifest from a previous
  budget-tripped run is cleared.
- Cap resolution precedence: env ``ATHENAEUM_MAX_API_CALLS`` > yaml
  ``librarian.max_api_calls`` > ``DEFAULT_MAX_API_CALLS`` (800).

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import (
    DEFAULT_MAX_API_CALLS,
    librarian_max_api_calls,
    run,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_knowledge_root(tmp_path: Path, n_files: int = 3) -> Path:
    """Minimal knowledge root: wiki/, raw/sessions/ with *n_files*, git repo."""
    root = tmp_path / "knowledge"
    root.mkdir()
    wiki = root / "wiki"
    wiki.mkdir()
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    for i in range(n_files):
        (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
            f"Met with Alice Zhang about topic {i} at Acme Corp.\n"
        )
    return root


def _fake_process_one_factory(calls_per_file: int = 2):
    """A process_one stand-in that burns *calls_per_file* API calls per file."""

    def fake_process_one(raw, index, wiki_root, client, *args, **kwargs):
        usage = kwargs.get("usage")
        if usage is not None:
            usage.api_calls += calls_per_file
        return SimpleNamespace(created=[], updated=[], escalated=[], skipped=[raw.ref])

    return fake_process_one


# ---------------------------------------------------------------------------
# Manifest + DEGRADED summary on budget trip
# ---------------------------------------------------------------------------


class TestDeferredManifest:
    def test_budget_trip_writes_manifest_and_degraded_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        # Budget of 1: file 1 burns 2 calls, files 2+3 are deferred.
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1,
        )

        # Exit code stays 0 — the nightly sweep must not treat this as a crash.
        assert rc == 0

        manifest = root / "wiki" / "_deferred_work.md"
        assert manifest.exists()
        text = manifest.read_text(encoding="utf-8")
        assert "deferred_count: 2" in text
        assert "20240411T120000Z-aabbccd1.md" in text
        assert "20240412T120000Z-aabbccd2.md" in text
        # The processed file must NOT be listed as deferred.
        assert "20240410T120000Z-aabbccd0.md" not in text

        messages = [r.getMessage() for r in caplog.records]
        degraded = [m for m in messages if "DEGRADED" in m]
        assert degraded, messages
        # Machine-greppable summary: marker, deferred count, manifest path.
        assert any(
            "Done (DEGRADED — budget exhausted)" in m
            and "2 deferred" in m
            and "_deferred_work.md" in m
            for m in degraded
        ), degraded
        # The plain "Done:" success line must NOT also fire.
        assert not any(m.startswith("Done:") for m in messages), messages

    def test_clean_run_writes_no_manifest_and_clears_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=1)
        # Stale manifest from a previous budget-tripped run.
        stale = root / "wiki" / "_deferred_work.md"
        stale.write_text("# Deferred work — stale from previous run\n")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == 0
        assert not stale.exists()
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("Done:") for m in messages), messages
        assert not any("DEGRADED" in m for m in messages), messages


# ---------------------------------------------------------------------------
# Cap resolution precedence (mirrors resolutions.resolve_max_per_run)
# ---------------------------------------------------------------------------


class TestMaxApiCallsPrecedence:
    def test_default_is_800(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        assert DEFAULT_MAX_API_CALLS == 800
        assert librarian_max_api_calls(None) == 800
        assert librarian_max_api_calls({}) == 800

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "123")
        assert librarian_max_api_calls({"librarian": {"max_api_calls": 222}}) == 123

    def test_yaml_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        assert librarian_max_api_calls({"librarian": {"max_api_calls": 222}}) == 222

    def test_invalid_env_falls_back_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "banana")
        assert librarian_max_api_calls({"librarian": {"max_api_calls": 222}}) == 222

    def test_negative_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "-5")
        assert librarian_max_api_calls(None) == DEFAULT_MAX_API_CALLS

    def test_run_resolves_cap_from_env_when_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """run(max_api_calls=None) picks up the env cap (precedence wired in)."""
        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "1")
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=None,
        )

        assert rc == 0
        assert (root / "wiki" / "_deferred_work.md").exists()
        messages = [r.getMessage() for r in caplog.records]
        assert any("budget exhausted (2/1)" in m for m in messages), messages
