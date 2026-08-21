# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-file quarantine mechanism (issue athenaeum#898).

Covers the mechanical action this module owns — physically moving a raw
file out of ``raw_root`` into ``<wiki_root>/_quarantine/``, the append-only
audit ledger, and the reversal path (``release_quarantine``) — independent
of the consecutive-bound-violation counting that decides WHEN to call it
(that lives in :mod:`athenaeum.librarian`, covered by
``tests/test_librarian_quarantine.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.quarantine import (
    QUARANTINE_KIND,
    RELEASE_KIND,
    list_pending_quarantine,
    quarantine_file,
    quarantine_item_id,
    read_quarantine_ledger,
    release_quarantine,
)


class _FakeRaw:
    """Minimal RawFile double — path/source/ref, no content needed here."""

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source

    @property
    def ref(self) -> str:
        return f"{self.source}/{self.path.name}"


def _make_tree(tmp_path: Path) -> tuple[Path, Path, _FakeRaw]:
    raw_root = tmp_path / "raw"
    wiki_root = tmp_path / "wiki"
    sessions = raw_root / "sessions"
    sessions.mkdir(parents=True)
    wiki_root.mkdir()
    fpath = sessions / "20260101T000000Z-aabbccdd.md"
    fpath.write_text("Some content.\n", encoding="utf-8")
    return raw_root, wiki_root, _FakeRaw(fpath, "sessions")


# ---------------------------------------------------------------------------
# quarantine_item_id — deterministic per (ref, created_at) event
# ---------------------------------------------------------------------------


class TestQuarantineItemId:
    def test_deterministic(self) -> None:
        assert quarantine_item_id("a/b.md", "t1") == quarantine_item_id("a/b.md", "t1")

    def test_distinct_events_get_distinct_ids(self) -> None:
        # Same ref, different quarantine EVENT (a later re-offense after a
        # release) must not collide.
        assert quarantine_item_id("a/b.md", "t1") != quarantine_item_id("a/b.md", "t2")


# ---------------------------------------------------------------------------
# quarantine_file — the physical move + audit-ledger record (AC 4)
# ---------------------------------------------------------------------------


class TestQuarantineFile:
    def test_moves_file_out_of_raw_root(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="bytes",
            detail="9,700,000 bytes exceeds the 5,242,880-byte limit",
            violations=2,
        )
        assert not raw.path.exists()
        moved = wiki_root / "_quarantine" / "sessions" / "20260101T000000Z-aabbccdd.md"
        assert moved.exists()
        assert moved.read_text(encoding="utf-8") == "Some content.\n"

    def test_writes_audit_ledger_record_naming_file_and_bound(
        self, tmp_path: Path
    ) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="llm_calls",
            detail="12 call(s) > 8-call limit",
            violations=2,
        )
        assert record["kind"] == QUARANTINE_KIND
        assert record["ref"] == "sessions/20260101T000000Z-aabbccdd.md"
        assert record["source"] == "sessions"
        assert record["bound"] == "llm_calls"
        assert record["detail"] == "12 call(s) > 8-call limit"
        assert record["violations"] == 2

        ledger = read_quarantine_ledger(wiki_root)
        assert ledger == [record]

    def test_returns_reconstructable_paths(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="wall_clock",
            detail="900.0s > 120s limit",
            violations=2,
        )
        assert (raw_root / record["original_path"]).parent.exists()
        assert (wiki_root / record["quarantine_path"]).exists()

    def test_ledger_record_written_before_the_move_survives_a_move_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Code-review finding (athenaeum#898): the ledger write and the physical
        move are two separate steps, not atomic. If the move raises (disk
        full, permission error), the CHOSEN failure mode must be a ledger
        record pointing at a file still in its original place — detectable
        (visible to anyone reading the ledger) — never an orphaned file
        moved with no record (invisible, findable only by a manual
        filesystem search). This is what justifies writing the ledger
        record FIRST, before shutil.move.
        """
        raw_root, wiki_root, raw = _make_tree(tmp_path)

        def _boom(*args, **kwargs):
            raise OSError("simulated disk-full mid-move")

        monkeypatch.setattr("athenaeum.store.FilesystemStore.move", _boom)

        with pytest.raises(OSError, match="simulated disk-full"):
            quarantine_file(
                raw,
                wiki_root=wiki_root,
                raw_root=raw_root,
                bound="bytes",
                detail="d",
                violations=2,
            )

        # The ledger record landed anyway (detectable) ...
        ledger = read_quarantine_ledger(wiki_root)
        assert len(ledger) == 1
        assert ledger[0]["ref"] == raw.ref
        # ... and the file was never moved (still exactly where it was).
        assert raw.path.exists()
        assert raw.path.read_text(encoding="utf-8") == "Some content.\n"
        moved = wiki_root / ledger[0]["quarantine_path"]
        assert not moved.exists()


# ---------------------------------------------------------------------------
# read_quarantine_ledger — tolerant reader
# ---------------------------------------------------------------------------


class TestReadQuarantineLedger:
    def test_missing_ledger_returns_empty(self, tmp_path: Path) -> None:
        assert read_quarantine_ledger(tmp_path / "wiki") == []

    def test_torn_trailing_line_skipped(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ledger_path = wiki_root / "_quarantine.jsonl"
        ledger_path.write_text(
            '{"v": 1, "kind": "quarantine", "id": "abc"}\n{"v": 1, "kind": "quar',
            encoding="utf-8",
        )
        records = read_quarantine_ledger(wiki_root)
        assert len(records) == 1
        assert records[0]["id"] == "abc"


# ---------------------------------------------------------------------------
# list_pending_quarantine — AC 5 (pending-decisions listing surface source)
# ---------------------------------------------------------------------------


class TestListPendingQuarantine:
    def test_quarantine_without_release_is_pending(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        pending = list_pending_quarantine(wiki_root)
        assert len(pending) == 1
        assert pending[0]["ref"] == raw.ref

    def test_released_item_is_not_pending(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        release_quarantine(
            wiki_root, raw_root, quarantine_id=record["id"], note="false positive"
        )
        assert list_pending_quarantine(wiki_root) == []


# ---------------------------------------------------------------------------
# release_quarantine — AC 6, reversibility
# ---------------------------------------------------------------------------


class TestReleaseQuarantine:
    def test_moves_file_back_to_discovery_set(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        assert not raw.path.exists()

        release_quarantine(wiki_root, raw_root, quarantine_id=record["id"])

        assert raw.path.exists()
        assert raw.path.read_text(encoding="utf-8") == "Some content.\n"
        moved = wiki_root / "_quarantine" / "sessions" / raw.path.name
        assert not moved.exists()

    def test_writes_release_record(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        release = release_quarantine(
            wiki_root, raw_root, quarantine_id=record["id"], note="reviewed, false positive"
        )
        assert release["kind"] == RELEASE_KIND
        assert release["id"] == record["id"]
        assert release["note"] == "reviewed, false positive"

        ledger = read_quarantine_ledger(wiki_root)
        kinds = {r["kind"] for r in ledger}
        assert kinds == {QUARANTINE_KIND, RELEASE_KIND}

    def test_unknown_id_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        with pytest.raises(ValueError, match="unknown quarantine item id"):
            release_quarantine(wiki_root, raw_root, quarantine_id="nope")

    def test_already_released_raises(self, tmp_path: Path) -> None:
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        release_quarantine(wiki_root, raw_root, quarantine_id=record["id"])
        with pytest.raises(ValueError, match="already released"):
            release_quarantine(wiki_root, raw_root, quarantine_id=record["id"])

    def test_missing_file_on_disk_still_releases_the_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An operator who manually deleted the quarantined file must still
        be able to clear the pending decision — never a permanently-stuck
        item because the underlying file evaporated."""
        raw_root, wiki_root, raw = _make_tree(tmp_path)
        record = quarantine_file(
            raw, wiki_root=wiki_root, raw_root=raw_root, bound="bytes", detail="d", violations=2
        )
        (wiki_root / record["quarantine_path"]).unlink()

        release = release_quarantine(wiki_root, raw_root, quarantine_id=record["id"])
        assert release["kind"] == RELEASE_KIND
        assert list_pending_quarantine(wiki_root) == []


# ---------------------------------------------------------------------------
# Recall-leak regression (code-review finding, athenaeum#898): quarantine's
# whole point is that a file pulled out of compile for being poison must
# not be served back as an answer. wiki/_quarantine/<source>/<name>.md kept
# its ORIGINAL basename, and every RECURSIVE wiki_root walk in
# athenaeum.search filtered only the leaf FILENAME for an underscore
# prefix — so a quarantined file sailed straight through unfiltered and
# was served as a keyword-recall hit. Fixed via
# athenaeum.search._wiki_relpath_excluded (checks every path SEGMENT, not
# just the filename).
# ---------------------------------------------------------------------------


class TestQuarantinedContentInvisibleToKeywordRecall:
    def test_quarantined_file_does_not_appear_in_keyword_query_results(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.search import KeywordBackend

        wiki_root = tmp_path / "wiki"
        (wiki_root / "_quarantine" / "contacts").mkdir(parents=True)
        (wiki_root / "alice.md").write_text(
            "---\ntype: person\n---\n\nAlice likes zebrafish.\n", encoding="utf-8"
        )
        (wiki_root / "_quarantine" / "contacts" / "poison.md").write_text(
            "---\ntype: person\n---\n\nPoison record mentions zebrafish too.\n",
            encoding="utf-8",
        )

        hits = KeywordBackend().query(
            "zebrafish", tmp_path / "cache", n=10, wiki_root=wiki_root
        )
        names = [h[0] for h in hits]
        assert names == ["alice.md"], (
            "a quarantined file was returned as a recall hit — the exact "
            "inversion of AC 4 ('moves the file out of the discovery set') "
            "that matters most"
        )

    def test_quarantined_file_excluded_from_keyword_backend_count(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.search import KeywordBackend

        wiki_root = tmp_path / "wiki"
        (wiki_root / "_quarantine" / "contacts").mkdir(parents=True)
        (wiki_root / "alice.md").write_text(
            "---\ntype: person\n---\n\nAlice.\n", encoding="utf-8"
        )
        (wiki_root / "_quarantine" / "contacts" / "poison.md").write_text(
            "---\ntype: person\n---\n\nPoison.\n", encoding="utf-8"
        )

        count = KeywordBackend().build_index(wiki_root, tmp_path / "cache")
        assert count == 1
