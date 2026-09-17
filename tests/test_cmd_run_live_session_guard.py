# SPDX-License-Identifier: Apache-2.0
"""``athenaeum run --no-live-session-guard`` reaches ``librarian.run()`` (issue athenaeum#1728).

Quine follow-up: every existing ``--no-live-session-guard`` test exercises
``athenaeum.librarian.run()`` or ``athenaeum.retire.run_retire_pass()``
directly -- none of them go through ``athenaeum._cmd_run.cmd_run()`` itself,
so a dropped ``live_session_guard=...`` kwarg at either of its two ``run(...)``
call sites (the ``--dry-run`` branch and the real/locked branch) would go
undetected. These tests patch ``athenaeum.librarian.run`` to a kwarg-capturing
fake and drive ``cmd_run`` through the real argparse parser, so they exercise
the SAME flag-to-kwarg wiring the CLI actually uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest


def _build_args(argv: list[str]) -> argparse.Namespace:
    from athenaeum._cmd_run import add_run_subparser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    add_run_subparser(subparsers)
    return parser.parse_args(argv)


class TestNoLiveSessionGuardFlagReachesRun:
    def test_dry_run_branch_threads_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum._cmd_run import cmd_run

        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

        args = _build_args(
            [
                "run",
                "--dry-run",
                "--no-live-session-guard",
                "--knowledge-root",
                str(tmp_path),
            ]
        )
        rc = cmd_run(args)

        assert rc == 0
        assert captured["live_session_guard"] is False

    def test_dry_run_branch_default_is_none_not_hardcoded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the flag, `live_session_guard` must reach `run()` as
        # `None` (so run()/run_retire_pass() resolve it from yaml/default),
        # never a hardcoded True/False -- proves this is genuinely the
        # parsed CLI value, not a constant slipped into the call.
        from athenaeum._cmd_run import cmd_run

        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

        args = _build_args(["run", "--dry-run", "--knowledge-root", str(tmp_path)])
        rc = cmd_run(args)

        assert rc == 0
        assert captured["live_session_guard"] is None

    def test_real_locked_branch_threads_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The non-dry-run branch (~285) acquires the run lock BEFORE calling
        # `run()` -- exercised here for real against a throwaway
        # knowledge_root, with `run()` itself faked so no actual compile
        # happens.
        from athenaeum._cmd_run import cmd_run

        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

        args = _build_args(
            [
                "run",
                "--no-live-session-guard",
                "--knowledge-root",
                str(tmp_path),
            ]
        )
        rc = cmd_run(args)

        assert rc == 0
        assert captured["live_session_guard"] is False
