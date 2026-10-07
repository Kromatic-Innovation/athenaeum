# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`athenaeum.decisions.migrate_legacy_queues` (issue athenaeum#1992).

Builds a fixture legacy store (pending merges + pending questions, each with
a resolved AND an unresolved record, plus two PII-hazard merge proposals
that are never approved, plus a calibration-ledger audit item of each
disposition) entirely under ``tmp_path`` — no live ``~/knowledge`` store is
read or written anywhere in this module.

The issue's own test AC: "a fixture legacy store (pending merges + pending
questions) migrates to the unified schema with zero item-count drift and
zero disposition drift, verified by id-set comparison, not by count alone."
Every assertion below compares SETS of ids (and a disposition dict keyed by
id), never bare counts. AC3 additionally names "audit items" as a third
migrated type and requires the unified store be AUTHORITATIVE (not merely a
second, unread presentation) — covered by the
``TestListPendingDecisionsIsAuthoritative`` class below, which asserts
``list_pending_decisions`` reads records back from the synced store rather
than re-deriving them independently.
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
from athenaeum.calibration import read_calibration_ledger, record_audit_review, sample_tier_decision
from athenaeum.decisions import (
    MigrationReport,
    list_pending_decisions,
    load_migrated_queue,
    migrate_legacy_queues,
)
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

    # Two calibration-sampled audit items (issue athenaeum#1992 AC3 names
    # "audit items" explicitly): one left pending (unreviewed), one
    # reviewed (confirmed). rate=1.0 forces deterministic sampling so the
    # fixture doesn't depend on the hash-based sampler picking these ids.
    audit_pending = sample_tier_decision(
        wiki_root,
        tier="T1",
        verdict="reject",
        proposal_id="audit-fixture-pending",
        reason="fixture reject",
        config={"librarian": {"audit_sample_rate_t1_rejects": 1.0}},
    )
    audit_reviewed = sample_tier_decision(
        wiki_root,
        tier="T2",
        verdict="approve",
        proposal_id="audit-fixture-reviewed",
        reason="fixture approve",
        config={"librarian": {"audit_sample_rate_t2_approvals": 1.0}},
        applied=True,
    )
    assert audit_pending is not None
    assert audit_reviewed is not None
    record_audit_review(
        wiki_root, audit_id=audit_reviewed["id"], human_verdict="approve"
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
        "audit_pending_id": audit_pending["id"],
        "audit_reviewed_id": audit_reviewed["id"],
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

    ledger = read_calibration_ledger(wiki_root)
    audit_ids = {str(r.get("id")) for r in ledger if r.get("kind") == "audit"}
    review_ids = {str(r.get("id")) for r in ledger if r.get("kind") == "review"}
    assert legacy_store["audit_pending_id"] in audit_ids
    assert legacy_store["audit_pending_id"] not in review_ids
    assert legacy_store["audit_reviewed_id"] in review_ids


def test_migration_zero_item_count_and_disposition_drift(legacy_store: dict) -> None:
    """The issue's own test AC: id-set comparison, not a bare count."""
    wiki_root = legacy_store["wiki_root"]

    # BEFORE snapshot, read directly off the legacy stores/ledger.
    before_merges = {pm.id: pm for pm in parse_pending_merges(wiki_root / "_pending_merges.md")}
    before_questions = {
        pq.id: pq for pq in parse_pending_questions(wiki_root / "_pending_questions.md")
    }
    before_ledger = read_calibration_ledger(wiki_root)
    before_audit_ids = {str(r.get("id")) for r in before_ledger if r.get("kind") == "audit"}
    before_ids = set(before_merges) | set(before_questions) | before_audit_ids

    report = migrate_legacy_queues(wiki_root)
    assert isinstance(report, MigrationReport)

    # Zero item-count drift, verified by id SET equality (not len(...) ==).
    assert report.ids == before_ids
    assert report.merge_count == len(before_merges)
    assert report.question_count == len(before_questions)
    assert report.audit_count == len(before_audit_ids)

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

    # Audit disposition drift, verified per id.
    assert report.by_id[legacy_store["audit_pending_id"]] == "pending"
    assert report.by_id[legacy_store["audit_reviewed_id"]] == "confirmed"

    # The persisted mirror round-trips to the same id set and dispositions.
    persisted = load_migrated_queue(wiki_root)
    persisted_by_id = {rec["id"]: rec["disposition"] for rec in persisted}
    assert set(persisted_by_id) == before_ids
    assert persisted_by_id == report.by_id


def test_migration_never_mutates_the_legacy_stores(legacy_store: dict) -> None:
    """Migration is read-only w.r.t. the legacy files/ledger — byte-identical before/after."""
    wiki_root = legacy_store["wiki_root"]
    merges_before = (wiki_root / "_pending_merges.md").read_text(encoding="utf-8")
    questions_before = (wiki_root / "_pending_questions.md").read_text(encoding="utf-8")
    ledger_path = wiki_root / "_calibration.jsonl"
    ledger_before = ledger_path.read_text(encoding="utf-8")

    migrate_legacy_queues(wiki_root)

    assert (wiki_root / "_pending_merges.md").read_text(encoding="utf-8") == merges_before
    assert (wiki_root / "_pending_questions.md").read_text(encoding="utf-8") == questions_before
    assert ledger_path.read_text(encoding="utf-8") == ledger_before


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


class TestListPendingDecisionsIsAuthoritative:
    """AC3: the unified store is what gets READ, not an unread second mirror.

    These tests prove the dependency directly — not just that the output
    happens to match, which could pass even if ``list_pending_decisions``
    still independently re-derived everything from the legacy files/ledger.
    """

    def test_sync_and_read_are_both_actually_called(
        self, legacy_store: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.decisions as decisions_mod

        wiki_root = legacy_store["wiki_root"]
        calls = {"migrate": 0, "load": 0}

        real_migrate = decisions_mod.migrate_legacy_queues
        real_load = decisions_mod.load_migrated_queue

        def _spy_migrate(*args, **kwargs):
            calls["migrate"] += 1
            return real_migrate(*args, **kwargs)

        def _spy_load(*args, **kwargs):
            calls["load"] += 1
            return real_load(*args, **kwargs)

        monkeypatch.setattr(decisions_mod, "migrate_legacy_queues", _spy_migrate)
        monkeypatch.setattr(decisions_mod, "load_migrated_queue", _spy_load)

        result = decisions_mod.list_pending_decisions(wiki_root)

        assert calls["migrate"] == 1
        assert calls["load"] == 1
        ids = {d["id"] for d in result}
        assert legacy_store["merge_pending_id"] in ids
        assert legacy_store["merge_pii_1_id"] in ids
        assert legacy_store["merge_pii_2_id"] in ids
        assert legacy_store["merge_approved_id"] not in ids  # resolved, excluded
        assert legacy_store["question_pending_id"] in ids
        assert legacy_store["question_answered_id"] not in ids  # answered, excluded
        assert legacy_store["audit_pending_id"] in ids
        assert legacy_store["audit_reviewed_id"] not in ids  # reviewed, excluded

    def test_output_comes_from_the_persisted_store_not_independent_rederivation(
        self, legacy_store: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fabricated record in the store, unrelated to any legacy file,
        must surface in the output — proof ``list_pending_decisions`` reads
        FROM ``load_migrated_queue``'s return value rather than recomputing
        independently from ``_pending_merges.md``/``_pending_questions.md``.
        """
        import athenaeum.decisions as decisions_mod

        wiki_root = legacy_store["wiki_root"]
        fabricated_item = {
            "type": "merge",
            "id": "fabricated-0000",
            "created_at": "2026-01-01",
            "summary": "fabricated — not in any legacy file",
            "confidence": 0.5,
            "payload": {
                "merge_target_name": "fabricated",
                "rationale": "test",
                "sources": [],
                "sources_omitted": 0,
            },
        }
        fabricated_record = {
            "id": "fabricated-0000",
            "type": "merge",
            "disposition": "pending",
            "sources": [],
            "item": fabricated_item,
        }

        def _fake_load(wiki_root_arg):
            return [fabricated_record]

        monkeypatch.setattr(decisions_mod, "load_migrated_queue", _fake_load)

        result = decisions_mod.list_pending_decisions(wiki_root)

        ids = {d["id"] for d in result}
        assert "fabricated-0000" in ids
        # And nothing else — proving the real legacy files are NOT
        # independently re-consulted once the store's own read is faked.
        assert ids == {"fabricated-0000"}

    def test_audit_items_are_owner_only(self, legacy_store: dict) -> None:

        wiki_root = legacy_store["wiki_root"]

        owner_ids = {d["id"] for d in list_pending_decisions(wiki_root)}
        assert legacy_store["audit_pending_id"] in owner_ids

        restricted_ids = {
            d["id"]
            for d in list_pending_decisions(wiki_root, caller_audience=set())
        }
        assert legacy_store["audit_pending_id"] not in restricted_ids

    def test_a_later_resolve_merge_disposition_change_is_reflected_next_call(
        self, legacy_store: dict
    ) -> None:
        """The sync-then-read path (not a cache) picks up a legacy-store
        mutation on the very next call — the staleness risk a cached
        mirror would have.
        """
        from athenaeum.pending_merges import resolve_merge

        wiki_root = legacy_store["wiki_root"]
        merges_path = wiki_root / "_pending_merges.md"

        before_ids = {d["id"] for d in list_pending_decisions(wiki_root)}
        assert legacy_store["merge_pending_id"] in before_ids

        result = resolve_merge(merges_path, legacy_store["merge_pending_id"], "reject")
        assert result["ok"]

        after_ids = {d["id"] for d in list_pending_decisions(wiki_root)}
        assert legacy_store["merge_pending_id"] not in after_ids
        # Everything else (notably the two PII-hazard proposals) unaffected.
        assert legacy_store["merge_pii_1_id"] in after_ids
        assert legacy_store["merge_pii_2_id"] in after_ids


class TestListPendingDecisionsFailSoft:
    """``list_pending_decisions`` is a READ api: a failure persisting the
    unified store must degrade PERSISTENCE, never the read (coordinator
    review on athenaeum#1992). Two independent proofs of the same
    contract — an OS-level read-only directory (the realistic failure
    mode) and a direct monkeypatch of ``atomic_write_text`` (the
    deterministic, environment-independent proof; the same pattern
    ``tests/test_decay_bucket_classify.py`` uses for its own
    ``atomic_write_text`` fail-soft coverage) — so the result doesn't
    depend on whether this sandbox happens to enforce directory
    permissions for the user running the suite.
    """

    def test_readonly_wiki_root_still_returns_a_correct_and_complete_list(
        self,
        legacy_store: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        wiki_root = legacy_store["wiki_root"]
        # A read-only SUBDIRECTORY derived from tmp_path (never a hand-rolled
        # absolute-home-style path literal, per public-safe-lint's
        # absolute-path gate) blocks atomic_write_text's mkstemp call inside
        # it while leaving every legacy file (already written before this
        # chmod) fully readable.
        original_mode = wiki_root.stat().st_mode
        wiki_root.chmod(0o555)
        try:
            with caplog.at_level(logging.WARNING, logger="athenaeum.decisions"):
                result = list_pending_decisions(wiki_root)
        finally:
            # Restore before the test ends so pytest's tmp_path teardown
            # (which needs to unlink entries inside this directory) succeeds.
            wiki_root.chmod(original_mode)

        assert any(
            "decisions" in rec.message and "persist" in rec.message
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        ), f"expected a persistence warning; got: {[r.message for r in caplog.records]}"

        ids = {d["id"] for d in result}
        assert legacy_store["merge_pending_id"] in ids
        assert legacy_store["merge_pii_1_id"] in ids
        assert legacy_store["merge_pii_2_id"] in ids
        assert legacy_store["merge_approved_id"] not in ids
        assert legacy_store["question_pending_id"] in ids
        assert legacy_store["question_answered_id"] not in ids
        assert legacy_store["audit_pending_id"] in ids
        assert legacy_store["audit_reviewed_id"] not in ids

    def test_write_failure_falls_back_to_the_same_in_memory_construction(
        self,
        legacy_store: dict,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Deterministic counterpart of the read-only-directory test above —
        proves ``report.records`` (the fallback) and what a successful
        write/read-back would have produced are value-identical, i.e. ONE
        construction, not two.
        """
        import logging

        import athenaeum.decisions as decisions_mod

        wiki_root = legacy_store["wiki_root"]

        # Ground truth: what the NORMAL (persisted) path returns.
        expected = list_pending_decisions(wiki_root)
        expected_by_id = {d["id"]: d for d in expected}

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated: read-only file system")

        monkeypatch.setattr(decisions_mod, "atomic_write_text", _boom)

        report = decisions_mod.migrate_legacy_queues(wiki_root)
        assert report.persisted is False
        assert report.records  # the in-memory construction is still there

        with caplog.at_level(logging.WARNING, logger="athenaeum.decisions"):
            result = decisions_mod.list_pending_decisions(wiki_root)

        assert any(
            rec.levelno == logging.WARNING and "persist" in rec.message
            for rec in caplog.records
        )

        result_by_id = {d["id"]: d for d in result}
        assert set(result_by_id) == set(expected_by_id)
        for item_id, expected_item in expected_by_id.items():
            assert result_by_id[item_id] == expected_item
