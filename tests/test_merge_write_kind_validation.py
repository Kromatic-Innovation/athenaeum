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
    find_identity_pages,
    parse_pending_merges,
    render_block,
    resolve_merge,
    resolve_target_page,
    write_pending_merge,
)
from tests.conftest import init_git_repo


def _write_wiki_page(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: concept\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _write_uid_wiki_page(
    path: Path, *, uid: str, name: str, body: str = "body\n"
) -> None:
    """A page in the corpus's REAL filename convention, ``<uid>-<slug>.md``.

    ``_write_wiki_page`` writes no ``uid:``, which is what makes the
    athenaeum#748 regression fixture above a valid negative control: a
    uid-SHAPED filename prefix that the page's own frontmatter does not back
    still resolves to nothing (issue athenaeum#1635's deliberate scoping).
    """
    path.write_text(
        "---\n" f"uid: {uid}\n" f"name: {name}\n" "type: concept\n" "---\n" f"{body}",
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

        # Issue athenaeum#947: this fold actually approves (unlike this module's
        # other resolve_merge calls, which are refused before reaching the
        # delete step regardless of git), so it needs a git repo.
        init_git_repo(wiki)
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


# ---------------------------------------------------------------------------
# Issue athenaeum#1642 — the approve-time target check resolves by IDENTITY,
# through the same helper the proposal-time check uses.
# ---------------------------------------------------------------------------


class TestIdentityTargetResolution:
    def test_uid_prefixed_page_classifies_as_fold(self, tmp_path: Path) -> None:
        """The corpus's real shape. Before athenaeum#1642 this derived
        ``create-merged`` because ``learn-s-i-m-p-l-e.md`` does not exist."""
        canonical = tmp_path / "f351b6a1-learn-s-i-m-p-l-e.md"
        _write_uid_wiki_page(canonical, uid="f351b6a1", name="Learn S.I.M.P.L.E.")
        assert classify_write_kind("Learn S.I.M.P.L.E.", tmp_path) == "fold-into-existing"
        assert resolve_target_page("Learn S.I.M.P.L.E.", tmp_path) == canonical

    def test_absent_target_still_classifies_create_merged(
        self, tmp_path: Path
    ) -> None:
        """Counter-example (athenaeum#1642 AC4): widening the rule must not
        make every fold look foldable. Nothing owns the slug -> create."""
        _write_uid_wiki_page(
            tmp_path / "f351b6a1-learn-s-i-m-p-l-e.md",
            uid="f351b6a1",
            name="Learn S.I.M.P.L.E.",
        )
        assert classify_write_kind("Something Else Entirely", tmp_path) == "create-merged"
        assert resolve_target_page("Something Else Entirely", tmp_path) is None

    def test_uid_shaped_prefix_without_matching_uid_does_not_resolve(
        self, tmp_path: Path
    ) -> None:
        """The scoping athenaeum#1635 chose, restated at approve time: the
        prefix must be the page's OWN ``uid:``, not merely uid-shaped, and an
        arbitrary prefix (the ``auto-`` case) never resolves."""
        _write_uid_wiki_page(
            tmp_path / "4c7946d3-maria-springer.md",
            uid="somethingelse",
            name="Maria Springer",
        )
        # The ``auto-`` case from athenaeum#1635: a librarian-minted prefix that
        # is not this page's ``uid:`` at all.
        _write_uid_wiki_page(
            tmp_path / "auto-maria-springer.md", uid="bbbb2222", name="Maria Springer"
        )
        # And a page carrying no ``uid:`` whatsoever behind a uid-shaped prefix
        # -- the athenaeum#748 regression fixture's shape.
        _write_wiki_page(
            tmp_path / "cccc3333-maria-springer.md", name="Maria Springer"
        )
        assert classify_write_kind("Maria Springer", tmp_path) == "create-merged"
        assert find_identity_pages("Maria Springer", tmp_path) == []

    def test_bare_slug_page_still_wins_and_needs_no_frontmatter(
        self, tmp_path: Path
    ) -> None:
        """Backward compatibility: the bare-slug form is matched on filename
        alone, exactly as before athenaeum#1642, and is preferred when both
        forms exist so pre-existing corpora resolve identically."""
        bare = tmp_path / "maria-springer.md"
        bare.write_text("no frontmatter at all\n", encoding="utf-8")
        uid_page = tmp_path / "aaaa1111-maria-springer.md"
        _write_uid_wiki_page(uid_page, uid="aaaa1111", name="Maria Springer")
        assert classify_write_kind("Maria Springer", tmp_path) == "fold-into-existing"
        assert resolve_target_page("Maria Springer", tmp_path) == bare
        assert find_identity_pages("Maria Springer", tmp_path) == [bare, uid_page]

    def test_sidecar_files_are_never_fold_targets(self, tmp_path: Path) -> None:
        """``_``-prefixed files are machinery, not corpus pages."""
        (tmp_path / "_pending-merges.md").write_text(
            "---\nuid: _pending\nname: Pending Merges\n---\nx\n", encoding="utf-8"
        )
        assert find_identity_pages("Pending Merges", tmp_path) == []

    def test_classify_and_approve_agree_for_uid_prefixed_target(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1642 AC2, the invariant end to end: a derived
        ``fold-into-existing`` can never later fail ``fold_target_missing``,
        and the fold writes into the EXISTING uid-prefixed file."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "f351b6a1-learn-s-i-m-p-l-e.md"
        _write_uid_wiki_page(
            canonical,
            uid="f351b6a1",
            name="Learn S.I.M.P.L.E.",
            body="canonical prose\n",
        )
        dup = wiki / "learn-simple.md"
        _write_wiki_page(dup, name="Learn Simple", body="dup\n")
        init_git_repo(wiki)

        merges = wiki / "_pending_merges.md"
        write_pending_merge(
            merges,
            merge_target_name="Learn S.I.M.P.L.E.",
            sources=[str(canonical), str(dup)],
            rationale="consolidate",
            draft_merged_body="---\nuid: f351b6a1\nname: Learn S.I.M.P.L.E.\n---\nmerged prose\n",
            confidence=0.9,
        )
        pm = parse_pending_merges(merges)[0]
        assert pm.write_kind == "fold-into-existing"

        result = resolve_merge(merges, pm.id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert canonical.exists()
        assert "merged prose" in canonical.read_text(encoding="utf-8")
        assert not dup.exists()
        assert result["folded_sources"] == [str(dup)]
        assert not (wiki / "learn-s-i-m-p-l-e.md").exists()

    def test_create_merged_into_identity_resolved_target_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """The other direction of the same invariant: a legacy/hand-edited
        ``create-merged`` block whose target DOES identity-resolve must be
        refused with ``target_exists`` rather than clobbering the page."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "f351b6a1-learn-s-i-m-p-l-e.md"
        _write_uid_wiki_page(
            canonical,
            uid="f351b6a1",
            name="Learn S.I.M.P.L.E.",
            body="canonical prose\n",
        )
        dup = wiki / "learn-simple.md"
        _write_wiki_page(dup, name="Learn Simple", body="dup\n")

        merges = wiki / "_pending_merges.md"
        pm_id = _hand_write_block(
            merges,
            merge_target_name="Learn S.I.M.P.L.E.",
            sources=[str(dup)],
            rationale="legacy block",
            draft_merged_body="would have clobbered\n",
            confidence=0.9,
            write_kind="create-merged",
        )

        result = resolve_merge(merges, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "target_exists"
        assert "f351b6a1-learn-s-i-m-p-l-e.md" in result["message"]
        assert "canonical prose" in canonical.read_text(encoding="utf-8")
        assert dup.exists()
        assert not (wiki / "learn-s-i-m-p-l-e.md").exists()
