# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#662 — junk Tier-1 matches must not inflate the Tier-3 call count.

``tier1_programmatic_match`` matches any indexed page name >= 3 chars, and the
wiki index accumulates junk pages (``here``, ``get``, ``main``, ``reach``,
``lane a``). Each match becomes a Tier-3 merge LLM call against a 16-23KB page —
measured at ~15-18 calls/file on the live host, roughly HALF worthless. This
suite pins the filter that drops junk matches before the Tier-3 call is issued,
and — the load-bearing part — proves a legitimate short-name entity survives.

No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import EntityIndex, RawFile
from athenaeum.tiers import (
    DEFAULT_JUNK_MATCH_STOPWORDS,
    resolve_junk_match_names,
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
    """An EntityIndex populated with exactly *names* (no filesystem pages needed)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    idx = EntityIndex(wiki)
    for i, name in enumerate(names):
        idx._by_name[name.lower()] = (f"uid-{i}", wiki / f"page-{i}.md")
    return idx


# Every junk name AND every legit name below appears as a whole word here.
_REPRESENTATIVE_CONTENT = (
    "Notes: here we get to the main reach, over in lane a. "
    "Met Ada at Acme Corp today; IBM and Reach were both mentioned."
)

_JUNK_NAMES = ["here", "get", "main", "reach", "lane a"]
_LEGIT_NAMES = ["ada", "acme corp", "ibm"]

# Issue athenaeum#1168 added a mention-density union gate downstream of this
# junk filter: a single-token key (like "ibm" or "ada") mentioned only once
# in a file is now suppressed unless it clears the density threshold. That
# gate is orthogonal to the junk-name filter this suite pins, so tests below
# that are specifically about junk-name filtering (not density) disable the
# density gate via ``mention_density_min_occurrences: 1`` -- any single
# occurrence then clears it, isolating the behavior under test.
_DENSITY_GATE_OFF = {"mention_density_min_occurrences": 1}


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------


def test_named_junk_matches_are_filtered(tmp_path: Path) -> None:
    index = _index_with_names(tmp_path, _JUNK_NAMES + _LEGIT_NAMES)
    raw = _make_raw(_REPRESENTATIVE_CONTENT)
    names = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    # None of the measured junk names survive to become a Tier-3 call.
    assert names.isdisjoint(set(_JUNK_NAMES))


def test_legitimate_short_name_entity_is_not_filtered(tmp_path: Path) -> None:
    """The load-bearing test — a real short entity must survive the filter.

    ``ibm`` (3 chars) and ``ada`` are legitimate and are NOT junk words, so they
    must still match. Fails if the filter is made too aggressive (e.g. a broad
    min-length floor that swallows short real names)."""
    index = _index_with_names(tmp_path, _JUNK_NAMES + _LEGIT_NAMES)
    raw = _make_raw(_REPRESENTATIVE_CONTENT)
    # Density gate off (athenaeum#1168): this test is about the junk filter,
    # not mention density, and "ibm"/"ada" are single-token, single-mention
    # in the fixture content.
    cfg = {"librarian": _DENSITY_GATE_OFF}
    names = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
    assert "ibm" in names
    assert "ada" in names
    assert "acme corp" in names


def test_before_after_call_count_on_representative_file(tmp_path: Path) -> None:
    """The measurement AC — before/after Tier-3-call count on a representative file.

    Each surviving Tier-1 match issues one Tier-3 merge call. With the filter
    OFF (everything allow-listed) all 8 names match; with the filter ON only the
    3 legitimate names remain — 5 junk calls removed, 0 legitimate calls lost.
    """
    index = _index_with_names(tmp_path, _JUNK_NAMES + _LEGIT_NAMES)
    raw = _make_raw(_REPRESENTATIVE_CONTENT)

    # Filter OFF: allow-list every name so the junk filter is a no-op. Density
    # gate also off (athenaeum#1168) -- this AC is about the junk filter.
    disable = {
        "librarian": {
            "junk_match_allowlist": _JUNK_NAMES + _LEGIT_NAMES,
            **_DENSITY_GATE_OFF,
        }
    }
    before = {n for n, _, _ in tier1_programmatic_match(raw, index, config=disable)}

    # Filter ON: built-in junk-name defaults, density gate off (this AC is
    # about the junk filter, not mention density).
    after_cfg = {"librarian": _DENSITY_GATE_OFF}
    after = {n for n, _, _ in tier1_programmatic_match(raw, index, config=after_cfg)}

    assert len(before) == 8  # 5 junk + 3 legit would each cost a Tier-3 call
    assert after == set(_LEGIT_NAMES)  # only the legit 3 survive
    assert before - after == set(_JUNK_NAMES)  # exactly the junk was dropped


def test_allowlist_unfilters_a_real_entity_named_like_junk(tmp_path: Path) -> None:
    """The escape hatch: a real company literally named "Reach" must be keepable.

    Without the allow-list ``reach`` is filtered (it's a default junk word); with
    it, the match survives. This is also the "verify it fails if the filter is
    made too aggressive" guard — remove the allow-list and the legit match dies.
    """
    index = _index_with_names(tmp_path, ["reach", "acme corp"])
    raw = _make_raw("Reach signed with Acme Corp.")

    default = {n for n, _, _ in tier1_programmatic_match(raw, index)}
    assert "reach" not in default  # filtered by default

    # Density gate off (athenaeum#1168): "reach" occurs once here and is
    # single-token, so the density gate would also suppress it -- this AC is
    # about the junk allow-list escape hatch, not mention density.
    cfg = {"librarian": {"junk_match_allowlist": ["Reach"], **_DENSITY_GATE_OFF}}
    kept = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
    assert "reach" in kept  # allow-listed back in (case-insensitively)


def test_operator_stopwords_extend_the_default(tmp_path: Path) -> None:
    index = _index_with_names(tmp_path, ["acme corp", "widget"])
    raw = _make_raw("Acme Corp shipped the widget.")
    cfg = {"librarian": {"junk_match_stopwords": ["widget"]}}
    names = {n for n, _, _ in tier1_programmatic_match(raw, index, config=cfg)}
    assert "widget" not in names  # operator-declared junk is filtered
    assert "acme corp" in names  # unrelated legit match unaffected


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveJunkMatchNames:
    def test_default_holds_the_measured_junk(self) -> None:
        base = resolve_junk_match_names(None)
        for junk in _JUNK_NAMES:
            assert junk in base
        assert base == {s.lower() for s in DEFAULT_JUNK_MATCH_STOPWORDS}

    def test_stopwords_extend_and_allowlist_removes(self) -> None:
        cfg = {
            "librarian": {
                "junk_match_stopwords": ["Acme", "widget"],
                "junk_match_allowlist": ["reach", "Acme"],
            }
        }
        eff = resolve_junk_match_names(cfg)
        assert "widget" in eff  # extended
        assert "acme" not in eff  # allow-list wins over stopwords
        assert "reach" not in eff  # allow-list wins over the default
        assert "here" in eff  # untouched default remains

    def test_malformed_config_falls_back_to_default(self) -> None:
        # Non-dict, wrong types, non-string members — never raises, ignores junk.
        for bad in (
            None,
            {},
            {"librarian": "nope"},
            {"librarian": {"junk_match_stopwords": "x"}},
            {"librarian": {"junk_match_stopwords": [1, 2, None]}},
        ):
            assert resolve_junk_match_names(bad) == {
                s.lower() for s in DEFAULT_JUNK_MATCH_STOPWORDS
            }
