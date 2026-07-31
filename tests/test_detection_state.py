# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-cluster detection-incomplete marker store (issue #569)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import detection_state


class TestResolveCacheDir:
    def test_honors_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path))
        assert detection_state.resolve_cache_dir() == tmp_path

    def test_defaults_to_home_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_CACHE_DIR", raising=False)
        assert detection_state.resolve_cache_dir() == Path.home() / ".cache" / "athenaeum"


class TestMarkAndClear:
    def test_mark_then_load(self, tmp_path: Path) -> None:
        detection_state.mark_incomplete(tmp_path, "cid-1", ["/a/x.md", "/a/y.md"])
        data = detection_state.load(tmp_path)
        assert data == {"cid-1": ["/a/x.md", "/a/y.md"]}

    def test_mark_is_idempotent_and_sorted_deduped(self, tmp_path: Path) -> None:
        detection_state.mark_incomplete(tmp_path, "cid-1", ["/a/y.md", "/a/x.md", "/a/x.md"])
        assert detection_state.load(tmp_path)["cid-1"] == ["/a/x.md", "/a/y.md"]

    def test_clear_removes_only_that_cluster(self, tmp_path: Path) -> None:
        detection_state.mark_incomplete(tmp_path, "cid-1", ["/a/x.md"])
        detection_state.mark_incomplete(tmp_path, "cid-2", ["/a/z.md"])
        detection_state.clear_incomplete(tmp_path, "cid-1")
        assert detection_state.load(tmp_path) == {"cid-2": ["/a/z.md"]}

    def test_clear_absent_is_noop(self, tmp_path: Path) -> None:
        detection_state.clear_incomplete(tmp_path, "never-marked")
        assert detection_state.load(tmp_path) == {}

    def test_empty_cluster_id_ignored(self, tmp_path: Path) -> None:
        detection_state.mark_incomplete(tmp_path, "", ["/a/x.md"])
        assert detection_state.load(tmp_path) == {}


class TestLoadTolerance:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert detection_state.load(tmp_path) == {}

    def test_corrupt_file_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "detection_incomplete.json").write_text("{not json", encoding="utf-8")
        assert detection_state.load(tmp_path) == {}

    def test_non_dict_json_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "detection_incomplete.json").write_text("[1,2,3]", encoding="utf-8")
        assert detection_state.load(tmp_path) == {}

    def test_coerces_malformed_entries(self, tmp_path: Path) -> None:
        (tmp_path / "detection_incomplete.json").write_text(
            json.dumps({"cid-1": ["/a/x.md"], "cid-2": "not-a-list"}),
            encoding="utf-8",
        )
        # The non-list value is dropped; the well-formed str→list entry survives.
        assert detection_state.load(tmp_path) == {"cid-1": ["/a/x.md"]}


class TestIncompleteMemberPaths:
    def test_returns_existing_paths_only(self, tmp_path: Path) -> None:
        real = tmp_path / "real.md"
        real.write_text("x", encoding="utf-8")
        gone = tmp_path / "gone.md"  # never created
        detection_state.mark_incomplete(tmp_path, "cid-1", [str(real), str(gone)])
        paths = detection_state.incomplete_member_paths(tmp_path)
        assert paths == {real.resolve()}

    def test_unions_across_clusters(self, tmp_path: Path) -> None:
        a = tmp_path / "a.md"
        a.write_text("a", encoding="utf-8")
        b = tmp_path / "b.md"
        b.write_text("b", encoding="utf-8")
        detection_state.mark_incomplete(tmp_path, "c1", [str(a)])
        detection_state.mark_incomplete(tmp_path, "c2", [str(b)])
        assert detection_state.incomplete_member_paths(tmp_path) == {a.resolve(), b.resolve()}

    def test_empty_when_no_markers(self, tmp_path: Path) -> None:
        assert detection_state.incomplete_member_paths(tmp_path) == set()


class TestLibrarianDeltaInjection:
    """Issue #569 (H6): _run_cluster_pass folds marked clusters' member paths
    into the delta set so live-delta re-examines them regardless of file
    changes."""

    def test_marked_members_folded_into_changed_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian
        from athenaeum.models import AutoMemoryFile

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))

        root = tmp_path / "root"
        root.mkdir()
        marked = root / "m.md"
        marked.write_text("---\nname: p\ntype: feedback\n---\nx\n", encoding="utf-8")
        changed = root / "c.md"
        changed.write_text("---\nname: q\ntype: feedback\n---\ny\n", encoding="utf-8")

        # A cluster left detection-incomplete on a prior run, whose member file
        # did NOT change this run.
        detection_state.mark_incomplete(cache_dir, "cid-1", [str(marked)])

        captured: dict[str, set[Path]] = {}

        def _fake_delta(auto_memory_files, changed_paths, *_a, **_k):  # type: ignore[no-untyped-def]
            captured["changed"] = set(changed_paths)
            return {"cid-x"}  # non-None → _run_cluster_pass returns without chromadb

        monkeypatch.setattr(librarian, "_delta_cluster_pass", _fake_delta)

        amf = AutoMemoryFile(
            path=changed, origin_scope="s", memory_type="feedback", name="q"
        )
        config = {"recall": {"extra_intake_roots": [str(root)]}}
        librarian._run_cluster_pass(
            [amf],
            tmp_path,
            config=config,
            dry_run=False,
            changed_paths={changed.resolve()},
        )

        # Both the genuinely-changed file AND the marked-but-unchanged member
        # are in the delta set handed to the closure.
        assert changed.resolve() in captured["changed"]
        assert marked.resolve() in captured["changed"]
