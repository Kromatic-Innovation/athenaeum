# SPDX-License-Identifier: Apache-2.0
"""Uid collision handling at integrate (athenaeum#2996, design note section 7.4).

Every repository is built under ``tmp_path`` by the two-clone harness; no model is
called. All people and organisations are invented.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from athenaeum import librarian
from athenaeum.sync_integrate import integrate_remote, push_after_compile
from tests.fixtures.two_clone_harness import TwoCloneHarness, git, make_two_clone_harness
from tests.test_sync_integrate import (
    BASE,
    SETTINGS,
    _run_second,
    _wired,
    declaration_text,
    page,
)

TWIN = "a1b2c3d4"
HEX16 = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
def harness(tmp_path: Path) -> TwoCloneHarness:
    return make_two_clone_harness(
        tmp_path,
        seed={"wiki/jordan.md": BASE, "wiki/_store/access.yaml": declaration_text()},
    )


def _message(h: TwoCloneHarness) -> str:
    return git(h.second.root, "log", "-1", "--format=%B")


def _collide(h: TwoCloneHarness, *, local_name: str = "Sam Ortiz") -> None:
    h.first.commit(f"wiki/{TWIN}-priya-raman.md", page(TWIN, "Priya Raman"))
    h.second.commit(f"wiki/{TWIN}-sam-ortiz.md", page(TWIN, local_name))
    assert push_after_compile(h.first.root, "main", SETTINGS).ok


def test_a_collision_re_mints_the_local_page_renames_it_and_records_it(
    harness: TwoCloneHarness,
) -> None:
    _collide(harness)
    result = integrate_remote(harness.second.root, "main", SETTINGS)
    assert result.status == "merged" and result.reason == ""
    ((old, new),) = result.reminted
    assert old == TWIN and HEX16.match(new)
    wiki = harness.second.root / "wiki"
    # the remote page keeps the uid and its file; the local page has the new uid and file
    assert f"uid: {TWIN}" in (wiki / f"{TWIN}-priya-raman.md").read_text()
    assert not (wiki / f"{TWIN}-sam-ortiz.md").exists()
    assert f"uid: {new}" in (wiki / f"{new}-sam-ortiz.md").read_text()
    assert f"Athenaeum-Uid-Reminted: {TWIN} {new}" in _message(harness).splitlines()
    # no model call and no held page, so no decision
    assert result.held == ()
    # the merge pushes
    assert push_after_compile(harness.second.root, "main", SETTINGS).ok
    assert harness.remote_tip() == harness.second.head()


def test_references_made_in_the_unpublished_commits_are_rewritten_but_published_ones_are_not(
    tmp_path: Path,
) -> None:
    published = BASE + f"\nSee also [[{TWIN}]].\n"
    h = make_two_clone_harness(
        tmp_path,
        seed={"wiki/jordan.md": published, "wiki/_store/access.yaml": declaration_text()},
    )
    h.first.commit(f"wiki/{TWIN}-priya-raman.md", page(TWIN, "Priya Raman"))
    h.first.commit(
        "wiki/remote-note.md", page("note-remote", "Remote Note", f"# R\n\nAbout [[{TWIN}]].\n")
    )
    h.second.commit(f"wiki/{TWIN}-sam-ortiz.md", page(TWIN, "Sam Ortiz"))
    h.second.commit(
        "wiki/local-note.md",
        page("note-local", "Local Note", f"# L\n\nAbout [[{TWIN}]] and [[{TWIN}-sam-ortiz]].\n"),
    )
    h.second.commit("wiki/jordan.md", published + f"\nAlso [[{TWIN}]].\n")
    assert push_after_compile(h.first.root, "main", SETTINGS).ok
    result = integrate_remote(h.second.root, "main", SETTINGS)
    ((_, new),) = result.reminted
    wiki = h.second.root / "wiki"
    assert f"[[{new}]] and [[{new}-sam-ortiz]]" in (wiki / "local-note.md").read_text()
    jordan = (wiki / "jordan.md").read_text()
    assert f"See also [[{TWIN}]]." in jordan  # already published: untouched
    assert f"Also [[{new}]]." in jordan  # new in the unpublished commit: rewritten
    assert f"[[{TWIN}]]" in (wiki / "remote-note.md").read_text()  # a remote page: never


def test_the_same_type_and_name_is_not_re_minted_it_is_a_page_merge(
    harness: TwoCloneHarness,
) -> None:
    harness.first.commit(f"wiki/{TWIN}-priya.md", page(TWIN, "Priya Raman"))
    harness.second.commit(f"wiki/{TWIN}-priya-r.md", page(TWIN, "  priya   RAMAN "))
    assert push_after_compile(harness.first.root, "main", SETTINGS).ok
    result = integrate_remote(harness.second.root, "main", SETTINGS)
    assert result.status == "merged" and result.reminted == ()
    assert "Athenaeum-Uid-Reminted" not in _message(harness)
    wiki = harness.second.root / "wiki"
    assert sorted(p.name for p in wiki.glob(f"{TWIN}*.md")) == [f"{TWIN}-priya.md"]
    assert TWIN in result.pages_changed_both  # merged against an empty ancestor


def test_the_same_path_added_on_both_sides_is_not_re_minted(harness: TwoCloneHarness) -> None:
    harness.first.commit(f"wiki/{TWIN}.md", page(TWIN, "Priya Raman", "# P\n\nOne.\n"))
    harness.second.commit(f"wiki/{TWIN}.md", page(TWIN, "Priya Raman", "# P\n\nTwo.\n"))
    assert push_after_compile(harness.first.root, "main", SETTINGS).ok
    result = integrate_remote(harness.second.root, "main", SETTINGS)
    assert result.status == "merged" and result.reminted == ()


def test_a_different_type_is_a_collision_even_with_the_same_name(harness: TwoCloneHarness) -> None:
    harness.first.commit(f"wiki/{TWIN}-x.md", page(TWIN, "Acme"))
    harness.second.commit(
        f"wiki/{TWIN}-y.md", page(TWIN, "Acme").replace("type: person", "type: company")
    )
    assert push_after_compile(harness.first.root, "main", SETTINGS).ok
    assert len(integrate_remote(harness.second.root, "main", SETTINGS).reminted) == 1


def test_a_librarian_run_counts_the_re_mint_in_the_run_summary(tmp_path: Path) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    h.first.commit(f"{TWIN}-priya-raman.md", page(TWIN, "Priya Raman"))
    assert h.first.push().ok
    h.second.commit(f"{TWIN}-sam-ortiz.md", page(TWIN, "Sam Ortiz"))
    state: librarian.SurfaceSyncState = _run_second(kr, cfg)
    assert state.as_profile_fields()["uid_reminted"] == 1
    assert h.remote_tip() == h.second.head()
