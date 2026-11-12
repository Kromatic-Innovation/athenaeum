# SPDX-License-Identifier: Apache-2.0
"""Clear the append-only flag promptwitness ledgers set on their own temp
files, and fail the session if any test leaves one behind (athenaeum#2335).

``promptwitness.ledger.append_line`` applies best-effort append-only
protection to the first partition file of every ledger it creates --
``chflags uappnd`` on macOS, ``chattr +a`` on Linux. On macOS an ordinary
user process can set that flag and the kernel then enforces it, and the flag
blocks *deleting* the file as well as rewriting it. So every test that writes
a throwaway ledger under ``tmp_path`` leaves an undeletable file behind:
pytest's own retention cleanup cannot remove the directory, renames it to
``garbage-*``, and fails on that too. One run of the athenaeum#2266
reproduction left 58 such files; 39 accumulated ``garbage-*`` directories on
one host held 3,344 of them.

Two fixtures, with deliberately different jobs:

``_clear_promptwitness_append_only`` (function-scoped, autouse) clears
whatever the ledger protected during each test, via the library's own clear
path. It is a fixture rather than per-test teardown code so that no test can
forget it and -- because fixture finalisers run regardless of outcome -- so
that a FAILING test cleans up too, which is the case that otherwise leaks
most.

``_no_protected_ledger_files_left_behind`` (session-scoped, autouse) is the
guard that keeps the fixture above honest as the suite grows. It would be
tautological if it only re-read the in-process registry the fixture just
drained, so it reads the filesystem instead: every ledger partition records
the protection verdict in its own genesis line as ``ledger_meta.protection``,
so a scan of ``tmp_path_factory``'s base finds exactly the files the library
marked protected, and cross-checks them against ``ledger.cleared_paths()`` --
every path anyone in this process asked to unprotect, whether the autouse
fixture did it at teardown or a tamper test did it inline before its rewrite.
A marked file nobody ever cleared is a leak: written by a child process whose
registry died with it, or by a future code path that bypasses the registry.

**Portability is the point, and it is why the check reads a JSON field rather
than a filesystem flag.** No ``macos-*`` runner exists in this repo (an
operator ruling on athenaeum#2266 declined to add one), so the guard has to
fail on the Linux CI runner for a bug that only *bites* on macOS. Linux
cannot help here twice over: ``chattr +a`` needs ``CAP_LINUX_IMMUTABLE``,
which an ordinary CI process does not hold, so the flag is never actually
set; and Linux has no ``st_flags`` to read it back from even if it were. The
``ledger_meta.protection`` marker is written on every platform whenever the
library *attempts* protection -- ``"chflags:applied"`` on macOS,
``"chattr:failed"`` on Linux -- and intent is what this guard needs. Nothing
here depends on the kernel enforcing anything.
"""

from __future__ import annotations

import json
from collections.abc import Container, Iterator
from pathlib import Path

import pytest

# Ledger partitions are named ``<YYYY-MM>.jsonl``; the protection verdict is a
# field of the genesis line's ``ledger_meta``.
_PARTITION_GLOB = "*.jsonl"
_PROTECTION_FIELD = "protection"

# A verdict meaning the library never ran a protect command, so there is
# nothing for anyone to clear. Every other verdict ("chflags:applied",
# "chattr:failed", ...) means a command WAS run against that path.
_NO_ATTEMPT = "unsupported"

def unprotect_for_tamper(path: Path) -> Path:
    """Clear the append-only flag on *path* so a tamper test can rewrite it.

    A test that edits a ledger partition in place to prove the verifier
    notices must first lift the protection ``append_line`` applied, or on macOS
    (``chflags uappnd``) the rewrite raises ``PermissionError`` (athenaeum#2429).
    Uses the library's own clear path; production protection is untouched.
    Returns *path* so it can be used inline.
    """
    from promptwitness import ledger

    ledger.clear_append_only_protection(path)
    return path


@pytest.fixture(autouse=True)
def _clear_promptwitness_append_only() -> Iterator[None]:
    """Clear the append-only flag off every ledger file this test protected.

    Autouse and function-scoped so no test can opt out or forget, and so the
    finaliser runs on failure as well as on success -- the same shape as
    ``_reset_t1_census`` and ``_reset_model_rates`` in ``tests/conftest.py``,
    for the same reason: process-global state one test sets must not survive
    into the next.

    Imported inside the body, not at module scope, so merely collecting the
    suite does not import ``promptwitness``.
    """
    yield
    from promptwitness import ledger

    for path in ledger.protected_paths():
        ledger.clear_append_only_protection(path)


@pytest.fixture(scope="session", autouse=True)
def _no_protected_ledger_files_left_behind(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Fail the session if a protected ledger file survives under this run's
    temp base without having been cleared.

    Scoped to ``tmp_path_factory.getbasetemp()`` -- this run's own
    ``pytest-<n>`` directory (or, under ``xdist``, this worker's subdirectory
    of it) -- so pre-existing ``garbage-*`` from earlier runs is out of scope,
    exactly as athenaeum#2335 scopes it. Cleaning those up on a given host is
    a one-off ``chflags -R nouappnd`` and explicitly not this guard's job.
    """
    yield

    from promptwitness import ledger

    basetemp = tmp_path_factory.getbasetemp()
    leaked = leaked_protected_files(basetemp, ledger.cleared_paths())
    if leaked:
        shown = "\n  ".join(leaked[:20])
        more = "" if len(leaked) <= 20 else f"\n  ... and {len(leaked) - 20} more"
        pytest.fail(
            f"{len(leaked)} promptwitness ledger file(s) under {basetemp} were marked "
            "append-only and never cleared. On macOS the flag blocks deletion, so "
            "pytest cannot remove these directories and they accumulate as "
            "undeletable `garbage-*` (athenaeum#2335). Clear them in the owning "
            "test's teardown with "
            "`promptwitness.ledger.clear_append_only_protection_under(<dir>)` -- "
            "needed when a CHILD process wrote the ledger, since this process's "
            f"registry cannot see those.\n  {shown}{more}",
            pytrace=False,
        )


def leaked_protected_files(basetemp: Path, cleared: Container[Path]) -> list[str]:
    """Every ledger partition under *basetemp* that was marked append-only and
    whose path is not in *cleared*, sorted.

    Split out from the session fixture so it can be exercised directly against
    a synthetic tree, the way ``tests/test_default_root_guard.py`` exercises its
    own audit hook: a guard with no test proving it still fires is a guard
    nobody can tell apart from a no-op.
    """
    return sorted(
        str(path)
        for path in basetemp.rglob(_PARTITION_GLOB)
        if path not in cleared and _marks_itself_protected(path)
    )


def _marks_itself_protected(path: Path) -> bool:
    """Whether *path*'s genesis line records that the library ran a protect
    command against it.

    Tolerant by design: this runs over every ``*.jsonl`` under the temp base,
    most of which are not ledgers at all, so anything unreadable or not a
    witness line is simply "not a protected ledger partition". A guard that
    raised on unrelated fixture data would be a worse guard.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
    except (OSError, UnicodeDecodeError):
        # Decoding happens inside `readline`, not inside `json.loads`, so a
        # non-UTF-8 fixture file raises here and nowhere else.
        return False
    if not first.strip():
        return False
    try:
        line = json.loads(first)
    except ValueError:
        return False
    if not isinstance(line, dict):
        return False
    meta = line.get("ledger_meta")
    if not isinstance(meta, dict):
        return False
    verdict = meta.get(_PROTECTION_FIELD)
    return isinstance(verdict, str) and verdict != _NO_ATTEMPT
