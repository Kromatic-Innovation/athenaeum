# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1168 — mention-density UNION gate before a tier-3 merge call.

Every surviving ``tier1_programmatic_match`` result becomes one tier-3 merge
LLM call (``librarian.py``). Before this gate, a single incidental
word-boundary mention of ANY indexed entity was enough to trigger a full-page
merge. This suite pins the shipped UNION gate: a match survives when EITHER
it has >= ``mention_density_min_occurrences`` word-boundary occurrences in
the file, OR its key is high-specificity (>= ``mention_density_specificity_
chars`` characters, or multi-token). A match is dropped only when BOTH
conditions fail -- low-specificity key AND a singleton mention.

Per the issue's correction, this is the SHIPPED configuration (measured
-20.1% on the issue's stratified sample); the raw ">= 2 occurrences" gate
alone (measured -51.2%, never had its false-negative profile measured) is
explicitly NOT what ships.

No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import EntityIndex, RawFile
from athenaeum.tiers import (
    DEFAULT_MENTION_DENSITY_MIN_OCCURRENCES,
    DEFAULT_MENTION_DENSITY_SPECIFICITY_CHARS,
    resolve_mention_density_min_occurrences,
    resolve_mention_density_specificity_chars,
    tier1_programmatic_match,
)


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _index_with_names(tmp_path: Path, names: list[str]) -> EntityIndex:
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    idx = EntityIndex(wiki)
    for i, name in enumerate(names):
        idx._by_name[name.lower()] = (f"uid-{i}", wiki / f"page-{i}.md")
    return idx


# ---------------------------------------------------------------------------
# The union gate itself
# ---------------------------------------------------------------------------


def test_short_single_token_key_dropped_on_single_mention(tmp_path: Path) -> None:
    """The core gate: a short (< 8 char), single-token key mentioned ONCE
    does not survive to become a tier-3 call."""
    index = _index_with_names(tmp_path, ["orca"])
    raw = _make_raw("The deal closed under Subscription (Orca).")
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "orca" not in names


def test_short_single_token_key_survives_on_two_mentions(tmp_path: Path) -> None:
    """Density clause: two word-boundary occurrences clear the gate even
    though the key is short and single-token."""
    index = _index_with_names(tmp_path, ["orca"])
    raw = _make_raw("Orca shipped the feature. Orca is the codename.")
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "orca" in names


def test_multi_token_key_survives_on_single_mention(tmp_path: Path) -> None:
    """Specificity clause: a multi-token key (e.g. a full personal name)
    qualifies on a single mention -- the common "named once, something
    important said" case is NOT affected by this gate."""
    index = _index_with_names(tmp_path, ["jane doe"])
    raw = _make_raw("Jane Doe signed the contract today.")
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "jane doe" in names


def test_long_single_token_key_survives_on_single_mention(tmp_path: Path) -> None:
    """Specificity clause: a single-token key >= 8 chars qualifies on a
    single mention (e.g. a distinctive company name)."""
    index = _index_with_names(tmp_path, ["athenaeum"])  # 9 chars, single token
    raw = _make_raw("We evaluated Athenaeum for the memory layer.")
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "athenaeum" in names


def test_short_single_token_key_at_specificity_boundary(tmp_path: Path) -> None:
    """Exactly 8 chars (the default specificity threshold) is exempt; 7 is not."""
    idx = _index_with_names(tmp_path, ["eightchr"])  # exactly 8 chars
    raw = _make_raw("Eightchr announced the merger.")
    names = {n for n, _, _ in tier1_programmatic_match(raw, idx)}
    assert "eightchr" in names

    idx7 = _index_with_names(tmp_path, ["sevench"])  # exactly 7 chars
    raw7 = _make_raw("Sevench announced the merger.")
    names7 = {n for n, _, _ in tier1_programmatic_match(raw7, idx7)}
    assert "sevench" not in names7


def test_junk_filter_and_density_gate_compose(tmp_path: Path) -> None:
    """The density gate runs AFTER the athenaeum#662 junk filter -- a junk
    name stays dropped regardless of density, and a legit multi-token name
    is unaffected by either."""
    index = _index_with_names(tmp_path, ["here", "acme corp"])
    raw = _make_raw("Here is a note. Here we go again. Acme Corp shipped.")
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "here" not in names  # junk filter, independent of 2 occurrences
    assert "acme corp" in names


def test_no_key_stops_firing_entirely(tmp_path: Path) -> None:
    """AC: the gate suppresses MATCHES, not KEYS -- the same short key still
    matches when density (or a later multi-token alias) qualifies it."""
    index = _index_with_names(tmp_path, ["orca"])
    single = _make_raw("Orca shipped the feature.")
    double = _make_raw("Orca shipped the feature. Orca is the codename.")
    assert "orca" not in {n for n, _, _ in tier1_programmatic_match(single, index)}
    assert "orca" in {n for n, _, _ in tier1_programmatic_match(double, index)}


# ---------------------------------------------------------------------------
# Config resolvers
# ---------------------------------------------------------------------------


class TestResolveMentionDensityMinOccurrences:
    def test_default(self) -> None:
        assert resolve_mention_density_min_occurrences(None) == (
            DEFAULT_MENTION_DENSITY_MIN_OCCURRENCES
        )

    def test_operator_override(self) -> None:
        cfg = {"librarian": {"mention_density_min_occurrences": 3}}
        assert resolve_mention_density_min_occurrences(cfg) == 3

    def test_bool_rejected_as_int_subclass(self) -> None:
        cfg = {"librarian": {"mention_density_min_occurrences": True}}
        assert resolve_mention_density_min_occurrences(cfg) == (
            DEFAULT_MENTION_DENSITY_MIN_OCCURRENCES
        )

    def test_non_positive_and_malformed_fall_back(self) -> None:
        for bad in (
            None,
            {},
            {"librarian": "nope"},
            {"librarian": {"mention_density_min_occurrences": 0}},
            {"librarian": {"mention_density_min_occurrences": -1}},
            {"librarian": {"mention_density_min_occurrences": "2"}},
            {"librarian": {"mention_density_min_occurrences": 2.5}},
        ):
            assert resolve_mention_density_min_occurrences(bad) == (
                DEFAULT_MENTION_DENSITY_MIN_OCCURRENCES
            )


class TestResolveMentionDensitySpecificityChars:
    def test_default(self) -> None:
        assert resolve_mention_density_specificity_chars(None) == (
            DEFAULT_MENTION_DENSITY_SPECIFICITY_CHARS
        )

    def test_operator_override(self) -> None:
        cfg = {"librarian": {"mention_density_specificity_chars": 5}}
        assert resolve_mention_density_specificity_chars(cfg) == 5

    def test_bool_rejected_as_int_subclass(self) -> None:
        cfg = {"librarian": {"mention_density_specificity_chars": True}}
        assert resolve_mention_density_specificity_chars(cfg) == (
            DEFAULT_MENTION_DENSITY_SPECIFICITY_CHARS
        )

    def test_non_positive_and_malformed_fall_back(self) -> None:
        for bad in (
            None,
            {},
            {"librarian": "nope"},
            {"librarian": {"mention_density_specificity_chars": 0}},
            {"librarian": {"mention_density_specificity_chars": -1}},
            {"librarian": {"mention_density_specificity_chars": "8"}},
        ):
            assert resolve_mention_density_specificity_chars(bad) == (
                DEFAULT_MENTION_DENSITY_SPECIFICITY_CHARS
            )


# ---------------------------------------------------------------------------
# Operator tuning end-to-end through tier1_programmatic_match
# ---------------------------------------------------------------------------


def test_operator_can_disable_density_gate(tmp_path: Path) -> None:
    """Setting min_occurrences=1 makes the density clause a no-op (any
    match, having occurred >= 1 time by construction, always clears it) --
    an operator escape hatch back to pre-athenaeum#1168 behavior."""
    index = _index_with_names(tmp_path, ["orca"])
    raw = _make_raw("Orca shipped the feature.")
    cfg = {"librarian": {"mention_density_min_occurrences": 1}}
    names = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
    assert "orca" in names


def test_operator_can_tighten_specificity_threshold(tmp_path: Path) -> None:
    """Raising the specificity threshold makes a previously-exempt
    single-token key subject to the density requirement again."""
    index = _index_with_names(tmp_path, ["athenaeum"])  # 9 chars
    raw = _make_raw("We evaluated Athenaeum for the memory layer.")
    cfg = {"librarian": {"mention_density_specificity_chars": 10}}
    names = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
    assert "athenaeum" not in names  # no longer exempt, and only 1 occurrence
