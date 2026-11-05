# SPDX-License-Identifier: Apache-2.0
"""Tests for scoring the `used` heuristic against human labels (issue
athenaeum#2302).

Every fixture here is HAND-AUTHORED SYNTHETIC DATA — no real transcript
content, per this module's hard safety constraint (see
``src/athenaeum/push_used_scoring.py``'s module docstring). Covers: the
Wilson score interval (known values, CI widening as n shrinks), the pure
scorer against a hand-built known confusion matrix, the
`relevant_but_not_opened` / `irrelevant` breakdown, the no-identifiers-in-
output guarantee, the stratified sampler, the label-file loader, and that
:func:`score_against_heuristic` drives the REAL
:func:`athenaeum.push_metrics.determine_references` rather than
reimplementing the rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import push_metrics
from athenaeum import push_used_scoring as pus

# ---------------------------------------------------------------------------
# Wilson score interval
# ---------------------------------------------------------------------------


def _ci(
    successes: int, n: int, *, confidence: float = pus.DEFAULT_CONFIDENCE_LEVEL
) -> tuple[float, float]:
    """Narrowing helper: :func:`wilson_score_interval` is only ``None`` at
    ``n == 0``, which every caller below avoids — asserting that here lets
    mypy (and a reader) see the unpack below is never against ``None``."""
    result = pus.wilson_score_interval(successes, n, confidence=confidence)
    assert result is not None
    return result


class TestWilsonScoreInterval:
    def test_none_when_n_zero(self) -> None:
        assert pus.wilson_score_interval(0, 0) is None

    def test_bounded_in_unit_interval(self) -> None:
        lo, hi = _ci(0, 5)
        assert 0.0 <= lo <= hi <= 1.0
        lo, hi = _ci(5, 5)
        assert 0.0 <= lo <= hi <= 1.0

    def test_known_value_matches_hand_computation(self) -> None:
        # p_hat = 0.5, n = 100, z = 1.959963984540054 (95%).
        # Hand-computed via the Wilson closed form (module docstring):
        #   denom = 1 + z^2/n
        #   center = p_hat + z^2/(2n)
        #   margin = z * sqrt((p_hat*(1-p_hat) + z^2/(4n)) / n)
        z = 1.959963984540054
        n = 100
        p_hat = 0.5
        denom = 1 + z**2 / n
        center = p_hat + z**2 / (2 * n)
        margin = z * ((p_hat * (1 - p_hat) + z**2 / (4 * n)) / n) ** 0.5
        expected_lo = (center - margin) / denom
        expected_hi = (center + margin) / denom
        lo, hi = _ci(50, 100)
        assert lo == pytest.approx(expected_lo)
        assert hi == pytest.approx(expected_hi)

    def test_interval_widens_as_sample_shrinks(self) -> None:
        """Same proportion (0.5), smaller n => a wider interval — the
        athenaeum#2302 acceptance criterion."""
        lo_small, hi_small = _ci(5, 10)
        lo_large, hi_large = _ci(500, 1000)
        width_small = hi_small - lo_small
        width_large = hi_large - lo_large
        assert width_small > width_large

    def test_interval_monotonically_widens_across_three_sizes(self) -> None:
        """Belt-and-suspenders: the same check at three nested sample sizes,
        all at the identical 0.5 proportion."""
        widths = []
        for n in (20, 200, 2000):
            lo, hi = _ci(n // 2, n)
            widths.append(hi - lo)
        assert widths[0] > widths[1] > widths[2]

    def test_unsupported_confidence_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported confidence"):
            pus.wilson_score_interval(1, 2, confidence=0.5)

    def test_invalid_counts_raise(self) -> None:
        with pytest.raises(ValueError):
            pus.wilson_score_interval(5, 2)
        with pytest.raises(ValueError):
            pus.wilson_score_interval(-1, 2)


# ---------------------------------------------------------------------------
# Known confusion matrix (AC: hand-built labels + heuristic verdicts; assert
# precision/recall equal the hand-computed values)
# ---------------------------------------------------------------------------


class TestScoreLabelsKnownConfusionMatrix:
    def test_known_confusion_matrix(self) -> None:
        """6 TP, 2 FP, 2 FN, 10 TN by construction. Ground truth positive is
        `changed_work`; heuristic positive is presence in `heuristic_used`.

        Hand-computed expectation:
            precision = TP / (TP + FP) = 6 / 8    = 0.75
            recall    = TP / (TP + FN) = 6 / 8    = 0.75
        """
        labels: list[pus.PushPageLabel] = []
        heuristic_used: dict[tuple[str, str], bool] = {}

        # 6 true positives: changed_work=True, heuristic says used.
        for i in range(6):
            key = (f"sess-tp-{i}", f"push-tp-{i}")
            labels.append(
                pus.PushPageLabel(
                    session_id=key[0], push_id=key[1], relevant=True, changed_work=True
                )
            )
            heuristic_used[key] = True

        # 2 false positives: changed_work=False (irrelevant), heuristic
        # wrongly says used.
        for i in range(2):
            key = (f"sess-fp-{i}", f"push-fp-{i}")
            labels.append(
                pus.PushPageLabel(
                    session_id=key[0], push_id=key[1], relevant=False, changed_work=False
                )
            )
            heuristic_used[key] = True

        # 2 false negatives: changed_work=True, heuristic wrongly says not used.
        for i in range(2):
            key = (f"sess-fn-{i}", f"push-fn-{i}")
            labels.append(
                pus.PushPageLabel(
                    session_id=key[0], push_id=key[1], relevant=True, changed_work=True
                )
            )
            heuristic_used[key] = False

        # 10 true negatives, split 6 relevant-but-not-opened / 4 irrelevant,
        # so the unused breakdown has a known, non-degenerate split too.
        for i in range(6):
            key = (f"sess-tn-rel-{i}", f"push-tn-rel-{i}")
            labels.append(
                pus.PushPageLabel(
                    session_id=key[0], push_id=key[1], relevant=True, changed_work=False
                )
            )
            heuristic_used[key] = False
        for i in range(4):
            key = (f"sess-tn-irr-{i}", f"push-tn-irr-{i}")
            labels.append(
                pus.PushPageLabel(
                    session_id=key[0], push_id=key[1], relevant=False, changed_work=False
                )
            )
            heuristic_used[key] = False

        report = pus.score_labels(labels, heuristic_used)

        assert report.confusion == pus.ConfusionCounts(tp=6, fp=2, fn=2, tn=10)
        assert report.confusion.total == 20
        assert report.precision.rate == pytest.approx(0.75)
        assert report.precision.successes == 6
        assert report.precision.n == 8
        assert report.recall.rate == pytest.approx(0.75)
        assert report.recall.successes == 6
        assert report.recall.n == 8

        # Unused breakdown: 12 pages with changed_work=False (2 FP + 10 TN),
        # of which 6 relevant (ignored pointer) and 6 irrelevant (retrieval
        # miss) -- the 2 FP are irrelevant (relevant=False), joining the 4
        # TN-irrelevant for 6 total irrelevant; 6 TN-relevant stay relevant.
        assert report.unused_breakdown.unused_total == 12
        assert report.unused_breakdown.relevant_but_not_opened.successes == 6
        assert report.unused_breakdown.relevant_but_not_opened.rate == pytest.approx(6 / 12)
        assert report.unused_breakdown.irrelevant.successes == 6
        assert report.unused_breakdown.irrelevant.rate == pytest.approx(6 / 12)

    def test_absent_heuristic_entry_treated_as_not_used(self) -> None:
        labels = [
            pus.PushPageLabel(session_id="s", push_id="p", relevant=True, changed_work=True)
        ]
        report = pus.score_labels(labels, {})
        assert report.confusion == pus.ConfusionCounts(tp=0, fp=0, fn=1, tn=0)

    def test_empty_labels(self) -> None:
        report = pus.score_labels([], {})
        assert report.confusion == pus.ConfusionCounts(tp=0, fp=0, fn=0, tn=0)
        assert report.precision.rate is None
        assert report.recall.rate is None
        assert report.unused_breakdown.unused_total == 0
        assert report.unused_breakdown.relevant_but_not_opened.rate is None
        assert report.unused_breakdown.irrelevant.rate is None


# ---------------------------------------------------------------------------
# Scorer output contains rates and counts only
# ---------------------------------------------------------------------------


class TestScoreReportIdentifierFree:
    def test_score_report_contains_no_identifiers(self) -> None:
        secret_session = "sess-SUPER-SECRET-ID-99887766"
        secret_push = "push-SUPER-SECRET-PAGE-11223344"
        query_text = "the user's private query about their medical records"
        page_text = "distinctive page prose that must never leak into a report"

        labels = [
            pus.PushPageLabel(
                session_id=secret_session, push_id=secret_push, relevant=True, changed_work=True
            ),
            pus.PushPageLabel(
                session_id="sess-other", push_id="push-other", relevant=False, changed_work=False
            ),
        ]
        heuristic_used = {(secret_session, secret_push): True}
        report = pus.score_labels(labels, heuristic_used)

        serialized = json.dumps(report.to_dict())
        for forbidden in (secret_session, secret_push, query_text, page_text):
            assert forbidden not in serialized

    def test_to_dict_keys_are_rates_and_counts_only(self) -> None:
        report = pus.score_labels(
            [pus.PushPageLabel(session_id="s", push_id="p", relevant=True, changed_work=True)],
            {("s", "p"): True},
        )
        payload = report.to_dict()
        assert set(payload) == {
            "v",
            "generated",
            "confidence",
            "sample_size",
            "confusion",
            "precision",
            "recall",
            "unused_breakdown",
        }
        assert set(payload["confusion"]) == {"tp", "fp", "fn", "tn", "total"}
        assert set(payload["precision"]) == {
            "successes",
            "n",
            "confidence",
            "rate",
            "ci_low",
            "ci_high",
        }


# ---------------------------------------------------------------------------
# score_against_heuristic drives the REAL determine_references, never
# reimplementing the `used` rule.
# ---------------------------------------------------------------------------


def _write_transcript(projects_root: Path, session_id: str, records: list[dict]) -> None:
    scope_dir = projects_root / "-scope-hash"
    scope_dir.mkdir(parents=True, exist_ok=True)
    (scope_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


class TestScoreAgainstHeuristicDrivesTheRealFunction:
    def test_cited_page_is_used_uncited_page_is_not(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects_root = tmp_path / "projects"

        push_metrics.record_push(
            push_metrics.build_push_record(
                session_id="sess-real",
                query="q",
                backend="fts5",
                hits=[
                    ("f1.md", {"uid": "cited123"}, push_metrics.estimate_tokens("b")),
                    ("f2.md", {"uid": "neverused"}, push_metrics.estimate_tokens("b")),
                ],
            ),
            cache_dir=cache,
        )
        _write_transcript(
            projects_root,
            "sess-real",
            [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Used cited123 to answer."}],
                    },
                },
            ],
        )

        labels = [
            pus.PushPageLabel(
                session_id="sess-real", push_id="cited123", relevant=True, changed_work=True
            ),
            pus.PushPageLabel(
                session_id="sess-real", push_id="neverused", relevant=False, changed_work=False
            ),
        ]
        report = pus.score_against_heuristic(
            labels, cache_dir=cache, projects_root=projects_root
        )
        # cited123: changed_work=True and the REAL determine_references
        # marks it referenced -> TP. neverused: changed_work=False and
        # determine_references does not mark it referenced -> TN.
        assert report.confusion == pus.ConfusionCounts(tp=1, fp=0, fn=0, tn=1)
        assert report.unused_breakdown.unused_total == 1
        assert report.unused_breakdown.irrelevant.successes == 1

    def test_compute_heuristic_verdicts_calls_one_session_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the wrapper calls determine_references exactly once per
        DISTINCT session, never per labelled page -- and never reimplements
        any part of the rule itself."""
        cache = tmp_path / "cache"
        calls: list[str] = []
        real = push_metrics.determine_references

        def _spy(session_id: str, **kwargs: Any) -> push_metrics.ReferenceResult | None:
            calls.append(session_id)
            return real(session_id, **kwargs)

        monkeypatch.setattr(push_metrics, "determine_references", _spy)

        labels = [
            pus.PushPageLabel(session_id="sess-a", push_id="p1", relevant=True, changed_work=True),
            pus.PushPageLabel(session_id="sess-a", push_id="p2", relevant=True, changed_work=True),
            pus.PushPageLabel(session_id="sess-b", push_id="p3", relevant=True, changed_work=True),
        ]
        pus.compute_heuristic_verdicts(labels, cache_dir=cache, projects_root=tmp_path / "projects")
        assert sorted(calls) == ["sess-a", "sess-b"]


# ---------------------------------------------------------------------------
# Stratified sampler
# ---------------------------------------------------------------------------


class TestStratifiedSamplePushedPages:
    def _seed(self, cache: Path, session_id: str, push_count: int, page_ids: list[str]) -> None:
        # record_push no-ops on an empty-items record (push_metrics.record_push's
        # own documented contract: "no session id or no pushed items" ->
        # False, nothing written) -- so every one of the push_count calls
        # must carry at least one item for the session's push-record COUNT
        # (this sampler's length proxy) to actually reach push_count. The
        # first call carries the full page_ids set; every later call reuses
        # page_ids[0] as a filler so the session's distinct pushed-id set
        # stays exactly page_ids (a set dedupes the repeat).
        filler_id = page_ids[0] if page_ids else "filler"
        for i in range(push_count):
            ids_for_this_push = page_ids if i == 0 else [filler_id]
            push_metrics.record_push(
                push_metrics.build_push_record(
                    session_id=session_id,
                    query=f"q{i}",
                    backend="fts5",
                    hits=[
                        (f"{pid}.md", {"uid": pid}, push_metrics.estimate_tokens("b"))
                        for pid in ids_for_this_push
                    ],
                ),
                cache_dir=cache,
            )

    def test_strata_split_by_length_and_population(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        # short session (1 push record), population "interactive"
        self._seed(cache, "sess-short", 1, ["p1", "p2"])
        # long session (15 push records), population "headless"
        self._seed(cache, "sess-long", 15, ["p3"])

        pages = pus.stratified_sample_pushed_pages(
            cache_dir=cache,
            n_per_stratum=10,
            session_population={"sess-short": "interactive", "sess-long": "headless"},
            seed=1,
        )
        strata_seen = {(p.length_bucket, p.population) for p in pages}
        assert (pus.LENGTH_SHORT, "interactive") in strata_seen
        assert (pus.LENGTH_LONG, "headless") in strata_seen
        # p1/p2 from the short/interactive session, p3 from long/headless.
        ids_by_stratum = {
            (pus.LENGTH_SHORT, "interactive"): {"p1", "p2"},
            (pus.LENGTH_LONG, "headless"): {"p3"},
        }
        for p in pages:
            assert p.push_id in ids_by_stratum[(p.length_bucket, p.population)]

    def test_unknown_population_when_no_mapping_given(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        self._seed(cache, "sess-1", 1, ["p1"])
        pages = pus.stratified_sample_pushed_pages(cache_dir=cache, n_per_stratum=10)
        assert all(p.population == pus.POPULATION_UNKNOWN for p in pages)

    def test_n_per_stratum_caps_the_sample(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        self._seed(cache, "sess-1", 1, [f"p{i}" for i in range(20)])
        pages = pus.stratified_sample_pushed_pages(cache_dir=cache, n_per_stratum=5, seed=42)
        assert len(pages) == 5

    def test_deterministic_with_seed(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        self._seed(cache, "sess-1", 1, [f"p{i}" for i in range(20)])
        first = pus.stratified_sample_pushed_pages(cache_dir=cache, n_per_stratum=5, seed=7)
        second = pus.stratified_sample_pushed_pages(cache_dir=cache, n_per_stratum=5, seed=7)
        assert first == second

    def test_exclude_sessions_removed_from_pool(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        self._seed(cache, "sess-keep", 1, ["p1"])
        self._seed(cache, "sess-excl", 1, ["p2"])
        pages = pus.stratified_sample_pushed_pages(
            cache_dir=cache, n_per_stratum=10, exclude_sessions=["sess-excl"]
        )
        assert all(p.session_id != "sess-excl" for p in pages)
        assert any(p.session_id == "sess-keep" for p in pages)

    def test_session_length_bucket_boundaries(self) -> None:
        assert pus.session_length_bucket(1) == pus.LENGTH_SHORT
        assert pus.session_length_bucket(3) == pus.LENGTH_SHORT
        assert pus.session_length_bucket(4) == pus.LENGTH_MEDIUM
        assert pus.session_length_bucket(10) == pus.LENGTH_MEDIUM
        assert pus.session_length_bucket(11) == pus.LENGTH_LONG


# ---------------------------------------------------------------------------
# Label file schema / loader
# ---------------------------------------------------------------------------


class TestLoadLabels:
    def test_round_trip(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        rows = [
            {"session_id": "s1", "push_id": "p1", "relevant": True, "changed_work": True},
            {"session_id": "s2", "push_id": "p2", "relevant": False, "changed_work": False},
        ]
        labels_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        loaded = pus.load_labels(labels_path)
        assert loaded == [
            pus.PushPageLabel(session_id="s1", push_id="p1", relevant=True, changed_work=True),
            pus.PushPageLabel(session_id="s2", push_id="p2", relevant=False, changed_work=False),
        ]

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            '{"session_id": "s1", "push_id": "p1", "relevant": true, "changed_work": true}\n'
            "\n"
            "   \n"
        )
        assert len(pus.load_labels(labels_path)) == 1

    def test_unlabelled_null_raises(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            '{"session_id": "s1", "push_id": "p1", "relevant": true, "changed_work": null}\n'
        )
        with pytest.raises(ValueError, match="unlabelled"):
            pus.load_labels(labels_path)

    def test_missing_session_id_raises(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text('{"push_id": "p1", "relevant": true, "changed_work": true}\n')
        with pytest.raises(ValueError, match="session_id"):
            pus.load_labels(labels_path)

    def test_non_bool_relevant_raises(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            '{"session_id": "s1", "push_id": "p1", "relevant": "yes", "changed_work": true}\n'
        )
        with pytest.raises(ValueError, match="boolean"):
            pus.load_labels(labels_path)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text("not json\n")
        with pytest.raises(ValueError, match="invalid JSON"):
            pus.load_labels(labels_path)


class TestSampleWorksheetRoundTrip:
    def test_write_then_fill_then_load(self, tmp_path: Path) -> None:
        pages = [
            pus.SampledPage(
                session_id="s1", push_id="p1", length_bucket=pus.LENGTH_SHORT, population="unknown"
            )
        ]
        worksheet_path = tmp_path / "worksheet.jsonl"
        pus.write_sample_worksheet(pages, output_path=worksheet_path)
        raw = json.loads(worksheet_path.read_text().splitlines()[0])
        assert raw["relevant"] is None
        assert raw["changed_work"] is None
        # Fill in the judgments the way a human labeller would, then load.
        raw["relevant"] = True
        raw["changed_work"] = False
        worksheet_path.write_text(json.dumps(raw) + "\n")
        loaded = pus.load_labels(worksheet_path)
        assert loaded == [
            pus.PushPageLabel(session_id="s1", push_id="p1", relevant=True, changed_work=False)
        ]


# ---------------------------------------------------------------------------
# Default output paths never land inside the repo
# ---------------------------------------------------------------------------


class TestDefaultOutputPathsOutsideRepo:
    def test_default_sample_output_path_is_under_cache_dir(self, tmp_path: Path) -> None:
        path = pus.default_sample_output_path(tmp_path / "cache")
        assert path == tmp_path / "cache" / "push_used_scoring" / "sample-for-labelling.jsonl"

    def test_default_score_output_path_is_under_cache_dir(self, tmp_path: Path) -> None:
        path = pus.default_score_output_path(tmp_path / "cache")
        assert path == tmp_path / "cache" / "push_used_scoring" / "score-report.json"

    def test_defaults_resolve_outside_the_repo_working_tree(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        sample_path = pus.default_sample_output_path()
        score_path = pus.default_score_output_path()
        assert repo_root not in sample_path.parents
        assert repo_root not in score_path.parents


class TestWriteScoreReport:
    def test_write_score_report_writes_json(self, tmp_path: Path) -> None:
        report = pus.score_labels(
            [pus.PushPageLabel(session_id="s", push_id="p", relevant=True, changed_work=True)],
            {("s", "p"): True},
        )
        output_path = tmp_path / "out" / "report.json"
        pus.write_score_report(report, output_path=output_path)
        payload = json.loads(output_path.read_text())
        assert payload["confusion"]["tp"] == 1
