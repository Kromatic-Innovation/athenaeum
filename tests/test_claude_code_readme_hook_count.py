# SPDX-License-Identifier: Apache-2.0
"""Pin test for athenaeum#1385: the hook count claimed in the lead sentence of
``examples/claude-code/README.md`` must match the row count of the hook table
directly beneath it.

The two numbers drift independently — a new hook lands a table row without
anyone updating the sentence above it (this happened twice: athenaeum#128
added `pending-questions-surface.sh`, and `rebuild-index.sh` landed later,
neither touching the lead count). Nothing else in the repo checks the two
against each other, so this test is the only thing that would catch the next
one.

Both parsers are pure functions over README *text* (not file paths), and are
exercised twice: once against the live file (the real regression guard) and
once against a synthetic fixture whose two counts are deliberately made to
disagree (the meta-test — proves the parsers actually distinguish match from
mismatch rather than e.g. both returning 0). A live-file-only test would pass
even if a parser silently returned nothing, as long as that nothing happened
to compare falsely-equal to another exact same accidental nothing; the
synthetic fixture rules that out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README_PATH = (
    Path(__file__).resolve().parent.parent / "examples" / "claude-code" / "README.md"
)

_WORD_TO_NUM = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_LEAD_COUNT_RE = re.compile(r"^(?P<count>[A-Za-z]+|\d+)\s+hook scripts\b")

_TABLE_HEADER_RE = re.compile(r"^\|\s*Hook\s*\|", re.IGNORECASE)


def parse_lead_hook_count(text: str) -> int:
    """Parse the hook count out of the README's lead sentence.

    The count may be spelled as a word ("Three") or a digit ("3"). Scans
    every line for a `"<count> hook scripts"` phrase rather than assuming a
    fixed line position, so it works against both the live file and an
    arbitrary synthetic fixture.

    Raises ``ValueError`` (never returns ``None`` or ``0`` as a stand-in) if
    no such sentence is found, so a broken/rotted parser fails the pin test
    loudly instead of silently comparing two "not found" results as equal.
    """
    for line in text.splitlines():
        match = _LEAD_COUNT_RE.match(line.strip())
        if not match:
            continue
        token = match.group("count")
        if token.isdigit():
            return int(token)
        num = _WORD_TO_NUM.get(token.lower())
        if num is not None:
            return num
        raise ValueError(f"lead sentence hook count is an unrecognized word: {token!r}")
    raise ValueError(
        "could not find a lead sentence of the form '<N> hook scripts' "
        "(count spelled as a word or a digit) in the given README text"
    )


def parse_hook_table_row_count(text: str) -> int:
    """Parse the number of data rows in the hook table.

    Locates the table by its header row (`"| Hook | ..."`), skips the
    following markdown separator row (`"|---|---|"`), then counts
    consecutive `|`-prefixed lines as data rows.

    Raises ``ValueError`` if no such table is found, for the same
    fail-loudly reason as ``parse_lead_hook_count``.
    """
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _TABLE_HEADER_RE.match(line.strip()):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            "could not find the hook table (no '| Hook | ...' header row) "
            "in the given README text"
        )

    row_count = 0
    # header_idx + 1 is the markdown separator row (|---|---|); data rows
    # start at header_idx + 2.
    for line in lines[header_idx + 2 :]:
        if not line.strip().startswith("|"):
            break
        row_count += 1
    return row_count


def _opening_section(text: str) -> str:
    """Return the text from the first heading up to (not including) the next
    heading — i.e. the README's opening section."""
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("#"):
            start = i
            break
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#"):
            end = i
            break
    return "\n".join(lines[start:end])


def test_lead_hook_count_matches_table_row_count() -> None:
    """The real regression guard: the live README's two numbers must agree."""
    text = README_PATH.read_text()
    lead_count = parse_lead_hook_count(text)
    table_rows = parse_hook_table_row_count(text)
    assert lead_count == table_rows, (
        f"examples/claude-code/README.md lead sentence claims {lead_count} "
        f"hook scripts but the hook table beneath it has {table_rows} rows — "
        "update one to match the other (athenaeum#1385)"
    )


def test_stop_hook_validate_named_in_opening_section() -> None:
    """`stop-hook-validate.sh` must be named in the opening section, so the
    lead paragraph and the later 'Auto-memory integration' table do not
    contradict each other about what the directory contains."""
    text = README_PATH.read_text()
    opening = _opening_section(text)
    assert "stop-hook-validate.sh" in opening, (
        "opening section of examples/claude-code/README.md must name "
        "`stop-hook-validate.sh` as a hook script shipped in this directory "
        "(athenaeum#1385)"
    )


def test_synthetic_mismatch_is_detected() -> None:
    """Meta-test (hard AC): run both parsers against a synthetic README whose
    lead count and table row count deliberately differ, and assert the
    mismatch is reported. This is what proves the parsers actually parse,
    rather than both trivially returning the same default on every input."""
    synthetic_readme = (
        "# Claude Code integration\n"
        "\n"
        "Five hook scripts that wire Athenaeum into Claude Code as a\n"
        "transparent recall sidecar.\n"
        "\n"
        "| Hook          | When it fires | What it does |\n"
        "|----------------|----------------|---------------|\n"
        "| `alpha.sh`     | Start          | does alpha    |\n"
        "| `beta.sh`      | Start          | does beta     |\n"
        "| `gamma.sh`     | Start          | does gamma    |\n"
        "\n"
        "## Next section\n"
        "\n"
        "unrelated content\n"
    )

    lead_count = parse_lead_hook_count(synthetic_readme)
    table_rows = parse_hook_table_row_count(synthetic_readme)

    assert lead_count == 5
    assert table_rows == 3
    assert lead_count != table_rows, (
        "synthetic fixture is supposed to have mismatched counts — if this "
        "assertion fails the parsers stopped distinguishing real input"
    )


def test_parsers_fail_loudly_when_nothing_matches() -> None:
    """Neither parser may silently return `0`/`None` on text with no lead
    sentence or no table — both must raise, per the issue's explicit
    rejection of a parser that 'returns nothing'."""
    no_lead_sentence = "# Claude Code integration\n\nNo count sentence here.\n"
    with pytest.raises(ValueError):
        parse_lead_hook_count(no_lead_sentence)

    no_table = "# Claude Code integration\n\nThree hook scripts, no table below.\n"
    with pytest.raises(ValueError):
        parse_hook_table_row_count(no_table)
