# SPDX-License-Identifier: Apache-2.0
"""Characterisation tests for the C4 capability surface (issue athenaeum#1254).

Part of the ``athenaeum#715`` phase-4 plan to retire ``merge.py``'s C4
contradiction detector (:func:`athenaeum.contradictions.detect_contradictions`).
This module IS the capability-loss register named in that plan: each test
below pins one downstream contract that a naive deletion of the detector
would silently break, so every later step of the retirement sequence has an
executable oracle to keep green. A test in this file failing (outside of a
deliberate, documented retirement step) means a load-bearing consumer of the
C4 verdict just lost its data.

All four tests are OFFLINE — no network call is made. The two tests that
exercise the LLM-mediated detection path use a ``unittest.mock.MagicMock``
built to mirror the Anthropic SDK response shape, per
``tests/test_contradictions.py``'s convention. The other two exercise pure
functions and the keyword (FTS5) recall backend, neither of which ever
touches an LLM client.

Register:

1. :class:`TestFrontmatterWrite` — the ``status: contradiction-flagged`` +
   ``contradiction_type`` frontmatter written by
   ``athenaeum.merge.render_merged_entry`` (observed at
   ``src/athenaeum/merge.py:1592-1594`` at time of writing). CONSUMER-side:
   builds a :class:`~athenaeum.merge.MergedWikiEntry` with ``contradiction``
   already populated and pins the projection into frontmatter, not the
   detector path that fills that field.
2. :class:`TestRecallHeaderRender` — the contested-header line rendered by
   ``athenaeum.mcp_server._recall_metadata_lines`` off
   ``status == "contradiction-flagged"`` (observed at
   ``src/athenaeum/mcp_server.py:619-624``). CONSUMER-side: writes
   ``status: contradiction-flagged`` directly to a page on disk, so it
   exercises only the ``status``-equality disjunct of that predicate, not
   the sibling ``fm.get("contradictions_detected")`` disjunct.
3. :class:`TestRetireGuard` — the retire-pass MOVE guard in
   ``athenaeum.retire._move_eligibility`` that blocks the move with
   ``"no contradiction verdict available — not safe to retire"`` when
   ``entry.contradiction is None`` (observed at
   ``src/athenaeum/retire.py:176-177``). CONSUMER-side, pure-function call.
4. :class:`TestConflictTypeReachesPendingQuestions` — the ``conflict_type``
   value (``factual`` / ``prescriptive`` / ``stance``) that the C4
   escalation path stamps onto the :class:`~athenaeum.models.EscalationItem`
   it appends (observed at ``src/athenaeum/merge.py:2412``), which
   ``tier4_escalate`` then renders into ``wiki/_pending_questions.md``.
   PRODUCER-side and end-to-end: drives ``merge_clusters_to_wiki`` from
   cluster JSONL through the (mocked) LLM detector to the rendered file —
   the only test in this register that exercises detection itself rather
   than a downstream projection of an already-decided verdict.

Tests 1-3 pin the CONSUMER contracts independently of whether the detector
still produces the data they consume — a defensible choice (each consumer
contract should hold on its own), but it means this register does not, by
itself, prove the detector keeps producing ``contradiction`` /
``contradictions_detected``. Only test 4 does that end to end.

Note on line-number drift: the citations above were re-read directly from
this branch's source at the time this file was written (issue athenaeum#1254's
"Assertions unchecked" warning applies to the ORIGINAL issue-filing citations,
which this module supersedes as the checked, executable version). Line
numbers will drift as the file changes; the tests anchor to BEHAVIOUR
(the rendered strings / returned values), not to line numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.mcp_server import recall_search
from athenaeum.merge import (
    CONTRADICTION_STATUS_FLAGGED,
    MergedWikiEntry,
    merge_clusters_to_wiki,
    render_merged_entry,
)
from athenaeum.models import parse_frontmatter
from athenaeum.retire import _move_eligibility

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_librarian_merge.py's fixture shape)
# ---------------------------------------------------------------------------


def _write_am_file(
    scope_dir: Path,
    filename: str,
    *,
    frontmatter_name: str,
    body: str,
) -> Path:
    """Write a minimal auto-memory markdown file."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        f"---\nname: {frontmatter_name}\ntype: feedback\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _write_cluster_jsonl(knowledge_root: Path, rows: list[dict[str, object]]) -> Path:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return out


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


def _mock_client(payload_text: str) -> MagicMock:
    """MagicMock mirroring ``anthropic.Anthropic().messages.create(...)``.

    Same shape as ``tests/test_contradictions.py``'s ``_fake_client`` — no
    network call is ever made, the mock intercepts ``messages.create``
    entirely.
    """
    client = MagicMock()
    response = MagicMock()
    # type="text" matters: a bare MagicMock(text=...) has a .type attribute
    # that is itself an auto-created MagicMock, which never equals "text".
    # provider.response_text() walks response.content for a block whose
    # .type == "text" and falls through to a content[0].text compatibility
    # fallback otherwise — so an untyped mock exercises that fallback shim,
    # not the primary path a real Anthropic response (whose blocks carry
    # type="text") actually takes.
    response.content = [MagicMock(type="text", text=payload_text)]
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# 1. merge.py: status + contradiction_type frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatterWrite:
    """Pins ``render_merged_entry``'s contradiction-flag frontmatter write.

    Capability-loss register (athenaeum#715 phase 4): if C4's write of
    ``entry.contradictions_detected`` / ``entry.contradiction`` were deleted
    (or ``render_merged_entry`` stopped projecting them into frontmatter),
    every downstream consumer that gates on ``status: contradiction-flagged``
    — the recall header (:class:`TestRecallHeaderRender`) and the retire
    guard (:class:`TestRetireGuard`) both included — would silently stop
    seeing contested pages as contested.
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

    Capability-loss register (athenaeum#715 phase 4): ``mcp_server.py``
    itself documents this as "the load-bearing case — silently returning
    one side of a disputed pair is the failure this header prevents" (the
    failure athenaeum#325 closed). If C4 stopped writing
    ``status: contradiction-flagged`` (see :class:`TestFrontmatterWrite`)
    and nothing replaced it, this header would never render and a recall
    caller would silently receive one side of a disputed fact with no
    indication the record is contested.
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

        # search_backend="keyword" (the default) is the FTS5 backend — no
        # LLM client is constructed or called anywhere on this path.
        result = recall_search(wiki, "pinned-topic")

        assert "**Status:** contradiction-flagged (see _pending_questions.md)" in result


# ---------------------------------------------------------------------------
# 3. retire.py: move-eligibility guard
# ---------------------------------------------------------------------------


class TestRetireGuard:
    """Pins the retire-pass guard against retiring an unverified cluster.

    Capability-loss register (athenaeum#715 phase 4): without a real C4
    verdict, ``entry.contradiction`` is always ``None`` — the guard this
    test pins is the ONLY thing standing between "a cluster with no
    contradiction check" and the raw sources it derived from being
    git-rm'd out from under an unconfirmed contested fact.
    """

    def test_none_contradiction_blocks_move_with_reason(self) -> None:
        entry = MergedWikiEntry(
            topic_slug="unverified-topic",
            cluster_id="c-pin-2",
            cluster_centroid_score=0.9,
            contradictions_detected=False,
            contradiction=None,
            body="b\n",
        )

        eligible, reason = _move_eligibility(entry)

        assert eligible is False
        assert reason == "no contradiction verdict available — not safe to retire"


# ---------------------------------------------------------------------------
# 4. merge.py: conflict_type reaching _pending_questions.md
# ---------------------------------------------------------------------------


class TestConflictTypeReachesPendingQuestions:
    """Pins the C4 escalation's ``conflict_type`` reaching the pending queue.

    Capability-loss register (athenaeum#715 phase 4): the comparator lane
    that the phase-4 plan intends as C4's eventual replacement
    (``verdict_effects._queue_contradiction``) hardcodes
    ``conflict_type="principled"`` for every escalation it writes — it
    carries no real per-conflict classification. This test pins that the
    CURRENT C4 path, by contrast, threads the DETECTOR's own
    ``factual``/``prescriptive`` classification all the way to the rendered
    ``_pending_questions.md`` block. If C4 were deleted and the comparator
    lane silently took over write-side without this classification, every
    escalated block would read ``**Conflict type**: principled`` regardless
    of what kind of conflict was actually detected — a real loss of
    information for whoever triages the queue.

    ``ConflictType`` (``contradictions.py:98``) is a THREE-member Literal —
    ``factual`` / ``prescriptive`` / ``stance`` — but only the first two are
    parametrized here. ``stance`` is deliberately excluded, not missed: a
    detector verdict with ``conflict_type == "stance"`` is caught by
    ``resolutions.py``'s deterministic athenaeum#327 opinion-attribution
    short-circuit (fires whenever neither side is explicitly a non-opinion
    ``claim_kind``, which includes this fixture's unclassified members) and
    resolved as ``attribute_both`` WITHOUT an Opus call — the pair never
    reaches escalation or ``_pending_questions.md`` at all, by design (both
    opinions stay active, non-destructively attributed, rather than being
    surfaced as a live contradiction to triage). See
    :meth:`test_stance_conflict_type_never_reaches_pending_questions` below
    for that path, and ``tests/test_resolutions.py`` for the short-circuit's
    own unit coverage. Reusing this test's assertion shape for ``stance``
    would assert something false about production behaviour.
    """

    @pytest.mark.parametrize("conflict_type", ["factual", "prescriptive"])
    def test_detector_conflict_type_appears_in_pending_questions(
        self, tmp_path: Path, conflict_type: str
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-scope-x"
        _write_am_file(
            scope,
            "feedback_pin_v1.md",
            frontmatter_name="Pin v1",
            body="Always commit directly to develop.",
        )
        _write_am_file(
            scope,
            "feedback_pin_v2.md",
            frontmatter_name="Pin v2",
            body="Never commit directly; always use a branch.",
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "code-pin-0001",
                    "member_paths": [
                        "-scope-x/feedback_pin_v1.md",
                        "-scope-x/feedback_pin_v2.md",
                    ],
                    # Arbitrary value below CONTRADICTION_COHESION_THRESHOLD
                    # (0.75), but that threshold is DEAD in production —
                    # merge_clusters_to_wiki's C4 pass never gates on
                    # centroid_score (verified: raising this to 0.99 still
                    # runs the detector). 0.6 is just a plausible cluster
                    # score for the fixture, not a control on the detector.
                    "centroid_score": 0.6,
                    "rationale": "cosine >= 0.55; shares tokens: commit, develop",
                },
            ],
        )
        _write_config(knowledge_root)

        payload = json.dumps(
            {
                "detected": True,
                "conflict_type": conflict_type,
                "members_involved": [
                    "-scope-x/feedback_pin_v1.md",
                    "-scope-x/feedback_pin_v2.md",
                ],
                "conflicting_passages": [
                    "Always commit directly to develop.",
                    "Never commit directly; always use a branch.",
                ],
                "rationale": "one says always commit, the other says never",
            }
        )
        fake_client = _mock_client(payload)

        entries = merge_clusters_to_wiki(knowledge_root, client=fake_client)

        assert len(entries) == 1
        assert entries[0].contradictions_detected is True

        pending = knowledge_root / "wiki" / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert f"**Conflict type**: {conflict_type}" in text

    def test_stance_conflict_type_never_reaches_pending_questions(
        self, tmp_path: Path
    ) -> None:
        """The third ``ConflictType`` member, ``stance``, is short-circuited
        BEFORE escalation (issue athenaeum#327) — pins that this is what
        currently happens, as the register's counterpart to the
        ``factual``/``prescriptive`` case above.

        Same fixture shape as the parametrized test, ``conflict_type``
        fixed to ``"stance"``: the detector's mocked response still reports
        ``detected: true``, but ``resolutions.py``'s deterministic opinion-
        attribution short-circuit intercepts it before an escalation is
        emitted (see the class docstring). If that short-circuit were ever
        deleted or its engagement gate narrowed, a ``stance`` verdict would
        start reaching ``_pending_questions.md`` like the other two types —
        this test would need to change deliberately, not silently.
        """
        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-scope-x"
        _write_am_file(
            scope,
            "feedback_pin_v1.md",
            frontmatter_name="Pin v1",
            body="The new onboarding flow is great.",
        )
        _write_am_file(
            scope,
            "feedback_pin_v2.md",
            frontmatter_name="Pin v2",
            body="The new onboarding flow is clunky.",
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "code-pin-0002",
                    "member_paths": [
                        "-scope-x/feedback_pin_v1.md",
                        "-scope-x/feedback_pin_v2.md",
                    ],
                    "centroid_score": 0.6,
                    "rationale": "cosine >= 0.55; shares tokens: onboarding, flow",
                },
            ],
        )
        _write_config(knowledge_root)

        payload = json.dumps(
            {
                "detected": True,
                "conflict_type": "stance",
                "members_involved": [
                    "-scope-x/feedback_pin_v1.md",
                    "-scope-x/feedback_pin_v2.md",
                ],
                "conflicting_passages": [
                    "The new onboarding flow is great.",
                    "The new onboarding flow is clunky.",
                ],
                "rationale": "opposing evaluative opinions on the onboarding flow",
            }
        )
        fake_client = _mock_client(payload)

        entries = merge_clusters_to_wiki(knowledge_root, client=fake_client)

        assert len(entries) == 1
        # Suppressed by the athenaeum#327 short-circuit, not escalated.
        assert entries[0].contradictions_detected is False
        assert entries[0].contradiction is not None
        assert entries[0].contradiction.rationale == "confirmation-pass-cleared"

        pending = knowledge_root / "wiki" / "_pending_questions.md"
        if pending.exists():
            text = pending.read_text(encoding="utf-8")
            assert "**Conflict type**: stance" not in text
