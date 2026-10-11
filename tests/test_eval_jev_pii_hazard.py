# SPDX-License-Identifier: Apache-2.0
"""Tests for the Jev PII-hazard eval script (issue athenaeum#2009).

Loaded via ``importlib.util.spec_from_file_location``, same pattern as
``test_eval_maya_pii_hazard.py``. Uses a fake :class:`JevBackend` and
fabricated ``example.invalid`` addresses / plain non-contact strings --
never anything corpus-derived. The live path (:class:`LiveJevBackend`,
which talks to the real Jev API) is exercised only by
``scripts/jev_smoke.py`` and is skipped here without ``JEV_API_KEY`` --
this module never makes a network call.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_jev_pii_hazard.py"
_MAYA_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_maya_pii_hazard.py"

# Load the Maya module first (under its own stable name) so the Jev script's
# own `_load_maya_module()` reuses it from sys.modules instead of exec'ing a
# second copy with a distinct Fixture/RowResult class identity.
if "eval_maya_pii_hazard" not in sys.modules:
    _maya_spec = importlib.util.spec_from_file_location("eval_maya_pii_hazard", _MAYA_SCRIPT)
    assert _maya_spec and _maya_spec.loader
    _maya_module = importlib.util.module_from_spec(_maya_spec)
    sys.modules["eval_maya_pii_hazard"] = _maya_module
    _maya_spec.loader.exec_module(_maya_module)

_spec = importlib.util.spec_from_file_location("eval_jev_pii_hazard", _SCRIPT)
assert _spec and _spec.loader
eval_jev_pii_hazard = importlib.util.module_from_spec(_spec)
sys.modules["eval_jev_pii_hazard"] = eval_jev_pii_hazard
_spec.loader.exec_module(eval_jev_pii_hazard)

maya = sys.modules["eval_maya_pii_hazard"]

_FAKE_CONTACT_TEXT = "You can reach Jordan Reyes at jordan.reyes@example.invalid for the handoff."
_FAKE_NONCONTACT_TEXT = "Order no. 48213 shipped yesterday via the usual courier."
_FAKE_ALLOWLISTED_VALUE = "noreply@example.invalid"


class FakeJevBackend(eval_jev_pii_hazard.JevBackend):
    """Hand-assigned (p_yes, input_tokens, output_tokens) per fixed string.
    No network, no real decision_provider import."""

    def __init__(
        self, mapping: dict[str, tuple[float, int, int]], raise_for: set[str] | None = None
    ):
        self._mapping = mapping
        self._raise_for = raise_for or set()
        self.calls: list[tuple[str, str]] = []

    def p_yes_with_usage(self, question: str, text: str) -> tuple[float, int, int]:
        self.calls.append((question, text))
        if text in self._raise_for:
            raise ValueError(f"synthetic failure containing the text: {text}")
        return self._mapping[text]


class TestRunJevEval:
    def _fixtures(self) -> list:
        Fixture = maya.Fixture
        return [
            Fixture(text=_FAKE_CONTACT_TEXT, label=True, source="jsonl"),
            Fixture(text=_FAKE_NONCONTACT_TEXT, label=False, source="jsonl"),
        ]

    def test_one_call_per_fixture_constant_question_and_id(self) -> None:
        backend = FakeJevBackend(
            {_FAKE_CONTACT_TEXT: (0.9, 10, 0), _FAKE_NONCONTACT_TEXT: (0.1, 8, 0)}
        )
        outcomes = eval_jev_pii_hazard.run_jev_eval(backend, self._fixtures())
        assert len(outcomes) == 2
        assert backend.calls == [
            (eval_jev_pii_hazard.QUESTION, _FAKE_CONTACT_TEXT),
            (eval_jev_pii_hazard.QUESTION, _FAKE_NONCONTACT_TEXT),
        ]
        assert outcomes[0].p_yes == 0.9
        assert outcomes[0].error_type is None
        assert outcomes[1].p_yes == 0.1

    def test_tokens_are_summed_from_every_response(self) -> None:
        backend = FakeJevBackend(
            {_FAKE_CONTACT_TEXT: (0.9, 100, 0), _FAKE_NONCONTACT_TEXT: (0.1, 50, 0)}
        )
        outcomes = eval_jev_pii_hazard.run_jev_eval(backend, self._fixtures())
        usage = eval_jev_pii_hazard.summarize_usage(outcomes)
        assert usage["total_input_tokens"] == 150
        assert usage["n_rows"] == 2
        assert usage["n_errors_final"] == 0

    def test_a_raising_backend_leaves_text_out_of_outcome_and_markdown(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backend = FakeJevBackend(
            {_FAKE_NONCONTACT_TEXT: (0.1, 5, 0)}, raise_for={_FAKE_CONTACT_TEXT}
        )
        outcomes = eval_jev_pii_hazard.run_jev_eval(backend, self._fixtures())
        assert outcomes[0].error_type == "ValueError"
        assert outcomes[0].p_yes is None

        results = eval_jev_pii_hazard.outcomes_to_row_results(
            outcomes, self._fixtures(), threshold=0.5
        )
        metrics = maya.compute_metrics(results)
        markdown = maya.render_markdown(metrics, threshold=0.5, abstain_band=None)
        assert _FAKE_CONTACT_TEXT not in markdown
        assert "jordan.reyes" not in markdown

        captured = capsys.readouterr()
        assert _FAKE_CONTACT_TEXT not in captured.out
        assert _FAKE_CONTACT_TEXT not in captured.err
        assert "jordan.reyes" not in captured.out
        assert "jordan.reyes" not in captured.err

    def test_spend_cap_stops_before_overage(self) -> None:
        # Each successful call reports 1,000,000 input tokens -> USD 0.042 each.
        # With a cap of 0.05, the 2nd call would push projected spend over the
        # cap based on the running total from the 1st, so it must not run.
        Fixture = maya.Fixture
        fixtures = [
            Fixture(text=_FAKE_CONTACT_TEXT, label=True, source="jsonl"),
            Fixture(text=_FAKE_NONCONTACT_TEXT, label=False, source="jsonl"),
            Fixture(text=_FAKE_ALLOWLISTED_VALUE, label=False, source="jsonl"),
        ]
        backend = FakeJevBackend(
            {
                _FAKE_CONTACT_TEXT: (0.9, 1_000_000, 0),
                _FAKE_NONCONTACT_TEXT: (0.1, 1_000_000, 0),
                _FAKE_ALLOWLISTED_VALUE: (0.2, 1_000_000, 0),
            }
        )
        with pytest.raises(eval_jev_pii_hazard.SpendCapExceeded):
            eval_jev_pii_hazard.run_jev_eval(backend, fixtures, max_spend_usd=0.05)
        # At most the calls affordable within the cap were made.
        assert len(backend.calls) <= 2


class TestRetryFailedRows:
    def test_retries_only_failed_rows_until_success(self) -> None:
        Fixture = maya.Fixture
        fixtures = [Fixture(text=_FAKE_CONTACT_TEXT, label=True, source="jsonl")]

        class FlakyBackend(eval_jev_pii_hazard.JevBackend):
            def __init__(self) -> None:
                self.attempts = 0

            def p_yes_with_usage(self, question: str, text: str) -> tuple[float, int, int]:
                self.attempts += 1
                if self.attempts == 1:
                    raise ValueError("transient")
                return (0.9, 10, 0)

        backend = FlakyBackend()
        outcomes = eval_jev_pii_hazard.run_jev_eval(backend, fixtures)
        assert outcomes[0].error_type == "ValueError"

        retried = eval_jev_pii_hazard.retry_failed_rows(backend, fixtures, outcomes)
        assert retried[0].error_type is None
        assert retried[0].p_yes == 0.9
        assert retried[0].attempts == 2

    def test_rows_never_dropped_when_retries_exhausted(self) -> None:
        Fixture = maya.Fixture
        fixtures = [Fixture(text=_FAKE_NONCONTACT_TEXT, label=False, source="jsonl")]
        backend = FakeJevBackend({}, raise_for={_FAKE_NONCONTACT_TEXT})
        outcomes = eval_jev_pii_hazard.run_jev_eval(backend, fixtures)
        retried = eval_jev_pii_hazard.retry_failed_rows(backend, fixtures, outcomes, max_rounds=2)
        assert len(retried) == 1
        assert retried[0].error_type is not None


class TestMainFullScriptPathWithFakeBackend:
    def test_main_writes_json_with_no_fixture_leakage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "fake-key-for-test")
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text(
            json.dumps({"text": _FAKE_CONTACT_TEXT, "label": True})
            + "\n"
            + json.dumps({"text": _FAKE_NONCONTACT_TEXT, "label": False})
            + "\n",
            encoding="utf-8",
        )
        allowlist_path = tmp_path / "allowlist.yaml"
        allowlist_path.write_text(
            f"- value: {_FAKE_ALLOWLISTED_VALUE!r}\n  reason: service account, not a person\n",
            encoding="utf-8",
        )
        output_path = tmp_path / "results.json"

        fake_backend = FakeJevBackend(
            {
                _FAKE_CONTACT_TEXT: (0.9, 10, 0),
                _FAKE_NONCONTACT_TEXT: (0.1, 8, 0),
                _FAKE_ALLOWLISTED_VALUE: (0.05, 6, 0),
            }
        )
        monkeypatch.setattr(eval_jev_pii_hazard, "_build_backend", lambda: fake_backend)

        exit_code = eval_jev_pii_hazard.main(
            [
                "--fixtures",
                str(fixtures_path),
                "--allowlist",
                str(allowlist_path),
                "--output",
                str(output_path),
            ]
        )

        assert exit_code == 0
        assert output_path.exists()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["n_fixtures"] == 3
        assert payload["n_still_errored"] == 0
        assert payload["usage"]["total_input_tokens"] == 24
        raw_text = output_path.read_text(encoding="utf-8")
        assert _FAKE_CONTACT_TEXT not in raw_text
        assert _FAKE_NONCONTACT_TEXT not in raw_text
        assert _FAKE_ALLOWLISTED_VALUE not in raw_text
        assert "jordan.reyes" not in raw_text

        captured = capsys.readouterr()
        assert _FAKE_CONTACT_TEXT not in captured.out
        assert "jordan.reyes" not in captured.out

        # mode 600
        assert (output_path.stat().st_mode & 0o777) == 0o600

    def test_main_refuses_without_jev_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text(json.dumps({"text": "x", "label": False}) + "\n", encoding="utf-8")
        exit_code = eval_jev_pii_hazard.main(
            ["--fixtures", str(fixtures_path), "--output", str(tmp_path / "out.json")]
        )
        assert exit_code == 1

    def test_main_requires_at_least_one_fixture_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JEV_API_KEY", "fake-key-for-test")
        exit_code = eval_jev_pii_hazard.main(["--output", str(tmp_path / "out.json")])
        assert exit_code == 2


class TestLiveJevBackendSkippedWithoutKey:
    """The real network-calling path is never exercised here -- only
    ``scripts/jev_smoke.py`` does that, and only with JEV_API_KEY present."""

    def test_skipped_without_key(self) -> None:
        import os

        if os.environ.get("JEV_API_KEY"):
            pytest.skip("JEV_API_KEY set -- live path exercised by jev_smoke.py, not here")
        pytest.skip("live path never exercised in this test module (no key)")
