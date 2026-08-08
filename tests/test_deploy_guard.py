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


def _new_commit(repo: Path, content: str) -> str:
    """Add a commit on top of HEAD and return its sha (for drift/rewind cases)."""
    (repo / "f.txt").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2")
    return _head(repo)


def test_forward_drift_reconciles_to_newer_ref(tmp_path: Path) -> None:
    # Common case: the deploy checkout is BEHIND the ref (a normal forward
    # deploy). The guard reconciles HEAD up to origin/<ref>, refreshes the venv,
    # stamps, and reports the OBSERVED head. All side-effect steps are stubbed to
    # stay offline; the reconcile does a real (in-repo) reset so HEAD truly moves.
    repo = _make_repo(tmp_path)
    c1 = _head(repo)
    c2 = _new_commit(repo, "v2\n")
    _git(repo, "reset", "--hard", c1)  # move the checkout BEHIND the ref
    assert _head(repo) == c1
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": c2,
        "ATHENAEUM_GUARD_RECONCILE_CMD": f'git -C "{repo}" reset --hard {c2}',
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode == 0, r.stderr
    assert "drift" in r.stderr
    assert "synced" in r.stderr
    assert "refreshed .venv" in r.stderr
    assert c2 in r.stderr  # reports the OBSERVED head, not a stale target
    assert _head(repo) == c2


def test_rewind_reconciles_backward_and_reports_observed_head(tmp_path: Path) -> None:
    # athenaeum#614 happy path: origin/<ref> was REWOUND to an ancestor, so HEAD
    # is AHEAD of the ref -- a case `merge --ff-only` cannot express. `reset
    # --hard` moves HEAD BACKWARD to the ref; the guard reports the observed
    # (moved) HEAD.
    repo = _make_repo(tmp_path)
    c1 = _head(repo)
    c2 = _new_commit(repo, "v2\n")  # HEAD now at c2 (descendant of c1)
    assert _head(repo) == c2
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": c1,  # ref is the ANCESTOR (a rewind/rollback)
        "ATHENAEUM_GUARD_RECONCILE_CMD": f'git -C "{repo}" reset --hard {c1}',
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode == 0, r.stderr
    assert "synced" in r.stderr
    assert c1 in r.stderr  # observed HEAD == the (older) ref
    assert _head(repo) == c1  # HEAD actually moved BACKWARD


def test_rewind_noop_reconcile_fails_loud_not_false_sync(tmp_path: Path) -> None:
    # THE regression for athenaeum#614. origin/<ref> was rewound to an ancestor
    # and the reconcile is a NO-OP -- exactly what `merge --ff-only` was against
    # an ancestor: exit 0, HEAD unchanged. The post-condition must detect that
    # HEAD never reached the ref and abort LOUDLY, naming both SHAs -- never a
    # silent false "synced" while the deploy serves stale code.
    repo = _make_repo(tmp_path)
    c1 = _head(repo)
    c2 = _new_commit(repo, "v2\n")  # HEAD at c2; ref (c1) is behind it
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": c1,
        "ATHENAEUM_GUARD_RECONCILE_CMD": "true",  # succeeds but moves nothing
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "post-condition" in r.stderr.lower()
    assert c1 in r.stderr and c2 in r.stderr  # names both expected ref and stuck HEAD
    assert "synced" not in r.stderr  # never a false success
    assert _head(repo) == c2  # HEAD unchanged by the no-op reconcile


def test_ff_cmd_accepted_as_reconcile_alias(tmp_path: Path) -> None:
    # ATHENAEUM_GUARD_FF_CMD is kept as a deprecated alias for the reconcile
    # command so older callers/tests keep working after the rename.
    repo = _make_repo(tmp_path)
    c1 = _head(repo)
    _new_commit(repo, "v2\n")
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": c1,
        "ATHENAEUM_GUARD_FF_CMD": f'git -C "{repo}" reset --hard {c1}',  # deprecated alias
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode == 0, r.stderr
    assert _head(repo) == c1


def test_head_at_ref_but_unstamped_rebuilds_and_stamps(tmp_path: Path) -> None:
    # HEAD already at the ref but the build marker is stale/absent: the running
    # code is correct, the venv/stamp are not. The guard rebuilds venv + stamp
    # WITHOUT moving git, and the post-condition (HEAD == ref) holds trivially.
    repo = _make_repo(tmp_path)
    head = _head(repo)  # no stamp written => marker absent
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": head,
        "ATHENAEUM_GUARD_INSTALL_CMD": "true",
        "ATHENAEUM_GUARD_STAMP_CMD": "true",
    }
    r = _run([], env)
    assert r.returncode == 0, r.stderr
    assert "refreshed .venv" in r.stderr
    assert _head(repo) == head


def test_abort_loud_on_reconcile_failure(tmp_path: Path) -> None:
    # A reconcile that FAILS (not a no-op) aborts loudly with a recovery hint.
    repo = _make_repo(tmp_path)
    c1 = _head(repo)
    _new_commit(repo, "v2\n")  # HEAD ahead of ref => drift => reconcile runs
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": c1,
        "ATHENAEUM_GUARD_RECONCILE_CMD": "false",  # reconcile fails
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "reconcile to origin/" in r.stderr
    assert "Recovery:" in r.stderr


def test_abort_loud_on_install_failure(tmp_path: Path) -> None:
    # HEAD at ref, unstamped => reach the venv-refresh step; make it fail.
    repo = _make_repo(tmp_path)
    head = _head(repo)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": head,
        "ATHENAEUM_GUARD_INSTALL_CMD": "false",
    }
    r = _run([], env)
    assert r.returncode != 0
    assert "ABORT" in r.stderr
    assert "venv refresh failed" in r.stderr


def test_abort_loud_on_stamp_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    env = {
        "ATHENAEUM_DEPLOY_DIR": str(repo),
        "ATHENAEUM_GUARD_REF_SHA": head,
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


def _source_and_eval_rc(func_call: str, env_extra: dict[str, str]) -> tuple[int, str]:
    """_source_and_eval's variant for helpers that signal failure via exit status."""
    env = dict(os.environ)
    env.pop("ATHENAEUM_DEPLOY_EXTRAS", None)
    env.pop("ATHENAEUM_GUARD_PYTHON", None)
    env.update(env_extra)
    r = subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; {func_call}'],
        capture_output=True,
        text=True,
        env=env,
    )
    return r.returncode, r.stdout.strip()


def _shim_dir(tmp_path: Path, versions: dict[str, str]) -> Path:
    """A PATH dir of fake interpreters that report a fixed MAJ.MIN.

    The guard asks an interpreter for its OWN version rather than trusting its
    name, so a two-line shim is enough to stand in for a real 3.11/3.13 — which
    is what keeps the athenaeum#832 regression tests hermetic: no pyenv, no
    homebrew, no real venv build, and identical behavior on any CI runner.
    """
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name, version in versions.items():
        shim = shims / name
        shim.write_text(f"#!/bin/sh\necho {version}\n")
        shim.chmod(0o755)
    return shims


def _tree_with_floor(tmp_path: Path, spec: str) -> Path:
    """A throwaway package tree declaring `requires-python = <spec>`."""
    tree = tmp_path / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "pyproject.toml").write_text(
        f'[project]\nname = "athenaeum"\nrequires-python = "{spec}"\n'
    )
    return tree


def _shimmed_path(shims: Path) -> str:
    return f"{shims}:{os.environ.get('PATH', '')}"


def test_default_install_cmd_uses_athenaeum_extras(tmp_path: Path) -> None:
    # The Python build step is adapted to athenaeum's real deploy (deploy-sync.sh):
    # editable install WITH the mcp+vector extras, not a bare uv sync / pip install.
    # Since athenaeum#832 the interpreter is a RESOLVED absolute path rather than
    # bare `python3`; the shim pins it so the assertion stays machine-independent.
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.13"})
    out = _source_and_eval(f"_dg_default_install_cmd {tree}", {"PATH": _shimmed_path(shims)})
    assert out == (
        f'"{shims}/python3" -m venv .venv && .venv/bin/pip install -q -e ".[mcp,vector]"'
    )


def test_deploy_extras_override_flows_into_install(tmp_path: Path) -> None:
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.13"})
    out = _source_and_eval(
        f"_dg_default_install_cmd {tree}",
        {"ATHENAEUM_DEPLOY_EXTRAS": "mcp", "PATH": _shimmed_path(shims)},
    )
    assert out == f'"{shims}/python3" -m venv .venv && .venv/bin/pip install -q -e ".[mcp]"'


def test_default_reconcile_cmd_is_hard_reset_to_origin_ref() -> None:
    # The drift reconcile is a `reset --hard origin/<ref>`, NOT `merge --ff-only`
    # -- so a rewind to an ancestor is actually applied, not a silent no-op
    # (athenaeum#614). This locks the fix's key behavior change.
    out = _source_and_eval("_dg_default_reconcile_cmd /tmp/deploy main", {})
    assert out == 'git -C "/tmp/deploy" reset --hard "origin/main"'


# ---------------------------------------------------------------------------
# Metadata-drift reconcile (issue athenaeum#685) — an in-sync HEAD with a stale editable
# install's .dist-info version must be refreshed, or fail loudly.
# ---------------------------------------------------------------------------


def _insync_env(repo: Path, head: str, extra: dict[str, str]) -> dict[str, str]:
    env = {"ATHENAEUM_DEPLOY_DIR": str(repo), "ATHENAEUM_GUARD_REF_SHA": head}
    env.update(extra)
    return env


def test_metadata_in_sync_does_not_refresh(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    # version-check reports in-sync (exit 0); the refresh must NOT run (would fail).
    env = _insync_env(
        repo,
        head,
        {
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": "true",
            "ATHENAEUM_GUARD_METADATA_REFRESH_CMD": "false",
        },
    )
    r = _run([], env)
    assert r.returncode == 0
    assert "in-sync" in r.stderr
    assert "refreshing" not in r.stderr
    assert _status_porcelain(repo) == ""


def test_metadata_drift_triggers_refresh_then_reverifies(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    marker = tmp_path / "refreshed.flag"
    # Stateful stub: drift (exit 10) until the refresh creates the marker, then
    # in-sync (exit 0). The refresh creates the marker — proving it ran and that
    # the guard re-verifies afterward.
    check = f'bash -c "[ -f {marker} ] && exit 0 || exit 10"'
    refresh = f"touch {marker}"
    env = _insync_env(
        repo,
        head,
        {
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": check,
            "ATHENAEUM_GUARD_METADATA_REFRESH_CMD": refresh,
        },
    )
    r = _run([], env)
    assert r.returncode == 0, r.stderr
    assert "metadata drift" in r.stderr
    assert "metadata refreshed" in r.stderr
    assert marker.exists()


def test_metadata_refresh_failure_aborts_loud(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    env = _insync_env(
        repo,
        head,
        {
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": 'bash -c "exit 10"',  # always drifted
            "ATHENAEUM_GUARD_METADATA_REFRESH_CMD": "false",  # refresh fails
        },
    )
    r = _run([], env)
    assert r.returncode != 0
    assert "metadata refresh failed" in r.stderr


def test_metadata_still_drifted_after_refresh_aborts_loud(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    env = _insync_env(
        repo,
        head,
        {
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": 'bash -c "exit 10"',  # never clears
            "ATHENAEUM_GUARD_METADATA_REFRESH_CMD": "true",  # refresh "succeeds"
        },
    )
    r = _run([], env)
    assert r.returncode != 0
    assert "still drifted after refresh" in r.stderr


def test_metadata_check_undetermined_warns_but_does_not_block(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _head(repo)
    _stamp(repo, head)
    # An undetermined/unrunnable check (e.g. the deploy venv predates the module,
    # before the one-off pip install -e . in AC5) is WARNED, not a hard abort —
    # the standalone `python -m athenaeum.deploy_check` surface still reports it.
    env = _insync_env(
        repo,
        head,
        {
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": 'bash -c "exit 20"',
            "ATHENAEUM_GUARD_METADATA_REFRESH_CMD": "false",  # must not run
        },
    )
    r = _run([], env)
    assert r.returncode == 0
    assert "WARN version-check could not confirm metadata" in r.stderr


def test_default_version_check_cmd_targets_deploy_venv() -> None:
    out = _source_and_eval("_dg_default_version_check_cmd /tmp/deploy", {})
    assert out == '"/tmp/deploy/.venv/bin/python" -m athenaeum.deploy_check --check "/tmp/deploy"'


def test_default_metadata_refresh_cmd_is_no_deps_editable() -> None:
    out = _source_and_eval("_dg_default_metadata_refresh_cmd /tmp/deploy", {})
    assert out == '"/tmp/deploy/.venv/bin/pip" install -q -e . --no-deps'


# ---------------------------------------------------------------------------
# Interpreter selection (issue athenaeum#832) — the venv rebuild must resolve an
# interpreter that SATISFIES the deploy tree's requires-python, never trust bare
# `python3` on $PATH. AC3: these cover the case where `python3` does not meet it.
# ---------------------------------------------------------------------------


def test_requires_python_floor_parsed_from_pyproject(tmp_path: Path) -> None:
    for i, (spec, expected) in enumerate(
        [
            (">=3.13", "3.13"),
            (">=3.13,<4.0", "3.13"),
            (">= 3.13", "3.13"),
            (">=3", "3.0"),
        ]
    ):
        tree = _tree_with_floor(tmp_path / f"case{i}", spec)
        assert _source_and_eval(f"_dg_requires_python {tree}", {}) == expected, spec


def test_requires_python_absent_is_not_an_error(tmp_path: Path) -> None:
    # A tree that declares no floor has none to enforce -> non-zero, and the
    # caller keeps the historical bare-`python3` behavior rather than aborting.
    empty = tmp_path / "no-pyproject"
    empty.mkdir()
    rc, out = _source_and_eval_rc(f"_dg_requires_python {empty}", {})
    assert rc != 0
    assert out == ""


def test_resolver_rejects_too_old_bare_python3(tmp_path: Path) -> None:
    # THE athenaeum#832 REGRESSION: `python3` resolves to 3.11 (the deploy host's
    # pyenv shim) while a satisfying 3.13 exists under a versioned name. Before
    # the fix the guard built the venv with 3.11 and pip failed with
    # "requires a different Python: 3.11.15 not in '>=3.13'".
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.11", "python3.13": "3.13"})
    out = _source_and_eval(f"_dg_resolve_python {tree}", {"PATH": _shimmed_path(shims)})
    assert out == f"{shims}/python3.13"

    cmd = _source_and_eval(f"_dg_default_install_cmd {tree}", {"PATH": _shimmed_path(shims)})
    assert cmd.startswith(f'"{shims}/python3.13" -m venv')


def test_resolver_prefers_bare_python3_when_it_already_satisfies(tmp_path: Path) -> None:
    # A healthy machine keeps today's exact behavior and cost — the fix must not
    # start preferring a versioned interpreter over a perfectly good `python3`.
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.13", "python3.14": "3.14"})
    out = _source_and_eval(f"_dg_resolve_python {tree}", {"PATH": _shimmed_path(shims)})
    assert out == f"{shims}/python3"


def test_resolver_accepts_a_newer_minor_than_the_floor(tmp_path: Path) -> None:
    # A floor no real interpreter can meet, so the ONLY satisfying candidate is
    # the shim — otherwise a genuine pythonX.Y sitting on the runner's PATH
    # between the floor and the shim wins on the nearest-the-floor rule and the
    # assertion becomes environment-dependent (it did: CI ships a real 3.13).
    tree = _tree_with_floor(tmp_path, ">=3.90")
    shims = _shim_dir(tmp_path, {"python3": "3.11", "python3.95": "3.95"})
    out = _source_and_eval(f"_dg_resolve_python {tree}", {"PATH": _shimmed_path(shims)})
    assert out == f"{shims}/python3.95"


def test_resolver_fails_when_nothing_satisfies(tmp_path: Path) -> None:
    # No interpreter on PATH meets the floor -> silent non-zero, so the caller
    # can abort loudly instead of handing pip an interpreter it will reject.
    tree = _tree_with_floor(tmp_path, ">=3.99")
    shims = _shim_dir(tmp_path, {"python3": "3.11"})
    rc, out = _source_and_eval_rc(f"_dg_resolve_python {tree}", {"PATH": _shimmed_path(shims)})
    assert rc != 0
    assert out == ""


def test_guard_python_override_is_used_when_it_satisfies(tmp_path: Path) -> None:
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.11", "python3.13": "3.13"})
    out = _source_and_eval(
        f"_dg_resolve_python {tree}",
        {"PATH": _shimmed_path(shims), "ATHENAEUM_GUARD_PYTHON": "python3.13"},
    )
    assert out == f"{shims}/python3.13"


def test_guard_python_override_that_does_not_satisfy_is_refused(tmp_path: Path) -> None:
    # An operator's explicit choice is the ONLY candidate: silently probing past
    # a bad ATHENAEUM_GUARD_PYTHON would hide the misconfiguration.
    tree = _tree_with_floor(tmp_path, ">=3.13")
    shims = _shim_dir(tmp_path, {"python3": "3.11", "python3.13": "3.13"})
    rc, out = _source_and_eval_rc(
        f"_dg_resolve_python {tree}",
        {"PATH": _shimmed_path(shims), "ATHENAEUM_GUARD_PYTHON": "python3"},
    )
    assert rc != 0
    assert out == ""


def test_stale_venv_below_the_floor_is_rebuilt_with_clear(tmp_path: Path) -> None:
    # `venv` reuses an existing tree, so a .venv built by 3.11 would keep failing
    # even with the right interpreter — the exact state the operator had to fix by
    # hand. A mismatched .venv is rebuilt with --clear.
    tree = _tree_with_floor(tmp_path, ">=3.13")
    (tree / ".venv" / "bin").mkdir(parents=True)
    stale = tree / ".venv" / "bin" / "python"
    stale.write_text("#!/bin/sh\necho 3.11\n")
    stale.chmod(0o755)
    shims = _shim_dir(tmp_path, {"python3": "3.13"})
    out = _source_and_eval(f"_dg_default_install_cmd {tree}", {"PATH": _shimmed_path(shims)})
    assert " -m venv --clear .venv" in out


def test_satisfying_venv_is_reused_without_clear(tmp_path: Path) -> None:
    tree = _tree_with_floor(tmp_path, ">=3.13")
    (tree / ".venv" / "bin").mkdir(parents=True)
    ok = tree / ".venv" / "bin" / "python"
    ok.write_text("#!/bin/sh\necho 3.13\n")
    ok.chmod(0o755)
    shims = _shim_dir(tmp_path, {"python3": "3.13"})
    out = _source_and_eval(f"_dg_default_install_cmd {tree}", {"PATH": _shimmed_path(shims)})
    assert "--clear" not in out
    assert " -m venv .venv" in out


def test_sync_aborts_loud_when_no_interpreter_satisfies(tmp_path: Path) -> None:
    # End-to-end on the drift path: rather than surface pip's opaque "requires a
    # different Python" AFTER reconciling the checkout, the guard aborts with the
    # constraint and a recovery hint naming ATHENAEUM_GUARD_PYTHON.
    repo = _make_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "athenaeum"\nrequires-python = ">=3.13"\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "declare requires-python")
    head = _head(repo)
    shims = _shim_dir(tmp_path, {"python3": "3.11"})
    r = _run(
        [],
        {
            "ATHENAEUM_DEPLOY_DIR": str(repo),
            "ATHENAEUM_GUARD_REF_SHA": head,
            "ATHENAEUM_GUARD_PYTHON": str(shims / "python3"),  # forced, too old
            "ATHENAEUM_GUARD_STAMP_CMD": "true",
        },
    )
    assert r.returncode != 0
    assert "no Python interpreter satisfying requires-python >=3.13" in r.stderr
    assert "ATHENAEUM_GUARD_PYTHON" in r.stderr


def test_install_cmd_override_bypasses_interpreter_resolution(tmp_path: Path) -> None:
    # ATHENAEUM_GUARD_INSTALL_CMD stays a full escape hatch: an explicit command
    # is run as-is, and resolution never gets a chance to veto it.
    repo = _make_repo(tmp_path)
    head = _head(repo)
    marker = tmp_path / "installed"
    r = _run(
        [],
        {
            "ATHENAEUM_DEPLOY_DIR": str(repo),
            "ATHENAEUM_GUARD_REF_SHA": head,
            "ATHENAEUM_GUARD_PYTHON": "/nonexistent/python",  # would fail resolution
            "ATHENAEUM_GUARD_INSTALL_CMD": f"touch {marker}",
            "ATHENAEUM_GUARD_STAMP_CMD": "true",
            "ATHENAEUM_GUARD_VERSION_CHECK_CMD": "true",
        },
    )
    assert r.returncode == 0, r.stderr
    assert marker.exists()


def test_real_repo_resolves_an_interpreter_meeting_its_own_floor() -> None:
    # Not a shim: against THIS repo's real pyproject.toml, whatever the guard
    # resolves must genuinely satisfy the declared floor. Skips (rather than
    # fails) on a machine with no satisfying interpreter on PATH — that is the
    # abort path, covered hermetically by the shim tests above.
    repo_root = GUARD.parent.parent
    floor = _source_and_eval(f"_dg_requires_python {repo_root}", {})
    assert floor, "athenaeum's pyproject.toml must declare a >= requires-python floor"
    rc, resolved = _source_and_eval_rc(f"_dg_resolve_python {repo_root}", {})
    if rc != 0:
        pytest.skip(f"no Python >={floor} on PATH; the guard's abort path is covered by shims")
    assert Path(resolved).exists()
    reported = subprocess.run(
        [resolved, "-c", 'import sys; print("%d.%d" % sys.version_info[:2])'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    want = tuple(int(p) for p in floor.split("."))
    got = tuple(int(p) for p in reported.split("."))
    assert got >= want, f"resolved {resolved} is {reported}, below requires-python >={floor}"
