# SPDX-License-Identifier: Apache-2.0
"""Atomic whole-file writes for the pending-* sidecars (issue #309).

The ``wiki/_pending_questions.md`` and ``wiki/_pending_merges.md`` sidecars
(plus their ``_archive.md`` siblings) are mutated by a read-existing +
append-block + write-whole-file pattern. A crash partway through a plain
``Path.write_text`` can leave a half-written file whose ``---`` block structure
is corrupt, breaking the parsers that split it into per-entry blocks.

:func:`atomic_write_text` writes to a temp file in the SAME directory (so the
rename stays on one filesystem) and then :func:`os.replace`s it over the
target. ``os.replace`` is atomic on POSIX and Windows: readers and the parsers
see either the complete old file or the complete new one, never a torn write.
The run lock (:mod:`athenaeum.runlock`) already serializes librarian processes;
this is defense-in-depth against a crash mid-append.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with *text* via a same-dir temp + ``os.replace``.

    Creates the parent directory if needed. On any failure before the rename,
    the temp file is cleaned up and the original *path* is left untouched.

    When *path* already exists, its permission bits are preserved: ``mkstemp``
    creates the temp file ``0600``, which would otherwise silently narrow the
    target's mode on every rewrite, so we ``chmod`` the temp to match the
    existing target before the rename.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # Preserve the target's existing permission bits (mkstemp made us 0600).
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
