"""``storage.excluded_fields`` and ``storage.excluded_read_mapping`` are removed (athenaeum#3173).

A configuration that still sets either key is refused at the first command that
reads or writes a routed field, with the ``storage.routes`` entries that
reproduce what the keys did. The golden file was produced by the previous
release (the two keys, one fixture knowledge root) and the entries the refusal
prints must give byte-identical reads and writes on that same fixture. To
regenerate the golden, run ``scenario`` with ``OLD`` on a checkout of the
previous release (it uses only API both releases share) and dump the result as
sorted, indented JSON.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import pii
from athenaeum.config import _REMOVED_CONFIG_KEYS, find_removed_config_keys
from athenaeum.corrections import process_correction_record
from athenaeum.models import EntityIndex

GOLDEN = Path(__file__).parent / "fixtures" / "removed_routing_keys_golden.json"
UID = "person-alex"
CONTACT = ["emails", "phones", "former_emails", "alt_emails"]

# The previous release's configuration: both removed keys, `person` read as `pii`
# by the built-in default, plus a second class that is read through the read mapping.
OLD: dict[str, Any] = {
    "librarian": {
        "corrections": {
            "fields": {
                "nickname": {"shape": "scalar", "writers": ["delivery-monitor"]},
                "title": {"shape": "scalar", "writers": ["delivery-monitor"]},
            }
        }
    },
    "storage": {
        "mapping": {"pii": "excluded", "supplier": "excluded"},
        "excluded_read_mapping": {"vendor": "supplier"},
        "excluded_fields": {"pii": [*CONTACT, "nickname"], "supplier": ["iban"]},
    },
}


def _fixture_root(root: Path) -> Path:
    """A knowledge root as the previous release left it: pages, and records in ``excluded/``."""
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alex.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\n\nAlex builds widgets.\n",
        encoding="utf-8",
    )
    (wiki / "acme.md").write_text(
        "---\nuid: acme\ntype: vendor\nname: Acme Ltd\n---\n\nAcme sells widgets.\n",
        encoding="utf-8",
    )
    excluded = root / "excluded"
    excluded.mkdir()
    (excluded / f"{UID}.md").write_text(
        f"---\nuid: {UID}\npii: true\nemails:\n  - alex@example.org\n"
        "phones:\n  - '+15550100'\nnickname: Al\n---\n\nArchival data.\n",
        encoding="utf-8",
    )
    (excluded / "acme.md").write_text(
        "---\nuid: acme\npii: true\niban:\n  - GB00TEST0000\n---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return root


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*.md"))
    }


def _correct(root: Path, cfg: dict[str, Any], field: str) -> str:
    record = {
        "record": "correction",
        "target": {"uid": UID},
        "op": "set",
        "field": field,
        "value": "Lex",
        "source": "api:delivery-monitor:2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
        "note": "fixture",
    }
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "delivery-monitor",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    result = process_correction_record(
        record,
        envelope,
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        registry_entities={UID: {"type": "person", "handles": {"emails": ["alex@example.org"]}}},
        config=cfg,
    )
    return str(result.disposition)


def scenario(tmp_path: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Reads and writes on the fixture, in a shape both releases can produce."""
    out: dict[str, Any] = {"reads": {}, "writes": {}}
    root = _fixture_root(tmp_path / "reads")
    for uid, page_type in ((UID, "person"), ("acme", "vendor")):
        surface = pii.surface_class_for_page_class(page_type, cfg)
        for include in (True, False):
            read = pii.read_entity(root, cfg, uid, surface_class=surface, include_excluded=include)
            assert read is not None
            text = json.dumps(dataclasses.asdict(read), default=str, sort_keys=True)
            out["reads"][f"{uid}:{include}"] = json.loads(text.replace(str(root), "<root>"))
    out["reads"]["tree"] = _tree(root)  # a read moves and writes nothing
    for field in ("nickname", "title"):
        wroot = _fixture_root(tmp_path / f"write-{field}")
        out["writes"][field] = {"disposition": _correct(wroot, cfg, field), "tree": _tree(wroot)}
    return out


def _printed_routes(message: str) -> dict[str, Any]:
    lines = message.splitlines()
    start = lines.index("storage:")
    end = lines.index("", start)
    return yaml.safe_load("\n".join(lines[start:end]))


def _refusal(cfg: dict[str, Any]) -> str:
    from athenaeum.storage import RemovedRoutingKeysError, route_for

    with pytest.raises(RemovedRoutingKeysError) as exc:
        route_for("person", "emails", config=cfg)
    return str(exc.value)


def _pasted(old: dict[str, Any]) -> dict[str, Any]:
    pasted = _printed_routes(_refusal(old))
    new = copy.deepcopy(old)
    for key in ("excluded_fields", "excluded_read_mapping"):
        del new["storage"][key]
    new["storage"]["routes"] = pasted["storage"]["routes"]
    return new


# --- AC3: refused, with the routes that reproduce the old behaviour ---------


@pytest.mark.parametrize("key", ["excluded_fields", "excluded_read_mapping"])
def test_either_removed_key_is_refused_by_a_routed_read(key: str) -> None:
    from athenaeum.storage import RemovedRoutingKeysError, StorageConfigError

    cfg = {"storage": {"mapping": {"pii": "excluded"}, key: {"pii": ["emails"]}}}
    with pytest.raises(RemovedRoutingKeysError) as exc:
        pii.assemble_excluded_read(
            Path("page.md"),
            {},
            {"uid": "u", "emails": ["a@example.org"]},
            surface_class="pii",
            config=cfg,
            include_excluded=True,
        )
    assert f"storage.{key}" in str(exc.value)
    assert issubclass(RemovedRoutingKeysError, StorageConfigError)


def test_the_refusal_prints_the_routes_that_replace_the_keys() -> None:
    message = _refusal(OLD)
    routes = _printed_routes(message)["storage"]["routes"]
    assert {"page_type": "person", "fields": [*CONTACT, "nickname"], "to": ["excluded"]} in routes
    assert {"page_type": "vendor", "fields": ["iban"], "to": ["excluded"]} in routes
    assert "`storage.excluded_fields` and `storage.excluded_read_mapping`" in message
    assert "no file moves" in message


def test_an_empty_removed_key_is_not_a_setting() -> None:
    from athenaeum.storage import route_for

    cfg = {"storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {}}}
    assert route_for("person", "emails", config=cfg).targets == ()


def test_both_keys_are_registered_as_removed() -> None:
    for key in ("storage.excluded_fields", "storage.excluded_read_mapping"):
        assert key in _REMOVED_CONFIG_KEYS
    found = find_removed_config_keys(OLD)
    assert {k for k, _ in found} == {"storage.excluded_fields", "storage.excluded_read_mapping"}


def test_pasting_the_printed_routes_gives_the_previous_releases_reads_and_writes(
    tmp_path: Path,
) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert scenario(tmp_path / "pasted", _pasted(OLD)) == golden


# --- AC4: records written under the old keys are read in place ---------------


def test_records_written_by_the_previous_release_are_read_without_migration(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    before = _tree(root)
    cfg = _pasted(OLD)
    surface = pii.surface_class_for_page_class("person", cfg)
    read = pii.read_entity(root, cfg, UID, surface_class=surface, include_excluded=True)
    assert read is not None
    assert read.contact["emails"] == ["alex@example.org"]
    assert read.contact["phones"] == ["+15550100"]
    assert _tree(root) == before
    assert (root / "excluded" / f"{UID}.md").exists()
    assert not (root / "excluded" / "routes").exists()


def test_the_removed_resolvers_and_default_table_are_deleted() -> None:
    from athenaeum import config

    assert not hasattr(config, "resolve_excluded_fields_config")
    assert not hasattr(config, "resolve_excluded_read_mapping")
    assert not hasattr(pii, "DEFAULT_EXCLUDED_READ_MAPPING")

