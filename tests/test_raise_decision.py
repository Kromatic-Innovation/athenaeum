# SPDX-License-Identifier: Apache-2.0
"""Tests for the agent-side raise path into the pending-decisions queue (issue athenaeum#912).

Before this, ``_pending_questions.md`` had exactly one writer — athenaeum's
own detectors (``tier4_escalate``) — so an agent that discovered something
needing a human decision had no way to file it. This module proves the three
acceptance criteria from the issue, each mapped to a test class below:

- ``TestRaisePendingQuestionUnit`` -> AC1 (validation) + AC3 (provenance is
  recorded on write). A raise/insert path exists; empty question/context is
  refused, never silently accepted.
- ``TestCrossSessionSurfacing`` -> AC2, the load-bearing one: an item raised
  by one session is observed in a DIFFERENT session's ``list_pending_decisions``
  — proved by constructing a completely FRESH ``create_server(...)`` instance
  for the read, mirroring the write-in-A / observe-in-fresh-B shape of
  ``tests/test_session_end.py::TestCrossAgentRecall``. Nothing here reads the
  write path to infer the outcome; it is observed by running.
- ``TestProvenanceDistinguishable`` -> AC3 (provenance is distinguishable):
  an agent-raised item and a detector-raised item coexist in
  ``list_pending_decisions`` output with different ``payload["raised_by"]``.
- ``TestResolveThroughExistingPath`` -> AC3 (resolution): an agent-raised
  item resolves through the EXISTING ``resolve_question`` MCP tool ->
  ``athenaeum ingest-answers`` tick (``apply_decision_answers``) pipeline
  with zero special-casing — the exact same call chain a detector-raised
  item resolves through.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum.answers import (
    list_unanswered,
    parse_pending_questions,
    raise_pending_question,
)
from athenaeum.decision_answers import apply_decision_answers
from athenaeum.decisions import list_pending_decisions

# ---------------------------------------------------------------------------
# Server helpers (mirrors tests/test_mcp_server.py's TestAllMcpToolWrappers)
# ---------------------------------------------------------------------------


def _server(tmp_path: Path, **kwargs: object):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    return create_server(raw_root=raw, wiki_root=wiki, **kwargs)  # type: ignore[arg-type]


def _call(server, name: str, caller):
    async def _run():
        tool = await server.get_tool(name)
        return caller(tool.fn)

    return asyncio.run(_run())


_DETECTOR_BLOCK = (
    "# Pending Questions\n\n"
    '## [2026-04-20] Entity: "Acme Corp" (from sessions/x.md)\n'
    "- [ ] Is Acme still Series A?\n"
    "**Conflict type**: principled\n"
    "**Description**: Prior wiki says Series A; new raw implies Series B.\n"
)


# ---------------------------------------------------------------------------
# AC1 + input validation: raise_pending_question itself
# ---------------------------------------------------------------------------


class TestRaisePendingQuestionUnit:
    def test_rejects_empty_question(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(pending, "   ", "plenty of context")
        assert result["ok"] is False
        assert result["error_code"] == "invalid_question"
        assert result["decision_id"] is None
        assert not pending.exists()

    def test_rejects_whitespace_only_context(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(pending, "Did you mean X?", "   \n  ")
        assert result["ok"] is False
        assert result["error_code"] == "missing_context"
        assert result["decision_id"] is None
        assert not pending.exists()

    def test_rejects_empty_context_outright(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(pending, "Did you mean X?", "")
        assert result["ok"] is False
        assert result["error_code"] == "missing_context"

    def test_happy_path_creates_file_with_provenance(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(
            pending,
            "Did you mean the stricter reading?",
            "Contact-sync fix narrowed the scope to scalar fields; confirm "
            "or override without re-reading the session transcript.",
            entity="contact-sync email scoping",
        )
        assert result["ok"] is True
        assert result["error_code"] is None
        assert result["decision_id"]
        assert result["raw_block"] is not None
        assert result["block"] == result["raw_block"]  # legacy alias

        items = list_unanswered(pending)
        assert len(items) == 1
        item = items[0]
        assert item["id"] == result["decision_id"]
        assert item["raised_by"] == "agent"
        assert item["question"] == "Did you mean the stricter reading?"
        assert "scalar fields" in item["description"]
        assert item["entity"] == "contact-sync email scoping"

    def test_default_entity_and_source_when_omitted(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(pending, "Flag?", "context body")
        assert result["ok"] is True
        pq = parse_pending_questions(pending)[0]
        assert pq.entity  # non-empty fallback label
        assert pq.source  # non-empty fallback ref
        assert pq.raised_by == "agent"

    def test_appends_after_existing_detector_block(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        pending.write_text(_DETECTOR_BLOCK, encoding="utf-8")

        result = raise_pending_question(pending, "New flag?", "standalone context")
        assert result["ok"] is True

        items = list_unanswered(pending)
        assert len(items) == 2
        by_entity = {i["entity"]: i for i in items}
        assert by_entity["Acme Corp"]["raised_by"] == ""  # detector item unaffected
        assert result["decision_id"] in {i["id"] for i in items}

    def test_decision_id_matches_reparse_off_disk(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(
            pending,
            "Q?",
            "context",
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
        reparsed = parse_pending_questions(pending)
        assert len(reparsed) == 1
        assert reparsed[0].id == result["decision_id"]


# ---------------------------------------------------------------------------
# AC2 (the load-bearing one): survives across a FRESH server instance
# ---------------------------------------------------------------------------


class TestCrossSessionSurfacing:
    """Write via one server instance; read via a completely fresh one.

    This is the entire point of building on the file-backed sidecar rather
    than an in-memory structure — surviving the session is the acceptance
    criterion, and it is verified here by RUNNING two independent
    ``create_server(...)`` calls, never by reading the write path and
    reasoning about it.
    """

    def test_raised_via_one_instance_visible_in_a_fresh_instance(
        self, tmp_path: Path
    ) -> None:
        # "Session A": raise through one server instance.
        server_a = _server(tmp_path)
        raised = _call(
            server_a,
            "raise_decision",
            lambda fn: fn(
                "Did you mean the stricter reading?",
                "Contact-sync fix narrowed scope to scalar fields; confirm "
                "or override.",
            ),
        )
        assert raised["ok"] is True
        decision_id = raised["decision_id"]

        # "Session B": a BRAND NEW server object built from the same on-disk
        # root — it shares no Python object, only the filesystem, with
        # server_a.
        server_b = _server(tmp_path)
        assert server_b is not server_a
        # Issue athenaeum#1431: `list_pending_decisions` now returns a bounded
        # envelope (`{"items", "total", "offset", "limit", "next_offset"}`)
        # rather than a bare list.
        decisions = _call(server_b, "list_pending_decisions", lambda fn: fn())["items"]
        ids = [d["id"] for d in decisions]
        assert decision_id in ids

        item = next(d for d in decisions if d["id"] == decision_id)
        assert item["type"] == "question"
        assert item["payload"]["raised_by"] == "agent"

        # list_pending_questions (the narrower tool) surfaces it too, from
        # yet another fresh instance. Issue athenaeum#1431: also a bounded
        # envelope now, not a bare list.
        server_c = _server(tmp_path)
        questions = _call(server_c, "list_pending_questions", lambda fn: fn())["items"]
        assert any(q["id"] == decision_id for q in questions)


# ---------------------------------------------------------------------------
# AC3a: provenance distinguishes agent-raised from detector-raised
# ---------------------------------------------------------------------------


class TestProvenanceDistinguishable:
    def test_agent_raised_and_detector_raised_coexist_distinguishably(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        pending.write_text(_DETECTOR_BLOCK, encoding="utf-8")

        raise_pending_question(pending, "Agent flag?", "standalone context")

        decisions = list_pending_decisions(wiki)
        questions = [d for d in decisions if d["type"] == "question"]
        assert len(questions) == 2
        by_provenance = {d["payload"]["raised_by"]: d for d in questions}
        assert set(by_provenance) == {"", "agent"}
        assert by_provenance[""]["payload"]["entity"] == "Acme Corp"
        assert by_provenance["agent"]["summary"] == "Agent flag?"


# ---------------------------------------------------------------------------
# AC3b: resolving an agent-raised item works through the EXISTING resolve
# path, with no special-casing.
# ---------------------------------------------------------------------------


class TestResolveThroughExistingPath:
    def test_resolve_question_tool_and_ingest_tick_resolve_agent_raised_item(
        self, tmp_path: Path
    ) -> None:
        server = _server(tmp_path)
        raised = _call(
            server,
            "raise_decision",
            lambda fn: fn("Flag?", "standalone context for a later reader"),
        )
        decision_id = raised["decision_id"]

        # Resolve through the EXISTING resolve_question MCP tool — the exact
        # same tool a detector-raised item resolves through, no branching on
        # provenance anywhere in this call.
        res = _call(
            server,
            "resolve_question",
            lambda fn: fn(decision_id, "Yes — stricter reading confirmed."),
        )
        assert res["ok"] is True
        assert res["deferred"] is True  # athenaeum#908: applied on the next tick, not now
        assert res["decision_id"] == decision_id

        pending = tmp_path / "wiki" / "_pending_questions.md"
        raw_root = tmp_path / "raw"

        # Still open until the tick — same deferred-apply contract a
        # detector-raised item has.
        assert any(pq["id"] == decision_id for pq in list_unanswered(pending))

        # The `athenaeum ingest-answers` tick applies it via
        # apply_decision_answers, which dispatches decision_type="question"
        # to athenaeum.answers.resolve_by_id — the SAME function a
        # detector-raised item resolves through. Zero special-casing for
        # agent-raised items anywhere in this dispatch.
        report = apply_decision_answers(wiki_root=tmp_path / "wiki", raw_root=raw_root)
        assert report.applied == 1
        assert list_unanswered(pending) == []

        # The block itself is flipped to [x] and closed, exactly like any
        # other resolved question.
        resolved = parse_pending_questions(pending)
        assert len(resolved) == 1
        assert resolved[0].answered is True
        assert resolved[0].raised_by == "agent"  # provenance survives resolution

    def test_resolve_removes_agent_raised_exactly_like_detector_raised(
        self, tmp_path: Path
    ) -> None:
        """Both provenances go through identical resolve_by_id mechanics."""
        from athenaeum.answers import resolve_by_id

        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        pending.write_text(_DETECTOR_BLOCK, encoding="utf-8")
        raised = raise_pending_question(pending, "Flag?", "standalone context")

        detector_id = parse_pending_questions(pending)[0].id
        agent_id = raised["decision_id"]

        r1 = resolve_by_id(pending, detector_id, "Confirmed Series B.")
        r2 = resolve_by_id(pending, agent_id, "Confirmed stricter reading.")
        assert r1["ok"] is True
        assert r2["ok"] is True

        # Both are now closed; list_unanswered no longer surfaces either.
        assert list_unanswered(pending) == []


# ---------------------------------------------------------------------------
# Issue athenaeum#1290: the "confirmation" decision type — an agent-raiseable
# "implemented X without Y, confirm?" flag, extending raise_decision /
# raise_pending_question (per AC1: extend the existing tool, since it exists
# but did not carry the required structured fields) rather than adding a
# parallel type or storage mechanism.
# ---------------------------------------------------------------------------


_CONFIRMATION_KWARGS = {
    "raiser": "dijkstra-lane-1290",
    "repo": "Kromatic-Innovation/athenaeum",
    "issue_ref": "1290",
    "narrowed_scope": "only scalar fields",
    "implemented_behavior": "extended raise_decision with a kind param",
    "alternative": "a brand new raise_confirmation tool",
}


class TestConfirmationValidation:
    def test_rejects_unknown_kind(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(pending, "Q?", "C", kind="bogus")
        assert result["ok"] is False
        assert result["error_code"] == "invalid_kind"
        assert not pending.exists()

    @pytest.mark.parametrize("missing_field", list(_CONFIRMATION_KWARGS))
    def test_rejects_missing_confirmation_field(
        self, tmp_path: Path, missing_field: str
    ) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        kwargs = dict(_CONFIRMATION_KWARGS)
        kwargs[missing_field] = "   "  # whitespace-only, same as empty
        result = raise_pending_question(pending, "", "", kind="confirmation", **kwargs)
        assert result["ok"] is False
        assert result["error_code"] == "missing_confirmation_field"
        assert not pending.exists()

    def test_plain_question_kind_is_byte_for_byte_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Omitting `kind` (or passing "question") behaves exactly as before athenaeum#1290."""
        pending_a = tmp_path / "a.md"
        pending_b = tmp_path / "b.md"
        r_default = raise_pending_question(
            pending_a, "Q?", "C", now=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        r_explicit = raise_pending_question(
            pending_b,
            "Q?",
            "C",
            kind="question",
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        assert r_default["raw_block"] == r_explicit["raw_block"]
        assert r_default["decision_id"] == r_explicit["decision_id"]


class TestConfirmationRaiseAndSurface:
    def test_happy_path_carries_all_required_fields(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(
            pending,
            "",  # auto-phrased — not a required confirmation field
            "",
            kind="confirmation",
            now=datetime(2026, 9, 2, 9, 50, tzinfo=timezone.utc),
            **_CONFIRMATION_KWARGS,
        )
        assert result["ok"] is True
        assert result["decision_id"]

        pq = parse_pending_questions(pending)[0]
        assert pq.decision_kind == "confirmation"
        assert pq.raiser == _CONFIRMATION_KWARGS["raiser"]
        assert pq.repo == _CONFIRMATION_KWARGS["repo"]
        assert pq.issue_ref == _CONFIRMATION_KWARGS["issue_ref"]
        assert pq.narrowed_scope == _CONFIRMATION_KWARGS["narrowed_scope"]
        assert pq.implemented_behavior == _CONFIRMATION_KWARGS["implemented_behavior"]
        assert pq.alternative == _CONFIRMATION_KWARGS["alternative"]
        assert pq.raised_at == "2026-09-02T09:50:00Z"
        assert pq.raised_by == "agent"
        # question/context were auto-phrased, never left blank on disk.
        assert pq.question.strip()
        assert pq.description.strip()

    def test_explicit_question_and_context_are_preserved(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True)
        result = raise_pending_question(
            pending,
            "Should the narrower reading stand?",
            "Full standalone context for a later reader.",
            kind="confirmation",
            **_CONFIRMATION_KWARGS,
        )
        assert result["ok"] is True
        pq = parse_pending_questions(pending)[0]
        assert pq.question == "Should the narrower reading stand?"
        assert pq.description == "Full standalone context for a later reader."

    def test_list_pending_decisions_tags_type_confirmation(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        raise_pending_question(pending, "", "", kind="confirmation", **_CONFIRMATION_KWARGS)

        decisions = list_pending_decisions(wiki)
        assert len(decisions) == 1
        item = decisions[0]
        assert item["type"] == "confirmation"
        assert item["confidence"] is None
        payload = item["payload"]
        assert payload["raiser"] == _CONFIRMATION_KWARGS["raiser"]
        assert payload["repo"] == _CONFIRMATION_KWARGS["repo"]
        assert payload["issue_ref"] == _CONFIRMATION_KWARGS["issue_ref"]
        assert payload["narrowed_scope"] == _CONFIRMATION_KWARGS["narrowed_scope"]
        assert (
            payload["implemented_behavior"]
            == _CONFIRMATION_KWARGS["implemented_behavior"]
        )
        assert payload["alternative"] == _CONFIRMATION_KWARGS["alternative"]
        assert payload["raised_at"]

    def test_confirmation_and_plain_question_coexist_distinguishably(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        pending.write_text(_DETECTOR_BLOCK, encoding="utf-8")
        raise_pending_question(pending, "Agent flag?", "standalone context")
        raise_pending_question(
            pending, "", "", kind="confirmation", **_CONFIRMATION_KWARGS
        )

        decisions = list_pending_decisions(wiki)
        by_type = {}
        for d in decisions:
            by_type.setdefault(d["type"], []).append(d)
        assert len(by_type["question"]) == 2  # detector + plain agent-raised
        assert len(by_type["confirmation"]) == 1


class TestConfirmationResolvesThroughExistingPath:
    def test_resolve_question_tool_resolves_a_confirmation_item(
        self, tmp_path: Path
    ) -> None:
        server = _server(tmp_path)
        raised = _call(
            server,
            "raise_decision",
            lambda fn: fn(kind="confirmation", **_CONFIRMATION_KWARGS),
        )
        assert raised["ok"] is True
        decision_id = raised["decision_id"]

        # Issue athenaeum#1431: bounded envelope, not a bare list — see the
        # comment in TestCrossSessionSurfacing above.
        decisions = _call(server, "list_pending_decisions", lambda fn: fn())["items"]
        item = next(d for d in decisions if d["id"] == decision_id)
        assert item["type"] == "confirmation"

        res = _call(
            server,
            "resolve_question",
            lambda fn: fn(decision_id, "Confirmed — extension approach is correct."),
        )
        assert res["ok"] is True
        assert res["deferred"] is True

        pending = tmp_path / "wiki" / "_pending_questions.md"
        raw_root = tmp_path / "raw"
        assert any(pq["id"] == decision_id for pq in list_unanswered(pending))

        report = apply_decision_answers(wiki_root=tmp_path / "wiki", raw_root=raw_root)
        assert report.applied == 1
        assert list_unanswered(pending) == []

        resolved = parse_pending_questions(pending)[0]
        assert resolved.answered is True
        assert resolved.decision_kind == "confirmation"
        assert resolved.raiser == _CONFIRMATION_KWARGS["raiser"]

        # And it's gone from the unified pending-decisions view. Issue
        # athenaeum#1431: bounded envelope, not a bare list.
        decisions_after = _call(server, "list_pending_decisions", lambda fn: fn())["items"]
        assert all(d["id"] != decision_id for d in decisions_after)
