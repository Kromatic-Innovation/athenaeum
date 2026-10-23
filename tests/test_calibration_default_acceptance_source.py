# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2079 AC1: a human answer is never a default-acceptance sample."""

from __future__ import annotations

from pathlib import Path

from athenaeum.calibration import (
    HUMAN_ANSWER_SOURCES,
    read_calibration_ledger,
    sample_default_acceptance,
)
from athenaeum.decision_answers import DECISION_ANSWER_SOURCE_TAG

_ALL = {"librarian": {"default_acceptance_audit_sample_rate": 1.0}}


def test_human_source_set_pins_the_real_answer_tag() -> None:
    assert DECISION_ANSWER_SOURCE_TAG in HUMAN_ANSWER_SOURCES


def test_human_answer_is_skipped_at_rate_one(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    assert (
        sample_default_acceptance(
            wiki, decision_id="m1", answer_source=DECISION_ANSWER_SOURCE_TAG, config=_ALL
        )
        is None
    )
    assert read_calibration_ledger(wiki) == []


def test_unknown_or_empty_source_fails_closed(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for src in ("", "   "):
        assert (
            sample_default_acceptance(wiki, decision_id="m1", answer_source=src, config=_ALL)
            is None
        )
    assert read_calibration_ledger(wiki) == []


def test_model_or_rule_source_is_sampled(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    rec = sample_default_acceptance(
        wiki, decision_id="m1", answer_source="t2_auto_apply", config=_ALL
    )
    assert rec is not None and rec["tier"] == "default_acceptance"
