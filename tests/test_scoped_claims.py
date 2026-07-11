# SPDX-License-Identifier: Apache-2.0
"""Scoped-claim poset + three-way verdict + scope-edit resolver actions (#329).

Covers the buildable subset of #329's design pass:

1. Dimension posets — org/locale trees (normalize/leq/meet) and time intervals.
2. ``ScopeTree`` config loading + coordinate parsing.
3. The three-way ``scope_comparison`` verdict (DISJOINT / OVERRIDE / OVERLAP).
4. Resolver integration — ``_scope_verdict_proposal`` + ``propose_resolution``
   short-circuit (no Opus call on a scope verdict).
5. Scope-edit enactment — ``enact_resolution`` narrows the named side's time
   window for ``scope_a`` / ``scope_b``; ``flip_action`` mirrors them.

Deferred design (asserted where relevant): time-interval NESTING does not drive
OVERRIDE (tree-specificity only); org/locale coordinate PINNING enactment is not
built (scope narrowing falls back to the time dimension).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.contradictions import ContradictionResult, _member_scope_header
from athenaeum.models import AutoMemoryFile, parse_frontmatter, parse_valid_until
from athenaeum.resolutions import (
    ENACTING_ACTIONS,
    ResolutionProposal,
    _scope_verdict_proposal,
    enact_resolution,
    flip_action,
    propose_resolution,
)
from athenaeum.scoped_claims import (
    ScopeCoordinate,
    ScopeTree,
    ScopeVerdict,
    TreeDimension,
    interval_leq,
    interval_meet_empty,
    scope_comparison,
    scope_leq,
)

# Shared org/locale tree used across the verdict tests.
_CFG = {
    "scope": {
        "org": ["kromatic", "kromatic/platform", "kromatic/marketing"],
        "locale": ["en", "en-US", "de-DE"],
    }
}


def _tree() -> ScopeTree:
    return ScopeTree.from_config(_CFG)


# ---------------------------------------------------------------------------
# TreeDimension
# ---------------------------------------------------------------------------


class TestTreeDimension:
    def _org(self) -> TreeDimension:
        return _tree().org

    def test_normalize_known_node_lowercased(self) -> None:
        assert self._org().normalize("Kromatic/Platform") == "kromatic/platform"

    def test_normalize_unknown_value_is_unscoped(self) -> None:
        # Authors may not mint scope values not in the tree (#329) — fail open
        # to unscoped so a typo adds no constraint rather than a phantom scope.
        assert self._org().normalize("acme/widgets") is None

    def test_normalize_none_and_nonstring_and_empty(self) -> None:
        org = self._org()
        assert org.normalize(None) is None
        assert org.normalize("") is None
        assert org.normalize("   ") is None
        assert org.normalize(123) is None

    def test_leq_top_is_top(self) -> None:
        org = self._org()
        # Everything is at-or-below TOP (unscoped); TOP is below only TOP.
        assert org.leq("kromatic/platform", None) is True
        assert org.leq(None, None) is True
        assert org.leq(None, "kromatic") is False

    def test_leq_descendant_below_ancestor(self) -> None:
        org = self._org()
        assert org.leq("kromatic/platform", "kromatic") is True
        assert org.leq("kromatic", "kromatic/platform") is False
        assert org.leq("kromatic", "kromatic") is True

    def test_meet_empty_incomparable_siblings(self) -> None:
        org = self._org()
        # Two distinct subtrees under a shared parent do not intersect.
        assert org.meet_empty("kromatic/platform", "kromatic/marketing") is True
        # Nested nodes intersect (lower subtree).
        assert org.meet_empty("kromatic/platform", "kromatic") is False
        # TOP intersects everything.
        assert org.meet_empty(None, "kromatic/platform") is False

    def test_locale_uses_dash_separator(self) -> None:
        loc = _tree().locale
        assert loc.leq("en-US", "en") is True
        assert loc.meet_empty("en-US", "de-DE") is True


# ---------------------------------------------------------------------------
# Time interval dimension
# ---------------------------------------------------------------------------


class TestIntervalDimension:
    def test_interval_leq_nested(self) -> None:
        # [Apr, Jun] within [open, open] and within [Jan, Dec].
        assert interval_leq((date(2026, 4, 1), date(2026, 6, 30)), (None, None))
        assert interval_leq(
            (date(2026, 4, 1), date(2026, 6, 30)),
            (date(2026, 1, 1), date(2026, 12, 31)),
        )

    def test_interval_leq_open_below_not_within_bounded(self) -> None:
        # a open-below cannot be within b that has a real lower bound.
        assert not interval_leq((None, date(2026, 6, 30)), (date(2026, 1, 1), None))

    def test_interval_meet_empty_strict_boundary(self) -> None:
        # Inclusive valid_until: ending 03-31 and starting 04-01 → disjoint.
        assert interval_meet_empty((None, date(2026, 3, 31)), (date(2026, 4, 1), None))
        # Sharing the boundary day 04-01 → NOT disjoint.
        assert not interval_meet_empty(
            (None, date(2026, 4, 1)), (date(2026, 4, 1), None)
        )

    def test_interval_meet_empty_open_windows_overlap(self) -> None:
        assert not interval_meet_empty((None, None), (None, None))


# ---------------------------------------------------------------------------
# ScopeTree config + coordinate parsing
# ---------------------------------------------------------------------------


class TestScopeTreeConfig:
    def test_empty_config_makes_all_values_inert(self) -> None:
        # No scope config (fresh single-user install) → every declared org/
        # locale value normalizes to unscoped, so scope frontmatter is inert.
        t = ScopeTree.from_config(None)
        assert t.org.normalize("kromatic") is None
        assert t.locale.normalize("en-US") is None

    def test_malformed_scope_config_is_empty(self) -> None:
        t = ScopeTree.from_config({"scope": {"org": "not-a-list", "locale": 5}})
        assert t.org.nodes == frozenset()
        assert t.locale.nodes == frozenset()

    def test_coordinate_parses_scope_block_and_time(self) -> None:
        t = _tree()
        meta = {
            "scope": {"org": "kromatic/platform", "locale": "en-US"},
            "valid_from": "2026-04-01",
            "valid_until": "2026-06-30",
        }
        c = t.coordinate(meta)
        assert c.org == "kromatic/platform"
        # Coordinate values are case-folded consistently (config nodes too).
        assert c.locale == "en-us"
        assert c.valid_from == date(2026, 4, 1)
        assert c.valid_until == date(2026, 6, 30)

    def test_coordinate_unknown_org_normalizes_to_unscoped(self) -> None:
        c = _tree().coordinate({"scope": {"org": "acme"}})
        assert c.org is None

    def test_coordinate_missing_scope_block(self) -> None:
        c = _tree().coordinate({"valid_from": "2026-04-01"})
        assert c.org is None and c.locale is None
        assert c.valid_from == date(2026, 4, 1)


# ---------------------------------------------------------------------------
# Three-way verdict
# ---------------------------------------------------------------------------


class TestScopeComparison:
    def _cmp(self, a: ScopeCoordinate, b: ScopeCoordinate):
        return scope_comparison(a, b, _tree())

    def test_disjoint_incomparable_org(self) -> None:
        r = self._cmp(
            ScopeCoordinate(org="kromatic/platform"),
            ScopeCoordinate(org="kromatic/marketing"),
        )
        assert r.verdict is ScopeVerdict.DISJOINT
        assert r.specific is None

    def test_disjoint_time_windows(self) -> None:
        r = self._cmp(
            ScopeCoordinate(valid_until=date(2026, 3, 31)),
            ScopeCoordinate(valid_from=date(2026, 4, 1)),
        )
        assert r.verdict is ScopeVerdict.DISJOINT

    def test_override_specific_team_under_org(self) -> None:
        # kromatic/platform ⊑ kromatic → platform side is the exception.
        r = self._cmp(
            ScopeCoordinate(org="kromatic/platform"),
            ScopeCoordinate(org="kromatic"),
        )
        assert r.verdict is ScopeVerdict.OVERRIDE
        assert r.specific == "a"

    def test_override_specific_side_b(self) -> None:
        r = self._cmp(
            ScopeCoordinate(org="kromatic"),
            ScopeCoordinate(org="kromatic/platform"),
        )
        assert r.verdict is ScopeVerdict.OVERRIDE
        assert r.specific == "b"

    def test_override_unscoped_vs_scoped(self) -> None:
        # A scoped claim carves an exception out of an org-wide (unscoped) rule.
        r = self._cmp(ScopeCoordinate(org="kromatic"), ScopeCoordinate())
        assert r.verdict is ScopeVerdict.OVERRIDE
        assert r.specific == "a"

    def test_overlap_same_context(self) -> None:
        r = self._cmp(ScopeCoordinate(org="kromatic"), ScopeCoordinate(org="kromatic"))
        assert r.verdict is ScopeVerdict.OVERLAP

    def test_overlap_fully_unscoped(self) -> None:
        r = self._cmp(ScopeCoordinate(), ScopeCoordinate())
        assert r.verdict is ScopeVerdict.OVERLAP

    def test_overlap_incomparable_but_overlapping(self) -> None:
        # org: a below b; locale: b below a → neither dominates, meet non-empty.
        r = self._cmp(
            ScopeCoordinate(org="kromatic/platform", locale="en"),
            ScopeCoordinate(org="kromatic", locale="en-US"),
        )
        assert r.verdict is ScopeVerdict.OVERLAP

    def test_time_nesting_alone_does_not_override(self) -> None:
        # Deferred design: a sub-interval does NOT trigger OVERRIDE — same
        # org/locale (unscoped) + nested time → OVERLAP (reaches the resolver,
        # preserving #324's shipped semantics).
        r = self._cmp(
            ScopeCoordinate(valid_from=date(2026, 4, 1), valid_until=date(2026, 6, 30)),
            ScopeCoordinate(),
        )
        assert r.verdict is ScopeVerdict.OVERLAP

    def test_scope_leq_full_product_includes_time(self) -> None:
        # scope_leq is the full product order (org+locale+time); documents the
        # complete poset even though the verdict uses tree-specificity only.
        a = ScopeCoordinate(org="kromatic/platform", valid_from=date(2026, 4, 1))
        b = ScopeCoordinate(org="kromatic")
        assert scope_leq(a, b, _tree()) is True
        assert scope_leq(b, a, _tree()) is False


# ---------------------------------------------------------------------------
# Detector trusted scope header (#329 segment)
# ---------------------------------------------------------------------------


def _write_member(
    tmp_path: Path,
    filename: str,
    *,
    scope_block: dict | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    origin_scope: str = "scope-x",
    body: str = "the claim",
) -> AutoMemoryFile:
    fm: list[str] = ["---", "name: probe", "type: feedback"]
    if valid_from is not None:
        fm.append(f"valid_from: {valid_from}")
    if valid_until is not None:
        fm.append(f"valid_until: {valid_until}")
    if scope_block is not None:
        fm.append("scope:")
        for k, v in scope_block.items():
            fm.append(f"  {k}: {v}")
    fm.append("---")
    path = tmp_path / filename
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name="probe",
        valid_from=valid_from or "",
        valid_until=valid_until or "",
    )


class TestDetectorScopeHeader:
    def test_scope_header_includes_org_locale(self, tmp_path: Path) -> None:
        am = _write_member(
            tmp_path,
            "a.md",
            scope_block={"org": "kromatic/platform", "locale": "en-US"},
        )
        header = _member_scope_header(am)
        assert "org: kromatic/platform" in header
        assert "locale: en-US" in header

    def test_scope_header_empty_without_scope(self, tmp_path: Path) -> None:
        am = _write_member(tmp_path, "a.md")
        assert _member_scope_header(am) == ""


# ---------------------------------------------------------------------------
# Resolver integration — _scope_verdict_proposal + propose_resolution
# ---------------------------------------------------------------------------


def _detected(members: list[AutoMemoryFile]) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="factual",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=["A passage.", "B passage."],
        rationale="test conflict",
    )


class TestScopeVerdictProposal:
    def test_disjoint_org_returns_not_a_conflict(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md", scope_block={"org": "kromatic/platform"})
        b = _write_member(tmp_path, "b.md", scope_block={"org": "kromatic/marketing"})
        prop = _scope_verdict_proposal(_detected([a, b]), [a, b], _CFG)
        assert prop is not None
        assert prop.action == "not_a_conflict"
        assert prop.confidence == 1.0
        assert "disjoint scope" in prop.rationale

    def test_override_returns_not_a_conflict_both_active(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md", scope_block={"org": "kromatic/platform"})
        b = _write_member(tmp_path, "b.md", scope_block={"org": "kromatic"})
        prop = _scope_verdict_proposal(_detected([a, b]), [a, b], _CFG)
        assert prop is not None
        assert prop.action == "not_a_conflict"
        assert "override" in prop.rationale

    def test_overlap_falls_through(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md", scope_block={"org": "kromatic"})
        b = _write_member(tmp_path, "b.md", scope_block={"org": "kromatic"})
        assert _scope_verdict_proposal(_detected([a, b]), [a, b], _CFG) is None

    def test_no_scope_config_is_noop(self, tmp_path: Path) -> None:
        # Fresh install (no tree) → org values inert → not the scope path.
        a = _write_member(tmp_path, "a.md", scope_block={"org": "kromatic/platform"})
        b = _write_member(tmp_path, "b.md", scope_block={"org": "kromatic/marketing"})
        assert _scope_verdict_proposal(_detected([a, b]), [a, b], None) is None

    def test_propose_resolution_short_circuits_without_client(
        self, tmp_path: Path
    ) -> None:
        # No Anthropic client, but the scope verdict resolves it deterministically
        # (a client-less run would otherwise return the degraded fallback).
        a = _write_member(tmp_path, "a.md", scope_block={"org": "kromatic/platform"})
        b = _write_member(tmp_path, "b.md", scope_block={"org": "kromatic/marketing"})
        prop = propose_resolution(_detected([a, b]), [a, b], client=None, config=_CFG)
        assert prop.action == "not_a_conflict"
        assert prop.confidence == 1.0


# ---------------------------------------------------------------------------
# Scope-edit enactment
# ---------------------------------------------------------------------------


def _valid_until(path: Path) -> date | None:
    """Parse a member file's ``valid_until`` (robust to YAML date quoting)."""
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return parse_valid_until(meta)


def _scope_proposal(action: str) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner="neither",
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=0.95,
    )


class TestScopeEnactment:
    def test_scope_actions_are_enacting_and_flippable(self) -> None:
        assert "scope_a" in ENACTING_ACTIONS
        assert "scope_b" in ENACTING_ACTIONS
        assert flip_action("scope_a") == "scope_b"
        assert flip_action("scope_b") == "scope_a"

    def test_scope_a_narrows_side_a_until_before_b_from(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md", body="A rule")  # open window
        b = _write_member(tmp_path, "b.md", valid_from="2026-04-01")
        narrowed = enact_resolution(_scope_proposal("scope_a"), [a.path, b.path])
        assert narrowed == a.path
        # a.valid_until closed to the day BEFORE b.valid_from → disjoint.
        assert _valid_until(a.path) == date(2026, 3, 31)
        # b is untouched — both members stay active.
        assert "superseded_by" not in b.path.read_text(encoding="utf-8")

    def test_scope_b_narrows_side_b(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md", valid_from="2026-07-01")
        b = _write_member(tmp_path, "b.md", body="B rule")
        narrowed = enact_resolution(_scope_proposal("scope_b"), [a.path, b.path])
        assert narrowed == b.path
        assert _valid_until(b.path) == date(2026, 6, 30)

    def test_scope_narrow_noop_without_time_boundary(self, tmp_path: Path) -> None:
        # Other side has no valid_from → org/locale pinning deferred to the ADR;
        # nothing is narrowed and the pair escalates (return None).
        a = _write_member(tmp_path, "a.md", body="A rule")
        b = _write_member(tmp_path, "b.md", body="B rule")
        assert enact_resolution(_scope_proposal("scope_a"), [a.path, b.path]) is None

    def test_scope_narrow_only_closes_never_widens(self, tmp_path: Path) -> None:
        # Existing tighter valid_until on a is preserved.
        a = _write_member(tmp_path, "a.md", valid_until="2026-01-15", body="A")
        b = _write_member(tmp_path, "b.md", valid_from="2026-06-01")
        enact_resolution(_scope_proposal("scope_a"), [a.path, b.path])
        assert _valid_until(a.path) == date(2026, 1, 15)

    def test_scope_narrow_missing_side_returns_none(self, tmp_path: Path) -> None:
        a = _write_member(tmp_path, "a.md")
        assert enact_resolution(_scope_proposal("scope_a"), [a.path]) is None
