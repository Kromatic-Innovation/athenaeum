# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#481 — ``revalidate_pending_merges`` periodic re-validation sweep.

athenaeum#480 closed the write-path bypass so no NEW degenerate over-cluster proposal
can be appended to ``wiki/_pending_merges.md``. This sweep is the complement:
it re-validates entries queued BEFORE the athenaeum#400/#421 gate tightened against the
CURRENT suppression gate and archives ones that now fail it — non-destructively
(moved to the archive with the gate reason, never deleted; no ``refines:``
suppression written on the source pages, the athenaeum#437 trap).

Covered:

1. An over-cap block is retired; a legal block and a resolved block are left
   untouched (the issue's binding AC4).
2. Dry-run (the default) writes NOTHING — no primary rewrite, no archive.
3. ``apply=True`` moves the stale block to ``_pending_merges_archive.md`` with
   the gate reason recorded, and is idempotent on a second run.
4. The opt-in confidence floor retires a low-confidence block.
5. A missing sidecar is a clean no-op.
6. Issue athenaeum#2435: a low-cohesion proposal's suppression outcome is
   unaffected by deleting the gate's dead complete-linkage/mean-similarity
   arms, because this call path never fed them cohesion data to begin with.
7. Issue athenaeum#2522: a folded (tombstoned) source is reported as
   "source folded into <target>", distinct from a genuinely missing one
   ("source page missing") -- including the mixed case where a block has
   both. And a proposal whose sources are ALL folded into its OWN target
   is archived as satisfied (``**Archived**:``), never as stale
   (``**Retired**:``) -- a wrong-target fold or a partial fold stays
   stale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from athenaeum.pending_merges import (
    parse_pending_merges,
    render_block,
    revalidate_pending_merges,
)

_FIXED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _over_cap_block() -> str:
    # 7 sources > the default max_merge_sources of 5 → over-cluster.
    return render_block(
        merge_target_name="merge-workflow-pattern",
        sources=[f"/k/src-{i}.md" for i in range(7)],
        rationale="chained on a weak bridge",
        draft_merged_body="draft",
        confidence=0.33,
        created_at="2026-06-20",
    )


def _legal_block(wiki: Path) -> str:
    # 2 sources, high confidence → passes the size cap and (at the permissive
    # similarity defaults) every other recoverable gate arm. Issue athenaeum#2072:
    # both sources must be REAL, live files — revalidate now also retires a
    # block with a missing/tombstoned source, so a "stays kept" fixture must
    # point at pages that actually exist.
    wiki.mkdir(parents=True, exist_ok=True)
    src_a = wiki / "acme-a.md"
    src_b = wiki / "acme-b.md"
    src_a.write_text("acme a\n", encoding="utf-8")
    src_b.write_text("acme b\n", encoding="utf-8")
    return render_block(
        merge_target_name="acme-corp",
        sources=[str(src_a), str(src_b)],
        rationale="cosine 0.92 topic overlap",
        draft_merged_body="draft",
        confidence=0.92,
        created_at="2026-07-05",
    )


def _resolved_block() -> str:
    block = render_block(
        merge_target_name="already-done",
        sources=[f"/k/done-{i}.md" for i in range(9)],  # also over-cap...
        rationale="handled",
        draft_merged_body="draft",
        confidence=0.10,
        created_at="2026-07-06",
    )
    # ...but already resolved (`- [x]`), so the sweep must NOT touch it.
    return block.replace("- [ ]", "- [x]", 1)


def _write_sidecar(wiki: Path, *blocks: str) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / "_pending_merges.md"
    path.write_text(
        "# Pending Merges\n\n" + "\n\n---\n\n".join(blocks) + "\n", encoding="utf-8"
    )
    return path


def test_dry_run_reports_over_cap_but_writes_nothing(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    merges_path = _write_sidecar(
        wiki, _over_cap_block(), _legal_block(wiki)
    )
    before = merges_path.read_text(encoding="utf-8")

    result = revalidate_pending_merges(merges_path, apply=False, now=_FIXED_NOW)

    assert result.applied is False
    assert len(result.retired) == 1
    assert result.retired[0].merge_target_name == "merge-workflow-pattern"
    assert "over-cluster" in result.retired[0].reason
    assert result.retired[0].n_sources == 7
    # Dry-run: primary is byte-identical and no archive was created.
    assert merges_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "wiki" / "_pending_merges_archive.md").exists()


def test_apply_archives_over_cap_and_keeps_legal_and_resolved(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    merges_path = _write_sidecar(
        wiki,
        _over_cap_block(),
        _legal_block(wiki),
        _resolved_block(),
    )

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.applied is True
    assert [r.merge_target_name for r in result.retired] == ["merge-workflow-pattern"]

    remaining = parse_pending_merges(merges_path)
    names = {pm.merge_target_name for pm in remaining}
    # The legal block AND the resolved-but-over-cap block are BOTH left in the
    # primary file — the sweep only retires UNRESOLVED, gate-failing blocks.
    assert names == {"acme-corp", "already-done"}
    assert "merge-workflow-pattern" not in names

    archive = tmp_path / "wiki" / "_pending_merges_archive.md"
    archive_text = archive.read_text(encoding="utf-8")
    assert archive_text.startswith("# Archived Merges")
    assert "merge-workflow-pattern" in archive_text
    # The gate reason and a Retired timestamp are recorded for auditability.
    assert "**Retired**: 2026-07-28T12:00:00Z" in archive_text
    assert "over-cluster" in archive_text


def test_apply_is_idempotent(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    merges_path = _write_sidecar(
        wiki, _over_cap_block(), _legal_block(wiki)
    )
    first = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)
    assert first.applied is True and len(first.retired) == 1

    after_first = merges_path.read_text(encoding="utf-8")
    second = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)
    # Nothing left to retire; the second run writes nothing new.
    assert second.retired == []
    assert second.applied is False
    assert merges_path.read_text(encoding="utf-8") == after_first


def test_confidence_floor_retires_low_confidence_block(tmp_path: Path) -> None:
    # A 2-source block passes the size cap, but with an opt-in confidence floor
    # configured, a below-floor block is retired.
    low_conf = render_block(
        merge_target_name="shaky-pair",
        sources=["/k/x.md", "/k/y.md"],
        rationale="weak",
        draft_merged_body="draft",
        confidence=0.20,
        created_at="2026-07-10",
    )
    wiki = tmp_path / "wiki"
    merges_path = _write_sidecar(wiki, low_conf, _legal_block(wiki))

    config = {"librarian": {"min_merge_confidence": 0.5}}
    result = revalidate_pending_merges(
        merges_path, config=config, apply=False, now=_FIXED_NOW
    )
    assert [r.merge_target_name for r in result.retired] == ["shaky-pair"]
    assert "low confidence" in result.retired[0].reason


def test_missing_sidecar_is_a_clean_noop(tmp_path: Path) -> None:
    result = revalidate_pending_merges(
        tmp_path / "wiki" / "_pending_merges.md", apply=True, now=_FIXED_NOW
    )
    assert result.retired == []
    assert result.kept == 0
    assert result.applied is False


# ---------------------------------------------------------------------------
# Issue athenaeum#2072 — a name-collision proposal (or any proposal) whose
# source page no longer exists under `wiki/` by revalidation time (folded,
# retired, or renamed by a later run) is stale with a distinct reason, and
# `--apply` archives it exactly like the over-cluster case.
# ---------------------------------------------------------------------------


def _write_tombstone(path: Path, *, name: str, folded_into: str) -> None:
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: concept\n"
        "status: folded\n"
        f"folded_into: {folded_into}\n"
        "embedded: false\n"
        "---\nbody\n",
        encoding="utf-8",
    )


def test_missing_source_page_is_stale_with_distinct_reason(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    present = wiki / "present.md"
    present.write_text("present\n", encoding="utf-8")
    deleted_path = wiki / "deleted.md"  # never written — simulates a page
    # retired/renamed by a later run after this proposal was queued.

    block = render_block(
        merge_target_name="orphaned-pair",
        sources=[str(present), str(deleted_path)],
        rationale="athenaeum#1170 nightly name-collision scan: 2 pages share the name",
        draft_merged_body="draft",
        confidence=1.0,
        created_at="2026-10-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=False, now=_FIXED_NOW)

    assert [r.merge_target_name for r in result.retired] == ["orphaned-pair"]
    assert result.retired[0].reason == "source page missing: 1 of 2"
    # Dry-run: nothing written.
    assert not (wiki / "_pending_merges_archive.md").exists()


def test_tombstoned_source_folded_elsewhere_is_stale_with_folded_reason(
    tmp_path: Path,
) -> None:
    """Issue athenaeum#2522 AC1: a tombstoned source is still STALE (the
    proposal cannot be reviewed as-is), but the reason now says it was
    FOLDED -- distinct from a genuinely missing/deleted page -- and names
    the fold target. Before athenaeum#2522 this reported the identical
    "source page missing" string as a deleted page would (hence this
    test's original name); that conflation is exactly what the issue asks
    to fix.

    This is also a PARTIAL case (one source folded, one still live), not
    an all-sources-folded one, so it is never satisfied-eligible — see
    ``test_all_sources_folded_into_own_target_is_satisfied``.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    folded_elsewhere = wiki / "folded-elsewhere.md"
    _write_tombstone(folded_elsewhere, name="Folded Elsewhere", folded_into="somewhere-else")
    other = wiki / "other.md"
    other.write_text("other\n", encoding="utf-8")

    block = render_block(
        merge_target_name="stale-tombstoned-pair",
        sources=[str(folded_elsewhere), str(other)],
        rationale="r",
        draft_merged_body="draft",
        confidence=1.0,
        created_at="2026-10-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert [r.merge_target_name for r in result.retired] == ["stale-tombstoned-pair"]
    assert result.retired[0].reason == "source folded into somewhere-else: 1 of 2"
    assert result.satisfied == []
    archive_text = (wiki / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "stale-tombstoned-pair" in archive_text
    assert "source folded into somewhere-else: 1 of 2" in archive_text
    assert "**Retired**:" in archive_text
    assert parse_pending_merges(merges_path) == []


def test_mixed_missing_and_folded_sources_report_both_counts(tmp_path: Path) -> None:
    """Issue athenaeum#2522 AC1: when a block has BOTH a genuinely missing
    source and a folded-elsewhere one, the reason reports both counts side
    by side rather than collapsing them into one undistinguished bucket."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    folded_elsewhere = wiki / "folded-elsewhere.md"
    _write_tombstone(folded_elsewhere, name="Folded Elsewhere", folded_into="somewhere-else")
    deleted_path = wiki / "deleted.md"  # never written -- genuinely gone
    live = wiki / "live.md"
    live.write_text("live\n", encoding="utf-8")

    block = render_block(
        merge_target_name="three-way-pair",
        sources=[str(folded_elsewhere), str(deleted_path), str(live)],
        rationale="r",
        draft_merged_body="draft",
        confidence=1.0,
        created_at="2026-10-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=False, now=_FIXED_NOW)

    assert [r.merge_target_name for r in result.retired] == ["three-way-pair"]
    reason = result.retired[0].reason
    assert "source folded into somewhere-else: 1 of 3" in reason
    assert "source page missing: 1 of 3" in reason


# ---------------------------------------------------------------------------
# Issue athenaeum#2522 AC2 — a proposal whose sources are ALL folded into ITS
# OWN target is already satisfied: the merge it asked for has, in effect,
# already happened. That is archived as satisfied (``**Archived**:``), not
# retired as stale (``**Retired**:``). A wrong-target fold or a partial fold
# stays on the ordinary stale path -- covered above and below.
# ---------------------------------------------------------------------------


def test_all_sources_folded_into_own_target_is_satisfied(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    # The live canonical page this proposal's merge_target_name resolves to.
    (wiki / "acme-corp.md").write_text(
        "---\nname: Acme Corp\ntype: concept\n---\nmerged body\n", encoding="utf-8"
    )
    src_a = wiki / "acme-a.md"
    src_b = wiki / "acme-b.md"
    _write_tombstone(src_a, name="Acme A", folded_into="acme-corp")
    _write_tombstone(src_b, name="Acme B", folded_into="acme-corp")

    block = render_block(
        merge_target_name="acme-corp",
        sources=[str(src_a), str(src_b)],
        rationale="queued before an unrelated fold landed",
        draft_merged_body="draft",
        confidence=0.95,
        created_at="2026-07-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.retired == []
    assert len(result.satisfied) == 1
    assert result.satisfied[0].merge_target_name == "acme-corp"
    assert result.satisfied[0].folded_into == "acme-corp"
    assert result.satisfied[0].n_sources == 2
    assert parse_pending_merges(merges_path) == []

    archive_text = (wiki / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "acme-corp" in archive_text
    assert "**Archived**:" in archive_text
    assert "**Retired**:" not in archive_text


def test_all_sources_folded_into_a_different_target_stays_stale(
    tmp_path: Path,
) -> None:
    """AC2's second load-bearing condition: EVERY source is folded, but
    into some THIRD page, not this proposal's own target -- still stale,
    never satisfied."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "acme-corp.md").write_text(
        "---\nname: Acme Corp\ntype: concept\n---\nmerged body\n", encoding="utf-8"
    )
    src_a = wiki / "acme-a.md"
    src_b = wiki / "acme-b.md"
    # Both folded, but into "some-other-page" -- not "acme-corp".
    _write_tombstone(src_a, name="Acme A", folded_into="some-other-page")
    _write_tombstone(src_b, name="Acme B", folded_into="some-other-page")

    block = render_block(
        merge_target_name="acme-corp",
        sources=[str(src_a), str(src_b)],
        rationale="r",
        draft_merged_body="draft",
        confidence=0.95,
        created_at="2026-07-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.satisfied == []
    assert [r.merge_target_name for r in result.retired] == ["acme-corp"]
    assert result.retired[0].reason == "source folded into some-other-page: 2 of 2"
    archive_text = (wiki / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "**Retired**:" in archive_text
    assert "**Archived**:" not in archive_text


def test_partial_fold_into_own_target_stays_stale(tmp_path: Path) -> None:
    """AC2's first load-bearing condition: "ALL", not "merely some" -- one
    source folded into the proposal's own target but the other still
    live is NOT satisfied; it stays stale like any other folded-source
    case."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "acme-corp.md").write_text(
        "---\nname: Acme Corp\ntype: concept\n---\nmerged body\n", encoding="utf-8"
    )
    src_a = wiki / "acme-a.md"
    _write_tombstone(src_a, name="Acme A", folded_into="acme-corp")
    src_b = wiki / "acme-b.md"
    src_b.write_text("still a live page\n", encoding="utf-8")

    block = render_block(
        merge_target_name="acme-corp",
        sources=[str(src_a), str(src_b)],
        rationale="r",
        draft_merged_body="draft",
        confidence=0.95,
        created_at="2026-07-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=False, now=_FIXED_NOW)

    assert result.satisfied == []
    assert [r.merge_target_name for r in result.retired] == ["acme-corp"]
    assert result.retired[0].reason == "source folded into acme-corp: 1 of 2"


def test_both_sources_present_is_untouched(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    merges_path = _write_sidecar(wiki, _legal_block(wiki))

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.retired == []
    remaining = parse_pending_merges(merges_path)
    assert {pm.merge_target_name for pm in remaining} == {"acme-corp"}
    assert not (wiki / "_pending_merges_archive.md").exists()


# ---------------------------------------------------------------------------
# Issue athenaeum#2435 — the complete-linkage and mean-similarity arms of
# ``_merge_proposal_suppression_reason`` were deleted as dead code: this
# call path never supplied the ``min_pairwise`` / ``mean_similarity`` /
# ``cluster_threshold`` data either arm needed to fire, because a queued
# ``_pending_merges.md`` block carries no cohesion data at all. The
# suppression set this sweep produces is therefore IDENTICAL before and
# after that deletion for any proposal, including a low-cohesion one.
# ---------------------------------------------------------------------------


def test_low_cohesion_proposal_suppression_set_unchanged_by_dead_arm_removal(
    tmp_path: Path,
) -> None:
    """A low-cohesion-looking proposal (the single-linkage-chain / weak-mean
    shape the now-deleted complete-linkage and mean-similarity arms used to
    target) is NOT suppressed by the revalidation sweep, because the sweep
    never had cohesion data to evaluate it with in the first place — only
    ``n_sources`` and ``confidence`` ever reached ``_merge_proposal_suppression_reason``
    from here. The suppression set (which target names get retired) is
    empty for this fixture both before and after issue athenaeum#2435's arm
    removal, proving the deletion changed no observable revalidate
    behaviour."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    # Issue athenaeum#2072: revalidate also retires a block with a
    # missing/tombstoned source, so these must be REAL, live files -- the
    # same idiom _legal_block uses -- or the "source page missing" gate
    # fires first and masks the cohesion-arm behaviour under test.
    chain_sources = []
    for name in ("chain-a", "chain-b", "chain-c"):
        src = wiki / f"{name}.md"
        src.write_text(f"{name}\n", encoding="utf-8")
        chain_sources.append(str(src))
    low_cohesion = render_block(
        merge_target_name="weak-bridge-cluster",
        sources=chain_sources,
        rationale="single-linkage chain -- weak pairwise bridge, would have "
        "tripped the retired complete-linkage/mean-similarity arms if this "
        "call path ever supplied cohesion data to them",
        draft_merged_body="draft",
        confidence=0.9,
        created_at="2026-07-28",
    )
    merges_path = _write_sidecar(wiki, low_cohesion, _legal_block(wiki))

    result = revalidate_pending_merges(merges_path, apply=False, now=_FIXED_NOW)

    # Suppression set is empty: the sweep has no cohesion data to act on,
    # so the low-cohesion proposal is kept -- unchanged by the arm removal.
    assert result.retired == []
    remaining_names = {pm.merge_target_name for pm in parse_pending_merges(merges_path)}
    assert remaining_names == {"weak-bridge-cluster", "acme-corp"}


# ---------------------------------------------------------------------------
# Review-bot finding, verified against the live corpus (24 of 367 real
# `status: folded` tombstones carry a bare-slug `folded_into` with no uid
# prefix): a pre-athenaeum#2545 fold stamped a tombstone's `folded_into` with
# the bare slug even when its target was ALREADY uid-prefixed. Comparing
# that legacy bare slug against a uid-prefixed target's current filename
# STEM (`<uid>-<slug>`) by raw equality never matches, so an
# already-satisfied proposal was misrouted into the stale/retired bucket.
# `_folded_into_matches` normalises both sides to the bare slug (stripping
# ONLY the target's own known `uid:` prefix) before comparing.
# ---------------------------------------------------------------------------


def _write_uid_prefixed_target(wiki: Path, *, uid: str, slug: str, name: str) -> Path:
    path = wiki / f"{uid}-{slug}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: concept\n---\nmerged body\n",
        encoding="utf-8",
    )
    return path


def test_legacy_bare_slug_fold_into_uid_prefixed_own_target_is_satisfied(
    tmp_path: Path,
) -> None:
    """Reproduces the Seer-flagged defect directly: BEFORE
    `_folded_into_matches`'s normalisation, comparing a legacy bare-slug
    `folded_into` against a uid-prefixed target's raw filename stem
    (`fa2aeafa-sublime-default-rule` != `sublime-default-rule`) fails, and
    this proposal lands in `result.retired` instead of `result.satisfied`.
    With the fix, both sides normalise to the bare slug
    `sublime-default-rule` and match."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    _write_uid_prefixed_target(
        wiki, uid="fa2aeafa", slug="sublime-default-rule", name="Sublime Default Rule"
    )
    src_a = wiki / "src-a.md"
    src_b = wiki / "src-b.md"
    # Legacy (pre-athenaeum#2545) shape: the BARE slug, not the target's
    # real uid-prefixed stem.
    _write_tombstone(src_a, name="Src A", folded_into="sublime-default-rule")
    _write_tombstone(src_b, name="Src B", folded_into="sublime-default-rule")

    block = render_block(
        merge_target_name="sublime-default-rule",
        sources=[str(src_a), str(src_b)],
        rationale="queued before an unrelated, pre-2545-shaped fold landed",
        draft_merged_body="draft",
        confidence=0.95,
        created_at="2026-07-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.retired == []
    assert len(result.satisfied) == 1
    assert result.satisfied[0].merge_target_name == "sublime-default-rule"
    assert result.satisfied[0].folded_into == "fa2aeafa-sublime-default-rule"
    assert parse_pending_merges(merges_path) == []
    archive_text = (wiki / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "**Archived**:" in archive_text
    assert "**Retired**:" not in archive_text


def test_lookalike_longer_slug_is_not_falsely_satisfied(tmp_path: Path) -> None:
    """Guards the exact failure mode the coordinator flagged as WORSE than
    the bug being fixed: normalising must never degrade into a
    substring/`endswith` match. A `folded_into` of
    `other-sublime-default-rule` (a longer, unrelated name that happens to
    END with the real target's slug, and does NOT start with the target's
    own uid) must NOT be treated as satisfied."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    _write_uid_prefixed_target(
        wiki, uid="fa2aeafa", slug="sublime-default-rule", name="Sublime Default Rule"
    )
    src_a = wiki / "src-a.md"
    src_b = wiki / "src-b.md"
    _write_tombstone(src_a, name="Src A", folded_into="other-sublime-default-rule")
    _write_tombstone(src_b, name="Src B", folded_into="other-sublime-default-rule")

    block = render_block(
        merge_target_name="sublime-default-rule",
        sources=[str(src_a), str(src_b)],
        rationale="r",
        draft_merged_body="draft",
        confidence=0.95,
        created_at="2026-07-01",
    )
    merges_path = _write_sidecar(wiki, block)

    result = revalidate_pending_merges(merges_path, apply=True, now=_FIXED_NOW)

    assert result.satisfied == []
    assert [r.merge_target_name for r in result.retired] == ["sublime-default-rule"]
    archive_text = (wiki / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "**Retired**:" in archive_text
    assert "**Archived**:" not in archive_text
