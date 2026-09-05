"""Structural guarantees for the docs tree.

These are the rules the docs reorganization is held to. They are cheap and
mechanical on purpose: the failure mode being prevented is a page that exists
but cannot be found, or a reader-facing page that leaks internal bookkeeping.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"
INDEX = DOCS / "index.md"

#: Pages written for a reader deciding whether to use athenaeum, or trying to
#: operate it. Issue numbers are historical bookkeeping and do not belong here.
#: ``reference/configuration.md`` is deliberately excluded: it is still
#: hand-maintained and its section anchors encode issue ids.
READER_FACING = (
    REPO_ROOT / "README.md",
    INDEX,
    DOCS / "guides",
    DOCS / "modules",
)

_MD_LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+\.md)")
_ISSUE_REF = re.compile(r"athenaeum#\d+")
#: Real people must not appear as example data in a public repo.
_OPERATOR_NAMES = re.compile(r"\bTristan\b|\btristankromer\b|\bAmanda\b", re.IGNORECASE)


def _markdown_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.md"))


def _reader_facing_files() -> list[Path]:
    out: list[Path] = []
    for t in READER_FACING:
        out.extend(_markdown_files(t))
    return out


def _all_docs_pages() -> list[Path]:
    """Every *tracked* page under ``docs/``, index excepted.

    Tracked, not merely present: a page excluded from version control is not
    part of the published corpus, so the index cannot be faulted for omitting
    it.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "docs"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return sorted(
        REPO_ROOT / rel
        for rel in listed
        if rel.endswith(".md") and not rel.endswith("/index.md")
    )


class TestLinksResolve:
    """Every relative markdown link in the tree points at a file that exists."""

    def test_every_relative_link_in_docs_resolves(self) -> None:
        broken: list[str] = []
        for page in sorted(DOCS.rglob("*.md")):
            for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
                for m in _MD_LINK.finditer(line):
                    target = m.group(1)
                    base = REPO_ROOT if target.startswith("docs/") else page.parent
                    if not (base / target).resolve().exists():
                        rel = page.relative_to(REPO_ROOT)
                        broken.append(f"{rel}:{lineno} -> {target}")
        assert not broken, "broken relative links:\n  " + "\n  ".join(broken)

    def test_every_relative_link_in_root_markdown_resolves(self) -> None:
        broken: list[str] = []
        for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md"):
            page = REPO_ROOT / name
            if not page.exists():
                continue
            for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
                for m in _MD_LINK.finditer(line):
                    if not (REPO_ROOT / m.group(1)).resolve().exists():
                        broken.append(f"{name}:{lineno} -> {m.group(1)}")
        assert not broken, "broken relative links:\n  " + "\n  ".join(broken)


class TestIndexIsComplete:
    """No page is unreachable from the index.

    This is the acceptance criterion the reorganization exists to satisfy: the
    corpus was never short on content, only on any way to find it.
    """

    def test_index_exists(self) -> None:
        assert INDEX.exists(), "docs/index.md is the map; it must exist"

    def test_every_docs_page_is_linked_from_the_index(self) -> None:
        text = INDEX.read_text(encoding="utf-8")
        linked = {
            (INDEX.parent / m.group(1)).resolve()
            for m in _MD_LINK.finditer(text)
        }
        missing = [
            str(p.relative_to(REPO_ROOT))
            for p in _all_docs_pages()
            if p.resolve() not in linked
        ]
        assert not missing, (
            "these pages exist but are unreachable from docs/index.md:\n  "
            + "\n  ".join(missing)
        )


class TestReaderFacingHygiene:
    """Reader-facing pages carry no internal bookkeeping."""

    @pytest.mark.parametrize(
        "page", _reader_facing_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
    )
    def test_no_issue_references(self, page: Path) -> None:
        hits = [
            f"{lineno}: {line.strip()}"
            for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
            if _ISSUE_REF.search(line)
        ]
        assert not hits, (
            f"{page.relative_to(REPO_ROOT)} cites issue numbers; nobody reading the "
            "docs needs the historical record:\n  " + "\n  ".join(hits)
        )

    @pytest.mark.parametrize(
        "page", _reader_facing_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
    )
    def test_no_operator_names_in_examples(self, page: Path) -> None:
        hits = [
            f"{lineno}: {line.strip()}"
            for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
            if _OPERATOR_NAMES.search(line)
        ]
        assert not hits, (
            f"{page.relative_to(REPO_ROOT)} names a real person; use the example cast "
            "(Jordan Reyes, Priya Raman, Acme Corp):\n  " + "\n  ".join(hits)
        )


class TestModulePagesAnswerTheFourQuestions:
    """Every module page answers the same four questions, in the same order.

    A module page exists to say what a component refuses. That section is the
    one readers currently have to infer, so it is the one worth enforcing.
    """

    REQUIRED = ("## What it does", "## What it reads", "## What it writes", "## What it refuses")

    @pytest.mark.parametrize(
        "page",
        sorted((DOCS / "modules").glob("*.md")),
        ids=lambda p: p.name,
    )
    def test_has_the_four_headings_in_order(self, page: Path) -> None:
        text = page.read_text(encoding="utf-8")
        positions = []
        for heading in self.REQUIRED:
            idx = text.find(heading)
            assert idx != -1, f"{page.name} is missing '{heading}'"
            positions.append(idx)
        assert positions == sorted(positions), (
            f"{page.name} has the four sections out of order"
        )

    @pytest.mark.parametrize(
        "page",
        sorted((DOCS / "modules").glob("*.md")),
        ids=lambda p: p.name,
    )
    def test_has_a_see_also_block(self, page: Path) -> None:
        assert "## See also" in page.read_text(encoding="utf-8"), (
            f"{page.name} must cross-link its guide, design record, and siblings"
        )


class TestGuidesAndModulesCrossLink:
    """A guide and its module page point at each other.

    They answer different questions about the same component -- the guide says
    *do this*, the module page says *what it reads, writes and refuses* -- and
    they drift apart the moment neither names the other.
    """

    @pytest.mark.parametrize(
        "page", sorted((DOCS / "modules").glob("*.md")), ids=lambda p: p.name
    )
    def test_module_see_also_names_a_guide(self, page: Path) -> None:
        see_also = page.read_text(encoding="utf-8").partition("## See also")[2]
        assert "../guides/" in see_also, (
            f"{page.name} has no guide in its See also block"
        )

    @pytest.mark.parametrize(
        "page", sorted((DOCS / "guides").glob("*.md")), ids=lambda p: p.name
    )
    def test_guide_opens_with_a_reference_pointer(self, page: Path) -> None:
        head = "".join(page.read_text(encoding="utf-8").splitlines(keepends=True)[:8])
        assert "**Reference:**" in head and "../modules/" in head, (
            f"{page.name} must open with a **Reference:** line naming its module page(s)"
        )
