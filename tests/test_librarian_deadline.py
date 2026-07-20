# SPDX-License-Identifier: Apache-2.0
"""Issue #396 — the librarian run must self-bound with a wall-clock deadline.

Budget caps (`--max-files` / `--max-api-calls`) bound how MUCH a run does but
nothing bounded how LONG it ran: a post-checkpoint phase that stopped making
progress (a hung `claude -p` merge subprocess) ran ~15h holding the run-lock
until externally killed. This suite covers the internal deadline that fixes it:

- `librarian_max_runtime` resolves env > yaml > default, and a non-positive
  value disables the deadline entirely (the explicit unbounded escape hatch).
- The per-file entity loop, on trip, defers the remaining intake, commits the
  partial progress, writes a deadline-labelled deferred manifest, and returns
  124 (matching coreutils `timeout` and the #337 interrupt path) — resumable.
- The merge pass (the phase the incident wedged in) checks the deadline at its
  per-cluster loop and raises `RunDeadlineExceeded`, which `run()` catches to
  commit partial + return 124.
- A disabled deadline (max_runtime <= 0) never trips.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import DEFAULT_MAX_RUNTIME, librarian_max_runtime, run
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


def test_entity_loop_deadline_defers_and_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

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

    # Resumable non-zero exit (coreutils `timeout` convention).
    assert rc == 124
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


def test_wiki_dedup_phase_boundary_deadline_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #290 wiki-dedup pass is a listed wedge site. It swallows its own
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

    assert rc == 124
    assert _porcelain(root) == ""
    assert "#290 wiki-dedup" in _last_subject(root)


def test_run_catches_merge_deadline_and_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() wraps the post-compile phase: a RunDeadlineExceeded from the merge
    pass is caught, partial progress is committed, and the run exits 124."""
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    # Force the auto-memory compile branch to run, then have it trip the
    # deadline exactly as the real merge loop would.
    monkeypatch.setattr(
        "athenaeum.librarian.discover_auto_memory_files",
        lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
    )

    def _boom(*_a, **_k):
        raise RunDeadlineExceeded("C4 contradiction detector / resolver")

    monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _boom)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=3600,
    )

    assert rc == 124
    assert _porcelain(root) == ""
    subject = _last_subject(root)
    assert subject.startswith("librarian: partial run (deadline 3600s exceeded during")
    assert "C4 contradiction detector / resolver" in subject


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
