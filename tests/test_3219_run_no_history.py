# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#3219 — ``run`` decides from the store's declared history.

A store declared with no history (``athenaeum init --no-history``, which
writes ``storage.history: none``) has no ``.git`` by choice. ``run`` used to
probe for ``.git`` itself and refuse such a store with exit 1. It now asks the
store's capability profile instead:

- declared ``none`` -> the run proceeds, logging once that the pre-run
  snapshot used for raw-file recovery is not available on this store;
- declared ``git`` but not versioned (no ``.git``) -> still refused with the
  existing message.

The compile is real (a tier0-structured raw file compiles with zero LLM
calls); the Anthropic client is mocked and never called.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import librarian
from athenaeum.cli import main as cli_main
from athenaeum.librarian import run
from tests.test_session_end import _write_tier0_raw

_NO_SNAPSHOT_FRAGMENT = "no pre-run snapshot on this store"
_REFUSAL_FRAGMENT = "refusing to run without a writable git repo"


@pytest.fixture
def mock_anthropic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``anthropic.Anthropic`` and set a fake key so the api preflight passes."""
    import anthropic as anthropic_mod

    client = MagicMock()
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key-not-real")
    # Pin the provider: ``init`` may otherwise select ``claude-cli`` when a
    # ``claude`` binary happens to be on PATH, and an ambient override would
    # outrank the config this module writes.
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "api")
    return client


def _init_no_history(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    assert cli_main(["init", "--path", str(root), "--no-history"]) == 0
    assert not (root / ".git").exists()
    return root


def _run(root: Path) -> int:
    return run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
    )


class TestNoHistoryStoreRuns:
    def test_run_compiles_a_page_and_exits_zero(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _init_no_history(tmp_path)
        capsys.readouterr()
        _write_tier0_raw(root, "p-0001", "Alice Example", "20260101T000000Z", "aaaa0001")

        rc = _run(root)

        assert rc == 0
        pages = list((root / "wiki").glob("p-0001-*.md"))
        assert pages, "the raw file must compile to a wiki page"
        assert "Alice Example" in pages[0].read_text(encoding="utf-8")
        # Still no repository: the run must not create one behind the
        # operator's declared choice.
        assert not (root / ".git").exists()
        mock_anthropic.messages.create.assert_not_called()

    def test_run_logs_the_missing_snapshot_exactly_once(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _init_no_history(tmp_path)
        _write_tier0_raw(root, "p-0002", "Bob Example", "20260101T000000Z", "bbbb0002")
        caplog.set_level(logging.INFO, logger="athenaeum")

        assert _run(root) == 0

        notes = [r for r in caplog.records if _NO_SNAPSHOT_FRAGMENT in r.getMessage()]
        assert len(notes) == 1
        assert str(root) in notes[0].getMessage()
        assert not any(_REFUSAL_FRAGMENT in r.getMessage() for r in caplog.records)


class TestGitDeclaredStoreWithoutRepoRefuses:
    def test_declared_git_without_dot_git_exits_one(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ``init`` without --no-history declares the default (git) history;
        # removing the repository leaves a store that declares git and has
        # no ``.git``.
        root = tmp_path / "kb"
        assert cli_main(["init", "--path", str(root)]) == 0
        import shutil

        shutil.rmtree(root / ".git")
        _write_tier0_raw(root, "p-0003", "Carol Example", "20260101T000000Z", "cccc0003")
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = _run(root)

        assert rc == 1
        assert any(
            _REFUSAL_FRAGMENT in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )
        assert not any(_NO_SNAPSHOT_FRAGMENT in r.getMessage() for r in caplog.records)
        assert not list((root / "wiki").glob("p-0003-*.md"))

    def test_invalid_history_value_exits_one(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _init_no_history(tmp_path)
        config = root / "athenaeum.yaml"
        text = config.read_text(encoding="utf-8")
        assert "history: none" in text
        config.write_text(text.replace("history: none", "history: sometimes"), encoding="utf-8")
        caplog.set_level(logging.INFO, logger="athenaeum")

        assert _run(root) == 1
        assert any("storage.history" in r.getMessage() for r in caplog.records)


def test_preconditions_gate_has_no_dot_git_probe() -> None:
    """AC: the ``.git`` test in ``run`` is replaced by the capability check."""
    source = inspect.getsource(librarian._run_preconditions)
    assert '".git"' not in source
    assert "declares_no_history" in source
