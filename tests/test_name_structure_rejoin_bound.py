# SPDX-License-Identifier: Apache-2.0
"""``name_structure`` must not propose re-merging a split-created sub-page
back into its parent while they are still large (issue athenaeum#2540 AC12).

:func:`athenaeum.name_structure.scan_qualified_name_splits` matches pages
shaped ``"<Parent> (<Sub-topic>)"`` -- exactly the name
:mod:`athenaeum.page_recompile_apply` mints for a new sub-page (AC4). Without
this gate, the very next qualified-name scan would propose folding that
sub-page straight back into its parent, which the oversize gate would then
split right back apart.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import render_frontmatter
from athenaeum.name_structure import exceeds_split_rejoin_bound, scan_qualified_name_splits
from athenaeum.page_relations import set_relation


def _write_page(wiki: Path, *, uid: str, name: str, body: str, **extra: object) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"uid": uid, "type": "project", "name": name}
    meta.update(extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


#: A bare page's `related` row for its split-created child -- the shape
#: `via` is actually recorded in (on the PARENT, not the child's own
#: `parent:` key -- see `read_relations`'s own docstring on that asymmetry).
def _split_related_row(child_uid: str) -> list[dict[str, str]]:
    return [{"uid": child_uid, "role": "child", "via": "split"}]


class TestExceedsSplitRejoinBound:
    """Direct unit tests of the predicate, at both sides of the bound."""

    def test_false_when_there_is_no_relation_at_all(self) -> None:
        assert not exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1"},
            bare_body="x" * 1000,
            qualified_meta={"uid": "c1"},
            qualified_body="y" * 1000,
            config=None,
        )

    def test_false_when_the_parent_relation_is_not_via_split(self) -> None:
        assert not exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1", "related": [{"uid": "c1", "role": "child", "via": "manual"}]},
            bare_body="x" * 100_000,
            qualified_meta={"uid": "c1", "parent": "p1"},
            qualified_body="y" * 100_000,
            config=None,
        )

    def test_below_the_fraction_is_not_blocked(self) -> None:
        # Default threshold 10,000 * default fraction 0.5 = 5,000. Combined
        # well under that.
        assert not exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1", "related": _split_related_row("c1")},
            bare_body="x" * 1000,
            qualified_meta={"uid": "c1", "parent": "p1"},
            qualified_body="y" * 1000,
            config=None,
        )

    def test_above_the_fraction_is_blocked(self) -> None:
        assert exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1", "related": _split_related_row("c1")},
            bare_body="x" * 3000,
            qualified_meta={"uid": "c1", "parent": "p1"},
            qualified_body="y" * 3000,
            config=None,
        )

    def test_both_sides_of_a_configured_bound(self) -> None:
        config = {"librarian": {"page_recompile": {"rejoin_fraction": 0.1}}}
        # threshold(default 10,000) * 0.1 = 1,000.
        assert not exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1", "related": _split_related_row("c1")},
            bare_body="x" * 400,
            qualified_meta={"uid": "c1", "parent": "p1"},
            qualified_body="y" * 400,
            config=config,
        )
        assert exceeds_split_rejoin_bound(
            bare_meta={"uid": "p1", "related": _split_related_row("c1")},
            bare_body="x" * 600,
            qualified_meta={"uid": "c1", "parent": "p1"},
            qualified_body="y" * 600,
            config=config,
        )


class TestScanQualifiedNameSplitsRespectsTheGate:
    def test_a_large_split_sub_page_is_not_proposed(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, uid="parent01", name="Acme", body="x" * 4000)
        _write_page(wiki, uid="child001", name="Acme (Pricing)", body="y" * 4000)
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="split")

        splits = scan_qualified_name_splits(wiki)
        assert splits == []

    def test_a_manually_attached_same_shaped_pair_is_still_proposed(self, tmp_path: Path) -> None:
        """The gate is scoped to via="split" only -- an operator's own
        `relate --parent` (via="manual") on an equally large pair is a
        DIFFERENT decision this issue does not touch."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, uid="parent01", name="Acme", body="x" * 4000)
        _write_page(wiki, uid="child001", name="Acme (Pricing)", body="y" * 4000)
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="manual")

        splits = scan_qualified_name_splits(wiki)
        assert len(splits) == 1

    def test_a_small_split_sub_page_is_still_proposed(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, uid="parent01", name="Acme", body="x" * 100)
        _write_page(wiki, uid="child001", name="Acme (Pricing)", body="y" * 100)
        set_relation(wiki, child_uid="child001", parent_uid="parent01", via="split")

        splits = scan_qualified_name_splits(wiki)
        assert len(splits) == 1
        assert splits[0].bare_name == "Acme"
        assert splits[0].qualified_name == "Acme (Pricing)"
