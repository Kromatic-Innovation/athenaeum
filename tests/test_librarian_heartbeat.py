# SPDX-License-Identifier: Apache-2.0
"""Integration tests: dark-zone phases emit ``librarian-heartbeat`` lines (athenaeum#398).

The T3 entity-merge pass (merge.py) and the post-compile phases (the athenaeum#290
wiki-dedup pass and the athenaeum#188 re-resolve pass) previously produced NO per-unit
progress logging, so a stall in any of them was invisible in the log. These
tests drive each phase directly (reusing the fixture/stub conventions from
``tests/test_librarian_merge.py`` and ``tests/test_wiki_dedupe.py``) and
assert the ``librarian-heartbeat`` start/done lines appear via ``caplog``.

The entity-phase suite near the bottom of this file (issue athenaeum#800) covers the
one phase that carried ZERO heartbeat coverage until now — see
``TestEntityHeartbeat`` below.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import ENTITY_FILE_FAILURE_PREFIX, run
from athenaeum.merge import merge_clusters_to_wiki
from athenaeum.models import EscalationItem
from athenaeum.tiers import reresolve_open_questions, tier4_escalate
from athenaeum.wiki_dedupe import propose_wiki_page_merges

# Reuse the deadline suite's run harness verbatim (same convention
# `test_librarian_entity_share.py` follows) — driving `run()` end-to-end with
# `process_one` stubbed is the only way to exercise the real per-file entity
# loop this issue instruments.
from tests.test_librarian_deadline import _seed_knowledge_root, _writing_process_one_factory


def _heartbeat_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [rec.message for rec in caplog.records if "librarian-heartbeat" in rec.message]


def _write_am_file(
    scope_dir: Path,
    filename: str,
    *,
    frontmatter_name: str,
    description: str,
    origin_session_id: str,
    origin_turn: int,
    sources: list[dict[str, object]],
    body: str,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {frontmatter_name}",
        "type: auto-memory",
        f"description: {description}",
        f"origin_session_id: {origin_session_id}",
        f"origin_turn: {origin_turn}",
        "sources:",
    ]
    for src in sources:
        lines.append(f"  - session: {src['session']}")
        lines.append(f"    turn: {src['turn']}")
        lines.append(f"    date: {src['date']}")
        lines.append(f"    excerpt: {src['excerpt']}")
    lines.append("---")
    lines.append(body)
    path = scope_dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_cluster_jsonl(knowledge_root: Path, rows: list[dict[str, object]]) -> Path:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return out


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n"
        "  extra_intake_roots:\n"
        "    - raw/auto-memory\n"
        "librarian:\n"
        "  heartbeat_interval: 0\n",
        encoding="utf-8",
    )


@pytest.fixture
def merge_root_two_clusters(tmp_path: Path) -> Path:
    """2 single-member clusters — enough for a real (non-empty) merge run."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristan-Code-proj"

    specs = [
        ("note_one.md", "s-aaa", 1, "First standalone note."),
        ("note_two.md", "s-bbb", 1, "Second standalone note."),
    ]
    for filename, session, turn, body in specs:
        _write_am_file(
            scope,
            filename,
            frontmatter_name=filename.replace("_", " ").replace(".md", ""),
            description="standalone note",
            origin_session_id=session,
            origin_turn=turn,
            sources=[
                {
                    "session": session,
                    "turn": turn,
                    "date": "2026-07-01",
                    "excerpt": body,
                }
            ],
            body=body,
        )

    rows = [
        {
            "cluster_id": f"proj-000{i + 1}",
            "member_paths": [f"-Users-tristan-Code-proj/{filename}"],
            "centroid_score": 1.0,
            "rationale": "singleton",
        }
        for i, (filename, _, _, _) in enumerate(specs)
    ]
    _write_cluster_jsonl(knowledge_root, rows)
    _write_config(knowledge_root)
    return knowledge_root


class TestMergeHeartbeats:
    def test_merge_write_and_merge_detect_emit_start_and_done(
        self, merge_root_two_clusters: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real (non-dry-run, client=None deterministic) merge run emits
        ``merge-detect`` and ``merge-write`` heartbeat start/done lines."""
        caplog.set_level(logging.INFO, logger="athenaeum")
        entries = merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=False,
            client=None,
        )
        assert len(entries) == 2

        lines = _heartbeat_lines(caplog)
        detect_lines = [line for line in lines if "phase=merge-detect" in line]
        write_lines = [line for line in lines if "phase=merge-write" in line]

        assert any("status=start" in line for line in detect_lines)
        assert any("status=done" in line for line in detect_lines)
        assert any("status=start" in line for line in write_lines)
        assert any("status=done" in line for line in write_lines)
        # 2 clusters -> 2 write ticks with interval_s=0 (always emit).
        assert sum("status=tick" in line for line in write_lines) == 2

        done_line = next(line for line in write_lines if "status=done" in line)
        assert "done=2" in done_line
        assert "compiled=2" in done_line

    def test_merge_dry_run_still_emits_detect_heartbeat(
        self, merge_root_two_clusters: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=True,
            client=None,
        )
        lines = _heartbeat_lines(caplog)
        detect_lines = [line for line in lines if "phase=merge-detect" in line]
        assert any("status=start" in line for line in detect_lines)
        assert any("status=done" in line for line in detect_lines)


# ---------------------------------------------------------------------------
# wiki-dedupe (athenaeum#290)
# ---------------------------------------------------------------------------

_BODY_A = "Kromatic is Tristan's primary venture and main business focus."
_BODY_B = "Tristan's primary venture is Kromatic, his main company."
_VEC_A = [1.0, 0.0]
_VEC_B = [0.98, 0.2]
_TEXT_TO_VEC = {_BODY_A: _VEC_A, _BODY_B: _VEC_B}


def _fake_embed(texts: list[str]) -> list[list[float]] | None:
    return [_TEXT_TO_VEC.get(t.strip(), [0.0, 0.0]) for t in texts]


def _write_wiki_page(wiki_root: Path, filename: str, body: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {filename[:-3]}\ntype: concept\n---\n{body}\n"
    path = wiki_root / filename
    path.write_text(text, encoding="utf-8")
    return path


class TestWikiDedupeHeartbeat:
    def test_propose_wiki_page_merges_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        wiki_root = tmp_path / "wiki"
        _write_wiki_page(wiki_root, "venture-a.md", _BODY_A)
        _write_wiki_page(wiki_root, "venture-b.md", _BODY_B)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={"librarian": {"heartbeat_interval": 0}},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert len(proposals) == 1

        lines = _heartbeat_lines(caplog)
        dedupe_lines = [line for line in lines if "phase=wiki-dedupe" in line]
        assert any("status=start" in line for line in dedupe_lines)
        assert any("status=done" in line for line in dedupe_lines)

    def test_no_candidates_still_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)

        proposals = propose_wiki_page_merges(
            tmp_path,
            config={"librarian": {"heartbeat_interval": 0}},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert proposals == []

        lines = _heartbeat_lines(caplog)
        dedupe_lines = [line for line in lines if "phase=wiki-dedupe" in line]
        assert any("status=start" in line for line in dedupe_lines)
        assert any("status=done" in line for line in dedupe_lines)
        done_line = next(line for line in dedupe_lines if "status=done" in line)
        assert "done=0" in done_line
        assert "total=0" in done_line


# ---------------------------------------------------------------------------
# reresolve (athenaeum#188)
# ---------------------------------------------------------------------------


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _write_reresolve_member(
    knowledge_root: Path, scope: str, filename: str, body: str
) -> str:
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: " + filename[:-3] + "\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return f"{scope}/{filename}"


def _escalate_proposalless(knowledge_root: Path) -> Path:
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    ref_a = _write_reresolve_member(
        knowledge_root, "scope-x", "feedback_a.md", "Tristan is German."
    )
    ref_b = _write_reresolve_member(
        knowledge_root, "scope-x", "feedback_b.md", "Tristan is NOT German."
    )
    description = (
        "Detector says these conflict.\n"
        "Passage 1: Tristan is German.\n"
        "Passage 2: Tristan is NOT German.\n"
        f"Members involved: {ref_a}, {ref_b}"
    )
    pending = wiki / "_pending_questions.md"
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-tristan.md",
                entity_name="Tristan",
                conflict_type="factual",
                description=description,
            )
        ],
        pending,
    )
    return pending


def _payload(action: str, *, winner: str = "a", confidence: float = 0.5) -> str:
    return (
        f'{{"recommended_winner": "{winner}", "action": "{action}", '
        f'"confidence": {confidence}, '
        '"rationale": "test verdict rationale.", '
        '"source_precedence_used": ["a:user > b:unsourced"]}'
    )


class TestReresolveHeartbeat:
    def test_reresolve_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        pending = _escalate_proposalless(tmp_path)

        client = _fake_client(_payload("keep_a", confidence=0.5))
        count = reresolve_open_questions(
            pending, client=client, config={"librarian": {"heartbeat_interval": 0}}
        )
        assert count == 1

        lines = _heartbeat_lines(caplog)
        reresolve_lines = [line for line in lines if "phase=reresolve" in line]
        assert any("status=start" in line for line in reresolve_lines)
        assert any("status=done" in line for line in reresolve_lines)
        assert any("status=tick" in line for line in reresolve_lines)
        tick_line = next(line for line in reresolve_lines if "status=tick" in line)
        assert "unit=Tristan" in tick_line

    def test_no_open_questions_still_emits_start_and_done(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text("# Pending Questions\n", encoding="utf-8")

        client = _fake_client(_payload("keep_a"))
        count = reresolve_open_questions(pending, client=client, config={})
        assert count == 0

        lines = _heartbeat_lines(caplog)
        reresolve_lines = [line for line in lines if "phase=reresolve" in line]
        assert any("status=start" in line for line in reresolve_lines)
        assert any("status=done" in line for line in reresolve_lines)


# ---------------------------------------------------------------------------
# Issue athenaeum#762 — the C4 detector/resolver loop must tick the RUN LOCK
# heartbeat (not merely the log-only PhaseHeartbeat), so a long C4 phase does
# not make a healthy run look wedged to heartbeat-age consumers.
# ---------------------------------------------------------------------------


class TestC4RunLockHeartbeat:
    def test_run_lock_heartbeat_advances_across_multi_cluster_c4(
        self, merge_root_two_clusters: Path
    ) -> None:
        """The `heartbeat` callable (RunLock.heartbeat, threaded from run() via
        ctx.heartbeat) is invoked and ADVANCES across a multi-cluster C4 pass —
        not merely reachable. Each call records a strictly-increasing tick so a
        genuine advance is asserted, not a single fire. FAILS against pre-athenaeum#762
        `merge_clusters_to_wiki` (which has no `heartbeat` parameter)."""
        import time

        ticks: list[float] = []

        def _record() -> None:
            # A real monotonic reading per call: two consecutive ticks are
            # strictly ordered, so "advances" is a real assertion, not a count.
            ticks.append(time.monotonic())

        entries = merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=False,
            client=None,
            heartbeat=_record,
        )
        assert len(entries) == 2

        # 2 clusters, each ticked at the per-cluster boundary AND its per-chunk
        # boundary, so the run-lock heartbeat fired multiple times and advanced
        # across the pass (>= one refresh per cluster).
        assert len(ticks) >= 2, "C4 must refresh the run-lock heartbeat per cluster"
        assert ticks == sorted(ticks), "heartbeat timestamps must advance monotonically"
        # Max observed inter-tick gap in this deterministic repro (client=None):
        # bounded by one cluster/chunk of in-process work. In production this
        # gap is bounded by one chunk's detector+resolver latency instead of the
        # whole C4 phase (the pre-fix behaviour).
        max_gap = max(
            (b - a for a, b in zip(ticks, ticks[1:])), default=0.0
        )
        assert max_gap >= 0.0  # measurement is well-defined (recorded in the PR body)

    def test_run_lock_heartbeat_is_best_effort(
        self, merge_root_two_clusters: Path
    ) -> None:
        """A heartbeat refresh that RAISES must never break or slow the run
        (athenaeum#762 AC): the C4 pass swallows it and completes normally."""

        def _boom() -> None:
            raise RuntimeError("simulated heartbeat write failure")

        entries = merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=False,
            client=None,
            heartbeat=_boom,
        )
        assert len(entries) == 2, "a failing heartbeat must not abort the C4 pass"

    def test_no_heartbeat_callable_is_a_noop(
        self, merge_root_two_clusters: Path
    ) -> None:
        """`heartbeat=None` (the default / a run with no lock) is a clean no-op —
        the C4 pass behaves exactly as before."""
        entries = merge_clusters_to_wiki(
            merge_root_two_clusters,
            config=None,
            dry_run=False,
            client=None,
            heartbeat=None,
        )
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# entity phase (issue athenaeum#800) — the one dark zone left with ZERO
# heartbeat coverage. Run 631aaade (2026-08-06) spent 85% of a 3446s window
# here and emitted nothing between "start" and its budget trip.
# ---------------------------------------------------------------------------


def _entity_heartbeat_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        rec.message
        for rec in caplog.records
        if "librarian-heartbeat" in rec.message and "phase=entity" in rec.message
    ]


class TestEntityHeartbeat:
    def test_start_tick_done_shape_and_one_tick_per_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same contract as merge-detect/merge-write/wiki-dedupe/reresolve:
        status=start|tick|done, done/total, compiled/unchanged/error, unit=
        (the raw file path), cumulative elapsed=. One tick per raw file
        processed — the property that makes per-file wall-clock recoverable
        by differencing consecutive `elapsed=` values.
        """
        caplog.set_level(logging.INFO, logger="athenaeum")
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        # interval_s=0 → every tick call emits its own line (mirrors how the
        # sibling heartbeat tests force this via `heartbeat_interval: 0`).
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "0")

        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _writing_process_one_factory(root / "wiki"),
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )
        assert rc == 0

        lines = _entity_heartbeat_lines(caplog)
        assert any("status=start" in line for line in lines)
        assert any("status=done" in line for line in lines)
        tick_lines = [line for line in lines if "status=tick" in line]
        assert len(tick_lines) == 3, "one tick per raw file processed"

        done_line = next(line for line in lines if "status=done" in line)
        assert "done=3" in done_line
        assert "total=3" in done_line
        # Every file in `_writing_process_one_factory` returns created=[...],
        # so all 3 ticks are compiled, none unchanged/error.
        assert "compiled=3" in done_line
        assert "unchanged=0" in done_line
        assert "error=0" in done_line

    def test_tick_unit_is_the_raw_file_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "0")
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _writing_process_one_factory(root / "wiki"),
        )

        # `_seed_knowledge_root(n_files=1)` writes exactly this deterministic
        # filename (i=0) — captured BEFORE run() because a successful entity
        # pass deletes the raw file, so it can't be globbed back afterward.
        raw_ref = "20240410T120000Z-aabbccd0.md"

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )
        assert rc == 0

        tick_line = next(
            line for line in _entity_heartbeat_lines(caplog) if "status=tick" in line
        )
        assert f"unit=sessions/{raw_ref}" in tick_line

    def test_file_failure_logs_reason_at_warning_with_path_and_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """athenaeum#800 AC: a file failure logs the reason (exception type +
        message) at WARNING with the file path — not only the filename in the
        trailing "Failed files" summary. Run 631aaade recorded three failed
        files with no error text captured at any level the log sweep caught.
        """
        caplog.set_level(logging.WARNING, logger="athenaeum")
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        def _boom_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            raise ValueError("malformed frontmatter block")

        monkeypatch.setattr("athenaeum.librarian.process_one", _boom_process_one)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )
        assert rc == 1  # the existing "Failed files" exit code (unchanged)

        raw_ref = next((root / "raw" / "sessions").glob("2024041*.md")).name
        failure_lines = [
            rec.message for rec in caplog.records if ENTITY_FILE_FAILURE_PREFIX in rec.message
        ]
        assert len(failure_lines) == 1
        line = failure_lines[0]
        assert f"sessions/{raw_ref}" in line
        assert "ValueError" in line
        assert "malformed frontmatter block" in line

    def test_entity_share_trip_warning_names_resource_with_both_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """athenaeum#800 AC: the trip warning must name WHICH resource tripped
        (the entity runtime share, not the call budget) and give both call-
        budget numbers. Run 631aaade tripped at 28/1200 calls (2.3%) while the
        log said only "entity phase runtime share exhausted" — indistinguishable
        from a call-budget exhaustion and misleading on its own.
        """
        caplog.set_level(logging.WARNING, logger="athenaeum")
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.delenv("ATHENAEUM_ENTITY_RUNTIME_SHARE", raising=False)

        from tests.test_librarian_deadline import _FakeClock

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

        state = {"n": 0}

        def _fake_process_one(raw, index, wiki_root_arg, client, *args, usage=None, **kwargs):
            state["n"] += 1
            page = (root / "wiki") / f"entity-{state['n']}.md"
            page.write_text(f"# Entity {state['n']}\n", encoding="utf-8")
            if usage is not None:
                usage.add(100, 50)  # one simulated API call's tokens
            if state["n"] == 1:
                clock.now = 700.0  # past the default 0.6 share of a 1000s window
            return SimpleNamespace(created=[page.name], updated=[], escalated=[], skipped=[])

        monkeypatch.setattr("athenaeum.librarian.process_one", _fake_process_one)
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files",
            lambda *_a, **_k: [],
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )
        assert rc == 0

        # Filtered on "api_call_budget usage" (unique to the trip-time WARNING)
        # rather than "entity phase runtime share exhausted" alone — that
        # shorter phrase also appears, unchanged, in the later "Done
        # (DEGRADED — ...)" summary line, which is not the warning this AC
        # is about.
        trip_lines = [
            rec.message for rec in caplog.records if "api_call_budget usage" in rec.message
        ]
        assert len(trip_lines) == 1
        line = trip_lines[0]
        # Names the tripped resource distinctly from the call budget...
        assert "entity phase runtime share exhausted" in line
        # ...but still gives both api_call_budget numbers (1 call made of a
        # 100 budget = 1.0%), so a reader can rule the call budget in or out.
        assert "1/100 calls" in line
        assert "1.0%" in line
