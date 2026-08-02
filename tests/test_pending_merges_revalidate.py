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


def _legal_block() -> str:
    # 2 sources, high confidence → passes the size cap and (at the permissive
    # similarity defaults) every other recoverable gate arm.
    return render_block(
        merge_target_name="acme-corp",
        sources=["/k/acme-a.md", "/k/acme-b.md"],
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
    merges_path = _write_sidecar(
        tmp_path / "wiki", _over_cap_block(), _legal_block()
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
    merges_path = _write_sidecar(
        tmp_path / "wiki",
        _over_cap_block(),
        _legal_block(),
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
    merges_path = _write_sidecar(
        tmp_path / "wiki", _over_cap_block(), _legal_block()
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
    merges_path = _write_sidecar(tmp_path / "wiki", low_conf, _legal_block())

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
