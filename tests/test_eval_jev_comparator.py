# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/eval_jev_comparator.py`` (issue athenaeum#2008).

Entirely offline: a fake :class:`~eval_jev_comparator.DecisionClient`
stands in for Jev (no network call is ever made in a test), and pair
selection is exercised against a synthetic ``wiki_root`` fixture, never a
real knowledge store.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from athenaeum._retry import TransientError
from athenaeum.decision_provider import DecisionResult
from athenaeum.verdicts import Basis, build_verdict_entry, content_hash

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_jev_comparator.py"
_SPEC = importlib.util.spec_from_file_location("eval_jev_comparator", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
m = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("eval_jev_comparator", m)
_SPEC.loader.exec_module(m)


class FakeClient:
    """A scripted :class:`eval_jev_comparator.DecisionClient` -- returns the
    next queued :class:`DecisionResult`, or raises the next queued
    exception, per call. Never touches the network."""

    def __init__(self, responses: list[DecisionResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def decide(
        self,
        *,
        question_id: str,
        kind: str,
        instructions: str,
        state: Any,
        criteria: Any = None,
    ) -> DecisionResult:
        self.calls.append(
            {
                "question_id": question_id,
                "kind": kind,
                "instructions": instructions,
                "state": state,
                "criteria": criteria,
            }
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _page(text: str, page_id: str = "p"):
    from athenaeum.comparator import page_from_text

    return page_from_text(page_id, text)


def _sample(pair_key: str = "a+b", claude_verdict: str = "distinct") -> "m.PairSample":
    return m.PairSample(
        pair_key=pair_key,
        id_a="a",
        id_b="b",
        claude_verdict=claude_verdict,
        page_a=_page("# A\n\nPage A body text here.", "a"),
        page_b=_page("# B\n\nPage B body text, unrelated.", "b"),
    )


class TestBuildState:
    def test_truncates_each_body_to_4000_chars(self) -> None:
        long_body = "x" * 10_000
        sample = m.PairSample(
            pair_key="a+b",
            id_a="a",
            id_b="b",
            claude_verdict="distinct",
            page_a=_page("# A\n\n" + long_body, "a"),
            page_b=_page("# B\n\nshort", "b"),
        )
        state = m._build_state(sample)
        assert len(state["page_a"]) == m.BODY_MAX_CHARS
        assert state["page_b"] == "# B\n\nshort"


class TestAskJev:
    def test_success_records_answer(self) -> None:
        client = FakeClient(
            [
                DecisionResult(
                    choice="distinct",
                    probabilities={"distinct": 0.8, "duplicate": 0.2},
                    confidence=0.8,
                    input_tokens=120,
                    output_tokens=5,
                )
            ]
        )
        answer = m.ask_jev(client, _sample(), pair_index=0)
        assert answer.jev_choice == "distinct"
        assert answer.error is None
        assert answer.input_tokens == 120
        assert client.calls[0]["kind"] == "choice"
        assert client.calls[0]["criteria"] == m.CRITERIA
        assert set(client.calls[0]["criteria"]) == set(m.VERDICT_VALUES)

    def test_transient_error_is_recorded_not_raised(self) -> None:
        client = FakeClient([TransientError("jev http 503")] * 10)
        answer = m.ask_jev(client, _sample(), pair_index=0)
        assert answer.jev_choice is None
        assert answer.error is not None
        assert "transient" in answer.error

    def test_value_error_is_recorded_not_raised(self) -> None:
        client = FakeClient([ValueError("jev: unparseable response")])
        answer = m.ask_jev(client, _sample(), pair_index=0)
        assert answer.jev_choice is None
        assert answer.error == "ValueError"


class TestRunEval:
    def test_no_client_runs_no_calls(self) -> None:
        run = m.run_eval(None, [_sample()])
        assert run.answers == []
        assert run.attempted == 0

    def test_stops_at_cost_cap(self) -> None:
        # Each call reports 1,000,000,000 input tokens -> cost per call is
        # far above any sane cap, so the second call must never fire.
        responses = [
            DecisionResult(choice="distinct", input_tokens=1_000_000_000) for _ in range(5)
        ]
        client = FakeClient(responses)
        samples = [_sample(f"a{i}+b{i}") for i in range(5)]
        run = m.run_eval(client, samples, cost_cap_usd=0.01)
        assert run.attempted == 1
        assert run.stopped_on_cost_cap is True


class TestComputeMetrics:
    def test_agreement_and_confusion(self) -> None:
        answers = [
            m.JevAnswer("a+b", "distinct", "distinct", {"distinct": 0.9}, 0.9, 10, 1, 0.1),
            m.JevAnswer("c+d", "duplicate", "distinct", {"distinct": 0.6}, 0.6, 10, 1, 0.1),
            m.JevAnswer("e+f", "duplicate", "duplicate", {"duplicate": 0.4}, 0.4, 10, 1, 0.1),
            m.JevAnswer("g+h", "distinct", None, None, None, None, None, 0.1, error="boom"),
        ]
        metrics = m.compute_metrics(answers)
        assert metrics.scored == 3
        assert metrics.errored == 1
        assert metrics.overall_agreement == pytest.approx(2 / 3)
        assert metrics.confusion[("distinct", "distinct")] == 1
        assert metrics.confusion[("duplicate", "distinct")] == 1
        assert metrics.confusion[("duplicate", "duplicate")] == 1
        # One scored answer has a top probability below 0.5 (0.4).
        assert metrics.below_half_confidence == 1
        agree, total = metrics.per_verdict_agreement["duplicate"]
        assert (agree, total) == (1, 2)


class TestRenderMarkdown:
    def test_no_live_key_path_has_no_agreement_section(self) -> None:
        stats = m.SelectionStats(total_gate2_entries=10, selected=4)
        run = m.RunResult()
        metrics = m.compute_metrics([])
        text = m.render_markdown(
            stats=stats, run=run, metrics=metrics, live=False, cost_cap_usd=5.0
        )
        assert "Live Jev calls: skipped" in text
        assert "## Agreement" not in text

    def test_live_path_renders_confusion_matrix(self) -> None:
        stats = m.SelectionStats(total_gate2_entries=10, selected=1)
        answers = [m.JevAnswer("a+b", "distinct", "distinct", {"distinct": 0.9}, 0.9, 10, 1, 0.1)]
        run = m.RunResult(answers=answers, attempted=1, total_input_tokens=10, total_cost_usd=1e-6)
        metrics = m.compute_metrics(answers)
        text = m.render_markdown(stats=stats, run=run, metrics=metrics, live=True, cost_cap_usd=5.0)
        assert "## Agreement" in text
        assert "### Confusion matrix" in text
        assert "## Recommendation" in text


class TestSelectGate2Pairs:
    def _write_ledger_entry(
        self,
        wiki_root: Path,
        id_a: str,
        id_b: str,
        verdict: str,
        *,
        text_a: str,
        text_b: str,
        comparator_version: str = "v1.gate2",
    ) -> None:
        ledger_dir = wiki_root / "_verdicts"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        basis = Basis(
            content_hashes=[content_hash(text_a), content_hash(text_b)],
            comparator_version=comparator_version,
        )
        entry = build_verdict_entry(
            id_a, id_b, verdict, basis=basis, decided_by="comparator", at="2026-10-08"
        )
        with (ledger_dir / "2026-10.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def test_resolves_gate2_pair_and_excludes_gate1(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        (wiki_root / "alpha.md").write_text("# Alpha\n\nAlpha body.", encoding="utf-8")
        (wiki_root / "beta.md").write_text("# Beta\n\nBeta body.", encoding="utf-8")
        self._write_ledger_entry(
            wiki_root,
            "alpha",
            "beta",
            "distinct",
            text_a="# Alpha\n\nAlpha body.",
            text_b="# Beta\n\nBeta body.",
        )
        # A gate1 (typed-exit, no LLM call) entry for an unrelated pair must
        # never be selected.
        self._write_ledger_entry(
            wiki_root,
            "alpha",
            "beta",
            "distinct",
            text_a="# Alpha\n\nAlpha body.",
            text_b="# Beta\n\nBeta body.",
            comparator_version="v1.gate1",
        )

        samples, stats = m.select_gate2_pairs(wiki_root, allowlist=[])
        assert stats.total_gate2_entries == 1
        assert stats.selected == 1
        assert samples[0].claude_verdict == "distinct"
        assert samples[0].page_a.body.strip() == "# Alpha\n\nAlpha body."

    def test_skips_missing_source(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        (wiki_root / "alpha.md").write_text("# Alpha\n\nAlpha body.", encoding="utf-8")
        self._write_ledger_entry(
            wiki_root,
            "alpha",
            "ghost",
            "distinct",
            text_a="# Alpha\n\nAlpha body.",
            text_b="never written",
        )
        samples, stats = m.select_gate2_pairs(wiki_root, allowlist=[])
        assert stats.skipped_missing_source == 1
        assert samples == []

    def test_skips_pii_hazard(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        text_a = "---\npii: true\n---\n\nAlpha body with a hazard flag."
        text_b = "# Beta\n\nBeta body."
        (wiki_root / "alpha.md").write_text(text_a, encoding="utf-8")
        (wiki_root / "beta.md").write_text(text_b, encoding="utf-8")
        self._write_ledger_entry(
            wiki_root, "alpha", "beta", "distinct", text_a=text_a, text_b=text_b
        )
        samples, stats = m.select_gate2_pairs(wiki_root, allowlist=[])
        assert stats.skipped_pii_hazard == 1
        assert samples == []

    def test_drift_without_git_history_is_skipped_and_counted(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # The ledger records a hash for text that was never written to disk
        # (no git history exists here at all) -- current text can never
        # match, and there is no history to recover it from.
        (wiki_root / "alpha.md").write_text("# Alpha\n\nCHANGED body.", encoding="utf-8")
        (wiki_root / "beta.md").write_text("# Beta\n\nBeta body.", encoding="utf-8")
        self._write_ledger_entry(
            wiki_root,
            "alpha",
            "beta",
            "distinct",
            text_a="# Alpha\n\nOriginal body.",
            text_b="# Beta\n\nBeta body.",
        )
        samples, stats = m.select_gate2_pairs(wiki_root, allowlist=[])
        assert stats.skipped_content_drift == 1
        assert samples == []


class TestMain:
    def test_main_skips_live_without_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JEV_API_KEY", raising=False)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        output = tmp_path / "out.md"
        rc = m.main(["--wiki-root", str(wiki_root), "--output", str(output)])
        assert rc == 0
        text = output.read_text(encoding="utf-8")
        assert "Live Jev calls: skipped" in text
