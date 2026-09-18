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
    FilingLossStats,
    NorthStarReport,
    WritePathStats,
    build_report,
    compute_filing_loss_stats,
    compute_write_path_stats,
    render_report,
)


def _obs(
    uid: str,
    page_uid: str,
    body: str,
    *,
    tokens: tuple[str, ...] = (),
    retain: bool = True,
) -> Observation:
    return Observation(
        uid=uid,
        page_uid=page_uid,
        source="sessions",
        timestamp="20260101T000000Z",
        uuid8="aaaaaaaa",
        body=body,
        answer_tokens=tokens,
        retain=retain,
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

    assert "| athenaeum | core | 2 | 2 | 2 | 2 | 0 | n/a | 3 | 2 | 0 |" in rendered
    assert "| native | core | 2 | n/a | 0 | n/a | 0 | n/a | 3 | 0 | n/a |" in rendered


# ---------------------------------------------------------------------------
# Transient (``retain=False``) ground truth -- issue athenaeum#1824.
# ---------------------------------------------------------------------------


def test_transient_tokens_are_scored_separately_from_retention() -> None:
    """A transient observation must not touch ANY retention field: its token
    is ground truth for the opposite expectation, so counting it in would
    both inflate the denominator and score a correct discard as a loss."""
    observations = [
        _obs("obs-1", "page-a", "durable fact", tokens=("Cinderquill",)),
        _obs(
            "obs-2",
            "transient-outage",
            "the portal is down this afternoon",
            tokens=("Zephrandil",),
            retain=False,
        ),
    ]
    store = {"page-a.md": "...Cinderquill..."}

    stats = compute_write_path_stats("athenaeum", "core", observations, store)

    # Retention side sees only the durable observation.
    assert stats.pages_targeted == 1
    assert stats.pages_written == 1
    assert stats.answer_tokens_total == 1
    assert stats.answer_tokens_retained == 1
    assert stats.observations_measured == 1
    assert stats.observations_dropped == 0
    # Transient side: the outage note was correctly discarded.
    assert stats.transient_total == 1
    assert stats.transient_retained == 0


def test_a_retained_transient_token_is_counted_against_the_store() -> None:
    observations = [
        _obs("obs-1", "page-a", "durable fact", tokens=("Cinderquill",)),
        _obs("obs-2", "transient-outage", "down today", tokens=("Zephrandil",), retain=False),
    ]
    store = {"page-a.md": "...Cinderquill...", "notes.md": "...Zephrandil..."}

    stats = compute_write_path_stats("athenaeum", "core", observations, store)

    assert stats.answer_tokens_retained == stats.answer_tokens_total == 1
    assert stats.transient_total == 1
    assert stats.transient_retained == 1, "hoarding a transient fact must be visible"


def test_transient_retained_is_none_when_no_transient_observation_is_present() -> None:
    """``None``, never a fabricated 0 -- the same discipline every other
    optional field on :class:`WritePathStats` already holds."""
    observations = [_obs("obs-1", "page-a", "durable fact", tokens=("Cinderquill",))]
    stats = compute_write_path_stats("athenaeum", "core", observations, {"a.md": "Cinderquill"})

    assert stats.transient_total == 0
    assert stats.transient_retained is None


def test_render_report_renders_the_transient_columns() -> None:
    stats = (
        WritePathStats(
            system="athenaeum",
            corpus_scale="core",
            pages_targeted=1,
            pages_written=1,
            answer_tokens_total=1,
            answer_tokens_retained=1,
            observations_total=2,
            observations_measured=1,
            observations_dropped=0,
            transient_total=3,
            transient_retained=1,
        ),
    )
    rendered = render_report(_minimal_report(stats))

    assert "transient_total | transient_retained" in rendered
    assert "| athenaeum | core | 1 | 1 | 1 | 1 | 3 | 1 | 2 | 1 | 0 |" in rendered
    assert "LOWER is better" in rendered


# ---------------------------------------------------------------------------
# lost_token_ids (issue athenaeum#1830, "name the lost facts")
# ---------------------------------------------------------------------------


def test_lost_token_ids_names_the_missing_durable_tokens() -> None:
    observations = [
        _obs("obs-1", "page-a", "fact one", tokens=("Cinderquill",)),
        _obs("obs-2", "page-b", "fact two", tokens=("Harrowvex",)),
    ]
    store = {"only-page-b.md": "...Harrowvex..."}

    stats = compute_write_path_stats("native", "core", observations, store)

    assert stats.lost_token_ids == ("Cinderquill",)


def test_lost_token_ids_is_empty_when_nothing_is_lost() -> None:
    observations = [_obs("obs-1", "page-a", "fact", tokens=("Cinderquill",))]
    stats = compute_write_path_stats("athenaeum", "core", observations, {"a.md": "Cinderquill"})
    assert stats.lost_token_ids == ()


def test_render_report_renders_lost_token_ids() -> None:
    stats = (
        WritePathStats(
            system="native",
            corpus_scale="core",
            pages_targeted=2,
            pages_written=1,
            answer_tokens_total=2,
            answer_tokens_retained=1,
            observations_total=2,
            observations_measured=2,
            observations_dropped=1,
            lost_token_ids=("Cinderquill",),
        ),
    )
    rendered = render_report(_minimal_report(stats))
    assert "lost_token_ids" in rendered
    assert "Cinderquill" in rendered


# ---------------------------------------------------------------------------
# Filing loss (issue athenaeum#1830 AC2) -- "Claude's memories versus
# Claude's memories after filing," scored against what the NATIVE writer
# itself retained, not against the full observation stream.
# ---------------------------------------------------------------------------


def test_filing_loss_counter_example_native_drop_is_not_a_filing_loss() -> None:
    """The AC's own counter-example: native drops token X entirely (it is
    never in native_files at all), and the librarian keeps EVERYTHING it
    was actually handed. filing_loss must read 0 lost -- X was never the
    librarian's to lose -- while the ordinary write_path_stats row (scored
    against the full observation stream) still shows total loss 1."""
    observations = [
        _obs("obs-1", "page-a", "fact one", tokens=("TokenX",)),
        _obs("obs-2", "page-b", "fact two", tokens=("TokenY",)),
    ]
    # Native wrote down TokenY but never wrote down TokenX at all.
    native_files = {"note.md": "...TokenY..."}
    # The librarian's compiled store keeps everything native gave it.
    compiled_store = {"page.md": "...TokenY..."}

    total = compute_write_path_stats("athenaeum", "core", observations, compiled_store)
    filing = compute_filing_loss_stats(
        "athenaeum", "core", observations, native_files, compiled_store
    )

    # Total loss: TokenX never reached the compiled store.
    assert total.answer_tokens_total == 2
    assert total.answer_tokens_retained == 1
    assert total.lost_token_ids == ("TokenX",)
    # Filing loss: TokenX was never native's to file, so it is excluded
    # from the denominator entirely -- filing lost NOTHING.
    assert filing.native_tokens_total == 1
    assert filing.filed_tokens_retained == 1
    assert filing.lost_token_ids == ()


def test_filing_loss_counts_a_token_native_kept_but_the_compile_dropped() -> None:
    observations = [_obs("obs-1", "page-a", "fact", tokens=("TokenX",))]
    native_files = {"note.md": "...TokenX..."}
    compiled_store = {"page.md": "no plant here"}

    filing = compute_filing_loss_stats(
        "athenaeum", "core", observations, native_files, compiled_store
    )

    assert filing.native_tokens_total == 1
    assert filing.filed_tokens_retained == 0
    assert filing.lost_token_ids == ("TokenX",)


def test_filing_loss_native_tokens_total_is_zero_reports_none_not_a_fabricated_zero() -> None:
    observations = [_obs("obs-1", "page-a", "fact", tokens=("TokenX",))]
    filing = compute_filing_loss_stats("athenaeum", "core", observations, {}, {})
    assert filing.native_tokens_total == 0
    assert filing.filed_tokens_retained is None


def test_render_report_renders_the_filing_loss_table() -> None:
    filing = (
        FilingLossStats(
            system="athenaeum",
            corpus_scale="core",
            native_tokens_total=2,
            filed_tokens_retained=1,
            lost_token_ids=("TokenX",),
        ),
    )
    report = build_report([], filing_loss_stats=filing)
    rendered = render_report(report)
    assert "Filing loss" in rendered
    assert "| athenaeum | core | 2 | 1 | TokenX |" in rendered


def test_render_report_filing_loss_table_omitted_message_when_empty() -> None:
    rendered = render_report(_minimal_report())
    assert "_no filing-loss data in this run_" in rendered


# ---------------------------------------------------------------------------
# Transient near-term / short-bucket grading (issue athenaeum#1830 operator
# ruling: correct if absent OR short decay bucket / near-term valid_until;
# wrong only if filed durably).
# ---------------------------------------------------------------------------


def test_transient_counter_example_short_bucket_grades_correct_long_horizon_grades_wrong() -> None:
    """The AC's own counter-example: a transient fact filed with a
    long-horizon ``valid_until`` (and no decay bucket) grades WRONG; the
    SAME fact filed with a short decay bucket grades CORRECT."""
    observations = [
        _obs(
            "obs-1",
            "transient-outage",
            "the portal is down this afternoon",
            tokens=("Zephrandil",),
            retain=False,
        ),
    ]

    durable_page = (
        "---\nname: outage note\ntype: reference\nvalid_until: 2099-01-01\n---\n"
        "Zephrandil is down.\n"
    )
    wrong = compute_write_path_stats("athenaeum", "core", observations, {"note.md": durable_page})
    assert wrong.transient_retained == 1, "filed with a long-horizon valid_until -- wrong"

    short_bucket_page = (
        "---\nname: outage note\ntype: reference\nbucket: daily\n---\nZephrandil is down.\n"
    )
    right = compute_write_path_stats(
        "athenaeum", "core", observations, {"note.md": short_bucket_page}
    )
    assert right.transient_retained == 0, "filed with a short decay bucket -- correct"


def test_transient_near_term_valid_until_anchored_to_the_observation_timestamp() -> None:
    """A transient observation timestamped 2026-05-10 (see
    ``corpus._TRANSIENT_OBSERVATIONS``) filed with a valid_until a few days
    later is near-term (correct); one filed with a valid_until months later
    is durable in effect (wrong) -- even though both are technically
    'bounded', only the near one behaves like a reasonable decay."""
    observations = [
        Observation(
            uid="obs-transient-portal-outage",
            page_uid="transient-portal-outage",
            source="sessions",
            timestamp="20260510T091500Z",
            uuid8="7f1a20c4",
            body="Client portal outage",
            answer_tokens=("Zephrandil",),
            retain=False,
        ),
    ]
    near_term_page = "---\nname: note\ntype: reference\nvalid_until: 2026-05-14\n---\nZephrandil\n"
    near = compute_write_path_stats("athenaeum", "core", observations, {"n.md": near_term_page})
    assert near.transient_retained == 0

    far_page = "---\nname: note\ntype: reference\nvalid_until: 2027-05-14\n---\nZephrandil\n"
    far = compute_write_path_stats("athenaeum", "core", observations, {"n.md": far_page})
    assert far.transient_retained == 1


def test_transient_absent_from_every_page_still_grades_correct() -> None:
    observations = [
        _obs("obs-1", "transient-outage", "down today", tokens=("Zephrandil",), retain=False),
    ]
    stats = compute_write_path_stats("athenaeum", "core", observations, {"page.md": "unrelated"})
    assert stats.transient_retained == 0
