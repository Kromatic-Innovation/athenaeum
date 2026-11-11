# SPDX-License-Identifier: Apache-2.0
"""DOMAIN TRUST OVERRIDES win-selection + edge-citation harness (issue
athenaeum#2332, TG4).

Computes two aggregate metrics over a fixed, hand-labeled pair set, run
through the real :func:`athenaeum.resolutions.propose_resolution`:

- **win-selection accuracy**: fraction of cases where the resolver's
  ``recommended_winner`` matches the case's hand label.
- **edge-citation rate**: of the cases where a ``DOMAIN TRUST OVERRIDES``
  block was actually rendered (``librarian.source_trust_enabled`` on AND
  a configured edge matches the pair's domain — see
  :func:`athenaeum.resolutions._domain_trust_overrides_lines`), the
  fraction whose ``source_precedence_used``
  (``src/athenaeum/resolutions.py:654``, the JSON field name in
  ``_RESOLVE_SYSTEM``) cites the edge. Failure class F10
  (``docs/design/trust-graph.md`` §4, "LLM ignores or over-applies
  overrides"): an absent citation is counted as a MISS against the
  denominator, never silently excluded — that is the entire point of
  tracking this metric separately from plain accuracy.

This module computes the metrics only. Running it against a real model and
setting a pass bar from the result is athenaeum#2365, which follows this
issue by operator ruling (``docs/design/trust-graph.md`` §6, TG4). Nothing
here makes a network call; callers supply the LLM client.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.resolutions import (
    ResolutionProposal,
    _build_user_message,
    propose_resolution,
)


@dataclass(frozen=True)
class TrustOverrideCase:
    """One hand-labeled pair for the harness.

    ``a``/``b`` are rendered as member "a" and "b" in that order — the
    resolver's own ``a``/``b`` convention (`_build_user_message`). The
    hand label is which one SHOULD win once the override applies.
    """

    case_id: str
    entity_type: str
    field: str
    a_source: str
    a_body: str
    b_source: str
    b_body: str
    expected_winner: str  # "a" | "b"


@dataclass(frozen=True)
class TrustOverrideCaseResult:
    case_id: str
    block_rendered: bool
    observed_winner: str
    correct: bool
    # Only meaningful when block_rendered is True -- None otherwise, so the
    # aggregate step below can tell "no block" from "block but not cited"
    # (the F10 miss) apart without a second flag.
    cited_edge: bool | None


def _write_case_member(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    source: str,
    entity_type: str,
    field: str,
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm = [
        "---",
        "name: probe",
        f"type: {entity_type}",
        f"source: {source}",
        "field_sources:",
        f"  {field}: '{source}'",
        "---",
    ]
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=scope_dir.name,
        memory_type=entity_type,
        name="probe",
        source_type="user-stated",
    )


def _detector_result(members: list[AutoMemoryFile], case: TrustOverrideCase) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="factual",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=[case.a_body, case.b_body],
        rationale=f"trust-override harness case {case.case_id}",
    )


def _cites_edge(source_precedence_used: list[str]) -> bool:
    """Whether the model's own ``source_precedence_used`` list credits an
    operator trust edge, rather than the ordinary tier taxonomy.

    The resolver's JSON contract (`_RESOLVE_SYSTEM`) only ever asks the
    model for a free-text comparison string (e.g. ``"a:api:govdb >
    b:user"``); it does not ask the model to echo
    `trust_graph.SourceTrustEdge.describe`'s exact text. "Cites the edge"
    is therefore read the same way a human reviewer would: does the
    string mention the word "edge", "override", or "trust" -- the
    resolver has no other vocabulary for naming an operator-declared
    override versus the default taxonomy. A model that silently applies
    the override while describing it as ordinary precedence is exactly
    the F10 failure this harness exists to surface, and must count as
    NOT cited.
    """
    haystack = " ".join(source_precedence_used).lower()
    return any(token in haystack for token in ("edge", "override", "trust"))


def run_case(
    case: TrustOverrideCase,
    client: Any,
    tmp_path: Path,
    *,
    config: dict[str, Any] | None,
) -> TrustOverrideCaseResult:
    """Run one case through the real resolver path and score it.

    ``config`` controls whether the override is live for this case (flag
    + edges) -- passing ``None`` or a flag-off config reproduces AC1's
    "no block" path, letting the SAME case set be run twice (overrides on
    vs off) by the caller.
    """
    scope_dir = tmp_path / f"scope-{case.case_id}"
    a = _write_case_member(
        scope_dir, "a.md", case.a_body, source=case.a_source,
        entity_type=case.entity_type, field=case.field,
    )
    b = _write_case_member(
        scope_dir, "b.md", case.b_body, source=case.b_source,
        entity_type=case.entity_type, field=case.field,
    )
    members = [a, b]
    detector = _detector_result(members, case)

    rendered_msg = _build_user_message(detector, members, config)
    block_rendered = "DOMAIN TRUST OVERRIDES:" in rendered_msg

    proposal = propose_resolution(detector, members, client, config, usage=TokenUsage())

    observed_winner = "neither"
    source_precedence_used: list[str] = []
    if isinstance(proposal, ResolutionProposal):
        observed_winner = proposal.recommended_winner
        source_precedence_used = list(proposal.source_precedence_used)

    cited_edge = _cites_edge(source_precedence_used) if block_rendered else None

    return TrustOverrideCaseResult(
        case_id=case.case_id,
        block_rendered=block_rendered,
        observed_winner=observed_winner,
        correct=observed_winner == case.expected_winner,
        cited_edge=cited_edge,
    )


@dataclass(frozen=True)
class TrustOverrideAggregate:
    total: int
    win_selection_accuracy: float
    # None when no case in this run actually rendered a block (e.g. the
    # whole run was done with overrides off) -- there is nothing to rate.
    edge_citation_rate: float | None
    blocks_rendered: int
    absent_citations: int  # F10 count, not merely (rendered - cited)


def aggregate(results: list[TrustOverrideCaseResult]) -> TrustOverrideAggregate:
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    rendered = [r for r in results if r.block_rendered]
    cited = sum(1 for r in rendered if r.cited_edge)
    absent = len(rendered) - cited
    return TrustOverrideAggregate(
        total=total,
        win_selection_accuracy=(correct / total) if total else 0.0,
        edge_citation_rate=(cited / len(rendered)) if rendered else None,
        blocks_rendered=len(rendered),
        absent_citations=absent,
    )
