# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.entity_schema — the declared/observed class resolver (athenaeum#964)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.entity_schema import QUERYABLE_FIELDS, resolve_entity_classes
from athenaeum.schemas import KNOWN_TYPES


def _write_page(
    wiki: Path,
    filename: str,
    *,
    uid: str,
    page_type: str,
    name: str,
    extra: str = "",
    audience: str | None = None,
) -> None:
    lines = [f"uid: {uid}", f"type: {page_type}", f"name: {name}"]
    if audience is not None:
        lines.append(f"audience: [{audience}]")
    if extra:
        lines.append(extra)
    (wiki / filename).write_text(
        "---\n" + "\n".join(lines) + "\n---\n\nBody.\n", encoding="utf-8"
    )


class TestQueryableFields:
    def test_is_exactly_type(self) -> None:
        # Issue athenaeum#964 AC: must not advertise a field the type filter does
        # not implement. Today that set is exactly {"type"}.
        assert QUERYABLE_FIELDS == ("type",)


class TestResolveEntityClasses:
    def test_declared_and_observed_both_true(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
        _write_page(wiki, "a.md", uid="u1", page_type="person", name="Alice")

        classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert classes["person"].declared is True
        assert classes["person"].observed is True
        assert classes["person"].count == 1

    def test_observed_undeclared_class_is_reported(self, tmp_path: Path) -> None:
        # Issue athenaeum#964 AC amendment 3: a corpus carrying a type absent from
        # types.md must be listed as observed-undeclared, not omitted.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
        _write_page(wiki, "m.md", uid="u1", page_type="auto-memory", name="Memory One")

        classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert classes["auto-memory"].declared is False
        assert classes["auto-memory"].observed is True
        assert classes["auto-memory"].count == 1

    def test_declared_but_unobserved_class_has_zero_count(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text(
            "| Type |\n|---|\n| person |\n| company |\n"
        )
        _write_page(wiki, "a.md", uid="u1", page_type="person", name="Alice")

        classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert classes["company"].declared is True
        assert classes["company"].observed is False
        assert classes["company"].count == 0

    def test_missing_types_md_falls_back_to_known_types(self, tmp_path: Path) -> None:
        # No `_schema/` directory at all.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        classes = resolve_entity_classes(wiki)
        names = {c.name for c in classes}
        assert names >= KNOWN_TYPES
        # Never hard-fails -- resolves cleanly to the fallback set.
        assert len(classes) > 0

    def test_empty_types_md_falls_back_to_known_types(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("")
        classes = resolve_entity_classes(wiki)
        names = {c.name for c in classes}
        assert names >= KNOWN_TYPES

    def test_untyped_page_is_not_a_class(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "u.md").write_text("---\nuid: u1\nname: Untyped\n---\n\nBody.\n")
        classes = resolve_entity_classes(wiki)
        assert all(c.name for c in classes)

    def test_fields_union_excludes_pii_surface_fields(self, tmp_path: Path) -> None:
        # Issue athenaeum#964 AC: a key routed to an excluded surface (e.g.
        # inline `emails`) must be omitted entirely from the reported field set.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(
            wiki,
            "a.md",
            uid="u1",
            page_type="person",
            name="Alice",
            extra="emails: [a@example.com]",
        )
        classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert "emails" not in classes["person"].fields
        assert "name" in classes["person"].fields
        assert "uid" in classes["person"].fields

    def test_fields_is_union_across_pages_of_same_class(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "a.md", uid="u1", page_type="person", name="Alice")
        _write_page(
            wiki,
            "b.md",
            uid="u2",
            page_type="person",
            name="Bob",
            extra="tags: [warm]",
        )
        classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert "tags" in classes["person"].fields
        assert "name" in classes["person"].fields

    def test_restricted_caller_counts_only_authorized_pages(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(
            wiki, "a.md", uid="u1", page_type="person", name="Alice", audience="ops"
        )
        _write_page(
            wiki,
            "b.md",
            uid="u2",
            page_type="person",
            name="Bob",
            audience="finance",
        )

        owner_classes = {c.name: c for c in resolve_entity_classes(wiki)}
        assert owner_classes["person"].count == 2

        restricted_classes = {
            c.name: c for c in resolve_entity_classes(wiki, caller_audience={"ops"})
        }
        assert restricted_classes["person"].count == 1

    def test_restricted_caller_with_no_readable_pages_of_a_class_reports_zero(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(
            wiki, "a.md", uid="u1", page_type="person", name="Alice", audience="finance"
        )
        classes = {
            c.name: c for c in resolve_entity_classes(wiki, caller_audience={"ops"})
        }
        assert classes["person"].observed is False
        assert classes["person"].count == 0

    def test_missing_wiki_root_does_not_raise(self, tmp_path: Path) -> None:
        classes = resolve_entity_classes(tmp_path / "does-not-exist")
        assert isinstance(classes, tuple)

    def test_result_sorted_by_name(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "a.md", uid="u1", page_type="zzz-type", name="Z")
        _write_page(wiki, "b.md", uid="u2", page_type="aaa-type", name="A")
        names = [c.name for c in resolve_entity_classes(wiki)]
        assert names == sorted(names)
