"""``storage.routes``: one rule sends fields and page types to a location (athenaeum#3166)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii
from athenaeum.config import write_default_config
from athenaeum.sensitivity_routing import SensitivityRoutingError, _vault_root_for_class
from athenaeum.storage import (
    StorageConfigError,
    configured_routes,
    page_write_root_for_class,
    resolve_adapter_for_class,
    route_for,
    routed_storage_mapping,
    surface_root_for_class,
)

UID = "person-alex"
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}


def _adapters(**extra: Any) -> dict[str, Any]:
    return {
        "vault": {
            "backing_store": "markdown",
            "surface_root": "vault",
            "corpus_policy": OFF,
            "sensitive": True,
        },
        "archive": {"backing_store": "markdown", "surface_root": "archive", "corpus_policy": OFF},
        "shared": {
            "backing_store": "markdown",
            "surface_root": "shared",
            "corpus_policy": {"embedded": True, "recallable": True, "merge_eligible": True},
        },
        **extra,
    }


def _cfg(routes: Any = None, **tables: Any) -> dict[str, Any]:
    storage: dict[str, Any] = {"adapters": _adapters(), **tables}
    if routes is not None:
        storage["routes"] = routes
    return {"storage": storage}


def _rule(**kw: Any) -> dict[str, Any]:
    return {"to": ["vault"], **kw}


# --- validation -----------------------------------------------------------


def test_unset_routes_are_empty_and_route_nothing() -> None:
    assert configured_routes(_cfg()) == ()
    assert route_for("person", "email", config=_cfg()).targets == ()
    assert route_for("person", config=_cfg()).rule is None


def test_field_rule_for_a_field_no_page_has_is_valid() -> None:
    cfg = _cfg([_rule(page_type="person", fields=["a_field_nobody_has"])])
    decision = route_for("person", "a_field_nobody_has", config=cfg)
    assert decision.targets == ("vault",) and decision.sensitive is True
    assert decision.rule == "storage.routes.person"


def test_field_rule_routes_its_fields_only_and_whole_rule_routes_the_type() -> None:
    cfg = _cfg(
        [
            _rule(page_type="person", fields=["email"]),
            {"page_type": "person", "to": ["shared"]},
        ]
    )
    assert route_for("person", "email", config=cfg).targets == ("vault",)
    assert route_for("person", "nickname", config=cfg).targets == ()
    whole = route_for("person", config=cfg)
    assert whole.targets == ("shared",) and whole.rule == "storage.routes.person"


@pytest.mark.parametrize(
    "routes",
    [
        [_rule(page_type="p", fields=["f"]), _rule(page_type="p", fields=["g", "f"])],
        [{"page_type": "p", "to": ["shared"]}, {"page_type": "p", "to": ["archive"]}],
        [_rule(sensitivity_class="c"), _rule(sensitivity_class="c")],
    ],
)
def test_duplicate_rules_refused(routes: list[dict[str, Any]]) -> None:
    with pytest.raises(StorageConfigError, match="more than once"):
        configured_routes(_cfg(routes))


def test_page_type_in_routes_and_mapping_refused() -> None:
    cfg = _cfg([{"page_type": "p", "to": ["shared"]}], mapping={"p": "archive"})
    with pytest.raises(StorageConfigError, match="both storage.routes and storage.mapping"):
        configured_routes(cfg)
    cfg = _cfg([_rule(page_type="p", fields=["f"])], mapping={"p": "archive"})
    with pytest.raises(StorageConfigError, match="both storage.routes"):
        configured_routes(cfg)


def test_field_rule_with_old_read_keys_refused() -> None:
    cfg = _cfg([_rule(page_type="p", fields=["f"])], excluded_read_mapping={"p": "x"})
    with pytest.raises(StorageConfigError, match="excluded_read_mapping"):
        configured_routes(cfg)
    cfg = _cfg([_rule(page_type="person", fields=["f"])], excluded_fields={"pii": ["f"]})
    with pytest.raises(StorageConfigError, match="excluded_fields"):
        configured_routes(cfg)


def test_unknown_target_refused() -> None:
    with pytest.raises(StorageConfigError, match="unknown location 'nope'"):
        configured_routes(_cfg([{"page_type": "p", "to": ["nope"]}]))


def test_field_rule_needs_a_sensitive_target() -> None:
    with pytest.raises(StorageConfigError, match="not marked sensitive"):
        configured_routes(_cfg([{"page_type": "p", "fields": ["f"], "to": ["archive"]}]))
    # ...but a whole-page-type rule may target any location.
    configured_routes(_cfg([{"page_type": "p", "to": ["archive"]}]))


@pytest.mark.parametrize("to", ["vault", None])
def test_to_must_be_a_list_of_locations(to: Any) -> None:
    with pytest.raises(StorageConfigError, match="to must"):
        configured_routes(_cfg([{"page_type": "p", "to": to}]))


@pytest.mark.parametrize(
    "rule",
    [
        "x",
        {"to": ["vault"]},
        {"page_type": "p", "sensitivity_class": "c", "to": ["vault"]},
        {"page_type": "p", "to": ["vault"], "bogus": 1},
        {"page_type": "p", "fields": [], "to": ["vault"]},
        {"sensitivity_class": "c", "fields": ["f"], "to": ["vault"]},
    ],
)
def test_malformed_rule_refused(rule: Any) -> None:
    with pytest.raises(StorageConfigError):
        configured_routes(_cfg([rule]))


def test_routes_must_be_a_list() -> None:
    with pytest.raises(StorageConfigError, match="must be a list"):
        configured_routes(_cfg({"page_type": "p"}))


# --- an indexed target behaves exactly as storage.mapping does -------------


def test_page_type_rule_to_indexed_location_matches_mapping(tmp_path: Path) -> None:
    old = _cfg(mapping={"recipe": "shared"})
    new = _cfg([{"page_type": "recipe", "to": ["shared"]}])
    for cfg in (old, new):
        adapter = resolve_adapter_for_class("recipe", cfg)
        assert adapter.name == "shared" and adapter.corpus_policy.in_corpus
        assert route_for("recipe", config=cfg).targets == ("shared",)
        assert page_write_root_for_class("recipe", cfg, tmp_path) == tmp_path / "shared"
    assert routed_storage_mapping(new) == {"recipe": "shared"}


# --- equivalence with the old keys ------------------------------------------


def _fixture_root(root: Path) -> Path:
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "alex.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\nBody.\n", encoding="utf-8"
    )
    (root / "vault").mkdir()
    (root / "vault" / f"{UID}.md").write_text(
        f"---\nuid: {UID}\nemails:\n  - alex@example.org\nnickname: Al\n"
        "phones:\n  - '+15550100'\n---\n",
        encoding="utf-8",
    )
    return root


OLD = _cfg(
    mapping={"ledger": "vault", "recipe": "shared"},
    excluded_fields={"ledger": ["emails", "nickname"]},
    excluded_read_mapping={"person": "ledger"},
)
NEW = _cfg(
    [
        _rule(page_type="person", fields=["emails", "nickname"]),
        {"page_type": "recipe", "to": ["shared"]},
        _rule(page_type="ledger"),
    ]
)


def test_routes_and_old_keys_give_identical_reads_and_writes(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    for field in (None, "emails", "nickname", "phones", "name", "never_seen"):
        for page_type in ("person", "recipe", "ledger", "note", None):
            if page_type == "ledger" and field is not None:
                continue  # OLD's surface-class name doubles as a page type; not a page type here
            a = route_for(page_type, field, config=OLD)
            b = route_for(page_type, field, config=NEW)
            assert (a.targets, a.sensitive) == (b.targets, b.sensitive), (page_type, field)
    for cls in ("person", "recipe", "note"):
        assert page_write_root_for_class(cls, OLD, root) == page_write_root_for_class(
            cls, NEW, root
        )
    assert surface_root_for_class("ledger", OLD, root) == surface_root_for_class(
        "ledger", NEW, root
    )
    reads = []
    for cfg in (OLD, NEW):
        surface = pii.surface_class_for_page_class("person", cfg)
        assert surface_root_for_class(surface, cfg, root) == root / "vault"
        read = pii.read_entity(root, cfg, UID, surface_class=surface, include_excluded=True)
        assert read is not None
        reads.append(repr(dataclasses.asdict(read)))
        withheld = pii.read_entity(root, cfg, UID, surface_class=surface, include_excluded=False)
        assert withheld is not None
        reads.append(repr(dataclasses.asdict(withheld)))
    assert reads[0] == reads[2] and reads[1] == reads[3]
    assert "alex@example.org" in reads[0] and "alex@example.org" not in reads[1]
    # Records already on disk were read in place and not moved.
    assert (root / "vault" / f"{UID}.md").exists()


# --- sensitivity_class rules ---------------------------------------------------


def test_sensitivity_class_rule_replaces_the_per_class_lookup(tmp_path: Path) -> None:
    cfg = _cfg([_rule(sensitivity_class="secret")])
    expected = tmp_path / "vault" / "sensitivity" / "secret"
    assert _vault_root_for_class("secret", cfg, tmp_path) == expected
    # No rule: refused (athenaeum#3168), no longer the built-in excluded location.
    with pytest.raises(SensitivityRoutingError, match="no storage.routes rule"):
        _vault_root_for_class("other", cfg, tmp_path)
    # An in-corpus target still fails closed.
    bad = _cfg([{"sensitivity_class": "secret", "to": ["shared"]}])
    with pytest.raises(SensitivityRoutingError):
        _vault_root_for_class("secret", bad, tmp_path)


# --- init -------------------------------------------------------------------------


def test_init_writes_a_commented_example_and_no_active_rule(tmp_path: Path) -> None:
    import yaml

    path = write_default_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "routes:" in text
    loaded = yaml.safe_load(text) or {}
    assert "routes" not in (loaded.get("storage") or {})


def test_lint_policy_pair_sees_a_sensitivity_class_rule() -> None:
    from athenaeum.sensitivity_lint import lint_read_policy_adapter_pairs

    classes = {"secret": {"read_policy": {"access": "confidential"}}}
    for target, expect in (("vault", 0), ("shared", 1)):
        cfg = _cfg([{"sensitivity_class": "secret", "to": [target]}])
        cfg["sensitivity"] = {"classes": classes}
        found = [f for f in lint_read_policy_adapter_pairs(cfg) if f.class_name == "secret"]
        assert len(found) == expect
