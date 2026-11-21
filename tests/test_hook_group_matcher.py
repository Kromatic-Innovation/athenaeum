# SPDX-License-Identifier: Apache-2.0
"""Every shipped Claude-format hook group carries a ``matcher`` key.

Cursor's CLI runs a Claude-format hook group only when the group has a
``matcher`` key (even ``""``) and skips one without it, silently -- a Cursor
user following our setup would get no per-turn push and no error. Claude
Code accepts the key and fires every hook either way, so shipped examples
always carry it (``""`` where no filter is wanted).

The test discovers examples instead of naming files: every ``*.json`` under
``examples/`` and every fenced ``json`` block in the markdown under ``docs/``,
``examples/``, ``skills/`` and the top-level ``*.md`` files. A block is
checked when it holds a ``hooks`` mapping whose event arrays contain groups
(dicts with an inner ``hooks`` list).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
_FENCE = re.compile(r"^```json[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_SKIP_PARTS = {".venv", "node_modules", ".git"}


def _parse(text: str) -> Any | None:
    """Parse a whole JSON document, or a bare ``"hooks": {...}`` fragment."""
    for candidate in (text, "{" + text + "}"):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _groups(doc: Any) -> Iterator[dict[str, Any]]:
    """Yield every hook group: a dict with an inner ``hooks`` list, found in
    an event array of any ``hooks`` mapping nested anywhere in *doc*."""
    if isinstance(doc, dict):
        mapping = doc.get("hooks")
        if isinstance(mapping, dict):
            for entries in mapping.values():
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                            yield entry
        for value in doc.values():
            yield from _groups(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _groups(value)


def _sources() -> Iterator[tuple[str, Any]]:
    def keep(path: Path) -> bool:
        return not (_SKIP_PARTS & set(path.relative_to(_REPO).parts))

    for path in sorted((_REPO / "examples").rglob("*.json")):
        if keep(path):
            yield str(path.relative_to(_REPO)), _parse(path.read_text(encoding="utf-8"))
    md_files = [
        *sorted(_REPO.glob("*.md")),
        *sorted((_REPO / "docs").rglob("*.md")),
        *sorted((_REPO / "examples").rglob("*.md")),
        *sorted((_REPO / "skills").rglob("*.md")),
    ]
    for path in md_files:
        if not keep(path):
            continue
        text = path.read_text(encoding="utf-8")
        for i, block in enumerate(_FENCE.findall(text)):
            yield f"{path.relative_to(_REPO)} (json block {i + 1})", _parse(block)


def _missing_matcher() -> tuple[list[str], int]:
    missing: list[str] = []
    checked = 0
    for label, doc in _sources():
        for group in _groups(doc):
            checked += 1
            if "matcher" not in group:
                missing.append(label)
    return missing, checked


def test_every_shipped_hook_group_has_a_matcher_key() -> None:
    missing, checked = _missing_matcher()
    assert checked >= 6, f"discovery found only {checked} hook groups -- sweep broken?"
    assert not missing, (
        "hook group(s) without a `matcher` key (Cursor skips these silently; "
        f'add `"matcher": ""`): {sorted(set(missing))}'
    )


@pytest.mark.parametrize(
    "path",
    [
        "examples/claude-code/settings-snippet.json",
        "examples/claude-code-plugin/hooks/hooks.json",
    ],
)
def test_the_named_examples_are_covered_by_discovery(path: str) -> None:
    labels = {label for label, doc in _sources() if any(True for _ in _groups(doc))}
    assert path in labels


def test_a_group_without_a_matcher_is_detected() -> None:
    bad = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    good = {"hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": []}]}}
    assert [g for g in _groups(bad) if "matcher" not in g]
    assert not [g for g in _groups(good) if "matcher" not in g]
    # a bare `"hooks": {...}` fragment, as docs show it, is parsed too
    assert _parse('"hooks": {"A": [{"hooks": []}]}') is not None
