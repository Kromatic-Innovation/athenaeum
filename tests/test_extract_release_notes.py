# SPDX-License-Identifier: Apache-2.0
"""Tests for the CHANGELOG-derived release-notes extractor + size guard
(issue athenaeum#1442).

The v0.20.0 tag push failed `release.yml`'s `Create GitHub Release` job with
an HTTP 422 ("body is too long (maximum is 125000 characters)") -- *after*
`Build sdist + wheel` and `Publish to PyPI` had already succeeded. The
extraction + truncation logic used to live in an inline `run:` heredoc in the
`github-release` job, where nothing could exercise the oversized-input path
except a real over-cap release. `scripts/extract_release_notes.py` factors
that logic out specifically so this file can exercise it.

Coverage, matching acceptance criterion 4 ("a test or workflow-level check
covers the oversized case, so the degrade path is exercised rather than
assumed"):

- under-cap passthrough (`test_under_cap_returned_verbatim`)
- over-cap truncation-with-link (`test_over_cap_is_truncated_with_link`,
  `test_truncated_output_never_exceeds_max_chars`)
- the boundary itself, both sides (`test_exactly_at_cap_is_not_truncated`,
  `test_one_over_cap_is_truncated`)
- truncation lands on a line boundary, never mid-word
  (`test_truncation_cuts_on_a_line_boundary`)
- no section found for the version at all (`test_missing_section_falls_back_to_link`)
- the anchor slug used in the truncation-notice link
  (`test_github_anchor_slug`)
- the CLI entrypoint end-to-end (`test_cli_writes_output_file`)
- acceptance criterion 5: the script run against this repo's own real
  `CHANGELOG.md` `[0.20.0]` section, which is confirmed still over the cap on
  `develop` (`test_real_v0_20_0_section_is_oversized_and_degrades_cleanly`)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "extract_release_notes.py"
REAL_CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _load_module():
    """Import the standalone script as a module (it lives in scripts/, not the package)."""
    spec = importlib.util.spec_from_file_location("extract_release_notes", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPO = "Kromatic-Innovation/athenaeum"
TAG = "v0.20.0"


def _changelog(section_body_len: int, version: str = "0.20.0") -> str:
    """A minimal synthetic CHANGELOG.md with one section of a given body length."""
    body = "x" * section_body_len
    return (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        f"## [{version}] - 2026-01-01\n\n"
        f"{body}\n\n"
        "## [0.1.0] - 2025-01-01\n\nolder stuff\n"
    )


# --------------------------------------------------------------------------- #
# extract_section() -- the awk-equivalent slicing
# --------------------------------------------------------------------------- #


def test_extract_section_finds_body_between_headers() -> None:
    mod = _load_module()
    changelog = _changelog(20)
    result = mod.extract_section(changelog, "0.20.0")
    assert result is not None
    header, body = result
    assert header == "## [0.20.0] - 2026-01-01"
    assert "x" * 20 in body
    assert "0.1.0" not in body  # never bleeds into the next section


def test_missing_section_returns_none() -> None:
    mod = _load_module()
    assert mod.extract_section(_changelog(20), "9.9.9") is None


# --------------------------------------------------------------------------- #
# github_anchor() -- the truncation-notice link's fragment
# --------------------------------------------------------------------------- #


def test_github_anchor_slug() -> None:
    mod = _load_module()
    assert mod.github_anchor("## [0.20.0] - 2026-09-04") == "0200---2026-09-04"


# --------------------------------------------------------------------------- #
# build_release_notes() -- passthrough / truncation / fallback
# --------------------------------------------------------------------------- #


def test_under_cap_returned_verbatim() -> None:
    mod = _load_module()
    changelog = _changelog(200)
    notes = mod.build_release_notes(
        changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=1000
    )
    assert "x" * 200 in notes
    assert "truncated" not in notes
    assert notes.startswith("## [0.20.0]")


def test_over_cap_is_truncated_with_link() -> None:
    mod = _load_module()
    changelog = _changelog(5000)
    notes = mod.build_release_notes(
        changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=1000
    )
    assert len(notes) <= 1000
    assert "truncated" in notes
    assert f"https://github.com/{REPO}/blob/{TAG}/CHANGELOG.md#" in notes
    # The link must point at the tag, not a floating branch name -- so it
    # keeps resolving after CHANGELOG.md changes on the default branch.
    assert "/blob/main/" not in notes
    assert "/blob/develop/" not in notes


def test_truncated_output_never_exceeds_max_chars() -> None:
    mod = _load_module()
    changelog = _changelog(300_000)
    for max_chars in (1000, 5000, 60_000, 100):
        notes = mod.build_release_notes(
            changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=max_chars
        )
        assert len(notes) <= max_chars, f"exceeded max_chars={max_chars}"


def test_exactly_at_cap_is_not_truncated() -> None:
    mod = _load_module()
    header = "## [0.20.0] - 2026-01-01"
    # Construct a body so `full` (header + "\n" + body + "\n") is exactly max_chars.
    max_chars = 500
    pad_len = max_chars - len(header) - 2  # two newlines: header\n body \n
    changelog = _changelog(pad_len)
    notes = mod.build_release_notes(
        changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=max_chars
    )
    assert len(notes) == max_chars
    assert "truncated" not in notes


def test_one_over_cap_is_truncated() -> None:
    mod = _load_module()
    header = "## [0.20.0] - 2026-01-01"
    max_chars = 500
    pad_len = max_chars - len(header) - 2 + 1  # one character past the exact boundary
    changelog = _changelog(pad_len)
    notes = mod.build_release_notes(
        changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=max_chars
    )
    assert len(notes) <= max_chars
    assert "truncated" in notes


def test_truncation_cuts_on_a_line_boundary() -> None:
    """Truncation must land on a newline, never mid-word/mid-line."""
    mod = _load_module()
    lines = [f"- change number {i} with some descriptive words here" for i in range(2000)]
    body = "\n".join(lines)
    header = "## [0.20.0] - 2026-01-01"
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n"
        f"{header}\n\n{body}\n\n## [0.1.0] - 2025-01-01\n\nolder\n"
    )
    notes = mod.build_release_notes(
        changelog_text=changelog, version="0.20.0", repo=REPO, tag=TAG, max_chars=2000
    )
    assert len(notes) <= 2000
    assert "truncated" in notes
    body_part = notes.split("\n\n… truncated")[0]
    # Every kept line (other than the header) must be one of the exact
    # source lines -- never a partial/cut-off fragment of one.
    kept_lines = body_part.splitlines()[2:]  # skip header + blank line
    for line in kept_lines:
        assert line in lines, f"partial line leaked through truncation: {line!r}"


def test_missing_section_falls_back_to_link() -> None:
    mod = _load_module()
    changelog = _changelog(20)
    notes = mod.build_release_notes(
        changelog_text=changelog, version="9.9.9", repo=REPO, tag="v9.9.9", max_chars=1000
    )
    expected_link = f"https://github.com/{REPO}/blob/v9.9.9/CHANGELOG.md"
    assert notes == f"See [CHANGELOG.md]({expected_link}) for details.\n"


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #


def test_cli_writes_output_file(tmp_path: Path) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(_changelog(5000))
    output_path = tmp_path / "release-notes.md"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--changelog",
            str(changelog_path),
            "--version",
            "0.20.0",
            "--repo",
            REPO,
            "--tag",
            TAG,
            "--output",
            str(output_path),
            "--max-chars",
            "1000",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    written = output_path.read_text()
    assert len(written) <= 1000
    assert "truncated" in written


# --------------------------------------------------------------------------- #
# Acceptance criterion 5: the real CHANGELOG.md on develop
# --------------------------------------------------------------------------- #


def test_real_v0_20_0_section_is_oversized_and_degrades_cleanly() -> None:
    """Run the extractor against this repo's actual CHANGELOG.md.

    Confirms (a) the incident condition still reproduces against the real
    0.20.0 section on develop -- it is genuinely over the default 125,000
    cap, well before applying our own tighter 60,000 threshold -- and (b)
    the degrade path produces a body that is short, readable, and carries a
    working link, i.e. actually publishable via `gh release create
    --notes-file`.
    """
    if not REAL_CHANGELOG.is_file():  # pragma: no cover - defensive only
        pytest.skip("CHANGELOG.md not present in this checkout")

    mod = _load_module()
    changelog_text = REAL_CHANGELOG.read_text(encoding="utf-8")

    section = mod.extract_section(changelog_text, "0.20.0")
    assert section is not None, "expected a [0.20.0] section in CHANGELOG.md"
    header, body = section
    raw_len = len(header) + 1 + len(body) + 1
    assert raw_len > mod.GITHUB_RELEASE_BODY_HARD_CAP, (
        "the real 0.20.0 section is no longer over GitHub's 125,000 cap -- "
        "the incident condition no longer reproduces against CHANGELOG.md on "
        "develop; this test's assumption needs re-checking against the "
        "current tree (see the issue's AC5)"
    )

    notes = mod.build_release_notes(
        changelog_text=changelog_text,
        version="0.20.0",
        repo=REPO,
        tag=TAG,
        max_chars=mod.DEFAULT_MAX_CHARS,
    )
    assert len(notes) <= mod.DEFAULT_MAX_CHARS
    assert "truncated" in notes
    assert f"https://github.com/{REPO}/blob/{TAG}/CHANGELOG.md#" in notes
    assert notes.startswith("## [0.20.0]")
