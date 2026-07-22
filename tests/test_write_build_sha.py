"""Tests for the deploy-SHA stamp (issue #413).

``scripts/write_build_sha.py`` writes the running commit SHA to
``dist/.build-sha`` so the cross-repo deploy-lag aggregator
(code-workspace-config#1428) can read "what commit is athenaeum running". The
byte format is a contract shared with hestia/voltaire's stamp readers, which do
``tr -d '[:space:]'`` and expect a bare 40-char hex SHA — so these tests pin:

- the exact file shape (single lowercase-hex line + trailing newline),
- that ``dist/`` is created on demand,
- that a stale stamp is overwritten,
- the ``ATHENAEUM_BUILD_SHA_ROOT`` test seam and the CLI entrypoint,
- and that a non-git root fails loudly (exit 1) rather than writing garbage.

``scripts/deploy-sync.sh`` (the single-checkout equivalent of voltaire's
deploy-guard.sh) is smoke-tested: with fetch disabled it must stamp the current
checkout and its ``--check`` mode must report in-sync/drift without mutating.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "write_build_sha.py"
DEPLOY_SYNC = REPO_ROOT / "scripts" / "deploy-sync.sh"


def _load_module():
    """Import the standalone script as a module (it lives in scripts/, not the package)."""
    spec = importlib.util.spec_from_file_location("write_build_sha", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_checkout(tmp_path: Path) -> Path:
    """A throwaway git repo with a single commit — the thing we stamp."""
    if shutil.which("git") is None:  # pragma: no cover - git is always present in CI
        pytest.skip("git not available on this runner")
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("fixture\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


# --------------------------------------------------------------------------- #
# write_build_sha() — the stamp-writing logic (criterion 3 target)
# --------------------------------------------------------------------------- #


def test_writes_head_sha(git_checkout: Path) -> None:
    mod = _load_module()
    returned = mod.write_build_sha(git_checkout)
    head = _git(git_checkout, "rev-parse", "HEAD")
    assert returned == head

    stamp = git_checkout / "dist" / ".build-sha"
    assert stamp.read_text() == head + "\n"


def test_creates_dist_dir_on_demand(git_checkout: Path) -> None:
    assert not (git_checkout / "dist").exists()
    mod = _load_module()
    mod.write_build_sha(git_checkout)
    assert (git_checkout / "dist").is_dir()
    assert (git_checkout / "dist" / ".build-sha").is_file()


def test_format_is_bare_sha_plus_newline(git_checkout: Path) -> None:
    """Exactly one 40-char lowercase-hex line + trailing newline.

    This is the reader contract: ``tr -d '[:space:]'`` on the file must yield
    the SHA and nothing else (no leading label, no CRLF, no second line).
    """
    mod = _load_module()
    sha = mod.write_build_sha(git_checkout)
    raw = (git_checkout / "dist" / ".build-sha").read_bytes()

    assert raw == (sha + "\n").encode("ascii")
    assert len(raw) == 41  # 40 hex + one '\n'
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
    assert raw.count(b"\n") == 1
    assert mod._SHA_RE.match(sha)  # 40 lowercase hex


def test_reader_contract_matches_voltaire(git_checkout: Path) -> None:
    """Emulate deploy-guard's ``tr -d '[:space:]' < dist/.build-sha`` read."""
    mod = _load_module()
    sha = mod.write_build_sha(git_checkout)
    content = (git_checkout / "dist" / ".build-sha").read_text()
    assert "".join(content.split()) == sha == _git(git_checkout, "rev-parse", "HEAD")


def test_overwrites_stale_stamp(git_checkout: Path) -> None:
    stamp = git_checkout / "dist" / ".build-sha"
    stamp.parent.mkdir()
    stamp.write_text("0" * 40 + "\n")  # a stale SHA from a previous deploy
    mod = _load_module()
    sha = mod.write_build_sha(git_checkout)
    assert stamp.read_text() == sha + "\n"
    assert "0" * 40 not in stamp.read_text()


def test_rejects_non_git_root(tmp_path: Path) -> None:
    mod = _load_module()
    with pytest.raises(subprocess.CalledProcessError):
        mod.write_build_sha(tmp_path)  # no .git here
    assert not (tmp_path / "dist").exists()  # nothing written on failure


def test_rejects_non_sha_output(git_checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``git`` that returns junk must raise, never stamp junk into the file."""
    mod = _load_module()

    class _Result:
        stdout = "not-a-sha\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(ValueError):
        mod.write_build_sha(git_checkout)
    assert not (git_checkout / "dist" / ".build-sha").exists()


# --------------------------------------------------------------------------- #
# CLI entrypoint + ATHENAEUM_BUILD_SHA_ROOT test seam
# --------------------------------------------------------------------------- #


def test_cli_stamps_via_env_root(git_checkout: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"ATHENAEUM_BUILD_SHA_ROOT": str(git_checkout), "PATH": _path()},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    head = _git(git_checkout, "rev-parse", "HEAD")
    assert (git_checkout / "dist" / ".build-sha").read_text() == head + "\n"
    assert head in proc.stdout  # "build-sha <sha> -> <path>"


def test_cli_nonzero_on_non_git_root(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"ATHENAEUM_BUILD_SHA_ROOT": str(tmp_path), "PATH": _path()},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "git rev-parse failed" in proc.stderr


# --------------------------------------------------------------------------- #
# main() + _default_root() — exercised in-process (subprocess tests above can't
# be traced by coverage, and these branches carry the exit-code contract)
# --------------------------------------------------------------------------- #


def test_default_root_honors_env(git_checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("ATHENAEUM_BUILD_SHA_ROOT", str(git_checkout))
    assert mod._default_root() == git_checkout.resolve()


def test_default_root_falls_back_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.delenv("ATHENAEUM_BUILD_SHA_ROOT", raising=False)
    # scripts/.. — the shipped script's own repo root.
    assert mod._default_root() == SCRIPT.resolve().parent.parent


def test_main_success(git_checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    mod = _load_module()
    monkeypatch.setenv("ATHENAEUM_BUILD_SHA_ROOT", str(git_checkout))
    assert mod.main([]) == 0
    head = _git(git_checkout, "rev-parse", "HEAD")
    assert (git_checkout / "dist" / ".build-sha").read_text() == head + "\n"
    assert head in capsys.readouterr().out


def test_main_returns_1_on_non_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    mod = _load_module()
    monkeypatch.setenv("ATHENAEUM_BUILD_SHA_ROOT", str(tmp_path))
    assert mod.main([]) == 1
    assert "git rev-parse failed" in capsys.readouterr().err


def test_main_returns_1_on_bad_sha(
    git_checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    mod = _load_module()
    monkeypatch.setenv("ATHENAEUM_BUILD_SHA_ROOT", str(git_checkout))

    class _Result:
        stdout = "deadbeef\n"  # too short → not a SHA

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
    assert mod.main([]) == 1
    assert "unexpected HEAD sha" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# deploy-sync.sh — single-checkout deploy-sync wrapper
# --------------------------------------------------------------------------- #


def _sync_env(checkout: Path) -> dict[str, str]:
    return {
        "ATHENAEUM_DEPLOY_DIR": str(checkout),
        "ATHENAEUM_SYNC_FETCH": "0",  # offline: stamp the checkout as-is
        "ATHENAEUM_PYTHON": sys.executable,
        "PATH": _path(),
    }


def _require_bash() -> None:
    if shutil.which("bash") is None:  # pragma: no cover - bash present in CI
        pytest.skip("bash not available on this runner")


def test_deploy_sync_stamps_checkout(git_checkout: Path) -> None:
    _require_bash()
    proc = subprocess.run(
        ["bash", str(DEPLOY_SYNC)],
        env=_sync_env(git_checkout),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    head = _git(git_checkout, "rev-parse", "HEAD")
    assert (git_checkout / "dist" / ".build-sha").read_text() == head + "\n"


def test_deploy_sync_check_reports_drift_then_in_sync(git_checkout: Path) -> None:
    _require_bash()
    # No stamp yet → drift, exit 10, nothing written.
    drift = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=_sync_env(git_checkout),
        capture_output=True,
        text=True,
    )
    assert drift.returncode == 10
    assert drift.stdout.startswith("drift")
    assert not (git_checkout / "dist" / ".build-sha").exists()  # --check mutates nothing

    # Stamp it, then --check reports in-sync, exit 0.
    subprocess.run(["bash", str(DEPLOY_SYNC)], env=_sync_env(git_checkout), check=True)
    ok = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=_sync_env(git_checkout),
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert ok.stdout.startswith("in-sync")


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
