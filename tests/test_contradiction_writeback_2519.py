# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2519 — contradiction answers cannot write back
once the raw source has been consumed.

Operator ruling: when a contradiction question is raised, it stores its
own copy of the conflicting claims and the path of the compiled page they
landed in. The answer is applied to that compiled page from the stored
copy. Raw notes keep being deleted after processing on the normal
schedule; nothing pins them.

Covers:
- AC1: a newly raised contradiction question stores a copy of the
  conflicting claims AND the path of the compiled page they landed in
  (``TestAttachContradictionWritebackCopy``, ``TestRaiseStoresCopy``).
- AC2: answering it applies the write-back to that compiled page from the
  stored copy, even after its raw source file has been deleted
  (``TestWritebackSurvivesRawDeletion``).
- AC3: raw intake consumption is unchanged — a raw file with an open
  question against it is still deleted on the normal schedule
  (``TestRawConsumptionUnchanged``).
- AC4: a question raised before this change, with no stored copy, keeps
  today's behaviour (``TestBackCompatNoStoredCopy``).

All fixtures are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.answers import (
    PendingQuestion,
    _block_source_refs,
    _writeback_source,
    parse_pending_questions,
)
from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityAction, EntityIndex, RawFile
from athenaeum.tiers import (
    MAX_CONFLICTING_CLAIMS_CHARS,
    attach_contradiction_writeback_copy,
    tier3_merge,
    tier4_escalate,
)

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = response
    return client


def _edits_json(edits: list[dict]) -> str:
    return json.dumps({"edits": edits})


def _write_page(wiki: Path, filename: str, *, uid: str, name: str, body: str) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    text = f"---\nuid: {uid}\nname: {name}\n---\n\n{body}"
    path = wiki / filename
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1 — the raise site stamps the durable copy
# ---------------------------------------------------------------------------


class TestAttachContradictionWritebackCopy:
    def test_stamps_relative_compiled_page_and_both_claim_sides(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = _write_page(
            wiki, "acme-corp.md", uid="a1b2c3d4", name="Acme Corp", body="Fintech.\n"
        )
        from athenaeum.models import EscalationItem

        esc = EscalationItem(
            raw_ref="sessions/20260601T120000Z-aabb.md",
            entity_name="Acme Corp",
            conflict_type="principled",
            description="ESCALATE: fintech vs pivot.",
        )
        attach_contradiction_writeback_copy(
            esc,
            existing_path=page,
            wiki_root=wiki,
            observations="Acme pivoted away from fintech.",
            existing_body="Fintech.\n",
        )
        assert esc.compiled_page == "acme-corp.md"
        assert "Acme pivoted away from fintech." in esc.conflicting_claims
        assert "Fintech." in esc.conflicting_claims
        # The raw ref itself is named, so a reader can still see where the
        # new claim came from even after that file is gone.
        assert "sessions/20260601T120000Z-aabb.md" in esc.conflicting_claims

    def test_bounds_each_side_of_the_copy(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = _write_page(wiki, "p.md", uid="u1", name="P", body="x\n")
        from athenaeum.models import EscalationItem

        esc = EscalationItem(
            raw_ref="sessions/s.md",
            entity_name="P",
            conflict_type="principled",
            description="ESCALATE: conflict.",
        )
        huge = "x" * (MAX_CONFLICTING_CLAIMS_CHARS * 3)
        attach_contradiction_writeback_copy(
            esc,
            existing_path=page,
            wiki_root=wiki,
            observations=huge,
            existing_body=huge,
        )
        # Each side capped independently — total stays well under 2x the cap
        # plus a small fixed overhead for the framing text.
        assert len(esc.conflicting_claims) < 2 * MAX_CONFLICTING_CLAIMS_CHARS + 200


class TestRaiseStoresCopyEndToEnd:
    def test_tier3_merge_escalation_round_trips_through_pending_questions(
        self, tmp_path: Path
    ) -> None:
        """The exact shape athenaeum.librarian / athenaeum.batch build: a
        tier3_merge ESCALATE response produces an EscalationItem, the raise
        site stamps it, tier4_escalate renders it, and the canonical parser
        recovers both new fields intact."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        existing_body = "# Acme Corp\n\nFintech startup.\n"
        page = _write_page(
            wiki, "acme-corp.md", uid="a1b2c3d4", name="Acme Corp", body=existing_body
        )
        action = EntityAction(
            kind="update",
            name="Acme Corp",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="Acme pivoted away from fintech.",
        )
        response = (
            "ESCALATE: Existing page says fintech, new observation says "
            "pivot away. Strategic direction conflict."
        )
        client = _mock_client(response)
        raw_ref = "sessions/20260601T120000Z-aabb.md"
        _body, esc = tier3_merge(action, existing_body, raw_ref, client)
        assert esc is not None

        attach_contradiction_writeback_copy(
            esc,
            existing_path=page,
            wiki_root=wiki,
            observations=action.observations,
            existing_body=existing_body,
        )

        pending = wiki / "_pending_questions.md"
        tier4_escalate([esc], pending)

        text = pending.read_text(encoding="utf-8")
        assert "**Compiled page**: acme-corp.md" in text
        assert "**Conflicting claims**:" in text

        pqs = parse_pending_questions(pending)
        assert len(pqs) == 1
        pq = pqs[0]
        assert pq.compiled_page == "acme-corp.md"
        assert "Acme pivoted away from fintech." in pq.conflicting_claims
        assert "Fintech startup." in pq.conflicting_claims
        # The raw ref is still the header's source — unchanged rendering.
        assert pq.source == raw_ref


# ---------------------------------------------------------------------------
# AC2 — write-back survives raw-source deletion
# ---------------------------------------------------------------------------


class TestWritebackSurvivesRawDeletion:
    def _answered_pq(
        self, *, compiled_page: str, conflicting_claims: str, answer: str
    ) -> PendingQuestion:
        return PendingQuestion(
            id="testid001234",
            entity="Acme Corp",
            source="sessions/20260601T120000Z-aabb.md",
            question="Does the new claim supersede the page?",
            conflict_type="principled",
            description="ESCALATE: fintech vs pivot.",
            created_at="2026-06-01",
            answered=True,
            answer_lines=[answer],
            raw_block="",
            compiled_page=compiled_page,
            conflicting_claims=conflicting_claims,
        )

    def test_block_source_refs_prefers_compiled_page(self) -> None:
        pq = self._answered_pq(
            compiled_page="acme-corp.md",
            conflicting_claims="New claim vs old claim",
            answer="Pivoted, update the page.",
        )
        assert _block_source_refs(pq) == ["acme-corp.md"]

    def test_annotation_fallback_lands_on_compiled_page_raw_gone(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw"
        wiki.mkdir()
        raw.mkdir()
        page = _write_page(
            wiki, "acme-corp.md", uid="a1b2c3d4", name="Acme Corp", body="Fintech.\n"
        )
        # The raw source this question was raised against was deleted by
        # the librarian's normal intake-consumption schedule — it never
        # exists under `raw` at all.
        assert not (raw / "sessions" / "20260601T120000Z-aabb.md").exists()

        pq = self._answered_pq(
            compiled_page="acme-corp.md",
            conflicting_claims=(
                "New claim (raw source sessions/20260601T120000Z-aabb.md):\n"
                "Acme pivoted away from fintech.\n\n"
                "Existing compiled page body:\nFintech.\n"
            ),
            answer="Confirmed — the pivot happened, annotate the page.",
        )
        roots = [wiki, raw]
        edited = _writeback_source(pq, roots, client=None)

        assert edited == 1
        result = page.read_text(encoding="utf-8")
        assert "Ratified annotation" in result
        assert "Confirmed" in result
        # The page was never deleted/replaced — original body preserved.
        assert "Fintech." in result

    def test_freetext_proposer_edits_compiled_page_raw_gone(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw"
        wiki.mkdir()
        raw.mkdir()
        page = _write_page(
            wiki,
            "acme-corp.md",
            uid="a1b2c3d4",
            name="Acme Corp",
            body="Fintech startup.\n",
        )
        new_body = "Pivoted away from fintech.\n"
        payload = _edits_json(
            [{"path": str(page), "changed": True, "new_body": new_body}]
        )
        client = _mock_client(payload)

        pq = self._answered_pq(
            compiled_page="acme-corp.md",
            conflicting_claims=(
                "New claim: Acme pivoted away from fintech.\n\n"
                "Existing compiled page body: Fintech startup.\n"
            ),
            answer="The pivot is confirmed — remove the fintech framing.",
        )
        roots = [wiki, raw]
        edited = _writeback_source(pq, roots, client=client)

        assert edited == 1
        result = page.read_text(encoding="utf-8")
        assert "Pivoted away from fintech." in result
        assert "Fintech startup." not in result
        # Frontmatter preserved, not clobbered.
        assert "uid: a1b2c3d4" in result


# ---------------------------------------------------------------------------
# AC3 — raw intake consumption is unchanged
# ---------------------------------------------------------------------------


class TestRawConsumptionUnchanged:
    def test_principled_escalation_does_not_retain_the_raw_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw file whose action produces a 'principled' contradiction
        escalation carries no `retain_raw` signal on the returned
        ProcessingResult — the ONLY gate athenaeum.librarian's entity-tier
        loop checks before unconditionally calling `raw.path.unlink()`
        (see librarian.py's "elif not ctx.dry_run: raw.path.unlink()"
        branch). This proves athenaeum#2519's fix (storing a durable
        write-back copy) does not change raw-consumption timing: the raw
        file is still deleted on the normal schedule even with an open
        contradiction question against it."""
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        raw_dir = knowledge / "raw" / "sessions"
        raw_dir.mkdir(parents=True)
        wiki.mkdir(parents=True)

        existing_body = "Fintech startup.\n"
        _write_page(wiki, "acme-corp.md", uid="a1b2c3d4", name="Acme Corp", body=existing_body)

        raw_path = raw_dir / "20260601T120000Z-aabb.md"
        raw_path.write_text("Acme pivoted away from fintech.\n", encoding="utf-8")
        raw = RawFile(path=raw_path, source="sessions", timestamp="", uuid8="")

        def _fake_tier2_classify(*_a: object, **_k: object) -> list[ClassifiedEntity]:
            return [
                ClassifiedEntity(
                    name="Acme Corp",
                    entity_type="company",
                    tags=[],
                    access="internal",
                    is_new=False,
                    existing_uid="a1b2c3d4",
                    observations="Acme pivoted away from fintech.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — must never be called for real"
        )
        write_client = _mock_client(
            "ESCALATE: Existing page says fintech, new observation says pivot "
            "away. Strategic direction conflict."
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            write_client=write_client,
        )

        assert len(result.escalated) == 1
        esc = result.escalated[0]
        assert esc.conflict_type == "principled"
        # The fix landed: this contradiction stored its durable copy.
        assert esc.compiled_page == "acme-corp.md"
        assert esc.conflicting_claims

        # The gate the librarian's unlink branch checks — unaffected.
        assert getattr(result, "retain_raw", False) is False

        # Replay that exact gate here to prove the raw file would be
        # deleted on the normal schedule, question still open or not.
        if not getattr(result, "retain_raw", False):
            raw_path.unlink()
        assert not raw_path.exists()


# ---------------------------------------------------------------------------
# AC4 — back-compat: a pre-athenaeum#2519 block has no stored copy
# ---------------------------------------------------------------------------


class TestBackCompatNoStoredCopy:
    def test_block_source_refs_falls_back_to_source_when_no_compiled_page(
        self,
    ) -> None:
        pq = PendingQuestion(
            id="legacyid0001",
            entity="Acme Corp",
            source="sessions/legacy.md",
            question="Does the new claim supersede the page?",
            conflict_type="principled",
            description="ESCALATE: fintech vs pivot.",
            created_at="2026-01-01",
            answered=True,
            answer_lines=["Confirmed."],
            raw_block="",
            # compiled_page / conflicting_claims default to "" — exactly a
            # block raised before athenaeum#2519.
        )
        assert pq.compiled_page == ""
        assert pq.conflicting_claims == ""
        assert _block_source_refs(pq) == ["sessions/legacy.md"]

    def test_writeback_source_unresolvable_raw_still_returns_zero_not_crash(
        self, tmp_path: Path
    ) -> None:
        """Pre-athenaeum#2519 behaviour: with no stored compiled-page copy,
        a deleted raw source simply fails to resolve and the write-back is
        a no-op (today's "held" outcome at the ingest_answers layer) —
        never a crash, never a silent edit of the wrong file."""
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw"
        wiki.mkdir()
        raw.mkdir()

        pq = PendingQuestion(
            id="legacyid0002",
            entity="Acme Corp",
            source="sessions/legacy-20260101.md",
            question="Does the new claim supersede the page?",
            conflict_type="principled",
            description="ESCALATE: fintech vs pivot.",
            created_at="2026-01-01",
            answered=True,
            answer_lines=["Confirmed — update the page."],
            raw_block="",
        )
        assert not (raw / "sessions" / "legacy-20260101.md").exists()
        edited = _writeback_source(pq, [wiki, raw], client=None)
        assert edited == 0
