# SPDX-License-Identifier: Apache-2.0
"""Tests for re-resolving OPEN, PROPOSAL-LESS pending questions (issue #188).

Mirrors the live finding: a question first escalated WITHOUT a proposal
(resolver budget exhausted or offline that run) stays raw ``[ ]`` forever
because ``tier4_escalate``'s open-pair dedup merges re-detections into the
existing block instead of re-running the resolver. ``reresolve_open_questions``
adds the heal pass: re-run the resolver on proposal-less open blocks subject to
the same per-run budget cap.

The resolver client is a :class:`unittest.mock.MagicMock` mirroring the
Anthropic SDK shape — no network calls (same pattern as test_resolutions.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.answers import parse_pending_questions
from athenaeum.models import EscalationItem, TokenUsage
from athenaeum.tiers import reresolve_open_questions, tier4_escalate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _write_member(knowledge_root: Path, scope: str, filename: str, body: str) -> str:
    """Create an auto-memory member file; return its ``<scope>/<name>`` ref."""
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: " + filename[:-3] + "\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return f"{scope}/{filename}"


def _escalate_proposalless(
    knowledge_root: Path,
    *,
    entity: str = "Tristan",
    passage_a: str = "Tristan is German.",
    passage_b: str = "Tristan is NOT German.",
) -> Path:
    """Write a single proposal-less open ``[ ]`` block + its member files.

    Returns the pending-questions path. The block carries ``Passage 1/2`` and
    a ``Members involved:`` line referencing the two created member files —
    exactly what the heal pass reconstructs the resolver inputs from.
    """
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    ref_a = _write_member(knowledge_root, "scope-x", "feedback_a.md", passage_a)
    ref_b = _write_member(knowledge_root, "scope-x", "feedback_b.md", passage_b)
    description = (
        "Detector says these conflict.\n"
        f"Passage 1: {passage_a}\n"
        f"Passage 2: {passage_b}\n"
        f"Members involved: {ref_a}, {ref_b}"
    )
    pending = wiki / "_pending_questions.md"
    # config=None → no auto-apply, no proposal block (the degraded escalation
    # the heal pass targets).
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-tristan.md",
                entity_name=entity,
                conflict_type="factual",
                description=description,
            )
        ],
        pending,
    )
    return pending


def _payload(action: str, *, winner: str = "a", confidence: float = 0.92) -> str:
    return (
        f'{{"recommended_winner": "{winner}", "action": "{action}", '
        f'"confidence": {confidence}, '
        '"rationale": "test verdict rationale.", '
        '"source_precedence_used": ["a:user > b:unsourced"]}'
    )


def _is_proposalless_open(pending: Path) -> bool:
    parsed = parse_pending_questions(pending)
    return any(
        (not pq.answered) and "**Proposed resolution**:" not in pq.raw_block
        for pq in parsed
    )


# ---------------------------------------------------------------------------
# not_a_conflict → drop + archive
# ---------------------------------------------------------------------------


def test_not_a_conflict_drops_and_archives(tmp_path: Path) -> None:
    """A re-resolve that returns not_a_conflict drops the question.

    The primary file loses the block; the archive gains it with an
    auto-dropped note (audit trail preserved — never silently deleted).
    """
    pending = _escalate_proposalless(tmp_path)
    assert len(parse_pending_questions(pending)) == 1

    client = _fake_client(_payload("not_a_conflict", winner="neither", confidence=0.9))
    count = reresolve_open_questions(pending, client=client, config={})

    assert count == 1
    assert parse_pending_questions(pending) == []  # dropped from primary
    archive = tmp_path / "wiki" / "_pending_questions_archive.md"
    assert archive.exists()
    archive_text = archive.read_text(encoding="utf-8")
    assert "Tristan" in archive_text
    assert "Auto-dropped" in archive_text


# ---------------------------------------------------------------------------
# real verdict → annotate in place, stay open
# ---------------------------------------------------------------------------


def test_real_verdict_annotates_and_stays_open(tmp_path: Path) -> None:
    """A real verdict (keep_a below auto-apply gate) annotates the block.

    The block gains the ``**Proposed resolution**:`` block and stays open
    (still ``[ ]``) for human review — confidence 0.92 < the 0.90… wait,
    0.92 >= 0.90 would auto-apply; use a confidence below the keep_a gate so
    it stays open and only annotates.
    """
    pending = _escalate_proposalless(tmp_path)
    # keep_a auto-apply gate is 0.90; 0.80 → annotate-only, stays open.
    client = _fake_client(_payload("keep_a", winner="a", confidence=0.80))
    count = reresolve_open_questions(pending, client=client, config={})

    assert count == 1
    parsed = parse_pending_questions(pending)
    assert len(parsed) == 1
    block = parsed[0].raw_block
    assert "**Proposed resolution**: keep_a" in block
    assert parsed[0].answered is False  # still open


# ---------------------------------------------------------------------------
# offline (client=None) → no mutation
# ---------------------------------------------------------------------------


def test_offline_leaves_blocks_untouched(tmp_path: Path) -> None:
    """``client=None`` leaves every proposal-less block exactly as-is."""
    pending = _escalate_proposalless(tmp_path)
    before = pending.read_text(encoding="utf-8")

    count = reresolve_open_questions(pending, client=None, config={})

    assert count == 0
    assert pending.read_text(encoding="utf-8") == before
    assert _is_proposalless_open(pending)


# ---------------------------------------------------------------------------
# idempotency — blocks WITH a proposal are never re-resolved
# ---------------------------------------------------------------------------


def test_block_with_proposal_never_reresolved(tmp_path: Path) -> None:
    """A block that already carries a proposal is skipped (no resolver call)."""
    pending = _escalate_proposalless(tmp_path)
    # First pass: annotate it (keep_a, below gate → stays open + annotated).
    client1 = _fake_client(_payload("keep_a", confidence=0.80))
    reresolve_open_questions(pending, client=client1, config={})
    assert "**Proposed resolution**:" in pending.read_text(encoding="utf-8")

    # Second pass: a fresh client that would DROP it if called. It must NOT be
    # called — the block already has a proposal.
    client2 = _fake_client(_payload("not_a_conflict", winner="neither", confidence=0.9))
    count = reresolve_open_questions(pending, client=client2, config={})

    assert count == 0
    client2.messages.create.assert_not_called()
    assert len(parse_pending_questions(pending)) == 1  # not dropped


# ---------------------------------------------------------------------------
# budget — surplus proposal-less blocks left untouched, partial progress
# ---------------------------------------------------------------------------


def test_budget_caps_reresolution_partial_progress(tmp_path: Path) -> None:
    """With resolve_max_per_run=1 and two proposal-less blocks, only one is
    re-resolved; the surplus is left open (re-resolvable next run), no crash."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    # Two distinct proposal-less blocks (distinct member pairs).
    ref_a = _write_member(tmp_path, "scope-x", "feedback_a.md", "claim A1")
    ref_b = _write_member(tmp_path, "scope-x", "feedback_b.md", "claim A2")
    ref_c = _write_member(tmp_path, "scope-x", "feedback_c.md", "claim B1")
    ref_d = _write_member(tmp_path, "scope-x", "feedback_d.md", "claim B2")
    pending = wiki / "_pending_questions.md"
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-1.md",
                entity_name="One",
                conflict_type="factual",
                description=(
                    "r1\nPassage 1: claim A1\nPassage 2: claim A2\n"
                    f"Members involved: {ref_a}, {ref_b}"
                ),
            ),
            EscalationItem(
                raw_ref="wiki/auto-2.md",
                entity_name="Two",
                conflict_type="factual",
                description=(
                    "r2\nPassage 1: claim B1\nPassage 2: claim B2\n"
                    f"Members involved: {ref_c}, {ref_d}"
                ),
            ),
        ],
        pending,
    )
    assert len(parse_pending_questions(pending)) == 2

    client = _fake_client(_payload("not_a_conflict", winner="neither", confidence=0.9))
    config = {"contradiction": {"resolve_max_per_run": 1}}
    count = reresolve_open_questions(pending, client=client, config=config)

    # Exactly one resolver call consumed the budget; one block healed
    # (dropped), one block left open and proposal-less.
    assert count == 1
    assert client.messages.create.call_count == 1
    parsed = parse_pending_questions(pending)
    assert len(parsed) == 1
    assert "**Proposed resolution**:" not in parsed[0].raw_block


# ---------------------------------------------------------------------------
# non-reconstructable block (missing members) → left open, not dropped
# ---------------------------------------------------------------------------


def test_unreconstructable_block_left_open(tmp_path: Path) -> None:
    """A block whose members can't be resolved is SKIPPED (left open)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    pending = wiki / "_pending_questions.md"
    # No member files written, and the description references nonexistent ones.
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-x.md",
                entity_name="Ghost",
                conflict_type="factual",
                description=(
                    "r\nPassage 1: p1\nPassage 2: p2\n"
                    "Members involved: scope-x/feedback_missing_a.md, "
                    "scope-x/feedback_missing_b.md"
                ),
            )
        ],
        pending,
    )
    before = pending.read_text(encoding="utf-8")
    client = _fake_client(_payload("not_a_conflict", winner="neither", confidence=0.9))
    count = reresolve_open_questions(pending, client=client, config={})

    assert count == 0
    client.messages.create.assert_not_called()
    assert pending.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# run-level budget accounting (issue #220) — resolver calls hit TokenUsage
# ---------------------------------------------------------------------------


def test_reresolve_counts_resolver_calls_against_usage(tmp_path: Path) -> None:
    """N resolver calls in the heal pass increment usage.api_calls by N.

    Symmetric to test_merge_counts_detector_and_resolver_calls (issue #220):
    the threaded run-level TokenUsage must see every propose_resolution call
    made by reresolve_open_questions so the run budget can trip on it.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    ref_a = _write_member(tmp_path, "scope-x", "feedback_a.md", "claim A1")
    ref_b = _write_member(tmp_path, "scope-x", "feedback_b.md", "claim A2")
    ref_c = _write_member(tmp_path, "scope-x", "feedback_c.md", "claim B1")
    ref_d = _write_member(tmp_path, "scope-x", "feedback_d.md", "claim B2")
    pending = wiki / "_pending_questions.md"
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-1.md",
                entity_name="One",
                conflict_type="factual",
                description=(
                    "r1\nPassage 1: claim A1\nPassage 2: claim A2\n"
                    f"Members involved: {ref_a}, {ref_b}"
                ),
            ),
            EscalationItem(
                raw_ref="wiki/auto-2.md",
                entity_name="Two",
                conflict_type="factual",
                description=(
                    "r2\nPassage 1: claim B1\nPassage 2: claim B2\n"
                    f"Members involved: {ref_c}, {ref_d}"
                ),
            ),
        ],
        pending,
    )

    # keep_a at 0.80 (below gate) → annotate-only; both blocks get a call.
    client = _fake_client(_payload("keep_a", confidence=0.80))
    # #239: the resolver responses must also feed token + cache counters
    # into the threaded usage so the run summary's cache line moves.
    client.messages.create.return_value.usage = MagicMock(
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=2400,
    )
    usage = TokenUsage()
    count = reresolve_open_questions(pending, client=client, config={}, usage=usage)

    assert count == 2
    assert client.messages.create.call_count == 2
    assert usage.api_calls == 2
    assert usage.input_tokens == 200
    assert usage.output_tokens == 20
    assert usage.cache_read_input_tokens == 4800


def test_reresolve_offline_counts_nothing(tmp_path: Path) -> None:
    """client=None makes no resolver calls and leaves usage.api_calls at 0."""
    pending = _escalate_proposalless(tmp_path)
    usage = TokenUsage()

    count = reresolve_open_questions(pending, client=None, config={}, usage=usage)

    assert count == 0
    assert usage.api_calls == 0
