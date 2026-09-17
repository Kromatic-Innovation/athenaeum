# SPDX-License-Identifier: Apache-2.0
"""Live-session guard on the move-then-retire pass (issue athenaeum#1728).

A memory file is retire-eligible only when the Claude Code session that owns
its scope is closed:

1. A session-end marker for the scope, newer than the file, releases the
   hold REGARDLESS of transcript age.
2. Failing that, a scope transcript modified within the quiet window (default
   30 minutes) holds the file; nothing modified within the window (including
   no transcripts at all) releases it.

Held files are counted and reported (``RetireReport.held_live_session``),
never silently skipped, on both a real run and ``--dry-run``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from athenaeum.contradictions import ContradictionResult
from athenaeum.live_session_guard import marker_path, record_session_end
from athenaeum.merge import MergedWikiEntry
from athenaeum.retire import HOLD_LIVE_SESSION, MOVE, run_retire_pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def _knowledge_root(tmp_path: Path) -> tuple[Path, Path, str]:
    kr = tmp_path / "knowledge"
    scope_name = "-Users-x-Code"
    scope = kr / "raw" / "auto-memory" / scope_name
    scope.mkdir(parents=True)
    (kr / "wiki").mkdir(parents=True)

    member = scope / "project_repo_owned_skills_contract.md"
    member.write_text("---\nname: c\n---\nA contract fact.\n", encoding="utf-8")
    (scope / "MEMORY.md").write_text(
        "# Memory Index\n- [Contract](project_repo_owned_skills_contract.md) — hook\n",
        encoding="utf-8",
    )

    _git(kr, "init", "-b", "develop")
    _git(kr, "config", "user.email", "t@example.com")
    _git(kr, "config", "user.name", "Live Session Guard Test")
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


class TestLiveTranscriptHolds:
    def test_transcript_modified_now_holds_file_and_index_line(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        (projects_root / scope_name / "sess-live.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.committed is False
        assert report.moved == []
        assert report.held_live_session == [str(member)]
        assert str(member) in report.held
        holds = [d for d in report.dispositions if d.disposition == HOLD_LIVE_SESSION]
        assert len(holds) == 1
        assert "live session" in holds[0].reason

        # File and its MEMORY.md pointer both survive untouched.
        assert member.exists()
        index_now = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(
            encoding="utf-8"
        )
        assert "project_repo_owned_skills_contract.md" in index_now


class TestAgedTranscriptMoves:
    def test_transcript_aged_past_window_moves_file(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        transcript = projects_root / scope_name / "sess-old.jsonl"
        transcript.write_text("{}")
        old = time.time() - 3600  # 1h, past the 1800s default window
        os.utime(transcript, (old, old))
        cache_dir = tmp_path / "cache"

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.committed is True
        assert report.moved == [str(member)]
        assert report.held_live_session == []
        moves = [d for d in report.dispositions if d.disposition == MOVE]
        assert len(moves) == 1
        assert not member.exists()  # git rm'd

    def test_no_transcripts_at_all_moves_file(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"  # scope dir never created
        cache_dir = tmp_path / "cache"

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.committed is True
        assert report.moved == [str(member)]
        assert report.held_live_session == []


class TestSessionEndMarkerReleasesHold:
    def test_marker_newer_than_file_releases_hold_regardless_of_transcript_age(
        self, tmp_path: Path
    ) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        transcript = projects_root / scope_name / "sess-live.jsonl"
        transcript.write_text("{}")  # fresh — would hold on transcript age alone
        cache_dir = tmp_path / "cache"

        # The session-end marker must be resolvable via the join key
        # `<projects_root>/<scope>/<session_id>.jsonl` -- name the transcript
        # after the session id `record_session_end` is called with.
        session_id = "sess-live"
        recorded_scope = record_session_end(
            session_id, cache_dir=cache_dir, projects_root=projects_root
        )
        assert recorded_scope == scope_name
        assert marker_path(cache_dir, scope_name).is_file()

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.committed is True
        assert report.moved == [str(member)]
        assert report.held_live_session == []

    def test_marker_older_than_file_does_not_release_hold(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        transcript = projects_root / scope_name / "sess-live.jsonl"
        transcript.write_text("{}")
        cache_dir = tmp_path / "cache"

        # Write a marker, then make the member file's mtime NEWER than the
        # marker -- the marker no longer explains the file's current
        # content, so the hold stands (falls through to the transcript rung,
        # which is fresh -> held).
        record_session_end("sess-live", cache_dir=cache_dir, projects_root=projects_root)
        future = time.time() + 10
        os.utime(member, (future, future))

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.held_live_session == [str(member)]
        assert report.moved == []


class TestGuardOptOut:
    def test_live_session_guard_false_bypasses_the_hold(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        (projects_root / scope_name / "sess-live.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
            live_session_guard=False,
        )

        assert report.moved == [str(member)]
        assert report.held_live_session == []

    def test_yaml_toggle_off_bypasses_the_hold(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        (projects_root / scope_name / "sess-live.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"

        config = _config()
        config["librarian"] = {"live_session_guard": False}

        report = run_retire_pass(
            [entry],
            kr,
            config=config,
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.moved == [str(member)]


class TestRunSummaryAndDryRunNameHeldFiles:
    def test_dry_run_reports_held_live_session_without_writing(self, tmp_path: Path) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path)
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        (projects_root / scope_name / "sess-live.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"
        before = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(encoding="utf-8")

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            dry_run=True,
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        assert report.dry_run is True
        assert report.held_live_session == [str(member)]
        assert member.exists()
        after = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(encoding="utf-8")
        assert after == before

    def test_held_live_session_rides_run_summary_line(self) -> None:
        from athenaeum.librarian import _render_run_summary

        line = _render_run_summary([("retire", 0.1, {"index_pruned": 0, "held_live_session": 2})])
        assert "held_live_session=2" in line

    def test_zero_held_live_session_still_reports_the_field(self) -> None:
        from athenaeum.librarian import _render_run_summary

        line = _render_run_summary([("retire", 0.1, {"index_pruned": 0, "held_live_session": 0})])
        assert "held_live_session=0" in line
