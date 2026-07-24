# SPDX-License-Identifier: Apache-2.0
"""Tests for merge-engine type integration (issue #433).

Covers the three #433 deliverables:

1. Type-compatibility precheck (:mod:`athenaeum.merge_type_gate`) — a
   cross-class cluster is rejected at proposal time with a machine-readable
   reason; a same-class cluster is unaffected (no regression on #421's
   mechanical guardrails).
2. Merge-vs-cite routing — a rejected cross-class cluster produces a cite
   proposal instead (never a destructive merge), exercised end to end
   through :func:`athenaeum.wiki_dedupe.propose_wiki_page_merges`.
3. Inference-block retraction (:func:`athenaeum.inference_blocks.retract_inference_block`)
   — round-trip parse -> retract -> parse preserves the fact core and any
   other blocks byte-for-byte, removing only the targeted block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.inference_blocks import parse_inference_blocks, retract_inference_block
from athenaeum.merge_type_gate import (
    CROSS_CLASS_REJECTED,
    build_cite_proposal,
    cross_class_precheck,
    read_memory_class,
)

# ---------------------------------------------------------------------------
# Shared fixtures: wiki pages with memory_class frontmatter
# ---------------------------------------------------------------------------

_BODY_A = "Kromatic is Tristan's primary venture and main business focus."
_BODY_B = "Tristan's primary venture is Kromatic, his main company."
_BODY_C = "The main venture Tristan runs day to day is Kromatic."
_BODY_GUIDELINE = "Always squash-merge feature branches before release."


def _write_page(
    wiki_root: Path,
    filename: str,
    *,
    page_type: str = "concept",
    name: str | None = None,
    memory_class: str | None = None,
    body: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or filename[:-3]}", f"type: {page_type}"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    lines.append("---")
    lines.append(body)
    path = wiki_root / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestReadMemoryClass:
    def test_reads_present_value(self, tmp_path: Path) -> None:
        p = _write_page(tmp_path, "a.md", memory_class="fact", body=_BODY_A)
        assert read_memory_class(p) == "fact"

    def test_absent_is_none(self, tmp_path: Path) -> None:
        p = _write_page(tmp_path, "b.md", body=_BODY_B)
        assert read_memory_class(p) is None

    def test_empty_string_is_none(self, tmp_path: Path) -> None:
        p = _write_page(tmp_path, "c.md", memory_class="", body=_BODY_C)
        assert read_memory_class(p) is None

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert read_memory_class(tmp_path / "does-not-exist.md") is None


# ---------------------------------------------------------------------------
# AC: cross-class cluster -> precheck rejects with a machine-readable reason
# ---------------------------------------------------------------------------


class TestCrossClassPrecheck:
    def test_cross_class_cluster_rejected(self, tmp_path: Path) -> None:
        fact_page = _write_page(
            tmp_path, "fact-a.md", memory_class="fact", body=_BODY_A
        )
        guideline_page = _write_page(
            tmp_path, "guideline-a.md", memory_class="guideline", body=_BODY_GUIDELINE
        )

        rejection = cross_class_precheck([fact_page, guideline_page])

        assert rejection is not None
        assert rejection.reason == CROSS_CLASS_REJECTED
        # Machine-readable: classes_seen maps class -> member paths.
        assert set(rejection.classes_seen) == {"fact", "guideline"}
        assert str(fact_page) in rejection.classes_seen["fact"]
        assert str(guideline_page) in rejection.classes_seen["guideline"]
        assert "fact" in rejection.detail and "guideline" in rejection.detail

    def test_to_dict_is_json_serializable_shape(self, tmp_path: Path) -> None:
        import json

        fact_page = _write_page(tmp_path, "f.md", memory_class="fact", body="x")
        axiom_page = _write_page(tmp_path, "ax.md", memory_class="axiom", body="y")
        rejection = cross_class_precheck([fact_page, axiom_page])
        assert rejection is not None
        # Must be json.dumps-able (machine-readable reason-record contract,
        # mirrors athenaeum.provenance.build_merge_provenance_record).
        encoded = json.dumps(rejection.to_dict())
        assert "cross_class_incompatible" in encoded

    def test_three_way_cross_class_lists_all_classes(self, tmp_path: Path) -> None:
        p1 = _write_page(tmp_path, "p1.md", memory_class="fact", body="a")
        p2 = _write_page(tmp_path, "p2.md", memory_class="guideline", body="b")
        p3 = _write_page(tmp_path, "p3.md", memory_class="decision", body="c")
        rejection = cross_class_precheck([p1, p2, p3])
        assert rejection is not None
        assert set(rejection.classes_seen) == {"fact", "guideline", "decision"}


# ---------------------------------------------------------------------------
# AC: same-class cluster passes the precheck unchanged (no #421 regression)
# ---------------------------------------------------------------------------


class TestSameClassPrecheckPasses:
    def test_same_class_cluster_passes(self, tmp_path: Path) -> None:
        a = _write_page(tmp_path, "a.md", memory_class="fact", body=_BODY_A)
        b = _write_page(tmp_path, "b.md", memory_class="fact", body=_BODY_B)
        c = _write_page(tmp_path, "c.md", memory_class="fact", body=_BODY_C)
        assert cross_class_precheck([a, b, c]) is None

    def test_all_untyped_cluster_passes(self, tmp_path: Path) -> None:
        # Conservative untyped policy: legacy pages with no memory_class at
        # all must not be blocked by the new gate.
        a = _write_page(tmp_path, "a.md", body=_BODY_A)
        b = _write_page(tmp_path, "b.md", body=_BODY_B)
        assert cross_class_precheck([a, b]) is None

    def test_typed_plus_untyped_passes(self, tmp_path: Path) -> None:
        # One typed + one untyped member: untyped is compatible-with-anything
        # under the conservative policy, so only ONE distinct typed class is
        # present -> passes.
        typed = _write_page(tmp_path, "typed.md", memory_class="fact", body=_BODY_A)
        untyped = _write_page(tmp_path, "untyped.md", body=_BODY_B)
        assert cross_class_precheck([typed, untyped]) is None

    def test_singleton_passes(self, tmp_path: Path) -> None:
        a = _write_page(tmp_path, "a.md", memory_class="fact", body=_BODY_A)
        assert cross_class_precheck([a]) is None

    def test_empty_passes(self) -> None:
        assert cross_class_precheck([]) is None


# ---------------------------------------------------------------------------
# Cite-proposal shape
# ---------------------------------------------------------------------------


class TestBuildCiteProposal:
    def test_fact_pages_are_cited_not_citing(self, tmp_path: Path) -> None:
        fact_page = _write_page(
            tmp_path, "fact-a.md", memory_class="fact", body=_BODY_A
        )
        guideline_page = _write_page(
            tmp_path, "guideline-a.md", memory_class="guideline", body=_BODY_GUIDELINE
        )
        rejection = cross_class_precheck([fact_page, guideline_page])
        assert rejection is not None

        cite = build_cite_proposal([fact_page, guideline_page], rejection)

        assert str(fact_page) in cite.cited
        assert str(fact_page) not in cite.citing
        assert str(guideline_page) in cite.citing
        assert str(guideline_page) not in cite.cited
        assert cite.action == "propose_cite"
        assert cite.rejection is rejection

    def test_no_fact_present_falls_back_to_alphabetical_primary(
        self, tmp_path: Path
    ) -> None:
        # guideline vs decision, no fact member: deterministic fallback picks
        # the alphabetically-first class ("decision" < "guideline") as citing.
        guideline_page = _write_page(
            tmp_path, "g.md", memory_class="guideline", body=_BODY_GUIDELINE
        )
        decision_page = _write_page(
            tmp_path, "d.md", memory_class="decision", body="Decided to use X."
        )
        rejection = cross_class_precheck([guideline_page, decision_page])
        assert rejection is not None
        cite = build_cite_proposal([guideline_page, decision_page], rejection)

        assert str(decision_page) in cite.citing
        assert str(guideline_page) in cite.cited


# ---------------------------------------------------------------------------
# End-to-end: propose_wiki_page_merges routes cross-class clusters to a cite
# proposal and emits ZERO merge proposals; same-class clusters are unaffected.
# ---------------------------------------------------------------------------

_VEC_A = [1.0, 0.0]
_VEC_B = [0.98, 0.2]
_VEC_C = [0.95, 0.31]

_CROSS_CLASS_TEXT_TO_VEC = {
    _BODY_A: _VEC_A,
    _BODY_GUIDELINE: _VEC_B,
}

_SAME_CLASS_TEXT_TO_VEC = {
    _BODY_A: _VEC_A,
    _BODY_B: _VEC_B,
    _BODY_C: _VEC_C,
}


def _fake_embed_factory(mapping: dict[str, list[float]]):
    def _fake_embed(texts: list[str]) -> list[list[float]] | None:
        return [mapping.get(t.strip(), [0.0, 0.0]) for t in texts]

    return _fake_embed


class TestProposeWikiPageMergesCrossClassRouting:
    def test_cross_class_cluster_zero_merge_proposals_cite_emitted(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root,
            "fact-a.md",
            memory_class="fact",
            body=_BODY_A,
        )
        _write_page(
            wiki_root,
            "guideline-a.md",
            memory_class="guideline",
            body=_BODY_GUIDELINE,
        )

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed_factory(_CROSS_CLASS_TEXT_TO_VEC),
        )

        assert len(proposals) == 1
        assert proposals[0]["action"] == "propose_cite"
        assert "rejection" in proposals[0]
        assert proposals[0]["rejection"]["reason"] == CROSS_CLASS_REJECTED

        # Zero merge proposals: no block ever written to _pending_merges.md.
        merges_path = wiki_root / "_pending_merges.md"
        assert not merges_path.exists()

    def test_same_class_cluster_produces_normal_merge_proposal(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: #421's gates + normal merge behavior unaffected."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "venture-a.md", memory_class="fact", body=_BODY_A)
        _write_page(wiki_root, "venture-b.md", memory_class="fact", body=_BODY_B)
        _write_page(wiki_root, "venture-c.md", memory_class="fact", body=_BODY_C)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed_factory(_SAME_CLASS_TEXT_TO_VEC),
        )

        assert len(proposals) == 1
        assert "action" not in proposals[0]  # normal merge-proposal shape
        assert len(proposals[0]["sources"]) == 3

        merges_path = wiki_root / "_pending_merges.md"
        assert merges_path.is_file()
        text = merges_path.read_text(encoding="utf-8")
        assert text.count("## [") == 1

    def test_untyped_cluster_still_merges_no_regression(self, tmp_path: Path) -> None:
        """Legacy/untyped pages (no memory_class at all) must keep merging
        exactly as before #433 — the conservative untyped policy."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "venture-a.md", body=_BODY_A)
        _write_page(wiki_root, "venture-b.md", body=_BODY_B)
        _write_page(wiki_root, "venture-c.md", body=_BODY_C)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed_factory(_SAME_CLASS_TEXT_TO_VEC),
        )

        assert len(proposals) == 1
        assert "action" not in proposals[0]
        merges_path = wiki_root / "_pending_merges.md"
        assert merges_path.is_file()


# ---------------------------------------------------------------------------
# AC: every precheck rejection logs a machine-readable reason.
# ---------------------------------------------------------------------------


class TestRejectionLogging:
    def test_rejection_logs_machine_readable_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        fact_page = _write_page(tmp_path, "f.md", memory_class="fact", body="x")
        guideline_page = _write_page(
            tmp_path, "g.md", memory_class="guideline", body="y"
        )
        with caplog.at_level(logging.INFO, logger="athenaeum.merge_type_gate"):
            rejection = cross_class_precheck([fact_page, guideline_page])
        assert rejection is not None
        assert any(
            CROSS_CLASS_REJECTED in record.message or "REJECTED" in record.message
            for record in caplog.records
        )

    def test_wiki_dedupe_logs_rejection_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root, "fact-a.md", memory_class="fact", body=_BODY_A
        )
        _write_page(
            wiki_root,
            "guideline-a.md",
            memory_class="guideline",
            body=_BODY_GUIDELINE,
        )

        with caplog.at_level(logging.INFO, logger="athenaeum.wiki_dedupe"):
            propose_wiki_page_merges(
                tmp_path,
                config={},
                threshold=0.8,
                embedding_provider=_fake_embed_factory(_CROSS_CLASS_TEXT_TO_VEC),
            )

        assert any(
            "cross-class cluster rejected" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# AC: inference-block retraction round-trip — fact core + other blocks
# byte-identical, only the targeted block gone.
# ---------------------------------------------------------------------------

_FACT_PAGE_WITH_INFERENCES = """---
name: acme-headcount
type: concept
memory_class: fact
---
Acme has 40 employees, per the Q2 filing.

## Inference
**Basis**: [[acme-headcount]]
**Confidence**: 0.8
Acme is probably still hiring given headcount growth.

## Summary
Just a summary section, not an inference block.

## Inference
**Basis**: [[acme-headcount]]
**Confidence**: 0.6
A second, distinct inference block that must survive retraction of the first.
"""


class TestRetractInferenceBlock:
    def test_round_trip_removes_only_targeted_block(self) -> None:
        blocks = parse_inference_blocks(_FACT_PAGE_WITH_INFERENCES)
        assert len(blocks) == 2
        target_id = blocks[0].id
        other_id = blocks[1].id

        retracted_text = retract_inference_block(
            _FACT_PAGE_WITH_INFERENCES, target_id
        )

        # Fact core (frontmatter + intro sentence) preserved byte-for-byte.
        assert "memory_class: fact" in retracted_text
        assert "Acme has 40 employees, per the Q2 filing." in retracted_text
        # The ## Summary section survives untouched.
        assert "Just a summary section, not an inference block." in retracted_text
        # The retracted block's own body is gone.
        assert "Acme is probably still hiring" not in retracted_text

        # Re-parsing sees exactly the surviving block, with the SAME id
        # (byte-identical raw block for the surviving unit).
        remaining = parse_inference_blocks(retracted_text)
        assert len(remaining) == 1
        assert remaining[0].id == other_id
        assert (
            remaining[0].body
            == "A second, distinct inference block that must survive "
            "retraction of the first."
        )

    def test_fact_core_byte_identical_outside_removed_block(self) -> None:
        """Stronger byte-identity check: everything before the retracted
        block's header and everything from the next block onward is
        UNCHANGED, character for character."""
        text = _FACT_PAGE_WITH_INFERENCES
        blocks = parse_inference_blocks(text)
        target_id = blocks[0].id

        retracted_text = retract_inference_block(text, target_id)

        header_idx = text.index("## Inference")
        prefix = text[:header_idx]
        assert retracted_text.startswith(prefix)

        summary_idx = text.index("## Summary")
        suffix = text[summary_idx:]
        assert retracted_text.endswith(suffix)

    def test_retract_second_block_preserves_first(self) -> None:
        blocks = parse_inference_blocks(_FACT_PAGE_WITH_INFERENCES)
        first_id = blocks[0].id
        second_id = blocks[1].id

        retracted_text = retract_inference_block(
            _FACT_PAGE_WITH_INFERENCES, second_id
        )
        remaining = parse_inference_blocks(retracted_text)
        assert len(remaining) == 1
        assert remaining[0].id == first_id
        assert "second, distinct inference block" not in retracted_text

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(ValueError, match="no ## Inference block"):
            retract_inference_block(_FACT_PAGE_WITH_INFERENCES, "deadbeef0000")

    def test_retract_only_block_leaves_valid_page(self) -> None:
        single_block_text = """---
name: solo-fact
memory_class: fact
---
The core fact statement.

## Inference
**Basis**: [[solo-fact]]
**Confidence**: 0.5
The only inference on this page.
"""
        blocks = parse_inference_blocks(single_block_text)
        assert len(blocks) == 1
        retracted = retract_inference_block(single_block_text, blocks[0].id)
        assert "The core fact statement." in retracted
        assert "The only inference on this page." not in retracted
        assert parse_inference_blocks(retracted) == []


# ---------------------------------------------------------------------------
# merge.py resolver-path wiring: cross-class precheck sits in _emit_escalation
# alongside the #421 suppression gate. Import-level sanity so a refactor that
# removes the wiring is caught, without needing the full Opus-resolver
# machinery in this file (that path is exercised in test_librarian_merge.py).
# ---------------------------------------------------------------------------


class TestMergePyWiring:
    def test_merge_module_imports_type_gate(self) -> None:
        import athenaeum.merge as merge_mod

        assert merge_mod.cross_class_precheck is cross_class_precheck
        assert merge_mod.build_cite_proposal is build_cite_proposal
