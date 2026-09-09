# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum push-metrics {baseline,coverage-audit,record}`
(issue athenaeum#711; `record` added by issue athenaeum#1478)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_capture_stderr(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cli_main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _seed_valid_ledger(cache_dir: Path, *, session_id: str = "s1") -> None:
    """Seed one push + one fully-referenced record so a baseline computed
    over *cache_dir* has ``reference_record_count > 0`` — the CLI refuses to
    write a snapshot otherwise (issue athenaeum#795)."""
    push = push_metrics.build_push_record(
        session_id=session_id, query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(push, cache_dir=cache_dir)
    ref = push_metrics.ReferenceResult(
        session_id=session_id,
        ts="2026-01-01T00:00:00Z",
        pushed_ids=["u1"],
        referenced_ids=["u1"],
    )
    push_metrics.record_reference_result(ref, cache_dir=cache_dir)


def test_baseline_empty_ledger_is_honest(tmp_path: Path) -> None:
    """athenaeum#795: an empty ledger (zero reference records, precision not
    computable) must be REFUSED — the athenaeum#711 incident this issue
    fixes was exactly this case silently writing a placeholder snapshot.
    """
    docs_path = tmp_path / "docs" / "measurements" / "memory-model-measurements.md"
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-path",
            str(docs_path),
            "--json",
        ]
    )
    assert rc == 1
    assert not docs_path.exists()
    assert "reference_records" in err
    assert out == ""


def test_baseline_rerun_is_idempotent(tmp_path: Path) -> None:
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    for _ in range(3):
        rc, _ = _run(
            [
                "push-metrics",
                "baseline",
                "--cache-dir",
                str(cache_dir),
                "--docs-path",
                str(docs_path),
            ]
        )
        assert rc == 0
    content = docs_path.read_text()
    assert content.count("## Push-precision and coverage baseline") == 1
    assert content.count("### Snapshot") == 3


def test_baseline_text_output(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
        ]
    )
    assert rc == 0
    assert "sessions: 1" in out
    assert "athenaeum_version:" in out


def test_baseline_exclude_session_flag(tmp_path: Path) -> None:
    """athenaeum#791 AC3/AC4: ``--exclude-session`` drops a known-synthetic
    session from the counts/precision and reports it as a distinct field.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="clean",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["u1"],
            referenced_ids=["u1"],
        ),
        cache_dir=cache_dir,
    )
    synth = push_metrics.build_push_record(
        session_id="synth", query="q", backend="fts5", hits=[("test-page.md", None, "b")]
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="synth",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["test-page.md"],
            referenced_ids=[],
        ),
        cache_dir=cache_dir,
    )

    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--exclude-session",
            "synth",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sessions"] == 1
    assert payload["push_records"] == 1
    assert payload["excluded_sessions"] == ["synth"]
    assert payload["excluded_push_records"] == 1


def test_baseline_exclude_session_accepts_unambiguous_prefix(tmp_path: Path) -> None:
    """athenaeum#987 AC1: a session-id prefix that resolves to exactly one
    known session is accepted, same effect as the full id.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="clean", ts="2026-01-01T00:00:00Z", pushed_ids=["u1"], referenced_ids=["u1"]
        ),
        cache_dir=cache_dir,
    )
    synth = push_metrics.build_push_record(
        session_id="d5774338-7d8b-4152-a252-248d156f95ef",
        query="q",
        backend="fts5",
        hits=[("test-page.md", None, "b")],
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="d5774338-7d8b-4152-a252-248d156f95ef",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["test-page.md"],
            referenced_ids=[],
        ),
        cache_dir=cache_dir,
    )

    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--exclude-session",
            "d5774338-7d8b",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sessions"] == 1
    assert payload["excluded_sessions"] == ["d5774338-7d8b-4152-a252-248d156f95ef"]
    assert payload["excluded_push_records"] == 1


def test_baseline_exclude_session_no_match_is_a_loud_failure(tmp_path: Path) -> None:
    """athenaeum#987: the exact incident this issue fixes — a supplied value
    matching no known session id must exit non-zero, never silently succeed
    with a zero-effect exclusion.
    """
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--exclude-session",
            "no-such-session",
            "--json",
        ]
    )
    assert rc != 0
    assert "no-such-session" in err
    assert out == ""


def test_baseline_exclude_session_ambiguous_prefix_is_a_loud_failure(tmp_path: Path) -> None:
    """athenaeum#987: a prefix matching more than one known session must
    also be a hard error, never a guess at which one was meant.
    """
    cache_dir = tmp_path / "cache"
    for sid in ("synth-a", "synth-b"):
        push_metrics.record_push(
            push_metrics.build_push_record(
                session_id=sid, query="q", backend="fts5", hits=[("f.md", {"uid": sid}, "b")]
            ),
            cache_dir=cache_dir,
        )
        push_metrics.record_reference_result(
            push_metrics.ReferenceResult(
                session_id=sid, ts="2026-01-01T00:00:00Z", pushed_ids=[sid], referenced_ids=[sid]
            ),
            cache_dir=cache_dir,
        )
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--exclude-session",
            "synth-",
            "--json",
        ]
    )
    assert rc != 0
    assert "ambiguous" in err
    assert out == ""


def test_baseline_without_exclude_session_reports_honest_zero(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["excluded_sessions"] == []
    assert payload["excluded_push_records"] == 0
    assert payload["excluded_reference_records"] == 0


def test_baseline_dry_run_does_not_write(tmp_path: Path) -> None:
    """AC1: ``--dry-run`` computes/displays the baseline without touching
    ``--docs-path``. Uses a VALID (writable) baseline so this test isolates
    the dry-run behavior from the separate zero-reference-records refusal.
    """
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(docs_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    assert not docs_path.exists()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["sessions"] == 1


def test_baseline_dry_run_inspects_invalid_baseline_without_writing(tmp_path: Path) -> None:
    """This is the exact athenaeum#711 incident scenario: check whether a
    baseline is computable, over an empty/dead-instrument ledger, without
    mutating ``docs/measurements/memory-model-measurements.md``. ``--dry-run --json`` is
    the safe way to do that — no refusal, no write, exit 0.
    """
    docs_path = tmp_path / "docs.md"
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-path",
            str(docs_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    assert not docs_path.exists()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["sessions"] == 0
    assert payload["precision"] is None


def test_baseline_json_alone_still_writes(tmp_path: Path) -> None:
    """States the chosen dry-run semantics (issue athenaeum#795): ``--json``
    is a stdout-format concern only and does NOT by itself suppress the
    write — ``--dry-run`` is the (separate, explicit) no-write flag.
    """
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(docs_path),
            "--json",
        ]
    )
    assert rc == 0
    assert docs_path.is_file()
    payload = json.loads(out)
    assert payload["dry_run"] is False


def test_baseline_default_docs_path_not_written_for_invalid_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC ("cover the default docs_path"): every other CLI test above passes
    an explicit ``--docs-path`` under ``tmp_path``. The default relative
    ``docs_path=Path("docs/measurements/memory-model-measurements.md")`` — resolved
    against cwd, and the thing that actually wrote into the repo during the
    athenaeum#711 incident — is exercised here instead, by chdir-ing into a
    tmp directory first so a bug in this test can never write into the real
    repo docs. The ledger is empty (the incident's own scenario: a
    zero-reference-record baseline), so the refusal added by athenaeum#795
    is what actually protects the default path in practice.
    """
    monkeypatch.chdir(tmp_path)
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert rc == 1
    assert "reference_records" in err
    assert not (tmp_path / "docs" / "measurements" / "memory-model-measurements.md").exists()


def test_coverage_audit_writes_worksheet_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    rec = push_metrics.build_push_record(
        session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "body")]
    )
    push_metrics.record_push(rec, cache_dir=cache_dir)

    output = tmp_path / "worksheet.json"
    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "1",
            "--seed",
            "1",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.is_file()
    payload = json.loads(output.read_text())
    assert payload["sampled_session_count"] == 1
    assert str(output) in out


def test_coverage_audit_json_stdout(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    rec = push_metrics.build_push_record(
        session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "body")]
    )
    push_metrics.record_push(rec, cache_dir=cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "1",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sampled_session_count"] == 1


def test_coverage_audit_exclude_session_flag(tmp_path: Path) -> None:
    """athenaeum#986 AC2: ``--exclude-session`` on coverage-audit at the CLI
    layer — same semantics as ``baseline --exclude-session``: the excluded
    session is dropped from the sample and reported, never silently ignored.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    synth = push_metrics.build_push_record(
        session_id="synth", query="q", backend="fts5", hits=[("test-page.md", None, "b")]
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)

    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "5",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--exclude-session",
            "synth",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sampled_session_count"] == 1
    assert payload["sessions"][0]["session_id"] == "clean"
    assert payload["excluded_sessions"] == ["synth"]
    assert payload["excluded_push_records"] == 1


def test_coverage_audit_exclude_session_accepts_unambiguous_prefix(tmp_path: Path) -> None:
    """athenaeum#987 AC3: the prefix behavior applies uniformly to
    coverage-audit, not just baseline.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    synth = push_metrics.build_push_record(
        session_id="d5774338-7d8b-4152-a252-248d156f95ef",
        query="q",
        backend="fts5",
        hits=[("test-page.md", None, "b")],
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)

    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "5",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--exclude-session",
            "d5774338-7d8b",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sampled_session_count"] == 1
    assert payload["sessions"][0]["session_id"] == "clean"
    assert payload["excluded_sessions"] == ["d5774338-7d8b-4152-a252-248d156f95ef"]
    assert payload["excluded_push_records"] == 1


def test_coverage_audit_exclude_session_no_match_is_a_loud_failure(tmp_path: Path) -> None:
    """athenaeum#987: same loud-failure behavior on coverage-audit as on
    baseline — a value matching no known session id is a hard error.
    """
    cache_dir = tmp_path / "cache"
    push_metrics.record_push(
        push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        ),
        cache_dir=cache_dir,
    )
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "5",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--exclude-session",
            "no-such-session",
            "--json",
        ]
    )
    assert rc != 0
    assert "no-such-session" in err
    assert out == ""


def test_coverage_audit_exclude_session_ambiguous_prefix_is_a_loud_failure(tmp_path: Path) -> None:
    """athenaeum#987: an ambiguous prefix is a hard error on coverage-audit
    too, never a silent pick of one candidate session.
    """
    cache_dir = tmp_path / "cache"
    for sid in ("synth-a", "synth-b"):
        push_metrics.record_push(
            push_metrics.build_push_record(
                session_id=sid, query="q", backend="fts5", hits=[("f.md", {"uid": sid}, "b")]
            ),
            cache_dir=cache_dir,
        )
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "5",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--exclude-session",
            "synth-",
            "--json",
        ]
    )
    assert rc != 0
    assert "ambiguous" in err
    assert out == ""


# ---------------------------------------------------------------------------
# `push-metrics record` — hook-path push recording entry point (athenaeum#1478)
# ---------------------------------------------------------------------------


def _isolated_cache_and_path(tmp_path: Path) -> tuple[Path, Path]:
    """An isolated ``--cache-dir``/``--path`` pair for a `record` test.

    `record`'s ledger write goes through the SAME behind-the-seam resolution
    (`push_metrics.durable_push_records_path`) `baseline`/`coverage-audit`
    already use, which prefers a ``--path``-relative ``wiki/`` location over
    ``--cache-dir`` once anything exists there. Passing both, both pointed at
    this test's own ``tmp_path``, keeps every `record` test byte-isolated
    from a real ``~/knowledge`` regardless of what already exists on the
    host running the suite.
    """
    cache_dir = tmp_path / "cache"
    knowledge_root = tmp_path / "knowledge"
    cache_dir.mkdir()
    knowledge_root.mkdir()
    return cache_dir, knowledge_root


def _read_hook_ledger_row(cache_dir: Path, knowledge_root: Path) -> dict:
    rows = push_metrics.read_push_records(cache_dir=cache_dir, wiki_root=knowledge_root / "wiki")
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    return rows[0]


def test_record_argv_ids_writes_one_row(tmp_path: Path) -> None:
    """AC1: an external caller (here, argv flags) can invoke this with a
    session id and a list of injected ids and get a written push record."""
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--session-id",
            "sess-argv",
            "--id",
            "abc12345",
            "--id",
            "def67890",
            "--backend",
            "fts5",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out) == {"wrote": True}
    row = _read_hook_ledger_row(cache_dir, knowledge_root)
    assert row["session_id"] == "sess-argv"
    assert row["source"] == "hook"
    assert [it["id"] for it in row["items"]] == ["abc12345", "def67890"]


def test_record_stdin_json_hook_input_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `athenaeum context --stdin-json`'s established hook-input
    convention: `{"session_id": ..., "ids": [...], ...}` from stdin."""
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    payload = json.dumps(
        {
            "session_id": "sess-stdin",
            "ids": ["11111111-page.md", "raw-note.md"],
            "backend": "vector",
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--stdin-json",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out) == {"wrote": True}
    row = _read_hook_ledger_row(cache_dir, knowledge_root)
    assert row["session_id"] == "sess-stdin"
    assert row["backend"] == "vector"
    # Entity filename truncated to its 8-hex uid prefix; raw-intake filename
    # (never name-derived) recorded whole.
    assert [it["id"] for it in row["items"]] == ["11111111", "raw-note.md"]


def test_record_stdin_json_ids_replace_rather_than_merge_argv_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    payload = json.dumps({"ids": ["from-stdin"]})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc, _ = _run(
        [
            "push-metrics",
            "record",
            "--session-id",
            "sess-1",
            "--id",
            "from-argv",
            "--stdin-json",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
        ]
    )
    assert rc == 0
    row = _read_hook_ledger_row(cache_dir, knowledge_root)
    assert [it["id"] for it in row["items"]] == ["from-stdin"]


def test_record_falls_back_to_resolve_session_id_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-example that must fail: athenaeum#734's silent no-op — a
    session-id variable name read but never exported by anything, so the
    guard was always false and zero rows were ever written. This pins the
    GOOD path: when `--session-id` is omitted, the CLI resolves via
    `push_metrics.resolve_session_id()`, same as every other push-metrics
    call site (issue athenaeum#734's single sanctioned resolver)."""
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-from-env")
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--id",
            "abc12345",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out) == {"wrote": True}
    row = _read_hook_ledger_row(cache_dir, knowledge_root)
    assert row["session_id"] == "sess-from-env"


def test_record_no_session_id_anywhere_is_an_honest_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same counter-example as above, the negative direction: with no
    `--session-id`, no stdin payload, and nothing in the environment, the
    CLI must still exit 0 (fire-and-forget contract) but write NOTHING —
    never a row with a fabricated or empty session id."""
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--id",
            "abc12345",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out) == {"wrote": False}
    assert push_metrics.read_push_records(
        cache_dir=cache_dir, wiki_root=knowledge_root / "wiki"
    ) == []


def test_record_survives_an_unwritable_ledger_path(tmp_path: Path) -> None:
    """AC4: the ledger path is replaced with a DIRECTORY so the append
    write raises `IsADirectoryError` — the CLI must still exit 0 (never
    surface a ledger failure as a nonzero exit a fire-and-forget caller
    was never going to check)."""
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    (knowledge_root / "wiki").mkdir(parents=True)
    (knowledge_root / "wiki" / "_push_records.jsonl").mkdir()
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--session-id",
            "sess-1",
            "--id",
            "abc12345",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out) == {"wrote": False}


def test_record_malformed_stdin_json_is_an_honest_noop_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir, knowledge_root = _isolated_cache_and_path(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("not valid json {"))
    rc, out = _run(
        [
            "push-metrics",
            "record",
            "--stdin-json",
            "--session-id",
            "sess-1",
            "--cache-dir",
            str(cache_dir),
            "--path",
            str(knowledge_root),
            "--json",
        ]
    )
    assert rc == 0
    # No `ids` survived the malformed payload and none were on argv either
    # -> an honest no-op, not a crash.
    assert json.loads(out) == {"wrote": False}


def test_no_subcommand_prints_usage(tmp_path: Path) -> None:
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = cli_main(["push-metrics"])
    assert rc == 2
    assert "usage" in buf.getvalue().lower()


def test_parser_tree_binds_func_for_push_metrics_subcommands() -> None:
    """Guards the athenaeum#553 dispatch invariant for the new subcommand tree.

    The generic parser-tree walk in ``test_cli.py`` already asserts this for
    every registered subcommand; this test additionally pins the two leaves
    ``push-metrics`` actually adds so a future refactor of this module alone
    fails fast and locally.
    """
    import argparse

    from athenaeum.cli import build_parser

    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    push_metrics_parser = subparsers_action.choices["push-metrics"]
    assert push_metrics_parser.get_default("func") is not None
    inner = next(
        a
        for a in push_metrics_parser._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    assert set(inner.choices) == {"baseline", "coverage-audit", "record"}
    for name, sub in inner.choices.items():
        assert (
            sub.get_default("func") is not None
            or push_metrics_parser.get_default("func") is not None
        ), f"push-metrics {name} has no resolvable func"
