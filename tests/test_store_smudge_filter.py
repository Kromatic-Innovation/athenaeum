# SPDX-License-Identifier: Apache-2.0
"""Store reads at a past revision honour the repository's smudge filters.

A toy base64 clean/smudge filter stands in for any content filter (encryption,
LFS pointers). Everything is built under ``tmp_path``; the people are invented.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.store import read_blob, read_file_at_revision
from athenaeum.sync_integrate import PageRoot, SyncSettings, push_after_compile, sync_before_compile
from tests.fixtures.two_clone_harness import TwoCloneHarness, git, make_two_clone_harness
from tests.test_sync_integrate import BASE, declaration_text

SETTINGS = SyncSettings(page_roots=(PageRoot("wiki"),), sleep=lambda _s: None)
ATTRIBUTES = "wiki/*.md filter=b64\n"


@pytest.fixture
def filter_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Define the toy filter in a throwaway global config so every clone has it."""
    cfg = tmp_path / "filters.gitconfig"
    cfg.write_text('[filter "b64"]\n\tclean = base64 -w0\n\tsmudge = base64 -d\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


@pytest.fixture
def filtered(tmp_path: Path, filter_config: None) -> TwoCloneHarness:
    return make_two_clone_harness(
        tmp_path,
        seed={
            ".gitattributes": ATTRIBUTES,
            "wiki/jordan.md": BASE,
            "wiki/_store/access.yaml": declaration_text(),
        },
    )


def _committed(repo: Path, rev: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "cat-file", "blob", f"{rev}:{path}"], cwd=str(repo), check=True, capture_output=True
    ).stdout


def test_read_at_revision_returns_smudged_plaintext(filtered: TwoCloneHarness) -> None:
    root = filtered.first.root
    # The committed object is the filtered form; the seam hands back what a checkout writes.
    assert _committed(root, "HEAD", "wiki/jordan.md") != BASE.encode()
    assert read_file_at_revision(root, "HEAD", "wiki/jordan.md") == BASE.encode()
    assert read_file_at_revision(root, "HEAD", "wiki/absent.md") is None


def test_read_blob_with_path_smudges_and_without_is_raw(filtered: TwoCloneHarness) -> None:
    root = filtered.first.root
    blob = git(root, "rev-parse", "HEAD:wiki/jordan.md")
    assert read_blob(root, blob, path="wiki/jordan.md") == BASE.encode()
    assert read_blob(root, blob) == _committed(root, "HEAD", "wiki/jordan.md")


def test_a_page_edited_on_both_sides_merges_instead_of_being_held(
    filtered: TwoCloneHarness,
) -> None:
    first, second = filtered.first, filtered.second
    first.commit("wiki/jordan.md", BASE.replace("role: Engineer", "role: Engineer\nstage: lead"))
    second.commit("wiki/jordan.md", BASE.replace("Acme Corp.", "Acme Corp, in Lisbon."))
    assert push_after_compile(first.root, "main", SETTINGS).ok
    report = sync_before_compile(second.root, "main", SETTINGS)
    assert report.integrate is not None and report.integrate.status == "merged"
    merged = (second.root / "wiki" / "jordan.md").read_text()
    assert "stage: lead" in merged and "in Lisbon" in merged and "<<<<<<<" not in merged


def test_without_a_filter_the_bytes_equal_cat_file_blob(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "wiki").mkdir()
    (repo / "wiki" / "a.md").write_bytes(b"line one\r\nline two\n\x00\xff tail")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    got = read_file_at_revision(repo, "HEAD", "wiki/a.md")
    assert got == _committed(repo, "HEAD", "wiki/a.md") == b"line one\r\nline two\n\x00\xff tail"
