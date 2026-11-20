# SPDX-License-Identifier: Apache-2.0
"""Read-only lint: one uid on two pages inside a surface (athenaeum#2993)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from athenaeum._cmd_uid_collision_lint import EXIT_COLLISIONS_FOUND, cmd_uid_collision_lint
from athenaeum.cli import main as cli_main
from athenaeum.uid_collision_lint import UnknownSurface, scan_uid_collisions

SHORT = "aaaa1111"
LONG = "0123456789abcdef"

_CONFIG = (
    "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
    "      surface_root: recipes\n      visibility: 1\n"
    "      corpus_policy:\n        embedded: true\n"
)


def _page(path: Path, uid: str, name: str, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nuid: {uid}\ntype: reference\nname: {name}\n{extra}---\n\n# {name}\n",
        encoding="utf-8",
    )
    return path


def _tombstone(path: Path, uid: str, name: str) -> Path:
    return _page(path, uid, name, "status: folded\nfolded_into: other\nembedded: false\n")


@pytest.fixture
def kroot(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "athenaeum.yaml").write_text(_CONFIG)
    return tmp_path


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _args(kroot: Path, *, as_json: bool = False, surface: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(path=kroot, json=as_json, surface=surface)


def test_reports_a_uid_on_two_pages_with_paths(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _page(kroot / "wiki" / f"{SHORT}-alicia.md", SHORT, "Alicia")
    _page(kroot / "wiki" / "bbbb2222-bob.md", "bbbb2222", "Bob")
    report = scan_uid_collisions(kroot)
    assert report.pages_scanned == 3
    (c,) = report.collisions
    assert (c.surface, c.uid) == ("wiki", SHORT)
    assert [p.path for p in c.pages] == [f"{SHORT}-alice.md", f"{SHORT}-alicia.md"]
    assert not any(p.tombstone for p in c.pages)


def test_tombstone_counts_and_is_marked(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _tombstone(kroot / "wiki" / f"{SHORT}-old-alice.md", SHORT, "Old Alice")
    (c,) = scan_uid_collisions(kroot).collisions
    assert {p.path: p.tombstone for p in c.pages} == {
        f"{SHORT}-alice.md": False,
        f"{SHORT}-old-alice.md": True,
    }


def test_same_uid_on_two_surfaces_is_not_reported(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _page(kroot / "recipes" / f"{SHORT}-soup.md", SHORT, "Soup")
    report = scan_uid_collisions(kroot)
    assert report.pages_scanned == 2
    assert report.ok
    assert cmd_uid_collision_lint(_args(kroot)) == 0


def test_collision_inside_a_non_wiki_surface_is_named_by_surface(kroot: Path) -> None:
    _page(kroot / "recipes" / f"{SHORT}-soup.md", SHORT, "Soup")
    _page(kroot / "recipes" / "sub" / f"{SHORT}-stew.md", SHORT, "Stew")
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    (c,) = scan_uid_collisions(kroot).collisions
    assert c.surface == "recipes"
    assert [p.path for p in c.pages] == [f"{SHORT}-soup.md", f"sub/{SHORT}-stew.md"]
    only_wiki = scan_uid_collisions(kroot, surface="wiki")
    assert only_wiki.ok and only_wiki.pages_scanned == 1


def test_both_uid_widths_are_handled_and_never_cross(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-a.md", SHORT, "A")
    _page(kroot / "wiki" / f"{SHORT}-b.md", SHORT, "B")
    _page(kroot / "wiki" / f"{LONG}-c.md", LONG, "C")
    _page(kroot / "wiki" / f"{LONG}-d.md", LONG, "D")
    # An 8-hex uid that is the prefix of a 16-hex uid is a different id.
    _page(kroot / "wiki" / f"{LONG[:8]}-e.md", LONG[:8], "E")
    uids = sorted(c.uid for c in scan_uid_collisions(kroot).collisions)
    assert uids == sorted([SHORT, LONG])


def test_is_read_only(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _tombstone(kroot / "wiki" / f"{SHORT}-old.md", SHORT, "Old")
    _page(kroot / "recipes" / f"{LONG}-x.md", LONG, "X")
    before = _tree_digest(kroot)
    scan_uid_collisions(kroot)
    cmd_uid_collision_lint(_args(kroot, as_json=True))
    assert _tree_digest(kroot) == before


def test_exit_code_and_json_output(kroot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _tombstone(kroot / "wiki" / f"{SHORT}-old.md", SHORT, "Old")
    assert cmd_uid_collision_lint(_args(kroot, as_json=True)) == EXIT_COLLISIONS_FOUND
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    (c,) = payload["collisions"]
    assert c["surface"] == "wiki" and c["uid"] == SHORT
    assert c["pages"] == [
        {"path": f"{SHORT}-alice.md", "tombstone": False},
        {"path": f"{SHORT}-old.md", "tombstone": True},
    ]


def test_plain_output_marks_the_tombstone(kroot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    _tombstone(kroot / "wiki" / f"{SHORT}-old.md", SHORT, "Old")
    assert cmd_uid_collision_lint(_args(kroot)) == EXIT_COLLISIONS_FOUND
    out = capsys.readouterr().out
    assert f"{SHORT}-old.md (tombstone)" in out
    assert f"{SHORT}-alice.md (tombstone)" not in out


def test_clean_corpus_exits_zero_through_the_real_cli(kroot: Path) -> None:
    _page(kroot / "wiki" / f"{SHORT}-alice.md", SHORT, "Alice")
    assert cli_main(["uid-collision-lint", "--path", str(kroot)]) == 0


def test_unknown_surface_is_refused_not_a_clean_scan(
    kroot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _page(kroot / "wiki" / f"{SHORT}-a.md", SHORT, "A")
    _page(kroot / "wiki" / f"{SHORT}-b.md", SHORT, "B")
    _page(kroot / "recipes" / f"{LONG}-x.md", LONG, "X")
    with pytest.raises(UnknownSurface):
        scan_uid_collisions(kroot, surface="recipez")
    assert cmd_uid_collision_lint(_args(kroot, surface="recipez")) == 1
    assert "recipez" in capsys.readouterr().err
    assert cmd_uid_collision_lint(_args(kroot, surface="recipes")) == 0
    assert cmd_uid_collision_lint(_args(kroot, surface="wiki")) == EXIT_COLLISIONS_FOUND
