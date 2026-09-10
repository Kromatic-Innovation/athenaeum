# SPDX-License-Identifier: Apache-2.0
"""Relatedness and redundancy in the eval corpus (issue athenaeum#1570).

Before this issue every page in the synthetic corpus was an island and no two
pages described the same thing, so the suite could not measure either of the
two failures the operator actually observes on the live wiki: pages that ARE
related but carry no edges, and pages that are near-duplicates and should be
consolidated. Both were invisible in BOTH directions -- the suite could not
tell a librarian that writes edges from one that does not.

**Offline and free.** These run in the default suite: no Anthropic call, no
API key, no ``embedding`` mark, no network. The only machinery involved is
``recall_search`` over a materialized temp-directory corpus, which is the same
thing ``tests/test_eval_recall_floor.py`` does.

**Out of scope, deliberately.** The consolidation VERDICT on Cluster B --
whether a resolver actually proposes the merge -- is athenaeum#1577. This
module covers the corpus and the measures only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from athenaeum.mcp_server import recall_search
from athenaeum.search import get_backend
from tests.evals import relatedness as R
from tests.evals.corpus import (
    LINK_ROLE,
    Page,
    RelatedEdge,
    build_corpus,
    load_core_pages,
    load_probes,
    load_redundant_clusters,
    load_unlinked_clusters,
    validate_core,
)
from tests.evals.metrics import recall_at_k


def _page(uid: str) -> Page:
    for page in load_core_pages():
        if page.uid == uid:
            return page
    raise AssertionError(f"core corpus has no page {uid!r}")


def _frontmatter(page: Page) -> dict:
    text = page.to_markdown()
    return yaml.safe_load(text[3 : text.find("\n---", 3)])


# ---------------------------------------------------------------------------
# AC1 -- `related:` is expressible AND rendered, and `links` cannot diverge
# ---------------------------------------------------------------------------


class TestRelatedIsRenderedInTheLiveShape:
    def test_related_renders_as_uid_role_mappings(self) -> None:
        """The live shape is ``WikiEntity.related: list[dict[str, str]]``
        (``models.py:1587``). A fixture that renders anything else is not a
        stand-in for a real wiki, and the breadcrumb reader
        (``viewer_corpus._related_uids``) would not see it."""
        page = _page("client-alderway")
        related = _frontmatter(page)["related"]
        assert isinstance(related, list) and related, "authored edges must render"
        for entry in related:
            assert isinstance(entry, dict), f"expected a mapping, got {entry!r}"
            assert set(entry) == {"uid", "role"}
            assert isinstance(entry["uid"], str) and isinstance(entry["role"], str)

    def test_edges_were_authored_but_unrendered_before_this_issue(self) -> None:
        """The defect athenaeum#1570 names, pinned from the other side: the
        ground truth WAS authored (``links:`` in ``core/01-clients.yaml``) and
        ``to_markdown`` never emitted it. If a future refactor stops rendering
        edges, this fails rather than quietly restoring the old state."""
        rendered = _page("client-alderway").to_markdown()
        assert "related:" in rendered
        assert "person-priya-shah" in rendered

    def test_links_is_a_view_over_related_and_cannot_diverge(self) -> None:
        """Constraint from AC1: ``links`` and ``related`` must not silently
        diverge. They cannot -- ``links`` is a read-only projection of
        ``related``, not a second stored field, so there is no state for the
        two to disagree about. Pinned because the obvious "fix" for a future
        bug is to reintroduce the field."""
        assert isinstance(Page.links, property)
        page = _page("client-alderway")
        assert page.links == ("person-priya-shah", "project-portal-refresh")
        assert set(page.links) <= {edge.uid for edge in page.related}
        for uid in page.links:
            assert any(e.uid == uid and e.role == LINK_ROLE for e in page.related)

    def test_every_link_target_appears_in_the_rendered_related_block(self) -> None:
        """The whole-corpus version of the assertion above: no page may declare
        a body link its rendered ``related:`` block does not carry."""
        for page in load_core_pages():
            if not page.links:
                continue
            rendered_uids = {e["uid"] for e in _frontmatter(page)["related"]}
            missing = set(page.links) - rendered_uids
            assert not missing, f"{page.uid}: links {missing} never reached `related:`"

    def test_related_is_emitted_between_tags_and_created(self) -> None:
        """Live ``WikiEntity.render()`` puts it there (``models.py:1727``).
        Key ORDER is not semantic to YAML, but a fixture that reads differently
        from a real page is a fixture a reviewer cannot compare against one."""
        lines = _page("client-alderway").to_markdown().splitlines()
        assert lines.index("tags:") < lines.index("related:")
        assert lines.index("related:") < next(
            i for i, ln in enumerate(lines) if ln.startswith("created:")
        )

    def test_a_uid_authored_in_both_spellings_renders_once(self) -> None:
        """``related:`` and ``links:`` are two spellings of one field, so a uid
        in both must not emit a duplicate edge."""
        from tests.evals.corpus import _parse_related

        edges = _parse_related(
            {"uid": "x", "related": [{"uid": "t", "role": "models"}], "links": ["t"]},
            Path("inline.yaml"),
        )
        assert edges == (RelatedEdge(uid="t", role="models"),)


# ---------------------------------------------------------------------------
# AC2 / AC3 -- both clusters exist, with ground truth and a negative control
# ---------------------------------------------------------------------------


class TestBothClustersAreInCoreWithGroundTruth:
    def test_corpus_including_the_new_ground_truth_is_consistent(self) -> None:
        """``validate_core`` now also resolves every relatedness/redundancy uid.
        A dangling ground-truth edge would score as a MISSING edge --
        indistinguishable from a librarian that failed to write it."""
        problems = validate_core(load_core_pages(), load_probes())
        assert not problems, "corpus inconsistencies:\n  " + "\n  ".join(problems)

    def test_cluster_a_is_three_pages_at_three_altitudes(self) -> None:
        cluster = _unlinked("quiet-handover")
        assert len(cluster.members) == 3
        types = {_page(uid).type for uid in cluster.members}
        assert types == {"concept", "model", "process"}, (
            f"Cluster A must be a thing, a model OF it, and a process EMBODYING "
            f"it -- got types {sorted(types)}"
        )

    def test_cluster_a_carries_no_edges_at_all(self) -> None:
        """The fixture's entire value. A corpus that already carries the edges
        cannot distinguish a librarian that writes them from one that does
        not, so this is the assertion that keeps the measure non-vacuous."""
        cluster = _unlinked("quiet-handover")
        for uid in cluster.members:
            assert _page(uid).related == (), (
                f"{uid} carries edges; Cluster A must start unlinked or the "
                "relatedness measure can pass without anything having run"
            )

    def test_cluster_a_ground_truth_is_the_three_missing_edges(self) -> None:
        cluster = _unlinked("quiet-handover")
        assert {(s, t) for s, t, _ in cluster.expected_edges} == {
            ("model-handover-ladder", "concept-quiet-handover"),
            ("process-shadow-fortnight", "concept-quiet-handover"),
            ("process-shadow-fortnight", "model-handover-ladder"),
        }

    def test_cluster_b_is_the_entity_split_signature(self) -> None:
        """A bare name, the same name with a parenthetical qualifier, and an
        associated team page."""
        cluster = _redundant("keelbridge")
        bare, qualified = (_page(uid) for uid in cluster.merge)
        assert bare.name == "Keelbridge"
        assert qualified.name.startswith("Keelbridge (") and qualified.name.endswith(")")
        (control,) = cluster.negative_control
        assert _page(control).type == "team"

    def test_cluster_b_negative_control_is_explicit_and_not_merged(self) -> None:
        """A and B want DIFFERENT verdicts, and B's own verdict is split: two
        of three pages merge, the team page does not. Without the control,
        "merge everything sharing this name" would score perfectly -- the
        redundancy analogue of linking everything."""
        cluster = _redundant("keelbridge")
        assert cluster.negative_control, "a redundancy cluster needs a control"
        assert not set(cluster.merge) & set(cluster.negative_control)
        assert len(cluster.merge) == 2 and len(cluster.negative_control) == 1

    def test_the_two_clusters_are_disjoint(self) -> None:
        """A fixture that conflates them would license a change that papers
        over B while appearing to fix A."""
        a = {uid for c in load_unlinked_clusters() for uid in c.members}
        b = {uid for c in load_redundant_clusters() for uid in (*c.merge, *c.negative_control)}
        assert not a & b


# ---------------------------------------------------------------------------
# AC4 + AC5 -- the relatedness measure, pinned in BOTH directions
# ---------------------------------------------------------------------------


class TestRelatednessMeasureFailsBothWays:
    """AC5, the anti-vacuity pin.

    A measure that only rewards more edges is the exact failure this eval
    exists to prevent, and it is the criterion most easily faked: a measure
    counting only ground-truth hits is maximised by linking every page to
    every other page. The viewer's one-hop caution
    (``_cmd_viewer.py:577-580``) is the reason that is a real failure and not
    merely inelegant -- breadcrumb colour stops carrying information when
    everything is a breadcrumb.

    Assertions here are ORDERING plus a gap, never a tuned constant: a
    threshold reverse-engineered from the adversary is not a measure.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def pages() -> list[Page]:
        return list(build_corpus(scale="core").pages)

    def test_committed_corpus_fails_because_the_edges_are_missing(self, pages: list[Page]) -> None:
        """The corpus as shipped is in the failing state ON PURPOSE. This is
        what the librarian relatedness writer has to move."""
        for cluster in load_unlinked_clusters():
            score = R.score_cluster(pages, cluster)
            assert score.found == 0 and score.f1 == 0.0, score.summary()

    def test_direction_1_linked_nothing_fails(self, pages: list[Page]) -> None:
        for cluster in load_unlinked_clusters():
            score = R.score_cluster(R.link_nothing(pages), cluster)
            assert score.recall == 0.0, score.summary()
            assert score.f1 == 0.0, score.summary()

    def test_direction_2_linked_everything_fails(self, pages: list[Page]) -> None:
        """Recall is PERFECT here and the measure must still fail it. That is
        the whole point: spurious edges are counted against the whole corpus,
        so precision collapses as ~1/N rather than bottoming out at 0.5 the
        way a cluster-scoped count would."""
        for cluster in load_unlinked_clusters():
            score = R.score_cluster(R.link_everything(pages), cluster)
            assert score.recall == 1.0, "adversary should find every edge"
            assert score.spurious, "adversary should be penalised for the rest"
            assert score.f1 < 0.1, score.summary()

    def test_the_correct_corpus_passes(self, pages: list[Page]) -> None:
        """The positive control. Without it the two directions prove only that
        the measure can fail, which is as useless as one that always passes."""
        clusters = load_unlinked_clusters()
        ideal = R.link_ground_truth(pages, clusters)
        for cluster in clusters:
            score = R.score_cluster(ideal, cluster)
            assert score.f1 == 1.0, score.summary()
            assert score.role_matches == score.found

    def test_the_three_corpora_are_strictly_ordered(self, pages: list[Page]) -> None:
        """The assertion that cannot be satisfied by a constant: nothing <
        everything < ideal, with everything nowhere near ideal."""
        for cluster in load_unlinked_clusters():
            nothing = R.score_cluster(R.link_nothing(pages), cluster).f1
            everything = R.score_cluster(R.link_everything(pages), cluster).f1
            ideal = R.score_cluster(R.link_ground_truth(pages, [cluster]), cluster).f1
            assert nothing < everything < ideal
            assert ideal - everything > 0.9

    def test_roles_are_reported_but_not_graded(self, pages: list[Page]) -> None:
        """Deliberate: the downstream consumers (the librarian relatedness
        writer, and athenaeum#1577) have not chosen a role vocabulary, and
        grading one this corpus invented unilaterally would make this eval a
        moving target for the issues it grades."""
        cluster = load_unlinked_clusters()[0]
        wrong_roles = [
            (source, target, "some-other-role") for source, target, _ in cluster.expected_edges
        ]
        from dataclasses import replace

        mutated = replace(cluster, expected_edges=tuple(wrong_roles))
        score = R.score_cluster(R.link_ground_truth(pages, [cluster]), mutated)
        assert score.f1 == 1.0, "role disagreement must not change the score"
        assert score.role_matches == 0, "but it must be visible in the report"


# ---------------------------------------------------------------------------
# AC4 + AC6 -- the redundancy probe and the retrieval asymmetry
# ---------------------------------------------------------------------------

# Backend and scale are pinned, not incidental. Measured this dispatch, on the
# probe query "what does the Keelbridge programme cover?":
#
#   core  keyword k=3 -> [project-keelbridge, team-keelbridge-desk, ...]
#   core  keyword k=5 -> [..., project-keelbridge-rollout]   (rank 5)
#   core  fts5    k=3 -> [project-keelbridge, project-keelbridge-rollout, ...]
#   small keyword k=3 -> [project-keelbridge, dis-..., dis-...]
#   small keyword k=5 -> [project-keelbridge, dis-, dis-, team-desk, concept-]
#   small fts5    k=5 -> [dis-, dis-, project-keelbridge, ...-rollout, ...]
#
# `small` + `keyword` is the cell where the asymmetry is unambiguous at BOTH
# k=3 and k=5 -- the distractor tier (built from this probe's own
# `distractor_terms`) supplies the retrieval pressure that makes the split
# cost something, exactly as in tests/test_eval_recall_floor.py. FTS5 at core
# scale retrieves both halves, so the asymmetry is asserted on keyword only
# rather than dressed up as backend-independent.
_ASYMMETRY_SCALE = "small"
_ASYMMETRY_BACKEND = "keyword"
_ASYMMETRY_K = 5


@pytest.fixture(scope="module")
def redundancy_wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    corpus = build_corpus(scale=_ASYMMETRY_SCALE)
    root = tmp_path_factory.mktemp("athenaeum-1570-corpus")
    wiki_root = corpus.materialize(root)
    get_backend("fts5").build_index(wiki_root, root / "cache")
    return wiki_root


def _retrieved_uids(wiki_root: Path, query: str, backend: str, k: int) -> list[str]:
    output = recall_search(
        wiki_root, query, top_k=k, search_backend=backend, cache_dir=wiki_root.parent / "cache"
    )
    uids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("**Path:**"):
            continue
        name = line.partition("**Path:**")[2].strip().removeprefix("wiki/")
        uids.append(name.removesuffix(".md"))
    return uids


class TestRedundancyProbeAndRetrievalAsymmetry:
    def test_the_probe_exists_and_names_the_split_entity(self) -> None:
        """AC4: a query that should retrieve the CONSOLIDATED entity, which
        does not exist as a page -- it is split across two of three cluster
        members."""
        cluster = _redundant("keelbridge")
        probe = _probe(cluster.probe)
        assert probe.probe_class == "redundancy"
        assert set(probe.expected_uids) == set(cluster.merge)
        assert set(probe.must_not_rank) == set(cluster.negative_control)

    def test_only_part_of_the_entity_is_retrieved(self, redundancy_wiki: Path) -> None:
        """AC6. The asymmetry is the measurement: a complete answer needs both
        halves, retrieval delivers one, and THAT is what makes the entity split
        cost something a number can see rather than merely being untidy.

        Asserted as strict-between-0-and-1 recall, not as "page X is missing",
        so a fix that surfaces the other half moves this test rather than
        requiring it to be rewritten."""
        cluster = _redundant("keelbridge")
        probe = _probe(cluster.probe)
        ranked = _retrieved_uids(redundancy_wiki, probe.query, _ASYMMETRY_BACKEND, _ASYMMETRY_K)
        score = recall_at_k(ranked, probe.expected_uids, _ASYMMETRY_K)
        assert 0.0 < score < 1.0, (
            f"expected PARTIAL retrieval of the split entity "
            f"{list(probe.expected_uids)}; got recall@{_ASYMMETRY_K}={score} "
            f"from {ranked}"
        )

    def test_the_retrieved_half_is_the_bare_name(self, redundancy_wiki: Path) -> None:
        """Which half wins is itself the finding: the bare page carries the
        query's vocabulary in its name, alias and tags (weighted 3x by the
        keyword scorer), so the qualified page loses even though it holds half
        the entity's facts."""
        cluster = _redundant("keelbridge")
        bare, qualified = cluster.merge
        ranked = _retrieved_uids(
            redundancy_wiki, _probe(cluster.probe).query, _ASYMMETRY_BACKEND, _ASYMMETRY_K
        )
        assert bare in ranked
        assert qualified not in ranked

    def test_the_negative_control_is_not_the_answer(self, redundancy_wiki: Path) -> None:
        """The team page shares the cluster's name and is a different entity.
        It is recorded in ``must_not_rank`` as ground truth; that the corpus
        currently surfaces it anyway is the state a consolidation change has
        to move, and is asserted here so the direction is on the record."""
        cluster = _redundant("keelbridge")
        (control,) = cluster.negative_control
        probe = _probe(cluster.probe)
        assert control in probe.must_not_rank
        assert control not in probe.expected_uids
        ranked = _retrieved_uids(redundancy_wiki, probe.query, _ASYMMETRY_BACKEND, _ASYMMETRY_K)
        assert control in ranked, (
            "the control ranking here is the CURRENT (wrong) behaviour this "
            "fixture records; if a change fixed it, update this assertion "
            "rather than deleting the control"
        )


# ---------------------------------------------------------------------------
# AC7 -- nothing here costs money
# ---------------------------------------------------------------------------


def test_this_module_makes_no_model_calls() -> None:
    """AC7 is absolute: no Anthropic call anywhere in this issue's tests, and
    no ``embedding`` mark either. Asserted structurally rather than trusted --
    a metered import creeping in is exactly the kind of change that passes
    review."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # ``anthropic`` is the SDK; ``tests.evals.harness`` is the live-API
    # harness. Importing either is how a metered call gets in here.
    assert not {m for m in imported if m.split(".")[0] == "anthropic"}, imported
    assert not {m for m in imported if m.startswith("tests.evals.harness")}, imported

    # No ``eval`` or ``embedding`` mark, module-level or per-test: either would
    # route this module into a metered job instead of the default suite.
    marks = {
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and ast.unparse(node).startswith("pytest.mark.")
    }
    assert not {m for m in marks if m.endswith((".eval", ".embedding"))}, marks
    assert "pytestmark" not in {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _unlinked(cluster_id: str):
    for cluster in load_unlinked_clusters():
        if cluster.id == cluster_id:
            return cluster
    raise AssertionError(f"no unlinked cluster {cluster_id!r}")


def _redundant(cluster_id: str):
    for cluster in load_redundant_clusters():
        if cluster.id == cluster_id:
            return cluster
    raise AssertionError(f"no redundant cluster {cluster_id!r}")


def _probe(probe_id: str):
    for probe in load_probes():
        if probe.id == probe_id:
            return probe
    raise AssertionError(f"no probe {probe_id!r}")
