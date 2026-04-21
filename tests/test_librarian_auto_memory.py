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
        self, auto_memory_root: Path,
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
        self, auto_memory_root: Path,
    ) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(auto_memory_root)
        unscoped = {
            f.path.name: f for f in files if f.origin_scope == "_unscoped"
        }
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
        self, auto_memory_root: Path,
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
        assert not any(
            f.path.name.startswith("20260422T120000Z") for f in files
        )

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
        target = next(
            f for f in files if f.path.name == "project_voltaire_part0.md"
        )
        assert "Body for part 0." in target.content
