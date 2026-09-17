# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the Phase 2 write-path report additions (issue
athenaeum#1726 AC4): :func:`tests.evals.north_star_report.compute_write_path_stats`
and its rendering in :func:`tests.evals.north_star_report.render_report`.

Pure computation throughout -- no corpus build, no subprocess, no model
client. Synthetic :class:`~tests.evals.corpus.Observation` instances and a
plain ``{path: text}`` store mapping are enough to exercise every branch.
"""

from __future__ import annotations

from tests.evals.corpus import Observation
from tests.evals.north_star_report import (
    NorthStarReport,
    WritePathStats,
    build_report,
    compute_write_path_stats,
    render_report,
)


def _obs(uid: str, page_uid: str, body: str, *, tokens: tuple[str, ...] = ()) -> Observation:
    return Observation(
        uid=uid,
        page_uid=page_uid,
        source="sessions",
        timestamp="20260101T000000Z",
        uuid8="aaaaaaaa",
        body=body,
        answer_tokens=tokens,
    )


def test_all_tokens_retained_reports_full_counts() -> None:
    observations = [
        _obs("obs-1", "page-a", "PTO policy: 25 days.", tokens=("Cinderquill",)),
        _obs("obs-2", "page-a", "PTO policy: owned by Sofia.", tokens=()),
        _obs("obs-3", "page-b", "Confidentiality: strict.", tokens=("Harrowvex",)),
    ]
    store = {"page-a.md": "...Cinderquill...", "page-b.md": "...Harrowvex..."}

    stats = compute_write_path_stats("athenaeum", "core", observations, store)

    assert stats.system == "athenaeum"
    assert stats.corpus_scale == "core"
    assert stats.pages_targeted == 2
    assert stats.pages_written == 2
    assert stats.answer_tokens_total == 2
    assert stats.answer_tokens_retained == 2
    assert stats.observations_total == 3
    assert stats.observations_measured == 2
    assert stats.observations_dropped == 0


def test_a_missing_token_flags_its_page_and_observation_as_dropped() -> None:
    observations = [
        _obs("obs-1", "page-a", "fact one", tokens=("Cinderquill",)),
        _obs("obs-2", "page-b", "fact two", tokens=("Harrowvex",)),
    ]
    # page-a's token never made it into the store; page-b's did.
    store = {"only-page-b.md": "...Harrowvex..."}

    stats = compute_write_path_stats("native", "core", observations, store)

    assert stats.pages_targeted == 2
    assert stats.pages_written == 1
    assert stats.answer_tokens_total == 2
    assert stats.answer_tokens_retained == 1
    assert stats.observations_dropped == 1


def test_content_addressed_not_filename_addressed() -> None:
    """A native store's filenames are the MODEL's own choice -- the scanner
    must find a token regardless of which file it landed in."""
    observations = [_obs("obs-1", "page-a", "fact", tokens=("Cinderquill",))]
    store = {"the-model-picked-this-name.md": "some prose mentioning Cinderquill in passing"}

    stats = compute_write_path_stats("native", "small", observations, store)

    assert stats.pages_written == 1
    assert stats.answer_tokens_retained == 1


def test_no_token_bearing_observations_reports_none_not_zero() -> None:
    """None of the observations carry a planted token -- every optional
    field must render as an explicit "not measurable", never a fabricated
    zero that would look identical to total loss."""
    observations = [_obs("obs-1", "page-a", "fact with no plant", tokens=())]
    stats = compute_write_path_stats("athenaeum", "core", observations, {})

    assert stats.pages_written is None
    assert stats.answer_tokens_retained is None
    assert stats.observations_dropped is None
    assert stats.observations_measured == 0
    assert stats.observations_total == 1


def test_empty_observation_stream_reports_none_not_zero() -> None:
    stats = compute_write_path_stats("athenaeum", "core", [], {})
    assert stats.pages_written is None
    assert stats.answer_tokens_retained is None
    assert stats.observations_dropped is None
    assert stats.observations_total == 0


def _minimal_report(write_path_stats: tuple[WritePathStats, ...] = ()) -> NorthStarReport:
    return build_report([], write_path_stats=write_path_stats)


def test_render_report_omits_write_path_rows_when_none_supplied() -> None:
    rendered = render_report(_minimal_report())
    assert "## Write path (Phase 2, athenaeum#1726)" in rendered
    assert "_no Phase 2 write-path data in this run_" in rendered


def test_render_report_includes_write_path_table_when_stats_supplied() -> None:
    stats = (
        WritePathStats(
            system="athenaeum",
            corpus_scale="core",
            pages_targeted=2,
            pages_written=2,
            answer_tokens_total=2,
            answer_tokens_retained=2,
            observations_total=3,
            observations_measured=2,
            observations_dropped=0,
        ),
        WritePathStats(
            system="native",
            corpus_scale="core",
            pages_targeted=2,
            pages_written=None,
            answer_tokens_total=0,
            answer_tokens_retained=None,
            observations_total=3,
            observations_measured=0,
            observations_dropped=None,
        ),
    )
    rendered = render_report(_minimal_report(stats))

    assert "| athenaeum | core | 2 | 2 | 2 | 2 | 3 | 2 | 0 |" in rendered
    assert "| native | core | 2 | n/a | 0 | n/a | 3 | 0 | n/a |" in rendered
