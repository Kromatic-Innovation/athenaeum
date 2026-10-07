# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.coordinate_coverage` (issue athenaeum#1944)."""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.coordinate_coverage import (
    NO_TYPE_BUCKET,
    measure_coordinate_coverage,
    newest_clusters_file,
    raw_member_subject_coverage_from_clusters,
    subject_relation_counts_from_clusters,
    subject_relation_counts_from_report,
)
from athenaeum.dimensions import Relation
from athenaeum.subject_population import PageDecision, decision_to_row


def _page(root: Path, name: str, frontmatter: str, body: str = "Body.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


class TestMeasureCoordinateCoverage:
    def test_reproduces_hand_computed_per_type_counts(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"

        # concept: three pages -- real subject, undeterminable, absent.
        _page(
            wiki,
            "c1.md",
            "uid: 'c1'\ntype: concept\nname: C1\nsubject: subject-000001",
        )
        _page(
            wiki,
            "c2.md",
            "uid: 'c2'\ntype: concept\nname: C2\nsubject: undeterminable",
        )
        _page(wiki, "c3.md", "uid: 'c3'\ntype: concept\nname: C3")

        # person: one page with claimed_scope/valid_from present.
        _page(
            wiki,
            "p1.md",
            "uid: 'p1'\ntype: person\nname: P1\n"
            "claimed_scope: kromatic\nvalid_from: '2026-01-01'",
        )

        # Frontmatter present, but no type: key -- NO_TYPE_BUCKET, not
        # folded into no_frontmatter.
        _page(wiki, "untyped.md", "uid: 'u1'\nname: Untyped")

        # No frontmatter at all -- a DIFFERENT count (no_frontmatter), not
        # NO_TYPE_BUCKET.
        (wiki / "plain.md").write_text("Just a plain markdown file.\n", encoding="utf-8")

        # `_`-prefixed file: must be skipped entirely.
        _page(wiki, "_hidden.md", "uid: 'h1'\ntype: concept\nname: Hidden")

        # excluded/ subdirectory: never entered.
        _page(wiki, "excluded/e1.md", "uid: 'e1'\ntype: concept\nname: Excluded")

        report = measure_coordinate_coverage(wiki)

        concept = report.by_type["concept"]
        assert concept.pages == 3
        assert concept.subject_real == 1
        assert concept.subject_undeterminable == 1
        assert concept.subject_absent == 1

        person = report.by_type["person"]
        assert person.pages == 1
        assert person.claimed_scope == 1
        assert person.valid_from == 1
        assert person.valid_until == 0

        assert report.by_type[NO_TYPE_BUCKET].pages == 1
        assert report.no_frontmatter == 1

        # Hidden + excluded pages never contributed to any bucket.
        total_pages = sum(cov.pages for cov in report.by_type.values())
        assert total_pages == 5  # 3 concept + 1 person + 1 (no type)

        totals = report.all_pages_total()
        assert totals.pages == 5
        assert totals.subject_real == 1
        assert totals.subject_undeterminable == 1

    def test_json_output_has_no_page_names_or_uids(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _page(wiki, "c1.md", "uid: 'c1'\ntype: concept\nname: Alpha Secret Name")

        report = measure_coordinate_coverage(wiki)
        payload = json.dumps(report.to_dict())
        assert "c1" not in payload
        assert "Alpha Secret Name" not in payload

    def test_empty_wiki_root_returns_empty_report(self, tmp_path: Path) -> None:
        report = measure_coordinate_coverage(tmp_path / "does-not-exist")
        assert report.by_type == {}
        assert report.no_frontmatter == 0


class TestSubjectRelationCountsFromReport:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        _page(wiki, "a.md", "uid: 'u1'\ntype: concept\nname: Alpha\nsubject: subject-A")
        _page(wiki, "b.md", "uid: 'u2'\ntype: concept\nname: Beta\nsubject: subject-A")
        _page(
            wiki, "c.md", "uid: 'u3'\ntype: concept\nname: Gamma\nsubject: undeterminable"
        )
        _page(wiki, "d.md", "uid: 'u4'\ntype: concept\nname: Delta\nsubject: subject-D")
        _page(wiki, "e.md", "uid: 'u5'\ntype: concept\nname: Epsilon")
        _page(wiki, "f.md", "uid: 'u6'\ntype: concept\nname: Zeta")
        return wiki

    def _write_report(self, report_path: Path, decisions: list[PageDecision]) -> None:
        rows = [decision_to_row(d) for d in decisions]
        report_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    def test_hand_computed_equal_unknown_disjoint_counts(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        report_path = tmp_path / "report.jsonl"

        decisions = [
            # u1 (subject-A) vs u2 (subject-A) -> EQUAL.
            PageDecision(
                uid="u1",
                name="Alpha",
                type="concept",
                path=wiki / "a.md",
                subject="subject-A",
                reason="matched",
                top_k_uids=("u2",),
            ),
            # u3 (undeterminable) vs u4 (subject-D) -> UNKNOWN (never a
            # real identity match or disjoint, per athenaeum#1944's
            # coordinate-reader fix).
            PageDecision(
                uid="u3",
                name="Gamma",
                type="concept",
                path=wiki / "c.md",
                subject="undeterminable",
                reason="undeterminable-degraded",
                top_k_uids=("u4",),
            ),
            # u5 (absent) vs u6 (absent) -> UNKNOWN (both-null, null_means
            # unknown for subject).
            PageDecision(
                uid="u5",
                name="Epsilon",
                type="concept",
                path=wiki / "e.md",
                subject="subject-E",
                reason="minted",
                top_k_uids=("u6",),
            ),
        ]
        self._write_report(report_path, decisions)

        counts = subject_relation_counts_from_report(wiki, report_path)
        assert counts == {Relation.EQUAL: 1, Relation.UNKNOWN: 2, Relation.DISJOINT: 0}

    def test_disjoint_is_always_zero_without_ratification(self, tmp_path: Path) -> None:
        # Two DIFFERENT real subject ids -- still UNKNOWN, never DISJOINT,
        # because gate1_separator_relations is never called with
        # subject_ratified=True here (ratification wiring is out of scope
        # for this issue).
        wiki = self._wiki(tmp_path)
        report_path = tmp_path / "report.jsonl"
        decisions = [
            PageDecision(
                uid="u1",
                name="Alpha",
                type="concept",
                path=wiki / "a.md",
                subject="subject-A",
                reason="matched",
                top_k_uids=("u4",),  # u4 carries subject-D, a DIFFERENT real id
            ),
        ]
        self._write_report(report_path, decisions)
        counts = subject_relation_counts_from_report(wiki, report_path)
        assert counts[Relation.DISJOINT] == 0
        assert counts[Relation.UNKNOWN] == 1

    def test_unresolvable_uid_is_silently_skipped(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        report_path = tmp_path / "report.jsonl"
        decisions = [
            PageDecision(
                uid="u1",
                name="Alpha",
                type="concept",
                path=wiki / "a.md",
                subject="subject-A",
                reason="matched",
                top_k_uids=("does-not-exist",),
            ),
        ]
        self._write_report(report_path, decisions)
        counts = subject_relation_counts_from_report(wiki, report_path)
        assert counts == {Relation.EQUAL: 0, Relation.UNKNOWN: 0, Relation.DISJOINT: 0}


class TestSubjectRelationCountsFromClusters:
    def _write_cluster_member(
        self, knowledge_root: Path, relpath: str, frontmatter: str
    ) -> None:
        path = knowledge_root / "raw" / "auto-memory" / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\nBody.\n", encoding="utf-8")

    def test_hand_computed_counts_over_one_cluster(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_cluster_member(
            knowledge, "m1.md", "type: auto-memory\nname: M1\nsubject: subject-A"
        )
        self._write_cluster_member(
            knowledge, "m2.md", "type: auto-memory\nname: M2\nsubject: subject-A"
        )
        self._write_cluster_member(knowledge, "m3.md", "type: auto-memory\nname: M3")

        clusters_path = knowledge / "raw" / "_librarian-clusters-20260101T000000Z.jsonl"
        clusters_path.parent.mkdir(parents=True, exist_ok=True)
        clusters_path.write_text(
            json.dumps(
                {
                    "cluster_id": "test-1",
                    "member_paths": ["m1.md", "m2.md", "m3.md"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        counts = subject_relation_counts_from_clusters(knowledge, clusters_path)
        # Pairs: (m1,m2) EQUAL; (m1,m3) UNKNOWN; (m2,m3) UNKNOWN.
        assert counts == {Relation.EQUAL: 1, Relation.UNKNOWN: 2, Relation.DISJOINT: 0}

    def test_defaults_to_newest_rotation_by_lexicographic_timestamp(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        raw = knowledge / "raw"
        raw.mkdir(parents=True)
        (raw / "_librarian-clusters-20260101T000000Z.jsonl").write_text(
            json.dumps({"cluster_id": "old", "member_paths": []}) + "\n", encoding="utf-8"
        )
        newest = raw / "_librarian-clusters-20260601T000000Z.jsonl"
        newest.write_text(
            json.dumps({"cluster_id": "new", "member_paths": []}) + "\n", encoding="utf-8"
        )

        assert newest_clusters_file(knowledge) == newest

    def test_no_clusters_file_returns_zero_counts(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        counts = subject_relation_counts_from_clusters(knowledge)
        assert counts == {Relation.EQUAL: 0, Relation.UNKNOWN: 0, Relation.DISJOINT: 0}


class TestRawMemberSubjectCoverageFromClusters:
    """Issue athenaeum#1946 AC1: raw-member subject coverage (present /
    undeterminable / absent), same counting rule as the per-type coverage
    above, over a fixture clusters file."""

    def test_present_undeterminable_absent_split(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        raw_dir = knowledge / "raw" / "auto-memory" / "scope-a"
        raw_dir.mkdir(parents=True)
        (raw_dir / "feedback_one.md").write_text(
            "---\nname: one\ntype: feedback\n---\nbody\n", encoding="utf-8"
        )
        (raw_dir / "feedback_two.md").write_text(
            "---\nname: two\ntype: feedback\nsubject: undeterminable\n---\nbody\n",
            encoding="utf-8",
        )
        (raw_dir / "feedback_three.md").write_text(
            "---\nname: three\ntype: feedback\nsubject: subject-000001\n---\nbody\n",
            encoding="utf-8",
        )
        clusters_path = knowledge / "raw" / "_librarian-clusters-fixture.jsonl"
        clusters_path.write_text(
            json.dumps(
                {
                    "cluster_id": "c1",
                    "member_paths": [
                        "scope-a/feedback_one.md",
                        "scope-a/feedback_two.md",
                        "scope-a/feedback_three.md",
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        counts = raw_member_subject_coverage_from_clusters(knowledge, clusters_path)
        assert counts == {"present": 1, "undeterminable": 1, "absent": 1}

    def test_no_clusters_file_returns_zero_counts(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        counts = raw_member_subject_coverage_from_clusters(knowledge)
        assert counts == {"present": 0, "undeterminable": 0, "absent": 0}
