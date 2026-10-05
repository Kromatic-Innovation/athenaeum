# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-memory merge pass (C3, issue athenaeum#197).

Covers :mod:`athenaeum.merge`. All fixtures synthesize a full
``raw/auto-memory/<scope>/`` tree plus a pre-written cluster JSONL
under ``tmp_path`` — the real ``~/knowledge/`` is never touched.

Load-bearing fixtures:

- ``voltaire_merge_root`` — 5 voltaire/nanoclaw files with real citation
  frontmatter under one cluster row. Regression guarantee: exactly one
  ``wiki/auto-voltaire*.md`` with 5 sources carrying session/turn/scope.
- ``contradiction_merge_root`` — two opposing-guidance files in one
  low-cohesion cluster. Since issue athenaeum#1256 retired the C4 detector
  from this module, the pass makes no LLM call and never flags anything —
  the fixture now pins the single emitted wiki entry as staying unflagged
  (``contradictions_detected: false``) by construction.
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
from athenaeum.search import FTS5Backend

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
                # Below the 0.75 cohesion threshold — load-bearing only for
                # tests that still consult CONTRADICTION_COHESION_THRESHOLD
                # directly; the merge pass itself no longer acts on this.
                "centroid_score": 0.62,
                "rationale": "cosine >= 0.55; shares tokens: prior, session, debris",
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
        assert meta["contradictions_detected"] is False  # no detector; never flagged
        assert meta["origin_scopes"] == ["-Users-tristankromer-Code-voltaire"]
        assert isinstance(meta["sources"], list)
        assert len(meta["sources"]) == 5


@pytest.fixture
def fused_unrelated_merge_root(tmp_path: Path) -> Path:
    """Two SEMANTICALLY UNRELATED memories fused into one cluster row.

    Reproduces the athenaeum#1596 worked example verbatim: a librarian design
    thesis and an unrelated "source pages are deliberate" ruling, fused at
    the observed centroid (0.6163) into one compiled page whose synthesized
    ``topic_slug`` matches neither member's written ``name:``.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-athenaeum"

    # Issue athenaeum#1596: filenames are deliberately UNRELATED to either
    # member's own written ``name:`` (below) and use only boring/short
    # tokens (``note``, ``alpha``, ``beta``) so ``derive_topic_slug`` cannot
    # accidentally pick up either member's real name as a token — that
    # would let an OR-of-terms FTS5 query resolve through ranking-token
    # leakage rather than through the ``aliases:`` fix under test (see the
    # FTS5 test below). Descriptions are likewise kept free of the two
    # members' name tokens for the same reason.
    _write_am_file(
        scope,
        "note_alpha.md",
        frontmatter_name="librarian-organisation-thesis",
        description="internal note about compilation ordering",
        origin_session_id="s-thesis",
        origin_turn=1,
        sources=[{"session": "s-thesis", "turn": 1, "date": "2026-09-10"}],
        body="The librarian should compile raw intake into durable wiki pages.",
    )
    _write_am_file(
        scope,
        "note_beta.md",
        frontmatter_name="source-pages-are-deliberate",
        description="policy note about a page format choice",
        origin_session_id="s-source",
        origin_turn=1,
        sources=[{"session": "s-source", "turn": 1, "date": "2026-09-10"}],
        body="A thin type: source page with no elaboration is intentional.",
    )

    _write_cluster_jsonl(
        knowledge_root,
        [
            {
                "cluster_id": "Users-tristankromer-Code-athenaeum-2a79a9d5",
                "member_paths": [
                    "-Users-tristankromer-Code-athenaeum/note_alpha.md",
                    "-Users-tristankromer-Code-athenaeum/note_beta.md",
                ],
                "centroid_score": 0.6163,
                "rationale": "reproduces athenaeum#1596's worked example",
            },
        ],
    )
    _write_config(knowledge_root)
    return knowledge_root


class TestFusedClusterAddressability:
    """Issue athenaeum#1596 AC1/AC2/AC4: a fused page must stay addressable
    under EVERY original member name it was written under, not just its
    synthesized ``topic_slug``.

    The eval-first shape: two unrelated raw memories fuse (as they do on
    unmodified develop, at this cluster's real centroid), and neither
    original name may become unreachable by exact-name lookup.
    """

    def test_fusion_actually_happens_and_slug_matches_neither_member(
        self, fused_unrelated_merge_root: Path
    ) -> None:
        """Sanity check the fixture reproduces the bug's precondition."""
        entries = merge_clusters_to_wiki(fused_unrelated_merge_root)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.topic_slug != "librarian-organisation-thesis"
        assert entry.topic_slug != "source-pages-are-deliberate"

    def test_fused_page_frontmatter_preserves_both_member_names_as_aliases(
        self, fused_unrelated_merge_root: Path
    ) -> None:
        merge_clusters_to_wiki(fused_unrelated_merge_root)
        wiki = fused_unrelated_merge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        aliases = meta.get("aliases") or []
        assert "librarian-organisation-thesis" in aliases
        assert "source-pages-are-deliberate" in aliases

    def test_both_original_names_resolve_via_fts5_search(
        self, fused_unrelated_merge_root: Path, tmp_path: Path
    ) -> None:
        """The binding eval: exact-name lookup must reach the fused page.

        This is the assertion that FAILS on unmodified develop (the
        compiled page carries no ``aliases:`` at all, so an FTS5 MATCH on
        either original name returns nothing) and PASSES once
        ``render_merged_entry`` writes them.
        """
        merge_clusters_to_wiki(fused_unrelated_merge_root)
        wiki = fused_unrelated_merge_root / "wiki"
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki, cache)

        for original_name in (
            "librarian-organisation-thesis",
            "source-pages-are-deliberate",
        ):
            results = backend.query(original_name, cache)
            filenames = {r[0] for r in results}
            assert filenames, (
                f"exact-name query {original_name!r} returned no hits — "
                "the fused page is unreachable under a name its author wrote"
            )
            assert any(f.startswith(AUTO_WIKI_PREFIX) for f in filenames)


class TestContradictionFixture:
    """Issue athenaeum#1256 retired the C4 detector/resolver loop from merge.py.

    The merge pass itself never populates ``contradictions_detected`` or
    ``contradiction`` anymore — it makes no LLM call — so with no detector
    to flag anything, the wiki entry stays unflagged by construction. The
    ``contradiction`` dataclass fields and the ``CONTRADICTION_COHESION_THRESHOLD``
    constant are kept on the API for backwards compatibility only.
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
# Issue athenaeum#181: self-reference lint applies to cluster-shim path
# ---------------------------------------------------------------------------


class TestClusterShimSelfReferenceLint:
    """The shim branch in :func:`merge_cluster_row` builds an
    :class:`AutoMemoryFile` on the fly when a cluster row references a
    file that C1 didn't discover. That branch must apply the same
    self-reference lint as the discovery path (issue athenaeum#181)."""

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
# Cluster-cohesion floor (issue athenaeum#278)
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
    """Cohesion floor suppresses low-cohesion cross-scope over-clusters (athenaeum#278)."""

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
