"""Integration tests for the ephemeral-intake gate (issue #278, Part 1).

Acceptance:
  (i)  an ephemeral-scope OR ``ephemeral: true``-flagged intake produces NO
       ``type: auto-memory`` page (dropped at discover; secondary-guarded at
       merge so a stale cluster row can't materialize one either).
  (ii) a legitimate-knowledge note is byte-unaffected (still discovered, still
       compiled into its page).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.librarian import discover_auto_memory_files
from athenaeum.merge import AUTO_WIKI_PREFIX, merge_clusters_to_wiki

LEGIT_SCOPE = "-Users-alice-Code-projectx"
EPHEMERAL_SCOPE = (
    "-private-tmp-claude-cctest-abc123"  # matches *cctest* / *private-tmp*
)


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


@pytest.fixture
def intake_root(tmp_path: Path) -> Path:
    """A knowledge root with one legit note, one ephemeral-scope note, and one
    ``ephemeral: true``-flagged note in an otherwise-legit scope."""
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"

    legit = auto / LEGIT_SCOPE
    legit.mkdir(parents=True)
    (legit / "reference_recall_architecture.md").write_text(
        "---\n"
        "name: Recall architecture\n"
        "description: FTS5 + vector recall pipeline\n"
        "type: reference\n"
        "originSessionId: sess-legit\n"
        "---\n"
        "The recall hook surfaces wiki context via a hybrid FTS5+vector merge.\n",
        encoding="utf-8",
    )
    # Flagged note that lives in a legit scope but self-declares throwaway.
    (legit / "feedback_install_token_boilerplate.md").write_text(
        "---\n"
        "name: Install-token boilerplate\n"
        "type: feedback\n"
        "ephemeral: true\n"
        "originSessionId: sess-flag\n"
        "---\n"
        "Ran the install-token dance again this session.\n",
        encoding="utf-8",
    )

    eph = auto / EPHEMERAL_SCOPE
    eph.mkdir(parents=True)
    (eph / "project_cctest_scratch.md").write_text(
        "---\n"
        "name: cctest scratch\n"
        "type: project\n"
        "originSessionId: sess-eph\n"
        "---\n"
        "Throwaway scratch from a cctest temp dir.\n",
        encoding="utf-8",
    )

    _write_config(knowledge_root)
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    return knowledge_root


class TestDiscoverDropsEphemeral:
    def test_ephemeral_scope_and_flag_dropped_legit_kept(
        self, intake_root: Path
    ) -> None:
        files = discover_auto_memory_files(intake_root)
        names = {f.path.name for f in files}
        # Legit note kept...
        assert "reference_recall_architecture.md" in names
        # ...ephemeral-scope and flagged notes dropped.
        assert "project_cctest_scratch.md" not in names
        assert "feedback_install_token_boilerplate.md" not in names
        assert len(files) == 1

    def test_legit_record_unaffected(self, intake_root: Path) -> None:
        files = discover_auto_memory_files(intake_root)
        (legit,) = files
        assert legit.origin_scope == LEGIT_SCOPE
        assert legit.name == "Recall architecture"
        assert legit.memory_type == "reference"


class TestMergeSecondaryGuard:
    def test_stale_cluster_row_for_ephemeral_member_makes_no_page(
        self, intake_root: Path
    ) -> None:
        # A stale C2 cluster JSONL references the ephemeral member that
        # discover now drops. The secondary guard at merge must refuse to
        # materialize a page for it.
        clusters = intake_root / "raw" / "_librarian-clusters.jsonl"
        clusters.write_text(
            json.dumps(
                {
                    "cluster_id": "eph-0001",
                    "member_paths": [f"{EPHEMERAL_SCOPE}/project_cctest_scratch.md"],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        entries = merge_clusters_to_wiki(intake_root)
        # No entry compiled for the ephemeral member.
        assert entries == []
        wiki = intake_root / "wiki"
        assert list(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")) == []
