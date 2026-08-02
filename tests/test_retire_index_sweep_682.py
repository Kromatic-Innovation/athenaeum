"""Fail-closed MEMORY.md pointer pruning in the retire pass (issue #682).

Regression cover for the silent memory-index deletion incident: the retire
pass pruned 10 *valid* pointers from a live Claude Code ``MEMORY.md`` because
the sweep dropped a pointer purely on a name match against the members retired
that pass (``retired_names.__contains__``) — it never proved the target file
was actually gone. ``MEMORY.md`` is loaded into every Claude Code session and
is written in place (the raw index is hardlinked to the operator's live
``~/.claude/projects/<scope>/memory/MEMORY.md``), so a false positive silently
deletes a memory.

The fix makes the real write path fail closed: a pointer is dropped ONLY when
its target was retired this pass AND is genuinely absent on disk, checked AFTER
the ``git rm`` so on-disk absence reflects the deletion. A present target keeps
its pointer. Every drop is logged by name (not counted), the pre-prune index is
git-backed up before modification, and the prune count rides the run-summary.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from athenaeum.contradictions import ContradictionResult
from athenaeum.merge import MergedWikiEntry
from athenaeum.retire import _plan_index_sweep, run_retire_pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


# ---------------------------------------------------------------------------
# _plan_index_sweep — the load-bearing fail-closed decision (AC1 + AC2)
# ---------------------------------------------------------------------------


class TestPlanIndexSweepFailClosed:
    """``require_absent=True`` never drops a pointer whose target still exists."""

    def _scope_with_pointer(self, tmp_path: Path) -> tuple[Path, Path]:
        # The observed layout: a memory file present in raw/auto-memory/<scope>/
        # with a live MEMORY.md pointer at it.
        scope = tmp_path / "raw" / "auto-memory" / "-Users-x-Code"
        scope.mkdir(parents=True)
        (scope / "MEMORY.md").write_text(
            "# Memory Index\n"
            "- [Guardrail](feedback_no_unauthorized_client_names_public.md) — hook\n"
            "- [Other](reference_live.md) — hook\n",
            encoding="utf-8",
        )
        member = scope / "feedback_no_unauthorized_client_names_public.md"
        member.write_text("---\nname: g\n---\nkeep me\n", encoding="utf-8")
        (scope / "reference_live.md").write_text("live", encoding="utf-8")

        # Faithful to the observed layout (#682): the same memory exists in the
        # operator's live Claude memory dir too, HARDLINKED to the raw copy (a
        # write to one is a write to the other). The sweep decides on the raw
        # sibling the MEMORY.md lives beside.
        memory_dir = tmp_path / ".claude" / "projects" / "-Users-x-Code" / "memory"
        memory_dir.mkdir(parents=True)
        os.link(member, memory_dir / member.name)
        return scope, member

    def test_present_target_is_never_pruned(self, tmp_path: Path) -> None:
        # AC1: a present-but-uncompiled memory file (present in BOTH the memory
        # dir and raw/auto-memory/) is NEVER pruned, even when it is in the
        # retired set for this pass.
        scope, member = self._scope_with_pointer(tmp_path)
        entry = MergedWikiEntry(
            topic_slug="t",
            cluster_id="c-1",
            cluster_centroid_score=1.0,
            contradictions_detected=False,
            body="b\n",
        )
        retiring = [(entry, [member])]

        plan = _plan_index_sweep(retiring, require_absent=True)

        # The file is present on disk → its pointer is kept → nothing to rewrite.
        assert plan == {}

    def test_absent_target_is_pruned(self, tmp_path: Path) -> None:
        # The complement: once the file is genuinely gone, its pointer is swept.
        scope, member = self._scope_with_pointer(tmp_path)
        member.unlink()  # the retire pass's git rm removed it for real
        entry = MergedWikiEntry(
            topic_slug="t",
            cluster_id="c-1",
            cluster_centroid_score=1.0,
            contradictions_detected=False,
            body="b\n",
        )
        retiring = [(entry, [member])]

        plan = _plan_index_sweep(retiring, require_absent=True)

        (index_path, (new_text, dropped)) = next(iter(plan.items()))
        assert dropped == ["feedback_no_unauthorized_client_names_public.md"]
        # The live, still-present sibling pointer survives untouched.
        assert "reference_live.md" in new_text
        assert "feedback_no_unauthorized_client_names_public.md" not in new_text

    def test_dry_run_predicts_by_name_without_disk_check(self, tmp_path: Path) -> None:
        # require_absent=False is the dry-run predictor: it reports what a real
        # run WOULD sweep (by retired name), since a dry run deletes nothing and
        # so absence cannot yet be observed. The present file is still on disk.
        scope, member = self._scope_with_pointer(tmp_path)
        entry = MergedWikiEntry(
            topic_slug="t",
            cluster_id="c-1",
            cluster_centroid_score=1.0,
            contradictions_detected=False,
            body="b\n",
        )
        retiring = [(entry, [member])]

        plan = _plan_index_sweep(retiring, require_absent=False)

        (_index_path, (_new_text, dropped)) = next(iter(plan.items()))
        assert dropped == ["feedback_no_unauthorized_client_names_public.md"]


# ---------------------------------------------------------------------------
# run_retire_pass — end-to-end wiring (AC3 logging, AC4 backup, AC5 summary)
# ---------------------------------------------------------------------------


def _knowledge_root(tmp_path: Path) -> tuple[Path, Path, str]:
    kr = tmp_path / "knowledge"
    scope_name = "-Users-x-Code"
    scope = kr / "raw" / "auto-memory" / scope_name
    scope.mkdir(parents=True)
    (kr / "wiki").mkdir(parents=True)

    member = scope / "project_repo_owned_skills_contract.md"
    member.write_text("---\nname: c\n---\nA contract fact.\n", encoding="utf-8")
    (scope / "MEMORY.md").write_text(
        "# Memory Index\n"
        "- [Contract](project_repo_owned_skills_contract.md) — hook\n"
        "- [Kept](feedback_kept.md) — hook\n",
        encoding="utf-8",
    )
    (scope / "feedback_kept.md").write_text("kept", encoding="utf-8")

    _git(kr, "init", "-b", "develop")
    _git(kr, "config", "user.email", "t@example.com")
    _git(kr, "config", "user.name", "Retire Test")
    _git(kr, "add", "-A")
    _git(kr, "commit", "-m", "seed")
    return kr, member, scope_name


def _landed_entry(scope_name: str) -> MergedWikiEntry:
    header = f"## From `{scope_name}/project_repo_owned_skills_contract.md`"
    return MergedWikiEntry(
        topic_slug="repo-owned-skills",
        cluster_id=f"{scope_name}-1",
        cluster_centroid_score=1.0,
        contradictions_detected=False,
        contradiction=ContradictionResult(detected=False, rationale="singleton"),
        member_paths=[f"{scope_name}/project_repo_owned_skills_contract.md"],
        body=f"{header}\nA contract fact.\n",
    )


def _config() -> dict:
    return {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}


class TestRunRetirePassIndexSweep:
    def test_genuine_retirement_prunes_logs_by_name_and_backs_up(
        self, tmp_path: Path, caplog
    ) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        with caplog.at_level(logging.INFO, logger="athenaeum.retire"):
            report = run_retire_pass(
                [entry], kr, config=_config(), projects_root=None
            )

        # The member's fact landed in the wiki and its raw file was git rm'd, so
        # its now-absent pointer is pruned — reported and logged BY NAME (AC3).
        assert report.committed is True
        assert report.index_pruned == [
            f"{scope_name}/project_repo_owned_skills_contract.md"
        ]
        assert any(
            "project_repo_owned_skills_contract.md" in r.getMessage()
            and "absent on disk" in r.getMessage()
            for r in caplog.records
        )

        # The live, still-present sibling pointer is untouched (fail closed).
        index_now = (
            kr / "raw" / "auto-memory" / scope_name / "MEMORY.md"
        ).read_text(encoding="utf-8")
        assert "feedback_kept.md" in index_now
        assert "project_repo_owned_skills_contract.md" not in index_now

        # AC4: the pre-prune MEMORY.md bytes are git-recoverable — backed up in
        # the provenance-snapshot commit BEFORE the rewrite commit.
        prior = _git(
            kr,
            "show",
            f"HEAD~1:raw/auto-memory/{scope_name}/MEMORY.md",
        )
        assert "project_repo_owned_skills_contract.md" in prior.stdout

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)
        before = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(
            encoding="utf-8"
        )

        report = run_retire_pass(
            [entry], kr, config=_config(), dry_run=True, projects_root=None
        )

        # Dry-run predicts the sweep but writes nothing.
        assert report.index_pruned == [
            f"{scope_name}/project_repo_owned_skills_contract.md"
        ]
        after = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(
            encoding="utf-8"
        )
        assert after == before
        assert member.exists()


class TestIndexPrunedInRunSummary:
    """AC5: a pruning event is visible in the greppable librarian-run-summary."""

    def test_index_pruned_rides_run_summary_line(self) -> None:
        from athenaeum.librarian import _render_run_summary

        line = _render_run_summary([("retire", 0.1, {"index_pruned": 3})])
        assert "retire secs=0.100 index_pruned=3" in line

    def test_zero_prune_still_reports_the_field(self) -> None:
        from athenaeum.librarian import _render_run_summary

        line = _render_run_summary([("retire", 0.1, {"index_pruned": 0})])
        assert "index_pruned=0" in line
