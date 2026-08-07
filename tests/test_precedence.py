# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum.precedence` (issue athenaeum#797, design doc
`docs/field-corrections.md` §6.1).

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

import pytest

from athenaeum.precedence import (
    SOURCE_PRECEDENCE_TIERS,
    UNKNOWN_SOURCE_RANK,
    source_rank,
)
from athenaeum.resolutions import _RESOLVE_SYSTEM

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
