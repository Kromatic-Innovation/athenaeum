# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schedule install``/``remove`` (issue athenaeum#2304).

Covers every acceptance criterion:

- AC1: ``install`` generates the platform's unit with absolute paths and
  the key source handled, and invokes the platform loader; ``remove``
  unloads it and deletes everything ``install`` wrote. Every test here
  runs against a TEMPORARY home directory
  (:func:`athenaeum.schedule.install`'s ``home=`` injection) with the
  platform loader mocked (``launchctl``/``systemctl``/``crontab`` never
  actually invoked) -- host verification of a real scheduled tick is
  athenaeum#2366, out of scope here.
- AC2: the generated unit runs ``athenaeum run`` with an environment that
  resolves the key source without a login shell, asserted directly on the
  generated file CONTENTS for each platform.
- AC3 (counter-example): with no key source available, ``install`` refuses
  (raises :class:`athenaeum.schedule.NoKeySourceError` / CLI exit 1) and
  says why, writing nothing, instead of installing a schedule that would
  fail every night.
- AC4: ``athenaeum doctor`` reports both the scheduled and the unscheduled
  states (see ``tests/test_doctor.py``'s own ``schedule`` check tests for
  the doctor-level half of this).

Every fixture path is ``tmp_path``-derived (public-safe-lint: no home-
directory literals), and no fixture command references a real GitHub issue
number.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from athenaeum import schedule
from athenaeum.cli import main as cli_main


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _pin_llm_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here resolves the GLOBAL provider chain
    (:func:`athenaeum.provider.resolve_provider`) -- delete the env override
    unconditionally so an ambient ``ATHENAEUM_LLM_PROVIDER`` on the runner
    can't silently substitute for a test's own yaml/env setup (same hazard
    ``tests/test_doctor.py``'s ``_pin_llm_provider_env`` documents)."""
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATHENAEUM_CLAUDE_CLI_BIN", raising=False)


@dataclass
class FakeLoader:
    """Records every call and returns a configurable result (default: a
    clean success), so no real ``launchctl``/``systemctl``/``crontab``
    process is ever spawned."""

    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    crontab_body: str = ""

    def __call__(self, argv: list[str], input_text: str | None = None) -> schedule.LoaderResult:
        self.calls.append((argv, input_text))
        if argv[:2] == ["crontab", "-l"]:
            return schedule.LoaderResult(
                0 if self.crontab_body is not None else 1, self.crontab_body or "", ""
            )
        if argv[:1] == ["crontab"]:
            # `crontab -` -- record the new body as the current one.
            self.crontab_body = input_text or ""
            return schedule.LoaderResult(self.returncode, self.stdout, self.stderr)
        return schedule.LoaderResult(self.returncode, self.stdout, self.stderr)


# ---------------------------------------------------------------------------
# detect_platform (AC1)
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    def test_darwin_is_launchd(self) -> None:
        assert schedule.detect_platform(system="Darwin") == "launchd"

    def test_linux_with_systemctl_is_systemd(self) -> None:
        assert (
            schedule.detect_platform(system="Linux", systemctl_available=True) == "systemd"
        )

    def test_linux_without_systemctl_is_cron(self) -> None:
        assert schedule.detect_platform(system="Linux", systemctl_available=False) == "cron"

    def test_windows_is_unsupported(self) -> None:
        with pytest.raises(schedule.UnsupportedPlatformError):
            schedule.detect_platform(system="Windows")


# ---------------------------------------------------------------------------
# resolve_key_source (AC2 success paths + AC3 counter-example)
# ---------------------------------------------------------------------------


class TestResolveKeySource:
    def test_api_key_from_process_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
        source = schedule.resolve_key_source(None, tmp_path / "cache")
        assert source.kind == "api_key"
        assert source.env == {"ANTHROPIC_API_KEY": "sk-test-env"}

    def test_api_key_from_cached_config_env(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "config.env").write_text("ANTHROPIC_API_KEY=sk-cached\n", encoding="utf-8")
        source = schedule.resolve_key_source(None, cache_dir)
        assert source.kind == "api_key"
        assert source.env == {"ANTHROPIC_API_KEY": "sk-cached"}

    def test_claude_cli_provider_with_binary_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(fake_bin))
        config = {"llm": {"provider": "claude-cli"}}
        source = schedule.resolve_key_source(config, tmp_path / "cache")
        assert source.kind == "claude_cli"
        assert source.env == {"ATHENAEUM_CLAUDE_CLI_BIN": str(fake_bin.resolve())}

    def test_no_key_source_refuses(self, tmp_path: Path) -> None:
        """AC3 counter-example: no env key, no cached config.env, default
        `api` provider -- install must refuse, not schedule a job that
        fails every night."""
        with pytest.raises(schedule.NoKeySourceError, match="no key source"):
            schedule.resolve_key_source(None, tmp_path / "cache")

    def test_claude_cli_provider_with_missing_binary_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force a miss regardless of whether a real `claude` happens to be
        # on this test runner's own PATH (this container's harness IS
        # Claude Code, so it usually is).
        monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(tmp_path / "no-such-claude-binary"))
        config = {"llm": {"provider": "claude-cli"}}
        with pytest.raises(schedule.NoKeySourceError, match="claude-cli"):
            schedule.resolve_key_source(config, tmp_path / "cache")

    def test_invalid_provider_refuses(self, tmp_path: Path) -> None:
        config = {"llm": {"provider": "not-a-real-provider"}}
        with pytest.raises(schedule.NoKeySourceError):
            schedule.resolve_key_source(config, tmp_path / "cache")


# ---------------------------------------------------------------------------
# Generator content (AC2): absolute paths + key source embedded, per platform.
# ---------------------------------------------------------------------------


class TestGenerators:
    def test_launchd_plist_has_absolute_paths_and_key(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "venv" / "bin" / "athenaeum"
        knowledge_root = tmp_path / "knowledge"
        log_path = tmp_path / "log"
        key_source = schedule.KeySource(kind="api_key", env={"ANTHROPIC_API_KEY": "sk-abc"})
        content = schedule.generate_launchd_plist(
            bin_path, knowledge_root, key_source, hour=3, minute=0, log_path=log_path
        )
        assert str(bin_path) in content
        assert str(knowledge_root) in content
        assert "<key>ANTHROPIC_API_KEY</key>" in content
        assert "<string>sk-abc</string>" in content
        assert "<key>Label</key>" in content
        assert schedule.LAUNCHD_LABEL in content

    def test_launchd_plist_claude_cli_env(self, tmp_path: Path) -> None:
        key_source = schedule.KeySource(
            kind="claude_cli", env={"ATHENAEUM_CLAUDE_CLI_BIN": "/abs/path/claude"}
        )
        content = schedule.generate_launchd_plist(
            tmp_path / "athenaeum", tmp_path / "knowledge", key_source, hour=3, minute=0,
            log_path=tmp_path / "log",
        )
        assert "<key>ATHENAEUM_CLAUDE_CLI_BIN</key>" in content
        assert "<string>/abs/path/claude</string>" in content
        assert "ANTHROPIC_API_KEY" not in content

    def test_systemd_service_has_absolute_paths_and_key(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin" / "athenaeum"
        knowledge_root = tmp_path / "knowledge"
        key_source = schedule.KeySource(kind="api_key", env={"ANTHROPIC_API_KEY": "sk-abc"})
        content = schedule.generate_systemd_service(
            bin_path, knowledge_root, key_source, log_path=tmp_path / "log"
        )
        assert f"ExecStart={bin_path} run --path {knowledge_root}" in content
        assert "Environment=ANTHROPIC_API_KEY=sk-abc" in content

    def test_systemd_timer_calendar(self) -> None:
        content = schedule.generate_systemd_timer(hour=4, minute=30)
        assert "OnCalendar=*-*-* 04:30:00" in content

    def test_cron_line_has_absolute_paths_and_key(self, tmp_path: Path) -> None:
        bin_path = tmp_path / "bin" / "athenaeum"
        knowledge_root = tmp_path / "knowledge"
        key_source = schedule.KeySource(kind="api_key", env={"ANTHROPIC_API_KEY": "sk-abc"})
        line = schedule.generate_cron_line(
            bin_path, knowledge_root, key_source, hour=3, minute=0, log_path=tmp_path / "log"
        )
        assert line.startswith("0 3 * * * ANTHROPIC_API_KEY=sk-abc ")
        assert str(bin_path) in line
        assert str(knowledge_root) in line
        assert line.endswith(f"# {schedule.CRON_TAG}")


# ---------------------------------------------------------------------------
# install() / remove() / status() -- per platform, loader mocked (AC1).
# ---------------------------------------------------------------------------


def _systemd_paths(home_dir: Path) -> tuple[Path, Path]:
    unit_dir = home_dir / ".config" / "systemd" / "user"
    service = unit_dir / f"{schedule.SYSTEMD_UNIT_NAME}.service"
    timer = unit_dir / f"{schedule.SYSTEMD_UNIT_NAME}.timer"
    return service, timer


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    return d


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    d = tmp_path / "knowledge"
    d.mkdir()
    return d


@pytest.fixture
def api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")


class TestInstallLaunchd:
    def test_install_writes_plist_and_loads(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        result = schedule.install(
            knowledge_root, home=home, loader=loader, platform_override="launchd"
        )
        plist_path = home / "Library" / "LaunchAgents" / f"{schedule.LAUNCHD_LABEL}.plist"
        assert result.platform == "launchd"
        assert result.paths == (plist_path,)
        assert plist_path.is_file()
        assert "ANTHROPIC_API_KEY" in plist_path.read_text(encoding="utf-8")
        assert loader.calls == [(["launchctl", "load", "-w", str(plist_path)], None)]
        assert result.loader_returncode == 0

    def test_remove_unloads_and_deletes(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="launchd")
        plist_path = home / "Library" / "LaunchAgents" / f"{schedule.LAUNCHD_LABEL}.plist"
        assert plist_path.is_file()

        remove_result = schedule.remove(home=home, loader=loader, platform_override="launchd")
        assert remove_result.had_schedule is True
        assert remove_result.removed_paths == (plist_path,)
        assert not plist_path.exists()
        assert loader.calls[-1] == (["launchctl", "unload", str(plist_path)], None)

    def test_remove_with_nothing_installed_is_a_noop(self, home: Path) -> None:
        loader = FakeLoader()
        result = schedule.remove(home=home, loader=loader, platform_override="launchd")
        assert result.had_schedule is False
        assert result.removed_paths == ()
        assert loader.calls == []

    def test_status_reflects_install_and_remove(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        assert schedule.status(home=home, platform_override="launchd").scheduled is False
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="launchd")
        assert schedule.status(home=home, platform_override="launchd").scheduled is True
        schedule.remove(home=home, loader=loader, platform_override="launchd")
        assert schedule.status(home=home, platform_override="launchd").scheduled is False


class TestInstallSystemd:
    def test_install_writes_units_and_enables(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        result = schedule.install(
            knowledge_root, home=home, loader=loader, platform_override="systemd"
        )
        service_path, timer_path = _systemd_paths(home)
        assert result.paths == (service_path, timer_path)
        assert service_path.is_file() and timer_path.is_file()
        assert "ANTHROPIC_API_KEY=sk-test-not-real" in service_path.read_text(encoding="utf-8")
        timer_name = f"{schedule.SYSTEMD_UNIT_NAME}.timer"
        assert loader.calls == [
            (["systemctl", "--user", "daemon-reload"], None),
            (["systemctl", "--user", "enable", "--now", timer_name], None),
        ]

    def test_remove_disables_and_deletes_both_units(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="systemd")
        service_path, timer_path = _systemd_paths(home)

        result = schedule.remove(home=home, loader=loader, platform_override="systemd")
        assert result.had_schedule is True
        assert set(result.removed_paths) == {service_path, timer_path}
        assert not service_path.exists()
        assert not timer_path.exists()
        assert (
            ["systemctl", "--user", "disable", "--now", f"{schedule.SYSTEMD_UNIT_NAME}.timer"],
            None,
        ) in loader.calls

    def test_status_reflects_install_and_remove(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader()
        assert schedule.status(home=home, platform_override="systemd").scheduled is False
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="systemd")
        assert schedule.status(home=home, platform_override="systemd").scheduled is True
        schedule.remove(home=home, loader=loader, platform_override="systemd")
        assert schedule.status(home=home, platform_override="systemd").scheduled is False


class TestInstallCron:
    def test_install_writes_crontab_line(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader(crontab_body="0 1 * * * /some/other/job\n")
        result = schedule.install(
            knowledge_root, home=home, loader=loader, platform_override="cron"
        )
        assert result.platform == "cron"
        assert result.paths == ()
        # The pre-existing, unrelated line survives untouched; our tagged
        # line is appended.
        assert "/some/other/job" in loader.crontab_body
        assert schedule.CRON_TAG in loader.crontab_body
        assert "ANTHROPIC_API_KEY=sk-test-not-real" in loader.crontab_body

    def test_install_is_idempotent_on_the_cron_line(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader(crontab_body="")
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="cron")
        first_body = loader.crontab_body
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="cron")
        # Re-running install must not duplicate the tagged line.
        assert first_body.count(schedule.CRON_TAG) == 1
        assert loader.crontab_body.count(schedule.CRON_TAG) == 1

    def test_remove_deletes_only_the_tagged_line(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader(crontab_body="0 1 * * * /some/other/job\n")
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="cron")
        assert schedule.CRON_TAG in loader.crontab_body

        result = schedule.remove(home=home, loader=loader, platform_override="cron")
        assert result.had_schedule is True
        assert schedule.CRON_TAG not in loader.crontab_body
        assert "/some/other/job" in loader.crontab_body

    def test_remove_with_no_crontab_at_all_is_a_noop(self, home: Path) -> None:
        loader = FakeLoader(crontab_body=None)  # type: ignore[arg-type]
        result = schedule.remove(home=home, loader=loader, platform_override="cron")
        assert result.had_schedule is False

    def test_status_reflects_install_and_remove(
        self, home: Path, knowledge_root: Path, api_key_env: None
    ) -> None:
        loader = FakeLoader(crontab_body="")

        def _scheduled() -> bool:
            return schedule.status(home=home, loader=loader, platform_override="cron").scheduled

        assert _scheduled() is False
        schedule.install(knowledge_root, home=home, loader=loader, platform_override="cron")
        assert _scheduled() is True
        schedule.remove(home=home, loader=loader, platform_override="cron")
        assert _scheduled() is False


class TestInstallRefusesWithoutKeySource:
    """AC3 counter-example, exercised through `install()` for every
    platform: no key source -> refuse, writing NOTHING."""

    @pytest.mark.parametrize("plat", ["launchd", "systemd", "cron"])
    def test_install_refuses_and_writes_nothing(
        self, home: Path, knowledge_root: Path, plat: str
    ) -> None:
        loader = FakeLoader(crontab_body="")
        with pytest.raises(schedule.NoKeySourceError):
            schedule.install(knowledge_root, home=home, loader=loader, platform_override=plat)
        # Nothing was written anywhere under `home`, and the loader was
        # never invoked -- the refusal happens before any I/O.
        assert not any(home.rglob("*"))
        assert loader.calls == []


# ---------------------------------------------------------------------------
# CLI integration (install + remove), loader mocked at the module seam.
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_install_then_remove(
        self, home: Path, knowledge_root: Path, api_key_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loader = FakeLoader()
        monkeypatch.setattr(schedule, "_default_loader", loader)

        rc, out, _err = _run_cli(
            [
                "schedule",
                "install",
                "--path",
                str(knowledge_root),
                "--home",
                str(home),
                "--backend",
                "cron",
            ]
        )
        assert rc == 0, out
        assert "schedule installed" in out
        assert schedule.status(home=home, loader=loader, platform_override="cron").scheduled

        rc, out, _err = _run_cli(
            ["schedule", "remove", "--home", str(home), "--backend", "cron"]
        )
        assert rc == 0, out
        assert "schedule removed" in out
        assert not schedule.status(home=home, loader=loader, platform_override="cron").scheduled

    def test_cli_install_refuses_without_key_source(
        self, home: Path, knowledge_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loader = FakeLoader(crontab_body="")
        monkeypatch.setattr(schedule, "_default_loader", loader)

        rc, _out, err = _run_cli(
            [
                "schedule",
                "install",
                "--path",
                str(knowledge_root),
                "--home",
                str(home),
                "--backend",
                "cron",
            ]
        )
        assert rc == 1
        assert "no key source" in err
        assert not any(home.rglob("*"))

    def test_cli_remove_with_nothing_installed_exits_zero(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loader = FakeLoader(crontab_body="")
        monkeypatch.setattr(schedule, "_default_loader", loader)
        rc, out, _err = _run_cli(["schedule", "remove", "--home", str(home), "--backend", "cron"])
        assert rc == 0
        assert "no cron schedule was installed" in out
