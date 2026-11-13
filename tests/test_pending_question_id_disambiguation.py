"""Tests for the pending-question id disambiguator (issue athenaeum#2517).

Two different pending questions could share one decision id, because the
id hash (`_make_id`) is over header + question text only — the description
is deliberately excluded (see `_make_id`'s docstring) so a description
clarification edit never invalidates a cached id. A detector-raised item
with a generic question line and the same header therefore collided
whenever only the description differed.

The fix: a creation-time disambiguator (`**Id salt**:`, a short hash of
the block's own description/context, captured ONCE and persisted, never
recomputed) is folded into the hash by `mint_unique_block_id` /
`_make_id`. This file covers:

- AC1: two blocks differing only in description get different ids, and a
  block carrying a salt stays stable across a LATER description edit.
- AC1 back-compat: a block with no salt line hashes exactly as before.
- AC2: the writer disambiguates (never refuses) on an actual id collision.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from athenaeum.answers import (
    _make_id,
    mint_unique_block_id,
    parse_pending_questions,
    raise_pending_question,
)
from athenaeum.models import EscalationItem
from athenaeum.tiers import tier4_escalate


@pytest.fixture
def pending_path(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki / "_pending_questions.md"


# ---------------------------------------------------------------------------
# `_make_id` back-compat
# ---------------------------------------------------------------------------


def test_make_id_with_no_salt_matches_pre_2517_formula() -> None:
    """``salt=""`` (the default) must reproduce the EXACT old hash.

    This is what lets an id already handed out (cached by an MCP consumer,
    or embedded in an in-flight answer) keep resolving via
    `resolve_by_id` for every block written before this change.
    """
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Is Acme still Series A?"
    old_formula = hashlib.sha1(f"{header}\n{question}".encode("utf-8")).hexdigest()[
        :12
    ]
    assert _make_id(header, question) == old_formula
    assert _make_id(header, question, "") == old_formula


def test_make_id_salt_changes_the_hash() -> None:
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Is Acme still Series A?"
    assert _make_id(header, question, "saltA") != _make_id(header, question, "saltB")
    assert _make_id(header, question, "saltA") != _make_id(header, question)


# ---------------------------------------------------------------------------
# A block without an `**Id salt**:` line (pre-athenaeum#2517, or hand-written)
# ---------------------------------------------------------------------------


def test_parsed_block_with_no_salt_line_uses_old_formula(pending_path: Path) -> None:
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Is Acme still Series A?"
    pending_path.write_text(
        "# Pending Questions\n\n"
        f"{header}\n"
        f"- [ ] {question}\n"
        "**Conflict type**: principled\n"
        "**Description**: Wiki says Series A; raw implies Series B.\n"
    )
    pq = parse_pending_questions(pending_path)[0]
    assert pq.id_salt == ""
    assert pq.id == _make_id(header, question)


# ---------------------------------------------------------------------------
# AC1: two blocks differing only in description get different ids
# ---------------------------------------------------------------------------


def test_tier4_escalate_gives_different_ids_to_same_header_same_question(
    pending_path: Path,
) -> None:
    """The exact failure mode athenaeum#2517 reports.

    Two detector items share an entity, raw_ref, and conflict_type (so the
    header is identical) and a description whose FIRST line — what
    `_question_from_description` turns into the checkbox question — is
    identical too. Neither has a `Members involved:`/`Passage N:` pair key
    (`_pair_key_from_description` returns None for both), so the athenaeum#157
    dedup path does not collapse them — tier4_escalate appends two
    separate blocks, as it always did. Before athenaeum#2517 those two blocks
    hashed to the SAME id; now they must not.
    """
    item_a = EscalationItem(
        raw_ref="wiki/x.md",
        entity_name="Acme Corp",
        conflict_type="field_correction",
        description="Field correction needed.\nStage: should be Series B, not Series A.",
    )
    item_b = EscalationItem(
        raw_ref="wiki/x.md",
        entity_name="Acme Corp",
        conflict_type="field_correction",
        description="Field correction needed.\nAmount: should be $5M, not $4M.",
    )
    tier4_escalate([item_a], pending_path)
    tier4_escalate([item_b], pending_path)

    parsed = parse_pending_questions(pending_path)
    assert len(parsed) == 2
    # Same header-defining fields and same (generic) question line.
    assert parsed[0].question == parsed[1].question == "Field correction needed."
    assert parsed[0].entity == parsed[1].entity == "Acme Corp"
    # ... but different ids, because their descriptions differ.
    assert parsed[0].id != parsed[1].id
    assert parsed[0].id_salt != ""
    assert parsed[1].id_salt != ""
    assert parsed[0].id_salt != parsed[1].id_salt


def test_raise_pending_question_gives_different_ids_to_same_header_same_question(
    pending_path: Path,
) -> None:
    """Same collision, via the agent-raised writer instead of the detector."""
    r1 = raise_pending_question(pending_path, "Confirm?", "First context.")
    r2 = raise_pending_question(pending_path, "Confirm?", "Second, different context.")
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r1["decision_id"] != r2["decision_id"]


# ---------------------------------------------------------------------------
# AC1: id stability across a LATER description edit, for a block carrying
# a salt (the harder half — the salt must be a frozen, persisted value,
# not a live re-hash of the description).
# ---------------------------------------------------------------------------


def test_id_stable_across_description_edit_when_salt_present(
    pending_path: Path,
) -> None:
    result = raise_pending_question(pending_path, "Confirm?", "Original context.")
    assert result["ok"] is True
    id_before = result["decision_id"]

    text = pending_path.read_text(encoding="utf-8")
    assert "Original context." in text
    text = text.replace(
        "Original context.", "A totally rewritten, much longer context paragraph."
    )
    pending_path.write_text(text, encoding="utf-8")

    id_after = parse_pending_questions(pending_path)[0].id
    assert id_after == id_before


# ---------------------------------------------------------------------------
# AC2: disambiguate (never refuse) on an actual collision
# ---------------------------------------------------------------------------


def test_mint_unique_block_id_is_deterministic_with_no_collision() -> None:
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Confirm?"
    salt1, id1 = mint_unique_block_id(header, question, "same content", set())
    salt2, id2 = mint_unique_block_id(header, question, "same content", set())
    assert salt1 == salt2
    assert id1 == id2


def test_mint_unique_block_id_disambiguates_on_collision() -> None:
    """When the natural (content-hash) candidate already exists, retry.

    AC2 — "the writer refuses or disambiguates on write instead of
    producing two blocks with one id" — this project chooses disambiguate:
    refusing to write a pending question would silently drop it.
    """
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Confirm?"
    content = "same content"
    # The id this exact (header, question, content) would naturally mint.
    _natural_salt, natural_id = mint_unique_block_id(header, question, content, set())

    salt, new_id = mint_unique_block_id(
        header, question, content, {natural_id}
    )
    assert new_id != natural_id
    assert new_id not in {natural_id}
    assert salt != _natural_salt


def test_mint_unique_block_id_terminates_even_with_many_prior_collisions() -> None:
    """A pathological run of prior collisions still yields a fresh id."""
    header = '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)'
    question = "Confirm?"
    content = "same content"

    existing: set[str] = set()
    salt, new_id = mint_unique_block_id(header, question, content, existing)
    existing.add(new_id)
    # Force three more collisions by pre-seeding the next few candidates.
    for attempt in range(2, 5):
        probe_salt = f"{salt.split('#')[0]}#{attempt}"
        existing.add(_make_id(header, question, probe_salt))

    salt2, new_id2 = mint_unique_block_id(header, question, content, existing)
    assert new_id2 not in existing
    assert salt2 not in ("",)


def test_tier4_escalate_disambiguates_true_duplicate_blocks(
    pending_path: Path,
) -> None:
    """Two items identical in EVERY hashed field still land as two blocks
    with two different ids -- the collision-retry path, exercised through
    the real writer rather than `mint_unique_block_id` directly.
    """
    item = EscalationItem(
        raw_ref="wiki/x.md",
        entity_name="Acme Corp",
        conflict_type="field_correction",
        description="Field correction needed.\nSame description, verbatim.",
    )
    # Two genuinely separate detector events for the identical pair would
    # normally collapse via the athenaeum#157 dedup path when they share a
    # members/passage key -- this description has neither, so both are
    # appended as distinct blocks (matching today's non-dedup behaviour).
    tier4_escalate([item], pending_path)
    tier4_escalate([item], pending_path)

    parsed = parse_pending_questions(pending_path)
    assert len(parsed) == 2
    assert parsed[0].id != parsed[1].id
