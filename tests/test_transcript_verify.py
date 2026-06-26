# SPDX-License-Identifier: Apache-2.0
"""Tests for origin-traced transcript verification (issue #260, slice A of #259).

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
