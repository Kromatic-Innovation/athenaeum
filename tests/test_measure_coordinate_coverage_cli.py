# SPDX-License-Identifier: Apache-2.0
"""``athenaeum measure coordinate-coverage`` CLI (issue athenaeum#1944)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _page(root: Path, name: str, frontmatter: str, body: str = "Body.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


class TestCoordinateCoverageCli:
    def test_json_matches_hand_computed_counts_and_leaks_nothing(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _page(
            wiki,
            "c1.md",
            "uid: 'c1'\ntype: concept\nname: Secret Name One\nsubject: subject-000001",
        )
        _page(wiki, "c2.md", "uid: 'c2'\ntype: concept\nname: Secret Name Two")

        rc, out = _run(["measure", "coordinate-coverage", "--path", str(knowledge), "--json"])
        assert rc == 0
        payload = json.loads(out)
        assert payload["by_type"]["concept"]["pages"] == 2
        assert payload["by_type"]["concept"]["subject_real"] == 1
        assert payload["by_type"]["concept"]["subject_absent"] == 1
        assert payload["all"]["pages"] == 2
        assert "pair_relation_counts" not in payload
        assert "cluster_relation_counts" not in payload

        assert "Secret Name One" not in out
        assert "Secret Name Two" not in out
        assert "c1" not in out
        assert "c2" not in out

    def test_pairs_from_report_adds_relation_distribution(self, tmp_path: Path) -> None:
        from athenaeum.subject_population import PageDecision, decision_to_row

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        a = _page(wiki, "a.md", "uid: 'u1'\ntype: concept\nname: A\nsubject: subject-A")
        b = _page(wiki, "b.md", "uid: 'u2'\ntype: concept\nname: B\nsubject: subject-A")

        report_path = tmp_path / "report.jsonl"
        decision = PageDecision(
            uid="u1",
            name="A",
            type="concept",
            path=a,
            subject="subject-A",
            reason="matched",
            top_k_uids=("u2",),
        )
        report_path.write_text(json.dumps(decision_to_row(decision)) + "\n", encoding="utf-8")
        assert b.exists()

        rc, out = _run(
            [
                "measure",
                "coordinate-coverage",
                "--path",
                str(knowledge),
                "--pairs-from-report",
                str(report_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["pair_relation_counts"]["equal"] == 1

    def test_clusters_flag_with_no_path_uses_newest_rotation(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        auto_memory = knowledge / "raw" / "auto-memory"
        auto_memory.mkdir(parents=True)
        (auto_memory / "m1.md").write_text(
            "---\ntype: auto-memory\nname: M1\nsubject: subject-A\n---\nBody.\n",
            encoding="utf-8",
        )
        (auto_memory / "m2.md").write_text(
            "---\ntype: auto-memory\nname: M2\nsubject: subject-A\n---\nBody.\n",
            encoding="utf-8",
        )
        clusters_path = knowledge / "raw" / "_librarian-clusters-20260101T000000Z.jsonl"
        clusters_path.write_text(
            json.dumps({"cluster_id": "c1", "member_paths": ["m1.md", "m2.md"]}) + "\n",
            encoding="utf-8",
        )

        rc, out = _run(
            ["measure", "coordinate-coverage", "--path", str(knowledge), "--clusters", "--json"]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["cluster_relation_counts"]["equal"] == 1

    def test_clusters_flag_absent_omits_cluster_counts(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)

        rc, out = _run(["measure", "coordinate-coverage", "--path", str(knowledge), "--json"])
        assert rc == 0
        payload = json.loads(out)
        assert "cluster_relation_counts" not in payload

    def test_text_output_has_no_page_names_or_uids(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _page(wiki, "c1.md", "uid: 'c1'\ntype: concept\nname: Totally Secret Name")

        rc, out = _run(["measure", "coordinate-coverage", "--path", str(knowledge)])
        assert rc == 0
        assert "Totally Secret Name" not in out
        assert "c1" not in out
