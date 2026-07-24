# SPDX-License-Identifier: Apache-2.0
"""Tests for the retraction cascade (issue #435).

Wires issue #425's merge-provenance ledger to issue #427's observation
supersession records: retracting a source that a completed merge relied on
flags that merge for human review — it never auto-unmerges.

Acceptance criteria under test:

1. End-to-end fixture flow: execute a merge with provenance → retract a
   supporting source → a correctly-linked ``retraction`` review item appears
   in ``list_pending_decisions``.
2. Retracting a source NOT in any merge's provenance flags nothing.
3. The flag is idempotent — re-scanning does not duplicate the review item.
4. No auto-unmerge path exists (the merge target is untouched by a scan, and
   the module exports no unmerge function).
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.decisions import list_pending_decisions
from athenaeum.pending_merges import (
    parse_pending_merges,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.pii import append_supersession
from athenaeum.provenance import read_merge_provenance, record_merge_provenance
from athenaeum.retraction_cascade import (
    build_retraction_review_record,
    read_retraction_reviews,
    review_id,
    scan_retraction_cascade,
)


def _write_wiki_page(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: concept\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _execute_merge(wiki: Path, *, target_name: str, sources: list[str]) -> str:
    """Run a real fold-into-existing merge, returning its merge id.

    The merge records provenance (issue #425) listing ``sources`` as its
    supporting ``source_paths`` — the exact fact the cascade keys on.
    """
    merges_path = wiki / "_pending_merges.md"
    write_pending_merge(
        merges_path,
        merge_target_name=target_name,
        sources=sources,
        rationale="r",
        draft_merged_body="merged\n",
        confidence=0.9,
        write_kind="fold-into-existing",
    )
    pm_id = parse_pending_merges(merges_path)[0].id
    result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
    assert result["ok"] is True
    return pm_id


# ---------------------------------------------------------------------------
# AC 1 — end-to-end: merge with provenance → retract a supporting source →
# a correctly-linked review item appears in list_pending_decisions.
# ---------------------------------------------------------------------------


class TestEndToEndCascade:
    def test_retracting_supporting_source_flags_dependent_merge(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        _write_wiki_page(wiki / "canonical.md", name="Canonical", body="old\n")
        src = wiki / "old-a.md"
        _write_wiki_page(src, name="Old A", body="a\n")

        merge_id = _execute_merge(wiki, target_name="Canonical", sources=[str(src)])
        # A merge's provenance now lists str(src) as a supporting source.
        assert read_merge_provenance(wiki)[0]["source_paths"] == [str(src)]

        # Retract that supporting source (issue #427 supersession).
        append_supersession(
            contacts, retracts=str(src), reason="source was fabricated", at="2026-07-24T00:00:00Z"
        )

        newly = scan_retraction_cascade(wiki, contacts)
        assert len(newly) == 1

        decisions = list_pending_decisions(wiki)
        retractions = [d for d in decisions if d["type"] == "retraction"]
        assert len(retractions) == 1
        item = retractions[0]
        # Correctly linked: names the dependent merge, retracted source, reason.
        assert item["payload"]["merge_id"] == merge_id
        assert item["payload"]["canonical_slug"] == "canonical"
        assert item["payload"]["retracted_ref"] == str(src)
        assert item["payload"]["reason"] == "source was fabricated"
        assert str(src) in item["summary"]
        assert item["confidence"] is None
        assert item["created_at"] == "2026-07-24T00:00:00Z"

    def test_review_item_persisted_to_sidecar(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="create-merged",
            canonical_slug="topic",
            source_paths=["obs-1", "obs-2"],
        )
        append_supersession(contacts, retracts="obs-1", reason="wrong", at="2026-07-24T01:00:00Z")

        scan_retraction_cascade(wiki, contacts)
        persisted = read_retraction_reviews(wiki)
        assert len(persisted) == 1
        assert persisted[0]["merge_id"] == "m1"
        assert persisted[0]["retracted_ref"] == "obs-1"
        assert persisted[0]["v"] == 1


# ---------------------------------------------------------------------------
# AC 2 — retracting a source not in any merge's provenance flags nothing.
# ---------------------------------------------------------------------------


class TestNoSpuriousFlags:
    def test_retracting_unrelated_source_flags_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="create-merged",
            canonical_slug="topic",
            source_paths=["obs-1"],
        )
        # Retract an observation that no merge relied on.
        append_supersession(contacts, retracts="obs-999", reason="n/a", at="2026-07-24T00:00:00Z")

        newly = scan_retraction_cascade(wiki, contacts)
        assert newly == []
        assert read_retraction_reviews(wiki) == []
        decisions = list_pending_decisions(wiki)
        assert [d for d in decisions if d["type"] == "retraction"] == []

    def test_no_supersessions_flags_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="create-merged",
            canonical_slug="topic",
            source_paths=["obs-1"],
        )
        assert scan_retraction_cascade(wiki, contacts) == []

    def test_no_provenance_flags_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        append_supersession(contacts, retracts="obs-1", reason="x", at="2026-07-24T00:00:00Z")
        assert scan_retraction_cascade(wiki, contacts) == []


# ---------------------------------------------------------------------------
# AC 3 — idempotent: re-processing the same retraction does not duplicate.
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rescan_does_not_duplicate(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="create-merged",
            canonical_slug="topic",
            source_paths=["obs-1"],
        )
        append_supersession(contacts, retracts="obs-1", reason="wrong", at="2026-07-24T00:00:00Z")

        first = scan_retraction_cascade(wiki, contacts)
        assert len(first) == 1
        second = scan_retraction_cascade(wiki, contacts)
        assert second == []  # nothing NEW to flag on a re-scan

        # Exactly one review item, both on disk and in the unified queue.
        assert len(read_retraction_reviews(wiki)) == 1
        decisions = list_pending_decisions(wiki)
        assert len([d for d in decisions if d["type"] == "retraction"]) == 1

    def test_two_supersessions_same_obs_flag_merge_once(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="create-merged",
            canonical_slug="topic",
            source_paths=["obs-1"],
        )
        append_supersession(contacts, retracts="obs-1", reason="first", at="2026-07-24T00:00:00Z")
        append_supersession(contacts, retracts="obs-1", reason="second", at="2026-07-24T02:00:00Z")

        newly = scan_retraction_cascade(wiki, contacts)
        assert len(newly) == 1  # deduped within a single scan
        assert len(read_retraction_reviews(wiki)) == 1

    def test_one_retraction_across_multiple_merges_flags_each(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        for mid, slug in (("m1", "topic-a"), ("m2", "topic-b")):
            record_merge_provenance(
                wiki,
                merge_id=mid,
                write_kind="create-merged",
                canonical_slug=slug,
                source_paths=["obs-shared"],
            )
        append_supersession(
            contacts, retracts="obs-shared", reason="bad", at="2026-07-24T00:00:00Z"
        )
        newly = scan_retraction_cascade(wiki, contacts)
        assert {r["merge_id"] for r in newly} == {"m1", "m2"}


# ---------------------------------------------------------------------------
# AC 4 — no auto-unmerge: a scan never touches the merged page, and the
# module exposes no unmerge path.
# ---------------------------------------------------------------------------


class TestNeverUnmerges:
    def test_scan_leaves_merged_page_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        contacts = tmp_path / "contacts"
        _write_wiki_page(wiki / "canonical.md", name="Canonical", body="old\n")
        src = wiki / "old-a.md"
        _write_wiki_page(src, name="Old A", body="a\n")
        _execute_merge(wiki, target_name="Canonical", sources=[str(src)])

        canonical_before = (wiki / "canonical.md").read_text(encoding="utf-8")
        append_supersession(contacts, retracts=str(src), reason="x", at="2026-07-24T00:00:00Z")
        scan_retraction_cascade(wiki, contacts)

        # The merge stands: the canonical page is byte-for-byte unchanged and
        # the folded source is NOT resurrected.
        assert (wiki / "canonical.md").read_text(encoding="utf-8") == canonical_before
        assert not src.exists()  # still gone (consumed by the fold), not un-deleted

    def test_module_exports_no_unmerge_function(self) -> None:
        import athenaeum.retraction_cascade as rc

        names = [n.lower() for n in dir(rc)]
        assert not any("unmerge" in n or "revert" in n or "undo" in n for n in names)


# ---------------------------------------------------------------------------
# Unit-level: record builder, reader tolerance, idempotency key.
# ---------------------------------------------------------------------------


class TestRecordAndReader:
    def test_review_id_is_deterministic_per_pair(self) -> None:
        a = review_id("m1", "obs-1")
        assert a == review_id("m1", "obs-1")
        assert a != review_id("m2", "obs-1")
        assert a != review_id("m1", "obs-2")

    def test_build_record_shape(self) -> None:
        rec = build_retraction_review_record(
            merge_id="m1",
            canonical_slug="topic",
            retracted_ref="obs-1",
            reason="wrong",
            created_at="2026-07-24T00:00:00Z",
        )
        assert rec["id"] == review_id("m1", "obs-1")
        assert rec["merge_id"] == "m1"
        assert rec["canonical_slug"] == "topic"
        assert rec["retracted_ref"] == "obs-1"
        assert rec["reason"] == "wrong"
        assert rec["created_at"] == "2026-07-24T00:00:00Z"
        assert rec["v"] == 1

    def test_reader_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_retraction_reviews(tmp_path / "wiki") == []

    def test_reader_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = wiki / "_pending_retractions.jsonl"
        path.write_text(
            '{"id":"a","merge_id":"m1"}\n{"id":"b","merge_id":',  # torn 2nd line
            encoding="utf-8",
        )
        recs = read_retraction_reviews(wiki)
        assert len(recs) == 1
        assert recs[0]["id"] == "a"
