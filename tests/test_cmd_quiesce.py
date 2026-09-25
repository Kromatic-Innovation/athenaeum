# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum quiesce`` (issue athenaeum#1898).

Covers the CLI wiring over :mod:`athenaeum.quiesce`: the three mutually
exclusive modes (set/release/status), the JSON output shape, the clear
rejection when ``--for`` exceeds the configured maximum, and the mutual
exclusivity guards. Library-level behavior (write/read/release semantics,
expiry) is covered in ``tests/test_quiesce.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.quiesce import quiesce_path


class TestSetMode:
    def test_sets_a_sentinel_and_prints_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "quiesce",
                "--for",
                "2h",
                "--reason",
                "backfilling",
                "--path",
                str(tmp_path),
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["command"] == "quiesce"
        assert payload["action"] == "set"
        assert payload["reason"] == "backfilling"
        assert "@" in payload["holder"]  # auto-derived <user>@<hostname>
        assert quiesce_path(tmp_path).is_file()

    def test_explicit_holder_is_honored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "quiesce",
                "--for",
                "1h",
                "--reason",
                "r",
                "--holder",
                "hestia-lane-42",
                "--path",
                str(tmp_path),
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["holder"] == "hestia-lane-42"

    @pytest.mark.parametrize(
        "raw,expected_hours",
        [("2h", 2), ("90m", 1.5), ("3600s", 1), ("3", 3)],
    )
    def test_duration_units_are_parsed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        raw: str,
        expected_hours: float,
    ) -> None:
        rc = main(
            ["quiesce", "--for", raw, "--reason", "r", "--path", str(tmp_path)]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        from datetime import datetime

        created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        assert (expires - created).total_seconds() == pytest.approx(
            expected_hours * 3600
        )

    def test_invalid_duration_is_rejected_at_parse_time(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "quiesce",
                    "--for",
                    "not-a-duration",
                    "--reason",
                    "r",
                    "--path",
                    str(tmp_path),
                ]
            )
        assert "invalid --for" in capsys.readouterr().err

    def test_duration_over_max_is_rejected_with_clear_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # AC4: a --for longer than the configured maximum (default 6h) is
        # rejected with a clear error, not silently clamped.
        rc = main(
            ["quiesce", "--for", "7h", "--reason", "r", "--path", str(tmp_path)]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "6h" in err
        assert not quiesce_path(tmp_path).exists()

    def test_configured_max_is_respected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "librarian:\n  quiesce:\n    max_hours: 1\n"
        )
        rc = main(
            ["quiesce", "--for", "2h", "--reason", "r", "--path", str(tmp_path)]
        )
        assert rc == 1
        assert "1h" in capsys.readouterr().err

    def test_missing_reason_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["quiesce", "--for", "2h", "--path", str(tmp_path)])
        assert rc == 1
        assert "error:" in capsys.readouterr().err

    def test_missing_for_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["quiesce", "--reason", "r", "--path", str(tmp_path)])
        assert rc == 1
        assert "error:" in capsys.readouterr().err


class TestReleaseMode:
    def test_releases_an_active_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["quiesce", "--for", "1h", "--reason", "r", "--path", str(tmp_path)])
        capsys.readouterr()

        rc = main(["quiesce", "--release", "--path", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {
            "command": "quiesce",
            "action": "release",
            "released": True,
        }
        assert not quiesce_path(tmp_path).exists()

    def test_release_on_absent_sentinel_is_a_successful_no_op(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["quiesce", "--release", "--path", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["released"] is False

    def test_release_combined_with_for_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "quiesce",
                "--release",
                "--for",
                "1h",
                "--reason",
                "r",
                "--path",
                str(tmp_path),
            ]
        )
        assert rc == 1
        assert "error:" in capsys.readouterr().err


class TestStatusMode:
    def test_status_with_no_sentinel_reports_inactive(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["quiesce", "--status", "--path", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {"command": "quiesce", "action": "status", "active": False}

    def test_status_with_active_sentinel_reports_holder_and_expiry(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(
            [
                "quiesce",
                "--for",
                "2h",
                "--reason",
                "backfilling",
                "--holder",
                "alice",
                "--path",
                str(tmp_path),
            ]
        )
        capsys.readouterr()

        rc = main(["quiesce", "--status", "--path", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["active"] is True
        assert payload["holder"] == "alice"
        assert payload["reason"] == "backfilling"
        assert isinstance(payload["created_at"], str)
        assert isinstance(payload["expires_at"], str)

    def test_status_never_writes_anything(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["quiesce", "--status", "--path", str(tmp_path)])
        capsys.readouterr()
        assert not quiesce_path(tmp_path).exists()

    def test_status_combined_with_release_is_rejected_by_argparse(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(["quiesce", "--status", "--release", "--path", str(tmp_path)])


class TestHelp:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["quiesce", "--help"])
        assert excinfo.value.code == 0
