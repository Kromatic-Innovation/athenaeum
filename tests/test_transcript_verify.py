# SPDX-License-Identifier: Apache-2.0
"""Tests for origin-traced transcript verification (issue athenaeum#260, slice A of athenaeum#259).

Covers :mod:`athenaeum.transcript_verify`. The verifier reads session
transcripts under ``<projects_root>/<scope>/*.jsonl`` to attribute the
*ultimate* source of a claim — the user, an external URL, a document, or
(when nothing can be established) an honest ``inferred``.

Every test injects a synthetic ``projects_root`` under ``tmp_path``; the
real ``~/.claude`` is never read. The load-bearing invariant: a
``source_ref`` is NEVER the raw ``auto-memory/...`` filename.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_transcript(
    projects_root: Path,
    scope: str,
    session_id: str,
    records: list[dict[str, object]],
) -> Path:
    """Write a synthetic ``<projects_root>/<scope>/<session>.jsonl`` transcript."""
    scope_dir = projects_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


class TestUserStated:
    def test_user_authored_claim_resolves_user_stated(
        self, projects_root: Path
    ) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        scope = "-Users-tristankromer-Code-voltaire"
        session = "abc12345"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Kromatic was founded in 2013, effective.",
                    },
                },
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "Noted."},
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=4,
            claim="Kromatic was founded in 2013",
            projects_root=projects_root,
        )
        assert stype == "user-stated"
        # source_ref must carry session + turn, never the raw filename.
        assert sref == f"{session}#turn4"

    def test_user_match_with_blocks_content(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        scope = "_unscoped"
        session = "blk00001"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "My wife is Emily."},
                        ],
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope, session, turn=2, claim="wife is Emily", projects_root=projects_root
        )
        assert stype == "user-stated"
        assert sref == f"{session}#turn2"


class TestExternal:
    def test_subagent_quoting_link_resolves_external(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        scope = "some-scope"
        session = "ext00001"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Find the HBS reference."},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Per https://www.hbs.edu/startup the canvas was cited."
                        ),
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=None,
            claim="the canvas was cited",
            projects_root=projects_root,
        )
        assert stype == "external"
        assert sref == "https://www.hbs.edu/startup"


class TestCrossSessionIsolation:
    def test_claim_in_other_session_not_attributed(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # Quine S1: two sessions in ONE scope. The claim lives only in
        # session B. Verifying against session A must NOT return user-stated
        # attributed to A — only A's own transcript is scanned.
        scope = "shared-scope"
        session_a = "aaaa0001"
        session_b = "bbbb0002"
        _write_transcript(
            projects_root,
            scope,
            session_a,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Session A small talk."},
                },
            ],
        )
        _write_transcript(
            projects_root,
            scope,
            session_b,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Krobar.ai launched in March 2025.",
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session_a,
            turn=1,
            claim="Krobar.ai launched in March 2025",
            projects_root=projects_root,
        )
        assert stype == "inferred"
        assert sref == f"{session_a}#turn1"
        # And session B itself still resolves correctly.
        stype_b, sref_b = verify_user_stated(
            scope,
            session_b,
            turn=5,
            claim="Krobar.ai launched in March 2025",
            projects_root=projects_root,
        )
        assert stype_b == "user-stated"
        assert sref_b == f"{session_b}#turn5"


class TestUserWinsOverExternal:
    def test_user_message_beats_agent_quoted_url(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # Quine C1: the same claim appears first as an agent-quoted URL and
        # then in a user message. user-stated must win.
        scope = "some-scope"
        session = "winuser1"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "I found it at https://example.com/canvas here.",
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Yes, the canvas is the right model.",
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=9,
            claim="the canvas",
            projects_root=projects_root,
        )
        assert stype == "user-stated"
        assert sref == f"{session}#turn9"


class TestExternalUrlHygiene:
    def test_trailing_punctuation_stripped_from_url(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # Quine N3: a URL ending a sentence must not keep the trailing period.
        scope = "some-scope"
        session = "urlpunc1"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "The source is https://www.hbs.edu/startup.",
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=None,
            claim="The source is",
            projects_root=projects_root,
        )
        assert stype == "external"
        assert sref == "https://www.hbs.edu/startup"


class TestUnicodeAndTurnZero:
    def test_unicode_claim_matches(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # Quine C3: a unicode-bearing claim must match cleanly.
        scope = "_unscoped"
        session = "uni00001"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            "I appreciate the term Widerspruchsfreiheit "
                            "(Zettelkasten)."
                        ),
                    },
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=2,
            claim="Widerspruchsfreiheit (Zettelkasten)",
            projects_root=projects_root,
        )
        assert stype == "user-stated"
        assert sref == f"{session}#turn2"

    def test_turn_zero_preserved_in_ref(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # Quine C3: turn 0 is a real turn — the ref must be ``#turn0``, not
        # collapse to the bare session (guards a future ``if turn:`` bug).
        scope = "some-scope"
        session = "turn0001"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "First thing I said."},
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=0,
            claim="First thing I said",
            projects_root=projects_root,
        )
        assert stype == "user-stated"
        assert sref == f"{session}#turn0"


class TestInferred:
    def test_missing_transcript_resolves_inferred(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        # projects_root exists but has no scope dir / no jsonl at all.
        projects_root.mkdir(parents=True, exist_ok=True)
        stype, sref = verify_user_stated(
            "-Users-tristankromer-Code-voltaire",
            "gone9999",
            turn=7,
            claim="anything at all",
            projects_root=projects_root,
        )
        assert stype == "inferred"
        # Best-effort ref still cites session+turn, NOT a raw filename.
        assert sref == "gone9999#turn7"
        assert "auto-memory" not in sref
        assert not sref.endswith(".md")

    def test_claim_absent_from_transcript_resolves_inferred(
        self, projects_root: Path
    ) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        scope = "some-scope"
        session = "miss0001"
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Totally unrelated text."},
                },
            ],
        )
        stype, sref = verify_user_stated(
            scope,
            session,
            turn=None,
            claim="a claim that never appears",
            projects_root=projects_root,
        )
        assert stype == "inferred"
        # No turn provided and no match → best-effort ref is the session id.
        assert sref == session
        assert "auto-memory" not in sref

    def test_source_ref_never_raw_filename(self, projects_root: Path) -> None:
        from athenaeum.transcript_verify import verify_user_stated

        scope = "some-scope"
        session = "rawref01"
        # Even when the transcript text literally mentions the raw filename,
        # the returned source_ref must never BE that filename.
        _write_transcript(
            projects_root,
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            "See raw/auto-memory/some-scope/user_tristan_address.md"
                        ),
                    },
                },
            ],
        )
        _stype, sref = verify_user_stated(
            scope,
            session,
            turn=3,
            claim="user_tristan_address.md",
            projects_root=projects_root,
        )
        assert sref == f"{session}#turn3"
        assert "auto-memory" not in sref
        assert not sref.endswith(".md")


class TestDefaultProjectsRootHonorsClaudeConfigDir:
    """athenaeum#723: DEFAULT_PROJECTS_ROOT must honor CLAUDE_CONFIG_DIR (which
    relocates ~/.claude), resolved at call time so every caller benefits with no
    per-caller projects_root plumbing."""

    def test_custom_config_dir_relocates_the_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.transcript_verify import default_projects_root

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/custom-claude-home")
        assert default_projects_root() == Path("/tmp/custom-claude-home") / "projects"

    def test_default_unchanged_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.transcript_verify import default_projects_root

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert default_projects_root() == Path.home() / ".claude" / "projects"

    def test_empty_config_dir_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.transcript_verify import default_projects_root

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        assert default_projects_root() == Path.home() / ".claude" / "projects"

    def test_caller_locates_transcripts_under_custom_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A transcript_verify caller passing NO explicit projects_root must find
        # the transcript under the custom CLAUDE_CONFIG_DIR — no plumbing added.
        from athenaeum.transcript_verify import verify_user_stated

        config_dir = tmp_path / "custom-config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        scope = "-Users-tristankromer-Code-voltaire"
        session = "cfg12345"
        _write_transcript(
            config_dir / "projects",
            scope,
            session,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Relocated home works."},
                }
            ],
        )
        stype, sref = verify_user_stated(
            scope, session, turn=1, claim="Relocated home works"
        )
        assert stype == "user-stated"
        assert sref == f"{session}#turn1"

    def test_push_metrics_reference_determination_honors_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AC: `athenaeum push-metrics` reference-determination locates
        # transcripts under a custom CLAUDE_CONFIG_DIR (it passes no
        # projects_root, so it resolves through default_projects_root()).
        from athenaeum import push_metrics

        config_dir = tmp_path / "custom-config"
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "pmref-session")

        # Seed a push record whose pushed id is later referenced in the session
        # transcript located under the CUSTOM config dir.
        record = push_metrics.build_push_record(
            session_id="pmref-session",
            query="q",
            backend="keyword",
            hits=[("thing-uid-1.md", {"uid": "pushed-uid-1"}, "some body text")],
        )
        push_metrics.record_push(record, cache_dir=cache_dir)
        pushed_id = record.items[0].id  # the opaque id that must be referenced

        scope = "-Users-tristankromer-Code-voltaire"
        _write_transcript(
            config_dir / "projects",
            scope,
            "pmref-session",
            [
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": f"used {pushed_id} here"},
                }
            ],
        )
        result = push_metrics.run_reference_determination(
            "pmref-session", cache_dir=cache_dir
        )
        # The transcript under the custom dir was located and scanned — a
        # determination was produced (not the None "no transcript" outcome).
        assert result is not None
