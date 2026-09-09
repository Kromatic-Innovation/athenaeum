# SPDX-License-Identifier: Apache-2.0
"""Recall relevance floor -- pins both directions (issue athenaeum#1492, AC4).

athenaeum#1492's defect: asked a question the corpus cannot answer, recall
returns a confident, well-formatted set of plausible-but-wrong pages instead
of nothing. The fix is a relevance floor (``athenaeum.config.
resolve_recall_relevance_floor`` + ``athenaeum.search.meets_relevance_floor``)
that ships INACTIVE -- a genuine no-op until an operator opts in, because
choosing a production threshold is explicitly out of scope for this issue.

Two test classes, two different things pinned:

* :class:`TestRelevanceFloorResolver` -- fast, corpus-free unit tests of the
  resolver + comparator. Proves AC1's "inactive by default, for every
  backend and both call paths" and AC3's "the push path's floor is settable
  independently of the explicit-call path" directly against the mechanism,
  without needing a materialized corpus or an index build.
* :class:`TestAbstentionProbesAbstainOnlyWhenFloorIsActive` -- the AC4 test
  named by the issue. Runs the real ``recall_search`` entry point against
  the three ``abstention``-class probes in
  ``tests/evals/data/corpus/probes/probes.yaml``, on both FTS5 and keyword,
  and asserts BOTH directions:

  1. with the floor set high enough, the probe returns an explicitly empty
     result (AC2, AC4 direction 1);
  2. with the floor INACTIVE (the default), the same probe returns a
     non-empty, confident-but-wrong result -- i.e. athenaeum#1492's defect is
     still reproducible (AC4 direction 2, the anti-vacuity clause: a test
     that only asserted direction 1 would pass against an implementation
     that always returns empty).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.config import resolve_recall_relevance_floor
from athenaeum.mcp_server import recall_search
from athenaeum.search import get_backend, meets_relevance_floor
from tests.evals.corpus import build_corpus, load_probes

# The four env vars ``resolve_recall_relevance_floor`` reads (see that
# function's docstring). Cleared in every test so a variable leaking from the
# real shell environment can never make a test pass or fail for the wrong
# reason.
_RECALL_FLOOR_ENV_NAMES = (
    "ATHENAEUM_RECALL_MIN_SCORE_FTS5",
    "ATHENAEUM_RECALL_MIN_SCORE_KEYWORD",
    "ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5",
    "ATHENAEUM_RECALL_PUSH_MIN_SCORE_KEYWORD",
)


@pytest.fixture(autouse=True)
def _clean_floor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RECALL_FLOOR_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Unit tests: the resolver + comparator, no corpus needed
# ---------------------------------------------------------------------------


class TestRelevanceFloorResolver:
    """AC1 and AC3, proved directly against the mechanism."""

    @pytest.mark.parametrize("backend", ["fts5", "keyword", "vector", "made-up-backend"])
    @pytest.mark.parametrize("unprompted", [False, True])
    def test_default_is_inactive_everywhere(self, backend: str, unprompted: bool) -> None:
        """AC1: no config, no env -> None (no floor) for every backend and
        both call paths. This is what makes shipping the floor a no-op."""
        assert resolve_recall_relevance_floor(None, backend, unprompted=unprompted) is None

    def test_unrecognized_backend_never_gets_a_floor(self) -> None:
        """``vector`` is not named in athenaeum#1492's acceptance criteria; even
        an explicit config value for it must not produce a floor."""
        config = {"recall": {"relevance_floor": {"vector": 0.5}}}
        assert resolve_recall_relevance_floor(config, "vector", unprompted=False) is None

    def test_yaml_floor_is_read_per_backend(self) -> None:
        config = {"recall": {"relevance_floor": {"fts5": -6.0, "keyword": 12.0}}}
        assert resolve_recall_relevance_floor(config, "fts5", unprompted=False) == -6.0
        assert resolve_recall_relevance_floor(config, "keyword", unprompted=False) == 12.0

    def test_push_and_explicit_call_floors_are_independently_settable(self) -> None:
        """AC3: the push path's floor is settable independently of the
        explicit-call path -- proved by giving them DIFFERENT values and
        reading each back through its own ``unprompted`` flag."""
        config = {
            "recall": {
                "relevance_floor": {
                    "fts5": -2.0,
                    "push": {"fts5": -9.0},
                }
            }
        }
        assert resolve_recall_relevance_floor(config, "fts5", unprompted=False) == -2.0
        assert resolve_recall_relevance_floor(config, "fts5", unprompted=True) == -9.0

    def test_explicit_call_floor_unset_does_not_leak_from_push(self) -> None:
        """The inverse of the test above: setting ONLY the push floor must
        leave the explicit-call path inactive, not silently inherit it."""
        config = {"recall": {"relevance_floor": {"push": {"keyword": 20.0}}}}
        assert resolve_recall_relevance_floor(config, "keyword", unprompted=False) is None
        assert resolve_recall_relevance_floor(config, "keyword", unprompted=True) == 20.0

    def test_env_beats_yaml_independently_per_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_KEYWORD", "42")
        config = {
            "recall": {
                "relevance_floor": {"keyword": 5.0, "push": {"keyword": 5.0}},
            }
        }
        # Explicit-call path: no env set for it -> yaml value.
        assert resolve_recall_relevance_floor(config, "keyword", unprompted=False) == 5.0
        # Push path: env set -> env wins over the yaml value for the SAME path.
        assert resolve_recall_relevance_floor(config, "keyword", unprompted=True) == 42.0

    def test_malformed_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RECALL_MIN_SCORE_FTS5", "not-a-number")
        config = {"recall": {"relevance_floor": {"fts5": -3.0}}}
        assert resolve_recall_relevance_floor(config, "fts5", unprompted=False) == -3.0

    def test_meets_relevance_floor_is_a_noop_when_inactive(self) -> None:
        assert meets_relevance_floor("fts5", -1000.0, None) is True
        assert meets_relevance_floor("keyword", -1000.0, None) is True

    def test_meets_relevance_floor_directions_differ_by_backend(self) -> None:
        """FTS5's rank is lower-is-better; keyword's score is higher-is-better.
        A floor comparator that used one direction for both would silently
        invert one backend's behavior."""
        # FTS5: more negative is better, so a MORE negative score clears a
        # LESS negative floor, and vice versa.
        assert meets_relevance_floor("fts5", -10.0, -5.0) is True
        assert meets_relevance_floor("fts5", -1.0, -5.0) is False
        # keyword: higher is better.
        assert meets_relevance_floor("keyword", 40.0, 10.0) is True
        assert meets_relevance_floor("keyword", 5.0, 10.0) is False


# ---------------------------------------------------------------------------
# AC4: the abstention probes, end-to-end, both directions
# ---------------------------------------------------------------------------

# "small" (not "core"): the hand-authored core alone does not reliably put a
# distractor at the top of every abstention probe's ranking on FTS5 -- one of
# the three already returns nothing at core scale, which would make the
# floor-INACTIVE assertion vacuous for that probe. "small" adds the
# distractor tier (built from each probe's own ``distractor_terms``), which
# is what makes all three probes reproduce athenaeum#1492's defect --
# confidently wrong, non-empty -- on BOTH backends. Measured this dispatch:
# FTS5 ranks land in [-9.9, -4.0]; keyword scores land in [12.0, 49.0].
_CORPUS_SCALE = "small"

# Chosen far outside the observed ranges above -- excludes every hit at this
# scale without hand-tuning to a fragile boundary value.
_ACTIVE_FLOOR_CONFIG = {
    "recall": {"relevance_floor": {"fts5": -1000.0, "keyword": 1_000_000.0}}
}
_INACTIVE_FLOOR_CONFIG: dict[str, object] | None = None

_BACKENDS = ("fts5", "keyword")


def _abstention_probe_ids() -> list[str]:
    ids = [p.id for p in load_probes() if p.probe_class == "abstention"]
    assert len(ids) == 3, f"expected 3 abstention probes in probes.yaml, found: {ids}"
    return ids


def _abstention_probe_query(probe_id: str) -> str:
    for probe in load_probes():
        if probe.id == probe_id:
            return probe.query
    raise AssertionError(f"unknown abstention probe id {probe_id!r}")


def _is_explicitly_empty(output: str) -> bool:
    return output.startswith("No wiki pages matched query:")


@pytest.fixture(scope="module")
def abstention_wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the synthetic corpus once and build its FTS5 index once.

    Shared read-only across every test below -- neither backend mutates the
    wiki tree or the index at query time, so one build serves the whole
    (probe x backend x floor-state) matrix cheaply.
    """
    corpus = build_corpus(scale=_CORPUS_SCALE)
    root = tmp_path_factory.mktemp("athenaeum-1492-corpus")
    wiki_root = corpus.materialize(root)
    get_backend("fts5").build_index(wiki_root, root / "cache")
    return wiki_root


def _run(wiki_root: Path, query: str, backend: str, config: dict[str, object] | None) -> str:
    return recall_search(
        wiki_root,
        query,
        top_k=5,
        search_backend=backend,
        cache_dir=wiki_root.parent / "cache",
        config=config,
    )


class TestAbstentionProbesAbstainOnlyWhenFloorIsActive:
    @pytest.mark.parametrize("backend", _BACKENDS)
    @pytest.mark.parametrize("probe_id", _abstention_probe_ids())
    def test_floor_inactive_still_reproduces_the_defect(
        self, abstention_wiki: Path, probe_id: str, backend: str
    ) -> None:
        """AC4 direction 2 (the anti-vacuity clause): with the floor at its
        shipped-default INACTIVE state, the probe still returns a non-empty,
        confidently-wrong result -- proving this test would have caught an
        implementation that always returns empty, and proving AC1's "no
        current behaviour changes on merge" from the other side."""
        output = _run(
            abstention_wiki,
            _abstention_probe_query(probe_id),
            backend,
            _INACTIVE_FLOOR_CONFIG,
        )
        assert not _is_explicitly_empty(output), (
            f"{backend}/{probe_id}: expected the pre-existing (wrong) "
            f"non-empty behaviour with the floor inactive; got an explicitly "
            f"empty result instead -- output: {output!r}"
        )

    @pytest.mark.parametrize("backend", _BACKENDS)
    @pytest.mark.parametrize("probe_id", _abstention_probe_ids())
    def test_floor_active_abstains(
        self, abstention_wiki: Path, probe_id: str, backend: str
    ) -> None:
        """AC2 + AC4 direction 1: with the floor set high enough, the SAME
        probe/backend pair the test above just proved returns a wrong page
        instead returns an explicitly empty result."""
        output = _run(
            abstention_wiki,
            _abstention_probe_query(probe_id),
            backend,
            _ACTIVE_FLOOR_CONFIG,
        )
        assert _is_explicitly_empty(output), (
            f"{backend}/{probe_id}: expected an explicitly empty result with "
            f"the floor active; got: {output!r}"
        )
