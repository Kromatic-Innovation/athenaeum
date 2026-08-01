# SPDX-License-Identifier: Apache-2.0
"""Issue #440 — the entity phase must not consume the whole run window.

`run_deadline` (#396) is a single budget shared by every phase, and the entity
loop only stops when that WHOLE budget is gone. Measured on the live corpus:
the entity phase took 3690s of a 3944s window (93.6%) on 3 files, and the C4
contradiction detector — which runs after it (#461 reorder) — got 0 seconds on
every one of 10+ consecutive nights. Contradictions were therefore never
detected at all, not merely detected slowly.

This suite covers the reserve that fixes it: the entity phase stops CLAIMING
new files once its share of `max_runtime` is spent, defers the remainder
(resumable, exactly like the #220 budget trip), and — the load-bearing part —
lets the run continue into the auto-memory / C4 block instead of exiting 124.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import (
    DEFAULT_ENTITY_RUNTIME_SHARE,
    librarian_entity_runtime_share,
    run,
)

# Reuse the deadline suite's fixtures verbatim — this reserve is a sibling of
# the #396 deadline and must be exercised against the same run harness.
from tests.test_librarian_deadline import (
    _FakeClock,
    _last_subject,
    _porcelain,
    _seed_knowledge_root,
    _writing_process_one_factory,
)

# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveEntityRuntimeShare:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)
        assert librarian_entity_runtime_share(None) == DEFAULT_ENTITY_RUNTIME_SHARE
        assert librarian_entity_runtime_share({}) == DEFAULT_ENTITY_RUNTIME_SHARE

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)
        cfg = {"librarian": {"entity_runtime_share": 0.25}}
        assert librarian_entity_runtime_share(cfg) == 0.25

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", "0.8")
        cfg = {"librarian": {"entity_runtime_share": 0.25}}
        assert librarian_entity_runtime_share(cfg) == 0.8

    @pytest.mark.parametrize("value", [0, 1, 1.5, -0.2])
    def test_out_of_range_disables_reserve(
        self, value: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outside 0 < share < 1 the reserve is off (entity may use the whole
        # window) — the explicit opt-out restoring pre-#440 behaviour.
        monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)
        cfg = {"librarian": {"entity_runtime_share": value}}
        assert librarian_entity_runtime_share(cfg) == 0.0

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `entity_runtime_share: yes` parses as True (an int subclass) and must
        # NOT become a 100% share.
        monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)
        cfg = {"librarian": {"entity_runtime_share": True}}
        assert librarian_entity_runtime_share(cfg) == DEFAULT_ENTITY_RUNTIME_SHARE

    def test_non_numeric_env_falls_through_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", "not-a-number")
        assert librarian_entity_runtime_share(None) == DEFAULT_ENTITY_RUNTIME_SHARE


# ---------------------------------------------------------------------------
# The reserve — entity yields, downstream phases still run
# ---------------------------------------------------------------------------


def _auto_memory_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Make the auto-memory block reachable and record whether it ran.

    Returns the call-record list; a non-empty list means the C2-C4 block that
    #440 exists to un-starve actually executed.
    """
    calls: list[object] = []
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
    )
    monkeypatch.setattr(
        "athenaeum.librarian._compile_auto_memory",
        lambda *a, **k: calls.append((a, k)) or [],
    )
    return calls


def test_entity_share_defers_intake_but_run_continues_into_automemory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this issue is about.

    Entity spends its share, stops claiming files — and the auto-memory / C4
    block STILL RUNS. Contrast `test_461_entity_deadline_trip_skips_automemory
    _block` in the deadline suite: a run-deadline trip skips that block and
    exits 124, which is precisely the starvation the reserve prevents.
    """
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    compile_calls = _auto_memory_spy(monkeypatch)

    # max_runtime=1000, default share 0.6 → entity_deadline=600, run_deadline=1000.
    # After file 1 the clock jumps to 700: past the ENTITY share but well inside
    # the run deadline, so iteration 2 yields rather than tripping the run.
    def _bump() -> None:
        clock.now = 700.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    # NOT 124: the run is healthy and finished its downstream phases.
    assert rc == 0
    assert compile_calls, (
        "the auto-memory / C4 block must run after an entity-share yield — "
        "un-starving it is the entire point of issue #440"
    )

    # Entity intake was bounded: one file compiled, the other two deferred and
    # still on disk for the next run.
    assert (root / "wiki" / "entity-1.md").exists()
    assert not (root / "wiki" / "entity-2.md").exists()
    remaining = sorted((root / "raw" / "sessions").glob("2024041*.md"))
    assert len(remaining) == 2, "deferred intake must remain on disk for the next run"

    # Manifest labels the yield distinctly from a budget or deadline trip.
    manifest = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
    assert "yielded to the C4 detector" in manifest
    assert "deferred_count: 2" in manifest
    assert "wall-clock deadline exceeded" not in manifest

    assert _porcelain(root) == ""


def test_run_deadline_trip_still_wins_over_entity_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that blew the REAL deadline reports that, not the softer yield.

    The share check sits after the run-deadline check, so when the clock is
    past both, the more severe condition is the one recorded — exit 124 and a
    deadline-labelled manifest, exactly as before #440.
    """
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    def _bump() -> None:
        clock.now = 5000.0  # past entity_deadline=600 AND run_deadline=1000

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    assert rc == 124
    manifest = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
    assert "wall-clock deadline exceeded" in manifest
    assert "yielded to the C4 detector" not in manifest


def test_share_disabled_lets_entity_use_the_whole_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-out restores pre-#440 behaviour byte-for-byte.

    With the share disabled the entity phase is bounded only by the run
    deadline, so a clock past the old entity share compiles every file.
    """
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", "0")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    _auto_memory_spy(monkeypatch)

    # 700 is past the share the default WOULD have imposed (600) but inside the
    # 1000s run deadline — with the reserve off, nothing stops the loop.
    def _bump() -> None:
        clock.now = 700.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    assert rc == 0
    for i in (1, 2, 3):
        assert (root / "wiki" / f"entity-{i}.md").exists()
    assert not (root / "wiki" / "_deferred_work.md").exists()
    assert _last_subject(root).startswith("librarian: processed 3 file(s)")


def test_disabled_run_deadline_disables_the_share_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share of a disabled window is meaningless — an unbounded run stays
    unbounded (`max_runtime <= 0` is the documented #396 escape hatch)."""
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    _auto_memory_spy(monkeypatch)

    def _bump() -> None:
        clock.now = 10_000_000.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=0,  # deadline disabled
    )

    assert rc == 0
    for i in (1, 2, 3):
        assert (root / "wiki" / f"entity-{i}.md").exists()
    assert not (root / "wiki" / "_deferred_work.md").exists()
