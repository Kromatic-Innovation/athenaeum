# SPDX-License-Identifier: Apache-2.0
"""Pure record merge and derived take-one-side (athenaeum#2929).

No repository, no file: every file version here is a string. The sections are
``docs/design/federation-multi-writer-sync.md`` section 5.3 and the ``derived``
row of section 5. Every name is invented.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import athenaeum.record_merge as record_merge
from athenaeum.pending_merges import (
    PendingMerge,
    parse_pending_merges_text,
    render_block,
    render_pending_merges,
)
from athenaeum.record_merge import (
    KIND_PENDING_MERGES,
    HeldRecord,
    merge_derived,
    merge_records,
)


def block(name: str, day: str = "2026-03-01", rationale: str = "same person") -> str:
    return render_block(
        merge_target_name=name,
        sources=[f"/wiki/{name}-a.md", f"/wiki/{name}-b.md"],
        rationale=rationale,
        draft_merged_body=f"# {name}\n",
        confidence=0.9,
        created_at=day,
        write_kind="create-merged",
    )


def resolved(text: str, decision: str) -> str:
    return text.replace("- [ ]", "- [x]", 1) + f"\n**Decision**: {decision}"


def doc(*blocks: str) -> str:
    return "\n\n---\n\n".join(["# Pending Merges", *blocks]) + "\n"


def ids(text: str) -> list[str]:
    return [m.id for m in parse_pending_merges_text(text)]


def by_name(text: str) -> dict[str, PendingMerge]:
    return {m.merge_target_name: m for m in parse_pending_merges_text(text)}


def test_disjoint_records_union_in_stable_order() -> None:
    a = block("Jordan Reyes", "2026-03-01")
    b = block("Priya Raman", "2026-03-05")
    c = block("Acme Corp", "2026-02-01")
    r = merge_records(KIND_PENDING_MERGES, doc(a, b), doc(c))
    assert r.held == ()
    assert [m.merge_target_name for m in parse_pending_merges_text(r.text)] == [
        "Acme Corp",
        "Jordan Reyes",
        "Priya Raman",
    ]
    assert set(r.resolvers.values()) == {"local-only", "remote-only"}


def test_order_does_not_depend_on_which_side_is_which() -> None:
    a, b = block("Jordan Reyes"), block("Priya Raman", "2026-03-05")
    assert (
        merge_records(KIND_PENDING_MERGES, doc(a), doc(b)).text
        == merge_records(KIND_PENDING_MERGES, doc(b), doc(a)).text
    )


def test_blocks_round_trip_through_the_parser_and_renderer() -> None:
    text = doc(block("Jordan Reyes"), resolved(block("Priya Raman"), "approve"))
    assert render_pending_merges(parse_pending_merges_text(text)) == text
    assert merge_records(KIND_PENDING_MERGES, text, text).text == text


def test_resolved_on_one_side_wins_over_open() -> None:
    open_ = block("Jordan Reyes")
    done = resolved(open_, "approve")
    for local, remote in ((doc(done), doc(open_)), (doc(open_), doc(done))):
        r = merge_records(KIND_PENDING_MERGES, local, remote)
        assert r.held == ()
        (only,) = parse_pending_merges_text(r.text)
        assert only.resolved and only.decision == "approve"
        assert r.resolvers[only.id] == "resolved-wins"


def test_resolved_differently_is_held_not_settled() -> None:
    open_ = block("Jordan Reyes")
    r = merge_records(
        KIND_PENDING_MERGES,
        doc(resolved(open_, "approve")),
        doc(resolved(open_, "reject")),
    )
    (only,) = parse_pending_merges_text(r.text)
    assert r.held == (HeldRecord(only.id, "resolved-differently"),)
    assert r.resolvers[only.id] == "held"
    assert only.decision == "reject"  # the remote record; the text still parses
    assert "<<<<<<<" not in r.text


def test_resolved_the_same_way_on_both_sides_is_settled() -> None:
    open_ = block("Jordan Reyes")
    r = merge_records(
        KIND_PENDING_MERGES,
        doc(resolved(open_, "approve") + "\n**Note**: yes"),
        doc(resolved(open_, "approve")),
    )
    assert r.held == ()
    assert list(r.resolvers.values()) == ["same-outcome"]


def test_open_on_both_sides_with_different_text_keeps_remote_and_reports_it() -> None:
    r = merge_records(
        KIND_PENDING_MERGES,
        doc(block("Jordan Reyes", rationale="first")),
        doc(block("Jordan Reyes", rationale="second")),
    )
    assert r.held == ()
    assert by_name(r.text)["Jordan Reyes"].rationale == "second"
    assert list(r.resolvers.values()) == ["open-differs"]


def test_unparseable_block_holds_the_whole_file_with_remote_text() -> None:
    bad = doc(block("Jordan Reyes")) + '\n---\n\n## [2026-03-01] Merge: "No Checkbox"\nbody\n'
    remote = doc(block("Priya Raman"))
    r = merge_records(KIND_PENDING_MERGES, bad, remote)
    assert r.held == (HeldRecord("", "unparseable"),)
    assert r.text == remote


def test_unknown_kind_is_held_whole() -> None:
    r = merge_records("mystery-ledger", "a", "b")
    assert r.held == (HeldRecord("", "unknown-kind"),)
    assert r.text == "b"


def test_derived_file_is_never_merged() -> None:
    r = merge_derived("local index\n", "remote index\n")
    assert (r.text, r.taken, r.regenerate) == ("remote index\n", "remote", True)
    r = merge_derived("local index\n", "remote index\n", take="local")
    assert (r.text, r.taken, r.regenerate) == ("local index\n", "local", True)


def test_derived_file_equal_on_both_sides_needs_no_regeneration() -> None:
    assert merge_derived("x\n", "x\n").regenerate is False


def test_derived_rejects_an_unknown_side() -> None:
    with pytest.raises(ValueError):
        merge_derived("a", "b", take="both")


def test_module_imports_no_io_git_or_model_surface() -> None:
    tree = ast.parse(Path(record_merge.__file__).read_text(encoding="utf-8"))
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
        if (n.module if isinstance(n, ast.ImportFrom) else a.name)
    }
    assert imported <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "typing",
        "athenaeum.pending_merges",
    }
