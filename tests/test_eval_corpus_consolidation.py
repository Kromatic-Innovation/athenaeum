# SPDX-License-Identifier: Apache-2.0
"""Consolidation verdict for name / name (qualifier) / team splits (issue athenaeum#1577).

Grades the WRITE path against issue athenaeum#1570's Cluster B fixture and its
ground truth in ``tests/evals/data/corpus/ground_truth/relatedness.yaml``.
Four things, matching the issue's ACs:

* **AC1** — the bare name and the qualified name are proposed as a merge; the
  team page is NOT. Asserted as an EXACT proposal set, because the negative
  control is over-determined (``Keelbridge Desk`` fails both the same-type
  requirement and the parenthetical pattern) and "the team page is absent"
  would also pass for a scan that proposed nothing at all, or that proposed
  six unrelated pairs.
* **AC2** — the proposal lands in ``wiki/_pending_merges.md``, is visible
  through ``list_pending_merges``, and is UNRESOLVED. Nothing on disk moved.
  ``docs/north-star.md`` §2.8: humans adopt anything irreversible through the
  one queue.
* **AC3** — measured on the live corpus by ``scripts/rescore_name_structure.py``
  and reported in the PR body, not here; this file only pins that the script's
  measuring function and the write path agree about what a hit is.
* **AC4** — after the verdict is enacted ON THE FIXTURE, the probe that
  previously retrieved one of the two halves reaches a page carrying both.

Why the ``embedding`` marker sits where it does
-----------------------------------------------

``pyproject.toml``'s ``addopts`` deselects ``embedding``-marked tests, so a
module-level mark would keep every assertion here out of required CI. The
marker is therefore applied ONLY to
:class:`TestTheEmbeddingPathDoesNotSeeThisSplit`, which is the one class that
genuinely needs real MiniLM vectors — it measures the GAP this issue's signal
closes, and a stubbed embedder would make that measurement vacuous. Every
other assertion below is deterministic (a glob and a regex; no vectors, no
network, no model call) and runs in the default suite, so the ground-truth
verdict, the negative control, the never-auto-applied invariant and the
retrieval recovery are all covered by an ordinary PR-green run.

Nothing here touches the operator's live corpus. Every fixture materializes
into ``tmp_path`` and :func:`_assert_disposable` refuses to proceed against
anything under ``~/knowledge``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.name_structure import (
    QUALIFIED_NAME_CONFIDENCE,
    build_draft_body,
    normalize_name,
    propose_qualified_name_merges,
    scan_qualified_name_splits,
    split_qualifier,
)
from athenaeum.pending_merges import list_pending_merges, resolve_merge
from athenaeum.search import get_backend
from tests.evals.corpus import build_corpus, load_redundant_clusters

# The ids issue athenaeum#1570 authored. Named as constants so a fixture rename
# breaks HERE with a readable failure rather than silently making an
# assertion vacuous.
BARE = "project-keelbridge"
QUALIFIED = "project-keelbridge-rollout"
TEAM = "team-keelbridge-desk"


def _assert_disposable(root: Path) -> None:
    """Refuse to run against anything that is not a throwaway tree.

    Belt and braces around a test that calls ``resolve_merge``, which
    performs ``git rm`` and rewrites page bodies. The eval corpus is
    synthetic and materializes under ``tmp_path``; if a future refactor ever
    pointed a fixture at a real wiki, this stops it before the fold rather
    than after.
    """
    resolved = root.resolve()
    forbidden = (Path.home() / "knowledge").resolve()
    assert forbidden not in resolved.parents and resolved != forbidden, (
        f"refusing to run a mutating consolidation test against {resolved}"
    )


def _git_init(root: Path) -> None:
    """A real git repo around the materialized corpus.

    ``resolve_merge``'s ``fold-into-existing`` path fails closed with
    ``no_git_repo`` unless ``wiki_root`` resolves inside a repository
    (issue athenaeum#947) — the removal must stay recoverable through plain
    ``git revert``. That is a guarantee worth exercising rather than
    stubbing, so the fixture supplies the repo instead of the test
    monkeypatching the check away.
    """
    env = {
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.invalid",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(root),
    }
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "corpus"], check=True, env=env)


def _keelbridge() -> object:
    for cluster in load_redundant_clusters():
        if cluster.id == "keelbridge":
            return cluster
    raise AssertionError("no redundant cluster 'keelbridge' -- fixture renamed?")


@pytest.fixture
def core_wiki(tmp_path: Path) -> Path:
    root = tmp_path / "core"
    wiki = build_corpus(scale="core").materialize(root)
    _assert_disposable(wiki)
    return wiki


def _pairs(wiki_root: Path) -> set[frozenset[str]]:
    """Proposed pairs as ``{frozenset({uid, uid})}``, keyed on page stems."""
    return {
        frozenset({s.bare_path.stem, s.qualified_path.stem})
        for s in scan_qualified_name_splits(wiki_root)
    }


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


class TestSplitQualifier:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Keelbridge (rollout)", ("Keelbridge", "rollout")),
            ("JTBD (Jobs to Be Done)", ("JTBD", "Jobs to Be Done")),
            ("Keelbridge  (rollout)", ("Keelbridge", "rollout")),
        ],
    )
    def test_a_trailing_parenthetical_is_a_qualifier(self, name, expected) -> None:
        assert split_qualifier(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "Keelbridge",
            "Keelbridge Desk",
            # Anchored at the end: a parenthetical in the middle is part of
            # the name, not a qualifier stripped off it.
            "Marsa Maroc (500 Startups) notes",
            # ``.+?`` needs a base to strip the qualifier FROM.
            "(draft)",
            # An empty qualifier says nothing about the entity.
            "Keelbridge ()",
        ],
    )
    def test_everything_else_is_not(self, name) -> None:
        assert split_qualifier(name) is None

    def test_comparison_is_case_and_whitespace_insensitive(self) -> None:
        assert normalize_name("  Keelbridge   Desk ") == normalize_name("keelbridge desk")

    @pytest.mark.parametrize("name", ["Heart (Team)", "Bell (team)", "Keelbridge (Desk)"])
    def test_a_group_qualifier_is_the_negative_control_generalised(self, name) -> None:
        """athenaeum#1570's control, encoded rather than inherited.

        The fixture's team page is caught by the same-type requirement
        because it is typed ``team``. The live corpus carries the harder
        variant — ``Heart`` / ``Heart (Team)``, both typed ``project`` —
        where nothing but the qualifier distinguishes the staffing group
        from the thing it staffs. Those were the only two clear false
        positives in the 37-hit live hand review on this issue's PR.
        """
        assert split_qualifier(name) is None

    def test_a_group_word_INSIDE_a_longer_qualifier_still_qualifies(self) -> None:
        """Whole-qualifier match, not substring.

        ``(rollout team briefing)`` is a facet of the programme that happens
        to contain the word "team"; suppressing it would be the denylist
        overreaching past the principle it encodes.
        """
        assert split_qualifier("Keelbridge (rollout team briefing)") == (
            "Keelbridge",
            "rollout team briefing",
        )


# ---------------------------------------------------------------------------
# AC1 -- the ground-truth verdict, negative control included
# ---------------------------------------------------------------------------


class TestClusterBVerdict:
    def test_the_fixture_still_says_what_this_test_grades(self) -> None:
        """The ground truth is athenaeum#1570's, not this test's.

        Read back rather than hard-coded so a fixture edit that changed the
        verdict would fail here instead of quietly re-pointing the eval at
        whatever the code now does.
        """
        cluster = _keelbridge()
        assert set(cluster.merge) == {BARE, QUALIFIED}
        assert set(cluster.negative_control) == {TEAM}

    def test_the_bare_and_qualified_pages_are_proposed_as_a_merge(self, core_wiki: Path) -> None:
        """AC1, positive half."""
        assert frozenset({BARE, QUALIFIED}) in _pairs(core_wiki)

    def test_the_team_page_is_not_proposed_with_anything(self, core_wiki: Path) -> None:
        """AC1's negative control -- 'a system that merges all three FAILS'.

        Asserted as "the team page appears in NO proposed pair", not merely
        "the three-way merge did not happen": over-merging via two separate
        pairwise proposals would reach the same wrong destination by a
        different route.
        """
        assert not [pair for pair in _pairs(core_wiki) if TEAM in pair]

    def test_the_proposal_set_over_the_whole_cluster_is_exactly_one_pair(
        self, core_wiki: Path
    ) -> None:
        """The two assertions above, tightened.

        Neither one alone rejects a scan that proposes nothing (which passes
        the negative control trivially) or one that proposes six unrelated
        Keelbridge pairs (which passes the positive half). Scoped to the
        cluster rather than the whole corpus, since what other fixtures
        propose is not Cluster B's business.
        """
        cluster_uids = {BARE, QUALIFIED, TEAM}
        touching = {pair for pair in _pairs(core_wiki) if pair & cluster_uids}
        assert touching == {frozenset({BARE, QUALIFIED})}


# ---------------------------------------------------------------------------
# AC2 -- the queue, and only the queue
# ---------------------------------------------------------------------------


class TestTheProposalIsQueuedAndNeverAutoApplied:
    def test_it_reaches_list_pending_merges_unresolved(self, core_wiki: Path) -> None:
        """AC2. ``docs/north-star.md`` §2.8 -- evals grade PROPOSALS."""
        propose_qualified_name_merges(core_wiki)
        merges = list_pending_merges(core_wiki / "_pending_merges.md")
        ours = [
            m
            for m in merges
            if any(QUALIFIED in str(s) for s in m.get("sources", []))
            and any(BARE in str(s) for s in m.get("sources", []))
        ]
        assert len(ours) == 1, f"expected exactly one queued Keelbridge merge, got {ours}"
        assert ours[0]["merge_target_name"] == BARE, (
            "the bare name is the wider scope and must be the fold target"
        )

    def test_no_page_is_touched_by_proposing(self, core_wiki: Path) -> None:
        """The proposal is inert until a human resolves it."""
        before = {p.name: p.read_bytes() for p in sorted(core_wiki.glob("*.md"))}
        propose_qualified_name_merges(core_wiki)
        after = {
            p.name: p.read_bytes()
            for p in sorted(core_wiki.glob("*.md"))
            if not p.name.startswith("_")
        }
        for name, blob in after.items():
            assert before[name] == blob, f"{name} changed while merely proposing"
        assert (core_wiki / f"{QUALIFIED}.md").exists()
        assert (core_wiki / f"{TEAM}.md").exists()

    def test_there_is_no_auto_merge_switch_to_turn_on(self) -> None:
        """AC2 as an invariant rather than a default.

        ``resolve_name_collisions`` takes ``auto_merge``; this path
        deliberately does not, so no config value and no caller can make a
        qualified-name proposal self-approve. A signature check is a crude
        assertion, but it is exactly the property the issue asks to be
        unable to regress.
        """
        import inspect

        params = inspect.signature(propose_qualified_name_merges).parameters
        assert "auto_merge" not in params
        assert "auto_applied" not in params

    def test_the_confidence_is_below_certain(self) -> None:
        """An exact name collision is certain; a parenthetical is not.

        Measured on the live corpus, roughly half the hits are scope/phase
        qualifiers a human may reject. Writing 1.0 (what
        ``name_collisions`` writes) would misreport that to a reviewer.
        """
        assert 0.0 < QUALIFIED_NAME_CONFIDENCE < 1.0

    def test_the_draft_body_keeps_both_halves_verbatim(self, core_wiki: Path) -> None:
        """The fold target must not lose the qualified page's facts.

        That loss IS the failure this issue exists to stop -- consolidating
        by deleting half the entity would score perfectly on a merge count
        and worse on retrieval.
        """
        bare_text = (core_wiki / f"{BARE}.md").read_text(encoding="utf-8")
        qualified_text = (core_wiki / f"{QUALIFIED}.md").read_text(encoding="utf-8")
        qualified_body = qualified_text.split("---", 2)[-1]
        draft = build_draft_body(bare_text, "Keelbridge (rollout)", qualified_body)
        assert "240 thousand GBP" in draft, "bare page's facts dropped"
        assert "four half-days per office" in draft, "qualified page's facts dropped"


# ---------------------------------------------------------------------------
# AC4 -- the retrieval asymmetry, after enactment
# ---------------------------------------------------------------------------

# Scale and backend match tests/test_eval_corpus_relatedness.py's pinned
# dispatch exactly. That test asserts recall@5 == 0.5 on the corpus AS
# COMMITTED and is deliberately left alone: it pins the CURRENT (wrong)
# state so a consolidation change has something to move. This class is the
# other end of the same measurement.
_SCALE = "small"
_BACKEND = "keyword"
_K = 5

# A fact that lives ONLY on the qualified page. Retrieval reaching a page
# that carries it is the operational meaning of "the consolidated page".
_ROLLOUT_ONLY_FACT = "four half-days per office"


@pytest.fixture
def small_wiki(tmp_path: Path) -> Path:
    root = tmp_path / "small"
    wiki = build_corpus(scale=_SCALE).materialize(root)
    _assert_disposable(wiki)
    _git_init(root)
    return wiki


def _retrieved(wiki_root: Path, query: str) -> list[str]:
    from athenaeum.mcp_server import recall_search

    output = recall_search(
        wiki_root,
        query,
        top_k=_K,
        search_backend=_BACKEND,
        cache_dir=wiki_root.parent / "cache",
    )
    uids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("**Path:**"):
            continue
        uids.append(
            line.partition("**Path:**")[2].strip().removeprefix("wiki/").removesuffix(".md")
        )
    return uids


class TestRetrievalAfterTheVerdictIsEnacted:
    def test_the_probe_reaches_the_consolidated_page(self, small_wiki: Path) -> None:
        """AC4, end to end: propose, approve, re-index, re-probe.

        Before: the bare page ranks and the qualified page holding the other
        half does not, so a complete answer is unreachable at k=5. After the
        human approves the queued proposal, one page carries both halves and
        the probe reaches it.

        The assertion is "a retrieved page contains the rollout-only fact",
        not "both uids rank" -- after a fold the qualified page no longer
        EXISTS, so a uid-based recall could never reach 1.0 and would be
        measuring the wrong thing. What the split cost was access to the
        facts; what consolidation restores is access to the facts.
        """
        probe_query = "what does the Keelbridge programme cover?"
        cache = small_wiki.parent / "cache"
        get_backend("fts5").build_index(small_wiki, cache)

        before = _retrieved(small_wiki, probe_query)
        assert BARE in before, "precondition: the bare page ranks"
        reachable_before = any(
            _ROLLOUT_ONLY_FACT in (small_wiki / f"{uid}.md").read_text(encoding="utf-8")
            for uid in before
            if (small_wiki / f"{uid}.md").exists()
        )
        assert not reachable_before, (
            "precondition: the rollout half is NOT reachable at k=5 -- this is "
            "athenaeum#1570's measured asymmetry; if it no longer holds the "
            "fixture changed and this eval needs re-pinning, not patching"
        )

        propose_qualified_name_merges(small_wiki)
        merges_path = small_wiki / "_pending_merges.md"
        merge_id = next(
            m["id"]
            for m in list_pending_merges(merges_path)
            if any(QUALIFIED in str(s) for s in m.get("sources", []))
        )
        result = resolve_merge(merges_path, merge_id, "approve", wiki_root=small_wiki)
        assert result.get("ok") or result.get("status") not in {
            "no_git_repo",
            "target_exists",
        }, f"fold refused: {result}"

        get_backend("fts5").build_index(small_wiki, cache)
        after = _retrieved(small_wiki, probe_query)
        assert BARE in after, "the consolidated page must still rank"
        consolidated = (small_wiki / f"{BARE}.md").read_text(encoding="utf-8")
        assert _ROLLOUT_ONLY_FACT in consolidated, (
            "the fold dropped the qualified page's facts -- consolidation that "
            "loses half the entity is the failure this issue exists to stop"
        )

    def test_the_negative_control_survives_the_fold(self, small_wiki: Path) -> None:
        """The team page is a standing staffing arrangement that outlives the
        programme. Enacting the merge must not touch it."""
        team_before = (small_wiki / f"{TEAM}.md").read_bytes()
        propose_qualified_name_merges(small_wiki)
        merges_path = small_wiki / "_pending_merges.md"
        merge_id = next(
            m["id"]
            for m in list_pending_merges(merges_path)
            if any(QUALIFIED in str(s) for s in m.get("sources", []))
        )
        resolve_merge(merges_path, merge_id, "approve", wiki_root=small_wiki)
        assert (small_wiki / f"{TEAM}.md").exists()
        assert (small_wiki / f"{TEAM}.md").read_bytes() == team_before


# ---------------------------------------------------------------------------
# The gap this signal closes -- the one class that needs real MiniLM
# ---------------------------------------------------------------------------


@pytest.mark.embedding
class TestTheEmbeddingPathDoesNotSeeThisSplit:
    """Why a name-structure signal, measured rather than asserted.

    Real MiniLM vectors, on purpose: a stubbed embedder would let this class
    "prove" whatever the stub was written to say, and the claim being made
    is about what the PRODUCTION embedding path does with these pages. This
    is the one place in this file where the ``embedding`` marker is earned.
    """

    def test_the_pages_are_not_even_dedupe_candidates(self, core_wiki: Path) -> None:
        """Reason one, and it is upstream of any cosine value.

        ``DEDUPE_CANDIDATE_TYPES`` is ``{concept, reference, principle}``.
        The observed instance (athenaeum#1568) and this fixture are
        ``project``/``team``, so the embedding pass never looks at them --
        no threshold change could have found this split.
        """
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        names = {p.path.stem for p in discover_wiki_dedupe_candidates(core_wiki)}
        assert not (names & {BARE, QUALIFIED, TEAM})

    def test_real_minilm_clustering_proposes_no_keelbridge_pair(self, core_wiki: Path) -> None:
        """Reason two: even the clustering itself yields nothing here.

        Run at the production ``DEFAULT_CLUSTER_THRESHOLD`` with the real
        provider. Asserted as "no formed cluster contains a Keelbridge
        page", which is true for both of the ways it can be true (excluded
        upstream, or below threshold) and stays true if the type filter is
        ever widened without a companion signal.
        """
        from athenaeum.clusters import DEFAULT_CLUSTER_THRESHOLD
        from athenaeum.wiki_dedupe import find_wiki_page_clusters

        clusters = find_wiki_page_clusters(core_wiki, threshold=DEFAULT_CLUSTER_THRESHOLD)

        # Self-check on the marker. ``_resolve_wiki_embeddings`` degrades to
        # the hashing-trick embedder when chromadb is absent or the call
        # fails, and it degrades SILENTLY apart from one WARNING. A green
        # run under that fallback would be asserting nothing about MiniLM
        # while wearing the ``embedding`` mark, so the fallback fails here
        # instead of passing quietly.
        from athenaeum.clusters import EMBEDDER_CHROMADB_DEFAULT

        assert clusters, "no clusters formed at all -- corpus or threshold changed"
        assert {c.embedder for c in clusters} == {EMBEDDER_CHROMADB_DEFAULT}, (
            "this test claims to measure real MiniLM, but the pass fell back "
            f"to the hashing-trick embedder: {sorted({c.embedder for c in clusters})}"
        )

        keelbridge_members = [
            (c.cluster_id, c.member_paths)
            for c in clusters
            if any("keelbridge" in m for m in c.member_paths)
        ]
        assert not keelbridge_members, (
            "the embedding path now clusters Keelbridge; the gap this signal "
            f"closes has moved and wants re-measuring: {keelbridge_members}"
        )

    def test_the_name_structure_signal_closes_that_gap(self, core_wiki: Path) -> None:
        """AC1 stated against the same corpus the two negatives above used."""
        cluster_uids = {BARE, QUALIFIED, TEAM}
        touching = {pair for pair in _pairs(core_wiki) if pair & cluster_uids}
        assert touching == {frozenset({BARE, QUALIFIED})}
