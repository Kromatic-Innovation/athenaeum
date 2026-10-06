# SPDX-License-Identifier: Apache-2.0
"""Tests for the unfold compensating-repair path (issue athenaeum#716).

Lane B (a sibling, concurrent lane for this same issue) owns
``src/athenaeum/pending_merges.py`` / ``src/athenaeum/provenance.py`` and is
the module that will eventually WRITE merge-provenance records carrying the
issue athenaeum#716 pinned fields (``folded_sources`` / ``links_rewritten`` /
``canonical_content_hash`` / ...). It had not landed in this checkout at the
time this lane ran, so these tests construct ledger records directly (via
:func:`_write_fold_record`) rather than through a real fold write — this is
exactly the FIXED ledger contract this module is specified to read, so a
test built against it now should keep working once lane B's own writer
lands and starts producing the same shape for real.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum import provenance
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import is_tombstone, parse_frontmatter, render_frontmatter, stamp_tombstone
from athenaeum.store import now_iso
from athenaeum.unfold import (
    UNFOLD_DIRECT,
    UNFOLD_QUEUED,
    can_unfold_directly,
    find_fold_record,
    load_repair_debt_counters,
    unfold_page,
)
from athenaeum.verdicts import content_hash

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_live(path: Path, *, name: str, body: str) -> None:
    path.write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")


def _write_tombstone(
    path: Path, *, name: str, folded_into: str, body: str = "tombstoned body\n"
) -> None:
    meta = stamp_tombstone({"name": name}, folded_into)
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")


def _write_fold_record(wiki_root: Path, record: dict) -> None:
    """Append one merge-provenance record directly — see the module
    docstring for why this bypasses :func:`athenaeum.provenance.record_merge_provenance`."""
    path = provenance.default_merge_provenance_path(wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _base_record(**overrides) -> dict:
    record = {
        "v": 1,
        "ts": now_iso(),
        "merge_id": "m1",
        "write_kind": "fold-into-existing",
        "canonical_slug": "canonical",
        "source_paths": ["source.md"],
        "auto_applied": False,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Direct unfold — canonical untouched since the fold (content-hash equal)
# ---------------------------------------------------------------------------


class TestDirectUnfold:
    def test_untouched_canonical_unfolds_directly(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        canonical_path = wiki_root / "canonical.md"
        source_path = wiki_root / "source.md"
        linker_path = wiki_root / "linker.md"
        _write_live(canonical_path, name="Canonical", body="canonical body\n")
        _write_tombstone(source_path, name="Source", folded_into="canonical")
        _write_live(linker_path, name="Linker", body="See [[canonical]] for details.\n")

        canonical_hash = content_hash(canonical_path.read_text(encoding="utf-8"))
        _write_fold_record(
            wiki_root,
            _base_record(
                folded_sources=["source.md"],
                aliases_added=[],
                links_rewritten=[
                    {"path": "linker.md", "from_slug": "source", "to_slug": "canonical"}
                ],
                canonical_content_hash=canonical_hash,
                coordinates_widened={},
            ),
        )

        result = unfold_page(source_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")

        assert result.action == UNFOLD_DIRECT
        assert result.canonical_slug == "canonical"
        meta, _body = parse_frontmatter(source_path.read_text(encoding="utf-8"))
        assert not is_tombstone(meta)
        assert "folded_into" not in meta
        assert "embedded" not in meta
        # The inbound link was re-pointed back to the restored source.
        assert "[[source]]" in linker_path.read_text(encoding="utf-8")
        assert "[[canonical]]" not in linker_path.read_text(encoding="utf-8")
        # Canonical itself is untouched.
        assert canonical_path.read_text(encoding="utf-8") == (
            "---\nname: Canonical\n---\ncanonical body\n"
        )
        counters = load_repair_debt_counters(tmp_path / "cache")
        assert counters == {"unfold_direct": 1, "unfold_queued": 0}

    def test_ambiguous_sibling_link_is_skipped_not_corrupted(self, tmp_path: Path) -> None:
        """Two sources folded into the same canonical both rewrote a link in
        the SAME sibling file to the SAME slug — restoring one must not
        guess which occurrence was whose (issue athenaeum#716 lane C's
        reported ledger-contract gap #2)."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        canonical_path = wiki_root / "canonical.md"
        source_a = wiki_root / "source-a.md"
        linker_path = wiki_root / "linker.md"
        _write_live(canonical_path, name="Canonical", body="canonical body\n")
        _write_tombstone(source_a, name="Source A", folded_into="canonical")
        _write_live(linker_path, name="Linker", body="[[canonical]] and [[canonical]]\n")

        canonical_hash = content_hash(canonical_path.read_text(encoding="utf-8"))
        _write_fold_record(
            wiki_root,
            _base_record(
                folded_sources=["source-a.md", "source-b.md"],
                aliases_added=[],
                links_rewritten=[
                    {"path": "linker.md", "from_slug": "source-a", "to_slug": "canonical"},
                    {"path": "linker.md", "from_slug": "source-b", "to_slug": "canonical"},
                ],
                canonical_content_hash=canonical_hash,
                coordinates_widened={},
            ),
        )

        result = unfold_page(source_a, wiki_root=wiki_root, cache_dir=tmp_path / "cache")
        assert result.action == UNFOLD_DIRECT
        assert result.details["links_skipped_ambiguous"] == ["linker.md"]
        # Untouched — not corrupted into a wrong slug for either source.
        assert linker_path.read_text(encoding="utf-8") == (
            "---\nname: Linker\n---\n[[canonical]] and [[canonical]]\n"
        )


# ---------------------------------------------------------------------------
# Queued — canonical modified since fold (content-hash comparison, not mtime)
# ---------------------------------------------------------------------------


class TestQueuedCanonicalModified:
    def test_modified_canonical_queues_with_a_diff(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        canonical_path = wiki_root / "canonical.md"
        source_path = wiki_root / "source.md"
        _write_live(canonical_path, name="Canonical", body="canonical body AFTER an edit\n")
        _write_tombstone(
            source_path, name="Source", folded_into="canonical", body="original source text\n"
        )

        # The hash recorded at fold time reflects a DIFFERENT (pre-edit) text.
        stale_hash = content_hash("---\nname: Canonical\n---\ncanonical body BEFORE the edit\n")
        _write_fold_record(
            wiki_root,
            _base_record(
                folded_sources=["source.md"],
                aliases_added=[],
                links_rewritten=[],
                canonical_content_hash=stale_hash,
                coordinates_widened={},
            ),
        )

        result = unfold_page(source_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")

        assert result.action == UNFOLD_QUEUED
        assert result.details["reason"] == "canonical_modified_since_fold"
        # The tombstone itself is untouched — no silent write on the queued path.
        meta, _ = parse_frontmatter(source_path.read_text(encoding="utf-8"))
        assert is_tombstone(meta)

        decisions = list_pending_decisions(wiki_root)
        questions = [d for d in decisions if d["type"] == "question"]
        assert len(questions) == 1
        description = questions[0]["payload"]["description"]
        assert "canonical_modified_since_fold" in description
        assert "```diff" in description
        assert "original source text" in description
        assert "canonical body AFTER an edit" in description

        counters = load_repair_debt_counters(tmp_path / "cache")
        assert counters == {"unfold_direct": 0, "unfold_queued": 1}


# ---------------------------------------------------------------------------
# Queued — the real-corpus case: a ledger record predating the pinned fields
# ---------------------------------------------------------------------------


class TestQueuedMissingPinnedFields:
    def test_older_record_without_pinned_fields_queues(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        canonical_path = wiki_root / "canonical.md"
        source_path = wiki_root / "source.md"
        _write_live(canonical_path, name="Canonical", body="canonical body\n")
        _write_tombstone(source_path, name="Source", folded_into="canonical")

        # A record with only the pre-athenaeum#716 fields — no folded_sources /
        # links_rewritten / canonical_content_hash.
        _write_fold_record(wiki_root, _base_record())

        record = find_fold_record(wiki_root, source_path, "canonical")
        assert record is not None
        ok, reason = can_unfold_directly(record, canonical_path)
        assert ok is False
        assert reason == "ledger_record_missing_unfold_fields"

        result = unfold_page(source_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")
        assert result.action == UNFOLD_QUEUED
        assert result.details["reason"] == "ledger_record_missing_unfold_fields"


class TestQueuedNoLedgerRecord:
    def test_no_matching_record_at_all_queues(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        canonical_path = wiki_root / "canonical.md"
        source_path = wiki_root / "source.md"
        _write_live(canonical_path, name="Canonical", body="canonical body\n")
        _write_tombstone(source_path, name="Source", folded_into="canonical")
        # No provenance file at all.

        result = unfold_page(source_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")
        assert result.action == UNFOLD_QUEUED
        assert result.details["reason"] == "no_ledger_record"


class TestQueuedMissingFoldTarget:
    def test_tombstone_without_folded_into_queues(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        source_path = wiki_root / "source.md"
        # Stamped folded, but no folded_into — a malformed/legacy tombstone.
        source_path.write_text("---\nname: Source\nstatus: folded\n---\nbody\n", encoding="utf-8")

        result = unfold_page(source_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")
        assert result.action == UNFOLD_QUEUED
        assert result.canonical_slug is None
        assert result.details["reason"] == "tombstone_missing_fold_target"


class TestNonTombstoneRefused:
    def test_live_page_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        live_path = wiki_root / "live.md"
        _write_live(live_path, name="Live", body="just a normal page\n")
        import pytest

        with pytest.raises(ValueError, match="not a tombstone"):
            unfold_page(live_path, wiki_root=wiki_root, cache_dir=tmp_path / "cache")


# ---------------------------------------------------------------------------
# Repair-debt counters
# ---------------------------------------------------------------------------


class TestRepairDebtCounters:
    def test_fresh_cache_dir_reads_as_zero(self, tmp_path: Path) -> None:
        assert load_repair_debt_counters(tmp_path / "cache") == {
            "unfold_direct": 0,
            "unfold_queued": 0,
        }

    def test_corrupt_counters_file_fails_open_to_zero(self, tmp_path: Path) -> None:
        from athenaeum.unfold import REPAIR_DEBT_COUNTERS_NAME

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / REPAIR_DEBT_COUNTERS_NAME).write_text("not json", encoding="utf-8")
        assert load_repair_debt_counters(cache_dir) == {"unfold_direct": 0, "unfold_queued": 0}

    def test_counters_accumulate_across_calls(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"

        _write_live(wiki_root / "canonical.md", name="Canonical", body="body\n")
        for i in range(2):
            source_path = wiki_root / f"source-{i}.md"
            _write_tombstone(source_path, name=f"Source {i}", folded_into="canonical")
            # No ledger record for either -> both queue.
            unfold_page(source_path, wiki_root=wiki_root, cache_dir=cache_dir)

        assert load_repair_debt_counters(cache_dir) == {"unfold_direct": 0, "unfold_queued": 2}
