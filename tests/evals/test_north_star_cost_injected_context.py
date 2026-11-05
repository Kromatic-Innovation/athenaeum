# SPDX-License-Identifier: Apache-2.0
"""Cost-per-correct counts recurring injected context (issue athenaeum#2305).

Part of athenaeum#2295. The cost-per-correct section (``compute_cost_per_correct``
/ ``render_cost_per_correct_table`` in ``tests/evals/north_star_report.py``)
summed only ``turn_tokens`` (plain input + output) across a cell's rows. That
undercounts a PUSH/ORACLE arm whose injected content stays resident in the
model's context across a multi-turn rollout: once the provider caches that
content, a later turn's own ``input_tokens`` excludes it (the API reports
cached bytes separately), so the old sum silently priced every later turn's
re-use of the SAME injected content at zero -- "a per-turn view favors
content" is the issue's own name for this. Fixed here by folding
``cache_creation_input_tokens``/``cache_read_input_tokens`` (now recorded per
turn on :class:`~tests.evals.rollout.TurnTokenUsage`, additively -- see that
class's own docstring) into the cell's token total, weighted the same way
:mod:`athenaeum.models` already bills cache activity (athenaeum#239: a cache
WRITE costs 1.25x a plain input token, a cache READ costs 0.10x) so a cached
turn and an uncached turn of the same raw size price differently, never the
same.

Also adds ``injected_bytes`` (the literal UTF-8 byte length of the content a
PUSH/ORACLE arm actually injected, summed per cell) alongside cost -- a
separate, additive :class:`~tests.evals.rollout.RolloutRecord` field
(``injected_context_bytes``), reported but never folded into the cost
arithmetic itself.

Both new fields are ``None`` -- rendered ``n/a``, never ``0`` -- for any row
that predates this issue (see :class:`~tests.evals.rollout.TurnTokenUsage` and
:class:`~tests.evals.rollout.RolloutRecord`'s own back-compat decode), and
:func:`tests.evals.north_star_report.render_cost_per_correct_table` omits
both new columns entirely when no row in the table carries the data --
exactly the existing convention the Phase 2 write-cost columns already use
(see ``test_north_star_verdicts.py``'s
``test_render_cost_per_correct_table_omits_write_columns_when_phase1_only``).

``test_cost_per_correct_section_byte_identical_for_legacy_rows`` is the
regression guard for that last claim: its golden fixture,
``tests/evals/data/north_star_report/cost_per_correct_section_legacy_golden.md``,
was captured by running this file's own ``_legacy_rows()`` against athenaeum
commit ``7c6a1577`` (the commit this issue's branch forked from, before any
of the changes above existed) -- so the test is a real regression guard, not
a tautology written against the already-changed code.
"""

from __future__ import annotations

from pathlib import Path

from tests.evals.containment import GridCell
from tests.evals.corpus import build_corpus
from tests.evals.north_star_report import (
    _CACHE_READ_TOKEN_WEIGHT,
    _CACHE_WRITE_TOKEN_WEIGHT,
    RolloutRow,
    build_report,
    compute_cost_per_correct,
    render_cost_per_correct_table,
    render_report,
)
from tests.evals.rollout import Arm, RolloutRecord, TurnTokenUsage

CORPUS_SCALE = "core"
_CORPUS = build_corpus(scale=CORPUS_SCALE)

# Real core probe/marker (tests/evals/data/corpus/probes/probes.yaml), reused
# verbatim from test_north_star_verdicts.py's own OTHER_PROBE_ID/
# OTHER_ANSWER_MARKER so grade_correctness has real ground truth to grade
# against rather than an invented stand-in.
PROBE_ID = "pto_allowance"
PROBE_CLASS = "single_hop"
ANSWER_MARKER = "25 days per year"

_FIXTURE_DIR = Path(__file__).parent / "data" / "north_star_report"
_LEGACY_GOLDEN_PATH = _FIXTURE_DIR / "cost_per_correct_section_legacy_golden.md"

_COST_SECTION_HEADER = "## Cost per correct answer (athenaeum#1734)"


def _row(record: RolloutRecord, *, replicate: int = 0) -> RolloutRow:
    cell = GridCell(
        probe=record.probe_id,
        arm=record.arm.value,
        corpus_scale=record.corpus_scale,
        replicate=replicate,
    )
    return RolloutRow(cell=cell, record=record)


def _legacy_rows() -> list[RolloutRow]:
    """Two rows using ONLY fields that existed before athenaeum#2305: one
    ORACLE cell that grades correct (a defined ``cost_per_correct``), one
    PULL cell that grades wrong (the zero-correct/``undefined`` branch this
    issue must leave untouched). Neither sets ``injected_context_bytes`` nor
    either ``TurnTokenUsage`` cache field -- both are additive fields this
    issue introduces, so a row built before they existed cannot set them,
    which is exactly the "legacy-shaped input" this module's byte-identical
    regression guard needs.
    """
    oracle = RolloutRecord(
        arm=Arm.ORACLE,
        probe_id=PROBE_ID,
        probe_class=PROBE_CLASS,
        corpus_scale=CORPUS_SCALE,
        answer=f"The PTO allowance is {ANSWER_MARKER}.",
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)],
        turn_count=1,
        transcript=[],
    )
    pull_wrong = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PROBE_ID,
        probe_class=PROBE_CLASS,
        corpus_scale=CORPUS_SCALE,
        answer="I'm not sure.",
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=50, output_tokens=10)],
        turn_count=1,
        transcript=[],
    )
    return [_row(oracle), _row(pull_wrong)]


def _cost_section(text: str) -> str:
    """The cost-per-correct section's own text, isolated from the rest of
    *text* -- including the report header's non-deterministic provenance
    stamp (git SHA, generation timestamp) -- by slicing between this
    section's heading and the next ``## `` heading, mirroring
    ``test_north_star_report.py``'s own
    ``test_correctness_section_is_byte_identical_after_harm_and_coverage_additions``
    convention for exactly this reason."""
    start = text.index(_COST_SECTION_HEADER)
    rest = text[start + len(_COST_SECTION_HEADER) :]
    next_heading = rest.index("\n## ")
    return text[start : start + len(_COST_SECTION_HEADER) + next_heading]


def test_cost_per_correct_section_byte_identical_for_legacy_rows() -> None:
    """athenaeum#2305 AC: the cost-per-correct section renders BYTE FOR BYTE
    identically to the pre-issue code when the input rows carry none of the
    new additive fields -- see this module's docstring for how the golden
    fixture was captured."""
    report = build_report(_legacy_rows())
    text = render_report(report)
    section = _cost_section(text)
    golden = _LEGACY_GOLDEN_PATH.read_text(encoding="utf-8")
    assert section == golden


# ---------------------------------------------------------------------------
# Multi-turn recurring injected context + cache-weighted pricing
# ---------------------------------------------------------------------------

#: An ORACLE record with its planted marker said correctly, so every cell
#: built from it has correct_n == 1 and a DEFINED cost_per_correct -- the
#: denominator every assertion below divides by.
_CORRECT_ANSWER = f"The PTO allowance is {ANSWER_MARKER}."


def _oracle_row(turn_tokens: list[TurnTokenUsage], **kwargs: object) -> RolloutRow:
    record = RolloutRecord(
        arm=Arm.ORACLE,
        probe_id=PROBE_ID,
        probe_class=PROBE_CLASS,
        corpus_scale=CORPUS_SCALE,
        answer=_CORRECT_ANSWER,
        turn_tokens=turn_tokens,
        turn_count=len(turn_tokens),
        transcript=[],
        **kwargs,  # type: ignore[arg-type]
    )
    return _row(record)


def test_multi_turn_injected_context_recurs_in_cache_weighted_cost() -> None:
    """athenaeum#2305 AC: a multi-turn rollout where the SAME injected
    content stays cached across later turns must have its cache charge
    counted on EVERY turn it recurs on, not only the turn that first wrote
    it to cache -- this is the literal fix for "a per-turn view favors
    content".

    Turn 1 writes 1000 tokens of injected content to cache
    (``cache_creation_input_tokens``); turns 2 and 3 re-read the SAME 1000
    tokens from cache (``cache_read_input_tokens``) rather than re-paying
    the write price -- the shape a multi-turn PUSH/ORACLE rollout takes
    once the provider caches the injected context.
    """
    rows = [
        _oracle_row(
            [
                TurnTokenUsage(
                    turn=1, input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000
                ),
                TurnTokenUsage(
                    turn=2, input_tokens=0, output_tokens=0, cache_read_input_tokens=1000
                ),
                TurnTokenUsage(
                    turn=3, input_tokens=0, output_tokens=0, cache_read_input_tokens=1000
                ),
            ]
        )
    ]
    costs = compute_cost_per_correct(rows)
    assert len(costs) == 1
    cell = costs[0]
    assert cell.correct_n == 1

    first_turn_only = 1000 * _CACHE_WRITE_TOKEN_WEIGHT
    every_turn = 1000 * _CACHE_WRITE_TOKEN_WEIGHT + 2 * (1000 * _CACHE_READ_TOKEN_WEIGHT)

    # Property 1: the recurring charge is counted -- summing all three
    # turns gives a LARGER total than counting only the first turn's
    # cache-creation charge would.
    assert cell.cache_weighted_tokens == every_turn
    assert cell.cache_weighted_tokens > first_turn_only
    assert cell.cost_per_correct == every_turn  # correct_n == 1, no other tokens


def test_cached_and_uncached_turns_price_differently() -> None:
    """athenaeum#2305 AC: a cached turn and an uncached turn carrying the
    SAME raw token count must price differently -- the whole point of
    folding cache usage in at athenaeum#239's multipliers rather than
    counting it 1:1 with a plain input token."""
    uncached_rows = [
        _oracle_row([TurnTokenUsage(turn=1, input_tokens=1000, output_tokens=0)])
    ]
    cached_rows = [
        _oracle_row(
            [
                TurnTokenUsage(
                    turn=1, input_tokens=0, output_tokens=0, cache_read_input_tokens=1000
                )
            ]
        )
    ]
    uncached_cost = compute_cost_per_correct(uncached_rows)[0]
    cached_cost = compute_cost_per_correct(cached_rows)[0]

    assert uncached_cost.correct_n == cached_cost.correct_n == 1
    # Property 2: same raw count (1000), different price -- a cache READ is
    # weighted at 0.10x a plain input token, strictly cheaper.
    assert cached_cost.cost_per_correct == 1000 * _CACHE_READ_TOKEN_WEIGHT
    assert uncached_cost.cost_per_correct == 1000
    assert cached_cost.cost_per_correct < uncached_cost.cost_per_correct


def test_cache_weighted_tokens_undefined_not_zero_when_uncaptured() -> None:
    """A cell whose rows never captured cache usage (every
    ``TurnTokenUsage`` leaves both cache fields at their ``None`` default)
    reports ``cache_weighted_tokens is None`` -- undefined, never a
    fabricated zero -- and `cost_per_correct` is unaffected, matching the
    pre-athenaeum#2305 formula exactly."""
    rows = [_oracle_row([TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)])]
    cell = compute_cost_per_correct(rows)[0]
    assert cell.cache_weighted_tokens is None
    assert cell.cost_per_correct == 100.0


def test_zero_correct_cell_stays_undefined_even_with_cache_data_present() -> None:
    """The existing "no correct answers -> no defined cost" handling
    (issue athenaeum#1734 AC2) must be unchanged by athenaeum#2305: cache
    activity being present does not manufacture a defined cost_per_correct
    out of a cell with zero correct answers."""
    record = RolloutRecord(
        arm=Arm.ORACLE,
        probe_id=PROBE_ID,
        probe_class=PROBE_CLASS,
        corpus_scale=CORPUS_SCALE,
        answer="wrong answer, no marker",
        turn_tokens=[
            TurnTokenUsage(
                turn=1, input_tokens=0, output_tokens=0, cache_creation_input_tokens=500
            )
        ],
        turn_count=1,
        transcript=[],
    )
    cell = compute_cost_per_correct([_row(record)])[0]
    assert cell.correct_n == 0
    assert cell.cost_per_correct is None
    assert cell.undefined_reason == "zero correct answers in this cell"
    # The cache contribution is still computed and reported -- only
    # cost_per_correct itself stays undefined.
    assert cell.cache_weighted_tokens == 500 * _CACHE_WRITE_TOKEN_WEIGHT


# ---------------------------------------------------------------------------
# injected_bytes per arm, alongside cost
# ---------------------------------------------------------------------------


def test_injected_bytes_total_summed_per_cell() -> None:
    rows = [
        _oracle_row(
            [TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)],
            injected_context_bytes=300,
        ),
        _oracle_row(
            [TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)],
            injected_context_bytes=200,
        ),
    ]
    cell = compute_cost_per_correct(rows)[0]
    assert cell.injected_bytes_total == 500


def test_injected_bytes_total_undefined_not_zero_when_uncaptured() -> None:
    rows = [_oracle_row([TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)])]
    cell = compute_cost_per_correct(rows)[0]
    assert cell.injected_bytes_total is None


def test_render_cost_per_correct_table_shows_cache_and_bytes_columns_when_present() -> None:
    rows = [
        _oracle_row(
            [
                TurnTokenUsage(
                    turn=1, input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000
                )
            ],
            injected_context_bytes=4096,
        )
    ]
    costs = compute_cost_per_correct(rows)
    rendered = "\n".join(render_cost_per_correct_table(costs))
    assert "cache_weighted_tokens" in rendered
    assert "injected_bytes" in rendered
    assert f"{1000 * _CACHE_WRITE_TOKEN_WEIGHT:.1f}" in rendered
    assert "4096" in rendered


def test_render_cost_per_correct_table_omits_cache_and_bytes_columns_when_absent() -> None:
    """Mirrors ``test_north_star_verdicts.py``'s own
    ``test_render_cost_per_correct_table_omits_write_columns_when_phase1_only``
    for the two new columns: absent input data means absent columns, never
    a column full of ``n/a``."""
    rows = [_oracle_row([TurnTokenUsage(turn=1, input_tokens=80, output_tokens=20)])]
    costs = compute_cost_per_correct(rows)
    rendered = "\n".join(render_cost_per_correct_table(costs))
    assert "cache_weighted_tokens" not in rendered
    assert "injected_bytes" not in rendered
