# SPDX-License-Identifier: Apache-2.0
"""Tests for the merge-vs-fold write paths (issue athenaeum#425; issue athenaeum#716
lane 716-B extends this with the tombstone fold, coordinate widening, the
narrowing-invariant refusal, and the fold-graph invariants).

Covers:

1. ``fold-into-existing`` folds sources into the pre-existing target page —
   ``target_exists`` is unreachable for a correctly-classified proposal.
2. ``create-merged`` is UNCHANGED — a misclassified create-kind proposal
   that hits an existing slug still fails closed with ``target_exists``.
3. Inbound wikilink rewrite across ``wiki/``; old source files TOMBSTONED
   (issue athenaeum#716, never deleted); ``aliases:`` added + deduped on re-fold;
   link-time alias resolution.
4. Vector-store hygiene: tombstoned slugs purged, aliases never embedded.
5. Provenance recording + the read API (library + CLI); the ledger's
   reversal-sufficient fields (issue athenaeum#716).
6. Coordinate widening (issue athenaeum#716): valid-time union, scope
   ancestor-widening, and the narrowing-invariant refusal.
7. Fold-graph invariants (issue athenaeum#716): acyclic refusal, exactly-one-
   live-canonical (including the chained A->B->C case), and the
   already-tombstoned-source refusal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athenaeum.models import is_tombstone, parse_frontmatter, slugify, tombstone_target
from athenaeum.pending_merges import (
    _apply_fold_into_existing,
    parse_pending_merges,
    resolve_alias_slug,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.provenance import read_merge_provenance
from tests.conftest import init_git_repo


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: feedback\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _write_wiki_page(
    path: Path, *, name: str, body: str = "", aliases=None, extra: dict | None = None
) -> None:
    fm = [f"name: {name}", "type: concept"]
    if aliases:
        alias_yaml = ", ".join(f'"{a}"' for a in aliases)
        fm.append(f"aliases: [{alias_yaml}]")
    for key, value in (extra or {}).items():
        fm.append(f"{key}: {value}")
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + body, encoding="utf-8"
    )


def _write_tombstone_page(
    path: Path, *, name: str, folded_into: str, body: str = ""
) -> None:
    """A page already carrying the issue athenaeum#716 tombstone stamp — used to
    fixture a fold-graph invariant scenario without going through a real
    fold first."""
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: concept\n"
        "status: folded\n"
        f"folded_into: {folded_into}\n"
        "embedded: false\n"
        "---\n" + body,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC 1 — fold-into-existing folds into the existing page; target_exists
# unreachable for a correctly-classified proposal.
# ---------------------------------------------------------------------------


class TestFoldIntoExisting:
    def test_fold_writes_draft_body_to_existing_target(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Pre-existing canonical target.
        target = wiki / "existing-topic.md"
        _write_wiki_page(target, name="Existing Topic", body="OLD BODY\n")

        src_a = wiki / "topic-variant-a.md"
        src_b = wiki / "topic-variant-b.md"
        _write_wiki_page(src_a, name="Topic Variant A", body="variant a\n")
        _write_wiki_page(src_b, name="Topic Variant B", body="variant b\n")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Existing Topic",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="MERGED BODY\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id

        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        assert result["error_code"] is None
        # target_exists never fires for a correctly-classified fold.
        written = target.read_text(encoding="utf-8")
        assert "MERGED BODY" in written
        assert "OLD BODY" not in written

    def test_target_exists_unreachable_for_fold_fixture(self, tmp_path: Path) -> None:
        """The exact fixture shape that would trip target_exists on create-merged
        must succeed cleanly when classified fold-into-existing."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "already-here.md"
        _write_wiki_page(target, name="Already Here", body="pre-existing\n")
        src = wiki / "src-one.md"
        _write_wiki_page(src, name="Src One", body="one\n")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Already Here",
            sources=[str(src)],
            rationale="r",
            draft_merged_body="new merged content\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True
        assert result["error_code"] != "target_exists"

    def test_source_files_tombstoned_after_fold(self, tmp_path: Path) -> None:
        """Issue athenaeum#716: fold no longer deletes sources — it tombstones
        them in place (status: folded, folded_into: <canonical>, embedded:
        false), preserving the file (and its body) on disk."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        src_b = wiki / "old-b.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")
        _write_wiki_page(src_b, name="Old B", body="b\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert src_a.exists()
        assert src_b.exists()
        assert set(result["folded_sources"]) == {str(src_a), str(src_b)}
        for src, body in ((src_a, "a\n"), (src_b, "b\n")):
            meta, src_body = parse_frontmatter(src.read_text(encoding="utf-8"))
            assert is_tombstone(meta)
            assert tombstone_target(meta) == "canonical"
            assert meta["embedded"] is False
            # The body is untouched — never destroyed, only the source page
            # is excluded from recall/index/embedding going forward.
            assert src_body == body

    def test_fold_does_not_tombstone_canonical_reappearing_in_own_sources(
        self, tmp_path: Path
    ) -> None:
        """A source path that IS the canonical page (same slug) must never be
        tombstoned — only the OTHER sources are folded away."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_b = wiki / "old-b.md"
        _write_wiki_page(src_b, name="Old B", body="b\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(target), str(src_b)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert target.exists()
        target_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert not is_tombstone(target_meta)  # canonical stays live
        assert src_b.exists()
        src_meta, _ = parse_frontmatter(src_b.read_text(encoding="utf-8"))
        assert is_tombstone(src_meta)
        assert tombstone_target(src_meta) == "canonical"
        assert result["folded_sources"] == [str(src_b)]


# ---------------------------------------------------------------------------
# Issue athenaeum#947 — removal must be git-only for recoverability.
#
# AC1: an approved fold lands as a provenance-snapshot commit BEFORE any
#      page is deleted, and the fold itself is its own commit.
# AC2: the fold REFUSES (rather than deleting) when the knowledge root is
#      not a git repo — no file removed, checkbox not flipped.
# ---------------------------------------------------------------------------


def _git_log_messages(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


class TestGitRecoverability:
    def test_fold_lands_as_two_commits_snapshot_then_fold(
        self, tmp_path: Path
    ) -> None:
        """A successful fold-into-existing approve takes a provenance-
        snapshot commit of the target + sources BEFORE any write, then
        commits the fold itself as its own commit — two NEW commits beyond
        the fixture's seed commit, in that order (newest first in
        ``git log``: fold commit, then snapshot commit)."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Seed the repo with UNRELATED content only. The target + source
        # pages are written and committed as their own step below (after
        # init), so they are still untracked when the fold runs and the
        # provenance-snapshot commit has real new content to capture —
        # see ``test_fold_no_op_snapshot_still_commits_fold`` below for the
        # already-committed (no-op snapshot) case.
        _write_wiki_page(wiki / "unrelated.md", name="Unrelated", body="x\n")
        init_git_repo(wiki)
        seed_messages = _git_log_messages(wiki)
        assert len(seed_messages) == 1  # just the fixture's seed commit

        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")
        # target/src_a are UNTRACKED here — not part of the seed commit.

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True

        messages = _git_log_messages(wiki)
        assert len(messages) == 3  # seed + snapshot + fold
        fold_msg, snapshot_msg, seed_msg = messages
        assert "provenance snapshot" in snapshot_msg
        assert "canonical" in snapshot_msg
        assert "athenaeum#947" in snapshot_msg
        assert "fold" in fold_msg
        assert "old-a" in fold_msg
        assert "canonical" in fold_msg
        assert "athenaeum#947" in fold_msg
        assert seed_msg == seed_messages[0]

    def test_fold_no_op_snapshot_still_commits_fold(self, tmp_path: Path) -> None:
        """When the target + sources are already fully committed (the
        common case — this fixture's seed commit already covers them), the
        provenance-snapshot commit is a legitimate no-op (nothing new to
        stage), but the fold commit still lands."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")
        init_git_repo(wiki)  # seed commit already covers target + src_a

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True

        messages = _git_log_messages(wiki)
        # No SEPARATE snapshot commit (nothing new was staged at that point —
        # target/src_a were already captured by the seed commit), but the
        # fold itself still commits.
        assert len(messages) == 2  # seed + fold
        assert "provenance snapshot" not in messages
        assert "fold" in messages[0]

    def test_fold_refuses_outside_git_repo_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        """No ``.git`` anywhere above ``wiki_root`` -> refuse, do not
        degrade. No page is deleted, the target is untouched, and the
        checkbox is NOT flipped (mirrors the ``fold_target_missing``
        shape)."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()  # deliberately NOT a git repo
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="OLD BODY\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="MERGED BODY\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "no_git_repo"
        assert result["resolved_block"] is None
        # Nothing was deleted.
        assert src_a.exists()
        # Target page untouched — the draft body never overwrote it.
        assert target.read_text(encoding="utf-8") == (
            "---\nname: Canonical\ntype: concept\n---\nOLD BODY\n"
        )
        # Checkbox still unchecked — merge remains pending, exactly like
        # fold_target_missing.
        md = merges_path.read_text(encoding="utf-8")
        assert "- [ ]" in md
        assert "- [x]" not in md


# ---------------------------------------------------------------------------
# AC 2 — create-merged UNCHANGED; misclassified create-kind still fails closed.
# ---------------------------------------------------------------------------


class TestCreateMergedUnchanged:
    def test_create_merged_writes_fresh_target(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src_a = wiki / "a.md"
        src_b = wiki / "b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Brand New Topic",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="fresh body",
            confidence=0.9,
            write_kind="create-merged",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        target = wiki / f"{slugify('Brand New Topic')}.md"
        assert target.read_text(encoding="utf-8") == "fresh body"
        # create-merged never deletes/rewrites sources.
        assert src_a.exists()
        assert src_b.exists()
        assert "folded_sources" not in result

    def test_misclassified_create_merged_fails_closed_on_existing_slug(
        self, tmp_path: Path
    ) -> None:
        """Defense in depth: a stale/hand-edited block claiming create-merged
        must still fail closed if the slug is actually taken.

        Since athenaeum#748 ``write_pending_merge`` derives ``write_kind`` and
        would refuse to STORE this misclassified block, the misclassified block
        is hand-written directly into the sidecar (the exact shape a legacy /
        hand-edited ``_pending_merges.md`` can carry) so the approve-time
        ``target_exists`` guard is exercised on its own."""
        from athenaeum.pending_merges import render_block

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "existing-name.md"
        target.write_text("PRE-EXISTING\n", encoding="utf-8")

        src_a = wiki / "x.md"
        src_b = wiki / "y.md"
        _write_source(src_a, name="x")
        _write_source(src_b, name="y")

        merges_path = wiki / "_pending_merges.md"
        # Hand-write a misclassified create-merged block for a slug that IS
        # taken — bypassing write_pending_merge's athenaeum#748 write-time guard.
        block = render_block(
            merge_target_name="existing-name",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="draft",
            confidence=0.9,
            write_kind="create-merged",
        )
        merges_path.write_text("# Pending Merges\n\n" + block + "\n", encoding="utf-8")
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "target_exists"
        assert target.read_text(encoding="utf-8") == "PRE-EXISTING\n"
        # Checkbox still unchecked — merge remains pending.
        md = merges_path.read_text(encoding="utf-8")
        assert "- [ ]" in md
        assert "- [x]" not in md


# ---------------------------------------------------------------------------
# AC 3 — inbound-ref rewrite, alias map + dedup, link-time resolution.
# ---------------------------------------------------------------------------


class TestReferenceRewriteAndAliases:
    def test_inbound_wikilinks_rewritten_to_canonical(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        referrer = wiki / "referrer.md"
        _write_wiki_page(
            referrer,
            name="Referrer",
            body="See [[old-a]] for details, also [[old-a|the older page]].\n",
        )

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert result["links_rewritten"] == 1
        rewritten_text = referrer.read_text(encoding="utf-8")
        assert "[[canonical]]" in rewritten_text
        assert "[[canonical|the older page]]" in rewritten_text
        assert "[[old-a]]" not in rewritten_text
        assert "[[old-a|" not in rewritten_text

    def test_aliases_added_and_deduped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(
            target, name="Canonical", body="old\n", aliases=["already-there"]
        )
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert result["aliases_added"] == ["old-a"]
        meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert meta["aliases"] == ["already-there", "old-a"]

    def test_aliases_deduped_on_second_merge(self, tmp_path: Path) -> None:
        """A second fold that re-folds an already-aliased slug must not
        duplicate the aliases: entry."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n", aliases=["old-a"])
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged again\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True
        assert result["aliases_added"] == []  # already present, nothing new
        meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert meta["aliases"] == ["old-a"]  # no duplicate

    def test_link_time_resolution_via_resolve_alias_slug(self, tmp_path: Path) -> None:
        """A not-yet-processed raw memory's [[old-slug]] link must resolve
        to the canonical page via aliases: frontmatter."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        # A raw memory processed AFTER the fold links [[old-a]] — must
        # resolve to the canonical page's own slug.
        assert resolve_alias_slug(wiki, "old-a") == "canonical"
        # A slug that was never an alias resolves to itself unchanged.
        assert resolve_alias_slug(wiki, "never-existed") == "never-existed"
        # The canonical slug itself resolves to itself.
        assert resolve_alias_slug(wiki, "canonical") == "canonical"


# ---------------------------------------------------------------------------
# AC 4 — vector store hygiene: deleted slugs purged, aliases never embedded.
# ---------------------------------------------------------------------------


class TestVectorHygiene:
    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_deleted_slug_purged_from_vector_store(self, tmp_path: Path) -> None:
        from athenaeum.search import VectorBackend

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="canonical content\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="old a content\n")

        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki, cache)

        # Sanity: old-a.md is indexed before the fold.
        import chromadb
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
        client = chromadb.PersistentClient(path=str(cache / "wiki-vectors"))
        collection = client.get_collection("wiki")
        before = collection.get(ids=["old-a.md"])
        assert before["ids"] == ["old-a.md"]

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged content\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(
            merges_path,
            pm_id,
            "approve",
            wiki_root=wiki,
            cache_dir=cache,
            search_backend="vector",
        )
        assert result["ok"] is True

        SharedSystemClient.clear_system_cache()
        client = chromadb.PersistentClient(path=str(cache / "wiki-vectors"))
        collection = client.get_collection("wiki")
        after = collection.get(ids=["old-a.md"])
        assert after["ids"] == []

    def test_no_cache_dir_skips_purge_without_error(self, tmp_path: Path) -> None:
        """cache_dir=None (the default) must not raise — purge is opportunistic."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True

    def test_aliases_never_embedded(self, tmp_path: Path) -> None:
        """A folded-away alias slug must never become its OWN embedded
        document — aliases are pointers recorded on the canonical page's
        frontmatter, not content that gets a vector entry of its own. The
        canonical page's document is the real merged body, and no id in the
        collection carries an alias's filename after the fold."""
        from athenaeum.search import VectorBackend

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="genuinely merged prose\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki, cache)

        import chromadb
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
        client = chromadb.PersistentClient(path=str(cache / "wiki-vectors"))
        collection = client.get_collection("wiki")
        # old-a.md was deleted by the fold, so it was never (re-)embedded
        # under its own filename id — no alias-only stub entry exists.
        result = collection.get(ids=["old-a.md"])
        assert result["ids"] == []
        # Every id in the whole collection is the canonical page only —
        # confirms the fold didn't leave a second (alias) entry behind.
        all_ids = collection.get()["ids"]
        assert all_ids == ["canonical.md"]
        # The canonical page's embedded document is the real merged body.
        canon = collection.get(ids=["canonical.md"], include=["documents"])
        assert "genuinely merged prose" in canon["documents"][0]


# ---------------------------------------------------------------------------
# AC 5 — provenance recording + read API.
# ---------------------------------------------------------------------------


class TestProvenanceRecording:
    def test_fold_records_provenance(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True

        records = read_merge_provenance(wiki)
        assert len(records) == 1
        rec = records[0]
        assert rec["merge_id"] == pm_id
        assert rec["write_kind"] == "fold-into-existing"
        assert rec["canonical_slug"] == "canonical"
        assert rec["source_paths"] == [str(src_a)]
        assert rec["v"] == 2
        assert "ts" in rec
        # Issue athenaeum#716: the record must be sufficient to reverse the
        # fold — the tombstoned source, the resulting post-fold content
        # hash, and (an empty) widened-coordinates map are all present.
        assert rec["folded_sources"] == [str(src_a)]
        assert rec["aliases_added"] == ["old-a"]
        assert rec["links_rewritten"] == []
        from athenaeum.verdicts import content_hash

        assert rec["canonical_content_hash"] == content_hash(
            target.read_text(encoding="utf-8")
        )
        assert rec["coordinates_widened"] == {}

    def test_create_merged_records_provenance_too(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src_a = wiki / "a.md"
        src_b = wiki / "b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Fresh Topic",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="fresh",
            confidence=0.9,
            write_kind="create-merged",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        records = read_merge_provenance(wiki)
        assert len(records) == 1
        assert records[0]["write_kind"] == "create-merged"
        assert records[0]["canonical_slug"] == slugify("Fresh Topic")

    def test_read_filters_by_canonical_slug_and_merge_id(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target_a = wiki / "canon-a.md"
        target_b = wiki / "canon-b.md"
        _write_wiki_page(target_a, name="Canon A", body="a\n")
        _write_wiki_page(target_b, name="Canon B", body="b\n")
        src_1 = wiki / "old-1.md"
        src_2 = wiki / "old-2.md"
        _write_wiki_page(src_1, name="Old 1", body="1\n")
        _write_wiki_page(src_2, name="Old 2", body="2\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canon A",
            sources=[str(src_1)],
            rationale="r",
            draft_merged_body="m1\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        write_pending_merge(
            merges_path,
            merge_target_name="Canon B",
            sources=[str(src_2)],
            rationale="r",
            draft_merged_body="m2\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pms = parse_pending_merges(merges_path)
        id_a = next(pm.id for pm in pms if pm.merge_target_name == "Canon A")
        id_b = next(pm.id for pm in pms if pm.merge_target_name == "Canon B")
        resolve_merge(merges_path, id_a, "approve", wiki_root=wiki)
        resolve_merge(merges_path, id_b, "approve", wiki_root=wiki)

        all_records = read_merge_provenance(wiki)
        assert len(all_records) == 2

        by_slug = read_merge_provenance(wiki, canonical_slug="canon-a")
        assert len(by_slug) == 1
        assert by_slug[0]["canonical_slug"] == "canon-a"

        by_id = read_merge_provenance(wiki, merge_id=id_b)
        assert len(by_id) == 1
        assert by_id[0]["merge_id"] == id_b

    def test_read_missing_ledger_returns_empty(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert read_merge_provenance(wiki) == []

    def test_ledger_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        ledger = wiki / "_merge_provenance.jsonl"
        good = json.dumps(
            {
                "v": 1,
                "ts": "2026-07-24T00:00:00Z",
                "merge_id": "abc123",
                "write_kind": "fold-into-existing",
                "canonical_slug": "canonical",
                "source_paths": ["/x/old-a.md"],
            }
        )
        ledger.write_text(good + "\n" + '{"v": 1, "merge_id": "torn"' , encoding="utf-8")
        records = read_merge_provenance(wiki)
        assert len(records) == 1
        assert records[0]["merge_id"] == "abc123"

    def test_cli_provenance_subcommand(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        from athenaeum.cli import main as cli_main

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                [
                    "merges",
                    "provenance",
                    "--path",
                    str(tmp_path),
                    "--json",
                ]
            )
        assert rc == 0
        records = json.loads(buf.getvalue())
        assert len(records) == 1
        assert records[0]["canonical_slug"] == "canonical"

    def test_cli_provenance_empty_text(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        from athenaeum.cli import main as cli_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["merges", "provenance", "--path", str(tmp_path)])
        assert rc == 0
        assert "0 recorded" in buf.getvalue()


# ---------------------------------------------------------------------------
# Internal helper coverage — direct unit tests for the smaller building
# blocks, mirroring the granularity of existing pending_merges tests.
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_apply_fold_into_existing_signature_has_registry_param(self) -> None:
        """Issue athenaeum#716: _apply_fold_into_existing now DOES have failure
        branches (the fold-graph + narrowing-invariant preflight below) —
        the pre-athenaeum#716 claim that it never returns ``ok: False`` no longer
        holds (see TestCoordinateWidening / TestFoldGraphInvariants for the
        behavioral tests of each new refusal). This is now just a signature
        smoke test for the ``registry`` parameter those checks are gated on."""
        import inspect

        sig = inspect.signature(_apply_fold_into_existing)
        assert "target_path" in sig.parameters
        assert "target_slug" in sig.parameters
        assert "registry" in sig.parameters


# ---------------------------------------------------------------------------
# AC 6 — coordinate widening (issue athenaeum#716): valid-time union, scope
# ancestor-widening, and the narrowing-invariant refusal.
# ---------------------------------------------------------------------------


class TestCoordinateWidening:
    def test_valid_time_widens_to_the_union_of_canonical_and_source(
        self, tmp_path: Path
    ) -> None:
        """Overlapping (here: nested) validity windows fold to their union —
        the canonical's prior window is narrower than the folded source's on
        BOTH ends, so the post-fold window must cover both."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(
            target,
            name="Canonical",
            body="old\n",
            extra={"valid_from": "2026-01-01", "valid_until": "2026-06-30"},
        )
        src_a = wiki / "old-a.md"
        _write_wiki_page(
            src_a,
            name="Old A",
            body="a\n",
            extra={"valid_from": "2025-01-01", "valid_until": "2026-12-31"},
        )

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert meta["valid_from"] == "2025-01-01"
        assert meta["valid_until"] == "2026-12-31"

        records = read_merge_provenance(wiki)
        assert records[0]["coordinates_widened"]["valid-time"] == [
            "2025-01-01",
            "2027-01-01",
        ]

    def test_scope_widens_to_the_shorter_ancestor_prefix(self, tmp_path: Path) -> None:
        """Issue athenaeum#716: "Equivalent content at nested scopes folds into
        the claim with the WIDEST coordinates." The canonical's own PRIOR
        scope is the narrow one here; a folded source carries the wider
        ancestor scope, and the canonical must end up at the wider one even
        though the draft body itself is silent on scope."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(
            target, name="Canonical", body="old\n", extra={"claimed_scope": "org/team-a"}
        )
        src_a = wiki / "old-a.md"
        _write_wiki_page(
            src_a, name="Old A", body="a\n", extra={"claimed_scope": "org"}
        )

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",  # silent on scope
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert meta["claimed_scope"] == "org"
        records = read_merge_provenance(wiki)
        assert records[0]["coordinates_widened"]["scope"] == "org"

    def test_no_widening_needed_reports_empty_coordinates_widened(
        self, tmp_path: Path
    ) -> None:
        """A fold where nothing actually widens (no separator-dimension
        coordinates anywhere) ships an empty ``coordinates_widened`` map —
        not absent, not null."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["ok"] is True

        records = read_merge_provenance(wiki)
        assert records[0]["coordinates_widened"] == {}

    def test_fold_refuses_when_canonical_would_narrow_a_sources_scope(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#716: "silent scope collapse is a fold bug by
        definition." Two genuinely DIFFERENT (sibling, not ancestor/
        descendant) scopes reaching fold-apply time is exactly the
        malformed-proposal case the narrowing invariant exists to catch —
        the widening step cannot manufacture a common ancestor between
        siblings, so it must refuse rather than silently pick one.
        Refusal happens BEFORE any mutation: no commit, no checkbox flip."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(
            target, name="Canonical", body="OLD BODY\n", extra={"claimed_scope": "team-a"}
        )
        src_a = wiki / "old-a.md"
        _write_wiki_page(
            src_a, name="Old A", body="a\n", extra={"claimed_scope": "team-b"}
        )

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="NEW BODY\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "fold_narrows_coordinate"
        assert result["resolved_block"] is None
        # Nothing was mutated: no commit taken, target untouched, source
        # untouched, checkbox still unflipped.
        assert target.read_text(encoding="utf-8") == (
            "---\nname: Canonical\ntype: concept\nclaimed_scope: team-a\n"
            "---\nOLD BODY\n"
        )
        assert src_a.exists()
        src_meta, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
        assert not is_tombstone(src_meta)
        log = subprocess.run(
            ["git", "log", "--format=%H"], cwd=str(wiki), capture_output=True, text=True, check=True
        )
        assert len(log.stdout.splitlines()) == 1  # only init_git_repo's seed commit
        md = merges_path.read_text(encoding="utf-8")
        assert "- [ ]" in md
        assert "- [x]" not in md


# ---------------------------------------------------------------------------
# AC 7 — fold-graph invariants (issue athenaeum#716): acyclic refusal,
# exactly-one-live-canonical (including the chained case), and the
# already-tombstoned-source refusal.
# ---------------------------------------------------------------------------


class TestFoldGraphInvariants:
    def test_fold_refuses_into_an_already_tombstoned_target(self, tmp_path: Path) -> None:
        """A fold target that is itself already a tombstone is refused —
        this single check is BOTH the acyclic guarantee (a live node can
        never have an outgoing edge, so nothing can loop back to one) and
        the exactly-one-live-canonical guarantee (a tombstone is not a live
        canonical of anything new) at once."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # "already-folded" is itself a tombstone pointing at "elsewhere".
        target = wiki / "already-folded.md"
        _write_tombstone_page(target, name="Already Folded", folded_into="elsewhere")
        wiki_elsewhere = wiki / "elsewhere.md"
        _write_wiki_page(wiki_elsewhere, name="Elsewhere", body="e\n")
        src_a = wiki / "new-source.md"
        _write_wiki_page(src_a, name="New Source", body="a\n")

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Already Folded",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "fold_target_is_tombstone"
        assert result["resolved_block"] is None
        assert src_a.exists()
        src_meta, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
        assert not is_tombstone(src_meta)

    def test_fold_refuses_an_already_tombstoned_source(self, tmp_path: Path) -> None:
        """A source that is already a tombstone (folded into a DIFFERENT
        canonical) must not be re-folded — a tombstone's folded_into is set
        once and never overwritten, which is also what keeps the graph
        acyclic by construction."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="c\n")
        other_target = wiki / "other.md"
        _write_wiki_page(other_target, name="Other", body="o\n")
        src_a = wiki / "already-folded-elsewhere.md"
        _write_tombstone_page(
            src_a, name="Already Folded Elsewhere", folded_into="other", body="a\n"
        )

        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Canonical",
            sources=[str(src_a)],
            rationale="r",
            draft_merged_body="merged\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is False
        assert result["error_code"] == "fold_source_already_tombstoned"
        assert result["resolved_block"] is None
        # The source's ORIGINAL fold target is unchanged.
        src_meta, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
        assert tombstone_target(src_meta) == "other"
        # The would-be new canonical was never written.
        assert target.read_text(encoding="utf-8") == (
            "---\nname: Canonical\ntype: concept\n---\nc\n"
        )

    def test_chained_fold_leaves_exactly_one_live_canonical(self, tmp_path: Path) -> None:
        """A folds into B, B LATER folds into C — both steps are legitimate
        (B is live at the time it absorbs A, and still live at the time IT
        is folded into C). After both: exactly one live page among
        {A, B, C} — C — proving the fold-graph invariant holds across a
        chain, not just a single fold."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page_a = wiki / "page-a.md"
        page_b = wiki / "page-b.md"
        page_c = wiki / "page-c.md"
        _write_wiki_page(page_a, name="Page A", body="a\n")
        _write_wiki_page(page_b, name="Page B", body="b\n")
        _write_wiki_page(page_c, name="Page C", body="c\n")
        init_git_repo(wiki)

        merges_path = wiki / "_pending_merges.md"

        # Fold A into B.
        write_pending_merge(
            merges_path,
            merge_target_name="Page B",
            sources=[str(page_a)],
            rationale="r",
            draft_merged_body="merged ab\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id_1 = next(
            pm.id for pm in parse_pending_merges(merges_path) if not pm.resolved
        )
        result_1 = resolve_merge(merges_path, pm_id_1, "approve", wiki_root=wiki)
        assert result_1["ok"] is True, result_1

        # B (still live at this point) now folds into C.
        write_pending_merge(
            merges_path,
            merge_target_name="Page C",
            sources=[str(page_b)],
            rationale="r",
            draft_merged_body="merged bc\n",
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id_2 = next(
            pm.id for pm in parse_pending_merges(merges_path) if not pm.resolved
        )
        result_2 = resolve_merge(merges_path, pm_id_2, "approve", wiki_root=wiki)
        assert result_2["ok"] is True, result_2

        a_meta, _ = parse_frontmatter(page_a.read_text(encoding="utf-8"))
        b_meta, _ = parse_frontmatter(page_b.read_text(encoding="utf-8"))
        c_meta, _ = parse_frontmatter(page_c.read_text(encoding="utf-8"))
        live = [
            p
            for p, meta in (("A", a_meta), ("B", b_meta), ("C", c_meta))
            if not is_tombstone(meta)
        ]
        assert live == ["C"]
        assert tombstone_target(a_meta) == "page-b"
        assert tombstone_target(b_meta) == "page-c"
