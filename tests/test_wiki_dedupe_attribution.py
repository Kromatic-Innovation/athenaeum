# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the wiki-dedup attribution ledger (issue athenaeum#1243).

Module-level shape, schema round-tripping, retention, and the AC3 reader.
The pass-level integration coverage (that
``propose_wiki_page_merges`` actually emits a row on every branch) lives in
``tests/test_wiki_dedupe.py`` alongside that pass's fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.clusters import (
    EMBEDDER_CHROMADB_DEFAULT,
    EMBEDDER_UNKNOWN,
)
from athenaeum.wiki_dedupe_attribution import (
    DEFAULT_ATTRIBUTION_FILENAME,
    NON_PROPOSAL_OUTCOMES,
    OUTCOME_CROSS_CLASS_REJECTED,
    OUTCOME_DECIDED,
    OUTCOME_FRESH,
    OUTCOME_NO_VERDICT,
    OUTCOME_READ_ERROR,
    OUTCOME_VALUES,
    SCHEMA_VERSION,
    AttributionRow,
    attribution_path,
    build_attribution_row,
    explain_pair,
    read_attribution_report,
    write_attribution_report,
)


def _row(**kw) -> AttributionRow:
    defaults = dict(
        path_a="/k/wiki/alpha.md",
        path_b="/k/wiki/beta.md",
        outcome=OUTCOME_NO_VERDICT,
        embedder=EMBEDDER_CHROMADB_DEFAULT,
        reason="llm-unavailable",
    )
    defaults.update(kw)
    path_a = defaults.pop("path_a")
    path_b = defaults.pop("path_b")
    outcome = defaults.pop("outcome")
    return build_attribution_row(path_a, path_b, outcome, **defaults)


class TestRowShape:
    def test_pair_key_is_order_independent_and_slug_based(self) -> None:
        """The pair key joins directly to a ``wiki/_verdicts/`` entry — it is
        :func:`athenaeum.verdicts.make_pair_key` over the two slugs, not a
        second id space, and it is derivable from the PATHS alone (which is
        what lets a cross-class rejection, which never builds a
        ``ComparatorPage``, key its row identically to a decided pair's)."""
        forward = _row(path_a="/k/wiki/alpha.md", path_b="/k/wiki/beta.md")
        reverse = _row(path_a="/k/wiki/beta.md", path_b="/k/wiki/alpha.md")
        assert forward.pair == reverse.pair == "alpha+beta"

    def test_explicit_pair_overrides_the_derived_key(self) -> None:
        row = _row(pair="zeta+omega")
        assert row.pair == "zeta+omega"

    def test_unknown_outcome_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="outcome must be one of"):
            _row(outcome="probably-fine")

    def test_round_trips_through_to_dict_from_dict(self) -> None:
        row = _row(
            outcome=OUTCOME_DECIDED,
            verdict="distinct",
            action="noop",
            cluster_id="wiki-c1",
            cluster_threshold=0.55,
            n_cluster_members=4,
            detail="detail text",
        )
        restored = AttributionRow.from_dict(json.loads(json.dumps(row.to_dict())))
        assert restored == row
        assert restored.schema_version == SCHEMA_VERSION

    def test_missing_fields_deserialize_without_a_keyerror(self) -> None:
        """A pre-schema row (or a partially-written one) must not explode the
        reader — same posture as ``Cluster.embedder``'s default."""
        restored = AttributionRow.from_dict({"pair": "a+b", "outcome": OUTCOME_FRESH})
        assert restored.embedder == EMBEDDER_UNKNOWN
        assert restored.sources == []
        assert restored.verdict is None

    @pytest.mark.parametrize("outcome", OUTCOME_VALUES)
    def test_became_proposal_partitions_the_outcome_vocabulary(
        self, outcome: str
    ) -> None:
        """Every outcome is decidably a proposal or not — no third state, so
        AC3's answer to "why did it not become a proposal?" is total."""
        row = _row(outcome=outcome)
        assert row.became_proposal is (outcome not in NON_PROPOSAL_OUTCOMES)

    def test_the_three_non_proposal_outcomes_are_exactly_the_ac1_hole(self) -> None:
        assert NON_PROPOSAL_OUTCOMES == {
            OUTCOME_CROSS_CLASS_REJECTED,
            OUTCOME_NO_VERDICT,
            OUTCOME_READ_ERROR,
        }


class TestWriteAndRead:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        rows = [_row(), _row(path_a="/k/wiki/g.md", path_b="/k/wiki/h.md")]
        canonical, rotation = write_attribution_report(
            rows, wiki_root, knowledge_root=tmp_path / "knowledge", config={}
        )
        assert canonical == attribution_path(wiki_root)
        assert canonical.is_file() and rotation is not None and rotation.is_file()
        assert read_attribution_report(wiki_root) == rows

    def test_zero_rows_writes_an_empty_canonical_file(self, tmp_path: Path) -> None:
        """athenaeum#1142's ``test_ledger_written_even_with_zero_suppressions``:
        written every real run, even to empty, so the canonical file always
        reflects THIS run's state and never a stale prior one."""
        wiki_root = tmp_path / "knowledge" / "wiki"
        write_attribution_report(
            [], wiki_root, knowledge_root=tmp_path / "knowledge", config={}
        )
        assert attribution_path(wiki_root).is_file()
        assert attribution_path(wiki_root).read_text(encoding="utf-8") == ""
        assert read_attribution_report(wiki_root) == []

    def test_canonical_is_replaced_not_appended(self, tmp_path: Path) -> None:
        """AC4: a current-run SNAPSHOT, not an accumulating append-only
        artifact — the asymmetry athenaeum#1142 drew against athenaeum#1229's
        1.4M-row unbounded ledger."""
        wiki_root = tmp_path / "knowledge" / "wiki"
        knowledge_root = tmp_path / "knowledge"
        write_attribution_report(
            [_row(), _row()], wiki_root, knowledge_root=knowledge_root, config={}
        )
        assert len(read_attribution_report(wiki_root)) == 2
        write_attribution_report(
            [_row()], wiki_root, knowledge_root=knowledge_root, config={}
        )
        assert len(read_attribution_report(wiki_root)) == 1

    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_attribution_report(tmp_path / "nope") == []

    def test_torn_trailing_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A crash mid-write can at worst cost the last row, never the whole
        artifact — the same tolerance ``verdicts._read_jsonl_tolerant`` gives
        the verdict ledger."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        good = json.dumps(_row().to_dict(), sort_keys=True)
        (wiki_root / DEFAULT_ATTRIBUTION_FILENAME).write_text(
            good + "\n" + good[: len(good) // 2], encoding="utf-8"
        )
        rows = read_attribution_report(wiki_root)
        assert len(rows) == 1
        assert rows[0].pair == "alpha+beta"


class TestRetentionIsBounded:
    def test_rotation_pruned_to_configured_retention(self, tmp_path: Path) -> None:
        """AC4, recovered from athenaeum#1142's
        ``test_rotation_pruned_to_configured_retention``: rotations follow the
        SAME ``librarian.rotation_retention`` policy
        ``raw/_librarian-clusters.jsonl`` already uses — reused via
        :func:`athenaeum.clusters.prune_cluster_rotations`, not a second
        retention knob. Hand-seeds the timestamps rather than depending on
        real wall-clock separation between calls."""
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        canonical = attribution_path(wiki_root)
        stem = canonical.stem
        for stamp in ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]:
            (wiki_root / f"{stem}-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")

        write_attribution_report(
            [],
            wiki_root,
            knowledge_root=knowledge_root,
            config={"librarian": {"rotation_retention": 2}},
        )
        remaining = sorted(p.name for p in wiki_root.glob(f"{stem}-*.jsonl"))
        # The 3 hand-seeded rotations + the 1 just written = 4 candidates;
        # keep=2 prunes to the 2 newest (the just-written one is newest by
        # construction — today's real UTC timestamp).
        assert len(remaining) == 2
        assert canonical.is_file()  # canonical never matches the rotation glob

    def test_retention_zero_disables_pruning(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        stem = attribution_path(wiki_root).stem
        for stamp in ["20260101T000000Z", "20260102T000000Z"]:
            (wiki_root / f"{stem}-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")
        write_attribution_report(
            [],
            wiki_root,
            knowledge_root=knowledge_root,
            config={"librarian": {"rotation_retention": 0}},
        )
        assert len(list(wiki_root.glob(f"{stem}-*.jsonl"))) == 3


class TestExplainPairAnswersTheDiagnosticQuestion:
    """AC3: ONE artifact read, no live host log access, answering
    athenaeum#1005's question — "which embedder produced this pair's
    candidacy, and why did it not become a proposal?" """

    def test_answers_both_halves_from_one_read(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        write_attribution_report(
            [
                _row(
                    reason="llm-unavailable",
                    detail="Gate 2 client is not configured",
                    cluster_threshold=0.55,
                )
            ],
            wiki_root,
            knowledge_root=tmp_path / "knowledge",
            config={},
        )
        answer = explain_pair(wiki_root, "alpha+beta")
        assert answer is not None
        # "which embedder produced this pair's candidacy"
        assert answer["embedder"] == EMBEDDER_CHROMADB_DEFAULT
        # "...and why did it not become a proposal?"
        assert answer["became_proposal"] is False
        assert answer["outcome"] == OUTCOME_NO_VERDICT
        assert answer["reason"] == "llm-unavailable"
        assert answer["detail"] == "Gate 2 client is not configured"
        assert answer["cluster_threshold"] == 0.55
        assert answer["at"]  # non-empty ISO timestamp

    def test_a_decided_pair_reports_became_proposal(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        write_attribution_report(
            [_row(outcome=OUTCOME_DECIDED, verdict="duplicate", action="fold-proposal")],
            wiki_root,
            knowledge_root=tmp_path / "knowledge",
            config={},
        )
        answer = explain_pair(wiki_root, "alpha+beta")
        assert answer is not None
        assert answer["became_proposal"] is True
        assert answer["verdict"] == "duplicate"

    def test_unexamined_pair_reports_none(self, tmp_path: Path) -> None:
        """Distinguishable from "examined and unsettled" precisely because AC1
        guarantees an examined pair always has a row."""
        wiki_root = tmp_path / "knowledge" / "wiki"
        write_attribution_report(
            [_row()], wiki_root, knowledge_root=tmp_path / "knowledge", config={}
        )
        assert explain_pair(wiki_root, "nobody+here") is None


class TestReadErrorRowShape:
    def test_read_error_is_a_non_proposal_outcome(self) -> None:
        row = _row(outcome=OUTCOME_READ_ERROR, reason="page-read-failed")
        assert row.became_proposal is False
