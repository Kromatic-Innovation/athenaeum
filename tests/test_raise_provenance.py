# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.raise_provenance and ``athenaeum raised-from-lint`` (athenaeum#2799)."""

from __future__ import annotations

import copy
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.cli import main
from athenaeum.precedence import SOURCE_PRECEDENCE_TIERS
from athenaeum.provenance import parse_source
from athenaeum.raise_provenance import (
    RAISE_SOURCE_TYPE,
    RaisedFromError,
    lint_raised_from,
    parse_raised_from,
    raise_source_ref,
)

# The design note's example id contains 8 and 9, outside the base32 alphabet
# the issue specifies; the block tests use a valid id of the same shape.
RID = "rs_k3v7t2hq5m4xw7pd5n6b"
GOOD: dict[str, Any] = {
    "v": 1,
    "raise_id": RID,
    "origin": "st_f2n6rb5hjc3t",
    "proposer": "group:department-heads",
    "via": "proposer",
    "content_hash": "sha256:" + "a" * 64,
    "raised_on": date(2026, 11, 18),
    "seq": 1,
    "watch": "active",
}


def _meta(**overrides: Any) -> dict[str, Any]:
    block = copy.deepcopy(GOOD)
    for k, v in overrides.items():
        if v is _DROP:
            block.pop(k, None)
        else:
            block[k] = v
    return {"raised_from": block}


_DROP = object()


def test_accepts_the_design_note_example() -> None:
    rf = parse_raised_from(_meta())
    assert rf is not None
    assert (rf.v, rf.raise_id, rf.origin, rf.proposer, rf.via) == (
        1, RID, "st_f2n6rb5hjc3t", "group:department-heads", "proposer",
    )
    assert rf.content_hash == "sha256:" + "a" * 64
    assert rf.raised_on == date(2026, 11, 18)
    assert (rf.seq, rf.watch) == (1, "active")


def test_watch_defaults_to_active() -> None:
    rf = parse_raised_from(_meta(watch=_DROP))
    assert rf is not None and rf.watch == "active"


def test_absent_block_is_none() -> None:
    assert parse_raised_from({"name": "x"}) is None
    assert parse_raised_from({}) is None


@pytest.mark.parametrize("key", ["source_uid", "source_path"])
def test_link_back_down_is_refused(key: str) -> None:
    value = "5e1d09c3a7b24f86" if key == "source_uid" else "wiki/people/x.md"
    meta = _meta(**{key: value})
    with pytest.raises(RaisedFromError) as exc:
        parse_raised_from(meta)
    assert "unknown_key" in exc.value.codes
    assert lint_raised_from(meta) == ["unknown_key"]


def test_time_of_day_is_refused_day_is_accepted() -> None:
    parsed = yaml.safe_load("raised_from:\n  raised_on: 2026-11-18T09:30:00Z\n")
    block = {**GOOD, "raised_on": parsed["raised_from"]["raised_on"]}
    with pytest.raises(RaisedFromError):
        parse_raised_from({"raised_from": block})
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(raised_on="2026-11-18T09:30:00Z"))
    assert parse_raised_from(_meta(raised_on=date(2026, 11, 18))) is not None
    rf = parse_raised_from(_meta(raised_on="2026-11-18"))
    assert rf is not None and rf.raised_on == date(2026, 11, 18)
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(raised_on="2026-13-45"))


@pytest.mark.parametrize(
    "proposer",
    [{"iss": "https://git.example.net", "sub": "1203"}, "group:department-heads"],
)
def test_proposer_accepted(proposer: Any) -> None:
    assert parse_raised_from(_meta(proposer=proposer)) is not None


@pytest.mark.parametrize(
    "proposer",
    ["Dana Whitlock", {"iss": "https://git.example.net"}, "group:", "group:  ",
     {"iss": "", "sub": "1"}],
)
def test_proposer_refused(proposer: Any) -> None:
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(proposer=proposer))


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("raise_id", "rs_" + "b" * 20, "rm_" + "b" * 20),
        ("origin", "st_" + "c" * 12, "xx_" + "c" * 12),
        ("via", "review:q4-audit", "review:"),
        ("content_hash", "sha256:" + "0" * 64, "0" * 64),
        ("seq", 7, 0),
        ("watch", "lapsed", "paused"),
        ("v", 1, 2),
    ],
)
def test_each_rule_has_accepting_and_refusing_case(key: str, good: Any, bad: Any) -> None:
    assert parse_raised_from(_meta(**{key: good})) is not None
    with pytest.raises(RaisedFromError) as exc:
        parse_raised_from(_meta(**{key: bad}))
    assert exc.value.codes == ("malformed_block",)


def test_block_without_v_is_refused() -> None:
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(v=_DROP))


def test_bool_is_not_an_integer_and_non_mapping_is_malformed() -> None:
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(seq=True))
    with pytest.raises(RaisedFromError):
        parse_raised_from(_meta(v=True))
    assert lint_raised_from({"raised_from": "nope"}) == ["malformed_block"]


def test_raise_source_type_parses_and_is_case_sensitive() -> None:
    ref = parse_source("raise:rs_k3v9t2hq8m4xw7pd5n6b")
    assert ref is not None and ref.type == RAISE_SOURCE_TYPE
    with pytest.raises(ValueError):
        parse_source("Raise:rs_k3v9t2hq8m4xw7pd5n6b")
    assert raise_source_ref(RID) == "raise:" + RID


def test_lint_well_formed_copy_is_clean() -> None:
    assert lint_raised_from({**_meta(), "sources": [raise_source_ref(RID)]}) == []


def test_lint_missing_and_mismatched_raise_source() -> None:
    assert lint_raised_from({**_meta(), "sources": ["session:abc"]}) == [
        "missing_raise_source"
    ]
    assert lint_raised_from(_meta()) == ["missing_raise_source"]
    other = raise_source_ref("rs_" + "z" * 20)
    assert lint_raised_from({**_meta(), "sources": [other]}) == ["raise_source_mismatch"]


def test_lint_ordinary_page_with_raise_source_is_not_reported() -> None:
    assert lint_raised_from({"name": "x", "sources": [raise_source_ref(RID)]}) == []


def test_precedence_rank_of_raise_is_unset() -> None:
    # The rank of the ``raise`` source type is owned by athenaeum#2789; the
    # lowest rank an unlisted type gets today is a default, not a decision.
    assert all(RAISE_SOURCE_TYPE not in tier for tier in SOURCE_PRECEDENCE_TIERS)


# -- the CLI -----------------------------------------------------------------


def _write(wiki: Path, name: str, meta: dict[str, Any]) -> Path:
    path = wiki / name
    path.write_text("---\n" + yaml.dump(meta, sort_keys=False) + "---\nBody.\n", "utf-8")
    return path


def _digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_cli_reports_only_the_malformed_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write(wiki, "aaaaaaaaaaaaaaaa-good.md", {
        "uid": "aaaaaaaaaaaaaaaa", "name": "Good", **_meta(),
        "sources": [raise_source_ref(RID)],
    })
    _write(wiki, "bbbbbbbbbbbbbbbb-bad.md", {
        "uid": "bbbbbbbbbbbbbbbb", "name": "Bad",
        **_meta(source_uid="5e1d09c3a7b24f86"), "sources": [raise_source_ref(RID)],
    })
    before = _digests(tmp_path)
    rc = main(["raised-from-lint", "--path", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert out.strip() == "bbbbbbbbbbbbbbbb: unknown_key"
    assert "aaaaaaaaaaaaaaaa" not in out
    assert _digests(tmp_path) == before


def test_cli_clean_corpus_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write(wiki, "cccccccccccccccc-plain.md", {"uid": "cccccccccccccccc", "name": "Plain"})
    assert main(["raised-from-lint", "--path", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""
