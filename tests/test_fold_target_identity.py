# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2545: a fold tombstone's ``folded_into`` must name
a real file, not a bare name-slug that owns none when the canonical lives
at ``<uid>-<slug>.md`` (the real corpus shape).

Covers:

1. A uid-prefixed canonical: the tombstone is stamped with the canonical's
   filename STEM (``<uid>-<slug>``), which names the real file.
2. Counter-example: a bare-slug canonical still gets the bare slug (the two
   forms coincide there) — unchanged behavior.
3. :func:`athenaeum.unfold._path_for_slug` resolves BOTH an old-style
   dangling bare-slug ``folded_into`` and the new stem form.
4. The merge-provenance ledger's ``canonical_slug`` stays in lockstep with
   the tombstone's ``folded_into`` value, so a later unfold's
   ``find_fold_record`` lookup (exact-match by that value) still finds the
   record for a uid-prefixed canonical — a regression this fix could
   otherwise introduce.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import is_tombstone, parse_frontmatter, tombstone_target
from athenaeum.pending_merges import (
    parse_pending_merges,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.provenance import read_merge_provenance
from athenaeum.unfold import _path_for_slug, can_unfold_directly, find_fold_record
from tests.conftest import init_git_repo


def _write_wiki_page(
    path: Path, *, name: str, body: str = "", uid: str | None = None
) -> None:
    fm = [f"name: {name}", "type: concept"]
    if uid:
        fm.append(f"uid: {uid}")
    path.write_text("---\n" + "\n".join(fm) + "\n---\n" + body, encoding="utf-8")


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\n{body}", encoding="utf-8")


class TestFoldedIntoStampsFileIdentity:
    """AC1 + AC2."""

    def test_uid_prefixed_canonical_stamps_filename_stem(self, tmp_path: Path) -> None:
        """AC1: the canonical lives at ``<uid>-<slug>.md`` — its ``name:``
        slug (``release-checklist``) names no file. The tombstone must be
        stamped with the stem that DOES."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "1a2b3c4d-release-checklist.md"
        _write_wiki_page(
            target, name="Release Checklist", body="old\n", uid="1a2b3c4d"
        )
        src = wiki / "old-checklist.md"
        _write_source(src, name="Old Checklist")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Release Checklist",
            sources=[str(src)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        assert is_tombstone(src_meta)
        # The stamped value names the REAL file.
        assert tombstone_target(src_meta) == "1a2b3c4d-release-checklist"
        assert (wiki / f"{tombstone_target(src_meta)}.md").is_file()
        # The bare name-slug, by contrast, names nothing.
        assert not (wiki / "release-checklist.md").exists()

    def test_bare_slug_canonical_unchanged(self, tmp_path: Path) -> None:
        """AC2 counter-example: a bare-slug canonical (``<slug>.md``) still
        gets ``folded_into: <slug>`` — the fix is a no-op for this shape."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src = wiki / "old-a.md"
        _write_source(src, name="Old A")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        assert tombstone_target(src_meta) == "canonical"


class TestTombstoneTargetReadersResolveBothForms:
    """AC3: ``_path_for_slug`` (unfold's own resolver) must resolve both the
    pre-fix bare-slug value and the post-fix stem value to the same live
    page."""

    def test_resolves_new_stem_form_directly(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "1a2b3c4d-release-checklist.md"
        _write_wiki_page(target, name="Release Checklist", uid="1a2b3c4d")

        resolved = _path_for_slug(wiki, "1a2b3c4d-release-checklist")
        assert resolved == target

    def test_resolves_old_bare_slug_form_as_fallback(self, tmp_path: Path) -> None:
        """A tombstone stamped before this fix still carries the bare
        ``release-checklist`` slug; no file is named exactly that, but
        exactly one file ends with ``-release-checklist.md``."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "1a2b3c4d-release-checklist.md"
        _write_wiki_page(target, name="Release Checklist", uid="1a2b3c4d")
        assert not (wiki / "release-checklist.md").exists()

        resolved = _path_for_slug(wiki, "release-checklist")
        assert resolved == target


class TestLedgerStaysConsistentWithTombstoneValue:
    """A consequence of AC1: the merge-provenance ledger's own
    ``canonical_slug`` must match the value the tombstone was stamped
    with, or a later unfold's exact-match ledger lookup
    (:func:`athenaeum.unfold.find_fold_record`) silently stops finding the
    record for every uid-prefixed canonical — converting a direct unfold
    into an always-queued one. Guards against reintroducing that mismatch.
    """

    def test_ledger_canonical_slug_matches_stamped_folded_into(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "1a2b3c4d-release-checklist.md"
        _write_wiki_page(
            target, name="Release Checklist", body="old\n", uid="1a2b3c4d"
        )
        src = wiki / "old-checklist.md"
        _write_source(src, name="Old Checklist")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Release Checklist",
            sources=[str(src)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True, result

        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        canonical_slug = tombstone_target(src_meta)
        records = read_merge_provenance(wiki)
        assert len(records) == 1
        assert records[0]["canonical_slug"] == canonical_slug

        # find_fold_record must still find it, by that same identity value —
        # this is the mechanism `unfold_page` actually relies on.
        record = find_fold_record(wiki, src, canonical_slug)
        assert record is not None
        assert record["merge_id"] == pm_id

    def test_direct_unfold_still_possible_for_uid_prefixed_canonical(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: ``can_unfold_directly`` says yes for an untouched
        uid-prefixed canonical, exactly as it already does for a bare-slug
        one — proving the fix did not degrade unfold for the common shape."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "1a2b3c4d-release-checklist.md"
        _write_wiki_page(
            target, name="Release Checklist", body="old\n", uid="1a2b3c4d"
        )
        src = wiki / "old-checklist.md"
        _write_source(src, name="Old Checklist")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Release Checklist",
            sources=[str(src)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True, result

        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        canonical_slug = tombstone_target(src_meta)
        canonical_path = _path_for_slug(wiki, canonical_slug)
        assert canonical_path == target

        record = find_fold_record(wiki, src, canonical_slug)
        ok, _reason = can_unfold_directly(record, canonical_path)
        assert ok is True
