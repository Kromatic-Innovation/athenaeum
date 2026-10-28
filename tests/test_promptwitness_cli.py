# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``promptwitness`` console script (athenaeum#2147,
athenaeum#2149, athenaeum#2150, athenaeum#2151, athenaeum#2152).

The ``keygen --provider os-keystore`` path is exercised end to end in
``tests/test_promptwitness_os_keystore.py``, which already owns the fake
keystore; what lives here is the parser shape.

The ``TestUpgradeFindingsRegression`` and ``TestDigestForSubjectInvariant``
classes near the bottom are regression coverage for PR athenaeum#2168 review
findings 1 and 2 (athenaeum#2151) -- see each test's docstring.
"""

from __future__ import annotations

import json
import stat
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

    def test_keygen_subcommand_registered(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["keygen"])
        assert args.command == "keygen"
        assert args.harness == "claude-code"
        assert args.key_path is None
        assert args.force is False

    def test_keygen_defaults_to_software_file(self) -> None:
        """The default must stay ``software-file``: athenaeum#2152 adds a
        tier, it does not change where an existing operator's key lives."""
        args = cli.build_parser().parse_args(["keygen"])
        assert args.provider == "software-file"
        assert args.keystore_backend is None

    def test_keygen_accepts_os_keystore_provider(self) -> None:
        args = cli.build_parser().parse_args(["keygen", "--provider", "os-keystore"])
        assert args.provider == "os-keystore"

    @pytest.mark.parametrize(
        "backend", ["macos-keychain", "secret-service", "windows-dpapi"]
    )
    def test_keygen_accepts_every_keystore_backend(self, backend: str) -> None:
        args = cli.build_parser().parse_args(
            ["keygen", "--provider", "os-keystore", "--keystore-backend", backend]
        )
        assert args.keystore_backend == backend

    def test_keygen_rejects_an_unknown_provider(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["keygen", "--provider", "hardware-presence"])

    def test_keygen_rejects_an_unknown_keystore_backend(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(
                ["keygen", "--provider", "os-keystore", "--keystore-backend", "keychain"]
            )

    def test_keygen_accepts_key_path_and_force(self, tmp_path: Path) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["keygen", "--key-path", str(tmp_path / "k.pem"), "--force"])
        assert args.key_path == str(tmp_path / "k.pem")
        assert args.force is True

    def test_no_subcommand_has_no_func(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([])
        assert getattr(args, "func", None) is None


class TestKeygenCommand:
    def test_generates_a_key_at_the_given_path(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        rc = cli.main(["keygen", "--key-path", str(key_path)])
        assert rc == 0
        assert key_path.exists()
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    def test_prints_key_id_and_path_not_material(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_path = tmp_path / "k.pem"
        rc = cli.main(["keygen", "--key-path", str(key_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["key_path"] == str(key_path)
        assert payload["algorithm"] == "ed25519"
        assert payload["strength"] == "software-file"
        assert payload["key_id"].startswith("ed25519:")
        assert "BEGIN" not in json.dumps(payload)

    def test_refuses_to_overwrite_without_force(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key_path = tmp_path / "k.pem"
        assert cli.main(["keygen", "--key-path", str(key_path)]) == 0
        capsys.readouterr()
        rc = cli.main(["keygen", "--key-path", str(key_path)])
        assert rc == 1
        err_payload = json.loads(capsys.readouterr().err)
        assert "error" in err_payload

    def test_force_overwrites_an_existing_key(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        assert cli.main(["keygen", "--key-path", str(key_path)]) == 0
        first_bytes = key_path.read_bytes()
        assert cli.main(["keygen", "--key-path", str(key_path), "--force"]) == 0
        assert key_path.read_bytes() != first_bytes


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


# ---------------------------------------------------------------------------
# athenaeum#2149 AC — `promptwitness hook claude-code --event session-end`
# ---------------------------------------------------------------------------


class TestHookClaudeCodeSubcommand:
    def test_registered_with_required_event_choice(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["hook", "claude-code", "--event", "session-end"])
        assert args.command == "hook"
        assert args.harness_command == "claude-code"
        assert args.event == "session-end"

    def test_unrecognized_event_is_rejected_by_argparse(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["hook", "claude-code", "--event", "user-prompt-submit"])

    def test_session_end_writes_a_session_close_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cli-sess")
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="cli-sess",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            channel="interactive",
        )

        rc = cli.main(["hook", "claude-code", "--event", "session-end"])

        assert rc == 0
        lines = list(ledger.iter_lines(tmp_path))
        close_lines = [ln for ln in lines if ln.kind == "session-close"]
        assert len(close_lines) == 1
        assert close_lines[0].session == "cli-sess"
        assert close_lines[0].turns == 1
        assert close_lines[0].turns_witnessed == 1

    def test_session_end_with_no_session_id_env_is_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        rc = cli.main(["hook", "claude-code", "--event", "session-end"])

        assert rc == 0
        assert ledger.list_partitions(tmp_path) == []


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


# ---------------------------------------------------------------------------
# verify / demo (athenaeum#2155)
# ---------------------------------------------------------------------------


class TestVerifySubcommand:
    def test_parser_requires_transcript_and_ledger(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["verify"])

    def test_parses_json_flag(self, tmp_path: Path) -> None:
        args = cli.build_parser().parse_args(
            ["verify", "--transcript", "t.json", "--ledger", str(tmp_path), "--json"]
        )
        assert args.command == "verify"
        assert args.json is True

    def _write_transcript(self, path: Path, records: list[dict]) -> None:
        path.write_text(json.dumps(records), encoding="utf-8")

    def test_exit_0_when_every_human_turn_witnessed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            prompt_digest=canon.digest(canon.normalize("hello")),
            raw_digest=canon.digest("hello"),
            prompt_chars=5,
            channel="interactive",
        )
        transcript = tmp_path / "t.json"
        self._write_transcript(
            transcript, [{"role": "human", "text": "hello", "session": "s1", "prompt_id": "p1"}]
        )
        rc = cli.main(["verify", "--transcript", str(transcript), "--ledger", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "witnessed" in out

    def test_exit_1_when_a_human_turn_is_not_witnessed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = tmp_path / "t.json"
        self._write_transcript(
            transcript, [{"role": "human", "text": "ghost", "session": "s1", "prompt_id": "p1"}]
        )
        rc = cli.main(["verify", "--transcript", str(transcript), "--ledger", str(tmp_path)])
        assert rc == 1

    def test_json_output_shape(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = tmp_path / "t.json"
        self._write_transcript(
            transcript, [{"role": "machine", "text": "<task-notification>x</task-notification>"}]
        )
        rc = cli.main(
            ["verify", "--transcript", str(transcript), "--ledger", str(tmp_path), "--json"]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["verdicts"][0]["verdict"] == "machine-authored"


class TestDemoSubcommand:
    def test_parser_defaults(self, tmp_path: Path) -> None:
        args = cli.build_parser().parse_args(["demo"])
        assert args.command == "demo"
        assert args.synthetic is False
        assert args.transcript is None
        assert args.out == "demo.html"

    def test_synthetic_mode_writes_html_with_no_script_tag(self, tmp_path: Path) -> None:
        out = tmp_path / "demo.html"
        rc = cli.main(["demo", "--out", str(out)])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "<script" not in text.lower()
        assert "Verified transcript" in text
        assert "Tampered transcript" in text

    def test_real_mode_requires_ledger(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = tmp_path / "t.json"
        transcript.write_text(json.dumps([{"role": "human", "text": "hi"}]), encoding="utf-8")
        rc = cli.main(["demo", "--transcript", str(transcript), "--out", str(tmp_path / "d.html")])
        assert rc == 2

    def test_synthetic_and_transcript_are_mutually_exclusive(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.json"
        transcript.write_text(json.dumps([{"role": "human", "text": "hi"}]), encoding="utf-8")
        rc = cli.main(
            [
                "demo",
                "--synthetic",
                "--transcript",
                str(transcript),
                "--ledger",
                str(tmp_path),
                "--out",
                str(tmp_path / "d.html"),
            ]
        )
        assert rc == 2

    def test_real_mode_writes_mode_0600_and_warns_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            prompt_digest=canon.digest(canon.normalize("hi")),
            raw_digest=canon.digest("hi"),
            prompt_chars=2,
            channel="interactive",
        )
        transcript = tmp_path / "t.json"
        transcript.write_text(
            json.dumps([{"role": "human", "text": "hi", "session": "s1", "prompt_id": "p1"}]),
            encoding="utf-8",
        )
        out = tmp_path / "real-demo.html"
        rc = cli.main(
            [
                "demo",
                "--transcript",
                str(transcript),
                "--ledger",
                str(tmp_path),
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        err = capsys.readouterr().err
        assert "do not publish" in err.lower()
        assert "real" in err.lower()
