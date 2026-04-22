# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-memory merge pass (C3, issue #197).

Covers :mod:`athenaeum.merge`. All fixtures synthesize a full
``raw/auto-memory/<scope>/`` tree plus a pre-written cluster JSONL
under ``tmp_path`` — the real ``~/knowledge/`` is never touched.

Load-bearing fixtures:

- ``voltaire_merge_root`` — 5 voltaire/nanoclaw files with real citation
  frontmatter under one cluster row. Regression guarantee: exactly one
  ``wiki/auto-voltaire*.md`` with 5 sources carrying session/turn/scope.
- ``contradiction_merge_root`` — two opposing-guidance files in one
  low-cohesion cluster. Must emit a single wiki entry with
  ``contradictions_detected: true`` in frontmatter.
- ``session_turn_dedupe_root`` — one file whose ``sources[]`` contains
  two different-turn-same-session entries plus a duplicate-turn entry.
  Asserts ``(session, turn)`` key, not ``(session, date)``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.merge import (
    AUTO_WIKI_PREFIX,
    CONTRADICTION_COHESION_THRESHOLD,
    dedupe_sources,
    derive_topic_slug,
    merge_clusters_to_wiki,
    read_cluster_rows,
    resolve_member_path,
    synthesize_body,
)
from athenaeum.models import parse_frontmatter

# ---------------------------------------------------------------------------
# Helpers
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
    """Write an auto-memory markdown file with full citation frontmatter."""
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
                meta_lines.append(f"    excerpt: \"{s['excerpt']}\"")
    meta_lines.append("---")
    text = "\n".join(meta_lines) + "\n" + body + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


def _write_cluster_jsonl(
    knowledge_root: Path, rows: list[dict[str, object]],
) -> Path:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def voltaire_merge_root(tmp_path: Path) -> Path:
    """5 voltaire/nanoclaw files + matching cluster JSONL (one cluster, 5 members)."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-voltaire"

    specs = [
        ("project_voltaire_nanoclaw.md", "s-aaa", 1, "Voltaire+nanoclaw"),
        (
            "project_voltaire_iMessage_channel.md",
            "s-bbb", 2,
            "Voltaire iMessage channel via NanoClaw",
        ),
        (
            "project_nanoclaw_voltaire_tickle.md",
            "s-ccc", 3,
            "Nanoclaw ticklestick voltaire",
        ),
        (
            "project_voltaire_sessions.md",
            "s-ddd", 4,
            "Voltaire sessions via box-claude",
        ),
        ("project_voltair_nanoclaw.md", "s-eee", 5, "Voltair typo clone"),
    ]
    for filename, session, turn, body in specs:
        _write_am_file(
            scope, filename,
            frontmatter_name=filename.replace("_", " ").replace(".md", ""),
            description="voltaire toolchain note",
            origin_session_id=session,
            origin_turn=turn,
            sources=[{
                "session": session,
                "turn": turn,
                "date": f"2026-04-{10 + turn:02d}",
                "excerpt": body,
            }],
            body=body,
        )

    member_paths = [f"-Users-tristankromer-Code-voltaire/{s[0]}" for s in specs]
    _write_cluster_jsonl(knowledge_root, [
        {
            "cluster_id": "voltaire-0001",
            "member_paths": member_paths,
            "centroid_score": 0.88,
            "rationale": "cosine >= 0.55; shares tokens: voltaire, nanoclaw",
        },
    ])
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def contradiction_merge_root(tmp_path: Path) -> Path:
    """Two opposing-guidance feedback files in one low-cohesion cluster."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

    _write_am_file(
        scope, "feedback_prior_session_debris_v1.md",
        frontmatter_name="Prior session debris v1",
        description="commit directly",
        origin_session_id="s-111", origin_turn=1,
        sources=[{"session": "s-111", "turn": 1, "date": "2026-04-10",
                  "excerpt": "commit to develop, do not park"}],
        body="Commit prior-session debris directly to develop. Do not park on WIP.",
    )
    _write_am_file(
        scope, "feedback_prior_session_debris_v2.md",
        frontmatter_name="Prior session debris v2",
        description="park on WIP",
        origin_session_id="s-222", origin_turn=2,
        sources=[{"session": "s-222", "turn": 2, "date": "2026-04-11",
                  "excerpt": "park on WIP, do not commit"}],
        body="Park prior-session debris on a WIP branch. Do not commit directly.",
    )

    _write_cluster_jsonl(knowledge_root, [
        {
            "cluster_id": "code-0001",
            "member_paths": [
                "-Users-tristankromer-Code/feedback_prior_session_debris_v1.md",
                "-Users-tristankromer-Code/feedback_prior_session_debris_v2.md",
            ],
            # Below the 0.75 cohesion threshold → contradictions flag fires.
            "centroid_score": 0.62,
            "rationale": "cosine >= 0.55; shares tokens: prior, session, debris",
        },
    ])
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def session_turn_dedupe_root(tmp_path: Path) -> Path:
    """One file whose sources[] stresses the (session, turn) dedupe key."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "scope-x"

    _write_am_file(
        scope, "feedback_dedupe_probe.md",
        frontmatter_name="Dedupe probe",
        description="probe",
        origin_session_id="s-shared", origin_turn=1,
        sources=[
            {"session": "s-shared", "turn": 1, "date": "2026-04-10",
             "excerpt": "turn 1"},
            # Same session, different turn — MUST NOT collapse.
            {"session": "s-shared", "turn": 2, "date": "2026-04-10",
             "excerpt": "turn 2"},
            # Same session+turn, different date — MUST collapse into turn-1.
            {"session": "s-shared", "turn": 1, "date": "2026-04-11",
             "excerpt": "turn 1 duplicate"},
        ],
        body="Dedupe probe body.",
    )

    _write_cluster_jsonl(knowledge_root, [
        {
            "cluster_id": "scope-x-0001",
            "member_paths": ["scope-x/feedback_dedupe_probe.md"],
            "centroid_score": 1.0,
            "rationale": "singleton",
        },
    ])
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def singleton_merge_root(tmp_path: Path) -> Path:
    """Two unrelated size-1 clusters — both MUST emit wiki entries."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "scope-x"

    _write_am_file(
        scope, "reference_dns_flakiness.md",
        frontmatter_name="DNS flakiness",
        description="macOS dns",
        origin_session_id="s-dns", origin_turn=1,
        sources=[{"session": "s-dns", "turn": 1, "date": "2026-04-10",
                  "excerpt": "dns"}],
        body="mDNSResponder flakes.",
    )
    _write_am_file(
        scope, "user_tristan_profile.md",
        frontmatter_name="Tristan profile",
        description="profile",
        origin_session_id="s-prof", origin_turn=1,
        sources=[{"session": "s-prof", "turn": 1, "date": "2026-04-10",
                  "excerpt": "profile"}],
        body="Consultant.",
    )

    _write_cluster_jsonl(knowledge_root, [
        {"cluster_id": "scope-x-0001",
         "member_paths": ["scope-x/reference_dns_flakiness.md"],
         "centroid_score": 1.0, "rationale": "singleton"},
        {"cluster_id": "scope-x-0002",
         "member_paths": ["scope-x/user_tristan_profile.md"],
         "centroid_score": 1.0, "rationale": "singleton"},
    ])
    _write_config(knowledge_root)
    return knowledge_root


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestDeriveTopicSlug:
    def test_voltaire_members_produce_voltaire_slug(self) -> None:
        paths = [
            "-Users-tristankromer-Code-voltaire/project_voltaire_nanoclaw.md",
            "-Users-tristankromer-Code-voltaire/project_voltaire_iMessage_channel.md",
            "-Users-tristankromer-Code-voltaire/project_nanoclaw_voltaire_tickle.md",
            "-Users-tristankromer-Code-voltaire/project_voltaire_sessions.md",
            "-Users-tristankromer-Code-voltaire/project_voltair_nanoclaw.md",
        ]
        slug = derive_topic_slug(paths, "voltaire-0001")
        # voltaire and nanoclaw must dominate; slug contains both tokens.
        assert "voltaire" in slug
        assert "nanoclaw" in slug

    def test_singleton_falls_back_on_useful_tokens(self) -> None:
        slug = derive_topic_slug(
            ["scope-x/reference_dns_flakiness.md"], "scope-x-0001",
        )
        assert "dns" in slug or "flakiness" in slug

    def test_falls_back_to_cluster_id_when_no_tokens(self) -> None:
        # All tokens are boring prefixes → fall back.
        slug = derive_topic_slug(["scope/feedback_auto.md"], "scope-0007")
        assert slug == "scope-0007"


class TestDedupeSources:
    def test_session_turn_is_the_key(self) -> None:
        """Two turns in the same session stay distinct; same turn dedupes."""
        entries = [
            {"session": "s", "turn": 1, "date": "2026-04-10"},
            {"session": "s", "turn": 2, "date": "2026-04-10"},
            {"session": "s", "turn": 1, "date": "2026-04-11"},  # dup of #1
        ]
        out = dedupe_sources(entries)
        assert len(out) == 2
        turns = {e["turn"] for e in out}
        assert turns == {1, 2}

    def test_session_alone_is_not_the_key(self) -> None:
        """Bare-session entries (no turn) collapse among themselves only."""
        entries = [
            {"session": "s"},
            {"session": "s"},
            {"session": "t", "turn": 1},
        ]
        out = dedupe_sources(entries)
        assert len(out) == 2


class TestResolveMemberPath:
    def test_resolves_relative_to_first_root(self, tmp_path: Path) -> None:
        root = tmp_path / "raw" / "auto-memory"
        scope = root / "scope-x"
        scope.mkdir(parents=True)
        f = scope / "feedback_probe.md"
        f.write_text("body\n", encoding="utf-8")
        got = resolve_member_path("scope-x/feedback_probe.md", [root])
        assert got == f.resolve()

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        root = tmp_path / "raw" / "auto-memory"
        root.mkdir(parents=True)
        assert resolve_member_path("scope-x/missing.md", [root]) is None


class TestReadClusterRows:
    def test_reads_canonical_only(self, tmp_path: Path) -> None:
        path = tmp_path / "clusters.jsonl"
        path.write_text(
            '{"cluster_id":"a","member_paths":["p"]}\n'
            '{"cluster_id":"b","member_paths":["q"]}\n',
            encoding="utf-8",
        )
        rows = read_cluster_rows(path)
        assert [r["cluster_id"] for r in rows] == ["a", "b"]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "clusters.jsonl"
        path.write_text(
            '{"cluster_id":"a","member_paths":["p"]}\n'
            'NOT JSON\n'
            '{"cluster_id":"c","member_paths":["r"]}\n',
            encoding="utf-8",
        )
        rows = read_cluster_rows(path)
        assert [r["cluster_id"] for r in rows] == ["a", "c"]


class TestSynthesizeBody:
    def test_dedupes_identical_paragraphs(self) -> None:
        body = synthesize_body([
            ("scope-a", "file1.md", "Shared paragraph.\n\nUnique A."),
            ("scope-b", "file2.md", "Shared paragraph.\n\nUnique B."),
        ])
        # Shared paragraph appears once (under file1's header); unique
        # paragraphs survive.
        assert body.count("Shared paragraph.") == 1
        assert "Unique A." in body
        assert "Unique B." in body

    def test_prefixes_each_section_with_scope_and_filename(self) -> None:
        body = synthesize_body([("scope-a", "file1.md", "only para")])
        assert "scope-a/file1.md" in body


# ---------------------------------------------------------------------------
# Full merge integration
# ---------------------------------------------------------------------------


class TestVoltaireFixture:
    """The load-bearing regression fixture from the issue."""

    def test_exactly_one_voltaire_entry_with_five_sources(
        self, voltaire_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(voltaire_merge_root)
        assert len(entries) == 1
        entry = entries[0]

        # Exactly one file on disk, prefixed with auto-.
        wiki = voltaire_merge_root / "wiki"
        outputs = sorted(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        assert len(outputs) == 1
        assert outputs[0].name == entry.filename

        # Slug mentions voltaire + nanoclaw (the load-bearing tokens).
        assert "voltaire" in entry.topic_slug
        assert "nanoclaw" in entry.topic_slug

        # 5 sources, each carrying session/turn/origin_scope.
        assert len(entry.sources) == 5
        sessions = {s["session"] for s in entry.sources}
        assert sessions == {"s-aaa", "s-bbb", "s-ccc", "s-ddd", "s-eee"}
        turns = {s["turn"] for s in entry.sources}
        assert turns == {1, 2, 3, 4, 5}
        for s in entry.sources:
            assert s["origin_scope"] == "-Users-tristankromer-Code-voltaire"

    def test_body_retains_loadbearing_tokens(
        self, voltaire_merge_root: Path,
    ) -> None:
        merge_clusters_to_wiki(voltaire_merge_root)
        wiki = voltaire_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        text = entry_file.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        assert "Voltaire" in body or "voltaire" in body.lower()
        assert "nanoclaw" in body.lower()
        # At least one hostname/path-y token from the 5 inputs.
        assert "NanoClaw" in body or "iMessage" in body or "box-claude" in body

    def test_frontmatter_shape(self, voltaire_merge_root: Path) -> None:
        merge_clusters_to_wiki(voltaire_merge_root)
        wiki = voltaire_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        text = entry_file.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        assert meta["type"] == "auto-memory"
        assert meta["cluster_id"] == "voltaire-0001"
        assert meta["contradictions_detected"] is False  # 0.88 > 0.75
        assert meta["origin_scopes"] == ["-Users-tristankromer-Code-voltaire"]
        assert isinstance(meta["sources"], list)
        assert len(meta["sources"]) == 5


class TestContradictionFixture:
    """C4 (#198): contradictions are detector-driven now.

    The old C3 behaviour keyed off ``centroid_score < 0.75``. With the
    detector stubbed out (no ``ANTHROPIC_API_KEY``, no client passed),
    the detector returns ``detected=False`` with
    ``rationale="llm-unavailable"`` and the wiki entry must NOT carry the
    ``contradictions_detected`` flag. Detector-positive behaviour is
    exercised in ``test_contradictions.py`` and the integration test
    below via a mocked client.
    """

    def test_no_client_means_no_flag(
        self, contradiction_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(contradiction_merge_root)
        assert len(entries) == 1
        assert entries[0].contradictions_detected is False
        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is False
        # When not flagged, status key is absent entirely.
        assert "status" not in meta
        # Both sources present regardless of detector outcome.
        assert len(meta["sources"]) == 2
        # No _pending_questions.md side-effect when the detector is a no-op.
        assert not (wiki / "_pending_questions.md").exists()

    def test_threshold_constant_retained_for_bc(self) -> None:
        # C4 no longer reads this constant, but it stays exported at its
        # historical value so any downstream import does not break.
        assert CONTRADICTION_COHESION_THRESHOLD == 0.75

    def test_detector_positive_flags_entry_and_writes_pending(
        self, contradiction_merge_root: Path,
    ) -> None:
        """With a mocked detector returning detected=True, the wiki entry
        carries ``status: contradiction-flagged`` AND a block is appended
        to ``wiki/_pending_questions.md`` using the answers.py grammar.
        """
        from unittest.mock import MagicMock

        fake_client = MagicMock()
        payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park on WIP."}'
        )
        response = MagicMock()
        response.content = [MagicMock(text=payload)]
        fake_client.messages.create.return_value = response

        entries = merge_clusters_to_wiki(
            contradiction_merge_root, client=fake_client,
        )
        assert len(entries) == 1
        assert entries[0].contradictions_detected is True

        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is True
        assert meta["status"] == "contradiction-flagged"
        assert meta["contradiction_type"] == "prescriptive"

        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "# Pending Questions" in text
        # Header uses the tier4_escalate "Entity:" grammar so answers.py
        # can round-trip the block through ingest-answers.
        assert "Entity:" in text
        # raw_ref points at the compiled wiki entry.
        assert "from wiki/" in text
        assert "**Conflict type**: prescriptive" in text
        assert "Park prior-session debris on a WIP branch." in text


class TestSessionTurnDedupe:
    def test_same_session_different_turns_not_collapsed(
        self, session_turn_dedupe_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(session_turn_dedupe_root)
        assert len(entries) == 1
        # Input had 3 source rows; turn-1 dup collapses → 2 remain.
        assert len(entries[0].sources) == 2
        turns = {s["turn"] for s in entries[0].sources}
        assert turns == {1, 2}
        # Both entries retain the shared session id.
        assert {s["session"] for s in entries[0].sources} == {"s-shared"}


class TestSingletonsEmitted:
    def test_every_singleton_becomes_a_wiki_entry(
        self, singleton_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(singleton_merge_root)
        # Both singletons become wiki entries — no min-size filter.
        assert len(entries) == 2
        wiki = singleton_merge_root / "wiki"
        outputs = sorted(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        assert len(outputs) == 2

    def test_no_memory_md_is_emitted(
        self, singleton_merge_root: Path,
    ) -> None:
        merge_clusters_to_wiki(singleton_merge_root)
        wiki = singleton_merge_root / "wiki"
        # Phase B removed the cross-scope wiki/MEMORY.md — we must not
        # recreate it.
        assert not (wiki / "MEMORY.md").exists()


class TestDryRun:
    def test_dry_run_builds_entries_without_writing(
        self, voltaire_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(voltaire_merge_root, dry_run=True)
        assert len(entries) == 1
        wiki = voltaire_merge_root / "wiki"
        # Directory may exist but must be empty of auto-* files.
        outputs = list(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")) if wiki.exists() else []
        assert outputs == []


class TestRawFilesUntouched:
    def test_raw_files_remain_after_merge(
        self, voltaire_merge_root: Path,
    ) -> None:
        raw_root = (
            voltaire_merge_root / "raw" / "auto-memory"
            / "-Users-tristankromer-Code-voltaire"
        )
        before = sorted(p.name for p in raw_root.glob("*.md"))
        merge_clusters_to_wiki(voltaire_merge_root)
        after = sorted(p.name for p in raw_root.glob("*.md"))
        assert before == after
        assert len(before) == 5


class TestMergeOnlyCLI:
    def test_merge_only_run_skips_clustering(
        self,
        voltaire_merge_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run(merge_only=True)`` reads the JSONL and writes wiki entries."""
        from athenaeum.librarian import run

        # Pre-existing cluster JSONL + no ANTHROPIC_API_KEY required.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rc = run(
            raw_root=voltaire_merge_root / "raw",
            wiki_root=voltaire_merge_root / "wiki",
            knowledge_root=voltaire_merge_root,
            merge_only=True,
        )
        assert rc == 0
        wiki = voltaire_merge_root / "wiki"
        outputs = sorted(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        assert len(outputs) == 1


