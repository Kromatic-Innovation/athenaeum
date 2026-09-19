# SPDX-License-Identifier: Apache-2.0
"""Real-embedder regression check for hybrid vector recall (issue athenaeum#1800).

``tests/evals/test_recall_covers_grep.py`` -- this module's sibling -- proves
its coverage and person/repo-disambiguation invariants against the
``vector`` backend. Its ``scale_fixture`` is MODULE-scoped, so it is set up
before pytest resolves which embedding function this SUITE run should use;
under the default (offline) selection, ``tests/conftest.py``'s autouse
``_offline_embedding_function`` fixture is FUNCTION-scoped and routes
chromadb's embedder through a deterministic lexical-hash stand-in
(``tests.offline_embeddings.OfflineONNXMiniLMStub``) for every test, but
that fixture's own gate, ``_default_selection``, decides per NODE, off that
node's markers.

Before issue athenaeum#1851, this ordering meant the sibling module's index
build landed on the REAL model (nothing had patched ``ONNXMiniLM_L6_V2``
yet at fixture-setup time) while every per-probe QUERY in a
default-selection run went through the stub -- queries scored in
stub-space against an index built in real-model space, which was not a
measurement of the shipped path at all, and in a network-restricted
environment could abort the whole module outright fetching the ONNX model.
Issue athenaeum#1851 fixed it: the sibling module's ``scale_fixture`` now
applies the SAME stand-in class directly around its own ``build_index``
call (the same pattern ``tests/test_retrieval_golden_1420.py``'s
``golden_caches`` uses), so its default (offline) run now measures index
and query through ONE consistent embedder -- the lexical-hash stand-in,
end to end. See that module's own module-level measurement note (above its
``_VECTOR_XFAIL``) for the corrected numbers and issue athenaeum#1800's
body for the original mismatch finding this module's own real-model
measurement exists to keep separate from.

This module is entirely different in ONE way that fixes the mismatch: it
carries a module-level ``pytestmark = pytest.mark.embedding``. That marker
is visible to ``_default_selection`` from the FIRST test collected in this
module onward -- including during THIS module's own (also module-scoped)
fixture setup, since fixture setup always happens as part of setting up
whichever test triggers it, with that test's own marker set already
resolved. So ``_offline_embedding_function`` (and ``_block_non_local_
network``) are no-ops for every test AND every fixture in this module,
uniformly -- index build and every query share the SAME real
``all-MiniLM-L6-v2`` model. ``test_no_lexical_hash_stub_used_in_this_module``
below asserts that directly, as a regression trip-wire.

The ``embedding`` marker also keeps this module out of the default
(contributor-facing) pytest selection (``pyproject.toml``'s ``addopts``)
and puts it in CI's non-required ``embedding-suite`` job
(``.github/workflows/evals.yml``, ``pytest tests/ -m embedding``) -- see
issue athenaeum#1800's acceptance criteria for why a LOCAL, foreground run
of this module (recorded wall time + model sha256) is this issue's actual
gate, not that non-required CI job.

Reuses the sibling module's probe data and pass/fail definitions BY NAME
(``_PROBE_IDS`` / ``_PERSON_REPO_PROBE_IDS`` / ``_probe_by_id`` /
``_measure`` / ``_recall_ranked_uids`` / ``_failure_message``) rather than
copying them -- this module's whole point is a different FIXTURE (real
model, built inside this module), not a different measurement.

``_VECTOR_REAL_XFAIL`` (below) is this module's own strict-xfail set, keyed
by ``(family, scale, probe_id)`` where ``family`` is ``"reachable_expected_
pages"`` (the coverage invariant) or ``"person_repo_disambiguation"`` (the
must-not-rank guard) -- a 3-tuple, unlike the sibling module's 2-tuple sets,
because this module's single fixture backs BOTH families' tests, so the key
must say which family the entry describes as well as scale/probe. It
equals EXACTLY the real-model failures measured under the shipped hybrid
defaults (see ``docs/measurements/recall-hybrid-fusion-sweep.md`` and the
issue athenaeum#1800 PR body for the sweep that picked them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

import tests.offline_embeddings as _offline_embeddings_module
from athenaeum.search import get_backend
from tests.evals.corpus import Corpus, build_corpus
from tests.evals.relevance_metrics import build_name_to_uid
from tests.evals.test_recall_covers_grep import (
    _PERSON_REPO_PROBE_IDS,
    _PROBE_IDS,
    _failure_message,
    _hook_session_ready,
    _measure,
    _probe_by_id,
    _recall_ranked_uids,
)

try:
    import chromadb  # noqa: F401

    _VECTOR_AVAILABLE = True
    _VECTOR_SKIP_REASON = ""
except ImportError as _exc:  # pragma: no cover -- environment dependent
    _VECTOR_AVAILABLE = False
    _VECTOR_SKIP_REASON = f"chromadb not installed ({_exc})"

#: Keeps this whole module out of the default pytest selection and off the
#: offline lexical-hash stand-in -- see module docstring.
pytestmark = [
    pytest.mark.embedding,
    pytest.mark.skipif(
        not _VECTOR_AVAILABLE, reason=_VECTOR_SKIP_REASON or "chromadb not installed"
    ),
]

#: Same two scales the sibling module runs -- ``core`` (fast, hand-authored)
#: and ``medium`` (1,000 pages, adds distractor/ballast retrieval pressure).
_SCALES: tuple[str, ...] = ("core", "medium")

_COVERAGE_FAMILY = "reachable_expected_pages"
_DISAMBIGUATION_FAMILY = "person_repo_disambiguation"


# ---------------------------------------------------------------------------
# Real-model failures under the SHIPPED hybrid defaults (issue athenaeum#1800).
#
# Measured by `scripts/sweep_recall_hybrid_fusion.py` (committed output:
# `docs/measurements/recall-hybrid-fusion-sweep.md`) at the winning
# combination `fts5_weight=1.0 (unchanged), guard_rank=1 (was 0), k=60
# (unchanged)` -- `RECALL_HYBRID_GUARD_RANK_DEFAULT` in
# `src/athenaeum/config.py`. Re-verified directly through THIS module's own
# fixture (real `recall_search` calls, not the sweep script's cached-hits
# reimplementation) before being recorded here.
#
# Baseline (pre-athenaeum#1800 defaults, fts5_weight=1.0/guard_rank=0/k=60)
# had SIX real-model failures; `guard_rank=1` clears
# `medium/portal_design_reviewer` (a coverage crowd-out fixed by protecting
# a single-list top-2 hit) without breaking any other real-model-passing
# case (eligibility rule (a)) and without moving a single node in the
# default (offline stand-in) selection's pass/xfail set (eligibility rule
# (b): `guard_rank<=2` was already documented as a no-op at this corpus's
# rank distribution by the athenaeum#1789 cross-lane regression lane, and
# was re-confirmed directly here: two full runs of
# `pytest tests/evals/test_recall_covers_grep.py`, with and without
# `ATHENAEUM_RECALL_HYBRID_GUARD_RANK=1`, produced 144 passed / 27 xfailed /
# 0 failed both times, and the two runs' XFAIL node-id sets diffed
# byte-identical -- not just a matching count).
#
# The five remaining failures, and why fusion cannot reach them:
#
# * `reachable_expected_pages/core/former_client_not_current` -- expected
#   page `client-atlas` is ABSENT from both backends' widened top-15
#   candidate lists entirely; no RRF parameter can rank a page that is not
#   a candidate in either input list.
# * `reachable_expected_pages/medium/former_client_not_current` -- three
#   expected `client-*` pages, same absent-from-both-candidate-lists
#   mechanism.
# * `reachable_expected_pages/medium/surname_is_ambiguous` -- two expected
#   `person-*` pages absent from the fused top-5; present deeper in the
#   real vector list (see the sweep table) but crowded out by purpose-built
#   `dis-surname_is_ambiguous-*` distractors that score higher under the
#   real model, not a rank-guard-fixable ordering.
# * `person_repo_disambiguation/core/repo_not_person` -- `must_not_rank`
#   page `person-rowan-wrenfield` is BOTH-list (present in both backends'
#   candidate lists) at fused rank 2; RRF's arm/guard mechanism only
#   protects a SINGLE-list hit's own high rank, so a both-list page cannot
#   be excluded by any (fts5_weight, guard_rank, k) combination in the
#   swept grid.
# * `person_repo_disambiguation/medium/repo_not_person` -- same mechanism,
#   fused rank 5.
#
# See the PR body for each remaining failure's exact rank on both arms,
# taken from the sweep output.
# ---------------------------------------------------------------------------
_VECTOR_REAL_XFAIL: frozenset[tuple[str, str, str]] = frozenset(
    {
        (_COVERAGE_FAMILY, "core", "former_client_not_current"),
        (_COVERAGE_FAMILY, "medium", "former_client_not_current"),
        (_COVERAGE_FAMILY, "medium", "surname_is_ambiguous"),
        (_DISAMBIGUATION_FAMILY, "core", "repo_not_person"),
        (_DISAMBIGUATION_FAMILY, "medium", "repo_not_person"),
    }
)


@dataclass
class _RealScaleFixture:
    corpus: Corpus
    wiki_root: Path
    fts5_cache_dir: Path
    vector_cache_dir: Path
    knowledge_root: Path
    hook_home: Path
    hook_ready: bool
    name_to_uid: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module", params=_SCALES)
def real_scale_fixture(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> _RealScaleFixture:
    """Builds its OWN corpus + FTS5 + vector index, inside this
    ``embedding``-marked module -- see module docstring for why this (and
    not the sibling module's shared ``scale_fixture``) is what makes index
    and query share one model."""
    scale = request.param
    corpus = build_corpus(scale)
    root = tmp_path_factory.mktemp(f"real-corpus-{scale}")
    wiki_root = corpus.materialize(root)
    cache_dir = root / "cache"
    get_backend("fts5").build_index(wiki_root, cache_dir)
    get_backend("vector").build_index(wiki_root, cache_dir)

    hook_home = root / "hook-home"
    hook_ready = _hook_session_ready(root, hook_home)
    name_result = build_name_to_uid(corpus.pages)

    return _RealScaleFixture(
        corpus=corpus,
        wiki_root=wiki_root,
        fts5_cache_dir=cache_dir,
        vector_cache_dir=cache_dir,
        knowledge_root=root,
        hook_home=hook_home,
        hook_ready=hook_ready,
        name_to_uid=name_result.mapping,
    )


# ---------------------------------------------------------------------------
# The coverage invariant, real model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe_id", _PROBE_IDS)
def test_recall_covers_grep_reachable_expected_pages_vector_real(
    request: pytest.FixtureRequest, real_scale_fixture: _RealScaleFixture, probe_id: str
) -> None:
    """Same invariant as the sibling module's fts5/vector tests -- every
    grep-reachable expected page must be in recall's top 5 -- against the
    REAL embedder, on the SHIPPED hybrid defaults (``config=None`` below
    resolves through ``src/athenaeum/config.py``'s actual defaults, the
    same as a live deployment, never an override)."""
    fx = real_scale_fixture
    key = (_COVERAGE_FAMILY, fx.corpus.scale, probe_id)
    if key in _VECTOR_REAL_XFAIL:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"athenaeum#1800: measured real-embedder retrieval-side "
                    f"miss (vector, {fx.corpus.scale}, {probe_id})"
                ),
            )
        )
    probe = _probe_by_id(fx.corpus, probe_id)
    measurement = _measure(
        scale=fx.corpus.scale,
        backend="vector",
        probe=probe,
        corpus=fx.corpus,
        wiki_root=fx.wiki_root,
        cache_dir=fx.vector_cache_dir,
        knowledge_root=fx.knowledge_root,
        hook_home=fx.hook_home,
        hook_ready=fx.hook_ready,
        name_to_uid=fx.name_to_uid,
    )
    missing = measurement.grep_reachable_expected - set(measurement.recall_ranked)
    assert not missing, _failure_message(measurement, missing)


# ---------------------------------------------------------------------------
# Disambiguation win, real model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe_id", _PERSON_REPO_PROBE_IDS)
def test_person_repo_disambiguation_excludes_wrong_page_vector_real(
    request: pytest.FixtureRequest, real_scale_fixture: _RealScaleFixture, probe_id: str
) -> None:
    """Same assertion as the sibling module's vector test -- the probe's
    ``must_not_rank`` page must stay out of recall's top 5 -- against the
    REAL embedder. Unlike the coverage test above, the sibling module's
    equivalent test carries NO xfail gate at all (issue athenaeum#1800's
    Motivation: this is where the real-model measurement first surfaced
    ``repo_not_person`` failing, a case invisible under the stand-in). This
    module's version DOES gate through ``_VECTOR_REAL_XFAIL`` -- the real
    model's own failures are named and tracked here, not left to fail CI
    uninformatively."""
    fx = real_scale_fixture
    key = (_DISAMBIGUATION_FAMILY, fx.corpus.scale, probe_id)
    if key in _VECTOR_REAL_XFAIL:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"athenaeum#1800: measured real-embedder disambiguation "
                    f"miss (vector, {fx.corpus.scale}, {probe_id})"
                ),
            )
        )
    probe = _probe_by_id(fx.corpus, probe_id)
    assert len(probe.must_not_rank) == 1
    wrong_uid = probe.must_not_rank[0]
    ranked = _recall_ranked_uids(
        fx.wiki_root, probe.query, search_backend="vector", cache_dir=fx.vector_cache_dir
    )
    assert wrong_uid not in ranked, (
        f"probe={probe_id!r} scale={fx.corpus.scale!r} backend='vector' (real embedder): "
        f"{wrong_uid!r} (must_not_rank) surfaced in recall's top 5: {ranked!r}"
    )


# ---------------------------------------------------------------------------
# No-stub-call trip-wire (issue athenaeum#1800 acceptance criterion: "One
# test in it must assert that no lexical-hash stub call happens anywhere in
# the module").
# ---------------------------------------------------------------------------

#: Module-level so appends made by the per-test wrapper below (installed
#: fresh each test via `monkeypatch`, which is function-scoped) persist
#: across the whole module's run -- this list is the ONLY state that must
#: survive from the first test to the last.
_stub_call_count: list[int] = []


@pytest.fixture(autouse=True)
def _track_offline_stub_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``OfflineONNXMiniLMStub.__call__`` so any invocation, from any
    test in this module, is recorded into the module-level
    ``_stub_call_count`` list. Deliberately does not REPLACE the stub's
    behavior (the wrapped call still runs the real stub logic if it is ever
    reached) -- this probe only counts calls, it never changes one."""
    original_call = _offline_embeddings_module.OfflineONNXMiniLMStub.__call__

    def _tracking_call(self: object, *args: object, **kwargs: object) -> object:
        _stub_call_count.append(1)
        return original_call(self, *args, **kwargs)

    monkeypatch.setattr(
        _offline_embeddings_module.OfflineONNXMiniLMStub, "__call__", _tracking_call
    )


def test_no_lexical_hash_stub_used_in_this_module() -> None:
    """Must be the LAST test declared in this module: pytest preserves
    declaration order within a module when no randomization plugin is
    active, and this repo runs none (`pytest-randomly` is not a dependency
    here -- `pyproject.toml`'s `[project.optional-dependencies]`/`dev`
    extra does not list it, and CI's `embedding-suite` job runs a plain
    `pytest tests/ -m embedding` with no `-p no:randomly` needed because
    there is no such plugin to disable). Running last means
    `_stub_call_count` reflects every test that ran before it in this
    module, not just its own setup.

    This is a regression trip-wire, not the primary proof the real model is
    used -- the primary proof is structural: this module's
    `pytestmark = pytest.mark.embedding` makes
    `tests/conftest.py::_default_selection` return `False` for every node
    here, which is what keeps `_offline_embedding_function` from ever
    patching `ONNXMiniLM_L6_V2` in the first place (see module docstring).
    This test catches the case where that structural guarantee silently
    breaks (e.g. a future edit to `_default_selection` that stops checking
    the `embedding` marker) by proving the STUB was never actually
    exercised, not just that the module carries the right marker.
    """
    assert _stub_call_count == [], (
        f"the offline lexical-hash embedding stub "
        f"(tests.offline_embeddings.OfflineONNXMiniLMStub) was invoked "
        f"{len(_stub_call_count)} time(s) during this embedding-marked "
        f"module's run -- every query and index build here must use the "
        f"REAL all-MiniLM-L6-v2 model, never the offline stand-in (issue "
        f"athenaeum#1800 exists because a MISMATCH between the two, not "
        f"just the stub's mere presence, silently invalidated the sibling "
        f"module's vector measurements)."
    )
