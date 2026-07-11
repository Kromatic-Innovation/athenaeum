# SPDX-License-Identifier: Apache-2.0
"""Tests for the #328 source-backfill pass (``repair --backfill-sources``).

Covers :func:`athenaeum.transcript_verify.classify_backfill_claim` and
:func:`athenaeum.repair.backfill_sources`. Every test injects a synthetic
``projects_root`` and auto-memory tree under ``tmp_path``; the real
``~/.claude`` and ``~/knowledge`` are never touched. No live LLM is used.

The pass re-classifies memories whose source was DEFAULTED to
``claude:inferred`` against their origin transcript, across three channels:
user-stated (source scalar → ``user:<ref>``, resolver tier 1),
agent-observed (source scalar → ``agent-observed:<model>:<ref>``, new tier 5),
and confirm-inferred (idempotency marker ``inferred_verified: true``).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from athenaeum import cli
from athenaeum.repair import backfill_sources

# ---------------------------------------------------------------------------
# Fixture builders


def _write_transcript(
    projects_root: Path,
    scope: str,
    session_id: str,
    records: list[dict[str, object]],
) -> Path:
    scope_dir = projects_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _write_memory(
    auto_memory_root: Path,
    scope: str,
    filename: str,
    *,
    name: str = "",
    body: str = "Body line.\n",
    session_id: str | None = "sess0001",
    turn: int | None = 4,
    source: str = "claude:inferred",
    extra_fm: str = "",
) -> Path:
    scope_dir = auto_memory_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: feedback"]
    if name:
        lines.append(f"name: {name}")
    if session_id is not None:
        lines.append(f"originSessionId: {session_id}")
    if turn is not None:
        lines.append(f"originTurn: {turn}")
    lines.append("source_type: inferred")
    if session_id is not None:
        lines.append(f"source_ref: {session_id}")
    lines.append(f"source: {source}")
    if extra_fm:
        lines.append(extra_fm.rstrip("\n"))
    lines.append("---")
    lines.append("")
    text = "\n".join(lines) + "\n" + body
    path = scope_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _user_record(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_record(text: str, model: str = "claude-opus-4-8") -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
    }


def _tool_result_record(text: str) -> dict[str, object]:
    """A tool result — delivered as a user-role record with a tool_result block."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
        },
    }


def _meta(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    fm = text.split("---\n", 2)[1]
    return yaml.safe_load(fm)


# ===========================================================================
# classify_backfill_claim — the transcript classifier primitive
# ===========================================================================


class TestClassifyBackfillClaim:
    def test_user_stated(self, tmp_path: Path) -> None:
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "s1", [_user_record("Kromatic was founded in 2013.")])
        res = classify_backfill_claim(
            "scopeA",
            "s1",
            turn=4,
            claim="Kromatic was founded in 2013",
            projects_root=pr,
        )
        assert res.channel == "user-stated"
        assert res.ref == "s1#turn4"

    def test_agent_observed_from_tool_result_with_model(self, tmp_path: Path) -> None:
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        _write_transcript(
            pr,
            "scopeA",
            "s2",
            [
                _assistant_record("Let me read the file.", model="claude-sonnet-4-6"),
                _tool_result_record("VERSION = 3.14 in config.py"),
            ],
        )
        res = classify_backfill_claim(
            "scopeA", "s2", turn=7, claim="VERSION = 3.14", projects_root=pr
        )
        assert res.channel == "agent-observed"
        assert res.ref == "s2#turn7"
        assert res.model == "claude-sonnet-4-6"

    def test_user_wins_over_tool_result(self, tmp_path: Path) -> None:
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        _write_transcript(
            pr,
            "scopeA",
            "s3",
            [
                _tool_result_record("the deploy target is Fly.io"),
                _user_record("Yes, the deploy target is Fly.io."),
            ],
        )
        res = classify_backfill_claim(
            "scopeA",
            "s3",
            turn=1,
            claim="the deploy target is Fly.io",
            projects_root=pr,
        )
        assert res.channel == "user-stated"

    def test_no_support_confirms_inferred(self, tmp_path: Path) -> None:
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "s4", [_user_record("Unrelated small talk.")])
        res = classify_backfill_claim(
            "scopeA", "s4", turn=2, claim="a claim never uttered", projects_root=pr
        )
        assert res.channel == "inferred"

    def test_missing_transcript_is_unavailable(self, tmp_path: Path) -> None:
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        pr.mkdir()
        res = classify_backfill_claim("scopeA", "gone", turn=2, claim="anything", projects_root=pr)
        assert res.channel == "unavailable"

    def test_plain_assistant_text_is_not_agent_observed(self, tmp_path: Path) -> None:
        # A claim appearing ONLY in plain assistant prose (not a tool_result
        # block) is not an artifact the agent READ — it must confirm inferred,
        # not upgrade to agent-observed.
        from athenaeum.transcript_verify import classify_backfill_claim

        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "s5", [_assistant_record("I think the answer is 42.")])
        res = classify_backfill_claim(
            "scopeA", "s5", turn=1, claim="the answer is 42", projects_root=pr
        )
        assert res.channel == "inferred"


# ===========================================================================
# backfill_sources — the end-to-end pass
# ===========================================================================


class TestUserStatedUpgrade:
    def test_source_scalar_rewritten_to_user_ref_and_precedence_lift(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "u100", [_user_record("Emily is my wife.")])
        path = _write_memory(
            am,
            "scopeX",
            "user_wife.md",
            name="Emily is my wife",
            session_id="u100",
            turn=3,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.user_stated == 1
        meta = _meta(path)
        # The scalar rewrite is the precedence bridge (resolver keys on the
        # SCALAR): claude:inferred (bottom) -> user:<ref> (tier 1).
        assert meta["source"] == "user:u100#turn3"
        assert meta["source_type"] == "user-stated"
        assert meta["source_ref"] == "u100#turn3"
        # Legal per provenance.parse_source.
        from athenaeum.provenance import parse_source

        parsed = parse_source(meta["source"])
        assert parsed is not None and parsed.type == "user"


class TestAgentObservedUpgrade:
    def test_source_scalar_rewritten_with_model(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(
            pr,
            "scopeX",
            "a200",
            [
                _assistant_record("reading", model="claude-opus-4-8"),
                _tool_result_record("develop tip is SHA abc123"),
            ],
        )
        path = _write_memory(
            am,
            "scopeX",
            "feedback_sha.md",
            name="develop tip is SHA abc123",
            session_id="a200",
            turn=9,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.agent_observed == 1
        meta = _meta(path)
        assert meta["source"] == "agent-observed:claude-opus-4-8:a200#turn9"
        assert meta["source_type"] == "agent-observed"
        assert meta["source_ref"] == "a200#turn9"
        assert meta["model"] == "claude-opus-4-8"
        from athenaeum.provenance import parse_source

        assert parse_source(meta["source"]) is not None

    def test_model_omitted_when_transcript_lacks_it(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        # A tool result with no assistant model turn.
        _write_transcript(pr, "scopeX", "a300", [_tool_result_record("the widget count is 7")])
        path = _write_memory(
            am,
            "scopeX",
            "feedback_widget.md",
            name="the widget count is 7",
            session_id="a300",
            turn=2,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.agent_observed == 1
        meta = _meta(path)
        assert meta["source"] == "agent-observed:a300#turn2"
        assert "model" not in meta or not meta.get("model")


class TestConfirmInferred:
    def test_marker_stamped_and_precedence_unchanged(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "i400", [_user_record("totally unrelated")])
        path = _write_memory(
            am,
            "scopeX",
            "feedback_guess.md",
            name="an unverifiable leap",
            session_id="i400",
            turn=1,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.confirmed_inferred == 1
        meta = _meta(path)
        assert meta["inferred_verified"] is True
        # Precedence UNCHANGED — the source scalar stays claude:inferred.
        assert meta["source"] == "claude:inferred"
        assert meta["source_type"] == "inferred"


class TestIdempotency:
    def test_rerun_is_noop(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "u500", [_user_record("fact stated here")])
        path = _write_memory(
            am,
            "scopeX",
            "user_fact.md",
            name="fact stated here",
            session_id="u500",
            turn=2,
        )
        r1 = backfill_sources(am, projects_root=pr, apply=True)
        assert r1.user_stated == 1
        after_first = path.read_text(encoding="utf-8")
        r2 = backfill_sources(am, projects_root=pr, apply=True)
        assert r2.user_stated == 0
        assert r2.agent_observed == 0
        assert r2.confirmed_inferred == 0
        # Byte-for-byte identical on the no-op re-run.
        assert path.read_text(encoding="utf-8") == after_first

    def test_confirmed_inferred_rerun_is_noop(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "i600", [_user_record("no match here")])
        path = _write_memory(
            am,
            "scopeX",
            "feedback_x.md",
            name="claim with no support",
            session_id="i600",
            turn=1,
        )
        backfill_sources(am, projects_root=pr, apply=True)
        snapshot = path.read_text(encoding="utf-8")
        r2 = backfill_sources(am, projects_root=pr, apply=True)
        assert r2.confirmed_inferred == 0
        assert path.read_text(encoding="utf-8") == snapshot


class TestDryRunWritesNothing:
    def test_dry_run_records_but_does_not_write(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "u700", [_user_record("dry run claim")])
        path = _write_memory(
            am,
            "scopeX",
            "user_dry.md",
            name="dry run claim",
            session_id="u700",
            turn=1,
        )
        before = path.read_text(encoding="utf-8")
        report = backfill_sources(am, projects_root=pr, apply=False)
        assert report.user_stated == 1
        assert len(report.changes) == 1
        # Nothing written in dry-run.
        assert path.read_text(encoding="utf-8") == before


class TestTranscriptMissingSkip:
    def test_missing_transcript_is_skipped_not_confirmed(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        pr.mkdir()
        am = tmp_path / "raw" / "auto-memory"
        path = _write_memory(
            am,
            "scopeGone",
            "feedback_orphan.md",
            name="orphaned claim",
            session_id="rolled-off",
            turn=1,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.user_stated == 0
        assert report.confirmed_inferred == 0
        assert any("transcript-unavailable" in reason for _p, reason in report.skips)
        # Unchanged — never guessed.
        assert _meta(path)["source"] == "claude:inferred"
        assert "inferred_verified" not in _meta(path)

    def test_no_origin_session_is_skipped(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        pr.mkdir()
        am = tmp_path / "raw" / "auto-memory"
        _write_memory(
            am,
            "scopeX",
            "feedback_noorigin.md",
            name="claim",
            session_id=None,
            turn=None,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert any("no-origin-session" in reason for _p, reason in report.skips)


class TestClaimMatchTitleVsFirstLine:
    def test_match_on_name_frontmatter(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "n800", [_user_record("The sky is blue today.")])
        path = _write_memory(
            am,
            "scopeX",
            "user_named.md",
            name="The sky is blue",
            body="Some unrelated body text.\n",
            session_id="n800",
            turn=1,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.user_stated == 1
        assert _meta(path)["source_type"] == "user-stated"

    def test_fallback_to_first_body_line_when_no_name(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "n900", [_user_record("The grass is green here.")])
        # No name/title in frontmatter — claim comes from the first body line.
        path = _write_memory(
            am,
            "scopeX",
            "user_firstline.md",
            name="",
            body="The grass is green\n\nmore body.\n",
            session_id="n900",
            turn=1,
        )
        report = backfill_sources(am, projects_root=pr, apply=True)
        assert report.user_stated == 1
        assert _meta(path)["source_type"] == "user-stated"


class TestAsserterPopulation:
    _ASSERTER = {
        "type": "person",
        "iss": "https://accounts.google.com",
        "sub": "1076-abc",
        "name": "Alice Example",
        "email": "alice@example.com",
    }

    def test_on_behalf_of_populated_when_identity_present(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "b100", [_user_record("claim with asserter")])
        path = _write_memory(
            am,
            "scopeX",
            "user_asserted.md",
            name="claim with asserter",
            session_id="b100",
            turn=1,
        )
        backfill_sources(am, projects_root=pr, apply=True, asserter=self._ASSERTER)
        assert _meta(path)["on_behalf_of"] == "Alice Example"

    def test_on_behalf_of_absent_without_identity(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "b200", [_user_record("claim no asserter")])
        path = _write_memory(
            am,
            "scopeX",
            "user_bare.md",
            name="claim no asserter",
            session_id="b200",
            turn=1,
        )
        # No asserter supplied — on_behalf_of must stay absent (the #327 fallback).
        backfill_sources(am, projects_root=pr, apply=True, asserter=None)
        assert "on_behalf_of" not in _meta(path)

    def test_on_behalf_of_absent_when_asserter_has_no_durable_key(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "b300", [_user_record("claim weak asserter")])
        path = _write_memory(
            am,
            "scopeX",
            "user_weak.md",
            name="claim weak asserter",
            session_id="b300",
            turn=1,
        )
        # An asserter with only a display name (no iss/sub) yields no durable
        # identity key → on_behalf_of stays absent.
        backfill_sources(am, projects_root=pr, apply=True, asserter={"name": "Nobody"})
        assert "on_behalf_of" not in _meta(path)


class TestBodyAndYamlUntouched:
    def test_body_and_unrelated_frontmatter_preserved(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "p100", [_user_record("preserve me claim")])
        body = "First line.\n\n## A heading\n\n- bullet one\n- bullet two\n"
        path = _write_memory(
            am,
            "scopeX",
            "user_preserve.md",
            name="preserve me claim",
            body=body,
            session_id="p100",
            turn=1,
            extra_fm="tags:\n  - alpha\n  - beta",
        )
        backfill_sources(am, projects_root=pr, apply=True)
        text = path.read_text(encoding="utf-8")
        # Body preserved byte-for-byte.
        assert text.endswith(body)
        # Unrelated frontmatter (tags block) preserved.
        assert "tags:\n  - alpha\n  - beta" in text
        meta = _meta(path)
        assert meta["tags"] == ["alpha", "beta"]
        assert meta["type"] == "feedback"


class TestBoundedResumableBatch:
    def test_limit_caps_acted_and_reports_resume(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        am = tmp_path / "raw" / "auto-memory"
        for i in range(3):
            sid = f"r{i}"
            _write_transcript(pr, "scopeX", sid, [_user_record(f"claim number {i}")])
            _write_memory(
                am,
                "scopeX",
                f"user_{i}.md",
                name=f"claim number {i}",
                session_id=sid,
                turn=1,
            )
        report = backfill_sources(am, projects_root=pr, apply=True, limit=2)
        assert report.user_stated == 2
        assert report.resume_after is not None
        # A follow-up run finishes the rest (idempotency makes resume implicit).
        report2 = backfill_sources(am, projects_root=pr, apply=True, limit=2)
        assert report2.user_stated == 1


class TestCliIntegration:
    def test_cli_backfill_dry_run_exit_code_2(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        knowledge = tmp_path / "knowledge"
        am = knowledge / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "c100", [_user_record("cli claim here")])
        _write_memory(
            am,
            "scopeX",
            "user_cli.md",
            name="cli claim here",
            session_id="c100",
            turn=1,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(
                [
                    "repair",
                    "--backfill-sources",
                    "--knowledge-root",
                    str(knowledge),
                    "--projects-root",
                    str(pr),
                ]
            )
        # Dry-run found an upgrade → CI gate signal 2.
        assert rc == 2
        assert "backfill-sources (DRY RUN)" in buf.getvalue()

    def test_cli_backfill_apply_writes(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        knowledge = tmp_path / "knowledge"
        am = knowledge / "raw" / "auto-memory"
        _write_transcript(pr, "scopeX", "c200", [_user_record("apply claim here")])
        path = _write_memory(
            am,
            "scopeX",
            "user_apply.md",
            name="apply claim here",
            session_id="c200",
            turn=1,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(
                [
                    "repair",
                    "--backfill-sources",
                    "--apply",
                    "--knowledge-root",
                    str(knowledge),
                    "--projects-root",
                    str(pr),
                ]
            )
        assert rc == 0
        assert _meta(path)["source"] == "user:c200#turn1"
