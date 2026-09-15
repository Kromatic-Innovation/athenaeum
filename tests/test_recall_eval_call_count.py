# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the AC4 call-count invariant (issue athenaeum#1572).

``tests/evals/test_recall_eval.py`` (the metered ``-m eval`` layer) asserts
that topic-extraction call count equals the number of probes graded — never
probes x backends. That assertion can only run live (it needs
``ANTHROPIC_API_KEY`` or the claude-cli provider), so this module proves the
MECHANISM offline, on regular (non-``eval``) CI, with zero network:

1. :func:`test_cached_extraction_matches_probes_graded` drives
   ``tests.evals.test_recall_eval._TopicCallTracker`` — the real class the
   live test uses — through the exact (probe x backend) call pattern the
   live test makes, and shows ``call_count == probes graded``. GREEN.
2. :func:`test_double_counting_variant_fails_the_call_count_assertion`
   constructs the COUNTER-EXAMPLE the acceptance criterion names
   explicitly: a naive implementation that calls extraction once per
   (probe, backend) pair instead of once per probe. It reuses the exact
   assertion shape ``test_recall_eval.test_topic_extraction_call_count``
   makes and shows it RED for that variant.

Both tests monkeypatch ``extract_topics``/``build_live_client`` at the
``tests.evals.test_recall_eval`` module level — no recorded fixture is
read or written; this proves the counting mechanism, not extraction
quality (that is the live layer's job).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.evals import test_recall_eval as recall_eval_module
from tests.evals.corpus import Probe

_BACKENDS = ("fts5", "vector")


def _fake_probes(n: int) -> list[Probe]:
    return [
        Probe(
            id=f"probe-{i}",
            probe_class="single_hop",
            query=f"query {i}",
            expected_uids=(f"uid-{i}",),
        )
        for i in range(n)
    ]


class _FakeSession:
    """Stands in for the real ``EvalSession`` — only ``observe_response``
    is called by ``_TopicCallTracker.topics_for``."""

    def observe_response(self, model: str, response: Any) -> None:
        pass


def _stub_extraction(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Replace the two live-backend seams ``_TopicCallTracker.topics_for``
    calls with deterministic, network-free stand-ins, recording every
    extraction invocation into ``calls``."""

    def _fake_build_live_client() -> object:
        return object()

    def _fake_extract_topics(prompt: str, timeout: float = 15.0) -> list[str]:
        calls.append(prompt)
        return [prompt]

    monkeypatch.setattr(recall_eval_module, "build_live_client", _fake_build_live_client)
    monkeypatch.setattr(recall_eval_module, "extract_topics", _fake_extract_topics)


def test_cached_extraction_matches_probes_graded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL pattern: ``_TopicCallTracker`` extracts once per probe id
    and serves every later backend's lookup from cache. Grading 5 probes
    across 2 backends (10 (probe, backend) cases) must still cost exactly
    5 extraction calls."""
    calls: list[str] = []
    _stub_extraction(monkeypatch, calls)
    tracker = recall_eval_module._TopicCallTracker()
    probes = _fake_probes(5)

    for probe in probes:
        for _backend in _BACKENDS:
            tracker.topics_for(
                probe,
                eval_record=False,
                eval_session=_FakeSession(),
                monkeypatch=monkeypatch,
            )

    graded_probes = len(probes)
    assert len(calls) == graded_probes
    assert tracker.call_count == graded_probes, (
        f"topic-extraction call count {tracker.call_count} != "
        f"probes graded {graded_probes}"
    )


def test_double_counting_variant_fails_the_call_count_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The COUNTER-EXAMPLE AC4 names explicitly: a run that calls
    extraction once per backend (i.e. once per (probe, backend) pair, with
    no per-probe cache) double-counts. Built here with NO caching — the
    thing ``_TopicCallTracker`` exists to prevent — and shown to fail the
    exact call-count assertion ``test_recall_eval.test_topic_extraction_
    call_count`` makes for the real, cached pattern above."""
    calls: list[str] = []
    _stub_extraction(monkeypatch, calls)
    probes = _fake_probes(5)

    call_count = 0
    for probe in probes:
        for _backend in _BACKENDS:
            # No cache lookup: every (probe, backend) pair re-extracts.
            recall_eval_module.extract_topics(probe.query, timeout=15.0)
            call_count += 1

    graded_probes = len(probes)
    # Built as advertised: one extraction per (probe, backend) pair.
    assert call_count == graded_probes * len(_BACKENDS)

    with pytest.raises(AssertionError):
        assert call_count == graded_probes, (
            f"topic-extraction call count {call_count} != "
            f"probes graded {graded_probes} — extraction must run exactly "
            "once per probe, cached across backend parametrizations"
        )


def test_floor_table_covers_every_probe_class_on_every_backend() -> None:
    """The floor table is hand-written; ``_PROBE_CLASSES`` is read from
    ``probes.yaml``. Adding a probe class to the YAML without adding its
    floors would raise ``KeyError`` inside
    ``test_recall_floors_by_backend_and_class`` — and because that test is
    ``-m eval`` and credential-gated, the crash would only surface on a
    PAID main-push Evals run, not on the PR that introduced it.

    This unmarked guard moves that failure forward to regular CI and turns
    it into a readable message. It asserts exact coverage in BOTH
    directions: a missing cell is the ``KeyError``, and a stale leftover
    cell is a floor nobody is asserting any more.
    """
    from tests.evals import test_recall_eval as layer

    expected = {
        (backend, probe_class)
        for backend in ("fts5", "vector")
        for probe_class in layer._PROBE_CLASSES
    }
    actual = set(layer._FLOOR_BY_BACKEND_AND_CLASS)

    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    assert not missing and not stale, (
        "_FLOOR_BY_BACKEND_AND_CLASS is out of sync with probes.yaml: "
        f"missing floors for {missing}; stale floors for {stale}. "
        "Every (backend, probe_class) cell must carry a stated floor — "
        "see athenaeum#1572 AC3."
    )
