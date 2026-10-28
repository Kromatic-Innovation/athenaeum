# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``promptwitness`` console script (athenaeum#2147).

The ``TestUpgradeFindingsRegression`` and ``TestDigestForSubjectInvariant``
classes near the bottom are regression coverage for PR athenaeum#2168 review
findings #1 and #2 (athenaeum#2151) -- see each test's docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptwitness import anchor as anchor_mod
from promptwitness import canon, cli, ledger
from tests.test_promptwitness_anchor import _install_fake_opentimestamps


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


class TestDigestForSubjectInvariant:
    """Regression coverage for PR athenaeum#2168 review finding #1 (athenaeum#2151):
    a subject's digest must be a pure function of the subject, never of
    when it was resolved.
    """

    def test_digest_for_an_explicit_witness_subject_matches_the_lines_own_hash(
        self, tmp_path: Path
    ) -> None:
        first = ledger.append_line(
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
        subject = f"witness:{first.ledger_id}:{first.seq}"
        digest = cli._digest_for_subject(tmp_path, subject)

        assert digest == first.line_hash()
        # The failure mode this guards: hashing the subject STRING instead
        # of resolving the line it names produces a different value.
        assert digest != canon.digest(subject)

    def test_a_subjects_digest_does_not_change_once_the_chain_head_moves_past_it(
        self, tmp_path: Path
    ) -> None:
        first = ledger.append_line(
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
        subject = f"witness:{first.ledger_id}:{first.seq}"
        digest_while_head = cli._digest_for_subject(tmp_path, subject)
        assert digest_while_head == first.line_hash()

        for i in range(1, 4):
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

        digest_after_head_moved = cli._digest_for_subject(tmp_path, subject)
        assert digest_after_head_moved == digest_while_head

    def test_a_subject_with_no_matching_ledger_line_falls_back_to_a_string_hash(
        self, tmp_path: Path
    ) -> None:
        digest = cli._digest_for_subject(tmp_path, "witness:pw-nonexistent:999")
        assert digest == canon.digest("witness:pw-nonexistent:999")

    def test_an_arbitrary_label_subject_falls_back_to_a_string_hash(self, tmp_path: Path) -> None:
        digest = cli._digest_for_subject(tmp_path, "my-custom-subject")
        assert digest == canon.digest("my-custom-subject")


class TestFindLatestPendingOtsAttestation:
    """Unit coverage for the helper :func:`_find_latest_pending_ots_attestation`
    introduces, used by ``--upgrade`` with no ``--subject``."""

    def test_finds_a_pending_subject_even_after_the_chain_head_moved_past_it(
        self, tmp_path: Path
    ) -> None:
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            extra={
                "subject": "witness:pw-x:1",
                "digest": "sha256:" + "aa" * 32,
                "op": "anchor",
                "attestation": {
                    "source": "opentimestamps",
                    "status": "pending",
                    "claimed_time": "t",
                    "proof_path": "anchors/w.ots",
                    "detail": {},
                },
            },
        )
        # Unrelated later activity moves the chain head away from that line.
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
            channel="interactive",
        )

        found = cli._find_latest_pending_ots_attestation(tmp_path)
        assert found is not None
        subject, digest, att = found
        assert subject == "witness:pw-x:1"
        assert digest == "sha256:" + "aa" * 32
        assert att.status == "pending"

        # The failure mode this guards: resolving from the (now different)
        # chain head instead would name an entirely different subject.
        head_subject, _ = cli._resolve_subject_and_digest(tmp_path, None)
        assert head_subject != subject

    def test_a_later_complete_line_for_the_same_subject_clears_it_from_consideration(
        self, tmp_path: Path
    ) -> None:
        for status in ("pending", "complete"):
            ledger.append_line(
                tmp_path,
                kind="anchor",
                harness="claude-code",
                harness_version="",
                hook_version="promptwitness 0.1.0",
                extra={
                    "subject": "witness:pw-x:1",
                    "digest": "sha256:" + "aa" * 32,
                    "op": "anchor",
                    "attestation": {
                        "source": "opentimestamps",
                        "status": status,
                        "claimed_time": "t",
                        "proof_path": "anchors/w.ots",
                        "detail": {},
                    },
                },
            )
        assert cli._find_latest_pending_ots_attestation(tmp_path) is None

    def test_returns_none_when_nothing_is_pending(self, tmp_path: Path) -> None:
        assert cli._find_latest_pending_ots_attestation(tmp_path) is None


class TestUpgradeFindingsRegression:
    """CLI-level regression coverage for PR athenaeum#2168 review findings #1 and #2."""

    def test_upgrade_without_subject_targets_the_pending_subject_not_the_moved_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Finding #1: before the fix, ``--upgrade`` with no ``--subject``
        re-derived the subject from the CURRENT chain head -- which
        anchoring itself had already moved past "alpha" -- so it targeted a
        subject that was never anchored and could never upgrade.
        """
        ns = _install_fake_opentimestamps(monkeypatch)
        url = anchor_mod.DEFAULT_CALENDARS[0]
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "alpha"])
        assert rc == 0
        capsys.readouterr()

        # Unrelated activity moves the chain head away from "alpha".
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
            channel="interactive",
        )

        alpha_att = cli._find_latest_ots_attestation(tmp_path, "alpha")
        assert alpha_att is not None
        assert alpha_att.status == "pending"
        key = (url, alpha_att.detail["commitment_hex"])
        ns.get_timestamp_responses[key] = lambda commitment: ns.Timestamp(
            commitment, attestations={ns.PendingAttestation()}
        )

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--upgrade"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        ots_result = next(
            r for r in payload["results"] if r["attestation"]["source"] == "opentimestamps"
        )
        assert ots_result["subject"] == "alpha"
        assert ots_result["op"] == "upgrade"

    def test_upgrade_against_a_complete_attestation_is_a_no_op_and_never_resubmits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Finding #2: ``--upgrade`` against an already-``complete``
        attestation must report it unchanged, never re-submit. No calendar
        is stubbed here at all -- if the no-op guard did not fire, the
        fallthrough ``ots.anchor()`` call would hit a real (guard-blocked)
        network attempt and degrade to ``status: "failed"``, which is the
        discriminating signal a weaker assertion (e.g. "did not raise")
        would miss.
        """
        subject = "beta"
        complete_detail = {
            "calendar": "https://a.pool.opentimestamps.org",
            "commitment_hex": "bb" * 32,
            "digest_hex": "bb" * 32,
        }
        complete_att = {
            "source": "opentimestamps",
            "status": "complete",
            "claimed_time": "2026-10-28T00:00:00.000Z",
            "proof_path": "anchors/beta.ots",
            "detail": complete_detail,
        }
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            extra={
                "subject": subject,
                "digest": "sha256:" + "bb" * 32,
                "op": "anchor",
                "attestation": complete_att,
            },
        )

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", subject, "--upgrade"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        ots_result = next(
            r for r in payload["results"] if r["attestation"]["source"] == "opentimestamps"
        )
        assert ots_result["op"] == "upgrade"
        assert ots_result["attestation"]["status"] == "complete"
        assert ots_result["attestation"]["proof_path"] == "anchors/beta.ots"
        assert ots_result["attestation"]["detail"] == complete_detail
