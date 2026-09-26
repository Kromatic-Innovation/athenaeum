# SPDX-License-Identifier: Apache-2.0
"""The per-turn overflow breadcrumb, pinned at the core (issue athenaeum#1905).

athenaeum#1783 gave two surfaces the "N more matching results were withheld
by the relevance cap — call ``recall``" notice: the MCP ``recall`` renderer
(Python) and the shell ``UserPromptSubmit`` hook (awk). athenaeum#1361/#1887
then made :mod:`athenaeum.claude_code_adapter` the shipped hook, the awk half
stopped running, and the notice silently disappeared from the per-turn push
path — athenaeum#1894 measured the cost as ``push_breadcrumb_pull`` falling
from 80.0% to 70.5%.

"Silently" is the operative word, and it is what this module exists to stop.
Every assertion here is against :func:`athenaeum.context.build_context` (the
core both the adapter and ``athenaeum context`` call), runs in-process with
no subprocess, no corpus materialization and no network, and so runs in
CI's default selection on every change — the adapter-vs-shell comparison in
``tests/evals/test_adapter_overflow_breadcrumb_1905.py`` is the other half of
the pin, and a much slower one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from athenaeum.context import CANDIDATE_WINDOW, build_context
from athenaeum.recall_overflow import render_overflow_line

#: Every fixture page matches this, so a query for it sweeps the whole index
#: and the cap is the only thing deciding what gets rendered.
QUERY = "widget"


def _build_index(path: Path, types: list[str], *, with_type_column: bool = True) -> Path:
    """One FTS5 page per entry in *types*, all matching :data:`QUERY`.

    Page *i* carries ``types[i]`` as its entity type, so a test states the
    withheld breakdown it expects by construction rather than by guessing
    what a real corpus happens to contain. *with_type_column* omits the
    ``type`` column entirely, standing in for an index built by an
    athenaeum old enough to predate it.
    """
    cols = ["filename", "name", "tags", "aliases", "description", "audience UNINDEXED"]
    insert_cols = ["filename", "name", "tags", "aliases", "description", "audience"]
    if with_type_column:
        cols.append("type UNINDEXED")
        insert_cols.append("type")
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE VIRTUAL TABLE wiki USING fts5({', '.join(cols)}, "
        'tokenize="porter unicode61")'
    )
    rows = []
    for i, page_type in enumerate(types):
        row = [f"page-{i:02d}.md", f"Widget Page {i:02d}", QUERY, "", "", "|__access_open__|"]
        if with_type_column:
            row.append(page_type)
        rows.append(tuple(row))
    conn.executemany(
        f"INSERT INTO wiki ({', '.join(insert_cols)}) "
        f"VALUES ({','.join('?' for _ in insert_cols)})",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _render(
    cache_dir: Path,
    types: list[str],
    *,
    n: int = 3,
    budget: int | None = None,
    with_type_column: bool = True,
) -> str:
    """Build a one-off index under *cache_dir* and return the envelope's
    rendered text — the exact string the packaged adapter appends to the
    preamble and injects."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    _build_index(cache_dir / "wiki-index.db", types, with_type_column=with_type_column)
    envelope = build_context(
        QUERY, "sess", cache_dir=cache_dir, n=n, budget=budget, use_llm=False
    )
    return envelope["render"]["text"]


def _overflow_line(text: str) -> str:
    """The breadcrumb line, or ``""``. It is the only line that is not a
    ``  - `` bullet, by the template's own construction (see
    :func:`athenaeum.recall_overflow.render_overflow_line`)."""
    tail = [line for line in text.split("\n") if line and not line.startswith("  - ")]
    return tail[-1] if tail else ""


class TestNothingWithheld:
    """Counter-example: a notice that fires when nothing was actually
    withheld would train the model to call ``recall`` on every turn, which
    is the opposite of a relevance signal."""

    def test_fewer_candidates_than_the_ceiling_renders_no_notice(self, tmp_path: Path) -> None:
        text = _render(tmp_path, ["note", "note"], n=7)
        assert text.splitlines() == ["  - Widget Page 00", "  - Widget Page 01"]
        assert _overflow_line(text) == ""

    def test_exactly_the_ceiling_renders_no_notice(self, tmp_path: Path) -> None:
        text = _render(tmp_path, ["note"] * 3, n=3)
        assert len(text.splitlines()) == 3
        assert _overflow_line(text) == ""

    def test_no_rendered_bullets_stays_silent_even_with_withheld_candidates(
        self, tmp_path: Path
    ) -> None:
        """The shell hook's own "empty" behaviour: ``exit 0``, no output at
        all. A bare overflow line with nothing to overflow FROM would be an
        injected context block that says only "there is more" — worse than
        silence, because the adapter's fail-safe contract treats an empty
        render as "inject nothing"."""
        text = _render(tmp_path, ["note"] * 10, n=7, budget=1)
        assert text == ""


class TestWithheldTally:
    def test_cap_withheld_are_counted_and_broken_down_by_type(self, tmp_path: Path) -> None:
        types = ["note"] * 3 + ["person", "person", "policy"]
        text = _render(tmp_path, types, n=3)
        assert len(text.splitlines()) == 4  # three bullets plus the notice
        assert _overflow_line(text) == render_overflow_line(
            {"person": 2, "policy": 1}, at_least=False
        )

    def test_the_count_is_a_lower_bound_once_the_fetch_window_is_exhausted(
        self, tmp_path: Path
    ) -> None:
        """Past ``CANDIDATE_WINDOW`` deduped candidates there may be more the
        turn never fetched, so ``N`` alone would understate — the same
        ``at least`` rule the shell hook applies."""
        text = _render(tmp_path, ["note"] * (CANDIDATE_WINDOW + 5), n=3)
        line = _overflow_line(text)
        assert line.startswith("memory has at least ")
        assert line == render_overflow_line({"note": CANDIDATE_WINDOW - 3}, at_least=True)

    def test_a_budget_drop_is_counted_not_silent(self, tmp_path: Path) -> None:
        """Budget-withheld and cap-withheld candidates share ONE tally: the
        breadcrumb says how many results the caller is not seeing, never why
        each one was dropped."""
        no_budget_pressure = _render(tmp_path / "a", ["note"] * 5, n=3)
        assert _overflow_line(no_budget_pressure) == render_overflow_line(
            {"note": 2}, at_least=False
        )

        # 12 tokens buys the preamble (~24) nothing, so pick a budget that
        # admits the first bullet and refuses the rest.
        squeezed = _render(tmp_path / "b", ["note"] * 5, n=3, budget=30)
        assert len(squeezed.splitlines()) == 2
        assert _overflow_line(squeezed) == render_overflow_line({"note": 4}, at_least=False)

    def test_an_index_without_a_type_column_folds_everything_to_page(
        self, tmp_path: Path
    ) -> None:
        """Legacy-DB safety, the same shape ``description`` already has: a
        coarser breakdown, never a crashed turn and never a missing notice.
        ``page`` is the convention
        :func:`athenaeum.search.apply_relevance_cap` documents for a hit with
        no resolvable type."""
        text = _render(tmp_path, ["note"] * 6, n=3, with_type_column=False)
        assert _overflow_line(text) == render_overflow_line({"page": 3}, at_least=False)


class TestSingleSourcedWording:
    def test_the_line_comes_from_the_packaged_template_not_a_literal(
        self, tmp_path: Path
    ) -> None:
        """The whole point of athenaeum#1905: this surface and the MCP
        ``recall`` surface must render the SAME sentence from the SAME file,
        so neither can drift the way the awk copy did."""
        from athenaeum.mcp_server import _render_recall_overflow_line

        text = _render(tmp_path, ["note"] * 3 + ["project"] * 2, n=3)
        assert _overflow_line(text) == _render_recall_overflow_line(
            {"project": 2}, at_least=False
        )

    def test_the_notice_is_never_mistakable_for_a_bullet(self, tmp_path: Path) -> None:
        """``tests/evals/test_recall_covers_grep.py`` counts hook bullets by
        their ``  - `` prefix; a notice that acquired one would inflate every
        breadcrumb measurement by exactly one phantom page."""
        text = _render(tmp_path, ["note"] * 6, n=3)
        assert not _overflow_line(text).startswith("-")
        assert len([ln for ln in text.splitlines() if ln.startswith("  - ")]) == 3
