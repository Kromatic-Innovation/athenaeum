# SPDX-License-Identifier: Apache-2.0
"""Tests for the Maya PII-hazard eval script (issue athenaeum#2049).

Loaded via ``importlib.util.spec_from_file_location`` -- the same pattern
``test_measure_contradiction_baseline.py`` uses for a ``scripts/`` module --
because ``scripts/`` is not an importable package and is deliberately outside
``[tool.mypy].files`` (that file list stays ``src/athenaeum`` only).

The synthetic smoke test below uses a fake :class:`MayaScorer` and fabricated
``example.invalid`` addresses / plain non-contact strings -- never anything
corpus-derived -- and exercises the FULL script path (CLI parsing, fixture
loading, the eval loop, metrics, and the markdown write) by monkeypatching
only the model-construction seam (``_build_scorer``), never the real
:class:`MayaAdapter`. A separate real-model test is skipped (not failed) when
``ATHENAEUM_MAYA_WEIGHTS_PATH`` is absent or ``transformers`` is not
installed -- the "no implicit download" acceptance criterion means this repo's
default CI never has either.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_maya_pii_hazard.py"

_spec = importlib.util.spec_from_file_location("eval_maya_pii_hazard", _SCRIPT)
assert _spec and _spec.loader
eval_maya_pii_hazard = importlib.util.module_from_spec(_spec)
# Registered in sys.modules BEFORE exec: dataclasses's own field-type
# resolution looks the defining module up by name there, which fails with
# an opaque AttributeError on a module that only exists via module_from_spec.
sys.modules["eval_maya_pii_hazard"] = eval_maya_pii_hazard
_spec.loader.exec_module(eval_maya_pii_hazard)


# Fabricated, never corpus-derived. `.invalid` is the reserved TLD for exactly
# this purpose (RFC 2606) -- it plainly cannot resolve to a real contact.
_FAKE_CONTACT_TEXT = "You can reach Jordan Reyes at jordan.reyes@example.invalid for the handoff."
_FAKE_NONCONTACT_TEXT = "Order no. 48213 shipped yesterday via the usual courier."
_FAKE_ALLOWLISTED_VALUE = "noreply@example.invalid"


class FakeScorer:
    """A :class:`eval_maya_pii_hazard.MayaScorer` with hand-assigned P(yes)
    per fixed string -- no model, no torch, no transformers."""

    def __init__(self, mapping: dict[str, float]) -> None:
        self._mapping = mapping

    def p_yes(self, question: str, text: str) -> float:
        assert question == eval_maya_pii_hazard.QUESTION
        return self._mapping[text]


class TestLoadFixturesJsonl:
    def test_loads_text_label_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(
            json.dumps({"text": _FAKE_CONTACT_TEXT, "label": True})
            + "\n"
            + json.dumps({"text": _FAKE_NONCONTACT_TEXT, "label": False})
            + "\n",
            encoding="utf-8",
        )
        fixtures = eval_maya_pii_hazard.load_fixtures_jsonl(path)
        assert [f.text for f in fixtures] == [_FAKE_CONTACT_TEXT, _FAKE_NONCONTACT_TEXT]
        assert [f.label for f in fixtures] == [True, False]

    def test_skips_blank_and_comment_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(
            "\n# a comment\n" + json.dumps({"text": _FAKE_NONCONTACT_TEXT, "label": False}) + "\n",
            encoding="utf-8",
        )
        fixtures = eval_maya_pii_hazard.load_fixtures_jsonl(path)
        assert len(fixtures) == 1

    def test_rejects_non_bool_label(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(json.dumps({"text": "x", "label": "yes"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="strict bool"):
            eval_maya_pii_hazard.load_fixtures_jsonl(path)

    def test_rejects_missing_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(json.dumps({"text": "x"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="text.*label"):
            eval_maya_pii_hazard.load_fixtures_jsonl(path)


class TestLoadFixturesAllowlist:
    def test_value_entries_become_negative_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.yaml"
        path.write_text(
            f"- value: {_FAKE_ALLOWLISTED_VALUE!r}\n  reason: service account, not a person\n",
            encoding="utf-8",
        )
        fixtures, n_pattern_skipped, n_errors = eval_maya_pii_hazard.load_fixtures_allowlist(path)
        assert len(fixtures) == 1
        assert fixtures[0].text == _FAKE_ALLOWLISTED_VALUE
        assert fixtures[0].label is False
        assert n_pattern_skipped == 0
        assert n_errors == 0

    def test_pattern_entries_are_counted_not_guessed(self, tmp_path: Path) -> None:
        path = tmp_path / "allowlist.yaml"
        path.write_text(
            "- pattern: '[^@]+@example\\.invalid'\n  reason: placeholder domain\n",
            encoding="utf-8",
        )
        fixtures, n_pattern_skipped, n_errors = eval_maya_pii_hazard.load_fixtures_allowlist(path)
        assert fixtures == []
        assert n_pattern_skipped == 1
        assert n_errors == 0

    def test_missing_file_is_zero_rows_not_an_error(self, tmp_path: Path) -> None:
        fixtures, n_pattern_skipped, n_errors = eval_maya_pii_hazard.load_fixtures_allowlist(
            tmp_path / "does-not-exist.yaml"
        )
        assert (fixtures, n_pattern_skipped, n_errors) == ([], 0, 0)


class TestRegexGateVerdict:
    def test_contact_shaped_string_matches(self) -> None:
        assert eval_maya_pii_hazard.regex_gate_verdict(_FAKE_CONTACT_TEXT) is True

    def test_non_contact_string_does_not_match(self) -> None:
        assert eval_maya_pii_hazard.regex_gate_verdict(_FAKE_NONCONTACT_TEXT) is False


class TestRunEvalAndMetrics:
    def _fixtures(self) -> list:
        Fixture = eval_maya_pii_hazard.Fixture
        return [
            Fixture(text=_FAKE_CONTACT_TEXT, label=True, source="jsonl"),
            Fixture(text=_FAKE_NONCONTACT_TEXT, label=False, source="jsonl"),
        ]

    def test_run_eval_produces_one_result_per_fixture(self) -> None:
        scorer = FakeScorer({_FAKE_CONTACT_TEXT: 0.9, _FAKE_NONCONTACT_TEXT: 0.1})
        results = eval_maya_pii_hazard.run_eval(scorer, self._fixtures(), threshold=0.5)
        assert len(results) == 2
        assert results[0].predicted is True
        assert results[0].regex_verdict is True
        assert results[1].predicted is False
        assert results[1].regex_verdict is False

    def test_metrics_perfect_agreement(self) -> None:
        scorer = FakeScorer({_FAKE_CONTACT_TEXT: 0.9, _FAKE_NONCONTACT_TEXT: 0.1})
        results = eval_maya_pii_hazard.run_eval(scorer, self._fixtures(), threshold=0.5)
        metrics = eval_maya_pii_hazard.compute_metrics(results)
        assert metrics.tp == 1
        assert metrics.tn == 1
        assert metrics.fp == 0
        assert metrics.fn == 0
        assert metrics.accuracy == 1.0
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0

    def test_precision_is_none_with_zero_predicted_positives(self) -> None:
        Fixture = eval_maya_pii_hazard.Fixture
        scorer = FakeScorer({_FAKE_NONCONTACT_TEXT: 0.1})
        results = eval_maya_pii_hazard.run_eval(
            scorer,
            [Fixture(text=_FAKE_NONCONTACT_TEXT, label=False, source="jsonl")],
            threshold=0.5,
        )
        metrics = eval_maya_pii_hazard.compute_metrics(results)
        assert metrics.precision is None
        assert "n/a" in eval_maya_pii_hazard.render_markdown(
            metrics, threshold=0.5, abstain_band=None
        )

    def test_abstention_band_excludes_rows_from_confusion_counts(self) -> None:
        scorer = FakeScorer({_FAKE_CONTACT_TEXT: 0.55, _FAKE_NONCONTACT_TEXT: 0.1})
        results = eval_maya_pii_hazard.run_eval(scorer, self._fixtures(), threshold=0.5)
        metrics = eval_maya_pii_hazard.compute_metrics(results, abstain_low=0.45, abstain_high=0.6)
        assert metrics.n_abstained == 1
        assert metrics.n_scored == 1

    def test_percentile_handles_single_value(self) -> None:
        assert eval_maya_pii_hazard._percentile([3.0], 0.95) == 3.0

    def test_markdown_never_contains_fixture_text(self) -> None:
        scorer = FakeScorer({_FAKE_CONTACT_TEXT: 0.9, _FAKE_NONCONTACT_TEXT: 0.1})
        results = eval_maya_pii_hazard.run_eval(scorer, self._fixtures(), threshold=0.5)
        metrics = eval_maya_pii_hazard.compute_metrics(results)
        markdown = eval_maya_pii_hazard.render_markdown(metrics, threshold=0.5, abstain_band=None)
        assert _FAKE_CONTACT_TEXT not in markdown
        assert _FAKE_NONCONTACT_TEXT not in markdown
        assert "jordan.reyes" not in markdown


class TestResolveWeightsPath:
    def test_refuses_when_env_unset_and_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(eval_maya_pii_hazard.WEIGHTS_PATH_ENV, raising=False)
        with pytest.raises(eval_maya_pii_hazard.MayaWeightsUnavailable, match="not set"):
            eval_maya_pii_hazard.resolve_weights_path(None)

    def test_refuses_when_path_is_not_a_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        with pytest.raises(eval_maya_pii_hazard.MayaWeightsUnavailable, match="not a directory"):
            eval_maya_pii_hazard.resolve_weights_path(str(missing))

    def test_accepts_explicit_override_directory(self, tmp_path: Path) -> None:
        assert eval_maya_pii_hazard.resolve_weights_path(str(tmp_path)) == tmp_path


class TestMainFullScriptPathWithFakeScorer:
    """Smoke test: exercises main() end to end -- CLI parsing, fixture
    loading (both sources), the eval loop, metrics, and the markdown file
    write -- with only the model-construction seam faked (issue
    athenaeum#2049 AC3)."""

    def test_main_writes_markdown_with_no_fixture_leakage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        weights_dir = tmp_path / "fake-weights"
        weights_dir.mkdir()
        output_path = tmp_path / "results.md"

        fake_scorer = FakeScorer(
            {
                _FAKE_CONTACT_TEXT: 0.9,
                _FAKE_NONCONTACT_TEXT: 0.1,
                _FAKE_ALLOWLISTED_VALUE: 0.2,
            }
        )
        monkeypatch.setattr(eval_maya_pii_hazard, "_build_scorer", lambda weights_path: fake_scorer)

        exit_code = eval_maya_pii_hazard.main(
            [
                "--fixtures",
                str(fixtures_path),
                "--allowlist",
                str(allowlist_path),
                "--output",
                str(output_path),
                "--weights-path",
                str(weights_dir),
            ]
        )

        assert exit_code == 0
        assert output_path.exists()
        text = output_path.read_text(encoding="utf-8")
        assert _FAKE_CONTACT_TEXT not in text
        assert _FAKE_NONCONTACT_TEXT not in text
        assert _FAKE_ALLOWLISTED_VALUE not in text
        assert "Maya PII-hazard eval" in text
        assert "| accuracy |" in text
        # Never imported torch for the fake path.
        assert "torch" not in __import__("sys").modules

    def test_main_refuses_without_weights_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(eval_maya_pii_hazard.WEIGHTS_PATH_ENV, raising=False)
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text(json.dumps({"text": "x", "label": False}) + "\n", encoding="utf-8")
        exit_code = eval_maya_pii_hazard.main(
            ["--fixtures", str(fixtures_path), "--output", str(tmp_path / "out.md")]
        )
        assert exit_code == 1
        assert "torch" not in __import__("sys").modules

    def test_main_requires_at_least_one_fixture_source(self, tmp_path: Path) -> None:
        exit_code = eval_maya_pii_hazard.main(["--output", str(tmp_path / "out.md")])
        assert exit_code == 2


@pytest.mark.skipif(
    not os.environ.get(eval_maya_pii_hazard.WEIGHTS_PATH_ENV),
    reason=(
        f"{eval_maya_pii_hazard.WEIGHTS_PATH_ENV} not set -- "
        "real-model branch requires local weights"
    ),
)
class TestRealMayaAdapter:
    """Only runs host-side, with real weights present. Never runs in this
    repo's default CI (issue athenaeum#2049 AC: no implicit download)."""

    def test_adapter_loads_and_scores(self, tmp_path: Path) -> None:
        pytest.importorskip("transformers")
        pytest.importorskip("torch")
        weights_path = Path(os.environ[eval_maya_pii_hazard.WEIGHTS_PATH_ENV]).expanduser()
        adapter = eval_maya_pii_hazard.MayaAdapter(weights_path)
        p_yes = adapter.p_yes(eval_maya_pii_hazard.QUESTION, _FAKE_NONCONTACT_TEXT)
        assert 0.0 <= p_yes <= 1.0
