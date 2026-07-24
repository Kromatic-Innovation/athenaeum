"""Tests for the source-handle registry (issue #453).

Covers the two shipped deliverables:

1. **Schema** — both entity templates carry the source-handle keys and still
   parse as YAML; the keys round-trip through tier0 passthrough (the whole
   point of putting handles on the entity page) without being dropped.
2. **Index builder** — ``athenaeum.registry.build_registry`` /
   ``athenaeum registry`` compiles wiki entity frontmatter into a well-formed
   ``registry.json``, INCLUDING the degenerate zero-populated-handles case
   (issue #453/#454: the seed lands later and must not gate the builder).

All fixtures are synthetic — no client data lives in this public repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from athenaeum.cli import main
from athenaeum.registry import (
    LIST_HANDLE_KEYS,
    SCALAR_HANDLE_KEYS,
    SOURCE_HANDLE_KEYS,
    build_registry,
    collect_handles,
    render_registry,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "athenaeum" / "templates"


def _write_entity(
    wiki: Path,
    filename: str,
    *,
    uid: str,
    etype: str = "company",
    name: str = "Test",
    extra: dict | None = None,
) -> None:
    meta: dict = {"uid": uid, "type": etype, "name": name}
    if extra:
        meta.update(extra)
    fm = yaml.dump(meta, sort_keys=False, allow_unicode=True)
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / filename).write_text(
        f"---\n{fm}---\n\n# {name}\n\nBody.\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Schema: templates carry the keys and round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["person.md", "company.md"])
def test_templates_carry_source_handle_keys(template: str) -> None:
    text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm = text[4:].split("\n---", 1)[0]
    meta = yaml.safe_load(fm)
    assert isinstance(meta, dict)
    for key in SOURCE_HANDLE_KEYS:
        assert key in meta, f"{template} missing source-handle key {key!r}"
    # List keys default to empty lists; scalars default to empty string.
    for key in LIST_HANDLE_KEYS:
        assert meta[key] == [], f"{template} {key!r} should default to []"
    for key in SCALAR_HANDLE_KEYS:
        assert meta[key] == "", f"{template} {key!r} should default to ''"


@pytest.mark.parametrize("template", ["person.md", "company.md"])
def test_template_empty_handles_yield_no_registry_entry(template: str) -> None:
    """An unpopulated (scaffold) entity contributes nothing to the registry."""
    text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    fm = text[4:].split("\n---", 1)[0]
    meta = yaml.safe_load(fm)
    assert collect_handles(meta) == {}


def test_handles_roundtrip_through_tier0_passthrough(tmp_path: Path) -> None:
    """The keys must survive tier0 passthrough (the reason they live on the
    entity page). Regression guard for the Tier 2/3 allowlist dropping them."""
    from athenaeum.librarian import tier0_passthrough
    from athenaeum.models import EntityIndex, RawFile

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    raw_dir = tmp_path / "raw" / "contact-wiki"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "seed-acme.md"
    raw_path.write_text(
        "---\n"
        "uid: company-acme\n"
        "type: company\n"
        "name: Acme\n"
        "domains:\n  - acme.example\n"
        "slack_channels:\n  - acme-team\n"
        "linkedin_url: https://www.linkedin.com/company/acme\n"
        "handles_verified: '2026-07-24'\n"
        "---\n\n# Acme\n\nBody.\n",
        encoding="utf-8",
    )
    raw = RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8="")
    index = EntityIndex(wiki)

    entity = tier0_passthrough(raw, index, wiki, ["company"])
    assert entity is not None

    written = (wiki / "company-acme-acme.md").read_text(encoding="utf-8")
    for needle in ("domains:", "acme.example", "slack_channels:", "acme-team",
                   "linkedin_url:", "handles_verified:"):
        assert needle in written, f"tier0 dropped {needle!r}"

    # And the registry builder then picks them up off the compiled page.
    registry = build_registry(wiki)
    assert registry["entities"]["company-acme"]["handles"] == {
        "domains": ["acme.example"],
        "slack_channels": ["acme-team"],
        "linkedin_url": "https://www.linkedin.com/company/acme",
        "handles_verified": "2026-07-24",
    }


# --------------------------------------------------------------------------
# collect_handles unit behaviour
# --------------------------------------------------------------------------


def test_collect_handles_only_populated_keys() -> None:
    meta = {
        "uid": "company-x",
        "type": "company",
        "name": "X",
        "domains": ["x.example"],
        "alt_emails": [],  # empty → omitted
        "slack_channels": [""],  # whitespace-only entries → omitted
        "linkedin_url": "",  # empty scalar → omitted
        "handles_verified": "2026-07-24",
    }
    assert collect_handles(meta) == {
        "domains": ["x.example"],
        "handles_verified": "2026-07-24",
    }


def test_collect_handles_tolerates_scalar_for_list_key() -> None:
    # A list key authored without brackets should still be collected.
    assert collect_handles({"domains": "solo.example"}) == {
        "domains": ["solo.example"]
    }


def test_collect_handles_drops_none_and_blank_entries() -> None:
    assert collect_handles({"domains": [None, "", "  ", "keep.example"]}) == {
        "domains": ["keep.example"]
    }


# --------------------------------------------------------------------------
# build_registry
# --------------------------------------------------------------------------


def test_empty_wiki_yields_well_formed_empty_registry(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    registry = build_registry(wiki)
    assert registry == {"version": 1, "entity_count": 0, "entities": {}}


def test_missing_wiki_dir_is_not_an_error(tmp_path: Path) -> None:
    registry = build_registry(tmp_path / "wiki")  # never created
    assert registry == {"version": 1, "entity_count": 0, "entities": {}}


def test_unpopulated_entities_produce_empty_registry(tmp_path: Path) -> None:
    """The #453/#454 degenerate case: entities exist, no handles populated."""
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "a.md", uid="company-a", extra={"domains": [], "linkedin_url": ""})
    _write_entity(wiki, "b.md", uid="person-b", etype="person", extra={"alt_emails": []})
    registry = build_registry(wiki)
    assert registry["entity_count"] == 0
    assert registry["entities"] == {}


def test_partial_registry_includes_only_handled_entities(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "acme.md", uid="company-acme", name="Acme",
                  extra={"domains": ["acme.example"]})
    _write_entity(wiki, "empty.md", uid="company-empty", name="Empty")  # no handles
    registry = build_registry(wiki)
    assert registry["entity_count"] == 1
    assert set(registry["entities"]) == {"company-acme"}
    assert registry["entities"]["company-acme"] == {
        "type": "company",
        "name": "Acme",
        "handles": {"domains": ["acme.example"]},
    }


def test_registry_is_deterministic_and_uid_sorted(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "z.md", uid="company-z", extra={"domains": ["z.example"]})
    _write_entity(wiki, "a.md", uid="company-a", extra={"domains": ["a.example"]})
    _write_entity(wiki, "m.md", uid="company-m", extra={"domains": ["m.example"]})
    registry = build_registry(wiki)
    assert list(registry["entities"]) == ["company-a", "company-m", "company-z"]
    # Byte-identical on re-run.
    assert render_registry(build_registry(wiki)) == render_registry(build_registry(wiki))


def test_underscore_and_uidless_pages_skipped(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "real.md", uid="company-real", extra={"domains": ["real.example"]})
    # _schema-style page skipped by name.
    _write_entity(wiki, "_schema.md", uid="schema-x", extra={"domains": ["skip.example"]})
    # Page with no uid skipped by content.
    (wiki / "nouid.md").write_text(
        "---\ntype: company\nname: NoUID\ndomains:\n  - nouid.example\n---\n\nBody.\n",
        encoding="utf-8",
    )
    registry = build_registry(wiki)
    assert set(registry["entities"]) == {"company-real"}


def test_type_agnostic_indexing(tmp_path: Path) -> None:
    """Any entity type carrying handles is indexed, not just person/company."""
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "proj.md", uid="project-p", etype="project", name="P",
                  extra={"domains": ["p.example"]})
    registry = build_registry(wiki)
    assert registry["entities"]["project-p"]["type"] == "project"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_registry_json_default_path(tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    _write_entity(wiki, "acme.md", uid="company-acme", name="Acme",
                  extra={"domains": ["acme.example"]})
    rc = main(["registry", "--path", str(knowledge)])
    assert rc == 0
    out_file = knowledge / "registry.json"
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["entity_count"] == 1
    assert data["entities"]["company-acme"]["handles"] == {"domains": ["acme.example"]}
    assert "1 entity" in capsys.readouterr().out


def test_cli_empty_wiki_writes_well_formed_registry(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    out = tmp_path / "custom-registry.json"
    rc = main(["registry", "--path", str(knowledge), "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"version": 1, "entity_count": 0, "entities": {}}


def test_cli_stdout_does_not_write_file(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    _write_entity(wiki, "acme.md", uid="company-acme", extra={"domains": ["acme.example"]})
    rc = main(["registry", "--path", str(knowledge), "--stdout"])
    assert rc == 0
    assert not (knowledge / "registry.json").exists()
    data = json.loads(capsys.readouterr().out)
    assert data["entities"]["company-acme"]["handles"] == {"domains": ["acme.example"]}
