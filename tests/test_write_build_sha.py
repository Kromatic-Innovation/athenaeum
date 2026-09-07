"""Tests for the deploy-SHA stamp (issue athenaeum#413).

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
checkout, and its ``--check`` mode must report both the checkout-vs-remote
state (``sync=``: in-sync / behind N / diverged / ...) and the stamp-vs-checkout
state (``stamp=``: current / stale / missing) without mutating anything
(athenaeum#1445). ``--check`` now fetches the deploy ref by default, so the
frozen-deployment and diverged-history cases from that issue get dedicated
coverage below.
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


def _bare_remote(tmp_path: Path, checkout: Path, ref: str) -> Path:
    """A local bare repo carrying ``checkout``'s current ``ref`` tip.

    A local filesystem path is a real ``git fetch`` target (no network
    involved), so tests can exercise ``--check``'s default fetch-and-compare
    path (athenaeum#1445 AC1) while staying fully offline.
    """
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "-q", "origin", f"{ref}:{ref}")
    return remote


def test_deploy_sync_check_reports_drift_then_in_sync(git_checkout: Path, tmp_path: Path) -> None:
    """This test's original target — the stamp comparison (missing -> current)
    round-tripping through ``--check`` without mutating anything — still holds
    now that ``--check`` also compares against ``origin/<ref>`` (athenaeum#1445
    AC1/AC2). A local bare "remote" (see ``_bare_remote``) gives it something
    real to fetch, so both reported axes are exercised here, not just the
    stamp half this test originally covered.

    BEFORE this change: two bare assertions, ``drift.stdout.startswith("drift")``
    and ``ok.stdout.startswith("in-sync")``, against a fixture with no remote
    configured at all — ``--check`` never looked at a remote pre-athenaeum#1445.
    AFTER: the output format gained a second axis (``sync=`` / ``stamp=``), and
    ``--check`` now fetches by default, so the fixture needs a real (local,
    offline) remote for a meaningful ``sync=`` reading instead of ``unknown``.
    """
    _require_bash()
    ref = _git(git_checkout, "rev-parse", "--abbrev-ref", "HEAD")
    _bare_remote(tmp_path, git_checkout, ref)
    env = dict(_sync_env(git_checkout), ATHENAEUM_DEPLOY_REF=ref)

    # No stamp yet -> sync=in-sync (checkout matches what it just pushed) but
    # stamp=missing -> exit 10 (install-only drift), nothing written.
    drift = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert drift.returncode == 10, drift.stdout + drift.stderr
    assert "sync=in-sync" in drift.stdout
    assert "stamp=missing" in drift.stdout
    assert not (git_checkout / "dist" / ".build-sha").exists()  # --check mutates nothing

    # Stamp it (mutating path, offline via ATHENAEUM_SYNC_FETCH=0), then
    # --check reports both axes healthy -> exit 0.
    subprocess.run(["bash", str(DEPLOY_SYNC)], env=env, check=True)
    ok = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "sync=in-sync" in ok.stdout
    assert "stamp=current" in ok.stdout


def test_deploy_sync_check_no_fetch_without_cached_ref_reports_no_fetch_unknown(
    git_checkout: Path,
) -> None:
    """``--no-fetch`` with no local ``origin/<ref>`` ever fetched has nothing
    to compare HEAD against. Every other ``--no-fetch`` sync reading is
    prefixed ``no-fetch:`` (asserted via the ``sync=in-sync`` round-trip
    above, run without ``--no-fetch``) so a live comparison is never confused
    with a cached one; this state is reachable ONLY under ``--no-fetch`` (a
    fetch failure without it exits 30 before this branch, and a successful
    fetch always leaves ``origin/<ref>`` resolvable), so it must carry that
    same prefix rather than rendering a bare ``unknown`` that looks like a
    third, unprefixed category.
    """
    _require_bash()
    proc = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check", "--no-fetch"],
        env=_sync_env(git_checkout),  # no origin remote configured at all
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "sync=no-fetch:unknown" in proc.stdout
    assert "sync=unknown" not in proc.stdout  # the bare, unprefixed form must never appear


def test_deploy_sync_check_frozen_deployment_reports_behind_not_in_sync(
    git_checkout: Path, tmp_path: Path
) -> None:
    """athenaeum#1445 AC5 — the incident's exact shape: a deploy checkout whose
    HEAD equals its stamp (nobody moved it since the last sync) but the deploy
    ref has since moved on. Before athenaeum#1445, ``--check`` only ever
    compared HEAD to the stamp and reported ``in-sync`` here — this is the
    "more stale the deploy, the more confidently it reports healthy" bug.
    """
    _require_bash()
    ref = _git(git_checkout, "rev-parse", "--abbrev-ref", "HEAD")
    remote = _bare_remote(tmp_path, git_checkout, ref)
    env = dict(_sync_env(git_checkout), ATHENAEUM_DEPLOY_REF=ref)

    # Stamp the checkout at its current (frozen) HEAD.
    subprocess.run(["bash", str(DEPLOY_SYNC)], env=env, check=True)
    frozen_head = _git(git_checkout, "rev-parse", "HEAD")
    assert (git_checkout / "dist" / ".build-sha").read_text().strip() == frozen_head

    # A second commit lands on the deploy ref via a SEPARATE clone — the
    # checkout under test never moves, simulating a frozen deploy worktree.
    pusher = tmp_path / "pusher"
    _git(tmp_path, "clone", "-q", str(remote), str(pusher))
    (pusher / "new-file.txt").write_text("advances the ref\n")
    _git(pusher, "add", "new-file.txt")
    _git(pusher, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "c2")
    _git(pusher, "push", "-q", "origin", f"HEAD:{ref}")

    check = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 11, check.stdout + check.stderr
    assert "sync=behind 1" in check.stdout
    assert "stamp=current" in check.stdout  # the stamp is still "right" — that's the trap
    assert "in-sync" not in check.stdout
    # HEAD itself must not have moved — this is a read-only check.
    assert _git(git_checkout, "rev-parse", "HEAD") == frozen_head


def test_deploy_sync_check_diverged_reports_diverged_not_in_sync(
    git_checkout: Path, tmp_path: Path
) -> None:
    """athenaeum#1445 AC6 — a checkout on rewritten-away history (the actual
    v0.20.0 incident: the deploy worktree was pinned pre-history-rewrite, 1761
    commits divergent from ``origin/main``) must report ``diverged``, never
    ``in-sync`` and never a bare merge failure.
    """
    _require_bash()
    ref = _git(git_checkout, "rev-parse", "--abbrev-ref", "HEAD")
    remote = _bare_remote(tmp_path, git_checkout, ref)
    env = dict(_sync_env(git_checkout), ATHENAEUM_DEPLOY_REF=ref)
    subprocess.run(["bash", str(DEPLOY_SYNC)], env=env, check=True)  # stamp the pre-rewrite tip
    pre_rewrite_head = _git(git_checkout, "rev-parse", "HEAD")

    # Rewrite history on the remote: an orphan commit, force-pushed over the
    # same ref name, sharing no ancestry with the checkout's current HEAD.
    rewriter = tmp_path / "rewriter"
    _git(tmp_path, "clone", "-q", str(remote), str(rewriter))
    _git(rewriter, "checkout", "-q", "--orphan", "rewritten-root")
    (rewriter / "rewritten.txt").write_text("post-rewrite history\n")
    _git(rewriter, "add", "rewritten.txt")
    _git(
        rewriter, "-c", "user.email=t@example.com", "-c", "user.name=t",
        "commit", "-q", "-m", "rewritten root",
    )
    _git(rewriter, "branch", "-M", ref)
    _git(rewriter, "push", "-q", "-f", "origin", ref)

    check = subprocess.run(
        ["bash", str(DEPLOY_SYNC), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 12, check.stdout + check.stderr
    assert "sync=diverged" in check.stdout
    assert "in-sync" not in check.stdout

    # The mutating path must also refuse the bare fast-forward and name the
    # remedy (AC4), rather than surfacing git's own "refusing to merge
    # unrelated histories" — the second failure from the same incident.
    sync = subprocess.run(
        ["bash", str(DEPLOY_SYNC)],
        # ATHENAEUM_SYNC_FETCH re-enabled here (env's "0" from _sync_env is for
        # the offline stamp-only call above): this call needs a real fetch to
        # observe the rewritten origin/<ref> and exercise the AC4 divergence
        # check, which lives on the fetch-enabled branch of the mutating path.
        env=dict(env, ATHENAEUM_SYNC_REINSTALL="0", ATHENAEUM_SYNC_FETCH="1"),
        capture_output=True,
        text=True,
    )
    assert sync.returncode != 0
    assert "DIVERGED" in sync.stderr
    assert "reset --hard" in sync.stderr  # the named remedy
    assert "refusing to merge unrelated histories" not in sync.stderr
    # HEAD itself must not have moved — the checkout was refused, not force-reset.
    assert _git(git_checkout, "rev-parse", "HEAD") == pre_rewrite_head


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
