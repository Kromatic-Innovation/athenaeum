"""storage.route_for -- one resolver for where a field / page type is stored (athenaeum#3158)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, storage
from athenaeum.storage import RouteDecision, StorageConfigError, route_for

PII_EXCLUDED: dict[str, Any] = {"storage": {"mapping": {"pii": "excluded"}}}


def _reference_field_targets(
    page_type: str, field: str, config: dict[str, Any] | None, record_meta: Any = None
) -> tuple[str, ...]:
    """The pre-resolver computation, spelled out from its three inputs."""
    surface_class = pii.surface_class_for_page_class(page_type, config)
    if not surface_class or not storage.is_excluded(surface_class, config):
        return ()
    if field not in pii.resolve_excluded_fields(surface_class, config, record_meta):
        return ()
    return (storage.resolve_adapter_for_class(surface_class, config).name,)


FIELD_CASES: list[tuple[str, dict[str, Any] | None, str, str, tuple[str, ...], str | None]] = [
    # id, config, page_type, field, targets, rule
    ("nothing-configured", None, "person", "emails", (), None),
    ("nothing-configured-empty", {}, "person", "emails", (), None),
    ("mapping-only-listed", PII_EXCLUDED, "person", "emails", ("excluded",), "storage.mapping.pii"),
    ("mapping-only-unlisted", PII_EXCLUDED, "person", "title", (), None),
    (
        "explicit-list-replaces-builtin",
        {"storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {"pii": ["nickname"]}}},
        "person",
        "emails",
        (),
        None,
    ),
    (
        "explicit-list-names-field",
        {"storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {"pii": ["nickname"]}}},
        "person",
        "nickname",
        ("excluded",),
        "storage.excluded_fields.pii",
    ),
    (
        "read-mapping-override",
        {
            "storage": {
                "mapping": {"supplier-pii": "excluded"},
                "excluded_read_mapping": {"vendor": "supplier-pii"},
                "excluded_fields": {"supplier-pii": ["iban"]},
            }
        },
        "vendor",
        "iban",
        ("excluded",),
        "storage.excluded_fields.supplier-pii",
    ),
    (
        "read-mapping-person-to-identity",
        {
            "storage": {
                "mapping": {"pii": "excluded"},
                "excluded_read_mapping": {"person": "person"},
            }
        },
        "person",
        "emails",
        (),
        None,
    ),
    (
        "class-with-empty-field-list",
        {"storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {"pii": []}}},
        "person",
        "emails",
        (),
        None,
    ),
    (
        "class-with-no-field-list-uses-record",
        {"storage": {"mapping": {"vault": "excluded"}}},
        "vault",
        "secret",
        ("excluded",),
        "storage.mapping.vault",
    ),
    ("page-class-not-excluded", PII_EXCLUDED, "company", "emails", (), None),
]


@pytest.mark.parametrize(
    ("config", "page_type", "field", "targets", "rule"),
    [c[1:] for c in FIELD_CASES],
    ids=[c[0] for c in FIELD_CASES],
)
def test_field_route_matches_existing_computation(
    config: dict[str, Any] | None,
    page_type: str,
    field: str,
    targets: tuple[str, ...],
    rule: str | None,
) -> None:
    record_meta = {"uid": "u1", "secret": "x", "emails": ["a@example.com"]}
    decision = route_for(page_type, field, config=config, record_meta=record_meta)
    assert decision == RouteDecision(targets, rule, bool(targets))
    assert decision.targets == _reference_field_targets(page_type, field, config, record_meta)


def test_unlisted_class_without_record_meta_keeps_field_on_page() -> None:
    cfg = {"storage": {"mapping": {"vault": "excluded"}}}
    assert route_for("vault", "secret", config=cfg).targets == ()


def test_route_decision_is_frozen() -> None:
    decision = route_for("person", "emails", config=PII_EXCLUDED, record_meta=None)
    with pytest.raises(AttributeError):
        decision.rule = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "config",
    [
        None,
        PII_EXCLUDED,
        {"storage": {"mapping": {"recipe": "wiki-markdown-embedded", "pii": "excluded"}}},
    ],
)
@pytest.mark.parametrize("page_type", ["person", "pii", "recipe", "unmapped", "  pii  "])
def test_whole_type_route_matches_resolve_adapter_for_class(
    config: dict[str, Any] | None, page_type: str
) -> None:
    decision = route_for(page_type, config=config)
    adapter = storage.resolve_adapter_for_class(page_type, config)
    assert decision.targets == (adapter.name,)
    assert decision.sensitive is (not adapter.corpus_policy.in_corpus)
    mapped = page_type.strip() in ((config or {}).get("storage", {}).get("mapping", {}))
    assert (decision.rule is not None) is mapped


def test_whole_type_unknown_location_raises() -> None:
    with pytest.raises(StorageConfigError):
        route_for("pii", config={"storage": {"mapping": {"pii": "no-such-adapter"}}})


def test_field_route_unknown_location_raises() -> None:
    cfg = {"storage": {"mapping": {"pii": "no-such-adapter"}}}
    with pytest.raises(StorageConfigError):
        route_for("person", "emails", config=cfg)


def test_no_page_type_and_no_surface_class_keeps_the_field_on_the_page() -> None:
    # athenaeum#3162: the corrections-only table is gone; with nothing to route
    # by, a field stays on the page.
    cfg = {"storage": {"mapping": {"pii": "excluded"}}}
    assert route_for(None, "phone", config=cfg) == RouteDecision((), None, False)


def test_surface_class_form_ignores_the_exclusion_gate() -> None:
    # The read path's form: a surface class that is NOT excluded still answers
    # for its field list (callers gate; read_entity does not).
    cfg = {"storage": {"excluded_fields": {"ledger": ["iban"]}}}
    on = route_for(None, "iban", config=cfg, surface_class="ledger")
    assert on.targets == ("wiki-markdown-embedded",) and on.sensitive is False
    assert on.rule == "storage.excluded_fields.ledger"
    assert route_for(None, "other", config=cfg, surface_class="ledger").targets == ()


def test_assemble_excluded_read_consults_route_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolve both modules from ``sys.modules`` at call time: other test files
    # reload ``athenaeum.*`` modules, and the join's deferred import reads the
    # CURRENT ``athenaeum.storage``, so a spy on a stale module object would be
    # bypassed (an order-dependent failure under xdist).
    storage_mod = importlib.import_module("athenaeum.storage")
    pii_mod = importlib.import_module("athenaeum.pii")
    calls: list[tuple[str | None, str | None, str | None]] = []
    real = storage_mod.route_for

    def spy(page_type, field=None, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((page_type, field, kwargs.get("surface_class")))
        return real(page_type, field, **kwargs)

    monkeypatch.setattr(storage_mod, "route_for", spy)
    fields, _, _ = pii_mod.assemble_excluded_read(
        Path("page.md"),
        {},
        {"uid": "u", "emails": ["a@example.com"], "nickname": "x"},
        surface_class="pii",
        config=PII_EXCLUDED,
        include_excluded=True,
    )
    assert fields == {"emails": ["a@example.com"]}
    assert (None, "emails", "pii") in calls and (None, "nickname", "pii") not in calls
