# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``promptwitness`` console script (athenaeum#2147)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptwitness import canon, cli, ledger


class TestBuildParser:
    def test_status_subcommand_registered(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"
        assert args.harness == "claude-code"

    def test_status_accepts_harness_and_dir_overrides(self, tmp_path: Path) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["status", "--harness", "codex", "--dir", str(tmp_path)])
        assert args.harness == "codex"
        assert args.dir == str(tmp_path)

    def test_no_subcommand_has_no_func(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([])
        assert getattr(args, "func", None) is None


class TestStatusCommand:
    def test_status_on_empty_ledger(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["status", "--dir", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["seq"] is None
        assert payload["chain_ok"] is True
        assert payload["partitions"] == []
        assert payload["key_strength"] == "none"

    def test_status_reports_head_seq_and_chain_ok(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for i in range(3):
            ledger.append_line(
                tmp_path,
                kind="turn",
                harness="claude-code",
                harness_version="1",
                hook_version="promptwitness 0.1.0",
                session="s1",
                turn=i,
                prompt_id=f"p{i}",
                prompt_kind="human",
                channel="interactive",
            )
        rc = cli.main(["status", "--dir", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["seq"] == 3
        assert payload["chain_ok"] is True
        assert payload["partitions"] == ledger.list_partitions(tmp_path)

    def test_status_never_prints_digests(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=0,
            prompt_id="p0",
            prompt_kind="human",
            prompt_digest=canon.digest(canon.normalize("super secret prompt text")),
            raw_digest=canon.digest("super secret prompt text"),
            channel="interactive",
        )
        rc = cli.main(["status", "--dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "sha256:" not in out
        payload = json.loads(out)
        assert set(payload.keys()) == {
            "ledger_dir",
            "seq",
            "chain_ok",
            "first_break_seq",
            "partitions",
            "key_strength",
        }


class TestAnchorCommandParsing:
    """Parser-level coverage for ``anchor`` (athenaeum#2151). Behavioral
    coverage of the adapters themselves lives in
    ``tests/test_promptwitness_anchor.py`` -- this file only exercises the
    CLI wiring: argument parsing, ledger-line production, and the
    empty-ledger error path. ``opentimestamps`` is genuinely not installed
    in this environment, so every test below sees the OpenTimestamps pass
    degrade to ``status: "failed"`` -- exactly the mandatory-fallback AC
    these tests pin: ``local-clock`` still anchors regardless.
    """

    def test_anchor_subcommand_registered(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["anchor", "--subject", "x"])
        assert args.command == "anchor"
        assert args.subject == "x"
        assert args.upgrade is False
        assert args.verify is False
        assert args.block_source is None
        assert args.timeout == 10.0


class TestAnchorCommand:
    def test_anchor_on_an_empty_ledger_without_subject_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["anchor", "--dir", str(tmp_path)])
        assert rc == 1
        err = json.loads(capsys.readouterr().err)
        assert "error" in err

    def test_anchor_with_explicit_subject_always_writes_a_local_clock_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "my-subject"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        ops = {r["op"]: r for r in payload["results"]}
        local_result = next(
            r for r in payload["results"] if r["attestation"]["source"] == "local-clock"
        )
        assert local_result["attestation"]["status"] == "complete"
        ots_result = next(
            r for r in payload["results"] if r["attestation"]["source"] == "opentimestamps"
        )
        assert ots_result["attestation"]["status"] == "failed"  # opentimestamps not installed
        assert "verify" not in ops  # --verify was not passed

        lines = list(ledger.iter_lines(tmp_path))
        assert [line.kind for line in lines] == ["anchor", "anchor"]
        assert ledger.verify_chain(tmp_path).ok

    def test_anchor_defaults_to_the_chain_head_subject(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        head = ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=0,
            prompt_id="p0",
            prompt_kind="human",
            channel="interactive",
        )
        rc = cli.main(["anchor", "--dir", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        expected_subject = f"witness:{head.ledger_id}:{head.seq}"
        assert all(r["subject"] == expected_subject for r in payload["results"])

    def test_verify_flag_adds_a_verify_result_without_an_extra_ledger_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = [
            "anchor",
            "--dir",
            str(tmp_path),
            "--subject",
            "x",
            "--verify",
            "--block-source",
            "offline",
        ]
        rc = cli.main(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        verify_results = [r for r in payload["results"] if r.get("op") == "verify"]
        assert len(verify_results) == 1
        assert verify_results[0]["verify_result"]["verify_source"] == "offline"

        # verify never appends a ledger line of its own -- still exactly two
        # ("anchor" x2: local-clock + opentimestamps).
        lines = list(ledger.iter_lines(tmp_path))
        assert [line.kind for line in lines] == ["anchor", "anchor"]

    def test_upgrade_flag_is_a_safe_no_op_with_no_prior_ots_attestation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x", "--upgrade"])
        assert rc == 0
        assert ledger.verify_chain(tmp_path).ok

    def test_repeated_anchor_calls_each_append_their_own_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for _ in range(2):
            rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x"])
            assert rc == 0
            capsys.readouterr()
        lines = list(ledger.iter_lines(tmp_path))
        assert len(lines) == 4
        assert ledger.verify_chain(tmp_path).ok
