# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/annotate_shadow_parity_subject_scope.py`` (issue athenaeum#1483).

Covers the label-blind `subject`/`claimed_scope` derivation rule -- shared
stem of a cluster's member `name:` values, applied identically regardless
of `outcome_class` -- and pins the two committed sibling fixtures
(`tests/evals/data/{detector,resolver}/cases.subject-scope.yaml`) to what
the script actually produces, so a hand-edit of either sibling (as opposed
to regenerating via the script) is caught.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import yaml

from athenaeum.cluster_comparator import candidate_pairs, page_from_auto_memory_file
from athenaeum.comparator import DEFAULT_REGISTRY, gate1_separator_relations
from athenaeum.dimensions import Relation
from athenaeum.shadow_parity import load_parity_cases, materialise_members

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "annotate_shadow_parity_subject_scope.py"

_spec = importlib.util.spec_from_file_location("annotate_shadow_parity_subject_scope", _SCRIPT)
assert _spec and _spec.loader
annotate_shadow_parity_subject_scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(annotate_shadow_parity_subject_scope)

shared_stem = annotate_shadow_parity_subject_scope.shared_stem
annotate_case = annotate_shadow_parity_subject_scope.annotate_case
annotate_corpus = annotate_shadow_parity_subject_scope.annotate_corpus
CORPORA = annotate_shadow_parity_subject_scope.CORPORA


class TestSharedStem:
    def test_one_name_is_a_prefix_of_the_other(self) -> None:
        # standup-time / standup-time-updated -- the shorter name IS the stem,
        # no truncation needed.
        assert shared_stem(["standup-time", "standup-time-updated"]) == "standup-time"

    def test_prefix_ends_exactly_on_a_hyphen(self) -> None:
        assert shared_stem(["invoice-general", "invoice-acme-exception"]) == "invoice"

    def test_prefix_stops_mid_word_truncates_to_last_segment(self) -> None:
        # "portal-deploy-march" / "portal-deploy-may" share "portal-deploy-ma"
        # character-wise; the mid-word "ma" fragment must not survive.
        assert shared_stem(["portal-deploy-march", "portal-deploy-may"]) == "portal-deploy"
        # "headcount-jan" / "headcount-jun" share "headcount-j".
        assert shared_stem(["headcount-jan", "headcount-jun"]) == "headcount"

    def test_no_shared_prefix_is_empty(self) -> None:
        assert shared_stem(["client-weekly-sync", "internal-monthly"]) == ""

    def test_empty_input(self) -> None:
        assert shared_stem([]) == ""


class TestAnnotateCaseIsLabelBlind:
    def test_rule_is_a_pure_function_of_names_not_outcome_class(self) -> None:
        # The issue's own worked example: a `contradict` case and a `pass`
        # case both derive a non-empty shared stem -- the rule cannot see
        # (and does not consult) outcome_class.
        contradict_case = {
            "id": "standup_time",
            "outcome_class": "contradict",
            "members": [
                {"filename": "a.md", "body": "x", "frontmatter": {"name": "standup-time"}},
                {
                    "filename": "b.md",
                    "body": "y",
                    "frontmatter": {"name": "standup-time-updated"},
                },
            ],
        }
        pass_case = {
            "id": "invoice_cadence_refinement",
            "outcome_class": "pass",
            "members": [
                {"filename": "a.md", "body": "x", "frontmatter": {"name": "invoice-general"}},
                {
                    "filename": "b.md",
                    "body": "y",
                    "frontmatter": {"name": "invoice-acme-exception"},
                },
            ],
        }
        annotated_contradict = annotate_case(contradict_case)
        annotated_pass = annotate_case(pass_case)

        subs_contradict = [m["frontmatter"]["subject"] for m in annotated_contradict["members"]]
        subs_pass = [m["frontmatter"]["subject"] for m in annotated_pass["members"]]
        # Both cases yield an internally-EQUAL subject pair (same value on
        # both members) despite opposite outcome_class -- "encodes no label".
        assert subs_contradict[0] == subs_contradict[1] == "standup-time"
        assert subs_pass[0] == subs_pass[1] == "invoice"

    def test_no_shared_stem_keeps_each_members_own_name(self) -> None:
        case = {
            "id": "meeting_cadence_different_scenarios",
            "outcome_class": "pass",
            "members": [
                {
                    "filename": "a.md",
                    "body": "x",
                    "frontmatter": {"name": "client-weekly-sync"},
                },
                {"filename": "b.md", "body": "y", "frontmatter": {"name": "internal-monthly"}},
            ],
        }
        annotated = annotate_case(case)
        subs = [m["frontmatter"]["subject"] for m in annotated["members"]]
        assert subs == ["client-weekly-sync", "internal-monthly"]
        assert subs[0] != subs[1]

    def test_claimed_scope_mirrors_subject(self) -> None:
        case = {
            "id": "x",
            "outcome_class": "pass",
            "members": [
                {"filename": "a.md", "body": "x", "frontmatter": {"name": "pto-days"}},
                {"filename": "b.md", "body": "y", "frontmatter": {"name": "pto-restatement"}},
            ],
        }
        annotated = annotate_case(case)
        for member in annotated["members"]:
            assert member["frontmatter"]["subject"] == member["frontmatter"]["claimed_scope"]

    def test_annotate_case_does_not_mutate_its_input(self) -> None:
        case = {
            "id": "x",
            "outcome_class": "pass",
            "members": [
                {"filename": "a.md", "body": "x", "frontmatter": {"name": "pto-days"}},
            ],
        }
        original_frontmatter_keys = set(case["members"][0]["frontmatter"].keys())
        annotate_case(case)
        assert set(case["members"][0]["frontmatter"].keys()) == original_frontmatter_keys

    def test_other_frontmatter_keys_pass_through_unchanged(self) -> None:
        case = {
            "id": "x",
            "outcome_class": "pass",
            "members": [
                {
                    "filename": "a.md",
                    "body": "x",
                    "frontmatter": {
                        "name": "pto-days",
                        "type": "reference",
                        "source_type": "user-stated",
                        "source_ref": "session-2026-01-05",
                        "updated": "2026-01-05",
                    },
                },
            ],
        }
        annotated = annotate_case(case)
        fm = annotated["members"][0]["frontmatter"]
        assert fm["type"] == "reference"
        assert fm["source_type"] == "user-stated"
        assert fm["source_ref"] == "session-2026-01-05"
        assert fm["updated"] == "2026-01-05"


class TestOriginalCorporaUntouched:
    def test_neither_source_corpus_carries_subject_or_claimed_scope(self) -> None:
        # Pins the issue's own premise: the failure mode this issue exists to
        # fix. If either source corpus is ever hand-edited to carry these
        # keys, this test (not just the "sibling, not in-place" convention)
        # catches it.
        for source in CORPORA:
            text = source.read_text(encoding="utf-8")
            assert "subject:" not in text
            assert "claimed_scope:" not in text


class TestCommittedSiblingsMatchTheScript:
    def test_committed_sibling_is_reproducible_from_the_script(self) -> None:
        # AC3: the annotation rule is inspectable and the run against it is
        # reproducible -- regenerating must exactly match what is committed,
        # so a hand-edit of the sibling (bypassing the script) is caught.
        for source in CORPORA:
            dest, content = annotate_corpus(source)
            assert dest.exists(), f"sibling fixture missing, run the script: {dest}"
            assert dest.read_text(encoding="utf-8") == content

    def test_committed_siblings_carry_subject_and_claimed_scope_on_every_member(self) -> None:
        for source in CORPORA:
            dest, _ = annotate_corpus(source)
            cases = yaml.safe_load(dest.read_text(encoding="utf-8"))
            assert cases, f"{dest} parsed empty"
            for case in cases:
                for member in case["members"]:
                    fm = member["frontmatter"]
                    assert "subject" in fm
                    assert "claimed_scope" in fm

    def test_committed_siblings_have_the_same_case_ids_as_their_source(self) -> None:
        for source in CORPORA:
            dest, _ = annotate_corpus(source)
            source_cases = yaml.safe_load(source.read_text(encoding="utf-8"))
            sibling_cases = yaml.safe_load(dest.read_text(encoding="utf-8"))
            assert [c["id"] for c in sibling_cases] == [c["id"] for c in source_cases]


class TestGate1ReachabilityOnTheAnnotatedFixture:
    """Repo-only (zero LLM calls) proof that the annotated sibling actually
    unblocks Gate 2's contradiction/specialization branches, per AC5's "is
    VERDICT_CONTRADICTION now reachable at all" -- exercising
    :func:`athenaeum.comparator.gate1_separator_relations` directly on every
    pair the real ``athenaeum measure shadow-parity`` run would produce.

    This is NOT the athenaeum#1483 measurement (no Gate 2 / LLM call happens
    here) -- it proves the STRUCTURAL premise: that ``unknown_dims`` is empty
    for the annotated pairs, so a live CONFLICTING verdict from Gate 2 would
    reach :data:`athenaeum.verdicts.VERDICT_CONTRADICTION` instead of being
    intercepted into ``VERDICT_UNDERDETERMINED`` the way the 2026-09-04 run
    was. See the PR body for how this evidence bears on AC4/AC5/AC6, which
    remain unmet (no ``claude-cli`` subscription auth in this environment).
    """

    def _unknown_and_disjoint_dims(self) -> dict[str, tuple[list[str], list[str]]]:
        results: dict[str, tuple[list[str], list[str]]] = {}
        for source in CORPORA:
            dest, _ = annotate_corpus(source)
            cases = load_parity_cases(dest)
            with tempfile.TemporaryDirectory() as tmp:
                for case in cases:
                    members = materialise_members(case, Path(tmp) / case.case_id)
                    for member_a, member_b in candidate_pairs(members):
                        page_a = page_from_auto_memory_file(member_a)
                        page_b = page_from_auto_memory_file(member_b)
                        rels = gate1_separator_relations(DEFAULT_REGISTRY, page_a.meta, page_b.meta)
                        unknown = sorted(n for n, r in rels.items() if r == Relation.UNKNOWN)
                        disjoint = sorted(n for n, r in rels.items() if r == Relation.DISJOINT)
                        results[case.case_id] = (unknown, disjoint)
        return results

    def test_seventeen_of_eighteen_clusters_have_no_unknown_separator_dims(self) -> None:
        # Before this issue: EVERY pair had unknown_dims == ["subject"]
        # (subject was UNKNOWN on all 18/18 -- the issue's own premise,
        # verified independently against comparator.py/dimensions.py before
        # any code changed). After annotation: only the one cluster with
        # genuinely unrelated member names (meeting_cadence_different_scenarios)
        # keeps an unknown dim; the other 17 -- including
        # deploy_target_sequential_snapshot, which exits at Gate 1 on a
        # DISJOINT valid-time before subject/scope even matter -- clear it.
        results = self._unknown_and_disjoint_dims()
        assert len(results) == 18
        cleared = [cid for cid, (unknown, _) in results.items() if unknown == []]
        assert len(cleared) == 17

    def test_the_one_unrelated_cluster_still_reads_unknown_on_subject(self) -> None:
        # meeting_cadence_different_scenarios: "client-weekly-sync" and
        # "internal-monthly" share no stem -- correctly stays UNKNOWN, never
        # forced EQUAL just to inflate the reachable count.
        results = self._unknown_and_disjoint_dims()
        unknown, _ = results["meeting_cadence_different_scenarios"]
        assert unknown == ["subject"]

    def test_deploy_target_cluster_still_exits_at_gate1_on_valid_time(self) -> None:
        # Unaffected by this issue: disjoint valid_from/valid_until windows
        # exit at Gate 1 regardless of subject/scope annotation.
        results = self._unknown_and_disjoint_dims()
        unknown, disjoint = results["deploy_target_sequential_snapshot"]
        assert disjoint == ["valid-time"]
