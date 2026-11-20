# SPDX-License-Identifier: Apache-2.0
"""The compile write path takes the store from the intake decision (athenaeum#2999).

Slice S3a of athenaeum#2625 (``docs/design/federation-intake-routing.md`` 4.1,
4.4): ``storage.mapping`` is the default store fed to ``decide``; the write
root comes from the decision's store, in both pipeline transports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.intake_decision import (
    PERSONAL,
    REASON_EXPLICIT,
    REASON_MAPPING,
    IntakeDecision,
)
from athenaeum.intake_destination import (
    decide_destination,
    destination_for_entity,
)
from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import EntityIndex, ProcessingResult, RawFile, WikiEntity
from athenaeum.storage import (
    ReadOnlySurfaceError,
    StorageConfigError,
    page_write_root_for_class,
)

CFG: dict[str, Any] = {
    "storage": {
        "adapters": {
            "recipes": {
                "backing_store": "markdown",
                "surface_root": "recipes",
                "corpus_policy": {"embedded": True, "recallable": True},
            },
            "notes": {
                "backing_store": "markdown",
                "surface_root": "notes",
                "corpus_policy": {"embedded": True, "recallable": True},
            },
            "archive": {
                "backing_store": "markdown",
                "surface_root": "archive",
                "read_only": True,
                "corpus_policy": {"embedded": True, "recallable": True},
            },
        },
        "mapping": {"concept": "recipes", "recipe": "recipes", "project": "notes"},
    },
}


def _decision(store: str | None, reason: str = REASON_EXPLICIT) -> IntakeDecision:
    return IntakeDecision("keep", store, "concept", reason)


class TestMappingIsTheDefault:
    def test_decision_store_is_the_mapping_entry(self) -> None:
        d = decide_destination("concept", (), CFG)
        assert (d.store, d.reason) == ("recipes", REASON_MAPPING)

    def test_unmapped_type_is_personal(self) -> None:
        assert decide_destination("company", (), CFG).store == PERSONAL
        assert decide_destination(None, (), CFG).store == PERSONAL
        assert decide_destination("company", (), None).store == PERSONAL

    def test_parity_with_type_derived_root_over_several_types(self, tmp_path: Path) -> None:
        for etype in ("concept", "recipe", "project", "company", "person", None):
            for cfg in (CFG, None):
                got = destination_for_entity(etype, cfg, tmp_path).root
                assert got == page_write_root_for_class(etype, cfg, tmp_path), (etype, cfg)

    def test_mapping_to_read_only_surface_still_refuses_as_before(self, tmp_path: Path) -> None:
        cfg = {"storage": {**CFG["storage"], "mapping": {"x": "archive"}}}
        with pytest.raises(ReadOnlySurfaceError):
            destination_for_entity("x", cfg, tmp_path)

    def test_mapping_to_unknown_adapter_still_raises(self, tmp_path: Path) -> None:
        cfg = {"storage": {**CFG["storage"], "mapping": {"x": "ghost"}}}
        with pytest.raises(StorageConfigError):
            destination_for_entity("x", cfg, tmp_path)


class TestDecisionDrivesTheRoot:
    def test_decision_naming_another_store_wins_over_the_type_mapping(
        self, tmp_path: Path
    ) -> None:
        # "concept" maps to recipes; the decision says notes.
        dest = destination_for_entity("concept", CFG, tmp_path, _decision("notes"))
        assert dest.root == tmp_path / "notes"
        assert dest.fallback is None
        assert dest.root != page_write_root_for_class("concept", CFG, tmp_path)

    def test_decision_naming_personal_writes_the_wiki(self, tmp_path: Path) -> None:
        dest = destination_for_entity("concept", CFG, tmp_path, _decision(PERSONAL))
        assert dest.root == tmp_path / "wiki"

    def test_unheld_store_fails_closed_to_personal_and_reports(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            dest = destination_for_entity("concept", CFG, tmp_path, _decision("ghost"))
        assert dest.root == tmp_path / "wiki"
        assert dest.store == PERSONAL
        assert dest.fallback and "ghost" in dest.fallback
        assert "fail-closed" in caplog.text

    def test_read_only_store_fails_closed_never_writes_there(self, tmp_path: Path) -> None:
        dest = destination_for_entity("concept", CFG, tmp_path, _decision("archive"))
        assert dest.root == tmp_path / "wiki"
        assert dest.fallback and "archive" in dest.fallback


def _raw(tmp_path: Path) -> RawFile:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "20260201T000000Z-aabbccdd.md"
    path.write_text("source\n", encoding="utf-8")
    return RawFile(
        path=path,
        source="sessions",
        timestamp="20260201T000000Z",
        uuid8="aabbccdd",
        _content="source\n",
    )


def _entity(etype: str, uid: str) -> WikiEntity:
    return WikiEntity(
        uid=uid,
        type=etype,
        name=f"Item {uid}",
        access="open",
        created="2026-01-01",
        updated="2026-01-01",
        body=f"# Item {uid}\n\nBody.\n",
        source="script:test",
    )


def _sync_paths(tmp_path: Path, cfg: dict[str, Any] | None) -> set[str]:
    root = tmp_path / "sync"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    _apply_tier3_results(
        ProcessingResult(raw_file=_raw(root)),
        new_entities=[
            _entity("concept", "c-1"),
            _entity("project", "n-1"),
            _entity("company", "k-1"),
        ],
        pending_updates=[],
        updated_uids=[],
        escalations=[],
        wiki_root=wiki,
        index=EntityIndex(wiki),
        config=cfg,
        raw=_raw(root),
    )
    return {p.relative_to(root).as_posix() for p in root.rglob("*.md") if "raw" not in p.parts}


class TestBothTransports:
    """The synchronous and batch transports resolve roots through one helper."""

    def test_both_transports_use_the_decision_helper(self) -> None:
        import inspect

        from athenaeum import batch, librarian

        for mod in (batch, librarian):
            src = inspect.getsource(mod)
            assert "destination_for_entity(" in src, mod.__name__
            assert "page_write_root_for_class(" not in src, mod.__name__

    def test_sync_transport_pages_land_where_the_mapping_says(self, tmp_path: Path) -> None:
        paths = _sync_paths(tmp_path, CFG)
        assert any(p.startswith("recipes/") for p in paths)
        assert any(p.startswith("notes/") for p in paths)
        assert any(p.startswith("wiki/") for p in paths)

    def test_batch_transport_matches_sync_transport_destinations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_per_type_write_routing import TestBatchApply

        yaml_cfg = TestBatchApply._SURFACE_YAML
        batch_root = TestBatchApply()._run(tmp_path, monkeypatch, "parity", yaml_cfg)
        # Batch put the concept page on the mapped surface; the same type through
        # the shared helper (what the sync transport calls) names the same root.
        assert list((batch_root / "notes").glob("*.md"))
        cfg = {"storage": {"adapters": {"notes": {"backing_store": "markdown",
               "surface_root": "notes", "corpus_policy": {"embedded": True}}},
               "mapping": {"concept": "notes"}}}
        assert destination_for_entity("concept", cfg, batch_root).root == batch_root / "notes"

