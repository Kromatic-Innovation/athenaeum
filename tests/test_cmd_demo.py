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
import builtins
import json
import select
import socket
from pathlib import Path

import pytest

from athenaeum import _cmd_demo, push_metrics
from athenaeum._cmd_demo import (
    _aggregate_sessions,
    _find_transcript,
    _parse_tail_ts,
    _probe_rows,
    _project_label,
    _report_rows,
    _shorten_home,
    _transcript_cwd,
    bind_server,
    cmd_demo,
    cmd_list_sessions,
    resolve_session_id,
)
from athenaeum._cmd_viewer import ViewerContractError


def _seed_push(cache_dir: Path, *, session_id: str, uid: str, ts: str = "2026-01-01T00:00:00Z") -> None:
    """Mirrors ``tests/test_cmd_viewer.py``'s helper of the same name."""
    record = push_metrics.build_push_record(
        session_id=session_id,
        query="q",
        backend="fts5",
        hits=[(f"{uid}.md", {"uid": uid, "access": "internal", "audience": ["owner"]}, "body")],
    )
    record.ts = ts
    push_metrics.record_push(record, cache_dir=cache_dir)


def _seed_reference(
    cache_dir: Path,
    *,
    session_id: str,
    pushed_ids: list[str],
    referenced_ids: list[str],
    ts: str = "2026-01-01T00:00:05Z",
) -> None:
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id=session_id,
            ts=ts,
            pushed_ids=pushed_ids,
            referenced_ids=referenced_ids,
        ),
        cache_dir=cache_dir,
    )

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


def test_list_sessions_flag_and_limit_are_registered() -> None:
    from athenaeum.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["demo", "--list-sessions", "--limit", "5"])
    assert args.func is cmd_demo
    assert args.list_sessions is True
    assert args.limit == 5


# --------------------------------------------------------------------------
# --list-sessions (issue athenaeum#1531)
# --------------------------------------------------------------------------


def _ls_args(**overrides: object) -> argparse.Namespace:
    base = {
        "path": None,
        "cache_dir": None,
        "projects_root": Path("/nonexistent"),
        "list_sessions": True,
        "limit": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# -- AC1: exits 0 without binding a port or starting a server --------------


def test_list_sessions_never_binds_a_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of AC1: prove no port is bound, not merely exit 0."""

    def _boom(**_kwargs: object):
        raise AssertionError("--list-sessions must never bind a server")

    monkeypatch.setattr(_cmd_demo, "bind_server", _boom)
    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", lambda **_k: [])
    assert cmd_demo(_ls_args(path=tmp_path)) == 0


def test_list_sessions_short_circuits_before_session_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--list-sessions must not require a resolvable current-session id --
    the early return sits ABOVE that failure path."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", lambda **_k: [])
    assert cmd_demo(_ls_args(path=tmp_path, projects_root=Path("/nonexistent"))) == 0


# -- ts parsing: mixed-format ledger timestamps -----------------------------


def test_parse_tail_ts_handles_mixed_formats_without_raising() -> None:
    second_precision = _parse_tail_ts("2026-09-09T22:21:04Z")
    microsecond_precision = _parse_tail_ts("2026-08-27T17:36:16.292160Z")
    naive = _parse_tail_ts("2026-01-01T00:00:00")
    missing = _parse_tail_ts(None)

    assert missing is None
    for dt in (second_precision, microsecond_precision, naive):
        assert dt is not None
        assert dt.tzinfo is not None
    # Must be comparable/sortable without TypeError (the exact crash the
    # mixed-format ledger produces against naive fromisoformat).
    assert sorted([second_precision, microsecond_precision, naive]) is not None
    assert microsecond_precision < second_precision


# -- AC4: project column comes from transcript cwd, never dir-un-mangling --


def test_project_label_uses_transcript_cwd_not_dash_unmangling(tmp_path: Path) -> None:
    """The exact trap the issue calls out: a real directory name can itself
    contain dashes, so un-mangling ``~/.claude/projects/<mangled>`` by
    replacing dashes with slashes is lossy and silently wrong. Pin a path
    containing a dash and assert the naive derivation FAILS while the
    cwd-based derivation succeeds.
    """
    session_id = "cwd-session"
    real_cwd = "/Users/tristankromer/local-deploys/hestia"
    mangled_scope = "-Users-tristankromer-local-deploys-hestia"
    projects_root = tmp_path / "projects"

    # cwd is NOT on the first few header-shaped records -- the scan must not
    # give up after record 1.
    records = [
        {"sessionId": session_id, "type": "summary"},
        {"sessionId": session_id, "type": "user", "mode": "default"},
        {"sessionId": session_id, "type": "user", "mode": "default"},
        {"sessionId": session_id, "type": "assistant", "cwd": real_cwd, "gitBranch": "main"},
    ]
    _write_transcript(projects_root / mangled_scope / f"{session_id}.jsonl", records)

    label = _project_label(session_id, projects_root)

    # Naive un-mangle: dash -> slash over the SCOPE DIRECTORY NAME.
    naive = "~" + mangled_scope.replace("-", "/")
    assert label != naive
    assert "local-deploys/hestia" in label
    assert "local/deploys" not in label


def test_transcript_cwd_scans_past_header_records(tmp_path: Path) -> None:
    """cwd is not on the first record -- an implementation that reads record
    1 and gives up must fail this."""
    transcript = tmp_path / "projects" / "scope" / "sess.jsonl"
    _write_transcript(
        transcript,
        [
            {"sessionId": "sess", "type": "summary"},
            {"sessionId": "sess", "type": "user"},
            {"sessionId": "sess", "type": "user"},
            {"sessionId": "sess", "type": "assistant", "cwd": "/tmp/somewhere"},
        ],
    )
    assert _transcript_cwd(transcript) == "/tmp/somewhere"


def test_find_transcript_globs_by_session_id_not_scope(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    scope_dir = projects_root / "-some-mangled-scope"
    scope_dir.mkdir(parents=True)
    target = scope_dir / "abc-123.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    assert _find_transcript("abc-123", projects_root) == target
    assert _find_transcript("does-not-exist", projects_root) is None


def test_shorten_home_replaces_home_prefix_only() -> None:
    home = str(Path.home())
    assert _shorten_home(f"{home}/Code/athenaeum") == "~/Code/athenaeum"
    assert _shorten_home("/opt/elsewhere") == "/opt/elsewhere"


# -- AC5: visible placeholder for a session with no findable transcript ----


def test_project_label_placeholder_when_transcript_missing(tmp_path: Path) -> None:
    label = _project_label("ghost-session", tmp_path / "projects")
    assert label.strip() != ""
    assert "no transcript found" in label


def test_project_label_placeholder_when_cwd_never_found(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    _write_transcript(
        projects_root / "scope" / "sess.jsonl",
        [{"sessionId": "sess", "type": "summary"}],
    )
    label = _project_label("sess", projects_root)
    assert label.strip() != ""
    assert "cwd not found" in label


# -- AC3: `ended` reflects a reference-determination record ----------------


def test_aggregate_sessions_ended_reflects_reference_record() -> None:
    records = [
        {"record_type": "push", "session_id": "s1", "ts": "2026-01-01T00:00:00Z", "pushed_count": 3},
        {"record_type": "push", "session_id": "s2", "ts": "2026-01-01T00:00:01Z", "pushed_count": 2},
        {
            "record_type": "reference",
            "session_id": "s1",
            "ts": "2026-01-01T00:00:02Z",
            "pushed_count": 3,
            "referenced_count": 1,
        },
    ]
    sessions = _aggregate_sessions(records)
    assert sessions["s1"]["ended"] is True
    assert sessions["s2"]["ended"] is False
    assert sessions["s1"]["rows"] == 3
    assert sessions["s2"]["rows"] == 2


def test_ended_set_matches_reference_record_session_ids_on_real_ledger(
    tmp_path: Path,
) -> None:
    """AC3 against real data: the discriminating, ledger-drift-proof check.

    From ONE ``push-metrics tail --json`` drain, the set of session ids
    rendered ``ended=yes`` must equal the set of session ids that actually
    carry a reference-determination record in that SAME drain -- verified
    internally rather than against a hardcoded snapshot of a live ledger,
    which moves between runs.
    """
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="ended-1", uid="u1", ts="2026-01-01T00:00:00Z")
    _seed_push(cache_dir, session_id="open-1", uid="u2", ts="2026-01-01T00:00:01Z")
    _seed_reference(
        cache_dir,
        session_id="ended-1",
        pushed_ids=["u1"],
        referenced_ids=["u1"],
        ts="2026-01-01T00:00:02Z",
    )

    records = list(push_metrics.tail_records(cache_dir=cache_dir))
    expected_ended = {r["session_id"] for r in records if r["record_type"] == "reference"}

    sessions = _aggregate_sessions(records)
    actual_ended = {sid for sid, info in sessions.items() if info["ended"]}
    assert actual_ended == expected_ended
    assert actual_ended == {"ended-1"}


# -- AC7: never opens a ledger file directly --------------------------------


def test_list_sessions_never_opens_ledger_files_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors ``test_cmd_viewer.py::test_viewer_never_opens_ledger_file_directly``:
    patch ``open`` in THIS process to blow up ONLY on the two ledger
    filenames, then prove the command still produces correct output --
    possible only if the actual ledger read happens inside the
    ``push-metrics tail --json`` subprocess this patch cannot reach. Scoped
    to the ledger basenames (not every ``open`` call) because this command
    legitimately opens each session's OWN transcript file in-process to read
    ``cwd`` -- that is not a ledger and is outside the push-metrics contract.
    """
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="s1", uid="u1")

    push_path = push_metrics.push_records_path(cache_dir)
    ref_path = push_metrics.reference_records_path(cache_dir)
    guarded_names = {push_path.name, ref_path.name}
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        name = Path(file).name if isinstance(file, (str, Path)) else ""
        if name in guarded_names:
            raise AssertionError(
                f"--list-sessions opened a ledger file directly: {file!r} "
                "-- it must go through `push-metrics tail --json` instead"
            )
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    knowledge_path = tmp_path / "knowledge"
    knowledge_path.mkdir()
    exit_code = cmd_list_sessions(
        _ls_args(path=knowledge_path, cache_dir=cache_dir, projects_root=tmp_path / "projects")
    )
    assert exit_code == 0


# -- AC6: empty ledger prints an explicit line, not a blank table ----------


def test_empty_ledger_prints_explicit_line_not_blank_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", lambda **_k: [])
    exit_code = cmd_list_sessions(_ls_args(path=tmp_path))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "no sessions with recall activity yet" in err


def test_contract_failure_is_distinct_from_empty_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A probe FAILURE must not render as AC6's empty-ledger line -- the same
    None-vs-zero discipline ``_report_rows`` already applies to a
    single-session probe (and the exact shape of the interpreter-trap
    gather-agent flagged: a failing subprocess must not read as \"empty\").
    """

    def _boom(**_kwargs: object):
        raise ViewerContractError("tail exited 1: ModuleNotFoundError")

    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", _boom)
    exit_code = cmd_list_sessions(_ls_args(path=tmp_path))
    failed_err = capsys.readouterr().err

    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", lambda **_k: [])
    empty_exit_code = cmd_list_sessions(_ls_args(path=tmp_path))
    empty_err = capsys.readouterr().err

    assert exit_code == 1
    assert empty_exit_code == 0
    assert failed_err != empty_err
    assert "no sessions with recall activity yet" not in failed_err


# -- AC2: ordering (newest first) + --limit ---------------------------------


def test_sessions_ordered_newest_activity_first_and_limit_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        {"record_type": "push", "session_id": "old", "ts": "2026-01-01T00:00:00Z", "pushed_count": 1},
        {"record_type": "push", "session_id": "newest", "ts": "2026-01-03T00:00:00Z", "pushed_count": 1},
        {"record_type": "push", "session_id": "middle", "ts": "2026-01-02T00:00:00Z", "pushed_count": 1},
    ]
    monkeypatch.setattr(_cmd_demo, "_run_tail_contract", lambda **_k: records)
    monkeypatch.setattr(_cmd_demo, "_project_label", lambda *_a, **_k: "~/proj")

    exit_code = cmd_list_sessions(_ls_args(path=tmp_path, limit=2))
    assert exit_code == 0

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    body = lines[1:]  # drop header
    assert len(body) == 2
    assert body[0].startswith("newest")
    assert body[1].startswith("middle")

