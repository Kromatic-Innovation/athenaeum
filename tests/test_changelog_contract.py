# SPDX-License-Identifier: Apache-2.0
"""``CHANGELOG.md`` follows its own heading contract (athenaeum#2725).

The CHANGELOG preamble promises that, from the contract's first release on,
each release opens with a ``**Summary.**`` paragraph and its
``**Upgrading from ...**`` steps, followed only by the Keep a Changelog
headings ``### Removed``, ``### Changed``, ``### Added``, ``### Fixed`` and
``### Security``, each at most once and in that order. The 0.25.0 and 0.26.0
entries shipped with bold pseudo-headings instead ("Breaking changes",
"Behaviour changes", ...), and the link-reference footer fell three releases
behind. These checks keep both promises honest.

Releases older than the contract's first release keep their original shape,
as the preamble says, and are not checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/Kromatic-Innovation/athenaeum"

# The first release the heading contract covers. The preamble names it.
CONTRACT_FIRST_RELEASE = (0, 25, 0)

# Allowed ``###`` headings, in the order the preamble requires.
ALLOWED_HEADINGS = ("Removed", "Changed", "Added", "Fixed", "Security")

# Bold paragraph openers the contract keeps: the summary and the upgrade steps.
ALLOWED_BOLD_OPENERS = ("**Summary.**", "**Upgrading from ")

# A build-notes link through the moving ``main`` branch drifts from the release
# it describes; tagged releases pin the tag instead (athenaeum#2727).
_BUILD_NOTES_MAIN_RE = re.compile(r"blob/main/docs/changelog/[^\s)]*-build-notes\.md")

_RELEASE_RE = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)
_BUILD_NOTES_DIR = REPO_ROOT / "docs" / "changelog"
_ISSUE_REF_RE = re.compile(r"athenaeum#(\d+)")

_UNRELEASED_LINK_RE = re.compile(r"^\[Unreleased\]:\s*(\S+)\s*$", re.MULTILINE)


def _version_str(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _releases(text: str) -> list[tuple[tuple[int, int, int], str]]:
    """Return ``[(version, body), ...]`` for every ``## [X.Y.Z]`` entry.

    Ordered as they appear (newest first). A body runs to the next ``## ``
    heading or the start of the link-reference footer.
    """
    matches = list(_RELEASE_RE.finditer(text))
    out: list[tuple[tuple[int, int, int], str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        # Cut the body at the next level-2 heading or the footer, whichever
        # comes first, so neither leaks into the last checked release.
        cut = re.search(r"^(## |\[[^\]]+\]: )", body, re.MULTILINE)
        if cut:
            body = body[: cut.start()]
        version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        out.append((version, body))
    return out


def _strip_fences(body: str) -> list[str]:
    """Return the body's lines with fenced code blocks removed."""
    lines: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return lines


def heading_violations(text: str) -> list[str]:
    """Return one message per heading-contract violation in ``text``."""
    problems: list[str] = []
    for version, body in _releases(text):
        if version < CONTRACT_FIRST_RELEASE:
            continue
        name = _version_str(version)
        lines = _strip_fences(body)
        seen: list[str] = []
        for idx, line in enumerate(lines):
            heading = re.match(r"^(#{3,})\s+(.*?)\s*$", line)
            if heading:
                level, title = heading.groups()
                if level != "###" or title not in ALLOWED_HEADINGS:
                    problems.append(f"[{name}] heading not allowed by the contract: {line!r}")
                    continue
                if title in seen:
                    problems.append(f"[{name}] heading repeated: ### {title}")
                    continue
                if seen and ALLOWED_HEADINGS.index(title) < ALLOWED_HEADINGS.index(seen[-1]):
                    order = ", ".join(ALLOWED_HEADINGS)
                    problems.append(
                        f"[{name}] ### {title} comes after ### {seen[-1]}; "
                        f"the preamble orders them {order}"
                    )
                seen.append(title)
                continue
            # A bold pseudo-heading: a paragraph that opens with ``**`` at
            # column 0. Bold text inside a bullet or mid-paragraph is fine.
            paragraph_start = idx == 0 or not lines[idx - 1].strip()
            bold_opener = line.startswith("**") and not line.startswith(ALLOWED_BOLD_OPENERS)
            if paragraph_start and bold_opener:
                problems.append(f"[{name}] bold pseudo-heading instead of a ### heading: {line!r}")
    return problems


def footer_violations(text: str) -> list[str]:
    """Return one message per missing or wrong link-reference footer entry."""
    problems: list[str] = []
    releases = [version for version, _ in _releases(text)]
    if not releases:
        return ["no ## [X.Y.Z] release headings found"]
    for i, version in enumerate(releases):
        if version < CONTRACT_FIRST_RELEASE:
            continue
        name = _version_str(version)
        link = re.search(rf"^\[{re.escape(name)}\]:\s*(\S+)\s*$", text, re.MULTILINE)
        if not link:
            problems.append(f"[{name}] has no [{name}]: footer link")
            continue
        if i + 1 < len(releases):
            previous = _version_str(releases[i + 1])
            expected = f"{REPO_URL}/compare/v{previous}...v{name}"
            if link.group(1) != expected:
                problems.append(f"[{name}] footer link is {link.group(1)}, expected {expected}")
    newest = _version_str(releases[0])
    unreleased = _UNRELEASED_LINK_RE.search(text)
    expected_unreleased = f"{REPO_URL}/compare/v{newest}...HEAD"
    if not unreleased:
        problems.append("no [Unreleased]: footer link")
    elif unreleased.group(1) != expected_unreleased:
        problems.append(
            f"[Unreleased] footer link is {unreleased.group(1)}, expected {expected_unreleased}"
        )
    return problems


def build_notes_main_links(text: str) -> list[str]:
    """Lines that link a ``*-build-notes.md`` file through ``blob/main/``."""
    return [
        f"line {n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if _BUILD_NOTES_MAIN_RE.search(line)
    ]


def cited_issues(text: str) -> set[str]:
    """Return the set of ``athenaeum#NNN`` issue numbers ``text`` cites."""
    return set(_ISSUE_REF_RE.findall(text))


def build_notes_violations(text: str, notes_dir: Path) -> list[str]:
    """Compare the newest release section that has build notes with them.

    Walks the release sections newest first and checks the first one for which
    ``<notes_dir>/<version>-build-notes.md`` exists. The section and its notes
    must cite the same set of ``athenaeum#NNN`` issue numbers, so the two
    sources of truth cannot drift apart. Returns one message per mismatch.
    """
    for version, body in _releases(text):
        name = _version_str(version)
        notes = notes_dir / f"{name}-build-notes.md"
        if not notes.is_file():
            continue
        in_changelog = cited_issues(body)
        in_notes = cited_issues(notes.read_text(encoding="utf-8"))
        problems: list[str] = []
        for number in sorted(in_changelog - in_notes, key=int):
            problems.append(f"[{name}] athenaeum#{number} is in CHANGELOG.md, not the build notes")
        for number in sorted(in_notes - in_changelog, key=int):
            problems.append(f"[{name}] athenaeum#{number} is in the build notes, not CHANGELOG.md")
        return problems
    return []


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The real CHANGELOG.md
# --------------------------------------------------------------------------


def test_preamble_names_the_contracts_first_release(changelog_text: str) -> None:
    """The preamble says where the contract starts, so this test's constant
    and the promise readers see cannot drift apart."""
    preamble = changelog_text.split("## [Unreleased]", 1)[0]
    assert f"From {_version_str(CONTRACT_FIRST_RELEASE)} on" in preamble


def test_contract_releases_exist(changelog_text: str) -> None:
    """Guard against a parser that silently checks nothing."""
    checked = [v for v, _ in _releases(changelog_text) if v >= CONTRACT_FIRST_RELEASE]
    assert CONTRACT_FIRST_RELEASE in checked


def test_changelog_follows_heading_contract(changelog_text: str) -> None:
    assert heading_violations(changelog_text) == []


def test_changelog_footer_links_are_current(changelog_text: str) -> None:
    assert footer_violations(changelog_text) == []


def test_build_notes_links_are_tag_pinned(changelog_text: str) -> None:
    assert build_notes_main_links(changelog_text) == []


def test_release_section_and_build_notes_cite_the_same_issues(changelog_text: str) -> None:
    assert build_notes_violations(changelog_text, _BUILD_NOTES_DIR) == []


def test_build_notes_check_is_not_vacuous(changelog_text: str) -> None:
    """Guard against a check that silently finds no build notes to compare."""
    versions = [_version_str(v) for v, _ in _releases(changelog_text)]
    assert any((_BUILD_NOTES_DIR / f"{v}-build-notes.md").is_file() for v in versions)


def test_systemd_fix_is_under_fixed_in_0_26_0(changelog_text: str) -> None:
    body = dict(_releases(changelog_text))[(0, 26, 0)]
    fixed = body.split("### Fixed", 1)[1].split("\n### ", 1)[0]
    assert "athenaeum#2707" in fixed
    assert "schedule remove" in fixed


# --------------------------------------------------------------------------
# Counter-examples: the checks fail on each shape they exist to catch
# --------------------------------------------------------------------------

_FOOTER = f"""
[Unreleased]: {REPO_URL}/compare/v1.1.0...HEAD
[1.1.0]: {REPO_URL}/compare/v1.0.0...v1.1.0
[1.0.0]: {REPO_URL}/compare/v0.24.0...v1.0.0
"""


def _changelog(release_body: str, footer: str = _FOOTER) -> str:
    return f"""# Changelog

## [Unreleased]

## [1.1.0] - 2026-01-02

**Summary.** A release.
{release_body}
## [1.0.0] - 2026-01-01

**Summary.** The previous release.

### Added

- Something.

## [0.24.0] - 2025-12-01

**Breaking changes**

- Older releases are not checked.
{footer}"""


_GOOD_BODY = """
**Upgrading from 1.0.0**

1. Upgrade.

```bash
**not a heading inside a fence**
```

### Changed

- **Breaking** (labelled sub-list inside a heading is fine):
  - A change.

### Added

- A feature.

### Fixed

- A fix.
"""


def test_counterexample_baseline_is_clean() -> None:
    text = _changelog(_GOOD_BODY)
    assert heading_violations(text) == []
    assert footer_violations(text) == []


def test_fails_on_fixed_before_added() -> None:
    text = _changelog("\n### Fixed\n\n- A fix.\n\n### Added\n\n- A feature.\n")
    problems = heading_violations(text)
    assert len(problems) == 1
    assert "### Added comes after ### Fixed" in problems[0]


def test_fails_on_bold_breaking_changes_pseudo_heading() -> None:
    text = _changelog("\n**Breaking changes**\n\n- A change.\n")
    problems = heading_violations(text)
    assert len(problems) == 1
    assert "bold pseudo-heading" in problems[0]
    assert "Breaking changes" in problems[0]


def test_fails_on_repeated_heading() -> None:
    text = _changelog("\n### Added\n\n- One.\n\n### Added\n\n- Two.\n")
    assert heading_violations(text) == ["[1.1.0] heading repeated: ### Added"]


def test_fails_on_heading_outside_the_allowed_five() -> None:
    text = _changelog("\n### Deprecated\n\n- Old.\n")
    problems = heading_violations(text)
    assert len(problems) == 1
    assert "### Deprecated" in problems[0]


def test_fails_on_missing_footer_link() -> None:
    footer = _FOOTER.replace(f"[1.1.0]: {REPO_URL}/compare/v1.0.0...v1.1.0\n", "")
    text = _changelog(_GOOD_BODY, footer=footer)
    assert footer_violations(text) == ["[1.1.0] has no [1.1.0]: footer link"]


def test_fails_on_stale_unreleased_link() -> None:
    footer = _FOOTER.replace("compare/v1.1.0...HEAD", "compare/v1.0.0...HEAD")
    problems = footer_violations(_changelog(_GOOD_BODY, footer=footer))
    assert len(problems) == 1
    assert problems[0].startswith("[Unreleased] footer link")


def test_fails_on_wrong_compare_range() -> None:
    footer = _FOOTER.replace("compare/v1.0.0...v1.1.0", "compare/v0.24.0...v1.1.0")
    problems = footer_violations(_changelog(_GOOD_BODY, footer=footer))
    assert len(problems) == 1
    assert "expected" in problems[0]


def test_fails_on_build_notes_link_through_blob_main() -> None:
    text = (
        "- Notes: [docs/changelog/1.0.0-build-notes.md]"
        f"({REPO_URL}/blob/main/docs/changelog/1.0.0-build-notes.md).\n"
    )
    assert len(build_notes_main_links(text)) == 1


def test_build_notes_link_pinned_to_a_tag_passes() -> None:
    text = f"[notes]({REPO_URL}/blob/v1.0.0/docs/changelog/1.0.0-build-notes.md)\n"
    assert build_notes_main_links(text) == []


def _notes_pair(tmp_path: Path, notes_text: str) -> str:
    (tmp_path / "1.1.0-build-notes.md").write_text(notes_text, encoding="utf-8")
    return _changelog("\n### Added\n\n- A feature (athenaeum#101).\n- Another (athenaeum#102).\n")


def test_build_notes_matching_pair_passes(tmp_path: Path) -> None:
    text = _notes_pair(tmp_path, "- Detail (athenaeum#102) and (athenaeum#101).\n")
    assert build_notes_violations(text, tmp_path) == []


def test_fails_when_build_notes_cite_a_different_set(tmp_path: Path) -> None:
    text = _notes_pair(tmp_path, "- Detail (athenaeum#101) and (athenaeum#103).\n")
    assert build_notes_violations(text, tmp_path) == [
        "[1.1.0] athenaeum#102 is in CHANGELOG.md, not the build notes",
        "[1.1.0] athenaeum#103 is in the build notes, not CHANGELOG.md",
    ]


def test_build_notes_check_skips_a_release_with_no_notes(tmp_path: Path) -> None:
    text = _changelog("\n### Added\n\n- A feature (athenaeum#101).\n")
    assert build_notes_violations(text, tmp_path) == []
