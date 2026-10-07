# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`athenaeum.decisions.migrate_legacy_queues` (issue athenaeum#1992).

Builds a fixture legacy store (pending merges + pending questions, each with
a resolved AND an unresolved record, plus two PII-hazard merge proposals
that are never approved) entirely under ``tmp_path`` — no live
``~/knowledge`` store is read or written anywhere in this module.

The issue's own test AC: "a fixture legacy store (pending merges + pending
questions) migrates to the unified schema with zero item-count drift and
zero disposition drift, verified by id-set comparison, not by count alone."
Every assertion below compares SETS of ids (and a disposition dict keyed by
id), never bare counts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.answers import (
    parse_pending_questions,
    raise_pending_question,
    resolve_by_id,
)
from athenaeum.decisions import MigrationReport, load_migrated_queue, migrate_legacy_queues
from athenaeum.pending_merges import (
    _make_id,
    _rewrite_block_resolved,
    parse_pending_merges,
    render_block,
)
from athenaeum.recompare import identify_pii_hazards


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _write_source(path: Path, *, name: str, pii: bool = False, body: str = "body text\n") -> None:
    lines = ["---", f"name: {name}"]
    if pii:
        lines.append("pii: true")
    lines += ["---", "", body]
    path.write_text("\n".join(lines), encoding="utf-8")


def _approve_block(wiki_root: Path, *, target: str, sources: list[str], rationale: str) -> str:
    """Build an already-RESOLVED (approved) merge block, text-only (no I/O).

    Mirrors what ``resolve_merge(..., "approve")`` would stamp on a block,
    via the same pure text transform (:func:`_rewrite_block_resolved`) it
    uses internally — without triggering any of that function's write-side
    effects (wiki-page writes, source tombstoning, vector purge), which
    would need a real embedding backend. Returns the resolved block text.
    """
    unresolved = render_block(
        merge_target_name=target,
        sources=sources,
        rationale=rationale,
        draft_merged_body="## merged\n\nstapled body\n",
        confidence=0.9,
        created_at="2026-01-01",
    )
    return _rewrite_block_resolved(
        unresolved, "approve", "", answered_at="2026-01-02T00:00:00+00:00"
    )


def _write_merges_file(wiki_root: Path, blocks: list[str]) -> Path:
    path = wiki_root / "_pending_merges.md"
    text = "# Pending Merges\n\n---\n\n" + "\n\n---\n\n".join(blocks) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def legacy_store(wiki_root: Path) -> dict:
    """Build the fixture legacy store: 4 merges + 2 questions, known ids.

    - ``merge_pending``: plain, unresolved.
    - ``merge_approved``: plain, resolved/approved (text-level only).
    - ``merge_pii_1`` / ``merge_pii_2``: the two PII-hazard proposals named
      by athenaeum#1992's AC — sources carry ``pii: true`` frontmatter, so
      :func:`identify_pii_hazards` flags them, and they are NEVER resolved
      anywhere in this fixture (no code path approves them).
    - ``question_pending``: plain, unanswered.
    - ``question_answered``: plain, answered via the real
      :func:`athenaeum.answers.resolve_by_id` (pure text I/O, no network).
    """
    questions_path = wiki_root / "_pending_questions.md"

    src_a1 = wiki_root / "src_a1.md"
    src_a2 = wiki_root / "src_a2.md"
    _write_source(src_a1, name="a1")
    _write_source(src_a2, name="a2")

    src_b1 = wiki_root / "src_b1.md"
    src_b2 = wiki_root / "src_b2.md"
    _write_source(src_b1, name="b1")
    _write_source(src_b2, name="b2")

    src_p1a = wiki_root / "src_p1a.md"
    src_p1b = wiki_root / "src_p1b.md"
    _write_source(src_p1a, name="p1a", pii=True, body="contact me at alice@example.com\n")
    _write_source(src_p1b, name="p1b", pii=True)

    src_p2a = wiki_root / "src_p2a.md"
    src_p2b = wiki_root / "src_p2b.md"
    _write_source(src_p2a, name="p2a", pii=True, body="call 555-0100\n")
    _write_source(src_p2b, name="p2b", pii=True)

    pending_id = _make_id([str(src_a1), str(src_a2)], "merge-pending")
    approved_id = _make_id([str(src_b1), str(src_b2)], "merge-approved")
    pii1_id = _make_id([str(src_p1a), str(src_p1b)], "merge-pii-1")
    pii2_id = _make_id([str(src_p2a), str(src_p2b)], "merge-pii-2")

    pending_block = render_block(
        merge_target_name="merge-pending",
        sources=[str(src_a1), str(src_a2)],
        rationale="plain pending",
        draft_merged_body="## merged\n\nstapled\n",
        confidence=0.85,
        created_at="2026-01-01",
    )
    approved_block = _approve_block(
        wiki_root,
        target="merge-approved",
        sources=[str(src_b1), str(src_b2)],
        rationale="plain approved",
    )
    pii1_block = render_block(
        merge_target_name="merge-pii-1",
        sources=[str(src_p1a), str(src_p1b)],
        rationale="pii hazard one",
        draft_merged_body="## merged\n\nstapled\n",
        confidence=0.70,
        created_at="2026-01-01",
    )
    pii2_block = render_block(
        merge_target_name="merge-pii-2",
        sources=[str(src_p2a), str(src_p2b)],
        rationale="pii hazard two",
        draft_merged_body="## merged\n\nstapled\n",
        confidence=0.70,
        created_at="2026-01-01",
    )
    _write_merges_file(
        wiki_root, [pending_block, approved_block, pii1_block, pii2_block]
    )

    pending_q = raise_pending_question(
        questions_path,
        "Is X true?",
        "context",
        entity="thing-pending",
        source="some/page.md",
    )
    answered_q = raise_pending_question(
        questions_path,
        "Is Y true?",
        "context",
        entity="thing-answered",
        source="some/other.md",
    )
    resolve_by_id(
        questions_path,
        answered_q["decision_id"],
        "yes, confirmed",
        answered_at="2026-01-03T00:00:00+00:00",
    )

    return {
        "wiki_root": wiki_root,
        "merge_pending_id": pending_id,
        "merge_approved_id": approved_id,
        "merge_pii_1_id": pii1_id,
        "merge_pii_2_id": pii2_id,
        "pii_sources": {
            pii1_id: [src_p1a, src_p1b],
            pii2_id: [src_p2a, src_p2b],
        },
        "question_pending_id": pending_q["decision_id"],
        "question_answered_id": answered_q["decision_id"],
    }


def test_pii_hazard_fixtures_are_actually_flagged(legacy_store: dict) -> None:
    """Ground the fixture's own claim: both PII proposals really are hazards."""
    for pii_id, sources in legacy_store["pii_sources"].items():
        reasons = identify_pii_hazards(sources)
        assert reasons, f"fixture proposal {pii_id} is not actually PII-hazardous"


def test_legacy_store_disposition_before_migration(legacy_store: dict) -> None:
    """Sanity-check the fixture's own BEFORE state, read straight off the legacy files."""
    wiki_root = legacy_store["wiki_root"]
    merges = {pm.id: pm for pm in parse_pending_merges(wiki_root / "_pending_merges.md")}
    questions = {
        pq.id: pq for pq in parse_pending_questions(wiki_root / "_pending_questions.md")
    }

    assert not merges[legacy_store["merge_pending_id"]].resolved
    assert merges[legacy_store["merge_approved_id"]].resolved
    assert merges[legacy_store["merge_approved_id"]].decision == "approve"
    # The two PII-hazard proposals: unresolved before migration, by id.
    assert not merges[legacy_store["merge_pii_1_id"]].resolved
    assert not merges[legacy_store["merge_pii_2_id"]].resolved

    assert not questions[legacy_store["question_pending_id"]].answered
    assert questions[legacy_store["question_answered_id"]].answered


def test_migration_zero_item_count_and_disposition_drift(legacy_store: dict) -> None:
    """The issue's own test AC: id-set comparison, not a bare count."""
    wiki_root = legacy_store["wiki_root"]

    # BEFORE snapshot, read directly off the legacy stores.
    before_merges = {pm.id: pm for pm in parse_pending_merges(wiki_root / "_pending_merges.md")}
    before_questions = {
        pq.id: pq for pq in parse_pending_questions(wiki_root / "_pending_questions.md")
    }
    before_ids = set(before_merges) | set(before_questions)

    report = migrate_legacy_queues(wiki_root)
    assert isinstance(report, MigrationReport)

    # Zero item-count drift, verified by id SET equality (not len(...) ==).
    assert report.ids == before_ids
    assert report.merge_count == len(before_merges)
    assert report.question_count == len(before_questions)

    # Zero disposition drift, verified per id.
    for mid, pm in before_merges.items():
        if not pm.resolved:
            assert report.by_id[mid] == "pending"
        elif pm.decision == "approve":
            assert report.by_id[mid] == "approved"
        elif pm.decision == "reject":
            assert report.by_id[mid] == "rejected"
    for qid, pq in before_questions.items():
        assert report.by_id[qid] == ("answered" if pq.answered else "pending")

    # The two PII-hazard proposals: still unapproved AFTER migration, by id
    # (never by count) — the issue's explicit AC.
    assert report.by_id[legacy_store["merge_pii_1_id"]] == "pending"
    assert report.by_id[legacy_store["merge_pii_2_id"]] == "pending"

    # The persisted mirror round-trips to the same id set and dispositions.
    persisted = load_migrated_queue(wiki_root)
    persisted_by_id = {rec["id"]: rec["disposition"] for rec in persisted}
    assert set(persisted_by_id) == before_ids
    assert persisted_by_id == report.by_id


def test_migration_never_mutates_the_legacy_stores(legacy_store: dict) -> None:
    """Migration is read-only w.r.t. the legacy files — byte-identical before/after."""
    wiki_root = legacy_store["wiki_root"]
    merges_before = (wiki_root / "_pending_merges.md").read_text(encoding="utf-8")
    questions_before = (wiki_root / "_pending_questions.md").read_text(encoding="utf-8")

    migrate_legacy_queues(wiki_root)

    assert (wiki_root / "_pending_merges.md").read_text(encoding="utf-8") == merges_before
    assert (wiki_root / "_pending_questions.md").read_text(encoding="utf-8") == questions_before


def test_migration_is_idempotent(legacy_store: dict) -> None:
    """Re-running migration with no legacy change produces a byte-identical file."""
    wiki_root = legacy_store["wiki_root"]
    report1 = migrate_legacy_queues(wiki_root)
    text1 = report1.path.read_text(encoding="utf-8")

    report2 = migrate_legacy_queues(wiki_root)
    text2 = report2.path.read_text(encoding="utf-8")

    assert report1.ids == report2.ids
    assert report1.by_id == report2.by_id
    assert text1 == text2


def test_migration_reflects_a_later_answer_on_resync(legacy_store: dict) -> None:
    """A later answer on the legacy store is picked up by re-running migration.

    Confirms the mirror is a real re-syncable materialization, not a
    one-shot snapshot that goes stale the moment an answer lands.
    """
    wiki_root = legacy_store["wiki_root"]
    migrate_legacy_queues(wiki_root)

    questions_path = wiki_root / "_pending_questions.md"
    result = resolve_by_id(
        questions_path,
        legacy_store["question_pending_id"],
        "resolved later",
        answered_at="2026-02-01T00:00:00+00:00",
    )
    assert result["ok"]

    report = migrate_legacy_queues(wiki_root)
    assert report.by_id[legacy_store["question_pending_id"]] == "answered"
    # Everything else untouched.
    assert report.by_id[legacy_store["merge_pii_1_id"]] == "pending"
    assert report.by_id[legacy_store["merge_pii_2_id"]] == "pending"


def test_migrated_record_shape_is_json_line_per_item(legacy_store: dict) -> None:
    wiki_root = legacy_store["wiki_root"]
    report = migrate_legacy_queues(wiki_root)
    lines = report.path.read_text(encoding="utf-8").splitlines()
    assert lines  # non-empty
    for line in lines:
        rec = json.loads(line)
        assert {"id", "type", "disposition", "item"} <= rec.keys()
