# SPDX-License-Identifier: Apache-2.0
"""Tests for ``resolve_intake_runtime_floor`` (issue athenaeum#1102).

Covers AC3 (env > yaml > default precedence, same style as
``librarian.entity_runtime_share``'s neighbours), AC4 (default disabled),
AC6 (non-positive/malformed falls through to disabled — the
``resolve_max_merge_sources`` "0 disables" convention), and AC7 (a floor
``>= 1.0`` is REFUSED, not clamped).
"""

from __future__ import annotations

import logging

import pytest

from athenaeum.config import resolve_intake_runtime_floor


class TestResolveIntakeRuntimeFloor:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # AC4: the floor defaults to disabled.
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        assert resolve_intake_runtime_floor(None) == 0.0
        assert resolve_intake_runtime_floor({}) == 0.0
        assert resolve_intake_runtime_floor({"librarian": {}}) == 0.0

    def test_yaml_value_wins_over_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": 0.2}}
        assert resolve_intake_runtime_floor(cfg) == 0.2

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "0.3")
        cfg = {"librarian": {"intake_runtime_floor": 0.2}}
        assert resolve_intake_runtime_floor(cfg) == 0.3

    def test_quoted_yaml_value_coerced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": "0.25"}}
        assert resolve_intake_runtime_floor(cfg) == 0.25

    # -- AC6: non-positive / malformed falls through to disabled ----------

    @pytest.mark.parametrize("value", [0, 0.0, -0.2, -1])
    def test_non_positive_yaml_disables(
        self, value: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": value}}
        assert resolve_intake_runtime_floor(cfg) == 0.0

    def test_bool_yaml_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `intake_runtime_floor: yes` parses as True (an int subclass) and
        # must not become a 100% floor.
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": True}}
        assert resolve_intake_runtime_floor(cfg) == 0.0

    def test_non_numeric_yaml_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": "not-a-number"}}
        assert resolve_intake_runtime_floor(cfg) == 0.0

    def test_env_zero_disables_even_with_nonzero_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mirrors resolve_max_merge_sources: a parsed env value (including
        # 0/negative) is authoritative over yaml.
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "0")
        cfg = {"librarian": {"intake_runtime_floor": 0.4}}
        assert resolve_intake_runtime_floor(cfg) == 0.0

    def test_malformed_env_warns_and_falls_back_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "0.3x")
        cfg = {"librarian": {"intake_runtime_floor": 0.2}}
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            assert resolve_intake_runtime_floor(cfg) == 0.2
        assert any(
            "ATHENAEUM_INTAKE_RUNTIME_FLOOR" in r.message for r in caplog.records
        ), caplog.text

    # -- AC7: a floor >= 1.0 (larger than the whole window) is REFUSED ----

    @pytest.mark.parametrize("value", [1.0, 1.5, 100])
    def test_floor_at_or_above_whole_window_is_refused(
        self, value: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", raising=False)
        cfg = {"librarian": {"intake_runtime_floor": value}}
        assert resolve_intake_runtime_floor(cfg) == 0.0

    def test_env_floor_at_or_above_whole_window_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_INTAKE_RUNTIME_FLOOR", "1.2")
        assert resolve_intake_runtime_floor(None) == 0.0
