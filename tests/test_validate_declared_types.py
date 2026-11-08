# SPDX-License-Identifier: Apache-2.0
"""``validate_wiki_meta`` honours deployment-declared types (issue athenaeum#2362)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from athenaeum.models import EntityIndex, RawFile
from athenaeum.schemas import KNOWN_TYPES, validate_wiki_meta

META = {"uid": "thing-1", "type": "recipe", "name": "A Thing"}


def test_declared_type_does_not_warn() -> None:
    assert "recipe" not in KNOWN_TYPES
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        validate_wiki_meta(dict(META), declared_types=["recipe", "company"])


def test_undeclared_unknown_type_still_warns() -> None:
    with pytest.warns(UserWarning, match="unknown wiki type: 'recipe'"):
        validate_wiki_meta(dict(META), declared_types=["company"])


def test_no_declared_types_argument_is_unchanged() -> None:
    with pytest.warns(UserWarning, match="unknown wiki type: 'recipe'"):
        validate_wiki_meta(dict(META))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        validate_wiki_meta({"uid": "p-1", "type": "person", "name": "P"})


def test_declared_types_accepts_a_one_shot_iterable() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        validate_wiki_meta(dict(META), declared_types=(t for t in ["recipe"]))


def test_tier0_passthrough_with_declared_type_is_warning_free(tmp_path: Path) -> None:
    from athenaeum.intake import tier0_passthrough

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    raw_path = tmp_path / "r.md"
    raw_path.write_text(
        "---\nuid: recipe-x\ntype: recipe\nname: X\naccess: internal\n---\n\n# X\n",
        encoding="utf-8",
    )
    raw = RawFile(path=raw_path, source="recipes", timestamp="", uuid8="")
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        out = tier0_passthrough(raw, EntityIndex(wiki), wiki, ["recipe"])
    assert out is not None and out.uid == "recipe-x"
