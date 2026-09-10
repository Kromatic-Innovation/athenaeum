# SPDX-License-Identifier: Apache-2.0
"""A deliberate ``recall`` is attributed to the session that MADE it (issue
athenaeum#1541).

The defect: a stdio MCP server is spawned once per conversation and its
``os.environ`` is frozen at spawn, but Claude Code ROTATES a conversation's
session id (compaction/resume) without restarting that server. So every
deliberate ``recall`` was recorded against the conversation's ORIGINAL id
forever, while the per-turn sidecar hook (a fresh process, fresh env, every
turn) recorded pushes against the CURRENT id. Pushes and pulls diverged at the
first compaction, and a session-scoped viewer could only ever render one half
of the pushed / pulled / used triple.

**Every test here pins the JOIN, not the happy path.** In each fixture the
environment id and the transcript-derived id DIFFER — a fixture where the two
agree cannot distinguish a fixed implementation from a broken one, which is
the whole reason AC4 exists. The loud fallback is pinned just as hard: the one
outcome this must never have is a silent substitution (issue athenaeum#1513).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import mcp_server, push_metrics

# The id frozen into the server process at spawn — the WRONG answer.
STALE_ENV_ID = "848ad5c9-a056-4d0d-b1a7-60c462111b01"
# The id the conversation rotated to and is calling under — the RIGHT answer.
CURRENT_ID = "85134494-83a9-4f90-8d44-1f06c8c75dbd"
SCOPE = "-srv-proj-athenaeum"


# ---------------------------------------------------------------------------
# Fixtures — a synthetic ~/.claude/projects tree. `projects_root` is injectable
# (mirroring `determine_references`) precisely so no test ever reads the
# operator's real transcripts.
# ---------------------------------------------------------------------------


def _tool_use_record(session_id: str, tool_use_id: str) -> str:
    """One assistant record issuing *tool_use_id*, shaped as Claude Code writes it."""
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "looking that up"},
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "mcp__athenaeum__recall",
                        "input": {"query": "deploy target"},
                    },
                ],
            },
        }
    )


def _filler(session_id: str, n: int) -> list[str]:
    return [
        json.dumps({"type": "user", "sessionId": session_id, "message": {"content": "hi"}})
        for _ in range(n)
    ]


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A scope directory holding the stale session's transcript and the
    current one's, with the tool_use record only in the current one."""
    scope = tmp_path / "projects" / SCOPE
    scope.mkdir(parents=True)
    (scope / f"{STALE_ENV_ID}.jsonl").write_text(
        "\n".join(_filler(STALE_ENV_ID, 5)) + "\n", encoding="utf-8"
    )
    (scope / f"{CURRENT_ID}.jsonl").write_text(
        "\n".join(_filler(CURRENT_ID, 3) + [_tool_use_record(CURRENT_ID, "toolu_JOINME")]) + "\n",
        encoding="utf-8",
    )
    return tmp_path / "projects"


@pytest.fixture
def stale_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server process's frozen environment: the id the conversation
    STARTED with, which it has since rotated away from."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE_ENV_ID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    # Pinned off by default so the scope directory resolves through the env
    # id's transcript alone. The tests that exercise the project-dir fallback
    # set it themselves — this keeps every OTHER test from depending on
    # whatever the surrounding runner happened to export.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


# ---------------------------------------------------------------------------
# AC2 — the join, with env and transcript deliberately disagreeing
# ---------------------------------------------------------------------------


class TestToolUseJoin:
    def test_transcript_id_wins_over_a_differing_env_id(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """The load-bearing assertion of this issue. The env says
        `848ad5c9`; the transcript carrying this call's toolUseId says
        `85134494`. The transcript must win."""
        resolver = push_metrics.ToolUseSessionResolver(projects_root=projects_root)

        result = resolver.resolve("toolu_JOINME")

        assert result.session_id == CURRENT_ID
        assert result.session_id != push_metrics.resolve_session_id()
        assert result.attribution == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_a_quoted_tool_use_id_never_wins(
        self, projects_root: Path, stale_env: None, tmp_path: Path
    ) -> None:
        """Transcripts quote each other — an agent that READS a `.jsonl`
        embeds another session's toolUseIds in its own transcript as ordinary
        text. A substring match would join to that decoy; only a structural
        `tool_use` content block counts.

        The decoy is written LAST so it also carries the newest mtime, i.e.
        it wins the candidate ordering and can only be rejected on shape.
        """
        scope = projects_root / SCOPE
        decoy = scope / "cccccccc-0000-0000-0000-00000000cccc.jsonl"
        decoy.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "cccccccc-0000-0000-0000-00000000cccc",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_SOMETHINGELSE",
                                "content": "file contents: ... toolu_JOINME ...",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert decoy.stat().st_mtime >= (scope / f"{CURRENT_ID}.jsonl").stat().st_mtime

        result = push_metrics.ToolUseSessionResolver(projects_root=projects_root).resolve(
            "toolu_JOINME"
        )

        assert result.session_id == CURRENT_ID
        assert result.attribution == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_only_the_tail_of_a_large_transcript_is_read(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """`recall` is the demo's hot path; a multi-MB transcript must never
        be scanned whole. Pinned by making the tail window smaller than the
        file and putting the record inside it."""
        path = projects_root / SCOPE / f"{CURRENT_ID}.jsonl"
        padding = "\n".join(_filler(CURRENT_ID, 4000))
        path.write_text(
            padding + "\n" + _tool_use_record(CURRENT_ID, "toolu_JOINME") + "\n",
            encoding="utf-8",
        )
        assert path.stat().st_size > 4096

        resolver = push_metrics.ToolUseSessionResolver(projects_root=projects_root, tail_bytes=4096)

        assert resolver.resolve("toolu_JOINME").session_id == CURRENT_ID

    def test_a_record_beyond_the_tail_window_is_not_found(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """The complement of the test above — proof the window is real and
        not incidentally reading the whole file. Falling out of the window is
        a MISS, which means the loud fallback, never a guess."""
        path = projects_root / SCOPE / f"{CURRENT_ID}.jsonl"
        path.write_text(
            _tool_use_record(CURRENT_ID, "toolu_JOINME")
            + "\n"
            + "\n".join(_filler(CURRENT_ID, 4000))
            + "\n",
            encoding="utf-8",
        )

        result = push_metrics.ToolUseSessionResolver(
            projects_root=projects_root, tail_bytes=4096, attempts=1
        ).resolve("toolu_JOINME")

        assert result.attribution == push_metrics.ATTRIBUTION_ENV_UNRESOLVED


# ---------------------------------------------------------------------------
# The fallback is LOUD — never a silent substitution (issue athenaeum#1513)
# ---------------------------------------------------------------------------


class TestLoudFallback:
    def test_no_tool_use_id_stamps_unresolved_and_does_no_io(
        self, projects_root: Path, stale_env: None
    ) -> None:
        naps: list[float] = []
        resolver = push_metrics.ToolUseSessionResolver(
            projects_root=projects_root / "does-not-exist",
            sleep=naps.append,
        )

        result = resolver.resolve(None)

        assert result.session_id == STALE_ENV_ID
        assert result.attribution == push_metrics.ATTRIBUTION_ENV_UNRESOLVED
        # No id on the wire is not a race — it is a fact. Nothing to retry.
        assert naps == []

    def test_unfound_id_retries_a_bounded_number_of_times_then_stamps(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """The `tool_use` record is written ~900ms before the call, but that
        is a message timestamp and not a proven fsync — so a miss is retried
        briefly. `attempts` bounds it: the hot path can never block for long,
        and an id that never lands falls back STAMPED."""
        naps: list[float] = []
        resolver = push_metrics.ToolUseSessionResolver(
            projects_root=projects_root, attempts=3, retry_delay=0.01, sleep=naps.append
        )

        result = resolver.resolve("toolu_NEVER_WRITTEN")

        assert result.session_id == STALE_ENV_ID
        assert result.attribution == push_metrics.ATTRIBUTION_ENV_UNRESOLVED
        assert naps == [0.01, 0.01]  # attempts - 1

    def test_a_retry_succeeds_once_the_record_lands(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """The race the retry exists for, played out: the first attempt misses
        and the record appears before the second."""
        path = projects_root / SCOPE / f"{CURRENT_ID}.jsonl"

        def land_the_record(_delay: float) -> None:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_tool_use_record(CURRENT_ID, "toolu_LATE") + "\n")

        resolver = push_metrics.ToolUseSessionResolver(
            projects_root=projects_root, attempts=2, retry_delay=0.0, sleep=land_the_record
        )

        result = resolver.resolve("toolu_LATE")

        assert result.session_id == CURRENT_ID
        assert result.attribution == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_an_unresolvable_scope_falls_back_rather_than_sweeping(
        self, tmp_path: Path, stale_env: None
    ) -> None:
        """With no transcript for the env id there is no scope directory to
        search. The answer is the stamped fallback — NOT an unbounded hunt
        across every scope for something that looks plausible."""
        empty = tmp_path / "projects"
        (empty / "-srv-proj-other").mkdir(parents=True)

        result = push_metrics.ToolUseSessionResolver(projects_root=empty, attempts=1).resolve(
            "toolu_JOINME"
        )

        assert result.session_id == STALE_ENV_ID
        assert result.attribution == push_metrics.ATTRIBUTION_ENV_UNRESOLVED

    def test_scope_still_resolves_after_the_spawn_transcript_rolls_off_disk(
        self, projects_root: Path, stale_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The long-lived-server case, which is the one this issue was filed
        from: servers here have run for days, and Claude Code rolls old
        transcripts off disk. Once the SPAWN id's transcript is gone, a scope
        resolved only from that id stops resolving — and every later recall on
        that connection would fall back for the life of the process, which is
        the stale attribution this fix exists to remove, merely stamped.

        `CLAUDE_PROJECT_DIR` (exported to spawned MCP servers) names the scope
        directly and does not decay.
        """
        (projects_root / SCOPE / f"{STALE_ENV_ID}.jsonl").unlink()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/srv/proj/athenaeum")

        result = push_metrics.ToolUseSessionResolver(projects_root=projects_root).resolve(
            "toolu_JOINME"
        )

        assert result.session_id == CURRENT_ID
        assert result.attribution == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_scope_falls_back_to_the_process_cwd(
        self, projects_root: Path, stale_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backstop for the same failure, for a client that exports no
        `CLAUDE_PROJECT_DIR`: the server's cwd is the project directory."""
        (projects_root / SCOPE / f"{STALE_ENV_ID}.jsonl").unlink()
        monkeypatch.setattr(push_metrics.os, "getcwd", lambda: "/srv/proj/athenaeum")

        result = push_metrics.ToolUseSessionResolver(projects_root=projects_root).resolve(
            "toolu_JOINME"
        )

        assert result.session_id == CURRENT_ID

    def test_a_failed_scope_resolution_is_not_latched_for_the_connection(
        self, projects_root: Path, stale_env: None
    ) -> None:
        """A miss must not be cached. One transient failure on a server that
        lives for days would otherwise disable the join permanently — a
        one-way door on the exact deployment shape this targets."""
        scope = projects_root / SCOPE
        stashed = scope / f"{STALE_ENV_ID}.jsonl"
        contents = stashed.read_text(encoding="utf-8")
        stashed.unlink()

        resolver = push_metrics.ToolUseSessionResolver(projects_root=projects_root, attempts=1)
        assert resolver.resolve("toolu_JOINME").attribution == (
            push_metrics.ATTRIBUTION_ENV_UNRESOLVED
        )

        stashed.write_text(contents, encoding="utf-8")

        assert resolver.resolve("toolu_JOINME").session_id == CURRENT_ID

    def test_scope_dir_name_matches_claude_codes_own_mangling(self) -> None:
        """Read off the real transcript tree, not from documentation: every
        character that is not a letter, digit, or hyphen becomes a hyphen."""
        assert push_metrics._scope_dir_name("/srv/proj/athenaeum") == "-srv-proj-athenaeum"
        assert (
            push_metrics._scope_dir_name("/srv/proj/hestia/.claude/worktrees/agent-1")
            == "-srv-proj-hestia--claude-worktrees-agent-1"
        )

    def test_no_env_id_and_no_join_yields_no_record_at_all(
        self, projects_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unchanged pre-existing behaviour: with nothing to attribute to,
        `record_push` writes nothing rather than inventing an id."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        result = push_metrics.ToolUseSessionResolver(
            projects_root=projects_root, attempts=1
        ).resolve("toolu_MISSING")

        assert result.session_id == ""


# ---------------------------------------------------------------------------
# End to end — what actually lands in the ledger
# ---------------------------------------------------------------------------


def _recall(tmp_path: Path, projects_root: Path, tool_use_id: str | None) -> tuple[Path, Path]:
    """Run one `recall` exactly as the MCP tool does, returning the roots the
    ledger resolves behind (issue athenaeum#980 AC4 puts it under the wiki
    root, so both are needed to read it back)."""
    wiki = tmp_path / "wiki"
    if not wiki.exists():
        wiki.mkdir()
        (wiki / "deploy_target.md").write_text(
            "---\nname: Deploy target\ntype: principle\n---\n\n"
            "The deploy target is the staging cluster.\n",
            encoding="utf-8",
        )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    out = mcp_server.recall_search(
        wiki,
        "deploy target",
        cache_dir=cache_dir,
        tool_use_id=tool_use_id,
        session_resolver=push_metrics.ToolUseSessionResolver(projects_root=projects_root),
    )
    assert "Deploy target" in out
    return wiki, cache_dir


class TestLedgerRow:
    def test_recall_row_carries_the_transcript_id_not_the_env_id(
        self, tmp_path: Path, projects_root: Path, stale_env: None
    ) -> None:
        """AC2, end to end: the row `recall` writes names the session that
        made the call, not the one frozen into the process environment."""
        wiki, cache_dir = _recall(tmp_path, projects_root, "toolu_JOINME")

        [row] = push_metrics.read_push_records(cache_dir, wiki_root=wiki)
        assert row["session_id"] == CURRENT_ID
        assert row["session_id"] != STALE_ENV_ID
        assert row["session_attribution"] == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_a_fallback_row_is_stamped_unresolved(
        self, tmp_path: Path, projects_root: Path, stale_env: None
    ) -> None:
        """The failure mode is recorded, not hidden: the row still carries the
        env id (unchanged behaviour) but says so on its face."""
        wiki, cache_dir = _recall(tmp_path, projects_root, None)

        [row] = push_metrics.read_push_records(cache_dir, wiki_root=wiki)
        assert row["session_id"] == STALE_ENV_ID
        assert row["session_attribution"] == push_metrics.ATTRIBUTION_ENV_UNRESOLVED

    def test_a_non_mcp_caller_writes_no_stamp_at_all(self, tmp_path: Path, stale_env: None) -> None:
        """The `athenaeum recall` CLI and the demo are short-lived processes
        whose `os.environ` is fresh, so their env id is NOT suspect. Stamping
        those rows `env-unresolved` would raise a false alarm on trustworthy
        rows — and issue athenaeum#1542's lane is concurrently reasoning about
        how the viewer classifies push rows. Such a caller attempts no
        attribution and writes no key: absent already means "provenance
        unknown"."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "deploy_target.md").write_text(
            "---\nname: Deploy target\ntype: principle\n---\n\n"
            "The deploy target is the staging cluster.\n",
            encoding="utf-8",
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        mcp_server.recall_search(wiki, "deploy target", cache_dir=cache_dir)

        [row] = push_metrics.read_push_records(cache_dir, wiki_root=wiki)
        assert row["session_id"] == STALE_ENV_ID
        assert "session_attribution" not in row

    def test_attribution_surfaces_in_the_public_tail_contract(
        self, tmp_path: Path, projects_root: Path, stale_env: None
    ) -> None:
        """A stamp nobody can read is not loud. It must reach the same
        `push-metrics tail --json` surface an operator actually looks at."""
        wiki, cache_dir = _recall(tmp_path, projects_root, "toolu_JOINME")

        [shaped] = list(push_metrics.tail_records(cache_dir=cache_dir, wiki_root=wiki))

        assert shaped["record_type"] == "push"
        assert shaped["session_id"] == CURRENT_ID
        assert shaped["session_attribution"] == push_metrics.ATTRIBUTION_TRANSCRIPT

    def test_ac6_no_query_text_or_topics_enter_the_ledger(
        self, tmp_path: Path, projects_root: Path, stale_env: None
    ) -> None:
        """athenaeum#711 still holds — nothing about this change puts query
        text, topics, or the join key itself on a row. Asserted over the
        SERIALIZED row, so a leak anywhere in the object graph is caught, not
        just at the top level."""
        wiki, cache_dir = _recall(tmp_path, projects_root, "toolu_JOINME")

        [row] = push_metrics.read_push_records(cache_dir, wiki_root=wiki)
        blob = json.dumps(row)
        assert "deploy target" not in blob
        assert "toolu_JOINME" not in blob
        assert row["query_hash"]

    def test_ac5_history_is_not_rewritten(
        self, tmp_path: Path, projects_root: Path, stale_env: None
    ) -> None:
        """Forward-only. A pre-existing misattributed row keeps its id AND
        keeps no `session_attribution` key at all — an absent key means
        "provenance unknown", never a retroactive claim in either direction."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "deploy_target.md").write_text(
            "---\nname: Deploy target\ntype: principle\n---\n\n"
            "The deploy target is the staging cluster.\n",
            encoding="utf-8",
        )
        historical = {
            "v": 1,
            "session_id": STALE_ENV_ID,
            "ts": "2026-09-10T03:21:57Z",
            "query_hash": "deadbeefdeadbeef",
            "backend": "vector",
            "items": [{"id": "abc12345", "tier": "internal", "scope": "owner", "token_cost": 9}],
            "pushed_count": 1,
            "token_cost": 9,
            "token_cost_estimated": True,
        }
        ledger = push_metrics.durable_push_records_path(wiki)
        ledger.write_text(json.dumps(historical) + "\n", encoding="utf-8")

        _recall(tmp_path, projects_root, "toolu_JOINME")

        old, new = push_metrics.read_push_records(tmp_path / "cache", wiki_root=wiki)
        assert old == historical
        assert "session_attribution" not in old
        assert new["session_id"] == CURRENT_ID


# ---------------------------------------------------------------------------
# The wire contract this whole fix rests on
# ---------------------------------------------------------------------------


class TestWireContract:
    def test_meta_key_is_the_one_claude_code_actually_sets(self) -> None:
        """Established by raw frame capture and re-confirmed over a real stdio
        transport. Pinned as a test so a rename becomes a visible diff rather
        than a silent return to stale attribution — the same discipline
        `SESSION_ID_ENV_VARS` is held to (issue athenaeum#734)."""
        assert push_metrics.TOOL_USE_ID_META_KEY == "claudecode/toolUseId"

    def test_active_tool_use_id_is_none_outside_a_request(self) -> None:
        """Every in-process caller (the CLI, these tests) has no active MCP
        request. That must be a quiet `None`, never an exception on the recall
        path."""
        assert mcp_server.active_tool_use_id() is None

    def test_recall_tool_schema_does_not_expose_plumbing(self) -> None:
        """The attribution join reads the active request's `_meta` rather than
        taking a `ctx` tool parameter, so the schema the MODEL reads is
        unchanged — no argument it could try to fill in."""
        import inspect

        params = set(inspect.signature(mcp_server.recall_search).parameters)
        assert {"tool_use_id", "session_resolver"} <= params
