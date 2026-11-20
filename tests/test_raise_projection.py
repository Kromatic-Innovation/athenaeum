# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.raise_projection (athenaeum#2798)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from athenaeum import raise_projection
from athenaeum.models import parse_frontmatter
from athenaeum.raise_projection import (
    derived_uid,
    is_message_id,
    is_raise_id,
    mint_message_id,
    mint_raise_id,
    payload_hash,
    project,
    render,
)
from athenaeum.verdicts import content_hash

OTHER_UID = "5e1d09c3a7b24f86"


def _page(meta: dict, body: str) -> str:
    return "---\n" + yaml.dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + body


SOURCE_META = {
    "uid": "1a2b3c4d5e6f7a8b",
    "type": "principle",
    "name": "Year-end PTO usage report",
    "description": "How to read the report",
    "aliases": ["PTO report"],
    "tags": ["hr", "reporting"],
    "access": "internal",
    "audience": ["department-heads"],
    "sources": ["session:abc"],
    "parent": OTHER_UID,
    "related": [{"uid": OTHER_UID, "relation": "see-also"}],
    "created": "2026-01-02",
    "updated": "2026-02-03",
    "raised_from": {"v": 1},
    "mystery": "unknown key",
}
SOURCE_BODY = "Report PTO usage every year.\n"
SOURCE = _page(SOURCE_META, SOURCE_BODY)

KEPT = ["type", "name", "description", "aliases", "tags", "access", "audience"]


def _copy_of(source: str) -> str:
    proj = project(source)
    meta = {
        "uid": derived_uid("st_f2n6rb5hjc3t", "rs_k3v9t2hq8m4xw7pd5n6b"),
        **proj.meta,
        "sources": ["raise:rs_k3v9t2hq8m4xw7pd5n6b"],
        "created": "2026-11-20",
        "updated": "2026-11-20",
        "raised_from": {"v": 1, "raise_id": "rs_k3v9t2hq8m4xw7pd5n6b", "seq": 1},
    }
    return _page(meta, proj.body)


def test_project_keeps_exactly_the_table_keys() -> None:
    proj = project(SOURCE)
    assert list(proj.meta) == KEPT
    for key in KEPT:
        assert proj.meta[key] == SOURCE_META[key]
    assert proj.body == SOURCE_BODY
    for dropped in (
        "uid", "sources", "parent", "related", "created", "updated",
        "raised_from", "mystery",
    ):
        assert dropped not in proj.meta


def test_untouched_copy_compares_equal_and_whole_page_hash_would_not() -> None:
    copy = _copy_of(SOURCE)
    assert payload_hash(copy) == payload_hash(SOURCE)
    assert content_hash(copy) != content_hash(render(project(SOURCE)))


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t.replace("every year", "every quarter"),
        lambda t: t.replace("name: Year-end", "name: Annual"),
        lambda t: t.replace("- reporting", "- finance"),
        lambda t: t.replace("- department-heads", "- everyone"),
    ],
    ids=["body", "name", "tags", "audience"],
)
def test_edited_copy_changes_payload_hash(edit) -> None:
    copy = _copy_of(SOURCE)
    edited = edit(copy)
    assert edited != copy
    assert payload_hash(edited) != payload_hash(SOURCE)


def test_linking_is_not_editing() -> None:
    base = payload_hash(SOURCE)
    with_related = _page({**SOURCE_META, "related": [{"uid": "aaaaaaaaaaaaaaaa"}]}, SOURCE_BODY)
    with_parent = _page({**SOURCE_META, "parent": "bbbbbbbbbbbbbbbb"}, SOURCE_BODY)
    linked_body = _page(SOURCE_META, "Report [[pto-page|PTO usage]] every year.\n")
    plain_body = _page(SOURCE_META, "Report PTO usage every year.\n")
    assert payload_hash(with_related) == base
    assert payload_hash(with_parent) == base
    assert payload_hash(linked_body) == payload_hash(plain_body)


def test_no_identifier_of_another_page_survives() -> None:
    body = (
        f"See [[{OTHER_UID}]] and [[{OTHER_UID}|the other page]] and "
        f"[label]({OTHER_UID}-some-slug.md) and [[{OTHER_UID}-some-slug.md]].\n"
    )
    page = _page(
        {**SOURCE_META, "parent": OTHER_UID, "related": [{"uid": OTHER_UID}]}, body
    )
    assert OTHER_UID not in render(project(page))


@pytest.mark.parametrize(
    "link",
    [
        f"[[{OTHER_UID}]]",
        f"[[{OTHER_UID}-year-end-pto]]",
        f"[[{OTHER_UID}-year-end-pto.md]]",
        f"[[people/{OTHER_UID}-x.md]]",
        f"[[people/{OTHER_UID}-x]]",
        f"[[{OTHER_UID}#Section]]",
        f"[[{OTHER_UID}-x.md#Section]]",
        f"[[{OTHER_UID}|Label]]",
        f"[PTO]({OTHER_UID}-year-end-pto.md)",
        f"[PTO]({OTHER_UID}-year-end-pto)",
        f"[PTO](../people/{OTHER_UID})",
        f"[PTO](../people/{OTHER_UID}.md#Section)",
        f"[PTO]({OTHER_UID}-x?v=1)",
    ],
)
def test_uid_absent_from_every_reference_shape(link: str) -> None:
    rendered = render(project(_page({"name": "x"}, f"before {link} after\n")))
    assert OTHER_UID not in rendered


def test_non_uid_targets_unchanged() -> None:
    body = (
        "[[Year-end PTO usage report]] and [handbook](https://docs.example.net/handbook) "
        "and ![chart](images/chart.png)\n"
    )
    out = project(_page({"name": "x"}, body)).body
    assert out == (
        "Year-end PTO usage report and [handbook](https://docs.example.net/handbook) "
        "and ![chart](images/chart.png)\n"
    )


def test_unlabelled_links() -> None:
    body = "a [[5e1d09c3a7b24f86]] b [[Year-end PTO usage report]] c\n"
    proj = project(_page({"name": "x"}, body))
    assert proj.body == "a  b Year-end PTO usage report c\n"


def test_labelled_and_markdown_links() -> None:
    proj = project(_page({"name": "x"}, "[[t|Label]] and [Other](other-page.md)\n"))
    assert proj.body == "Label and Other\n"


def test_external_link_is_byte_identical() -> None:
    body = "Read [handbook](https://docs.example.net/handbook) and [m](mailto:a@example.net).\n"
    assert project(_page({"name": "x"}, body)).body == body


def test_project_is_idempotent() -> None:
    for page in (SOURCE, _page({"name": "x"}, "[[a|b]] text\n"), "no frontmatter\n"):
        once = project(page)
        assert project(render(once)) == once


def test_render_deterministic_and_key_order_independent() -> None:
    shuffled = {k: SOURCE_META[k] for k in reversed(list(SOURCE_META))}
    a, b = render(project(SOURCE)), render(project(_page(shuffled, SOURCE_BODY)))
    assert a == b
    assert render(project(SOURCE)) == a


def test_mint_ids_shape() -> None:
    for _ in range(50):
        assert re.fullmatch(r"rs_[a-z2-7]{20}", mint_raise_id())
        assert re.fullmatch(r"rm_[a-z2-7]{20}", mint_message_id())
    assert is_raise_id(mint_raise_id()) and is_message_id(mint_message_id())
    assert mint_raise_id() != mint_raise_id()


@pytest.mark.parametrize(
    "bad",
    ["rm_" + "a" * 20, "rs_" + "a" * 19, "rs_" + "a" * 21, "rs_" + "A" * 20,
     "rs_" + "1" * 20, "rs_" + "a" * 20 + "\n", None, 5],
)
def test_raise_id_validator_refuses(bad) -> None:
    assert not is_raise_id(bad)


@pytest.mark.parametrize(
    "bad", ["rs_" + "a" * 20, "rm_" + "a" * 19, "rm_" + "A" * 20, "rm_" + "a" * 21]
)
def test_message_id_validator_refuses(bad) -> None:
    assert not is_message_id(bad)


def test_derived_uid_stable_and_shape() -> None:
    uid = derived_uid("st_f2n6rb5hjc3t", "rs_k3v9t2hq8m4xw7pd5n6b")
    assert re.fullmatch(r"[0-9a-f]{16}", uid)
    assert uid == EXPECTED_UID


def test_derived_uid_differs_per_target() -> None:
    rid = "rs_k3v9t2hq8m4xw7pd5n6b"
    assert derived_uid("st_f2n6rb5hjc3t", rid) != derived_uid("st_aaaaaaaaaaaa", rid)


def test_module_imports_no_io() -> None:
    tree = ast.parse(Path(raise_projection.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.add(mod)
            imported.update(f"{mod}.{a.name}" for a in node.names)
    for banned in ("athenaeum.store", "athenaeum.storage", "athenaeum.config", "subprocess"):
        assert banned not in imported


def test_parse_frontmatter_roundtrip_sanity() -> None:
    meta, _ = parse_frontmatter(render(project(SOURCE)))
    assert list(meta) == KEPT


EXPECTED_UID = "b376f458d0f49752"
