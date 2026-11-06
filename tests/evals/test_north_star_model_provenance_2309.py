# SPDX-License-Identifier: Apache-2.0
"""The model behind a grid run is recorded WITH the data -- issue athenaeum#2309.

Before this issue the model appeared nowhere in the result store and nowhere
in the rendered report header: it was recoverable only from the dispatch
inputs of the Actions run that produced the artifact. That made a noise floor
unverifiable from the artifact alone, and made "measured under the same model"
-- the precondition athenaeum#2310's conditions may only be read under -- a
claim nobody could check.

UNMARKED -- no network, no credential, no model call.
"""

from __future__ import annotations

from tests.evals.containment import GridCell
from tests.evals.north_star_report import (
    RolloutRow,
    _pooled_model,
    build_report,
    render_report,
)
from tests.evals.rollout import Arm, RolloutRecord


def _row(model: str, *, probe: str = "pto_allowance") -> RolloutRow:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id=probe,
        probe_class="single_hop",
        corpus_scale="core",
        answer="an answer",
        model=model,
    )
    cell = GridCell(probe=probe, arm="none", corpus_scale="core", replicate=0)
    return RolloutRow(cell=cell, record=record)


def test_record_defaults_to_unstamped_not_to_a_guessed_model() -> None:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
    )
    assert record.model == ""


def test_payload_roundtrip_carries_the_model() -> None:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        model="claude-sonnet-5",
    )
    assert RolloutRecord.from_payload(record.to_payload()).model == "claude-sonnet-5"


def test_a_row_persisted_before_the_field_existed_decodes_as_unstamped() -> None:
    payload = RolloutRecord(
        arm=Arm.NONE,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
        model="claude-haiku-4-5-20251001",
    ).to_payload()
    del payload["model"]
    assert RolloutRecord.from_payload(payload).model == ""


def test_pooled_model_reads_one_model_off_the_rows() -> None:
    rows = [_row("claude-haiku-4-5-20251001", probe=p) for p in ("a", "b")]
    assert _pooled_model(rows) == "claude-haiku-4-5-20251001"


def test_pooled_model_reports_a_mix_instead_of_hiding_it() -> None:
    rows = [_row("claude-haiku-4-5-20251001"), _row("claude-sonnet-5")]
    pooled = _pooled_model(rows)
    assert pooled.startswith("MIXED: ")
    assert "claude-haiku-4-5-20251001" in pooled
    assert "claude-sonnet-5" in pooled


def test_unstamped_rows_pool_as_unstamped_never_as_a_model_name() -> None:
    assert _pooled_model([_row("")]) == "unstamped"
    assert _pooled_model([]) == "unstamped"


def test_report_header_states_the_model_beside_the_digest_and_grader() -> None:
    report = build_report([_row("claude-sonnet-5")])
    assert report.model == "claude-sonnet-5"
    header = render_report(report).splitlines()
    assert "- model: claude-sonnet-5" in header
    # Stamped alongside the two provenance lines it is read with, never
    # somewhere a reader comparing two reports would miss it.
    assert any(line.startswith("- grader_revision: ") for line in header)
