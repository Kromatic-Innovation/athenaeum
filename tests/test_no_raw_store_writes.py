# SPDX-License-Identifier: Apache-2.0
"""Guard against raw ``.write_text()`` re-accumulating on store-path modules (issue athenaeum#534).

The modules listed in :data:`_STORE_PATH_MODULES` mutate the durable knowledge
base — wiki pages, raw intake, durable ``_index.md``/manifest files, and other
content under ``knowledge_root``. A plain ``Path.write_text(...)`` there can
leave a half-written file behind if the process is killed mid-write, corrupting
whatever parser reads it next.

Issue athenaeum#534 converted every raw store-path ``.write_text(`` call in these
modules to :func:`athenaeum.atomic_io.atomic_write_text`, which writes to a
same-directory temp file, ``fsync``s it, and ``os.replace``s it over the
target — so readers only ever see the complete old file or the complete new
one, never a torn write. This test scans each module's source and fails if a
raw ``.write_text(`` call reappears, so a future edit can't silently
reintroduce a non-atomic durable write.

Only modules that were FULLY converted in issue athenaeum#534 are listed here. Modules
with legitimate non-store ``.write_text`` usage (cache/config/CLI/temp
files) — ``init.py``, ``config.py``, ``cli.py``, ``search.py``, ``repair.py`` —
are intentionally excluded; they are out of scope for this invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# Store-path modules fully converted to atomic_write_text in issue athenaeum#534.
_STORE_PATH_MODULES = [
    "claim_kind.py",
    "pending_merges.py",
    "_cmd_authority.py",
    "memory_index.py",
    "retire.py",
    "answers.py",
    "dedupe.py",
    "tiers.py",
    "librarian.py",
    "batch.py",
    "merge.py",
    "resolutions.py",
    "clusters.py",
    "mcp_server.py",
]


@pytest.mark.parametrize("module_name", _STORE_PATH_MODULES)
def test_no_raw_write_text_in_store_path_module(module_name: str) -> None:
    """Durable-write modules must use atomic_write_text, never raw .write_text().

    A regression here means someone added a plain ``Path.write_text(...)`` call
    to a module that mutates the durable knowledge base. Replace it with
    ``atomic_write_text(path, text)`` from :mod:`athenaeum.atomic_io`.
    """
    source = (_SRC_ROOT / module_name).read_text(encoding="utf-8")
    assert ".write_text(" not in source, (
        f"{module_name} contains a raw .write_text( call — durable store-path "
        "writes must use athenaeum.atomic_io.atomic_write_text instead "
        "(issue athenaeum#534)"
    )
