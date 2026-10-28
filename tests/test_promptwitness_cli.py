# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``promptwitness`` console script (athenaeum#2147,
athenaeum#2150, athenaeum#2152).

The ``keygen --provider os-keystore`` path is exercised end to end in
``tests/test_promptwitness_os_keystore.py``, which already owns the fake
keystore; what lives here is the parser shape.
"""

from __future__ import annotations

import json
import stat
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
