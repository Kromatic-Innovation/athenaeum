# SPDX-License-Identifier: Apache-2.0
"""Tests for the fold-question apply path (issue athenaeum#2518).

Before this fix, EVERY answer to a fold question (a `duplicate`-verdict
question raised by `athenaeum.verdict_effects._apply_duplicate` with a
`comparator:<pair_key>` source ref) landed in the `**Write-back**: held`
state forever: the ref never resolves to a file, so
`athenaeum.answers._writeback_source` could never act on it regardless of
the answer. Over 1,100 real answers were recorded this way and nothing
ever folded (issue athenaeum#2518's reported incident).

The fix routes a fold question's answer through
`athenaeum._cmd_pending._resolve_fold_question` instead -- wired into
`athenaeum.answers.ingest_answers` as its `fold_applier` callback -- which
hands the pair to the normal merge machinery
(`athenaeum.pending_merges.write_pending_merge` +
`athenaeum.pending_merges.resolve_merge`) so the fold applies immediately,
in the same `ingest-answers` pass, with the SAME safety checks any other
merge is subject to.

Synthetic fixture pages only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum._cmd_pending import _resolve_fold_question
from athenaeum.answers import IngestAnswersReport, ingest_answers
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import parse_pending_merges
from tests.conftest import init_git_repo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_page(path: Path, *, name: str, body: str) -> None:
    path.write_text(
        f"---\nname: {name}\ntype: concept\n---\n{body}\n", encoding="utf-8"
    )


def _fold_block(
    *,
    pair_key: str = "canonical-page+other-page",
    canonical_id: str = "canonical-page",
    checkbox: str = "[x]",
    answer: str = "approve",
) -> str:
    """A fold question block shaped exactly like
    `verdict_effects._apply_duplicate` -> `_queue` -> `tiers.tier4_escalate`
    renders one: `**Conflict type**: duplicate`, a `comparator:<pair_key>`
    source ref, and a description ending "Proposed canonical side: <side>
    (<id>)." -- the one surviving record of which side was proposed
    canonical, which `_resolve_fold_question` recovers via regex.
    """
    description = (
        'Approve folding "Canonical Page" and "Other Page" into one page? '
        "Approving folds the two pages immediately (issue athenaeum#2518), "
        "through the normal merge path and its existing safety checks -- "
        "no page body is synthesized here first; see the structural "
        "overlap evidence at /tmp/evidence.md. Proposed canonical side: "
        f"a ({canonical_id})."
    )
    block = (
        '## [2026-11-13] Entity: "Canonical Page vs Other Page" '
        f"(from comparator:{pair_key})\n"
        f"- {checkbox} {description}\n\n"
        "**Conflict type**: duplicate\n"
        f"**Description**: {description}\n"
    )
    if answer:
        block += f"\n{answer}\n"
    return block


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir()
    return w


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def _setup_pages(wiki: Path) -> None:
    _write_page(wiki / "canonical-page.md", name="Canonical Page", body="canonical body")
    _write_page(wiki / "other-page.md", name="Other Page", body="other body")


def _run(wiki: Path, raw_root: Path, *, answer: str, report: IngestAnswersReport) -> int:
    pending_path = wiki / "_pending_questions.md"
    pending_path.write_text("# Pending Questions\n\n" + _fold_block(answer=answer))
    return ingest_answers(
        pending_path,
        raw_root,
        report=report,
        fold_applier=lambda pq: _resolve_fold_question(pq, wiki_root=wiki, config=None),
    )


# ---------------------------------------------------------------------------
# AC1 + AC2 — approve folds immediately, in one tick, with no second question
# ---------------------------------------------------------------------------


class TestFoldApprove:
    def test_approve_folds_immediately_in_one_tick(
        self, wiki: Path, raw_root: Path
    ) -> None:
        _setup_pages(wiki)
        init_git_repo(wiki)
        report = IngestAnswersReport()
        count = _run(wiki, raw_root, answer="approve", report=report)

        assert count == 1
        other_meta, _ = parse_frontmatter(
            (wiki / "other-page.md").read_text(encoding="utf-8")
        )
        assert other_meta.get("status") == "folded"
        assert other_meta.get("folded_into") == "canonical-page"
        canonical_text = (wiki / "canonical-page.md").read_text(encoding="utf-8")
        assert "canonical body" in canonical_text

        archive = (wiki / "_pending_questions_archive.md").read_text(encoding="utf-8")
        assert "**Write-back**: folded" in archive
        assert "held" not in archive.lower()

        assert report.folds_applied == 1
        assert report.held == 0

    def test_approve_raises_no_new_pending_merge_question(
        self, wiki: Path, raw_root: Path
    ) -> None:
        _setup_pages(wiki)
        init_git_repo(wiki)
        report = IngestAnswersReport()
        _run(wiki, raw_root, answer="approve", report=report)

        pending_path = wiki / "_pending_questions.md"
        # No unanswered fold question left behind.
        assert "- [ ]" not in pending_path.read_text(encoding="utf-8")

        # The merge proposal this run queued through the normal merge path
        # is ALREADY resolved -- never a second open question for a human
        # to separately approve.
        merges_path = wiki / "_pending_merges.md"
        proposals = parse_pending_merges(merges_path)
        assert len(proposals) == 1
        assert proposals[0].resolved is True
        assert proposals[0].decision == "approve"


# ---------------------------------------------------------------------------
# AC3 — a safety check the merge path applies refuses; nothing folds
# ---------------------------------------------------------------------------


class TestFoldSafetyCheckRefusal:
    def test_no_git_repo_refuses_and_folds_nothing(
        self, wiki: Path, raw_root: Path
    ) -> None:
        _setup_pages(wiki)
        # Deliberately NOT git-initialized: trips `resolve_merge`'s
        # `no_git_repo` gate, the SAME safety check any other
        # fold-into-existing merge is subject to -- reused verbatim, not
        # reimplemented.
        report = IngestAnswersReport()
        count = _run(wiki, raw_root, answer="approve", report=report)

        assert count == 1
        other_meta, _ = parse_frontmatter(
            (wiki / "other-page.md").read_text(encoding="utf-8")
        )
        assert other_meta.get("status") != "folded"
        canonical_text = (wiki / "canonical-page.md").read_text(encoding="utf-8")
        assert canonical_text == (
            "---\nname: Canonical Page\ntype: concept\n---\ncanonical body\n"
        )

        archive = (wiki / "_pending_questions_archive.md").read_text(encoding="utf-8")
        assert "**Write-back**: refused" in archive
        assert "no_git_repo" in archive
        assert "held" not in archive.lower()
        assert report.folds_refused == 1
        assert report.held == 0


# ---------------------------------------------------------------------------
# AC4 — approve OR reject, never held
# ---------------------------------------------------------------------------


class TestFoldReject:
    def test_reject_never_held_and_folds_nothing(
        self, wiki: Path, raw_root: Path
    ) -> None:
        _setup_pages(wiki)
        init_git_repo(wiki)
        report = IngestAnswersReport()
        count = _run(wiki, raw_root, answer="reject", report=report)

        assert count == 1
        other_meta, _ = parse_frontmatter(
            (wiki / "other-page.md").read_text(encoding="utf-8")
        )
        assert other_meta.get("status") != "folded"

        canonical_meta, _ = parse_frontmatter(
            (wiki / "canonical-page.md").read_text(encoding="utf-8")
        )
        rejected_with = canonical_meta.get("merge_rejected_with") or []
        assert "Other Page" in rejected_with

        archive = (wiki / "_pending_questions_archive.md").read_text(encoding="utf-8")
        assert "**Write-back**: rejected" in archive
        assert "held" not in archive.lower()
        assert report.folds_rejected == 1
        assert report.held == 0

    def test_unrecognized_answer_is_held_not_folded_or_rejected(
        self, wiki: Path, raw_root: Path
    ) -> None:
        """Only a well-formed approve/reject is guaranteed never to hold
        (AC4). Free text that is neither token is still held -- same
        never-guess posture `_apply_field_correction` already takes for an
        unrecognized field-correction answer."""
        _setup_pages(wiki)
        init_git_repo(wiki)
        report = IngestAnswersReport()
        count = _run(wiki, raw_root, answer="maybe later", report=report)

        assert count == 0
        pending_path = wiki / "_pending_questions.md"
        assert "**Write-back**: held" in pending_path.read_text(encoding="utf-8")
        assert report.held == 1
        assert report.folds_applied == 0
        assert report.folds_rejected == 0
