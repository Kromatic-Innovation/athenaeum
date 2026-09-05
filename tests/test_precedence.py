# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum.precedence` (issue athenaeum#797, design doc
`docs/design/field-corrections.md` §6.1).

The membership-drift test is the point of this file, not a footnote: a
naive test that merely counts tiers passes forever while missing an omitted
token (the first draft of the design doc dropped `twitter:` exactly this
way — indistinguishable from the token legitimately ranking 9 as
`unsourced`). So this test PARSES the `SOURCE-PRECEDENCE TAXONOMY` block of
`resolutions._RESOLVE_SYSTEM` — the prose the module derives from — rather
than transcribing the expected tiers by hand, and it proves the parser
itself is doing real work via a positive control that mutates the prompt
text and asserts the comparison then FAILS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from athenaeum import resolutions
from athenaeum.precedence import (
    SOURCE_PRECEDENCE_TIERS,
    UNKNOWN_SOURCE_RANK,
    source_rank,
)
from athenaeum.resolutions import _RESOLVE_SYSTEM

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_DIR = _REPO_ROOT / "docs"

_TAXONOMY_HEADER = "SOURCE-PRECEDENCE TAXONOMY (highest to lowest):"
_TAXONOMY_FOOTER = "TIE-BREAK:"

# A tier-line token looks like ``<type>:<ref-placeholder>``. We only ever
# need the type segment (before the first colon); the ref half is
# free-form documentation prose, not part of the taxonomy.
_TOKEN_RE = re.compile(r"^([a-z][a-z0-9_-]*):")
# The one tier with no ``<type>:<ref>`` shorthand at all is the terminal
# fallback ("unsourced / empty — always loses..."): its label is a bare
# word, followed by a bare-word PROSE SYNONYM ("empty"), not a second
# equally-ranked type token the way tier 2's "linkedin: / twitter:" is.
# A bare word is therefore accepted only as a tier's PRIMARY (first)
# label, never as a secondary alternative — which is exactly what admits
# "unsourced" while excluding "empty".
_BARE_WORD_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _extract_taxonomy_block(prompt_text: str) -> str:
    start = prompt_text.index(_TAXONOMY_HEADER) + len(_TAXONOMY_HEADER)
    end = prompt_text.index(_TAXONOMY_FOOTER, start)
    return prompt_text[start:end]


def _parse_taxonomy(prompt_text: str) -> tuple[tuple[str, ...], ...]:
    """Parse the ``SOURCE-PRECEDENCE TAXONOMY`` block into ordered tiers.

    Returns one tuple of de-duplicated (order-preserving) type tokens per
    numbered tier line, ordered by the tier's own number. A malformed or
    absent block parses to ``()`` — deliberately not an exception — so the
    denominator assertions below are what catches a broken parser, exactly
    as the module docstring requires ("a parser that silently extracts
    nothing compares empty to empty and passes forever").
    """
    block = _extract_taxonomy_block(prompt_text)
    # Split into per-tier chunks: each chunk starts at a line beginning
    # with "<digits>. " and runs until the next such line (continuation
    # lines for multi-line entries, e.g. tiers 5 and 8, are indented and
    # therefore swept into the same chunk).
    chunks = re.split(r"\n(?=\d+\.\s)", block.strip())
    tiers: list[tuple[str, ...]] = []
    for chunk in chunks:
        m = re.match(r"^(\d+)\.\s+(.*)", chunk, re.DOTALL)
        if not m:
            continue
        body = m.group(2)
        # The token list lives before the first em-dash; everything after
        # is descriptive prose, not taxonomy membership.
        header = body.split("—", 1)[0]
        seen: dict[str, None] = {}
        for i, part in enumerate(header.split("/")):
            stripped = part.strip()
            token_match = _TOKEN_RE.match(stripped)
            if token_match:
                seen[token_match.group(1)] = None
            elif i == 0 and _BARE_WORD_RE.match(stripped):
                seen[stripped] = None
        tiers.append(tuple(seen.keys()))
    return tuple(tiers)


def test_parser_denominator_sanity() -> None:
    """The parser must actually parse something non-trivial.

    Guards against the exact failure the module docstring warns about — a
    parser that silently extracts nothing (or too little) and thereby
    compares empty-to-empty forever. 9 tiers, at least 10 raw type tokens
    (tier 3 alone contributes two — ``api:apollo`` / ``api:<vendor>``).
    """
    tiers = _parse_taxonomy(_RESOLVE_SYSTEM)
    assert len(tiers) == 9
    total_tokens = sum(len(tier) for tier in tiers)
    assert total_tokens >= 10, (
        f"parsed only {total_tokens} tokens across {len(tiers)} tiers — "
        "parser regression (would silently pass an empty-vs-empty compare)"
    )


def test_precedence_tiers_match_resolve_system_prompt() -> None:
    """`SOURCE_PRECEDENCE_TIERS` must agree with the prompt on order AND
    per-tier membership — not merely on tier count.

    This is the exact regression: an omitted token (e.g. ``twitter:``
    silently dropped from tier 2) still leaves 9 tiers, so a count-only
    comparison would pass while the omitted token quietly ranks 9 — a
    seven-rank demotion indistinguishable from ``unsourced``.
    """
    parsed = _parse_taxonomy(_RESOLVE_SYSTEM)
    assert len(parsed) == len(SOURCE_PRECEDENCE_TIERS) == 9
    for rank, (parsed_tier, code_tier) in enumerate(
        zip(parsed, SOURCE_PRECEDENCE_TIERS), start=1
    ):
        assert frozenset(parsed_tier) == frozenset(code_tier), (
            f"tier {rank} membership drift: prompt says {parsed_tier!r}, "
            f"precedence.py says {code_tier!r}"
        )


def test_positive_control_mutated_prompt_fails_comparison() -> None:
    """Prove the parser does real work: drop `twitter:` from the taxonomy
    text in-test (reproducing the design doc's first-draft bug) and assert
    the comparison against `SOURCE_PRECEDENCE_TIERS` then FAILS.

    Without this, a parser that always returns the same thing regardless of
    input would make the drift test above pass forever for the wrong
    reason — comparing two hard-coded structures instead of actually
    reading the prompt.
    """
    mutated = _RESOLVE_SYSTEM.replace(
        "2. linkedin:<username> / twitter:<username> — user-curated public profile.",
        "2. linkedin:<username> — user-curated public profile.",
    )
    # Confirm the mutation actually took (guards the guard).
    assert mutated != _RESOLVE_SYSTEM
    parsed = _parse_taxonomy(mutated)
    assert frozenset(parsed[1]) != frozenset(SOURCE_PRECEDENCE_TIERS[1]), (
        "positive control did not perturb tier 2 — the parser is not "
        "actually reading the mutated text"
    )


# ---------------------------------------------------------------------------
# Drift-guard site (3): the `9-tier` count in resolutions.py's module
# docstring (issue athenaeum#1375).
#
# Before this pair, `tests/test_precedence.py` parsed the canonical
# `SOURCE-PRECEDENCE TAXONOMY` block itself but never read
# `resolutions.__doc__` — the drift guard at `resolutions.py:403-421`
# NAMED this site as INDEPENDENT and requiring a test, but nothing bound it.
# ---------------------------------------------------------------------------

_MODULE_DOC_TIER_COUNT_RE = re.compile(r"(\d+)-tier")


def test_module_docstring_tier_count_matches_taxonomy() -> None:
    """`resolutions.__doc__` states the tier count as ``<N>-tier``; assert it
    against the canonical parse rather than trusting a hand-maintained
    literal (drift-guard site (3), `resolutions.py:403-421`).
    """
    doc = resolutions.__doc__ or ""
    match = _MODULE_DOC_TIER_COUNT_RE.search(doc)
    assert match, (
        "resolutions.__doc__ does not contain an `<N>-tier` count — parser "
        "regression (would silently pass a doc that says nothing about the "
        "tier count)"
    )
    stated = int(match.group(1))
    expected = len(_parse_taxonomy(_RESOLVE_SYSTEM))
    assert stated == expected, (
        f"module docstring states {stated}-tier, but the canonical "
        f"SOURCE-PRECEDENCE TAXONOMY block parses to {expected} tiers"
    )


def test_module_docstring_tier_count_positive_control() -> None:
    """Prove the parser does real work: perturb the docstring's stated tier
    count in-test and assert the comparison against the canonical parse
    then FAILS.
    """
    mutated_doc = _MODULE_DOC_TIER_COUNT_RE.sub(
        "11-tier", resolutions.__doc__ or "", count=1
    )
    # Confirm the mutation actually took (guards the guard).
    assert mutated_doc != resolutions.__doc__
    match = _MODULE_DOC_TIER_COUNT_RE.search(mutated_doc)
    assert match is not None
    stated = int(match.group(1))
    expected = len(_parse_taxonomy(_RESOLVE_SYSTEM))
    assert stated != expected, (
        "positive control did not perturb the docstring tier count — the "
        "comparison would still pass for the wrong reason"
    )


# ---------------------------------------------------------------------------
# Drift-guard site (4a): docs/design/conflict-resolution.md §11's inline list of
# the taxonomy's `<type>:` components (issue athenaeum#1375).
#
# The doc omitted `twitter:` — nine tokens instead of ten — while
# `precedence.py` ranks it correctly at tier 2. This is the exact drift the
# guard names but nothing previously bound.
# ---------------------------------------------------------------------------

_SECTION_HEADER_RE = re.compile(r"^## (\d+)\. ", re.MULTILINE)
_TYPE_COMPONENT_LIST_RE = re.compile(
    r"shorthand's type component \(([^)]*)\)", re.DOTALL
)
_BACKTICKED_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_-]*):?`")


def _extract_markdown_section(markdown_text: str, section_number: int) -> str:
    """Return the body of markdown section ``## <section_number>. ...`` up to
    (not including) the next numbered ``## <n>. `` heading.

    Bounding the section this way keeps later, unrelated sections (and their
    own backticked tokens) out of scope.
    """
    headers = list(_SECTION_HEADER_RE.finditer(markdown_text))
    for i, header in enumerate(headers):
        if int(header.group(1)) == section_number:
            start = header.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown_text)
            return markdown_text[start:end]
    raise AssertionError(f"section {section_number} heading not found in document")


def _parse_doc_type_components(section_text: str) -> frozenset[str]:
    """Extract the backticked ``<type>:`` tokens from the ONE parenthesized
    list that follows "shorthand's type component" in §11.

    Deliberately scoped to that single parenthesized run, not the whole
    section: §11's later "Change" bullets mention ``model-prior:<model-id>``
    and ``script:<slug>`` in unrelated prose, and a looser scan would
    pollute the set with those.
    """
    match = _TYPE_COMPONENT_LIST_RE.search(section_text)
    if not match:
        return frozenset()
    return frozenset(_BACKTICKED_TOKEN_RE.findall(match.group(1)))


def _canonical_token_set(prompt_text: str) -> frozenset[str]:
    return frozenset(tok for tier in _parse_taxonomy(prompt_text) for tok in tier)


def test_conflict_resolution_doc_type_components_match_taxonomy() -> None:
    """§11's inline type-component list must name every token in the
    canonical taxonomy — drift-guard site (4a), `resolutions.py:403-421`.
    """
    doc_text = (_DOCS_DIR / "design" / "conflict-resolution.md").read_text(encoding="utf-8")
    section = _extract_markdown_section(doc_text, 11)
    doc_tokens = _parse_doc_type_components(section)
    assert doc_tokens, (
        "no `shorthand's type component (...)` list found in §11 — parser "
        "regression (would silently pass an empty comparison)"
    )
    canonical_tokens = _canonical_token_set(_RESOLVE_SYSTEM)
    assert doc_tokens == canonical_tokens, (
        f"docs/design/conflict-resolution.md §11's type-component list "
        f"{sorted(doc_tokens)} does not match the canonical taxonomy's "
        f"token set {sorted(canonical_tokens)}"
    )


def test_conflict_resolution_doc_type_components_positive_control() -> None:
    """Prove the parser does real work: drop `twitter:` from the canonical
    block (reproducing this issue's exact regression) and assert the §11
    comparison then FAILS.
    """
    mutated = _RESOLVE_SYSTEM.replace(
        "2. linkedin:<username> / twitter:<username> — user-curated public profile.",
        "2. linkedin:<username> — user-curated public profile.",
    )
    # Confirm the mutation actually took (guards the guard).
    assert mutated != _RESOLVE_SYSTEM
    mutated_tokens = _canonical_token_set(mutated)
    assert "twitter" not in mutated_tokens

    doc_text = (_DOCS_DIR / "design" / "conflict-resolution.md").read_text(encoding="utf-8")
    section = _extract_markdown_section(doc_text, 11)
    doc_tokens = _parse_doc_type_components(section)
    assert doc_tokens, "no type-component list parsed from §11 (see sibling test)"

    assert doc_tokens != mutated_tokens, (
        "positive control did not perturb the comparison — the §11 token "
        "set should no longer match the (twitter-dropped) canonical set"
    )


# ---------------------------------------------------------------------------
# Drift-guard site (4b): docs/design/field-corrections.md §6.1's stated tier and
# minimum-token counts (issue athenaeum#1375).
# ---------------------------------------------------------------------------

_FIELD_CORRECTIONS_DENOMINATOR_RE = re.compile(
    r"parsed (\d+) tiers and at least (\d+) tokens"
)


def test_field_corrections_doc_denominator_matches_taxonomy() -> None:
    """§6.1 states the drift-guard test's expected denominator inline
    ("it parsed 9 tiers and at least 10 tokens"); assert those numbers
    agree with the canonical taxonomy block — drift-guard site (4b).
    """
    doc_text = (_DOCS_DIR / "design" / "field-corrections.md").read_text(encoding="utf-8")
    match = _FIELD_CORRECTIONS_DENOMINATOR_RE.search(doc_text)
    assert match, (
        "docs/design/field-corrections.md does not contain the 'parsed <N> tiers "
        "and at least <M> tokens' sentence — parser regression"
    )
    stated_tiers = int(match.group(1))
    stated_min_tokens = int(match.group(2))

    tiers = _parse_taxonomy(_RESOLVE_SYSTEM)
    assert stated_tiers == len(tiers), (
        f"§6.1 states {stated_tiers} tiers, but the canonical block parses "
        f"to {len(tiers)}"
    )
    total_tokens = sum(len(tier) for tier in tiers)
    assert stated_min_tokens <= total_tokens, (
        f"§6.1 states a minimum of {stated_min_tokens} tokens, but the "
        f"canonical block only parses {total_tokens}"
    )


def test_field_corrections_doc_denominator_positive_control() -> None:
    """Prove the parser does real work: drop `twitter:` from the canonical
    block and assert the stated minimum-token-count comparison then FAILS.
    """
    mutated = _RESOLVE_SYSTEM.replace(
        "2. linkedin:<username> / twitter:<username> — user-curated public profile.",
        "2. linkedin:<username> — user-curated public profile.",
    )
    # Confirm the mutation actually took (guards the guard).
    assert mutated != _RESOLVE_SYSTEM
    mutated_total_tokens = sum(len(tier) for tier in _parse_taxonomy(mutated))

    doc_text = (_DOCS_DIR / "design" / "field-corrections.md").read_text(encoding="utf-8")
    match = _FIELD_CORRECTIONS_DENOMINATOR_RE.search(doc_text)
    assert match, "no 'parsed <N> tiers and at least <M> tokens' sentence found"
    stated_min_tokens = int(match.group(2))

    assert mutated_total_tokens < stated_min_tokens, (
        "positive control did not perturb the comparison — dropping "
        "twitter: from the canonical block should drop the total token "
        "count below the doc-stated minimum"
    )


@pytest.mark.parametrize(
    "source,expected_rank",
    [
        ("user:conv-2026", 1),
        ("linkedin:tkromer", 2),
        ("twitter:someone", 2),  # the exact regression this design guards
        ("api:apollo", 3),
        ("wikipedia:Foo", 4),
        ("agent-observed:claude:session-1", 5),
        ("claude:tier3-write", 6),
        ("script:enrich", 7),
        ("model-prior:claude-3-opus", 8),
        ("unsourced:x", 9),
        (None, 9),
        ("totally-unknown-type:ref", 9),
    ],
)
def test_source_rank(source: str | None, expected_rank: int) -> None:
    assert source_rank(source) == expected_rank


def test_source_rank_unparseable_scalar_ranks_unknown() -> None:
    assert source_rank("not a valid source") == UNKNOWN_SOURCE_RANK


def test_source_rank_structured_dict() -> None:
    assert source_rank({"type": "api", "ref": "apollo"}) == 3
