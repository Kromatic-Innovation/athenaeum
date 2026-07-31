# SPDX-License-Identifier: Apache-2.0
"""Parametrized golden-snapshot pin over every registered LLM prompt (issue #561).

This subsumes the old single ``_RESOLVE_SYSTEM`` snapshot pin
(``tests/test_resolve_system_snapshot.py`` / ``tests/data/resolve_system.txt``,
both removed): every prompt in ``athenaeum.prompt_registry.PROMPTS`` is now byte-
pinned to a golden under ``tests/data/prompts/``, so a prompt edit buried in a
triple-quoted literal cannot land without a reviewer seeing it as a prompt diff.

The test is deliberately offline — zero runtime, zero network, no API client — so
it is compatible with the eval-light default (``-m 'not eval'``). Snapshots answer
"did prompt bytes change without a reviewer seeing it as a prompt change?"; evals
answer a different question and stay where they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.prompt_registry import (
    PROMPTS,
    parse_golden,
    prompt_manifest,
    render_docs,
    render_golden,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN_DIR = _REPO_ROOT / "tests" / "data" / "prompts"
_DOCS = _REPO_ROOT / "docs" / "prompts.md"
_REGEN = "python -m athenaeum.prompt_registry --write"


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_prompt_matches_golden(name: str) -> None:
    path = _GOLDEN_DIR / f"{name}.txt"
    assert path.exists(), f"missing golden {path.name}; regenerate: {_REGEN}"
    expected = parse_golden(path.read_text(encoding="utf-8"))
    assert PROMPTS[name] == expected, (
        f"prompt {name!r} drifted from its golden. If the edit is intentional, run "
        f"`{_REGEN}` in the same commit so it lands as a reviewable golden diff."
    )


def test_no_orphan_or_missing_goldens() -> None:
    on_disk = {path.stem for path in _GOLDEN_DIR.glob("*.txt")}
    registered = set(PROMPTS)
    assert on_disk == registered, (
        f"golden set mismatch (missing={registered - on_disk}, "
        f"orphan={on_disk - registered}); regenerate: {_REGEN}"
    )


def test_render_golden_roundtrips() -> None:
    for name in PROMPTS:
        assert parse_golden(render_golden(name)) == PROMPTS[name]


def test_manifest_covers_registry_with_sha256() -> None:
    manifest = prompt_manifest()
    assert set(manifest) == set(PROMPTS)
    assert all(len(digest) == 64 for digest in manifest.values())


def test_docs_prompts_md_is_byte_current() -> None:
    assert _DOCS.exists(), f"missing docs/prompts.md; regenerate: {_REGEN}"
    assert _DOCS.read_text(encoding="utf-8") == render_docs(), (
        f"docs/prompts.md is stale — regenerate in the same commit: {_REGEN}"
    )
