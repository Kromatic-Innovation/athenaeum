# SPDX-License-Identifier: Apache-2.0
"""Tests for Lane 1 / athenaeum#167 — `refines:` and `supersedes:` frontmatter fields.

Covers:

- Parser helpers (``parse_refines`` / ``parse_supersedes``) — happy path,
  empty default, and malformed-shape errors.
- ``AutoMemoryFile`` populates the new fields with empty-list defaults
  and exposes :meth:`supersedes_names`.
- Resolver auto-prefer: ``propose_resolution`` returns a synthetic
  ``keep_<superseder>`` proposal (confidence 1.0) for a declared
  supersession WITHOUT calling the Anthropic client.

The former "Conflict-detector short-circuit in :mod:`athenaeum.merge`"
coverage (``merge._declared_relationship`` / ``merge._filter_declared_pairs``)
was removed by issue athenaeum#2435: both were dead code left behind by the
C4 detector's retirement (issue athenaeum#1256), with zero ``src/`` callers.
The live declared-relationship primitive those two adapted
(:func:`athenaeum.declared_relationships.declared_relationship`) is used
directly below where a test still needs to drive it (see
:class:`TestDeclaredWinnerEdgeCases`), and is otherwise exercised through
:mod:`athenaeum.comparator`'s Gate 1 (``tests/test_comparator.py``'s
``TestDeclaredRelationshipGate1``).

These tests do NOT make network calls; the Anthropic client is a
``unittest.mock.MagicMock`` so any unexpected resolver invocation is
loud (``assert_not_called``).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.declared_relationships import DeclaredRelationshipFacts, declared_relationship
from athenaeum.models import (
    AutoMemoryFile,
    parse_merge_rejected_with,
    parse_refines,
    parse_supersedes,
)
from athenaeum.resolutions import propose_resolution

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    name: str,
    refines: list[str] | None = None,
    supersedes: list[dict[str, str]] | None = None,
    merge_rejected_with: list[str] | None = None,
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm_lines = ["---", f"name: {name}", "type: feedback"]
    if refines:
        fm_lines.append("refines:")
        for r in refines:
            fm_lines.append(f"  - {r}")
    if supersedes:
        fm_lines.append("supersedes:")
        for rec in supersedes:
            fm_lines.append(f"  - name: {rec['name']}")
            if rec.get("as_of"):
                fm_lines.append(f"    as_of: {rec['as_of']}")
            if rec.get("reason"):
                fm_lines.append(f"    reason: {rec['reason']!r}")
    if merge_rejected_with:
        fm_lines.append("merge_rejected_with:")
        for r in merge_rejected_with:
            fm_lines.append(f"  - {r}")
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name=name,
        refines=list(refines or []),
        supersedes=list(supersedes or []),
        merge_rejected_with=list(merge_rejected_with or []),
    )


def _facts(am: AutoMemoryFile) -> DeclaredRelationshipFacts:
    """Adapt an :class:`AutoMemoryFile` to the live declared-relationship
    primitive -- the same unpacking ``merge._declared_relationship`` did
    before issue athenaeum#2435 deleted that dead adapter."""
    return DeclaredRelationshipFacts(
        name=am.name,
        refines=am.refines,
        supersedes_names=am.supersedes_names(),
        merge_rejected_with=am.merge_rejected_with,
    )


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------


class TestParseRefines:
    def test_missing_key_returns_empty(self) -> None:
        assert parse_refines({}) == []
        assert parse_refines({"name": "x"}) == []

    def test_none_meta_returns_empty(self) -> None:
        assert parse_refines(None) == []

    def test_happy_path(self) -> None:
        assert parse_refines({"refines": ["a-slug", "b-slug"]}) == [
            "a-slug",
            "b-slug",
        ]

    def test_strips_whitespace(self) -> None:
        assert parse_refines({"refines": ["  spaced  "]}) == ["spaced"]

    def test_scalar_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_refines({"refines": "not-a-list"})

    def test_empty_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_refines({"refines": [""]})

    def test_non_string_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_refines({"refines": [123]})


class TestParseSupersedes:
    def test_missing_key_returns_empty(self) -> None:
        assert parse_supersedes({}) == []

    def test_happy_path(self) -> None:
        out = parse_supersedes(
            {
                "supersedes": [
                    {
                        "name": "old-slug",
                        "as_of": "2026-05-01",
                        "reason": "renamed",
                    }
                ]
            }
        )
        assert out == [{"name": "old-slug", "as_of": "2026-05-01", "reason": "renamed"}]

    def test_defaults_missing_optional_keys(self) -> None:
        out = parse_supersedes({"supersedes": [{"name": "old-slug"}]})
        assert out == [{"name": "old-slug", "as_of": "", "reason": ""}]

    def test_scalar_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_supersedes({"supersedes": "not-a-list"})

    def test_non_dict_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="mappings"):
            parse_supersedes({"supersedes": ["bare-string"]})

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name' key"):
            parse_supersedes({"supersedes": [{"as_of": "2026-01-01"}]})


class TestParseMergeRejectedWith:
    """Issue athenaeum#715: mirrors TestParseRefines exactly — same shape/errors."""

    def test_missing_key_returns_empty(self) -> None:
        assert parse_merge_rejected_with({}) == []
        assert parse_merge_rejected_with({"name": "x"}) == []

    def test_none_meta_returns_empty(self) -> None:
        assert parse_merge_rejected_with(None) == []

    def test_happy_path(self) -> None:
        assert parse_merge_rejected_with(
            {"merge_rejected_with": ["a-slug", "b-slug"]}
        ) == ["a-slug", "b-slug"]

    def test_strips_whitespace(self) -> None:
        assert parse_merge_rejected_with({"merge_rejected_with": ["  spaced  "]}) == [
            "spaced"
        ]

    def test_scalar_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_merge_rejected_with({"merge_rejected_with": "not-a-list"})

    def test_empty_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_merge_rejected_with({"merge_rejected_with": [""]})

    def test_non_string_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_merge_rejected_with({"merge_rejected_with": [123]})


# ---------------------------------------------------------------------------
# AutoMemoryFile dataclass
# ---------------------------------------------------------------------------


def test_auto_memory_defaults_empty(tmp_path: Path) -> None:
    path = tmp_path / "feedback_x.md"
    path.write_text("---\nname: x\ntype: feedback\n---\nbody\n", encoding="utf-8")
    am = AutoMemoryFile(
        path=path,
        origin_scope="scope-x",
        memory_type="feedback",
        name="x",
    )
    assert am.refines == []
    assert am.supersedes == []
    assert am.merge_rejected_with == []
    assert am.supersedes_names() == []


def test_supersedes_names_filters_malformed(tmp_path: Path) -> None:
    path = tmp_path / "feedback_x.md"
    path.write_text("---\nname: x\n---\n", encoding="utf-8")
    am = AutoMemoryFile(
        path=path,
        origin_scope="scope-x",
        memory_type="feedback",
        name="x",
        supersedes=[
            {"name": "old-a", "as_of": "", "reason": ""},
            {"as_of": "2026-01-01"},  # type: ignore[list-item]
            {"name": ""},
        ],
    )
    assert am.supersedes_names() == ["old-a"]


# ---------------------------------------------------------------------------
# Resolver auto-prefer (acceptance #3)
# ---------------------------------------------------------------------------


class TestResolverAutoPrefer:
    def test_supersedes_short_circuits_without_llm_call(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "new",
            name="memory-a",
            supersedes=[{"name": "memory-b", "as_of": "2026-05-01"}],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "old", name="memory-b")
        client = MagicMock()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["new", "old"],
            rationale="detector flagged",
        )
        proposal = propose_resolution(result, [a, b], client)
        # No network call — declared supersession short-circuits.
        client.messages.create.assert_not_called()
        assert proposal.action == "keep_a"
        assert proposal.recommended_winner == "a"
        assert proposal.confidence == 1.0
        assert "supersession" in proposal.rationale.lower()

    def test_supersedes_reverse_direction(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path / "s", "feedback_a.md", "old", name="memory-a")
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "new",
            name="memory-b",
            supersedes=[{"name": "memory-a"}],
        )
        client = MagicMock()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["old", "new"],
        )
        proposal = propose_resolution(result, [a, b], client)
        client.messages.create.assert_not_called()
        assert proposal.action == "keep_b"
        assert proposal.recommended_winner == "b"

    def test_refines_short_circuits_to_not_a_conflict(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "narrow",
            name="memory-a",
            refines=["memory-b"],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "general", name="memory-b")
        client = MagicMock()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["narrow", "general"],
        )
        proposal = propose_resolution(result, [a, b], client)
        client.messages.create.assert_not_called()
        assert proposal.action == "not_a_conflict"
        assert proposal.recommended_winner == "neither"
        assert proposal.confidence == 1.0

    def test_undeclared_pair_falls_through_to_llm(self, tmp_path: Path) -> None:
        """No declaration → resolver still calls the client."""
        a = _write_am(tmp_path / "s", "feedback_a.md", "always X", name="a")
        b = _write_am(tmp_path / "s", "feedback_b.md", "never X", name="b")
        client = MagicMock()
        client.messages.create.return_value.content = [
            MagicMock(
                text='{"recommended_winner":"a","action":"keep_a",'
                '"rationale":"r","confidence":0.7,'
                '"source_precedence_used":["a:user > b:unsourced"]}'
            )
        ]
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["always X", "never X"],
        )
        proposal = propose_resolution(result, [a, b], client)
        client.messages.create.assert_called_once()
        assert proposal.action == "keep_a"
        assert proposal.confidence == 0.7


# ---------------------------------------------------------------------------
# Quine review athenaeum#171 follow-ups
# ---------------------------------------------------------------------------


class TestDeclaredWinnerEdgeCases:
    """Quine review of PR athenaeum#171 — short-circuit refusal + mutual cases."""

    def _llm_reply(self) -> MagicMock:
        client = MagicMock()
        client.messages.create.return_value.content = [
            MagicMock(
                text='{"recommended_winner":"neither","action":'
                '"retain_both_with_context","rationale":"r","confidence":0.5,'
                '"source_precedence_used":[]}'
            )
        ]
        return client

    def test_member_echo_below_two_refuses_short_circuit(self, tmp_path: Path) -> None:
        """MUST #1: detector echo <2 → resolver must NOT short-circuit."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "new",
            name="memory-a",
            supersedes=[{"name": "memory-b"}],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "old", name="memory-b")
        client = self._llm_reply()
        # members_involved length 0 — detector underspecified the pair.
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[],
            conflicting_passages=["new", "old"],
        )
        propose_resolution(result, [a, b], client)
        # Fell through to LLM rather than short-circuiting against the
        # wrong pair.
        client.messages.create.assert_called_once()

    def test_member_echo_one_refuses_short_circuit(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "new",
            name="memory-a",
            supersedes=[{"name": "memory-b"}],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "old", name="memory-b")
        client = self._llm_reply()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[f"{a.origin_scope}/{a.path.name}"],
            conflicting_passages=["new", "old"],
        )
        propose_resolution(result, [a, b], client)
        client.messages.create.assert_called_once()

    def test_mutual_supersedes_escalates_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """MUST #3: A↔B mutual supersedes → fall to LLM + WARNING log."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "side a",
            name="memory-a",
            supersedes=[{"name": "memory-b"}],
        )
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "side b",
            name="memory-b",
            supersedes=[{"name": "memory-a"}],
        )
        client = self._llm_reply()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["side a", "side b"],
        )
        with caplog.at_level("WARNING", logger="athenaeum.resolutions"):
            propose_resolution(result, [a, b], client)
        # Did NOT pick a silent winner.
        client.messages.create.assert_called_once()
        assert any(
            "mutual supersedes" in rec.getMessage().lower() for rec in caplog.records
        )

    def test_mutual_supersedes_in_merge_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """MUST #3, declared-relationship-primitive side: mutual supersedes
        → no declared verdict. (Ported from ``merge._declared_relationship``,
        deleted as dead code by issue athenaeum#2435; the logger is now
        ``athenaeum.declared_relationships``, the module that owns the
        live primitive.)"""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "side a",
            name="memory-a",
            supersedes=[{"name": "memory-b"}],
        )
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "side b",
            name="memory-b",
            supersedes=[{"name": "memory-a"}],
        )
        with caplog.at_level("WARNING", logger="athenaeum.declared_relationships"):
            verdict = declared_relationship(_facts(a), _facts(b))
        assert verdict is None
        assert any(
            "mutual supersedes" in rec.getMessage().lower() for rec in caplog.records
        )

    def test_mixed_refine_plus_supersede_keeps_superseder(self, tmp_path: Path) -> None:
        """MUST #3 doc: A refines B, B supersedes A → keep_b wins."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "narrow",
            name="memory-a",
            refines=["memory-b"],
        )
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "replacement",
            name="memory-b",
            supersedes=[{"name": "memory-a"}],
        )
        client = MagicMock()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["narrow", "replacement"],
        )
        proposal = propose_resolution(result, [a, b], client)
        client.messages.create.assert_not_called()
        assert proposal.action == "keep_b"
        assert proposal.recommended_winner == "b"
        assert "supersession" in proposal.rationale.lower()

    def test_slugify_normalizes_case_mismatch(self, tmp_path: Path) -> None:
        """SHOULD #4: A.name 'memory-a' matches B.refines ['Memory-A']."""
        a = _write_am(tmp_path / "s", "feedback_a.md", "general", name="memory-a")
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "narrow",
            name="memory-b",
            refines=["Memory-A"],
        )
        # declared-relationship-primitive side
        assert declared_relationship(_facts(a), _facts(b)) == "declared-refinement"
        # resolutions.py side
        client = MagicMock()
        result = ContradictionResult(
            detected=True,
            conflict_type="prescriptive",
            members_involved=[
                f"{a.origin_scope}/{a.path.name}",
                f"{b.origin_scope}/{b.path.name}",
            ],
            conflicting_passages=["general", "narrow"],
        )
        proposal = propose_resolution(result, [a, b], client)
        client.messages.create.assert_not_called()
        assert proposal.action == "not_a_conflict"

