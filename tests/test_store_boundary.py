# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501
"""Per-type stores: no merges across stores, no links to a more private store (athenaeum#2619)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from athenaeum._cmd_store_link_lint import EXIT_VIOLATIONS_FOUND, cmd_store_link_lint
from athenaeum.dedupe import DuplicatePair, merge_duplicate_persons
from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import EntityIndex, ProcessingResult, RawFile
from athenaeum.pending_merges import (
    CROSS_STORE_MERGE_ERROR_CODE,
    parse_pending_merges,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.storage import (
    StorageConfigError,
    available_adapters,
    resolve_locator_surfaces,
    surface_visibility,
)
from athenaeum.store_boundary import (
    CrossStoreMergeError,
    assert_same_surface,
    link_forbidden,
    locator_for,
)
from athenaeum.store_links import guard_entity_store_links, scan_store_links

PRIVATE_UID = "aaaa1111"
SHARED_UID = "bbbb2222"
SHARED2_UID = "cccc3333"


def _config(visibility: Any = 1) -> dict[str, Any]:
    return {
        "storage": {
            "adapters": {
                "recipes": {
                    "backing_store": "markdown",
                    "surface_root": "recipes",
                    "corpus_policy": {"embedded": True},
                    "visibility": visibility,
                }
            }
        }
    }


def _page(path: Path, uid: str, name: str, body: str = "", extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nuid: {uid}\ntype: reference\nname: {name}\n{extra}---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def kroot(tmp_path: Path) -> Path:
    """A knowledge root with a private wiki page and two shared `recipes` pages."""
    (tmp_path / "wiki").mkdir()
    _page(tmp_path / "wiki" / f"{PRIVATE_UID}-alice.md", PRIVATE_UID, "Alice")
    _page(tmp_path / "recipes" / f"{SHARED_UID}-soup.md", SHARED_UID, "Soup")
    _page(tmp_path / "recipes" / f"{SHARED2_UID}-bread.md", SHARED2_UID, "Bread")
    return tmp_path


def _index(kroot: Path, cfg: dict[str, Any]) -> EntityIndex:
    return EntityIndex(kroot / "wiki", resolve_locator_surfaces(kroot, cfg))


class TestVisibilityConfig:
    def test_wiki_is_most_private_and_unranked_defaults_to_zero(self) -> None:
        cfg = _config(visibility=None)
        assert surface_visibility("wiki", cfg) == 0
        assert surface_visibility("recipes", cfg) == 0
        assert surface_visibility("recipes", None) == 0

    def test_declared_rank_is_read(self) -> None:
        assert surface_visibility("recipes", _config(2)) == 2
        assert available_adapters(_config(2))["recipes"].visibility == 2

    @pytest.mark.parametrize("bad", [-1, "high", 1.5, True])
    def test_bad_rank_is_a_config_error(self, bad: Any) -> None:
        with pytest.raises(StorageConfigError):
            available_adapters(_config(bad))

    def test_link_direction_predicate(self) -> None:
        cfg = _config(1)
        assert link_forbidden("recipes", "wiki", cfg)  # shared -> private
        assert not link_forbidden("wiki", "recipes", cfg)  # private -> shared
        assert not link_forbidden("recipes", "recipes", cfg)
        assert not link_forbidden("recipes", "wiki", _config(None))  # unranked


class TestWriteBoundaryLinkGuard:
    def _guard(self, kroot, cfg, page_path, meta, body, **kw):
        rendered = "---\nuid: x\n---\n\n" + body
        return guard_entity_store_links(
            kroot / "wiki", page_path.name, rendered, meta, page_path=page_path,
            index=_index(kroot, cfg), config=cfg, **kw,
        )

    def test_shared_to_private_is_refused_and_logged_with_both_uids(self, kroot, caplog) -> None:
        cfg = _config(1)
        src = kroot / "recipes" / "dddd4444-stew.md"
        with caplog.at_level(logging.WARNING):
            ok = self._guard(kroot, cfg, src, {"uid": "dddd4444"}, f"Made with [[{PRIVATE_UID}]].")
        assert ok is False
        assert "dddd4444" in caplog.text and PRIVATE_UID in caplog.text
        assert "recipes" in caplog.text and "wiki" in caplog.text
        assert (kroot / "wiki" / "_store_link_rejected" / src.name).is_file()
        ledger = (kroot / "wiki" / "_store_link_rejected.jsonl").read_text()
        assert PRIVATE_UID in ledger and "dddd4444" in ledger

    @pytest.mark.parametrize(
        "meta,body",
        [
            ({"uid": "dddd4444", "related": [{"uid": PRIVATE_UID, "role": "see-also"}]}, ""),
            ({"uid": "dddd4444", "parent": PRIVATE_UID}, ""),
            ({"uid": "dddd4444"}, "See [Alice](aaaa1111-alice.md)."),
            ({"uid": "dddd4444"}, "See [[Alice]]."),
        ],
    )
    def test_every_link_shape_is_judged(self, kroot, meta, body) -> None:
        src = kroot / "recipes" / "dddd4444-stew.md"
        assert self._guard(kroot, _config(1), src, meta, body) is False

    def test_private_to_shared_is_allowed(self, kroot) -> None:
        src = kroot / "wiki" / "eeee5555-note.md"
        assert self._guard(kroot, _config(1), src, {"uid": "eeee5555"}, f"[[{SHARED_UID}]]") is True

    def test_same_surface_and_unresolvable_targets_are_allowed(self, kroot) -> None:
        src = kroot / "recipes" / "dddd4444-stew.md"
        body = f"[[{SHARED2_UID}]] and [[no-such-page]]"
        assert self._guard(kroot, _config(1), src, {"uid": "dddd4444"}, body) is True

    def test_unranked_deployment_refuses_nothing(self, kroot) -> None:
        src = kroot / "recipes" / "dddd4444-stew.md"
        assert self._guard(kroot, _config(None), src, {"uid": "dddd4444"}, f"[[{PRIVATE_UID}]]") is True

    def test_only_added_links_are_judged_on_update(self, kroot) -> None:
        src = kroot / "recipes" / f"{SHARED_UID}-soup.md"
        body = f"[[{PRIVATE_UID}]]"
        prev = "---\nuid: bbbb2222\n---\n\n" + body
        assert self._guard(kroot, _config(1), src, {"uid": SHARED_UID}, body, previous_text=prev) is True
        assert self._guard(kroot, _config(1), src, {"uid": SHARED_UID}, body) is False

    def test_apply_tier3_results_refuses_a_shared_update_linking_private(self, kroot) -> None:
        cfg = _config(1)
        target = kroot / "recipes" / f"{SHARED_UID}-soup.md"
        before = target.read_text()
        new = before + f"\nNow with [[{PRIVATE_UID}]].\n"
        result = ProcessingResult(raw_file=RawFile(path=kroot / "r.md", source="claude-session", timestamp="20260807T090000Z", uuid8="aabb0011", _content="c"))
        _apply_tier3_results(
            result, new_entities=[], pending_updates=[(target, new)], updated_uids=[SHARED_UID],
            escalations=[], wiki_root=kroot / "wiki", index=_index(kroot, cfg), config=cfg,
        )
        assert result.store_link_rejected == 1
        assert result.updated == []
        assert target.read_text() == before


class TestLint:
    def _args(self, kroot: Path, as_json: bool = False) -> argparse.Namespace:
        return argparse.Namespace(path=kroot, json=as_json)

    def test_lists_every_violation_and_exits_nonzero(self, kroot, capsys) -> None:
        (kroot / "athenaeum.yaml").write_text(
            "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
            "      surface_root: recipes\n      visibility: 1\n      corpus_policy:\n        embedded: true\n"
        )
        _page(kroot / "recipes" / "dddd4444-stew.md", "dddd4444", "Stew", f"[[{PRIVATE_UID}]] [[{SHARED2_UID}]]")
        _page(kroot / "wiki" / "eeee5555-note.md", "eeee5555", "Note", f"[[{SHARED_UID}]]")
        report = scan_store_links(kroot)
        assert [(v.source_uid, v.target_uid) for v in report.violations] == [("dddd4444", PRIVATE_UID)]
        assert cmd_store_link_lint(self._args(kroot, as_json=True)) == EXIT_VIOLATIONS_FOUND
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False and payload["violations"][0]["target_uid"] == PRIVATE_UID

    def test_clean_corpus_exits_zero(self, kroot) -> None:
        (kroot / "athenaeum.yaml").write_text(
            "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
            "      surface_root: recipes\n      visibility: 1\n      corpus_policy:\n        embedded: true\n"
        )
        _page(kroot / "wiki" / "eeee5555-note.md", "eeee5555", "Note", f"[[{SHARED_UID}]]")
        assert cmd_store_link_lint(self._args(kroot)) == 0


class TestNoMergeAcrossStores:
    def test_assert_same_surface(self, kroot) -> None:
        loc = locator_for(kroot / "wiki", _config(1))
        wiki = kroot / "wiki" / f"{PRIVATE_UID}-alice.md"
        a = kroot / "recipes" / f"{SHARED_UID}-soup.md"
        b = kroot / "recipes" / f"{SHARED2_UID}-bread.md"
        assert_same_surface(loc, [a, b])
        with pytest.raises(CrossStoreMergeError):
            assert_same_surface(loc, [wiki, a])

    def test_merge_duplicate_persons_refuses_a_cross_store_pair(self, kroot) -> None:
        (kroot / "athenaeum.yaml").write_text(
            "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
            "      surface_root: recipes\n      corpus_policy:\n        embedded: true\n"
        )
        pair = DuplicatePair(
            canonical_uid=PRIVATE_UID, absorbed_uid=SHARED_UID, match_signal="name_exact",
            confidence="HIGH",
            canonical_path=str(kroot / "wiki" / f"{PRIVATE_UID}-alice.md"),
            absorbed_path=str(kroot / "recipes" / f"{SHARED_UID}-soup.md"),
        )
        with pytest.raises(CrossStoreMergeError):
            merge_duplicate_persons([pair], apply=True, wiki_root=kroot / "wiki")
        assert (kroot / "recipes" / f"{SHARED_UID}-soup.md").exists()

    def test_resolve_merge_refuses_cross_store_approval(self, kroot) -> None:
        (kroot / "athenaeum.yaml").write_text(
            "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
            "      surface_root: recipes\n      corpus_policy:\n        embedded: true\n"
        )
        merges = kroot / "wiki" / "_pending_merges.md"
        write_pending_merge(
            merges,
            merge_target_name="Alice",
            sources=[
                str(kroot / "wiki" / f"{PRIVATE_UID}-alice.md"),
                str(kroot / "recipes" / f"{SHARED_UID}-soup.md"),
            ],
            rationale="same",
            draft_merged_body="merged",
            confidence=0.9,
        )
        before = merges.read_text()
        mid = parse_pending_merges(merges)[0].id
        res = resolve_merge(merges, mid, "approve", wiki_root=kroot / "wiki")
        assert res["ok"] is False
        assert res["error_code"] == CROSS_STORE_MERGE_ERROR_CODE
        assert merges.read_text() == before


class TestOtherWritePaths:
    def test_structured_upsert_refuses_and_leaves_page_and_raw(self, kroot, tmp_path) -> None:
        from athenaeum.structured_upsert import REASON_STORE_BOUNDARY, tier0_structured_upsert

        cfg = _config(1)
        cfg["librarian"] = {
            "structured_upsert": {"reference": {"sections": ["Notes"], "append_sections": ["Notes"]}}
        }
        page = kroot / "recipes" / f"{SHARED_UID}-soup.md"
        page.write_text(
            f"---\nuid: {SHARED_UID}\ntype: reference\nname: Soup\naccess: internal\n---\n\n# Soup\n\n## Notes\n\n- a\n",
            encoding="utf-8",
        )
        before = page.read_text()
        raw_path = kroot / "raw.md"
        raw_path.write_text(
            f"---\nuid: {SHARED_UID}\ntype: reference\nname: Soup\n---\n\n# Soup\n\n## Notes\n\n- see [[{PRIVATE_UID}]]\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="src", timestamp="", uuid8="")
        out = tier0_structured_upsert(raw, _index(kroot, cfg), kroot / "wiki", ["reference"], config=cfg)
        assert out is not None and out.status == "refused" and out.reason == REASON_STORE_BOUNDARY
        assert page.read_text() == before
        assert raw_path.exists()

    def test_structured_upsert_allows_a_shared_link(self, kroot) -> None:
        from athenaeum.structured_upsert import tier0_structured_upsert

        cfg = _config(1)
        cfg["librarian"] = {
            "structured_upsert": {"reference": {"sections": ["Notes"], "append_sections": ["Notes"]}}
        }
        page = kroot / "recipes" / f"{SHARED_UID}-soup.md"
        page.write_text(
            f"---\nuid: {SHARED_UID}\ntype: reference\nname: Soup\naccess: internal\n---\n\n# Soup\n\n## Notes\n\n- a\n",
            encoding="utf-8",
        )
        raw_path = kroot / "raw.md"
        raw_path.write_text(
            f"---\nuid: {SHARED_UID}\ntype: reference\nname: Soup\n---\n\n# Soup\n\n## Notes\n\n- see [[{SHARED2_UID}]]\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="src", timestamp="", uuid8="")
        out = tier0_structured_upsert(raw, _index(kroot, cfg), kroot / "wiki", ["reference"], config=cfg)
        assert out is not None and out.status == "applied"

    def test_batch_refusal_retains_the_raw(self, tmp_path, monkeypatch) -> None:
        from athenaeum import batch as batch_mod
        from athenaeum.batch import process_batch_run
        from athenaeum.intake import discover_raw_files
        from athenaeum.models import TokenUsage
        from tests.test_batch_mode import (
            FALLBACK_ACCESS,
            FALLBACK_TAGS,
            _FakeClient,
            _scripted_responder,
            _seed_root,
        )

        monkeypatch.setattr(batch_mod, "admit_page_write", lambda *a, **k: False)
        root = _seed_root(tmp_path, "k", ["Standalone fact about WidgetPlate gadget.\n"])
        raws = discover_raw_files(root / "raw")
        result = process_batch_run(
            raws, EntityIndex(root / "wiki"), root / "wiki",
            _FakeClient(_scripted_responder, allow_sync=False), ["concept", "reference"],
            FALLBACK_TAGS, FALLBACK_ACCESS, usage=TokenUsage(), config=None, max_api_calls=100,
        )
        assert result.created == 0
        assert all(r.path.exists() for r in raws)

    def test_admit_page_write_is_one_shape_and_notices_an_unindexed_surface(self, kroot) -> None:
        from athenaeum.store_links import admit_page_write

        cfg = _config(1)
        bad = f"---\nuid: dddd4444\n---\n\n[[{PRIVATE_UID}]]"
        page = kroot / "recipes" / "dddd4444-stew.md"
        # index built WITHOUT the surfaces: the helper still judges correctly
        assert admit_page_write(kroot / "wiki", page, bad, index=EntityIndex(kroot / "wiki"), config=cfg) is False
        assert admit_page_write(kroot / "wiki", kroot / "wiki" / "x.md", bad, config=cfg) is True

    def test_dedupe_merge_refused_when_it_would_add_a_private_link(self, kroot, monkeypatch) -> None:
        from athenaeum import dedupe

        calls: list[str] = []
        monkeypatch.setattr(dedupe, "admit_page_write", lambda *a, **k: calls.append(k["source"]) or False)
        a = _page(kroot / "wiki" / "ffff6666-a.md", "ffff6666", "Same")
        b = _page(kroot / "wiki" / "ffff7777-b.md", "ffff7777", "Same")
        pair = DuplicatePair("ffff6666", "ffff7777", "name_exact", "HIGH", str(a), str(b))
        rep = merge_duplicate_persons([pair], apply=True, wiki_root=kroot / "wiki")
        assert calls == ["dedupe-merge"] and rep.merged == 0 and b.exists() and rep.errors


class TestPendingMergesAcrossSurfaces:
    def _surface_cfg(self, kroot: Path) -> None:
        (kroot / "athenaeum.yaml").write_text(
            "storage:\n  adapters:\n    recipes:\n      backing_store: markdown\n"
            "      surface_root: recipes\n      corpus_policy:\n        embedded: true\n"
        )

    def test_target_and_source_on_extra_surface_resolve(self, kroot) -> None:
        from athenaeum.pending_merges import resolve_source_path, resolve_target_page

        self._surface_cfg(kroot)
        assert resolve_target_page("Soup", kroot / "wiki") == kroot / "recipes" / f"{SHARED_UID}-soup.md"
        assert resolve_source_path(f"{SHARED2_UID}-bread.md", wiki_root=kroot / "wiki") == (
            kroot / "recipes" / f"{SHARED2_UID}-bread.md"
        )

    def test_target_on_extra_surface_with_wiki_source_is_refused_not_created(self, kroot) -> None:
        self._surface_cfg(kroot)
        merges = kroot / "wiki" / "_pending_merges.md"
        write_pending_merge(
            merges, merge_target_name="Soup",
            sources=[str(kroot / "wiki" / f"{PRIVATE_UID}-alice.md")],
            rationale="same", draft_merged_body="merged", confidence=0.9,
        )
        before = merges.read_text()
        res = resolve_merge(merges, parse_pending_merges(merges)[0].id, "approve", wiki_root=kroot / "wiki")
        assert res["error_code"] == CROSS_STORE_MERGE_ERROR_CODE
        assert merges.read_text() == before
        assert not (kroot / "wiki" / "soup.md").exists()

    def test_surface_sources_with_a_wiki_create_target_are_refused(self, kroot) -> None:
        self._surface_cfg(kroot)
        merges = kroot / "wiki" / "_pending_merges.md"
        write_pending_merge(
            merges, merge_target_name="Brand New Dish",
            sources=[str(kroot / "recipes" / f"{SHARED_UID}-soup.md")],
            rationale="same", draft_merged_body="merged", confidence=0.9,
        )
        res = resolve_merge(merges, parse_pending_merges(merges)[0].id, "approve", wiki_root=kroot / "wiki")
        assert res["error_code"] == CROSS_STORE_MERGE_ERROR_CODE
        assert not (kroot / "wiki" / "brand-new-dish.md").exists()


class TestRoutedCreate:
    def test_type_mapped_to_shared_surface_cannot_link_wiki_page(self, kroot) -> None:
        from athenaeum.models import WikiEntity

        cfg = _config(1)
        cfg["storage"]["mapping"] = {"recipe": "recipes"}
        (kroot / "wiki" / "_schema").mkdir()
        (kroot / "wiki" / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| recipe |\n| reference |\n", encoding="utf-8"
        )
        entity = WikiEntity(
            uid="dddd4444", type="recipe", name="Stew", created="2026-11-15", updated="2026-11-15",
            body=f"# Stew\n\nCooked for [[{PRIVATE_UID}]].\n",
        )
        raw = RawFile(path=kroot / "r.md", source="claude-session", timestamp="20260807T090000Z", uuid8="aabb0011", _content="c")
        result = ProcessingResult(raw_file=raw)
        _apply_tier3_results(
            result, new_entities=[entity], pending_updates=[], updated_uids=[], escalations=[],
            wiki_root=kroot / "wiki", index=_index(kroot, cfg), config=cfg,
        )
        assert result.created == [] and result.store_link_rejected == 1
        assert result.retain_raw is True
        assert not (kroot / "recipes" / entity.filename).exists()
        assert not (kroot / "wiki" / entity.filename).exists()

    def test_same_type_without_the_link_is_written_to_the_surface(self, kroot) -> None:
        from athenaeum.models import WikiEntity

        cfg = _config(1)
        cfg["storage"]["mapping"] = {"recipe": "recipes"}
        (kroot / "wiki" / "_schema").mkdir()
        (kroot / "wiki" / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| recipe |\n| reference |\n", encoding="utf-8"
        )
        entity = WikiEntity(
            uid="dddd4444", type="recipe", name="Stew", created="2026-11-15", updated="2026-11-15",
            body=f"# Stew\n\nWith [[{SHARED2_UID}]].\n",
        )
        raw = RawFile(path=kroot / "r.md", source="claude-session", timestamp="20260807T090000Z", uuid8="aabb0011", _content="c")
        result = ProcessingResult(raw_file=raw)
        _apply_tier3_results(
            result, new_entities=[entity], pending_updates=[], updated_uids=[], escalations=[],
            wiki_root=kroot / "wiki", index=_index(kroot, cfg), config=cfg,
        )
        assert [e.uid for e in result.created] == ["dddd4444"]
        assert (kroot / "recipes" / entity.filename).is_file()
