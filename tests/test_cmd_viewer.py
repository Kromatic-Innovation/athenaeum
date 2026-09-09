# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum viewer`` (issue athenaeum#1480).

Uses :mod:`athenaeum.push_metrics` freely to SEED fixture ledgers -- that is
the test's job, not the viewer's. The point this file exists to prove is the
opposite: that ``athenaeum._cmd_viewer`` itself never touches those ledgers
except through the documented ``push-metrics tail --json`` CLI contract. See
``test_viewer_never_opens_ledger_file_directly`` and
``test_cmd_viewer_never_imports_push_metrics_module`` below.
"""

from __future__ import annotations

import builtins
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from athenaeum import _cmd_viewer, push_metrics


def _seed_push(cache_dir: Path, *, session_id: str, uid: str, source: str = "") -> None:
    """Mirrors ``tests/test_push_metrics_cli.py``'s helper of the same name."""
    if source == push_metrics.SOURCE_HOOK:
        push_metrics.record_hook_push(session_id, [uid], cache_dir=cache_dir)
        return
    record = push_metrics.build_push_record(
        session_id=session_id,
        query="q",
        backend="fts5",
        hits=[(f"{uid}.md", {"uid": uid, "access": "internal", "audience": ["owner"]}, "body")],
    )
    if source:
        record.source = source
    push_metrics.record_push(record, cache_dir=cache_dir)


def _seed_reference(
    cache_dir: Path, *, session_id: str, pushed_ids: list[str], referenced_ids: list[str]
) -> None:
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id=session_id,
            ts="2026-01-01T00:00:05Z",
            pushed_ids=pushed_ids,
            referenced_ids=referenced_ids,
        ),
        cache_dir=cache_dir,
    )


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def test_add_viewer_subparser_registers_with_cli() -> None:
    import argparse

    from athenaeum.cli import build_parser

    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert "viewer" in subparsers_action.choices
    viewer_parser = subparsers_action.choices["viewer"]
    assert viewer_parser.get_default("func") is _cmd_viewer.cmd_viewer
    args = viewer_parser.parse_args([])
    assert args.port == _cmd_viewer.DEFAULT_PORT
    assert args.session is None


# ---------------------------------------------------------------------------
# shape_viewer_payload -- pure function, no subprocess
# ---------------------------------------------------------------------------


def _push_record(session_id: str, items: list[dict], *, source: str | None = None) -> dict:
    rec = {
        "record_type": "push",
        "v": 1,
        "session_id": session_id,
        "ts": "2026-01-01T00:00:00Z",
        "query_hash": "abc",
        "backend": "fts5",
        "items": items,
        "pushed_count": len(items),
        "token_cost": sum(i.get("token_cost", 0) for i in items),
        "token_cost_estimated": True,
    }
    if source:
        rec["source"] = source
    return rec


def _reference_record(session_id: str, *, pushed_count: int, referenced_ids: list[str]) -> dict:
    return {
        "record_type": "reference",
        "v": 1,
        "session_id": session_id,
        "ts": "2026-01-01T00:00:05Z",
        "pushed_count": pushed_count,
        "referenced_count": len(referenced_ids),
        "referenced_ids": referenced_ids,
        "precision": (len(referenced_ids) / pushed_count) if pushed_count else None,
    }


_ITEM_A = {"id": "a", "tier": "internal", "scope": "owner", "token_cost": 10, "memory_tier": "warm"}
_ITEM_B = {"id": "b", "tier": "open", "scope": "open", "token_cost": 20, "memory_tier": "hot"}
_ITEM_C = {"id": "c", "tier": "internal", "scope": "owner", "token_cost": 30, "memory_tier": "cold"}


def test_shape_viewer_payload_splits_pushed_pulled_overlap() -> None:
    records = [
        _push_record("s1", [_ITEM_A, _ITEM_B], source=push_metrics.SOURCE_HOOK),  # unbidden: a, b
        _push_record("s1", [_ITEM_B, _ITEM_C]),  # deliberate (no source): b, c -- b overlaps
    ]
    payload = _cmd_viewer.shape_viewer_payload(session_id="s1", records=records)

    unbidden_ids = {r["id"] for r in payload["pushed_unbidden"]}
    deliberate_ids = {r["id"] for r in payload["pulled_deliberately"]}
    overlap_ids = {r["id"] for r in payload["overlap"]}

    assert unbidden_ids == {"a", "b"}
    assert deliberate_ids == {"b", "c"}
    assert overlap_ids == {"b"}
    assert payload["session_id"] == "s1"


def test_shape_viewer_payload_sidecar_source_counts_as_unbidden() -> None:
    records = [_push_record("s1", [_ITEM_A], source="sidecar")]
    payload = _cmd_viewer.shape_viewer_payload(session_id="s1", records=records)
    assert {r["id"] for r in payload["pushed_unbidden"]} == {"a"}
    assert payload["pulled_deliberately"] == []


def test_shape_viewer_payload_row_carries_all_required_fields() -> None:
    """AC2: id, tier, scope, memory tier, estimated token cost, referenced."""
    records = [
        _push_record("s1", [_ITEM_A]),
        _reference_record("s1", pushed_count=1, referenced_ids=["a"]),
    ]
    payload = _cmd_viewer.shape_viewer_payload(session_id="s1", records=records)
    row = payload["pulled_deliberately"][0]
    assert row == {
        "id": "a",
        "tier": "internal",
        "scope": "owner",
        "memory_tier": "warm",
        "token_cost": 10,
        "referenced": True,
    }


def test_shape_viewer_payload_no_reference_determination_yet() -> None:
    """AC6: pushes exist, no reference-determination record at all yet --
    every row's `referenced` must be `None` (pending), never `False`
    (which would misreport "checked, and not referenced")."""
    records = [_push_record("s1", [_ITEM_A, _ITEM_B], source=push_metrics.SOURCE_HOOK)]
    payload = _cmd_viewer.shape_viewer_payload(session_id="s1", records=records)

    assert payload["has_reference_determination"] is False
    for row in payload["pushed_unbidden"]:
        assert row["referenced"] is None


def test_shape_viewer_payload_reference_determination_present_marks_true_and_false() -> None:
    records = [
        _push_record("s1", [_ITEM_A, _ITEM_B], source=push_metrics.SOURCE_HOOK),
        _reference_record("s1", pushed_count=2, referenced_ids=["a"]),
    ]
    payload = _cmd_viewer.shape_viewer_payload(session_id="s1", records=records)
    by_id = {r["id"]: r["referenced"] for r in payload["pushed_unbidden"]}
    assert by_id == {"a": True, "b": False}
    assert payload["has_reference_determination"] is True


# ---------------------------------------------------------------------------
# build_viewer_data -- real subprocess against a real seeded ledger
# ---------------------------------------------------------------------------


def test_build_viewer_data_end_to_end_against_real_ledger(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="target", uid="hook1", source=push_metrics.SOURCE_HOOK)
    _seed_push(cache_dir, session_id="target", uid="pull1")
    _seed_push(cache_dir, session_id="other", uid="noise")

    payload = _cmd_viewer.build_viewer_data(
        session_id="target", path=tmp_path / "knowledge", cache_dir=cache_dir
    )

    assert payload["session_id"] == "target"
    assert {r["id"] for r in payload["pushed_unbidden"]} == {"hook1"}
    assert {r["id"] for r in payload["pulled_deliberately"]} == {"pull1"}
    assert payload["overlap"] == []
    assert payload["has_reference_determination"] is False


def test_build_viewer_data_unscoped_sees_every_session(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="a", uid="ida", source=push_metrics.SOURCE_HOOK)
    _seed_push(cache_dir, session_id="b", uid="idb")

    payload = _cmd_viewer.build_viewer_data(
        session_id=None, path=tmp_path / "knowledge", cache_dir=cache_dir
    )
    assert {r["id"] for r in payload["pushed_unbidden"]} == {"ida"}
    assert {r["id"] for r in payload["pulled_deliberately"]} == {"idb"}


def test_build_viewer_data_raises_on_nonzero_contract_exit(tmp_path: Path, monkeypatch) -> None:
    import subprocess as subprocess_mod

    def _fake_run(argv, **kwargs):
        return subprocess_mod.CompletedProcess(argv, returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(_cmd_viewer.subprocess, "run", _fake_run)
    with pytest.raises(_cmd_viewer.ViewerContractError, match="boom"):
        _cmd_viewer.build_viewer_data(session_id="s1", path=tmp_path, cache_dir=tmp_path)


def test_build_viewer_data_raises_on_non_json_line(tmp_path: Path, monkeypatch) -> None:
    """A misbehaving `push-metrics tail --json` that emits a non-JSON line is
    a contract violation, not something to silently skip -- surfaced as the
    same :class:`_cmd_viewer.ViewerContractError` a nonzero exit raises."""
    import subprocess as subprocess_mod

    def _fake_run(argv, **kwargs):
        return subprocess_mod.CompletedProcess(argv, returncode=0, stdout="not json\n", stderr="")

    monkeypatch.setattr(_cmd_viewer.subprocess, "run", _fake_run)
    with pytest.raises(_cmd_viewer.ViewerContractError, match="non-JSON"):
        _cmd_viewer.build_viewer_data(session_id="s1", path=tmp_path, cache_dir=tmp_path)


# ---------------------------------------------------------------------------
# AC4: consumes the contract, opens no ledger file directly
# ---------------------------------------------------------------------------


def test_cmd_viewer_never_imports_push_metrics_module() -> None:
    """Cheap defense-in-depth via a real AST walk (never a text/substring
    scan, which would also flag this module's own doc-comments explaining
    the rule): no top-level or function-local import of
    :mod:`athenaeum.push_metrics`, by any name or alias. The real,
    load-bearing proof is the behavioral test below (patched `open`), which
    would also catch a same-effect import this static check somehow missed."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_cmd_viewer))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "athenaeum.push_metrics", (
                    f"found `import {alias.name}` in _cmd_viewer.py"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module != "athenaeum.push_metrics", (
                f"found `from {module} import ...` in _cmd_viewer.py"
            )
            imports_push_metrics = module == "athenaeum" and any(
                a.name == "push_metrics" for a in node.names
            )
            assert not imports_push_metrics, (
                "found `from athenaeum import push_metrics` in _cmd_viewer.py"
            )


def test_viewer_never_opens_ledger_file_directly(tmp_path: Path, monkeypatch) -> None:
    """The mechanical AC4 proof: patch `open` in THIS (parent) process to
    blow up if ever called with a ledger path, then prove the viewer still
    returns fully correct data. That is only possible if the actual file
    read happens inside the subprocess this test's patch cannot reach --
    i.e. the viewer truly goes through `push-metrics tail --json` rather
    than reading the ledger in-process."""
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="s1", uid="hook1", source=push_metrics.SOURCE_HOOK)
    _seed_push(cache_dir, session_id="s1", uid="pull1")
    _seed_reference(
        cache_dir, session_id="s1", pushed_ids=["hook1", "pull1"], referenced_ids=["pull1"]
    )

    push_path = push_metrics.push_records_path(cache_dir)
    ref_path = push_metrics.reference_records_path(cache_dir)
    guarded_names = {push_path.name, ref_path.name}
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        name = Path(file).name if isinstance(file, (str, Path)) else ""
        if name in guarded_names:
            raise AssertionError(
                f"viewer process opened a ledger file directly: {file!r} "
                "-- it must go through `push-metrics tail --json` instead"
            )
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    payload = _cmd_viewer.build_viewer_data(
        session_id="s1", path=tmp_path / "knowledge", cache_dir=cache_dir
    )

    # The guard did not fire (no exception), and the data is still fully
    # correct -- proving the read happened in the (unguarded) subprocess.
    assert {r["id"] for r in payload["pushed_unbidden"]} == {"hook1"}
    assert {r["id"] for r in payload["pulled_deliberately"]} == {"pull1"}
    assert payload["has_reference_determination"] is True
    referenced_by_id = {
        r["id"]: r["referenced"]
        for r in payload["pushed_unbidden"] + payload["pulled_deliberately"]
    }
    assert referenced_by_id == {"hook1": False, "pull1": True}


# ---------------------------------------------------------------------------
# HTTP server -- localhost-only bind, routes, and a real end-to-end request
# ---------------------------------------------------------------------------


def test_make_server_binds_loopback_only_on_os_assigned_port(tmp_path: Path) -> None:
    server = _cmd_viewer.make_server(session_id="s1", path=tmp_path, cache_dir=tmp_path, port=0)
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert host == "127.0.0.1"
        assert port != 0
    finally:
        server.server_close()


class _RunningServer:
    def __init__(self, server) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "viewer server thread did not stop"
        self.server.server_close()


def test_serve_html_and_data_json_end_to_end_no_reference_determination(tmp_path: Path) -> None:
    """Step-5-shaped test: really binds a socket, really serves both routes,
    and covers AC6 (pushes present, no reference determination yet)."""
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="s1", uid="hook1", source=push_metrics.SOURCE_HOOK)
    _seed_push(cache_dir, session_id="s1", uid="pull1")

    server = _cmd_viewer.make_server(
        session_id="s1", path=tmp_path / "knowledge", cache_dir=cache_dir, port=0
    )
    with _RunningServer(server) as running:
        port = running.server_address[1]
        base = f"http://127.0.0.1:{port}"

        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
        assert "Pushed unbidden" in html
        assert "Pulled deliberately" in html
        assert "Overlap" in html

        with urllib.request.urlopen(f"{base}/data.json", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))

    assert payload["session_id"] == "s1"
    assert payload["has_reference_determination"] is False
    assert {r["id"] for r in payload["pushed_unbidden"]} == {"hook1"}
    assert {r["id"] for r in payload["pulled_deliberately"]} == {"pull1"}
    assert all(r["referenced"] is None for r in payload["pushed_unbidden"])


def test_data_json_scopes_to_one_session(tmp_path: Path) -> None:
    """AC3: --session scoping, so a viewer that itself triggers recall would
    not see its own activity mixed in with the session under study."""
    cache_dir = tmp_path / "cache"
    _seed_push(cache_dir, session_id="target", uid="t1", source=push_metrics.SOURCE_HOOK)
    _seed_push(
        cache_dir, session_id="viewer-own-session", uid="v1", source=push_metrics.SOURCE_HOOK
    )

    server = _cmd_viewer.make_server(
        session_id="target", path=tmp_path / "knowledge", cache_dir=cache_dir, port=0
    )
    with _RunningServer(server) as running:
        port = running.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/data.json", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

    assert {r["id"] for r in payload["pushed_unbidden"]} == {"t1"}


def test_unknown_route_returns_404(tmp_path: Path) -> None:
    server = _cmd_viewer.make_server(session_id="s1", path=tmp_path, cache_dir=tmp_path, port=0)
    with _RunningServer(server) as running:
        port = running.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert exc_info.value.code == 404


def test_data_json_returns_502_on_contract_failure(tmp_path: Path, monkeypatch) -> None:
    import subprocess as subprocess_mod

    def _fake_run(argv, **kwargs):
        return subprocess_mod.CompletedProcess(
            argv, returncode=1, stdout="", stderr="ledger unreadable"
        )

    monkeypatch.setattr(_cmd_viewer.subprocess, "run", _fake_run)

    server = _cmd_viewer.make_server(session_id="s1", path=tmp_path, cache_dir=tmp_path, port=0)
    with _RunningServer(server) as running:
        port = running.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/data.json", timeout=5)
        assert exc_info.value.code == 502


# ---------------------------------------------------------------------------
# AC5 / packaging: static asset actually ships in the wheel
# ---------------------------------------------------------------------------


def test_load_static_html_reads_the_packaged_asset() -> None:
    body = _cmd_viewer._load_static_html()
    assert b"<html" in body
    assert b"data.json" in body


def test_built_wheel_contains_viewer_static_asset() -> None:
    """Mirrors tests/test_skill_packaging.py's real-build check: pyproject.toml
    is unchanged for this issue (AC5), so this proves the asset ships via
    hatchling's existing default packaging of `src/athenaeum`, not via a new
    `include` entry."""
    pytest.importorskip("build", reason="`build` not installed; `pip install athenaeum[dev]`")

    import subprocess
    import sys
    import tempfile
    import zipfile

    repo_root = Path(__file__).resolve().parent.parent

    with tempfile.TemporaryDirectory() as outdir:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                outdir,
                str(repo_root),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"wheel build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        wheel_path = next(Path(outdir).glob("*.whl"))
        with zipfile.ZipFile(wheel_path) as zf:
            names = zf.namelist()
        assert "athenaeum/viewer_static/index.html" in names
