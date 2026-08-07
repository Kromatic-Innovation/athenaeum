# SPDX-License-Identifier: Apache-2.0
"""Every ``tests/*.py`` path named in a DRIFT GUARD comment must exist (issue
athenaeum#799).

A drift-guard comment's entire purpose is to tell a future editor which
sites must change together. A guard naming a file that does not exist is
worse than no guard at all: an editor either stalls looking for the missing
file, or trusts the guard and believes a site is covered when nothing covers
it. This happened for real — `docs/field-corrections.md` §6.1 (merged in
athenaeum#796) transcribed a stale pair of deleted files straight out of
`resolutions.py`'s comment, and shipped a normative guard citing two
nonexistent files (caught and corrected in athenaeum#798).

This test scans every ``src/athenaeum/**/*.py`` module for ``DRIFT GUARD``
comment/docstring blocks, extracts every ``tests/...py`` path mentioned
inside each block, and asserts each one exists on disk. It PARSES the
in-tree guards rather than transcribing an expected list, so a future guard
that names a new (or renamed) test file is checked automatically, and it
asserts its own denominator — a parser that silently matches nothing must
fail loudly rather than passing vacuously forever (the same positive-control
discipline `docs/field-corrections.md` §6.1 demands of the precedence
drift-guard test in `tests/test_precedence.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "athenaeum"

_DRIFT_GUARD_MARKER = "DRIFT GUARD"
# A referenced test-file path: `tests/...py`, optionally wrapped in
# backticks/double-backticks (reST/Markdown code spans) and optionally
# followed by `::test_name` (dropped — only the file path is checked).
_TEST_PATH_RE = re.compile(r"tests/[\w./-]+\.py")


def _drift_guard_blocks(text: str) -> list[str]:
    """Return the text of each DRIFT GUARD block in a module's source.

    A block starts at the line containing the literal marker and extends
    through subsequent non-blank lines — this covers both conventions in
    use: a contiguous ``#``-comment block, and a docstring paragraph (which
    ends at the next blank line either way).
    """
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if _DRIFT_GUARD_MARKER in lines[i]:
            block: list[str] = []
            j = i
            while j < len(lines) and lines[j].strip() != "":
                block.append(lines[j])
                j += 1
            blocks.append("\n".join(block))
            i = j
        else:
            i += 1
    return blocks


def _extract_drift_guard_test_paths() -> set[str]:
    paths: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for block in _drift_guard_blocks(text):
            paths.update(_TEST_PATH_RE.findall(block))
    return paths


def test_drift_guard_test_paths_exist() -> None:
    """Every `tests/*.py` path named inside a DRIFT GUARD block must be real."""
    paths = _extract_drift_guard_test_paths()

    # Positive control: fail loudly if the parser silently matched nothing
    # rather than passing vacuously forever — mirrors the denominator
    # assertion `docs/field-corrections.md` §6.1 requires of the sibling
    # precedence drift-guard test.
    assert paths, (
        "no tests/*.py paths were extracted from any DRIFT GUARD block under "
        "src/athenaeum/ — the parser is broken (or every guard was removed), "
        "either of which defeats this test's purpose"
    )

    missing = sorted(p for p in paths if not (_REPO_ROOT / p).exists())
    assert not missing, (
        f"DRIFT GUARD comment(s) under src/athenaeum/ reference test file(s) "
        f"that do not exist on disk: {missing}. A guard naming a missing file "
        f"is worse than no guard — fix the path or the guard (athenaeum#799)."
    )
