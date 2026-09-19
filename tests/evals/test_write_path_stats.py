# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the Phase 2 write-path report additions (issue
athenaeum#1726 AC4): :func:`tests.evals.north_star_report.compute_write_path_stats`
and its rendering in :func:`tests.evals.north_star_report.render_report`.

Pure computation throughout -- no corpus build, no subprocess, no model
client. Synthetic :class:`~tests.evals.corpus.Observation` instances and a
plain ``{path: text}`` store mapping are enough to exercise every branch.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from tests.evals.corpus import (
    GENERATOR_VERSION,
    Observation,
    ObservationStream,
    build_corpus,
    generate_core_observations,
    generate_transient_observations,
)
from tests.evals.north_star_cli import (
    _phase2_append_rows,
    _write_path_stats_row,
    load_phase2_results,
)
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
    expected_bucket: str = "",
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
        expected_bucket=expected_bucket,
    )


def _page(*, bucket: str = "", body: str, name: str = "note") -> str:
    """A compiled page as it lands in a store: frontmatter plus body.

    ``bucket=""`` omits the key entirely -- the shape a page with no decay
    vocabulary at all has, which is what the native arm's memory files look
    like to this scanner.
    """
    front = [f"name: {name}", "type: reference"]
    if bucket:
        front.append(f"bucket: {bucket}")
    return "---\n" + "\n".join(front) + "\n---\n" + body + "\n"


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

    assert "| athenaeum | core | 2 | 2 | 2 | 2 | 0 | n/a | n/a | 0 | 3 | 2 | 0 |" in rendered
    assert "| native | core | 2 | n/a | 0 | n/a | 0 | n/a | n/a | 0 | 3 | 0 | n/a |" in rendered


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
    assert "| athenaeum | core | 1 | 1 | 1 | 1 | 3 | 1 | n/a | 0 | 2 | 1 | 0 |" in rendered
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


# ---------------------------------------------------------------------------
# Decay CORRECTNESS (issue athenaeum#1841) -- `expected_bucket` ground truth,
# `decay_correct`, `durable_overdecayed`, and the widened table.
#
# The athenaeum#1830 rule above is deliberately untouched: it still grades a
# transient token correct as soon as SOME short decay was picked. These tests
# pin the SECOND grading, which asks whether the RIGHT one was.
# ---------------------------------------------------------------------------

_TRANSIENT = _obs(
    "obs-transient-portal-outage",
    "transient-portal-outage",
    "the portal is down this afternoon",
    tokens=("Zephrandil",),
    retain=False,
    expected_bucket="daily",
)


def test_corpus_transients_carry_the_expected_decay_bucket() -> None:
    """AC1's ground truth, read off the corpus itself rather than restated:
    an outage and a point-in-time status are `daily` facts, a
    backlog-pass-scoped instruction is `weekly`."""
    by_token = {
        token: obs.expected_bucket
        for obs in generate_transient_observations()
        for token in obs.answer_tokens
    }

    assert by_token == {
        "Zephrandil": "daily",
        "Marrowglint": "daily",
        "Ossivane": "weekly",
    }


def test_durable_observations_record_no_bucket_expectation() -> None:
    """A page-derived observation's correct filing is "retained", not
    "decayed on some horizon" -- so it carries no expectation, and the
    default must stay the empty string rather than a guessed bucket."""
    corpus = build_corpus(scale="core", seed=20260908)
    stream = generate_core_observations(corpus.pages, corpus.probes, seed=20260908, scale="core")

    durable = [obs for obs in stream.observations if obs.retain]
    assert durable, "core corpus must produce durable observations"
    assert {obs.expected_bucket for obs in durable} == {""}


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_expected_bucket_never_reaches_the_materialised_stream(tmp_path: Path) -> None:
    """AC2, asserted rather than assumed.

    `ObservationStream.materialize` writes only `obs.body` under a
    `timestamp`/`uuid8`-derived filename, so the new field cannot perturb a
    byte of the emitted tree. Proved by materialising the real stream twice
    -- once as generated, once with every `expected_bucket` stripped back to
    the pre-athenaeum#1841 empty default -- and comparing file-for-file
    digests. Both session sizes are covered because bundling changes which
    observations share a file, not what is written from each.

    Measured against develop fb69628 before the field existed: both trees
    digest identically to the manifest captured there.
    """
    corpus = build_corpus(scale="core", seed=20260908)
    stream = generate_core_observations(corpus.pages, corpus.probes, seed=20260908, scale="core")
    stripped = ObservationStream(
        observations=[replace(obs, expected_bucket="") for obs in stream.observations],
        seed=stream.seed,
        scale=stream.scale,
    )
    assert any(obs.expected_bucket for obs in stream.observations), (
        "fixture must actually differ from the stripped stream, or this proves nothing"
    )

    for session_size in (1, 5):
        with_field = tmp_path / f"with-{session_size}"
        without_field = tmp_path / f"without-{session_size}"
        stream.materialize(with_field, session_size=session_size)
        stripped.materialize(without_field, session_size=session_size)

        assert _tree_digest(with_field) == _tree_digest(without_field)


def test_corpus_fingerprint_is_unchanged_and_generator_version_does_not_bump() -> None:
    """AC2's other half: `Corpus.fingerprint` digests PAGES, never
    observations, so widening an observation cannot move it -- and
    `GENERATOR_VERSION` must therefore stay put.

    The literal below was measured on develop fb69628 BEFORE
    `expected_bucket` was added (`build_corpus(scale="core",
    seed=20260908).fingerprint()`), which is what makes this a before/after
    comparison rather than a restatement of current behaviour. It moves only
    when a core page or `GENERATOR_VERSION` really does change -- see the
    same pin's provenance comment in `tests/test_eval_corpus_generator.py`.

    Re-pinned under athenaeum#1839: `core/16-redundancy.yaml` added nine new
    core pages (three redundancy probe pairs plus a negative control each),
    which shifts `Corpus.fingerprint()` since it hashes every page's
    rendered markdown -- same class of expected change as every prior core-
    page addition. `GENERATOR_VERSION` did not move (still 4, bumped by the
    already-landed athenaeum#1843); floor/stats tables recorded against the
    prior literal are not comparable to runs against this one. Re-derived by
    running `build_corpus(scale="core", seed=20260908).fingerprint()` in two
    separate processes on this branch, both yielding the literal below.
    Re-derived a second time in the same PR after the Rivencourt alias/tag
    fix described in
    `tests/test_eval_corpus_generator.py::test_xlarge_scale_is_pinned`'s own
    provenance comment.
    """
    corpus = build_corpus(scale="core", seed=20260908)

    assert corpus.fingerprint() == "edcd8dd3286d0135"
    assert GENERATOR_VERSION == 4


def test_decay_correct_counts_a_transient_filed_under_the_expected_bucket() -> None:
    store = {"note.md": _page(bucket="daily", body="Zephrandil is down.")}

    stats = compute_write_path_stats("athenaeum", "core", [_TRANSIENT], store)

    assert stats.transient_total == 1
    assert stats.decay_correct == 1
    assert stats.transient_retained == 0


def test_decay_correct_counter_example_weekly_when_daily_was_expected() -> None:
    """The AC's own counter-example, and the one fixture that shows the two
    columns DISAGREE: `weekly` is a reasonable decay, so the unchanged
    athenaeum#1830 rule still grades this correct (`transient_retained == 0`)
    -- but `daily` was expected, so it counts toward `transient_total` and
    NOT toward `decay_correct`."""
    store = {"note.md": _page(bucket="weekly", body="Zephrandil is down.")}

    stats = compute_write_path_stats("athenaeum", "core", [_TRANSIENT], store)

    assert stats.transient_total == 1
    assert stats.decay_correct == 0, "weekly is not daily -- the decay is wrong"
    assert stats.transient_retained == 0, (
        "athenaeum#1830's rule is unchanged: a short bucket is still a reasonable decay"
    )


def test_decay_correct_requires_every_page_carrying_the_token_to_agree() -> None:
    """One durable copy is still a durable filing -- the same all-pages rule
    `_transient_token_handled_correctly` holds."""
    store = {
        "short.md": _page(bucket="daily", body="Zephrandil is down.", name="short"),
        "long.md": _page(bucket="durable", body="Zephrandil is down.", name="long"),
    }

    stats = compute_write_path_stats("athenaeum", "core", [_TRANSIENT], store)

    assert stats.decay_correct == 0


def test_decay_correct_is_none_when_the_store_declares_no_bucket_vocabulary() -> None:
    """The native arm: plain memory files with no frontmatter at all. There
    is no decay to grade, so the column must read "not measurable", never a
    fabricated 0 -- the same convention `transient_retained` holds."""
    store = {"native-memory.md": "Some note mentioning Zephrandil."}

    stats = compute_write_path_stats("native", "core", [_TRANSIENT], store)

    assert stats.decay_correct is None
    assert stats.transient_total == 1


def test_decay_correct_is_none_when_no_transient_carries_an_expectation() -> None:
    observations = [
        _obs("obs-1", "transient-outage", "down today", tokens=("Zephrandil",), retain=False),
    ]
    store = {"note.md": _page(bucket="daily", body="Zephrandil is down.")}

    stats = compute_write_path_stats("athenaeum", "core", observations, store)

    assert stats.decay_correct is None


def test_a_transient_with_no_expectation_is_never_counted_decay_correct() -> None:
    """A mixed stream: one transient carries ground truth, one does not.
    The un-expected token cannot be graded, so it must not be counted
    correct just because it happens to sit on a short-bucket page -- that
    would silently inflate the column against `transient_total`."""
    ungraded = _obs(
        "obs-2",
        "transient-export-status",
        "the nightly export finished",
        tokens=("Marrowglint",),
        retain=False,
    )
    store = {
        "a.md": _page(bucket="daily", body="Zephrandil is down.", name="a"),
        "b.md": _page(bucket="daily", body="Marrowglint batch finished.", name="b"),
    }

    stats = compute_write_path_stats("athenaeum", "core", [_TRANSIENT, ungraded], store)

    assert stats.transient_total == 2
    assert stats.decay_correct == 1


def test_a_dropped_transient_is_not_decay_correct_but_is_still_handled_correctly() -> None:
    """Absence is the athenaeum#1830 rule's best outcome and is scored by
    `transient_retained`; it is not a CORRECT DECAY, because no decay was
    picked at all. The two columns are reported side by side precisely so
    this case is legible instead of being averaged away."""
    store = {"unrelated.md": _page(bucket="durable", body="nothing planted here")}

    stats = compute_write_path_stats("athenaeum", "core", [_TRANSIENT], store)

    assert stats.transient_retained == 0
    assert stats.decay_correct == 0


def test_durable_token_on_a_daily_page_is_overdecayed() -> None:
    """The AC's over-decay counter-example: the SAME durable token filed
    `daily` is a fact the store has scheduled to forget; filed `durable` it
    is not."""
    observations = [_obs("obs-1", "page-a", "durable fact", tokens=("Cinderquill",))]

    overdecayed = compute_write_path_stats(
        "athenaeum",
        "core",
        observations,
        {"page-a.md": _page(bucket="daily", body="Cinderquill is the policy owner.")},
    )
    assert overdecayed.durable_overdecayed == 1
    assert overdecayed.answer_tokens_retained == 1, "over-decay is not loss -- it is still there"

    filed_durable = compute_write_path_stats(
        "athenaeum",
        "core",
        observations,
        {"page-a.md": _page(bucket="durable", body="Cinderquill is the policy owner.")},
    )
    assert filed_durable.durable_overdecayed == 0


def test_durable_overdecayed_ignores_pages_with_no_bucket_and_transient_tokens() -> None:
    """A page with no decay vocabulary makes no decay claim, so it is not an
    over-decay; and a TRANSIENT token on a `daily` page is the correct
    filing, never an over-decay -- that column is durable-only."""
    observations = [
        _obs("obs-1", "page-a", "durable fact", tokens=("Cinderquill",)),
        _TRANSIENT,
    ]
    store = {
        "page-a.md": _page(body="Cinderquill is the policy owner.", name="page-a"),
        "note.md": _page(bucket="daily", body="Zephrandil is down."),
    }

    stats = compute_write_path_stats("athenaeum", "core", observations, store)

    assert stats.durable_overdecayed == 0
    assert stats.decay_correct == 1


def _write_path_table(rendered: str) -> tuple[str, str, str]:
    """The header, separator and first data row of the Phase 2 write-path
    table, located by the header's own first column rather than by index."""
    lines = rendered.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| system | corpus_scale | pages_targeted |"):
            return lines[i], lines[i + 1], lines[i + 2]
    raise AssertionError("write-path table header not found in rendered report")


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def test_render_report_emits_the_decay_columns_and_explains_them() -> None:
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
            decay_correct=2,
            durable_overdecayed=1,
        ),
    )
    rendered = render_report(_minimal_report(stats))

    assert "transient_retained | decay_correct | durable_overdecayed" in rendered
    assert "| athenaeum | core | 1 | 1 | 1 | 1 | 3 | 1 | 2 | 1 | 2 | 1 | 0 |" in rendered
    # The legend must name `durable_overdecayed` as the SECOND
    # lower-is-better column, not leave a reader to infer its direction from
    # the older `transient_retained` sentence.
    assert "`durable_overdecayed` is the SECOND lower-is-better column" in rendered
    assert "HIGHER is better" in rendered


def test_render_report_renders_unmeasurable_decay_correct_as_na() -> None:
    stats = (
        WritePathStats(
            system="native",
            corpus_scale="core",
            pages_targeted=1,
            pages_written=1,
            answer_tokens_total=1,
            answer_tokens_retained=1,
            observations_total=1,
            observations_measured=1,
            observations_dropped=0,
            transient_total=3,
            transient_retained=3,
            decay_correct=None,
            durable_overdecayed=0,
        ),
    )
    rendered = render_report(_minimal_report(stats))

    assert "| native | core | 1 | 1 | 1 | 1 | 3 | 3 | n/a | 0 | 1 | 1 | 0 |" in rendered


def test_write_path_table_header_separator_and_row_have_the_same_cell_count() -> None:
    """Widening the table by two columns means widening the `| --- |`
    separator by two cells too -- a markdown table whose separator is short
    renders as literal text, and no assertion on the ROW alone would catch
    it."""
    stats = (
        WritePathStats(
            system="athenaeum",
            corpus_scale="core",
            pages_targeted=1,
            pages_written=1,
            answer_tokens_total=1,
            answer_tokens_retained=1,
            observations_total=1,
            observations_measured=1,
            observations_dropped=0,
            transient_total=1,
            transient_retained=0,
            decay_correct=1,
            durable_overdecayed=0,
        ),
    )
    header, separator, row = _write_path_table(render_report(_minimal_report(stats)))

    assert _cells(header)[7:12] == [
        "transient_retained",
        "decay_correct",
        "durable_overdecayed",
        "observations_total",
        "observations_measured",
    ]
    assert len(_cells(header)) == 15
    assert len(_cells(separator)) == len(_cells(header))
    assert len(_cells(row)) == len(_cells(header))
    assert set(_cells(separator)) == {"---"}


def test_decay_columns_survive_the_phase2_sibling_store_round_trip(tmp_path: Path) -> None:
    """The report only ever sees a row that went through the Phase 2 sibling
    JSONL, so a field the reader does not rebuild is a field the rendered
    table can never show. A row written before athenaeum#1841 (no decay keys
    at all) must still load, degrading to `n/a`/0 rather than being skipped.
    """
    store = tmp_path / "phase2.jsonl"
    measured = WritePathStats(
        system="athenaeum",
        corpus_scale="medium",
        pages_targeted=1,
        pages_written=1,
        answer_tokens_total=1,
        answer_tokens_retained=1,
        observations_total=2,
        observations_measured=1,
        observations_dropped=0,
        transient_total=3,
        transient_retained=0,
        decay_correct=3,
        durable_overdecayed=1,
    )
    legacy = {
        "kind": "write_path",
        "system": "native",
        "corpus_scale": "medium",
        "pages_targeted": 1,
        "pages_written": 1,
        "answer_tokens_total": 1,
        "answer_tokens_retained": 1,
        "observations_total": 1,
        "observations_measured": 1,
        "observations_dropped": 0,
    }
    _phase2_append_rows(store, [_write_path_stats_row(measured), legacy])

    stats, _costs, _filing = load_phase2_results(store)
    by_system = {row.system: row for row in stats}

    assert by_system["athenaeum"].decay_correct == 3
    assert by_system["athenaeum"].durable_overdecayed == 1
    assert by_system["native"].decay_correct is None
    assert by_system["native"].durable_overdecayed == 0
