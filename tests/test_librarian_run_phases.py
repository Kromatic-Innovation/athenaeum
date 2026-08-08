# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ``librarian.run()`` phase split (issue athenaeum#546).

``run()`` used to be a ~1,300-line god-function interleaving git
preconditions, config resolution, signal handling, deadline arming, spend
accounting, wiki-dedup, the tier loop, and auto-memory compile — all only
reachable end-to-end via a real (mocked-LLM) run. athenaeum#546 extracted each
``# ---`` section into a named phase function taking a shared
:class:`~athenaeum.librarian.RunContext`, so each phase is now importable
and testable in isolation.

This file covers the phases that were previously untestable without driving
a full ``run()``: the git/config preconditions gate, the several config
resolutions, VCS I/O, deadline arming, and the wiki-dedup phase's own
deadline check. The full end-to-end behavior (signal handling, the tier
loop, auto-memory, finalize return codes) stays covered by the existing
``test_librarian*.py`` suite, which now doubles as this refactor's
behavior-preservation guard — unchanged black-box assertions against the
public ``run()`` entry point.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from athenaeum.librarian import (
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_RUNTIME,
    RunContext,
    _arm_run_deadline,
    _resolve_run_config,
    _run_git_vcs_io,
    _run_preconditions,
    _run_wiki_dedup_phase,
    _warn_if_knob_provider_override_inert,
)
from athenaeum.provider import ProviderConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Phase Test")
    # ``git commit`` no-ops on an empty tree (e.g. a bare ``wiki/`` dir with
    # no tracked files, since git does not track empty directories) — seed a
    # placeholder so the initial commit always has something to commit.
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")


def _make_ctx(tmp_path: Path, **overrides) -> RunContext:
    """A minimal RunContext with sane defaults, mirroring how ``run()``
    constructs it before the phase calls."""
    knowledge_root = overrides.pop("knowledge_root", tmp_path / "knowledge")
    wiki_root = overrides.pop("wiki_root", knowledge_root / "wiki")
    raw_root = overrides.pop("raw_root", knowledge_root / "raw")
    defaults = dict(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=None,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=None,
        heartbeat=None,
        out_run_stats=None,
    )
    defaults.update(overrides)
    ctx = RunContext(**defaults)
    ctx.skip_entity_tiers = ctx.cluster_only or ctx.merge_only
    ctx.config = {}
    return ctx


# ---------------------------------------------------------------------------
# RunContext basics
# ---------------------------------------------------------------------------


class TestRunContext:
    def test_deadline_exceeded_false_when_disabled(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.run_deadline is None
        assert ctx.deadline_exceeded() is False

    def test_deadline_exceeded_true_once_past(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.run_deadline = 0.0  # already in the past (monotonic() > 0)
        assert ctx.deadline_exceeded() is True

    def test_tick_heartbeat_calls_injected_callback(self, tmp_path: Path) -> None:
        calls = []
        ctx = _make_ctx(tmp_path, heartbeat=lambda: calls.append(1))
        ctx.tick_heartbeat()
        ctx.tick_heartbeat()
        assert len(calls) == 2

    def test_tick_heartbeat_noop_when_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, heartbeat=None)
        ctx.tick_heartbeat()  # must not raise

    def test_export_run_stats_populates_out_dict(self, tmp_path: Path) -> None:
        stats: dict = {}
        ctx = _make_ctx(tmp_path, out_run_stats=stats)
        ctx.beyond_window = 3
        ctx.deferred_refs = ["a.md", "b.md"]
        ctx.failed_files = ["c.md"]
        ctx.stuck_files = [{"ref": "d.md", "failures": 3, "action": "update:X", "error": "E"}]
        ctx.entity_budget_tripped = True
        ctx.processed_count = 5
        ctx.export_run_stats()
        assert stats == {
            "beyond_window": 3,
            "deferred_refs": ["a.md", "b.md"],
            "failed_files": ["c.md"],
            # Issue athenaeum#663: stuck files exported as machine-detectable run state.
            "stuck_files": [{"ref": "d.md", "failures": 3, "action": "update:X", "error": "E"}],
            # Issue athenaeum#669: the entity-share yield (athenaeum#440) as
            # machine-detectable state.
            "entity_budget_tripped": True,
            "entity_files_claimed": 5,
            "entity_files_deferred": 2,
        }

    def test_mutation_through_context_is_visible_to_next_phase(
        self, tmp_path: Path
    ) -> None:
        """The dataclass must carry mutation BY REFERENCE, not snapshot —
        the spec's explicit gotcha. A field mutated by one "phase" (here
        simulated directly) must be visible to code that reads ctx after."""
        ctx = _make_ctx(tmp_path)
        ctx.total_created = 0
        _pretend_phase_mutates(ctx)
        assert ctx.total_created == 5
        assert ctx.deferred_refs == ["x.md"]


def _pretend_phase_mutates(ctx: RunContext) -> None:
    ctx.total_created += 5
    ctx.deferred_refs.append("x.md")


# ---------------------------------------------------------------------------
# _run_preconditions
# ---------------------------------------------------------------------------


class TestRunPreconditions:
    def test_bad_provider_config_returns_1(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with patch(
            "athenaeum.librarian.resolve_provider",
            side_effect=ProviderConfigError("bad provider"),
        ):
            assert _run_preconditions(ctx) == 1

    def test_preflight_failure_returns_1(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="claude-cli"),
            patch(
                "athenaeum.librarian.preflight_provider",
                return_value="claude binary missing",
            ),
        ):
            assert _run_preconditions(ctx) == 1

    def test_missing_api_key_returns_1_for_api_provider(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.api_key = None
        ctx.dry_run = False
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) == 1

    def test_missing_api_key_ok_on_dry_run(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=True
        )
        ctx.api_key = None
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) is None

    def test_missing_wiki_root_returns_1(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        ctx = _make_ctx(tmp_path, knowledge_root=knowledge_root, dry_run=True)
        ctx.api_key = "sk-test"
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) == 1

    def test_missing_git_returns_1_when_not_dry_run(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=False
        )
        ctx.api_key = "sk-test"
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) == 1

    def test_skip_entity_tiers_bypasses_wiki_and_git_checks(
        self, tmp_path: Path
    ) -> None:
        """cluster_only/merge_only must not require wiki_root or .git to
        exist — mirrors the original ``not skip_entity_tiers`` guards."""
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        ctx = _make_ctx(
            tmp_path,
            knowledge_root=knowledge_root,
            wiki_root=knowledge_root / "nonexistent-wiki",
            dry_run=False,
            cluster_only=True,
        )
        ctx.skip_entity_tiers = True
        ctx.api_key = None
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) is None

    def test_all_checks_pass_returns_none_and_sets_provider(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        _git_init(knowledge_root)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=False
        )
        ctx.api_key = "sk-test"
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
        ):
            assert _run_preconditions(ctx) is None
        assert ctx.provider == "api"


# ---------------------------------------------------------------------------
# _warn_if_knob_provider_override_inert (issue athenaeum#786)
#
# Mirrors tests/test_reasoning_tiers.py::TestInertModelKnobWarning (issue
# athenaeum#780's precedent this function follows): a per-knob provider override
# for a knob the librarian pipeline does not yet route per-knob warns loudly
# (naming the knob) instead of silently doing nothing; a knob that IS routed
# (``topic``), or no override at all, stays silent.
# ---------------------------------------------------------------------------


class TestWarnIfKnobProviderOverrideInert:
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for knob in (
            "CLASSIFY",
            "WRITE",
            "RESOLVE",
            "TOPIC",
            "REASONING_T1",
            "REASONING_T2",
        ):
            monkeypatch.delenv(f"ATHENAEUM_{knob}_LLM_PROVIDER", raising=False)

    def test_warns_for_write_knob_yaml_override(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._clear_env(monkeypatch)
        config = {"llm": {"providers": {"write": "claude-cli"}}}
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _warn_if_knob_provider_override_inert(config)
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "write" in msg
        assert "no effect" in msg.lower()
        assert "llm.providers.write" in msg

    def test_warns_for_env_override(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._clear_env(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_CLASSIFY_LLM_PROVIDER", "claude-cli")
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _warn_if_knob_provider_override_inert(None)
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "classify" in msg
        assert "ATHENAEUM_CLASSIFY_LLM_PROVIDER" in msg

    def test_warns_once_per_ineffective_knob_when_several_set(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._clear_env(monkeypatch)
        config = {
            "llm": {
                "providers": {
                    "classify": "claude-cli",
                    "write": "claude-cli",
                    "reasoning_t1": "claude-cli",
                }
            }
        }
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _warn_if_knob_provider_override_inert(config)
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 3
        assert any("classify" in m for m in messages)
        assert any("write" in m for m in messages)
        assert any("reasoning_t1" in m for m in messages)

    def test_no_warning_for_topic_knob_override(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ``topic`` IS routed (query_topics resolves it independently) — an
        # override there must never trigger the ineffective-knob warning.
        self._clear_env(monkeypatch)
        config = {"llm": {"providers": {"topic": "claude-cli"}}}
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _warn_if_knob_provider_override_inert(config)
        assert caplog.records == []

    def test_no_warning_when_no_per_knob_keys(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # AC6: a config with no ``llm.providers`` section (and no per-knob
        # env vars) logs NOTHING — byte-identical to a pre-athenaeum#786 install.
        self._clear_env(monkeypatch)
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _warn_if_knob_provider_override_inert(None)
            _warn_if_knob_provider_override_inert({})
            _warn_if_knob_provider_override_inert({"llm": {"provider": "claude-cli"}})
        assert caplog.records == []

    def test_wired_into_run_preconditions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Integration check: the warning actually fires as part of the real
        startup gate, not just when called directly."""
        self._clear_env(monkeypatch)
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=True
        )
        ctx.api_key = "sk-test"
        ctx.config = {"llm": {"providers": {"write": "claude-cli"}}}
        with (
            patch("athenaeum.librarian.resolve_provider", return_value="api"),
            patch("athenaeum.librarian.preflight_provider", return_value=None),
            caplog.at_level("WARNING", logger="athenaeum.librarian"),
        ):
            assert _run_preconditions(ctx) is None
        assert any(
            "write" in r.getMessage() and "no effect" in r.getMessage().lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# _resolve_run_config
# ---------------------------------------------------------------------------


class TestResolveRunConfig:
    def test_defaults_applied_when_all_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.provider = "api"
        assert _resolve_run_config(ctx) is None
        assert ctx.max_api_calls == DEFAULT_MAX_API_CALLS
        assert ctx.max_files == DEFAULT_MAX_FILES
        assert ctx.max_runtime == DEFAULT_MAX_RUNTIME
        assert ctx.batch_mode is False
        assert ctx.retire is True
        assert ctx.push_after_run is False
        assert ctx.pull_before_run is False

    def test_explicit_args_win_over_defaults(self, tmp_path: Path) -> None:
        ctx = _make_ctx(
            tmp_path,
            max_api_calls=42,
            max_files=7,
            max_runtime=99,
            batch_mode=False,
            retire=False,
            push_after_run=True,
            pull_before_run=True,
        )
        ctx.provider = "api"
        assert _resolve_run_config(ctx) is None
        assert ctx.max_api_calls == 42
        assert ctx.max_files == 7
        assert ctx.max_runtime == 99
        assert ctx.retire is False
        assert ctx.push_after_run is True
        assert ctx.pull_before_run is True

    def test_batch_mode_rejected_for_claude_cli_provider(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "claude-cli"
        assert _resolve_run_config(ctx) == 1

    def test_batch_mode_allowed_for_api_provider(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "api"
        assert _resolve_run_config(ctx) is None
        assert ctx.batch_mode is True

    # -- issue athenaeum#786 AC5: the guard checks the ``classify``/``write``
    # knobs' OWN resolved providers (batch.py's two execute_batch call
    # sites), not just ``ctx.provider`` -----------------------------------

    def test_batch_mode_rejected_when_write_knob_overridden_to_claude_cli(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "api"  # global default supports batching
        ctx.config = {"llm": {"providers": {"write": "claude-cli"}}}
        assert _resolve_run_config(ctx) == 1

    def test_batch_mode_rejected_when_classify_knob_overridden_to_claude_cli(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "api"
        ctx.config = {"llm": {"providers": {"classify": "claude-cli"}}}
        assert _resolve_run_config(ctx) == 1

    def test_batch_mode_allowed_when_unrelated_knob_overridden_to_claude_cli(
        self, tmp_path: Path
    ) -> None:
        # ``topic`` is not one of the two knobs the batch path serves — an
        # override there must not trip the guard.
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "api"
        ctx.config = {"llm": {"providers": {"topic": "claude-cli"}}}
        assert _resolve_run_config(ctx) is None
        assert ctx.batch_mode is True

    def test_batch_mode_no_per_knob_keys_matches_global_byte_identical(
        self, tmp_path: Path
    ) -> None:
        # AC6: a config with no ``llm.providers`` section resolves every knob
        # to ``ctx.provider`` — same outcome as the pre-athenaeum#786 single-check
        # guard for both the reject and allow cases.
        ctx = _make_ctx(tmp_path, batch_mode=True)
        ctx.provider = "claude-cli"
        ctx.config = {}
        assert _resolve_run_config(ctx) == 1

        ctx2 = _make_ctx(tmp_path, batch_mode=True)
        ctx2.provider = "api"
        ctx2.config = {}
        assert _resolve_run_config(ctx2) is None

    def test_zero_budget_is_valid_and_does_not_error(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, max_api_calls=0)
        ctx.provider = "api"
        assert _resolve_run_config(ctx) is None
        assert ctx.max_api_calls == 0


# ---------------------------------------------------------------------------
# _run_git_vcs_io
# ---------------------------------------------------------------------------


class TestRunGitVcsIo:
    def test_captures_head_when_not_dry_run(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        (knowledge_root / "wiki").mkdir()
        _git_init(knowledge_root)
        expected_head = _git(
            knowledge_root, "rev-parse", "HEAD"
        ).stdout.strip()

        ctx = _make_ctx(tmp_path, knowledge_root=knowledge_root, dry_run=False)
        ctx.pull_before_run = False
        _run_git_vcs_io(ctx)
        assert ctx.head_at_start == expected_head

    def test_head_is_none_on_dry_run(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        _git_init(knowledge_root)

        ctx = _make_ctx(tmp_path, knowledge_root=knowledge_root, dry_run=True)
        ctx.pull_before_run = False
        _run_git_vcs_io(ctx)
        assert ctx.head_at_start is None

    def test_pull_invoked_before_head_capture_when_enabled(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        _git_init(knowledge_root)

        ctx = _make_ctx(tmp_path, knowledge_root=knowledge_root, dry_run=False)
        ctx.pull_before_run = True
        calls: list[str] = []
        with patch(
            "athenaeum.librarian._maybe_pull_before_run",
            side_effect=lambda *a, **k: calls.append("pull"),
        ):
            _run_git_vcs_io(ctx)
        assert calls == ["pull"]
        assert ctx.head_at_start is not None


# ---------------------------------------------------------------------------
# _arm_run_deadline
# ---------------------------------------------------------------------------


class TestArmRunDeadline:
    def test_disabled_deadline_when_max_runtime_non_positive(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=0)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.run_deadline is None

    def test_deadline_armed_when_max_runtime_positive(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.run_deadline is not None
        assert ctx.run_deadline > 0

    def test_subscription_covered_flag_set_for_claude_cli(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "claude-cli"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.usage.subscription_covered is True

    def test_subscription_covered_flag_unset_for_api(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.usage.subscription_covered is False

    def test_out_run_stats_seeded_with_defaults(self, tmp_path: Path) -> None:
        stats: dict = {}
        ctx = _make_ctx(tmp_path, max_runtime=3600, out_run_stats=stats)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert stats == {
            "beyond_window": 0,
            "deferred_refs": [],
            "failed_files": [],
        }

    def test_out_run_stats_not_overwritten_if_already_present(
        self, tmp_path: Path
    ) -> None:
        stats = {"beyond_window": 9, "deferred_refs": ["a"], "failed_files": ["b"]}
        ctx = _make_ctx(tmp_path, max_runtime=3600, out_run_stats=stats)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.librarian.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert stats == {
            "beyond_window": 9,
            "deferred_refs": ["a"],
            "failed_files": ["b"],
        }


# ---------------------------------------------------------------------------
# _run_wiki_dedup_phase
# ---------------------------------------------------------------------------


class TestRunWikiDedupPhase:
    def test_skips_dedup_when_wiki_root_missing(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        # wiki_root does not exist on disk.
        with patch(
            "athenaeum.wiki_dedupe.propose_wiki_page_merges"
        ) as mock_dedup:
            result = _run_wiki_dedup_phase(ctx)
        mock_dedup.assert_not_called()
        assert result is None
        assert ctx.run_profile == []

    def test_runs_dedup_and_records_profile_when_wiki_root_present(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki_root)
        with patch(
            "athenaeum.wiki_dedupe.propose_wiki_page_merges"
        ) as mock_dedup:
            result = _run_wiki_dedup_phase(ctx)
        mock_dedup.assert_called_once()
        assert result is None
        assert len(ctx.run_profile) == 1
        assert ctx.run_profile[0][0] == "wiki-dedup"

    def test_dedup_exception_is_swallowed_and_still_profiled(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki_root)
        with patch(
            "athenaeum.wiki_dedupe.propose_wiki_page_merges",
            side_effect=RuntimeError("boom"),
        ):
            result = _run_wiki_dedup_phase(ctx)
        assert result is None  # swallowed, run continues
        assert len(ctx.run_profile) == 1

    def test_deadline_already_exceeded_stops_run_after_dedup(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()
        ctx = _make_ctx(
            tmp_path, wiki_root=wiki_root, knowledge_root=knowledge_root, dry_run=True
        )
        ctx.run_deadline = 0.0  # already elapsed
        ctx.max_runtime = 60
        with patch("athenaeum.wiki_dedupe.propose_wiki_page_merges"):
            result = _run_wiki_dedup_phase(ctx)
        assert result == 124
        assert ctx.summary_emitted is True

    def test_deadline_not_exceeded_returns_none(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki_root)
        ctx.run_deadline = None  # disabled
        with patch("athenaeum.wiki_dedupe.propose_wiki_page_merges"):
            result = _run_wiki_dedup_phase(ctx)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
