# SPDX-License-Identifier: Apache-2.0
"""Tests for write_kind derivation + fail-closed validation (issue athenaeum#748).

``write_pending_merge`` used to accept ``write_kind`` as a caller-supplied
string and store it unvalidated; ``resolve_merge`` then dispatched on it and,
for ``fold-into-existing``, DELETED every source page. A wrong value was
therefore destructive, and nothing checked it at either write or approval time.

These tests pin the three fixes:

1. ``write_pending_merge`` DERIVES ``write_kind`` from whether the target slug
   exists; a slug that does not exist is ``create-merged`` regardless of what
   the caller passes, and a caller value that disagrees fails closed.
2. ``resolve_merge`` re-checks target existence before the fold path and
   refuses with ``fold_target_missing`` when the target slug is absent — even
   for a hand-written / legacy misclassified block that bypassed fix 1.
3. A source whose resolved path IS the target page is never deleted.

Plus the concrete 2026-08-02 incident replayed as a regression test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.models import slugify
from athenaeum.pending_merges import (
    classify_write_kind,
    parse_pending_merges,
    render_block,
    resolve_merge,
    write_pending_merge,
)


def _write_wiki_page(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: concept\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _hand_write_block(merges_path: Path, **block_kwargs) -> str:
    """Write a single proposal block directly into the sidecar.

    Bypasses ``write_pending_merge``'s athenaeum#748 write-time validation so a
    misclassified block — the exact shape a legacy or hand-edited
    ``_pending_merges.md`` can carry — can be exercised at approve time.
    Returns the proposal id.
    """
    block = render_block(**block_kwargs)
    merges_path.write_text("# Pending Merges\n\n" + block + "\n", encoding="utf-8")
    return parse_pending_merges(merges_path)[0].id


# ---------------------------------------------------------------------------
# AC 1 — write_pending_merge derives write_kind.
# ---------------------------------------------------------------------------


class TestWriteKindDerived:
    def test_nonexistent_target_classified_create_merged_regardless_of_caller(
        self, tmp_path: Path
    ) -> None:
        """A proposal for a slug that does not exist is create-merged even
        when the caller passes create-merged (agrees) or nothing (derives)."""
        merges = tmp_path / "_pending_merges.md"
        # Caller passes nothing → derived.
        write_pending_merge(
            merges,
            merge_target_name="Brand New",
            sources=["a.md"],
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
        )
        pm = parse_pending_merges(merges)[0]
        assert pm.write_kind == "create-merged"

    def test_existing_target_derived_fold_when_caller_omits(
        self, tmp_path: Path
    ) -> None:
        _write_wiki_page(tmp_path / "canonical.md", name="Canonical")
        merges = tmp_path / "_pending_merges.md"
        write_pending_merge(
            merges,
            merge_target_name="Canonical",
            sources=[str(tmp_path / "src.md")],
            rationale="r",
            draft_merged_body="body",
            confidence=0.9,
            write_kind=None,
        )
        pm = parse_pending_merges(merges)[0]
        assert pm.write_kind == "fold-into-existing"

    def test_disagreeing_fold_for_absent_target_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """The destructive misclassification — caller asserts fold-into-existing
        for a slug that does NOT exist — is refused at write time."""
        merges = tmp_path / "_pending_merges.md"
        with pytest.raises(ValueError, match="write_kind mismatch"):
            write_pending_merge(
                merges,
                merge_target_name="Does Not Exist",
                sources=["a.md"],
                rationale="r",
                draft_merged_body="body",
                confidence=0.9,
                write_kind="fold-into-existing",
            )
        # Nothing was written.
        assert not merges.exists()

    def test_disagreeing_create_for_existing_target_fails_closed(
        self, tmp_path: Path
    ) -> None:
        _write_wiki_page(tmp_path / "canonical.md", name="Canonical")
        merges = tmp_path / "_pending_merges.md"
        with pytest.raises(ValueError, match="write_kind mismatch"):
            write_pending_merge(
                merges,
                merge_target_name="Canonical",
                sources=["a.md"],
                rationale="r",
                draft_merged_body="body",
                confidence=0.9,
                write_kind="create-merged",
            )

    def test_unknown_write_kind_fails_closed(self, tmp_path: Path) -> None:
        merges = tmp_path / "_pending_merges.md"
        with pytest.raises(ValueError, match="write_kind must be one of"):
            write_pending_merge(
                merges,
                merge_target_name="Whatever",
                sources=["a.md"],
                rationale="r",
                draft_merged_body="body",
                confidence=0.9,
                write_kind="obliterate-everything",
            )

    def test_classify_write_kind_matches_resolve_target_path(
        self, tmp_path: Path
    ) -> None:
        assert classify_write_kind("Nope", tmp_path) == "create-merged"
        _write_wiki_page(tmp_path / f"{slugify('Yep')}.md", name="Yep")
        assert classify_write_kind("Yep", tmp_path) == "fold-into-existing"


# ---------------------------------------------------------------------------
# AC 3 — resolve_merge re-checks target existence before the fold path.
# ---------------------------------------------------------------------------


class TestResolveRechecksFoldTarget:
    def test_fold_target_missing_refuses_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        """A hand-written fold-into-existing block whose target slug is absent
        must be refused with a distinct error code — no page created, no
        source deleted."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = wiki / "src-one.md"
        _write_wiki_page(src, name="Src One")

        merges = wiki / "_pending_merges.md"
        pm_id = _hand_write_block(
            merges,
            merge_target_name="Ghost Target",  # ghost-target.md does NOT exist
            sources=[str(src)],
            rationale="r",
            draft_merged_body="new body\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )

        result = resolve_merge(merges, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "fold_target_missing"
        # No new page written for the ghost slug.
        assert not (wiki / f"{slugify('Ghost Target')}.md").exists()
        # Source preserved.
        assert src.exists()
        # Checkbox still unchecked — merge remains pending.
        md = merges.read_text(encoding="utf-8")
        assert "- [ ]" in md
        assert "- [x]" not in md


# ---------------------------------------------------------------------------
# AC 4 — a source whose resolved path equals the target page is never deleted.
# ---------------------------------------------------------------------------


class TestTargetPageNeverDeleted:
    def test_source_resolving_to_target_via_symlink_survives(
        self, tmp_path: Path
    ) -> None:
        """A source with a DIFFERENT stem-slug that nonetheless resolves to the
        canonical target page (here via a symlink) slips past the slug-based
        ``folded_sources`` filter but must still be skipped by the path-equality
        guard — the canonical page is never touched and the aliasing source is
        not reported as folded."""
        import os

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="canonical prose\n")
        other = wiki / "other.md"
        _write_wiki_page(other, name="Other", body="o\n")

        # A differently-named symlink to the canonical page: stem "alias-link"
        # != target slug "canonical", so the slug filter does NOT exclude it —
        # only the athenaeum#748 path-equality guard prevents it being folded.
        alias_link = wiki / "alias-link.md"
        os.symlink(target.name, alias_link)

        merges = wiki / "_pending_merges.md"
        write_pending_merge(
            merges,
            merge_target_name="Canonical",
            sources=[str(alias_link), str(other)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
        )
        pm_id = parse_pending_merges(merges)[0].id
        result = resolve_merge(merges, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        # Canonical page preserved with its (rewritten) content, never deleted.
        assert target.exists(), "canonical target page must never be deleted"
        # The aliasing source was skipped by the guard, not folded away.
        assert alias_link.exists()
        assert str(alias_link) not in result["folded_sources"]
        # The genuine other source WAS folded away.
        assert not other.exists()
        assert result["folded_sources"] == [str(other)]


# ---------------------------------------------------------------------------
# Regression — the concrete 2026-08-02 incident (issue athenaeum#748 Motivation).
# ---------------------------------------------------------------------------


class TestAugust2Regression:
    def test_canonical_uid_slug_page_not_deleted_by_misclassified_fold(
        self, tmp_path: Path
    ) -> None:
        """Canonical page is ``<uid>-<slug>.md``; merge_target_name slugifies
        to a DIFFERENT slug that owns no page; write_kind hand-set to
        fold-into-existing. The fold target (``maria-springer.md``) does not
        exist, so the canonical ``4c7946d3-maria-springer.md`` must survive."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "4c7946d3-maria-springer.md"
        _write_wiki_page(canonical, name="Maria Springer", body="canonical prose\n")
        dup_a = wiki / "dup-a.md"
        dup_b = wiki / "dup-b.md"
        _write_wiki_page(dup_a, name="Dup A", body="a\n")
        _write_wiki_page(dup_b, name="Dup B", body="b\n")

        # merge_target_name "Maria Springer" -> slug "maria-springer",
        # which is NOT the canonical filename's slug.
        assert not (wiki / "maria-springer.md").exists()

        merges = wiki / "_pending_merges.md"
        pm_id = _hand_write_block(
            merges,
            merge_target_name="Maria Springer",
            sources=[str(canonical), str(dup_a), str(dup_b)],
            rationale="consolidate duplicates",
            draft_merged_body="draft that would have clobbered a new page\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )

        result = resolve_merge(merges, pm_id, "approve", wiki_root=wiki)

        # Refused, not silently destructive.
        assert result["ok"] is False
        assert result["error_code"] == "fold_target_missing"
        # The canonical page and BOTH duplicates are all preserved.
        assert canonical.read_text(encoding="utf-8") == (
            "---\nname: Maria Springer\ntype: concept\n---\ncanonical prose\n"
        )
        assert dup_a.exists()
        assert dup_b.exists()
        # No new page was created for the wrong slug.
        assert not (wiki / "maria-springer.md").exists()
