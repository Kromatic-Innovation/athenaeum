# SPDX-License-Identifier: Apache-2.0
"""Characterisation tests for the C4 capability surface (issue athenaeum#1254).

Part of the ``athenaeum#715`` phase-4 plan to retire ``merge.py``'s C4
contradiction detector (:func:`athenaeum.contradictions.detect_contradictions`).
This module IS the capability-loss register named in that plan. The detector
itself has now been RETIRED (issue athenaeum#1256): ``merge.py``'s C4 region
is gone, ``merge_clusters_to_wiki`` takes no LLM client and writes no
escalation, and ``cross_scope.py`` / ``detection_state.py`` were deleted.
Each test below still pins one downstream contract that a naive deletion
could have silently broken — re-pointed, where the capability moved, at
whatever now serves it. A test in this file failing (outside of a
deliberate, documented change) means a load-bearing consumer of a
contradiction verdict just lost its data.

All four tests are OFFLINE — no network call is made anywhere in this file.

Register (post-retirement state):

1. :class:`TestFrontmatterWrite` — the ``status: contradiction-flagged`` +
   ``contradiction_type`` frontmatter written by
   ``athenaeum.merge.render_merged_entry`` off an already-populated
   :class:`~athenaeum.merge.MergedWikiEntry.contradiction`. UNCHANGED: both
   the field and the projection were deliberately KEPT by the retirement
   (the merge pass itself just no longer populates them). CONSUMER-side,
   independent of who/what populates ``contradiction``.
2. :class:`TestRecallHeaderRender` — the contested-header line rendered by
   ``athenaeum.mcp_server._recall_metadata_lines`` off
   ``status == "contradiction-flagged"``. UNCHANGED: writes the status
   directly to a page on disk, so it never depended on the detector in the
   first place. CONSUMER-side.
3. :class:`TestRetireGuard` — the retire-pass MOVE guard, now
   ``athenaeum.retire._move_eligibility(entry, wiki_root, members)``
   (issue athenaeum#1256 changed its signature from the old one-argument
   ``_move_eligibility(entry)``). It no longer reads ``entry.contradiction``
   at all; eligibility is decided by reading the comparator's verdict
   ledger (:func:`athenaeum.verdicts.get_verdict_status`) over every
   candidate pair among *members*. This test rebuilds the safety property
   the old test pinned — a cluster with no trustworthy verdict is NOT
   move-eligible — against a REAL ledger fixture (no stub), per the idiom
   in ``tests/test_retire_ledger_eligibility.py``.
4. :class:`TestConflictTypeReachesPendingQuestions` — the ``conflict_type``
   value (``factual`` / ``prescriptive`` / ``stance``) reaching
   ``wiki/_pending_questions.md``. PRODUCER-side and end-to-end, but no
   longer through ``merge_clusters_to_wiki`` (which writes no escalation
   post-retirement) — re-pointed at the comparator lane
   (``athenaeum.verdict_effects.apply_verdict_effect`` /
   ``_queue_contradiction``), the port destination named by issue
   athenaeum#1679. Verified below (and see the class docstring) that the
   comparator DOES carry a real per-conflict ``conflict_type`` end to end,
   not the hardcoded ``"principled"`` fallback it uses only when a caller
   never classifies at all.

Tests 1-3 pin the CONSUMER contracts independently of whether anything
upstream currently produces the data they consume — a defensible choice
(each consumer contract should hold on its own), but it means this register
does not, by itself, prove a producer keeps producing ``contradiction`` /
``contradictions_detected`` / ledgered verdicts. Only test 4 does that, and
only for the comparator lane's own escalation path (test 3's ledger lane is
exercised directly against a real ledger fixture instead, since the
comparator has no live caller yet — issue athenaeum#1946).

Note on line-number drift: this module supersedes line-number citations as
the checked, executable version. The tests anchor to BEHAVIOUR (the
rendered strings / returned values), not to line numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    CompareOutcome,
    page_from_text,
)
from athenaeum.contradictions import ContradictionResult
from athenaeum.mcp_server import recall_search
from athenaeum.merge import (
    CONTRADICTION_STATUS_FLAGGED,
    MergedWikiEntry,
    render_merged_entry,
)
from athenaeum.models import parse_frontmatter
from athenaeum.retire import _move_eligibility
from athenaeum.runlock import RunLock
from athenaeum.verdict_effects import apply_verdict_effect
from athenaeum.verdicts import (
    Basis,
    append_verdict,
    build_verdict_entry,
    page_id_for_path,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _member(tmp_path: Path, name: str) -> Path:
    """A minimal real auto-memory file -- mirrors the idiom in
    ``tests/test_retire_ledger_eligibility.py``."""
    scope = tmp_path / "raw" / "auto-memory" / "scope-x"
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / f"{name}.md"
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\nbody {name}\n", encoding="utf-8")
    return path


def _comparator_page(page_id: str, body: str) -> object:
    text = f"---\nname: {page_id}\n---\n\n{body}\n"
    return page_from_text(page_id, text)


# ---------------------------------------------------------------------------
# 1. merge.py: status + contradiction_type frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatterWrite:
    """Pins ``render_merged_entry``'s contradiction-flag frontmatter write.

    Capability-loss register (athenaeum#1254 / athenaeum#1256): the
    ``contradiction`` field and this frontmatter projection were both
    deliberately KEPT by the C4 retirement -- only the merge pass's own
    *population* of the field was removed. If a future change ever deleted
    the field or the projection, every downstream consumer that gates on
    ``status: contradiction-flagged`` -- the recall header
    (:class:`TestRecallHeaderRender`) included -- would silently stop
    seeing contested pages as contested, however they got flagged.
    """

    def test_frontmatter_carries_status_and_conflict_type(self) -> None:
        contradiction = ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=["a.md", "b.md"],
            conflicting_passages=["claim A", "claim B"],
            rationale="incompatible claims",
        )
        entry = MergedWikiEntry(
            topic_slug="pinned-topic",
            cluster_id="c-pin-1",
            cluster_centroid_score=0.6,
            contradictions_detected=True,
            contradiction=contradiction,
            body="Some merged body text.\n",
        )

        rendered = render_merged_entry(entry)
        meta, _ = parse_frontmatter(rendered)

        assert meta["status"] == CONTRADICTION_STATUS_FLAGGED
        assert meta["contradiction_type"] == "factual"


# ---------------------------------------------------------------------------
# 2. mcp_server.py: recall header render
# ---------------------------------------------------------------------------


class TestRecallHeaderRender:
    """Pins the contested-header line the recall tool renders.

    Capability-loss register (athenaeum#1254 / athenaeum#1256): unaffected
    by the detector's retirement -- ``mcp_server.py`` itself documents this
    as "the load-bearing case -- silently returning one side of a disputed
    pair is the failure this header prevents" (the failure athenaeum#325
    closed). Whatever writes ``status: contradiction-flagged`` now (the
    comparator lane's ``write_contested_flag``, per
    :class:`TestConflictTypeReachesPendingQuestions` below, rather than C4),
    this header must still render off it.
    """

    def test_contradiction_flagged_status_renders_in_recall_output(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "disputed_topic.md").write_text(
            "---\n"
            "name: Disputed topic\n"
            "status: contradiction-flagged\n"
            "---\n\n"
            "This pinned-topic fact has two conflicting sides.\n",
            encoding="utf-8",
        )

        # search_backend="keyword" (the default) is the FTS5 backend -- no
        # LLM client is constructed or called anywhere on this path.
        result = recall_search(wiki, "pinned-topic")

        assert "**Status:** contradiction-flagged (see _pending_questions.md)" in result


# ---------------------------------------------------------------------------
# 3. retire.py: move-eligibility guard
# ---------------------------------------------------------------------------


class TestRetireGuard:
    """Pins the retire-pass guard against retiring an unverified cluster.

    Capability-loss register (athenaeum#1256): the C4 detector is retired,
    and ``_move_eligibility`` no longer takes just an *entry* nor reads
    ``entry.contradiction`` -- its signature is now
    ``_move_eligibility(entry, wiki_root, members)`` and it decides
    eligibility by reading the comparator's verdict ledger
    (:func:`athenaeum.verdicts.get_verdict_status`) for every candidate
    pair among *members* (operator decision, athenaeum#1256, 2026-09-15).

    The safety property the old C4-era test pinned is unchanged in
    substance: the guard below is still THE ONLY thing standing between a
    cluster with no trustworthy verdict and the raw sources it derived from
    being ``git rm``'d out from under an unconfirmed contested fact -- it
    is just now "no ledger row for this pair" rather than "no
    ``entry.contradiction``". Built against a REAL ledger (no stub), via
    the ``build_verdict_entry`` + ``append_verdict`` idiom under a real
    ``RunLock``, matching ``tests/test_retire_ledger_eligibility.py``.
    """

    def test_no_ledger_row_blocks_move_with_reason(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a = _member(tmp_path, "a")
        b = _member(tmp_path, "b")
        entry = MergedWikiEntry(
            topic_slug="unverified-topic",
            cluster_id="c-pin-2",
            cluster_centroid_score=0.9,
            contradictions_detected=False,
            contradiction=None,
            body="b\n",
        )

        # No verdict is ever written to the ledger for (a, b) -- the
        # "never attempted" case the operator-specified mapping holds for.
        eligible, reason = _move_eligibility(entry, wiki_root, [a, b])

        assert eligible is False
        assert "no comparator verdict ledgered" in reason
        assert "athenaeum#1946" in reason

    def test_contradiction_verdict_blocks_move_with_reason(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a = _member(tmp_path, "a")
        b = _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)
        entry = MergedWikiEntry(
            topic_slug="contested-topic",
            cluster_id="c-pin-3",
            cluster_centroid_score=0.9,
            contradictions_detected=False,
        )

        with RunLock(tmp_path) as lock:
            verdict_entry = build_verdict_entry(
                id_a, id_b, "contradiction", basis=Basis(), at="2026-09-01", decided_by="comparator"
            )
            append_verdict(wiki_root, verdict_entry, lock=lock)

        eligible, reason = _move_eligibility(entry, wiki_root, [a, b])

        assert eligible is False
        assert "contradiction" in reason


# ---------------------------------------------------------------------------
# 4. verdict_effects.py: conflict_type reaching _pending_questions.md
# ---------------------------------------------------------------------------


class TestConflictTypeReachesPendingQuestions:
    """Pins contradiction escalation's ``conflict_type`` reaching the pending queue.

    Capability-loss register (athenaeum#1256): the C4 escalation path this
    test used to drive (``merge_clusters_to_wiki`` -> the detector's own
    ``factual``/``prescriptive``/``stance`` classification ->
    ``_pending_questions.md``) is GONE -- ``merge_clusters_to_wiki`` takes
    no client and writes no escalation. The capability was ported, not
    lost: issue athenaeum#1679 gave :class:`~athenaeum.comparator.CompareOutcome`
    its own ``conflict_type: ConflictType | None`` field
    (``src/athenaeum/comparator.py``), and
    ``athenaeum.verdict_effects._queue_contradiction`` threads it into the
    :class:`~athenaeum.models.EscalationItem` it appends
    (``conflict_type=outcome.conflict_type or "principled"`` --
    ``"principled"`` is only the FALLBACK for a caller that never
    classifies; a caller that does, as this test's fixtures do, gets its
    own value straight through). This test drives that lane directly via
    ``athenaeum.verdict_effects.apply_verdict_effect`` with a
    ``CompareOutcome(verdict="contradiction", conflict_type=...)``, per the
    established fixture idiom in ``tests/test_verdict_effects.py``
    (``TestEF10bConflictTypeThreading.test_contradiction_uses_outcome_conflict_type``
    pins the same threading against ``athenaeum.decisions.list_pending_decisions``;
    this test additionally confirms the literal rendered text in
    ``_pending_questions.md``).

    ``ConflictType`` (``models.py``) is a THREE-member Literal --
    ``factual`` / ``prescriptive`` / ``stance`` -- and all three are
    exercised here, UNLIKE the retired C4-era version of this test (which
    deliberately excluded ``stance``, because C4's own escalation path ran
    behind ``resolutions.py``'s athenaeum#327 opinion-attribution
    short-circuit). The comparator lane has NO equivalent short-circuit:
    ``athenaeum.verdict_effects.apply_verdict_effect`` dispatches a
    ``contradiction`` verdict straight to ``_apply_contradiction`` /
    ``_queue_contradiction`` regardless of ``conflict_type`` -- there is no
    ``conflict_type == "stance"`` branch anywhere in ``comparator.py`` or
    ``verdict_effects.py``. The ``attribute_both`` ported action
    (``apply_suppress_or_attribute_both_effect``, issue athenaeum#1680) is a
    SEPARATE, caller-driven resolver action gated on an explicit
    ``action=`` + ``confidence=`` the CALLER must already have decided on
    (e.g. from a ``ResolutionProposal``) -- it is not triggered
    automatically merely because ``outcome.conflict_type == "stance"``. So
    this is a genuine, confirmed behaviour change from the C4 era: a
    ``stance`` contradiction now reaches ``_pending_questions.md`` exactly
    like ``factual``/``prescriptive`` do, rather than being intercepted
    before escalation. This test pins that TRUE new behaviour rather than
    forcing the old suppression assertion.
    """

    @pytest.mark.parametrize("conflict_type", ["factual", "prescriptive", "stance"])
    def test_comparator_conflict_type_appears_in_pending_questions(
        self, tmp_path: Path, conflict_type: str
    ) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _comparator_page("pin-v1", "Always commit directly to develop.")
        page_b = _comparator_page("pin-v2", "Never commit directly; always use a branch.")
        outcome = CompareOutcome(
            verdict=VERDICT_CONTRADICTION,
            conflicting_passages=[
                "Always commit directly to develop.",
                "Never commit directly; always use a branch.",
            ],
            conflict_type=conflict_type,
        )

        # Auto-supersession is DEFAULT OFF (config=None already resolves this
        # way), but pinned explicitly here so this test does not depend on
        # the ambient environment: every contradiction must route to the
        # decision queue, never be auto-enacted by athenaeum.supersession.
        config = {"librarian": {"auto_supersession_enabled": False}}

        result = apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root, config=config)

        assert result.action == "queued"

        pending = wiki_root / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert f"**Conflict type**: {conflict_type}" in text

    def test_unclassified_conflict_falls_back_to_principled(self, tmp_path: Path) -> None:
        """The counterpart to the three real ``ConflictType`` values above:
        a caller that never classifies the conflict at all (``conflict_type``
        left at its default ``None``) still reaches the pending queue, but
        with the pre-existing hardcoded fallback literal, not a classified
        value -- the behaviour this whole register exists to distinguish
        from a real classification."""
        wiki_root = tmp_path / "wiki"
        page_a = _comparator_page("pin-v1", "Always commit directly to develop.")
        page_b = _comparator_page("pin-v2", "Never commit directly; always use a branch.")
        outcome = CompareOutcome(
            verdict=VERDICT_CONTRADICTION,
            conflicting_passages=[
                "Always commit directly to develop.",
                "Never commit directly; always use a branch.",
            ],
        )
        config = {"librarian": {"auto_supersession_enabled": False}}

        apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root, config=config)

        text = (wiki_root / "_pending_questions.md").read_text(encoding="utf-8")
        assert "**Conflict type**: principled" in text
