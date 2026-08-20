# SPDX-License-Identifier: Apache-2.0
"""``_run_finalize_phase``'s verdict-ledger advisor (issue athenaeum#712).

Issue athenaeum#712's Wiring AC: with ``librarian.verdict_ledger_enabled`` OFF
(the default), a live ``athenaeum run`` must be byte-identical to before this
issue — no new file under ``wiki/_verdicts/``, no new run-summary phase, no
exit-code change. With it ON (and the caller's run lock threaded through,
mirroring the real ``_cmd_run.py`` -> ``run(..., lock=lock)`` call), the
finalize phase must materialize a well-formed (if still comparator-empty)
ledger. This exercises ``_run_finalize_phase`` directly (mirrors
``tests/test_librarian_zero_yield.py``'s ``_finalize_ctx`` pattern) rather
than driving a full LLM-backed ``run()`` — cheaper and does not need a
network-reachable provider.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.librarian import RunContext, _run_finalize_phase
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
