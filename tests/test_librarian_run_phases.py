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

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from athenaeum.decisions import list_pending_decisions
from athenaeum.librarian import (
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_RUNTIME,
    EXIT_GRACEFUL_PARTIAL,
    RunContext,
    _arm_run_deadline,
    _resolve_run_config,
    _run_git_vcs_io,
    _run_intake_audit_phase,
    _run_preconditions,
    _run_rule_proposal_phase,
    _run_wiki_dedup_phase,
)
from athenaeum.provider import ProviderConfigError
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.test_rule_proposals import (
    _NOW,
    _SMALL_CONFIG,
    _draft_payload,
    _fake_client,
    _seed_deferred_rows,
)

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
            # Issue athenaeum#898: quarantined files exported as machine-detectable
            # run state, mirroring stuck_files above (empty here — none set).
            "quarantined_files": [],
            # Issue athenaeum#669: the entity-share yield (athenaeum#440) as
            # machine-detectable state.
            "entity_budget_tripped": True,
            "entity_files_claimed": 5,
            "entity_files_deferred": 2,
            # Issue athenaeum#899: the zero-yield alarm's verdict, exported
            # alongside the other run-state flags. ``None`` here — the
            # predicate is only evaluated by ``_run_finalize_phase``, which
            # this unit test never calls.
            "zero_yield": None,
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
# Per-knob provider overrides are now genuinely EFFECTIVE (issue athenaeum#841,
# finishing the athenaeum#786 routing seam). Supersedes the old
# TestWarnIfKnobProviderOverrideInert class this file used to carry: an
# override for one of the five librarian-pipeline knobs used to be
# accepted-but-inert (warned, no effect on the client actually used); it now
# changes which client serves that knob, and a bad value fails the run
# loudly at the SAME preflight gate as the global provider (rather than
# surfacing as a raw traceback later, when ``_arm_run_deadline`` builds that
# knob's client).
# ---------------------------------------------------------------------------


class TestPerKnobProviderOverridesAreEffective:
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
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)

    def test_bad_per_knob_override_fails_preconditions_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=True
        )
        ctx.api_key = "sk-test"
        ctx.config = {"llm": {"providers": {"write": "not-a-real-provider"}}}
        with patch("athenaeum.librarian.preflight_provider", return_value=None):
            assert _run_preconditions(ctx) == 1

    def test_good_per_knob_overrides_pass_preconditions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_env(monkeypatch)
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        ctx = _make_ctx(
            tmp_path, knowledge_root=knowledge_root, wiki_root=wiki_root, dry_run=True
        )
        ctx.api_key = "sk-test"
        ctx.config = {"llm": {"providers": {"write": "claude-cli"}}}
        with patch("athenaeum.librarian.preflight_provider", return_value=None):
            assert _run_preconditions(ctx) is None

    def test_arm_run_deadline_builds_distinct_clients_per_knob_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core athenaeum#841 behavior: two knobs on different providers in
        ONE run get two DIFFERENT client objects, not one shared client."""
        self._clear_env(monkeypatch)
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {"llm": {"providers": {"write": "claude-cli"}}}
        ctx.api_key = "sk-test"

        with patch("athenaeum.provider._construct_client") as mock_construct:
            mock_construct.side_effect = lambda provider, **kw: object()
            _arm_run_deadline(ctx)

        assert ctx.knob_providers == {
            "classify": "api",
            "write": "claude-cli",
            "resolve": "api",
            "reasoning_t1": "api",
            "reasoning_t2": "api",
        }
        # ``write`` is on a DIFFERENT provider -> a DIFFERENT client object.
        assert ctx.write_client is not ctx.classify_client
        # classify/resolve/reasoning_t1/reasoning_t2 all share the global
        # provider -> the SAME cached client object (AC3: constructed per
        # DISTINCT provider, not per knob).
        assert ctx.classify_client is ctx.resolve_client
        assert ctx.classify_client is ctx.reasoning_t1_client
        assert ctx.classify_client is ctx.reasoning_t2_client

    def test_arm_run_deadline_single_client_with_no_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC6: a config with no ``llm.providers`` overrides constructs
        exactly ONE client shared by all five knobs — the pre-athenaeum#841
        single-``merge_client`` behavior, preserved byte-for-byte."""
        self._clear_env(monkeypatch)
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {}
        ctx.api_key = "sk-test"

        with patch("athenaeum.provider._construct_client") as mock_construct:
            mock_construct.side_effect = lambda provider, **kw: object()
            _arm_run_deadline(ctx)

        assert mock_construct.call_count == 1
        clients = {
            ctx.classify_client,
            ctx.write_client,
            ctx.resolve_client,
            ctx.reasoning_t1_client,
            ctx.reasoning_t2_client,
        }
        assert len(clients) == 1


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
        with patch("athenaeum.provider.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.run_deadline is None

    def test_deadline_armed_when_max_runtime_positive(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.provider.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.run_deadline is not None
        assert ctx.run_deadline > 0

    def test_subscription_covered_flag_set_for_claude_cli(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "claude-cli"
        ctx.config = {}
        with patch("athenaeum.provider.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.usage.subscription_covered is True

    def test_subscription_covered_flag_unset_for_api(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, max_runtime=3600)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.provider.build_llm_client", return_value=None):
            _arm_run_deadline(ctx)
        assert ctx.usage.subscription_covered is False

    def test_out_run_stats_seeded_with_defaults(self, tmp_path: Path) -> None:
        stats: dict = {}
        ctx = _make_ctx(tmp_path, max_runtime=3600, out_run_stats=stats)
        ctx.provider = "api"
        ctx.config = {}
        with patch("athenaeum.provider.build_llm_client", return_value=None):
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
        with patch("athenaeum.provider.build_llm_client", return_value=None):
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
        assert result == EXIT_GRACEFUL_PARTIAL
        assert ctx.summary_emitted is True

    def test_deadline_not_exceeded_returns_none(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki_root)
        ctx.run_deadline = None  # disabled
        with patch("athenaeum.wiki_dedupe.propose_wiki_page_merges"):
            result = _run_wiki_dedup_phase(ctx)
        assert result is None


# ---------------------------------------------------------------------------
# _run_intake_audit_phase (issue athenaeum#836)
# ---------------------------------------------------------------------------


class TestRunIntakeAuditPhase:
    def test_no_unclaimed_files_leaves_empty_summary(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_intake_audit_phase(ctx)
        assert ctx.intake_audit_summary == {
            "unclaimed_files": 0,
            "groups": 0,
            "raised_groups": 0,
            "raised_files": 0,
            "already_open_groups": 0,
        }
        assert not (ctx.wiki_root / "_pending_questions.md").exists()

    def test_unclaimed_file_raises_into_pending_questions(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        (ctx.raw_root / "daily-activity").mkdir(parents=True)
        (ctx.raw_root / "daily-activity" / "events.bak").write_text("{}\n")

        _run_intake_audit_phase(ctx)

        assert ctx.intake_audit_summary is not None
        assert ctx.intake_audit_summary["unclaimed_files"] == 1
        assert ctx.intake_audit_summary["raised_groups"] == 1
        pending = ctx.wiki_root / "_pending_questions.md"
        assert pending.exists()
        assert "unmatched extension" in pending.read_text()

    def test_dry_run_computes_counts_but_writes_nothing(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, dry_run=True)
        (ctx.raw_root / "daily-activity").mkdir(parents=True)
        (ctx.raw_root / "daily-activity" / "events.bak").write_text("{}\n")

        _run_intake_audit_phase(ctx)

        assert ctx.intake_audit_summary is not None
        assert ctx.intake_audit_summary["unclaimed_files"] == 1
        assert ctx.intake_audit_summary["raised_groups"] == 0
        assert not (ctx.wiki_root / "_pending_questions.md").exists()

    def test_recognised_raw_file_raises_nothing(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        (ctx.raw_root / "sessions").mkdir(parents=True)
        (ctx.raw_root / "sessions" / "20260810T120000Z-abcdef01.md").write_text(
            "---\n---\nbody\n"
        )

        _run_intake_audit_phase(ctx)

        assert ctx.intake_audit_summary is not None
        assert ctx.intake_audit_summary["unclaimed_files"] == 0
        assert not (ctx.wiki_root / "_pending_questions.md").exists()


# ---------------------------------------------------------------------------
# _run_rule_proposal_phase (issue athenaeum#1063 -- wires
# athenaeum.rule_proposals.run_rule_proposal_detection into the nightly run,
# closing the loop athenaeum#905 (detector) / athenaeum#921 (applier) opened)
# ---------------------------------------------------------------------------


def _rule_proposal_ctx(
    tmp_path: Path, *, count: int, config: dict, threshold: int = 3
) -> RunContext:
    """A RunContext whose wiki/raw roots already carry *count* deferred
    disposition rows for one shape (via ``_seed_deferred_rows``, which
    hardcodes ``tmp_path/"wiki"`` and ``tmp_path/"raw"``) -- so the caller
    only needs to choose the shape's row count and the phase's config."""
    wiki_root = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    _seed_deferred_rows(tmp_path, source="s", count=count, tier=None)
    ctx = _make_ctx(
        tmp_path,
        wiki_root=wiki_root,
        raw_root=raw_root,
        knowledge_root=tmp_path / "knowledge",
        now=_NOW,
    )
    ctx.provider = "api"
    ctx.config = config
    return ctx


def _rule_proposals_config(*, enabled: bool, threshold: int = 3) -> dict:
    return {
        "librarian": {
            "rule_proposals": {
                "enabled": enabled,
                "threshold": threshold,
                "window_days": 7,
                "exemplar_count": 2,
            }
        }
    }


class TestRunRuleProposalPhase:
    """Verification bar (athenaeum#1063): gate-off no-op, gate-on + threshold
    met proposes and reaches ``list_pending_decisions``, threshold not met
    is skipped even with the gate on, an expired deadline skips the phase,
    and the drafting call's tokens land in the spend ledger."""

    def test_noop_when_gate_off_by_default(self, tmp_path: Path) -> None:
        # No `librarian.rule_proposals.enabled` key at all -- the documented
        # default (config gate OFF).
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=_SMALL_CONFIG)
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_rule_proposal_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.rule_proposals_summary is None
        assert ctx.usage.api_calls == 0

    def test_noop_when_gate_explicitly_false(self, tmp_path: Path) -> None:
        config = _rule_proposals_config(enabled=False)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_rule_proposal_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.rule_proposals_summary is None
        assert ctx.usage.api_calls == 0

    def test_runs_and_proposes_when_gate_on_and_threshold_met(
        self, tmp_path: Path
    ) -> None:
        config = _rule_proposals_config(enabled=True, threshold=3)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        fake = _fake_client()
        with patch("athenaeum.librarian.build_llm_client", return_value=fake):
            _run_rule_proposal_phase(ctx)
        assert ctx.rule_proposals_summary is not None
        assert ctx.rule_proposals_summary["threshold_crossed"] == 1
        assert ctx.rule_proposals_summary["proposed"] == 1
        assert len(fake.calls) == 1  # exactly one drafting call

    def test_proposed_rule_reaches_list_pending_decisions(self, tmp_path: Path) -> None:
        """The JTBD athenaeum#1063 exists for: a `proposed-rule` decision must
        become visible through `list_pending_decisions` via the SAME wiring
        a real nightly run takes -- not merely via a direct call to
        `run_rule_proposal_detection` (which athenaeum#905's own tests already
        cover)."""
        config = _rule_proposals_config(enabled=True, threshold=3)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        fake = _fake_client()
        with patch("athenaeum.librarian.build_llm_client", return_value=fake):
            _run_rule_proposal_phase(ctx)
        decisions = list_pending_decisions(ctx.wiki_root)
        assert any(d["type"] == "proposed-rule" for d in decisions)

    def test_skipped_when_threshold_not_met_even_with_gate_on(
        self, tmp_path: Path
    ) -> None:
        config = _rule_proposals_config(enabled=True, threshold=3)
        # Only 2 deferred rows -- below the threshold of 3.
        ctx = _rule_proposal_ctx(tmp_path, count=2, config=config)
        fake = _fake_client()
        with patch("athenaeum.librarian.build_llm_client", return_value=fake):
            _run_rule_proposal_phase(ctx)
        assert ctx.rule_proposals_summary is not None
        assert ctx.rule_proposals_summary["threshold_crossed"] == 0
        assert ctx.rule_proposals_summary["proposed"] == 0
        assert fake.calls == []  # the model was never invoked
        decisions = list_pending_decisions(ctx.wiki_root)
        assert all(d["type"] != "proposed-rule" for d in decisions)

    def test_deadline_already_expired_skips_phase(self, tmp_path: Path) -> None:
        config = _rule_proposals_config(enabled=True, threshold=3)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        ctx.run_deadline = 0.0  # already elapsed
        ctx.max_runtime = 60
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_rule_proposal_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.rule_proposals_summary == {"skipped_deadline_tripped": True}
        assert ctx.usage.api_calls == 0

    def test_deadline_tripped_flag_skips_phase(self, tmp_path: Path) -> None:
        """``ctx.deadline_tripped`` (set by an earlier phase, e.g. the entity
        tier loop) skips this phase even if ``ctx.run_deadline`` itself has
        not technically elapsed yet -- mirrors the auto-memory block's own
        guard at its call site in ``run()``."""
        config = _rule_proposals_config(enabled=True, threshold=3)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        ctx.deadline_tripped = True
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_rule_proposal_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.rule_proposals_summary == {"skipped_deadline_tripped": True}

    def test_spend_ledger_records_usage_for_the_drafting_call(
        self, tmp_path: Path
    ) -> None:
        """Consistent with the tier-2/3 call sites (``tiers.py``'s
        ``_record_usage``): the drafting call's tokens land in ``ctx.usage``
        tagged ``knob="rule_proposals"``, and the knob's resolved
        provider/model are recorded for the end-of-run per-knob-provider
        spend split (issue athenaeum#841)."""
        config = _rule_proposals_config(enabled=True, threshold=3)
        ctx = _rule_proposal_ctx(tmp_path, count=3, config=config)
        payload = json.dumps(_draft_payload())
        usage_obj = make_llm_usage(input_tokens=111, output_tokens=22)
        fake = FakeLLMClient(response=make_llm_response(payload, usage=usage_obj))
        with patch("athenaeum.librarian.build_llm_client", return_value=fake):
            _run_rule_proposal_phase(ctx)
        assert ctx.usage.api_calls == 1
        assert ctx.usage.per_knob["rule_proposals"]["input_tokens"] == 111
        assert ctx.usage.per_knob["rule_proposals"]["output_tokens"] == 22
        assert ctx.knob_providers["rule_proposals"] == "api"
        assert ctx.knob_models["rule_proposals"]  # resolved to a concrete model id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
