# SPDX-License-Identifier: Apache-2.0
"""Issue #337 — a timeout-killed librarian run must not strand its output.

A wall-clock timeout (the pre-dawn sweep's ``timeout``, which SIGTERMs then,
after a grace, KILLs) can land between the start-of-run ``pre-processing
snapshot`` commit and the terminal ``processed N file(s)`` commit. Without a
handler, every wiki page written so far is left uncommitted for the NEXT
run's ``git add -A`` snapshot to absorb under a misleading message.

Covers each acceptance criterion:

- Interrupt mid-run (real SIGTERM after ≥1 file) → the processed work is
  committed with a distinct ``librarian: partial run (…)`` message, the
  working tree is clean, and the process exits 124 (matching coreutils
  ``timeout``).
- Normal completion is unchanged: still exactly one ``processed N file(s)``
  commit, no ``partial run`` commit, clean tree, exit 0.
- Opt-in only: the default run (``install_signal_handlers=False``) must not
  touch the process-wide SIGTERM handler — in-process callers (the MCP
  server, tests) keep their own signal handling.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import run

# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained; parallel to test_budget_deferred.py)
# ---------------------------------------------------------------------------


def _seed_knowledge_root(tmp_path: Path, n_files: int = 3) -> Path:
    """Minimal knowledge root: wiki/, raw/sessions/ with *n_files*, git repo.

    The raw files are written AFTER the seed commit, so they are uncommitted
    at run start — exactly like real intake — and the run's pre-processing
    snapshot commits them before the entity loop begins.
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


def _writing_process_one_factory(wiki_root: Path, *, interrupt_on: int | None = None):
    """A ``process_one`` stand-in that writes one wiki page per file.

    When *interrupt_on* is set, it sends SIGTERM to its own process right
    after writing that Nth page — simulating a wall-clock timeout arriving
    mid-run, after the page is on disk but before the run commits.
    """
    state = {"n": 0}

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        state["n"] += 1
        page = wiki_root / f"entity-{state['n']}.md"
        page.write_text(f"# Entity {state['n']}\nfrom {raw.ref}\n", encoding="utf-8")
        if interrupt_on is not None and state["n"] == interrupt_on:
            os.kill(os.getpid(), signal.SIGTERM)
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


def _log_subjects(root: Path) -> str:
    return subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_interrupt_commits_partial_progress_and_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", interrupt_on=2),
    )

    # Safety net: if run() ever regresses and fails to install its own
    # SIGTERM handler, this sentinel turns the self-sent SIGTERM into a
    # clean AssertionError instead of killing the whole pytest process
    # (a bare SIGTERM has default disposition = terminate).
    def _sentinel(signum: int, frame: object) -> None:
        raise AssertionError(
            "run() did not install a SIGTERM handler (issue #337 regression)"
        )

    prev = signal.signal(signal.SIGTERM, _sentinel)
    try:
        with pytest.raises(SystemExit) as excinfo:
            run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_api_calls=100,
                install_signal_handlers=True,
            )
    finally:
        signal.signal(signal.SIGTERM, prev)

    # Exit code matches coreutils `timeout` so the pre-dawn sweep still
    # records timed_out=true.
    assert excinfo.value.code == 124

    # The interrupt left NOTHING uncommitted — the whole point of #337.
    assert _porcelain(root) == "", "working tree must be clean after a partial commit"

    # The partial commit is present, distinct, and greppable. File 1 was
    # fully processed (1 created) before the interrupt fired during file 2.
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject.startswith("librarian: partial run (interrupted after 1 file(s)")
    assert "1C" in subject

    # Work written before AND at the interrupt point is durably committed
    # (file 2's page was on disk when SIGTERM arrived).
    assert (root / "wiki" / "entity-1.md").exists()
    assert (root / "wiki" / "entity-2.md").exists()


def test_normal_completion_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=2)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki"),
    )

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        install_signal_handlers=True,
    )

    assert rc == 0
    subjects = _log_subjects(root)
    # No behavior change: exactly one terminal commit, no partial-run commit.
    assert "librarian: partial run" not in subjects
    assert subjects.count("librarian: processed") == 1
    assert _porcelain(root) == ""


def test_default_does_not_install_signal_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki"),
    )

    before = signal.getsignal(signal.SIGTERM)
    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        # install_signal_handlers defaults to False — opt-in only.
    )
    after = signal.getsignal(signal.SIGTERM)

    assert rc == 0
    assert after is before, (
        "default run must not install a process-wide SIGTERM handler "
        "(in-process callers keep their own signal handling)"
    )
