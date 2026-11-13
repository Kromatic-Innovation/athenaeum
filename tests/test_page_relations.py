# SPDX-License-Identifier: Apache-2.0
"""Tests for the first-class parent/child page relation (issue athenaeum#2535).

Covers every acceptance criterion:

- AC1: shape -- child gets a top-level ``parent:`` key, parent gets one
  ``related`` row per child (``{uid, role: child, via}``).
- AC2: :func:`set_relation`/:func:`remove_relation` write both pages in one
  all-or-nothing operation; a failure injected after the first write leaves
  both pages byte-identical to before.
- AC3: one refusal test each for a second parent, a cycle, a third nesting
  level, and an inactive parent -- no write in any case.
- AC4: a pre-existing ``split-into``/``split-from`` pair reads as the
  relation with ``via: "split"``, with no migration run.
- AC5: idempotent rendering -- a second write of the same relation is a
  byte-for-byte no-op; an attached page's body is otherwise unchanged.
- AC7: ``count_one_sided_relations`` counts exactly the mismatched pairs.
- AC8: ``parent`` is accepted by frontmatter validation.

Every fixture lives under ``tmp_path`` only (public-safe-lint: no home-
directory literals), and no fixture page cites a real GitHub issue number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import schemas
from athenaeum.models import parse_frontmatter
from athenaeum.page_relations import (
    ChildRef,
    ParentRef,
    RelationError,
    count_one_sided_relations,
    read_relations,
    remove_relation,
    set_relation,
)


def _page(
    *,
    uid: str,
    name: str,
    ptype: str = "reference",
    parent: str | None = None,
    related: list[dict[str, str]] | None = None,
    description: str | None = None,
    extra_meta: dict[str, object] | None = None,
    body: str = "Original body text.\n",
) -> str:
    lines = ["---", f"uid: {uid}", f"type: {ptype}", f"name: {name}"]
    if description is not None:
        lines.append(f"description: {description}")
    if parent is not None:
        lines.append(f"parent: {parent}")
    if related is not None:
        lines.append("related:")
        for row in related:
            row_items = list(row.items())
            first_key, first_val = row_items[0]
            lines.append(f"  - {first_key}: {first_val}")
            for key, val in row_items[1:]:
                lines.append(f"    {key}: {val}")
    if extra_meta:
        for key, val in extra_meta.items():
            lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip("\n"))
    lines.append("")
    return "\n".join(lines)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _write(wiki: Path, uid: str, name: str, **kwargs) -> Path:
    path = wiki / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(_page(uid=uid, name=name, **kwargs), encoding="utf-8")
    return path


class TestSetRelationShape:
    """AC1: the on-disk shape after a successful set_relation."""

    def test_child_gets_top_level_parent_key(self, wiki: Path) -> None:
        _write(wiki, "parent01", "Acme Corp")
        child_path = _write(wiki, "child001", "Client Counts")

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        meta, _ = parse_frontmatter(child_path.read_text(encoding="utf-8"))
        assert meta["parent"] == "parent01"

    def test_parent_gets_one_related_row_shaped_uid_role_via(self, wiki: Path) -> None:
        parent_path = _write(wiki, "parent01", "Acme Corp")
        _write(wiki, "child001", "Client Counts")

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="attach")

        meta, _ = parse_frontmatter(parent_path.read_text(encoding="utf-8"))
        assert meta["related"] == [{"uid": "child001", "role": "child", "via": "attach"}]


class TestAtomicity:
    """AC2: one writer, one remover, each all-or-nothing across both pages."""

    def test_failure_after_first_write_leaves_both_pages_unchanged(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent_path = _write(wiki, "parent01", "Acme Corp")
        child_path = _write(wiki, "child001", "Client Counts")
        original_parent = parent_path.read_text(encoding="utf-8")
        original_child = child_path.read_text(encoding="utf-8")

        import athenaeum.page_relations as pr_mod

        real_atomic_write = pr_mod.atomic_write_text
        calls = {"n": 0}

        def _flaky(path, text, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure after first write")
            return real_atomic_write(path, text, **kwargs)

        monkeypatch.setattr(pr_mod, "atomic_write_text", _flaky)

        with pytest.raises(RuntimeError, match="injected failure"):
            set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        assert parent_path.read_text(encoding="utf-8") == original_parent
        assert child_path.read_text(encoding="utf-8") == original_child

    def test_remove_relation_round_trips(self, wiki: Path) -> None:
        parent_path = _write(wiki, "parent01", "Acme Corp")
        child_path = _write(wiki, "child001", "Client Counts")
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        remove_relation(wiki, child_uid="child001")

        child_meta, _ = parse_frontmatter(child_path.read_text(encoding="utf-8"))
        parent_meta, _ = parse_frontmatter(parent_path.read_text(encoding="utf-8"))
        assert "parent" not in child_meta
        assert not parent_meta.get("related")

    def test_remove_relation_failure_after_first_write_leaves_both_unchanged(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent_path = _write(wiki, "parent01", "Acme Corp")
        child_path = _write(wiki, "child001", "Client Counts")
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")
        original_parent = parent_path.read_text(encoding="utf-8")
        original_child = child_path.read_text(encoding="utf-8")

        import athenaeum.page_relations as pr_mod

        real_atomic_write = pr_mod.atomic_write_text
        calls = {"n": 0}

        def _flaky(path, text, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure after first write")
            return real_atomic_write(path, text, **kwargs)

        monkeypatch.setattr(pr_mod, "atomic_write_text", _flaky)

        with pytest.raises(RuntimeError, match="injected failure"):
            remove_relation(wiki, child_uid="child001")

        assert parent_path.read_text(encoding="utf-8") == original_parent
        assert child_path.read_text(encoding="utf-8") == original_child


class TestRefusals:
    """AC3: one test per named refusal -- no write in any case."""

    def test_refuses_second_different_parent(self, wiki: Path) -> None:
        _write(wiki, "parentA1", "Parent A")
        parent_b = _write(wiki, "parentB1", "Parent B")
        child_path = _write(wiki, "child001", "Child")
        set_relation(wiki, child_uid="child001", parent_uid="parentA1", via="manual")
        before_child = child_path.read_text(encoding="utf-8")
        before_parent_b = parent_b.read_text(encoding="utf-8")

        with pytest.raises(RelationError, match="already has a parent"):
            set_relation(wiki, child_uid="child001", parent_uid="parentB1", via="manual")

        assert child_path.read_text(encoding="utf-8") == before_child
        assert parent_b.read_text(encoding="utf-8") == before_parent_b

    def test_same_relation_twice_is_not_a_second_parent_refusal(self, wiki: Path) -> None:
        _write(wiki, "parent01", "Parent")
        _write(wiki, "child001", "Child")
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")
        # Re-affirming the SAME (child, parent, via) must succeed, not refuse.
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

    def test_refuses_self_parent_cycle(self, wiki: Path) -> None:
        page_path = _write(wiki, "selfy001", "Selfy")
        before = page_path.read_text(encoding="utf-8")

        with pytest.raises(RelationError, match="cycle"):
            set_relation(wiki, child_uid="selfy001", parent_uid="selfy001", via="manual")

        assert page_path.read_text(encoding="utf-8") == before

    def test_refuses_third_nesting_level(self, wiki: Path) -> None:
        _write(wiki, "grandpa1", "Grandparent")
        parent_path = _write(wiki, "parent01", "Parent")
        set_relation(wiki, child_uid="parent01", parent_uid="grandpa1", via="manual")
        new_leaf = _write(wiki, "leaf0001", "Leaf")
        before_parent = parent_path.read_text(encoding="utf-8")
        before_leaf = new_leaf.read_text(encoding="utf-8")

        with pytest.raises(RelationError, match="third nesting level"):
            set_relation(wiki, child_uid="leaf0001", parent_uid="parent01", via="manual")

        assert parent_path.read_text(encoding="utf-8") == before_parent
        assert new_leaf.read_text(encoding="utf-8") == before_leaf

    def test_refuses_inactive_tombstoned_parent(self, wiki: Path) -> None:
        # Real tombstone shape per models.is_tombstone/stamp_tombstone:
        # status: folded (+ folded_into, informational only for this check).
        parent_path = _write(
            wiki,
            "deadpar1",
            "Dead Parent",
            extra_meta={"status": "folded", "folded_into": "somewhere"},
        )
        child_path = _write(wiki, "child001", "Child")
        before_child = child_path.read_text(encoding="utf-8")
        before_parent = parent_path.read_text(encoding="utf-8")

        with pytest.raises(RelationError, match="inactive"):
            set_relation(wiki, child_uid="child001", parent_uid="deadpar1", via="manual")

        assert child_path.read_text(encoding="utf-8") == before_child
        assert parent_path.read_text(encoding="utf-8") == before_parent

    def test_refuses_retired_parent(self, wiki: Path) -> None:
        _write(wiki, "retpar01", "Retired Parent", extra_meta={"retired": "true"})
        child_path = _write(wiki, "child001", "Child")
        before = child_path.read_text(encoding="utf-8")

        with pytest.raises(RelationError, match="inactive"):
            set_relation(wiki, child_uid="child001", parent_uid="retpar01", via="manual")

        assert child_path.read_text(encoding="utf-8") == before


class TestLegacyReadCompat:
    """AC4: pre-existing split-into/split-from rows read as via: split, with
    no migration run."""

    def test_legacy_split_pair_reads_as_split_relation(self, wiki: Path) -> None:
        _write(
            wiki,
            "hub00001",
            "Hub Page",
            related=[{"uid": "leaf00001", "role": "split-into"}],
        )
        _write(
            wiki,
            "leaf00001",
            "Leaf Page",
            related=[{"uid": "hub00001", "role": "split-from"}],
        )

        hub_meta, _ = parse_frontmatter((wiki / "hub00001-hub-page.md").read_text(encoding="utf-8"))
        leaf_meta, _ = parse_frontmatter(
            (wiki / "leaf00001-leaf-page.md").read_text(encoding="utf-8")
        )

        hub_rel = read_relations(hub_meta)
        leaf_rel = read_relations(leaf_meta)

        assert hub_rel.children == (ChildRef(uid="leaf00001", via="split"),)
        assert leaf_rel.parent == ParentRef(uid="hub00001", via="split")

        # No migration: the files on disk are untouched by a pure read.
        assert "split-into" in (wiki / "hub00001-hub-page.md").read_text(encoding="utf-8")
        assert "split-from" in (wiki / "leaf00001-leaf-page.md").read_text(encoding="utf-8")


class TestIdempotentRendering:
    """AC5: a second write of the same relation is a byte-for-byte no-op;
    an attached page's body is otherwise unchanged."""

    def test_second_identical_write_is_byte_for_byte_noop(self, wiki: Path) -> None:
        parent_path = _write(
            wiki, "parent01", "Acme Corp", body="Some pre-existing hub content.\n"
        )
        child_path = _write(
            wiki,
            "child001",
            "Client Counts",
            description="How many clients Acme has, by year.",
            body="The actual client-count figures go here.\n",
        )

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")
        after_first_parent = parent_path.read_text(encoding="utf-8")
        after_first_child = child_path.read_text(encoding="utf-8")

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        assert parent_path.read_text(encoding="utf-8") == after_first_parent
        assert child_path.read_text(encoding="utf-8") == after_first_child

    def test_attached_body_otherwise_unchanged(self, wiki: Path) -> None:
        _write(wiki, "parent01", "Acme Corp", body="Hub prose, paragraph one.\n")
        child_path = _write(
            wiki,
            "child001",
            "Client Counts",
            body="Untouched prose that must survive verbatim.\n",
        )

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="attach")

        _, new_body = parse_frontmatter(child_path.read_text(encoding="utf-8"))
        assert "Untouched prose that must survive verbatim." in new_body

    def test_parent_body_gets_sub_pages_section_with_wikilink_and_description(
        self, wiki: Path
    ) -> None:
        parent_path = _write(wiki, "parent01", "Acme Corp")
        _write(
            wiki,
            "child001",
            "Client Counts",
            description="How many clients Acme has.",
        )

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        _, parent_body = parse_frontmatter(parent_path.read_text(encoding="utf-8"))
        assert "## Sub-pages" in parent_body
        assert "[[child001|Client Counts]]" in parent_body
        assert "How many clients Acme has." in parent_body

    def test_child_body_gets_first_line_breadcrumb(self, wiki: Path) -> None:
        _write(wiki, "parent01", "Acme Corp")
        child_path = _write(wiki, "child001", "Client Counts")

        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        _, child_body = parse_frontmatter(child_path.read_text(encoding="utf-8"))
        first_line = child_body.splitlines()[0]
        assert "[[parent01|Acme Corp]]" in first_line


class TestCountOneSidedRelations:
    """AC7: count only, no further diagnosis."""

    def test_one_sided_and_complete_pair_counts_exactly_one(self, wiki: Path) -> None:
        # Complete pair: both directions agree.
        _write(
            wiki,
            "parent01",
            "Complete Parent",
            related=[{"uid": "child001", "role": "child", "via": "manual"}],
        )
        _write(wiki, "child001", "Complete Child", parent="parent01")

        # One-sided pair: child claims a parent that does not reciprocate.
        _write(wiki, "parent02", "Lonely Parent")
        _write(wiki, "child002", "Orphaned Child", parent="parent02")

        assert count_one_sided_relations(wiki) == 1

    def test_zero_when_corpus_empty(self, wiki: Path) -> None:
        assert count_one_sided_relations(wiki) == 0


class TestFrontmatterValidation:
    """AC8: ``parent`` is accepted by frontmatter validation."""

    def test_parent_key_round_trips_through_validate_wiki_meta(self) -> None:
        model = schemas.validate_wiki_meta(
            {"uid": "child001", "type": "reference", "name": "Child", "parent": "parent01"}
        )
        assert model.parent == "parent01"

    def test_parent_key_absent_is_fine(self) -> None:
        model = schemas.validate_wiki_meta(
            {"uid": "lonely01", "type": "reference", "name": "Lonely"}
        )
        assert model.parent is None
