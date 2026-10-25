# SPDX-License-Identifier: Apache-2.0
"""Tests for the Maya PII-hazard classifier glue (issues athenaeum#2063,
athenaeum#2064).

Every test uses a fake :class:`~athenaeum.yesno.YesNoScorer` and a
``tmp_path``-rooted cache directory -- no test writes to
``~/.cache/athenaeum`` and no test imports torch or transformers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.pii_hazard_classifier import (
    DECISION_CLEAR,
    DECISION_KEEP,
    DECISION_REGEX,
    MODE_NONE,
    MODE_PREFILTER,
    MODE_SHADOW,
    RECOGNIZER_EMAIL,
    RECOGNIZER_PHONE,
    HazardToken,
    ShadowCollector,
    compute_weights_id,
    decide_prefilter,
    format_prefilter_summary,
    is_clearable_phone_shape,
    load_pii_hazard_question,
    prefilter_cleared_tokens,
    question_sha256,
    run_shadow_scoring,
    summarize_prefilter,
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


# ---------------------------------------------------------------------------
# Prefilter mode (issue athenaeum#2064, operator ruling of 2026-10-25)
# ---------------------------------------------------------------------------

#: A bare identifier-shaped phone candidate -- 11 digits, no separators, no
#: leading "+". Synthetic: the shape, not any real value, is what matters.
_CLEARABLE_PHONE = "33709806658"

#: A candidate formatted the way a human writes a number they dial. Never
#: clearable at any score (AC3).
_FORMATTED_PHONE = "+1 415 555 0100"


class TestDecidePrefilterPhoneAxis:
    """AC1: the three P(yes) positions on a clearable phone candidate."""

    def test_below_the_band_clears(self) -> None:
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, 0.1) == DECISION_CLEAR
        )

    def test_inside_the_band_leaves_the_regex_to_decide(self) -> None:
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, 0.5) == DECISION_REGEX
        )

    def test_above_the_band_keeps(self) -> None:
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, 0.9) == DECISION_KEEP
        )

    @pytest.mark.parametrize("p_yes", [0.4, 0.6])
    def test_both_band_ends_are_closed(self, p_yes: float) -> None:
        # A score exactly ON an endpoint is the model abstaining, not
        # deciding -- the band is [low, high], not (low, high).
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, p_yes)
            == DECISION_REGEX
        )

    def test_an_unscored_candidate_is_regex_decided(self) -> None:
        # The `scorer: unavailable` path at the single-token level: no score
        # means no decision, never a clear.
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, None) == DECISION_REGEX
        )

    def test_the_band_is_a_parameter_not_a_hidden_constant(self) -> None:
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _CLEARABLE_PHONE, 0.1, (0.05, 0.95))
            == DECISION_REGEX
        )


class TestDecidePrefilterEmailAxisIsNeverCleared:
    """AC2: Maya may not clear an email-shaped candidate in any mode."""

    @pytest.mark.parametrize("p_yes", [0.0, 0.05, 0.5, 0.95])
    def test_no_score_clears_an_email(self, p_yes: float) -> None:
        assert (
            decide_prefilter(RECOGNIZER_EMAIL, "role@example.invalid", p_yes)
            == DECISION_REGEX
        )

    def test_an_email_is_never_even_reported_as_kept(self) -> None:
        # "kept" records the model AGREEING with the regex; on the email axis
        # the model's agreement is not consulted at all, so the decision is
        # `regex` rather than `keep` even at a high score.
        assert (
            decide_prefilter(RECOGNIZER_EMAIL, "role@example.invalid", 0.99)
            == DECISION_REGEX
        )


class TestClearablePhoneShape:
    """AC3: only a bare 10-to-12-digit run is ever clearable."""

    @pytest.mark.parametrize("value", ["0123456789", _CLEARABLE_PHONE, "012345678901"])
    def test_bare_runs_of_ten_to_twelve_digits_are_clearable(self, value: str) -> None:
        assert is_clearable_phone_shape(value)

    @pytest.mark.parametrize(
        "value",
        [
            "012345678",  # nine digits: too short
            "0123456789012",  # thirteen digits: too long
            _FORMATTED_PHONE,  # leading "+" and separators
            "+33709806658",  # leading "+" alone
            "415-555-0100",  # separators alone
            "(415) 5550100",  # parens
            "337 098 06658",  # spaces
            "3370980665a",  # not all digits
        ],
    )
    def test_everything_else_is_not_clearable(self, value: str) -> None:
        assert not is_clearable_phone_shape(value)

    def test_a_formatted_phone_stays_regex_decided_at_a_clearing_score(self) -> None:
        assert (
            decide_prefilter(RECOGNIZER_PHONE, _FORMATTED_PHONE, 0.01) == DECISION_REGEX
        )


class TestPrefilterLedgerRows:
    """AC6: every cleared token is auditable without plaintext."""

    def _run(self, tmp_path: Path, tokens: list[tuple[str, str]], scores: list[float]):
        collector = ShadowCollector(surface="lint-pii")
        for recognizer, text in tokens:
            collector.add(recognizer, text)
        return run_shadow_scoring(
            collector,
            mode=MODE_PREFILTER,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_FakeScorer(scores),
        )

    def test_a_cleared_token_writes_a_cleared_row(self, tmp_path: Path) -> None:
        self._run(tmp_path, [(RECOGNIZER_PHONE, _CLEARABLE_PHONE)], [0.1])

        token_rows = [r for r in _read_ledger_lines(tmp_path) if r["kind"] == "token"]
        assert len(token_rows) == 1
        row = token_rows[0]
        assert row["mode"] == MODE_PREFILTER
        assert row["cleared"] is True
        assert row["decision"] == DECISION_CLEAR
        assert row["p_yes"] == pytest.approx(0.1)
        assert row["recognizer"] == RECOGNIZER_PHONE
        assert row["question_sha"] == question_sha256(load_pii_hazard_question())
        # The audit trail must carry no value and no path, exactly as in
        # shadow mode (issue athenaeum#2063's privacy contract).
        assert _CLEARABLE_PHONE not in json.dumps(row)

    def test_kept_band_and_email_rows_record_not_cleared(self, tmp_path: Path) -> None:
        self._run(
            tmp_path,
            [
                (RECOGNIZER_PHONE, _CLEARABLE_PHONE),
                (RECOGNIZER_PHONE, "012345678901"),
                (RECOGNIZER_PHONE, _FORMATTED_PHONE),
                (RECOGNIZER_EMAIL, "role@example.invalid"),
            ],
            [0.9, 0.5, 0.01, 0.01],
        )

        token_rows = [r for r in _read_ledger_lines(tmp_path) if r["kind"] == "token"]
        assert [r["decision"] for r in token_rows] == [
            DECISION_KEEP,
            DECISION_REGEX,
            DECISION_REGEX,
            DECISION_REGEX,
        ]
        assert all(r["cleared"] is False for r in token_rows)

    def test_an_unavailable_scorer_clears_nothing(self, tmp_path: Path) -> None:
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_PHONE, _CLEARABLE_PHONE)

        result = run_shadow_scoring(
            collector,
            mode=MODE_PREFILTER,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_AlwaysUnavailableScorer(),
        )

        # No scores at all, so nothing can be cleared -- the gate falls back
        # to regex-only by construction, not by a second check.
        assert result == {}
        assert prefilter_cleared_tokens(result) == frozenset()
        rows = _read_ledger_lines(tmp_path)
        assert [r["kind"] for r in rows] == ["summary"]
        assert rows[0]["scorer"] == "unavailable"
        assert rows[0]["mode"] == MODE_PREFILTER

    def test_shadow_rows_carry_no_prefilter_fields(self, tmp_path: Path) -> None:
        # AC5 pin: a shadow row is field-for-field what issue athenaeum#2063
        # wrote. `decision`/`cleared` are prefilter-only.
        collector = ShadowCollector(surface="lint-pii")
        collector.add(RECOGNIZER_PHONE, _CLEARABLE_PHONE)

        run_shadow_scoring(
            collector,
            mode=MODE_SHADOW,
            weights_path=None,
            interpreter=None,
            timeout_secs=10.0,
            cache_dir=tmp_path,
            scorer=_FakeScorer([0.1]),
        )

        token_rows = [r for r in _read_ledger_lines(tmp_path) if r["kind"] == "token"]
        assert "decision" not in token_rows[0]
        assert "cleared" not in token_rows[0]


class TestPrefilterSummary:
    """AC7: the five counts partition the scored batch."""

    def test_counts_bucket_every_scored_candidate_exactly_once(self) -> None:
        scored = {
            (RECOGNIZER_PHONE, _CLEARABLE_PHONE): 0.1,  # cleared
            (RECOGNIZER_PHONE, "012345678901"): 0.95,  # kept
            (RECOGNIZER_PHONE, "0123456789"): 0.5,  # in band
            (RECOGNIZER_PHONE, _FORMATTED_PHONE): 0.1,  # phone shape untouched
            (RECOGNIZER_EMAIL, "role@example.invalid"): 0.1,  # email untouched
            (RECOGNIZER_EMAIL, "noreply@example.invalid"): 0.9,  # email untouched
        }

        summary = summarize_prefilter(scored)

        assert summary.cleared == 1
        assert summary.kept == 1
        assert summary.in_band == 1
        assert summary.phone_shape_untouched == 1
        assert summary.email_untouched == 2
        assert summary.total == len(scored)

    def test_the_line_names_the_band_and_every_count(self) -> None:
        line = format_prefilter_summary(
            summarize_prefilter({(RECOGNIZER_PHONE, _CLEARABLE_PHONE): 0.1})
        )
        assert line == (
            "pii hazard prefilter (band [0.4, 0.6]): 1 cleared, 0 kept, "
            "0 in band (regex decided), 0 email untouched, "
            "0 phone-shape untouched"
        )

    def test_an_empty_batch_summarizes_to_zeros(self) -> None:
        assert summarize_prefilter({}).total == 0
