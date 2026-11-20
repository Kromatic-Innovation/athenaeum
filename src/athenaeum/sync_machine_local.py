# SPDX-License-Identifier: Apache-2.0
"""The check that no machine-local file is tracked in a shared repository (athenaeum#2994).

Slice S6b of ``docs/design/federation-multi-writer-sync.md``, the machine-local row
of section 5. The artifact registry (:data:`athenaeum.store.ARTIFACT_REGISTRY`)
marks some files ``operational`` with scope ``machine-local``: state that belongs
to one machine. In a repository several machines write, a tracked copy would merge
one machine's state into every other.

:func:`find_tracked_machine_local` lists the tracked paths whose base name a
machine-local registry entry declares (``ArtifactDeclaration.file_names``). It
reads the git index through the store seam and changes nothing: it never untracks,
moves or deletes a file. A file that exists only in the working tree (untracked or
ignored) is not listed, and neither is a tracked ``store-durable`` file.

Layering: L4. Imports the store seam only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from athenaeum.store import ARTIFACT_REGISTRY, list_tracked_files

__all__ = ["find_tracked_machine_local", "machine_local_file_names"]


def machine_local_file_names() -> frozenset[str]:
    """Base names of every ``operational`` / ``machine-local`` registry artifact."""
    return frozenset(
        name
        for decl in ARTIFACT_REGISTRY
        if decl.persistence_class == "operational" and decl.operational_scope == "machine-local"
        for name in decl.file_names
    )


def find_tracked_machine_local(repo: Path) -> tuple[str, ...]:
    """Tracked paths of *repo* that the registry says are machine-local, sorted.

    Raises ``subprocess.CalledProcessError`` when *repo* is not a git repository.
    """
    names = machine_local_file_names()
    return tuple(p for p in list_tracked_files(repo) if PurePosixPath(p).name in names)


def check_repo(repo: Path) -> tuple[tuple[str, ...], str]:
    """``(paths, error)``: the findings, or ``((), error-class-name)`` when git failed."""
    try:
        return find_tracked_machine_local(repo), ""
    except (subprocess.CalledProcessError, OSError) as exc:
        return (), type(exc).__name__
