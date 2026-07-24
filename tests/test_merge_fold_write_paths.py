# SPDX-License-Identifier: Apache-2.0
"""Tests for the merge-vs-fold write paths (issue #425).

Covers:

1. ``fold-into-existing`` folds sources into the pre-existing target page —
   ``target_exists`` is unreachable for a correctly-classified proposal.
2. ``create-merged`` is UNCHANGED — a misclassified create-kind proposal
   that hits an existing slug still fails closed with ``target_exists``.
3. Inbound wikilink rewrite across ``wiki/``; old source files deleted;
   ``aliases:`` added + deduped on re-fold; link-time alias resolution.
4. Vector-store hygiene: deleted slugs purged, aliases never embedded.
5. Provenance recording + the read API (library + CLI).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.models import parse_frontmatter, slugify
from athenaeum.pending_merges import (
    _apply_fold_into_existing,
    parse_pending_merges,
    resolve_alias_slug,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.provenance import read_merge_provenance


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: feedback\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _write_wiki_page(path: Path, *, name: str, body: str = "", aliases=None) -> None:
    fm = [f"name: {name}", "type: concept"]
    if aliases:
        alias_yaml = ", ".join(f'"{a}"' for a in aliases)
        fm.append(f"aliases: [{alias_yaml}]")
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + body, encoding="utf-8"
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

    def test_source_files_deleted_after_fold(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_a = wiki / "old-a.md"
        src_b = wiki / "old-b.md"
        _write_wiki_page(src_a, name="Old A", body="a\n")
        _write_wiki_page(src_b, name="Old B", body="b\n")

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
        assert not src_a.exists()
        assert not src_b.exists()
        assert set(result["folded_sources"]) == {str(src_a), str(src_b)}

    def test_fold_does_not_delete_canonical_reappearing_in_own_sources(
        self, tmp_path: Path
    ) -> None:
        """A source path that IS the canonical page (same slug) must never be
        deleted — only the OTHER sources are folded away."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "canonical.md"
        _write_wiki_page(target, name="Canonical", body="old\n")
        src_b = wiki / "old-b.md"
        _write_wiki_page(src_b, name="Old B", body="b\n")

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
        assert not src_b.exists()
        assert result["folded_sources"] == [str(src_b)]


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
        must still fail closed if the slug is actually taken."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target = wiki / "existing-name.md"
        target.write_text("PRE-EXISTING\n", encoding="utf-8")

        src_a = wiki / "x.md"
        src_b = wiki / "y.md"
        _write_source(src_a, name="x")
        _write_source(src_b, name="y")

        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="existing-name",
            sources=[str(src_a), str(src_b)],
            rationale="r",
            draft_merged_body="draft",
            confidence=0.9,
            write_kind="create-merged",
        )
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
        assert rec["v"] == 1
        assert "ts" in rec

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
    def test_apply_fold_into_existing_reports_ok_false_never(self) -> None:
        """_apply_fold_into_existing has no failure branch — it is only
        invoked once write_kind classification has already guaranteed the
        target exists. Documents the contract as a smoke test."""
        import inspect

        sig = inspect.signature(_apply_fold_into_existing)
        assert "target_path" in sig.parameters
        assert "target_slug" in sig.parameters
