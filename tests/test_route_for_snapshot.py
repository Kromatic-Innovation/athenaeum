"""Behaviour is unchanged by the storage resolver migration (athenaeum#3158).

A fixture knowledge root is run through the three migrated readers -- the
excluded-field join on the read path, ``process_correction_record`` and the
free-text value-routing vault location -- under table-driven configurations.
Each reader's output is compared with a reference that spells out the
pre-resolver computation straight from the raw configuration dict, so the
comparison does not pass through ``storage.route_for``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii
from athenaeum.corrections import process_correction_record
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.sensitivity_routing import SensitivityRoutingError, _vault_root_for_class
from athenaeum.storage import EXCLUDED, StorageConfigError

UID = "person-alex"


def _page(wiki: Path, meta: dict[str, str]) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / "alex.md"
    path.write_text(
        "---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\nBody.\n",
        encoding="utf-8",
    )
    return path


def _fixture_root(tmp_path: Path) -> Path:
    _page(tmp_path / "wiki", {"uid": UID, "type": "person", "name": "Alex Doe"})
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / f"{UID}.md").write_text(
        "---\n"
        f"uid: {UID}\n"
        "emails:\n  - alex@example.org\n"
        "phones:\n  - '+15550100'\n"
        "nickname: Al\n"
        "---\n",
        encoding="utf-8",
    )
    return tmp_path


def _storage(**tables: Any) -> dict[str, Any]:
    return {"storage": tables}


READ_CONFIGS: dict[str, dict[str, Any]] = {
    "mapping-only": _storage(mapping={"pii": "excluded"}),
    "explicit-list": _storage(mapping={"pii": "excluded"}, excluded_fields={"pii": ["nickname"]}),
    "empty-list": _storage(mapping={"pii": "excluded"}, excluded_fields={"pii": []}),
    "read-mapping-override": _storage(
        mapping={"pii": "excluded", "ledger": "excluded"},
        excluded_read_mapping={"person": "pii"},
    ),
}


def _reference_read_fields(cfg: dict[str, Any], record: dict[str, Any]) -> dict[str, list[str]]:
    """The old join, from the raw config: surface class, field list, then values."""
    storage = cfg.get("storage") or {}
    surface = (storage.get("excluded_read_mapping") or {}).get("person", "pii")
    configured = (storage.get("excluded_fields") or {}).get(surface)
    names = list(configured) if configured is not None else list(pii.CONTACT_DATA_FIELDS)
    out: dict[str, list[str]] = {}
    for name in names:
        raw = record.get(name)
        if raw is None:
            continue
        out[name] = [str(v) for v in (raw if isinstance(raw, list) else [raw])]
    return out


@pytest.mark.parametrize("name", sorted(READ_CONFIGS))
def test_read_path_join_is_unchanged(tmp_path: Path, name: str) -> None:
    root = _fixture_root(tmp_path)
    cfg = READ_CONFIGS[name]
    record, _ = parse_frontmatter((root / "excluded" / f"{UID}.md").read_text(encoding="utf-8"))
    surface = pii.surface_class_for_page_class("person", cfg)
    read = pii.read_entity(root, cfg, UID, surface_class=surface, include_excluded=True)
    expected = _reference_read_fields(cfg, record)
    assert read is not None
    assert {k: list(v) for k, v in read.contact.items()} == expected


VALUE_CONFIGS: dict[str, dict[str, Any]] = {
    "unmapped-is-refused": {},
    "mapped-excluded": _storage(mapping={"secret": "excluded"}),
    "mapped-to-wiki-refused": _storage(mapping={"secret": "wiki-markdown-embedded"}),
    "mapped-unknown-adapter": _storage(mapping={"secret": "no-such-adapter"}),
}


def _reference_vault_root(cfg: dict[str, Any], cls: str, root: Path) -> Path | str:
    mapping = (cfg.get("storage") or {}).get("mapping") or {}
    if cls.strip() not in mapping:
        return "unrouted"  # athenaeum#3168: no longer the built-in excluded location
    target = mapping[cls.strip()]
    if target == "no-such-adapter":
        return "invalid"
    if target == "wiki-markdown-embedded":
        return "in-corpus"
    return EXCLUDED.resolve_root(root) / "sensitivity" / cls


@pytest.mark.parametrize("name", sorted(VALUE_CONFIGS))
def test_value_routing_vault_location_is_unchanged(tmp_path: Path, name: str) -> None:
    cfg = VALUE_CONFIGS[name]
    expected = _reference_vault_root(cfg, "secret", tmp_path)
    if expected == "unrouted":
        with pytest.raises(SensitivityRoutingError, match="no storage.routes rule"):
            _vault_root_for_class("secret", cfg, tmp_path)
    elif expected == "invalid":
        with pytest.raises(SensitivityRoutingError, match="storage.mapping is invalid"):
            _vault_root_for_class("secret", cfg, tmp_path)
    elif expected == "in-corpus":
        with pytest.raises(SensitivityRoutingError, match="participates in the corpus"):
            _vault_root_for_class("secret", cfg, tmp_path)
    else:
        assert _vault_root_for_class("secret", cfg, tmp_path) == expected


def _corrections_cfg(extra_fields: list[str] | None, mapping: dict[str, str]) -> dict[str, Any]:
    """Corrections config routed through the storage keys (athenaeum#3162)."""
    storage: dict[str, Any] = {"mapping": mapping}
    if extra_fields is not None:
        storage["excluded_fields"] = {"pii": [*pii.BUILTIN_CONTACT_FIELDS, *extra_fields]}
    return {
        "librarian": {
            "corrections": {
                "fields": {"bounced": {"shape": "scalar", "writers": ["delivery-monitor"]}}
            }
        },
        "storage": storage,
    }


def _apply(root: Path, cfg: dict[str, Any]) -> Any:
    record = {
        "record": "correction",
        "target": {"type": "person", "handle": {"alt_emails": "alex@example.org"}},
        "op": "set",
        "field": "bounced",
        "value": "2026-08-06",
        "source": "api:delivery-monitor:2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
        "note": "permanent delivery failure",
    }
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "delivery-monitor",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    registry = {UID: {"type": "person", "handles": {"alt_emails": ["alex@example.org"]}}}
    return process_correction_record(
        record,
        envelope,
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        registry_entities=registry,
        config=cfg,
    )


def test_corrections_unrouted_field_writes_the_page(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    result = _apply(root, _corrections_cfg(None, {"pii": "excluded"}))
    assert result.disposition == "applied"
    meta, _ = parse_frontmatter((root / "wiki" / "alex.md").read_text(encoding="utf-8"))
    assert meta["bounced"] == "2026-08-06"
    record, _ = parse_frontmatter((root / "excluded" / f"{UID}.md").read_text(encoding="utf-8"))
    assert "bounced" not in record


def test_corrections_routed_field_writes_the_surface_record(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    page = root / "wiki" / "alex.md"
    before = page.read_text(encoding="utf-8")
    result = _apply(root, _corrections_cfg(["bounced"], {"pii": "excluded"}))
    assert result.disposition == "routed-elsewhere"
    assert page.read_text(encoding="utf-8") == before
    record, _ = parse_frontmatter((root / "excluded" / f"{UID}.md").read_text(encoding="utf-8"))
    assert record["bounced"] == "2026-08-06"


def test_corrections_routed_to_unknown_adapter_still_raises(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    with pytest.raises(StorageConfigError):
        _apply(root, _corrections_cfg(["bounced"], {"pii": "no-such-adapter"}))
