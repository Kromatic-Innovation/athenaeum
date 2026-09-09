# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/public-safe-lint-gate.sh and .githooks/pre-push
(athenaeum#1104 — gate public-safe-lint at the push boundary, and fail on
suppressed rules that were never explicitly reviewed).

Two things are under test, matching the issue's own acceptance criteria:

1. The gate script (a thin wrapper around the committed public-safe-lint.sh)
   fails outright on a genuine leak shape, AND fails when a rule carries an
   active suppression that is not present in the committed
   ``.public-safe-lint-suppression-allowlist`` — even though
   public-safe-lint.sh's own exit code would be 0 in that case (a
   suppressed hit is still a "clean" verdict as far as the linter itself is
   concerned). An approved-suppression control proves the mechanism
   discriminates rather than failing on every suppression unconditionally.
2. The pre-push hook wiring: a bypass is recorded (never silent) and a
   push without the bypass is blocked by a seeded leak.

All scans run against throwaway ``tmp_path`` trees, never this repo's own
source tree — the point is to exercise the gate's logic, not to lint
athenaeum itself a second time (that's public-safe-lint.sh's own concern,
run directly in CI and in the push-boundary hook this issue adds).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GATE = REPO_ROOT / "scripts" / "public-safe-lint-gate.sh"
LINTER = REPO_ROOT / "public-safe-lint.sh"
PRE_PUSH_HOOK = REPO_ROOT / ".githooks" / "pre-push"
PUSH_WRAPPER = REPO_ROOT / "scripts" / "git-push-safe.sh"


def _clean_git_env() -> dict[str, str]:
    """os.environ with any GIT_CONFIG_COUNT/KEY_n/VALUE_n stripped.

    Some containerized dev environments this repo is built in set those to
    force `core.hooksPath` workspace-wide for their own unrelated hook
    needs -- env-based git config outranks a repo's own `.git/config`, so
    left in place it would silently defeat the plain-`git push` tests
    below (they'd test the ambient environment's hooks, not this repo's).
    `test_push_wrapper_survives_hostile_hookspath_override` below restores
    exactly this shape deliberately, to prove the wrapper's resilience to
    it."""
    return {
        k: v
        for k, v in os.environ.items()
        if k != "GIT_CONFIG_COUNT"
        and not k.startswith("GIT_CONFIG_KEY_")
        and not k.startswith("GIT_CONFIG_VALUE_")
    }


def _run_gate(scan_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATE), str(scan_dir), str(LINTER)],
        capture_output=True,
        text=True,
    )


def test_gate_scripts_exist_and_are_executable() -> None:
    for path in (GATE, PRE_PUSH_HOOK):
        assert path.is_file(), path
        assert path.stat().st_mode & 0o111, f"{path} is not executable"


def test_gate_passes_on_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("nothing interesting here\n")
    result = _run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE OK" in result.stdout


# Seed literals below are assembled from sub-fragments rather than written
# as single contiguous strings -- this test file is itself committed and
# scanned by public-safe-lint.sh (both directly in CI and by the
# push-boundary gate this issue adds), and a seed that appears as a
# matchable literal in this file's own source would make it flag itself.
# Mirrors the same discipline public-safe-lint.sh's own canary block uses,
# for the same reason (see that script's header comment).
_HASH = "#"
_ISSUE_DIGITS_A = "1234"
_ISSUE_DIGITS_B = "4321"
_ATTRIBUTION_PREFIX = "agreed with"
_ATTRIBUTION_INITIALS = "AB"


def test_gate_fails_on_seeded_leak_shape(tmp_path: Path) -> None:
    """AC4, first half: a genuine leak shape (bare issue ref) fails the
    gate outright, before suppression logic is even reached."""
    (tmp_path / "notes.md").write_text(f"see issue {_HASH}{_ISSUE_DIGITS_A} for context\n")
    result = _run_gate(tmp_path)
    assert result.returncode != 0
    assert "FAIL [bare-issue-ref]" in result.stdout
    assert "GATE FAIL" in (result.stdout + result.stderr)


def test_gate_fails_on_unapproved_suppression(tmp_path: Path) -> None:
    """AC4, second half: public-safe-lint.sh itself reports exit 0 (a
    suppressed hit is still its own definition of "clean"), but the gate
    fails anyway because no `.public-safe-lint-suppression-allowlist` file
    approves the suppressed rule. This is the AC2 assertion — a suppressed
    rule that was never explicitly reviewed must not pass silently."""
    (tmp_path / "f.txt").write_text(
        f"{_ATTRIBUTION_PREFIX} {_ATTRIBUTION_INITIALS} on the approach\n"
    )
    (tmp_path / ".public-safe-lintignore").write_text("personal-attribution-agreed-with f.txt\n")
    # Deliberately no .public-safe-lint-suppression-allowlist: asserted
    # suppressed-rule count is 0, so any active suppression must fail.

    # Sanity: the underlying linter alone considers this clean (exit 0),
    # which is exactly the unverified-green gap this issue is about.
    linter_only = subprocess.run(
        ["bash", str(LINTER), str(tmp_path)], capture_output=True, text=True
    )
    assert linter_only.returncode == 0, linter_only.stdout + linter_only.stderr
    assert "SUPPRESSED" in linter_only.stdout

    result = _run_gate(tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "GATE FAIL" in (result.stdout + result.stderr)
    assert "personal-attribution-agreed-with" in (result.stdout + result.stderr)


def test_gate_passes_on_approved_suppression(tmp_path: Path) -> None:
    """Control for the previous test: the same suppressed rule passes once
    it is explicitly present in the committed allowlist — proving the gate
    discriminates reviewed suppressions from new/unreviewed ones, rather
    than failing on every suppression unconditionally."""
    (tmp_path / "f.txt").write_text(
        f"{_ATTRIBUTION_PREFIX} {_ATTRIBUTION_INITIALS} on the approach\n"
    )
    (tmp_path / ".public-safe-lintignore").write_text("personal-attribution-agreed-with f.txt\n")
    (tmp_path / ".public-safe-lint-suppression-allowlist").write_text(
        "personal-attribution-agreed-with\n"
    )
    result = _run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE OK" in result.stdout


def test_repo_own_allowlist_covers_current_suppressions(tmp_path: Path) -> None:
    """Running the gate against a CLEAN EXPORT of athenaeum's own committed
    HEAD (never the live working tree -- gitignored dirs like `.venv`
    produce hundreds of false hits a real clone/CI checkout never sees)
    must pass, proving the committed
    .public-safe-lint-suppression-allowlist actually matches the repo's
    current suppression state rather than being aspirational."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "HEAD"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(export_dir)], input=archive.stdout, check=True)

    result = _run_gate(export_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE OK" in result.stdout


@pytest.fixture()
def bare_repo_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway bare 'remote' plus a clone with this repo's gate script,
    linter, and pre-push hook copied in and wired via core.hooksPath -- for
    exercising the hook end-to-end without touching the real athenaeum repo
    or network."""
    env = _clean_git_env()
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "develop", str(remote)], check=True, env=env
    )
    subprocess.run(["git", "init", "-q", "-b", "develop", str(work)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "t@example.com"], check=True, env=env
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True, env=env
    )

    # Bring in the real, current scripts under test.
    (work / "scripts").mkdir()
    (work / "scripts" / "public-safe-lint-gate.sh").write_bytes(GATE.read_bytes())
    (work / "scripts" / "public-safe-lint-gate.sh").chmod(0o755)
    (work / "scripts" / "git-push-safe.sh").write_bytes(PUSH_WRAPPER.read_bytes())
    (work / "scripts" / "git-push-safe.sh").chmod(0o755)
    (work / "public-safe-lint.sh").write_bytes(LINTER.read_bytes())
    (work / "public-safe-lint.sh").chmod(0o755)
    (work / ".githooks").mkdir()
    (work / ".githooks" / "pre-push").write_bytes(PRE_PUSH_HOOK.read_bytes())
    (work / ".githooks" / "pre-push").chmod(0o755)
    subprocess.run(
        ["git", "-C", str(work), "config", "core.hooksPath", ".githooks"], check=True, env=env
    )

    (work / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "-u", "origin", "develop"], check=True, env=env
    )

    return remote, work


def test_pre_push_hook_blocks_seeded_leak(bare_repo_and_clone: tuple[Path, Path]) -> None:
    _remote, work = bare_repo_and_clone
    (work / "leak.md").write_text(f"refs {_HASH}{_ISSUE_DIGITS_B} in prose\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, env=_clean_git_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "add leaking doc"],
        check=True,
        env=_clean_git_env(),
    )

    result = subprocess.run(
        ["git", "-C", str(work), "push", "origin", "develop"],
        capture_output=True,
        text=True,
        env=_clean_git_env(),
    )
    assert result.returncode != 0
    assert "bare-issue-ref" in (result.stdout + result.stderr)


def test_pre_push_hook_bypass_is_recorded_and_allows_push(
    bare_repo_and_clone: tuple[Path, Path],
) -> None:
    _remote, work = bare_repo_and_clone
    (work / "leak.md").write_text(f"refs {_HASH}{_ISSUE_DIGITS_B} in prose\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, env=_clean_git_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "add leaking doc"],
        check=True,
        env=_clean_git_env(),
    )

    result = subprocess.run(
        ["git", "-C", str(work), "push", "origin", "develop"],
        capture_output=True,
        text=True,
        env={
            **_clean_git_env(),
            "SKIP_PUBLIC_LINT": "1",
            "SKIP_PUBLIC_LINT_REASON": "no-doc-change",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BYPASSED (recorded)" in result.stderr

    bypass_log = work / ".git" / "public-safe-lint-bypass.log"
    assert bypass_log.is_file()
    content = bypass_log.read_text()
    assert "reason=no-doc-change" in content
    assert "branch=develop" in content


def test_pre_push_hook_rejects_bypass_without_valid_reason(
    bare_repo_and_clone: tuple[Path, Path],
) -> None:
    _remote, work = bare_repo_and_clone
    (work / "leak.md").write_text(f"refs {_HASH}{_ISSUE_DIGITS_B} in prose\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, env=_clean_git_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "add leaking doc"],
        check=True,
        env=_clean_git_env(),
    )

    result = subprocess.run(
        ["git", "-C", str(work), "push", "origin", "develop"],
        capture_output=True,
        text=True,
        env={**_clean_git_env(), "SKIP_PUBLIC_LINT": "1"},
    )
    assert result.returncode != 0
    assert "ambiguous bypass" in (result.stdout + result.stderr)


def test_push_wrapper_survives_hostile_hookspath_override(
    bare_repo_and_clone: tuple[Path, Path],
) -> None:
    """Regression test for a real collision found while building this
    issue: some containerized dev environments set
    GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n to force
    core.hooksPath to an unrelated, workspace-wide directory for their own
    hook needs. Env-based git config outranks this repo's own
    `.git/config` setting, so a plain `git push` silently skips the gate
    in that environment. scripts/git-push-safe.sh uses `git -c
    core.hooksPath=...`, which outranks even that environment override --
    this test recreates the exact collision and proves the wrapper still
    catches a seeded leak."""
    _remote, work = bare_repo_and_clone
    (work / "leak.md").write_text(f"refs {_HASH}{_ISSUE_DIGITS_B} in prose\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, env=_clean_git_env())
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "add leaking doc"],
        check=True,
        env=_clean_git_env(),
    )

    hostile_env = {
        **_clean_git_env(),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        # Points somewhere with no pre-push hook at all -- the same shape
        # as the real collision (an unrelated, existing hooks directory).
        "GIT_CONFIG_VALUE_0": str(work.parent / "elsewhere-hooks"),
    }
    (work.parent / "elsewhere-hooks").mkdir(exist_ok=True)

    # Sanity: plain `git push` under the hostile override does NOT run our
    # hook, so the leak sails through -- proving the collision is real,
    # not a straw man.
    unguarded = subprocess.run(
        ["git", "-C", str(work), "push", "origin", "develop"],
        capture_output=True,
        text=True,
        env=hostile_env,
    )
    assert unguarded.returncode == 0, unguarded.stdout + unguarded.stderr

    # Reset the remote back to before that leak landed, so the wrapper's
    # push below has something to push and re-exercises the same commit.
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "--force", "HEAD~1:refs/heads/develop"],
        check=True,
        env=hostile_env,
    )

    guarded = subprocess.run(
        ["bash", str(work / "scripts" / "git-push-safe.sh"), "origin", "develop"],
        capture_output=True,
        text=True,
        env=hostile_env,
    )
    assert guarded.returncode != 0, guarded.stdout + guarded.stderr
    assert "bare-issue-ref" in (guarded.stdout + guarded.stderr)


# --- bash 3.2 portability (Seer HIGH finding on PR athenaeum#1233) ----------
#
# The gate ran `mapfile -t ARR < <(...)` at two call sites. `mapfile` (and its
# synonym `readarray`) is a bash 4.0 builtin; stock macOS ships GNU bash
# 3.2.57 at /bin/bash, and `.githooks/pre-push` invokes the gate through
# `bash`. Every macOS contributor's push therefore died with
# `mapfile: command not found` -- a leak gate that hard-fails on the majority
# developer platform. These two tests are the regression guard.

#: Shell scripts on the push path (or that CONTRIBUTING tells contributors to
#: run), all of which must stay bash-3.2-clean.
PUSH_PATH_SHELL_SCRIPTS = [
    REPO_ROOT / "scripts" / "public-safe-lint-gate.sh",
    REPO_ROOT / "scripts" / "install-git-hooks.sh",
    REPO_ROOT / "scripts" / "git-push-safe.sh",
    REPO_ROOT / "scripts" / "run-tests.sh",
    REPO_ROOT / ".githooks" / "pre-push",
    REPO_ROOT / "public-safe-lint.sh",
]

#: (regex, human explanation) for constructs that need bash >= 4.0. Written
#: as regexes over the script source rather than a shell-version probe, so
#: this test has teeth on CI (where /bin/bash is 5.x and a version-gated
#: executable test would skip vacuously).
BASH4_ONLY_CONSTRUCTS = [
    (r"(?<![\w-])mapfile\b", "mapfile is a bash 4.0 builtin"),
    (r"(?<![\w-])readarray\b", "readarray is a bash 4.0 builtin"),
    (r"\bdeclare\s+-[A-Za-z]*[Ag]", "declare -A/-g need bash 4.0"),
    (r"\blocal\s+-[A-Za-z]*A", "local -A needs bash 4.0"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^", "${var^^} needs bash 4.0"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*,,", "${var,,} needs bash 4.0"),
    (r"(?<![\w-])coproc\b", "coproc needs bash 4.0"),
    (r"(?<![\w-])globstar\b", "globstar needs bash 4.0"),
    (r"\bwait\s+-n\b", "wait -n needs bash 4.3"),
    (r"\bread\b[^\n|]*\s-[A-Za-z]*[iN]\b", "read -i/-N need bash 4.0"),
    (r"\|&", "|& needs bash 4.0"),
    (r"&>>", "&>> needs bash 4.0"),
]


@pytest.mark.parametrize("script", PUSH_PATH_SHELL_SCRIPTS, ids=lambda p: p.name)
def test_push_path_scripts_are_bash_3_2_portable(script: Path) -> None:
    """No bash-4-only construct on the push path.

    stock macOS /bin/bash is 3.2.57; the pre-push hook runs these through
    `bash`, so a bash-4-only builtin breaks pushing for every macOS
    contributor (athenaeum#1104, Seer finding on PR athenaeum#1233).
    """
    assert script.exists(), f"{script} missing -- update PUSH_PATH_SHELL_SCRIPTS"
    source = script.read_text()
    offenders = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # a comment naming the construct (like this file does)
        for pattern, why in BASH4_ONLY_CONSTRUCTS:
            if re.search(pattern, line):
                offenders.append(f"{script.name}:{lineno}: {why} -- {stripped}")
    assert not offenders, "bash-4-only construct(s) on the push path:\n" + "\n".join(offenders)


def _bin_bash_major() -> int | None:
    try:
        out = subprocess.run(
            ["/bin/bash", "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
            capture_output=True,
            text=True,
        )
    except OSError:  # pragma: no cover - no /bin/bash at all
        return None
    return int(out.stdout.strip()) if out.stdout.strip().isdigit() else None


@pytest.mark.skipif(
    (_bin_bash_major() or 99) >= 4,
    reason="/bin/bash is >= 4.0 here; the static test above is the portable guard",
)
def test_gate_runs_under_system_bash_3_2(tmp_path: Path) -> None:
    """Executable companion: on a host whose /bin/bash IS 3.2 (macOS), the
    gate must actually complete under it -- both the empty-suppression-set
    path and the non-empty one, since bash 3.2 also trips `set -u` on an
    empty array without the `${ARR[@]:-}` guard."""
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "README.md").write_text("hello\n")
    empty = subprocess.run(
        ["/bin/bash", str(GATE), str(clean), str(LINTER)],
        capture_output=True,
        text=True,
    )
    combined = empty.stdout + empty.stderr
    assert "command not found" not in combined, combined
    assert empty.returncode == 0, combined
    assert "GATE OK" in empty.stdout, combined

    supp = tmp_path / "supp"
    supp.mkdir()
    (supp / "notes.md").write_text(f"see issue {_HASH}{_ISSUE_DIGITS_B} for details\n")
    (supp / ".public-safe-lintignore").write_text("bare-issue-ref\tnotes.md\n")
    (supp / ".public-safe-lint-suppression-allowlist").write_text("bare-issue-ref\n")
    nonempty = subprocess.run(
        ["/bin/bash", str(GATE), str(supp), str(LINTER)],
        capture_output=True,
        text=True,
    )
    combined = nonempty.stdout + nonempty.stderr
    assert "command not found" not in combined, combined
    assert nonempty.returncode == 0, combined
    assert "bare-issue-ref" in nonempty.stdout, combined


# ---------------------------------------------------------------------------
# Email-domain guard (athenaeum#1443)
#
# Fixture and source email addresses must live on RFC 2606 / RFC 6761
# reserved domains, not real ones someone else owns -- the same rule
# code-workspace-config/scripts/_gh-email-guard.sh already enforces on
# outbound gh writes. This is the "narrow allowance" version of that rule
# for the corpus itself.
#
# IMPORTANT: this test's own file is IN SCOPE of the scan it defines
# (it lives in tests/, which the scan walks). Every negative-control
# address below is therefore built BY CONSTRUCTION (string concatenation,
# f-string interpolation, or a scratch file under tmp_path) so the raw
# source text of this file never contains a forbidden address as a
# contiguous literal -- a plain grep of this file would not find one either.
# ---------------------------------------------------------------------------

EMAIL_SHAPE_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: RFC 2606 (example.com/.net/.org) plus RFC 6761 (.example/.invalid/.test/
#: .localhost) reserved names. A domain is reserved if it equals one of
#: these or is a subdomain of one (e.g. ``mail.example.com``,
#: ``sub.corp.example``).
RESERVED_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "example.com",
    "example.net",
    "example.org",
    "example",  # RFC 6761 .example TLD -- covers any *.example domain
    "invalid",  # RFC 6761 .invalid TLD
    "test",  # RFC 6761 .test TLD
    "localhost",  # RFC 6761 .localhost TLD
)

#: Genuine infrastructure addresses -- not a person's contact data. The
#: issue's own three (git@github.com, *@users.noreply.github.com,
#: open-source@kromatic.com) plus two judgement-call additions made while
#: building this guard: ``git@gitlab.com`` and ``git@bitbucket.org`` are
#: already-documented siblings of ``git@github.com`` in
#: ``src/athenaeum/pii.py``'s ``SERVICE_ADDRESSES`` (issue athenaeum#507) --
#: the same SSH pseudo-user shape, just the other two git hosts the corpus
#: references. Not in the issue's literal list, but squarely the same
#: category, so allowlisted here with the reason on record rather than
#: migrated (migrating them would falsify the product's own fixture data).
INFRA_EXACT_ADDRESSES: frozenset[str] = frozenset(
    {
        "git@github.com",
        "open-source@kromatic.com",
        "git@gitlab.com",
        "git@bitbucket.org",
    }
)

#: Genuine infrastructure domains (any local-part), matched as an exact
#: domain or a suffix of one. ``group.calendar.google.com`` and
#: ``iam.gserviceaccount.com`` are the other two judgement-call additions:
#: both are already recognized as service-identifier domains, not contact
#: data, by ``src/athenaeum/pii.py`` and ``src/athenaeum/pii_restore.py``
#: (issue athenaeum#507) -- a Calendar group id and a GCP service-account
#: pseudo-user are transport identifiers, the same reasoning that exempts
#: ``git@github.com``.
INFRA_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "users.noreply.github.com",
    "group.calendar.google.com",
    "iam.gserviceaccount.com",
)


def _is_reserved_or_infra(address: str) -> bool:
    """True when *address* (an email-shaped token) is safe to appear in the
    corpus: a reserved RFC 2606/6761 domain, or named genuine
    infrastructure."""
    lowered = address.lower()
    if lowered in INFRA_EXACT_ADDRESSES:
        return True
    domain = lowered.rsplit("@", 1)[-1]
    for suffix in INFRA_DOMAIN_SUFFIXES:
        if domain == suffix or domain.endswith("." + suffix):
            return True
    for suffix in RESERVED_DOMAIN_SUFFIXES:
        if domain == suffix or domain.endswith("." + suffix):
            return True
    return False


def find_non_reserved_emails(text: str) -> list[str]:
    """Every email-shaped token in *text* whose domain is neither reserved
    nor named infrastructure."""
    return [addr for addr in EMAIL_SHAPE_RE.findall(text) if not _is_reserved_or_infra(addr)]


def _tracked_files(*subdirs: str) -> list[Path]:
    """Tracked files under *subdirs* via ``git ls-files`` -- never an
    unbounded ``rglob``, which would also walk ``.venv/``, ``.git/`` and any
    other untracked vendored tree a lane happens to have on disk."""
    result = subprocess.run(
        ["git", "ls-files", *subdirs],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / p for p in result.stdout.splitlines() if p]


class TestNoNonReservedEmailDomains:
    """athenaeum#1443 -- no string in ``src/athenaeum/`` or ``tests/`` may
    carry an email address on a real, non-reserved domain."""

    def test_repo_has_no_non_reserved_email_addresses(self) -> None:
        offenders: list[str] = []
        for path in _tracked_files("src/athenaeum", "tests"):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for addr in find_non_reserved_emails(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {addr}")
        assert not offenders, (
            "email address(es) on a non-reserved domain found -- migrate to "
            "example.com/.net/.org, a *.example/*.invalid/*.test/*.localhost "
            "subdomain, or add a justified allowance to INFRA_EXACT_ADDRESSES "
            "/ INFRA_DOMAIN_SUFFIXES above:\n" + "\n".join(offenders)
        )

    def test_predicate_flags_a_synthetic_non_reserved_address(self) -> None:
        """AC3, minimally: a made-up address on a real-shaped domain, built
        from parts so this file's own source never contains it as a
        contiguous literal, is caught by the predicate."""
        local = "jo" + "hn"
        domain = "acme" + "." + "com"
        synthetic = f"{local}@{domain}"
        assert find_non_reserved_emails(f"contact {synthetic} for details") == [synthetic]

    def test_predicate_flags_each_migrated_address_if_reintroduced(
        self, tmp_path: Path
    ) -> None:
        """AC3 concretely: the four addresses this issue migrated out of the
        tree, reassembled from parts and written into a SCRATCH file (never
        this repo), are each still caught by the exact predicate the
        repo-wide scan above uses -- proving a regression would fail CI."""
        migrated_local_domain_parts = [
            ("jane" + ".doe", "acme" + ".com"),
            ("sales", "acme" + ".com"),
            ("foo", "bar" + ".com"),
            ("jo.5551234567", "x" + ".com"),
        ]
        for local, domain in migrated_local_domain_parts:
            address = f"{local}@{domain}"
            scratch = tmp_path / "reintroduced.py"
            scratch.write_text(f'FIXTURE_EMAIL = "{address}"\n', encoding="utf-8")
            found = find_non_reserved_emails(scratch.read_text(encoding="utf-8"))
            assert found == [address], f"predicate failed to flag {address!r}"

    def test_reserved_and_infra_addresses_are_not_flagged(self) -> None:
        """Negative control: the reserved set and the named infrastructure
        allowance are not false positives."""
        allowed = [
            "person@example.com",
            "person@example.net",
            "person@example.org",
            "person@sub.example.com",
            "person@corp.example",
            "person@thing.invalid",
            "person@thing.test",
            "person@thing.localhost",
            "git@github.com",
            "GIT@GitHub.com",
            "someone@users.noreply.github.com",
            "open-source@kromatic.com",
            "git@gitlab.com",
            "git@bitbucket.org",
            "cal-abc@group.calendar.google.com",
            "svc@my-project.iam.gserviceaccount.com",
        ]
        for addr in allowed:
            assert find_non_reserved_emails(f"see {addr}") == [], addr
