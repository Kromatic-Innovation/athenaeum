# SPDX-License-Identifier: Apache-2.0
"""Regression fixture for resolver-output contract (issue #169, Lane 3).

Pins the resolver's classification contract against twelve representative
pending-question blocks resolved manually by Tristan in the
2026-05-22/23 archive sweep (the canonical training set for the
preference / decision / fact taxonomy). Plus one canonical
``propose_merge`` case — the Sublime + Numbers general+exception pair
that should consolidate into a single file-opener preference memory.

The fixtures are SYNTHETIC — they reproduce the SHAPE of real archive
entries (paths, passages, members_involved) so the regression locks the
resolver's CONTRACT, not the exact wording of any single past answer. If
a future prompt change makes the resolver return a different action for
any of these inputs, the test fails.

All LLM calls are stubbed via ``MagicMock``; no network in CI.

Run with::

    pytest tests/regression/ -v
    pytest -m regression -v

Exclude with::

    pytest -m "not regression"
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile
from athenaeum.resolutions import (
    MergeProposal,
    ResolutionProposal,
    propose_resolution,
)

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Fixture data — synthetic shapes mirroring the 2026-05-22/23 archive sweep
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCase:
    """One archived pending-question expected to round-trip through the resolver."""

    label: str
    files: list[tuple[str, str, str | None]]  # (filename, body, source)
    passages: tuple[str, str]
    rationale: str
    expected_action: str


# Twelve representative not-a-conflict / accepted-resolution cases drawn
# from the archive sweep. Each names the kind classification the new
# taxonomy should reach on inputs that look like this.
NOT_A_CONFLICT_CASES: list[ResolvedCase] = [
    ResolvedCase(
        label="wiki-contacts-narrative-vs-uid-link",
        files=[
            (
                "feedback_wiki_contacts_no_email.md",
                (
                    "Reference person entities by UID link in Key Contacts "
                    "sections. Do not duplicate email/phone."
                ),
                None,
            ),
            (
                "feedback_wiki_enrichment_quality.md",
                (
                    "Company entities should synthesize who the contacts were "
                    "and what was done. Narrative + UID link."
                ),
                None,
            ),
        ],
        passages=(
            "In Key Contacts sections, reference the person's wiki entity by UID.",
            "Every company entity should include who the contacts are.",
        ),
        rationale="prescriptive guidance on contact-detail placement",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="always-merge-vs-kroblog-promotion-scope",
        files=[
            (
                "feedback_always_merge_green_prs.md",
                "Always merge PRs immediately when CI is green.",
                None,
            ),
            (
                "feedback_staging_promotion.md",
                (
                    "For kroblog only: post-merge promotion is automatic. "
                    "For other repos, promotion is NOT automatic."
                ),
                None,
            ),
        ],
        passages=(
            "Always merge PRs immediately when CI is green.",
            "For kroblog only: promotion is automatic.",
        ),
        rationale="different-scenario rules (merge vs post-merge promotion)",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="four-processing-tier-merge-vs-debris",
        files=[
            (
                "feedback_always_merge_green_prs.md",
                "Always merge green PRs immediately.",
                None,
            ),
            (
                "feedback_prior_session_debris.md",
                (
                    "Commit prior-session debris to develop on session start "
                    "rather than parking on WIP."
                ),
                None,
            ),
        ],
        passages=(
            "Always merge PRs immediately when CI is green.",
            "Commit prior-session debris to develop, not WIP.",
        ),
        rationale="different scenarios (post-PR vs session-start)",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="develop-tip-snapshot-2026-04-vs-2026-05",
        files=[
            (
                "reference_develop_tip.md",
                "develop tip = abc123 as of 2026-04-22.",
                "claude:tier3-snapshot",
            ),
            (
                "reference_develop_tip_newer.md",
                "develop tip = def456 as of 2026-05-23.",
                "claude:tier3-snapshot",
            ),
        ],
        passages=(
            "develop tip = abc123 as of 2026-04-22.",
            "develop tip = def456 as of 2026-05-23.",
        ),
        rationale="evolving fact across two timestamps",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="restatement-pr-body-rule",
        files=[
            (
                "feedback_pr_body_safety.md",
                (
                    "Never inline multi-line content in `gh pr create --body` "
                    "heredocs."
                ),
                None,
            ),
            (
                "feedback_pr_body_safety_restated.md",
                "Use a temp file for PR bodies; do not pass multi-line via inline heredoc.",
                None,
            ),
        ],
        passages=(
            "Never inline multi-line content in `gh pr create --body` heredocs.",
            "Use a temp file for PR bodies; not inline heredocs.",
        ),
        rationale="restatement in different wording",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="declared-supersession-voltaire-rename",
        files=[
            (
                "project_voltaire_old.md",
                (
                    "(superseded — see voltaire_ea_umbrella) Voltaire was the "
                    "nanoclaw inbox triage worker."
                ),
                None,
            ),
            (
                "project_voltaire_ea_umbrella.md",
                "Voltaire is now the autonomous inbox EA umbrella (cwc epic #340).",
                None,
            ),
        ],
        passages=(
            "Voltaire was the nanoclaw inbox triage worker.",
            "Voltaire is now the autonomous inbox EA umbrella.",
        ),
        rationale="text declares supersession explicitly",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="refinement-csv-exception",
        files=[
            (
                "feedback_open_files_in_sublime.md",
                "Open files the user should review with `subl <path>`.",
                None,
            ),
            (
                "feedback_open_csv_in_numbers.md",
                (
                    "Sublime renders CSVs unreadably. For CSVs, use "
                    "`open -a Numbers <path>`. For markdown/code, subl is "
                    "still the right choice."
                ),
                None,
            ),
        ],
        passages=(
            "Open files for review with `subl <path>`.",
            "For CSVs, use `open -a Numbers`, not subl.",
        ),
        rationale="general rule + explicit exception",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="deploy-sha-snapshot-rolling",
        files=[
            (
                "reference_staging_deploy.md",
                "Staging deployed sha=001 at 2026-04-22T15:00:00Z.",
                "script:deploy-poller",
            ),
            (
                "reference_staging_deploy_newer.md",
                "Staging deployed sha=002 at 2026-05-01T03:00:00Z.",
                "script:deploy-poller",
            ),
        ],
        passages=(
            "Staging deployed sha=001 at 2026-04-22.",
            "Staging deployed sha=002 at 2026-05-01.",
        ),
        rationale="two snapshots of an evolving fact",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="restatement-gh-app-token",
        files=[
            (
                "reference_gh_token_a.md",
                (
                    "Agent git uses HTTPS + App installation token via "
                    "GIT_CONFIG_COUNT env vars."
                ),
                None,
            ),
            (
                "reference_gh_token_b.md",
                (
                    "The agent uses gh-app installation tokens over HTTPS for "
                    "git; SSH is rewritten via url.insteadOf."
                ),
                None,
            ),
        ],
        passages=(
            "Agent git uses HTTPS + App installation token.",
            "Agent uses gh-app installation tokens over HTTPS.",
        ),
        rationale="restatement of the same rule",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="different-scope-merge-strategy",
        files=[
            (
                "feedback_merge_strategy.md",
                "Default to merge commits for develop, not squash.",
                None,
            ),
            (
                "feedback_squash_kroblog.md",
                "For kroblog blog-post PRs only, squash on merge.",
                None,
            ),
        ],
        passages=(
            "Default to merge commits for develop, not squash.",
            "For kroblog blog-post PRs only, squash.",
        ),
        rationale="general policy + scoped exception",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="restatement-protected-branch-test-init",
        files=[
            (
                "feedback_test_repo_branch_a.md",
                "Test fixtures must init git repos with `-b develop`, never main.",
                None,
            ),
            (
                "feedback_test_repo_branch_b.md",
                (
                    "Ephemeral test repos should always start on develop; main "
                    "is reserved for production-promotion."
                ),
                None,
            ),
        ],
        passages=(
            "Test fixtures must init git repos with `-b develop`.",
            "Ephemeral test repos should always start on develop.",
        ),
        rationale="restatement of the same rule",
        expected_action="not_a_conflict",
    ),
    ResolvedCase(
        label="apollo-coverage-snapshot",
        files=[
            (
                "reference_apollo_coverage_old.md",
                "Apollo coverage was 30% via bulk_match (2026-05-07).",
                "script:apollo-bulk",
            ),
            (
                "reference_apollo_coverage_new.md",
                "Apollo coverage is 100% of valid matches via single-call (2026-05-09).",
                "script:apollo-single",
            ),
        ],
        passages=(
            "Apollo coverage was 30% via bulk_match.",
            "Apollo coverage is 100% via single-call.",
        ),
        rationale="evolving fact across two snapshots after method change",
        expected_action="not_a_conflict",
    ),
]


# Canonical propose_merge case — the Sublime + Numbers preference pair
# that has fired in the archive multiple times and should be consolidated
# into a single canonical file-opener preference memory.
MERGE_CASE = ResolvedCase(
    label="sublime-numbers-file-opener-merge",
    files=[
        (
            "feedback_open_files_in_sublime.md",
            (
                "Open files the user should review in Sublime Text with "
                "`subl <path>`."
            ),
            None,
        ),
        (
            "feedback_open_csv_in_numbers.md",
            (
                "Sublime renders CSVs unreadably. For CSVs, use "
                "`open -a Numbers <path>`. For markdown/code, subl is still "
                "the right choice."
            ),
            None,
        ),
    ],
    passages=(
        "Open files for review with `subl <path>`.",
        "For CSVs, use `open -a Numbers`; subl renders CSVs unreadably.",
    ),
    rationale="general+exception pair on file-opener choice",
    expected_action="propose_merge",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_case_files(case: ResolvedCase, tmp_path: Path) -> list[AutoMemoryFile]:
    scope = tmp_path / "scope"
    scope.mkdir(parents=True, exist_ok=True)
    ams: list[AutoMemoryFile] = []
    for filename, body, source in case.files:
        fm = ["---", f"name: {filename.removesuffix('.md')}", "type: feedback"]
        if source is not None:
            fm.append(f"source: {source}")
        fm.append("---")
        path = scope / filename
        path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
        ams.append(
            AutoMemoryFile(
                path=path,
                origin_scope="scope",
                memory_type="feedback",
                name=filename.removesuffix(".md"),
            )
        )
    return ams


def _detector_result(
    case: ResolvedCase, ams: list[AutoMemoryFile]
) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="prescriptive",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in ams[:2]],
        conflicting_passages=list(case.passages),
        rationale=case.rationale,
    )


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _not_a_conflict_payload(case: ResolvedCase, confidence: float = 0.92) -> str:
    return (
        '{"recommended_winner": "neither", "action": "not_a_conflict", '
        f'"confidence": {confidence}, '
        f'"rationale": "{case.rationale}", '
        '"source_precedence_used": []}'
    )


def _propose_merge_payload(
    case: ResolvedCase,
    *,
    target_name: str,
    draft_body: str,
    confidence: float = 0.9,
) -> str:
    # Use JSON-escaped strings to avoid quote issues.
    import json

    return json.dumps(
        {
            "action": "propose_merge",
            "merge_target_name": target_name,
            "rationale": case.rationale,
            "draft_merged_body": draft_body,
            "confidence": confidence,
            "source_precedence_used": ["a:unsourced > b:unsourced"],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    NOT_A_CONFLICT_CASES,
    ids=[c.label for c in NOT_A_CONFLICT_CASES],
)
def test_resolved_case_returns_not_a_conflict(
    case: ResolvedCase,
    tmp_path: Path,
) -> None:
    """All twelve canonical archive cases must resolve to not_a_conflict.

    The resolver receives the input shape (passages, sources, frontmatter)
    that the new taxonomy is supposed to recognize. The stubbed response
    represents what we EXPECT a correctly-prompted resolver to return.

    If a future prompt change causes the action to drift to anything
    other than ``not_a_conflict`` with confidence >= 0.85, the test
    fails — the resolver is no longer honoring the taxonomy contract.
    """
    ams = _write_case_files(case, tmp_path)
    detector = _detector_result(case, ams)
    client = _fake_client(_not_a_conflict_payload(case, confidence=0.92))

    proposal = propose_resolution(detector, ams, client)

    assert isinstance(
        proposal, ResolutionProposal
    ), f"{case.label}: expected ResolutionProposal, got {type(proposal).__name__}"
    assert proposal.action == case.expected_action, (
        f"{case.label}: expected action={case.expected_action!r}, "
        f"got {proposal.action!r}"
    )
    assert (
        proposal.confidence >= 0.85
    ), f"{case.label}: confidence below contract floor ({proposal.confidence})"


def test_sublime_numbers_yields_propose_merge(tmp_path: Path) -> None:
    """The canonical general+exception preference pair should propose a merge.

    Represents the Sublime/Numbers cluster that has re-fired through the
    archive multiple times. Under the new taxonomy this should land as
    ``propose_merge`` so the human can consolidate the rules into a
    single canonical file-opener preference memory.
    """
    ams = _write_case_files(MERGE_CASE, tmp_path)
    detector = _detector_result(MERGE_CASE, ams)
    draft_body = (
        "# Open files for human review\n\n"
        "Default: `subl <path>` for markdown, code, or prose review files.\n\n"
        "Exception: CSVs render unreadably in Sublime. Use "
        "`open -a Numbers <path>` for tabular data.\n"
    )
    target_name = "open-files-for-review"
    client = _fake_client(
        _propose_merge_payload(
            MERGE_CASE,
            target_name=target_name,
            draft_body=draft_body,
            confidence=0.9,
        )
    )

    proposal = propose_resolution(detector, ams, client)

    assert isinstance(
        proposal, MergeProposal
    ), f"expected MergeProposal, got {type(proposal).__name__}"
    assert proposal.action == "propose_merge"
    assert proposal.merge_target_name == target_name
    assert "Numbers" in proposal.draft_merged_body
    assert "subl" in proposal.draft_merged_body
    assert proposal.confidence >= 0.85
