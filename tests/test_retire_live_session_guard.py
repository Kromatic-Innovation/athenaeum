# SPDX-License-Identifier: Apache-2.0
"""Live-session guard on the move-then-retire pass (issue athenaeum#1728).

A memory file is retire-eligible only when the Claude Code session that OWNS
it is closed:

1. A session-end marker whose session id matches the file's OWNING session
   (``originSessionId`` frontmatter, or the session-recovery join when
   absent), newer than the file, releases the hold REGARDLESS of transcript
   age -- and regardless of any OTHER session still live in the same scope.
2. Failing that (no marker, a marker for a different session, a marker
   older than the file, or an unresolved owner), ANY transcript in the
   scope modified within the quiet window (default 30 minutes) holds the
   file; nothing modified within the window across the whole scope
   (including no transcripts at all) releases it.

Held files are counted (run summary) and named, with a reason, in
``RetireReport.dispositions`` -- never silently skipped -- on both a real
run and ``--dry-run``.
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


def _knowledge_root(
    tmp_path: Path, *, origin_session_id: str | None = None
) -> tuple[Path, Path, str]:
    """A git-initialized knowledge root with one landed, move-eligible member.

    ``origin_session_id``, when given, is stamped as the member's
    ``originSessionId`` frontmatter -- the primary rung
    :func:`athenaeum.live_session_guard.resolve_owning_session_id` reads.
    Left unset (the default), the member declares no owner and the
    synthetic transcripts these tests write carry no write-cited/time-
    window signal either, so owner resolution honestly returns ``None`` --
    exactly the "unknown owner" case several tests below exercise on
    purpose.
    """
    kr = tmp_path / "knowledge"
    scope_name = "-Users-x-Code"
    scope = kr / "raw" / "auto-memory" / scope_name
    scope.mkdir(parents=True)
    (kr / "wiki").mkdir(parents=True)

    member = scope / "project_repo_owned_skills_contract.md"
    frontmatter = "---\nname: c\n"
    if origin_session_id is not None:
        frontmatter += f"originSessionId: {origin_session_id}\n"
    frontmatter += "---\nA contract fact.\n"
    member.write_text(frontmatter, encoding="utf-8")
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
    """The marker rung applies only when it names the file's OWNING session."""

    def test_owner_matched_marker_releases_hold_regardless_of_transcript_age(
        self, tmp_path: Path
    ) -> None:
        # The member declares `originSessionId: sess-live` -- the SAME
        # session the marker below is recorded for -- so the marker rung
        # applies and releases the hold even though the transcript is fresh
        # (would otherwise hold on transcript age alone via the quiet
        # window).
        kr, member, scope_name = _knowledge_root(tmp_path, origin_session_id="sess-live")
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
        kr, member, scope_name = _knowledge_root(tmp_path, origin_session_id="sess-live")
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        (projects_root / scope_name).mkdir(parents=True)
        transcript = projects_root / scope_name / "sess-live.jsonl"
        transcript.write_text("{}")
        cache_dir = tmp_path / "cache"

        # Write a marker for the OWNING session, then make the member file's
        # mtime NEWER than the marker -- the marker no longer explains the
        # file's current content, so the hold stands (falls through to the
        # quiet-window rung, which is fresh -> held).
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


class TestMultiSessionScope:
    """A scope can hold more than one live session; the marker must not blur them.

    Regression cover for the Quine review's demonstrated bug: two live
    transcripts (sessions A and B) share one scope, a memory file is owned
    by B, and `record_session_end("sess-A")` writes a marker naming A. The
    fixed guard must NOT read "A has ended" as license to retire a file B
    -- still live -- owns.
    """

    def test_marker_for_a_different_session_does_not_release_an_other_owners_file(
        self, tmp_path: Path
    ) -> None:
        kr, member, scope_name = _knowledge_root(tmp_path, origin_session_id="sess-B")
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / scope_name
        scope_dir.mkdir(parents=True)
        # Both sessions' transcripts are live (fresh) in the SAME scope.
        (scope_dir / "sess-A.jsonl").write_text("{}")
        (scope_dir / "sess-B.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"

        # Session A ends; its marker is recorded. Session B is still live.
        recorded_scope = record_session_end(
            "sess-A", cache_dir=cache_dir, projects_root=projects_root
        )
        assert recorded_scope == scope_name

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        # The file is owned by B, not A -- A's marker must not release it,
        # and the quiet window sees B's (and A's) live transcript, so it is
        # correctly held rather than moved out from under B.
        assert report.committed is False
        assert report.moved == []
        assert report.held_live_session == [str(member)]
        assert member.exists()
        index_now = (kr / "raw" / "auto-memory" / scope_name / "MEMORY.md").read_text(
            encoding="utf-8"
        )
        assert "project_repo_owned_skills_contract.md" in index_now

    def test_marker_for_the_owning_session_releases_even_with_another_session_live(
        self, tmp_path: Path
    ) -> None:
        # The complement: B's OWN marker releases B's file even though A is
        # still live in the same scope -- the quiet window is only ever
        # consulted when the marker rung does NOT already resolve it.
        kr, member, scope_name = _knowledge_root(tmp_path, origin_session_id="sess-B")
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / scope_name
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-A.jsonl").write_text("{}")
        (scope_dir / "sess-B.jsonl").write_text("{}")
        cache_dir = tmp_path / "cache"

        record_session_end("sess-B", cache_dir=cache_dir, projects_root=projects_root)

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


class TestUnknownOwnerFallsBackToQuietWindow:
    def test_unknown_owner_with_one_fresh_transcript_holds(self, tmp_path: Path) -> None:
        # No `originSessionId` frontmatter and no write-cited/time-window
        # transcript signal -- the owner genuinely cannot be determined. A
        # marker for some OTHER, unrelated session exists in the same
        # scope's cache -- proving it is the OWNER being unknown, not merely
        # "no marker at all", that routes this to the quiet-window rung.
        kr, member, scope_name = _knowledge_root(tmp_path)  # no origin_session_id
        entry = _landed_entry(scope_name)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / scope_name
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-unrelated.jsonl").write_text("{}")  # fresh
        cache_dir = tmp_path / "cache"
        record_session_end("sess-unrelated", cache_dir=cache_dir, projects_root=projects_root)

        report = run_retire_pass(
            [entry],
            kr,
            config=_config(),
            projects_root=projects_root,
            cache_dir=cache_dir,
        )

        # Unknown owner never short-circuits to a release: the marker rung
        # has nothing to match against, so the quiet window decides, and a
        # fresh transcript in the scope holds.
        assert report.committed is False
        assert report.held_live_session == [str(member)]
        assert member.exists()


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

    # The `held_live_session=N` count riding the run-summary prose line is
    # covered end-to-end via a real `athenaeum.librarian.run()` call (Quine
    # review, issue athenaeum#1728 follow-up) rather than hand-feeding
    # `_render_run_summary` a synthetic profile tuple: see
    # `test_run_level_held_live_session_count_rides_the_real_run_summary` on
    # `TestRetireIntegrationViaRun` in tests/test_librarian_auto_memory.py.
