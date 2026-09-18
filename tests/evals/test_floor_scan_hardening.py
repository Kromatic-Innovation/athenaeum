# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#1764 -- the three gaps Quine review of PR
athenaeum#1763 (the athenaeum#1761 relevance-floor input) found:

1. A mixed-floor store must yield an ``aborted=True`` PARTIAL report from
   ``main()``, never a bare traceback.
2. ``--floor-scan`` must group ``floor_scan_summary`` by the backend
   recorded on each row rather than pooling FTS5 bm25 scores and vector
   distances into one meaningless blended percentile summary.
3. Before any cell runs, the CLI must compare the requested floors against
   the floor values already recorded in ``--store`` rows and refuse on a
   mismatch unless ``--allow-floor-mismatch`` is passed.

**Deliberately unmarked** (mirrors ``test_north_star_partial_safety.py``'s
own note): every cell here runs through a stub, no live LLM client is
constructed, no ``claude`` binary is spawned, and nothing gates on
``ANTHROPIC_API_KEY`` -- so this module costs no tokens and must run in
``ci.yml``'s default job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.evals import north_star_cli
from tests.evals.containment import GridCell, ResultStore
from tests.evals.north_star_report import RolloutRow, append_rollout_row
from tests.evals.rollout import ALL_ARMS, Arm, RolloutRecord

#: A real probe id from the "core" corpus (mirrors
#: ``test_relevance_floor_input.py``'s own ``_row`` helper) -- used with a
#: replicate index no default grid ever selects (default ``--replicates``
#: is ``"0"``), so a pre-populated row under this identity never collides
#: with a cell key a fresh dispatch produces.
_PROBE_ID = "pto_allowance"
_UNUSED_REPLICATE = 99


def _stub_all_arm_records(probe_id: str, corpus_scale: str) -> dict[str, RolloutRecord]:
    return {
        arm.value: RolloutRecord(
            arm=arm,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale=corpus_scale,
            answer=f"stub answer for {arm.value}",
        )
        for arm in ALL_ARMS
    }


def _pre_existing_row(
    store: ResultStore,
    *,
    relevance_floor_vector: float | None,
    search_backend: str | None = None,
) -> None:
    append_rollout_row(
        store,
        GridCell(
            probe=_PROBE_ID,
            arm=Arm.NONE.value,
            corpus_scale="core",
            replicate=_UNUSED_REPLICATE,
        ),
        RolloutRecord(
            arm=Arm.NONE,
            probe_id=_PROBE_ID,
            probe_class="single_hop",
            corpus_scale="core",
            answer="a",
            relevance_floor_vector=relevance_floor_vector,
            search_backend=search_backend,
        ),
    )


# ---------------------------------------------------------------------------
# 1. A mixed-floor store yields a PARTIAL report from main(), not a crash
# ---------------------------------------------------------------------------


def test_main_mixed_floor_store_yields_partial_report_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives ``main()`` end to end (issue athenaeum#1764 item 1).

    The store already carries one row at ``relevance_floor_vector=None``
    (a floor-off row from an earlier dispatch); this call requests
    ``0.3`` with ``--allow-floor-mismatch`` so the pre-flight check (item
    3, tested below) does not itself refuse first. The two floor values
    then genuinely disagree once the fresh rows land, which is exactly the
    ``north_star_report.build_report`` ``ValueError`` this test pins:
    before the fix, that exception escaped ``main``'s
    ``try``/``except (Exception, KeyboardInterrupt)`` block entirely
    (the block only wraps ``_run_cells``, not ``build_report``) and
    crashed with a bare traceback -- no ``.md`` report was ever written.
    """
    store_path = tmp_path / "store.jsonl"
    out_dir = tmp_path / "measurements"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None)

    monkeypatch.setattr(
        north_star_cli,
        "run_probe_all_arms",
        lambda probe_id, corpus_scale, **_kwargs: _stub_all_arm_records(probe_id, corpus_scale),
    )

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--relevance-floor-vector",
            "0.3",
            "--allow-floor-mismatch",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(out_dir),
        ]
    )

    # Never crashes -- always returns an int -- and a run this method had to
    # abort out of is non-zero.
    assert exit_code == 1
    reports = list(out_dir.glob("north-star-*.md"))
    assert len(reports) == 1, f"expected exactly one report, got {reports}"
    report_text = reports[0].read_text(encoding="utf-8")
    assert "PARTIAL RUN" in report_text
    # Names the values found -- the exact distinct-values repr
    # _pooled_floor_value's ValueError carries -- not merely a loose "0.3"
    # substring a cost figure or correctness score could also satisfy.
    assert "abort_reason:" in report_text
    assert "relevance_floor_vector" in report_text
    assert "[0.3, None]" in report_text


def test_rolloutrecord_round_trip_preserves_search_backend() -> None:
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id="p1",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        search_backend="vector",
    )
    decoded = RolloutRecord.from_payload(record.to_payload())
    assert decoded.search_backend == "vector"


def test_rolloutrecord_from_payload_defaults_search_backend_to_none_for_old_rows() -> None:
    """Back-compat: a payload persisted before issue athenaeum#1764 carries
    no ``search_backend`` key at all."""
    old_payload = RolloutRecord(
        arm=Arm.NONE, probe_id="p1", probe_class="single_hop", corpus_scale="core", answer="a"
    ).to_payload()
    del old_payload["search_backend"]
    decoded = RolloutRecord.from_payload(old_payload)
    assert decoded.search_backend is None


# ---------------------------------------------------------------------------
# 2. floor_scan_summary groups by backend, never blends
# ---------------------------------------------------------------------------


def _row(*, probe_id: str, scores: list[float], backend: str | None) -> RolloutRow:
    return RolloutRow(
        cell=GridCell(probe=probe_id, arm=Arm.PULL.value, corpus_scale="core", replicate=0),
        record=RolloutRecord(
            arm=Arm.PULL,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale="core",
            answer="a",
            retrieval_hit_scores=scores,
            search_backend=backend,
        ),
    )


def test_floor_scan_summary_prints_one_block_per_backend_never_blended() -> None:
    rows = [
        _row(probe_id="p1", scores=[-9.0, -5.0], backend="fts5"),
        _row(probe_id="p2", scores=[0.1, 0.4], backend="vector"),
    ]
    summary = north_star_cli.floor_scan_summary(rows)

    assert "backend=fts5 (lower is better)" in summary
    assert "backend=vector (lower is better)" in summary
    blocks = [b for b in summary.split("\n\n") if b.strip()]
    assert len(blocks) == 2, f"expected 2 separate blocks, got {len(blocks)}: {blocks!r}"
    fts5_block = next(b for b in blocks if "backend=fts5" in b)
    vector_block = next(b for b in blocks if "backend=vector" in b)
    # The whole point: fts5's bm25 numbers must never leak into vector's
    # percentile line, and vice versa.
    assert "min=-9.0000" in fts5_block
    assert "min=-9.0000" not in vector_block
    assert "min=0.1000" in vector_block
    assert "min=0.1000" not in fts5_block


def test_floor_scan_summary_unknown_backend_rows_group_separately() -> None:
    """A row persisted before ``search_backend`` existed (issue
    athenaeum#1764) carries ``None`` -- it must group under its own
    "unknown" block, distinct from a labeled backend's block, rather than
    silently folding into whichever labeled block happens to exist."""
    rows = [
        _row(probe_id="p1", scores=[1.0], backend=None),
        _row(probe_id="p2", scores=[2.0], backend="fts5"),
    ]
    summary = north_star_cli.floor_scan_summary(rows)
    assert "backend=unknown (pre-athenaeum#1764 store)" in summary
    assert "backend=fts5 (lower is better)" in summary
    blocks = [b for b in summary.split("\n\n") if b.strip()]
    assert len(blocks) == 2


def test_floor_scan_summary_empty_store() -> None:
    """Pins the empty-rows path introduced alongside the backend-grouping
    change: no rows means no backend groups, and this function must say so
    rather than print an empty string or a stray blank block."""
    assert north_star_cli.floor_scan_summary([]) == "0 rows in store."


def test_floor_scan_summary_single_unlabeled_backend_stays_pre_1764_compatible() -> None:
    """Every row in an old, single-backend store carries ``search_backend
    = None`` -- the whole store is one "unknown" group, and the counts and
    percentile line this function already printed before issue
    athenaeum#1764 still appear (now inside that one labeled block)."""
    rows = [_row(probe_id="p1", scores=[0.1, 0.5, 0.9], backend=None)]
    summary = north_star_cli.floor_scan_summary(rows)
    assert "1 of 1 rows carry retrieval_hit_scores" in summary
    assert "n=3" in summary
    assert "min=0.1000" in summary
    assert "max=0.9000" in summary


# ---------------------------------------------------------------------------
# 3. check_floor_mismatch and its --allow-floor-mismatch override
# ---------------------------------------------------------------------------


def _floor_row(
    *, relevance_floor_vector: float | None, search_backend: str | None = None
) -> RolloutRow:
    return RolloutRow(
        cell=GridCell(probe=_PROBE_ID, arm=Arm.NONE.value, corpus_scale="core", replicate=0),
        record=RolloutRecord(
            arm=Arm.NONE,
            probe_id=_PROBE_ID,
            probe_class="single_hop",
            corpus_scale="core",
            answer="a",
            relevance_floor_vector=relevance_floor_vector,
            search_backend=search_backend,
        ),
    )


def test_check_floor_mismatch_passes_on_empty_store() -> None:
    assert (
        north_star_cli.check_floor_mismatch(
            [], relevance_floor_vector=0.3, relevance_floor_fts5=None, search_backend="fts5"
        )
        is None
    )


def test_check_floor_mismatch_passes_when_all_none_matches_no_flags() -> None:
    rows = [_floor_row(relevance_floor_vector=None), _floor_row(relevance_floor_vector=None)]
    assert (
        north_star_cli.check_floor_mismatch(
            rows, relevance_floor_vector=None, relevance_floor_fts5=None, search_backend="fts5"
        )
        is None
    )


def test_check_floor_mismatch_passes_when_values_agree() -> None:
    rows = [_floor_row(relevance_floor_vector=0.3), _floor_row(relevance_floor_vector=0.3)]
    assert (
        north_star_cli.check_floor_mismatch(
            rows, relevance_floor_vector=0.3, relevance_floor_fts5=None, search_backend="fts5"
        )
        is None
    )


def test_check_floor_mismatch_refuses_on_disagreement() -> None:
    rows = [_floor_row(relevance_floor_vector=None)]
    mismatch = north_star_cli.check_floor_mismatch(
        rows, relevance_floor_vector=0.3, relevance_floor_fts5=None, search_backend="fts5"
    )
    assert mismatch is not None
    assert "relevance_floor_vector" in mismatch


# ---------------------------------------------------------------------------
# 3b. check_floor_mismatch: --search-backend (Quine must-fix on PR#1765)
# ---------------------------------------------------------------------------


def test_check_floor_mismatch_passes_when_backend_matches() -> None:
    rows = [_floor_row(relevance_floor_vector=None, search_backend="fts5")]
    assert (
        north_star_cli.check_floor_mismatch(
            rows, relevance_floor_vector=None, relevance_floor_fts5=None, search_backend="fts5"
        )
        is None
    )


def test_check_floor_mismatch_refuses_when_backend_disagrees() -> None:
    rows = [_floor_row(relevance_floor_vector=None, search_backend="fts5")]
    mismatch = north_star_cli.check_floor_mismatch(
        rows, relevance_floor_vector=None, relevance_floor_fts5=None, search_backend="vector"
    )
    assert mismatch is not None
    assert "search_backend" in mismatch


def test_check_floor_mismatch_passes_when_stored_backend_is_none() -> None:
    """A pre-athenaeum#1764 store's rows carry ``search_backend=None`` --
    that must never trip the guard on its own, whatever backend this
    dispatch requests."""
    rows = [_floor_row(relevance_floor_vector=None, search_backend=None)]
    assert (
        north_star_cli.check_floor_mismatch(
            rows,
            relevance_floor_vector=None,
            relevance_floor_fts5=None,
            search_backend="vector",
        )
        is None
    )


def test_main_refuses_floor_mismatch_before_running_any_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None)

    called = False

    def _spy(*_args: Any, **_kwargs: Any) -> dict[str, RolloutRecord]:
        nonlocal called
        called = True
        raise AssertionError("run_probe_all_arms must not run on a floor-mismatch refusal")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _spy)

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--relevance-floor-vector",
            "0.3",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert exit_code == 1
    assert not called, "the run spent a cell despite the floor mismatch"
    err = capsys.readouterr().err
    assert "floor mismatch" in err
    assert "--allow-floor-mismatch" in err
    assert not list((tmp_path / "measurements").glob("*.md"))


def test_main_allow_floor_mismatch_override_lets_cells_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--allow-floor-mismatch`` bypasses the item-3 refusal so the cell
    the pre-flight check would otherwise have blocked actually runs --
    proving the flag is wired to the check, not merely parsed and
    ignored."""
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None)

    called = False

    def _stub(probe_id: str, corpus_scale: str, **_kwargs: Any) -> dict[str, RolloutRecord]:
        nonlocal called
        called = True
        return _stub_all_arm_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)

    north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--relevance-floor-vector",
            "0.3",
            "--allow-floor-mismatch",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert called, "--allow-floor-mismatch must let the cell run"


def test_main_refuses_search_backend_mismatch_before_running_any_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The must-fix from Quine review of PR#1765: a store built with one
    search backend, resumed under a different one, is the same silent-mix
    hazard as a floor mismatch and must refuse the same way."""
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None, search_backend="fts5")

    called = False

    def _spy(*_args: Any, **_kwargs: Any) -> dict[str, RolloutRecord]:
        nonlocal called
        called = True
        raise AssertionError("run_probe_all_arms must not run on a backend-mismatch refusal")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _spy)

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--search-backend",
            "vector",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert exit_code == 1
    assert not called, "the run spent a cell despite the backend mismatch"
    err = capsys.readouterr().err
    assert "search_backend" in err
    assert "--allow-floor-mismatch" in err


def test_main_allow_floor_mismatch_override_also_covers_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flag covers both hazards: --allow-floor-mismatch also bypasses a
    --search-backend disagreement, not only a floor disagreement."""
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None, search_backend="fts5")

    called = False

    def _stub(probe_id: str, corpus_scale: str, **_kwargs: Any) -> dict[str, RolloutRecord]:
        nonlocal called
        called = True
        return _stub_all_arm_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)

    north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--search-backend",
            "vector",
            # Issue athenaeum#1787: a vector dispatch is scoped to
            # core+medium -- named explicitly here since this test is about
            # the floor/backend-mismatch bypass, not the scale scope.
            "--corpus-scales",
            "core",
            "--allow-floor-mismatch",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert called, "--allow-floor-mismatch must also let a backend-mismatched run proceed"


def test_main_none_backend_store_passes_regardless_of_requested_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-athenaeum#1764 store (search_backend=None on every row) must
    never trip the backend half of the guard on its own."""
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None, search_backend=None)

    called = False

    def _stub(probe_id: str, corpus_scale: str, **_kwargs: Any) -> dict[str, RolloutRecord]:
        nonlocal called
        called = True
        return _stub_all_arm_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--search-backend",
            "vector",
            # Issue athenaeum#1787: a vector dispatch is scoped to
            # core+medium -- named explicitly here since this test is about
            # the None-backend back-compat behaviour, not the scale scope.
            "--corpus-scales",
            "core",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert called, "a None-backend store must not trip the pre-flight guard on its own"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 4. The mixed-floor recovery path itself must not crash main() a second
#    time if IT also raises (Quine "should" on PR#1765)
# ---------------------------------------------------------------------------


def test_main_recovery_build_report_failure_still_writes_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the recovery ``build_report(..., pool_floor_values=False)`` call
    itself raises (a bug in this recovery path, or a store row otherwise
    unparseable by ``build_report``), ``main()`` must still write a PARTIAL
    report and exit non-zero, never crash with a bare traceback a second
    time."""
    store_path = tmp_path / "store.jsonl"
    out_dir = tmp_path / "measurements"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None)

    monkeypatch.setattr(
        north_star_cli,
        "run_probe_all_arms",
        lambda probe_id, corpus_scale, **_kwargs: _stub_all_arm_records(probe_id, corpus_scale),
    )

    real_build_report = north_star_cli.build_report
    call_count = 0

    def _flaky_build_report(rows: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        # 1st call: the normal attempt, raises MixedFloorError for real (the
        # genuine mixed-floor rows this dispatch produces). 2nd call: the
        # recovery attempt (pool_floor_values=False) -- fail IT too, to
        # exercise the recovery path's own except clause. 3rd call: the
        # recovery's own fallback (rows=()) -- let it through for real, so a
        # report actually gets written.
        if call_count == 2:
            raise RuntimeError("synthetic recovery-path failure")
        return real_build_report(rows, **kwargs)

    monkeypatch.setattr(north_star_cli, "build_report", _flaky_build_report)

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--relevance-floor-vector",
            "0.3",
            "--allow-floor-mismatch",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(out_dir),
        ]
    )

    assert exit_code == 1
    reports = list(out_dir.glob("north-star-*.md"))
    assert len(reports) == 1, f"expected exactly one report, got {reports}"
    report_text = reports[0].read_text(encoding="utf-8")
    assert "PARTIAL RUN" in report_text
    assert "synthetic recovery-path failure" in report_text


def test_main_floor_mismatch_refusal_preempts_dry_run_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins where the item-3 check sits relative to ``--dry-run``: BEFORE
    the wall-clock/ceiling/price projection lines, not after. This differs
    from the price-refusal convention this module documents elsewhere ("all
    three projection lines have printed on the dry-run path, so the
    operator sees the wall clock, the ceiling and the price rather than
    only the refusal") -- deliberately: a floor mismatch is a sanity
    refusal that belongs beside ``--floor-scan``'s "before any grid-sizing
    or spend validation" placement, not beside the spend-refusal path,
    because grid-sizing/pricing has nothing to say about whether resuming
    this store under these flags is even meaningful."""
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    _pre_existing_row(store, relevance_floor_vector=None)

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--dry-run",
            "--store",
            str(store_path),
            "--relevance-floor-vector",
            "0.3",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "projected wall clock" not in out
    assert "token ceiling" not in out
