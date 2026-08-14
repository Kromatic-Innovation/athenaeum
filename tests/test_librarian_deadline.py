# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#396 — the librarian run must self-bound with a wall-clock deadline.

Budget caps (`--max-files` / `--max-api-calls`) bound how MUCH a run does but
nothing bounded how LONG it ran: a post-checkpoint phase that stopped making
progress (a hung `claude -p` merge subprocess) ran ~15h holding the run-lock
until externally killed. This suite covers the internal deadline that fixes it:

- `librarian_max_runtime` resolves env > yaml > default, and a non-positive
  value disables the deadline entirely (the explicit unbounded escape hatch).
- The per-file entity loop, on trip, defers the remaining intake, commits the
  partial progress, writes a deadline-labelled deferred manifest, and returns
  `EXIT_GRACEFUL_PARTIAL` (75, issue athenaeum#897) — resumable. Distinct from the
  `EXIT_EXTERNAL_KILL` (124) coreutils `timeout` uses and the athenaeum#337
  interrupt path returns on a delivered signal — this internal check never
  returns 124.
- The merge pass (the phase the incident wedged in) checks the deadline at its
  per-cluster loop and raises `RunDeadlineExceeded`, which `run()` catches to
  commit partial + return `EXIT_GRACEFUL_PARTIAL` (75).
- A disabled deadline (max_runtime <= 0) never trips.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import (
    DEFAULT_MAX_RUNTIME,
    DEFAULT_SESSION_END_OUTER_TIMEOUT,
    DEFAULT_SESSION_END_RUNTIME_MARGIN,
    EXIT_GRACEFUL_PARTIAL,
    librarian_max_runtime,
    run,
    session_end_max_runtime,
    session_end_outer_timeout,
    session_end_runtime_margin,
)
from athenaeum.merge import RunDeadlineExceeded, merge_clusters_to_wiki

# ---------------------------------------------------------------------------
# Fixtures / helpers (parallel to test_librarian_interrupt.py)
# ---------------------------------------------------------------------------


class _FakeClock:
    """A hand-advanced monotonic clock so a test can trip the deadline
    deterministically without sleeping. ``now`` is bumped by the fake
    ``process_one`` (or ``read_cluster_rows``) between the phases we care about.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _seed_knowledge_root(tmp_path: Path, n_files: int = 3) -> Path:
    """Minimal knowledge root: wiki/, raw/sessions/ with *n_files*, git repo.

    Seeded on a non-protected branch so the global protected-branch commit
    hook (main/staging) never interferes; the raw files are written AFTER the
    seed commit so they are uncommitted at run start, exactly like real intake.
    """
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
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


def _writing_process_one_factory(wiki_root: Path, *, bump_clock=None, bump_after=None):
    """A ``process_one`` stand-in that writes one wiki page per file.

    When *bump_clock*/*bump_after* are set, it advances the fake clock right
    after writing the *bump_after*-th page — simulating the wall-clock deadline
    passing mid-run, after the page is on disk but before the next iteration's
    boundary check.
    """
    state = {"n": 0}

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        state["n"] += 1
        page = wiki_root / f"entity-{state['n']}.md"
        page.write_text(f"# Entity {state['n']}\nfrom {raw.ref}\n", encoding="utf-8")
        if bump_clock is not None and state["n"] == bump_after:
            bump_clock()
        return SimpleNamespace(created=[page.name], updated=[], escalated=[], skipped=[])

    return fake_process_one


def _porcelain(root: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _last_subject(root: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveMaxRuntime:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        assert librarian_max_runtime(None) == DEFAULT_MAX_RUNTIME
        assert librarian_max_runtime({}) == DEFAULT_MAX_RUNTIME

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        assert librarian_max_runtime({"librarian": {"max_runtime": 120}}) == 120

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_RUNTIME", "42")
        assert librarian_max_runtime({"librarian": {"max_runtime": 120}}) == 42

    def test_non_positive_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unlike the budget resolvers, <= 0 is a VALID explicit choice (unbounded
        # run), returned verbatim rather than clamped to the default.
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        assert librarian_max_runtime({"librarian": {"max_runtime": 0}}) == 0
        assert librarian_max_runtime({"librarian": {"max_runtime": -1}}) == -1

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `max_runtime: yes` in yaml parses as True (int subclass) — must NOT
        # become a 1-second deadline.
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        assert librarian_max_runtime({"librarian": {"max_runtime": True}}) == (
            DEFAULT_MAX_RUNTIME
        )

    def test_non_numeric_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_RUNTIME", "not-a-number")
        assert librarian_max_runtime(None) == DEFAULT_MAX_RUNTIME


# ---------------------------------------------------------------------------
# Entity-loop deadline trip
# ---------------------------------------------------------------------------


def test_entity_loop_deadline_defers_and_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    # Issue athenaeum#898: this test's fake clock jumps 5000s INSIDE the first
    # file's process_one call (see _writing_process_one_factory's docstring
    # — it simulates time passing between iterations, but from the new
    # per-file wall-clock bound's perspective it looks like file 1 itself
    # took 5000s). Raise the per-file bound out of the way so this
    # RUN-level deadline test exercises only what it names, not the
    # unrelated per-file bound — mirrors the existing
    # ATHENAEUM_MAX_API_CALLS isolation above.
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "999999")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    # Deadline armed at now=0 → run_deadline=1000. The first file processes,
    # then the clock jumps past the deadline, so the SECOND iteration's
    # boundary check trips and defers files 2 & 3.
    def _bump() -> None:
        clock.now = 5000.0

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

    # Resumable graceful-partial exit (issue athenaeum#897) — athenaeum's own
    # internal deadline check, never the external-kill 124.
    assert rc == EXIT_GRACEFUL_PARTIAL
    # Partial progress committed; nothing left uncommitted.
    assert _porcelain(root) == ""
    assert _last_subject(root).startswith("librarian: processed 1 file(s)")
    # Only the first file was processed; the rest are deferred (still on disk).
    assert (root / "wiki" / "entity-1.md").exists()
    assert not (root / "wiki" / "entity-2.md").exists()
    remaining = sorted((root / "raw" / "sessions").glob("2024041*.md"))
    assert len(remaining) == 2, "deferred intake must remain on disk for the next run"
    # Deferred manifest is written and LABELLED as a deadline trip (not budget).
    manifest = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
    assert "wall-clock deadline exceeded" in manifest
    assert "deferred_count: 2" in manifest


def test_disabled_deadline_never_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    # Clock jumps far past any deadline after the first file — but with
    # max_runtime=0 the deadline is disabled (run_deadline is None), so the run
    # completes normally and processes every file.
    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

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
        max_runtime=0,  # disabled — unbounded run
    )

    assert rc == 0
    for i in (1, 2, 3):
        assert (root / "wiki" / f"entity-{i}.md").exists()
    assert not (root / "wiki" / "_deferred_work.md").exists()
    assert _porcelain(root) == ""


# ---------------------------------------------------------------------------
# Merge-pass (post-compile) deadline — the phase the incident wedged in
# ---------------------------------------------------------------------------


def test_merge_pass_raises_on_past_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)

    # One cluster row is enough: the per-cluster deadline guard sits at the TOP
    # of the loop, so a deadline already in the past raises before any merge.
    monkeypatch.setattr(
        "athenaeum.merge.read_cluster_rows", lambda *_a, **_k: [{"cluster_id": "c1"}]
    )

    with pytest.raises(RunDeadlineExceeded) as excinfo:
        merge_clusters_to_wiki(
            root,
            auto_memory_files=[],
            dry_run=True,
            deadline=0.0,  # monotonic 0 is always in the past → immediate trip
        )
    assert excinfo.value.phase == "C3 cluster merge"


def test_wiki_dedup_phase_boundary_deadline_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The athenaeum#290 wiki-dedup pass is a listed wedge site. It swallows its own
    exceptions, so the deadline "covers" it via a between-phase check right
    after it — a long wiki-dedup stops the run before the heavier phases."""
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    # Deadline armed at now=0 → run_deadline=1000. Simulate a wiki-dedup pass
    # that ran long by jumping the clock past the deadline inside it; the
    # boundary check right after it then trips.
    def _slow_dedup(*_a, **_k) -> None:
        clock.now = 5000.0

    monkeypatch.setattr("athenaeum.wiki_dedupe.propose_wiki_page_merges", _slow_dedup)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert _porcelain(root) == ""
    assert "athenaeum#290 wiki-dedup" in _last_subject(root)


def test_run_catches_merge_deadline_and_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() wraps the post-compile phase: a RunDeadlineExceeded from the merge
    pass is caught, partial progress is committed, and the run exits
    EXIT_GRACEFUL_PARTIAL (75)."""
    # Issue athenaeum#461: the entity phase now runs BEFORE the auto-memory block, so
    # an empty entity intake (n_files=0) isolates this test's actual target
    # (the auto-memory/merge deadline-catch) from the entity loop — a
    # nonempty intake here would have the entity loop's `process_one` make a
    # real (fake-keyed) API call before the auto-memory block is ever
    # reached.
    root = _seed_knowledge_root(tmp_path, n_files=0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    # Force the auto-memory compile branch to run, then have it trip the
    # deadline exactly as the real merge loop would — but first write a file
    # to disk (mirroring a real C3 merge partially writing wiki/auto-*.md
    # pages before the deadline trips), so there is genuine partial progress
    # for `_stop_on_deadline`'s `git_snapshot` to commit. Without this the
    # entity phase (now a no-op on n_files=0) leaves the tree clean and
    # `git_snapshot` no-ops, making "committed" unobservable.
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
    )

    def _boom(*_a, **_k):
        (root / "wiki" / "auto-partial.md").write_text(
            "---\nname: partial\n---\npartial C3 output\n", encoding="utf-8"
        )
        raise RunDeadlineExceeded("C4 contradiction detector / resolver")

    monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _boom)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=3600,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert _porcelain(root) == ""
    subject = _last_subject(root)
    assert subject.startswith("librarian: partial run (deadline 3600s exceeded during")
    assert "C4 contradiction detector / resolver" in subject


# ---------------------------------------------------------------------------
# Issue athenaeum#461 — entity phase moved ahead of the auto-memory block
# ---------------------------------------------------------------------------


def test_461_entity_runs_first_then_automemory_deadline_trips_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: entity intake is compiled FIRST; a deadline that only trips
    during the (later) auto-memory phase still exits EXIT_GRACEFUL_PARTIAL
    (75) with the auto-memory phase name — proving the entity phase got to
    run before the shared deadline was spent, which is the whole point of
    the athenaeum#461 reorder.
    """
    root = _seed_knowledge_root(tmp_path, n_files=2)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    # Real (unmocked) entity loop via the writing stand-in — no clock bump
    # here, so both files process cleanly and quickly, well within budget.
    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki"),
    )

    # Auto-memory discovery finds one scope; the compile itself is a slow
    # stand-in that trips the (already-armed) deadline exactly like the real
    # merge loop would once entity has already consumed some wall-clock time.
    fake_am = SimpleNamespace(origin_scope="scope-a")
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [fake_am],
    )

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    def _slow_compile(*_a, **_k):
        # Simulate the auto-memory compile running long enough to blow the
        # deadline — mirrors the real merge loop's per-cluster deadline
        # check raising RunDeadlineExceeded, after partially writing a wiki
        # page (like a real C3 merge would before it trips). Without this
        # write there is nothing left uncommitted for `_stop_on_deadline`'s
        # `git_snapshot` to catch, since the entity phase above already
        # committed its own work cleanly.
        (root / "wiki" / "auto-partial.md").write_text(
            "---\nname: partial\n---\npartial C3 output\n", encoding="utf-8"
        )
        clock.now = 5000.0
        raise RunDeadlineExceeded("C4 contradiction detector / resolver")

    monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _slow_compile)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    # Entity intake was consumed FIRST — both files compiled before the
    # auto-memory phase (and the deadline trip) ever ran.
    assert (root / "wiki" / "entity-1.md").exists()
    assert (root / "wiki" / "entity-2.md").exists()
    remaining = sorted((root / "raw" / "sessions").glob("2024041*.md"))
    assert remaining == [], "entity intake must be fully consumed, not deferred"

    # The trip happened in the auto-memory phase, after entity succeeded.
    assert rc == EXIT_GRACEFUL_PARTIAL
    subject = _last_subject(root)
    assert subject.startswith("librarian: partial run (deadline 1000s exceeded during")
    assert "C4 contradiction detector / resolver" in subject


def test_461_entity_deadline_trip_skips_automemory_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a deadline trip DURING the entity phase skips the auto-memory
    block entirely (gated on ``not deadline_tripped``) and exits
    EXIT_GRACEFUL_PARTIAL (75) — proving `_compile_auto_memory` is never
    invoked once the entity loop has already spent the shared deadline.
    """
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    # First entity file processes, then the clock jumps past the deadline —
    # the second iteration's boundary check trips deadline_tripped=True.
    def _bump() -> None:
        clock.now = 5000.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    # Spy on _compile_auto_memory — it must NEVER be called once the entity
    # loop has tripped the deadline.
    compile_calls: list[object] = []

    def _spy_compile(*args, **kwargs):
        compile_calls.append((args, kwargs))
        return []

    monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _spy_compile)
    # Auto-memory discovery would find a scope if reached — proving the
    # skip is about the auto-memory BLOCK (gated on deadline_tripped), not
    # merely an empty discovery result.
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert compile_calls == [], "auto-memory compile must be skipped after an entity deadline trip"
    # Only the first file was processed; the deadline trip deferred the rest.
    assert (root / "wiki" / "entity-1.md").exists()
    assert not (root / "wiki" / "entity-2.md").exists()


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def test_cli_max_runtime_threads_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.cli import main

    captured: dict[str, object] = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

    # --dry-run takes the no-lock path; both run() call sites forward
    # max_runtime=args.max_runtime identically.
    rc = main(["run", "--dry-run", "--max-runtime", "77", "--path", str(tmp_path)])
    assert rc == 0
    assert captured["max_runtime"] == 77


def test_cli_max_runtime_defaults_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.cli import main

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "athenaeum.librarian.run", lambda **kwargs: captured.update(kwargs) or 0
    )

    # Unset → None, so run() resolves env > yaml > default itself.
    rc = main(["run", "--dry-run", "--path", str(tmp_path)])
    assert rc == 0
    assert captured["max_runtime"] is None


# ---------------------------------------------------------------------------
# Issue athenaeum#761 — the phase-boundary / C4 deadline exit must push too
#
# `stop_on_deadline` returned EXIT_GRACEFUL_PARTIAL (75, formerly 124 before
# athenaeum#897) to run()'s caller BEFORE _run_finalize_phase, so the post-run
# push (librarian.push_after_run) never fired on the phase-boundary / C4 path
# — 26 commits stranded on one machine over three days. The fix pushes from
# inside stop_on_deadline, right after the partial commit.
# ---------------------------------------------------------------------------


def _spy_git_push(
    monkeypatch: pytest.MonkeyPatch, *, succeed: bool = True
) -> list[dict[str, object]]:
    """Spy on ``librarian.git_push`` and return the recorded calls."""
    from athenaeum import librarian

    calls: list[dict[str, object]] = []

    def fake_push(knowledge_root, remote="origin", branch=None):
        calls.append({"knowledge_root": knowledge_root, "remote": remote})
        return succeed

    monkeypatch.setattr(librarian, "git_push", fake_push)
    return calls


def _trip_c4_deadline(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the auto-memory/merge (C4) phase to write partial progress and then
    raise ``RunDeadlineExceeded`` — the exact phase-boundary path
    ``test_run_catches_merge_deadline_and_exits_75`` drives, factored out."""
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
    )

    def _boom(*_a, **_k):
        (root / "wiki" / "auto-partial.md").write_text(
            "---\nname: partial\n---\npartial C3 output\n", encoding="utf-8"
        )
        raise RunDeadlineExceeded("C4 contradiction detector / resolver")

    monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _boom)


def test_761_phase_boundary_deadline_pushes_after_partial_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C4 / phase-boundary deadline exit pushes when push_after_run is on
    and the run committed. FAILS against pre-athenaeum#761 (no push on this path)."""
    root = _seed_knowledge_root(tmp_path, n_files=0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    calls = _spy_git_push(monkeypatch)
    _trip_c4_deadline(root, monkeypatch)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=3600,
        push_after_run=True,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert _porcelain(root) == ""
    # The partial-progress commit moved HEAD, so the push fires — exactly once.
    assert len(calls) == 1, "phase-boundary deadline path must push the partial commit"
    assert calls[0]["knowledge_root"] == root


def test_761_entity_loop_deadline_pushes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entity-loop deadline path falls through to _run_finalize_phase, which
    already pushes. Adding the stop_on_deadline push must NOT double it: the
    entity trip sets deadline_tripped and skips the auto-memory block (and its
    stop_on_deadline call sites), so the push still fires exactly once."""
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    calls = _spy_git_push(monkeypatch)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    def _bump() -> None:
        clock.now = 5000.0

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
        push_after_run=True,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert len(calls) == 1, "entity-loop deadline path must push exactly once, not twice"


def test_761_dry_run_deadline_never_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must never push on the phase-boundary path (the push call is
    nested under `if not self.dry_run` and _maybe_push_after_run guards dry_run
    a second time)."""
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    calls = _spy_git_push(monkeypatch)

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)
    monkeypatch.setattr(
        "athenaeum.wiki_dedupe.propose_wiki_page_merges",
        lambda *_a, **_k: setattr(clock, "now", 5000.0),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=1000,
        dry_run=True,
        push_after_run=True,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert calls == [], "--dry-run must never push, even on the deadline path"


def test_761_deadline_push_failure_keeps_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push failure on the deadline path is non-fatal — the
    EXIT_GRACEFUL_PARTIAL (75) exit code is unchanged (mirrors the
    finalize-phase push contract)."""
    root = _seed_knowledge_root(tmp_path, n_files=0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    calls = _spy_git_push(monkeypatch, succeed=False)
    _trip_c4_deadline(root, monkeypatch)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=3600,
        push_after_run=True,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL, (
        "a failed push must not change the EXIT_GRACEFUL_PARTIAL deadline exit code"
    )
    assert len(calls) == 1


def test_761_skipped_push_emits_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue athenaeum#761 acceptance criterion: an opted-in push that is SKIPPED
    (here: no new commits) is no longer silent — it emits a log line so an
    operator can tell a skipped push from one that happened or failed."""
    import logging

    from athenaeum.librarian import _capture_head, _maybe_push_after_run

    root = _seed_knowledge_root(tmp_path, n_files=0)
    head = _capture_head(root)
    with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
        _maybe_push_after_run(
            root,
            config=None,
            push_after_run=True,
            dry_run=False,
            head_at_start=head,  # HEAD unchanged → skip, but must log why
        )
    assert any(
        "post-run push skipped: no new commits" in r.message for r in caplog.records
    ), "a skipped opted-in push must emit a log line"


# ---------------------------------------------------------------------------
# Issue athenaeum#896 — SessionEnd's inner budget derived from the outer kill timer
# ---------------------------------------------------------------------------


class TestSessionEndOuterTimeout:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KNOWLEDGE_REBUILD_TIMEOUT", raising=False)
        assert session_end_outer_timeout(None) == DEFAULT_SESSION_END_OUTER_TIMEOUT

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The SAME env var the SessionEnd wrapper script
        # (code-workspace-config/scripts/hooks/knowledge-rebuild-index.sh,
        # not in this repo) reads for its own `timeout` wrap — this is the
        # "single definition both the wrapper and the derivation read".
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "600")
        assert session_end_outer_timeout(None) == 600

    def test_non_numeric_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "not-a-number")
        assert session_end_outer_timeout(None) == DEFAULT_SESSION_END_OUTER_TIMEOUT


class TestSessionEndRuntimeMargin:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        assert session_end_runtime_margin(None) == DEFAULT_SESSION_END_RUNTIME_MARGIN
        assert session_end_runtime_margin({}) == DEFAULT_SESSION_END_RUNTIME_MARGIN

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        assert (
            session_end_runtime_margin({"librarian": {"session_end_runtime_margin": 30}})
            == 30
        )

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", "45")
        assert (
            session_end_runtime_margin({"librarian": {"session_end_runtime_margin": 30}})
            == 45
        )

    def test_negative_env_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", "-5")
        assert session_end_runtime_margin(None) == DEFAULT_SESSION_END_RUNTIME_MARGIN

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        assert (
            session_end_runtime_margin({"librarian": {"session_end_runtime_margin": True}})
            == DEFAULT_SESSION_END_RUNTIME_MARGIN
        )


class TestSessionEndMaxRuntimeInvariant:
    """AC: the derived inner runtime is ALWAYS strictly less than the
    configured outer timeout, for a range of configured outer values —
    including small ones, per the athenaeum#896 clamp requirement."""

    @pytest.mark.parametrize(
        "outer",
        [2, 3, 5, 10, 30, 60, 119, 121, 300, 900, 1800, 3600, 86400, 1_000_000],
    )
    def test_inner_always_strictly_less_than_outer(
        self, outer: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", str(outer))
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        inner = session_end_max_runtime(None)
        assert inner > 0, f"outer={outer}: derived inner must be strictly positive"
        assert inner < outer, f"outer={outer}: derived inner must be < outer"

    @pytest.mark.parametrize("margin", [0, 1, 60, 120, 500, 10_000])
    def test_inner_always_strictly_less_than_outer_across_margins(
        self, margin: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invariant must hold for a range of MARGIN values too — including
        a margin that would (before clamping) exceed the outer timeout."""
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "900")
        monkeypatch.setenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", str(margin))
        inner = session_end_max_runtime(None)
        assert 0 < inner < 900

    def test_default_outer_and_margin_yields_expected_inner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KNOWLEDGE_REBUILD_TIMEOUT", raising=False)
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        assert session_end_max_runtime(None) == (
            DEFAULT_SESSION_END_OUTER_TIMEOUT - DEFAULT_SESSION_END_RUNTIME_MARGIN
        )

    def test_much_smaller_than_nightly_default_max_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug this issue fixes: before athenaeum#896, the inner deadline fell
        through to DEFAULT_MAX_RUNTIME (3600s) — 4x the 900s outer default —
        so the graceful-stop path could never win the race. The derived
        value must be comfortably under the outer default, not just under
        DEFAULT_MAX_RUNTIME (which 900 alone already satisfies)."""
        monkeypatch.delenv("KNOWLEDGE_REBUILD_TIMEOUT", raising=False)
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        inner = session_end_max_runtime(None)
        assert inner < DEFAULT_MAX_RUNTIME
        assert inner < DEFAULT_SESSION_END_OUTER_TIMEOUT

    def test_outer_disabled_falls_back_to_nightly_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved outer <= 0 means the wrapper's own `timeout` is disabled
        (coreutils `timeout 0` never kills) — no external race to protect
        against, so this is exempt from the strict-invariant range above and
        falls back to DEFAULT_MAX_RUNTIME rather than deriving from a
        non-positive outer."""
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "0")
        assert session_end_max_runtime(None) == DEFAULT_MAX_RUNTIME
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "-30")
        assert session_end_max_runtime(None) == DEFAULT_MAX_RUNTIME

    def test_never_negative_for_any_outer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clamp sensibly rather than going negative — for EVERY outer value,
        including pathologically tiny ones, the result is never negative."""
        for outer in (-100, -1, 0, 1, 2, 3):
            monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", str(outer))
            assert session_end_max_runtime(None) >= 0


# ---------------------------------------------------------------------------
# Issue athenaeum#896 — derived deadline trips the SAME graceful-stop path
# ---------------------------------------------------------------------------


def test_derived_max_runtime_trips_graceful_stop_and_exits_75(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value `session_end_max_runtime()` derives is a real, usable
    `max_runtime` — fed straight into `run()`, an entity-loop deadline trip
    with it behaves EXACTLY like the existing
    `test_entity_loop_deadline_defers_and_exits_75` case above: graceful
    stop, partial progress committed, deferred intake left on disk,
    EXIT_GRACEFUL_PARTIAL (75) exit. (The SessionEnd-composition-level
    equivalent of this test — via `session_end()` rather than `run()`
    directly — lives in
    `test_session_end.py::TestSessionEndDerivedDeadlineGracefulStop`.)"""
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "1000")
    monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
    # Issue athenaeum#898: isolate this run-level-deadline test from the new
    # per-file wall-clock bound — see the identical note on
    # test_entity_loop_deadline_defers_and_exits_75 above.
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "999999")

    derived = session_end_max_runtime(None)
    assert derived < 1000

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

    def _bump() -> None:
        clock.now = derived + 5000.0

    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=derived,
    )

    assert rc == EXIT_GRACEFUL_PARTIAL
    assert _porcelain(root) == ""
    assert _last_subject(root).startswith("librarian: processed 1 file(s)")
    assert (root / "wiki" / "entity-1.md").exists()
    assert not (root / "wiki" / "entity-2.md").exists()
    remaining = sorted((root / "raw" / "sessions").glob("2024041*.md"))
    assert len(remaining) == 2, "deferred intake must remain on disk for the next run"
