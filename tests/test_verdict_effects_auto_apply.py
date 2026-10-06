# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-apply gate (issue athenaeum#716): the allowlisted set of
operations :mod:`athenaeum.verdict_effects` may ever enact with no human in
the loop, and the two-gate (operator opt-in + fresh verdict basis) duplicate
auto-apply authorization check.

Test-class names continue ``tests/test_verdict_effects.py``'s ``EFn``
numbering (this module's own acceptance criteria are not separately numbered
in the issue, so it picks up where that module's docstring left off).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
    CompareOutcome,
    page_from_text,
)
from athenaeum.runlock import RunLock
from athenaeum.verdict_effects import (
    AUTO_APPLY_FOLD_ON_DUPLICATE,
    AUTO_APPLY_OPERATIONS,
    AUTO_APPLY_SPECIALIZATION_REFINES,
    AUTO_APPLY_SUPERSESSION_MARKING,
    _check_auto_apply_operation,
    apply_verdict_effect,
)
from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry


def _page(page_id: str, *, body: str = "some claim text"):
    text = f"---\nname: {page_id}\n---\n{body}\n"
    return page_from_text(page_id, text)


def _outcome(verdict: str | None, **kwargs) -> CompareOutcome:
    return CompareOutcome(verdict=verdict, **kwargs)


def _basis(**overrides) -> Basis:
    defaults = dict(
        content_hashes=["hash-a", "hash-b"],
        coords=[],
        coord_origins={},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis="implicit-superuser",
        predicate_instrument=["status", "status"],
        comparator_version="v1.gate2",
    )
    defaults.update(overrides)
    return Basis(**defaults)


def _append_entry(wiki_root: Path, id_a: str, id_b: str, *, stale: bool = False) -> None:
    entry = build_verdict_entry(
        id_a, id_b, "duplicate", basis=_basis(), decided_by="comparator"
    )
    if stale:
        import dataclasses

        entry = dataclasses.replace(entry, stale=True, stale_reason="test-induced")
    lock = RunLock(wiki_root)
    lock.acquire()
    try:
        append_verdict(wiki_root, entry, lock=lock)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# The enumerated allowlist + its refusal
# ---------------------------------------------------------------------------


class TestAutoApplyAllowlist:
    def test_allowlist_is_exactly_the_three_operations(self) -> None:
        assert AUTO_APPLY_OPERATIONS == {
            AUTO_APPLY_FOLD_ON_DUPLICATE,
            AUTO_APPLY_SPECIALIZATION_REFINES,
            AUTO_APPLY_SUPERSESSION_MARKING,
        }

    def test_allowlisted_operations_do_not_raise(self) -> None:
        for op in AUTO_APPLY_OPERATIONS:
            _check_auto_apply_operation(op)  # must not raise

    def test_anything_outside_the_allowlist_is_refused_loudly(self) -> None:
        for bogus in ("distinct", "underdetermined", "propose_merge", "", "fold-on-contradiction"):
            with pytest.raises(ValueError, match="not an auto-appliable operation"):
                _check_auto_apply_operation(bogus)

    def test_only_duplicate_branch_ever_populates_auto_apply_details(self, tmp_path: Path) -> None:
        """Issue athenaeum#716 AC: "nothing else can reach the auto-apply
        path" -- distinct/underdetermined/an unresolved contradiction never
        even consult the gate, so their EffectResult carries no
        auto_apply_authorized key at all (as opposed to carrying it and
        reporting False -- the absence itself is the proof those branches
        never asked)."""
        wiki_root = tmp_path / "wiki"
        page_a, page_b = _page("alpha"), _page("beta")

        distinct_result = apply_verdict_effect(
            page_a, page_b, _outcome(VERDICT_DISTINCT, separator=["scope"]), wiki_root=wiki_root
        )
        assert "auto_apply_authorized" not in distinct_result.details

        underdetermined_result = apply_verdict_effect(
            page_a, page_b, _outcome(VERDICT_UNDERDETERMINED, missing=["scope"]),
            wiki_root=wiki_root,
        )
        assert "auto_apply_authorized" not in underdetermined_result.details

        contradiction_result = apply_verdict_effect(
            page_a, page_b, _outcome(VERDICT_CONTRADICTION), wiki_root=wiki_root
        )
        assert "auto_apply_authorized" not in contradiction_result.details


# ---------------------------------------------------------------------------
# Duplicate verdict: the two-gate authorization check
# ---------------------------------------------------------------------------


class TestDuplicateAutoApplyAuthorization:
    def test_default_off_never_authorizes(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        result = apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        assert result.action == "fold-proposal"  # existing evidence+queue behavior, unchanged
        assert result.details["auto_apply_authorized"] is False
        assert result.details["auto_apply_reason"] == "auto_apply_disabled"
        assert "auto_apply_blocked_reason" not in result.details

    def test_gate_on_but_no_ledger_entry_fails_closed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        config = {"librarian": {"reversible_verdict_auto_apply_enabled": True}}
        result = apply_verdict_effect(
            _page("alpha"), _page("beta"), outcome, wiki_root=wiki_root, config=config
        )
        assert result.details["auto_apply_authorized"] is False
        assert result.details["auto_apply_reason"] == "no_verdict_ledger_entry"

    def test_stale_verdict_blocks_auto_apply(self, tmp_path: Path) -> None:
        """Issue athenaeum#716 AC: "test the stale-blocks-auto-apply path
        explicitly" -- a stale-marked verdict (athenaeum#712's own
        invalidation signal) must never authorize a new automatic fold, even
        with the gate on."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _append_entry(wiki_root, "alpha", "beta", stale=True)
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        config = {"librarian": {"reversible_verdict_auto_apply_enabled": True}}
        result = apply_verdict_effect(
            _page("alpha"), _page("beta"), outcome, wiki_root=wiki_root, config=config
        )
        assert result.details["auto_apply_authorized"] is False
        assert result.details["auto_apply_reason"] == "stale_verdict"
        # Still queues exactly like the gate-off path -- stale never crashes,
        # never silently no-ops.
        assert result.action == "fold-proposal"
        assert result.queued

    def test_fresh_verdict_with_gate_on_is_authorized_but_not_yet_executed(
        self, tmp_path: Path
    ) -> None:
        """Authorization is granted (gate on + fresh basis), but this module
        does not itself perform the fold write -- see
        ``src/athenaeum/verdict_effects.py``'s ``_apply_duplicate`` comment
        and the lane C report for the cross-lane reason (the fold-write
        primitive lives in ``athenaeum.pending_merges``, which this module's
        own docstring states it does not import)."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _append_entry(wiki_root, "alpha", "beta", stale=False)
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        config = {"librarian": {"reversible_verdict_auto_apply_enabled": True}}
        result = apply_verdict_effect(
            _page("alpha"), _page("beta"), outcome, wiki_root=wiki_root, config=config
        )
        assert result.details["auto_apply_authorized"] is True
        assert result.details["auto_apply_reason"] == "authorized"
        assert result.details["auto_apply_blocked_reason"] == (
            "fold_write_primitive_unavailable_to_this_module"
        )
        # Falls back to today's evidence+queue behavior -- never a silent
        # no-op, never a half-applied write.
        assert result.action == "fold-proposal"
        assert len(result.artifacts) == 1


# ---------------------------------------------------------------------------
# Specialization is retained UNCONDITIONAL — issue athenaeum#716 does not gate it
# ---------------------------------------------------------------------------


class TestSpecializationRemainsUngated:
    def test_specialization_still_writes_refines_with_gate_off_and_no_ledger_entry(
        self, tmp_path: Path
    ) -> None:
        """Pre-existing behavior since issue athenaeum#715 -- NOT newly gated
        by this issue's auto-apply key. See
        ``tests/test_verdict_effects.py::TestEF6SpecializationWritesRefines``
        for the original coverage; this just pins that the NEW gate does not
        regress it when both the config key and a verdict-ledger entry are
        entirely absent."""
        path_a = tmp_path / "alpha.md"
        path_b = tmp_path / "beta.md"
        path_a.write_text("---\nname: alpha\n---\nspecific\n", encoding="utf-8")
        path_b.write_text("---\nname: beta\n---\ngeneral\n", encoding="utf-8")
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side="a")
        wiki_root = tmp_path / "wiki"
        result = apply_verdict_effect(
            _page("alpha"), _page("beta"), outcome,
            wiki_root=wiki_root, path_a=path_a, path_b=path_b,
        )
        assert result.action == "refines-written"
        from athenaeum.models import parse_frontmatter

        meta, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        assert meta["refines"] == ["beta"]
