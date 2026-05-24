# SPDX-License-Identifier: Apache-2.0
"""Tests for Lane 1 / #167 — `refines:` and `supersedes:` frontmatter fields.

Covers:

- Parser helpers (``parse_refines`` / ``parse_supersedes``) — happy path,
  empty default, and malformed-shape errors.
- ``AutoMemoryFile`` populates the new fields with empty-list defaults
  and exposes :meth:`supersedes_names`.
- Conflict-detector short-circuit in :mod:`athenaeum.merge`: a pair that
  declares its relationship via ``refines`` / ``supersedes`` does NOT
  reach the Haiku detector.
- Resolver auto-prefer: ``propose_resolution`` returns a synthetic
  ``keep_<superseder>`` proposal (confidence 1.0) for a declared
  supersession WITHOUT calling the Anthropic client.

These tests do NOT make network calls; the Anthropic client is a
``unittest.mock.MagicMock`` so any unexpected resolver invocation is
loud (``assert_not_called``).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.merge import _declared_relationship, _filter_declared_pairs
from athenaeum.models import (
    AutoMemoryFile,
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
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name=name,
        refines=list(refines or []),
        supersedes=list(supersedes or []),
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
# Conflict-detector skip (acceptance #2 + #5)
# ---------------------------------------------------------------------------


class TestDetectorSkip:
    def test_undeclared_pair_reaches_detector(self, tmp_path: Path) -> None:
        """Two conflicting memories with NO declaration → detector sees them."""
        a = _write_am(tmp_path / "s", "feedback_a.md", "always X", name="memory-a")
        b = _write_am(tmp_path / "s", "feedback_b.md", "never X", name="memory-b")
        assert _declared_relationship(a, b) is None
        filtered, declared = _filter_declared_pairs([a, b])
        assert declared is None
        assert filtered == [a, b]

    def test_declared_supersedes_skips_detector(self, tmp_path: Path) -> None:
        """``a`` supersedes ``b`` → short-circuit verdict."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "new guidance",
            name="memory-a",
            supersedes=[
                {"name": "memory-b", "as_of": "2026-05-01", "reason": "renamed"}
            ],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "old guidance", name="memory-b")
        assert _declared_relationship(a, b) == "declared-supersession"
        filtered, declared = _filter_declared_pairs([a, b])
        assert declared == "declared-supersession"
        assert filtered == []

    def test_declared_refines_skips_detector(self, tmp_path: Path) -> None:
        """``a`` refines ``b`` → short-circuit verdict."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "narrowed",
            name="memory-a",
            refines=["memory-b"],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "general", name="memory-b")
        assert _declared_relationship(a, b) == "declared-refinement"
        filtered, declared = _filter_declared_pairs([a, b])
        assert declared == "declared-refinement"
        assert filtered == []

    def test_reverse_direction_also_skips(self, tmp_path: Path) -> None:
        """Declaration on EITHER side suppresses — direction doesn't matter."""
        a = _write_am(tmp_path / "s", "feedback_a.md", "general", name="memory-a")
        b = _write_am(
            tmp_path / "s",
            "feedback_b.md",
            "narrowed",
            name="memory-b",
            refines=["memory-a"],
        )
        assert _declared_relationship(a, b) == "declared-refinement"

    def test_partial_declaration_in_three_member_cluster(self, tmp_path: Path) -> None:
        """If one pair in the chunk is undeclared, the whole chunk must run."""
        a = _write_am(
            tmp_path / "s",
            "feedback_a.md",
            "new",
            name="memory-a",
            supersedes=[{"name": "memory-b"}],
        )
        b = _write_am(tmp_path / "s", "feedback_b.md", "old", name="memory-b")
        c = _write_am(tmp_path / "s", "feedback_c.md", "other", name="memory-c")
        filtered, declared = _filter_declared_pairs([a, b, c])
        assert declared is None
        assert filtered == [a, b, c]


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
# Quine review #171 follow-ups
# ---------------------------------------------------------------------------


class TestDeclaredWinnerEdgeCases:
    """Quine review of PR #171 — short-circuit refusal + mutual cases."""

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
        """MUST #3 merge.py side: mutual supersedes → no declared verdict."""
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
        with caplog.at_level("WARNING", logger="athenaeum.merge"):
            verdict = _declared_relationship(a, b)
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
        # merge.py side
        assert _declared_relationship(a, b) == "declared-refinement"
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


class TestCrossScopeMalformedLogs:
    """SHOULD #5: cross_scope.py logs WARNING on malformed frontmatter."""

    def test_malformed_supersedes_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.cross_scope import (
            SimilarityCandidate,
            candidate_to_auto_memory_files,
        )

        scope_dir = tmp_path / "scope-x"
        scope_dir.mkdir()
        bad = scope_dir / "feedback_bad.md"
        # supersedes must be a list; pass a scalar to trigger ValueError.
        bad.write_text(
            "---\nname: bad\ntype: feedback\nsupersedes: not-a-list\n---\nbody\n",
            encoding="utf-8",
        )
        good = scope_dir / "feedback_good.md"
        good.write_text(
            "---\nname: good\ntype: feedback\n---\nbody\n", encoding="utf-8"
        )
        candidate = SimilarityCandidate(
            a_path=bad,
            b_path=good,
            a_scope="scope-x",
            b_scope="scope-x",
            similarity=0.9,
        )
        with caplog.at_level("WARNING", logger="athenaeum.cross_scope"):
            ams = candidate_to_auto_memory_files(candidate)
        assert len(ams) == 2
        # bad file loaded with empty declared-relationship lists.
        bad_am = next(am for am in ams if am.path == bad)
        assert bad_am.supersedes == []
        assert bad_am.refines == []
        msgs = [rec.getMessage() for rec in caplog.records]
        assert any(str(bad) in m for m in msgs)
        assert any("invalid refines/supersedes" in m for m in msgs)
