# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1102 — the intake-runtime-floor reserve, end-to-end.

Companion to ``tests/test_librarian_entity_share.py`` (issue athenaeum#440): that
suite covers the entity phase's OWN cap (``entity_runtime_share``, "entity may
spend AT MOST this much of the window"). This suite covers the NEW, separate
guarantee this issue adds — ``librarian.intake_runtime_floor``, "the intake
path that feeds C4 is reserved AT LEAST this much of the window" — and its
combination with the athenaeum#440 share via
:func:`athenaeum.librarian._arm_run_deadline`'s ``min()`` of the two candidate
entity deadlines.

Reuses the athenaeum#440/athenaeum#396 deadline suite's fixtures verbatim, exactly like
``test_librarian_entity_share.py`` does — this reserve is a sibling of both
and must be exercised against the same run harness.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import run
from tests.test_librarian_deadline import (
    _FakeClock,
    _seed_knowledge_root,
    _writing_process_one_factory,
)


def _auto_memory_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Mirrors ``test_librarian_entity_share.py``'s helper of the same name."""
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


def _common_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)
    # Isolate from the per-file wall-clock bound (issue athenaeum#898), exactly like
    # the athenaeum#440 entity-share suite does for the same reason.
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "999999")


class TestIntakeFloorDefaultOff:
    def test_floor_unset_is_identical_to_pre_1102_behaviour(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: with the key unset, phase scheduling is identical to current
        behaviour — the exact scenario
        ``test_entity_share_defers_intake_but_run_continues_into_automemory``
        (athenaeum#440) exercises, re-run here with the floor explicitly absent to
        pin that athenaeum#1102 changed nothing when unarmed.
        """
        root = _seed_knowledge_root(tmp_path, n_files=3)
        _common_env(monkeypatch)
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
        compile_calls = _auto_memory_spy(monkeypatch)

        # max_runtime=1000, default entity share 0.6 -> entity_deadline=600.
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
        assert compile_calls, "auto-memory/C4 must still run — unchanged from athenaeum#440"
        assert (root / "wiki" / "entity-1.md").exists()
        assert not (root / "wiki" / "entity-2.md").exists()
        manifest = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "yielded to the C4 detector" in manifest
        assert "deferred_count: 2" in manifest


class TestIntakeFloorForcesEarlierYield:
    """AC5: with the floor set, the entity phase yields once it has consumed
    its permitted share, and the intake path then receives at least the
    reserved share. Both tests bump the clock to the SAME instant (500,
    exactly the floor-derived deadline for a 1000s window) after file 1 —
    the control shows the athenaeum#440 share ALONE would not yet stop there
    (600 > 500); the treatment shows the athenaeum#1102 floor does.
    """

    def test_control_share_alone_does_not_yield_at_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        _common_env(monkeypatch)
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
        _auto_memory_spy(monkeypatch)

        def _bump() -> None:
            clock.now = 500.0

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
        # Default share (0.6) -> entity_deadline=600; the clock never reaches
        # it (only one bump, to 500), so all three files compile.
        for i in (1, 2, 3):
            assert (root / "wiki" / f"entity-{i}.md").exists()
        assert not (root / "wiki" / "_deferred_work.md").exists()

    def test_floor_yields_entity_at_500_leaving_the_reserve_for_intake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        _common_env(monkeypatch)
        # Reserve 50% of the 1000s window for intake -> entity_deadline_from_floor
        # = 1000 - 0.5*1000 = 500, TIGHTER than the default share's 600.
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "0.5")

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
        compile_calls = _auto_memory_spy(monkeypatch)

        def _bump() -> None:
            clock.now = 500.0

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

        # Healthy, resumable yield — not EXIT_GRACEFUL_PARTIAL.
        assert rc == 0
        # Entity claimed exactly one file before yielding at its (floor-
        # tightened) deadline; the other two are deferred, resumable.
        assert (root / "wiki" / "entity-1.md").exists()
        assert not (root / "wiki" / "entity-2.md").exists()
        assert not (root / "wiki" / "entity-3.md").exists()
        # The intake path (auto-memory/C4) still got its turn — the entire
        # point of both the athenaeum#440 share and this athenaeum#1102 floor.
        assert compile_calls, (
            "the intake path must still run when the floor forces an "
            "earlier entity yield — un-starving it is the point of athenaeum#1102"
        )
        manifest = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "yielded to the C4 detector" in manifest
        assert "deferred_count: 2" in manifest


class TestOversizedFloorRefused:
    """AC7: a floor >= 1.0 (reserving the whole window or more) is REFUSED,
    not clamped — it must never be able to starve the ENTITY phase in the
    opposite direction. Reuses the exact clock=500 boundary from the control
    above: if the oversized floor had ANY effect, entity would yield early
    (mirroring the treatment test); this asserts it does not.
    """

    def test_floor_of_1_0_is_refused_entity_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        _common_env(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "1.0")

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
        _auto_memory_spy(monkeypatch)

        def _bump() -> None:
            clock.now = 500.0

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
        # A refused (disabled) floor leaves the athenaeum#440 share (600) as the
        # only bound; the clock never reaches it, so all three files compile
        # -- entity is NOT starved by the malformed reservation.
        for i in (1, 2, 3):
            assert (root / "wiki" / f"entity-{i}.md").exists()
        assert not (root / "wiki" / "_deferred_work.md").exists()

    def test_floor_of_1_5_is_refused_entity_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        _common_env(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "1.5")

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
        _auto_memory_spy(monkeypatch)

        def _bump() -> None:
            clock.now = 500.0

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
