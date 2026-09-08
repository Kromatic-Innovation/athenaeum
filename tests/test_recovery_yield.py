# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.recovery_yield` (issue athenaeum#1453).

Covers the store (``load_state``/``write_state`` fail-open + round-trip),
threshold resolution precedence/validation, and the pure ``evaluate``
predicate — in particular the "zero denominator is no-data, not a zero
yield" rule and the inclusive at-threshold boundary, both load-bearing per
the issue's own framing: "This issue is satisfied when the signal and its
threshold exist, never when the share happens to look good."
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.recovery_yield import (
    DEFAULT_RECOVERY_YIELD_THRESHOLD,
    STATE_NAME,
    evaluate,
    load_state,
    resolve_threshold,
    write_state,
)


class TestLoadStateFailOpen:
    def test_missing_file_is_zeroed(self, tmp_path: Path) -> None:
        state = load_state(tmp_path)
        assert state == {"uncited": 0, "recovered": 0, "write_cited": 0, "time_window": 0}

    def test_corrupt_json_is_zeroed(self, tmp_path: Path) -> None:
        (tmp_path / STATE_NAME).write_text("{not json", encoding="utf-8")
        assert load_state(tmp_path)["uncited"] == 0

    def test_non_dict_json_is_zeroed(self, tmp_path: Path) -> None:
        (tmp_path / STATE_NAME).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        state = load_state(tmp_path)
        assert state == {"uncited": 0, "recovered": 0, "write_cited": 0, "time_window": 0}

    def test_negative_ints_fall_back_to_zero(self, tmp_path: Path) -> None:
        (tmp_path / STATE_NAME).write_text(
            json.dumps({"uncited": -1, "recovered": -5, "write_cited": -1, "time_window": -1}),
            encoding="utf-8",
        )
        state = load_state(tmp_path)
        assert state == {"uncited": 0, "recovered": 0, "write_cited": 0, "time_window": 0}

    def test_bool_field_is_rejected_not_coerced_to_int(self, tmp_path: Path) -> None:
        """``bool`` is an ``int`` subclass — must not silently become 0/1."""
        (tmp_path / STATE_NAME).write_text(
            json.dumps({"uncited": True, "recovered": False, "write_cited": 0, "time_window": 0}),
            encoding="utf-8",
        )
        state = load_state(tmp_path)
        assert state["uncited"] == 0
        assert state["recovered"] == 0

    def test_non_numeric_field_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / STATE_NAME).write_text(
            json.dumps({"uncited": "lots", "recovered": 3, "write_cited": 3, "time_window": 0}),
            encoding="utf-8",
        )
        state = load_state(tmp_path)
        assert state["uncited"] == 0
        # Sibling valid fields are unaffected by one bad field.
        assert state["recovered"] == 3

    def test_missing_fields_default_to_zero(self, tmp_path: Path) -> None:
        (tmp_path / STATE_NAME).write_text(json.dumps({"uncited": 5}), encoding="utf-8")
        state = load_state(tmp_path)
        assert state == {"uncited": 5, "recovered": 0, "write_cited": 0, "time_window": 0}


class TestWriteStateRoundTrips:
    def test_round_trips_through_load_state(self, tmp_path: Path) -> None:
        write_state(tmp_path, uncited=10, recovered=7, write_cited=5, time_window=2)
        state = load_state(tmp_path)
        assert state == {"uncited": 10, "recovered": 7, "write_cited": 5, "time_window": 2}

    def test_stamps_updated(self, tmp_path: Path) -> None:
        write_state(tmp_path, uncited=1, recovered=1, write_cited=1, time_window=0)
        data = json.loads((tmp_path / STATE_NAME).read_text(encoding="utf-8"))
        assert isinstance(data["updated"], str) and data["updated"]

    def test_a_later_write_overwrites_not_stale(self, tmp_path: Path) -> None:
        write_state(tmp_path, uncited=10, recovered=1, write_cited=1, time_window=0)
        write_state(tmp_path, uncited=3, recovered=3, write_cited=2, time_window=1)
        state = load_state(tmp_path)
        assert state == {"uncited": 3, "recovered": 3, "write_cited": 2, "time_window": 1}

    def test_a_clean_pass_still_overwrites_unconditionally(self, tmp_path: Path) -> None:
        """A run with nothing to recover must not leave a stale prior record."""
        write_state(tmp_path, uncited=10, recovered=1, write_cited=1, time_window=0)
        write_state(tmp_path, uncited=0, recovered=0, write_cited=0, time_window=0)
        state = load_state(tmp_path)
        assert state == {"uncited": 0, "recovered": 0, "write_cited": 0, "time_window": 0}


class TestResolveThreshold:
    def test_default_when_nothing_set(self) -> None:
        assert resolve_threshold(None) == DEFAULT_RECOVERY_YIELD_THRESHOLD

    def test_yaml_wins_over_default(self) -> None:
        cfg = {"librarian": {"recovery_yield_threshold": 0.75}}
        assert resolve_threshold(cfg) == 0.75

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RECOVERY_YIELD_THRESHOLD", "0.9")
        cfg = {"librarian": {"recovery_yield_threshold": 0.75}}
        assert resolve_threshold(cfg) == 0.9

    def test_env_wins_over_default_with_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RECOVERY_YIELD_THRESHOLD", "0.2")
        assert resolve_threshold(None) == 0.2

    @pytest.mark.parametrize("raw", ["not-a-number", "", "nan-ish"])
    def test_malformed_env_falls_back_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RECOVERY_YIELD_THRESHOLD", raw)
        cfg = {"librarian": {"recovery_yield_threshold": 0.6}}
        assert resolve_threshold(cfg) == 0.6

    @pytest.mark.parametrize("raw", ["-0.1", "1.1", "2"])
    def test_out_of_range_env_falls_back_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RECOVERY_YIELD_THRESHOLD", raw)
        cfg = {"librarian": {"recovery_yield_threshold": 0.6}}
        assert resolve_threshold(cfg) == 0.6

    def test_bool_yaml_value_is_rejected(self) -> None:
        """``bool`` is an ``int``/``float``-adjacent subclass in yaml's type model."""
        cfg = {"librarian": {"recovery_yield_threshold": True}}
        assert resolve_threshold(cfg) == DEFAULT_RECOVERY_YIELD_THRESHOLD

    def test_non_numeric_yaml_value_is_rejected(self) -> None:
        cfg = {"librarian": {"recovery_yield_threshold": "high"}}
        assert resolve_threshold(cfg) == DEFAULT_RECOVERY_YIELD_THRESHOLD

    @pytest.mark.parametrize("raw", [-0.1, 1.1, 2])
    def test_out_of_range_yaml_value_is_rejected(self, raw: float) -> None:
        cfg = {"librarian": {"recovery_yield_threshold": raw}}
        assert resolve_threshold(cfg) == DEFAULT_RECOVERY_YIELD_THRESHOLD

    def test_boundary_values_zero_and_one_are_accepted(self) -> None:
        assert resolve_threshold({"librarian": {"recovery_yield_threshold": 0.0}}) == 0.0
        assert resolve_threshold({"librarian": {"recovery_yield_threshold": 1.0}}) == 1.0

    def test_missing_librarian_section_falls_back_to_default(self) -> None:
        assert resolve_threshold({}) == DEFAULT_RECOVERY_YIELD_THRESHOLD

    def test_non_dict_librarian_section_falls_back_to_default(self) -> None:
        assert resolve_threshold({"librarian": "oops"}) == DEFAULT_RECOVERY_YIELD_THRESHOLD


class TestEvaluate:
    def _state(
        self, *, uncited: int, recovered: int, write_cited: int = 0, time_window: int = 0
    ) -> dict[str, int]:
        return {
            "uncited": uncited,
            "recovered": recovered,
            "write_cited": write_cited,
            "time_window": time_window,
        }

    def test_zero_denominator_is_no_data_not_zero_yield(self) -> None:
        """A zero denominator must never read as a breach."""
        result = evaluate(self._state(uncited=0, recovered=0), threshold=0.5)
        assert result.rate is None
        assert result.verdict == "no-data"
        assert result.within_threshold is None

    def test_above_threshold_is_ok(self) -> None:
        result = evaluate(self._state(uncited=10, recovered=8), threshold=0.5)
        assert result.rate == pytest.approx(0.8)
        assert result.verdict == "ok"
        assert result.within_threshold is True

    def test_below_threshold_is_breach(self) -> None:
        result = evaluate(self._state(uncited=10, recovered=2), threshold=0.5)
        assert result.rate == pytest.approx(0.2)
        assert result.verdict == "breach"
        assert result.within_threshold is False

    def test_exactly_at_threshold_is_ok_inclusive(self) -> None:
        """At-threshold is decided inclusively: OK, not a breach."""
        result = evaluate(self._state(uncited=10, recovered=5), threshold=0.5)
        assert result.rate == pytest.approx(0.5)
        assert result.verdict == "ok"
        assert result.within_threshold is True

    def test_zero_recovered_nonzero_uncited_is_breach_not_no_data(self) -> None:
        """Distinguish 'nothing to recover' from 'recovery resolved nothing'."""
        result = evaluate(self._state(uncited=3, recovered=0), threshold=0.5)
        assert result.rate == 0.0
        assert result.verdict == "breach"
