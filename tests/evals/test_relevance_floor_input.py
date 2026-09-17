# SPDX-License-Identifier: Apache-2.0
"""Tests for the relevance-floor harness input (issue athenaeum#1761).

Covers, in order: the CLI's ``athenaeum.yaml`` writer shape, both real
recall-path read sites picking the written config up (fts5 -- always
available, no optional ``[vector]`` extra needed to run CI), the
``RolloutRecord`` round trip for the three new fields, ``north_star_report``'s
mixed-floor pooling refusal, and the ``evals.yml`` wiring (grep pattern
mirrors ``tests/evals/test_north_star_max_tokens.py::
test_evals_yml_wires_the_max_tokens_input_to_the_flag``).

UNMARKED -- every test here is offline: a real materialized corpus and a
real fts5 index, no network call, no subprocess spawn, no token spent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.evals import north_star_cli
from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import (
    RolloutRow,
    append_rollout_row,
    build_report,
    load_rollout_rows,
)
from tests.evals.rollout import (
    RECALL_TOOL_NAME,
    Arm,
    RolloutRecord,
    run_pull_api,
    run_push_breadcrumb_pull_api,
)

from .test_rollout_api_mode import (
    _QueuedApiClient,
    _RecordedTurn,
    _text_block,
    _tool_result_texts,
    _tool_use_block,
)

# ---------------------------------------------------------------------------
# 1. write_relevance_floor_config -- shape and no-op contract
# ---------------------------------------------------------------------------


def test_write_relevance_floor_config_shape(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    north_star_cli.write_relevance_floor_config(
        root,
        relevance_floor_vector=0.42,
        relevance_floor_fts5=-3.5,
        search_backend="vector",
    )
    config = yaml.safe_load((root / "athenaeum.yaml").read_text(encoding="utf-8"))
    assert config["search_backend"] == "vector"
    floor = config["recall"]["relevance_floor"]
    assert floor["vector"] == 0.42
    assert floor["fts5"] == -3.5
    assert floor["push"]["vector"] == 0.42
    assert floor["push"]["fts5"] == -3.5


def test_write_relevance_floor_config_one_backend_only(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    north_star_cli.write_relevance_floor_config(
        root, relevance_floor_vector=0.1, relevance_floor_fts5=None, search_backend="vector"
    )
    floor = yaml.safe_load((root / "athenaeum.yaml").read_text(encoding="utf-8"))["recall"][
        "relevance_floor"
    ]
    assert floor["vector"] == 0.1
    assert "fts5" not in floor
    assert floor["push"] == {"vector": 0.1}


def test_write_relevance_floor_config_noop_when_both_none(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    north_star_cli.write_relevance_floor_config(
        root, relevance_floor_vector=None, relevance_floor_fts5=None, search_backend="fts5"
    )
    # No-op means no-op: the directory itself must not appear either, so a
    # default (no-floor) dispatch leaves zero trace of this mechanism.
    assert not root.exists()


# ---------------------------------------------------------------------------
# 2. Both api-mode recall read sites honour a floor written this way
# ---------------------------------------------------------------------------


def _pull_probe(corpus):
    return next(p for p in corpus.probes if p.id == "pto_allowance")


def test_run_pull_api_honours_a_written_fts5_floor(tmp_path: Path) -> None:
    """AC: the API-mode PULL tool executor's ``recall_search`` call reads
    ``athenaeum.yaml`` from the knowledge root (``wiki_root.parent``) and
    applies the configured floor -- an extreme ``fts5`` floor (mirrors
    ``tests/test_shell_hooks.py``'s own ``-999.0`` choice for the same
    reason: BM25 rank on a tiny fixture corpus never approaches it) drops
    every real hit, so the tool call comes back empty instead of the real
    match.
    """
    from athenaeum.search import get_backend

    corpus = build_corpus("core")
    probe = _pull_probe(corpus)
    wiki_root = corpus.materialize(tmp_path)
    cache_dir = tmp_path / "cache"
    get_backend("fts5").build_index(wiki_root, cache_dir)

    north_star_cli.write_relevance_floor_config(
        tmp_path,
        relevance_floor_vector=None,
        relevance_floor_fts5=-999.0,
        search_backend="fts5",
    )

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_recall_1",
                    name=RECALL_TOOL_NAME,
                    input={"query": probe.query},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("I don't know.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)

    record = run_pull_api(
        probe,
        wiki_root,
        cache_dir,
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="fts5",
    )

    tool_result = _tool_result_texts(record, "toolu_recall_1")
    assert "No wiki pages matched" in tool_result, f"floor did not apply: {tool_result!r}"


def test_run_push_breadcrumb_pull_api_honours_a_written_fts5_floor(tmp_path: Path) -> None:
    """Same AC as above, for the other api-mode recall executor
    (``run_push_breadcrumb_pull_api``). ``context_fn`` is stubbed to skip
    the real breadcrumb-hook subprocess -- that assembly path is pinned
    separately, at the hook level, by
    ``tests/test_shell_hooks.py::TestUserPromptRecall::
    test_breadcrumb_hook_honours_a_vector_floor_written_by_the_cli``; this
    test isolates the SECOND read site (the ``recall`` tool executor) that
    same CLI-written file must also reach.
    """
    from athenaeum.search import get_backend

    corpus = build_corpus("core")
    probe = _pull_probe(corpus)
    knowledge_root = tmp_path
    wiki_root = corpus.materialize(knowledge_root)
    cache_dir = knowledge_root / "cache"
    hook_home = knowledge_root / "hook_home"
    get_backend("fts5").build_index(wiki_root, cache_dir)

    north_star_cli.write_relevance_floor_config(
        knowledge_root,
        relevance_floor_vector=None,
        relevance_floor_fts5=-999.0,
        search_backend="fts5",
    )

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_recall_1", name=RECALL_TOOL_NAME, input={"query": probe.query}
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("I don't know.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)

    record = run_push_breadcrumb_pull_api(
        probe,
        knowledge_root,
        hook_home,
        cache_dir,
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="fts5",
        context_fn=lambda *_args, **_kwargs: "",
    )

    tool_result = _tool_result_texts(record, "toolu_recall_1")
    assert "No wiki pages matched" in tool_result, f"floor did not apply: {tool_result!r}"


# ---------------------------------------------------------------------------
# 3. RolloutRecord round trip
# ---------------------------------------------------------------------------


def test_rolloutrecord_round_trip_with_floor_and_score_fields() -> None:
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id="p1",
        probe_class="single_hop",
        corpus_scale="core",
        answer="answer",
        mode="api",
        relevance_floor_vector=0.3,
        relevance_floor_fts5=-2.0,
        retrieval_hit_scores=[0.1, 0.4, 0.9],
    )
    decoded = RolloutRecord.from_payload(record.to_payload())
    assert decoded.relevance_floor_vector == 0.3
    assert decoded.relevance_floor_fts5 == -2.0
    assert decoded.retrieval_hit_scores == [0.1, 0.4, 0.9]


def test_rolloutrecord_from_payload_defaults_new_fields_to_none_for_old_rows() -> None:
    """Back-compat: a payload persisted before issue athenaeum#1761 carries
    none of the three new keys at all."""
    old_payload = RolloutRecord(
        arm=Arm.NONE, probe_id="p1", probe_class="single_hop", corpus_scale="core", answer="a"
    ).to_payload()
    for key in ("relevance_floor_vector", "relevance_floor_fts5", "retrieval_hit_scores"):
        del old_payload[key]
    decoded = RolloutRecord.from_payload(old_payload)
    assert decoded.relevance_floor_vector is None
    assert decoded.relevance_floor_fts5 is None
    assert decoded.retrieval_hit_scores is None


# ---------------------------------------------------------------------------
# 4. north_star_report refuses to pool differing floor values
# ---------------------------------------------------------------------------


def _row(arm: Arm, *, floor_vector: float | None) -> RolloutRow:
    # A real probe id (from the real "core" corpus) is required past the
    # raising test below: build_report's own compute_group_stats looks the
    # probe up in the corpus to grade correctness.
    record = RolloutRecord(
        arm=arm,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        mode="api",
        relevance_floor_vector=floor_vector,
    )
    return RolloutRow(
        cell=GridCell(probe="pto_allowance", arm=arm.value, corpus_scale="core", replicate=0),
        record=record,
    )


def test_build_report_refuses_to_pool_differing_relevance_floor_vector() -> None:
    rows = [_row(Arm.NONE, floor_vector=None), _row(Arm.PULL, floor_vector=0.3)]
    with pytest.raises(ValueError, match="relevance_floor_vector"):
        build_report(rows)


def test_build_report_pools_a_uniform_floor_value_without_raising() -> None:
    rows = [_row(Arm.NONE, floor_vector=0.3), _row(Arm.PULL, floor_vector=0.3)]
    report = build_report(rows)
    assert report.relevance_floor_vector == 0.3


def test_build_report_all_none_floor_is_the_pre_1761_behavior() -> None:
    rows = [_row(Arm.NONE, floor_vector=None), _row(Arm.PULL, floor_vector=None)]
    report = build_report(rows)
    assert report.relevance_floor_vector is None


def test_build_report_empty_rows_does_not_raise() -> None:
    report = build_report([])
    assert report.relevance_floor_vector is None
    assert report.relevance_floor_fts5 is None


def test_resultstore_round_trip_preserves_floor_fields(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "store.jsonl")
    cell = GridCell(probe="p1", arm=Arm.PULL.value, corpus_scale="core", replicate=0)
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id="p1",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        mode="api",
        relevance_floor_vector=0.5,
        relevance_floor_fts5=-1.0,
        retrieval_hit_scores=[0.2],
    )
    append_rollout_row(store, cell, record)

    [row] = load_rollout_rows(store)
    assert row.cell == cell
    assert row.record.relevance_floor_vector == 0.5
    assert row.record.relevance_floor_fts5 == -1.0
    assert row.record.retrieval_hit_scores == [0.2]


# ---------------------------------------------------------------------------
# 5. evals.yml wiring (mirrors test_north_star_max_tokens.py's own idiom)
# ---------------------------------------------------------------------------


def test_evals_yml_wires_the_relevance_floor_inputs_to_the_flags() -> None:
    repo_root = Path(north_star_cli.__file__).resolve().parents[2]
    evals_yml = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")

    assert "north_star_relevance_floor_vector:" in evals_yml
    assert "north_star_relevance_floor_fts5:" in evals_yml
    assert (
        "NORTH_STAR_RELEVANCE_FLOOR_VECTOR: "
        "${{ github.event.inputs.north_star_relevance_floor_vector }}"
    ) in evals_yml
    assert (
        "NORTH_STAR_RELEVANCE_FLOOR_FTS5: "
        "${{ github.event.inputs.north_star_relevance_floor_fts5 }}"
    ) in evals_yml
    assert (
        "RELEVANCE_FLOOR_VECTOR_FLAG=(--relevance-floor-vector "
        '"$NORTH_STAR_RELEVANCE_FLOOR_VECTOR")'
    ) in evals_yml
    assert (
        'RELEVANCE_FLOOR_FTS5_FLAG=(--relevance-floor-fts5 "$NORTH_STAR_RELEVANCE_FLOOR_FTS5")'
        in evals_yml
    )
    assert '"${RELEVANCE_FLOOR_VECTOR_FLAG[@]}"' in evals_yml
    assert '"${RELEVANCE_FLOOR_FTS5_FLAG[@]}"' in evals_yml
    # Blank input must leave both flags off entirely.
    assert 'if [ -n "${NORTH_STAR_RELEVANCE_FLOOR_VECTOR:-}" ]; then' in evals_yml
    assert 'if [ -n "${NORTH_STAR_RELEVANCE_FLOOR_FTS5:-}" ]; then' in evals_yml
    # AC2: the new inputs must not have grown the job a push trigger -- the
    # north-star job stays dispatch-only and opt-in, same gate as before.
    assert (
        "github.event_name == 'workflow_dispatch' " "&& github.event.inputs.north_star == 'true'"
    ) in evals_yml


# ---------------------------------------------------------------------------
# 6. --floor-scan
# ---------------------------------------------------------------------------


def test_floor_scan_summary_reports_no_scores_for_a_pre_1761_store() -> None:
    rows = [
        RolloutRow(
            cell=GridCell(probe="p1", arm=Arm.PULL.value, corpus_scale="core", replicate=0),
            record=RolloutRecord(
                arm=Arm.PULL,
                probe_id="p1",
                probe_class="single_hop",
                corpus_scale="core",
                answer="a",
            ),
        )
    ]
    summary = north_star_cli.floor_scan_summary(rows)
    assert "0 of 1 rows carry retrieval_hit_scores" in summary
    assert "no scores to summarise" in summary


def test_floor_scan_summary_reports_distribution_when_scores_present() -> None:
    rows = [
        RolloutRow(
            cell=GridCell(probe="p1", arm=Arm.PULL.value, corpus_scale="core", replicate=0),
            record=RolloutRecord(
                arm=Arm.PULL,
                probe_id="p1",
                probe_class="single_hop",
                corpus_scale="core",
                answer="a",
                retrieval_hit_scores=[0.1, 0.5, 0.9],
            ),
        )
    ]
    summary = north_star_cli.floor_scan_summary(rows)
    assert "1 of 1 rows carry retrieval_hit_scores" in summary
    assert "n=3" in summary
    assert "min=0.1000" in summary
    assert "max=0.9000" in summary


def test_main_floor_scan_exits_zero_and_makes_no_client(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "store.jsonl"
    store = ResultStore(store_path)
    cell = GridCell(probe="p1", arm=Arm.PULL.value, corpus_scale="core", replicate=0)
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id="p1",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        retrieval_hit_scores=[0.2, 0.4],
    )
    append_rollout_row(store, cell, record)

    rc = north_star_cli.main(["--floor-scan", str(store_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 of 1 rows carry retrieval_hit_scores" in out
