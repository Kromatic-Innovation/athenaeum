# SPDX-License-Identifier: Apache-2.0
"""The relevance-bounded cap: derived ceiling, cap function, and overflow
breadcrumb (issue athenaeum#1783).

Two tests bind the ceiling value (never a typed literal in either):

* :func:`test_ceiling_equals_max_aggregation_expected_uids` parses
  ``tests/evals/data/corpus/probes/probes.yaml`` DIRECTLY (never through
  ``tests.evals.corpus``, which is an in-flight sibling lane's module) and
  asserts :data:`athenaeum.config.RECALL_CAP_CEILING_DEFAULT` equals
  ``max(len(expected_uids))`` over the probes whose ``probe_class`` is
  ``aggregation``.
* :func:`test_hook_fallback_literal_matches_derived_ceiling` parses
  ``examples/claude-code/user-prompt-recall.sh`` and asserts its own
  shell-level fallback literal equals the SAME value.

The rest of this module unit-tests :func:`athenaeum.search.apply_relevance_cap`
and :func:`athenaeum.config.resolve_recall_cap_ceiling` directly, then proves
the three case shapes (floor-only, ceiling-hit, empty) end to end through
:func:`athenaeum.mcp_server.recall_search` -- the hook-surface equivalents
live in ``tests/test_shell_hooks.py`` (``TestUserPromptRecall``'s
``test_cap_*`` methods), reusing that module's own hook-subprocess fixtures
rather than duplicating them here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from athenaeum.config import RECALL_CAP_CEILING_DEFAULT, resolve_recall_cap_ceiling
from athenaeum.mcp_server import recall_search
from athenaeum.search import FTS5Backend, apply_relevance_cap

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBES_YAML = REPO_ROOT / "tests" / "evals" / "data" / "corpus" / "probes" / "probes.yaml"
USER_PROMPT_HOOK = REPO_ROOT / "examples" / "claude-code" / "user-prompt-recall.sh"


# ---------------------------------------------------------------------------
# Derivation tests -- the ceiling must never appear as a typed literal below.
# ---------------------------------------------------------------------------


def test_ceiling_equals_max_aggregation_expected_uids() -> None:
    """AC: the default ceiling is derived from the aggregation class's own
    answer-set sizes, read directly off the probes fixture -- not through
    ``tests.evals.corpus`` (an in-flight sibling lane's module, out of this
    issue's scope), and not typed as a literal here.
    """
    probes = yaml.safe_load(PROBES_YAML.read_text(encoding="utf-8"))
    aggregation_sizes = [
        len(probe["expected_uids"])
        for probe in probes
        if probe.get("probe_class") == "aggregation"
    ]
    assert aggregation_sizes, "no aggregation probes found -- fixture may have moved"
    derived_ceiling = max(aggregation_sizes)

    assert RECALL_CAP_CEILING_DEFAULT == derived_ceiling, (
        f"RECALL_CAP_CEILING_DEFAULT ({RECALL_CAP_CEILING_DEFAULT}) must equal "
        f"max(len(expected_uids)) over aggregation probes ({derived_ceiling}, "
        f"from sizes {sorted(aggregation_sizes)})"
    )
    assert resolve_recall_cap_ceiling(None) == derived_ceiling


def test_hook_fallback_literal_matches_derived_ceiling() -> None:
    """AC: the hook's own shell-level fallback literal for ``CEILING``
    equals the SAME derived value -- parsed out of the shipped hook script,
    never hand-typed here either.
    """
    text = USER_PROMPT_HOOK.read_text(encoding="utf-8")
    match = re.search(r"RECALL_CAP_CEILING:-(\d+)\}\}", text)
    assert match, (
        "could not find the hook's CEILING env>yaml-cache>default fallback "
        "literal (expected a `${RECALL_CAP_CEILING:-<digits>}}` pattern) -- "
        "the hook may have been refactored; update this regex to match"
    )
    hook_literal = int(match.group(1))
    assert hook_literal == resolve_recall_cap_ceiling(None), (
        f"the hook's fallback literal ({hook_literal}) has drifted from the "
        f"derived ceiling ({resolve_recall_cap_ceiling(None)})"
    )

    # Belt-and-braces: the `case` guard's own fallback-on-malformed-value
    # literal must agree too (`''|*[!0-9]*) CEILING=7 ;;`).
    case_matches = set(re.findall(r"CEILING=(\d+)\s*;;", text))
    assert case_matches == {str(hook_literal)}, (
        f"the hook's `case` fallback literal(s) {case_matches} must all equal "
        f"the same derived ceiling ({hook_literal})"
    )


# ---------------------------------------------------------------------------
# athenaeum.search.apply_relevance_cap
# ---------------------------------------------------------------------------


def test_apply_relevance_cap_keeps_at_most_limit_in_order() -> None:
    hits = [(f"p{i}.md", f"Page {i}", float(-i)) for i in range(10)]
    kept, withheld = apply_relevance_cap(hits, 4)
    assert kept == hits[:4]
    assert sum(withheld.values()) == 6


def test_apply_relevance_cap_never_re_ranks() -> None:
    """Deliberately NOT sorted by score -- a caller's own ordering must
    survive untouched; this function only slices."""
    hits = [("a.md", "A", -1.0), ("b.md", "B", -9.0), ("c.md", "C", -3.0)]
    kept, _ = apply_relevance_cap(hits, 2)
    assert kept == hits[:2]


def test_apply_relevance_cap_fewer_than_limit_returns_all_no_withheld() -> None:
    hits = [("a.md", "A", -1.0), ("b.md", "B", -2.0)]
    kept, withheld = apply_relevance_cap(hits, 5)
    assert kept == hits
    assert withheld == {}


def test_apply_relevance_cap_untyped_hit_counts_as_page() -> None:
    hits = [(f"p{i}.md", f"Page {i}", 0.0) for i in range(3)]
    _, withheld = apply_relevance_cap(hits, 0)
    assert withheld == {"page": 3}


def test_apply_relevance_cap_type_of_only_called_for_withheld_hits() -> None:
    calls: list[str] = []

    def type_of(hit: tuple[str, str, float]) -> str:
        calls.append(hit[0])
        return "widget"

    hits = [(f"p{i}.md", f"Page {i}", 0.0) for i in range(5)]
    kept, withheld = apply_relevance_cap(hits, 2, type_of=type_of)
    assert kept == hits[:2]
    assert calls == ["p2.md", "p3.md", "p4.md"], (
        "type_of must be called ONLY for hits past the limit, in order, "
        f"never for a kept hit -- got {calls}"
    )
    assert withheld == {"widget": 3}


def test_apply_relevance_cap_groups_multiple_types() -> None:
    hits = [
        ("p1.md", "P1", 0.0),
        ("p2.md", "P2", 0.0),
        ("p3.md", "P3", 0.0),
        ("p4.md", "P4", 0.0),
    ]
    types = {"p1.md": "person", "p2.md": "person", "p3.md": "project", "p4.md": None}
    _, withheld = apply_relevance_cap(hits, 0, type_of=lambda h: types[h[0]])
    assert withheld == {"person": 2, "project": 1, "page": 1}


# ---------------------------------------------------------------------------
# athenaeum.config.resolve_recall_cap_ceiling
# ---------------------------------------------------------------------------


def test_resolve_recall_cap_ceiling_default_with_no_config() -> None:
    assert resolve_recall_cap_ceiling(None) == RECALL_CAP_CEILING_DEFAULT


def test_resolve_recall_cap_ceiling_yaml_overrides_default() -> None:
    config = {"recall": {"cap": {"ceiling": 12}}}
    assert resolve_recall_cap_ceiling(config) == 12


def test_resolve_recall_cap_ceiling_env_overrides_yaml(monkeypatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "3")
    config = {"recall": {"cap": {"ceiling": 12}}}
    assert resolve_recall_cap_ceiling(config) == 3


def test_resolve_recall_cap_ceiling_malformed_env_falls_through(monkeypatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "not-a-number")
    assert resolve_recall_cap_ceiling(None) == RECALL_CAP_CEILING_DEFAULT


def test_resolve_recall_cap_ceiling_non_positive_yaml_falls_through() -> None:
    config = {"recall": {"cap": {"ceiling": 0}}}
    assert resolve_recall_cap_ceiling(config) == RECALL_CAP_CEILING_DEFAULT
    config = {"recall": {"cap": {"ceiling": -5}}}
    assert resolve_recall_cap_ceiling(config) == RECALL_CAP_CEILING_DEFAULT


# ---------------------------------------------------------------------------
# MCP surface: floor-only / ceiling-hit / empty, plus overflow line shape.
# ---------------------------------------------------------------------------


def _write_page(wiki: Path, filename: str, *, name: str, page_type: str, body: str) -> None:
    (wiki / filename).write_text(
        f"---\nname: {name}\ntype: {page_type}\ntags: [ceilingprobe]\n---\n\n{body}\n"
    )


def test_mcp_floor_only_all_hits_emitted_no_overflow(tmp_path: Path) -> None:
    """floor-only: fewer hits than the limit clear the floor. All are
    emitted, with no overflow line."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_page(wiki, "a.md", name="Page A", page_type="person", body="ceilingprobe alpha")
    _write_page(wiki, "b.md", name="Page B", page_type="person", body="ceilingprobe beta")
    cache = tmp_path / "cache"
    FTS5Backend().build_index(wiki, cache)

    text = recall_search(wiki, "ceilingprobe", top_k=5, search_backend="fts5", cache_dir=cache)
    assert "Page A" in text
    assert "Page B" in text
    assert "relevance cap" not in text


def test_mcp_ceiling_hit_caps_at_top_k_and_adds_overflow_line(tmp_path: Path) -> None:
    """ceiling-hit: more hits clear the floor than the limit (here,
    ``top_k``). Exactly ``limit`` are emitted, plus one overflow line whose
    count and types match the withheld hits."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    total = 6
    for i in range(total):
        _write_page(
            wiki, f"p{i}.md", name=f"Page {i}", page_type="person", body="ceilingprobe"
        )
    cache = tmp_path / "cache"
    FTS5Backend().build_index(wiki, cache)

    top_k = 3
    text = recall_search(
        wiki, "ceilingprobe", top_k=top_k, search_backend="fts5", cache_dir=cache
    )
    pushed = sum(1 for i in range(total) if f"Page {i}" in text)
    assert pushed == top_k, f"expected exactly {top_k} pushed hits, got {pushed}:\n{text}"
    withheld = total - top_k
    assert str(withheld) in text
    assert "person" in text
    assert "relevance cap" in text
    overflow_lines = [line for line in text.splitlines() if "relevance cap" in line]
    assert len(overflow_lines) == 1, f"expected exactly one overflow line, got {overflow_lines}"
    assert not overflow_lines[0].startswith("-"), overflow_lines[0]


def test_mcp_empty_no_hits_no_overflow_line(tmp_path: Path) -> None:
    """empty: nothing clears the floor. MCP returns its 'No wiki pages
    matched' text, and no overflow line."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_page(wiki, "a.md", name="Page A", page_type="person", body="ceilingprobe")
    cache = tmp_path / "cache"
    FTS5Backend().build_index(wiki, cache)

    config = {"recall": {"relevance_floor": {"fts5": -999.0}}}
    text = recall_search(
        wiki, "ceilingprobe", top_k=5, search_backend="fts5", cache_dir=cache, config=config
    )
    assert "No wiki pages matched" in text
    assert "relevance cap" not in text


def test_mcp_overflow_line_marks_lower_bound_when_window_exhausted(tmp_path: Path) -> None:
    """When the fetch window itself was exhausted (more raw candidates
    exist than were even fetched), the withheld count is a LOWER BOUND
    ('at least N'), not an exact figure."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # More than the window width (max(top_k, 15) == 15 here) so the
    # initial fetch itself is exhausted, not just the post-cap tail.
    total = 20
    for i in range(total):
        _write_page(
            wiki, f"p{i}.md", name=f"Page {i}", page_type="person", body="ceilingprobe"
        )
    cache = tmp_path / "cache"
    FTS5Backend().build_index(wiki, cache)

    text = recall_search(wiki, "ceilingprobe", top_k=3, search_backend="fts5", cache_dir=cache)
    assert "at least" in text, f"expected a lower-bound overflow line:\n{text}"
