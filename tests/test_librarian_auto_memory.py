"""Tests for auto-memory ingest path (C1, issue #195).

Covers :func:`athenaeum.librarian.discover_auto_memory_files` and the
:class:`athenaeum.models.AutoMemoryFile` record schema. The ingest path
is a parallel sibling to :func:`discover_raw_files` — these tests verify
prefix matching, exclusion contracts, scope preservation, and
entity-schema isolation (no collision with ``RAW_FILE_RE``).

Clustering (C2) and merge (C3) are out of scope for this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Synthetic tree fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def auto_memory_root(tmp_path: Path) -> Path:
    """Build a synthetic ``knowledge/raw/auto-memory/`` tree for tests.

    Mirrors the production layout exactly: scope directories are named
    after the path-hash identifier (``-Users-tristankromer-Code-voltaire``
    style), ``_unscoped/`` is a real scope dir, ``MEMORY.md`` sits in each
    scope, and ``_migration-log.jsonl`` lives at the auto-memory root.
    Returns the ``knowledge_root`` (parent of ``raw/``) — tests pass this
    straight to :func:`discover_auto_memory_files`.
    """
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"
    auto.mkdir(parents=True)

    # Write a config that opts into raw/auto-memory as an extra intake
    # root (matches the shipped default, but we declare it explicitly so
    # the test doesn't depend on defaults drifting).
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )

    # Scope 1: voltaire — 5 project_* + 1 typo clone
    voltaire = auto / "-Users-tristankromer-Code-voltaire"
    voltaire.mkdir()
    for i in range(5):
        (voltaire / f"project_voltaire_part{i}.md").write_text(
            "---\n"
            f"name: Voltaire component {i}\n"
            f"description: Description {i}\n"
            "type: project\n"
            "originSessionId: abc123\n"
            "---\n"
            f"Body for part {i}.\n",
            encoding="utf-8",
        )
    # Typo clone — prefix strict, body tolerant
    (voltaire / "project_voltair_nanoclaw.md").write_text(
        "---\nname: Voltair typo\ntype: project\n---\nTypo body.\n",
        encoding="utf-8",
    )
    # MEMORY.md — MUST be excluded
    (voltaire / "MEMORY.md").write_text(
        "---\nname: MEMORY\n---\nTable of contents.\n",
        encoding="utf-8",
    )

    # Scope 2: _unscoped — has originSessionId even though scope=null
    unscoped = auto / "_unscoped"
    unscoped.mkdir()
    (unscoped / "feedback_unscoped_rule_a.md").write_text(
        "---\n"
        "name: Unscoped feedback A\n"
        "type: feedback\n"
        "originSessionId: ffff1111\n"
        "---\nUnscoped body A.\n",
        encoding="utf-8",
    )
    (unscoped / "feedback_unscoped_rule_b.md").write_text(
        "---\n"
        "name: Unscoped feedback B\n"
        "type: feedback\n"
        "originSessionId: ffff2222\n"
        "---\nUnscoped body B.\n",
        encoding="utf-8",
    )

    # Scope 3: some-scope — user + reference + Recall_ (capital R)
    some = auto / "some-scope"
    some.mkdir()
    (some / "user_tristan_profile.md").write_text(
        "---\nname: Tristan profile\ntype: user\n---\nProfile body.\n",
        encoding="utf-8",
    )
    (some / "reference_sentry_projects.md").write_text(
        "---\nname: Sentry projects\ntype: reference\nsources:\n  - sentry.io\n---\nRef body.\n",
        encoding="utf-8",
    )
    (some / "Recall_architecture.md").write_text(
        "---\nname: Recall arch\ntype: reference\n---\nRecall body.\n",
        encoding="utf-8",
    )

    # Sibling file at auto-memory root — MUST be excluded by
    # directory-only iteration. If the scanner mistakenly descended into
    # non-directories we'd pick this up as a file (but it's not .md
    # anyway — belt and suspenders).
    (auto / "_migration-log.jsonl").write_text(
        '{"ts": "2026-04-21T00:00:00Z", "op": "test"}\n',
        encoding="utf-8",
    )

    # Also plant an entity-schema file in one scope to verify the
    # auto-memory regex does NOT match it (cross-schema isolation).
    (voltaire / "20260422T120000Z-a1b2c3d4.md").write_text(
        "---\nuid: a1b2c3d4\nname: Entity-format stray\n---\nBody.\n",
        encoding="utf-8",
    )

    return knowledge_root


# ---------------------------------------------------------------------------
# Regex semantics
# ---------------------------------------------------------------------------


class TestAutoMemoryFileRegex:
    def test_matches_all_five_prefixes(self) -> None:
        from athenaeum.librarian import AUTO_MEMORY_FILE_RE

        for name in (
            "feedback_x.md",
            "project_y.md",
            "reference_z.md",
            "user_tristan.md",
            "Recall_architecture.md",
        ):
            assert AUTO_MEMORY_FILE_RE.match(name), f"should match: {name}"

    def test_tolerant_to_typo_body(self) -> None:
        from athenaeum.librarian import AUTO_MEMORY_FILE_RE

        # Typo clone must still match — dedup is C2's job, not the regex.
        assert AUTO_MEMORY_FILE_RE.match("project_voltair_nanoclaw.md")

    def test_rejects_entity_schema_filenames(self) -> None:
        from athenaeum.librarian import AUTO_MEMORY_FILE_RE, RAW_FILE_RE

        # Entity-schema files (<timestamp>-<uuid8>.md) must NOT be
        # picked up by the auto-memory regex, and vice versa. This is
        # the load-bearing isolation guarantee.
        entity_name = "20260422T120000Z-a1b2c3d4.md"
        assert RAW_FILE_RE.match(entity_name)
        assert AUTO_MEMORY_FILE_RE.match(entity_name) is None

    def test_rejects_memory_md(self) -> None:
        from athenaeum.librarian import AUTO_MEMORY_FILE_RE

        assert AUTO_MEMORY_FILE_RE.match("MEMORY.md") is None


# ---------------------------------------------------------------------------
# discover_auto_memory_files
# ---------------------------------------------------------------------------


class TestDiscoverAutoMemoryFiles:
    def test_discovers_files_across_scopes(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        # voltaire: 5 project + 1 typo clone = 6
        # _unscoped: 2 feedback
        # some-scope: user + reference + Recall = 3
        # MEMORY.md excluded (one per scope would otherwise = +3)
        # Entity-schema file excluded by regex = +0
        assert len(files) == 11

    def test_typo_clone_ingested_as_distinct_record(
        self,
        auto_memory_root: Path,
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        names = {f.path.name for f in files}
        # Dedup is C2's job — the ingest path must surface the typo
        # as its own record.
        assert "project_voltaire_part0.md" in names
        assert "project_voltair_nanoclaw.md" in names

    def test_memory_md_excluded(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        assert not any(f.path.name == "MEMORY.md" for f in files)

    def test_migration_log_excluded(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        assert not any(f.path.name == "_migration-log.jsonl" for f in files)
        # And it wasn't somehow reinterpreted as a scope dir:
        assert "_migration-log.jsonl" not in {f.origin_scope for f in files}

    def test_unscoped_is_included(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        unscoped = [f for f in files if f.origin_scope == "_unscoped"]
        assert len(unscoped) == 2

    def test_unscoped_preserves_origin_session_id(
        self,
        auto_memory_root: Path,
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        unscoped = {f.path.name: f for f in files if f.origin_scope == "_unscoped"}
        # _unscoped files have originSessionId populated even though the
        # scope itself is _unscoped — the AC from the issue body
        # explicitly calls this out.
        assert unscoped["feedback_unscoped_rule_a.md"].origin_session_id == "ffff1111"
        assert unscoped["feedback_unscoped_rule_b.md"].origin_session_id == "ffff2222"

    def test_scope_preserved_verbatim(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        scopes = {f.origin_scope for f in files}
        # Scope dirname is the canonical identifier — path-hash kept
        # in full, not normalized.
        assert "-Users-tristankromer-Code-voltaire" in scopes
        assert "_unscoped" in scopes
        assert "some-scope" in scopes

    def test_memory_type_extracted_from_prefix(
        self,
        auto_memory_root: Path,
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        by_name = {f.path.name: f for f in files}
        assert by_name["project_voltaire_part0.md"].memory_type == "project"
        assert by_name["feedback_unscoped_rule_a.md"].memory_type == "feedback"
        assert by_name["user_tristan_profile.md"].memory_type == "user"
        assert by_name["reference_sentry_projects.md"].memory_type == "reference"
        # Capital-R Recall normalizes to lowercase.
        assert by_name["Recall_architecture.md"].memory_type == "recall"

    def test_frontmatter_fields_preserved(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        by_name = {f.path.name: f for f in files}
        ref = by_name["reference_sentry_projects.md"]
        assert ref.sources == ["sentry.io"]
        assert ref.name == "Sentry projects"

    def test_entity_schema_file_ignored(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        # The planted 20260422T120000Z-a1b2c3d4.md must NOT appear —
        # it doesn't match AUTO_MEMORY_FILE_RE. The entity-intake path
        # owns those files; auto-memory must not poach them.
        assert not any(f.path.name.startswith("20260422T120000Z") for f in files)

    def test_missing_knowledge_root_returns_empty(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        # A knowledge root with no raw/auto-memory subtree at all —
        # resolve_extra_intake_roots will warn and return []; discovery
        # must return an empty list, not crash.
        bare = tmp_path / "bare"
        bare.mkdir()
        assert discover_auto_memory_files(bare) == []


# ---------------------------------------------------------------------------
# AutoMemoryFile record schema
# ---------------------------------------------------------------------------


class TestAutoMemoryRecord:
    def test_ref_format(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        unscoped = [f for f in files if f.origin_scope == "_unscoped"]
        assert unscoped[0].ref.startswith("_unscoped/")
        assert unscoped[0].ref.endswith(".md")

    def test_content_accessor(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        target = next(f for f in files if f.path.name == "project_voltaire_part0.md")
        assert "Body for part 0." in target.content


# ---------------------------------------------------------------------------
# Issue #173: self-reference in refines / supersedes is dropped + warned
# ---------------------------------------------------------------------------


class TestSelfReferenceLint:
    def _make_root(self, tmp_path: Path, frontmatter: str) -> Path:
        knowledge_root = tmp_path / "knowledge"
        auto = knowledge_root / "raw" / "auto-memory"
        scope = auto / "_unscoped"
        scope.mkdir(parents=True)
        (knowledge_root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
            encoding="utf-8",
        )
        (scope / "project_self.md").write_text(
            f"---\n{frontmatter}---\nBody.\n",
            encoding="utf-8",
        )
        return knowledge_root

    def test_refines_self_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        root = self._make_root(
            tmp_path,
            "name: Self Memory\n"
            "description: d\n"
            "type: project\n"
            "refines:\n"
            "  - Self Memory\n"
            "  - Other Memory\n",
        )
        with caplog.at_level("WARNING"):
            files = discover_auto_memory_files(root)
        assert len(files) == 1
        assert files[0].refines == ["Other Memory"]
        assert any("refines self" in r.message for r in caplog.records)

    def test_supersedes_self_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        root = self._make_root(
            tmp_path,
            "name: Self Memory\n"
            "description: d\n"
            "type: project\n"
            "supersedes:\n"
            "  - name: Self Memory\n"
            "    as_of: 2026-01-01\n"
            "    reason: typo\n"
            "  - name: Other Memory\n"
            "    as_of: 2026-01-02\n"
            "    reason: real\n",
        )
        with caplog.at_level("WARNING"):
            files = discover_auto_memory_files(root)
        assert len(files) == 1
        assert [s["name"] for s in files[0].supersedes] == ["Other Memory"]
        assert any("supersedes self" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Issue #260: origin-traced source footnotes (source_type / source_ref)
# ---------------------------------------------------------------------------


class TestSourceTypeSchema:
    """``_parse_one_source`` carries source_type + source_ref (slice A, #260)."""

    def test_dict_source_carries_type_and_ref(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source(
            {
                "session": "abc123",
                "turn": 4,
                "source_type": "user-stated",
                "source_ref": "abc123#turn4",
            },
            "some-scope",
        )
        assert parsed is not None
        assert parsed["source_type"] == "user-stated"
        assert parsed["source_ref"] == "abc123#turn4"

    def test_missing_type_defaults_inferred(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source({"session": "abc123", "turn": 2}, "some-scope")
        assert parsed is not None
        assert parsed["source_type"] == "inferred"
        # A missing source_ref is back-filled from session+turn, never blank.
        assert parsed["source_ref"] == "abc123#turn2"

    def test_invalid_type_coerced_to_inferred(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source(
            {"session": "abc123", "source_type": "wishful-thinking"},
            "some-scope",
        )
        assert parsed is not None
        assert parsed["source_type"] == "inferred"

    def test_bare_string_source_is_inferred(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source("abc123", "some-scope")
        assert parsed is not None
        assert parsed["source_type"] == "inferred"
        # The legacy bare-UUID ref is the session id — never a filename.
        assert parsed["source_ref"] == "abc123"

    def test_source_ref_is_never_the_raw_filename(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source(
            {"session": "abc123", "turn": 1, "origin_scope": "some-scope"},
            "some-scope",
        )
        assert parsed is not None
        assert "auto-memory" not in parsed["source_ref"]
        assert not parsed["source_ref"].endswith(".md")

    def test_explicit_filename_source_ref_is_rejected(self) -> None:
        from athenaeum.merge import _parse_one_source

        # The DANGEROUS path (Quine M1): a producer explicitly stamps a raw
        # auto-memory filename into source_ref. The guard must reject it and
        # fall back to the safe session+turn ref — not pass it through.
        parsed = _parse_one_source(
            {
                "session": "abc123",
                "turn": 7,
                "origin_scope": "some-scope",
                "source_ref": "raw/auto-memory/some-scope/user_tristan_address.md",
            },
            "some-scope",
        )
        assert parsed is not None
        assert parsed["source_ref"] == "abc123#turn7"
        assert "auto-memory" not in parsed["source_ref"]
        assert not parsed["source_ref"].endswith(".md")

    def test_explicit_bare_md_source_ref_is_rejected(self) -> None:
        from athenaeum.merge import _parse_one_source

        # Even a non-auto-memory ``.md`` ref is filename-shaped and rejected.
        parsed = _parse_one_source(
            {"session": "sess9", "source_ref": "user_profile.md"},
            "some-scope",
        )
        assert parsed is not None
        assert parsed["source_ref"] == "sess9"

    def test_turn_zero_ref_is_preserved(self) -> None:
        from athenaeum.merge import _parse_one_source

        # Guard against a future ``if turn:`` regression — turn 0 is a real
        # turn and must render ``#turn0``, not collapse to the bare session.
        parsed = _parse_one_source({"session": "abc123", "turn": 0}, "some-scope")
        assert parsed is not None
        assert parsed["source_ref"] == "abc123#turn0"


class TestDedupeSourcesProvenance:
    """``dedupe_sources`` collapses same-(session,turn) to the FIRST entry."""

    def test_first_provenance_wins_on_collision(self) -> None:
        from athenaeum.merge import dedupe_sources

        # Same (session, turn), different provenance. The dedupe key ignores
        # source_type/source_ref, so the FIRST entry (input order) wins.
        deduped = dedupe_sources(
            [
                {"session": "s1", "turn": 2, "source_type": "user-stated"},
                {"session": "s1", "turn": 2, "source_type": "inferred"},
            ]
        )
        assert len(deduped) == 1
        assert deduped[0]["source_type"] == "user-stated"


class TestSourceFootnoteRendering:
    """``render_merged_entry`` renders origin-traced footnotes into the body."""

    def _entry(self) -> "object":
        from athenaeum.merge import MergedWikiEntry

        return MergedWikiEntry(
            topic_slug="tristan-profile",
            cluster_id="some-scope-1",
            cluster_centroid_score=1.0,
            contradictions_detected=False,
            origin_scopes=["some-scope"],
            sources=[
                {
                    "session": "abc123",
                    "turn": 4,
                    "origin_scope": "some-scope",
                    "source_type": "user-stated",
                    "source_ref": "abc123#turn4",
                },
                {
                    "session": "def456",
                    "origin_scope": "some-scope",
                    "source_type": "external",
                    "source_ref": "https://www.hbs.edu/startup",
                },
            ],
            body="Tristan's profile.\n",
        )

    def test_footnotes_rendered_in_body(self) -> None:
        from athenaeum.merge import render_merged_entry

        out = render_merged_entry(self._entry())
        # Footnote definitions carrying source_type + source_ref appear in
        # the body, matching the worked-example ``[^name]: **Source:**`` style.
        assert "[^src-1]:" in out
        assert "**Source:**" in out
        assert "user-stated" in out
        assert "abc123#turn4" in out
        assert "external" in out
        assert "https://www.hbs.edu/startup" in out

    def test_footnotes_never_cite_raw_filename(self) -> None:
        from athenaeum.merge import render_merged_entry, render_source_footnotes

        out = render_merged_entry(self._entry())
        footnotes = render_source_footnotes(self._entry().sources)
        assert footnotes.strip() != ""
        # The ultimate-source rule: the raw auto-memory path is never cited.
        assert "raw/auto-memory" not in out
        assert ".md`" not in footnotes

    def test_round_trip_type_and_ref_through_frontmatter(self) -> None:
        from athenaeum.merge import _parse_one_source, render_merged_entry
        from athenaeum.models import parse_frontmatter

        out = render_merged_entry(self._entry())
        meta, _body = parse_frontmatter(out)
        sources = meta["sources"]
        assert sources[0]["source_type"] == "user-stated"
        assert sources[0]["source_ref"] == "abc123#turn4"
        # Re-parsing the rendered frontmatter preserves the new fields.
        reparsed = _parse_one_source(sources[0], "some-scope")
        assert reparsed is not None
        assert reparsed["source_type"] == "user-stated"
        assert reparsed["source_ref"] == "abc123#turn4"

    def test_footnotes_are_a_trailing_appendix_not_inline(self) -> None:
        from athenaeum.merge import render_merged_entry

        # Slice-A contract (Quine S2): footnotes are a trailing sources
        # APPENDIX. The original body text precedes all [^src-N] definitions,
        # and the synthesized body carries no inline [^src-N] reference marker
        # attached to the fact (per-fact inline attachment is slice B).
        out = render_merged_entry(self._entry())
        body_marker = out.index("Tristan's profile.")
        first_footnote = out.index("[^src-1]:")
        assert body_marker < first_footnote
        # The body paragraph itself has no inline [^src reference appended.
        body_line = out[body_marker : out.index("\n", body_marker)]
        assert "[^src" not in body_line


class TestAutoMemoryFileSourceFields:
    """``AutoMemoryFile`` threads source_type / source_ref from frontmatter."""

    def test_defaults_to_inferred_when_absent(self, auto_memory_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        by_name = {f.path.name: f for f in files}
        # Files without explicit citation provenance default to inferred.
        assert by_name["user_tristan_profile.md"].source_type == "inferred"

    def test_parses_explicit_source_fields(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "some-scope"
        scope.mkdir(parents=True)
        (knowledge_root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
            encoding="utf-8",
        )
        (scope / "user_addr.md").write_text(
            "---\n"
            "name: Address\n"
            "type: user\n"
            "source_type: user-stated\n"
            "source_ref: sess9#turn3\n"
            "---\nHome address.\n",
            encoding="utf-8",
        )
        files = discover_auto_memory_files(knowledge_root)
        assert len(files) == 1
        assert files[0].source_type == "user-stated"
        assert files[0].source_ref == "sess9#turn3"


# ---------------------------------------------------------------------------
# Issue #261: move-then-retire lifecycle (slice B of #259)
# ---------------------------------------------------------------------------


import json  # noqa: E402
import subprocess  # noqa: E402


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init(root: Path) -> None:
    """Initialize a git repo with an identity and an initial commit of everything."""
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Retire Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed raw intake")


def _write_am(
    scope_dir: Path,
    filename: str,
    *,
    name: str,
    session: str,
    turn: int,
    body: str,
    extra_fm: str = "",
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: feedback\n"
        f"originSessionId: {session}\n"
        f"originTurn: {turn}\n"
        "sources:\n"
        f"  - session: {session}\n"
        f"    turn: {turn}\n"
        f"{extra_fm}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )


def _write_clusters(knowledge_root: Path, rows: list[dict]) -> None:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def retire_root(tmp_path: Path) -> Path:
    """A git-initialized knowledge root with one non-contradictory cluster.

    A single ``berlin`` memory in one scope, clustered as a singleton. With no
    Anthropic client the C4 detector returns ``detected=False`` so the cluster
    is eligible for move-then-retire.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-home"
    _write_am(
        scope,
        "user_tristan_berlin_address.md",
        name="Tristan Berlin address",
        session="sess-berlin",
        turn=3,
        body="Tristan lives in Berlin, Germany.",
    )
    _write_clusters(
        knowledge_root,
        [
            {
                "cluster_id": "home-0001",
                "member_paths": [
                    "-Users-tristankromer-Code-home/user_tristan_berlin_address.md"
                ],
                "centroid_score": 1.0,
                "rationale": "singleton",
            }
        ],
    )
    _write_config(knowledge_root)
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    _git_init(knowledge_root)
    return knowledge_root


def _raw_file(knowledge_root: Path) -> Path:
    return (
        knowledge_root
        / "raw"
        / "auto-memory"
        / "-Users-tristankromer-Code-home"
        / "user_tristan_berlin_address.md"
    )


class TestRetireNonContradictory:
    def test_noncontradictory_raw_is_moved_and_git_removed(
        self, retire_root: Path
    ) -> None:
        from athenaeum.merge import AUTO_WIKI_PREFIX, merge_clusters_to_wiki
        from athenaeum.models import parse_frontmatter
        from athenaeum.retire import run_retire_pass

        entries = merge_clusters_to_wiki(retire_root)
        report = run_retire_pass(entries, retire_root)

        raw = _raw_file(retire_root)
        # The raw file is git-removed from the working tree...
        assert not raw.exists()
        assert report.committed is True
        assert len(report.moved) == 1

        # ...but recoverable from git history.
        log = _git(retire_root, "log", "--diff-filter=D", "--name-only", "--format=")
        assert "user_tristan_berlin_address.md" in log.stdout

        # The fact + an origin-traced footnote now live in the wiki entry.
        wiki = retire_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        text = entry_file.read_text(encoding="utf-8")
        assert "Berlin" in text
        assert "[^src-1]:" in text
        assert "**Source:**" in text
        meta, _ = parse_frontmatter(text)
        assert meta["retired"] is True
        # The ultimate-source invariant: never cite the raw filename.
        assert "raw/auto-memory" not in text

    def test_wiki_update_and_deletion_land_in_one_commit(
        self, retire_root: Path
    ) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import run_retire_pass

        entries = merge_clusters_to_wiki(retire_root)
        run_retire_pass(entries, retire_root)

        # The HEAD commit (commit B) must contain BOTH the wiki write and the
        # raw deletion together.
        show = _git(retire_root, "show", "--stat", "--format=", "HEAD")
        assert "wiki/auto-" in show.stdout
        assert "user_tristan_berlin_address.md" in show.stdout
        status = _git(retire_root, "status", "--porcelain")
        assert status.stdout.strip() == ""

    def test_second_run_is_a_noop(self, retire_root: Path) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import run_retire_pass

        entries = merge_clusters_to_wiki(retire_root)
        run_retire_pass(entries, retire_root)
        head_after_first = _git(retire_root, "rev-parse", "HEAD").stdout.strip()

        # Re-merge: the member is gone, so no entry resolves; retire is a no-op.
        entries2 = merge_clusters_to_wiki(retire_root)
        report2 = run_retire_pass(entries2, retire_root)
        head_after_second = _git(retire_root, "rev-parse", "HEAD").stdout.strip()

        assert report2.moved == []
        assert report2.committed is False
        assert head_after_first == head_after_second

    def test_recall_over_retired_tree_still_surfaces_fact(
        self, retire_root: Path
    ) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import run_retire_pass
        from athenaeum.search import FTS5Backend

        entries = merge_clusters_to_wiki(retire_root)
        run_retire_pass(entries, retire_root)

        wiki_root = retire_root / "wiki"
        extra_roots = [retire_root / "raw" / "auto-memory"]
        cache_dir = retire_root / ".cache"
        backend = FTS5Backend()
        backend.build_index(wiki_root, cache_dir, extra_roots=extra_roots)

        hits = backend.query("berlin", cache_dir, n=5)
        filenames = {h[0] for h in hits}
        # The fact still surfaces — via the compiled wiki entry, not the
        # (now-retired) raw intake file.
        assert any(f.startswith("auto-") for f in filenames)
        assert not any("auto-memory/" in f for f in filenames)


class TestRetireContradictory:
    def test_contradictory_raw_is_held_not_deleted(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import HOLD, run_retire_pass

        knowledge_root = tmp_path / "knowledge"
        scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code"
        a = _write_am(
            scope,
            "feedback_commit_v1.md",
            name="Commit directly",
            session="s-111",
            turn=1,
            body="Commit prior-session debris directly to develop.",
        )
        b = _write_am(
            scope,
            "feedback_commit_v2.md",
            name="Park on WIP",
            session="s-222",
            turn=2,
            body="Park prior-session debris on a WIP branch; do not commit.",
        )
        _write_clusters(
            knowledge_root,
            [
                {
                    "cluster_id": "code-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code/feedback_commit_v1.md",
                        "-Users-tristankromer-Code/feedback_commit_v2.md",
                    ],
                    "centroid_score": 0.62,
                    "rationale": "opposing guidance",
                }
            ],
        )
        _write_config(knowledge_root)
        (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
        _git_init(knowledge_root)

        payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            '"-Users-tristankromer-Code/feedback_commit_v1.md", '
            '"-Users-tristankromer-Code/feedback_commit_v2.md"], '
            '"conflicting_passages": ["Commit directly.", "Park on WIP."], '
            '"rationale": "One says commit; the other says park."}'
        )
        response = MagicMock()
        response.content = [MagicMock(text=payload)]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        entries = merge_clusters_to_wiki(knowledge_root, client=fake_client)
        assert entries[0].contradictions_detected is True

        report = run_retire_pass(entries, knowledge_root)
        # The contradictory raw stays queued — a delete must never race a
        # pending confirmation.
        assert a.exists()
        assert b.exists()
        assert report.moved == []
        assert len(report.held) == 2
        assert all(d.disposition == HOLD for d in report.dispositions)
        assert report.committed is False


class TestRetireDryRun:
    def test_dry_run_writes_nothing_but_reports_plan(self, retire_root: Path) -> None:
        from athenaeum.merge import AUTO_WIKI_PREFIX, merge_clusters_to_wiki
        from athenaeum.retire import MOVE, run_retire_pass

        # dry_run merge builds entries without writing wiki; retire dry_run
        # must likewise write nothing.
        entries = merge_clusters_to_wiki(retire_root, dry_run=True)
        head_before = _git(retire_root, "rev-parse", "HEAD").stdout.strip()

        report = run_retire_pass(entries, retire_root, dry_run=True)

        raw = _raw_file(retire_root)
        assert raw.exists()  # nothing removed
        wiki = retire_root / "wiki"
        assert list(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")) == []  # nothing written
        head_after = _git(retire_root, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after  # no commit

        # ...but the plan is reported with the intended disposition.
        assert report.dry_run is True
        assert report.committed is False
        assert len(report.dispositions) == 1
        assert report.dispositions[0].disposition == MOVE


class TestRetireOriginTracing:
    def test_footnote_upgrades_to_user_stated_when_transcript_confirms(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.merge import AUTO_WIKI_PREFIX, merge_clusters_to_wiki
        from athenaeum.retire import run_retire_pass

        knowledge_root = tmp_path / "knowledge"
        scope_name = "-Users-tristankromer-Code-home"
        scope = knowledge_root / "raw" / "auto-memory" / scope_name
        _write_am(
            scope,
            "user_tristan_berlin_address.md",
            name="Tristan Berlin address",
            session="sess-berlin",
            turn=3,
            body="Tristan lives in Berlin, Germany.",
        )
        _write_clusters(
            knowledge_root,
            [
                {
                    "cluster_id": "home-0001",
                    "member_paths": [f"{scope_name}/user_tristan_berlin_address.md"],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        _write_config(knowledge_root)
        (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
        _git_init(knowledge_root)

        # Synthetic transcript tree: the user authored the claim in the
        # originating session.
        projects_root = tmp_path / "projects"
        session_dir = projects_root / scope_name
        session_dir.mkdir(parents=True)
        (session_dir / "sess-berlin.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Tristan lives in Berlin, Germany.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entries = merge_clusters_to_wiki(knowledge_root)
        run_retire_pass(entries, knowledge_root, projects_root=projects_root)

        wiki = knowledge_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        text = entry_file.read_text(encoding="utf-8")
        # The footnote is upgraded from the honest ``inferred`` default to the
        # verified ``user-stated`` with a session+turn ref.
        assert "user-stated" in text
        assert "sess-berlin#turn3" in text


class TestRetireNoGitSafety:
    def test_no_git_repo_means_no_retire(self, tmp_path: Path) -> None:
        from athenaeum.merge import merge_clusters_to_wiki
        from athenaeum.retire import SKIP, run_retire_pass

        knowledge_root = tmp_path / "knowledge"
        scope = (
            knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-home"
        )
        raw = _write_am(
            scope,
            "user_tristan_berlin_address.md",
            name="Tristan Berlin address",
            session="sess-berlin",
            turn=3,
            body="Tristan lives in Berlin, Germany.",
        )
        _write_clusters(
            knowledge_root,
            [
                {
                    "cluster_id": "home-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code-home/user_tristan_berlin_address.md"
                    ],
                    "centroid_score": 1.0,
                    "rationale": "singleton",
                }
            ],
        )
        _write_config(knowledge_root)
        (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
        # NOTE: no _git_init — recovery is git-only, so retire must refuse.

        entries = merge_clusters_to_wiki(knowledge_root)
        report = run_retire_pass(entries, knowledge_root)

        assert raw.exists()  # nothing retired without git
        assert report.moved == []
        assert report.committed is False
        assert any(d.disposition == SKIP for d in report.dispositions)


class TestRetireIntegrationViaRun:
    def test_run_merge_only_retires_by_default(self, retire_root: Path) -> None:
        from athenaeum.librarian import run
        from athenaeum.merge import AUTO_WIKI_PREFIX
        from athenaeum.models import parse_frontmatter

        rc = run(
            raw_root=retire_root / "raw",
            wiki_root=retire_root / "wiki",
            knowledge_root=retire_root,
            merge_only=True,
        )
        assert rc == 0
        # Default-on: a normal merge run retires the non-contradictory raw.
        assert not _raw_file(retire_root).exists()
        wiki = retire_root / "wiki"
        entry_file = next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md"))
        meta, _ = parse_frontmatter(entry_file.read_text(encoding="utf-8"))
        assert meta["retired"] is True
