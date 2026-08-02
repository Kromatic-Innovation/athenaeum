# SPDX-License-Identifier: Apache-2.0
"""Tests for the installed-metadata vs pyproject version-drift check (athenaeum#685)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.deploy_check import (
    EXIT_DRIFT,
    EXIT_IN_SYNC,
    EXIT_UNDETERMINED,
    DriftResult,
    VersionDriftError,
    check_version_drift,
    installed_version,
    main,
    pyproject_version,
)


def _write_pyproject(tree: Path, version: str | None) -> Path:
    tree.mkdir(parents=True, exist_ok=True)
    version_line = f'version = "{version}"\n' if version is not None else ""
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "athenaeum"\n' + version_line, encoding="utf-8"
    )
    return tree


class TestReaders:
    def test_installed_version_matches_metadata(self) -> None:
        # The installed dist has a real version (the dev extra installs it).
        assert installed_version("athenaeum")

    def test_installed_version_missing_dist_raises_loudly(self) -> None:
        with pytest.raises(VersionDriftError, match="metadata not found"):
            installed_version("no-such-athenaeum-dist-xyz")

    def test_pyproject_version_reads_declared(self, tmp_path: Path) -> None:
        tree = _write_pyproject(tmp_path, "1.2.3")
        assert pyproject_version(tree) == "1.2.3"

    def test_pyproject_missing_file_raises_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(VersionDriftError, match="cannot read"):
            pyproject_version(tmp_path)

    def test_pyproject_without_version_raises_loudly(self, tmp_path: Path) -> None:
        tree = _write_pyproject(tmp_path, None)
        with pytest.raises(VersionDriftError, match="no \\[project\\].version"):
            pyproject_version(tree)

    def test_pyproject_unparseable_raises_loudly(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "pyproject.toml").write_text("this is = = not toml\n")
        with pytest.raises(VersionDriftError, match="cannot parse"):
            pyproject_version(tmp_path)


class TestDrift:
    def test_in_sync_when_installed_equals_declared(self, tmp_path: Path) -> None:
        installed = installed_version("athenaeum")
        tree = _write_pyproject(tmp_path, installed)
        result = check_version_drift(tree)
        assert result == DriftResult(installed=installed, declared=installed)
        assert result.in_sync is True

    def test_drift_when_declared_differs(self, tmp_path: Path) -> None:
        installed = installed_version("athenaeum")
        tree = _write_pyproject(tmp_path, installed + "-bumped")
        result = check_version_drift(tree)
        assert result.in_sync is False
        assert result.installed == installed
        assert result.declared == installed + "-bumped"

    def test_check_propagates_undetermined(self, tmp_path: Path) -> None:
        # A missing pyproject must raise, not silently pass as in-sync — the
        # exact failure this check exists to prevent (AC4).
        with pytest.raises(VersionDriftError):
            check_version_drift(tmp_path)


class TestMain:
    def test_main_in_sync_exit_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        installed = installed_version("athenaeum")
        _write_pyproject(tmp_path, installed)
        assert main([str(tmp_path)]) == EXIT_IN_SYNC
        assert "in-sync" in capsys.readouterr().out

    def test_main_drift_exit_10(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        installed = installed_version("athenaeum")
        _write_pyproject(tmp_path, installed + "-bumped")
        assert main([str(tmp_path)]) == EXIT_DRIFT
        assert "drift" in capsys.readouterr().err

    def test_main_undetermined_exit_20(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Missing pyproject → loud UNDETERMINED, not a silent pass.
        assert main([str(tmp_path)]) == EXIT_UNDETERMINED
        assert "UNDETERMINED" in capsys.readouterr().err

    def test_main_undetermined_on_missing_dist(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_pyproject(tmp_path, "1.0.0")
        assert main([str(tmp_path), "--dist", "no-such-dist-xyz"]) == EXIT_UNDETERMINED
        assert "UNDETERMINED" in capsys.readouterr().err
