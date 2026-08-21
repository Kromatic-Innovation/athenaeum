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
  refused file before clustering.
- The hard "no deletion anywhere" constraint (issue athenaeum#968 AC4): a
  static source scan asserting none of the three new #968 modules call a
  filesystem-deletion primitive.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum.authority import AuthorityManifest, AuthoritySource
from athenaeum.never_ingest import (
    NeverIngestRefusal,
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
        # ids-only: no content field anywhere in the row.
        assert set(rows[0]) == {"ts", "class", "detail", "origin_scope", "file_ref_hash"}

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
