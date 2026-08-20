# SPDX-License-Identifier: Apache-2.0
"""The verdict-ledger advisor's ``run()`` wiring (issue athenaeum#712).

Issue athenaeum#712's Wiring AC: with ``librarian.verdict_ledger_enabled`` OFF
(the default), a live ``athenaeum run`` must be byte-identical to before this
issue — no new file under ``wiki/_verdicts/``, no new run-summary phase, no
exit-code change. With it ON (and the caller's run lock threaded through,
mirroring the real ``_cmd_run.py`` -> ``run(..., lock=lock)`` call), the
finalize phase must materialize a well-formed (if still comparator-empty)
ledger.

Two layers of coverage, deliberately kept separate:

- ``TestVerdictLedgerFinalizeAdvisor`` exercises ``_run_finalize_phase``
  directly (mirrors ``tests/test_librarian_zero_yield.py``'s
  ``_finalize_ctx`` pattern) — cheap, and proves the ledger-materializing
  LOGIC is correct in isolation. It does NOT go through the top-level
  ``run()`` entry point, so it never exercises ``run()``'s own
  ``ctx.lock = lock`` assignment or prove a real ``ctx.lock`` threads
  through the OTHER phases (wiki-dedup, entity tier, C2-C4) without
  incident.
- ``TestRunEndToEndLockThreading`` closes that gap: it drives the real
  top-level ``run(..., lock=lock)`` end to end, mirroring the established
  "empty-raw-backlog, no LLM mock needed" pattern already used elsewhere in
  this suite (e.g. ``tests/test_librarian_corrections.py``'s
  ``test_correction_phase_runs_before_entity_tier_phase``) — a knowledge
  root with zero raw files and zero wiki entities lets every LLM-calling
  phase no-op cleanly, so ``ANTHROPIC_API_KEY`` only needs to be a
  syntactically-present fake string; no network call is ever attempted, no
  ``anthropic.Anthropic`` mock is needed. Confirmed by grep before writing
  this: no existing test in the suite passed ``lock=`` to ``run()``, so the
  new parameter's full-pipeline integration was previously unit-tested only
  at the ``_run_finalize_phase`` boundary above, never end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.librarian import RunContext, _run_finalize_phase, run
from athenaeum.runlock import RunLock
from athenaeum.verdicts import epoch_registry_path, ledger_exists
from tests.test_librarian_run_phases import _make_ctx


def _finalize_ctx(tmp_path: Path, **overrides) -> RunContext:
    """Mirrors ``tests/test_librarian_zero_yield.py``'s helper of the same shape."""
    ctx = _make_ctx(
        tmp_path,
        push_after_run=False,
        cluster_only=False,
        strict_budget=False,
    )
    ctx.wiki_root.mkdir(parents=True, exist_ok=True)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


class TestVerdictLedgerFinalizeAdvisor:
    def test_flag_off_leaves_no_verdicts_dir(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            ctx = _finalize_ctx(
                tmp_path,
                config={"librarian": {"verdict_ledger_enabled": False}},
                lock=lock,
            )
            _run_finalize_phase(ctx)
        assert ledger_exists(ctx.wiki_root) is False

    def test_flag_off_default_config_leaves_no_verdicts_dir(self, tmp_path: Path) -> None:
        """No librarian.verdict_ledger_enabled key at all -> same as False."""
        lock = RunLock(tmp_path)
        with lock:
            ctx = _finalize_ctx(tmp_path, config={}, lock=lock)
            _run_finalize_phase(ctx)
        assert ledger_exists(ctx.wiki_root) is False

    def test_flag_on_with_lock_materializes_well_formed_ledger(
        self, tmp_path: Path
    ) -> None:
        lock = RunLock(tmp_path)
        with lock:
            ctx = _finalize_ctx(
                tmp_path,
                config={"librarian": {"verdict_ledger_enabled": True}},
                lock=lock,
            )
            _run_finalize_phase(ctx)
        assert ledger_exists(ctx.wiki_root) is True
        assert epoch_registry_path(ctx.wiki_root).exists()

    def test_flag_on_without_lock_leaves_no_verdicts_dir(self, tmp_path: Path) -> None:
        """Mirrors a --dry-run caller, which never holds a lock — see
        run()'s `lock` docstring: with either condition unmet (flag off,
        or no lock), the finalize phase touches nothing under
        wiki/_verdicts/."""
        ctx = _finalize_ctx(
            tmp_path,
            config={"librarian": {"verdict_ledger_enabled": True}},
            lock=None,
        )
        _run_finalize_phase(ctx)
        assert ledger_exists(ctx.wiki_root) is False

    def test_flag_on_dry_run_leaves_no_verdicts_dir(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            ctx = _finalize_ctx(
                tmp_path,
                dry_run=True,
                config={"librarian": {"verdict_ledger_enabled": True}},
                lock=lock,
            )
            _run_finalize_phase(ctx)
        assert ledger_exists(ctx.wiki_root) is False

    def test_flag_off_run_summary_and_exit_code_unaffected(self, tmp_path: Path) -> None:
        """No new run-summary phase, no exit-code change with the flag off."""
        lock = RunLock(tmp_path)
        with lock:
            ctx_off = _finalize_ctx(
                tmp_path / "off",
                config={"librarian": {"verdict_ledger_enabled": False}},
                lock=lock,
            )
            rc_off = _run_finalize_phase(ctx_off)
        assert rc_off == 0
        assert ctx_off.run_profile == []


# ---------------------------------------------------------------------------
# TestRunEndToEndLockThreading — the top-level run(..., lock=lock) surface.
# ---------------------------------------------------------------------------


def _seed_empty_knowledge_root(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal knowledge root: schema only, zero raw files, zero wiki
    entities. Mirrors ``tests/test_librarian_corrections.py``'s
    ``TestPhaseOrdering::test_correction_phase_runs_before_entity_tier_phase``
    setup — the established "``run()`` completes cleanly against an empty
    corpus, no LLM client mock needed" pattern already used elsewhere in
    this suite. Returns ``(knowledge_root, wiki_root)``.
    """
    root = tmp_path / "knowledge"
    root.mkdir()
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n"
    )
    (wiki / "_schema" / "tags.md").write_text(
        "# Tags\n\n| Tag |\n|-----|\n| active |\n"
    )
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    (root / "raw" / "sessions").mkdir(parents=True)

    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root, wiki


class TestRunEndToEndLockThreading:
    """Drives the real top-level ``run()`` entry point, not ``_run_finalize_phase``.

    The whole point is coverage of ``run()``'s own ``ctx.lock = lock``
    assignment and proof that a genuinely held ``RunLock`` threads through
    the rest of the phase pipeline (wiki-dedup, entity tier, C2-C4,
    finalize) without incident — see the module docstring.
    """

    def test_flag_on_end_to_end_materializes_well_formed_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, wiki = _seed_empty_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  verdict_ledger_enabled: true\n"
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        lock = RunLock(root)
        with lock:
            rc = run(
                raw_root=root / "raw",
                wiki_root=wiki,
                knowledge_root=root,
                max_runtime=30,
                lock=lock,
            )
        assert rc == 0
        assert ledger_exists(wiki) is True
        assert epoch_registry_path(wiki).exists()

    def test_flag_off_end_to_end_leaves_no_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No athenaeum.yaml at all -- default config, flag off (the
        default state for every real operator today) -- must be
        byte-identical to before athenaeum#712: no wiki/_verdicts/ directory,
        clean exit code, same as the flag-on run's rc above."""
        root, wiki = _seed_empty_knowledge_root(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        lock = RunLock(root)
        with lock:
            rc = run(
                raw_root=root / "raw",
                wiki_root=wiki,
                knowledge_root=root,
                max_runtime=30,
                lock=lock,
            )
        assert rc == 0
        assert ledger_exists(wiki) is False
