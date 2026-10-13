# SPDX-License-Identifier: Apache-2.0
"""Tests for the Maya PII-hazard classifier glue (issue athenaeum#2063).

Every test uses a fake :class:`~athenaeum.yesno.YesNoScorer` and a
``tmp_path``-rooted cache directory -- no test writes to
``~/.cache/athenaeum`` and no test imports torch or transformers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.pii_hazard_classifier import (
    MODE_NONE,
    MODE_SHADOW,
    RECOGNIZER_EMAIL,
    RECOGNIZER_PHONE,
    HazardToken,
    ShadowCollector,
    compute_weights_id,
    load_pii_hazard_question,
    question_sha256,
    run_shadow_scoring,
)
from athenaeum.yesno import YesNoScorerUnavailable


class _FakeScorer:
    def __init__(self, p_yes_values: list[float]) -> None:
        self._values = p_yes_values
        self.last_load_secs = 0.02
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def score(self, question: str, texts):
        self.calls.append((question, tuple(texts)))
        return list(self._values[: len(texts)])


class _AlwaysUnavailableScorer:
    def score(self, question: str, texts):
        raise YesNoScorerUnavailable("fake: always unavailable")


def _read_ledger_lines(cache_dir: Path) -> list[dict]:
    partitions = list((cache_dir / "pii_hazard_shadow").glob("*.jsonl"))
    assert len(partitions) == 1
    lines = [
        json.loads(line)
        for line in partitions[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return lines


class TestModeNoneIsInert:
    def test_returns_empty_mapping_and_writes_no_ledger(self, tmp_path: Path) -> None:
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_EMAIL, "someone@example.com")
        result = run_shadow_scoring(
            collector,
            mode=MODE_NONE,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_FakeScorer([0.9]),
        )
        assert result == {}
        assert not (tmp_path / "pii_hazard_shadow").exists()


class TestShadowCollectorDedup:
    def test_dedupes_by_recognizer_and_text_keeping_first_seen_order(self) -> None:
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_EMAIL, "a@example.com")
        collector.add(RECOGNIZER_PHONE, "a@example.com")  # same text, different recognizer
        collector.add(RECOGNIZER_EMAIL, "a@example.com")  # exact duplicate, dropped
        collector.add(RECOGNIZER_EMAIL, "b@example.com")
        assert collector.tokens == [
            HazardToken(recognizer=RECOGNIZER_EMAIL, text="a@example.com"),
            HazardToken(recognizer=RECOGNIZER_PHONE, text="a@example.com"),
            HazardToken(recognizer=RECOGNIZER_EMAIL, text="b@example.com"),
        ]


class TestShadowScoringHappyPath:
    def test_scores_tokens_and_writes_one_token_row_each_plus_a_summary(
        self, tmp_path: Path
    ) -> None:
        collector = ShadowCollector(surface="lint-pii", n_skipped_allowlisted=3, n_skipped_is_pii=1)
        collector.add(RECOGNIZER_EMAIL, "a@example.com")
        collector.add(RECOGNIZER_PHONE, "+1-555-0100")
        scorer = _FakeScorer([0.91, 0.12])

        result = run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=scorer,
        )

        assert result == {
            (RECOGNIZER_EMAIL, "a@example.com"): pytest.approx(0.91),
            (RECOGNIZER_PHONE, "+1-555-0100"): pytest.approx(0.12),
        }

        rows = _read_ledger_lines(tmp_path)
        token_rows = [r for r in rows if r["kind"] == "token"]
        summary_rows = [r for r in rows if r["kind"] == "summary"]
        assert len(token_rows) == 2
        assert len(summary_rows) == 1

        summary = summary_rows[0]
        assert summary["surface"] == "lint-pii"
        assert summary["mode"] == MODE_SHADOW
        assert summary["n_candidates"] == 2
        assert summary["n_scored"] == 2
        assert summary["n_skipped_allowlisted"] == 3
        assert summary["n_skipped_is_pii"] == 1
        assert summary["scorer"] == "ok"
        assert summary["reason"] is None

    def test_token_rows_never_carry_a_value_hash_or_path(self, tmp_path: Path) -> None:
        sentinel = "pii-sentinel@example.invalid"
        collector = ShadowCollector(surface="recompare")
        collector.add(RECOGNIZER_EMAIL, sentinel)
        scorer = _FakeScorer([0.5])

        run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=scorer,
        )

        raw_bytes = next((tmp_path / "pii_hazard_shadow").glob("*.jsonl")).read_bytes()
        assert sentinel.encode("utf-8") not in raw_bytes
        import hashlib

        assert hashlib.sha256(sentinel.encode("utf-8")).hexdigest().encode() not in raw_bytes
        assert str(tmp_path).encode("utf-8") not in raw_bytes

        rows = _read_ledger_lines(tmp_path)
        token_row = next(r for r in rows if r["kind"] == "token")
        expected_fields = {
            "kind",
            "v",
            "ts",
            "run_id",
            "surface",
            "mode",
            "recognizer",
            "p_yes",
            "regex_hazard",
            "would_clear_at",
            "question_sha",
            "weights_id",
            "transform",
        }
        assert set(token_row) == expected_fields

    def test_empty_collector_writes_only_a_summary_row(self, tmp_path: Path) -> None:
        collector = ShadowCollector(surface="recompare")
        result = run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_FakeScorer([]),
        )
        assert result == {}
        rows = _read_ledger_lines(tmp_path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "summary"
        assert rows[0]["n_candidates"] == 0


class TestShadowScoringFailClosed:
    def test_scorer_unavailable_writes_unavailable_summary_and_empty_result(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_EMAIL, "a@example.com")

        result = run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_AlwaysUnavailableScorer(),
        )

        assert result == {}
        rows = _read_ledger_lines(tmp_path)
        assert len(rows) == 1
        assert rows[0]["kind"] == "summary"
        assert rows[0]["scorer"] == "unavailable"
        assert rows[0]["reason"] == "YesNoScorerUnavailable"
        err = capsys.readouterr().err
        assert "a@example.com" not in err

    def test_unset_weights_path_fails_closed_with_config_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_EMAIL, "a@example.com")

        result = run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=None,
        )

        assert result == {}
        rows = _read_ledger_lines(tmp_path)
        assert rows[0]["scorer"] == "unavailable"
        err = capsys.readouterr().err
        assert "weights_path" in err


class TestQuestionAndWeightsId:
    def test_question_loads_and_hashes_consistently(self) -> None:
        q = load_pii_hazard_question()
        assert "contact a specific human" in q
        assert question_sha256(q) == question_sha256(q)
        assert len(question_sha256(q)) == 64

    def test_weights_id_none_when_unreadable(self, tmp_path: Path) -> None:
        assert compute_weights_id(tmp_path / "nope") is None

    def test_weights_id_from_config_json(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        wid = compute_weights_id(tmp_path)
        assert wid is not None
        assert len(wid) == 64
