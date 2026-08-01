# SPDX-License-Identifier: Apache-2.0
"""Issue #669 — the entity-share yield (#440) must be machine-detectable run state.

PR #661/#440 let the entity phase YIELD its window share instead of consuming
the whole run. That is deliberate and correct, but it silently disarmed the
duration-based cap detector (cron-fleet#94): the run now ends well under the cap
threshold, so a #440-shaped stall is no longer visible by duration. This suite
pins the additive observability that fixes the blind spot — the yield is emitted
in `out_run_stats` (and the run-summary line) so a consumer can distinguish
"entity yielded on purpose" from "API budget exhausted" WITHOUT parsing log text
or the deferred manifest header — while asserting the yield BEHAVIOR is unchanged.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from athenaeum.librarian import run
from tests.test_librarian_deadline import (
    _FakeClock,
    _seed_knowledge_root,
    _writing_process_one_factory,
)
from tests.test_librarian_entity_share import _auto_memory_spy


def test_entity_budget_tripped_emitted_after_a_share_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """After a share yield: the flag + claimed/deferred counts are emitted.

    Fails against the pre-#669 code: `entity_budget_tripped` never reached
    `out_run_stats` at all.
    """
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    _auto_memory_spy(monkeypatch)

    # max_runtime=1000, default share 0.6 -> entity_deadline=600. After file 1 the
    # clock jumps to 700 (past the share, inside the run deadline) so files 2-3 yield.
    def _bump() -> None:
        clock.now = 700.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    stats: dict = {}
    caplog.clear()
    caplog.set_level(logging.INFO, logger="athenaeum.librarian")
    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
        out_run_stats=stats,
    )

    # Behavior unchanged (#440): healthy run, one file compiled, two deferred.
    assert rc == 0

    # The machine-detectable contract (#669).
    assert stats["entity_budget_tripped"] is True
    assert stats["entity_files_claimed"] == 1
    assert stats["entity_files_deferred"] == 2

    # And the run-summary line carries it too, alongside the existing flags.
    assert "entity_budget_tripped=true" in caplog.text.lower()


def test_entity_budget_tripped_is_false_after_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean run (no yield): the flag is present and False; the summary is quiet."""
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    _auto_memory_spy(monkeypatch)

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki"),
    )

    stats: dict = {}
    caplog.clear()
    caplog.set_level(logging.INFO, logger="athenaeum.librarian")
    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=0,  # deadline + share disabled — nothing yields
        out_run_stats=stats,
    )

    assert rc == 0
    # Present and correct: False, with all three files claimed and none deferred.
    assert stats["entity_budget_tripped"] is False
    assert stats["entity_files_claimed"] == 3
    assert stats["entity_files_deferred"] == 0
    # The summary line does not render the yield token on a clean run. Match the
    # `key=` form so a tmp path that happens to embed this test's name (which
    # contains "entity_budget_tripped") is not a false positive.
    assert "entity_budget_tripped=" not in caplog.text
