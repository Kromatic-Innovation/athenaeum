# SPDX-License-Identifier: Apache-2.0
"""Every ``validate_wiki_meta`` site that holds a declared-type list passes it (athenaeum#2362).

Each test declares a type that is not in ``KNOWN_TYPES`` and turns the
unknown-type warning into an error, so dropping ``declared_types=`` at the site
under test fails the test.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from athenaeum.models import EntityIndex, ProcessingResult, RawFile, WikiEntity
from athenaeum.schemas import KNOWN_TYPES

TYPE = "dishware"  # declared by the deployment, not built in
assert TYPE not in KNOWN_TYPES
SCHEMA = f"# Types\n\n| Type |\n|------|\n| {TYPE} |\n| concept |\n"


@pytest.fixture(autouse=True)
def _unknown_type_warning_is_an_error() -> object:
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message="unknown wiki type")
        yield


def _declare(wiki: Path) -> None:
    (wiki / "_schema").mkdir(parents=True, exist_ok=True)
    (wiki / "_schema" / "types.md").write_text(SCHEMA, encoding="utf-8")


def _raw(tmp_path: Path, content: str) -> RawFile:
    d = tmp_path / "raw" / "src"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "r.md"
    p.write_text(content, encoding="utf-8")
    return RawFile(path=p, source="src", timestamp="", uuid8="")


def _page(wiki: Path, uid: str = f"{TYPE}-x") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    p = wiki / f"{uid}.md"
    p.write_text(
        f"---\nuid: {uid}\ntype: {TYPE}\nname: X\naccess: internal\n---\n\n"
        "# X\n\n## Notes\n\n- a\n",
        encoding="utf-8",
    )
    return p


def test_structured_upsert_passes_declared_types(tmp_path: Path) -> None:
    from athenaeum.structured_upsert import tier0_structured_upsert

    wiki = tmp_path / "wiki"
    page = _page(wiki)
    raw = _raw(
        tmp_path,
        f"---\nuid: {TYPE}-x\ntype: {TYPE}\nname: X\n---\n\n# X\n\n## Notes\n\n- b\n",
    )
    config = {
        "librarian": {
            "structured_upsert": {
                TYPE: {"sections": ["Notes"], "append_sections": ["Notes"]},
            }
        }
    }
    out = tier0_structured_upsert(raw, EntityIndex(wiki), wiki, [TYPE], config=config)
    assert out is not None and out.status == "applied"
    assert "- b" in page.read_text(encoding="utf-8")


def test_handle_upsert_passes_declared_types(tmp_path: Path) -> None:
    from athenaeum.librarian import tier0_handle_upsert

    wiki = tmp_path / "wiki"
    page = _page(wiki)
    raw = _raw(
        tmp_path,
        f"---\nuid: {TYPE}-x\ntype: {TYPE}\nname: X\ndomains:\n  - x.example\n---\n\n# X\n",
    )
    out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, [TYPE])
    assert out is not None and out[1] is True
    assert "x.example" in page.read_text(encoding="utf-8")


def test_tier3_create_passes_declared_types(tmp_path: Path) -> None:
    from athenaeum.librarian import _apply_tier3_results

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _declare(wiki)
    raw = _raw(tmp_path, "unused\n")
    result = ProcessingResult(raw_file=raw)
    entity = WikiEntity(
        uid="dish0001",
        type=TYPE,
        name="Blue Plate",
        created="2026-11-08",
        updated="2026-11-08",
        body="# Blue Plate\n\nA plate.\n",
    )
    _apply_tier3_results(
        result,
        new_entities=[entity],
        pending_updates=[],
        updated_uids=[],
        escalations=[],
        wiki_root=wiki,
        index=EntityIndex(wiki),
        config=None,
    )
    assert [e.uid for e in result.created] == ["dish0001"]


def _corrections_record(**target: object) -> dict:
    return {
        "record": "correction",
        "target": target,
        "op": "set",
        "field": "industry",
        "value": "Ceramics",
        "source": "api:apollo",
        "observed_at": "2026-08-06T00:00:00Z",
    }


def _corrections_cfg() -> dict:
    return {
        "librarian": {
            "corrections": {
                "fields": {"industry": {"shape": "scalar", "writers": ["employer-feed"]}}
            }
        }
    }


def _corrections_envelope() -> dict:
    return {
        "record": "batch",
        "schema_version": 1,
        "submitter": "employer-feed",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }


def test_corrections_create_passes_declared_types(tmp_path: Path) -> None:
    from athenaeum.corrections import process_correction_record

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _declare(wiki)
    result = process_correction_record(
        _corrections_record(type=TYPE, handle={"domains": "plates.example"}),
        _corrections_envelope(),
        index=EntityIndex(wiki),
        knowledge_root=tmp_path,
        registry_entities={},
        config=_corrections_cfg(),
    )
    assert result.disposition == "applied"


def test_corrections_update_passes_declared_types(tmp_path: Path) -> None:
    from athenaeum.corrections import process_correction_record

    wiki = tmp_path / "wiki"
    _page(wiki)
    _declare(wiki)
    result = process_correction_record(
        _corrections_record(uid=f"{TYPE}-x"),
        _corrections_envelope(),
        index=EntityIndex(wiki),
        knowledge_root=tmp_path,
        registry_entities={},
        config=_corrections_cfg(),
    )
    assert result.disposition == "applied"


def test_batch_create_passes_declared_types(tmp_path: Path) -> None:
    import json

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

    def responder(params: dict) -> str:
        out = _scripted_responder(params)
        if out.startswith("["):
            return json.dumps([{**e, "entity_type": TYPE} for e in json.loads(out)])
        return out

    root = _seed_root(tmp_path, "k", ["Standalone fact about WidgetPlate gadget.\n"])
    _declare(root / "wiki")
    result = process_batch_run(
        discover_raw_files(root / "raw"),
        EntityIndex(root / "wiki"),
        root / "wiki",
        _FakeClient(responder, allow_sync=False),
        [TYPE, "concept"],
        FALLBACK_TAGS,
        FALLBACK_ACCESS,
        usage=TokenUsage(),
        config=None,
        max_api_calls=100,
    )
    assert result.created == 1
