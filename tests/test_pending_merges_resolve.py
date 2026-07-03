# SPDX-License-Identifier: Apache-2.0
"""Tests for ``resolve_merge`` (issue #169, Lane 3, Quine fixes).

Covers the four bugs surfaced in the Quine review of PR #176:

1. resolved_block points at the rewritten target block (not block 0).
2. reject with missing source A returns ok=True with a warning field.
3. reject derives refines: from source B's frontmatter name (not stem).
4. approve fails closed when wiki/<target-slug>.md already exists.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import (
    resolve_merge,
    write_pending_merge,
)


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: feedback\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def test_resolve_merge_returns_correct_resolved_block(tmp_path: Path) -> None:
    """resolved_block must be the rewritten TARGET block, not block 0."""
    merges = tmp_path / "_pending_merges.md"
    src_a1 = tmp_path / "feedback_alpha_a.md"
    src_b1 = tmp_path / "feedback_alpha_b.md"
    src_a2 = tmp_path / "feedback_beta_a.md"
    src_b2 = tmp_path / "feedback_beta_b.md"
    for p, n in [
        (src_a1, "alpha_a"),
        (src_b1, "alpha_b"),
        (src_a2, "beta_a"),
        (src_b2, "beta_b"),
    ]:
        _write_source(p, name=n)

    # Block #1 — alpha pair.
    write_pending_merge(
        merges,
        merge_target_name="alpha-merged",
        sources=[str(src_a1), str(src_b1)],
        rationale="alpha",
        draft_merged_body="alpha body",
        confidence=0.9,
    )
    # Block #2 — beta pair. The bug returned block 0 (alpha) regardless
    # of which id was resolved; this test resolves beta and asserts the
    # response carries the beta block.
    block2 = write_pending_merge(
        merges,
        merge_target_name="beta-merged",
        sources=[str(src_a2), str(src_b2)],
        rationale="beta",
        draft_merged_body="beta body",
        confidence=0.9,
    )

    # Recover beta's id by re-parsing.
    from athenaeum.pending_merges import parse_pending_merges

    pms = parse_pending_merges(merges)
    beta_id = next(pm.id for pm in pms if pm.merge_target_name == "beta-merged")

    result = resolve_merge(merges, beta_id, "reject", note="not a merge")

    assert result["ok"] is True, result
    rb = result["resolved_block"]
    assert rb is not None
    assert "beta-merged" in rb, f"resolved_block names wrong target: {rb!r}"
    assert "alpha-merged" not in rb
    assert "- [x]" in rb
    assert "**Decision**: reject" in rb
    # Sanity: the original beta block text appears in the rewritten one.
    assert block2.splitlines()[0] in rb


def test_resolve_merge_reject_warns_when_source_a_missing(tmp_path: Path) -> None:
    """reject path: source A gone → ok=True with warning, not silent success."""
    merges = tmp_path / "_pending_merges.md"
    # Note: source A path is never created on disk.
    src_a = tmp_path / "feedback_missing_a.md"
    src_b = tmp_path / "feedback_present_b.md"
    _write_source(src_b, name="present_b")

    write_pending_merge(
        merges,
        merge_target_name="ghost-merge",
        sources=[str(src_a), str(src_b)],
        rationale="ghost",
        draft_merged_body="ghost body",
        confidence=0.9,
    )
    from athenaeum.pending_merges import parse_pending_merges

    pm_id = parse_pending_merges(merges)[0].id

    result = resolve_merge(merges, pm_id, "reject")

    assert result["ok"] is True, result
    assert "warning" in result, f"expected warning field, got {result}"
    assert "refines_write_failed" in result["warning"]
    # Checkbox still flipped — half-progress is useful.
    new_text = merges.read_text(encoding="utf-8")
    assert "- [x]" in new_text
    assert "**Decision**: reject" in new_text


def test_resolve_merge_reject_uses_source_b_frontmatter_name(tmp_path: Path) -> None:
    """refines: declaration must use source B's frontmatter name, not stem."""
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "feedback_a_side.md"
    # Filename stem ('weird-stem') differs from frontmatter name.
    src_b = tmp_path / "feedback_weird-stem.md"
    _write_source(src_a, name="a_side")
    _write_source(src_b, name="real-canonical-name")

    write_pending_merge(
        merges,
        merge_target_name="canonical-merge",
        sources=[str(src_a), str(src_b)],
        rationale="r",
        draft_merged_body="b",
        confidence=0.9,
    )
    from athenaeum.pending_merges import parse_pending_merges

    pm_id = parse_pending_merges(merges)[0].id

    result = resolve_merge(merges, pm_id, "reject")
    assert result["ok"] is True
    assert "warning" not in result

    meta, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
    refines = meta.get("refines")
    assert isinstance(refines, list)
    assert "real-canonical-name" in refines, refines
    # The filename stem should NOT be the declared value.
    assert "weird-stem" not in refines


def test_resolve_merge_approve_fails_when_target_exists(tmp_path: Path) -> None:
    """approve must not overwrite an existing wiki/<slug>.md."""
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "feedback_x.md"
    src_b = tmp_path / "feedback_y.md"
    _write_source(src_a, name="x")
    _write_source(src_b, name="y")

    write_pending_merge(
        merges,
        merge_target_name="existing-name",
        sources=[str(src_a), str(src_b)],
        rationale="r",
        draft_merged_body="draft",
        confidence=0.9,
    )
    from athenaeum.pending_merges import parse_pending_merges

    pm_id = parse_pending_merges(merges)[0].id

    # Pre-create the target wiki entry.
    target = tmp_path / "existing-name.md"
    target.write_text("PRE-EXISTING\n", encoding="utf-8")

    result = resolve_merge(merges, pm_id, "approve")

    assert result["ok"] is False
    assert result["error_code"] == "target_exists"
    assert "already exists" in result["message"]
    # Wiki target untouched.
    assert target.read_text(encoding="utf-8") == "PRE-EXISTING\n"
    # Checkbox still unchecked — merge remains pending.
    md = merges.read_text(encoding="utf-8")
    assert "- [ ]" in md
    assert "- [x]" not in md
    assert "**Decision**:" not in md


def test_split_blocks_ignores_fenced_frontmatter_and_subheadings(
    tmp_path: Path,
) -> None:
    """Draft bodies with embedded ``---`` frontmatter and ``## `` headings
    must round-trip intact — not get truncated by ``_split_blocks``
    mistaking their contents for real block/paragraph delimiters
    (issue #289).
    """
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "feedback_fence_a.md"
    src_b = tmp_path / "feedback_fence_b.md"
    _write_source(src_a, name="fence_a")
    _write_source(src_b, name="fence_b")

    draft_body = (
        "---\n"
        "uid: abc123\n"
        "name: fenced-merge\n"
        "---\n"
        "\n"
        "## A markdown subheading\n"
        "\n"
        "Body content that must survive the round-trip.\n"
        "\n"
        "## Another subheading\n"
        "\n"
        "More content after the second heading."
    )

    write_pending_merge(
        merges,
        merge_target_name="fenced-merge",
        sources=[str(src_a), str(src_b)],
        rationale="has embedded frontmatter and subheadings",
        draft_merged_body=draft_body,
        confidence=0.9,
    )

    from athenaeum.pending_merges import list_pending_merges, parse_pending_merges

    pms = parse_pending_merges(merges)
    assert len(pms) == 1
    assert pms[0].draft_merged_body == draft_body

    listed = list_pending_merges(merges)
    assert len(listed) == 1
    assert listed[0]["draft_merged_body"] == draft_body

    pm_id = pms[0].id
    result = resolve_merge(merges, pm_id, "approve")
    assert result["ok"] is True, result

    target = tmp_path / "fenced-merge.md"
    assert target.read_text(encoding="utf-8") == draft_body


def test_split_blocks_fenced_draft_followed_by_second_block(tmp_path: Path) -> None:
    """Realistic production shape: a fenced draft with embedded ``---``
    and ``## `` content, immediately followed by another ``## Merge:``
    block (``write_pending_merge`` always separates entries with
    ``\\n\\n---\\n\\n``). Both blocks must parse correctly and
    independently — the first block's fenced content must not bleed
    into the second, and the second must not be lost (Quine review of
    PR #291, issue #289).
    """
    merges = tmp_path / "_pending_merges.md"
    src_a1 = tmp_path / "feedback_first_a.md"
    src_b1 = tmp_path / "feedback_first_b.md"
    src_a2 = tmp_path / "feedback_second_a.md"
    src_b2 = tmp_path / "feedback_second_b.md"
    for p, n in [
        (src_a1, "first_a"),
        (src_b1, "first_b"),
        (src_a2, "second_a"),
        (src_b2, "second_b"),
    ]:
        _write_source(p, name=n)

    first_draft = (
        "---\n"
        "uid: fenced-first\n"
        "---\n"
        "\n"
        "## Embedded subheading\n"
        "\n"
        "Fenced content for the first block."
    )
    second_draft = "Plain draft body for the second block."

    write_pending_merge(
        merges,
        merge_target_name="fenced-first",
        sources=[str(src_a1), str(src_b1)],
        rationale="first block has embedded frontmatter and a subheading",
        draft_merged_body=first_draft,
        confidence=0.8,
    )
    write_pending_merge(
        merges,
        merge_target_name="plain-second",
        sources=[str(src_a2), str(src_b2)],
        rationale="second block is a plain trailing merge",
        draft_merged_body=second_draft,
        confidence=0.7,
    )

    from athenaeum.pending_merges import list_pending_merges, parse_pending_merges

    pms = parse_pending_merges(merges)
    assert len(pms) == 2
    by_name = {pm.merge_target_name: pm for pm in pms}
    assert set(by_name) == {"fenced-first", "plain-second"}
    assert by_name["fenced-first"].draft_merged_body == first_draft
    assert by_name["plain-second"].draft_merged_body == second_draft

    listed = {item["merge_target_name"]: item for item in list_pending_merges(merges)}
    assert len(listed) == 2
    assert listed["fenced-first"]["draft_merged_body"] == first_draft
    assert listed["plain-second"]["draft_merged_body"] == second_draft


def test_split_blocks_recovers_from_unclosed_fence_before_next_block(
    tmp_path: Path,
) -> None:
    """A malformed block whose ```markdown fence is never closed must not
    silently swallow the next well-formed block. Content of the
    malformed block can still be imperfect — the point is the second
    block is not lost (Quine review of PR #291, issue #289).
    """
    merges = tmp_path / "_pending_merges.md"
    text = (
        "# Pending Merges\n\n"
        '## [2026-01-01] Merge: "malformed-a"\n'
        "- [ ] Approve this merge? Sources: a, b\n\n"
        "**Rationale**: malformed, fence never closes\n"
        "**Sources**:\n"
        "- a\n"
        "- b\n"
        "**Confidence**: 0.50\n"
        "**Draft**:\n"
        "```markdown\n"
        "unterminated draft body\n"
        "\n"
        "---\n\n"
        '## [2026-01-02] Merge: "well-formed-b"\n'
        "- [ ] Approve this merge? Sources: c, d\n\n"
        "**Rationale**: well-formed, must still be recognized\n"
        "**Sources**:\n"
        "- c\n"
        "- d\n"
        "**Confidence**: 0.90\n"
        "**Draft**:\n"
        "```markdown\n"
        "well-formed body\n"
        "```\n"
    )
    merges.write_text(text, encoding="utf-8")

    from athenaeum.pending_merges import _split_blocks, parse_pending_merges

    blocks = _split_blocks(text)
    assert len(blocks) == 2, f"expected 2 blocks, got {len(blocks)}: {blocks}"
    assert 'Merge: "malformed-a"' in blocks[0]
    assert 'Merge: "well-formed-b"' in blocks[1]
    assert "malformed-a" not in blocks[1]

    pms = parse_pending_merges(merges)
    assert len(pms) == 2
    by_name = {pm.merge_target_name: pm for pm in pms}
    assert set(by_name) == {"malformed-a", "well-formed-b"}
    assert by_name["well-formed-b"].draft_merged_body == "well-formed body"
