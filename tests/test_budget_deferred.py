# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#220 — run-level budget exhaustion must leave a paper trail.

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
    EXIT_LIBRARIAN_REFUSAL,
    librarian_max_api_calls,
    run,
)
from athenaeum.resolutions import resolve_max_per_run

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

    def test_degraded_summary_logs_at_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The DEGRADED summary line is a WARNING, matching the trip warning."""
        root = _seed_knowledge_root(tmp_path, n_files=2)
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
            max_api_calls=1,
        )

        assert rc == 0
        degraded = [
            r for r in caplog.records if "DEGRADED — budget exhausted" in r.getMessage()
        ]
        assert degraded, [r.getMessage() for r in caplog.records]
        assert all(r.levelno == logging.WARNING for r in degraded), degraded

    def test_empty_intake_clears_stale_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Empty intake is a clean run: stale manifest must be removed.

        Regression: the `if not raw_files: return 0` early-return used to
        fire BEFORE the stale-manifest clear, preserving a stale manifest
        forever once the backlog drained without new intake.
        """
        root = _seed_knowledge_root(tmp_path, n_files=0)
        stale = root / "wiki" / "_deferred_work.md"
        stale.write_text("# Deferred work — stale from previous run\n")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert rc == 0
        assert not stale.exists()
        messages = [r.getMessage() for r in caplog.records]
        assert any("No raw files to process" in m for m in messages), messages

    def test_461_empty_entity_intake_still_runs_automemory_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#461, AC4: an empty entity/raw intake must NOT return early
        anymore — the auto-memory block (independent of raw entity intake)
        still runs. Regression guard for the pre-athenaeum#461 `if not raw_files:
        ... return 0` early return, which the athenaeum#461 reorder removed in favor
        of falling through to the (now second) auto-memory block.
        """
        root = _seed_knowledge_root(tmp_path, n_files=0)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        fake_am = SimpleNamespace(origin_scope="scope-a")
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files",
            lambda *_a, **_k: [fake_am],
        )
        compile_calls: list[object] = []

        def _spy_compile(*args, **kwargs):
            compile_calls.append((args, kwargs))
            return []

        monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _spy_compile)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert rc == 0
        messages = [r.getMessage() for r in caplog.records]
        assert any("No raw files to process" in m for m in messages), messages
        # The auto-memory block ran despite the empty entity intake — proving
        # the old whole-run early return is gone.
        assert len(compile_calls) == 1, "auto-memory compile must still run on empty raw intake"

    def test_dry_run_trip_writes_no_manifest_and_keeps_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dry run never trips the budget, never writes, never clears."""
        root = _seed_knowledge_root(tmp_path, n_files=3)
        stale = root / "wiki" / "_deferred_work.md"
        stale_content = "# Deferred work — stale from previous run\n"
        stale.write_text(stale_content)
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            max_api_calls=1,
        )

        assert rc == 0
        # Stale manifest untouched, byte-for-byte.
        assert stale.read_text(encoding="utf-8") == stale_content
        messages = [r.getMessage() for r in caplog.records]
        assert not any("DEGRADED" in m for m in messages), messages

    def test_second_tripped_run_overwrites_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A second tripped run replaces the manifest; it does not append."""
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        kwargs = dict(raw_root=root / "raw", wiki_root=root / "wiki")
        # Run 1: file 0 processed (and deleted), files 1+2 deferred.
        assert run(knowledge_root=root, max_api_calls=1, **kwargs) == 0
        # Run 2: file 1 processed, file 2 deferred.
        assert run(knowledge_root=root, max_api_calls=1, **kwargs) == 0

        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert text.count("# Deferred work — librarian run budget exhausted") == 1
        assert "deferred_count: 1" in text
        assert "20240412T120000Z-aabbccd2.md" in text
        # Run 1's deferred (now-processed) file must NOT linger.
        assert "20240411T120000Z-aabbccd1.md" not in text

    def test_trip_counts_backlog_beyond_max_files_window(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """max_files truncation must not hide backlog from the manifest.

        4 files, max_files=2, budget 1: file 0 is processed, file 1 is the
        in-window deferral, files 2+3 sit beyond the window. deferred_count
        reports the TRUE backlog (3), with the window split itemized.
        """
        root = _seed_knowledge_root(tmp_path, n_files=4)
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
            max_files=2,
            max_api_calls=1,
        )

        assert rc == 0
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 3" in text
        assert "deferred_in_window: 1" in text
        assert "deferred_beyond_window: 2" in text
        assert "plus 2 more beyond the max_files window" in text
        assert "20240411T120000Z-aabbccd1.md" in text
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "DEGRADED — budget exhausted" in m and "3 deferred" in m for m in messages
        ), messages

    def test_failed_files_get_manifest_section(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Files that errored this run appear in their own manifest section."""
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        inner = _fake_process_one_factory()

        def failing_process_one(raw, *args, **kwargs):
            if "aabbccd0" in raw.ref:
                raise RuntimeError("boom — malformed file")
            return inner(raw, *args, **kwargs)

        monkeypatch.setattr("athenaeum.librarian.process_one", failing_process_one)
        caplog.set_level(logging.INFO, logger="athenaeum")

        # Budget 1: file 0 fails (burns nothing), file 1 burns 2, file 2 trips.
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1,
        )

        # Failed files keep the existing exit-1 contract.
        assert rc == 1
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "## Failed this run (retried next run)" in text
        failed_section = text.split("## Failed this run (retried next run)")[1]
        assert "20240410T120000Z-aabbccd0.md" in failed_section
        # The failed file is NOT in the deferred list; the tripped file is.
        deferred_section = text.split("## Deferred raw files")[1].split(
            "## Failed this run"
        )[0]
        assert "20240410T120000Z-aabbccd0.md" not in deferred_section
        assert "20240412T120000Z-aabbccd2.md" in deferred_section


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

    def test_explicit_arg_beats_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """run(max_api_calls=200) wins over env ATHENAEUM_MAX_API_CALLS=1."""
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
            max_api_calls=200,
        )

        assert rc == 0
        assert not (root / "wiki" / "_deferred_work.md").exists()
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("Done:") for m in messages), messages
        assert not any("DEGRADED" in m for m in messages), messages

    def test_env_zero_is_valid_cap_everything_deferred(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """env "0" is a VALID cap (not a fallback): every file is deferred.

        Pinning the surprise: zero passes the ``value >= 0`` env gate, so a
        run with ATHENAEUM_MAX_API_CALLS=0 trips immediately and defers the
        whole intake -- but issue athenaeum#1135 changed the exit code: this
        is EXACTLY the zero-progress DEGRADED REFUSAL (early-stop reason +
        zero files committed), so it now exits EXIT_LIBRARIAN_REFUSAL (3),
        not 0. Before athenaeum#1135 this test asserted ``rc == 0`` -- the
        precise "compiled nothing, looked like success" hole the issue
        closes. See TestLibrarianRunRefusal below for the dedicated
        athenaeum#1135 predicate/marker-line/allow_degraded coverage.
        """
        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "0")
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

        assert rc == EXIT_LIBRARIAN_REFUSAL
        manifest = root / "wiki" / "_deferred_work.md"
        assert manifest.exists()
        text = manifest.read_text(encoding="utf-8")
        assert "deferred_count: 2" in text
        assert "20240410T120000Z-aabbccd0.md" in text
        assert "20240411T120000Z-aabbccd1.md" in text
        messages = [r.getMessage() for r in caplog.records]
        assert any("budget exhausted (0/0)" in m for m in messages), messages
        degraded_markers = [
            r for r in caplog.records if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert degraded_markers, [r.getMessage() for r in caplog.records]
        assert degraded_markers[0].levelno == logging.ERROR
        assert "reason=budget files=0" in degraded_markers[0].getMessage()


# ---------------------------------------------------------------------------
# Zero-budget startup warning (athenaeum#235)
# ---------------------------------------------------------------------------


class TestZeroBudgetStartupWarning:
    """A resolved budget of 0 must be flagged loudly at run start.

    Env/yaml ``0`` is a valid defer-everything cap, but it is also the most
    common accidental misconfiguration — issue athenaeum#235 requires a prominent
    WARNING at run start so an unintended 0 is diagnosable immediately
    rather than from the DEGRADED summary.
    """

    def test_warning_fires_when_resolved_budget_is_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "0")
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

        # Issue athenaeum#1135: max_api_calls=0 trips immediately on this run's
        # single file (files=0 processed) -- a zero-progress DEGRADED
        # REFUSAL, not the plain 0 this test asserted pre-athenaeum#1135. The
        # startup-warning behavior under test here is otherwise unchanged.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        zero_warnings = [
            r for r in caplog.records if "API budget is 0" in r.getMessage()
        ]
        # Exactly once: the warning must fire, and must not be duplicated.
        assert len(zero_warnings) == 1, [r.getMessage() for r in caplog.records]
        assert all(r.levelno == logging.WARNING for r in zero_warnings)
        # The message must name the knobs so the operator can fix it.
        assert any(
            "ATHENAEUM_MAX_API_CALLS" in r.getMessage()
            and "librarian.max_api_calls" in r.getMessage()
            for r in zero_warnings
        ), [r.getMessage() for r in zero_warnings]

    def test_warning_does_not_fire_when_budget_positive(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=1)
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
        messages = [r.getMessage() for r in caplog.records]
        assert not any("API budget is 0" in m for m in messages), messages

    def test_warning_does_not_fire_when_budget_positive_from_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Positive budget resolved from env (not CLI arg) is also silent."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_MAX_API_CALLS", "5")
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
        messages = [r.getMessage() for r in caplog.records]
        assert not any("API budget is 0" in m for m in messages), messages

    def test_warning_fires_for_yaml_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """yaml ``librarian.max_api_calls: 0`` triggers the warning too."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  max_api_calls: 0\n", encoding="utf-8"
        )
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
            max_api_calls=None,
        )

        # Issue athenaeum#1135: same zero-progress DEGRADED REFUSAL as
        # test_warning_fires_when_resolved_budget_is_zero above.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        messages = [r.getMessage() for r in caplog.records]
        assert any("API budget is 0" in m for m in messages), messages


# ---------------------------------------------------------------------------
# Run-level budget threading (athenaeum#220 fix round, finding 3)
# ---------------------------------------------------------------------------


class TestRunLevelBudgetThreading:
    def test_entity_phase_spend_survives_to_the_run_level_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """API calls burned by the entity phase count against max_api_calls
        and remain visible on the SHARED run-level ``TokenUsage`` all the
        way through to the finalize-phase "Token usage:" summary.

        Issue athenaeum#461: the entity phase now runs BEFORE the auto-memory
        block and gets first claim on the shared run-level budget (inverted
        from the pre-athenaeum#461 ordering, where the merge pass ran first and
        could starve the entity loop).

        Issue athenaeum#1256 retired the C4 contradiction detector along with
        ``merge_clusters_to_wiki``'s ``usage`` parameter -- this test used to
        prove threading survived the athenaeum#461 reorder by reading the entity
        phase's accumulated spend back out of the merge call's own kwargs.
        The merge pass takes no budget-relevant argument at all now and
        makes no LLM call, so that specific observation point is gone; the
        underlying invariant (the entity phase's spend is not lost or
        reset by the time the run finishes) is still real and still worth
        proving, just via the run's own end-of-run summary line instead --
        the same ``caplog``/"Token usage:" idiom
        ``test_run_summary_cache_line_renders_from_resolver_only_traffic``
        below already uses for exactly this purpose.
        """
        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        fake_am = SimpleNamespace(origin_scope="user")
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files",
            lambda *a, **k: [fake_am],
        )
        # athenaeum#370 PR2: _run_cluster_pass now returns None (whole-corpus) or the
        # delta affected-id set — not the old int cluster count.
        monkeypatch.setattr(
            "athenaeum.librarian._run_cluster_pass", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "athenaeum.librarian.merge_clusters_to_wiki", lambda *a, **k: []
        )
        monkeypatch.setattr(
            "athenaeum.librarian._run_reresolve_pass", lambda *a, **k: 0
        )
        # Entity stand-in burns 3 calls/file: the pre-file budget check (< 5)
        # lets both files run (0 -> 3 -> 6), so the entity phase completes
        # normally (no entity-side "budget exhausted" trip) but leaves 6
        # calls already spent on the shared TokenUsage before the
        # auto-memory block (which now spends nothing) ever runs.
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory(calls_per_file=3)
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=5,
        )

        assert rc == 0
        # The merge pass ran AFTER the entity phase and spent nothing; the
        # finalize-phase summary (recorded last, after every phase) must
        # still report the entity phase's full 6-call spend — proving the
        # shared TokenUsage survives the athenaeum#461 reorder all the way to
        # the end of the run, not merely across the one boundary C4 used to
        # guard.
        token_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("Token usage:")
        ]
        assert token_lines, [r.getMessage() for r in caplog.records]
        assert "6 API calls" in token_lines[0]

    def test_yaml_bool_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`max_api_calls: yes` in yaml must not become a cap of 1 (bool is int)."""
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        assert (
            librarian_max_api_calls({"librarian": {"max_api_calls": True}})
            == DEFAULT_MAX_API_CALLS
        )
        assert (
            librarian_max_api_calls({"librarian": {"max_api_calls": False}})
            == DEFAULT_MAX_API_CALLS
        )

    def test_resolve_max_per_run_yaml_bool_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same one-line bool guard on resolutions.resolve_max_per_run."""
        from athenaeum.resolutions import DEFAULT_RESOLVE_MAX_PER_RUN

        monkeypatch.delenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", raising=False)
        assert (
            resolve_max_per_run({"contradiction": {"resolve_max_per_run": True}})
            == DEFAULT_RESOLVE_MAX_PER_RUN
        )
        assert resolve_max_per_run({"contradiction": {"resolve_max_per_run": 7}}) == 7


# ---------------------------------------------------------------------------
# merge-only / cluster-only early returns clear the stale manifest
# (v0.7.3 release-gate review)
# ---------------------------------------------------------------------------


class TestEarlyReturnPathsClearStaleManifest:
    @pytest.mark.parametrize("mode", ["merge_only", "cluster_only"])
    def test_early_return_clears_stale_manifest(
        self, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """merge-only / cluster-only runs are clean runs: stale manifest goes.

        Regression: both early-return paths used to skip the stale
        ``_deferred_work.md`` clearing that full and empty-intake runs
        perform, so a budget-tripped full run followed by merge-only or
        cluster-only maintenance runs preserved the stale manifest
        indefinitely.
        """
        root = _seed_knowledge_root(tmp_path, n_files=0)
        stale = root / "wiki" / "_deferred_work.md"
        stale.write_text("# Deferred work — stale from previous run\n")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            **{mode: True},
        )

        assert rc == 0
        assert not stale.exists()

    @pytest.mark.parametrize("mode", ["merge_only", "cluster_only"])
    def test_dry_run_early_return_keeps_stale_manifest(
        self, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry-run merge-only / cluster-only must not touch the manifest."""
        root = _seed_knowledge_root(tmp_path, n_files=0)
        stale = root / "wiki" / "_deferred_work.md"
        stale_content = "# Deferred work — stale from previous run\n"
        stale.write_text(stale_content)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            **{mode: True},
        )

        assert rc == 0
        assert stale.read_text(encoding="utf-8") == stale_content


# ---------------------------------------------------------------------------
# --strict-budget: opt-in nonzero exit on a budget-tripped run (issue athenaeum#227)
# ---------------------------------------------------------------------------


class TestStrictBudget:
    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n_files: int
    ) -> Path:
        root = _seed_knowledge_root(tmp_path, n_files=n_files)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        return root

    def test_trip_with_strict_budget_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Budget trip + strict_budget=True → exit 1 (manifest still written)."""
        root = self._setup(tmp_path, monkeypatch, n_files=3)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1,
            strict_budget=True,
        )

        assert rc == 1
        # The DEGRADED-path side effects are unchanged: manifest still lands.
        assert (root / "wiki" / "_deferred_work.md").exists()

    def test_trip_without_flag_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default behavior unchanged: budget trip exits 0."""
        root = self._setup(tmp_path, monkeypatch, n_files=3)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1,
        )

        assert rc == 0

    def test_clean_run_with_strict_budget_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """strict_budget on a run that never trips must not change the exit."""
        root = self._setup(tmp_path, monkeypatch, n_files=1)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=50,
            strict_budget=True,
        )

        assert rc == 0
        assert not (root / "wiki" / "_deferred_work.md").exists()
