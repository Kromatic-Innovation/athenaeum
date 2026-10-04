"""``FilesystemStore.uncommitted_paths`` over real ``git`` working trees.

Issue athenaeum#1944 added this method as the fail-CLOSED precondition behind
``athenaeum subject-population --from-report --apply``: a target page with
uncommitted local changes must be refused, never silently merged into.

The regression these tests exist for (caught in PR review): the first
implementation parsed the newline ``git status --porcelain`` format, which --
under the default ``core.quotePath=true`` -- renders any path holding
non-ASCII bytes or shell-special characters as a C-quoted, backslash-escaped
literal. That literal never matches the real filename, so a dirty page read
CLEAN and the guard failed OPEN. ``test_a_dirty_page_with_a_non_ascii_name``
and its siblings below are the positive controls for the quoting cases; the
plain-ASCII case is asserted alongside them so a change that broke the
ordinary path would not hide behind the exotic ones.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.store import FilesystemStore

_SURFACE = "wiki"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git working tree with one committed page per exotic name."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    # core.quotePath is left at its default on purpose -- that default is
    # exactly the condition the parsing regression depended on.
    for name in _ALL_NAMES:
        (root / "wiki" / name).write_text("committed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


_ASCII = "plain.md"
_NON_ASCII = "café-résumé.md"
_SPACED = "two words.md"
_TRAILING_SPACE = "trailing .md"
_ALL_NAMES = (_ASCII, _NON_ASCII, _SPACED, _TRAILING_SPACE)


def _store(root: Path) -> FilesystemStore:
    return FilesystemStore(root, {_SURFACE: root / "wiki"})


def _page(root: Path, name: str) -> Path:
    return root / "wiki" / name


class TestDirtyPagesAreReported:
    """Every name shape, dirtied, must come back as dirty."""

    @pytest.mark.parametrize("name", _ALL_NAMES)
    def test_a_modified_page_is_reported_dirty(self, repo: Path, name: str) -> None:
        page = _page(repo, name)
        page.write_text("locally modified\n", encoding="utf-8")
        assert _store(repo).uncommitted_paths([page]) == [page]

    @pytest.mark.parametrize("name", _ALL_NAMES)
    def test_a_staged_page_is_reported_dirty(self, repo: Path, name: str) -> None:
        page = _page(repo, name)
        page.write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "--", f"wiki/{name}")
        assert _store(repo).uncommitted_paths([page]) == [page]

    def test_an_untracked_page_is_reported_dirty(self, repo: Path) -> None:
        page = _page(repo, "brand-new-café.md")
        page.write_text("untracked\n", encoding="utf-8")
        assert _store(repo).uncommitted_paths([page]) == [page]


class TestCleanPagesAreNotReported:
    """The negative control: a clean tree must report nothing, or the tests
    above would pass against an implementation that simply returns every
    path it is handed."""

    def test_a_clean_tree_reports_nothing(self, repo: Path) -> None:
        pages = [_page(repo, name) for name in _ALL_NAMES]
        assert _store(repo).uncommitted_paths(pages) == []

    def test_only_the_dirty_page_is_reported(self, repo: Path) -> None:
        dirty = _page(repo, _NON_ASCII)
        dirty.write_text("modified\n", encoding="utf-8")
        pages = [_page(repo, name) for name in _ALL_NAMES]
        assert _store(repo).uncommitted_paths(pages) == [dirty]

    def test_a_dirty_page_outside_the_queried_set_is_not_reported(self, repo: Path) -> None:
        _page(repo, _NON_ASCII).write_text("modified\n", encoding="utf-8")
        assert _store(repo).uncommitted_paths([_page(repo, _ASCII)]) == []


class TestRenameEntriesResolveToTheDestination:
    """A staged rename emits the destination AND the source path; only the
    destination is a page the caller is about to write."""

    def test_a_staged_rename_reports_the_destination(self, repo: Path) -> None:
        source = _page(repo, _NON_ASCII)
        destination = _page(repo, "renamed-café.md")
        _git(repo, "mv", "--", f"wiki/{_NON_ASCII}", f"wiki/{destination.name}")
        reported = _store(repo).uncommitted_paths([destination, _page(repo, _ASCII)])
        assert reported == [destination]
        assert not source.exists()

    def test_a_rename_does_not_mark_an_unrelated_sibling_dirty(self, repo: Path) -> None:
        _git(repo, "mv", "--", f"wiki/{_NON_ASCII}", "wiki/renamed.md")
        # `_SPACED` is untouched by the rename; a parser that mis-consumed the
        # rename's source record could shift and mis-attribute the next entry.
        assert _store(repo).uncommitted_paths([_page(repo, _SPACED)]) == []


class TestFailsClosed:
    """A tree this method cannot interrogate must come back as fully dirty,
    never as clean (the documented fail-CLOSED contract)."""

    def test_a_non_git_tree_reports_every_existing_path(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        page = _page(root, _ASCII)
        page.write_text("no repo here\n", encoding="utf-8")
        assert _store(root).uncommitted_paths([page]) == [page]

    def test_a_path_that_does_not_exist_is_dropped(self, repo: Path) -> None:
        assert _store(repo).uncommitted_paths([_page(repo, "absent.md")]) == []
