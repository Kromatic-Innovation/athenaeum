"""Offline tests for scripts/deploy-guard.sh (athenaeum#510).

Exercises the guard's decision + sync logic hermetically: a throwaway local git
repo stands in for the deploy checkout, and the script's documented test hooks
(ATHENAEUM_GUARD_FETCH=0, ATHENAEUM_GUARD_REF_SHA, ATHENAEUM_GUARD_FF_CMD,
ATHENAEUM_GUARD_INSTALL_CMD, ATHENAEUM_GUARD_STAMP_CMD, ...) keep every case
fully offline — no network, no `hestia redeploy`, no real deploy dir, no venv
build. Mirrors the determinism approach of athenaeum-adapters'
tests/test_deploy_guard.py and athenaeum's own tests/test_write_build_sha.py.

CI runs bash on ubuntu-latest; these tests skip cleanly if bash/git is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "deploy-guard.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="deploy-guard.sh needs bash + git",
)

ZERO_SHA = "0" * 40


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "deploy"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("v1\n")
    # Mirror the real athenaeum deploy checkout: `dist/` is gitignored, so the
    # `dist/.build-sha` stamp never shows as an untracked change (a stamp that
    # dirtied the worktree would make the guard's dirty-refuse trip on itself).
    (repo / ".gitignore").write_text("dist/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    return repo


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _status_porcelain(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _stamp(repo: Path, sha: str) -> None:
    """Write the built marker deploy-sync.sh / the guard use: a bare SHA line."""
    (repo / "dist").mkdir(parents=True, exist_ok=True)
    (repo / "dist" / ".build-sha").write_text(sha + "\n")


def _run(args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ATHENAEUM_DEPLOY_DIR", None)
    env.pop("LOCAL_DEPLOYS_DIR", None)
    env["ATHENAEUM_GUARD_FETCH"] = "0"  # never hit the network
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(GUARD), *args], capture_output=True, text=True, env=env
    )


def test_script_present_and_executable() -> None:
    # AC1: repo ships scripts/deploy-guard.sh, committed executable.
    assert GUARD.exists()
    assert os.access(GUARD, os.X_OK)


def test_lib_local_deploys_present() -> None:
    # AC2: scripts/lib/local-deploys.sh ships alongside the guard.
    lib = GUARD.parent / "lib" / "local-deploys.sh"
    assert lib.exists()
    assert "local_deploy_dir()" in lib.read_text()


def test_pre_activation_when_deploy_dir_absent(tmp_path: Path) -> None:
    absent = tmp_path / "nope"
    r = _run(["--check"], {"ATHENAEUM_DEPLOY_DIR": str(absent)})
    assert r.returncode == 0
    assert "pre-activation" in r.stdout

    r2 = _run([], {"ATHENAEUM_DEPLOY_DIR": str(absent)})
    assert r2.returncode == 0
    assert "pre-activation" in r2.stderr


def test_in_sync_exits_zero_and_changes_nothing(tmp_path: Path) -> None:
    # AC3: --check reports in-sync; the mutating mode is idempotent on a
    # synced+stamped checkout (stamp == deploy-ref sha).
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    env = {"ATHENAEUM_DEPLOY_DIR": str(repo), "ATHENAEUM_GUARD_REF_SHA": head}

    r = _run(["--check"], env)
    assert r.returncode == 0
    assert "in-sync" in r.stdout

    # Run the mutating mode twice; both must be no-ops on an in-sync checkout.
    for _ in range(2):
        r2 = _run([], env)
        assert r2.returncode == 0
        assert "in-sync" in r2.stderr
    assert _head(repo) == head
    assert _status_porcelain(repo) == ""


def test_check_reports_drift_when_unstamped(tmp_path: Path) -> None:
    # AC3: an unstamped (or stale-stamped) checkout is drift, not an error.
    repo = _make_repo(tmp_path)
    env = {"ATHENAEUM_DEPLOY_DIR": str(repo), "ATHENAEUM_GUARD_REF_SHA": _head(repo)}
    r = _run(["--check"], env)
    assert r.returncode == 10
    assert "drift" in r.stdout


def test_check_reports_drift_on_stale_stamp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _stamp(repo, ZERO_SHA)  # a stale stamp from a previous deploy
    env = {"ATHENAEUM_DEPLOY_DIR": str(repo), "ATHENAEUM_GUARD_REF_SHA": _head(repo)}
    r = _run(["--check"], env)
    assert r.returncode == 10
    assert "drift" in r.stdout


def test_check_error_when_ref_unresolvable(tmp_path: Path) -> None:
    # No injected ref sha + fetch disabled + no origin remote -> error (exit 20),
    # never a silent "in-sync".
    repo = _make_repo(tmp_path)
    r = _run(["--check"], {"ATHENAEUM_DEPLOY_DIR": str(repo)})
    assert r.returncode == 20
    assert "error" in r.stdout


def test_drift_syncs_with_stubbed_commands(tmp_path: Path) -> None:
    # AC: on drift, fast-forward then refresh the venv then stamp. All three
    # steps are stubbed to `true` so the case stays fully offline.
    repo = _make_repo(tmp_path)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": ZERO_SHA,
        "ATHENAEUM_GUARD_FF_CMD": "true",
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode == 0
    assert "drift" in r.stderr
    assert "synced" in r.stderr
    assert "refreshed .venv" in r.stderr


def test_abort_loud_on_ff_failure(tmp_path: Path) -> None:
    # AC: fails loudly with a recovery hint on a non-fast-forward condition
    # rather than force-resetting the checkout.
    repo = _make_repo(tmp_path)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": ZERO_SHA,
        "ATHENAEUM_GUARD_FF_CMD": "false",
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "never force-resets" in r.stderr
    assert "Recovery:" in r.stderr


def test_abort_loud_on_install_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": ZERO_SHA,
        "ATHENAEUM_GUARD_FF_CMD": "true",
        "ATHENAEUM_GUARD_INSTALL_CMD": "false",
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "venv refresh failed" in r.stderr


def test_abort_loud_on_stamp_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": ZERO_SHA,
        "ATHENAEUM_GUARD_FF_CMD": "true",
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "false",
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "build-sha stamp failed" in r.stderr


def test_dirty_worktree_refused(tmp_path: Path) -> None:
    # A main-pinned deploy worktree must never be hand-edited: a dirty tree is
    # refused loudly on the in-sync path too, and never force-reset.
    repo = _make_repo(tmp_path)
    _stamp(repo, _head(repo))
    (repo / "f.txt").write_text("hand-edited\n")
    env = {"ATHENAEUM_DEPLOY_DIR": str(repo), "ATHENAEUM_GUARD_REF_SHA": _head(repo)}
    r = _run([], env)
    assert r.returncode != 0
    assert "dirty" in r.stderr
    assert "never force-resets" in r.stderr
    # The guard must not have discarded the local edit.
    assert (repo / "f.txt").read_text() == "hand-edited\n"


def test_local_deploy_dir_contract(tmp_path: Path) -> None:
    # AC2: with LOCAL_DEPLOYS_DIR set and no explicit override, the guard must
    # resolve $LOCAL_DEPLOYS_DIR/athenaeum via the sourced lib/local-deploys.sh.
    # Point it at an absent dir; the pre-activation line names the resolved path,
    # proving the contract without a real checkout.
    root = tmp_path / "local-deploys"
    root.mkdir()
    env = dict(os.environ)
    env.pop("ATHENAEUM_DEPLOY_DIR", None)
    env["ATHENAEUM_GUARD_FETCH"] = "0"
    env["LOCAL_DEPLOYS_DIR"] = str(root)
    r = subprocess.run(
        ["bash", str(GUARD), "--check"], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0
    assert str(root / "athenaeum") in r.stdout


def _source_and_eval(func_call: str, env_extra: dict[str, str]) -> str:
    """Source the guard (entrypoint is source-guarded) and echo one helper's output."""
    env = dict(os.environ)
    env.pop("ATHENAEUM_DEPLOY_EXTRAS", None)
    env.update(env_extra)
    r = subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; {func_call}'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_default_install_cmd_uses_athenaeum_extras() -> None:
    # The Python build step is adapted to athenaeum's real deploy (deploy-sync.sh):
    # editable install WITH the mcp+vector extras, not a bare uv sync / pip install.
    out = _source_and_eval("_dg_default_install_cmd", {})
    assert out == 'python3 -m venv .venv && .venv/bin/pip install -q -e ".[mcp,vector]"'


def test_deploy_extras_override_flows_into_install() -> None:
    out = _source_and_eval("_dg_default_install_cmd", {"ATHENAEUM_DEPLOY_EXTRAS": "mcp"})
    assert out == 'python3 -m venv .venv && .venv/bin/pip install -q -e ".[mcp]"'
