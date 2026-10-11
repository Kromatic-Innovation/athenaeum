# SPDX-License-Identifier: Apache-2.0
"""Tests for the Jev-vs-current-model `claim_kind` eval script (issue athenaeum#1998).

Loaded via ``importlib.util.spec_from_file_location``, the same pattern
``test_eval_maya_pii_hazard.py`` uses, since ``scripts/`` is not an
importable package. Every text used below is fabricated inline -- never read
from ``~/knowledge`` -- so this test makes no network call, needs no
credential, and touches no real corpus file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_jev_claim_kind.py"

_spec = importlib.util.spec_from_file_location("eval_jev_claim_kind", _SCRIPT)
assert _spec and _spec.loader
eval_jev_claim_kind = importlib.util.module_from_spec(_spec)
sys.modules["eval_jev_claim_kind"] = eval_jev_claim_kind
_spec.loader.exec_module(eval_jev_claim_kind)


def _fixture_text(claim_kind: str, body: str) -> str:
    return f"---\nname: synthetic\nclaim_kind: {claim_kind}\n---\n{body}\n"


# Fabricated synthetic snippets, one per kind, never corpus-derived.
_SYNTHETIC_BY_KIND = {
    "fact": _fixture_text("fact", "The develop tip was SHA deadbeef on the day this was written."),
    "observation": _fixture_text("observation", "The staging deploy failed with a 502 at 14:03."),
    "opinion": _fixture_text("opinion", "Tabs are better than spaces."),
    "decision": _fixture_text("decision", "We pivoted the queue from polling to a webhook."),
    "policy": _fixture_text("policy", "Always merge green PRs before end of day."),
    "definition": _fixture_text("definition", "A 'lane' is a single repo's work queue."),
}


class FakeAnthropic:
    """A :class:`ClassifyBackend` returning a fixed label per input text."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls = 0

    def classify(self, text: str) -> tuple[str, int, int]:
        self.calls += 1
        return self._mapping[text], 100, 5


class FakeJev:
    """A :class:`DecideBackend` returning a fixed label per input snippet."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls = 0

    def classify(self, snippet: str) -> tuple[str, int, int]:
        self.calls += 1
        return self._mapping[snippet], 80, 0


def test_discover_cases_follows_symlinked_subdirectories(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "a.md").write_text(_SYNTHETIC_BY_KIND["fact"], encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(real_dir, target_is_directory=True)

    by_label = eval_jev_claim_kind.discover_cases(root)
    assert len(by_label["fact"]) == 1
    assert sum(len(v) for v in by_label.values()) == 1


def test_discover_cases_skips_unclassified_and_invalid(tmp_path: Path) -> None:
    (tmp_path / "unclassified.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    (tmp_path / "bad.md").write_text(
        "---\nname: x\nclaim_kind: not-a-real-kind\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "good.md").write_text(_SYNTHETIC_BY_KIND["policy"], encoding="utf-8")

    by_label = eval_jev_claim_kind.discover_cases(tmp_path)
    assert sum(len(v) for v in by_label.values()) == 1
    assert len(by_label["policy"]) == 1


def test_sample_stratified_is_deterministic_and_bounded_by_pool_size() -> None:
    by_label = {
        kind: [
            eval_jev_claim_kind.Case(case_id=f"{kind}-{i}", text="x", label=kind) for i in range(3)
        ]
        for kind in eval_jev_claim_kind.CLAIM_KINDS
    }
    # Two kinds are empty -- the sample must come back short of the request,
    # never padded by over-drawing another kind.
    by_label["opinion"] = []
    by_label["definition"] = []

    sample_a, available_a = eval_jev_claim_kind.sample_stratified(by_label, n=50, seed=42)
    sample_b, available_b = eval_jev_claim_kind.sample_stratified(by_label, n=50, seed=42)

    assert len(sample_a) == 3 * 4  # 4 non-empty kinds x 3 each
    assert [c.case_id for c in sample_a] == [c.case_id for c in sample_b]
    assert available_a["opinion"] == 0
    assert available_a == available_b


def test_run_eval_alternates_call_order_and_uses_identical_snippet_both_arms() -> None:
    cases = [
        eval_jev_claim_kind.Case(case_id="c0", text=_SYNTHETIC_BY_KIND["fact"], label="fact"),
        eval_jev_claim_kind.Case(case_id="c1", text=_SYNTHETIC_BY_KIND["opinion"], label="opinion"),
    ]
    from athenaeum.claim_kind import _snippet

    anthropic_map = {c.text: c.label for c in cases}
    jev_map = {_snippet(c.text): c.label for c in cases}

    anthropic = FakeAnthropic(anthropic_map)
    jev = FakeJev(jev_map)

    results = eval_jev_claim_kind.run_eval(cases, anthropic, jev)

    assert len(results) == 2
    assert results[0].first_arm == "anthropic"
    assert results[1].first_arm == "jev"
    assert results[0].anthropic_pred == "fact"
    assert results[0].jev_pred == "fact"
    assert results[1].anthropic_pred == "opinion"
    assert results[1].jev_pred == "opinion"
    # One warm-up call per arm plus one per scored case.
    assert anthropic.calls == 2 + 1
    assert jev.calls == 2 + 1


def test_summarize_accuracy_confusion_and_agreement() -> None:
    results = [
        eval_jev_claim_kind.CaseResult(
            case_id="c0",
            label="fact",
            first_arm="anthropic",
            anthropic_pred="fact",
            anthropic_latency_s=0.1,
            anthropic_input_tokens=100,
            anthropic_output_tokens=5,
            jev_pred="fact",
            jev_latency_s=0.05,
            jev_input_tokens=80,
            jev_output_tokens=0,
        ),
        eval_jev_claim_kind.CaseResult(
            case_id="c1",
            label="opinion",
            first_arm="jev",
            anthropic_pred="fact",  # wrong
            anthropic_latency_s=0.2,
            anthropic_input_tokens=100,
            anthropic_output_tokens=5,
            jev_pred="opinion",  # right
            jev_latency_s=0.07,
            jev_input_tokens=80,
            jev_output_tokens=0,
        ),
    ]
    summary = eval_jev_claim_kind.summarize(
        results,
        available_counts={k: 1 for k in eval_jev_claim_kind.CLAIM_KINDS},
        anthropic_cost_usd=0.0,
    )
    assert summary.n == 2
    assert summary.anthropic.accuracy == pytest.approx(0.5)
    assert summary.jev.accuracy == pytest.approx(1.0)
    assert summary.agreement_rate == pytest.approx(0.5)
    assert summary.anthropic.confusion[("opinion", "fact")] == 1
    assert summary.jev.confusion[("opinion", "opinion")] == 1
    assert summary.jev_cost_usd == pytest.approx(
        160 * eval_jev_claim_kind.JEV_USD_PER_MTOK_INPUT / 1_000_000
    )


def test_render_markdown_never_includes_fixture_text() -> None:
    results = [
        eval_jev_claim_kind.CaseResult(
            case_id="c0",
            label="fact",
            first_arm="anthropic",
            anthropic_pred="fact",
            anthropic_latency_s=0.1,
            anthropic_input_tokens=100,
            anthropic_output_tokens=5,
            jev_pred="fact",
            jev_latency_s=0.05,
            jev_input_tokens=80,
            jev_output_tokens=0,
        ),
    ]
    summary = eval_jev_claim_kind.summarize(
        results,
        available_counts={k: 1 for k in eval_jev_claim_kind.CLAIM_KINDS},
        anthropic_cost_usd=0.0,
    )
    markdown = eval_jev_claim_kind.render_markdown(
        summary,
        network_note="synthetic smoke test, no real network call",
        sample_size_requested=50,
        seed=42,
    )
    # The doc legitimately mentions "~/knowledge" in its own prose (the
    # default sample-dir / provenance-limitation text) -- what must never
    # appear is actual fixture content or a real sample-dir path.
    assert "The develop tip" not in markdown  # a fixture body string
    assert str(Path.home()) not in markdown
    assert "# Jev vs. current-model eval" in markdown
    assert "Majority-class baseline" in markdown


def test_main_end_to_end_with_fake_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_dir = tmp_path / "auto-memory"
    sample_dir.mkdir()
    for kind, text in _SYNTHETIC_BY_KIND.items():
        (sample_dir / f"{kind}.md").write_text(text, encoding="utf-8")

    from athenaeum.claim_kind import _snippet

    anthropic_map = {text: kind for kind, text in _SYNTHETIC_BY_KIND.items()}
    jev_map = {_snippet(text): kind for kind, text in _SYNTHETIC_BY_KIND.items()}

    monkeypatch.setattr(
        eval_jev_claim_kind, "_build_anthropic_backend", lambda config: FakeAnthropic(anthropic_map)
    )
    monkeypatch.setattr(eval_jev_claim_kind, "_build_jev_backend", lambda: FakeJev(jev_map))

    output = tmp_path / "report.md"
    rc = eval_jev_claim_kind.main(
        [
            "--sample-dir",
            str(sample_dir),
            "--sample-size",
            "50",
            "--seed",
            "7",
            "--output",
            str(output),
            "--network-note",
            "synthetic smoke test, no real network call",
        ]
    )
    assert rc == 0
    content = output.read_text(encoding="utf-8")
    assert "accuracy | 1.000" in content
    assert "Order no." not in content


def test_main_refuses_when_no_labelled_cases(tmp_path: Path) -> None:
    sample_dir = tmp_path / "empty"
    sample_dir.mkdir()
    output = tmp_path / "report.md"
    rc = eval_jev_claim_kind.main(
        [
            "--sample-dir",
            str(sample_dir),
            "--output",
            str(output),
            "--network-note",
            "n/a",
        ]
    )
    assert rc == 2
    assert not output.exists()
