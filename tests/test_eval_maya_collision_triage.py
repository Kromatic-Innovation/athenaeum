# SPDX-License-Identifier: Apache-2.0
"""Tests for the Maya collision-triage ordering eval (issue athenaeum#2066).

Loaded via ``importlib.util.spec_from_file_location``, the same pattern
``test_eval_maya_pii_hazard.py`` uses for a ``scripts/`` module. The
synthetic smoke test below uses a fake :class:`PairScorer` and fabricated
names/descriptions -- never anything corpus-derived -- and exercises the
FULL script path. A separate real-model test is skipped (not failed) when
``ATHENAEUM_MAYA_WEIGHTS_PATH`` is absent or ``transformers`` is not
installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_maya_collision_triage.py"

_spec = importlib.util.spec_from_file_location("eval_maya_collision_triage", _SCRIPT)
assert _spec and _spec.loader
eval_maya_collision_triage = importlib.util.module_from_spec(_spec)
sys.modules["eval_maya_collision_triage"] = eval_maya_collision_triage
_spec.loader.exec_module(eval_maya_collision_triage)


# Fabricated, never corpus-derived.
_RECORD_A_SAME = "Jordan Reyes\n\nSenior engineer on the platform team, joined 2024."
_RECORD_B_SAME = "Jordan R. Reyes\n\nPlatform engineering, started last year."
_RECORD_A_DIFF = "Taylor Kim\n\nMarketing lead for the north region."
_RECORD_B_DIFF = "Taylor Kim\n\nFreelance illustrator, works mostly in gouache."


class FakePairScorer:
    """A :class:`eval_maya_collision_triage.PairScorer` with hand-assigned
    P(yes) per fixed combined text -- no model, no torch, no transformers."""

    def __init__(self, mapping: dict[str, float], token_counts: dict[str, int] | None = None):
        self._mapping = mapping
        self._token_counts = token_counts or {}

    def p_yes(self, question: str, text: str) -> float:
        assert question == eval_maya_collision_triage.QUESTION
        return self._mapping[text]

    def token_count(self, question: str, text: str) -> int:
        # Word-count-based approximation -- fine for a test fake; the real
        # adapter uses the actual tokenizer.
        return self._token_counts.get(text, len(text.split()))


class TestLoadFixturesJsonl:
    def test_loads_pair_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(
            json.dumps({"text_a": _RECORD_A_SAME, "text_b": _RECORD_B_SAME, "label": True})
            + "\n"
            + json.dumps({"text_a": _RECORD_A_DIFF, "text_b": _RECORD_B_DIFF, "label": False})
            + "\n",
            encoding="utf-8",
        )
        fixtures = eval_maya_collision_triage.load_fixtures_jsonl(path)
        assert len(fixtures) == 2
        assert fixtures[0].label is True
        assert fixtures[1].label is False

    def test_skips_blank_and_comment_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(
            "\n# a comment\n" + json.dumps({"text_a": "a", "text_b": "b", "label": False}) + "\n",
            encoding="utf-8",
        )
        fixtures = eval_maya_collision_triage.load_fixtures_jsonl(path)
        assert len(fixtures) == 1

    def test_rejects_non_bool_label(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(
            json.dumps({"text_a": "a", "text_b": "b", "label": "yes"}) + "\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="strict bool"):
            eval_maya_collision_triage.load_fixtures_jsonl(path)

    def test_rejects_missing_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "fixtures.jsonl"
        path.write_text(json.dumps({"text_a": "a"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="text_a.*text_b.*label"):
            eval_maya_collision_triage.load_fixtures_jsonl(path)


class TestBuildPairText:
    def test_combines_both_records(self) -> None:
        combined = eval_maya_collision_triage.build_pair_text("hello", "world")
        assert "Record A: hello" in combined
        assert "Record B: world" in combined

    def test_truncates_each_side_independently(self) -> None:
        long_a = "x" * 5000
        short_b = "short record"
        combined = eval_maya_collision_triage.build_pair_text(long_a, short_b)
        # Record B must survive even though A is far longer than the budget.
        assert "Record B: short record" in combined
        assert len(combined) < 5000


class TestRunEvalAndMetrics:
    def _fixtures(self) -> list:
        PairFixture = eval_maya_collision_triage.PairFixture
        return [
            PairFixture(text_a=_RECORD_A_SAME, text_b=_RECORD_B_SAME, label=True),
            PairFixture(text_a=_RECORD_A_DIFF, text_b=_RECORD_B_DIFF, label=False),
        ]

    def test_run_eval_produces_one_result_per_fixture(self) -> None:
        fixtures = self._fixtures()
        same_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        diff_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_DIFF, _RECORD_B_DIFF)
        scorer = FakePairScorer({same_text: 0.9, diff_text: 0.1})
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        assert len(results) == 2
        assert results[0].p_yes == 0.9
        assert results[1].p_yes == 0.1

    def test_auroc_perfect_separation(self) -> None:
        fixtures = self._fixtures()
        same_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        diff_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_DIFF, _RECORD_B_DIFF)
        scorer = FakePairScorer({same_text: 0.9, diff_text: 0.1})
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        metrics = eval_maya_collision_triage.compute_metrics(results)
        assert metrics.auroc == pytest.approx(1.0)

    def test_auroc_none_when_one_class_empty(self) -> None:
        PairFixture = eval_maya_collision_triage.PairFixture
        fixtures = [PairFixture(text_a=_RECORD_A_SAME, text_b=_RECORD_B_SAME, label=True)]
        same_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        scorer = FakePairScorer({same_text: 0.9})
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        metrics = eval_maya_collision_triage.compute_metrics(results)
        assert metrics.auroc is None
        markdown = eval_maya_collision_triage.render_markdown(metrics, temperature=1.0)
        assert "Inconclusive" in markdown

    def test_auroc_handles_ties_with_mid_rank(self) -> None:
        """Two pairs tied at the same P(yes), one positive one negative:
        AUROC must be 0.5 (the tie contributes its average rank to both),
        not an arbitrary 0 or 1 from iteration order."""
        PairFixture = eval_maya_collision_triage.PairFixture
        fixtures = [
            PairFixture(text_a="a1", text_b="b1", label=True),
            PairFixture(text_a="a2", text_b="b2", label=False),
        ]
        tied_text_1 = eval_maya_collision_triage.build_pair_text("a1", "b1")
        tied_text_2 = eval_maya_collision_triage.build_pair_text("a2", "b2")
        scorer = FakePairScorer({tied_text_1: 0.5, tied_text_2: 0.5})
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        metrics = eval_maya_collision_triage.compute_metrics(results)
        assert metrics.auroc == pytest.approx(0.5)

    def test_precision_at_k_none_when_fewer_rows_than_k(self) -> None:
        results = [
            eval_maya_collision_triage.PairResult(
                label=True, p_yes=0.9, latency_s=0.01, over_length=False
            )
        ]
        assert eval_maya_collision_triage.precision_at_k(results, 10) is None
        assert eval_maya_collision_triage.precision_at_k_current_order(results, 10) is None

    def test_precision_at_k_orders_by_descending_p_yes(self) -> None:
        R = eval_maya_collision_triage.PairResult
        results = [
            R(label=False, p_yes=0.2, latency_s=0.01, over_length=False),
            R(label=True, p_yes=0.9, latency_s=0.01, over_length=False),
        ]
        # Maya order puts the positive first -> precision@1 == 1.0
        assert eval_maya_collision_triage.precision_at_k(results, 1) == pytest.approx(1.0)
        # Current (fixture/ledger) order has the negative first -> precision@1 == 0.0
        assert eval_maya_collision_triage.precision_at_k_current_order(results, 1) == pytest.approx(
            0.0
        )

    def test_over_length_fraction(self) -> None:
        PairFixture = eval_maya_collision_triage.PairFixture
        fixtures = [
            PairFixture(text_a="a1", text_b="b1", label=True),
            PairFixture(text_a="a2", text_b="b2", label=False),
        ]
        t1 = eval_maya_collision_triage.build_pair_text("a1", "b1")
        t2 = eval_maya_collision_triage.build_pair_text("a2", "b2")
        full_t1 = "Record A: a1\n\nRecord B: b1"
        full_t2 = "Record A: a2\n\nRecord B: b2"
        scorer = FakePairScorer(
            {t1: 0.5, t2: 0.5},
            token_counts={full_t1: 600, full_t2: 100},
        )
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        metrics = eval_maya_collision_triage.compute_metrics(results)
        assert metrics.n_over_length == 1
        assert metrics.over_length_fraction == pytest.approx(0.5)

    def test_markdown_never_contains_fixture_text(self) -> None:
        fixtures = self._fixtures()
        same_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        diff_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_DIFF, _RECORD_B_DIFF)
        scorer = FakePairScorer({same_text: 0.9, diff_text: 0.1})
        results = eval_maya_collision_triage.run_eval(scorer, fixtures)
        metrics = eval_maya_collision_triage.compute_metrics(results)
        markdown = eval_maya_collision_triage.render_markdown(metrics, temperature=1.0)
        assert "Jordan" not in markdown
        assert "Taylor" not in markdown


class TestResolveWeightsPath:
    def test_refuses_when_env_unset_and_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(eval_maya_collision_triage.WEIGHTS_PATH_ENV, raising=False)
        with pytest.raises(eval_maya_collision_triage.MayaWeightsUnavailable, match="not set"):
            eval_maya_collision_triage.resolve_weights_path(None)

    def test_refuses_when_path_is_not_a_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        with pytest.raises(
            eval_maya_collision_triage.MayaWeightsUnavailable, match="not a directory"
        ):
            eval_maya_collision_triage.resolve_weights_path(str(missing))

    def test_accepts_explicit_override_directory(self, tmp_path: Path) -> None:
        assert eval_maya_collision_triage.resolve_weights_path(str(tmp_path)) == tmp_path


class TestMainFullScriptPathWithFakeScorer:
    """Smoke test: exercises main() end to end with only the
    model-construction seam faked (issue athenaeum#2066, mirrors athenaeum#2049 AC3)."""

    def test_main_writes_markdown_with_no_fixture_leakage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text(
            json.dumps({"text_a": _RECORD_A_SAME, "text_b": _RECORD_B_SAME, "label": True})
            + "\n"
            + json.dumps({"text_a": _RECORD_A_DIFF, "text_b": _RECORD_B_DIFF, "label": False})
            + "\n",
            encoding="utf-8",
        )
        weights_dir = tmp_path / "fake-weights"
        weights_dir.mkdir()
        output_path = tmp_path / "results.md"

        same_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        diff_text = eval_maya_collision_triage.build_pair_text(_RECORD_A_DIFF, _RECORD_B_DIFF)
        fake_scorer = FakePairScorer({same_text: 0.9, diff_text: 0.1})
        monkeypatch.setattr(
            eval_maya_collision_triage,
            "_build_scorer",
            lambda weights_path: fake_scorer,
        )

        exit_code = eval_maya_collision_triage.main(
            [
                "--fixtures",
                str(fixtures_path),
                "--output",
                str(output_path),
                "--weights-path",
                str(weights_dir),
            ]
        )

        assert exit_code == 0
        assert output_path.exists()
        text = output_path.read_text(encoding="utf-8")
        assert "Jordan" not in text
        assert "Taylor" not in text
        assert "Maya collision-triage ordering eval" in text
        assert "AUROC" in text
        assert "torch" not in sys.modules

    def test_main_refuses_without_weights_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(eval_maya_collision_triage.WEIGHTS_PATH_ENV, raising=False)
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text(
            json.dumps({"text_a": "x", "text_b": "y", "label": False}) + "\n", encoding="utf-8"
        )
        exit_code = eval_maya_collision_triage.main(
            ["--fixtures", str(fixtures_path), "--output", str(tmp_path / "out.md")]
        )
        assert exit_code == 1
        assert "torch" not in sys.modules

    def test_main_refuses_without_fixtures_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures_path = tmp_path / "fixtures.jsonl"
        fixtures_path.write_text("", encoding="utf-8")
        weights_dir = tmp_path / "fake-weights"
        weights_dir.mkdir()
        exit_code = eval_maya_collision_triage.main(
            [
                "--fixtures",
                str(fixtures_path),
                "--output",
                str(tmp_path / "out.md"),
                "--weights-path",
                str(weights_dir),
            ]
        )
        assert exit_code == 2


@pytest.mark.skipif(
    not os.environ.get(eval_maya_collision_triage.WEIGHTS_PATH_ENV),
    reason=(
        f"{eval_maya_collision_triage.WEIGHTS_PATH_ENV} not set -- "
        "real-model branch requires local weights"
    ),
)
class TestRealMayaAdapter:
    """Only runs host-side, with real weights present. Never runs in this
    repo's default CI (issue athenaeum#2066: no implicit download)."""

    def test_adapter_loads_and_scores(self) -> None:
        pytest.importorskip("transformers")
        pytest.importorskip("torch")
        weights_path = Path(os.environ[eval_maya_collision_triage.WEIGHTS_PATH_ENV]).expanduser()
        scorer = eval_maya_collision_triage._build_scorer(weights_path)
        combined = eval_maya_collision_triage.build_pair_text(_RECORD_A_SAME, _RECORD_B_SAME)
        p_yes = scorer.p_yes(eval_maya_collision_triage.QUESTION, combined)
        assert 0.0 <= p_yes <= 1.0
