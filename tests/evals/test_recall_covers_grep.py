# SPDX-License-Identifier: Apache-2.0
"""Offline retrieval-coverage test (issue athenaeum#1770).

The ``native_grep`` rollout arm (``tests/evals/rollout.py::run_native_grep``)
reads the same corpus as Athenaeum through nothing more than file search over
topic files. In the fourth live north-star grid it beat the verdict arm on
two probes because a keyword search found the expected page and ``recall``
did not surface it in its own top hits. The operator's standing rule: if a
page surfaces with a simple grep on the question, it must surface in
Athenaeum's ``recall`` too, or the retrieval layer -- not the model -- lost
the row.

This module checks exactly that property, offline and deterministically, for
every non-abstention probe (``probe.expected_uids`` non-empty) at the
``core`` and ``medium`` corpus scales:

1. Materializes the corpus the same way ``north_star_cli.py`` does
   (:meth:`tests.evals.corpus.Corpus.materialize`) and builds the SAME FTS5
   index the grid uses by default (``get_backend("fts5").build_index``,
   the exact call ``run_probe_all_arms`` makes -- see
   ``tests/evals/rollout.py`` around line 2760).
2. Computes a grep baseline: for each query content term
   (:func:`tests.evals.corpus._content_terms`), the set of page uids whose
   body, name, or aliases contain that term -- what a model's own
   ``grep -ril`` over the materialized topic files would see (this mirrors
   :func:`tests.evals.rollout.run_native_grep`'s prompt: "use your file
   search tools", over the same on-disk tree).
3. Runs a REAL ``recall_search`` call (``src/athenaeum/mcp_server.py:242``)
   on the raw probe query, at the grid's default backend and ``top_k=5``
   (the same limit :func:`tests.evals.rollout.run_push_pages_upper_bound`
   passes).
4. Asserts every expected page the grep baseline reaches is present in
   recall's result set. **The invariant is one-directional**: recall is not
   required to return everything grep returns, and grep is *expected* to
   over-retrieve (that is the whole reason a smarter ranker is worth having)
   -- see the "precision measurement" section below, which reports that
   without asserting on it.

Operator scope refinement (added to the issue after the initial proposal):
alongside the coverage invariant, this module also computes and PRINTS
(never asserts on) a precision/relevance table per probe:

* how many pages each retriever returns that are **not** in
  ``expected_uids`` (grep's full hit set vs. recall's top-``k`` vs. the
  hook's top 3) -- this is the "grep over-retrieves, recall should not"
  measurement;
* the rank position of each expected page within recall's ordering;
* whether each of the probe's ``must_not_rank`` pages was surfaced by
  grep, recall, or the hook -- the disambiguation win the operator expects
  Athenaeum to show over a bare keyword search (e.g. the
  ``rowanwrenfield`` repo page surfacing on a question about Rowan
  Wrenfield the person).

A per-probe-class summary row aggregates: expected pages reached by grep,
by recall, and by the hook's top 3, plus irrelevant pages returned by each.

Vector backend: the same comparison runs a second time against the
``vector`` backend (skipped cleanly when ``chromadb`` -- the same import
every other vector-backed test in this repo gates on, e.g.
``tests/test_search.py`` -- is unavailable), because live hook traffic is
vector while the grid defaults to FTS5.

Hook breadcrumb measurement: uses the REAL ``examples/claude-code/
session-start-recall.sh`` / ``user-prompt-recall.sh`` pair -- the same
subprocess seam ``tests.evals.rollout.build_push_breadcrumb_context`` uses
for the ``PUSH_BREADCRUMB`` arm -- built ONCE per corpus scale (session
start) and queried once per probe (user prompt), matching how a real
Claude Code session actually uses the hook (index built once, queried many
times). The hook's own additionalContext text carries no ``uid`` field (it
renders ``name [— description]`` bullets only -- see
``examples/claude-code/user-prompt-recall.sh``'s render loop), so hook hits
are mapped back to uids by exact page-name lookup. If ``bash`` is
unavailable, or the hook subprocess seam otherwise fails, this module falls
back to the SAME FTS5 index queried directly with ``n=3`` (a plain
``LIMIT 3``-equivalent) and labels the row as a fallback rather than a real
hook run, per issue athenaeum#1770 item 1.

**Deliberately unmarked** (no ``rollout`` marker): every comparison here
runs against a locally materialized corpus and a locally built FTS5/vector
index. No network call, no ``ANTHROPIC_API_KEY``, no ``claude -p`` spawn,
no model call of any kind.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.search import get_backend
from tests.evals.corpus import Corpus, Probe, _content_terms, build_corpus
from tests.evals.rollout import (
    SESSION_START_HOOK,
    USER_PROMPT_HOOK,
    build_breadcrumb_hook_env,
)

try:
    import chromadb  # noqa: F401

    _VECTOR_AVAILABLE = True
    _VECTOR_SKIP_REASON = ""
except ImportError as _exc:  # pragma: no cover -- environment dependent
    _VECTOR_AVAILABLE = False
    _VECTOR_SKIP_REASON = f"chromadb not installed ({_exc})"

#: Corpus scales this module runs the comparison against -- issue athenaeum#1770's
#: own scope. ``core`` is the fast, fully hand-authored hardcoded corpus;
#: ``medium`` (1,000 pages, ``tests.evals.corpus.SCALES``) adds distractor
#: and ballast retrieval pressure without paying ``large``/``xlarge``'s
#: build cost in a module every offline CI job runs.
_SCALES: tuple[str, ...] = ("core", "medium")

#: Same top_k the grid's PUSH_PAGES_UPPER_BOUND arm and the api-mode PULL
#: tool executor's `recall` call both use (`tests/evals/rollout.py`).
_TOP_K = 5

#: The shipped hook's own cap (`examples/claude-code/user-prompt-recall.sh`:
#: `head -3`). Not this module's to change -- see the issue's "Out of scope".
_HOOK_TOP_K = 3

#: Matches one rendered recall hit's own `**Uid:** <uid>` header line
#: (`src/athenaeum/mcp_server.py`, `_recall_via_backend`'s per-hit block) --
#: parsed off REAL `recall_search` output, never reimplemented ranking.
_UID_LINE_RE = re.compile(r"\*\*Uid:\*\*\s*(\S+)")

#: Matches one hook breadcrumb bullet line (`  - <name>` or
#: `  - <name> — <description>`); see `examples/claude-code/
#: user-prompt-recall.sh`'s render loop -- the bullet carries no uid, only
#: `name` (bold em dash separates an optional clamped description).
_HOOK_BULLET_RE = re.compile(r"^\s*-\s*(.+?)(?:\s+—\s+.*)?$")


# ---------------------------------------------------------------------------
# Grep baseline
# ---------------------------------------------------------------------------


def _grep_hits(corpus: Corpus, query: str) -> dict[str, set[str]]:
    """uid -> matched content terms, over EVERY page in *corpus* -- mirrors
    what a model's own file search over the materialized topic tree would
    see (``_content_terms`` is the same content-term definition
    ``validate_core``'s ``follow_through`` check and
    ``north_star_report.lexical_overlap`` both use). Matches against body,
    name, and aliases -- the text on the rendered page, not internal
    frontmatter a grep over markdown files would also read verbatim.
    """
    terms = _content_terms(query)
    if not terms:
        return {}
    hits: dict[str, set[str]] = {}
    for page in corpus.pages:
        haystack = _content_terms(" ".join((page.body, page.name, *page.aliases)))
        matched = terms & haystack
        if matched:
            hits[page.uid] = matched
    return hits


# ---------------------------------------------------------------------------
# Recall (real recall_search call)
# ---------------------------------------------------------------------------


def _recall_ranked_uids(
    wiki_root: Path, query: str, *, search_backend: str, cache_dir: Path
) -> list[str]:
    """Ranked uids parsed off a REAL ``recall_search`` call's own rendered
    ``**Uid:**`` header -- never a reimplementation of its ranking."""
    text = recall_search(
        wiki_root, query, top_k=_TOP_K, search_backend=search_backend, cache_dir=cache_dir
    )
    return _UID_LINE_RE.findall(text)


def _recall_scored_hits(
    wiki_root: Path, query: str, *, search_backend: str, cache_dir: Path
) -> list[tuple[str, float]]:
    """Raw (uid, score) pairs from the SAME backend ``recall_search`` itself
    dispatches through (``recall_search``'s own docstring: "All three
    dispatch through athenaeum.search.get_backend") -- used only for the
    failure message's "recall's top hits with scores" (issue athenaeum#1770 AC2)
    and the precision table; the coverage assertion itself uses
    :func:`_recall_ranked_uids`, the real public entry point.
    """
    hits = get_backend(search_backend).query(query, cache_dir, n=_TOP_K, wiki_root=wiki_root)
    scored: list[tuple[str, float]] = []
    for filename, _name, score in hits:
        uid = filename[:-3] if filename.endswith(".md") else filename
        scored.append((uid, score))
    return scored


# ---------------------------------------------------------------------------
# Hook breadcrumbs (real user-prompt-recall.sh seam, FTS5 LIMIT-3 fallback)
# ---------------------------------------------------------------------------


def _hook_session_ready(knowledge_root: Path, hook_home: Path) -> bool:
    """Run ``session-start-recall.sh`` ONCE for *hook_home*, building the
    hook's own index from *knowledge_root*. Returns ``False`` (never
    raises) when the seam is unavailable offline -- no ``bash`` on PATH, or
    the hook itself errors -- so callers fall back to a direct FTS5 query
    (issue athenaeum#1770 item 1). Mirrors
    ``tests.evals.rollout.build_push_breadcrumb_context``'s own subprocess
    call, split out so it runs once per scale rather than once per probe.
    """
    if shutil.which("bash") is None:
        return False
    env = build_breadcrumb_hook_env(knowledge_root, hook_home)
    try:
        subprocess.run(
            ["bash", str(SESSION_START_HOOK)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=True,
        )
        return True
    except Exception:  # noqa: BLE001 -- offline fallback, never fatal
        return False


def _hook_breadcrumb_names(knowledge_root: Path, hook_home: Path, query: str) -> list[str]:
    """One query against the already-built hook session -- the page NAMES
    (not uids; see this module's docstring) the hook's own top-3 breadcrumb
    output names, in the hook's own order."""
    env = build_breadcrumb_hook_env(knowledge_root, hook_home)
    result = subprocess.run(
        ["bash", str(USER_PROMPT_HOOK)],
        input=json.dumps({"prompt": query, "session_id": f"cov-{uuid.uuid4().hex}"}),
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    payload = json.loads(result.stdout)
    text = str(payload.get("hookSpecificOutput", {}).get("additionalContext", ""))
    names: list[str] = []
    for line in text.split("\n"):
        m = _HOOK_BULLET_RE.match(line)
        if m:
            names.append(m.group(1).strip())
    return names


def _hook_ranked_uids(
    *,
    knowledge_root: Path,
    hook_home: Path,
    query: str,
    hook_ready: bool,
    name_to_uid: dict[str, str],
    wiki_root: Path,
    fts5_cache_dir: Path,
) -> tuple[list[str], bool]:
    """The hook's top-3 uids, and whether this was a REAL hook run (``True``)
    or the FTS5 ``n=3`` fallback (``False``)."""
    if hook_ready:
        names = _hook_breadcrumb_names(knowledge_root, hook_home, query)
        uids = [name_to_uid[name] for name in names if name in name_to_uid]
        return uids, True
    hits = get_backend("fts5").query(query, fts5_cache_dir, n=_HOOK_TOP_K, wiki_root=wiki_root)
    uids = [fname[:-3] if fname.endswith(".md") else fname for fname, _n, _s in hits]
    return uids, False


# ---------------------------------------------------------------------------
# Per-probe measurement
# ---------------------------------------------------------------------------


@dataclass
class _ProbeMeasurement:
    scale: str
    backend: str
    probe: Probe
    grep_hits: dict[str, set[str]]
    recall_ranked: list[str]
    recall_scored: list[tuple[str, float]]
    hook_ranked: list[str]
    hook_is_real: bool

    @property
    def expected(self) -> set[str]:
        return set(self.probe.expected_uids)

    @property
    def grep_reachable_expected(self) -> set[str]:
        return self.expected & self.grep_hits.keys()

    @property
    def grep_irrelevant_count(self) -> int:
        return len(self.grep_hits.keys() - self.expected)

    @property
    def recall_irrelevant_count(self) -> int:
        return len({u for u in self.recall_ranked if u not in self.expected})

    @property
    def hook_irrelevant_count(self) -> int:
        return len({u for u in self.hook_ranked if u not in self.expected})

    def recall_rank_of(self, uid: str) -> int | None:
        try:
            return self.recall_ranked.index(uid) + 1
        except ValueError:
            return None

    def must_not_rank_surfaced(self) -> dict[str, dict[str, bool]]:
        """uid -> {"grep": bool, "recall": bool, "hook": bool} for each of
        ``probe.must_not_rank`` -- the disambiguation win column (issue
        athenaeum#1770 scope refinement)."""
        out: dict[str, dict[str, bool]] = {}
        for uid in self.probe.must_not_rank:
            out[uid] = {
                "grep": uid in self.grep_hits,
                "recall": uid in self.recall_ranked,
                "hook": uid in self.hook_ranked,
            }
        return out


def _measure(
    *,
    scale: str,
    backend: str,
    probe: Probe,
    corpus: Corpus,
    wiki_root: Path,
    cache_dir: Path,
    knowledge_root: Path,
    hook_home: Path,
    hook_ready: bool,
    name_to_uid: dict[str, str],
) -> _ProbeMeasurement:
    grep_hits = _grep_hits(corpus, probe.query)
    recall_ranked = _recall_ranked_uids(
        wiki_root, probe.query, search_backend=backend, cache_dir=cache_dir
    )
    recall_scored = _recall_scored_hits(
        wiki_root, probe.query, search_backend=backend, cache_dir=cache_dir
    )
    hook_ranked, hook_is_real = _hook_ranked_uids(
        knowledge_root=knowledge_root,
        hook_home=hook_home,
        query=probe.query,
        hook_ready=hook_ready,
        name_to_uid=name_to_uid,
        wiki_root=wiki_root,
        fts5_cache_dir=cache_dir if backend == "fts5" else cache_dir,
    )
    return _ProbeMeasurement(
        scale=scale,
        backend=backend,
        probe=probe,
        grep_hits=grep_hits,
        recall_ranked=recall_ranked,
        recall_scored=recall_scored,
        hook_ranked=hook_ranked,
        hook_is_real=hook_is_real,
    )


def _failure_message(measurement: _ProbeMeasurement, missing: set[str]) -> str:
    lines = [
        f"probe={measurement.probe.id!r} class={measurement.probe.probe_class!r} "
        f"scale={measurement.scale!r} backend={measurement.backend!r}",
        f"query={measurement.probe.query!r}",
        "",
        "grep-reachable expected pages missing from recall's top " f"{_TOP_K}:",
    ]
    for uid in sorted(missing):
        terms = sorted(measurement.grep_hits.get(uid, ()))
        lines.append(f"  - {uid}: grep terms {terms}")
    lines.append("")
    lines.append(f"recall's top {_TOP_K} hits (uid, score):")
    for uid, score in measurement.recall_scored:
        lines.append(f"  - {uid}: {score:.4f}")
    return "\n".join(lines)


def _print_probe_table_row(measurement: _ProbeMeasurement) -> None:
    m = measurement
    ranks = {uid: m.recall_rank_of(uid) for uid in sorted(m.expected)}
    mnr = m.must_not_rank_surfaced()
    print(
        f"| {m.scale} | {m.backend} | {m.probe.id} | {m.probe.probe_class} | "
        f"{len(m.grep_reachable_expected)}/{len(m.expected)} | "
        f"grep_irrelevant={m.grep_irrelevant_count} | "
        f"recall_irrelevant={m.recall_irrelevant_count} | "
        f"hook_irrelevant={m.hook_irrelevant_count} "
        f"({'real' if m.hook_is_real else 'fts5-fallback'}) | "
        f"recall_ranks={ranks} | "
        f"must_not_rank={mnr} |"
    )


# ---------------------------------------------------------------------------
# Fixtures: materialize once per scale, build the FTS5/vector index once
# ---------------------------------------------------------------------------


@dataclass
class _ScaleFixture:
    corpus: Corpus
    wiki_root: Path
    fts5_cache_dir: Path
    vector_cache_dir: Path | None
    knowledge_root: Path
    hook_home: Path
    hook_ready: bool
    name_to_uid: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module", params=_SCALES)
def scale_fixture(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> _ScaleFixture:
    scale = request.param
    corpus = build_corpus(scale)
    root = tmp_path_factory.mktemp(f"corpus-{scale}")
    wiki_root = corpus.materialize(root)
    fts5_cache_dir = root / "fts5-cache"
    get_backend("fts5").build_index(wiki_root, fts5_cache_dir)

    # Issue athenaeum#1792: the vector index shares `fts5_cache_dir` as its
    # cache root (FTS5's `wiki-index.db` and the vector backend's
    # `wiki-vectors/` subdir coexist there with no collision) rather than a
    # separate `vector-cache` directory. This matches production's actual
    # layout -- ONE cache_dir (`resolve_cache_dir()`) backs both backends,
    # exactly what `examples/claude-code/user-prompt-recall.sh` assumes
    # (`DB_FILE`/`VECTOR_DIR` as siblings under one `CACHE_DIR`) -- and is
    # required for the vector backend's hybrid dispatch
    # (`athenaeum.search.fts5_index_available` / the RRF fusion block in
    # `recall_search`) to find an FTS5 index to fuse against at all. Before
    # this issue the two lived in separate roots purely for this module's
    # own test isolation; that isolation was never a documented invariant
    # and nothing else in this module depends on the two staying apart.
    vector_cache_dir: Path | None = None
    if _VECTOR_AVAILABLE:
        vector_cache_dir = fts5_cache_dir
        get_backend("vector").build_index(wiki_root, vector_cache_dir)

    hook_home = root / "hook-home"
    hook_ready = _hook_session_ready(root, hook_home)

    return _ScaleFixture(
        corpus=corpus,
        wiki_root=wiki_root,
        fts5_cache_dir=fts5_cache_dir,
        vector_cache_dir=vector_cache_dir,
        knowledge_root=root,
        hook_home=hook_home,
        hook_ready=hook_ready,
        name_to_uid={page.name: page.uid for page in corpus.pages},
    )


def _non_abstention_probes() -> tuple[Probe, ...]:
    return tuple(p for p in build_corpus("core").probes if p.expected_uids)


_PROBE_IDS: tuple[str, ...] = tuple(p.id for p in _non_abstention_probes())


def _probe_by_id(corpus: Corpus, probe_id: str) -> Probe:
    return next(p for p in corpus.probes if p.id == probe_id)


# ---------------------------------------------------------------------------
# Retrieval-side misses found by a real run of this module (issue
# athenaeum#1770 AC4) against the current corpus/index. DO NOT edit the
# fixtures or this test to make these pass -- a fix belongs in
# src/athenaeum/search.py or the recall query path, tracked separately (see
# the PR body / docs/design/native-memory-baseline.md §5). Each entry is
# marked xfail(strict=True): a case that starts passing flips to an XPASS,
# which is the signal to remove it from this set.
# ---------------------------------------------------------------------------

#: fts5 backend -- 6 of 52 (scale, probe) cases, measured 2026-09-17 against
#: develop @ 5693c1c7 (post athenaeum#1768/#1769).
_FTS5_XFAIL: frozenset[tuple[str, str]] = frozenset(
    {
        ("core", "confidentiality_rule"),
        ("core", "budget_threshold_current"),
        ("medium", "confidentiality_rule"),
        ("medium", "budget_threshold_current"),
        ("medium", "surname_is_ambiguous"),
        ("medium", "former_client_not_current"),
    }
)

#: vector backend -- 17 of 52 (scale, probe) cases remain, measured
#: 2026-09-17 against athenaeum#1792's reciprocal-rank-fusion hybrid (fuses
#: a WIDENED vector list with a widened FTS5 list, both re-queried at
#: `_HYBRID_FTS5_CANDIDATE_POOL` width over the same index root, each with
#: its own relevance floor applied before fusion -- see
#: ``athenaeum.mcp_server.recall_search``'s hybrid block and
#: ``athenaeum.search.reciprocal_rank_fusion``). Down from the 49 measured
#: pre-fusion (issue athenaeum#1770's original finding, kept in git history
#: / the athenaeum#1792 PR body, not here).
#:
#: Widening ONLY the fts5 side (an earlier version of this fix) left this
#: set at 20, not zero: reciprocal rank fusion sums `1/(k+rank)` across
#: every list a hit appears in, so with the vector side left at its native
#: `top_k=5`, at most 5 hits could ever be "present in both lists" and the
#: fused top-5 became exactly that intersection once 5 hits overlapped --
#: no fts5-only hit could enter regardless of its fts5 rank. Widening BOTH
#: sides (this version) raised that ceiling and rescued 3 more cases
#: (core: portal_design_reviewer, person_not_repo; medium:
#: thorncastle_first_contact).
#:
#: Five of these seventeen are also in ``_FTS5_XFAIL`` above
#: (confidentiality_rule and budget_threshold_current at both scales, plus
#: medium's surname_is_ambiguous) -- fusion cannot surface a page neither
#: input list ranks within its own widened window. The other twelve are
#: cases where FTS5 itself reaches the page somewhere in its own ranking
#: but the page's *grep*-reachability comes from a term that still ranks
#: it outside BOTH backends' widened top-N for this probe's exact query
#: wording -- a bm25-ranking / query-construction question for FTS5's own
#: path (athenaeum#1789's scope) or the embedding model's own semantic gap,
#: not something a fusion mechanism operating on ranked lists can repair
#: without also widening the pool arbitrarily far (a cost/precision
#: tradeoff, not a correctness bug in the fusion itself). Kept as one
#: explicit set, not a blanket "xfail everything for this backend", so a
#: genuine per-case fix is visible one entry at a time.
_VECTOR_XFAIL: frozenset[tuple[str, str]] = frozenset(
    {
        ("core", "pto_allowance"),
        ("core", "confidentiality_rule"),
        ("core", "budget_threshold_current"),
        ("core", "surname_is_ambiguous"),
        ("core", "former_client_not_current"),
        ("medium", "pto_allowance"),
        ("medium", "confidentiality_rule"),
        ("medium", "portal_design_reviewer"),
        ("medium", "ratecard_tooling_owner"),
        ("medium", "standup_time_current"),
        ("medium", "budget_threshold_current"),
        ("medium", "tamsin_ferro_role_change"),
        ("medium", "person_not_repo"),
        ("medium", "surname_is_ambiguous"),
        ("medium", "given_name_is_ambiguous"),
        ("medium", "former_client_not_current"),
        ("medium", "keelbridge_programme_scope"),
    }
)


# ---------------------------------------------------------------------------
# The coverage invariant -- fts5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe_id", _PROBE_IDS)
def test_recall_covers_grep_reachable_expected_pages_fts5(
    request: pytest.FixtureRequest, scale_fixture: _ScaleFixture, probe_id: str
) -> None:
    """AC1/AC2 (issue athenaeum#1770): every expected page a grep baseline
    reaches must also be in recall's fts5 result set. On failure, names the
    probe, the expected uid, the grep terms that reached it, and recall's
    top hits with scores."""
    fx = scale_fixture
    if (fx.corpus.scale, probe_id) in _FTS5_XFAIL:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"athenaeum#1770: measured retrieval-side miss "
                    f"(fts5, {fx.corpus.scale}, {probe_id})"
                ),
            )
        )
    probe = _probe_by_id(fx.corpus, probe_id)
    measurement = _measure(
        scale=fx.corpus.scale,
        backend="fts5",
        probe=probe,
        corpus=fx.corpus,
        wiki_root=fx.wiki_root,
        cache_dir=fx.fts5_cache_dir,
        knowledge_root=fx.knowledge_root,
        hook_home=fx.hook_home,
        hook_ready=fx.hook_ready,
        name_to_uid=fx.name_to_uid,
    )
    _print_probe_table_row(measurement)
    missing = measurement.grep_reachable_expected - set(measurement.recall_ranked)
    assert not missing, _failure_message(measurement, missing)


# ---------------------------------------------------------------------------
# The coverage invariant -- vector (skipped when chromadb is unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _VECTOR_AVAILABLE, reason=_VECTOR_SKIP_REASON or "chromadb not installed")
@pytest.mark.parametrize("probe_id", _PROBE_IDS)
def test_recall_covers_grep_reachable_expected_pages_vector(
    request: pytest.FixtureRequest, scale_fixture: _ScaleFixture, probe_id: str
) -> None:
    """Same invariant as the fts5 test, against the ``vector`` backend --
    live hook traffic is vector by default while the grid defaults to
    fts5, so both are worth pinning independently."""
    fx = scale_fixture
    if (fx.corpus.scale, probe_id) in _VECTOR_XFAIL:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"athenaeum#1770: measured retrieval-side miss "
                    f"(vector, {fx.corpus.scale}, {probe_id})"
                ),
            )
        )
    assert fx.vector_cache_dir is not None  # guaranteed by the skipif above
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
    _print_probe_table_row(measurement)
    missing = measurement.grep_reachable_expected - set(measurement.recall_ranked)
    assert not missing, _failure_message(measurement, missing)


# ---------------------------------------------------------------------------
# Disambiguation win, asserted (issue athenaeum#1792 AC2) -- person_not_repo /
# repo_not_person. Previously this was only a PRINTED column
# (`_ProbeMeasurement.must_not_rank_surfaced`); this asserts it directly for
# both probes on both backends, so a regression in either ranker fails CI
# instead of requiring a human to read the precision table.
# ---------------------------------------------------------------------------

_PERSON_REPO_PROBE_IDS: tuple[str, ...] = ("person_not_repo", "repo_not_person")


@pytest.mark.parametrize("probe_id", _PERSON_REPO_PROBE_IDS)
def test_person_repo_disambiguation_excludes_wrong_page_fts5(
    scale_fixture: _ScaleFixture, probe_id: str
) -> None:
    """athenaeum#1792 AC2: the wrong page of the person/repo pair -- the
    probe's own ``must_not_rank`` uid -- must stay out of recall's top
    ``_TOP_K`` on the fts5 backend. A bare keyword search cannot tell
    "Rowan Wrenfield the person" from "rowanwrenfield the repo" (both
    probes' queries share every distractor term); this is the win a
    smarter ranker is worth having over grep, per PR athenaeum#1771's
    finding."""
    fx = scale_fixture
    probe = _probe_by_id(fx.corpus, probe_id)
    assert len(probe.must_not_rank) == 1
    wrong_uid = probe.must_not_rank[0]
    ranked = _recall_ranked_uids(
        fx.wiki_root, probe.query, search_backend="fts5", cache_dir=fx.fts5_cache_dir
    )
    assert wrong_uid not in ranked, (
        f"probe={probe_id!r} scale={fx.corpus.scale!r} backend='fts5': "
        f"{wrong_uid!r} (must_not_rank) surfaced in recall's top {_TOP_K}: {ranked!r}"
    )


@pytest.mark.skipif(not _VECTOR_AVAILABLE, reason=_VECTOR_SKIP_REASON or "chromadb not installed")
@pytest.mark.parametrize("probe_id", _PERSON_REPO_PROBE_IDS)
def test_person_repo_disambiguation_excludes_wrong_page_vector(
    scale_fixture: _ScaleFixture, probe_id: str
) -> None:
    """Same assertion as the fts5 test above, against the vector backend --
    the backend live hook traffic actually uses, and the one issue
    athenaeum#1792's hybrid rank fusion must not regress: a hybrid that
    fused in the wrong page just because fts5 might rank it (it does not,
    for this probe pair) would silently undo this win."""
    fx = scale_fixture
    assert fx.vector_cache_dir is not None  # guaranteed by the skipif above
    probe = _probe_by_id(fx.corpus, probe_id)
    wrong_uid = probe.must_not_rank[0]
    ranked = _recall_ranked_uids(
        fx.wiki_root, probe.query, search_backend="vector", cache_dir=fx.vector_cache_dir
    )
    assert wrong_uid not in ranked, (
        f"probe={probe_id!r} scale={fx.corpus.scale!r} backend='vector': "
        f"{wrong_uid!r} (must_not_rank) surfaced in recall's top {_TOP_K}: {ranked!r}"
    )


# ---------------------------------------------------------------------------
# Per-probe-class summary (printed only -- issue athenaeum#1770 scope
# refinement item: one row per probe class)
# ---------------------------------------------------------------------------


def test_print_per_probe_class_summary(scale_fixture: _ScaleFixture) -> None:
    """Not an assertion -- a printed summary table, one row per probe
    class, of expected pages reached by grep/recall/hook and irrelevant
    pages returned by each, for the fts5 backend at this scale. Copied
    (for ``core``/``fts5``, the grid's own default) into
    ``docs/design/native-memory-baseline.md`` §5 as a caveat on the
    ``push_breadcrumb*`` arms."""
    fx = scale_fixture
    by_class: dict[str, list[_ProbeMeasurement]] = {}
    for probe in _non_abstention_probes():
        measurement = _measure(
            scale=fx.corpus.scale,
            backend="fts5",
            probe=probe,
            corpus=fx.corpus,
            wiki_root=fx.wiki_root,
            cache_dir=fx.fts5_cache_dir,
            knowledge_root=fx.knowledge_root,
            hook_home=fx.hook_home,
            hook_ready=fx.hook_ready,
            name_to_uid=fx.name_to_uid,
        )
        by_class.setdefault(probe.probe_class, []).append(measurement)

    print(f"\nPer-probe-class summary -- scale={fx.corpus.scale} backend=fts5")
    print(
        "| probe_class | probes | expected | grep_reached | recall_reached | "
        "hook_reached | grep_irrelevant | recall_irrelevant | hook_irrelevant |"
    )
    for probe_class in sorted(by_class):
        rows = by_class[probe_class]
        n_expected = sum(len(m.expected) for m in rows)
        grep_reached = sum(len(m.grep_reachable_expected) for m in rows)
        recall_reached = sum(len(m.expected & set(m.recall_ranked)) for m in rows)
        hook_reached = sum(len(m.expected & set(m.hook_ranked)) for m in rows)
        grep_irrelevant = sum(m.grep_irrelevant_count for m in rows)
        recall_irrelevant = sum(m.recall_irrelevant_count for m in rows)
        hook_irrelevant = sum(m.hook_irrelevant_count for m in rows)
        print(
            f"| {probe_class} | {len(rows)} | {n_expected} | {grep_reached} | "
            f"{recall_reached} | {hook_reached} | {grep_irrelevant} | "
            f"{recall_irrelevant} | {hook_irrelevant} |"
        )
    assert by_class  # sanity: the corpus must carry at least one probe class
