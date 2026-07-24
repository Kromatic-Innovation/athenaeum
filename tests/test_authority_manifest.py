# SPDX-License-Identifier: Apache-2.0
"""Tests for the authority manifest + pointer-stub converter (issue #426).

Covers each acceptance criterion named in the issue:

- Manifest schema validates; a malformed manifest raises a clear error.
- Detector: a fixture memory duplicating a manifest-listed source is
  flagged; a non-duplicate passes.
- Converter output is a one-line pointer stub; stubs are excluded from
  merge proposals AND embed only the pointer line.
- CLI lint lists duplicates read-only (does not mutate).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum.authority import (
    AuthorityManifest,
    AuthorityManifestError,
    AuthoritySource,
    convert_page_to_pointer_stub,
    convert_to_pointer_stub,
    find_duplicate_source,
    find_duplicates_in_wiki,
    is_pointer_stub,
    load_authority_manifest,
    parse_authority_manifest,
    pointer_stub_line,
)
from athenaeum.config import resolve_authority_manifest_path
from athenaeum.models import parse_frontmatter

_VALID_MANIFEST_YAML = """\
version: 1
sources:
  - slug: skill-dijkstra
    location: .claude/skills/dijkstra/SKILL.md
    kind: skill
    topics:
      - lean-development-workflow
      - clean-commit-discipline
  - slug: config-athenaeum
    location: src/athenaeum/config.py
    kind: code
    topics:
      - config-resolution-precedence
"""


def _write_page(
    wiki_root: Path,
    filename: str,
    *,
    page_type: str = "concept",
    name: str | None = None,
    topics: list[str] | None = None,
    tags: list[str] | None = None,
    pointer_stub: bool = False,
    body: str = "Some body content.",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or filename[:-3]}", f"type: {page_type}"]
    if topics:
        lines.append("topics:")
        lines.extend(f"  - {t}" for t in topics)
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {t}" for t in tags)
    if pointer_stub:
        lines.append("pointer_stub: true")
    lines.append("---")
    lines.append(body)
    path = wiki_root / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1: manifest schema validates; malformed -> clear error.
# ---------------------------------------------------------------------------


class TestManifestSchema:
    def test_valid_manifest_parses(self) -> None:
        manifest = parse_authority_manifest(_VALID_MANIFEST_YAML)
        assert manifest.version == 1
        assert len(manifest.sources) == 2
        slugs = {s.slug for s in manifest.sources}
        assert slugs == {"skill-dijkstra", "config-athenaeum"}
        dijkstra = next(s for s in manifest.sources if s.slug == "skill-dijkstra")
        assert dijkstra.location == ".claude/skills/dijkstra/SKILL.md"
        assert dijkstra.kind == "skill"
        assert dijkstra.topics == (
            "lean-development-workflow",
            "clean-commit-discipline",
        )

    def test_invalid_yaml_raises_clear_error(self) -> None:
        with pytest.raises(AuthorityManifestError, match="invalid YAML"):
            parse_authority_manifest("not: valid: yaml: [")

    def test_empty_document_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="empty document"):
            parse_authority_manifest("")

    def test_non_mapping_top_level_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="must be a mapping"):
            parse_authority_manifest("- just\n- a\n- list\n")

    def test_missing_version_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="unsupported version"):
            parse_authority_manifest("sources: []\n")

    def test_wrong_version_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="unsupported version"):
            parse_authority_manifest("version: 2\nsources: []\n")

    def test_missing_sources_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="'sources'"):
            parse_authority_manifest("version: 1\n")

    def test_empty_sources_raises(self) -> None:
        with pytest.raises(AuthorityManifestError, match="'sources'"):
            parse_authority_manifest("version: 1\nsources: []\n")

    def test_source_missing_slug_raises(self) -> None:
        text = "version: 1\nsources:\n  - location: x\n    topics: [a]\n"
        with pytest.raises(AuthorityManifestError, match="slug"):
            parse_authority_manifest(text)

    def test_source_missing_location_raises(self) -> None:
        text = "version: 1\nsources:\n  - slug: a\n    topics: [a]\n"
        with pytest.raises(AuthorityManifestError, match="location"):
            parse_authority_manifest(text)

    def test_source_empty_topics_raises(self) -> None:
        text = "version: 1\nsources:\n  - slug: a\n    location: x\n    topics: []\n"
        with pytest.raises(AuthorityManifestError, match="topics"):
            parse_authority_manifest(text)

    def test_source_non_string_topic_raises(self) -> None:
        text = (
            "version: 1\nsources:\n  - slug: a\n    location: x\n"
            "    topics: [1]\n"
        )
        with pytest.raises(AuthorityManifestError, match="topics"):
            parse_authority_manifest(text)

    def test_duplicate_slug_raises(self) -> None:
        text = (
            "version: 1\n"
            "sources:\n"
            "  - slug: a\n    location: x\n    topics: [t1]\n"
            "  - slug: a\n    location: y\n    topics: [t2]\n"
        )
        with pytest.raises(AuthorityManifestError, match="duplicate source slug"):
            parse_authority_manifest(text)

    def test_source_entry_not_mapping_raises(self) -> None:
        text = "version: 1\nsources:\n  - just-a-string\n"
        with pytest.raises(AuthorityManifestError, match="must be a mapping"):
            parse_authority_manifest(text)


class TestManifestLoader:
    def test_load_missing_file_returns_empty_manifest(self, tmp_path: Path) -> None:
        manifest = load_authority_manifest(tmp_path / "nope.yaml")
        assert manifest.sources == ()
        assert manifest.version == 1

    def test_load_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "authority-manifest.yaml"
        path.write_text(_VALID_MANIFEST_YAML, encoding="utf-8")
        manifest = load_authority_manifest(path)
        assert len(manifest.sources) == 2

    def test_load_malformed_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "authority-manifest.yaml"
        path.write_text("version: 1\nsources: not-a-list\n", encoding="utf-8")
        with pytest.raises(AuthorityManifestError):
            load_authority_manifest(path)


class TestResolveAuthorityManifestPath:
    def test_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_AUTHORITY_MANIFEST", raising=False)
        result = resolve_authority_manifest_path(tmp_path, None)
        assert result == tmp_path / "authority-manifest.yaml"

    def test_yaml_override_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_AUTHORITY_MANIFEST", raising=False)
        config = {"librarian": {"authority_manifest_path": "custom/manifest.yaml"}}
        result = resolve_authority_manifest_path(tmp_path, config)
        assert result == tmp_path / "custom/manifest.yaml"

    def test_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_AUTHORITY_MANIFEST", "/tmp/env-manifest.yaml")
        config = {"librarian": {"authority_manifest_path": "custom/manifest.yaml"}}
        result = resolve_authority_manifest_path(tmp_path, config)
        assert result == Path("/tmp/env-manifest.yaml")


# ---------------------------------------------------------------------------
# AC2: detector — a fixture memory duplicating a manifest source is flagged;
# a non-duplicate passes. Deterministic lookup, not semantic similarity.
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest() -> AuthorityManifest:
    return parse_authority_manifest(_VALID_MANIFEST_YAML)


class TestFindDuplicateSource:
    def test_duplicate_via_topics_list_is_flagged(
        self, manifest: AuthorityManifest
    ) -> None:
        meta = {"topics": ["lean-development-workflow"]}
        source = find_duplicate_source(meta, manifest)
        assert source is not None
        assert source.slug == "skill-dijkstra"

    def test_duplicate_is_case_and_whitespace_insensitive(
        self, manifest: AuthorityManifest
    ) -> None:
        meta = {"topics": ["  Lean-Development-Workflow  "]}
        source = find_duplicate_source(meta, manifest)
        assert source is not None
        assert source.slug == "skill-dijkstra"

    def test_duplicate_via_single_topic_scalar(
        self, manifest: AuthorityManifest
    ) -> None:
        meta = {"topic": "config-resolution-precedence"}
        source = find_duplicate_source(meta, manifest)
        assert source is not None
        assert source.slug == "config-athenaeum"

    def test_duplicate_via_tags(self, manifest: AuthorityManifest) -> None:
        meta = {"tags": ["clean-commit-discipline"]}
        source = find_duplicate_source(meta, manifest)
        assert source is not None
        assert source.slug == "skill-dijkstra"

    def test_non_duplicate_passes(self, manifest: AuthorityManifest) -> None:
        meta = {"topics": ["something-entirely-unrelated"]}
        assert find_duplicate_source(meta, manifest) is None

    def test_empty_meta_passes(self, manifest: AuthorityManifest) -> None:
        assert find_duplicate_source({}, manifest) is None
        assert find_duplicate_source(None, manifest) is None

    def test_empty_manifest_never_flags(self) -> None:
        empty = AuthorityManifest(version=1, sources=())
        meta = {"topics": ["lean-development-workflow"]}
        assert find_duplicate_source(meta, empty) is None

    def test_similar_but_not_exact_topic_does_not_match(
        self, manifest: AuthorityManifest
    ) -> None:
        # Deterministic lookup, NOT semantic similarity: a close-but-not-exact
        # string must NOT match.
        meta = {"topics": ["lean development workflows"]}
        assert find_duplicate_source(meta, manifest) is None


class TestFindDuplicatesInWiki:
    def test_scans_wiki_and_flags_duplicates_only(
        self, tmp_path: Path, manifest: AuthorityManifest
    ) -> None:
        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root,
            "dup.md",
            topics=["lean-development-workflow"],
            body="Duplicated content.",
        )
        _write_page(wiki_root, "unique.md", topics=["something-else"], body="Fine.")

        matches = find_duplicates_in_wiki(wiki_root, manifest)
        names = {m.page_path.name for m in matches}
        assert names == {"dup.md"}
        assert matches[0].source.slug == "skill-dijkstra"

    def test_already_converted_stub_is_skipped(
        self, tmp_path: Path, manifest: AuthorityManifest
    ) -> None:
        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root,
            "stub.md",
            topics=["lean-development-workflow"],
            pointer_stub=True,
            body="pointer line",
        )
        matches = find_duplicates_in_wiki(wiki_root, manifest)
        assert matches == []

    def test_missing_wiki_root_returns_empty(
        self, tmp_path: Path, manifest: AuthorityManifest
    ) -> None:
        assert find_duplicates_in_wiki(tmp_path / "nope", manifest) == []


# ---------------------------------------------------------------------------
# AC3: converter output is a one-line pointer stub; excluded from merge
# proposals AND embeds only the pointer line.
# ---------------------------------------------------------------------------


_SOURCE = AuthoritySource(
    slug="skill-dijkstra",
    location=".claude/skills/dijkstra/SKILL.md",
    topics=("lean-development-workflow",),
    kind="skill",
)


class TestPointerStubConverter:
    def test_pointer_stub_line_is_one_line(self) -> None:
        line = pointer_stub_line("Lean dev workflow notes", _SOURCE)
        assert "\n" not in line
        assert "Lean dev workflow notes" in line
        assert _SOURCE.location in line
        assert _SOURCE.slug in line

    def test_convert_produces_one_line_body_and_flag(self) -> None:
        original = (
            "---\n"
            "name: Lean dev workflow notes\n"
            "type: concept\n"
            "topics:\n  - lean-development-workflow\n"
            "---\n"
            "A whole paragraph of duplicated content that should be replaced.\n"
            "More duplicated content on a second line.\n"
        )
        converted = convert_to_pointer_stub(original, _SOURCE)
        meta, body = parse_frontmatter(converted)
        assert is_pointer_stub(meta) is True
        body_lines = [ln for ln in body.strip().splitlines() if ln.strip()]
        assert len(body_lines) == 1
        assert "Lean dev workflow notes" in body_lines[0]
        assert _SOURCE.location in body_lines[0]
        # Original duplicated content must be gone.
        assert "duplicated content" not in body

    def test_convert_preserves_other_frontmatter(self) -> None:
        original = (
            "---\nname: X\ntype: concept\nuid: abc123\n---\nbody text here\n"
        )
        converted = convert_to_pointer_stub(original, _SOURCE)
        meta, _ = parse_frontmatter(converted)
        assert meta["uid"] == "abc123"
        assert meta["type"] == "concept"

    def test_convert_title_override(self) -> None:
        original = "---\nname: Original Name\ntype: concept\n---\nbody\n"
        converted = convert_to_pointer_stub(original, _SOURCE, title="Custom Title")
        _, body = parse_frontmatter(converted)
        assert "Custom Title" in body
        assert "Original Name" not in body

    def test_convert_is_idempotent(self) -> None:
        original = "---\nname: X\ntype: concept\n---\nbody text\n"
        once = convert_to_pointer_stub(original, _SOURCE)
        twice = convert_to_pointer_stub(once, _SOURCE)
        meta1, body1 = parse_frontmatter(once)
        meta2, body2 = parse_frontmatter(twice)
        assert is_pointer_stub(meta1) and is_pointer_stub(meta2)
        assert body1.strip() == body2.strip()

    def test_convert_page_to_pointer_stub_reads_file(self, tmp_path: Path) -> None:
        page = tmp_path / "page.md"
        page.write_text(
            "---\nname: Page Title\ntype: concept\n---\nold content\n",
            encoding="utf-8",
        )
        converted = convert_page_to_pointer_stub(page, _SOURCE)
        meta, body = parse_frontmatter(converted)
        assert is_pointer_stub(meta)
        assert "old content" not in body
        # Convert does not itself write — the source file is untouched.
        assert "old content" in page.read_text(encoding="utf-8")


class TestIsPointerStub:
    def test_true_variants(self) -> None:
        assert is_pointer_stub({"pointer_stub": True})
        assert is_pointer_stub({"pointer_stub": "true"})
        assert is_pointer_stub({"pointer_stub": "Yes"})

    def test_false_variants(self) -> None:
        assert not is_pointer_stub({})
        assert not is_pointer_stub(None)
        assert not is_pointer_stub({"pointer_stub": False})
        assert not is_pointer_stub({"pointer_stub": "no"})


class TestStubExcludedFromMergeProposals:
    def test_pointer_stub_excluded_from_dedupe_candidates(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "live.md", body="live content")
        _write_page(
            wiki_root,
            "stub.md",
            pointer_stub=True,
            body="Stub title — see some/path (authoritative: some-slug)",
        )

        candidates = discover_wiki_dedupe_candidates(wiki_root)
        names = {c.path.name for c in candidates}
        assert names == {"live.md"}


class TestStubEmbedsOnlyPointerLine:
    def test_add_records_embeds_only_pointer_line_for_stub(
        self, tmp_path: Path
    ) -> None:
        """Regression for issue #426 stub hygiene.

        Exercises ``VectorBackend._add_records`` directly against a fake
        chromadb collection (no real chromadb dependency needed for this
        assertion) so the test stays fast and deterministic — mirrors the
        existing ``monkeypatch.setattr(VectorBackend, "_add_records", ...)``
        convention in ``tests/test_search.py``.
        """
        from athenaeum.search import VectorBackend

        stub_text = (
            "---\n"
            "name: Lean dev workflow notes\n"
            "type: concept\n"
            "pointer_stub: true\n"
            "---\n"
            "Lean dev workflow notes — see .claude/skills/dijkstra/SKILL.md "
            "(authoritative: skill-dijkstra)\n"
        )
        meta, _ = parse_frontmatter(stub_text)
        record = ("stub.md", tmp_path / "stub.md", "hash123", stub_text, meta, (0, 0, ""))

        class _FakeCollection:
            def __init__(self) -> None:
                self.documents: list[str] = []

            def add(self, *, ids, documents, metadatas) -> None:
                self.documents.extend(documents)

        backend = VectorBackend()
        fake_collection = _FakeCollection()
        backend._add_records(fake_collection, [record])

        assert len(fake_collection.documents) == 1
        doc = fake_collection.documents[0]
        assert "Lean dev workflow notes" in doc
        assert ".claude/skills/dijkstra/SKILL.md" in doc
        # The frontmatter block itself must NOT be part of the embedded doc.
        assert "pointer_stub" not in doc
        assert "type: concept" not in doc

    def test_add_records_embeds_full_text_for_non_stub(self, tmp_path: Path) -> None:
        from athenaeum.search import VectorBackend

        text = (
            "---\nname: Regular Page\ntype: concept\n---\n"
            "This is the full regular body content.\n"
        )
        meta, _ = parse_frontmatter(text)
        record = ("regular.md", tmp_path / "regular.md", "hash456", text, meta, (0, 0, ""))

        class _FakeCollection:
            def __init__(self) -> None:
                self.documents: list[str] = []

            def add(self, *, ids, documents, metadatas) -> None:
                self.documents.extend(documents)

        backend = VectorBackend()
        fake_collection = _FakeCollection()
        backend._add_records(fake_collection, [record])

        assert len(fake_collection.documents) == 1
        assert fake_collection.documents[0] == text[: backend._DOC_LIMIT]


# ---------------------------------------------------------------------------
# AC4: CLI lint lists duplicates read-only (does not mutate).
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "athenaeum.cli", *args],
        capture_output=True,
        text=True,
    )


class TestAuthorityLintCLI:
    def test_lint_lists_duplicate_read_only(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        (knowledge_root).mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _VALID_MANIFEST_YAML, encoding="utf-8"
        )
        page = _write_page(
            wiki_root,
            "dup.md",
            topics=["lean-development-workflow"],
            body="Duplicated content that should not be touched.",
        )
        before = page.read_text(encoding="utf-8")
        before_mtime = page.stat().st_mtime_ns

        result = _run_cli("authority", "lint", "--path", str(knowledge_root))

        assert result.returncode == 0
        assert "dup.md" in result.stdout
        assert "skill-dijkstra" in result.stdout
        # READ-ONLY: the page on disk is byte-for-byte unchanged.
        after = page.read_text(encoding="utf-8")
        assert after == before
        assert page.stat().st_mtime_ns == before_mtime

    def test_lint_json_output_is_a_list(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _VALID_MANIFEST_YAML, encoding="utf-8"
        )
        _write_page(wiki_root, "dup.md", topics=["lean-development-workflow"])

        result = _run_cli(
            "authority", "lint", "--path", str(knowledge_root), "--json"
        )
        assert result.returncode == 0
        import json

        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert len(payload) == 1
        assert payload[0]["source_slug"] == "skill-dijkstra"

    def test_lint_no_duplicates(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _VALID_MANIFEST_YAML, encoding="utf-8"
        )
        _write_page(wiki_root, "unique.md", topics=["not-owned-by-anything"])

        result = _run_cli("authority", "lint", "--path", str(knowledge_root))
        assert result.returncode == 0
        assert "0 duplicates" in result.stdout

    def test_lint_malformed_manifest_is_clear_error_not_traceback(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            "version: 99\nsources: []\n", encoding="utf-8"
        )
        result = _run_cli("authority", "lint", "--path", str(knowledge_root))
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "unsupported version" in result.stderr


class TestAuthorityConvertCLI:
    def test_convert_dry_run_does_not_mutate(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _VALID_MANIFEST_YAML, encoding="utf-8"
        )
        page = _write_page(
            wiki_root, "dup.md", topics=["lean-development-workflow"], body="dup body"
        )
        before = page.read_text(encoding="utf-8")

        result = _run_cli(
            "authority",
            "convert",
            "--path",
            str(knowledge_root),
            "--page",
            str(page),
            "--source-slug",
            "skill-dijkstra",
        )
        assert result.returncode == 0
        assert page.read_text(encoding="utf-8") == before
        assert "pointer_stub" in result.stdout

    def test_convert_apply_writes_stub(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _VALID_MANIFEST_YAML, encoding="utf-8"
        )
        page = _write_page(
            wiki_root, "dup.md", topics=["lean-development-workflow"], body="dup body"
        )

        result = _run_cli(
            "authority",
            "convert",
            "--path",
            str(knowledge_root),
            "--page",
            str(page),
            "--source-slug",
            "skill-dijkstra",
            "--apply",
        )
        assert result.returncode == 0
        meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert is_pointer_stub(meta)
        assert "dup body" not in body
