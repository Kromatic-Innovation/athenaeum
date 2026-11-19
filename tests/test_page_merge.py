# SPDX-License-Identifier: Apache-2.0
"""Pure three-way page merge (athenaeum#2773): one test per row of the rule table.

The table is ``docs/design/federation-multi-writer-sync.md`` section 5.1. Every
page here is invented (Jordan Reyes, Priya Raman, Acme Corp).
"""

from __future__ import annotations

from typing import Any

import pytest

from athenaeum.config import StructuredUpsertSpec
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_merge import (
    HeldUnit,
    PageMergeResult,
    merge_page,
)

MARKERS = ("<<<<<<<", "=======", ">>>>>>>")

BASE_FM: dict[str, Any] = {
    "uid": "person-jordan-reyes",
    "type": "person",
    "name": "Jordan Reyes",
    "created": "2026-01-01",
    "updated": "2026-02-01",
    "access": "internal",
    "audience": ["hr"],
    "tags": ["a", "b"],
    "stage": "lead",
    "role": "Engineer at Acme Corp",
}

BASE_BODY = (
    "# Jordan Reyes\n"
    "\n"
    "## Summary\n"
    "\n"
    "Jordan works at Acme Corp.\n"
    "Jordan reports to Priya Raman.\n"
    "Jordan leads the billing team.\n"
    "Jordan is based in Lisbon.\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Met at the Acme offsite\n"
    "- Prefers email\n"
    "\n"
    "## Profile\n"
    "\n"
    "Engineer.\n"
)


def page(body: str | None = None, **fm: Any) -> str:
    """A page: BASE_FM overridden by *fm* (a ``None`` value drops the key)."""
    meta = dict(BASE_FM)
    for k, v in fm.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    return render_frontmatter(meta) + (BASE_BODY if body is None else body)


def body_edit(old: str, new: str) -> str:
    assert old in BASE_BODY
    return BASE_BODY.replace(old, new)


SPEC = StructuredUpsertSpec(
    sections=("Summary", "Notes", "Profile"),
    replace_sections=frozenset({"profile"}),
    append_sections=frozenset({"notes"}),
    union_keys=("clients",),
    set_keys=("role",),
    ordered_keys=(("stage", ("lead", "prospect", "customer")),),
)


def fm(result: PageMergeResult) -> dict[str, object]:
    assert result.text is not None
    return parse_frontmatter(result.text)[0]


def body(result: PageMergeResult) -> str:
    assert result.text is not None
    return parse_frontmatter(result.text)[1]


def clean(result: PageMergeResult) -> None:
    assert result.contested == ()
    assert result.held == ()


# --- Table row: changed on neither side, on one side only, or identically ---


def test_row_neither_side_changed() -> None:
    b = page()
    r = merge_page(b, page(role="CTO"), page(tags=["a", "b"], role="CFO"))
    assert r.contested == ("fm:role",)
    assert r.resolvers["fm:uid"] == "unchanged"
    assert r.resolvers["fm:tags"] == "unchanged"
    assert r.resolvers["section:notes"] == "unchanged"


def test_row_changed_on_one_side_only() -> None:
    b = page()
    local = page(role="CTO at Acme Corp")
    remote = page(body=body_edit("Prefers email", "Prefers chat"))
    r = merge_page(b, local, remote, SPEC)
    clean(r)
    assert fm(r)["role"] == "CTO at Acme Corp"
    assert r.resolvers["fm:role"] == "local-only"
    assert "Prefers chat" in body(r)
    assert r.resolvers["section:notes"] == "remote-only"
    mirror = merge_page(b, remote, local, SPEC)
    assert mirror.resolvers["fm:role"] == "remote-only"
    assert fm(mirror)["role"] == "CTO at Acme Corp"


def test_row_changed_identically_on_both_sides() -> None:
    b = page()
    edited = page(role="CTO at Acme Corp", body=body_edit("Lisbon", "Porto"))
    other = page(role="CTO at Acme Corp", body=body_edit("Lisbon", "Porto") + "\nExtra.\n")
    r = merge_page(b, edited, other)
    clean(r)
    assert r.resolvers["fm:role"] == "identical"
    assert r.resolvers["section:summary"] == "identical"


# --- Table rows: deletion ---


def test_row_deleted_on_both_sides() -> None:
    r = merge_page(page(), None, None)
    assert r.deleted and r.text is None
    assert r.resolvers == {"page": "deleted-both"}
    clean(r)


def test_row_erasure_wins_over_an_edit() -> None:
    b = page()
    r = merge_page(b, None, page(role="CTO"), erasure=True)
    assert r.deleted
    assert r.resolvers == {"page": "erasure"}
    mirror = merge_page(b, page(role="CTO"), None, erasure=True)
    assert mirror.deleted


def test_row_deleted_on_one_side_untouched_on_the_other() -> None:
    r = merge_page(page(), None, page())
    assert r.deleted and r.resolvers == {"page": "deleted-one-side"}
    clean(r)


# --- Table row: updated ---


def test_row_updated_takes_the_later_date() -> None:
    b = page()
    local = page(updated="2026-03-01", role="CTO")
    remote = page(updated="2026-04-01", stage="lead")
    r = merge_page(b, local, remote)
    assert fm(r)["updated"] == "2026-04-01"
    assert r.resolvers["fm:updated"] == "updated-later"
    swapped = merge_page(b, remote, local)
    assert fm(swapped)["updated"] == "2026-04-01"


# --- Table row: list-valued keys ---


def test_row_list_key_three_way_set_merge_keeps_a_removal() -> None:
    b = page(tags=["a", "b"])
    r = merge_page(b, page(tags=["a"]), page(tags=["a", "b", "c"], role="x"))
    assert fm(r)["tags"] == ["a", "c"]
    assert r.resolvers["fm:tags"] == "set-merge"


def test_row_list_key_from_the_spec_union_keys() -> None:
    b = page(clients=["Acme Corp"])
    local = page(clients=["Acme Corp", "Globex"])
    remote = page(clients=["Acme Corp", "Initech"])
    r = merge_page(b, local, remote, SPEC)
    assert fm(r)["clients"] == ["Acme Corp", "Initech", "Globex"]
    assert r.resolvers["fm:clients"] == "set-merge"
    # Without the spec the key is an ordinary key changed differently.
    assert merge_page(b, local, remote).contested == ("fm:clients",)


@pytest.mark.parametrize("key", ["aliases", "sources"])
def test_row_list_key_default_keys(key: str) -> None:
    b = page(**{key: ["one"]})
    r = merge_page(b, page(**{key: ["one", "two"]}), page(**{key: ["one", "three"]}))
    assert fm(r)[key] == ["one", "three", "two"]
    assert r.resolvers[f"fm:{key}"] == "set-merge"


# --- Table row: ordered keys ---


def test_row_ordered_key_takes_the_value_further_along() -> None:
    b = page()
    r = merge_page(b, page(stage="customer"), page(stage="prospect"), SPEC)
    assert fm(r)["stage"] == "customer"
    assert r.resolvers["fm:stage"] == "ordered"
    r2 = merge_page(b, page(stage="prospect"), page(stage="customer"), SPEC)
    assert fm(r2)["stage"] == "customer"


def test_row_ordered_key_without_a_spec_is_contested() -> None:
    r = merge_page(page(), page(stage="customer"), page(stage="prospect"))
    assert r.contested == ("fm:stage",)
    assert fm(r)["stage"] == "prospect"


# --- Table row: append sections ---


def test_row_append_section_orders_ancestor_then_remote_then_local() -> None:
    b = page()
    local = page(body=body_edit("- Prefers email\n", "- Prefers email\n- Local note\n- Shared\n"))
    remote = page(body=body_edit("- Prefers email\n", "- Prefers email\n- Remote note\n- Shared\n"))
    r = merge_page(b, local, remote, SPEC)
    clean(r)
    assert r.resolvers["section:notes"] == "append"
    notes = body(r).split("## Notes\n")[1].split("## Profile")[0]
    lines = [ln for ln in notes.splitlines() if ln.strip()]
    assert lines == [
        "- Met at the Acme offsite",
        "- Prefers email",
        "- Remote note",
        "- Shared",
        "- Local note",
    ]


def test_row_append_section_keeps_a_removal() -> None:
    b = page()
    local = page(body=body_edit("- Prefers email\n", ""))
    remote = page(body=body_edit("- Prefers email\n", "- Prefers email\n- Remote note\n"))
    r = merge_page(b, local, remote, SPEC)
    notes = body(r).split("## Notes\n")[1].split("## Profile")[0]
    assert "Prefers email" not in notes
    assert "Remote note" in notes


# --- Table row: lines changed in different places ---


def test_row_lines_changed_in_different_places_apply_both() -> None:
    b = page()
    local = page(body=body_edit("Acme Corp.\n", "Acme Corp since 2020.\n"))
    remote = page(body=body_edit("Lisbon.\n", "Porto.\n"))
    r = merge_page(b, local, remote)
    clean(r)
    assert r.resolvers["section:summary"] == "line-merge"
    out = body(r)
    assert "since 2020" in out and "Porto" in out


# --- Table rows: held keys ---


@pytest.mark.parametrize(
    ("key", "local_value", "remote_value"),
    [
        ("uid", "person-jordan-reyes-2", "person-jordan-reyes-3"),
        ("type", "contact", "company"),
        ("created", "2026-01-05", "2026-01-09"),
        ("access", "private", "internal-plus"),
    ],
)
def test_row_identity_and_access_keys_are_held_never_contested(
    key: str, local_value: str, remote_value: str
) -> None:
    b = page()
    r = merge_page(b, page(**{key: local_value}), page(**{key: remote_value}), SPEC)
    assert r.held == (HeldUnit(f"fm:{key}", f"held-{key}"),)
    assert r.contested == ()
    assert r.resolvers[f"fm:{key}"] == "held"
    assert fm(r)[key] == remote_value


def test_row_held_key_with_the_other_side_missing_it_keeps_the_remote_state() -> None:
    r = merge_page(page(), page(created="2026-01-05"), page(created=None))
    assert r.held == (HeldUnit("fm:created", "held-created"),)
    assert "created" not in fm(r)


def test_audience_is_not_unioned() -> None:
    b = page(audience=["hr"])
    local = page(audience=["hr", "staff"])
    remote = page(audience=["hr"], body=body_edit("Prefers email", "Prefers chat"))
    r = merge_page(b, local, remote, SPEC)
    assert r.held == (HeldUnit("fm:audience", "held-audience"),)
    assert r.contested == ()
    assert fm(r)["audience"] == ["hr"]
    # The body edit still merged.
    assert "Prefers chat" in body(r)


def test_audience_is_not_set_merged_even_when_both_change_it() -> None:
    b = page(audience=["hr"])
    r = merge_page(b, page(audience=["hr", "staff"]), page(audience=["hr", "legal"]))
    assert fm(r)["audience"] == ["hr", "legal"]
    assert [h.unit for h in r.held] == ["fm:audience"]


def test_access_differing_is_held_with_the_remote_value() -> None:
    r = merge_page(page(), page(access="private"), page(access="public"))
    assert r.held == (HeldUnit("fm:access", "held-access"),)
    assert r.contested == ()
    assert fm(r)["access"] == "public"


# --- Table rows: contested ---


def test_row_other_key_changed_differently_is_contested() -> None:
    r = merge_page(page(), page(role="CTO"), page(role="CFO"))
    assert r.contested == ("fm:role",)
    assert fm(r)["role"] == "CFO"
    assert r.held == ()


def test_row_set_key_changed_differently_is_contested() -> None:
    r = merge_page(page(), page(role="CTO"), page(role="CFO"), SPEC)
    assert r.contested == ("fm:role",)
    assert fm(r)["role"] == "CFO"


def test_same_line_changed_differently_is_contested_without_markers() -> None:
    b = page()
    local = page(body=body_edit("Lisbon.", "Porto."))
    remote = page(body=body_edit("Lisbon.", "Madrid."))
    r = merge_page(b, local, remote)
    assert r.contested == ("section:summary",)
    assert r.resolvers["section:summary"] == "contested"
    assert "Madrid." in body(r) and "Porto." not in body(r)
    assert not any(m in (r.text or "") for m in MARKERS)


def test_overlapping_inserts_at_one_point_are_contested() -> None:
    b = page()
    r = merge_page(
        b,
        page(body=body_edit("Lisbon.\n", "Lisbon.\nLocal line.\n")),
        page(body=body_edit("Lisbon.\n", "Lisbon.\nRemote line.\n")),
    )
    assert r.contested == ("section:summary",)


def test_row_replace_section_changed_differently_is_contested() -> None:
    b = page()
    local = page(body=body_edit("Engineer.", "Staff engineer."))
    remote = page(body=body_edit("Engineer.", "Principal engineer."))
    r = merge_page(b, local, remote, SPEC)
    assert r.contested == ("section:profile",)
    assert "Principal engineer." in body(r)
    # The same edits in a section the spec does not replace overlap on a line: also contested.
    assert merge_page(b, local, remote).contested == ("section:profile",)


def test_row_deleted_on_one_side_edited_on_the_other_is_contested() -> None:
    b = page()
    edited = page(role="CTO")
    r = merge_page(b, None, edited)
    assert r.contested == ("page",)
    assert r.resolvers == {"page": "delete-vs-edit"}
    assert r.text == edited  # the page stays with the new fact (operator ruling 4)
    mirror = merge_page(b, edited, None)
    assert mirror.contested == ("page",)
    assert mirror.text == edited


# --- Acceptance criteria beyond the table ---


@pytest.mark.parametrize("spec", [None, SPEC])
def test_identity_returns_the_page_byte_for_byte(spec: StructuredUpsertSpec | None) -> None:
    x = page()
    r = merge_page(x, x, x, spec)
    assert r.text == x
    clean(r)


def test_identity_preserves_unusual_bytes() -> None:
    x = "---\nuid: u1\ntype: person\nname:   Jordan Reyes   # c\n---\nno trailing newline"
    r = merge_page(x, x, x, None)
    assert r.text == x


def test_one_sided_change_returns_the_changed_side() -> None:
    b = page()
    local = page(role="CTO", audience=["hr", "staff"], access="private")
    r = merge_page(b, local, b, SPEC)
    assert r.text == local
    clean(r)
    r2 = merge_page(b, b, local, SPEC)
    assert r2.text == local
    clean(r2)


def test_symmetry_when_nothing_is_contested_or_held() -> None:
    b = page()
    local = page(
        tags=["a"],
        updated="2026-03-01",
        stage="prospect",
        extra_x="1",
        body=body_edit("Acme Corp.\n", "Acme Corp since 2020.\n"),
    )
    remote = page(
        tags=["a", "b"],
        updated="2026-05-01",
        stage="customer",
        extra_y="2",
        body=body_edit("Lisbon.\n", "Porto.\n"),
    )
    one = merge_page(b, local, remote, SPEC)
    two = merge_page(b, remote, local, SPEC)
    clean(one)
    clean(two)
    assert one.text == two.text
    assert fm(one)["tags"] == ["a"]


def test_symmetry_allows_the_documented_append_order() -> None:
    b = page()
    local = page(body=body_edit("- Prefers email\n", "- Prefers email\n- L\n"))
    remote = page(body=body_edit("- Prefers email\n", "- Prefers email\n- R\n"))
    one = merge_page(b, local, remote, SPEC)
    two = merge_page(b, remote, local, SPEC)
    assert one.text != two.text
    assert sorted(body(one).splitlines()) == sorted(body(two).splitlines())


def test_rename_on_one_side_and_body_edit_on_the_other() -> None:
    b = page()
    renamed = body_edit("# Jordan Reyes", "# Jordan Reyes-Okafor")
    local = page(name="Jordan Reyes-Okafor", body=renamed)
    remote = page(body=body_edit("Prefers email", "Prefers chat"))
    r = merge_page(b, local, remote, SPEC)
    clean(r)
    assert fm(r)["name"] == "Jordan Reyes-Okafor"
    assert "# Jordan Reyes-Okafor" in body(r)
    assert "Prefers chat" in body(r)


def test_both_sides_created_the_page_identically() -> None:
    x = page()
    r = merge_page(None, x, x, SPEC)
    assert r.text == x
    clean(r)


def test_both_sides_created_the_page_differently() -> None:
    porto = body_edit("Lisbon.", "Porto.")
    local = page(role="CTO", tags=["a"], audience=["hr", "staff"], body=porto)
    madrid = body_edit("Lisbon.", "Madrid.")
    remote = page(role="CFO", tags=["b"], updated="2026-09-09", body=madrid)
    r = merge_page(None, local, remote, SPEC)
    differing = {"fm:role", "fm:tags", "fm:updated", "fm:audience", "section:summary"}
    assert set(r.contested) | {h.unit for h in r.held} == differing
    assert [h.unit for h in r.held] == ["fm:audience"]
    assert fm(r)["role"] == "CFO"
    assert fm(r)["audience"] == ["hr"]
    assert "Madrid." in body(r)
    assert not any(m in (r.text or "") for m in MARKERS)


def test_one_side_creates_the_page() -> None:
    x = page()
    r = merge_page(None, None, x)
    assert r.text == x
    assert r.resolvers == {"page": "added-one-side"}


@pytest.mark.parametrize("spec", [None, SPEC])
def test_every_result_parses_and_has_identity_keys(spec: StructuredUpsertSpec | None) -> None:
    b = page()
    cases = [
        (page(role="CTO"), page(role="CFO")),
        (page(uid="person-x"), page(uid="person-y")),
        (page(name="Jordan R."), page(name="J. Reyes")),
        (page(body=body_edit("Lisbon.", "A.")), page(body=body_edit("Lisbon.", "B."))),
        (page(tags=["z"]), page(tags=None)),
    ]
    for local, remote in cases:
        r = merge_page(b, local, remote, spec)
        assert r.text is not None
        meta, _ = parse_frontmatter(r.text)
        for key in ("uid", "type", "name"):
            assert str(meta.get(key) or "").strip(), (key, r)


def test_a_body_ending_in_an_open_fence_is_held_whole() -> None:
    fenced = BASE_BODY + "\n```python\nx = 1\n"
    b = page()
    for local, remote in (
        (page(role="CTO", body=fenced), page(body=body_edit("Lisbon.", "Porto."))),
        (page(body=body_edit("Lisbon.", "Porto.")), page(role="CTO", body=fenced)),
    ):
        r = merge_page(b, local, remote)
        assert r.held == (HeldUnit("body", "unclosed-fence"),)
        assert r.contested == ()
        assert body(r) == parse_frontmatter(remote)[1]
        assert fm(r)["role"] == "CTO"  # frontmatter units still merge


def test_a_heading_inside_a_fence_is_not_a_section() -> None:
    fenced_ok = BASE_BODY + "\n```\n## Not a section\n```\n"
    b = page(body=fenced_ok)
    local = page(body=fenced_ok.replace("Lisbon.", "Porto."))
    remote = page(body=fenced_ok.replace("Engineer.", "Lead."))
    r = merge_page(b, local, remote)
    clean(r)
    assert "## Not a section" in body(r)
    assert "section:not a section" not in r.resolvers


def test_an_unparseable_side_is_held() -> None:
    r = merge_page(page(), "no frontmatter\n", page(role="CTO"))
    assert r.held == (HeldUnit("page", "unparseable"),)
    assert r.text == page(role="CTO")


def test_a_section_deleted_on_one_side_and_edited_on_the_other_is_contested() -> None:
    b = page()
    local = page(body=BASE_BODY.split("## Profile")[0])
    remote = page(body=body_edit("Engineer.", "Principal engineer."))
    r = merge_page(b, local, remote)
    assert r.contested == ("section:profile",)
    assert "Principal engineer." in body(r)


def test_a_section_added_on_one_side_is_kept() -> None:
    b = page()
    local = page(body=BASE_BODY + "\n## History\n\nJoined 2020.\n")
    remote = page(role="CTO")
    r = merge_page(b, local, remote)
    clean(r)
    assert "## History" in body(r)
    assert r.resolvers["section:history"] == "local-only"
