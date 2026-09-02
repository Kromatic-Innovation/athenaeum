# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum measure {shadow-linkage,backlog-price,ordinary-night}``
(issue athenaeum#713)."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from athenaeum._cmd_measure import add_measure_subparser
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


def _write_page(wiki_root: Path, filename: str, body: str) -> None:
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / filename).write_text(
        f"---\nname: {filename[:-3]}\ntype: concept\n---\n{body}\n", encoding="utf-8"
    )


class TestMeasureDispatch:
    def test_bare_measure_prints_usage(self) -> None:
        rc, _out, err = _run_capture_stderr(["measure"])
        assert rc == 2
        assert "shadow-linkage" in err


class TestShadowLinkageCli:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_page(knowledge_root / "wiki", "a.md", "hello world alpha")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--dry-run",
            ]
        )
        assert rc == 0
        assert "dry run" in out
        assert not docs_path.exists()

    def test_writes_snapshot_and_json_output(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_page(knowledge_root / "wiki", "a.md", "hello world alpha")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["candidate_file_count"] == 1
        assert docs_path.is_file()

    def test_empty_wiki_refuses_write_and_exits_nonzero(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, _out, err = _run_capture_stderr(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
            ]
        )
        assert rc == 1
        assert "error" in err
        assert not docs_path.exists()


class TestBacklogPriceCli:
    def test_dry_run_and_json(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        raw_dir = knowledge_root / "raw" / "s"
        raw_dir.mkdir(parents=True)
        (raw_dir / "20260801T000000Z-aaaaaaaa.md").write_text("x")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "backlog-price",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--dry-run",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["backlog_count"] == 1
        assert not docs_path.exists()

    def test_empty_backlog_refuses_write(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, _out, err = _run_capture_stderr(
            [
                "measure",
                "backlog-price",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        assert rc == 1
        assert "error" in err

    def test_operator_supplied_overrides_flow_through(self, tmp_path: Path) -> None:
        """Issue athenaeum#1095 AC3: --backlog-count/--calls-per-file/
        --wall-clock-per-file-seconds are threaded from the CLI into
        build_price_sheet, and the snapshot records operator-supplied
        provenance for each."""
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "backlog-price",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backlog-count",
                "500",
                "--calls-per-file",
                "3.5",
                "--wall-clock-per-file-seconds",
                "1.5",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["backlog_count"] == 500
        assert payload["backlog_count_source"] == "operator-supplied"
        assert payload["calls_per_file"] == 3.5
        assert payload["calls_per_file_source"] == "operator-supplied"
        assert payload["wall_clock_per_file_seconds"] == 1.5
        assert payload["wall_clock_source"] == "operator-supplied"


class TestOrdinaryNightCli:
    def test_writes_snapshot_even_when_indeterminate(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "ordinary-night",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["verdict"] == "indeterminate"
        assert docs_path.is_file()

    def test_comparator_pair_count_amortization_flows_through(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "ordinary-night",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--comparator-pair-count",
                "70",
                "--comparator-amortization-nights",
                "7",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["amortized"]["comparator_pairs_per_night"] == 10.0

    def test_operator_supplied_overrides_flow_through(self, tmp_path: Path) -> None:
        """Issue athenaeum#1095 AC5: --calls-per-file/--files-per-day/
        --wall-clock-per-file-seconds are threaded from the CLI into
        build_ordinary_night_table, and the snapshot records
        operator-supplied provenance for each."""
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "ordinary-night",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--calls-per-file",
                "2.0",
                "--files-per-day",
                "5.0",
                "--wall-clock-per-file-seconds",
                "1.0",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["calls_per_file"] == 2.0
        assert payload["calls_per_file_source"] == "operator-supplied"
        assert payload["files_per_day"] == 5.0
        assert payload["files_per_day_source"] == "operator-supplied"
        assert payload["wall_clock_per_file_seconds"] == 1.0
        assert payload["wall_clock_source"] == "operator-supplied"


class TestOverrideArgConsolidation:
    """Issue athenaeum#1285: --cache-dir/--summary-log/--calls-per-file/
    --wall-clock-per-file-seconds are declared once each (via
    ``_add_cache_dir``/``_add_summary_log``/``_add_override_arg`` in
    ``_cmd_measure.py``) and shared by both ``backlog-price`` and
    ``ordinary-night`` instead of being copy-pasted per subcommand. Build
    the real parsers and pin: both subcommands still accept every override
    flag, and the two blocks that were byte-for-byte duplicates
    (``--cache-dir``, ``--summary-log``) now render identical help text on
    both subcommands (previously ``ordinary-night``'s copies had no/stub
    help text — see PR body for that intentional pick)."""

    @staticmethod
    def _measure_subparsers() -> dict[str, argparse.ArgumentParser]:
        top = argparse.ArgumentParser()
        subparsers = top.add_subparsers(dest="cmd")
        add_measure_subparser(subparsers)
        measure_action = next(
            a for a in top._actions if isinstance(a, argparse._SubParsersAction)
        )
        measure_parser = measure_action.choices["measure"]
        measure_sub_action = next(
            a for a in measure_parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        return dict(measure_sub_action.choices)

    def _action(self, parser: argparse.ArgumentParser, flag: str) -> argparse.Action:
        return next(a for a in parser._actions if flag in a.option_strings)

    def test_both_subcommands_accept_every_shared_override_flag(self) -> None:
        subs = self._measure_subparsers()
        price_p, night_p = subs["backlog-price"], subs["ordinary-night"]
        for flag in ("--cache-dir", "--summary-log", "--calls-per-file"):
            assert self._action(price_p, flag) is not None
            assert self._action(night_p, flag) is not None
        # wall-clock-per-file-seconds is shared by all three commands that take it
        assert self._action(price_p, "--wall-clock-per-file-seconds") is not None
        assert self._action(night_p, "--wall-clock-per-file-seconds") is not None
        # override args unique to one subcommand still exist, unaffected
        assert self._action(price_p, "--backlog-count") is not None
        assert self._action(night_p, "--files-per-day") is not None

    def test_shared_blocks_render_identical_help_across_subcommands(self) -> None:
        # --cache-dir and --summary-log carry no per-issue AC reference, so
        # these two are byte-identical across both subcommands.
        subs = self._measure_subparsers()
        price_p, night_p = subs["backlog-price"], subs["ordinary-night"]
        for flag in ("--cache-dir", "--summary-log"):
            price_help = self._action(price_p, flag).help
            night_help = self._action(night_p, flag).help
            assert price_help == night_help, flag

    def test_calls_per_file_and_wall_clock_help_match_modulo_ac_reference(self) -> None:
        # --calls-per-file and --wall-clock-per-file-seconds each cite a
        # different AC per subcommand (AC3(b)/AC3(c) vs AC5, issue
        # athenaeum#1095) by design — everything else in the shared
        # template must still match.
        subs = self._measure_subparsers()
        price_p, night_p = subs["backlog-price"], subs["ordinary-night"]
        price_calls = self._action(price_p, "--calls-per-file").help
        night_calls = self._action(night_p, "--calls-per-file").help
        assert price_calls.replace("AC3(b)", "AC5") == night_calls

        price_wc = self._action(price_p, "--wall-clock-per-file-seconds").help
        night_wc = self._action(night_p, "--wall-clock-per-file-seconds").help
        assert price_wc.replace("AC3(c)", "AC5") == night_wc

    def test_all_defaults_and_types_unchanged(self) -> None:
        subs = self._measure_subparsers()
        price_p, night_p = subs["backlog-price"], subs["ordinary-night"]
        for parser, flag, expected_type in [
            (price_p, "--backlog-count", int),
            (price_p, "--calls-per-file", float),
            (price_p, "--wall-clock-per-file-seconds", float),
            (night_p, "--calls-per-file", float),
            (night_p, "--files-per-day", float),
            (night_p, "--wall-clock-per-file-seconds", float),
        ]:
            action = self._action(parser, flag)
            assert action.default is None, flag
            assert action.type is expected_type, flag
