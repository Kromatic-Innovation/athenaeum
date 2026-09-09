"""Tests for ``athenaeum demo`` (issue athenaeum#1525).

The interesting behavior is all in the launch path, not the serving: which
session id gets resolved, which port gets bound when the preferred one is
taken, and — the load-bearing one — that a probe FAILURE and a legitimate
zero-row count produce different warnings. Those two collapsing into one is the
exact bug this command exists to avoid, so it gets a test that fails if they
ever render the same advice.
"""

from __future__ import annotations

import argparse
import select
import socket
from pathlib import Path

import pytest

from athenaeum import _cmd_demo
from athenaeum._cmd_demo import (
    _probe_rows,
    _report_rows,
    bind_server,
    cmd_demo,
    resolve_session_id,
)
from athenaeum._cmd_viewer import ViewerContractError

# --------------------------------------------------------------------------
# Session id resolution (AC1)
# --------------------------------------------------------------------------


def test_env_var_wins_over_transcripts(tmp_path: Path) -> None:
    """The running session saying who it is beats an mtime inference."""
    projects = tmp_path / "projects" / "scope"
    projects.mkdir(parents=True)
    (projects / "newest-transcript.jsonl").write_text("{}\n", encoding="utf-8")

    resolved = resolve_session_id(
        projects_root=tmp_path / "projects",
        environ={"CLAUDE_CODE_SESSION_ID": "from-env"},
    )
    assert resolved == "from-env"


def test_session_id_env_precedence_matches_recorder() -> None:
    """CLAUDE_CODE_SESSION_ID outranks CLAUDE_SESSION_ID.

    Order must match athenaeum's own push recorder: if the two disagree, the
    viewer scopes to a session the recorder never wrote to.
    """
    resolved = resolve_session_id(
        projects_root=Path("/nonexistent"),
        environ={"CLAUDE_CODE_SESSION_ID": "primary", "CLAUDE_SESSION_ID": "secondary"},
    )
    assert resolved == "primary"


def test_blank_env_var_falls_through(tmp_path: Path) -> None:
    """An empty/whitespace env var must not resolve to an empty session id."""
    projects = tmp_path / "projects" / "scope"
    projects.mkdir(parents=True)
    (projects / "real-session.jsonl").write_text("{}\n", encoding="utf-8")

    resolved = resolve_session_id(
        projects_root=tmp_path / "projects",
        environ={"CLAUDE_CODE_SESSION_ID": "   "},
    )
    assert resolved == "real-session"


def test_newest_transcript_basename_is_the_id(tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "scope"
    projects.mkdir(parents=True)
    older = projects / "older.jsonl"
    newer = projects / "newer.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    import os

    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert resolve_session_id(projects_root=tmp_path / "projects", environ={}) == "newer"


def test_no_env_and_no_transcripts_resolves_none(tmp_path: Path) -> None:
    assert resolve_session_id(projects_root=tmp_path / "empty", environ={}) is None


# --------------------------------------------------------------------------
# Port fallback (AC2)
# --------------------------------------------------------------------------


def test_busy_port_falls_back_to_free_one(tmp_path: Path) -> None:
    """A viewer already holding the preferred port must not kill the launch."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        server, fell_back = bind_server(
            session_id="s", path=tmp_path, cache_dir=tmp_path, port=busy_port
        )
        try:
            assert fell_back is True
            assert server.server_address[1] != busy_port
            # Localhost-only bind is preserved through the fallback path.
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()
    finally:
        blocker.close()


def test_free_port_is_used_as_is(tmp_path: Path) -> None:
    server, fell_back = bind_server(session_id="s", path=tmp_path, cache_dir=tmp_path, port=0)
    try:
        assert fell_back is False
        assert server.server_address[1] != 0
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# Probe honesty (AC4) -- the load-bearing distinction
# --------------------------------------------------------------------------


def test_probe_failure_is_none_not_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _boom(**_kwargs: object) -> dict[str, object]:
        raise ViewerContractError("tail exited 1")

    monkeypatch.setattr(_cmd_demo, "build_viewer_data", _boom)
    assert _probe_rows(session_id="s", path=tmp_path, cache_dir=tmp_path) is None


def test_probe_counts_all_three_buckets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        _cmd_demo,
        "build_viewer_data",
        lambda **_k: {
            "pushed_unbidden": [{}, {}],
            "pulled_deliberately": [{}],
            "overlap": [],
        },
    )
    assert _probe_rows(session_id="s", path=tmp_path, cache_dir=tmp_path) == 3


def test_failed_probe_and_zero_rows_give_different_advice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point of the command's warning logic.

    A failed probe knows nothing about the session id and must not imply it is
    wrong; a genuine zero SHOULD raise that possibility. If these two ever
    render the same text, an operator gets confident advice about an id that
    was never in question.
    """
    _report_rows(None, "sess-abc")
    failed = capsys.readouterr().err

    _report_rows(0, "sess-abc")
    empty = capsys.readouterr().err

    assert failed != empty
    assert "session id is wrong" in empty
    assert "session id is wrong" not in failed
    assert "NOTHING either way" in failed


def test_nonzero_rows_reported_without_warning(capsys: pytest.CaptureFixture[str]) -> None:
    _report_rows(7, "sess-abc")
    err = capsys.readouterr().err
    assert "7 recall rows" in err
    assert "warning" not in err


# --------------------------------------------------------------------------
# cmd_demo wiring (AC1, AC3)
# --------------------------------------------------------------------------


def _args(**overrides: object) -> argparse.Namespace:
    base = {
        "session": None,
        "path": None,
        "cache_dir": None,
        "port": 0,
        "projects_root": Path("/nonexistent"),
        "no_browser": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_unresolvable_session_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert cmd_demo(_args()) == 1
    assert "could not resolve a Claude session id" in capsys.readouterr().err


def test_no_browser_opens_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--no-browser must not call out to a browser, and must still serve."""
    opened: list[str] = []
    monkeypatch.setattr(_cmd_demo.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(_cmd_demo, "_probe_rows", lambda **_k: 1)

    served: list[bool] = []

    class _FakeServer:
        server_address = ("127.0.0.1", 9999)

        def serve_forever(self) -> None:
            served.append(True)
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(_cmd_demo, "bind_server", lambda **_k: (_FakeServer(), False))

    assert cmd_demo(_args(session="s", path=tmp_path, no_browser=True)) == 0
    assert opened == []
    assert served == [True]


def test_browser_opens_the_port_actually_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC2's real requirement: the URL opened is the one that got bound.

    Opening the *requested* port after falling back to a different one is the
    failure this guards — it sends the operator to a dead address while the
    working server sits elsewhere.
    """
    opened: list[str] = []
    monkeypatch.setattr(_cmd_demo.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(_cmd_demo, "_probe_rows", lambda **_k: 1)

    class _FakeServer:
        server_address = ("127.0.0.1", 45678)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(_cmd_demo, "bind_server", lambda **_k: (_FakeServer(), True))

    assert cmd_demo(_args(session="s", path=tmp_path, port=8756, no_browser=False)) == 0
    assert opened == ["http://127.0.0.1:45678/"]


def test_url_line_is_flushed_before_serving(tmp_path: Path) -> None:
    """The URL must reach a redirected stdout BEFORE the server blocks.

    Found by running the command for real: stdout is block-buffered when it is
    a pipe, and `serve_forever` never returns, so an unflushed URL line stays
    in the buffer for the whole life of the process. A caller that captures
    stdout to learn the URL sees an empty stream and concludes the command
    hung. Asserted end-to-end through a real subprocess with a real pipe,
    because the buffering only manifests when stdout is not a tty.
    """
    import subprocess
    import sys as _sys

    proc = subprocess.Popen(
        [
            _sys.executable,
            "-m",
            "athenaeum.cli",
            "demo",
            "--session",
            "flush-probe",
            "--port",
            "0",
            "--no-browser",
            "--path",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        # select() rather than a bare readline(): the unflushed regression
        # produces NO line at all, and a blocking read would hang the whole
        # suite until CI's own timeout killed it. Bounded wait turns that into
        # a clean, fast, legible assertion failure instead.
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        assert ready, (
            "no line on stdout within 30s — the URL is almost certainly sitting "
            "unflushed in a block-buffered pipe (see cmd_demo's flush=True)"
        )
        line = proc.stdout.readline()
        assert "athenaeum demo: http://127.0.0.1:" in line
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_browser_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A headless box with no browser must still serve the page."""

    def _boom(_url: str) -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(_cmd_demo.webbrowser, "open", _boom)
    monkeypatch.setattr(_cmd_demo, "_probe_rows", lambda **_k: 1)

    class _FakeServer:
        server_address = ("127.0.0.1", 9999)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(_cmd_demo, "bind_server", lambda **_k: (_FakeServer(), False))
    assert cmd_demo(_args(session="s", path=tmp_path, no_browser=False)) == 0


# --------------------------------------------------------------------------
# CLI registration (AC5)
# --------------------------------------------------------------------------


def test_demo_is_registered_with_viewer_scoping_flags() -> None:
    from athenaeum.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["demo", "--session", "s", "--port", "1234", "--no-browser", "--cache-dir", "/tmp/c"]
    )
    assert args.func is cmd_demo
    assert args.session == "s"
    assert args.port == 1234
    assert args.no_browser is True
    assert args.cache_dir == Path("/tmp/c")
