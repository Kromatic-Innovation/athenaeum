"""Tests for compile-as-of (issue #359, §8.5).

compile-as-of RE-RUNS the deterministic C3 blend
(:func:`athenaeum.merge.merge_clusters_to_wiki`) with an ``as_of`` date
threaded into the per-member active predicate, writing a recompiled snapshot
to a scratch directory. It is distinct from slice 3's read-time ``--as-of``
filter, which only hides already-compiled pages and cannot resurrect a
member's content that the live compile already dropped.

Fixture shape: one cluster with two members —

- ``alpha`` : no ``valid_until`` (always active).
- ``bravo`` : ``valid_until: 2024-12-31`` (expired relative to any 2025+
  "today", but valid on 2024-06-01).

The live compile (as_of=today) blends only ``alpha``; compile-as-of on
2024-06-01 re-includes ``bravo``. Because the live compiled page never
contains ``bravo``'s body, no read-time filter could surface it — only a
recompile does. That contrast is the core assertion.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from athenaeum.merge import compile_as_of, merge_clusters_to_wiki

SCOPE = "-Users-tester-Code-projectx"


def _write_member(
    scope_dir: Path,
    filename: str,
    *,
    name: str,
    body: str,
    valid_until: str | None = None,
    superseded_by: str | None = None,
) -> None:
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: {}".format(name), "type: feedback"]
    if valid_until is not None:
        lines.append(f"valid_until: {valid_until}")
    if superseded_by is not None:
        lines.append(f"superseded_by: {superseded_by}")
    lines.append("---")
    text = "\n".join(lines) + "\n" + body + "\n"
    (scope_dir / filename).write_text(text, encoding="utf-8")


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


def _write_clusters(knowledge_root: Path, member_paths: list[str]) -> None:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "cluster_id": "projectx-0001",
        "member_paths": member_paths,
        "centroid_score": 0.9,
    }
    out.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def temporal_root(tmp_path: Path) -> Path:
    """One cluster; alpha (open) + bravo (valid_until 2024-12-31)."""
    knowledge_root = tmp_path / "knowledge"
    scope_dir = knowledge_root / "raw" / "auto-memory" / SCOPE
    _write_member(
        scope_dir,
        "feedback_alpha.md",
        name="alpha",
        body="Alpha guidance is stable and always current.",
    )
    _write_member(
        scope_dir,
        "feedback_bravo.md",
        name="bravo",
        body="Bravo guidance held in 2024 only.",
        valid_until="2024-12-31",
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    _write_config(knowledge_root)
    _write_clusters(
        knowledge_root,
        [f"{SCOPE}/feedback_alpha.md", f"{SCOPE}/feedback_bravo.md"],
    )
    return knowledge_root


def _page_text(wiki_dir: Path) -> str:
    pages = sorted(wiki_dir.glob("auto-*.md"))
    assert pages, f"no compiled pages under {wiki_dir}"
    return "\n".join(p.read_text(encoding="utf-8") for p in pages)


class TestCompileAsOf:
    def test_live_compile_drops_expired_member(self, temporal_root: Path) -> None:
        # Live compile keys on today (>= 2025): bravo's window closed 2024-12-31.
        entries = merge_clusters_to_wiki(temporal_root, as_of=date(2025, 6, 1), client=None)
        assert len(entries) == 1
        text = _page_text(temporal_root / "wiki")
        assert "Alpha guidance" in text
        assert "Bravo guidance" not in text

    def test_compile_as_of_reincludes_valid_member(
        self, temporal_root: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "asof-2024"
        entries = compile_as_of(temporal_root, date(2024, 6, 1), out)
        assert len(entries) == 1
        text = _page_text(out)
        # Both members were valid on 2024-06-01 → both blended.
        assert "Alpha guidance" in text
        assert "Bravo guidance" in text

    def test_differs_from_live_and_read_filter(self, temporal_root: Path, tmp_path: Path) -> None:
        # Live compile (what a slice-3 read-time --as-of would filter OVER).
        merge_clusters_to_wiki(temporal_root, as_of=date(2025, 6, 1), client=None)
        live_text = _page_text(temporal_root / "wiki")
        # The live page NEVER contains bravo's body, so no read-time filter
        # could resurrect it — only a recompile does.
        assert "Bravo guidance" not in live_text

        out = tmp_path / "asof-2024"
        compile_as_of(temporal_root, date(2024, 6, 1), out)
        asof_text = _page_text(out)
        assert "Bravo guidance" in asof_text
        assert asof_text != live_text

    def test_as_of_after_expiry_still_excludes(self, temporal_root: Path, tmp_path: Path) -> None:
        # 2025-06-01 is AFTER bravo's 2024-12-31 close → excluded even in
        # compile-as-of. Confirms the rewind is a real predicate, not
        # "include everything".
        out = tmp_path / "asof-2025"
        compile_as_of(temporal_root, date(2025, 6, 1), out)
        text = _page_text(out)
        assert "Alpha guidance" in text
        assert "Bravo guidance" not in text

    def test_does_not_touch_live_wiki(self, temporal_root: Path, tmp_path: Path) -> None:
        out = tmp_path / "asof-only"
        compile_as_of(temporal_root, date(2024, 6, 1), out)
        # The live wiki was never compiled in this test → stays empty.
        assert not list((temporal_root / "wiki").glob("auto-*.md"))
        assert list(out.glob("auto-*.md"))

    def test_out_dir_may_not_be_live_wiki(self, temporal_root: Path) -> None:
        with pytest.raises(ValueError, match="must not be the live wiki"):
            compile_as_of(temporal_root, date(2024, 6, 1), temporal_root / "wiki")

    def test_raw_members_are_not_mutated(self, temporal_root: Path, tmp_path: Path) -> None:
        bravo = temporal_root / "raw" / "auto-memory" / SCOPE / "feedback_bravo.md"
        before = bravo.read_text(encoding="utf-8")
        compile_as_of(temporal_root, date(2024, 6, 1), tmp_path / "asof")
        assert bravo.read_text(encoding="utf-8") == before


class TestDocumentedLimitation:
    """Undated ``superseded_by`` tombstones cannot be rewound (valid-time only)."""

    def test_superseded_member_stays_excluded(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        scope_dir = knowledge_root / "raw" / "auto-memory" / SCOPE
        _write_member(
            scope_dir,
            "feedback_keep.md",
            name="keep",
            body="Keeper guidance current.",
        )
        _write_member(
            scope_dir,
            "feedback_gone.md",
            name="gone",
            body="Superseded guidance body.",
            superseded_by="keep",
        )
        (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
        _write_config(knowledge_root)
        _write_clusters(
            knowledge_root,
            [f"{SCOPE}/feedback_keep.md", f"{SCOPE}/feedback_gone.md"],
        )
        out = tmp_path / "asof"
        compile_as_of(knowledge_root, date(2020, 1, 1), out)
        text = _page_text(out)
        # An undated tombstone carries no application date, so compile-as-of
        # (valid-time) cannot resurrect it even on a very early date.
        assert "Keeper guidance" in text
        assert "Superseded guidance" not in text
