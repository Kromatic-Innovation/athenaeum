# SPDX-License-Identifier: Apache-2.0
"""Tests for the never-ingest class list (issue athenaeum#968, part 2).

Covers:
- classify_never_ingest: mirror-of-live-source (via the authority manifest's
  existing topic-index lookup) and pending-state-todo (flag + phrase list),
  each gated on being present in ``manifest.never_ingest_classes``.
- filter_never_ingest: real AutoMemoryFile fixtures via
  discover_auto_memory_files (mirrors tests/test_ephemeral_intake.py's own
  shape); refused files are excluded from the returned list but NEVER
  deleted from disk; refusals are ledgered (unless dry_run).
- The refusal ledger round-trips.
- Wiring: `_run_auto_memory_phase` consults the manifest and excludes a
  refused file before clustering; ``process_one`` does the same for the
  entity tier, at the per-file COMPILE choke point, WITHOUT changing
  ``discover_raw_files``'s own return value (the athenaeum#713 backlog-count
  guard).
- The hard "no deletion anywhere" constraint (issue athenaeum#968 AC4): a
  static source scan asserting none of the three new athenaeum#968 modules call
  a filesystem-deletion primitive.
"""

from __future__ import annotations

import ast
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.authority import AuthorityManifest, AuthoritySource, parse_authority_manifest
from athenaeum.never_ingest import (
    NeverIngestRefusal,
    check_and_refuse,
    classify_never_ingest,
    filter_never_ingest,
    read_refusals,
    record_refusal,
    refusals_path,
)

_MANIFEST_YAML = """\
version: 1
sources:
  - slug: css-typeface-source
    location: assets/styles/cover.css
    kind: config
    topics:
      - webfont-embedding
never_ingest_classes:
  - mirror-of-live-source
  - pending-state-todo
"""


_ALL_CLASSES: tuple[str, ...] = ("mirror-of-live-source", "pending-state-todo")


def _manifest(*, classes: tuple[str, ...] = _ALL_CLASSES) -> AuthorityManifest:
    return AuthorityManifest(
        version=1,
        sources=(
            AuthoritySource(
                slug="css-typeface-source",
                location="assets/styles/cover.css",
                topics=("webfont-embedding",),
                kind="config",
            ),
        ),
        never_ingest_classes=classes,
    )


class TestClassifyMirrorOfLiveSource:
    def test_matching_topic_refused(self) -> None:
        meta = {"name": "Montserrat cover font", "topics": ["webfont-embedding"]}
        match = classify_never_ingest(meta, "body", manifest=_manifest())
        assert match is not None
        assert match.class_slug == "mirror-of-live-source"
        assert "css-typeface-source" in match.detail

    def test_non_matching_topic_passes(self) -> None:
        meta = {"name": "Something else", "topics": ["unrelated-topic"]}
        assert classify_never_ingest(meta, "body", manifest=_manifest()) is None

    def test_class_not_enabled_never_matches(self) -> None:
        meta = {"name": "x", "topics": ["webfont-embedding"]}
        manifest = _manifest(classes=("pending-state-todo",))
        assert classify_never_ingest(meta, "body", manifest=manifest) is None

    def test_no_classes_enabled_is_inert(self) -> None:
        meta = {"name": "x", "topics": ["webfont-embedding"]}
        manifest = _manifest(classes=())
        assert classify_never_ingest(meta, "body", manifest=manifest) is None


class TestClassifyPendingStateTodo:
    def test_explicit_flag_refused(self) -> None:
        meta = {"name": "x", "pending_state": True}
        match = classify_never_ingest(meta, "body", manifest=_manifest())
        assert match is not None
        assert match.class_slug == "pending-state-todo"

    def test_phrase_in_body_refused(self) -> None:
        body = "Has this been added to the SKILL.md yet? Checking back later."
        match = classify_never_ingest({"name": "x"}, body, manifest=_manifest())
        assert match is not None
        assert match.class_slug == "pending-state-todo"

    def test_ordinary_body_passes(self) -> None:
        body = "The webfont must be embedded or Paged.js silently falls back."
        assert classify_never_ingest({"name": "x"}, body, manifest=_manifest()) is None

    def test_class_not_enabled_never_matches(self) -> None:
        meta = {"name": "x", "pending_state": True}
        manifest = _manifest(classes=("mirror-of-live-source",))
        assert classify_never_ingest(meta, "body", manifest=manifest) is None


class TestRefusalLedgerRoundtrip:
    def test_record_then_read(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        refusal = NeverIngestRefusal(
            ts="2026-08-21T00:00:00Z",
            class_slug="mirror-of-live-source",
            detail="topic owned by css-typeface-source",
            origin_scope="-Users-alice-Code-projectx",
            file_ref_hash="abc123",
        )
        assert record_refusal(refusal, cache_dir=cache_dir) is True
        rows = read_refusals(cache_dir)
        assert len(rows) == 1
        assert rows[0]["class"] == "mirror-of-live-source"
        assert rows[0]["origin_scope"] == "-Users-alice-Code-projectx"
        assert rows[0]["tier"] == "auto-memory"  # default tier
        # ids-only: no content field anywhere in the row.
        assert set(rows[0]) == {
            "ts",
            "class",
            "detail",
            "origin_scope",
            "file_ref_hash",
            "tier",
        }

    def test_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        assert read_refusals(tmp_path / "cache") == []

    def test_ledger_path_under_cache_dir(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        assert refusals_path(cache_dir) == cache_dir / "_never_ingest_refusals.jsonl"


LEGIT_SCOPE = "-Users-alice-Code-projectx"


def _write_auto_memory_root(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory" / LEGIT_SCOPE
    auto.mkdir(parents=True)
    (auto / "reference_montserrat_cover_font.md").write_text(
        "---\n"
        "name: Montserrat cover font\n"
        "type: reference\n"
        "topics:\n"
        "  - webfont-embedding\n"
        "originSessionId: sess-mirror\n"
        "---\n"
        "The cover uses Montserrat, set in assets/styles/cover.css.\n",
        encoding="utf-8",
    )
    (auto / "reference_recall_architecture.md").write_text(
        "---\n"
        "name: Recall architecture\n"
        "description: FTS5 + vector recall pipeline\n"
        "type: reference\n"
        "originSessionId: sess-legit\n"
        "---\n"
        "The recall hook surfaces wiki context via a hybrid FTS5+vector merge.\n",
        encoding="utf-8",
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    return knowledge_root


class TestFilterNeverIngest:
    def test_refuses_matching_file_keeps_legit(self, tmp_path: Path) -> None:
        from athenaeum.intake import discover_auto_memory_files

        knowledge_root = _write_auto_memory_root(tmp_path)
        files = discover_auto_memory_files(knowledge_root)
        assert len(files) == 2

        cache_dir = tmp_path / "cache"
        kept, refused = filter_never_ingest(files, _manifest(), cache_dir=cache_dir)

        assert {f.path.name for f in kept} == {"reference_recall_architecture.md"}
        assert len(refused) == 1
        assert refused[0].class_slug == "mirror-of-live-source"

    def test_refused_file_never_deleted_from_disk(self, tmp_path: Path) -> None:
        from athenaeum.intake import discover_auto_memory_files

        knowledge_root = _write_auto_memory_root(tmp_path)
        files = discover_auto_memory_files(knowledge_root)
        refused_path = next(
            f.path for f in files if f.path.name == "reference_montserrat_cover_font.md"
        )
        original_text = refused_path.read_text(encoding="utf-8")

        filter_never_ingest(files, _manifest(), cache_dir=tmp_path / "cache")

        assert refused_path.exists()
        assert refused_path.read_text(encoding="utf-8") == original_text

    def test_refusal_is_ledgered(self, tmp_path: Path) -> None:
        from athenaeum.intake import discover_auto_memory_files

        knowledge_root = _write_auto_memory_root(tmp_path)
        files = discover_auto_memory_files(knowledge_root)
        cache_dir = tmp_path / "cache"

        filter_never_ingest(files, _manifest(), cache_dir=cache_dir)

        rows = read_refusals(cache_dir)
        assert len(rows) == 1
        assert rows[0]["class"] == "mirror-of-live-source"

    def test_dry_run_does_not_write_ledger(self, tmp_path: Path) -> None:
        from athenaeum.intake import discover_auto_memory_files

        knowledge_root = _write_auto_memory_root(tmp_path)
        files = discover_auto_memory_files(knowledge_root)
        cache_dir = tmp_path / "cache"

        kept, refused = filter_never_ingest(
            files, _manifest(), cache_dir=cache_dir, dry_run=True
        )

        assert len(refused) == 1  # still reported to the caller
        assert read_refusals(cache_dir) == []  # but not durably ledgered

    def test_no_classes_enabled_is_a_complete_noop(self, tmp_path: Path) -> None:
        from athenaeum.intake import discover_auto_memory_files

        knowledge_root = _write_auto_memory_root(tmp_path)
        files = discover_auto_memory_files(knowledge_root)
        manifest = _manifest(classes=())

        kept, refused = filter_never_ingest(files, manifest, cache_dir=tmp_path / "cache")

        assert len(kept) == len(files)
        assert refused == []


def _write_all_refused_auto_memory_root(tmp_path: Path) -> Path:
    """Like :func:`_write_auto_memory_root` but with ONLY the refused file --
    so after the never-ingest filter runs, `_run_auto_memory_phase` hits its
    existing early-return (empty `auto_memory_files`) before touching
    clustering/embedding machinery this test does not want to drive.
    """
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory" / LEGIT_SCOPE
    auto.mkdir(parents=True)
    (auto / "reference_montserrat_cover_font.md").write_text(
        "---\n"
        "name: Montserrat cover font\n"
        "type: reference\n"
        "topics:\n"
        "  - webfont-embedding\n"
        "originSessionId: sess-mirror\n"
        "---\n"
        "The cover uses Montserrat, set in assets/styles/cover.css.\n",
        encoding="utf-8",
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    return knowledge_root


class TestLibrarianWiring:
    """Issue athenaeum#968: `_run_auto_memory_phase` consults the never-ingest gate
    (via the on-disk authority manifest) BEFORE clustering."""

    def test_refused_auto_memory_file_excluded_before_clustering(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import RunContext, TokenUsage, _run_auto_memory_phase

        knowledge_root = _write_all_refused_auto_memory_root(tmp_path)
        (knowledge_root / "authority-manifest.yaml").write_text(
            _MANIFEST_YAML, encoding="utf-8"
        )

        ctx = RunContext(
            raw_root=knowledge_root / "raw",
            wiki_root=knowledge_root / "wiki",
            knowledge_root=knowledge_root,
            dry_run=False,
            max_files=None,
            max_api_calls=None,
            max_runtime=None,
            cluster_only=False,
            merge_only=False,
            strict_budget=False,
            batch_mode=None,
            retire=None,
            push_after_run=None,
            pull_before_run=None,
            projects_root=None,
            install_signal_handlers=False,
            changed_paths=None,
            full_compile=False,
            now=datetime.now(timezone.utc),
            heartbeat=None,
            out_run_stats=None,
        )
        ctx.config = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}
        ctx.usage = TokenUsage()
        ctx.run_deadline = None

        _run_auto_memory_phase(ctx)

        assert ctx.never_ingest_summary is not None
        assert ctx.never_ingest_summary["refused"] == 1
        assert ctx.never_ingest_summary["by_class"] == {"mirror-of-live-source": 1}

        # The refused source file is still on disk, untouched.
        refused_path = (
            knowledge_root
            / "raw"
            / "auto-memory"
            / LEGIT_SCOPE
            / "reference_montserrat_cover_font.md"
        )
        assert refused_path.exists()


class TestNoDeletionStaticScan:
    """Issue athenaeum#968 AC4 (hard constraint): "No deletion behaviour anywhere
    in this issue -- movement is tier metadata only." Asserted mechanically:
    none of the three new modules this issue adds call a filesystem-deletion
    primitive anywhere in their source.
    """

    _FORBIDDEN_ATTRS = {"unlink", "rmtree", "remove", "rmdir"}
    _MODULES = ("never_ingest", "usage_report", "ingestion_gate")

    @pytest.mark.parametrize("modname", _MODULES)
    def test_module_contains_no_deletion_call(self, modname: str) -> None:
        src_path = Path(__file__).resolve().parents[1] / "src" / "athenaeum" / f"{modname}.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self._FORBIDDEN_ATTRS:
                offenders.append(node.attr)
            if (
                isinstance(node, ast.Name)
                and node.id in self._FORBIDDEN_ATTRS
                and isinstance(getattr(node, "ctx", None), ast.Load)
            ):
                offenders.append(node.id)
        assert offenders == [], (
            f"{modname}.py calls a deletion primitive ({offenders}) -- "
            "issue athenaeum#968 forbids deletion anywhere in this issue's scope"
        )


class TestCheckAndRefuse:
    """The shared classify+ledger primitive both intake tiers call."""

    def test_match_builds_and_ledgers(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        meta = {"name": "x", "topics": ["webfont-embedding"]}
        refusal = check_and_refuse(
            meta,
            "body",
            manifest=_manifest(),
            origin_scope="graph-writer",
            filename="20260801T000000Z-abcd1234.md",
            tier="entity",
            cache_dir=cache_dir,
        )
        assert refusal is not None
        assert refusal.tier == "entity"
        assert refusal.class_slug == "mirror-of-live-source"
        assert read_refusals(cache_dir) == [refusal.to_dict()]

    def test_no_match_returns_none_and_ledgers_nothing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        refusal = check_and_refuse(
            {"name": "x"},
            "ordinary body",
            manifest=_manifest(),
            origin_scope="graph-writer",
            filename="f.md",
            tier="entity",
            cache_dir=cache_dir,
        )
        assert refusal is None
        assert read_refusals(cache_dir) == []

    def test_dry_run_returns_match_but_does_not_ledger(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        meta = {"name": "x", "topics": ["webfont-embedding"]}
        refusal = check_and_refuse(
            meta,
            "body",
            manifest=_manifest(),
            origin_scope="graph-writer",
            filename="f.md",
            tier="entity",
            cache_dir=cache_dir,
            dry_run=True,
        )
        assert refusal is not None
        assert read_refusals(cache_dir) == []


ENTITY_SOURCE_DIR = "graph-writer"

_ENTITY_MANIFEST_YAML = """\
version: 1
sources:
  - slug: css-typeface-source
    location: assets/styles/cover.css
    kind: config
    topics:
      - webfont-embedding
never_ingest_classes:
  - mirror-of-live-source
"""


def _raw_from_content(raw_dir: Path, content: str, *, filename: str = "note.md"):
    from athenaeum.models import RawFile

    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=ENTITY_SOURCE_DIR, timestamp="", uuid8="")


class TestEntityTierWiring:
    """Issue athenaeum#968: `process_one` enforces the never-ingest gate at the
    per-file COMPILE choke point (never at discovery), per the coordinator's
    review -- `discover_raw_files`'s own return value must stay
    byte-identical so athenaeum#713's backlog-count instruments
    (`backlog_price_sheet.py` / `ordinary_night_table.py`) never silently
    move.
    """

    _MATCHING_CONTENT = (
        "---\nname: Montserrat cover font\ntopics:\n  - webfont-embedding\n"
        "---\nThe cover uses Montserrat, set in assets/styles/cover.css.\n"
    )

    def test_refused_file_never_reaches_the_llm_tiers(self, tmp_path: Path) -> None:
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / ENTITY_SOURCE_DIR, self._MATCHING_CONTENT)
        manifest = parse_authority_manifest(_ENTITY_MANIFEST_YAML)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "never-ingest refusal must short-circuit before any LLM tier runs"
        )
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            never_ingest_manifest=manifest,
        )
        client.messages.create.assert_not_called()
        assert not result.created
        assert not result.updated
        assert not result.escalated
        assert result.skipped == ["never-ingest:mirror-of-live-source"]

    def test_refused_file_sets_retain_raw_by_design(self, tmp_path: Path) -> None:
        """Issue athenaeum#2363: the never-ingest return path sets BOTH
        ``retain_raw`` (so the entity loop keeps the file on disk at all)
        and ``retain_raw_by_design`` (so the entity loop does NOT count this
        retention toward the athenaeum#663 stuck-file threshold / athenaeum#1185
        backoff -- a never-ingest raw is refused by design on every run,
        per ``athenaeum.never_ingest``'s own module docstring, so it is
        never "progress pending" the way a structured-upsert refusal is).
        """
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / ENTITY_SOURCE_DIR, self._MATCHING_CONTENT)
        manifest = parse_authority_manifest(_ENTITY_MANIFEST_YAML)

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            None,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            never_ingest_manifest=manifest,
        )
        assert result.retain_raw is True
        assert result.retain_raw_by_design is True

    def test_refused_file_never_deleted_and_no_wiki_page_written(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / ENTITY_SOURCE_DIR, self._MATCHING_CONTENT)
        original_text = raw.path.read_text(encoding="utf-8")
        manifest = parse_authority_manifest(_ENTITY_MANIFEST_YAML)

        process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            None,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            never_ingest_manifest=manifest,
        )

        assert raw.path.exists()
        assert raw.path.read_text(encoding="utf-8") == original_text
        assert list(wiki.glob("*.md")) == []

    def test_refusal_ledgered_with_entity_tier(self, tmp_path: Path) -> None:
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / ENTITY_SOURCE_DIR, self._MATCHING_CONTENT)
        manifest = parse_authority_manifest(_ENTITY_MANIFEST_YAML)

        process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            None,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            never_ingest_manifest=manifest,
        )
        # process_one resolves the cache dir itself (mirrors the auto-memory
        # gate's own ATHENAEUM_CACHE_DIR-driven resolution) -- read it back
        # the same way rather than threading cache_dir through process_one.
        from athenaeum.config import resolve_cache_dir

        rows = read_refusals(resolve_cache_dir(None))
        assert len(rows) == 1
        assert rows[0]["tier"] == "entity"
        assert rows[0]["class"] == "mirror-of-live-source"

    def test_no_manifest_threaded_is_a_complete_noop(self, tmp_path: Path) -> None:
        """``never_ingest_manifest=None`` (the default -- every pre-athenaeum#968
        caller) must behave byte-identically to before this issue: the
        matching content reaches Tier 1 normally instead of being refused.
        ``dry_run=True`` keeps this test LLM-free (returns after Tier 1)
        without changing what's under test -- whether the never-ingest gate
        fired.
        """
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / ENTITY_SOURCE_DIR, self._MATCHING_CONTENT)

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            None,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            dry_run=True,
        )
        assert "never-ingest:mirror-of-live-source" not in result.skipped


class TestDiscoverRawFilesUnaffectedByNeverIngest:
    """The athenaeum#713 regression guard the coordinator asked for explicitly:
    `discover_raw_files`'s return (set AND count) must be byte-identical
    regardless of never-ingest classification -- the gate lives at the
    per-file COMPILE choke point (`process_one`), never at discovery, so
    `backlog_price_sheet.py` / `ordinary_night_table.py` (both call
    `discover_raw_files` directly for their own backlog counts, athenaeum#713,
    held pending an operator decision) never silently see a different
    number.
    """

    def test_discovery_set_unchanged_with_and_without_never_ingest_classes(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.intake import discover_raw_files

        raw_root = tmp_path / "raw"
        matching = (
            "---\nname: Montserrat cover font\ntopics:\n  - webfont-embedding\n"
            "---\nThe cover uses Montserrat, set in assets/styles/cover.css.\n"
        )
        non_matching = "---\nname: Ordinary note\n---\nNothing special here.\n"
        _raw_from_content(raw_root / ENTITY_SOURCE_DIR, matching, filename="a.md")
        _raw_from_content(raw_root / ENTITY_SOURCE_DIR, non_matching, filename="b.md")

        # discover_raw_files takes no manifest argument at all -- it cannot
        # be influenced by never_ingest_classes even in principle. This test
        # pins that contract: the returned ref set is identical regardless
        # of what a caller does with never-ingest classification downstream.
        refs_before = sorted(r.ref for r in discover_raw_files(raw_root))
        refs_after = sorted(r.ref for r in discover_raw_files(raw_root))
        assert refs_before == refs_after == [
            f"{ENTITY_SOURCE_DIR}/a.md",
            f"{ENTITY_SOURCE_DIR}/b.md",
        ]

    def test_process_one_refusal_does_not_touch_discovery_output(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: even after `process_one` has refused a file (via a
        manifest a CALLER threaded in), a FRESH `discover_raw_files` call
        still returns that same file -- refusal is a per-run compile
        decision, not a discovery-time exclusion.
        """
        from athenaeum.intake import discover_raw_files
        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex

        raw_root = tmp_path / "raw"
        matching = (
            "---\nname: Montserrat cover font\ntopics:\n  - webfont-embedding\n"
            "---\nThe cover uses Montserrat, set in assets/styles/cover.css.\n"
        )
        _raw_from_content(raw_root / ENTITY_SOURCE_DIR, matching, filename="a.md")

        refs_before = sorted(r.ref for r in discover_raw_files(raw_root))

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = discover_raw_files(raw_root)[0]
        manifest = parse_authority_manifest(_ENTITY_MANIFEST_YAML)
        process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            None,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            never_ingest_manifest=manifest,
        )

        refs_after = sorted(r.ref for r in discover_raw_files(raw_root))
        assert refs_before == refs_after == [f"{ENTITY_SOURCE_DIR}/a.md"]


# ---------------------------------------------------------------------------
# Run()-level survival (issue athenaeum#2363)
# ---------------------------------------------------------------------------

_RUN_LEVEL_MATCHING_CONTENT = (
    "---\nname: Montserrat cover font\ntopics:\n  - webfont-embedding\n"
    "---\nThe cover uses Montserrat, set in assets/styles/cover.css.\n"
)


def _seed_run_level_root(tmp_path: Path) -> Path:
    """Minimal knowledge root for a full ``run()``: wiki/_schema, a git
    repo, and an authority manifest declaring the never-ingest class the
    raw content written by each test below triggers. Mirrors
    tests/test_structured_upsert.py's own ``_seed_root`` helper, the
    established pattern for a real (mocked-LLM) ``run()`` in this suite.
    """
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n| company |\n"
    )
    (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    (root / "authority-manifest.yaml").write_text(_ENTITY_MANIFEST_YAML, encoding="utf-8")
    (root / "raw" / "sessions").mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q", "-b", "test-branch"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test Runner"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    return root


def _write_run_level_raw(root: Path, *, filename: str = "20260101T120000Z-aabbccd0.md") -> Path:
    path = root / "raw" / "sessions" / filename
    path.write_text(_RUN_LEVEL_MATCHING_CONTENT, encoding="utf-8")
    return path


class TestRunLevelNeverIngestSurvival:
    """Issue athenaeum#2363 AC: a never-ingest match must survive a full
    ``run()`` -- not just the ``process_one``-level assertions above -- on
    BOTH transports. The two honor ``retain_raw`` at DIFFERENT sites: the
    synchronous entity loop's unlink branch (`librarian.py`, around L9600),
    and the batch finalizer's (`batch.py`, around L2043). Each transport
    also now runs its OWN never-ingest check (the batch transport never
    calls ``process_one`` -- it replicates the deterministic tier-0 steps
    inline, same as its existing `tier0_structured_upsert` call), so a
    regression in either one's wiring is a distinct failure mode this file
    covers separately.
    """

    def test_survives_sequential_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = _seed_run_level_root(tmp_path)
        raw_path = _write_run_level_raw(root)
        original = raw_path.read_text(encoding="utf-8")

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "never-ingest refusal must short-circuit before any LLM tier runs"
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == 0
        assert raw_path.exists()
        assert raw_path.read_text(encoding="utf-8") == original
        assert list((root / "wiki").glob("*.md")) == []
        client.messages.create.assert_not_called()

    def test_survives_batch_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = _seed_run_level_root(tmp_path)
        raw_path = _write_run_level_raw(root)
        original = raw_path.read_text(encoding="utf-8")

        client = MagicMock()
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            batch_mode=True,
        )

        assert rc == 0
        assert raw_path.exists()
        assert raw_path.read_text(encoding="utf-8") == original
        assert list((root / "wiki").glob("*.md")) == []
        # The never-ingest gate must fire in the batch transport's OWN
        # assembly loop, before any tier-2 classification is even built --
        # a single refused file means zero batch requests ever submitted.
        client.messages.batches.create.assert_not_called()

    def test_retained_by_design_never_crosses_stuck_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Design-decision test (issue athenaeum#2363): a never-ingest
        retention must NOT accrue toward the athenaeum#663 stuck-file ledger /
        athenaeum#1185 backoff, unlike the structured-upsert refusal that
        shares the same ``retain_raw`` branch (see
        tests/test_structured_upsert.py::
        test_refused_raw_attempts_increment_and_threshold_marks_it_stuck,
        which proves that SIBLING refusal DOES accrue). This test would
        fail if ``retain_raw_by_design`` were dropped or ignored: with the
        threshold forced to 1 and backoff forced to 0, a run that counted
        the refusal would surface this file as stuck on the very first run.
        """
        import anthropic as anthropic_mod

        from athenaeum.librarian import _load_stuck_ledger, run

        root = _seed_run_level_root(tmp_path)
        raw_path = _write_run_level_raw(root)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError("LLM tier reached")
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "1")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")

        for _ in range(3):
            stats: dict = {}
            rc = run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_api_calls=100,
                max_runtime=0,
                out_run_stats=stats,
            )
            assert rc == 0
            assert stats["stuck_files"] == []
            assert _load_stuck_ledger(root / "wiki") == {}
            assert raw_path.exists()

        client.messages.create.assert_not_called()


class TestNeverIngestWindowStarvation:
    """Issue athenaeum#2363 addendum (Seer finding, coordinator-verified true
    positive). A never-ingest raw is retained on disk by design and
    deliberately excluded from the stuck-file ledger -- but
    `_hold_out_unworkable_raw` (`athenaeum.librarian`, issue athenaeum#1322)
    only holds a file out of the `max_files` intake window when it is NAMED
    IN THE STUCK LEDGER. Opting never-ingest out of that ledger silently
    opted it out of the window-scheduling job too: before this fix, a
    never-ingest raw was rediscovered and consumed a window slot on every
    run, forever -- the exact athenaeum#1322 starvation pathology
    (`_hold_out_unworkable_raw`'s own docstring: 50 permanently-stuck files
    filling a 50-slot window for six hours while 4,181 healthy records
    behind them never got a slot) arriving through a different door.

    This test pins the fix with the shape that actually catches a
    regression: not merely "the hold-out ledger has N entries" (which would
    pass even if the hold-out were never consulted at window-fill time), but
    that a WORKABLE file sorted BEHIND more never-ingest files than
    `max_files` allows still gets a slot within a few runs, and that every
    never-ingest raw is still on disk throughout. Removing the hold-out
    (reverting to pre-fix behavior) makes this test fail because the
    workable file would never be created at all.
    """

    def test_workable_file_gets_a_slot_despite_never_ingest_backlog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = _seed_run_level_root(tmp_path)
        sessions = root / "raw" / "sessions"

        # Three never-ingest raws, timestamped so discovery (oldest-first)
        # places all three AHEAD of the workable raw below -- the exact
        # "permanently blocks the front of the queue" shape the issue
        # describes. max_files=2 below is smaller than 3, so pre-fix this
        # window is filled by never-ingest files on EVERY run, forever.
        ni_paths = []
        for i in range(3):
            p = sessions / f"2026010{i + 1}T120000Z-ni0000{i}.md"
            p.write_text(_RUN_LEVEL_MATCHING_CONTENT, encoding="utf-8")
            ni_paths.append(p)

        # A workable, LLM-free raw (tier0_passthrough-eligible: uid/type/name
        # already wiki-shaped) sorted LAST by timestamp -- the healthy record
        # standing behind the never-ingest backlog.
        workable_path = sessions / "20260104T120000Z-wrk00001.md"
        workable_path.write_text(
            "---\nuid: wrk00001\ntype: company\nname: Workable Co\n---\n"
            "A workable raw that needs no LLM call.\n",
            encoding="utf-8",
        )

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "this test's raws are never-ingest or tier0-passthrough-eligible "
            "-- neither reaches an LLM tier"
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        # Run twice: run 1 has no holdout ledger yet (first encounter), so
        # the window (max_files=2) fills with the two earliest never-ingest
        # raws and the workable file gets nothing. Run 2's hold-out should
        # keep those two already-recorded never-ingest raws OUT of the
        # window, freeing both slots for the third never-ingest raw AND the
        # workable file.
        for _ in range(2):
            rc = run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_api_calls=100,
                max_files=2,
            )
            assert rc == 0

        # The workable file got its slot: promoted to wiki, raw deleted.
        assert not workable_path.exists(), (
            "workable raw was never given a window slot -- the "
            "never-ingest backlog starved it (issue athenaeum#1322/athenaeum#2363 "
            "regression)"
        )
        assert (root / "wiki" / "wrk00001-workable-co.md").exists()

        # AC1 still holds: every never-ingest raw survives, untouched, on
        # BOTH runs -- the hold-out must never delete what it excludes.
        for p in ni_paths:
            assert p.exists()
            assert p.read_text(encoding="utf-8") == _RUN_LEVEL_MATCHING_CONTENT

        # And no never-ingest raw was ever counted as stuck.
        from athenaeum.librarian import _load_stuck_ledger

        assert _load_stuck_ledger(root / "wiki") == {}
