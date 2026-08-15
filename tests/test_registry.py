"""Tests for the source-handle registry (issue athenaeum#453).

Covers the two shipped deliverables:

1. **Schema** — both entity templates carry the source-handle keys and still
   parse as YAML; the keys round-trip through tier0 passthrough (the whole
   point of putting handles on the entity page) without being dropped.
2. **Index builder** — ``athenaeum.registry.build_registry`` /
   ``athenaeum registry`` compiles wiki entity frontmatter into a well-formed
   ``registry.json``, INCLUDING the degenerate zero-populated-handles case
   (issue athenaeum#453/#454: the seed lands later and must not gate the builder).

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
# Structured seed onto an EXISTING entity survives compile as frontmatter
# (issue athenaeum#486 — a re-seed must not be flattened into prose by the LLM tiers)
# --------------------------------------------------------------------------


def _cbusa_seed_raw(raw_dir: Path) -> "RawFile":  # noqa: F821 (local import below)
    """A raw-intake seed carrying athenaeum#453's source-handle block for CBUSA.

    Uses the CBUSA shape the live incident (2026-07-27) hit — note ``cbusa.us``,
    not ``.com`` — so this fixture pins the exact case that had to be hand-edited.
    """
    from athenaeum.models import RawFile

    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "seed-cbusa.md"
    raw_path.write_text(
        "---\n"
        "uid: company-cbusa\n"
        "type: company\n"
        "name: CBUSA\n"
        "domains:\n  - cbusa.us\n"
        "slack_channels:\n  - cbusa-team\n"
        "linkedin_url: https://www.linkedin.com/company/cbusa\n"
        "handles_verified: '2026-07-27'\n"
        "---\n\n# CBUSA\n\nseed body — must not become the entity's prose.\n",
        encoding="utf-8",
    )
    return RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8="")


def _existing_cbusa_page(wiki: Path) -> Path:
    """A CBUSA company page that exists but has NO handles yet (pre-seed state)."""
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / "company-cbusa-cbusa.md"
    page.write_text(
        "---\nuid: company-cbusa\ntype: company\nname: CBUSA\n"
        "access: internal\ntags:\n  - client\n---\n\n# CBUSA\n\nA client account.\n",
        encoding="utf-8",
    )
    return page


def _run_seed(wiki: Path, raw: "RawFile"):  # noqa: F821
    """Compile ``raw`` through ``process_one`` with a client that MUST NOT run.

    A structured source-handle seed onto a known entity is handled by the
    deterministic Tier-0 upsert (athenaeum#486); if any LLM call fires, the handles would
    be classified into prose — so the mock raises, turning that regression into a
    hard test failure rather than a silent quality loss.
    """
    from unittest.mock import MagicMock

    from athenaeum.librarian import process_one
    from athenaeum.models import EntityIndex

    client = MagicMock()
    client.messages.create.side_effect = AssertionError(
        "LLM tiers must not run for a structured source-handle seed (athenaeum#486)"
    )
    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        client,
        valid_types=["company", "person"],
        valid_tags=["client"],
        valid_access=["open", "internal", "confidential", "personal"],
    )
    client.messages.create.assert_not_called()
    return result


def test_seed_onto_existing_entity_lands_as_frontmatter_not_prose(tmp_path: Path) -> None:
    """A raw-intake source-handle seed for an entity that already exists compiles
    onto the page's frontmatter (matching athenaeum#453's schema) — never folded into the
    body prose — and the registry resolves it end to end (issue athenaeum#486 acceptance)."""
    wiki = tmp_path / "wiki"
    page = _existing_cbusa_page(wiki)
    raw = _cbusa_seed_raw(tmp_path / "raw" / "contact-wiki")

    result = _run_seed(wiki, raw)
    assert result.updated == ["company-cbusa"]
    assert not result.created

    written = page.read_text(encoding="utf-8")
    frontmatter, _, body = written.partition("\n---\n")
    # Handles land as frontmatter keys...
    for needle in (
        "domains:",
        "cbusa.us",
        "slack_channels:",
        "cbusa-team",
        "linkedin_url:",
        "handles_verified:",
    ):
        assert needle in frontmatter, f"seed dropped {needle!r} from frontmatter"
    # ...and the seed body did NOT replace / pollute the entity's own prose.
    assert "A client account." in body
    assert "must not become the entity's prose" not in written

    # registry.json resolves the seeded entity end to end (athenaeum#453 index builder).
    registry = build_registry(wiki)
    assert registry["entities"]["company-cbusa"]["handles"] == {
        "domains": ["cbusa.us"],
        "slack_channels": ["cbusa-team"],
        "linkedin_url": "https://www.linkedin.com/company/cbusa",
        "handles_verified": "2026-07-27",
    }


def test_reseeding_the_same_handles_is_a_noop(tmp_path: Path) -> None:
    """Idempotent re-seed (issue athenaeum#486 AC #4): seeding the same handles twice must
    not duplicate, re-flatten, or otherwise change the compiled page — the second
    pass is a byte-for-byte no-op and reports no update."""
    wiki = tmp_path / "wiki"
    page = _existing_cbusa_page(wiki)

    _run_seed(wiki, _cbusa_seed_raw(tmp_path / "raw" / "contact-wiki"))
    after_first = page.read_text(encoding="utf-8")

    # Second, identical seed (fresh raw dir so it is a distinct intake file).
    result2 = _run_seed(wiki, _cbusa_seed_raw(tmp_path / "raw" / "contact-wiki-2"))

    assert result2.updated == []  # no handle delta → nothing reported as updated
    assert page.read_text(encoding="utf-8") == after_first  # byte-for-byte stable


def test_seed_updates_a_changed_handle_value(tmp_path: Path) -> None:
    """A seed that CHANGES a handle re-lands it as frontmatter (not a stale
    no-op) while leaving untouched handle keys intact (issue athenaeum#486)."""
    wiki = tmp_path / "wiki"
    _existing_cbusa_page(wiki)
    _run_seed(wiki, _cbusa_seed_raw(tmp_path / "raw" / "contact-wiki"))

    # Re-seed with an added slack channel.
    from athenaeum.models import RawFile

    raw_dir = tmp_path / "raw" / "contact-wiki-3"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "seed-cbusa.md"
    raw_path.write_text(
        "---\nuid: company-cbusa\ntype: company\nname: CBUSA\n"
        "slack_channels:\n  - cbusa-team\n  - cbusa-exec\n---\n\n# CBUSA\n\nseed\n",
        encoding="utf-8",
    )
    result = _run_seed(wiki, RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8=""))

    assert result.updated == ["company-cbusa"]
    registry = build_registry(wiki)
    handles = registry["entities"]["company-cbusa"]["handles"]
    assert handles["slack_channels"] == ["cbusa-team", "cbusa-exec"]
    # Untouched handle keys from the first seed survive the second seed.
    assert handles["domains"] == ["cbusa.us"]
    assert handles["linkedin_url"] == "https://www.linkedin.com/company/cbusa"


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
    """The athenaeum#453/#454 degenerate case: entities exist, no handles populated."""
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


def test_registry_includes_apollo_organization_id_handle(tmp_path: Path) -> None:
    """A company page carrying `apollo_organization_id` (issue athenaeum#874)
    appears in ``registry.json`` under ``handles``, exactly like any other
    scalar handle key."""
    wiki = tmp_path / "wiki"
    _write_entity(wiki, "acme.md", uid="company-acme", name="Acme",
                  extra={"apollo_organization_id": "5f1a2b3c"})
    registry = build_registry(wiki)
    assert registry["entity_count"] == 1
    assert registry["entities"]["company-acme"] == {
        "type": "company",
        "name": "Acme",
        "handles": {"apollo_organization_id": "5f1a2b3c"},
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
