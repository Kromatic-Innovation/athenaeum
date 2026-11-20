# SPDX-License-Identifier: Apache-2.0
"""The per-repository sync lease inside the git directory (athenaeum#2997, slice S11).

Sections 4 (step 1) and 9 of ``docs/design/federation-multi-writer-sync.md``. The
run lock (:class:`athenaeum.runlock.RunLock`) is at the knowledge root and serialises
processes that share one root. Two installs on one machine can still mount the same
clone of a store, so each repository in ``integrate`` mode takes one lease on a lock
file **inside that repository's git directory** for steps 2 to 7 of the sequence.
Nothing inside ``.git`` is tracked, so the file is never committed and never reaches
another writer.

The primitive is unchanged: :meth:`athenaeum.store.FilesystemStore.lease`, a
single-machine ``flock`` that the kernel drops when its holder dies. No lease spans
machines (section 9 rejects one).

The sequence spans the compile, so the lease outlives one call: it is taken by
:func:`acquire_repo_lease` (from ``sync_before_compile`` and ``push_after_compile``)
and kept in a process-level table, so a second ask by the *same* process is a
re-entry rather than a self-deadlock (two ``flock`` descriptors on one file conflict
even in one process). :func:`release_repo_lease` ends it after the push;
:func:`release_all_leases` is the safety net for a run that ends without one.

Layering: L4. Imports the store seam only.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import threading
from contextlib import AbstractContextManager
from pathlib import Path

from athenaeum.storage import open_store
from athenaeum.store import Lease, LeaseHeldError, git_dir

log = logging.getLogger(__name__)

__all__ = [
    "LEASE_FILE",
    "LEASE_HELD",
    "LEASE_NOT_TAKEN",
    "LEASE_TAKEN",
    "acquire_repo_lease",
    "release_all_leases",
    "release_repo_lease",
]

#: The lock file's name inside the git directory.
LEASE_FILE = "athenaeum-sync.lock"

LEASE_TAKEN = "taken"  # this call took it, or this process already held it
LEASE_HELD = "held-elsewhere"  # another process holds it: do not touch the repository
LEASE_NOT_TAKEN = "unavailable"  # no git directory or no flock: proceed unguarded

_TTL_SECONDS = 0.0  # unused by the filesystem adapter; the kernel releases on death
_held: dict[Path, AbstractContextManager[Lease]] = {}
_guard = threading.Lock()


def acquire_repo_lease(repo: Path) -> tuple[str, str]:
    """Try once to take *repo*'s lease; return ``(status, reason)``.

    Never blocks and never raises: a contended lease is ``LEASE_HELD`` and the
    caller skips the repository. ``LEASE_NOT_TAKEN`` (not a git repository, or a
    platform without ``flock``) lets the sync proceed as it did before, which is
    what :class:`~athenaeum.runlock.RunLock` does on such a platform.
    """
    try:
        gdir = git_dir(repo)
    except (subprocess.CalledProcessError, OSError):
        return LEASE_NOT_TAKEN, "no-git-dir"
    key = gdir.resolve()
    with _guard:
        if key in _held:
            return LEASE_TAKEN, ""
        lease = open_store(None, gdir).lease(LEASE_FILE, _TTL_SECONDS)
        try:
            lease.__enter__()
        except LeaseHeldError:
            return LEASE_HELD, "lease-held"
        except RuntimeError:  # no fcntl
            return LEASE_NOT_TAKEN, "no-flock"
        except OSError as exc:
            log.warning("athenaeum sync: lease unavailable in %s: %s", repo, type(exc).__name__)
            return LEASE_NOT_TAKEN, "lease-error"
        _held[key] = lease
    return LEASE_TAKEN, ""


def release_repo_lease(repo: Path) -> None:
    """Drop *repo*'s lease if this process holds it; a no-op otherwise."""
    try:
        key = git_dir(repo).resolve()
    except (subprocess.CalledProcessError, OSError):
        return
    with _guard:
        lease = _held.pop(key, None)
    if lease is not None:
        lease.__exit__(None, None, None)


def release_all_leases() -> None:
    """Drop every lease this process holds (end of a run, and at exit)."""
    with _guard:
        leases = list(_held.values())
        _held.clear()
    for lease in leases:
        lease.__exit__(None, None, None)


atexit.register(release_all_leases)
