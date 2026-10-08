# SPDX-License-Identifier: Apache-2.0
"""Tests for resolutions-as-claims ingestion (issue athenaeum#2017 AC1/AC5)."""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.dimension_proposals import (
    DIMENSION_PROPOSALS_LEDGER_VERSION,
    PROPOSAL_KIND,
    mark_proposals_stale_for_decision,
    read_dimension_proposals_ledger,
    stale_proposal_ids,
)
from athenaeum.resolution_claims import (
    CLAIM_KIND,
    REVOCATION_KIND,
    claim_id,
    ingest_resolution_claim,
    is_revoked,
    list_active_resolution_claims,
    read_resolution_claims,
    revoke_resolution_claim,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    Basis,
    append_verdict,
    build_verdict_entry,
    lookup_pair,
)


def _wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


class TestIngestionAC1:
    def test_ingests_coordinate_answer_as_claim_with_provenance(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        record = ingest_resolution_claim(
            wiki_root,
            decision_id="dec-1",
            decision_type="coordinate",
            verdict='{"answers": []}',
            resolved_at="2026-10-01T00:00:00+00:00",
        )
        assert record is not None
        assert record["decision_id"] == "dec-1"
        assert record["decision_type"] == "coordinate"
        assert record["decided_by"] == "human"
        assert record["created_at"] == "2026-10-01T00:00:00+00:00"
        assert record["kind"] == CLAIM_KIND

        claims = read_resolution_claims(wiki_root)
        assert len(claims) == 1
        assert claims[0]["id"] == claim_id("dec-1", "coordinate")

    def test_ingests_dimension_proposal_ratification_with_dimension_name(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        record = ingest_resolution_claim(
            wiki_root,
            decision_id="dp-1",
            decision_type="dimension-proposal",
            verdict="approve",
            dimension_name="jurisdiction",
        )
        assert record is not None
        assert record["dimension_name"] == "jurisdiction"

    def test_ingests_audit_contradiction_call(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        record = ingest_resolution_claim(
            wiki_root,
            decision_id="audit-1",
            decision_type="audit",
            verdict="agree",
        )
        assert record is not None

    def test_ignores_decision_types_outside_the_named_enumeration(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        for decision_type in ("question", "merge", "proposed-rule"):
            record = ingest_resolution_claim(
                wiki_root,
                decision_id=f"x-{decision_type}",
                decision_type=decision_type,
                verdict="whatever",
            )
            assert record is None
        assert read_resolution_claims(wiki_root) == []

    def test_reingesting_the_same_decision_is_idempotent(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        ingest_resolution_claim(
            wiki_root, decision_id="dec-1", decision_type="audit", verdict="agree"
        )
        ingest_resolution_claim(
            wiki_root, decision_id="dec-1", decision_type="audit", verdict="agree"
        )
        claims = [c for c in read_resolution_claims(wiki_root) if c.get("kind") == CLAIM_KIND]
        assert len(claims) == 1


class TestRevocationAC5:
    def test_revocation_never_deletes_the_original_claim(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        ingest_resolution_claim(
            wiki_root, decision_id="dec-1", decision_type="audit", verdict="agree"
        )
        with RunLock(wiki_root.parent) as lock:
            revoke_resolution_claim(wiki_root, "dec-1", reason="human reversed the call", lock=lock)

        records = read_resolution_claims(wiki_root)
        kinds = [r["kind"] for r in records]
        assert CLAIM_KIND in kinds
        assert REVOCATION_KIND in kinds
        assert is_revoked("dec-1", records)
        assert list_active_resolution_claims(wiki_root) == []

    def test_revocation_stale_marks_verdicts_citing_the_decision_in_coord_origins(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        for n in ("alpha", "beta"):
            (wiki_root / f"{n}.md").write_text(
                f"---\nname: {n}\ntype: feedback\n---\nbody\n", encoding="utf-8"
            )
        ingest_resolution_claim(
            wiki_root,
            decision_id="dec-coord-1",
            decision_type="coordinate",
            verdict='{"answers": []}',
        )

        with RunLock(wiki_root.parent) as lock:
            entry = build_verdict_entry(
                "alpha",
                "beta",
                "distinct",
                basis=Basis(
                    content_hashes=["a", "b"],
                    coords=[None, None],
                    coord_origins={"subject": "dec-coord-1"},
                    registry_epoch=1,
                    tree_epoch=1,
                    authority_basis=None,
                    predicate_instrument=[None, None],
                    comparator_version="v1.gate2",
                ),
                decided_by="human-batch:dec-coord-1",
            )
            append_verdict(wiki_root, entry, lock=lock)

            pair = entry.pair
            assert not lookup_pair(wiki_root, pair).stale

            result = revoke_resolution_claim(
                wiki_root, "dec-coord-1", reason="coordinate answer challenged", lock=lock
            )

        assert result["marked_verdicts"] == 1
        refreshed = lookup_pair(wiki_root, pair)
        assert refreshed is not None
        assert refreshed.stale is True
        assert refreshed.stale_reason is not None

    def test_revocation_stale_marks_proposals_citing_the_decision_never_deletes(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        ledger_path = wiki_root / "_dimension_proposals.jsonl"
        ingest_resolution_claim(
            wiki_root,
            decision_id="dec-coord-2",
            decision_type="coordinate",
            verdict='{"answers": []}',
        )
        proposal_record = {
            "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": "proposal-1",
            "created_at": "2026-10-01T00:00:00+00:00",
            "name": "jurisdiction",
            "dimension_kind": "hierarchy",
            "null_semantics": "unknown",
            "separates": True,
            "applies_to": {},
            "applies_to_narrowed": False,
            "example_pairs": ["alpha+beta"],
            "backfill_plan": {"jurisdiction": "auto"},
            "ask_count": 0,
            "count": 5,
            "window_days": 30,
            "threshold": 2,
            "coord_origins": {"jurisdiction": "dec-coord-2"},
        }
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(proposal_record, sort_keys=True) + "\n")

        with RunLock(wiki_root.parent) as lock:
            revoke_resolution_claim(wiki_root, "dec-coord-2", reason="answer reversed", lock=lock)
        marked = mark_proposals_stale_for_decision(
            wiki_root, "dec-coord-2", reason="resolution claim revoked: answer reversed"
        )

        assert marked == ["proposal-1"]
        records = read_dimension_proposals_ledger(wiki_root)
        # Never deletes the original proposal record.
        assert any(r["kind"] == PROPOSAL_KIND and r["id"] == "proposal-1" for r in records)
        assert "proposal-1" in stale_proposal_ids(records)

        # Idempotent: calling mark_proposals_stale_for_decision again does
        # not re-mark an already-stale proposal.
        again = mark_proposals_stale_for_decision(wiki_root, "dec-coord-2", reason="second pass")
        assert again == []
