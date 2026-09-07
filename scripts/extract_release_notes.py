#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Extract + size-guard the CHANGELOG.md release-notes body `release.yml`
feeds to `gh release create --notes-file` (issue athenaeum#1442).

The v0.20.0 tag push failed its `Create GitHub Release` job with
`HTTP 422: body is too long (maximum is 125000 characters)` -- and it failed
*after* `Build sdist + wheel` and `Publish to PyPI` had already succeeded, so
the package had already shipped to PyPI (irreversible) when the announcement
step failed. The step that produced the oversized body was an inline `awk`
one-liner in the `github-release` job, run only after `publish` -- there was
no size check at all, and no way to unit-test an inline `run:` heredoc.

This script is that logic, factored out so it can run **in the `build` job**,
before `publish`, and so its degrade path can be exercised by a real test
(`tests/test_extract_release_notes.py`) instead of assumed:

- Under the cap: the CHANGELOG section for the tag's version is emitted
  verbatim (matches the pre-athenaeum#1442 behavior).
- Over the cap: the section is truncated on a line boundary with a visible
  notice plus a working link to the full section **at the tag** (so the link
  keeps resolving even after CHANGELOG.md changes on the default branch).
  A short, linked release beats a failed release job after an irreversible
  PyPI upload.
- No section found for the version at all: falls back to a bare CHANGELOG.md
  link (matches the pre-athenaeum#1442 fallback).

Run standalone:

    python scripts/extract_release_notes.py \\
        --changelog CHANGELOG.md --version 0.20.0 \\
        --repo Kromatic-Innovation/athenaeum --tag v0.20.0 \\
        --output release-notes.md

`release.yml`'s `build` job invokes it exactly like that (see the
"Extract + size-guard release notes" step) and uploads the resulting
`release-notes.md` as a build artifact; the `github-release` job (which runs
after `publish`) only downloads and uses it -- it never re-derives or
re-checks the body, because by the time `publish` has run the body is already
known-publishable.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# GitHub's hard cap on a release body, confirmed by the HTTP 422 that failed
# the v0.20.0 tag push: "body is too long (maximum is 125000 characters)".
GITHUB_RELEASE_BODY_HARD_CAP = 125_000

# Our own threshold, held well below the hard cap (issue athenaeum#1442,
# acceptance criterion 3). Reasoning:
#   - the hand-curated body that replaced the failed v0.20.0 release was
#     ~3,200 characters -- a release body anywhere near 125,000 characters
#     is not something a human reads anyway, so truncating well before the
#     hard cap costs nothing in practice;
#   - headroom below the hard cap absorbs the truncation notice + link text
#     appended after truncation, and any drift between how this script
#     counts characters (Python `len(str)`) and how GitHub's API counts
#     them server-side;
#   - 60,000 is roughly half the hard cap: generous enough that a normal,
#     even a fairly large, release note is never truncated, small enough
#     that hitting it at all is a clear signal something unusual (like the
#     v0.20.0 seven-theme changelog megasection) is going on.
DEFAULT_MAX_CHARS = 60_000

_SECTION_MARKER_PREFIX = "## ["
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]", re.UNICODE)


def github_anchor(header_line: str) -> str:
    """Approximate a GitHub Flavored Markdown heading anchor slug.

    GitHub lowercases the heading text, strips punctuation (keeping word
    characters, spaces, and hyphens), then turns spaces into hyphens. This is
    good enough for our own generated links: there is no ambiguity to resolve
    because CHANGELOG.md version headers are unique, so we never need the
    duplicate-heading `-1`/`-2` disambiguation GitHub's own slugger adds for
    repeated headings.
    """
    text = header_line.lstrip("#").strip().lower()
    text = _SLUG_STRIP_RE.sub("", text)
    return text.replace(" ", "-")


def extract_section(changelog_text: str, version: str) -> tuple[str, str] | None:
    """Return ``(header_line, section_body)`` for ``## [<version>]`` in
    ``changelog_text``, or ``None`` if no such section exists.

    Mirrors the awk one-liner this replaces: a section starts at a line
    beginning with ``## [<version>]`` and ends at the next line beginning
    with ``## [`` (or end of file). Sub-headings inside a section use `###`
    or deeper, so they never trip the end-of-section check.
    """
    marker = f"{_SECTION_MARKER_PREFIX}{version}]"
    header: str | None = None
    body_lines: list[str] = []
    found = False
    for line in changelog_text.splitlines():
        if not found:
            if line.startswith(marker):
                found = True
                header = line
            continue
        if line.startswith(_SECTION_MARKER_PREFIX):
            break
        body_lines.append(line)
    if not found or header is None:
        return None
    return header, "\n".join(body_lines).strip("\n")


def _truncate_on_boundary(text: str, budget: int) -> str:
    """Cut ``text`` to at most ``budget`` characters, backing up to the
    nearest preceding newline so the cut never lands mid-word or mid-line.

    Never returns more than ``budget`` characters.
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    truncated = text[:budget]
    last_newline = truncated.rfind("\n")
    if last_newline == -1:
        # No newline within budget at all (pathologically long single line).
        # Still never exceed budget.
        return truncated
    return truncated[:last_newline]


def build_release_notes(
    *,
    changelog_text: str,
    version: str,
    repo: str,
    tag: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Build the final ``--notes-file`` body for ``tag`` in ``repo``.

    - No section found: fall back to a bare link to CHANGELOG.md (matches
      the pre-athenaeum#1442 workflow behavior).
    - Section fits under ``max_chars``: returned verbatim (plus a single
      trailing newline).
    - Section exceeds ``max_chars``: truncated on a line boundary, with a
      visible notice plus a working link to the full section **at the tag**
      (a tag ref, so it keeps resolving even after CHANGELOG.md changes on
      the default branch).

    The return value is always at most ``max_chars`` characters.
    """
    changelog_link = f"https://github.com/{repo}/blob/{tag}/CHANGELOG.md"
    section = extract_section(changelog_text, version)

    if section is None:
        print(
            f"::warning::No CHANGELOG.md section found for [{version}]; "
            "falling back to a CHANGELOG link",
            file=sys.stderr,
        )
        return f"See [CHANGELOG.md]({changelog_link}) for details.\n"

    header, body = section
    full = (f"{header}\n{body}" if body else header).strip("\n") + "\n"

    if len(full) <= max_chars:
        return full

    anchor = github_anchor(header)
    full_link = f"{changelog_link}#{anchor}"
    notice = (
        f"\n\n… truncated ({len(full):,} characters exceeds this "
        f"release's {max_chars:,}-character notes limit) — "
        f"full notes: {full_link}\n"
    )
    print(
        f"::warning::Release notes for {tag} truncated: {len(full):,} chars "
        f"exceeds the {max_chars:,}-char limit; full notes linked at {full_link}",
        file=sys.stderr,
    )
    budget = max_chars - len(notice)
    if budget <= 0:
        # Pathological: max_chars smaller than the notice itself. Emit only
        # the notice (it still carries a working link) rather than raising.
        return notice.lstrip("\n")[:max_chars]
    truncated_body = _truncate_on_boundary(full, budget)
    result = truncated_body.rstrip("\n") + notice
    # Belt-and-suspenders: never return more than max_chars, even if the
    # boundary search above landed unexpectedly.
    return result[:max_chars]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", required=True, type=Path, help="path to CHANGELOG.md")
    parser.add_argument("--version", required=True, help="bare version, e.g. 0.20.0")
    parser.add_argument(
        "--repo", required=True, help="owner/name, e.g. ${{ github.repository }}"
    )
    parser.add_argument(
        "--tag", required=True, help="tag ref name, e.g. ${{ github.ref_name }} (v0.20.0)"
    )
    parser.add_argument("--output", required=True, type=Path, help="where to write the body")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"truncation threshold (default {DEFAULT_MAX_CHARS:,})",
    )
    args = parser.parse_args(argv)

    changelog_text = args.changelog.read_text(encoding="utf-8")
    notes = build_release_notes(
        changelog_text=changelog_text,
        version=args.version,
        repo=args.repo,
        tag=args.tag,
        max_chars=args.max_chars,
    )
    args.output.write_text(notes, encoding="utf-8")
    print(f"extract_release_notes: wrote {len(notes)} chars to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
