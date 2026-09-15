# SPDX-License-Identifier: Apache-2.0
"""Tests for the merged-ledger session-shape measurement script (issue
athenaeum#1593 AC4 — the script itself, not a live run: this lane cannot
read the operator's real ledgers, see the script's own module docstring).

Pins the classification rule (must match `athenaeum.push_metrics` /
`athenaeum._cmd_viewer`'s documented reader rule exactly) and the
both/hook-only/pull-only bucketing this issue's AC4 asks for.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "measure_session_shapes.py"

_spec = importlib.util.spec_from_file_location("measure_session_shapes", _SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _push(session_id: str, ts: str, source: str = "hook") -> dict:
    return {"v": 1, "session_id": session_id, "ts": ts, "items": [], "source": source}


def _pull(session_id: str, ts: str) -> dict:
    """A row with no `source` key at all -- the MCP `recall` path's shape."""
    return {"v": 1, "session_id": session_id, "ts": ts, "items": []}


class TestClassifyRow:
    def test_hook_source_is_a_push(self) -> None:
        assert harness.classify_row(_push("s1", "2026-09-14T00:00:00Z", "hook")) == "push"

    def test_sidecar_source_is_a_push(self) -> None:
        assert harness.classify_row(_push("s1", "2026-09-14T00:00:00Z", "sidecar")) == "push"

    def test_absent_source_after_cutover_is_a_pull(self) -> None:
        assert harness.classify_row(_pull("s1", "2026-09-14T00:00:00Z")) == "pull"

    def test_absent_source_before_cutover_is_unknown(self) -> None:
        """Pre-`SOURCE_FIELD_FIRST_SEEN` (2026-09-09T03:48:00Z, issue
        athenaeum#1542): a source-less row from before the key existed is
        NOT evidence of a pull -- exactly the viewer's own reader rule."""
        assert harness.classify_row(_pull("s1", "2026-01-01T00:00:00Z")) == "unknown"

    def test_unparsable_ts_is_unknown(self) -> None:
        assert harness.classify_row(_pull("s1", "not-a-timestamp")) == "unknown"


class TestMeasure:
    def test_both_hook_only_and_pull_only_are_distinguished(self, tmp_path: Path) -> None:
        ledger = tmp_path / "_push_records.jsonl"
        _write_ledger(
            ledger,
            [
                _push("both-session", "2026-09-14T10:00:00Z"),
                _pull("both-session", "2026-09-14T10:01:00Z"),
                _push("hook-only-session", "2026-09-14T10:00:00Z"),
                _pull("pull-only-session", "2026-09-14T10:00:00Z"),
                _pull("unknown-only-session", "2026-01-01T00:00:00Z"),
            ],
        )

        result = harness.measure(
            [ledger],
            split_date=harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc),
        )

        after = result["windows"]["after"]
        assert after["both"] == 1
        assert after["hook-only"] == 1
        assert after["pull-only"] == 1
        # This session's only row predates the split date, so it lands in
        # the "before" window, not "after" -- windowing is orthogonal to
        # shape and this asserts both independently.
        assert result["windows"]["before"]["unknown-only"] == 1
        assert result["total_sessions"] == 4
        assert result["total_rows"] == 5

    def test_session_bucketed_by_its_latest_row_not_earliest(self, tmp_path: Path) -> None:
        """A session whose activity straddles the split date lands in the
        window its MOST RECENT row falls in."""
        ledger = tmp_path / "_push_records.jsonl"
        _write_ledger(
            ledger,
            [
                _push("straddler", "2026-09-13T23:00:00Z"),
                _pull("straddler", "2026-09-14T01:00:00Z"),
            ],
        )

        result = harness.measure(
            [ledger],
            split_date=harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc),
        )

        assert result["windows"]["after"]["both"] == 1
        assert result["windows"]["before"]["both"] == 0

    def test_merges_two_ledger_files(self, tmp_path: Path) -> None:
        ledger_a = tmp_path / "a" / "_push_records.jsonl"
        ledger_b = tmp_path / "b" / "_push_records.jsonl"
        _write_ledger(ledger_a, [_push("s1", "2026-09-14T00:00:00Z")])
        _write_ledger(ledger_b, [_pull("s1", "2026-09-14T00:01:00Z")])

        result = harness.measure(
            [ledger_a, ledger_b],
            split_date=harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc),
        )

        assert result["windows"]["after"]["both"] == 1
        assert result["ledgers_read"] == {str(ledger_a): 1, str(ledger_b): 1}

    def test_a_missing_ledger_is_reported_not_raised(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist" / "_push_records.jsonl"

        result = harness.measure(
            [missing], split_date=harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc)
        )

        assert result["ledgers_read"] == {str(missing): 0}
        assert result["total_rows"] == 0

    def test_a_torn_trailing_line_is_skipped_not_raised(self, tmp_path: Path) -> None:
        ledger = tmp_path / "_push_records.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(_push("s1", "2026-09-14T00:00:00Z")) + "\n")
            fh.write('{"v": 1, "session_id": "torn"')

        result = harness.measure(
            [ledger], split_date=harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc)
        )

        assert result["total_rows"] == 1


class TestRenderMarkdown:
    def test_report_states_which_ledger_files_were_read(self, tmp_path: Path) -> None:
        """AC4's explicit wording: the report must STATE which ledger files
        it read, not just report aggregate counts."""
        ledger = tmp_path / "cache" / "_push_records.jsonl"
        _write_ledger(ledger, [_push("s1", "2026-09-14T00:00:00Z")])
        split_date = harness.datetime(2026, 9, 14, tzinfo=harness.timezone.utc)

        result = harness.measure([ledger], split_date=split_date)
        report = harness.render_markdown(result, split_date=split_date, generated_at=split_date)

        assert str(ledger) in report
        assert "both" in report
        assert "hook-only" in report
        assert "pull-only" in report
        assert "unknown-only" in report


class TestMainCli:
    def test_main_prints_a_report_to_stdout(self, tmp_path: Path, capsys) -> None:
        ledger = tmp_path / "_push_records.jsonl"
        _write_ledger(ledger, [_push("s1", "2026-09-14T00:00:00Z")])

        rc = harness.main(["--ledger", str(ledger), "--split-date", "2026-09-14"])

        assert rc == 0
        out = capsys.readouterr().out
        assert str(ledger) in out
        assert "Session shapes" in out
