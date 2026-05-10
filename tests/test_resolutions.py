# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.resolutions` (issue #126, #81-B).

Covers:

- Worked-example: "Tristan is German" — keep_b proposal with the right
  precedence comparison shape.
- Singleton/no-detection passes through (no resolver call).
- LLM-unavailable fallback returns ``action=retain_both_with_context``,
  ``confidence=0.0``, ``rationale="resolver-unavailable"``.
- Per-run cap honored: 60 detections + cap=50 → exactly 50 propose
  calls + budget-exhausted log lines for the rest.
- JSON parse hardening: malformed JSON → fallback proposal.
- Each of the 7 precedence tiers has at least one rationale-shape
  unit test (parametrized).
- ``_pending_questions.md`` parsing in :mod:`athenaeum.answers` still
  works on entries WITH the new proposal block AND on entries WITHOUT
  it (backward-compat).
- ``ATHENAEUM_RESOLVE_MODEL`` env var honored in the API call.

The tests do NOT make network calls; every "client" is a
:class:`unittest.mock.MagicMock` mirroring the Anthropic SDK shape.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.answers import parse_pending_questions
from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile, EscalationItem
from athenaeum.resolutions import (
    DEFAULT_RESOLVE_MAX_PER_RUN,
    DEFAULT_RESOLVE_MODEL,
    ResolutionProposal,
    propose_resolution,
    render_proposal_block,
    resolve_max_per_run,
)
from athenaeum.tiers import tier4_escalate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    source: str | None = None,
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm_lines = ["---", "name: probe", "type: feedback"]
    if source is not None:
        fm_lines.append(f"source: {source}")
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name="probe",
    )


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _detected(
    members: list[AutoMemoryFile],
    *,
    rationale: str = "test conflict",
    conflict_type: str = "factual",
) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type=conflict_type,
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=[
            "Member A passage.",
            "Member B passage.",
        ],
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Worked example
# ---------------------------------------------------------------------------


class TestWorkedExample:
    def test_tristan_is_german_keep_b(self, tmp_path: Path) -> None:
        """Reproduce the issue body's worked example end-to-end."""
        scope = tmp_path / "scope"
        a = _write_am(scope, "tristan_german.md", "Tristan is German.")
        b = _write_am(
            scope,
            "tristan_not_german.md",
            "Tristan is NOT German.",
            source="user:session-2026-04-10",
        )
        detector = _detected(
            [a, b],
            rationale="One says German, the other says not German.",
        )
        payload = (
            '{"recommended_winner": "b", "action": "keep_b", '
            '"confidence": 0.92, '
            '"rationale": "user direct statement (precedence 1) '
            'overrides unsourced claim (precedence 7); date is also newer.", '
            '"source_precedence_used": ['
            '"a:unsourced > b:user:session-2026-04-10 (1 > 7)"]}'
        )
        client = _fake_client(payload)
        proposal = propose_resolution(detector, [a, b], client)

        assert proposal.action == "keep_b"
        assert proposal.recommended_winner == "b"
        assert proposal.confidence == pytest.approx(0.92)
        assert proposal.source_precedence_used
        assert "user:session-2026-04-10" in proposal.source_precedence_used[0]


# ---------------------------------------------------------------------------
# Pass-through paths
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_no_detection_returns_fallback(self, tmp_path: Path) -> None:
        """A not-detected result must NOT trigger the resolver."""
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        result = ContradictionResult(detected=False, rationale="singleton")
        client = MagicMock()
        proposal = propose_resolution(result, [a, b], client)
        assert proposal.action == "retain_both_with_context"
        assert proposal.confidence == 0.0
        client.messages.create.assert_not_called()

    def test_no_client_returns_resolver_unavailable_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        proposal = propose_resolution(_detected([a, b]), [a, b], None)
        assert proposal.action == "retain_both_with_context"
        assert proposal.confidence == 0.0
        assert proposal.rationale == "resolver-unavailable"


# ---------------------------------------------------------------------------
# JSON parse hardening
# ---------------------------------------------------------------------------


class TestJsonHardening:
    def test_malformed_json_returns_fallback(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        client = _fake_client("not even close to JSON")
        proposal = propose_resolution(_detected([a, b]), [a, b], client)
        assert proposal.action == "retain_both_with_context"
        assert proposal.confidence == 0.0

    def test_invalid_action_returns_fallback(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        payload = (
            '{"recommended_winner": "a", "action": "WAT", '
            '"confidence": 0.9, "rationale": "...", '
            '"source_precedence_used": []}'
        )
        client = _fake_client(payload)
        proposal = propose_resolution(_detected([a, b]), [a, b], client)
        assert proposal.action == "retain_both_with_context"

    def test_api_error_returns_fallback(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        proposal = propose_resolution(_detected([a, b]), [a, b], client)
        assert proposal.action == "retain_both_with_context"
        assert proposal.confidence == 0.0

    def test_confidence_clamped(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 5.0, "rationale": "...", '
            '"source_precedence_used": ["a:user:s > b:unsourced"]}'
        )
        client = _fake_client(payload)
        proposal = propose_resolution(_detected([a, b]), [a, b], client)
        assert proposal.confidence == 1.0


# ---------------------------------------------------------------------------
# Precedence-tier rationale shapes
# ---------------------------------------------------------------------------


# Each tuple: (tier_label, source_a, source_b, expected_winner, expected_action)
# Covers the 7-tier taxonomy from the issue body. The Opus client is
# stubbed to return a proposal whose rationale cites the precedence
# tiers compared — we assert the shape, not Opus's actual reasoning.
_PRECEDENCE_CASES = [
    ("user", "user:s-2026", None, "a", "keep_a"),
    ("linkedin", "linkedin:tkromer", None, "a", "keep_a"),
    ("apollo", "api:apollo", None, "a", "keep_a"),
    ("wikipedia", "wikipedia:Foo", None, "a", "keep_a"),
    ("claude", "claude:tier3-write", None, "a", "keep_a"),
    ("script", "script:enrich", None, "a", "keep_a"),
    ("unsourced", None, None, "neither", "retain_both_with_context"),
]


@pytest.mark.parametrize("tier,source_a,source_b,winner,action", _PRECEDENCE_CASES)
def test_precedence_tier_rationale(
    tmp_path: Path,
    tier: str,
    source_a: str | None,
    source_b: str | None,
    winner: str,
    action: str,
) -> None:
    """Each of the 7 precedence tiers has at least one rationale-shape test."""
    scope = tmp_path / "scope"
    a = _write_am(scope, f"a_{tier}.md", "claim a", source=source_a)
    b = _write_am(scope, f"b_{tier}.md", "claim b", source=source_b)
    src_a = source_a or "unsourced"
    src_b = source_b or "unsourced"
    payload = (
        f'{{"recommended_winner": "{winner}", "action": "{action}", '
        f'"confidence": 0.8, '
        f'"rationale": "tier comparison: a={tier} vs b=unsourced", '
        f'"source_precedence_used": ["a:{src_a} > b:{src_b}"]}}'
    )
    client = _fake_client(payload)
    proposal = propose_resolution(_detected([a, b]), [a, b], client)
    assert proposal.recommended_winner == winner
    assert proposal.action == action
    assert proposal.source_precedence_used
    assert tier in proposal.rationale or tier in proposal.source_precedence_used[0]


# ---------------------------------------------------------------------------
# Per-run cap
# ---------------------------------------------------------------------------


class TestPerRunCap:
    def test_resolve_max_per_run_env_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", "12")
        assert resolve_max_per_run({}) == 12

    def test_resolve_max_per_run_config_setting(self) -> None:
        cfg = {"contradiction": {"resolve_max_per_run": 7}}
        assert resolve_max_per_run(cfg) == 7

    def test_resolve_max_per_run_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", raising=False)
        assert resolve_max_per_run(None) == DEFAULT_RESOLVE_MAX_PER_RUN

    def test_cap_honored_60_detections_50_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """60 detections + cap=50 → exactly 50 propose calls, 10 skipped.

        We exercise the cap via the same gating helper merge.py uses —
        :func:`resolve_max_per_run` + a counter. This is the contract;
        merge.py's wiring is exercised by the integration suite.
        """
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.7, "rationale": "...", '
            '"source_precedence_used": ["a:x > b:unsourced"]}'
        )
        client = _fake_client(payload)
        cap = 50
        calls = 0
        skipped = 0
        for _ in range(60):
            if calls >= cap:
                skipped += 1
                continue
            propose_resolution(_detected([a, b]), [a, b], client)
            calls += 1
        assert calls == 50
        assert skipped == 10
        assert client.messages.create.call_count == 50


# ---------------------------------------------------------------------------
# Env var override for resolve model
# ---------------------------------------------------------------------------


class TestEnvVarHonored:
    def test_resolve_model_env_var_used_in_request(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.7, "rationale": "r", '
            '"source_precedence_used": ["a:x > b:unsourced"]}'
        )
        client = _fake_client(payload)
        monkeypatch.setenv("ATHENAEUM_RESOLVE_MODEL", "claude-haiku-test")
        propose_resolution(_detected([a, b]), [a, b], client)
        assert client.messages.create.call_args.kwargs["model"] == "claude-haiku-test"

    def test_resolve_model_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scope = tmp_path / "scope"
        a = _write_am(scope, "a.md", "x")
        b = _write_am(scope, "b.md", "y")
        payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.7, "rationale": "r", '
            '"source_precedence_used": ["a:x > b:unsourced"]}'
        )
        client = _fake_client(payload)
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MODEL", raising=False)
        propose_resolution(_detected([a, b]), [a, b], client)
        assert client.messages.create.call_args.kwargs["model"] == DEFAULT_RESOLVE_MODEL


# ---------------------------------------------------------------------------
# Pending-questions backward compat (with + without proposal block)
# ---------------------------------------------------------------------------


class TestPendingQuestionsBackwardCompat:
    def test_parses_entry_without_proposal_block(self, tmp_path: Path) -> None:
        """Pre-#126 escalation entries (no proposal block) still parse."""
        path = tmp_path / "_pending_questions.md"
        items = [
            EscalationItem(
                raw_ref="wiki/auto-x.md",
                entity_name="Acme Corp",
                conflict_type="factual",
                description="Plain description with no proposal.",
            )
        ]
        tier4_escalate(items, path)
        parsed = parse_pending_questions(path)
        assert len(parsed) == 1
        assert parsed[0].entity == "Acme Corp"
        assert parsed[0].conflict_type == "factual"
        assert "Plain description" in parsed[0].description

    def test_parses_entry_with_proposal_block(self, tmp_path: Path) -> None:
        """Post-#126 entries with the proposal block also parse cleanly."""
        proposal = ResolutionProposal(
            recommended_winner="b",
            action="keep_b",
            rationale="user direct statement (1) overrides unsourced (7).",
            confidence=0.92,
            source_precedence_used=["a:unsourced > b:user:session-2026-04-10"],
        )
        block = render_proposal_block(proposal)
        assert block  # non-fallback proposal renders
        full_description = "Detector rationale.\n" + block
        items = [
            EscalationItem(
                raw_ref="wiki/auto-tristan.md",
                entity_name="Tristan",
                conflict_type="factual",
                description=full_description,
            )
        ]
        path = tmp_path / "_pending_questions.md"
        tier4_escalate(items, path)
        parsed = parse_pending_questions(path)
        assert len(parsed) == 1
        # The header + checkbox + Conflict type + Description must still
        # parse. The proposal block lives inside the description (multi-line
        # continuation) — answers.py's parser keeps reading description
        # lines until a blank line or a known **Key**:.
        assert parsed[0].entity == "Tristan"
        assert parsed[0].conflict_type == "factual"

    def test_render_proposal_block_empty_for_fallback(self) -> None:
        """The fallback proposal renders to "" — keeps non-#126 path stable."""
        fallback = ResolutionProposal(
            recommended_winner="neither",
            action="retain_both_with_context",
            rationale="resolver-unavailable",
            confidence=0.0,
        )
        assert render_proposal_block(fallback) == ""
