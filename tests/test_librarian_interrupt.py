# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#337 — a timeout-killed librarian run must not strand its output.

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

Post-athenaeum#897: this signal-driven exit is the ONE remaining path that
returns 124 (`EXIT_EXTERNAL_KILL`) — athenaeum's own internal deadline check
(`tests/test_librarian_deadline.py`) now returns a distinct 75
(`EXIT_GRACEFUL_PARTIAL`) instead. See docs/exit-codes.md for the full
contract.
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


def _writing_process_one_factory(
    wiki_root: Path,
    *,
    interrupt_on: int | None = None,
    sig: int = signal.SIGTERM,
):
    """A ``process_one`` stand-in that writes one wiki page per file.

    When *interrupt_on* is set, it sends *sig* (default SIGTERM) to its own
    process right after writing that Nth page — simulating a wall-clock
    timeout (SIGTERM) or a manual Ctrl-C (SIGINT) arriving mid-run, after the
    page is on disk but before the run commits.
    """
    state = {"n": 0}

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        state["n"] += 1
        page = wiki_root / f"entity-{state['n']}.md"
        page.write_text(f"# Entity {state['n']}\nfrom {raw.ref}\n", encoding="utf-8")
        if interrupt_on is not None and state["n"] == interrupt_on:
            os.kill(os.getpid(), sig)
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


@pytest.mark.parametrize(
    "sig",
    [signal.SIGTERM, signal.SIGINT],
    ids=["sigterm-timeout", "sigint-ctrl-c"],
)
def test_interrupt_commits_partial_progress_and_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sig: int
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=3)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setattr(
        "athenaeum.librarian.process_one",
        _writing_process_one_factory(root / "wiki", interrupt_on=2, sig=sig),
    )

    # Safety net: if run() ever regresses and fails to install its own
    # handler, this sentinel turns the self-sent signal into a clean
    # AssertionError instead of killing the whole pytest process (a bare
    # SIGTERM/SIGINT would otherwise terminate/abort it).
    def _sentinel(signum: int, frame: object) -> None:
        raise AssertionError(
            f"run() did not install a signal {signum} handler (issue athenaeum#337 regression)"
        )

    prev = signal.signal(sig, _sentinel)
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
        signal.signal(sig, prev)

    # Exit code matches coreutils `timeout` so the pre-dawn sweep still
    # records timed_out=true.
    assert excinfo.value.code == 124

    # The interrupt left NOTHING uncommitted — the whole point of athenaeum#337.
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

    # A normal opt-in run must also RESTORE the process-wide handlers it
    # installed (issue athenaeum#337) — not just install them. Capturing before/after
    # proves the terminal-commit restore path fires, so the handler never
    # outlives the run.
    term_before = signal.getsignal(signal.SIGTERM)
    int_before = signal.getsignal(signal.SIGINT)

    rc = run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        install_signal_handlers=True,
    )

    assert rc == 0
    assert signal.getsignal(signal.SIGTERM) is term_before
    assert signal.getsignal(signal.SIGINT) is int_before
    subjects = _log_subjects(root)
    # No behavior change: exactly one terminal commit, no partial-run commit.
    assert "librarian: partial run" not in subjects
    assert subjects.count("librarian: processed") == 1
    assert _porcelain(root) == ""


def test_interrupt_records_partial_spend_to_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue athenaeum#483: an interrupted run still writes a spend-ledger record.

    The terminal ``record_spend`` (end of a clean run) never runs on the
    SIGTERM/SIGINT path, so before athenaeum#483 a killed run — or one the spend
    ceiling itself tripped — left NO ledger entry and ``athenaeum spend``
    reported $0 for it forever. The interrupt handler now records whatever
    spend accrued before exiting 124.
    """
    import json

    root = _seed_knowledge_root(tmp_path, n_files=3)
    ledger = tmp_path / "spend.jsonl"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

    # A process_one that accrues real token usage (so record_spend has
    # something to write — it no-ops on an empty accumulator) and sends
    # SIGTERM to itself after the 2nd file, mid-run.
    state = {"n": 0}

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        state["n"] += 1
        kwargs["usage"].add_tokens(100, 50, model="claude-test")
        (root / "wiki" / f"entity-{state['n']}.md").write_text(
            f"# Entity {state['n']}\n", encoding="utf-8"
        )
        if state["n"] == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return SimpleNamespace(
            created=[f"entity-{state['n']}.md"], updated=[], escalated=[], skipped=[]
        )

    monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

    def _sentinel(signum: int, frame: object) -> None:
        raise AssertionError("run() did not install a SIGTERM handler (athenaeum#337 regression)")

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

    assert excinfo.value.code == 124
    # The whole point of athenaeum#483: a killed run leaves a ledger record rather
    # than silently reporting $0 for spend it actually incurred.
    assert ledger.exists(), "interrupt must write a spend-ledger record"
    records = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["run_type"] == "librarian"
    assert rec["provider"] == "anthropic"
    # Two files accrued 150 tokens each before the interrupt fired.
    assert rec["total_tokens"] == 300
    assert rec["estimated_cost_usd"] > 0


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
