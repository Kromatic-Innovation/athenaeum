# SPDX-License-Identifier: Apache-2.0
"""Intake scheduling cannot starve a source (issue athenaeum#1291).

Before this issue, ``discover_raw_files`` returned its result grouped by source
directory in ``sorted()`` order and the run loop filled its ``max_files`` window
by head-truncating that list. Because the list was ordered by source NAME and
cut from the HEAD, the window could only ever advance past a source once that
source's own backlog fell below the cap — so a large, alphabetically-early,
continuously-refilled source starved every lexicographically later source
indefinitely. On the deployment that surfaced it, ~2,200 records sat frozen in
``raw/mural-board-summary/`` across at least 8 consecutive runs while
``raw/auto-memory/`` alone exceeded the entire per-run budget.

These pin all four acceptance criteria:

* **AC1** — a source with pending intake cannot be excluded for unbounded
  consecutive runs regardless of sort position or another source's backlog.
* **AC2** — the end-to-end case: a large, alphabetically-early, continuously
  refilled source, and a later source that still receives slots.
* **AC3** — a source starved for K consecutive runs is NAMED in the run summary.
* **AC4** — ``max_files`` semantics are preserved: this changes WHICH files fill
  the window, never how many.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.intake import round_robin_by_source
from athenaeum.librarian import _render_run_summary, run
from athenaeum.models import RawFile
from athenaeum.run_summary_log import (
    STALL_FIELD,
    STALL_STREAK_THRESHOLD,
    STARVATION_FIELD,
    STARVATION_STREAK_THRESHOLD,
    combined_starvation_priority,
    parse_run_summary_line,
    previous_starved_sources,
    read_combined_starvation_priority,
    read_run_summary_ledger,
    read_stall_streaks,
    read_starvation_priority,
    read_starvation_streaks,
    stall_priority,
    stall_streaks,
    stalled_sources_in_record,
    starvation_priority,
    starvation_streaks,
    starved_sources_in_record,
)


def _raw(source: str, name: str) -> RawFile:
    return RawFile(
        path=Path("/nonexistent") / source / name,
        source=source,
        timestamp="20240410T120000Z",
        uuid8="aabbccdd",
    )


# ---------------------------------------------------------------------------
# The scheduler itself (AC1, AC4)
# ---------------------------------------------------------------------------


class TestRoundRobinBySource:
    def test_a_later_source_is_reached_despite_an_early_sources_backlog(self) -> None:
        # THE defect, at the unit level: `auto-memory` alone exceeds the window,
        # so head-truncation gives `mural` nothing forever.
        files = [_raw("auto-memory", f"a{i}.md") for i in range(80)]
        files += [_raw("mural-board-summary", f"m{i}.md") for i in range(2126)]

        window = round_robin_by_source(files, 50)

        assert len(window) == 50
        assert {f.source for f in window} == {"auto-memory", "mural-board-summary"}
        # An even split, because both queues are deeper than their share.
        assert sum(1 for f in window if f.source == "mural-board-summary") == 25

    def test_within_source_ordering_is_preserved(self) -> None:
        files = [_raw("alpha", f"a{i}.md") for i in range(5)]
        files += [_raw("beta", f"b{i}.md") for i in range(5)]

        window = round_robin_by_source(files, 6)

        assert [f.path.name for f in window if f.source == "alpha"] == [
            "a0.md",
            "a1.md",
            "a2.md",
        ]
        assert [f.path.name for f in window if f.source == "beta"] == [
            "b0.md",
            "b1.md",
            "b2.md",
        ]

    def test_window_is_interleaved_so_a_mid_window_stop_touched_every_source(
        self,
    ) -> None:
        # A run that trips its wall-clock deadline part-way through the window
        # must still have touched every source, not only the earliest ones.
        files = [_raw("alpha", f"a{i}.md") for i in range(5)]
        files += [_raw("beta", f"b{i}.md") for i in range(5)]

        window = round_robin_by_source(files, 4)

        assert [f.source for f in window] == ["alpha", "beta", "alpha", "beta"]

    def test_a_shallow_source_does_not_forfeit_the_remainder_to_nobody(self) -> None:
        # `beta` has only one file; the rest of the window still fills from
        # `alpha` rather than being left short.
        files = [_raw("alpha", f"a{i}.md") for i in range(10)]
        files += [_raw("beta", "b0.md")]

        window = round_robin_by_source(files, 5)

        assert len(window) == 5
        assert sum(1 for f in window if f.source == "beta") == 1

    def test_budget_semantics_are_preserved(self) -> None:
        # AC4: exactly `min(len(files), limit)` slots, never more, never fewer.
        files = [_raw(f"s{i % 7}", f"f{i}.md") for i in range(200)]
        for limit in (0, 1, 7, 50, 199, 200, 500):
            assert len(round_robin_by_source(files, limit)) == min(len(files), limit)

    def test_a_non_positive_limit_yields_an_empty_window(self) -> None:
        files = [_raw("alpha", "a0.md")]
        assert round_robin_by_source(files, 0) == []
        assert round_robin_by_source(files, -3) == []

    def test_an_under_cap_corpus_keeps_discovery_order_verbatim(self) -> None:
        # Nothing to schedule: the pre-athenaeum#1291 order must survive.
        files = [_raw("alpha", "a0.md"), _raw("beta", "b0.md"), _raw("alpha", "a1.md")]
        assert round_robin_by_source(files, 50) == files

    def test_source_turn_order_is_first_appearance_order(self) -> None:
        files = [_raw("beta", "b0.md"), _raw("alpha", "a0.md")]
        window = round_robin_by_source(files + files, 2)
        assert [f.source for f in window] == ["beta", "alpha"]

    def test_no_source_waits_unboundedly_across_repeated_runs(self) -> None:
        # AC1, stated as the property it actually claims: drain the corpus one
        # window at a time, refilling the alphabetically-early source at the
        # drain rate every run (exactly the observed `auto-memory` behaviour),
        # and assert the later source is scheduled within a bounded number of
        # runs rather than never.
        early = [_raw("auto-memory", f"a{i}.md") for i in range(79)]
        late = [_raw("zzz-late-source", f"z{i}.md") for i in range(100)]
        first_run_scheduling_late = None
        for run_no in range(1, 11):
            window = round_robin_by_source(early + late, 50)
            taken = {f.path for f in window}
            if any(f.source == "zzz-late-source" for f in window):
                first_run_scheduling_late = first_run_scheduling_late or run_no
            early = [f for f in early if f.path not in taken]
            late = [f for f in late if f.path not in taken]
            # Refill the early source at (above) the drain rate.
            early += [_raw("auto-memory", f"refill-{run_no}-{i}.md") for i in range(30)]
        assert first_run_scheduling_late == 1
        # And the late source is fully drained, not merely nibbled at.
        assert late == []


class TestTurnOrderRotates:
    """AC1 below the ``limit >= n_sources`` threshold.

    Round-robin alone bounds the wait only while every source gets at least
    one slot per run. With a window narrower than the source count, a FIXED
    turn order starves the same trailing sources on every run forever — the
    original defect at a different threshold. ``priority_sources`` (fed the
    previous run's zero-slot sources by the librarian) rotates the head.
    """

    def test_without_rotation_the_same_sources_win_every_run(self) -> None:
        # The property that makes rotation necessary, pinned so the reason for
        # `priority_sources` cannot be refactored away silently.
        files = [_raw(f"s{i}", f"f{j}.md") for i in range(5) for j in range(10)]
        first = [f.source for f in round_robin_by_source(files, 3)]
        second = [f.source for f in round_robin_by_source(files, 3)]
        assert first == second == ["s0", "s1", "s2"]

    def test_priority_sources_take_their_turn_first(self) -> None:
        files = [_raw(f"s{i}", f"f{j}.md") for i in range(5) for j in range(10)]
        window = round_robin_by_source(files, 3, priority_sources=["s3", "s4"])
        assert [f.source for f in window] == ["s3", "s4", "s0"]

    def test_a_narrow_window_reaches_every_source_within_a_bounded_run_count(
        self,
    ) -> None:
        # AC1 proper: feed each run's zero-slot sources into the next run's
        # priority head, exactly as the librarian does via the ledger, and
        # assert every source is scheduled within ceil(n_sources / limit) runs.
        sources = [f"s{i}" for i in range(5)]
        files = [_raw(s, f"f{j}.md") for s in sources for j in range(50)]
        limit = 2
        history: list[dict] = []
        ever_scheduled: set[str] = set()
        bound = -(-len(sources) // limit)  # ceil
        for _ in range(bound):
            window = round_robin_by_source(
                files, limit, priority_sources=starvation_priority(history)
            )
            scheduled = {f.source for f in window}
            ever_scheduled |= scheduled
            history.append(_record(sorted(set(sources) - scheduled)))
        assert ever_scheduled == set(sources)

    @pytest.mark.parametrize("n_sources", range(2, 13))
    def test_the_ceil_bound_holds_across_every_window_width(
        self, n_sources: int
    ) -> None:
        # AC1 as a swept property rather than one hand-picked case: for every
        # source count 2..12 and every window width 1..n+2, every source is
        # scheduled within ceil(n_sources / limit) runs.
        sources = [f"s{i}" for i in range(n_sources)]
        files = [_raw(s, f"f{j}.md") for s in sources for j in range(200)]
        for limit in range(1, n_sources + 3):
            history: list[dict] = []
            ever_scheduled: set[str] = set()
            for _ in range(-(-n_sources // limit)):
                window = round_robin_by_source(
                    files, limit, priority_sources=starvation_priority(history)
                )
                scheduled = {f.source for f in window}
                ever_scheduled |= scheduled
                history.append(_record(sorted(set(sources) - scheduled)))
            assert ever_scheduled == set(sources), f"limit={limit}"

    def test_rotation_by_name_alone_would_not_bound_the_wait(self) -> None:
        # Why `starvation_priority` ages by streak rather than just handing
        # back last run's starved set: with a name-ordered head, a source can
        # keep losing its turn to sources starved only once and never run.
        sources = [f"s{i}" for i in range(5)]
        files = [_raw(s, f"f{j}.md") for s in sources for j in range(50)]
        priority: list[str] = []
        ever_scheduled: set[str] = set()
        for _ in range(3):
            window = round_robin_by_source(files, 2, priority_sources=priority)
            scheduled = {f.source for f in window}
            ever_scheduled |= scheduled
            priority = sorted(set(sources) - scheduled)  # name-ordered, unaged
        assert "s4" not in ever_scheduled

    def test_a_priority_source_with_no_pending_files_costs_nothing(self) -> None:
        files = [_raw("alpha", f"a{i}.md") for i in range(5)]
        window = round_robin_by_source(files, 2, priority_sources=["gone", "alpha"])
        assert [f.path.name for f in window] == ["a0.md", "a1.md"]

    def test_duplicate_priority_entries_do_not_double_a_sources_turn(self) -> None:
        files = [_raw(f"s{i}", f"f{j}.md") for i in range(3) for j in range(5)]
        window = round_robin_by_source(files, 3, priority_sources=["s2", "s2"])
        assert [f.source for f in window] == ["s2", "s0", "s1"]


# ---------------------------------------------------------------------------
# Starvation streaks over the durable athenaeum#1102 ledger (AC3)
# ---------------------------------------------------------------------------


def _record(
    starved: list[str] | None,
    *,
    entity: bool = True,
    stalled: list[str] | None = None,
) -> dict:
    if not entity:
        return {"v": 2, "ts": "2026-09-01T00:00:00Z", "phases": {"retire": {"secs": 0}}}
    fields: dict = {"secs": 1.0, "reason": "completed"}
    if starved:
        fields[STARVATION_FIELD] = ",".join(starved)
    # Issue athenaeum#1295: `stalled` is the processing-stall channel's own
    # field, additive to (never derived from) `starved` above -- a record
    # can carry either, both, or neither.
    if stalled:
        fields[STALL_FIELD] = ",".join(stalled)
    return {"v": 2, "ts": "2026-09-01T00:00:00Z", "phases": {"entity": fields}}


class TestStarvationStreaks:
    def test_first_starvation_scores_one(self) -> None:
        assert starvation_streaks(["mural"], []) == {"mural": 1}

    def test_streak_counts_consecutive_trailing_runs_including_this_one(self) -> None:
        history = [_record(["mural"]) for _ in range(7)]
        assert starvation_streaks(["mural"], history) == {"mural": 8}

    def test_a_run_that_fed_the_source_breaks_the_streak(self) -> None:
        history = [_record(["mural"]), _record([]), _record(["mural"])]
        assert starvation_streaks(["mural"], history) == {"mural": 2}

    def test_a_run_with_no_entity_phase_neither_breaks_nor_extends(self) -> None:
        # A merge-only run could not schedule anything, so it is no evidence
        # either way — it must not silently reset a real stall to 1.
        history = [_record(["mural"]), _record(None, entity=False), _record(["mural"])]
        assert starvation_streaks(["mural"], history) == {"mural": 3}

    def test_sources_are_tracked_independently(self) -> None:
        history = [_record(["mural", "retros"]), _record(["mural"])]
        assert starvation_streaks(["mural", "retros"], history) == {
            "mural": 3,
            "retros": 1,
        }

    def test_starved_sources_in_record_distinguishes_absent_from_empty(self) -> None:
        assert starved_sources_in_record(_record(None, entity=False)) is None
        assert starved_sources_in_record(_record([])) == set()
        assert starved_sources_in_record(_record(["a", "b"])) == {"a", "b"}
        assert starved_sources_in_record({"phases": "not-a-dict"}) is None

    def test_reads_the_durable_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(
            "".join(json.dumps(_record(["mural"])) + "\n" for _ in range(3)),
            encoding="utf-8",
        )
        assert read_starvation_streaks(["mural"], ledger_path=ledger) == {"mural": 4}

    def test_a_missing_ledger_degrades_to_no_history(self, tmp_path: Path) -> None:
        streaks = read_starvation_streaks(
            ["mural"], ledger_path=tmp_path / "absent.jsonl"
        )
        assert streaks == {"mural": 1}

    def test_no_starved_sources_never_touches_the_filesystem(self) -> None:
        assert read_starvation_streaks([], ledger_path=Path("/nonexistent/x")) == {}


class TestStarvationPriority:
    """The aged rotation input the librarian feeds back into the scheduler."""

    def test_longest_starved_source_leads(self) -> None:
        # `s4` was starved on both runs; `s0`/`s1` only on the last one.
        history = [_record(["s2", "s3", "s4"]), _record(["s0", "s1", "s4"])]
        assert starvation_priority(history) == ["s4", "s0", "s1"]

    def test_ties_break_by_name_for_determinism(self) -> None:
        assert starvation_priority([_record(["b", "a", "c"])]) == ["a", "b", "c"]

    def test_a_healthy_last_run_needs_no_rotation(self) -> None:
        assert starvation_priority([_record(["s3"]), _record([])]) == []

    def test_no_history_is_plain_discovery_order(self) -> None:
        assert starvation_priority([]) == []

    def test_skips_runs_whose_entity_phase_never_ran(self) -> None:
        history = [_record(["s3"]), _record(None, entity=False)]
        assert starvation_priority(history) == ["s3"]

    def test_reads_the_durable_ledger_and_fails_open(self, tmp_path: Path) -> None:
        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(
            json.dumps(_record(["s3", "s4"])) + "\n" + json.dumps(_record(["s4"])) + "\n",
            encoding="utf-8",
        )
        assert read_starvation_priority(ledger_path=ledger) == ["s4"]
        assert read_starvation_priority(ledger_path=tmp_path / "absent") == []


class TestPreviousStarvedSources:
    def test_reads_the_most_recent_entity_run(self) -> None:
        history = [_record(["old"]), _record(["s3", "s4"])]
        assert previous_starved_sources(history) == ["s3", "s4"]

    def test_skips_runs_whose_entity_phase_never_ran(self) -> None:
        history = [_record(["s3"]), _record(None, entity=False)]
        assert previous_starved_sources(history) == ["s3"]

    def test_a_run_that_starved_nobody_reads_as_empty_not_stale(self) -> None:
        history = [_record(["s3"]), _record([])]
        assert previous_starved_sources(history) == []

    def test_no_history_reads_as_nothing_starved(self) -> None:
        assert previous_starved_sources([]) == []


class TestRunSummaryNamesStarvedSources:
    def test_a_source_over_the_threshold_is_named_in_the_head(self) -> None:
        line = _render_run_summary(
            [("entity", 1.0, {"files": 50, "reason": "completed"})],
            starved_streaks={"mural-board-summary": STARVATION_STREAK_THRESHOLD},
        )
        assert (
            f"starved_sources=mural-board-summary:{STARVATION_STREAK_THRESHOLD}" in line
        )

    def test_a_transient_one_run_exclusion_is_not_flagged(self) -> None:
        line = _render_run_summary(
            [("entity", 1.0, {"files": 50, "reason": "completed"})],
            starved_streaks={"mural-board-summary": 1},
        )
        assert "starved_sources=" not in line

    def test_omitted_entirely_on_a_healthy_run(self) -> None:
        line = _render_run_summary([("entity", 1.0, {"reason": "completed"})])
        assert "starved_sources=" not in line

    def test_the_athenaeum713_parser_survives_the_new_tokens(self) -> None:
        # The prose line is a documented, parsed surface; the new head and
        # entity tokens must not break the reader that consumes it.
        line = _render_run_summary(
            [("entity", 1.0, {"files": 2, STARVATION_FIELD: "s3,s4"})],
            starved_streaks={"s3": 5, "s4": 3},
        )
        record = parse_run_summary_line(line)
        assert record is not None
        assert record.phases["entity"][STARVATION_FIELD] == "s3,s4"
        assert record.phases["entity"]["files"] == "2"
        assert "starved_sources=s3:5,s4:3" in line


# ---------------------------------------------------------------------------
# Processing-stall streaks over the durable athenaeum#1102 ledger
# (issue athenaeum#1295 AC1/AC3) — the SECOND, independent starvation
# channel: a source that gets slots but never gets its slots reached before
# a mid-window max_runtime/max_api_calls trip. Mirrors every
# TestStarvation*/TestPreviousStarvedSources test above one-for-one, reading
# STALL_FIELD instead of STARVATION_FIELD, to pin that the two channels stay
# structurally identical in shape but are NEVER conflated in practice (see
# TestBothChannelsInOneRun below for the "never conflated" property itself).
# ---------------------------------------------------------------------------


class TestStallStreaks:
    def test_first_stall_scores_one(self) -> None:
        assert stall_streaks(["mural"], []) == {"mural": 1}

    def test_streak_counts_consecutive_trailing_runs_including_this_one(self) -> None:
        history = [_record(None, stalled=["mural"]) for _ in range(7)]
        assert stall_streaks(["mural"], history) == {"mural": 8}

    def test_a_run_that_processed_the_source_breaks_the_streak(self) -> None:
        history = [
            _record(None, stalled=["mural"]),
            _record(None, stalled=[]),
            _record(None, stalled=["mural"]),
        ]
        assert stall_streaks(["mural"], history) == {"mural": 2}

    def test_a_run_with_no_entity_phase_neither_breaks_nor_extends(self) -> None:
        history = [
            _record(None, stalled=["mural"]),
            _record(None, entity=False),
            _record(None, stalled=["mural"]),
        ]
        assert stall_streaks(["mural"], history) == {"mural": 3}

    def test_stalled_sources_in_record_distinguishes_absent_from_empty(self) -> None:
        assert stalled_sources_in_record(_record(None, entity=False)) is None
        assert stalled_sources_in_record(_record(None, stalled=[])) == set()
        assert stalled_sources_in_record(_record(None, stalled=["a", "b"])) == {
            "a",
            "b",
        }
        assert stalled_sources_in_record({"phases": "not-a-dict"}) is None

    def test_starved_and_stalled_fields_are_read_independently(self) -> None:
        # The two channels can both be present on the SAME record without
        # either reader picking up the other's field.
        record = _record(["mural"], stalled=["retros"])
        assert starved_sources_in_record(record) == {"mural"}
        assert stalled_sources_in_record(record) == {"retros"}

    def test_reads_the_durable_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(
            "".join(
                json.dumps(_record(None, stalled=["mural"])) + "\n" for _ in range(3)
            ),
            encoding="utf-8",
        )
        assert read_stall_streaks(["mural"], ledger_path=ledger) == {"mural": 4}

    def test_a_missing_ledger_degrades_to_no_history(self, tmp_path: Path) -> None:
        streaks = read_stall_streaks(["mural"], ledger_path=tmp_path / "absent.jsonl")
        assert streaks == {"mural": 1}

    def test_no_stalled_sources_never_touches_the_filesystem(self) -> None:
        assert read_stall_streaks([], ledger_path=Path("/nonexistent/x")) == {}


class TestStallPriority:
    def test_longest_stalled_source_leads(self) -> None:
        history = [
            _record(None, stalled=["s2", "s3", "s4"]),
            _record(None, stalled=["s0", "s1", "s4"]),
        ]
        assert stall_priority(history) == ["s4", "s0", "s1"]

    def test_ties_break_by_name_for_determinism(self) -> None:
        assert stall_priority([_record(None, stalled=["b", "a", "c"])]) == [
            "a",
            "b",
            "c",
        ]

    def test_a_healthy_last_run_needs_no_rotation(self) -> None:
        assert (
            stall_priority([_record(None, stalled=["s3"]), _record(None, stalled=[])])
            == []
        )

    def test_no_history_is_plain_discovery_order(self) -> None:
        assert stall_priority([]) == []

    def test_skips_runs_whose_entity_phase_never_ran(self) -> None:
        history = [_record(None, stalled=["s3"]), _record(None, entity=False)]
        assert stall_priority(history) == ["s3"]


class TestCombinedStarvationPriority:
    """Plan step 3: the two channels compose into ONE deterministic head."""

    def test_slot_priority_is_unchanged_and_leads(self) -> None:
        # The athenaeum#1291 AC1 bound rests on this list's order/position
        # never moving; combining channels must not disturb it.
        combined = combined_starvation_priority(["s2", "s0"], ["s5", "s1"])
        assert combined == ["s2", "s0", "s5", "s1"]

    def test_a_source_named_by_both_channels_is_not_duplicated(self) -> None:
        combined = combined_starvation_priority(["s2", "s0"], ["s0", "s5"])
        assert combined == ["s2", "s0", "s5"]

    def test_either_channel_alone_passes_through(self) -> None:
        assert combined_starvation_priority(["s2"], []) == ["s2"]
        assert combined_starvation_priority([], ["s5"]) == ["s5"]

    def test_both_empty_is_empty(self) -> None:
        assert combined_starvation_priority([], []) == []

    def test_reads_the_durable_ledger_and_composes_both_channels(
        self, tmp_path: Path
    ) -> None:
        ledger = tmp_path / "run_summary.jsonl"
        # One record: `slot-src` got zero slots (starved), `stall-src` got
        # slots but processed nothing (stalled). A real run's entity phase
        # can emit both fields on the same record (see TestBothChannelsInOneRun).
        ledger.write_text(
            json.dumps(_record(["slot-src"], stalled=["stall-src"])) + "\n",
            encoding="utf-8",
        )
        assert read_combined_starvation_priority(ledger_path=ledger) == [
            "slot-src",
            "stall-src",
        ]
        assert read_combined_starvation_priority(ledger_path=tmp_path / "absent") == []


class TestRunSummaryNamesStalledSources:
    """Mirrors TestRunSummaryNamesStarvedSources for the athenaeum#1295 channel."""

    def test_a_source_over_the_threshold_is_named_in_the_head(self) -> None:
        line = _render_run_summary(
            [("entity", 1.0, {"files": 50, "reason": "completed"})],
            stall_streaks={"mural-board-summary": STALL_STREAK_THRESHOLD},
        )
        assert (
            f"stalled_sources=mural-board-summary:{STALL_STREAK_THRESHOLD}" in line
        )

    def test_a_transient_one_run_exclusion_is_not_flagged(self) -> None:
        line = _render_run_summary(
            [("entity", 1.0, {"files": 50, "reason": "completed"})],
            stall_streaks={"mural-board-summary": 1},
        )
        assert "stalled_sources=" not in line

    def test_omitted_entirely_on_a_healthy_run(self) -> None:
        line = _render_run_summary([("entity", 1.0, {"reason": "completed"})])
        assert "stalled_sources=" not in line

    def test_the_two_head_tokens_are_never_conflated_on_one_line(self) -> None:
        # Both channels flagged on the SAME run: two distinct head tokens,
        # neither swallowing the other.
        line = _render_run_summary(
            [("entity", 1.0, {"reason": "completed"})],
            starved_streaks={"slot-src": STARVATION_STREAK_THRESHOLD},
            stall_streaks={"stall-src": STALL_STREAK_THRESHOLD},
        )
        assert f"starved_sources=slot-src:{STARVATION_STREAK_THRESHOLD}" in line
        assert f"stalled_sources=stall-src:{STALL_STREAK_THRESHOLD}" in line

    def test_the_athenaeum713_parser_survives_the_new_tokens(self) -> None:
        line = _render_run_summary(
            [("entity", 1.0, {"files": 2, STALL_FIELD: "s3,s4"})],
            stall_streaks={"s3": 5, "s4": 3},
        )
        record = parse_run_summary_line(line)
        assert record is not None
        assert record.phases["entity"][STALL_FIELD] == "s3,s4"
        assert record.phases["entity"]["files"] == "2"
        assert "stalled_sources=s3:5,s4:3" in line


# ---------------------------------------------------------------------------
# End-to-end through run() (AC2, AC3)
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path) -> Path:
    """Knowledge root on a non-protected branch (mirrors athenaeum#900's harness)."""
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    (root / "raw").mkdir()
    (root / "raw" / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


def _write_source(root: Path, source: str, count: int, *, day_offset: int = 0) -> None:
    src = root / "raw" / source
    src.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        stamp = f"2024{(4 + day_offset):02d}{(i % 28) + 1:02d}T12{i % 60:02d}00Z"
        (src / f"{stamp}-{i:08x}.md").write_text(
            f"Note {i} from {source} about Acme Corp.\n", encoding="utf-8"
        )


def _recording_process_one(seen: list[str], wiki_root: Path):
    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        seen.append(raw.source)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(
            created=[page.name], updated=[], escalated=[], skipped=[]
        )

    return fake_process_one


def _budget_counting_process_one(seen: list[str], wiki_root: Path):
    """Like :func:`_recording_process_one`, but spends one ``usage.api_calls``
    per file (issue athenaeum#1295).

    The real entity loop's ``max_api_calls`` check (``ctx.usage.api_calls >=
    ctx.max_api_calls``) reads the SAME ``TokenUsage`` instance the librarian
    threads into ``process_one`` as the ``usage=`` kwarg — a stub that never
    touches it (like ``_recording_process_one`` above) can never trip the
    budget, no matter how low ``max_api_calls`` is set. This stub increments
    it by exactly 1 per call, mirroring a single-call file, so a test can
    drive a real, deterministic mid-window budget trip without a live API or
    a wall-clock sleep.
    """

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        usage = kwargs.get("usage")
        if usage is not None:
            usage.api_calls += 1
        seen.append(raw.source)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(
            created=[page.name], updated=[], escalated=[], skipped=[]
        )

    return fake_process_one


class TestEndToEndFairness:
    def test_a_later_source_receives_slots_despite_a_refilled_early_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC2, verbatim: a large, alphabetically-early, continuously-refilled
        # source and a later source that must still receive slots. Before
        # athenaeum#1291 `seen` was 100% `aaa-early` on every run.
        root = _seed(tmp_path)
        _write_source(root, "aaa-early", 60)
        _write_source(root, "zzz-late", 40, day_offset=1)

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        stats: dict = {}

        for run_no in range(3):
            seen: list[str] = []
            monkeypatch.setattr(
                "athenaeum.librarian.process_one",
                _recording_process_one(seen, root / "wiki"),
            )
            rc = run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_files=10,
                max_api_calls=1000,
                out_run_stats=stats,
            )
            assert rc == 0
            # AC4: the window is still exactly max_files.
            assert len(seen) == 10
            # AC1/AC2: the late source is scheduled on EVERY run, not once the
            # early source's backlog happens to fall below the cap.
            assert "zzz-late" in seen, f"run {run_no}: late source starved"
            assert seen.count("zzz-late") == 5
            assert stats["starved_sources"] == []
            # Refill the early source at the drain rate, as the real deployment's
            # continuously-written `auto-memory` does.
            _write_source(root, "aaa-early", 5, day_offset=2 + run_no)

    def test_caller_scoped_files_stay_pinned_at_the_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The athenaeum#900 guarantee must survive the round-robin: the caller's
        # own files compile FIRST, and fair scheduling applies only to the
        # remaining budget.
        root = _seed(tmp_path)
        _write_source(root, "aaa-early", 40)
        _write_source(root, "zzz-late", 40, day_offset=1)
        mine = root / "raw" / "zzz-late" / "20260815T120000Z-deadbeef.md"
        mine.write_text("Met Alice Zhang about the new thing.\n", encoding="utf-8")

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        names: list[str] = []

        def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            seen.append(raw.source)
            names.append(raw.path.name)
            page = root / "wiki" / f"entity-{len(seen)}.md"
            page.write_text("# Entity\n", encoding="utf-8")
            return SimpleNamespace(
                created=[page.name], updated=[], escalated=[], skipped=[]
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=4,
            max_api_calls=100,
            entity_changed_paths={mine},
        )

        assert rc == 0
        assert names[0] == mine.name
        assert len(names) == 4
        # The remaining 3 slots are shared, not handed wholesale to `aaa-early`.
        assert seen[1:].count("aaa-early") >= 1
        assert seen[1:].count("zzz-late") >= 1


    def test_a_starved_source_is_named_in_the_run_summary_after_k_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        # AC3 end-to-end, on the ONE starvation shape the scheduler cannot fix
        # by rotating: the athenaeum#900 caller-scoped pin. A compile whose
        # caller names at least `max_files` of its own files consumes the whole
        # window BY CONTRACT, so every other source gets zero slots on every
        # such run and no turn-order rotation can change that. That is exactly
        # the case an operator must be told about by NAME, rather than as a
        # `beyond_window` count that reads like ordinary backpressure.
        root = _seed(tmp_path)
        _write_source(root, "backlog", 20)
        mine = []
        callers = root / "raw" / "caller"
        callers.mkdir(parents=True)
        for i in range(4):
            p = callers / f"2026081{i}T120000Z-deadbee{i}.md"
            p.write_text(f"Met Alice Zhang {i}.\n", encoding="utf-8")
            mine.append(p)
        ledger = tmp_path / "run_summary.jsonl"

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )

        summaries: list[str] = []
        for run_no in range(STARVATION_STREAK_THRESHOLD):
            seen: list[str] = []
            monkeypatch.setattr(
                "athenaeum.librarian.process_one",
                _recording_process_one(seen, root / "wiki"),
            )
            stats: dict = {}
            with caplog.at_level("INFO", logger="athenaeum.librarian"):
                caplog.clear()
                rc = run(
                    raw_root=root / "raw",
                    wiki_root=root / "wiki",
                    knowledge_root=root,
                    max_files=2,
                    max_api_calls=1000,
                    entity_changed_paths=set(mine),
                    out_run_stats=stats,
                )
            assert rc == 0
            # The caller's pin filled the window; `backlog` got nothing.
            assert stats["starved_sources"] == ["backlog"], f"run {run_no}"
            summaries.append(
                next(m for m in caplog.messages if m.startswith("librarian-run-summary"))
            )
            # Re-seed the caller's own files so the pin fills the window again
            # (the two just compiled are consumed), and keep `backlog` pending.
            for i in range(2):
                p = callers / f"2026082{run_no}{i}T120000Z-cafebab{i}.md"
                p.write_text(f"Met Bob Lee {run_no}-{i}.\n", encoding="utf-8")
                mine.append(p)

        # One run with no slots is ordinary windowing, so the first summary
        # must NOT flag it; by run K it is a stall, and the summary names the
        # source rather than reporting a bare count.
        assert "starved_sources=" not in summaries[0]
        assert (
            f"starved_sources=backlog:{STARVATION_STREAK_THRESHOLD}" in summaries[-1]
        )
        assert any(
            m.startswith("librarian-source-starvation") and "source=backlog" in m
            for m in caplog.messages
        )


# ---------------------------------------------------------------------------
# Processing-stall channel, end-to-end through run() (issue athenaeum#1295)
#
# athenaeum#1291's `starved_sources` counts SLOTS. It cannot see a source
# that receives slots every run but never gets them REACHED because a
# mid-window `max_runtime`/`max_api_calls` trip stops the loop first. These
# pin the athenaeum#1295 acceptance criteria:
#
# * AC1 -- a source with pending intake, nonzero slots, and zero processed
#   files for K consecutive runs is named, DISTINCTLY from the athenaeum#1291
#   zero-slot case (TestBothChannelsInOneRun).
# * AC2 -- a real max_api_calls trip, part-way through a genuinely
#   INTERLEAVED window, names the affected source
#   (TestEndToEndProcessingStall).
# * AC3 -- the counter persists through the athenaeum#1102 ledger with no
#   new state file, and the streak accrues across consecutive runs and
#   resets (TestStallStreakEndToEnd; TestStallStreaks above covers reset at
#   the pure-function level).
# * Plan step 3 -- a source stalled through this channel ages into the head
#   of the NEXT run's turn order (TestTurnOrderAgesInStalledSource).
# * Negative cases -- a healthy run's summary line and ledger record are
#   byte-unchanged, and a source that processes files is never named
#   (TestStallSignalUnchangedOnHealthyRuns).
# ---------------------------------------------------------------------------


class TestEndToEndProcessingStall:
    def test_a_mid_window_api_call_budget_trip_stalls_the_last_turn_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC2, literally: interleave a window across THREE sources (so the
        # window is genuinely interleaved, not just two sources trivially
        # alternating), budget a call count that reaches only the first TWO
        # turns, and assert the third source -- touched by neither a
        # zero-slot outcome NOR any processing -- is named STALLED, not
        # starved. This is the shape the issue's premise rests on:
        # interleaving softens the damage (alpha/beta each got their first
        # file through) but does not eliminate it (gamma's slots existed —
        # equal footing with alpha/beta — and were never reached).
        root = _seed(tmp_path)
        for source in ("alpha", "beta", "gamma"):
            _write_source(root, source, 3)
        ledger = tmp_path / "run_summary.jsonl"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen, root / "wiki"),
        )
        stats: dict = {}

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=6,
            max_api_calls=2,
            out_run_stats=stats,
        )

        assert rc == 0
        # The window really was interleaved: alpha's and beta's FIRST files
        # were processed before gamma's first file was ever reached, even
        # though gamma had files in the window on equal footing.
        assert seen == ["alpha", "beta"]
        assert stats["stalled_sources"] == ["gamma"]
        assert stats["starved_sources"] == []

    def test_a_processed_source_is_never_named_stalled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The direct negative of the above: alpha and beta each processed a
        # file this run, so neither may appear in `stalled_sources` even
        # though the run overall DID trip its budget.
        root = _seed(tmp_path)
        for source in ("alpha", "beta", "gamma"):
            _write_source(root, source, 3)
        ledger = tmp_path / "run_summary.jsonl"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen, root / "wiki"),
        )
        stats: dict = {}

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=6,
            max_api_calls=2,
            out_run_stats=stats,
        )

        assert "alpha" not in stats["stalled_sources"]
        assert "beta" not in stats["stalled_sources"]


class TestBothChannelsInOneRun:
    def test_zero_slot_and_zero_processed_fire_together_without_conflation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        # AC1: both starvation channels can trip in the SAME run --
        # `ccc` gets NO slots at all (the athenaeum#1291 case), `bbb` gets a
        # slot but the budget trips before it is reached (the athenaeum#1295
        # case) -- and the two lists must never merge or cross-contaminate.
        root = _seed(tmp_path)
        for source in ("aaa", "bbb", "ccc"):
            _write_source(root, source, 2)
        ledger = tmp_path / "run_summary.jsonl"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen, root / "wiki"),
        )
        stats: dict = {}

        with caplog.at_level("INFO", logger="athenaeum.librarian"):
            rc = run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                # A window narrower than the source count: only 2 of 3
                # sources get ANY slot at all.
                max_files=2,
                max_api_calls=1,
                out_run_stats=stats,
            )

        assert rc == 0
        assert seen == ["aaa"]
        assert stats["starved_sources"] == ["ccc"]
        assert stats["stalled_sources"] == ["bbb"]
        # Never conflated: disjoint sets, and neither list contains a source
        # from the wrong channel.
        assert set(stats["starved_sources"]).isdisjoint(stats["stalled_sources"])
        summary_line = next(
            m for m in caplog.messages if m.startswith("librarian-run-summary")
        )
        assert f" {STARVATION_FIELD}=ccc" in summary_line
        assert f" {STALL_FIELD}=bbb" in summary_line


class TestStallStreakEndToEnd:
    def test_a_stalled_source_is_named_after_k_runs_and_resets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        # AC3 end-to-end. A caller-scoped pin (exempt from round-robin AND
        # from priority-order aging, per athenaeum#900) always consumes the
        # run's single API call, so `backlog` -- which DOES receive slots
        # every run via round-robin -- never gets any of them reached,
        # REGARDLESS of the Plan-step-3 turn-order aging this issue adds
        # (that aging only reorders the round-robin remainder, never the
        # pinned head). This isolates the streak-accrual property from the
        # self-correcting aging behaviour, which is exercised separately in
        # TestTurnOrderAgesInStalledSource below.
        root = _seed(tmp_path)
        _write_source(root, "backlog", 20)
        callers = root / "raw" / "caller"
        callers.mkdir(parents=True)
        mine = []
        p0 = callers / "20260810T120000Z-deadbee0.md"
        p0.write_text("Met Alice Zhang.\n", encoding="utf-8")
        mine.append(p0)
        ledger = tmp_path / "run_summary.jsonl"

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )

        summaries: list[str] = []
        for run_no in range(STALL_STREAK_THRESHOLD):
            seen: list[str] = []
            monkeypatch.setattr(
                "athenaeum.librarian.process_one",
                _budget_counting_process_one(seen, root / "wiki"),
            )
            stats: dict = {}
            with caplog.at_level("INFO", logger="athenaeum.librarian"):
                caplog.clear()
                rc = run(
                    raw_root=root / "raw",
                    wiki_root=root / "wiki",
                    knowledge_root=root,
                    # 1 pinned + 2 round-robin slots for `backlog`.
                    max_files=3,
                    max_api_calls=1,
                    entity_changed_paths=set(mine),
                    out_run_stats=stats,
                )
            assert rc == 0
            # The pin consumed the sole call; `backlog` got slots (not
            # starved) but never got run (stalled).
            assert stats["starved_sources"] == [], f"run {run_no}"
            assert stats["stalled_sources"] == ["backlog"], f"run {run_no}"
            summaries.append(
                next(m for m in caplog.messages if m.startswith("librarian-run-summary"))
            )
            # Re-pin a fresh caller file for the next run (the previous one
            # was consumed/deleted).
            p = callers / f"2026082{run_no}T120000Z-cafebab{run_no}.md"
            p.write_text(f"Met Bob Lee {run_no}.\n", encoding="utf-8")
            mine.append(p)

        # One run with slots but nothing processed is ordinary backpressure;
        # by run K it is a stall, named rather than left as a bare count.
        assert "stalled_sources=" not in summaries[0]
        assert (
            f"stalled_sources=backlog:{STALL_STREAK_THRESHOLD}" in summaries[-1]
        )
        assert any(
            m.startswith("librarian-source-processing-stall")
            and "source=backlog" in m
            for m in caplog.messages
        )
        # AC3: no second state file -- every field above came out of the
        # SAME athenaeum#1102 ledger the athenaeum#1291 pair already writes.
        ledger_records = read_run_summary_ledger(ledger)
        assert len(ledger_records) == STALL_STREAK_THRESHOLD
        assert all(
            rec["phases"]["entity"].get(STALL_FIELD) == "backlog"
            for rec in ledger_records
        )

        # AC3 (reset half): a healthy run -- generous budget, the pin no
        # longer needed to prove anything -- processes `backlog` for real,
        # and its streak breaks.
        seen = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen, root / "wiki"),
        )
        stats = {}
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=3,
            max_api_calls=1000,
            entity_changed_paths=set(mine),
            out_run_stats=stats,
        )
        assert rc == 0
        assert stats["stalled_sources"] == []
        assert "backlog" in seen


class TestTurnOrderAgesInStalledSource:
    def test_a_stalled_source_leads_the_very_next_runs_turn_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plan step 3: without this, the signal is diagnostic only. Two
        # sources, discovery order alphabetical (`aaa-normal` naturally
        # first). Run 1 trips a 1-call budget and stalls `zzz-stalled` (last
        # in natural turn order). Run 2 must schedule `zzz-stalled` FIRST --
        # proving the athenaeum#1295 channel, not just the athenaeum#1291
        # one, feeds `round_robin_by_source`'s priority head.
        root = _seed(tmp_path)
        _write_source(root, "aaa-normal", 3)
        _write_source(root, "zzz-stalled", 3)
        ledger = tmp_path / "run_summary.jsonl"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )

        seen_run1: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen_run1, root / "wiki"),
        )
        stats1: dict = {}
        rc1 = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=2,
            max_api_calls=1,
            out_run_stats=stats1,
        )
        assert rc1 == 0
        # Natural (unaged) order: `aaa-normal` goes first, `zzz-stalled`
        # never gets its (one) slot reached.
        assert seen_run1 == ["aaa-normal"]
        assert stats1["stalled_sources"] == ["zzz-stalled"]

        seen_run2: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _budget_counting_process_one(seen_run2, root / "wiki"),
        )
        stats2: dict = {}
        rc2 = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=2,
            max_api_calls=1,
            out_run_stats=stats2,
        )
        assert rc2 == 0
        # Aged order: `zzz-stalled` now leads, and IT is the one that gets
        # this run's sole call -- self-correction, not merely a diagnostic.
        assert seen_run2 == ["zzz-stalled"]
        assert stats2["stalled_sources"] == ["aaa-normal"]


class TestStallSignalUnchangedOnHealthyRuns:
    def test_a_healthy_runs_summary_line_and_ledger_record_are_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        # Negative case: every file fits and every file is processed --
        # `stalled_sources` (and STALL_FIELD on the ledger record) must be
        # absent, not merely empty-and-rendered, so a healthy run's line is
        # byte-identical to its pre-athenaeum#1295 form.
        root = _seed(tmp_path)
        _write_source(root, "alpha", 2)
        _write_source(root, "beta", 2)
        ledger = tmp_path / "run_summary.jsonl"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setattr(
            "athenaeum.run_summary_log.default_run_summary_ledger_path",
            lambda cache_dir=None: ledger,
        )
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )
        stats: dict = {}

        with caplog.at_level("INFO", logger="athenaeum.librarian"):
            rc = run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_files=10,
                max_api_calls=1000,
                out_run_stats=stats,
            )

        assert rc == 0
        assert len(seen) == 4
        assert stats["starved_sources"] == []
        assert stats["stalled_sources"] == []
        summary_line = next(
            m for m in caplog.messages if m.startswith("librarian-run-summary")
        )
        assert STALL_FIELD not in summary_line
        assert "stalled_sources=" not in summary_line

        ledger_records = read_run_summary_ledger(ledger)
        assert len(ledger_records) == 1
        assert STALL_FIELD not in ledger_records[0]["phases"]["entity"]
        assert STARVATION_FIELD not in ledger_records[0]["phases"]["entity"]
