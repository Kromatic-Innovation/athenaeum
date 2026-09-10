# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the intake-attachment grader (issue athenaeum#1580).

NO ``eval`` marker — these run in ordinary CI, on every PR, with no network.
That is deliberate and it is most of this layer's real regression value: the
live half (``test_attachment_eval.py``) is deselected by default and runs only
from ``evals.yml``, so without this file a green CI would prove nothing beyond
"the module imports".

What is pinned here is the part of the layer that can be wrong SILENTLY:

* an attachment scored from an edge the athenaeum#1576 relatedness writer
  stamped (``role: term-overlap``) rather than from the routing decision —
  the confound that would make the eval pass hardest where the librarian
  failed worst;
* a page keyed by filename rather than uid, which would report every page the
  librarian renamed as a removal AND a mint;
* the AC4 grading of an irreversible act as a failure rather than a pass;
* tier attribution reading ``deterministic`` for a run that actually spent
  money at the write tier (the dated-snapshot model-id trap).

Each check has the negative control next to it, so a grader that stopped
discriminating would fail rather than quietly agree with everything.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from athenaeum.relatedness import ROLE_TERM_OVERLAP
from tests.evals.attachment import (
    attribute_tier,
    diff_wiki,
    score_case,
    snapshot_wiki,
)
from tests.evals.harness import EVAL_DATA_ROOT

CASES_PATH = EVAL_DATA_ROOT / "attachment" / "cases.yaml"


def _page(
    wiki: Path,
    *,
    uid: str,
    name: str,
    body: str,
    related: list[dict[str, str]] | None = None,
    stem: str | None = None,
    page_type: str = "project",
) -> Path:
    lines = [
        "---",
        f"uid: {uid}",
        f"type: {page_type}",
        f'name: "{name}"',
        "access: internal",
    ]
    if related:
        lines.append("related:")
        for edge in related:
            lines.append(f'  - uid: "{edge["uid"]}"')
            lines.append(f'    role: "{edge["role"]}"')
    lines.append('source_ref: "sessions/2026-01-01.md"')
    lines.append("---")
    path = wiki / f"{stem or uid}.md"
    path.write_text("\n".join(lines) + "\n\n" + body + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The relatedness-writer confound (the reason this grader exists)
# ---------------------------------------------------------------------------


class TestTermOverlapEdgesAreNotAttachment:
    def test_a_minted_page_pointing_at_the_target_is_still_a_mint(self, tmp_path: Path) -> None:
        """THE confound. A librarian that wrongly mints a second page for an
        entity that already has one produces a new page whose athenaeum#1576
        ``term-overlap`` edges point straight at the page it should have
        attached to — the two share the entity's distinctive vocabulary. That
        must score as the failure it is, not as an attachment."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Quarrowfield Uplift", body="The programme.")
        before = snapshot_wiki(wiki)

        # The wrong outcome: a SECOND page, carrying only writer-stamped edges.
        _page(
            wiki,
            uid="proj-a-dup",
            name="Quarrowfield Uplift (phase two)",
            body="Phase two.",
            related=[{"uid": "proj-a", "role": ROLE_TERM_OVERLAP}],
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.minted == {"proj-a-dup"}
        assert delta.touched_uids == frozenset()
        assert not delta.attachment_edges, "a term-overlap edge is not an attachment"
        assert delta.incidental_edges == {"proj-a-dup": frozenset({("proj-a", ROLE_TERM_OVERLAP)})}

        case = {
            "expected": {
                "max_new_pages": 0,
                "touch_or_proposal_uids": ["proj-a"],
                "must_not_mint_name_substrings": ["Quarrowfield"],
            }
        }
        passed, detail = score_case(case, delta)
        assert not passed
        assert "Quarrowfield" in detail

    def test_positive_control_a_real_attachment_scores(self, tmp_path: Path) -> None:
        """The same grader must PASS when the source actually landed on the
        existing page — otherwise the check above is satisfied by a grader
        that just fails everything."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Quarrowfield Uplift", body="The programme.")
        before = snapshot_wiki(wiki)

        _page(
            wiki,
            uid="proj-a",
            name="Quarrowfield Uplift",
            body="The programme.\n\nThe tablets phase slipped a fortnight.",
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.minted == frozenset()
        assert delta.touched_uids == {"proj-a"}

        case = {
            "expected": {
                "max_new_pages": 0,
                "touch_or_proposal_uids": ["proj-a"],
                "must_not_mint_name_substrings": ["Quarrowfield"],
            }
        }
        passed, detail = score_case(case, delta)
        assert passed, detail

    def test_a_non_term_overlap_edge_gained_by_an_existing_page_is_attachment(
        self, tmp_path: Path
    ) -> None:
        """The other side of the filter: an edge with any role BUT
        ``term-overlap`` is the routing decision's own work and must count."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Quarrowfield Uplift", body="Body.")
        _page(wiki, uid="co-b", name="Steepgate Ceramics", body="Body.", page_type="company")
        before = snapshot_wiki(wiki)

        _page(
            wiki,
            uid="proj-a",
            name="Quarrowfield Uplift",
            body="Body.",
            related=[{"uid": "co-b", "role": "evidenced-by"}],
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.attachment_edges == {"proj-a": frozenset({("co-b", "evidenced-by")})}
        assert "proj-a" in delta.touched_uids


# ---------------------------------------------------------------------------
# Page identity
# ---------------------------------------------------------------------------


def test_pages_are_keyed_by_uid_not_filename(tmp_path: Path) -> None:
    """The corpus materializes ``<uid>.md``; the real librarian writes
    ``<uid>-<slug>.md``. Keyed by filename, every page the librarian rewrote
    would read as a removal AND a mint — turning an ordinary attachment into
    an AC4 contract violation."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _page(wiki, uid="proj-a", name="Quarrowfield Uplift", body="Body.", stem="proj-a")
    before = snapshot_wiki(wiki)

    (wiki / "proj-a.md").unlink()
    _page(
        wiki,
        uid="proj-a",
        name="Quarrowfield Uplift",
        body="Body.\n\nMore.",
        stem="proj-a-quarrowfield-uplift",
    )
    delta = diff_wiki(before, snapshot_wiki(wiki))

    assert delta.removed_uids == frozenset()
    assert delta.minted == frozenset()
    assert delta.touched_uids == {"proj-a"}


# ---------------------------------------------------------------------------
# athenaeum#1598 — proposal scoping is per-uid, not a whole-run boolean
# ---------------------------------------------------------------------------


class TestProposalScopingIsPerUid:
    """Pins the hole athenaeum#1598 found: ``delta.proposed`` was a single
    boolean for the whole run, so ANY proposal anywhere satisfied
    ``touch_or_proposal_uids``/``requires_proposal`` for EVERY uid in the
    list. A librarian that proposes liberally, without the proposal naming
    the right page, scored as correct.

    AC3 (the issue's own words): "a synthetic delta carrying one proposal
    about an UNRELATED uid must FAIL a case whose ``touch_or_proposal_uids``
    names a different page." Asserted directly on the grader, per AC3 --
    no live run needed to produce this shape.
    """

    def test_old_grader_shape_would_have_passed_this__new_grader_fails_it(
        self, tmp_path: Path
    ) -> None:
        """Reproduces the hole with the OLD (whole-run boolean) semantics
        inline, so the regression is visible without reverting the fix: a
        proposal naming an entity NOT in ``touch_or_proposal_uids`` used to
        satisfy the check for every uid in the list via ``delta.proposed``."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="attach-company-steepgate", name="Steepgate Ceramics", body="Body.")
        _page(wiki, uid="unrelated-project", name="Fallowdyke Freight Audit", body="Body.")
        before = snapshot_wiki(wiki)

        # A proposal about an ENTIRELY UNRELATED page -- the source never
        # reached Steepgate at all.
        (wiki / "_pending_merges.md").write_text(
            '# Pending merges\n\n## [2026-09-10] Merge: "Fallowdyke Freight Audit"\n',
            encoding="utf-8",
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        # The confound, made explicit: the OLD whole-run signal is True even
        # though the proposal is about a different page entirely.
        assert delta.proposed
        assert delta.proposed_uids == {"unrelated-project"}
        assert "attach-company-steepgate" not in delta.proposed_uids

        case = {
            "expected": {
                "max_new_pages": 0,
                "touch_or_proposal_uids": ["attach-company-steepgate"],
            }
        }
        passed, detail = score_case(case, delta)
        assert not passed, (
            "AC3 regression: an unrelated proposal satisfied a case whose "
            "touch_or_proposal_uids names a different page"
        )
        assert "attach-company-steepgate" in detail

    def test_a_proposal_naming_the_right_uid_still_passes(self, tmp_path: Path) -> None:
        """Positive control: the fix must not make a genuine proposal fail --
        otherwise Case C-style cases could never be satisfied by proposing."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="attach-company-steepgate", name="Steepgate Ceramics", body="Body.")
        before = snapshot_wiki(wiki)

        (wiki / "_pending_merges.md").write_text(
            '# Pending merges\n\n## [2026-09-10] Merge: "Steepgate Ceramics"\n',
            encoding="utf-8",
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.proposed_uids == {"attach-company-steepgate"}

        case = {
            "expected": {
                "max_new_pages": 0,
                "touch_or_proposal_uids": ["attach-company-steepgate"],
            }
        }
        passed, detail = score_case(case, delta)
        assert passed, detail

    def test_requires_proposal_without_a_uid_to_scope_against_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """AC2: ``requires_proposal`` alone (no ``touch_or_proposal_uids``)
        has nothing to scope the proposal check against, so it must fail
        rather than silently fall back to the whole-run boolean."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.")
        before = snapshot_wiki(wiki)

        (wiki / "_pending_merges.md").write_text(
            '# Pending merges\n\n## [2026-09-10] Merge: "Bracklemoor Transit Study"\n',
            encoding="utf-8",
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))
        assert delta.proposed  # the old whole-run signal is still true

        passed, detail = score_case({"expected": {"requires_proposal": True}}, delta)
        assert not passed
        assert "scope" in detail


# ---------------------------------------------------------------------------
# athenaeum#1595 — a thin type:source page is the design, not a failure
# ---------------------------------------------------------------------------


class TestSourcePageMintIsNotADuplicateEntityFailure:
    """A thin ``type: source`` page minted for a new source is CORRECT
    (operator ruling, 2026-09-10) -- the failure shapes are a second ENTITY
    page, or a source page nothing links to (orphaned)."""

    def test_a_thin_linked_source_page_passes(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(
            wiki,
            uid="attach-company-steepgate",
            name="Steepgate Ceramics",
            body="Body.",
            page_type="company",
        )
        before = snapshot_wiki(wiki)

        # A new thin source page, linked back to the entity it evidences.
        _page(
            wiki,
            uid="src-steepgate-board",
            name="Steepgate onboarding discovery board",
            body="A board export about Steepgate Ceramics.",
            page_type="source",
            related=[{"uid": "attach-company-steepgate", "role": "evidences"}],
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        case = {
            "expected": {
                "max_new_pages": 1,
                "mint_types_must_be": ["source"],
                "source_mint_link_uid": "attach-company-steepgate",
                "max_source_body_bytes": 2000,
            }
        }
        passed, detail = score_case(case, delta)
        assert passed, detail

    def test_a_duplicate_entity_page_still_fails_on_type(self, tmp_path: Path) -> None:
        """The actual observed athenaeum#1595 failure shape: a page named for
        the entity is minted, but as a duplicate ENTITY page, not a source
        page. ``mint_types_must_be`` catches this by TYPE, not by name, so it
        cannot be dodged by a name this layer's substring list didn't
        anticipate."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(
            wiki,
            uid="attach-company-steepgate",
            name="Steepgate Ceramics",
            body="Body.",
            page_type="company",
        )
        before = snapshot_wiki(wiki)

        _page(wiki, uid="dup-steepgate", name="Steepgate", body="Duplicate.", page_type="company")
        delta = diff_wiki(before, snapshot_wiki(wiki))

        case = {
            "expected": {
                "max_new_pages": 1,
                "mint_types_must_be": ["source"],
                "source_mint_link_uid": "attach-company-steepgate",
            }
        }
        passed, detail = score_case(case, delta)
        assert not passed
        assert "dup-steepgate" in detail

    def test_an_orphaned_source_page_fails_even_though_the_type_is_correct(
        self, tmp_path: Path
    ) -> None:
        """AC2's other half: the page itself is legitimate (type: source),
        but nothing links it to the entity it is supposed to evidence."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(
            wiki,
            uid="attach-company-steepgate",
            name="Steepgate Ceramics",
            body="Body.",
            page_type="company",
        )
        before = snapshot_wiki(wiki)

        # A thin source page with NO edge, source_ref, or proposal connecting
        # it back to the entity it is about.
        _page(
            wiki,
            uid="src-steepgate-board",
            name="Steepgate onboarding discovery board",
            body="A board export.",
            page_type="source",
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        case = {
            "expected": {
                "max_new_pages": 1,
                "mint_types_must_be": ["source"],
                "source_mint_link_uid": "attach-company-steepgate",
            }
        }
        passed, detail = score_case(case, delta)
        assert not passed
        assert "orphaned" in detail

    def test_an_oversize_source_page_fails_thinness(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(
            wiki,
            uid="attach-company-steepgate",
            name="Steepgate Ceramics",
            body="Body.",
            page_type="company",
        )
        before = snapshot_wiki(wiki)

        _page(
            wiki,
            uid="src-steepgate-board",
            name="Steepgate onboarding discovery board",
            body="x" * 3000,
            page_type="source",
            related=[{"uid": "attach-company-steepgate", "role": "evidences"}],
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        case = {
            "expected": {
                "max_new_pages": 1,
                "mint_types_must_be": ["source"],
                "source_mint_link_uid": "attach-company-steepgate",
                "max_source_body_bytes": 2000,
            }
        }
        passed, detail = score_case(case, delta)
        assert not passed
        assert "thinness" in detail

    def test_a_source_page_name_is_exempt_from_the_duplicate_name_check(
        self, tmp_path: Path
    ) -> None:
        """A thin source page carrying the subject's name in its own title
        (the live shape -- "Steepgate onboarding discovery board") must NOT
        trip ``must_not_mint_name_substrings``; only a non-source mint may."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(
            wiki,
            uid="attach-company-steepgate",
            name="Steepgate Ceramics",
            body="Body.",
            page_type="company",
        )
        before = snapshot_wiki(wiki)

        _page(
            wiki,
            uid="src-steepgate-board",
            name="Steepgate onboarding discovery board",
            body="A board export.",
            page_type="source",
            related=[{"uid": "attach-company-steepgate", "role": "evidences"}],
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        case = {
            "expected": {
                "max_new_pages": 1,
                "must_not_mint_name_substrings": ["Steepgate"],
            }
        }
        passed, detail = score_case(case, delta)
        assert passed, detail


# ---------------------------------------------------------------------------
# AC4 — irreversibility is a proposal, never an applied change
# ---------------------------------------------------------------------------


class TestIrreversibleOutcomesMustBeProposals:
    def test_a_vanished_page_fails_however_tidy_the_result(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.")
        _page(wiki, uid="proj-a2", name="Bracklemoor Transit Study (phase two)", body="Body.")
        before = snapshot_wiki(wiki)

        # A consolidation that APPLIED itself: the redundant page is gone.
        (wiki / "proj-a2.md").unlink()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.\n\nPhase two.")
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.removed_uids == {"proj-a2"}
        passed, detail = score_case(
            {"expected": {"max_new_pages": 0, "touch_or_proposal_uids": ["proj-a"]}}, delta
        )
        assert not passed
        assert "irreversible" in detail

    def test_a_grown_pending_queue_satisfies_the_routing_requirement(self, tmp_path: Path) -> None:
        """A proposal is a correct answer — the issue's own wording is "merge
        into the existing page (or a proposal to)". A grader that demanded an
        applied merge would push the librarian toward the very irreversibility
        AC4 forbids."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.")
        (wiki / "_pending_merges.md").write_text("# Pending merges\n", encoding="utf-8")
        before = snapshot_wiki(wiki)

        (wiki / "_pending_merges.md").write_text(
            "# Pending merges\n\n## Bracklemoor Transit Study\n", encoding="utf-8"
        )
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert delta.proposed
        assert delta.grown_queues == {"_pending_merges.md"}
        passed, detail = score_case(
            {
                "expected": {
                    "max_new_pages": 0,
                    "touch_or_proposal_uids": ["proj-a"],
                    "requires_proposal": True,
                }
            },
            delta,
        )
        assert passed, detail

    def test_requires_proposal_is_not_satisfied_by_an_applied_change(self, tmp_path: Path) -> None:
        """Negative control for the check above: editing the page instead of
        queueing a proposal must NOT satisfy ``requires_proposal``."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.")
        before = snapshot_wiki(wiki)
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.\n\nPhase two.")
        delta = diff_wiki(before, snapshot_wiki(wiki))

        passed, detail = score_case({"expected": {"requires_proposal": True}}, delta)
        assert not passed
        assert "proposal" in detail

    def test_a_type_rejected_park_is_not_a_proposal(self, tmp_path: Path) -> None:
        """Observed for real in this layer's own baseline run: the
        athenaeum#1196 type guard parks a page the librarian could not write
        and records it in the wiki root under a leading underscore. That is a
        FAILED write, not a proposal. A grader treating every growing
        ``_*.md`` as a pending-decision surface would let ``requires_proposal``
        be satisfied by the librarian giving up."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, uid="proj-a", name="Bracklemoor Transit Study", body="Body.")
        before = snapshot_wiki(wiki)

        (wiki / "_type_rejected.md").write_text("parked: booking-engine\n", encoding="utf-8")
        delta = diff_wiki(before, snapshot_wiki(wiki))

        assert not delta.proposed
        assert delta.minted == frozenset(), "a parked page is not a minted page either"
        passed, detail = score_case({"expected": {"requires_proposal": True}}, delta)
        assert not passed
        assert "proposal" in detail


# ---------------------------------------------------------------------------
# The negative control must be able to pass (case D)
# ---------------------------------------------------------------------------


def test_minting_a_genuinely_new_entity_passes_case_d_shape(tmp_path: Path) -> None:
    """Case D inverts every other case's expectation. A grader that blanket-
    asserted "nothing new appeared" would fail D by construction, and the
    layer would then license attach-everything."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _page(wiki, uid="proj-a", name="Quarrowfield Uplift", body="Body.")
    before = snapshot_wiki(wiki)
    _page(wiki, uid="proj-new", name="Fallowdyke Freight Audit", body="New work.")
    delta = diff_wiki(before, snapshot_wiki(wiki))

    passed, detail = score_case({"expected": {"min_new_pages": 1, "max_new_pages": 2}}, delta)
    assert passed, detail

    # ...and attaching it to the wrong existing entity instead must fail D.
    attach_only = diff_wiki(before, before)
    failed, why = score_case({"expected": {"min_new_pages": 1, "max_new_pages": 2}}, attach_only)
    assert not failed
    assert "min 1" in why


# ---------------------------------------------------------------------------
# AC2 — tier attribution is observed, not declared
# ---------------------------------------------------------------------------


class TestTierAttribution:
    def test_a_dated_snapshot_id_still_buckets_to_its_family(self) -> None:
        """The trap: ``athenaeum.yaml``'s ``models:`` section may pin
        ``claude-sonnet-5-20260101`` where the default constant is
        ``claude-sonnet-5``. Exact-equality bucketing would file that as
        ``other`` and report ``deterministic`` for a run that spent real money
        at the write tier — hiding exactly what AC2 exists to surface."""
        attribution = attribute_tier(
            ["claude-haiku-4-5-20251001", "claude-sonnet-5-20260101"],
            matched=1,
            escalated=0,
            classify_model="claude-haiku-4-5",
            write_model="claude-sonnet-5",
        )
        assert attribution.classify_calls == 1
        assert attribution.write_calls == 1
        assert attribution.other_calls == 0
        assert attribution.decided_by == "write_merge"

    def test_precedence_is_most_expensive_wins(self) -> None:
        """Tier 1 runs on every file, so a cheapest-wins precedence would
        report ``deterministic`` for nearly every case and make AC2 useless."""
        assert (
            attribute_tier(
                [], matched=3, escalated=0, classify_model="c", write_model="w"
            ).decided_by
            == "deterministic"
        )
        assert (
            attribute_tier(
                ["c"], matched=3, escalated=0, classify_model="c", write_model="w"
            ).decided_by
            == "classify"
        )
        assert (
            attribute_tier(
                ["c", "w"], matched=3, escalated=0, classify_model="c", write_model="w"
            ).decided_by
            == "write_merge"
        )
        assert (
            attribute_tier(
                ["c", "w"], matched=3, escalated=1, classify_model="c", write_model="w"
            ).decided_by
            == "escalation"
        )


# ---------------------------------------------------------------------------
# The golden set itself
# ---------------------------------------------------------------------------


class TestCasesFile:
    def test_all_five_outcome_classes_are_present_exactly_once(self) -> None:
        """One case per outcome class, and specifically the NEGATIVE CONTROL
        (``mint_new_entity``) present: without it, attach-everything scores
        perfectly."""
        spec = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
        classes = [c["outcome_class"] for c in spec["cases"]]
        assert sorted(classes) == [
            "attach_name_variant",
            "attach_same_name",
            "attach_source_document",
            "attach_two_entities",
            "mint_new_entity",
        ]
        assert len(set(c["id"] for c in spec["cases"])) == len(spec["cases"])

    def test_every_expected_uid_names_an_overlay_page(self) -> None:
        """A ``touch_or_proposal_uids`` entry naming a page that does not
        exist scores as a MISS — indistinguishable from a librarian that
        failed to route. A fixture error must never masquerade as a result;
        same reasoning as ``corpus.validate_core``."""
        spec = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
        known = {p["uid"] for p in spec["wiki_pages"]}
        for case in spec["cases"]:
            for uid in case["expected"].get("touch_or_proposal_uids", []) or []:
                assert uid in known, f"case {case['id']}: unknown uid {uid!r}"

    def test_seeded_manifest_agrees_with_recorded_fixtures(self) -> None:
        """AC5: the layer stays OUT of ``seeded-layers.yml`` until an
        ``evals.yml record=true`` run has actually seeded its fixtures. Listing
        it early would make ``test_seeded_manifest_layers_are_populated``
        demand fixtures that do not exist; omitting it after seeding would
        reopen the H13 silent-coverage-hole this manifest exists to close."""
        from tests.evals.harness import LAYER_ATTACHMENT, RECORDED_ROOT

        manifest = yaml.safe_load((RECORDED_ROOT / "seeded-layers.yml").read_text())
        seeded = manifest.get("seeded") or {}
        recorded_dir = RECORDED_ROOT / LAYER_ATTACHMENT
        has_fixtures = recorded_dir.is_dir() and any(recorded_dir.glob("*.json"))
        assert (LAYER_ATTACHMENT in seeded) == has_fixtures, (
            "seeded-layers.yml and tests/fixtures/recorded/attachment/ disagree: "
            "list the layer when (and only when) a record run has seeded it"
        )
