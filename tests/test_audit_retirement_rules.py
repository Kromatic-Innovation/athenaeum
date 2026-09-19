# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1849 — retirement prompt keeps name-only stubs and
affiliation pages; flags CRM-metadata-only pages.

Two things this module pins, and one it deliberately does NOT:

- ``TestPromptStatesEachRule`` asserts a distinguishing substring for each
  of the four sub-rules added to ``AUDIT_SYSTEM``'s RETIREMENT CANDIDACY
  section, so a future prompt edit cannot silently drop one.
- ``TestMotivationTableFixtures`` carries one synthetic fixture page per
  row of the issue's Motivation table, each driven through
  :func:`build_audit_report` with a :class:`FakeLLMClient` responder that
  returns that row's verdict verbatim. These tests pin PARSING and
  REPORTING per rule — that a verdict the model returns lands correctly
  in the :class:`~athenaeum.audit.AuditReport` as ``retirement_candidate``
  / ``retirement_reason`` — they do NOT exercise or judge the model's own
  reasoning (no live LLM call is made anywhere in this module).

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
