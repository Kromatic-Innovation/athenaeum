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


def test_ingest_resolved_merges_archives_and_is_idempotent(tmp_path: Path) -> None:
    """Issue #299: resolved blocks move to the archive on first run; a
    second run with no new resolutions is a no-op (mirrors
    ``answers.ingest_answers``'s idempotency contract).
    """
    from athenaeum.pending_merges import ingest_resolved_merges

    merges = tmp_path / "_pending_merges.md"
    merges.write_text(
        "# Pending Merges\n\n"
        '## [2026-05-29] Merge: "resolved-one"\n'
        "- [x] Approve this merge? Sources: a, b\n\n"
        "**Rationale**: r\n**Sources**:\n- a\n- b\n"
        "**Confidence**: 0.90\n**Draft**:\n```markdown\nbody\n```\n\n"
        "**Decision**: approve\n\n"
        "---\n\n"
        '## [2026-06-01] Merge: "still-open"\n'
        "- [ ] Approve this merge? Sources: c, d\n\n"
        "**Rationale**: r2\n**Sources**:\n- c\n- d\n"
        "**Confidence**: 0.80\n**Draft**:\n```markdown\nbody2\n```\n",
        encoding="utf-8",
    )

    count = ingest_resolved_merges(merges)
    assert count == 1

    primary = merges.read_text(encoding="utf-8")
    assert "resolved-one" not in primary
    assert "still-open" in primary

    archive = tmp_path / "_pending_merges_archive.md"
    assert archive.exists()
    assert "resolved-one" in archive.read_text(encoding="utf-8")

    # Idempotent: re-running with nothing newly resolved archives nothing.
    count_again = ingest_resolved_merges(merges)
    assert count_again == 0
    assert "still-open" in merges.read_text(encoding="utf-8")


def test_ingest_resolved_merges_archives_rejected_block_with_note(
    tmp_path: Path,
) -> None:
    """A ``reject``-resolved block (with a **Note**:) archives the same way
    as an ``approve``-resolved one — the prior test only exercised approve.
    """
    from athenaeum.pending_merges import ingest_resolved_merges

    merges = tmp_path / "_pending_merges.md"
    merges.write_text(
        "# Pending Merges\n\n"
        '## [2026-05-29] Merge: "rejected-one"\n'
        "- [x] Approve this merge? Sources: a, b\n\n"
        "**Rationale**: r\n**Sources**:\n- a\n- b\n"
        "**Confidence**: 0.90\n**Draft**:\n```markdown\nbody\n```\n\n"
        "**Decision**: reject\n"
        "**Note**: duplicate of an unrelated topic\n",
        encoding="utf-8",
    )

    count = ingest_resolved_merges(merges)
    assert count == 1
    assert "rejected-one" not in merges.read_text(encoding="utf-8")

    archived = (tmp_path / "_pending_merges_archive.md").read_text(encoding="utf-8")
    assert "rejected-one" in archived
    assert "**Decision**: reject" in archived
    assert "duplicate of an unrelated topic" in archived


def test_ingest_resolved_merges_newest_archived_first(tmp_path: Path) -> None:
    """A second run's newly-archived block is prepended above the first
    run's archived block — the archive's documented newest-first contract.
    """
    from athenaeum.pending_merges import ingest_resolved_merges

    merges = tmp_path / "_pending_merges.md"
    archive = tmp_path / "_pending_merges_archive.md"

    def _resolved_block(name: str) -> str:
        return (
            f'## [2026-05-29] Merge: "{name}"\n'
            "- [x] Approve this merge? Sources: a, b\n\n"
            "**Rationale**: r\n**Sources**:\n- a\n- b\n"
            "**Confidence**: 0.90\n**Draft**:\n```markdown\nbody\n```\n\n"
            "**Decision**: approve\n"
        )

    merges.write_text("# Pending Merges\n\n" + _resolved_block("first-round") + "\n")
    assert ingest_resolved_merges(merges) == 1

    merges.write_text("# Pending Merges\n\n" + _resolved_block("second-round") + "\n")
    assert ingest_resolved_merges(merges) == 1

    archived_text = archive.read_text(encoding="utf-8")
    assert archived_text.index("second-round") < archived_text.index("first-round")


def test_resolve_merge_then_ingest_end_to_end(tmp_path: Path) -> None:
    """Real producer→consumer contract: ``resolve_merge``'s own
    ``_rewrite_block_resolved`` output (not a hand-crafted approximation)
    must be exactly what ``ingest_resolved_merges`` recognizes as resolved.
    """
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "feedback_e2e_a.md"
    src_b = tmp_path / "feedback_e2e_b.md"
    _write_source(src_a, name="e2e_a")
    _write_source(src_b, name="e2e_b")

    write_pending_merge(
        merges,
        merge_target_name="e2e-merge",
        sources=[str(src_a), str(src_b)],
        rationale="end to end",
        draft_merged_body="Merged body.",
        confidence=0.9,
    )
    from athenaeum.pending_merges import parse_pending_merges

    pm_id = parse_pending_merges(merges)[0].id
    result = resolve_merge(merges, pm_id, "approve")
    assert result["ok"] is True, result

    from athenaeum.pending_merges import ingest_resolved_merges

    count = ingest_resolved_merges(merges)
    assert count == 1
    assert "e2e-merge" not in merges.read_text(encoding="utf-8")
    archive = tmp_path / "_pending_merges_archive.md"
    assert "e2e-merge" in archive.read_text(encoding="utf-8")


def test_scan_fence_state_transitions() -> None:
    """``_scan_fence_state`` is the single source of truth for fence
    open/close transitions, used identically by ``_split_blocks`` and
    ``_parse_block`` (issue #292).
    """
    from athenaeum.pending_merges import _scan_fence_state

    assert _scan_fence_state("plain text", 0) == 0
    assert _scan_fence_state("```markdown", 0) == 3
    assert _scan_fence_state("still open", 3) == 3
    assert _scan_fence_state("```", 3) == 0
    # A four-backtick opener requires a four-backtick closer.
    assert _scan_fence_state("````markdown", 0) == 4
    assert _scan_fence_state("```", 4) == 4
    assert _scan_fence_state("````", 4) == 0


def test_split_blocks_nested_fence_of_different_length_survives(
    tmp_path: Path,
) -> None:
    """A Draft body that embeds its own fenced snippet (e.g. documenting
    a shell command) must not have that inner fence mistaken for the
    outer ```markdown fence's closer, as long as the inner fence uses a
    different backtick-run length — the documented convention (#292).
    This pins the convention's round-trip behavior post-refactor; the
    #292 deliverable itself is the shared helper (previous test) that
    keeps ``_split_blocks``/``_parse_block`` from re-diverging, not new
    nesting capability — exact-length closing already tolerated a
    different-length nested fence before this refactor.
    """
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "feedback_nested_a.md"
    src_b = tmp_path / "feedback_nested_b.md"
    _write_source(src_a, name="nested_a")
    _write_source(src_b, name="nested_b")

    draft_body = (
        "Run this to check status:\n"
        "\n"
        "````bash\n"
        "echo done\n"
        "````\n"
        "\n"
        "More content after the nested fence."
    )

    write_pending_merge(
        merges,
        merge_target_name="nested-fence-merge",
        sources=[str(src_a), str(src_b)],
        rationale="draft embeds a four-backtick fenced snippet",
        draft_merged_body=draft_body,
        confidence=0.85,
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

    target = tmp_path / "nested-fence-merge.md"
    assert target.read_text(encoding="utf-8") == draft_body


# ---------------------------------------------------------------------------
# issue #394 — _pending_merges.md regrew to 13MB + ~30K malformed-header
# warnings/run because a merge draft copied a source body verbatim (via
# athenaeum.merge.synthesize_body), and that body contained a bare three-
# backtick code fence. Under the old three-backtick outer ```markdown fence
# the inner fence closed the outer one prematurely, leaking the draft's
# ``## From `<scope>/<file>` `` subsections out as bogus top-level blocks the
# reader rejected as malformed and could never archive — so orphan fragments
# accreted forever and flooded stderr. (Regression of the #299/#303 fix.)
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_render_block_outer_fence_outgrows_inner_code_fence() -> None:
    """The writer picks a ```markdown fence longer than any backtick run in
    the draft body, so a bare three-backtick code fence inside the body can
    never close the outer fence (issue #394)."""
    from athenaeum.pending_merges import render_block

    draft_body = "Intro paragraph.\n\n```\nnpm test\n```\n\nOutro paragraph."
    block = render_block(
        merge_target_name="fence-in-body",
        sources=["/k/user/a.md", "/k/user/b.md"],
        rationale="body copies a source that contains a bare ``` fence",
        draft_merged_body=draft_body,
        confidence=0.9,
        created_at="2026-07-19",
    )
    # A four-backtick outer fence wraps the three-backtick inner one.
    assert "````markdown" in block
    assert "```markdown" not in block.replace("````markdown", "")


def test_render_block_round_trips_draft_with_inner_code_fence() -> None:
    """A draft body containing a bare three-backtick fence must survive the
    write -> _split_blocks -> _parse_block round-trip byte-for-byte, and must
    NOT leak its ``## From`` subsections as extra blocks (issue #394)."""
    from athenaeum.merge import synthesize_body
    from athenaeum.pending_merges import (
        _parse_block,
        _split_blocks,
        render_block,
    )

    # Exactly the shape synthesize_body emits: `## From` subsections whose
    # content copies a source body that includes a bare ``` code fence.
    draft_body = synthesize_body(
        [
            ("user", "alice.md", "Alice is a PM.\n\nRun:\n```\nnpm test\n```\n\nMore."),
            ("user", "bob.md", "Bob is an engineer."),
        ]
    )
    assert "## From `user/alice.md`" in draft_body
    assert "## From `user/bob.md`" in draft_body

    block = render_block(
        merge_target_name="alice",
        sources=["/k/user/alice.md", "/k/user/bob.md"],
        rationale="same person",
        draft_merged_body=draft_body,
        confidence=0.9,
        created_at="2026-07-19",
    )
    text = "# Pending Merges\n\n" + block + "\n"

    blocks = _split_blocks(text)
    assert len(blocks) == 1, f"draft leaked into extra blocks: {blocks}"
    parsed = _parse_block(blocks[0])
    assert parsed is not None
    assert parsed.merge_target_name == "alice"
    # render_block rstrips / _parse_block strips trailing newlines (existing
    # behavior); the point here is the body content survives intact and the
    # `## From` subsections are NOT leaked out as separate blocks.
    assert parsed.draft_merged_body == draft_body.strip("\n")
    assert "## From `user/bob.md`" in parsed.draft_merged_body


def test_split_blocks_reabsorbs_leaked_from_subsections(caplog) -> None:
    """A legacy block written with the old three-backtick fence, whose draft
    leaked its ``## From`` subsections, must parse as ONE block with no
    "malformed header" warning — the leaked subsections are re-absorbed as
    content, not sprayed as bogus blocks (issue #394)."""
    import logging

    from athenaeum.pending_merges import _split_blocks, parse_pending_merges

    # Old-writer output: three-backtick outer fence, three-backtick inner fence.
    legacy = (
        "# Pending Merges\n\n"
        '## [2026-06-01] Merge: "alice"\n'
        "- [ ] Approve this merge? Sources: a, b\n\n"
        "**Rationale**: same person\n"
        "**Sources**:\n"
        "- /k/user/a.md\n"
        "- /k/user/b.md\n"
        "**Confidence**: 0.92\n"
        "**Draft**:\n"
        "```markdown\n"
        "## From `user/a.md`\n\n"
        "Alice is a PM.\n\n"
        "```\n"  # <-- bare inner fence prematurely closes the outer fence
        "npm test\n"
        "```\n\n"
        "## From `user/b.md`\n\n"  # <-- would have leaked as a bogus block
        "Alice owns the roadmap.\n"
        "```\n"
    )

    with caplog.at_level(logging.WARNING, logger="athenaeum.pending_merges"):
        blocks = _split_blocks(legacy)
    first_lines = [b.splitlines()[0] for b in blocks]
    assert first_lines == ['## [2026-06-01] Merge: "alice"'], first_lines
    assert not any(
        "malformed header" in rec.getMessage() for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "_pending_merges.md"
        p.write_text(legacy, encoding="utf-8")
        pms = parse_pending_merges(p)
        assert [pm.merge_target_name for pm in pms] == ["alice"]


def test_ingest_resolved_merges_drains_orphan_fragments_from_fixture(
    tmp_path: Path, caplog
) -> None:
    """Running ``ingest-merges`` against the regressed live-sidecar fixture
    drains the accreted orphan ``## From`` fragments, keeps the still-valid
    unresolved merge proposals, is idempotent, and emits no malformed-header
    flood (issue #394 acceptance criteria)."""
    import logging

    from athenaeum.pending_merges import (
        ingest_resolved_merges,
        parse_pending_merges,
    )

    fixture = (FIXTURES_DIR / "pending_merges_regressed.md").read_text(
        encoding="utf-8"
    )
    merges = tmp_path / "_pending_merges.md"
    merges.write_text(fixture, encoding="utf-8")

    before_from = fixture.count("## From")
    assert before_from >= 4  # valid subsections + accreted orphans

    with caplog.at_level(logging.WARNING, logger="athenaeum.pending_merges"):
        archived = ingest_resolved_merges(merges)  # RUN 1 — compaction pass

    assert archived == 0  # nothing was resolved this run
    after1 = merges.read_text(encoding="utf-8")
    assert len(after1) < len(fixture), "sidecar did not shrink"

    # The two still-valid unresolved proposals survive; the standalone orphan
    # fragments are drained.
    pms = parse_pending_merges(merges)
    assert {pm.merge_target_name for pm in pms} == {"alice-pm", "bob-eng"}
    assert "user_carol_orphan" not in after1

    # No "malformed header" flood while draining.
    assert not any(
        "malformed header" in rec.getMessage() for rec in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # Idempotent: a second run makes no further change.
    archived2 = ingest_resolved_merges(merges)  # RUN 2
    assert archived2 == 0
    assert merges.read_text(encoding="utf-8") == after1
