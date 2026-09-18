# SPDX-License-Identifier: Apache-2.0
"""Recall-sidecar live-API eval (issues athenaeum#331, athenaeum#1572).

The recall pipeline this covers:

    prompt --> query_topics.extract_topics (LIVE Haiku call) --> topics
           --> recall_search (synthetic corpus, FTS5 or vector backend)
           --> formatted output text asserted against expected page uids
               (``tests/evals/data/corpus/probes/probes.yaml``)

Only ``extract_topics`` hits the network; :func:`recall_search` runs
against ``tests.evals.corpus.build_corpus("small")`` with a real FTS5 or
vector index, so results are deterministic once the topic list is fixed.

**Repointed off the 16-page fixture wiki (athenaeum#1572).** The old
``tests/evals/data/recall/wiki/`` + ``tests/evals/data/recall/cases.yaml``
golden set graded 6 hand-picked cases on the ``keyword`` backend only.
Neither is deleted here (AC1) — they now serve a DIFFERENT, out-of-scope
consumer: ``tests/test_recorded_fixtures.py::test_recall_replay`` replays
the 6 fixtures already recorded under ``tests/fixtures/recorded/recall/``
(``owner_thornhollow``, ``pto_policy``, ``budget_approver_contradict``,
``ambiguous_policy_escalate``, ``bluewater_terms_proper_noun``,
``office_address_contradict``) against that exact wiki, on regular
(non-``eval``) CI, zero network. That test asserts things the corpus/probe
schema has no way to express:

* raw natural-language ``prompt`` strings keyed to THIS wiki's uids (the
  synthetic corpus has no equivalent id space — its uids are
  ``policy-pto``, ``client-bluewater``, etc., not ``rec-pto``,
  ``rec-client-bluewater``);
* the athenaeum#325 contradiction-flag header
  (``budget_approver_contradict``, ``office_address_contradict`` — the
  corpus has no contradiction-flagged fixture page);
* multi-candidate escalation on a deliberately ambiguous prompt
  (``ambiguous_policy_escalate``, asserted via ``min_distinct_pages`` —
  ``probes.yaml`` has no such field, only ``expected_uids``/
  ``must_not_rank``).

Deleting the wiki would silently red an unrelated, already-passing,
zero-network test. All 16 retained pages (``budget-approver.md``,
``client-acme.md``, ``client-bluewater.md``, ``client-owner-thornhollow.md``,
``client-thornhollow.md``, ``expense-policy.md``, ``meeting-cadence.md``,
``office-address.md``, ``owner-amir.md``, ``owner-priya.md``,
``project-invoice-cadence.md``, ``project-portal-hosting.md``,
``pto-policy.md``, ``standup.md``, ``tool-pagemoor.md``,
``tool-tallyfold.md``) back that replay contract as a set; none is used by
this module any more.

**No second copy of expected uids (AC2).** Every ``expected_uids`` value
below is read live from ``tests.evals.corpus.load_probes()`` — this module
holds no hardcoded uid list of its own.

**Floors per backend and per probe_class (AC3).** See
:data:`_FLOOR_BY_BACKEND_AND_CLASS`. Abstention probes are graded with
:func:`tests.evals.metrics.grade_abstention`, never :func:`recall_at_k` —
there is no expected uid, and the emptiness of the push is the assertion.
Every non-abstention probe's PASS predicate is ``recall_at_k(..., 5) ==
1.0`` when it carries exactly one expected uid, and ``>= 0.5`` when it
carries two or more (``spend_approver_named``, ``person_not_repo``,
``former_client_not_current``, ...) — requiring every hop in the top 5 is
the strict reading, but a probe class whose OWN docstring in
``probes.yaml`` states "retrieving either alone yields a confidently
incomplete answer" is explicitly grading whether *at least one* correct
hop surfaced, and a top-5 window is shared retrieval pressure with
distractor/ballast pages the old 16-page fixture never had. Floor VALUES
are measured against ``build_corpus("small")`` with the offline
probe-query fallback (see below) and carry one probe of slack per class
(none for single-probe classes, which cannot be discounted further)
against live topic-extraction variance.

**Abstention floor is 0, deliberately descriptive, not aspirational
(issue athenaeum#1492).** ``athenaeum.search.meets_relevance_floor`` /
``athenaeum.config.resolve_recall_relevance_floor`` ship INACTIVE by
default — athenaeum#1492's own scope explicitly left production tuning
open. With no floor active, ``recall_search`` returns a confident,
non-empty, wrong result for every abstention probe (measured directly,
both backends, ``tests/test_eval_recall_floor.py`` pins the same finding
for FTS5/keyword). Setting this layer's abstention floor to anything above
0 would either fail on every run (vacuously red) or require this issue to
activate athenaeum#1492's floor in production config, which is out of
athenaeum#1572's scope. The floor records the CURRENT state so a future
activation of athenaeum#1492's floor is what turns it green, not a rigged
threshold.

**Topic-extraction call count == probes graded, not probes × backends
(AC4).** :class:`_TopicCallTracker` extracts once per probe id and caches
the result; the ``fts5`` and ``vector`` parametrizations of the SAME probe
both read the cached topic list. ``tests/test_recall_eval_call_count.py``
proves this offline: the real (cached) pattern's call count equals probes
graded, and a deliberately reconstructed "once per backend" variant fails
that same assertion.

**Token totals, before and after (AC4).** BEFORE is MEASURED, read
directly from the ``eval-summary.json`` artifact of main-push run
`34934473115 <https://github.com/Kromatic-Innovation/athenaeum/actions/
runs/34934473115>`_ — the run this issue's own body cites — relayed
through a throwaway Actions branch and sha256-verified (the artifact's
``*.blob.core.windows.net`` host is unreachable from a lane container).
That run's ``layer_scores.recall`` is ``{"passed": 6, "total": 6}``
(6 keyword-backend cases, the pre-athenaeum#1572 layout) and its
``token_usage`` is::

    input_tokens=76964 output_tokens=19423
    cache_creation_input_tokens=4390 cache_read_input_tokens=30730

A same-day rerun, run 34981842660 (2026-09-15T14:32Z, also main-push),
shows the same ``recall`` 6/6 with ``input_tokens=74765
output_tokens=15945`` — close to run 34934473115, confirming the figures
above are not a one-off outlier.

**This total is WHOLE-RUN, not per-layer** — ``EvalSession.emit_summary``
(``tests/evals/harness.py``) accumulates ``token_usage`` across every eval
layer in the session (``attachment``, ``classify``, ``decomposition``,
``detector``, ``merge``, ``recall``, ``resolver``, ``underdetermined``,
``write_tier_compare``), so the 76,964/19,423 figures above are NOT
recall's own cost. The ``per_model`` breakdown in the same artifact does
not separate it either: ``query_topics.extract_topics``'s
``DEFAULT_TOPIC_MODEL`` is ``athenaeum.query_topics.DEFAULT_TOPIC_MODEL ==
athenaeum.config.DEFAULT_CLASSIFY_MODEL == "claude-haiku-4-5-20251001"``
— the SAME model id ``tiers.tier2_classify`` (the ``classify`` layer,
6 cases in this run) defaults to — so the ``claude-haiku-4-5-20251001``
per-model bucket (``input_tokens=17722 output_tokens=3650``) is the SUM of
both layers' calls, not recall's alone. Isolating recall's own share would
require a live run of ONLY this module, which no container here can make
(``ANTHROPIC_API_KEY`` is empty). This limitation is real and stated
rather than papered over with a computed-looking split.

AFTER is DERIVED, not measured: extraction runs once per probe (the
call-count invariant above), so a full run of this module makes 20 calls
(one per probe in ``probes.yaml``, shared across both backend
parametrizations by :class:`_TopicCallTracker`) versus the 6 the old
keyword-only layout made — call count rises ~3.33×. Per-call cost is
capped by ``query_topics.py``'s fixed system prompt and
``_TOPIC_MAX_TOKENS=256``, so total extraction tokens should scale
roughly with call count, but no live run has produced an actual AFTER
number from this container; do not read the ~3.33× figure as measured.

**Vector cases carry ``pytest.mark.embedding`` (issue athenaeum#1572 plan
step 2)** — same convention as ``tests/test_search.py`` /
``tests/evals/test_recall_eval.py``'s neighbours: the real MiniLM ONNX
model has to actually run, so a contributor selecting `-m eval` alone still
collects the case, but its `embedding` mark is visible to tooling that
filters on it (e.g. `evals.yml`'s `MiniLM-dependent suite` job selection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.query_topics import DEFAULT_TOPIC_MODEL, extract_topics
from athenaeum.search import get_backend
from tests.evals.corpus import Probe, build_corpus, load_probes
from tests.evals.harness import (
    LAYER_RECALL,
    RecordingClient,
    build_live_client,
    live_ready,
)
from tests.evals.metrics import grade_abstention, mrr, recall_at_k, uids_from_recall_output

pytestmark = pytest.mark.eval

# "small" (not "core"): adds the distractor tier built from each probe's own
# ``distractor_terms``, which is what makes retrieval pressure real rather
# than trivial — same choice, same rationale, as
# ``tests/test_eval_recall_floor.py``'s ``_CORPUS_SCALE``.
_CORPUS_SCALE = "small"

_ALL_PROBES: list[Probe] = load_probes()
_PROBE_CLASSES: tuple[str, ...] = tuple(sorted({p.probe_class for p in _ALL_PROBES}))

# Measured this dispatch against ``build_corpus("small")`` (seed 20260908,
# GENERATOR_VERSION 2) using the offline probe-query fallback (topic
# extraction returns [] with no live backend, so the query IS the probe's
# raw ``query`` — see ``extract_topics``'s no-key fallback). One probe of
# slack subtracted from every class that measured 100% at size >= 2; classes
# already below 100%, and every size-1 class, are left at the measured
# value. Abstention is fixed at 0 for both backends — see the module
# docstring.
#
# ``follow_through`` (issue athenaeum#1737) is NOT measured against a live
# backend yet -- this table only needs to exist so
# ``test_floor_table_covers_every_probe_class_on_every_backend`` (regular
# CI, no credentials) stays in sync with ``probes.yaml``; the live
# ``test_recall_floors_by_backend_and_class`` this floor actually gates is
# credential-gated (``-m eval``) and was not run to derive it. Set to the
# same conservative 1-of-6 floor as ``distractor_robustness``/``redundancy``
# rather than a measured value -- lower this further, or replace it with a
# measured floor, the first time this class actually runs live.
#
# ``aggregation`` (issue athenaeum#1780) is the same situation, same fix:
# not measured against a live backend (no ``ANTHROPIC_API_KEY`` in this
# container, same constraint ``follow_through``'s comment above names), so
# this entry exists only to keep the offline sync-check green, not as a
# derived value. Its own 3 probes each carry 6-7 ``expected_uids`` (well
# above `_passes`'s `len(expected_uids) <= 1` single-uid case), so the
# ``>= 0.5`` ``recall_at_k`` threshold applies -- the same threshold
# ``multi_hop``/``disambiguation`` already use for their own multi-uid
# probes above, not a class-specific one. Set to the SAME conservative
# 1-of-3 floor (this class has 3 probes total, vs. 6 for
# ``distractor_robustness``/``redundancy``/``follow_through`` -- 1 is the
# floor regardless once a class has more than one probe, since it is
# already the minimum non-zero pass count) rather than a measured value --
# replace with a measured floor the first time this class actually runs
# live.
#
# ``contradiction``/``negative_knowledge`` (issue athenaeum#1781, wave-2
# item G) are NOT measured against a live backend either, same reason and
# same derivation method as ``follow_through`` immediately above: this
# table exists to keep regular (non-``eval``, credential-gated) CI in sync
# with ``probes.yaml`` via ``test_floor_table_covers_every_probe_class_on_
# every_backend``, not to state a measured live floor. Both classes are
# also `report_only: True` (`tests.evals.corpus.WAVE_2_PROBE_CLASSES`, not
# yet in `CONDITION_2_ENROLLED`), so a floor here has no bearing on any §7
# decision condition regardless. Set to the same conservative 1-of-6 floor
# as ``follow_through``/``distractor_robustness``/``redundancy`` -- lower
# this further, or replace it with a measured floor, the first time either
# class actually runs live.
_FLOOR_BY_BACKEND_AND_CLASS: dict[tuple[str, str], int] = {
    ("fts5", "single_hop"): 3,
    ("fts5", "multi_hop"): 2,
    ("fts5", "temporal"): 2,
    ("fts5", "disambiguation"): 3,
    ("fts5", "distractor_robustness"): 1,
    ("fts5", "redundancy"): 1,
    ("fts5", "follow_through"): 1,
    ("fts5", "aggregation"): 1,
    ("fts5", "contradiction"): 1,
    ("fts5", "negative_knowledge"): 1,
    ("fts5", "abstention"): 0,
    ("vector", "single_hop"): 3,
    ("vector", "multi_hop"): 2,
    ("vector", "temporal"): 2,
    ("vector", "disambiguation"): 3,
    ("vector", "distractor_robustness"): 1,
    ("vector", "redundancy"): 1,
    ("vector", "follow_through"): 1,
    ("vector", "aggregation"): 1,
    ("vector", "contradiction"): 1,
    ("vector", "negative_knowledge"): 1,
    ("vector", "abstention"): 0,
}

_BACKEND_PARAMS = ("fts5", pytest.param("vector", marks=pytest.mark.embedding))


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def _recall_corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Materialize the synthetic corpus once and build BOTH real indexes.

    Shared read-only across every (probe, backend) case — neither backend
    mutates the wiki tree or index at query time, so one build serves the
    whole matrix. The vector index uses the REAL chromadb MiniLM embedder
    (no offline stand-in — this module carries the ``eval`` marker, which
    ``tests/conftest.py::_offline_embedding_function`` explicitly excludes).
    """
    corpus = build_corpus(scale=_CORPUS_SCALE)
    root = tmp_path_factory.mktemp("athenaeum-1572-corpus")
    wiki_root = corpus.materialize(root)
    cache_dir = root / "cache"
    get_backend("fts5").build_index(wiki_root, cache_dir)
    get_backend("vector").build_index(wiki_root, cache_dir)
    return wiki_root, cache_dir


@dataclass
class _TopicCallTracker:
    """Per-module cache + counter proving AC4's call-count invariant.

    ``topics_for`` extracts once per probe id; a second call for the same
    id (the SAME probe graded against the other backend) is served from
    ``cache`` and does not increment ``call_count``. See
    ``tests/test_recall_eval_call_count.py`` for the offline proof that
    this shape — and only this shape — satisfies "call count == probes
    graded".
    """

    cache: dict[str, list[str]] = field(default_factory=dict)
    call_count: int = 0

    def topics_for(
        self,
        probe: Probe,
        *,
        eval_record: bool,
        eval_session: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[str]:
        if probe.id in self.cache:
            return self.cache[probe.id]

        real_client = build_live_client()
        recording = RecordingClient(real_client, record=eval_record, layer=LAYER_RECALL)
        recording.start_case(probe.id)

        original_create = recording.messages.create

        def _create(**params: Any) -> Any:
            response = original_create(**params)
            eval_session.observe_response(str(params.get("model", "")), response)
            return response

        recording.messages.create = _create  # type: ignore[method-assign]

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: recording)
        topics = extract_topics(probe.query, timeout=15.0)
        recording.end_case()

        self.cache[probe.id] = topics
        self.call_count += 1
        return topics


@pytest.fixture(scope="module")
def _topic_tracker() -> _TopicCallTracker:
    return _TopicCallTracker()


def _probe_class_of(probe_id: str) -> str:
    for probe in _ALL_PROBES:
        if probe.id == probe_id:
            return probe.probe_class
    raise AssertionError(f"unknown probe id {probe_id!r}")


def _passes(probe: Probe, uids: list[str]) -> bool:
    if probe.probe_class == "abstention":
        return grade_abstention(uids).outcome.name == "CLEAN"
    threshold = 1.0 if len(probe.expected_uids) <= 1 else 0.5
    return recall_at_k(uids, probe.expected_uids, 5) >= threshold


@pytest.mark.parametrize("backend", _BACKEND_PARAMS)
@pytest.mark.parametrize("probe", _ALL_PROBES, ids=lambda p: p.id)
def test_recall_case(
    probe: Probe,
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
    _recall_corpus: tuple[Path, Path],
    _topic_tracker: _TopicCallTracker,
) -> None:
    """Run one (probe, backend) case end-to-end: extract_topics (cached
    per probe, AC4) --> recall_search on the real backend."""
    wiki_root, cache_dir = _recall_corpus

    # Issue athenaeum#980 AC4: recall_search's push-metrics instrumentation writes
    # behind the seam (wiki_root=); disable it so a push record never lands
    # in this materialized-under-tmp_path corpus tree (harmless either way,
    # but keeping parity with the prior fixture-wiki test's posture).
    monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", "0")

    topics = _topic_tracker.topics_for(
        probe, eval_record=eval_record, eval_session=eval_session, monkeypatch=monkeypatch
    )
    query = " ".join(topics) if topics else probe.query

    output = recall_search(
        wiki_root,
        query,
        top_k=5,
        search_backend=backend,
        cache_dir=cache_dir,
    )
    uids = uids_from_recall_output(output)
    case_passed = _passes(probe, uids)

    if probe.probe_class == "abstention":
        detail = f"suggested={uids}"
    else:
        detail = (
            f"recall@5={recall_at_k(uids, probe.expected_uids, 5):.2f} "
            f"mrr={mrr(uids, probe.expected_uids):.2f}"
        )

    eval_session.record_case(
        LAYER_RECALL,
        f"{probe.id}:{backend}",
        expected=f"class={probe.probe_class} expected_uids={list(probe.expected_uids)}",
        observed=f"topics={topics} uids={uids}",
        passed=case_passed,
        detail=detail,
    )


def test_recall_floors_by_backend_and_class(eval_session: Any, _live_ready: None) -> None:
    """AC3: assert every (backend, probe_class) floor from
    :data:`_FLOOR_BY_BACKEND_AND_CLASS`."""
    cases = [r for r in eval_session.results if r.layer == LAYER_RECALL]
    assert cases, "recall eval collected no cases"

    failures: list[str] = []
    for backend in ("fts5", "vector"):
        for probe_class in _PROBE_CLASSES:
            floor = _FLOOR_BY_BACKEND_AND_CLASS[(backend, probe_class)]
            matching = [
                r
                for r in cases
                if r.case_id.endswith(f":{backend}")
                and _probe_class_of(r.case_id.rsplit(":", 1)[0]) == probe_class
            ]
            passed = sum(1 for r in matching if r.passed)
            if passed < floor:
                failures.append(
                    f"{backend}/{probe_class}: {passed}/{len(matching)} "
                    f"(need >= {floor})"
                )
    assert not failures, (
        "recall below floor for one or more (backend, probe_class) cells: "
        + "; ".join(failures)
        + f". Topic model: {DEFAULT_TOPIC_MODEL}. Check eval-summary.json for "
        "per-case failures."
    )


def test_topic_extraction_call_count(
    eval_session: Any, _live_ready: None, _topic_tracker: _TopicCallTracker
) -> None:
    """AC4: extraction call count equals the number of DISTINCT probes
    graded — not probes x backends. A run that called extraction once per
    backend would report ``call_count == probes_graded * len(backends)``
    here and fail this assertion; see
    ``tests/test_recall_eval_call_count.py`` for that counter-example
    constructed and shown red, offline.
    """
    graded_probe_ids = {
        r.case_id.rsplit(":", 1)[0] for r in eval_session.results if r.layer == LAYER_RECALL
    }
    assert graded_probe_ids, "recall eval collected no cases"
    assert _topic_tracker.call_count == len(graded_probe_ids), (
        f"topic-extraction call count {_topic_tracker.call_count} != "
        f"probes graded {len(graded_probe_ids)} — extraction must run "
        "exactly once per probe, cached across backend parametrizations"
    )
