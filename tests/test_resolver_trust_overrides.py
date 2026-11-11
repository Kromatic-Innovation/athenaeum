# SPDX-License-Identifier: Apache-2.0
"""Tests for the DOMAIN TRUST OVERRIDES rendering in the LLM resolver's
per-pair user message (issue athenaeum#2332, TG4).

Covers the issue's acceptance criteria that are code-level (not the paid
eval, athenaeum#2365):

- AC1: with no edges configured, or the flag off, :func:`_build_user_message`
  is byte-identical to the pre-TG4 message (no ``DOMAIN TRUST OVERRIDES``
  text anywhere, and identical in full to a config=None baseline).
- AC2 / F8: an opinion-vs-opinion pair never shows the block, even when a
  matching edge is configured and the flag is on.
- The block appears only when a configured edge's domain
  (entity_type, field_sources key, evidence_kind) actually matches this
  pair, and lists exactly the matching edge(s) via
  :meth:`athenaeum.trust_graph.SourceTrustEdge.describe`.

Exercises the real :func:`athenaeum.resolutions._build_user_message` and
the real :func:`athenaeum.config.resolve_source_trust_edges` /
:func:`athenaeum.config.resolve_source_trust_enabled` against ordinary
``librarian:`` yaml-shaped config dicts -- no mocked trust_graph internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile
from athenaeum.resolutions import _build_user_message

_GOVDB_EDGE_CONFIG: dict[str, Any] = {
    "librarian": {
        "source_trust_enabled": True,
        "source_trust_edges": [
            {
                "domain": {"entity_type": "person", "field": "payout_amount"},
                "over": "api:govdb",
                "under": "user",
                "note": "a documentary government ledger beats self-report",
            }
        ],
    }
}

_UNRELATED_EDGE_CONFIG: dict[str, Any] = {
    "librarian": {
        "source_trust_enabled": True,
        "source_trust_edges": [
            {
                "domain": {"entity_type": "project", "field": "implementation_detail"},
                "over": "agent-observed",
                "under": "user",
            }
        ],
    }
}

_FLAG_OFF_CONFIG: dict[str, Any] = {
    "librarian": {
        "source_trust_enabled": False,
        "source_trust_edges": _GOVDB_EDGE_CONFIG["librarian"]["source_trust_edges"],
    }
}

_NO_EDGES_CONFIG: dict[str, Any] = {
    "librarian": {"source_trust_enabled": True, "source_trust_edges": []}
}


def _write_member(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    source: str,
    entity_type: str = "person",
    field_sources: dict[str, str] | None = None,
    claim_kind: str = "",
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    """Write a raw member file + matching :class:`AutoMemoryFile` record.

    Mirrors ``tests/test_resolutions.py``'s ``_write_opinion_member`` /
    ``_write_am`` helpers, extended with ``entity_type`` (frontmatter
    ``type:``) and ``field_sources`` -- the two pieces of this pair's
    concrete domain :func:`_domain_trust_overrides_lines` matches edges
    against.
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm: list[str] = ["---", "name: probe", f"type: {entity_type}", f"source: {source}"]
    if claim_kind:
        fm.append(f"claim_kind: {claim_kind}")
    if field_sources:
        fm.append("field_sources:")
        for key, val in field_sources.items():
            fm.append(f"  {key}: '{val}'")
    fm.append("---")
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name="probe",
        claim_kind=claim_kind,
        source_type="user-stated",
    )


def _detected(members: list[AutoMemoryFile]) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="factual",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=["$150,000 per the user.", "$210,000 per the ledger."],
        rationale="payout figure disagreement",
    )


class TestByteIdentityAC1:
    """AC1: no edges, or the flag off, is byte-identical to today."""

    def test_no_trust_config_matches_no_edges_configured(self, tmp_path: Path) -> None:
        scope = tmp_path / "s"
        a = _write_member(scope, "a.md", "Payout is $150,000.", source="user:conv-1")
        b = _write_member(scope, "b.md", "Payout is $210,000.", source="api:govdb")
        detector = _detected([a, b])

        baseline = _build_user_message(detector, [a, b], None)
        with_empty_edges = _build_user_message(detector, [a, b], _NO_EDGES_CONFIG)
        with_flag_off = _build_user_message(detector, [a, b], _FLAG_OFF_CONFIG)

        assert baseline == with_empty_edges == with_flag_off
        assert "DOMAIN TRUST OVERRIDES" not in baseline

    def test_configured_but_non_matching_domain_is_also_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """A real edge, flag on, but scoped to a domain this pair never
        touches -- must render nothing (not just "flag off" is safe)."""
        scope = tmp_path / "s"
        a = _write_member(scope, "a.md", "Payout is $150,000.", source="user:conv-1")
        b = _write_member(scope, "b.md", "Payout is $210,000.", source="api:govdb")
        detector = _detected([a, b])

        baseline = _build_user_message(detector, [a, b], None)
        unrelated = _build_user_message(detector, [a, b], _UNRELATED_EDGE_CONFIG)

        assert baseline == unrelated
        assert "DOMAIN TRUST OVERRIDES" not in unrelated


class TestMatchingEdgeRendering:
    """The block appears, with the right edge, only on an actual domain match."""

    def test_matching_domain_renders_the_edge_as_data(self, tmp_path: Path) -> None:
        scope = tmp_path / "s"
        a = _write_member(
            scope,
            "a.md",
            "Payout is $150,000.",
            source="user:conv-1",
            entity_type="person",
            field_sources={"payout_amount": "user:conv-1"},
        )
        b = _write_member(
            scope,
            "b.md",
            "Payout is $210,000.",
            source="api:govdb",
            entity_type="person",
            field_sources={"payout_amount": "api:govdb"},
        )
        detector = _detected([a, b])

        msg = _build_user_message(detector, [a, b], _GOVDB_EDGE_CONFIG)

        assert "DOMAIN TRUST OVERRIDES:" in msg
        assert "'api:govdb' over 'user'" in msg
        assert "a documentary government ledger beats self-report" in msg

    def test_field_sources_on_only_one_side_still_matches(self, tmp_path: Path) -> None:
        """The pair's domain is the UNION of both members' field_sources
        keys -- a match via either side's keys is sufficient."""
        scope = tmp_path / "s"
        a = _write_member(
            scope,
            "a.md",
            "Payout is $150,000.",
            source="user:conv-1",
            entity_type="person",
            field_sources={"payout_amount": "user:conv-1"},
        )
        b = _write_member(
            scope,
            "b.md",
            "Payout is $210,000.",
            source="api:govdb",
            entity_type="person",
            # No field_sources on this side at all.
        )
        detector = _detected([a, b])

        msg = _build_user_message(detector, [a, b], _GOVDB_EDGE_CONFIG)

        assert "DOMAIN TRUST OVERRIDES:" in msg


class TestOpinionSuppressionF8:
    """AC2 / F8: an opinion-vs-opinion pair never shows the block."""

    def test_both_opinion_suppresses_even_with_a_matching_edge(self, tmp_path: Path) -> None:
        scope = tmp_path / "s"
        a = _write_member(
            scope,
            "a.md",
            "Tabs are better.",
            source="user:conv-1",
            entity_type="person",
            field_sources={"payout_amount": "user:conv-1"},
            claim_kind="opinion",
        )
        b = _write_member(
            scope,
            "b.md",
            "Spaces are better.",
            source="api:govdb",
            entity_type="person",
            field_sources={"payout_amount": "api:govdb"},
            claim_kind="opinion",
        )
        detector = _detected([a, b])

        msg = _build_user_message(detector, [a, b], _GOVDB_EDGE_CONFIG)

        assert "DOMAIN TRUST OVERRIDES" not in msg

    def test_one_sided_opinion_does_not_suppress(self, tmp_path: Path) -> None:
        """Only a BOTH-opinion pair is suppressed (F8's exact condition) --
        one opinion + one non-opinion side still renders a matching edge."""
        scope = tmp_path / "s"
        a = _write_member(
            scope,
            "a.md",
            "Payout is $150,000.",
            source="user:conv-1",
            entity_type="person",
            field_sources={"payout_amount": "user:conv-1"},
            claim_kind="fact",
        )
        b = _write_member(
            scope,
            "b.md",
            "Payout is $210,000.",
            source="api:govdb",
            entity_type="person",
            field_sources={"payout_amount": "api:govdb"},
            claim_kind="opinion",
        )
        detector = _detected([a, b])

        msg = _build_user_message(detector, [a, b], _GOVDB_EDGE_CONFIG)

        assert "DOMAIN TRUST OVERRIDES:" in msg
