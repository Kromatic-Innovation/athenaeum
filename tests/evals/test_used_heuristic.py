# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``used``-column heuristic measurement (issue athenaeum#1575).

UNMARKED — fully offline. No network, no credential, no Anthropic API call:
the synthetic arm is local fixture files, the rollout arm reads an
already-persisted ``ResultStore``.

The fixture *precondition* tests matter more than the outcome tests. A
content-only fixture whose page text happens to contain its own uid would
score as a true positive and silently zero out the false-negative cell — the
measurement would still "pass" while measuring nothing. So each fixture's
uid presence/absence in its own transcript is asserted directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.evals import used_heuristic
from tests.evals.containment import GridCell, ResultStore
from tests.evals.north_star_report import append_rollout_row, load_rollout_rows
from tests.evals.rollout import Arm, RolloutRecord
from tests.evals.used_heuristic import (
    build_fixtures,
    build_report,
    compute_rollout_agreement,
    render_report,
    run_synthetic_eval,
    write_report,
)
from tests.evals.used_heuristic_cli import main as cli_main

# ---------------------------------------------------------------------------
# AC1 fixtures: preconditions before outcomes
# ---------------------------------------------------------------------------


def _blob(fixture: used_heuristic.Fixture) -> str:
    """Everything ``determine_references`` will search, as one string.

    Deliberately the raw JSON of every transcript record — a strict superset
    of the haystack the heuristic assembles, so "uid absent here" implies
    "uid absent from the heuristic's blob" with no reimplementation of its
    extraction logic.
    """
    return "\n".join(json.dumps(rec) for rec in fixture.transcript_records)


def test_all_four_quadrants_are_present() -> None:
    quadrants = {f.quadrant for f in build_fixtures()}
    assert quadrants == {
        "used_with_uid",
        "used_without_uid",
        "echoed_not_used",
        "not_used",
    }


def test_fixture_uid_presence_preconditions() -> None:
    """The two quadrants that make the measurement non-decorative."""
    fixtures = {f.quadrant: f for f in build_fixtures()}

    # False-negative quadrant: the uid must be genuinely absent, or the
    # fixture silently becomes a true positive.
    content_only = fixtures["used_without_uid"]
    assert content_only.uid not in _blob(content_only)
    # ...and the page's distinctive content must be present, or it is not
    # "used" at all and the ground truth would be wrong.
    assert "lantern ferry departs on the quarter hour" in _blob(content_only)

    # False-positive quadrant: the uid must be present via an echo, and the
    # answer must not act on the page.
    echo = fixtures["echoed_not_used"]
    assert echo.uid in _blob(echo)
    assert "quarry siren is tested at noon" not in json.dumps(echo.transcript_records[-1])

    # Control quadrants.
    assert fixtures["used_with_uid"].uid in _blob(fixtures["used_with_uid"])
    assert fixtures["not_used"].uid not in _blob(fixtures["not_used"])


def test_no_fixture_uid_leaks_into_another_fixture_body() -> None:
    """Cross-contamination would make a quadrant's verdict depend on a
    neighbour's text rather than its own."""
    fixtures = build_fixtures()
    for fixture in fixtures:
        for other in fixtures:
            if other is fixture:
                continue
            assert fixture.uid not in other.page_body
            assert fixture.uid not in _blob(other)


# ---------------------------------------------------------------------------
# AC1 outcomes: the confusion matrix
# ---------------------------------------------------------------------------


def test_synthetic_eval_produces_all_four_matrix_cells(tmp_path: Path) -> None:
    result = run_synthetic_eval(tmp_path)
    cells = {o.quadrant: o.cell for o in result.outcomes}
    assert cells == {
        "used_with_uid": "true_positive",
        "used_without_uid": "false_negative",
        "echoed_not_used": "false_positive",
        "not_used": "true_negative",
    }
    matrix = result.matrix
    assert (matrix.true_positive, matrix.false_negative) == (1, 1)
    assert (matrix.false_positive, matrix.true_negative) == (1, 1)
    assert matrix.total == 4


def test_rates_are_computed_from_the_matrix(tmp_path: Path) -> None:
    matrix = run_synthetic_eval(tmp_path).matrix
    assert matrix.false_negative_rate == 0.5
    assert matrix.false_positive_rate == 0.5


def test_rates_are_none_not_zero_on_an_empty_matrix() -> None:
    empty = used_heuristic.ConfusionMatrix()
    assert empty.false_negative_rate is None
    assert empty.false_positive_rate is None


def test_synthetic_eval_uses_the_real_heuristic(tmp_path: Path) -> None:
    """The eval must drive ``determine_references`` itself, not a local
    reimplementation of it — otherwise it measures its own copy."""
    calls: list[str] = []
    from athenaeum import push_metrics

    original = push_metrics.determine_references

    def spy(session_id: str, **kwargs: object) -> object:
        calls.append(session_id)
        return original(session_id, **kwargs)  # type: ignore[arg-type]

    push_metrics.determine_references = spy  # type: ignore[assignment]
    try:
        run_synthetic_eval(tmp_path)
    finally:
        push_metrics.determine_references = original  # type: ignore[assignment]

    assert sorted(calls) == sorted(f.session_id for f in build_fixtures())


# ---------------------------------------------------------------------------
# AC2: rollout agreement, and its no-records degradation
# ---------------------------------------------------------------------------


def _push_record(probe_class: str, delivered_uid: str, body: str, answer: str) -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.PUSH,
        probe_id=f"probe-{delivered_uid}",
        probe_class=probe_class,
        corpus_scale="core",
        answer=answer,
        transcript=[{"pushed_context": f"**Uid:** {delivered_uid}\n{body}"}],
    )


def _store_with(tmp_path: Path, records: list[RolloutRecord]) -> ResultStore:
    store = ResultStore(tmp_path / "results.jsonl")
    for index, record in enumerate(records):
        cell = GridCell(
            probe=record.probe_id, arm=record.arm.value, corpus_scale="core", replicate=index
        )
        append_rollout_row(store, cell, record)
    return store


def test_agreement_counts_both_disagreement_directions(tmp_path: Path) -> None:
    body = "the harbour clock is wound anticlockwise on the second Tuesday of the month"
    store = _store_with(
        tmp_path,
        [
            # ledger yes, content yes -> agree
            _push_record("direct", "uid-a", body, f"uid-a says {body}."),
            # ledger no, content yes -> suspected false negative
            _push_record("direct", "uid-b", body, f"Answering: {body}."),
            # ledger yes, content no -> suspected echo
            _push_record("direct", "uid-c", body, "uid-c is not relevant; see the changelog."),
        ],
    )
    stats = compute_rollout_agreement(load_rollout_rows(store))
    assert len(stats) == 1
    stat = stats[0]
    assert stat.probe_class == "direct"
    assert stat.n == 3
    assert stat.ledger_no_content_yes == 1
    assert stat.ledger_yes_content_no == 1
    assert stat.agree == 1
    assert stat.agreement_rate == 1 / 3


def test_agreement_is_broken_out_per_probe_class(tmp_path: Path) -> None:
    body = "the lantern ferry departs on the quarter hour from the east slip"
    store = _store_with(
        tmp_path,
        [
            _push_record("direct", "uid-a", body, f"uid-a: {body}"),
            _push_record("confusable", "uid-b", body, f"uid-b: {body}"),
        ],
    )
    stats = compute_rollout_agreement(load_rollout_rows(store))
    assert [s.probe_class for s in stats] == ["confusable", "direct"]


def test_non_push_arms_are_excluded(tmp_path: Path) -> None:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id="probe-none",
        probe_class="direct",
        corpus_scale="core",
        answer="no memory was delivered",
    )
    store = _store_with(tmp_path, [record])
    assert compute_rollout_agreement(load_rollout_rows(store)) == []


def test_missing_store_yields_no_rows_not_an_error(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "never-run.jsonl")
    assert compute_rollout_agreement(load_rollout_rows(store)) == []


def test_absent_records_render_as_no_records_never_zero_agreement(tmp_path: Path) -> None:
    """The bug this guards against lives in the renderer, not the computer:
    a ``None`` agreement formatted as ``0.0%`` reads as "the heuristic agrees
    with nothing", which is a finding the data does not support."""
    report = build_report(
        run_synthetic_eval(tmp_path),
        [],
        store_path=tmp_path / "empty.jsonl",
        store_consulted=True,
    )
    text = render_report(report)
    agreement_section = text.split("(AC2)", 1)[1]
    assert "No records." in agreement_section
    assert "not an agreement of zero" in agreement_section
    assert "0.0%" not in agreement_section


def test_no_store_supplied_is_distinct_from_an_empty_store(tmp_path: Path) -> None:
    text = render_report(
        build_report(run_synthetic_eval(tmp_path), [], store_path=None, store_consulted=False)
    )
    agreement_section = text.split("(AC2)", 1)[1]
    assert "No rollout result store was supplied" in agreement_section
    assert "0.0%" not in agreement_section


# ---------------------------------------------------------------------------
# AC3: the dated measurement
# ---------------------------------------------------------------------------


def test_report_states_both_rates_and_what_used_means(tmp_path: Path) -> None:
    text = render_report(
        build_report(
            run_synthetic_eval(tmp_path),
            [],
            store_path=None,
            store_consulted=False,
            generated="2026-01-02T03:04:05+00:00",
        )
    )
    assert "False-negative rate" in text
    assert "False-positive rate" in text
    assert "50.0%" in text
    assert "uid string appears" in text


def test_write_report_is_dated_and_lands_in_the_requested_dir(tmp_path: Path) -> None:
    report = build_report(
        run_synthetic_eval(tmp_path),
        [],
        store_path=None,
        store_consulted=False,
        generated="2026-01-02T03:04:05+00:00",
    )
    out_dir = tmp_path / "docs" / "measurements"
    path = write_report(report, out_dir=out_dir)
    assert path.name == "used-column-heuristic-accuracy-2026-01-02.md"
    assert path.read_text(encoding="utf-8").startswith("# `used` column heuristic accuracy")


def test_report_names_the_ac4_followup(tmp_path: Path) -> None:
    """A measurement that states a defect without naming where it is tracked
    reads as a finding that went nowhere."""
    text = render_report(
        build_report(run_synthetic_eval(tmp_path), [], store_path=None, store_consulted=False)
    )
    assert used_heuristic.FOLLOWUP_ISSUE in text
    assert "unchanged (AC4)" in text


def test_default_measurements_dir_is_the_tracked_docs_corpus() -> None:
    assert used_heuristic.DEFAULT_MEASUREMENTS_DIR == Path("docs/measurements")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_a_measurement(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "out"
    assert cli_main(["--out-dir", str(out_dir)]) == 0
    written = list(out_dir.glob("used-column-heuristic-accuracy-*.md"))
    assert len(written) == 1
    assert "wrote" in capsys.readouterr().out


def test_cli_stdout_mode_writes_no_file(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "out"
    assert cli_main(["--out-dir", str(out_dir), "--stdout"]) == 0
    assert not out_dir.exists()
    assert "confusion matrix" in capsys.readouterr().out


def test_cli_over_a_store_reports_agreement(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    body = "the quarry siren is tested at noon on the first of the month"
    store = _store_with(tmp_path, [_push_record("direct", "uid-a", body, f"uid-a: {body}")])
    assert cli_main(["--store", str(store.path), "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "| direct |" in out
    assert "No records." not in out
