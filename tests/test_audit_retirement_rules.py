# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1849 — retirement prompt keeps name-only stubs and
affiliation pages; flags CRM-metadata-only pages.

Reverted for issue athenaeum#1877: ``AUDIT_SYSTEM``'s section 2 is back
to its pre-athenaeum#1869 (``audit-v3``) wording, shipped under the new
version string ``audit-v5``. athenaeum#1869 rewrote the section into
``audit-v4`` on a structural argument — the placeholder exemption sat
nested under a trigger that contradicted it — but shipped the rewrite
unmeasured, because that lane had no live model backend. The
measurement arrived afterwards and disproved the premise; see
``TestPromptStructure`` below for the numbers.

Three things this module pins, and one it deliberately does NOT:

- ``TestPromptStatesEachRule`` asserts a distinguishing substring for each
  of the four sub-rules in ``AUDIT_SYSTEM``'s RETIREMENT CANDIDACY
  section, so a future prompt edit cannot silently drop one.
- ``TestPromptStructure`` pins the restored ``audit-v3`` shape and records
  the A/B that justifies it, so a future edit back toward ``audit-v4``'s
  structure has to be re-measured rather than re-argued.
- ``TestMotivationTableFixtures`` carries one synthetic fixture page per
  row of the issue's Motivation table, each driven through
  :func:`build_audit_report` with a :class:`FakeLLMClient` responder that
  returns that row's verdict verbatim. These tests pin PARSING and
  REPORTING per rule — that a verdict the model returns lands correctly
  in the :class:`~athenaeum.audit.AuditReport` as ``retirement_candidate``
  / ``retirement_reason`` — they do NOT exercise or judge the model's own
  reasoning (no live LLM call is made anywhere in this module; the
  recorded eval layer under ``tests/evals/`` is what exercises the real
  prompt against the real model).

Kept as a separate module from ``tests/test_audit.py`` and
``tests/test_audit_1667.py`` so this issue's acceptance criteria map to
one file. All fixtures live under ``tmp_path``; nothing here reads or
writes a live knowledge store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum.audit import AUDIT_SYSTEM, build_audit_report
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage


def _page(root: Path, name: str, frontmatter: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


def _client(responder) -> FakeLLMClient:
    def wrapped(**params: Any) -> Any:
        return make_llm_response(responder(params), usage=make_llm_usage(10, 5))

    return FakeLLMClient(responder=wrapped)


# --- The prompt states each of the four sub-rules --------------------------


class TestPromptStatesEachRule:
    def test_rule_1_placeholder_exemption(self) -> None:
        assert "placeholder awaiting enrichment" in AUDIT_SYSTEM

    def test_rule_2_dated_outcome_is_a_claim(self) -> None:
        assert "recording a dated engagement or relationship outcome states a claim" in AUDIT_SYSTEM

    def test_rule_3_pipeline_metadata_only_is_a_candidate(self) -> None:
        assert "pipeline-list membership" in AUDIT_SYSTEM

    def test_rule_4_self_declared_spurious_entity_is_a_candidate(self) -> None:
        assert "artifact of parsing a filename" in AUDIT_SYSTEM

    def test_precedence_sentence_present(self) -> None:
        assert "at most one affiliation line" in AUDIT_SYSTEM
        assert "falls under the pipeline-metadata rule, not the placeholder rule" in AUDIT_SYSTEM


# --- The restored audit-v3 structure (issue athenaeum#1877) ----------------


class TestPromptStructure:
    """Pins the section-2 structure this prompt actually ships — ``audit-v5``,
    which restores ``audit-v3``'s wording byte for byte.

    athenaeum#1869 replaced that wording with ``audit-v4`` on the argument
    that the placeholder exemption was nested under a trigger contradicting
    it ("states no claim at all ... just a name/heading or nothing"), and a
    live 50-page dry run had flagged 7 of 7 bare name-only person stubs.
    The argument was never checked against a model: that lane had no live
    backend. The check arrived afterwards, two samples per arm on
    ``claude-haiku-4-5-20251001`` over
    ``tests/evals/data/audit_retirement/cases.yaml`` (floor 5 of 6), and it
    disagreed — ``audit-v3`` scored 5/6 twice, ``audit-v4`` 2/6 and 3/6.
    ``audit-v4`` did not fix the case it was written for (the bare stub is
    flagged under both wordings) and it introduced two false positives
    reproducible across both samples that ``audit-v3`` does not have:
    ``name_plus_one_affiliation_line`` and
    ``dated_engagement_with_pipeline_metadata``. Both are real content, and
    neither is covered by any code-side override, so ``audit-v4`` made
    ``athenaeum audit``'s retirement list less actionable, not more.

    The bare-stub case that motivated athenaeum#1869 stays covered on the
    live path by the deterministic ``audit._is_bare_person_stub`` override,
    which athenaeum#1869 also shipped and this revert keeps — see
    ``tests/test_audit_1869.py``. Neither prompt version passes that case on
    its own.

    These assertions are the inverse of the ones athenaeum#1869 added. They
    exist so the next edit toward ``audit-v4``'s structure is a measured
    change rather than an argued one.
    """

    def test_no_claim_trigger_names_a_bare_heading(self) -> None:
        # Restored deliberately. It reads as a contradiction against the
        # placeholder sub-bullet below it, and removing it scored WORSE on
        # every case except the one it was aimed at, which it did not fix.
        assert "just a name/heading or nothing" in AUDIT_SYSTEM
        assert "it states no claim at all" in AUDIT_SYSTEM

    def test_exclusions_are_stated_as_sub_bullets_under_the_no_claim_trigger(self) -> None:
        # audit-v4 hoisted these to top-level exclusions stated before the
        # triggers. That reordering is what the A/B measured as a
        # regression, so the nesting is restored.
        no_claim_trigger = AUDIT_SYSTEM.index("it states no claim at all")
        placeholder_exemption = AUDIT_SYSTEM.index("is a placeholder awaiting enrichment")
        assert no_claim_trigger < placeholder_exemption
        assert "Three exclusions apply first" not in AUDIT_SYSTEM

    def test_pipeline_metadata_and_spurious_entity_rules_are_nested_sub_bullets(self) -> None:
        # Five-space indent ("     - "), not audit-v4's three ("   - ").
        assert "     - a page whose only content beyond its name is CRM" in AUDIT_SYSTEM
        assert "     - a page whose own text says the entity itself is spurious" in AUDIT_SYSTEM

    def test_output_example_shows_true_with_a_reason_placeholder(self) -> None:
        # The measured audit-v3 arm was the WHOLE AUDIT_SYSTEM constant
        # byte-equal to its pre-athenaeum#1869 state, output-shape example
        # included, so the example reverts with the section it was measured
        # alongside. Shipping audit-v4's `false` example on top of
        # audit-v3's section 2 would be a third wording nobody has measured.
        assert '"retirement_candidate": true,' in AUDIT_SYSTEM
        assert '"retirement_reason": "<one-line reason, empty string when false>"' in AUDIT_SYSTEM


# --- Motivation-table fixtures: synthetic pages, one per row ---------------


def _name_only_page(root: Path) -> Path:
    return _page(
        root,
        "dana.md",
        "uid: dana1\ntype: person\nname: Dana Example\n",
        "# Dana Example\n",
    )


def _affiliation_only_page(root: Path) -> Path:
    return _page(
        root,
        "alex.md",
        "uid: alex1\ntype: person\nname: Alex Sample\n",
        "# Alex Sample\nWorks at Example Widgets Ltd.\n",
    )


def _dated_engagement_with_pipeline_metadata_page(root: Path) -> Path:
    return _page(
        root,
        "engagement.md",
        "uid: engagement1\ntype: company\nname: Example Widgets Ltd.\n"
        "pipeline_stage: closed-lost\ndeal_status: dormant\n",
        "Engagement with Example Widgets Ltd. ended without a contract in "
        "2019-11 after first contact in 2019-08.\n",
    )


def _pipeline_metadata_only_page(root: Path) -> Path:
    return _page(
        root,
        "prospect.md",
        "uid: prospect1\ntype: company\nname: Sample Prospect Co\n"
        "pipeline_stage: qualifying\n",
        "# Sample Prospect Co\nAppears in the Q3 outreach list.\n",
    )


def _spurious_entity_page(root: Path) -> Path:
    return _page(
        root,
        "spurious.md",
        "uid: spurious1\ntype: company\nname: Untitled-42-Final-v3\n",
        "# Untitled-42-Final-v3\nThis entity was created by mis-parsing an "
        "imported filename and does not name a real organization.\n",
    )


class TestMotivationTableFixtures:
    """Each test's responder returns exactly the operator-adjudicated
    verdict from the issue's Motivation table for that page shape; the
    assertions confirm the verdict lands in the report, not that the model
    would produce it (see module docstring)."""

    def test_name_only_person_page_is_kept(self, wiki: Path) -> None:
        _name_only_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_name_plus_single_affiliation_line_page_is_kept(self, wiki: Path) -> None:
        _affiliation_only_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_dated_engagement_outcome_with_pipeline_metadata_is_kept(self, wiki: Path) -> None:
        _dated_engagement_with_pipeline_metadata_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_pipeline_metadata_only_page_is_flagged(self, wiki: Path) -> None:
        _pipeline_metadata_only_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": True,
                    "retirement_reason": "only content is pipeline-list membership",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is True
        assert v.retirement_reason

    def test_self_declared_spurious_entity_page_is_flagged(self, wiki: Path) -> None:
        _spurious_entity_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": True,
                    "retirement_reason": "page's own text says this is a mis-parsed filename",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is True
        assert v.retirement_reason
