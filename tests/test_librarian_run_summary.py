# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#464 (slice E of athenaeum#460) — permanent per-phase run summary.

Pure observability: `run()` emits ONE machine-greppable `librarian-run-summary`
line at the end of every exit path (the clean finalize AND every `_stop_on_deadline`
124 trip) covering the phases that actually ran, with wall-clock seconds, LLM
call counts (detector/resolver/entity-tier), and work counts per phase.

This suite covers:

1. The summary line appears on a clean run and carries the expected phase
   tokens (`entity secs=`, `auto-memory ... detector_haiku=`/`resolver_opus=`).
2. The summary line appears on a 124 (deadline) exit, covering only the
   phases that ran before the trip — reusing the `_FakeClock` / stubbed-slow-
   compile pattern from `tests/test_librarian_deadline.py`.
3. The merge detector/resolver counts are threaded correctly from
   `merge_clusters_to_wiki`'s new `out_stats` param up through the summary
   line (both a focused `out_stats` unit test and an end-to-end check via a
   mocked detector/resolver client).
4. No behavior change: exit code / created counts are unchanged by the new
   observability-only code path.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import _render_run_summary, run
from athenaeum.merge import RunDeadlineExceeded, merge_clusters_to_wiki

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_librarian_merge.py's fixture conventions)
# ---------------------------------------------------------------------------


def _write_am_file(
    scope_dir: Path,
    filename: str,
    *,
    frontmatter_name: str,
    description: str = "",
    origin_session_id: str | None = None,
    origin_turn: int | None = None,
    sources: list[dict[str, object]] | None = None,
    body: str = "",
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    meta_lines = [
        "---",
        f"name: {frontmatter_name}",
        f"description: {description}",
        "type: feedback",
    ]
    if origin_session_id is not None:
        meta_lines.append(f"originSessionId: {origin_session_id}")
    if origin_turn is not None:
        meta_lines.append(f"originTurn: {origin_turn}")
    if sources:
        meta_lines.append("sources:")
        for s in sources:
            meta_lines.append(f"  - session: {s['session']}")
            if "turn" in s:
                meta_lines.append(f"    turn: {s['turn']}")
            if "date" in s:
                meta_lines.append(f"    date: {s['date']}")
            if "excerpt" in s:
                meta_lines.append(f'    excerpt: "{s["excerpt"]}"')
    meta_lines.append("---")
    text = "\n".join(meta_lines) + "\n" + body + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


def _write_cluster_jsonl(knowledge_root: Path, rows: list[dict[str, object]]) -> Path:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return out


def _summary_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        rec.message for rec in caplog.records if "librarian-run-summary" in rec.message
    ]


class _FakeClock:
    """Hand-advanced monotonic clock (mirrors test_librarian_deadline.py)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _seed_knowledge_root(tmp_path: Path, n_files: int = 0) -> Path:
    """Minimal knowledge root: wiki/_schema, raw/sessions/, git repo."""
    root = tmp_path / "knowledge"
    root.mkdir()
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n"
    )
    (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    for i in range(n_files):
        (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
            f"Met with Alice Zhang about topic {i} at Acme Corp.\n"
        )
    return root


# ---------------------------------------------------------------------------
# 3a. `_render_run_summary` unit tests
# ---------------------------------------------------------------------------


class TestRenderRunSummary:
    def test_empty_profile_still_has_prefix_and_total(self) -> None:
        line = _render_run_summary([])
        assert line.startswith("librarian-run-summary total_secs=0.000")

    def test_only_ran_phases_are_included(self) -> None:
        profile = [
            ("wiki-dedup", 0.1, {}),
            ("entity", 4.2, {"calls": 6, "created": 2}),
        ]
        line = _render_run_summary(profile)
        assert "wiki-dedup secs=0.100" in line
        assert "entity secs=4.200 calls=6 created=2" in line
        # A phase that never ran (e.g. auto-memory, on an early trip) is
        # simply absent — not rendered as a zero-valued phase.
        assert "auto-memory" not in line

    def test_total_secs_sums_phase_seconds(self) -> None:
        profile = [("a", 1.0, {}), ("b", 2.5, {})]
        line = _render_run_summary(profile)
        assert "total_secs=3.500" in line

    def test_degraded_count_surfaced_when_present(self) -> None:
        # Issue athenaeum#472: when Tier-2 classification dropped all entities for some
        # files (unparseable JSON, even after repair + retry), the count rides
        # the entity phase so an operator sees it in the summary line instead
        # of grepping warnings.
        profile = [
            ("entity", 4.2, {"calls": 6, "created": 2, "files": 5, "degraded": 3}),
        ]
        line = _render_run_summary(profile)
        assert "degraded=3" in line

    def test_degraded_absent_on_clean_run(self) -> None:
        # A clean entity phase (the run loop omits degraded when 0) renders no
        # degraded token — the clean-run summary line is unchanged.
        profile = [("entity", 4.2, {"calls": 6, "created": 2, "files": 5})]
        line = _render_run_summary(profile)
        assert "degraded" not in line

    def test_out_tok_per_call_rendered_in_entity_segment(self) -> None:
        # Issue athenaeum#490 (slice A): output-tokens-per-call rides the entity
        # segment so the silent full-page-echo fallback's ~10x output-cost
        # spike is visible in the one-line summary without a by-hand ratio
        # calc. The value is a known figure derived for this call set.
        profile = [
            (
                "entity",
                4.2,
                {"calls": 6, "created": 2, "files": 5, "out_tok_per_call": 2750},
            ),
        ]
        line = _render_run_summary(profile)
        assert "out_tok_per_call=2750" in line
        # Renders in dict order — after files, before any degraded/truncated.
        assert (
            "entity secs=4.200 calls=6 created=2 files=5 out_tok_per_call=2750"
            in line
        )


# ---------------------------------------------------------------------------
# 1. Clean run: summary present, covers the phases that ran
# ---------------------------------------------------------------------------


class TestCleanRunSummary:
    def test_summary_present_with_entity_and_automemory_tokens(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A clean run (entity intake + auto-memory corpus, both real, no
        client) emits ONE librarian-run-summary line carrying the entity
        and auto-memory phase tokens the issue calls out explicitly."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        # A second raw file's classify/create mocked responses so the entity
        # loop actually creates something (feeds `created=` > 0).
        import anthropic as anthropic_mod

        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Alice Zhang",
                            "entity_type": "person",
                            "tags": ["active"],
                            "access": "internal",
                            "observations": "Product leader.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# Alice Zhang\n\nProduct leader.")]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            classify_response,
            create_response,
        ]
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        # A minimal auto-memory scope so the auto-memory phase runs too (no
        # client calls needed there: a single-member cluster with no C4
        # client stays deterministic, matching TestContradictionFixture's
        # "no client means no flag" no-op path).
        scope = root / "raw" / "auto-memory" / "-Users-tristan-Code-proj"
        _write_am_file(
            scope,
            "feedback_note_one.md",
            frontmatter_name="note one",
            description="standalone note",
            origin_session_id="s-aaa",
            origin_turn=1,
            sources=[
                {
                    "session": "s-aaa",
                    "turn": 1,
                    "date": "2026-07-01",
                    "excerpt": "standalone",
                }
            ],
            body="A standalone note.",
        )
        (root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
            encoding="utf-8",
        )

        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == 0
        lines = _summary_lines(caplog)
        assert len(lines) == 1, "exactly one summary line must be emitted per run"
        line = lines[0]
        assert line.startswith("librarian-run-summary total_secs=")
        assert "entity secs=" in line
        assert "auto-memory" in line
        assert "detector_haiku=" in line
        assert "resolver_opus=" in line
        assert "created=1" in line
        assert "files=1" in line


# ---------------------------------------------------------------------------
# 2. 124 (deadline) exit: summary covers only the phases that ran
# ---------------------------------------------------------------------------


class TestDeadlineExitSummary:
    def test_wiki_dedup_deadline_trip_emits_summary_without_later_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Reuses the athenaeum#396 wiki-dedup-phase-boundary trip: the deadline blows
        immediately after the athenaeum#290 wiki-dedup pass, so the summary must
        contain `wiki-dedup` but NOT `entity` or `auto-memory` (they never
        ran)."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

        def _slow_dedup(*_a, **_k) -> None:
            clock.now = 5000.0

        monkeypatch.setattr(
            "athenaeum.wiki_dedupe.propose_wiki_page_merges", _slow_dedup
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )

        assert rc == 124
        lines = _summary_lines(caplog)
        assert len(lines) == 1, "exactly one summary line must be emitted on a 124 exit"
        line = lines[0]
        assert "wiki-dedup secs=" in line
        # Assert on the phase SEGMENT, not a bare "entity" substring: issue athenaeum#567
        # adds a `schema_fragments=…,_entity-template:…` attribution token to the
        # head, so "entity" now legitimately appears there. The intent here is
        # that the entity PHASE never ran — i.e. no `entity secs=` segment.
        assert "entity secs=" not in line
        assert "auto-memory secs=" not in line

    def test_entity_loop_deadline_trip_summary_has_entity_not_automemory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The entity loop itself trips the deadline (AC2 of athenaeum#461's suite):
        the summary must include `entity` (it ran, partially) but the
        auto-memory block never got a turn."""
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

        state = {"n": 0}

        def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            state["n"] += 1
            page = wiki_root_arg / f"entity-{state['n']}.md"
            page.write_text(f"# Entity {state['n']}\n", encoding="utf-8")
            if state["n"] == 1:
                clock.now = 5000.0
            return SimpleNamespace(
                created=[page.name], updated=[], escalated=[], skipped=[]
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files",
            lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
        )
        compile_calls: list[object] = []
        monkeypatch.setattr(
            "athenaeum.librarian._compile_auto_memory",
            lambda *a, **k: compile_calls.append((a, k)) or [],
        )

        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )

        assert rc == 124
        assert compile_calls == [], "auto-memory must never run once entity trips"
        lines = _summary_lines(caplog)
        assert len(lines) == 1
        line = lines[0]
        assert "entity secs=" in line
        assert "created=1" in line
        assert "auto-memory" not in line

    def test_automemory_deadline_trip_summary_has_both_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Mirrors test_run_catches_merge_deadline_and_exits_124: entity
        completes cleanly (empty intake), then the auto-memory compile trips
        RunDeadlineExceeded. The summary must show BOTH `entity` (it ran, as
        a no-op) and `auto-memory` (it started and partially ran)."""
        root = _seed_knowledge_root(tmp_path, n_files=0)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files",
            lambda *_a, **_k: [SimpleNamespace(origin_scope="scope-a")],
        )

        def _boom(*_a, **_k):
            (root / "wiki" / "auto-partial.md").write_text(
                "---\nname: partial\n---\npartial C3 output\n", encoding="utf-8"
            )
            raise RunDeadlineExceeded("C4 contradiction detector / resolver")

        monkeypatch.setattr("athenaeum.librarian._compile_auto_memory", _boom)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=3600,
        )

        assert rc == 124
        lines = _summary_lines(caplog)
        assert len(lines) == 1
        line = lines[0]
        assert "entity secs=" in line
        assert "auto-memory secs=" in line
        assert "detector_haiku=0" in line


# ---------------------------------------------------------------------------
# 3. Merge detector/resolver counts thread correctly
# ---------------------------------------------------------------------------


class TestMergeOutStatsThreading:
    def test_out_stats_populated_on_dry_run_return(self, tmp_path: Path) -> None:
        """The dry-run return site (~line 2170 in merge.py) also populates
        out_stats — not just the normal-write return site."""
        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristan-Code-proj"
        _write_am_file(
            scope,
            "note_one.md",
            frontmatter_name="note one",
            origin_session_id="s-aaa",
            origin_turn=1,
            sources=[
                {"session": "s-aaa", "turn": 1, "date": "2026-07-01", "excerpt": "x"}
            ],
            body="A standalone note.",
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "proj-0001",
                    "member_paths": ["-Users-tristan-Code-proj/note_one.md"],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        _write_config(knowledge_root)

        out_stats: dict = {}
        entries = merge_clusters_to_wiki(
            knowledge_root, dry_run=True, out_stats=out_stats
        )
        assert len(entries) == 1
        assert out_stats["haiku_calls"] == 0
        assert out_stats["resolve_calls"] == 0
        assert out_stats["pairs_added_via_similarity"] == 0
        assert out_stats["entries_merged"] == 1
        assert out_stats["escalations_written"] == 0

    def test_out_stats_reports_detector_and_resolver_calls(
        self, tmp_path: Path
    ) -> None:
        """A detector-positive + resolver-confirmed-not-a-conflict pair
        drives haiku_calls=1, resolve_calls=1 — and out_stats must report
        exactly that (the merge.py unit-test analog of the athenaeum#464 wiring)."""
        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"
        _write_am_file(
            scope,
            "feedback_v1.md",
            frontmatter_name="v1",
            origin_session_id="s-111",
            origin_turn=1,
            sources=[
                {"session": "s-111", "turn": 1, "date": "2026-04-10", "excerpt": "x"}
            ],
            body="Commit prior-session debris directly to develop.",
        )
        _write_am_file(
            scope,
            "feedback_v2.md",
            frontmatter_name="v2",
            origin_session_id="s-222",
            origin_turn=2,
            sources=[
                {"session": "s-222", "turn": 2, "date": "2026-04-11", "excerpt": "y"}
            ],
            body="Park prior-session debris on a WIP branch.",
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "code-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code/feedback_v1.md",
                        "-Users-tristankromer-Code/feedback_v2.md",
                    ],
                    "centroid_score": 0.62,
                    "rationale": "cosine >= 0.55",
                }
            ],
        )
        _write_config(knowledge_root)

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_v1.md", '
            '"-Users-tristankromer-Code/feedback_v2.md"], '
            '"conflicting_passages": ["a", "b"], '
            '"rationale": "conflict"}'
        )
        resolver_payload = (
            '{"recommended_winner": "neither", "action": "not_a_conflict", '
            '"confidence": 0.91, "rationale": "different scenarios", '
            '"source_precedence_used": []}'
        )

        def _resp(text: str) -> MagicMock:
            r = MagicMock()
            r.content = [MagicMock(text=text)]
            return r

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _resp(detector_payload),
            _resp(resolver_payload),
        ]

        out_stats: dict = {}
        entries = merge_clusters_to_wiki(
            knowledge_root, client=fake_client, out_stats=out_stats
        )
        assert len(entries) == 1
        assert out_stats["haiku_calls"] == 1
        assert out_stats["resolve_calls"] == 1
        assert out_stats["entries_merged"] == 1
        # not_a_conflict clears the flag and writes no escalation.
        assert out_stats["escalations_written"] == 0

    def test_default_out_stats_none_is_backward_compatible(
        self, tmp_path: Path
    ) -> None:
        """Every pre-athenaeum#464 caller omits out_stats — must stay byte-identical
        (no error, no extra side effect)."""
        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristan-Code-proj"
        _write_am_file(
            scope,
            "note_one.md",
            frontmatter_name="note one",
            origin_session_id="s-aaa",
            origin_turn=1,
            sources=[
                {"session": "s-aaa", "turn": 1, "date": "2026-07-01", "excerpt": "x"}
            ],
            body="A standalone note.",
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "proj-0001",
                    "member_paths": ["-Users-tristan-Code-proj/note_one.md"],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        _write_config(knowledge_root)

        entries = merge_clusters_to_wiki(knowledge_root)  # no out_stats kwarg
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# 4. No behavior change
# ---------------------------------------------------------------------------


class TestNoBehaviorChange:
    def test_max_api_calls_stops_processing_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Regression pin: the pre-existing #6 budget-exhaustion behavior
        (and its log message) is unaffected by the new summary line —
        exercised alongside asserting the summary line ALSO appears."""
        root = _seed_knowledge_root(tmp_path, n_files=0)
        sessions = root / "raw" / "sessions"
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Met with Alice Zhang about product strategy.\n"
        )
        (sessions / "20240410T130000Z-11223344.md").write_text(
            "Discussed innovation accounting methodology in detail.\n"
        )

        import anthropic as anthropic_mod

        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Alice Zhang",
                            "entity_type": "person",
                            "tags": ["active"],
                            "access": "internal",
                            "observations": "Product leader.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# Alice Zhang\n\nProduct leader.")]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [classify_response, create_response]
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.DEBUG, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=2,
        )

        # Unchanged pre-athenaeum#464 behavior: budget message still fires, exit 0.
        assert rc == 0
        assert any(
            "budget exhausted" in rec.message.lower()
            or "API call budget" in rec.message
            for rec in caplog.records
        ), "Expected budget exhaustion log message"
        # AND the new observability line is present alongside it.
        assert _summary_lines(caplog), "summary line must also be emitted"


# ---------------------------------------------------------------------------
# 5. Output-tokens-per-call (issue athenaeum#490, slice A)
# ---------------------------------------------------------------------------


class TestEntityOutputTokensPerCall:
    """The entity segment carries output-tokens-per-call, computed as the
    phase's output-token delta // its call delta — the figure that makes the
    silent full-page-echo fallback (a ~10x output-cost degrade) visible in the
    run summary. Correct for a known call set, and 0-guarded on no calls."""

    def _entity_field(self, line: str, key: str) -> int:
        # Parse `key=<int>` out of the entity segment of the summary line.
        entity_seg = next(
            seg for seg in line.split(" | ") if seg.startswith("entity ")
        )
        token = next(t for t in entity_seg.split() if t.startswith(f"{key}="))
        return int(token.split("=", 1)[1])

    def test_entity_segment_reports_output_tokens_per_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fake entity loop adds a KNOWN output-token and call delta to the
        run's usage; the summary's entity ``out_tok_per_call`` must equal the
        injected output delta // the reported call delta."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        # process_one is the sole source of entity-phase usage here: it bumps
        # api_calls by 4 and output_tokens by 2400 for the one file. Any extra
        # api_calls the loop makes (indexing, etc.) only widen the denominator,
        # so we assert against the REPORTED call count, not a hard-coded 4.
        def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            usage = kwargs["usage"]
            usage.api_calls += 4
            usage.output_tokens += 2400
            page = wiki_root_arg / "entity-alice.md"
            page.write_text("# Alice\n", encoding="utf-8")
            return SimpleNamespace(
                created=[page.name], updated=[], escalated=[], skipped=[]
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)
        # Keep the auto-memory phase out of the way (it runs AFTER the entity
        # snapshot, but stubbing it keeps the test hermetic and fast).
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files", lambda *_a, **_k: []
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == 0
        lines = _summary_lines(caplog)
        assert len(lines) == 1
        line = lines[0]
        assert "out_tok_per_call=" in line, line
        calls = self._entity_field(line, "calls")
        out_per_call = self._entity_field(line, "out_tok_per_call")
        assert calls >= 4
        # 2400 output tokens spread across the phase's `calls` calls.
        assert out_per_call == 2400 // calls, line

    def test_out_tok_per_call_is_zero_when_no_entity_calls(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No entity intake => no calls => out_tok_per_call renders 0, never a
        divide-by-zero."""
        root = _seed_knowledge_root(tmp_path, n_files=0)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files", lambda *_a, **_k: []
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == 0
        line = _summary_lines(caplog)[0]
        assert self._entity_field(line, "out_tok_per_call") == 0
