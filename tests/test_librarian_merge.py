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
from datetime import datetime, timedelta, timezone
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


def _write_cluster_jsonl(
    knowledge_root: Path,
    rows: list[dict[str, object]],
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
    scope = (
        knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-voltaire"
    )

    specs = [
        ("project_voltaire_nanoclaw.md", "s-aaa", 1, "Voltaire+nanoclaw"),
        (
            "project_voltaire_iMessage_channel.md",
            "s-bbb",
            2,
            "Voltaire iMessage channel via NanoClaw",
        ),
        (
            "project_nanoclaw_voltaire_tickle.md",
            "s-ccc",
            3,
            "Nanoclaw ticklestick voltaire",
        ),
        (
            "project_voltaire_sessions.md",
            "s-ddd",
            4,
            "Voltaire sessions via box-claude",
        ),
        ("project_voltair_nanoclaw.md", "s-eee", 5, "Voltair typo clone"),
    ]
    for filename, session, turn, body in specs:
        _write_am_file(
            scope,
            filename,
            frontmatter_name=filename.replace("_", " ").replace(".md", ""),
            description="voltaire toolchain note",
            origin_session_id=session,
            origin_turn=turn,
            sources=[
                {
                    "session": session,
                    "turn": turn,
                    "date": f"2026-04-{10 + turn:02d}",
                    "excerpt": body,
                }
            ],
            body=body,
        )

    member_paths = [f"-Users-tristankromer-Code-voltaire/{s[0]}" for s in specs]
    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "voltaire-0001",
                "member_paths": member_paths,
                "centroid_score": 0.88,
                "rationale": "cosine >= 0.55; shares tokens: voltaire, nanoclaw",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def contradiction_merge_root(tmp_path: Path) -> Path:
    """Two opposing-guidance feedback files in one low-cohesion cluster."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

    _write_am_file(
        scope,
        "feedback_prior_session_debris_v1.md",
        frontmatter_name="Prior session debris v1",
        description="commit directly",
        origin_session_id="s-111",
        origin_turn=1,
        sources=[
            {
                "session": "s-111",
                "turn": 1,
                "date": "2026-04-10",
                "excerpt": "commit to develop, do not park",
            }
        ],
        body="Commit prior-session debris directly to develop. Do not park on WIP.",
    )
    _write_am_file(
        scope,
        "feedback_prior_session_debris_v2.md",
        frontmatter_name="Prior session debris v2",
        description="park on WIP",
        origin_session_id="s-222",
        origin_turn=2,
        sources=[
            {
                "session": "s-222",
                "turn": 2,
                "date": "2026-04-11",
                "excerpt": "park on WIP, do not commit",
            }
        ],
        body="Park prior-session debris on a WIP branch. Do not commit directly.",
    )

    _write_cluster_jsonl(
        knowledge_root,
        [
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
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def escalation_dedupe_root(tmp_path: Path) -> Path:
    """Same source-file PAIR pulled into 3 overlapping clusters (issue #146).

    The two opposing-guidance feedback files appear in three distinct
    cluster rows (different ``cluster_id``s, different centroid scores).
    Detection runs per cluster, so the detector fires 3 times on the same
    source-file pair — but escalation must dedup to ONE pending question.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

    _write_am_file(
        scope,
        "feedback_open_files_in_sublime.md",
        frontmatter_name="Open files in Sublime",
        description="use sublime",
        origin_session_id="s-sub",
        origin_turn=1,
        sources=[
            {
                "session": "s-sub",
                "turn": 1,
                "date": "2026-04-10",
                "excerpt": "open files in sublime",
            }
        ],
        body="Open files in Sublime Text. It is the default editor.",
    )
    _write_am_file(
        scope,
        "feedback_open_csv_in_numbers.md",
        frontmatter_name="Open CSVs in Numbers",
        description="use numbers",
        origin_session_id="s-num",
        origin_turn=2,
        sources=[
            {
                "session": "s-num",
                "turn": 2,
                "date": "2026-04-11",
                "excerpt": "open csv files in numbers",
            }
        ],
        body="Open CSV files in Numbers, never in Sublime Text.",
    )

    members = [
        "-Users-tristankromer-Code/feedback_open_files_in_sublime.md",
        "-Users-tristankromer-Code/feedback_open_csv_in_numbers.md",
    ]
    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "auth-git-closes",
                "member_paths": members,
                "centroid_score": 0.61,
                "rationale": "cosine >= 0.55; overlapping cluster A",
            },
            {
                "cluster_id": "auth-staging-voltaire",
                "member_paths": members,
                "centroid_score": 0.60,
                "rationale": "cosine >= 0.55; overlapping cluster B",
            },
            {
                "cluster_id": "auth-voltaire-closes",
                "member_paths": members,
                "centroid_score": 0.59,
                "rationale": "cosine >= 0.55; overlapping cluster C",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def distinct_conflicts_root(tmp_path: Path) -> Path:
    """Two DIFFERENT source-file pairs in two clusters (issue #146).

    Each cluster flags a distinct conflict — dedup must NOT collapse them;
    both must produce their own pending question.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

    for fname, name, body in (
        (
            "feedback_open_files_in_sublime.md",
            "Open files in Sublime",
            "Open files in Sublime Text.",
        ),
        (
            "feedback_open_csv_in_numbers.md",
            "Open CSVs in Numbers",
            "Open CSV files in Numbers, never in Sublime.",
        ),
        (
            "reference_voltaire_pytest_venv.md",
            "Voltaire pytest venv",
            "Run pytest from the voltaire-local venv.",
        ),
        (
            "reference_workspace_pytest_venv.md",
            "Workspace pytest venv",
            "Run pytest from the shared workspace venv, not a local one.",
        ),
    ):
        _write_am_file(
            scope,
            fname,
            frontmatter_name=name,
            description=name,
            origin_session_id=f"s-{fname[:6]}",
            origin_turn=1,
            sources=[
                {
                    "session": f"s-{fname[:6]}",
                    "turn": 1,
                    "date": "2026-04-10",
                    "excerpt": body,
                }
            ],
            body=body,
        )

    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "editor-conflict",
                "member_paths": [
                    "-Users-tristankromer-Code/feedback_open_files_in_sublime.md",
                    "-Users-tristankromer-Code/feedback_open_csv_in_numbers.md",
                ],
                "centroid_score": 0.61,
                "rationale": "cosine >= 0.55; editor conflict",
            },
            {
                "cluster_id": "venv-conflict",
                "member_paths": [
                    "-Users-tristankromer-Code/reference_voltaire_pytest_venv.md",
                    "-Users-tristankromer-Code/reference_workspace_pytest_venv.md",
                ],
                "centroid_score": 0.60,
                "rationale": "cosine >= 0.55; venv conflict",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def suppress_then_detect_root(tmp_path: Path) -> Path:
    """Same source-file PAIR in two clusters (issue #146 / #145 interaction).

    Cluster 1 flags the pair but the resolver confirmation pass suppresses
    it (``not_a_conflict``). Cluster 2 flags the SAME pair and the resolver
    does NOT suppress. Because the dedup check sits AFTER the suppress
    early-return in ``_emit_escalation``, the suppressed cluster never
    records the member key — so cluster 2 still escalates. Exactly one
    pending question is expected.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

    _write_am_file(
        scope,
        "feedback_open_files_in_sublime.md",
        frontmatter_name="Open files in Sublime",
        description="use sublime",
        origin_session_id="s-sub",
        origin_turn=1,
        sources=[
            {
                "session": "s-sub",
                "turn": 1,
                "date": "2026-04-10",
                "excerpt": "open files in sublime",
            }
        ],
        body="Open files in Sublime Text. It is the default editor.",
    )
    _write_am_file(
        scope,
        "feedback_open_csv_in_numbers.md",
        frontmatter_name="Open CSVs in Numbers",
        description="use numbers",
        origin_session_id="s-num",
        origin_turn=2,
        sources=[
            {
                "session": "s-num",
                "turn": 2,
                "date": "2026-04-11",
                "excerpt": "open csv files in numbers",
            }
        ],
        body="Open CSV files in Numbers, never in Sublime Text.",
    )

    members = [
        "-Users-tristankromer-Code/feedback_open_files_in_sublime.md",
        "-Users-tristankromer-Code/feedback_open_csv_in_numbers.md",
    ]
    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "suppress-first",
                "member_paths": members,
                "centroid_score": 0.61,
                "rationale": "cosine >= 0.55; overlapping cluster (suppressed)",
            },
            {
                "cluster_id": "detect-second",
                "member_paths": members,
                "centroid_score": 0.60,
                "rationale": "cosine >= 0.55; overlapping cluster (detected)",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def session_turn_dedupe_root(tmp_path: Path) -> Path:
    """One file whose sources[] stresses the (session, turn) dedupe key."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "scope-x"

    _write_am_file(
        scope,
        "feedback_dedupe_probe.md",
        frontmatter_name="Dedupe probe",
        description="probe",
        origin_session_id="s-shared",
        origin_turn=1,
        sources=[
            {
                "session": "s-shared",
                "turn": 1,
                "date": "2026-04-10",
                "excerpt": "turn 1",
            },
            # Same session, different turn — MUST NOT collapse.
            {
                "session": "s-shared",
                "turn": 2,
                "date": "2026-04-10",
                "excerpt": "turn 2",
            },
            # Same session+turn, different date — MUST collapse into turn-1.
            {
                "session": "s-shared",
                "turn": 1,
                "date": "2026-04-11",
                "excerpt": "turn 1 duplicate",
            },
        ],
        body="Dedupe probe body.",
    )

    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "scope-x-0001",
                "member_paths": ["scope-x/feedback_dedupe_probe.md"],
                "centroid_score": 1.0,
                "rationale": "singleton",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def singleton_merge_root(tmp_path: Path) -> Path:
    """Two unrelated size-1 clusters — both MUST emit wiki entries."""
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "scope-x"

    _write_am_file(
        scope,
        "reference_dns_flakiness.md",
        frontmatter_name="DNS flakiness",
        description="macOS dns",
        origin_session_id="s-dns",
        origin_turn=1,
        sources=[
            {"session": "s-dns", "turn": 1, "date": "2026-04-10", "excerpt": "dns"}
        ],
        body="mDNSResponder flakes.",
    )
    _write_am_file(
        scope,
        "user_tristan_profile.md",
        frontmatter_name="Tristan profile",
        description="profile",
        origin_session_id="s-prof",
        origin_turn=1,
        sources=[
            {"session": "s-prof", "turn": 1, "date": "2026-04-10", "excerpt": "profile"}
        ],
        body="Consultant.",
    )

    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "scope-x-0001",
                "member_paths": ["scope-x/reference_dns_flakiness.md"],
                "centroid_score": 1.0,
                "rationale": "singleton",
            },
            {
                "cluster_id": "scope-x-0002",
                "member_paths": ["scope-x/user_tristan_profile.md"],
                "centroid_score": 1.0,
                "rationale": "singleton",
            },
        ],
    )
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
            ["scope-x/reference_dns_flakiness.md"],
            "scope-x-0001",
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
            '{"cluster_id":"a","member_paths":["p"]}\n{"cluster_id":"b","member_paths":["q"]}\n',
            encoding="utf-8",
        )
        rows = read_cluster_rows(path)
        assert [r["cluster_id"] for r in rows] == ["a", "b"]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "clusters.jsonl"
        path.write_text(
            '{"cluster_id":"a","member_paths":["p"]}\n'
            "NOT JSON\n"
            '{"cluster_id":"c","member_paths":["r"]}\n',
            encoding="utf-8",
        )
        rows = read_cluster_rows(path)
        assert [r["cluster_id"] for r in rows] == ["a", "c"]


class TestSynthesizeBody:
    def test_dedupes_identical_paragraphs(self) -> None:
        body = synthesize_body(
            [
                ("scope-a", "file1.md", "Shared paragraph.\n\nUnique A."),
                ("scope-b", "file2.md", "Shared paragraph.\n\nUnique B."),
            ]
        )
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
        self,
        voltaire_merge_root: Path,
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
        self,
        voltaire_merge_root: Path,
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
        self,
        contradiction_merge_root: Path,
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
        self,
        contradiction_merge_root: Path,
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
            contradiction_merge_root,
            client=fake_client,
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

    def test_confirmation_pass_suppresses_false_positive(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Issue #145: when the Haiku detector flags a cluster but the
        resolver confirmation pass returns ``not_a_conflict``, NO pending
        question is written and the wiki entry is NOT flagged.
        """
        from unittest.mock import MagicMock

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park."}'
        )
        resolver_payload = (
            '{"recommended_winner": "neither", "action": "not_a_conflict", '
            '"confidence": 0.91, '
            '"rationale": "Different-scenario rules; they never both apply.", '
            '"source_precedence_used": []}'
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        fake_client = MagicMock()
        # First call = detector (Haiku), second = resolver confirmation
        # pass (Opus). The resolver clears the false positive.
        fake_client.messages.create.side_effect = [
            _make_response(detector_payload),
            _make_response(resolver_payload),
        ]

        entries = merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
        )
        assert len(entries) == 1
        # Confirmation pass cleared it — entry frontmatter is not flagged.
        assert entries[0].contradictions_detected is False

        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is False
        assert "status" not in meta
        # The whole point of #145: no human-queue entry for a false positive.
        assert not (wiki / "_pending_questions.md").exists()

    def test_budget_exhausted_falls_back_to_escalate(
        self,
        contradiction_merge_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue #145: with the resolver budget at 0, the confirmation
        pass cannot run — the cluster escalates WITHOUT a proposal block,
        exactly as before #145.
        """
        from unittest.mock import MagicMock

        monkeypatch.setenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", "0")

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park."}'
        )
        response = MagicMock()
        response.content = [MagicMock(text=detector_payload)]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        entries = merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
        )
        assert len(entries) == 1
        assert entries[0].contradictions_detected is True

        wiki = contradiction_merge_root / "wiki"
        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "Park prior-session debris on a WIP branch." in text
        # Budget exhausted → no resolver call → no proposal block.
        assert "**Proposed resolution**" not in text

    def test_genuine_verdict_attaches_proposal_block(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Issue #148: when the resolver confirmation pass returns a
        genuine verdict (a real ``keep_a`` / ``keep_b`` / ``merge``
        action, NOT ``not_a_conflict``), the cluster still escalates AND
        the pending question carries a ``**Proposed resolution**`` block.

        This is the inverse of ``test_budget_exhausted_falls_back_to_escalate``
        — there the block is absent because no resolver call ran; here a
        non-zero-confidence verdict makes ``render_proposal_block`` emit it.
        """
        from unittest.mock import MagicMock

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park."}'
        )
        # A genuine resolver verdict: keep_a, non-zero confidence so
        # render_proposal_block emits the block.
        valid_resolver_payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.92, '
            '"rationale": "Member a is user-direct (precedence 1); '
            'member b is unsourced.", '
            '"source_precedence_used": ["a:user:session-1 > b:unsourced"]}'
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _make_response(detector_payload),
            _make_response(valid_resolver_payload),
        ]

        entries = merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
        )
        assert len(entries) == 1
        # Genuine verdict → cluster still escalates and entry is flagged.
        assert entries[0].contradictions_detected is True

        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is True
        assert meta["status"] == "contradiction-flagged"

        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "Park prior-session debris on a WIP branch." in text
        # The genuine-verdict branch: the proposal block IS rendered.
        assert "**Proposed resolution**" in text

    def test_malformed_resolver_response_still_escalates(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Issue #148 (fail-safe): a resolver that *replies* but with no
        parseable JSON must NOT silently suppress a real conflict. The
        mock returns a well-formed response object whose ``.content[0].text``
        is plain prose, so execution flows through ``_parse_response``,
        ``extract_json_object`` finds no JSON object, and the path taken
        is ``_fallback("resolver-returned-no-json")``. That fallback returns
        ``retain_both_with_context`` — not ``SUPPRESS_ACTION`` — so the
        cluster still escalates to ``_pending_questions.md``.
        """
        from unittest.mock import MagicMock

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park."}'
        )
        # Garbled, non-JSON resolver output — _parse_response cannot
        # extract a JSON object, so the fallback proposal is used.
        malformed_resolver_response = (
            "I'm sorry, I cannot resolve this contradiction right now."
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _make_response(detector_payload),
            _make_response(malformed_resolver_response),
        ]

        entries = merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
        )
        assert len(entries) == 1
        # A garbled resolver response is the fail-safe direction: escalate.
        assert entries[0].contradictions_detected is True

        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is True
        assert meta["status"] == "contradiction-flagged"

        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "Park prior-session debris on a WIP branch." in text
        # Deterministic fallback (confidence 0.0) → no proposal block.
        assert "**Proposed resolution**" not in text

    def test_resolver_malformed_response_object_still_escalates(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Issue #148 (fail-safe): a malformed resolver *response object*
        — one where accessing ``response.content[0].text`` raises — must
        NOT silently suppress a real conflict. With ``content == []`` the
        ``[0]`` index raises ``IndexError``, so ``propose_resolution``
        takes the ``_fallback("resolver-malformed-response")`` branch.
        That fallback returns ``retain_both_with_context`` — not
        ``SUPPRESS_ACTION`` — so the cluster still escalates to
        ``_pending_questions.md``.
        """
        from unittest.mock import MagicMock

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; the other says park."}'
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        # The detector response is well-formed; the resolver response is a
        # malformed object — empty content list → IndexError on [0].
        malformed_resolver_response = MagicMock()
        malformed_resolver_response.content = []

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _make_response(detector_payload),
            malformed_resolver_response,
        ]

        entries = merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
        )
        assert len(entries) == 1
        # A malformed resolver response object is the fail-safe direction:
        # escalate.
        assert entries[0].contradictions_detected is True

        wiki = contradiction_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["contradictions_detected"] is True
        assert meta["status"] == "contradiction-flagged"

        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        text = pending.read_text(encoding="utf-8")
        assert "Park prior-session debris on a WIP branch." in text
        # Deterministic fallback (confidence 0.0) → no proposal block.
        assert "**Proposed resolution**" not in text


class TestEscalationDedupe:
    """Issue #146: dedup escalations by the flagged source-file SET, not
    by cluster slug, across the whole run."""

    @staticmethod
    def _count_escalations(pending_path: Path) -> int:
        """Each escalation writes exactly one '(from wiki/...)' header."""
        if not pending_path.exists():
            return 0
        text = pending_path.read_text(encoding="utf-8")
        return text.count("(from wiki/")

    def test_same_pair_in_three_clusters_yields_one_escalation(
        self,
        escalation_dedupe_root: Path,
    ) -> None:
        """Three overlapping clusters all flag the same two source files.
        The run must produce EXACTLY ONE pending question, not three."""
        from unittest.mock import MagicMock

        # Detector flags the same source-file pair every call. members_involved
        # is the dedup key — identical across all three clusters.
        payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_open_files_in_sublime.md", '
            '"-Users-tristankromer-Code/feedback_open_csv_in_numbers.md"], '
            '"conflicting_passages": ['
            '"Open files in Sublime Text.", '
            '"Open CSV files in Numbers, never in Sublime Text."], '
            '"rationale": "One says Sublime; the other says Numbers."}'
        )
        response = MagicMock()
        response.content = [MagicMock(text=payload)]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        entries = merge_clusters_to_wiki(
            escalation_dedupe_root,
            client=fake_client,
        )
        # Three cluster rows → three wiki entries.
        assert len(entries) == 3

        pending = escalation_dedupe_root / "wiki" / "_pending_questions.md"
        assert pending.exists()
        # The whole point of #146: one conflict, one pending question.
        assert self._count_escalations(pending) == 1

    def test_distinct_pairs_each_produce_their_own_escalation(
        self,
        distinct_conflicts_root: Path,
    ) -> None:
        """Two clusters flagging two DIFFERENT source-file pairs must not
        be collapsed — each distinct conflict keeps its own entry."""
        from unittest.mock import MagicMock

        editor_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_open_files_in_sublime.md", '
            '"-Users-tristankromer-Code/feedback_open_csv_in_numbers.md"], '
            '"conflicting_passages": ["Sublime.", "Numbers."], '
            '"rationale": "Editor conflict."}'
        )
        venv_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/reference_voltaire_pytest_venv.md", '
            '"-Users-tristankromer-Code/reference_workspace_pytest_venv.md"], '
            '"conflicting_passages": ["voltaire venv.", "workspace venv."], '
            '"rationale": "Venv conflict."}'
        )

        def _route(*args: object, **kwargs: object) -> MagicMock:
            """Return the payload whose members match the cluster under test
            so each detector call reports its own flagged pair."""
            messages = kwargs.get("messages") or []
            blob = json.dumps(messages)
            text = venv_payload if "pytest_venv" in blob else editor_payload
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = _route

        entries = merge_clusters_to_wiki(
            distinct_conflicts_root,
            client=fake_client,
        )
        assert len(entries) == 2

        pending = distinct_conflicts_root / "wiki" / "_pending_questions.md"
        assert pending.exists()
        # Distinct conflicts → distinct entries; dedup must NOT collapse them.
        assert self._count_escalations(pending) == 2

    def test_single_member_result_escalates_without_poisoning_dedup_set(
        self,
        distinct_conflicts_root: Path,
    ) -> None:
        """A detected result with fewer than 2 flagged ``members_involved``
        escalates but is NOT recorded in the run-scoped dedup set — the
        detector docstring warns callers must not assume 2 entries. A later
        DISTINCT conflict must still escalate normally.
        """
        from unittest.mock import MagicMock

        # Cluster 1 (editor): detector echoes only ONE flagged member, so
        # `members_involved` is length 1 after `_clean_detector_payload`.
        # This exercises the `len(member_key) >= 2` fall-through.
        editor_one_member_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_open_files_in_sublime.md"], '
            '"conflicting_passages": ["Sublime.", "Numbers."], '
            '"rationale": "Editor conflict; detector echoed one member."}'
        )
        # Cluster 2 (venv): a genuine, DISTINCT 2-member conflict.
        venv_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/reference_voltaire_pytest_venv.md", '
            '"-Users-tristankromer-Code/reference_workspace_pytest_venv.md"], '
            '"conflicting_passages": ["voltaire venv.", "workspace venv."], '
            '"rationale": "Venv conflict."}'
        )
        # Both clusters are genuinely detected: the resolver confirmation
        # pass keeps them (non-suppress action).
        keep_payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.88, "rationale": "Real conflict; keep a.", '
            '"source_precedence_used": []}'
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        def _route(*args: object, **kwargs: object) -> MagicMock:
            """Detector vs resolver is distinguished by payload shape; the
            editor vs venv cluster by the member paths in the messages."""
            messages = kwargs.get("messages") or []
            blob = json.dumps(messages)
            if "recommended_winner" in blob or "not_a_conflict" in blob:
                # Resolver confirmation pass.
                return _make_response(keep_payload)
            # Detector call.
            text = venv_payload if "pytest_venv" in blob else editor_one_member_payload
            return _make_response(text)

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = _route

        entries = merge_clusters_to_wiki(
            distinct_conflicts_root,
            client=fake_client,
        )
        assert len(entries) == 2

        pending = distinct_conflicts_root / "wiki" / "_pending_questions.md"
        assert pending.exists()
        # The 1-member editor result escalates (not recorded in the dedup
        # set); the distinct venv conflict escalates too. A poisoned set
        # would have collapsed one of them.
        assert self._count_escalations(pending) == 2

    def test_suppressed_pair_does_not_block_later_genuine_detection(
        self,
        suppress_then_detect_root: Path,
    ) -> None:
        """Cluster 1 flags source-file pair P but the resolver returns the
        suppress verdict (``not_a_conflict``) — no escalation, no key
        recorded. Cluster 2 flags the SAME pair P and is genuinely detected.
        Exactly ONE escalation (cluster 2's) must result.

        This pins the ordering: the #146 dedup check sits AFTER the #145
        suppress early-return in ``_emit_escalation``. A future refactor
        moving the dedup check above the suppress return would record P on
        the suppressed cluster and silently drop cluster 2's real
        escalation — this test catches that regression.
        """
        from unittest.mock import MagicMock

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_open_files_in_sublime.md", '
            '"-Users-tristankromer-Code/feedback_open_csv_in_numbers.md"], '
            '"conflicting_passages": ['
            '"Open files in Sublime Text.", '
            '"Open CSV files in Numbers, never in Sublime Text."], '
            '"rationale": "One says Sublime; the other says Numbers."}'
        )
        # Cluster 1's resolver suppresses; cluster 2's resolver keeps.
        suppress_payload = (
            '{"recommended_winner": "neither", "action": "not_a_conflict", '
            '"confidence": 0.91, '
            '"rationale": "Different-scenario rules; they never both apply.", '
            '"source_precedence_used": []}'
        )
        keep_payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.88, "rationale": "Real conflict; keep a.", '
            '"source_precedence_used": []}'
        )

        def _make_response(text: str) -> MagicMock:
            resp = MagicMock()
            resp.content = [MagicMock(text=text)]
            return resp

        # Call order across both clusters:
        #   cluster 1 → detector, resolver (suppress)
        #   cluster 2 → detector, resolver (keep)
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _make_response(detector_payload),
            _make_response(suppress_payload),
            _make_response(detector_payload),
            _make_response(keep_payload),
        ]

        entries = merge_clusters_to_wiki(
            suppress_then_detect_root,
            client=fake_client,
        )
        assert len(entries) == 2

        pending = suppress_then_detect_root / "wiki" / "_pending_questions.md"
        assert pending.exists()
        # Cluster 1 suppressed → no key recorded → cluster 2's genuine
        # detection of the SAME pair still escalates. Exactly one question.
        assert self._count_escalations(pending) == 1


class TestSessionTurnDedupe:
    def test_same_session_different_turns_not_collapsed(
        self,
        session_turn_dedupe_root: Path,
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
        self,
        singleton_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(singleton_merge_root)
        # Both singletons become wiki entries — no min-size filter.
        assert len(entries) == 2
        wiki = singleton_merge_root / "wiki"
        outputs = sorted(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        assert len(outputs) == 2

    def test_no_memory_md_is_emitted(
        self,
        singleton_merge_root: Path,
    ) -> None:
        merge_clusters_to_wiki(singleton_merge_root)
        wiki = singleton_merge_root / "wiki"
        # Phase B removed the cross-scope wiki/MEMORY.md — we must not
        # recreate it.
        assert not (wiki / "MEMORY.md").exists()


class TestDryRun:
    def test_dry_run_builds_entries_without_writing(
        self,
        voltaire_merge_root: Path,
    ) -> None:
        entries = merge_clusters_to_wiki(voltaire_merge_root, dry_run=True)
        assert len(entries) == 1
        wiki = voltaire_merge_root / "wiki"
        # Directory may exist but must be empty of auto-* files.
        outputs = list(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")) if wiki.exists() else []
        assert outputs == []


class TestRawFilesUntouched:
    def test_raw_files_remain_after_merge(
        self,
        voltaire_merge_root: Path,
    ) -> None:
        raw_root = (
            voltaire_merge_root
            / "raw"
            / "auto-memory"
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


# ---------------------------------------------------------------------------
# Issue #181: self-reference lint applies to cluster-shim path
# ---------------------------------------------------------------------------


class TestClusterShimSelfReferenceLint:
    """The shim branch in :func:`merge_cluster_row` builds an
    :class:`AutoMemoryFile` on the fly when a cluster row references a
    file that C1 didn't discover. That branch must apply the same
    self-reference lint as the discovery path (issue #181)."""

    def test_refines_self_dropped_on_shim(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.merge import merge_cluster_row

        member = tmp_path / "shim_self.md"
        member.write_text(
            "---\nname: Shim Mem\ntype: feedback\nrefines:\n  - Shim Mem\n  - Other\n---\nbody\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c-shim",
            "member_paths": [str(member)],
            "centroid_score": 1.0,
        }
        with caplog.at_level("WARNING"):
            entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert len(entry.resolved_members) == 1
        assert entry.resolved_members[0].refines == ["Other"]
        assert any(
            "refines self" in r.getMessage()
            and "Shim Mem" in r.getMessage()
            and str(member) in r.getMessage()
            for r in caplog.records
        )

    def test_supersedes_self_dropped_on_shim(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.merge import merge_cluster_row

        member = tmp_path / "shim_self.md"
        member.write_text(
            "---\nname: Shim Mem\ntype: feedback\n"
            "supersedes:\n"
            "  - name: Shim Mem\n    as_of: 2026-01-01\n    reason: typo\n"
            "  - name: Other\n    as_of: 2026-01-02\n    reason: real\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c-shim",
            "member_paths": [str(member)],
            "centroid_score": 1.0,
        }
        with caplog.at_level("WARNING"):
            entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert [s["name"] for s in entry.resolved_members[0].supersedes] == ["Other"]
        assert any(
            "supersedes self" in r.getMessage()
            and "Shim Mem" in r.getMessage()
            and str(member) in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Issue #249: incremental confirmation pass — cache not_a_conflict verdicts
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedding provider for the similarity-sweep test.

    Mirrors ``test_cross_scope._StubEmbedder``: the constructor takes
    ``{recall_index_id: vector}`` and ``fetch_embeddings`` returns the
    intersection of requested ids and the stored map.
    """

    def __init__(self, embeddings: dict[str, list[float]]) -> None:
        self._embeddings = embeddings

    def fetch_embeddings(self, ids: object, cache_dir: Path) -> dict[str, list[float]]:
        del cache_dir
        return {i: self._embeddings[i] for i in ids if i in self._embeddings}


class TestNotAConflictCache:
    """Issue #249: the nightly confirmation pass caches ``not_a_conflict``
    verdicts so the expensive Opus confirmation call is skipped for claim
    pairs already settled as false positives.

    These exercise :func:`merge_clusters_to_wiki` against the
    ``contradiction_merge_root`` fixture, whose two opposing-guidance files
    flag a ``prescriptive`` conflict. The detector + resolver are stubbed
    via a ``MagicMock`` client with a scripted ``side_effect`` — exactly the
    pattern ``TestContradictionFixture`` uses.
    """

    # The two passages the detector reports for contradiction_merge_root,
    # in the resolver's a/b order, plus the conflict type. The fingerprint
    # is computed over these — keep in sync with the payloads below.
    PASSAGE_A = "Commit prior-session debris directly to develop."
    PASSAGE_B = "Park prior-session debris on a WIP branch."
    CONFLICT_TYPE = "prescriptive"

    DETECTOR_PAYLOAD = (
        '{"detected": true, "conflict_type": "prescriptive", '
        '"members_involved": ['
        '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
        '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
        '"conflicting_passages": ['
        '"Commit prior-session debris directly to develop.", '
        '"Park prior-session debris on a WIP branch."], '
        '"rationale": "One says commit directly; the other says park on WIP."}'
    )
    SUPPRESS_PAYLOAD = (
        '{"recommended_winner": "neither", "action": "not_a_conflict", '
        '"confidence": 0.91, '
        '"rationale": "Different-scenario rules; they never both apply.", '
        '"source_precedence_used": []}'
    )

    @staticmethod
    def _make_response(text: str):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        return resp

    @classmethod
    def _expected_fingerprint(cls) -> str:
        from athenaeum.fingerprint import claim_pair_fingerprint

        return claim_pair_fingerprint(cls.PASSAGE_A, cls.PASSAGE_B, cls.CONFLICT_TYPE)

    @staticmethod
    def _cache_rows(knowledge_root: Path) -> list[dict]:
        path = knowledge_root / "raw" / "_resolved_contradictions.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_records_not_a_conflict_on_clear(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """A detected cluster the resolver clears as ``not_a_conflict``
        writes exactly one cache row (``action=not_a_conflict``,
        ``resolved_by=auto``) keyed by the claim-pair fingerprint."""
        from unittest.mock import MagicMock

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
            self._make_response(self.SUPPRESS_PAYLOAD),
        ]

        merge_clusters_to_wiki(contradiction_merge_root, client=fake_client)

        rows = self._cache_rows(contradiction_merge_root)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "not_a_conflict"
        assert row["resolved_by"] == "auto"
        assert row["fingerprint"] == self._expected_fingerprint()

    def test_cache_hit_skips_opus_and_drops_escalation(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Pre-seeding the cache with the pair's ``not_a_conflict``
        fingerprint must skip the Opus confirmation call entirely and drop
        the escalation (no pending question written)."""
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import record_resolution

        record_resolution(
            contradiction_merge_root,
            fingerprint=self._expected_fingerprint(),
            verdict="not_a_conflict",
            resolved_by="auto",
        )

        # Only the detector (Haiku) should be called. If the resolver
        # (Opus) is invoked, side_effect runs dry → StopIteration, which
        # the test would surface. We additionally assert the call count.
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]

        entries = merge_clusters_to_wiki(contradiction_merge_root, client=fake_client)
        assert len(entries) == 1
        # Cache hit → confirmation suppressed → entry not flagged.
        assert entries[0].contradictions_detected is False

        # Exactly one API call: the detector. No Opus confirmation call.
        assert fake_client.messages.create.call_count == 1

        wiki = contradiction_merge_root / "wiki"
        # Escalation dropped → no pending question.
        assert not (wiki / "_pending_questions.md").exists()

    def test_no_duplicate_cache_rows_on_repeat_run(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """Running the pass a second time must NOT append a second cache
        row for the same pair (dedup bounds file growth)."""
        from unittest.mock import MagicMock

        # Run 1: detector + resolver-suppress → records one row.
        client1 = MagicMock()
        client1.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
            self._make_response(self.SUPPRESS_PAYLOAD),
        ]
        merge_clusters_to_wiki(contradiction_merge_root, client=client1)
        assert len(self._cache_rows(contradiction_merge_root)) == 1

        # Run 2: cache hit → only the detector runs; no new row appended.
        client2 = MagicMock()
        client2.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]
        merge_clusters_to_wiki(contradiction_merge_root, client=client2)

        rows = self._cache_rows(contradiction_merge_root)
        assert len(rows) == 1
        assert client2.messages.create.call_count == 1

    def test_material_edit_reescalates(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """A material edit to a passage changes the fingerprint → cache
        miss → the resolver IS called for the new pair."""
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import record_resolution

        # Seed the cache with the ORIGINAL pair's fingerprint.
        record_resolution(
            contradiction_merge_root,
            fingerprint=self._expected_fingerprint(),
            verdict="not_a_conflict",
            resolved_by="auto",
        )

        # Detector now reports a MATERIALLY different passage B → different
        # fingerprint → not in the cache → resolver must run.
        edited_detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"Commit prior-session debris directly to develop.", '
            '"Stash prior-session debris on a feature branch instead."], '
            '"rationale": "One says commit; the other says stash on a branch."}'
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(edited_detector_payload),
            self._make_response(self.SUPPRESS_PAYLOAD),
        ]

        merge_clusters_to_wiki(contradiction_merge_root, client=fake_client)

        # Both calls fired: detector AND the Opus confirmation (cache miss).
        assert fake_client.messages.create.call_count == 2

    def test_human_verdict_not_short_circuited(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """A pair settled by a HUMAN ``keep_a`` verdict is NOT in the
        ``not_a_conflict`` skip set, so the resolver still runs (the verdict
        can flow to tier4_escalate for enactment). Issue #249's scoping
        refinement: only ``not_a_conflict`` verdicts short-circuit Opus."""
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import record_resolution

        # Human keep_a verdict on the SAME pair — NOT a not_a_conflict clear.
        record_resolution(
            contradiction_merge_root,
            fingerprint=self._expected_fingerprint(),
            verdict="keep_a",
            resolved_by="human",
        )

        keep_payload = (
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.92, "rationale": "Real conflict; keep a.", '
            '"source_precedence_used": []}'
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
            self._make_response(keep_payload),
        ]

        merge_clusters_to_wiki(contradiction_merge_root, client=fake_client)

        # Resolver IS still called (detector + Opus) — the human verdict is
        # not in the not_a_conflict skip set.
        assert fake_client.messages.create.call_count == 2

    def test_cosmetic_edit_stays_cached(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """The mirror of ``test_material_edit_reescalates``: a COSMETIC edit
        (whitespace + case churn, substance identical) does NOT change the
        fingerprint, so the cache still hits → the Opus confirmation is
        skipped and no pending question is written.

        Together with ``test_material_edit_reescalates`` this pins the
        material-vs-cosmetic boundary at the MERGE layer, not just in the
        ``fingerprint.py`` unit tests.
        """
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import record_resolution

        # Seed the cache with the ORIGINAL pair's fingerprint.
        record_resolution(
            contradiction_merge_root,
            fingerprint=self._expected_fingerprint(),
            verdict="not_a_conflict",
            resolved_by="auto",
        )

        # Detector reports the SAME claims but re-cased and re-spaced —
        # `_normalize_claim` (casefold + whitespace collapse) maps these to
        # the same normalized strings, so the fingerprint is unchanged → the
        # cache still hits.
        cosmetic_detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v1.md", '
            '"-Users-tristankromer-Code/feedback_prior_session_debris_v2.md"], '
            '"conflicting_passages": ['
            '"  COMMIT   prior-session debris   directly to DEVELOP.  ", '
            '"park PRIOR-session   debris  on a WIP   branch."], '
            '"rationale": "Same claims, only whitespace/case churn."}'
        )
        # If the cache hit fails, the resolver would be consulted and the
        # one-element side_effect list runs dry → StopIteration surfaces.
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(cosmetic_detector_payload),
        ]

        entries = merge_clusters_to_wiki(contradiction_merge_root, client=fake_client)
        assert len(entries) == 1
        # Cache still hit (cosmetic-stable fingerprint) → entry not flagged.
        assert entries[0].contradictions_detected is False

        # Exactly one API call: the detector. No Opus confirmation call —
        # `propose_resolution` was never reached.
        assert fake_client.messages.create.call_count == 1

        wiki = contradiction_merge_root / "wiki"
        assert not (wiki / "_pending_questions.md").exists()

    def test_similarity_sweep_cache_hit_skips_opus(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The similarity-sweep branch routes through the SAME
        ``_maybe_propose`` cache gate as the per-cluster path: a swept pair
        whose ``not_a_conflict`` fingerprint is pre-seeded skips the Opus
        confirmation call and produces no pending question.

        Two members live in DIFFERENT scopes as singleton clusters, so the
        per-cluster pass makes zero detector calls — the only detection comes
        from the similarity sweep (mode=``similarity``). This guards against a
        future refactor that bypasses the shared chokepoint on the sweep path.
        """
        from unittest.mock import MagicMock

        from athenaeum import cross_scope as cs
        from athenaeum.fingerprint import claim_pair_fingerprint, record_resolution

        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "similarity")

        # Two singleton clusters in different scope branches (no ancestor
        # link) — the sweep is the only path that pairs them.
        knowledge_root = tmp_path / "knowledge"
        scope_a = (
            knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-foo"
        )
        scope_b = (
            knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-bar"
        )
        _write_am_file(
            scope_a,
            "feedback_commit_directly.md",
            frontmatter_name="Commit directly",
            description="commit directly",
            origin_session_id="s-foo",
            origin_turn=1,
            sources=[
                {"session": "s-foo", "turn": 1, "date": "2026-04-10", "excerpt": "x"}
            ],
            body=self.PASSAGE_A,
        )
        _write_am_file(
            scope_b,
            "feedback_park_on_wip.md",
            frontmatter_name="Park on WIP",
            description="park on WIP",
            origin_session_id="s-bar",
            origin_turn=1,
            sources=[
                {"session": "s-bar", "turn": 1, "date": "2026-04-11", "excerpt": "y"}
            ],
            body=self.PASSAGE_B,
        )
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "foo-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code-foo/feedback_commit_directly.md"
                    ],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                },
                {
                    "cluster_id": "bar-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code-bar/feedback_park_on_wip.md"
                    ],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                },
            ],
        )
        _write_config(knowledge_root)

        # Pre-seed the cache with the swept pair's not_a_conflict fingerprint.
        fingerprint = claim_pair_fingerprint(
            self.PASSAGE_A, self.PASSAGE_B, self.CONFLICT_TYPE
        )
        record_resolution(
            knowledge_root,
            fingerprint=fingerprint,
            verdict="not_a_conflict",
            resolved_by="auto",
        )

        # Inject a deterministic embedder so the sweep returns the pair
        # (mirrors test_cross_scope.TestModeWiring).
        a_id = "auto-memory/-Users-tristankromer-Code-foo/feedback_commit_directly.md"
        b_id = "auto-memory/-Users-tristankromer-Code-bar/feedback_park_on_wip.md"
        embedder = _StubEmbedder({a_id: [1.0, 0.0], b_id: [0.99, 0.05]})
        original_pairs = cs.cross_scope_similarity_pairs

        def patched_pairs(*args: object, **kwargs: object):
            kwargs["embedding_provider"] = embedder
            return original_pairs(*args, **kwargs)

        monkeypatch.setattr(
            "athenaeum.merge.cross_scope_similarity_pairs",
            patched_pairs,
        )

        detector_payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code-foo/feedback_commit_directly.md", '
            '"-Users-tristankromer-Code-bar/feedback_park_on_wip.md"], '
            '"conflicting_passages": ['
            f'"{self.PASSAGE_A}", '
            f'"{self.PASSAGE_B}"], '
            '"rationale": "One says commit directly; the other says park."}'
        )
        # Only the sweep's detector (Haiku) should fire. If the resolver
        # (Opus) is reached, the one-element side_effect list runs dry →
        # StopIteration surfaces.
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(detector_payload),
        ]

        merge_clusters_to_wiki(knowledge_root, client=fake_client)

        # Singletons skip per-cluster detection; the sweep makes the single
        # detector call. The cache hit at _maybe_propose skips the Opus
        # confirmation entirely.
        assert fake_client.messages.create.call_count == 1

        wiki = knowledge_root / "wiki"
        # Sweep escalation dropped by the shared cache gate → no question.
        assert not (wiki / "_pending_questions.md").exists()


class TestNotAConflictDecay:
    """Issue #251: read-time decay of stale auto ``not_a_conflict``
    suppressions. With a positive ``not_a_conflict_ttl_days``, an auto
    suppression older than the ttl is treated as ABSENT when building the
    skip set, so the pair re-enters the Opus confirmation pass. The cache
    file is never mutated — decay is a read-time interpretation.

    Reuses ``TestNotAConflictCache``'s payloads + fixture; ``now`` is
    injected into ``merge_clusters_to_wiki`` for determinism (no wall-clock).
    """

    NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _make_response(text: str):
        return TestNotAConflictCache._make_response(text)

    @classmethod
    def _expected_fingerprint(cls) -> str:
        return TestNotAConflictCache._expected_fingerprint()

    @staticmethod
    def _cache_rows(knowledge_root: Path) -> list[dict]:
        return TestNotAConflictCache._cache_rows(knowledge_root)

    @staticmethod
    def _stamp(now: datetime, days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    DETECTOR_PAYLOAD = TestNotAConflictCache.DETECTOR_PAYLOAD
    SUPPRESS_PAYLOAD = TestNotAConflictCache.SUPPRESS_PAYLOAD

    def _seed_auto_suppress(
        self,
        knowledge_root: Path,
        resolved_at: str,
        resolved_by: str = "auto",
        verdict: str = "not_a_conflict",
    ) -> None:
        from athenaeum.fingerprint import record_resolution

        record_resolution(
            knowledge_root,
            fingerprint=self._expected_fingerprint(),
            verdict=verdict,
            resolved_by=resolved_by,
            resolved_at=resolved_at,
        )

    def test_ttl_zero_old_row_still_suppresses(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """ttl_days unset/0 → byte-identical to today: a 40-day-old auto
        suppress still skips the Opus confirmation (only the detector fires)."""
        from unittest.mock import MagicMock

        self._seed_auto_suppress(contradiction_merge_root, self._stamp(self.NOW, 40))

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]

        merge_clusters_to_wiki(
            contradiction_merge_root, client=fake_client, now=self.NOW
        )
        # ttl=0 → still cached → only detector, no Opus.
        assert fake_client.messages.create.call_count == 1

    def test_expired_row_reenters_confirmation(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """ttl_days=30, auto suppress dated 40 days ago → NOT in the skip
        set → the pair re-enters the Opus confirmation (detector + Opus)."""
        from unittest.mock import MagicMock

        self._seed_auto_suppress(contradiction_merge_root, self._stamp(self.NOW, 40))

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
            self._make_response(self.SUPPRESS_PAYLOAD),
        ]

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
            config=cfg,
            now=self.NOW,
        )
        # Expired → cache miss → Opus confirmation runs.
        assert fake_client.messages.create.call_count == 2

    def test_recent_row_still_suppresses(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """ttl_days=30, auto suppress dated 10 days ago (< ttl) → still in
        the skip set → Opus is skipped (only the detector fires)."""
        from unittest.mock import MagicMock

        self._seed_auto_suppress(contradiction_merge_root, self._stamp(self.NOW, 10))

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
            config=cfg,
            now=self.NOW,
        )
        assert fake_client.messages.create.call_count == 1

    def test_human_verdict_never_decays(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """A HUMAN not_a_conflict verdict, even dated 400 days ago, is never
        decayed — it stays in the skip set so Opus is skipped."""
        from unittest.mock import MagicMock

        self._seed_auto_suppress(
            contradiction_merge_root,
            self._stamp(self.NOW, 400),
            resolved_by="human",
        )

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
            config=cfg,
            now=self.NOW,
        )
        # Human verdict never decays → still cached → no Opus.
        assert fake_client.messages.create.call_count == 1

    def test_undated_row_still_suppresses(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """A row with NO resolved_at (legacy/external) is fail-safe: treated
        as not stale, so it keeps suppressing even with a positive ttl."""
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import record_resolution

        # record_resolution stamps resolved_at by default; write an undated
        # row directly to exercise the missing-field path.
        path = contradiction_merge_root / "raw" / "_resolved_contradictions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fingerprint": self._expected_fingerprint(),
                    "action": "not_a_conflict",
                    "resolved_by": "auto",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # Reference the import so ruff/readers see why we bypassed it.
        assert record_resolution is not None

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=fake_client,
            config=cfg,
            now=self.NOW,
        )
        assert fake_client.messages.create.call_count == 1

    def test_refresh_resets_clock_newest_wins(
        self,
        contradiction_merge_root: Path,
    ) -> None:
        """After an expired pair re-clears, the fresh auto row supersedes the
        stale one (newest-wins) and the clock resets: a follow-up run finds
        the pair cached again and skips Opus.

        Run 1: stale (40d) row + ttl=30 → re-enters → Opus suppresses →
        appends a FRESH row stamped at ``now``. Run 2 (same ttl/now) →
        fresh row is < ttl → cached → Opus skipped."""
        from unittest.mock import MagicMock

        self._seed_auto_suppress(contradiction_merge_root, self._stamp(self.NOW, 40))

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }

        # Run 1: expired → Opus runs and re-clears → fresh row appended.
        client1 = MagicMock()
        client1.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
            self._make_response(self.SUPPRESS_PAYLOAD),
        ]
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=client1,
            config=cfg,
            now=self.NOW,
        )
        assert client1.messages.create.call_count == 2
        rows = self._cache_rows(contradiction_merge_root)
        # Append-only: the stale row stays as history; a fresh row is added.
        assert len(rows) == 2
        assert rows[-1]["resolved_at"] == self._stamp(self.NOW, 0)

        # Run 2: the fresh row (age 0 < ttl) now wins → cached → no Opus.
        client2 = MagicMock()
        client2.messages.create.side_effect = [
            self._make_response(self.DETECTOR_PAYLOAD),
        ]
        merge_clusters_to_wiki(
            contradiction_merge_root,
            client=client2,
            config=cfg,
            now=self.NOW,
        )
        assert client2.messages.create.call_count == 1

    def test_large_expired_backlog_respects_resolve_max_per_run(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """A large backlog of expired auto suppressions must not exceed
        ``resolve_max_per_run`` Opus calls in one run — re-validation flows
        through the existing per-run cap, spreading cost across nights."""
        from unittest.mock import MagicMock

        from athenaeum.fingerprint import claim_pair_fingerprint, record_resolution

        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"

        # Build N distinct clusters, each a unique conflicting pair. Seed an
        # EXPIRED auto suppress for every pair so all N would re-enter the
        # confirmation pass absent the cap.
        n_pairs = 6
        cap = 2
        cluster_rows: list[dict[str, object]] = []
        detector_payloads: list[str] = []
        for i in range(n_pairs):
            a_name = f"feedback_decay_a_{i}.md"
            b_name = f"feedback_decay_b_{i}.md"
            pa = f"Always rotate the widget clockwise (case {i})."
            pb = f"Always rotate the widget counter-clockwise (case {i})."
            _write_am_file(
                scope,
                a_name,
                frontmatter_name=f"Decay a {i}",
                description="clockwise",
                origin_session_id=f"s-a-{i}",
                origin_turn=1,
                sources=[{"session": f"s-a-{i}", "turn": 1}],
                body=pa,
            )
            _write_am_file(
                scope,
                b_name,
                frontmatter_name=f"Decay b {i}",
                description="counter",
                origin_session_id=f"s-b-{i}",
                origin_turn=1,
                sources=[{"session": f"s-b-{i}", "turn": 1}],
                body=pb,
            )
            cluster_rows.append(
                {
                    "cluster_id": f"code-decay-{i:04d}",
                    "member_paths": [
                        f"-Users-tristankromer-Code/{a_name}",
                        f"-Users-tristankromer-Code/{b_name}",
                    ],
                    "centroid_score": 0.60,
                    "rationale": "decay backlog fixture",
                }
            )
            fp = claim_pair_fingerprint(pa, pb, "prescriptive")
            record_resolution(
                knowledge_root,
                fingerprint=fp,
                verdict="not_a_conflict",
                resolved_by="auto",
                resolved_at=self._stamp(self.NOW, 40),
            )
            detector_payloads.append(
                json.dumps(
                    {
                        "detected": True,
                        "conflict_type": "prescriptive",
                        "members_involved": [
                            f"-Users-tristankromer-Code/{a_name}",
                            f"-Users-tristankromer-Code/{b_name}",
                        ],
                        "conflicting_passages": [pa, pb],
                        "rationale": "opposing rotation guidance",
                    }
                )
            )

        _write_cluster_jsonl(knowledge_root, cluster_rows)
        _write_config(knowledge_root)

        # Scripted client: every detector call returns detected=True; every
        # resolver call returns the suppress verdict. The detector + resolver
        # interleave per cluster, so script enough responses for the worst
        # case (all detectors + up-to-cap resolvers) and count resolver calls.
        monkeypatch.setenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", str(cap))

        resolver_calls = {"n": 0}

        def _side_effect(*args, **kwargs):
            body = json.dumps(kwargs)
            # Distinguish resolver (system prompt mentions "resolver") from
            # detector by inspecting the system text passed to the client.
            system = kwargs.get("system", "")
            if isinstance(system, str) and "resolver" in system.lower():
                resolver_calls["n"] += 1
                return self._make_response(self.SUPPRESS_PAYLOAD)
            # Detector: return the next opposing-pair payload. Any of them is
            # fine — the gate keys off the fingerprint of the returned pair.
            idx = min(_side_effect.detector_idx, len(detector_payloads) - 1)
            _side_effect.detector_idx += 1
            assert body is not None
            return self._make_response(detector_payloads[idx])

        _side_effect.detector_idx = 0

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = _side_effect

        cfg = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "contradiction": {"not_a_conflict_ttl_days": 30},
        }
        merge_clusters_to_wiki(
            knowledge_root,
            client=fake_client,
            config=cfg,
            now=self.NOW,
        )

        # The expired backlog re-enters, but the per-run cap bounds Opus
        # calls: never more than ``cap`` resolver invocations in one run.
        assert resolver_calls["n"] <= cap


# ---------------------------------------------------------------------------
# Cluster-cohesion floor (issue #278)
# ---------------------------------------------------------------------------


def _write_cohesion_fixture(knowledge_root: Path) -> None:
    """Synthesize four clusters spanning the cohesion-floor decision matrix.

    Rows written to the canonical cluster JSONL:

    * ``blend-0001`` -- LOW cohesion (0.42), 5 distinct origin scopes: the
      cross-scope over-cluster signature. Suppressed when the floor is on.
    * ``cohere-0001`` -- HIGH cohesion (0.88), 2 scopes: materializes.
    * ``single-0001`` -- singleton, cohesion 1.0, 1 scope: materializes.
    * ``lone-0001`` -- LOW cohesion (0.42) but a SINGLE scope: must NOT be
      suppressed (the gate requires multi-scope origin).
    """
    am_root = knowledge_root / "raw" / "auto-memory"

    # 1) Low-cohesion cross-scope over-cluster: one member per scope.
    blend_scopes = [
        "-auto-auth-git-staging",
        "-auto-staging-agent-worktree",
        "-auto-grn-staging-auth",
        "-auto-hermes-staging",
        "-auto-caveman-setup",
    ]
    blend_members: list[str] = []
    for i, scope in enumerate(blend_scopes):
        _write_am_file(
            am_root / scope,
            f"reference_blendalpha_{i}.md",
            frontmatter_name=f"blendalpha {i}",
            description="low-cohesion blend member",
            origin_session_id=f"s-blend-{i}",
            origin_turn=i,
            body=f"Blend member {i} from {scope}.",
        )
        blend_members.append(f"{scope}/reference_blendalpha_{i}.md")

    # 2) High-cohesion two-scope cluster.
    cohere_members: list[str] = []
    for i, scope in enumerate(["-auto-coherent-one", "-auto-coherent-two"]):
        _write_am_file(
            am_root / scope,
            f"project_coherentbeta_{i}.md",
            frontmatter_name=f"coherentbeta {i}",
            description="high-cohesion member",
            origin_session_id=f"s-cohere-{i}",
            origin_turn=i,
            body=f"Coherent beta member {i}.",
        )
        cohere_members.append(f"{scope}/project_coherentbeta_{i}.md")

    # 3) Coherent single-scope singleton (centroid 1.0).
    _write_am_file(
        am_root / "-auto-gamma-only",
        "project_singlegamma.md",
        frontmatter_name="singlegamma",
        description="singleton",
        origin_session_id="s-gamma",
        origin_turn=0,
        body="Single gamma fact.",
    )

    # 4) Low-cohesion SINGLE-scope cluster (two members, one scope).
    lone_members: list[str] = []
    for i in range(2):
        _write_am_file(
            am_root / "-auto-lonescope",
            f"project_lonescopedelta_{i}.md",
            frontmatter_name=f"lonescopedelta {i}",
            description="low-cohesion single-scope member",
            origin_session_id=f"s-lone-{i}",
            origin_turn=i,
            body=f"Lone-scope delta member {i}.",
        )
        lone_members.append(f"-auto-lonescope/project_lonescopedelta_{i}.md")

    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "blend-0001",
                "member_paths": blend_members,
                "centroid_score": 0.42,
                "rationale": "similarity; low-cohesion cross-scope blend",
            },
            {
                "cluster_id": "cohere-0001",
                "member_paths": cohere_members,
                "centroid_score": 0.88,
                "rationale": "cosine >= 0.55; coherent",
            },
            {
                "cluster_id": "single-0001",
                "member_paths": ["-auto-gamma-only/project_singlegamma.md"],
                "centroid_score": 1.0,
                "rationale": "singleton",
            },
            {
                "cluster_id": "lone-0001",
                "member_paths": lone_members,
                "centroid_score": 0.42,
                "rationale": "cosine >= 0.55; low-cohesion single-scope",
            },
        ],
    )
    _write_config(knowledge_root)


_FLOOR_ON_CFG = {
    "recall": {"extra_intake_roots": ["raw/auto-memory"]},
    "librarian": {"min_cluster_cohesion": 0.47, "min_cluster_cohesion_scopes": 4},
}
_FLOOR_OFF_CFG = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}


class TestClusterCohesionFloor:
    """Cohesion floor suppresses low-cohesion cross-scope over-clusters (#278)."""

    def _slugs_on_disk(self, knowledge_root: Path) -> set[str]:
        wiki = knowledge_root / "wiki"
        return {p.name for p in wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")}

    def test_low_cohesion_cross_scope_cluster_is_suppressed(
        self, tmp_path: Path, caplog
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_cohesion_fixture(knowledge_root)
        import logging

        with caplog.at_level(logging.INFO, logger="athenaeum.merge"):
            entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_ON_CFG)

        # The over-cluster is not in the returned list (so the retire pass
        # never retires its raw) and no page is written for it.
        cluster_ids = {e.cluster_id for e in entries}
        assert "blend-0001" not in cluster_ids
        slugs = self._slugs_on_disk(knowledge_root)
        assert not any("blendalpha" in s for s in slugs)

        # Raw members are left in place -- not lost, not retired.
        am_root = knowledge_root / "raw" / "auto-memory"
        for i in range(5):
            scope = [
                "-auto-auth-git-staging",
                "-auto-staging-agent-worktree",
                "-auto-grn-staging-auth",
                "-auto-hermes-staging",
                "-auto-caveman-setup",
            ][i]
            assert (am_root / scope / f"reference_blendalpha_{i}.md").exists()

        # The suppression is logged with cluster id + centroid + scope count.
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "SUPPRESSED" in msgs
        assert "blend-0001" in msgs
        assert "centroid=0.4200" in msgs
        assert "scopes=5" in msgs

    def test_high_cohesion_and_single_scope_clusters_materialize(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_cohesion_fixture(knowledge_root)
        entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_ON_CFG)
        cluster_ids = {e.cluster_id for e in entries}
        # High-cohesion (0.88) and the coherent singleton (1.0) materialize.
        assert "cohere-0001" in cluster_ids
        assert "single-0001" in cluster_ids
        slugs = self._slugs_on_disk(knowledge_root)
        assert any("coherentbeta" in s for s in slugs)
        assert any("singlegamma" in s for s in slugs)

    def test_low_cohesion_single_scope_cluster_is_not_suppressed(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_cohesion_fixture(knowledge_root)
        entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_ON_CFG)
        cluster_ids = {e.cluster_id for e in entries}
        # Low cohesion (0.42) but ONE scope -> below the scope floor -> kept.
        assert "lone-0001" in cluster_ids
        slugs = self._slugs_on_disk(knowledge_root)
        assert any("lonescopedelta" in s for s in slugs)

    def test_floor_off_by_default_materializes_over_cluster(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_cohesion_fixture(knowledge_root)
        # No min_cluster_cohesion configured -> default 0.0 (off) -> the
        # over-cluster materializes exactly as before this feature.
        entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_OFF_CFG)
        cluster_ids = {e.cluster_id for e in entries}
        assert cluster_ids == {"blend-0001", "cohere-0001", "single-0001", "lone-0001"}
        slugs = self._slugs_on_disk(knowledge_root)
        assert any("blendalpha" in s for s in slugs)

    def test_scope_count_boundary_pins_ge(self, tmp_path: Path) -> None:
        """min_cluster_cohesion_scopes default 4: a low-cohesion 3-scope cluster
        is KEPT, a low-cohesion 4-scope cluster is suppressed (>= boundary)."""
        knowledge_root = tmp_path / "knowledge"
        am_root = knowledge_root / "raw" / "auto-memory"
        three_members: list[str] = []
        for i in range(3):
            scope = f"-auto-three-{i}"
            _write_am_file(
                am_root / scope,
                f"reference_threecut_{i}.md",
                frontmatter_name=f"threecut {i}",
                body=f"three-scope member {i}",
            )
            three_members.append(f"{scope}/reference_threecut_{i}.md")
        four_members: list[str] = []
        for i in range(4):
            scope = f"-auto-four-{i}"
            _write_am_file(
                am_root / scope,
                f"reference_fourcut_{i}.md",
                frontmatter_name=f"fourcut {i}",
                body=f"four-scope member {i}",
            )
            four_members.append(f"{scope}/reference_fourcut_{i}.md")
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "three-scope",
                    "member_paths": three_members,
                    "centroid_score": 0.42,
                    "rationale": "low cohesion, 3 scopes",
                },
                {
                    "cluster_id": "four-scope",
                    "member_paths": four_members,
                    "centroid_score": 0.42,
                    "rationale": "low cohesion, 4 scopes",
                },
            ],
        )
        _write_config(knowledge_root)
        entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_ON_CFG)
        cluster_ids = {e.cluster_id for e in entries}
        # 3 scopes < floor(4) -> kept; 4 scopes >= floor(4) -> suppressed.
        assert "three-scope" in cluster_ids
        assert "four-scope" not in cluster_ids

    def test_suppressed_member_still_reachable_by_ancestor_detection(
        self, tmp_path: Path
    ) -> None:
        """Load-bearing property (matches the merge.py comment): a suppressed
        cluster's member stays in the discovered file list, so in DEFAULT
        ``ancestor`` mode it is pooled into a KEPT cluster whose scope it is an
        ancestor of -- and a contradiction against it still escalates.

        If the suppressed member were dropped from ``auto_memory_files``, the
        kept singleton would be a 1-member chunk, the detector would never run,
        and no pending question would be written. So asserting the escalation
        happens proves the member is still reachable.
        """
        from unittest.mock import MagicMock

        knowledge_root = tmp_path / "knowledge"
        am_root = knowledge_root / "raw" / "auto-memory"

        # Kept cluster: a coherent singleton in a deep scope.
        keep_scope = "-Users-tk-Code-proj"
        _write_am_file(
            am_root / keep_scope,
            "project_deploycadence.md",
            frontmatter_name="deploycadence keep",
            origin_session_id="s-keep",
            origin_turn=1,
            body="Always deploy on Fridays.",
        )
        keep_ref = f"{keep_scope}/project_deploycadence.md"

        # Suppressed low-cohesion cross-scope cluster. ONE member sits in an
        # ANCESTOR scope of the kept cluster (-Users-tk-Code) and contradicts
        # it; the other three are in unrelated scopes (4 scopes total).
        anc_scope = "-Users-tk-Code"
        _write_am_file(
            am_root / anc_scope,
            "feedback_deploycadence_v2.md",
            frontmatter_name="deploycadence anc",
            origin_session_id="s-anc",
            origin_turn=2,
            body="Never deploy on Fridays.",
        )
        anc_ref = f"{anc_scope}/feedback_deploycadence_v2.md"
        blend_members = [anc_ref]
        for i in range(3):
            scope = f"-auto-unrelated-{i}"
            _write_am_file(
                am_root / scope,
                f"reference_filler_{i}.md",
                frontmatter_name=f"filler {i}",
                body=f"unrelated filler {i}",
            )
            blend_members.append(f"{scope}/reference_filler_{i}.md")

        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "keep-0001",
                    "member_paths": [keep_ref],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                },
                {
                    "cluster_id": "blend-0001",
                    "member_paths": blend_members,
                    "centroid_score": 0.42,
                    "rationale": "low-cohesion cross-scope blend",
                },
            ],
        )
        _write_config(knowledge_root)

        # Detector returns a contradiction between the kept member and the
        # suppressed ancestor member (same client also feeds the resolver,
        # which falls through to escalate -- mirrors the positive-path test).
        payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            f'"members_involved": ["{keep_ref}", "{anc_ref}"], '
            '"conflicting_passages": ['
            '"Always deploy on Fridays.", "Never deploy on Fridays."], '
            '"rationale": "One says always; the other says never."}'
        )
        response = MagicMock()
        response.content = [MagicMock(text=payload)]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        entries = merge_clusters_to_wiki(
            knowledge_root, config=_FLOOR_ON_CFG, client=fake_client
        )
        cluster_ids = {e.cluster_id for e in entries}
        assert "keep-0001" in cluster_ids
        assert "blend-0001" not in cluster_ids

        # The suppressed ancestor member was pooled into the kept singleton and
        # the detector fired -> the kept entry is flagged and a pending question
        # is written. This is only possible if the suppressed raw stayed in the
        # discovered file list.
        kept = next(e for e in entries if e.cluster_id == "keep-0001")
        assert kept.contradictions_detected is True
        assert (knowledge_root / "wiki" / "_pending_questions.md").exists()
        # And the suppressed member's raw file is left in place on disk.
        assert (am_root / anc_scope / "feedback_deploycadence_v2.md").exists()

    def test_threshold_boundary_is_inclusive_keep(self, tmp_path: Path) -> None:
        """A cluster sitting EXACTLY at the floor materializes; just below is cut."""
        knowledge_root = tmp_path / "knowledge"
        am_root = knowledge_root / "raw" / "auto-memory"
        scopes = [
            "-auto-edge-one",
            "-auto-edge-two",
            "-auto-edge-three",
            "-auto-edge-four",
        ]
        at_members: list[str] = []
        below_members: list[str] = []
        for i, scope in enumerate(scopes):
            _write_am_file(
                am_root / scope,
                f"reference_edgeat_{i}.md",
                frontmatter_name=f"edgeat {i}",
                body=f"edge-at member {i}",
            )
            at_members.append(f"{scope}/reference_edgeat_{i}.md")
            _write_am_file(
                am_root / scope,
                f"reference_edgebelow_{i}.md",
                frontmatter_name=f"edgebelow {i}",
                body=f"edge-below member {i}",
            )
            below_members.append(f"{scope}/reference_edgebelow_{i}.md")
        _write_cluster_jsonl(
            knowledge_root,
            [
                {
                    "cluster_id": "edge-at",
                    "member_paths": at_members,
                    "centroid_score": 0.47,
                    "rationale": "exactly at floor",
                },
                {
                    "cluster_id": "edge-below",
                    "member_paths": below_members,
                    "centroid_score": 0.46,
                    "rationale": "just below floor",
                },
            ],
        )
        _write_config(knowledge_root)
        entries = merge_clusters_to_wiki(knowledge_root, config=_FLOOR_ON_CFG)
        cluster_ids = {e.cluster_id for e in entries}
        # centroid == floor materializes (inclusive-keep); centroid < floor cut.
        assert "edge-at" in cluster_ids
        assert "edge-below" not in cluster_ids
