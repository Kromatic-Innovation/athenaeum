"""``pii.record_placement``: where a record about an entity is kept (athenaeum#3177)."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, storage
from athenaeum.pii import RecordPlacement, record_placement

UID = "person-alex"
OTHER = "person-blair"
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
EMAIL = "alex@example.org"
OTHER_EMAIL = "blair@example.org"


def _cfg(routes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    storage_cfg: dict[str, Any] = {
        "adapters": {
            "vault": {
                "backing_store": "markdown",
                "surface_root": "vault",
                "corpus_policy": OFF,
                "sensitive": True,
            },
            "vault2": {
                "backing_store": "markdown",
                "surface_root": "vault2",
                "corpus_policy": OFF,
                "sensitive": True,
            },
        }
    }
    if routes is not None:
        storage_cfg["routes"] = routes
    return {"storage": storage_cfg}


def _page(root: Path, uid: str, name: str, page_type: str = "person") -> None:
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / f"{uid}.md").write_text(
        f"---\nuid: {uid}\ntype: {page_type}\nname: {name}\ntitle: Engineer\n---\nBody.\n",
        encoding="utf-8",
    )


def _record(root: Path, uid: str, email: str) -> None:
    (root / "vault").mkdir(parents=True, exist_ok=True)
    (root / "vault" / f"{uid}.md").write_text(
        f"---\nuid: {uid}\nemails:\n  - {email}\n---\n", encoding="utf-8"
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _page(tmp_path, UID, "Alex Doe")
    _page(tmp_path, OTHER, "Blair Roe")
    _record(tmp_path, UID, EMAIL)
    _record(tmp_path, OTHER, OTHER_EMAIL)
    return tmp_path


EMAILS_TO_VAULT = [{"page_type": "person", "fields": ["emails"], "to": ["vault"]}]


def test_the_two_standard_cases(root: Path) -> None:
    routed = _cfg(EMAILS_TO_VAULT)
    got = record_placement(root, routed, UID, field="emails")
    assert got == RecordPlacement("vault", ("vault",), "field-route")
    assert record_placement(root, routed, UID, field="title").kind == "ordinary"
    for field in ("emails", "title"):
        assert record_placement(root, _cfg(), UID, field=field).kind == "ordinary"


def test_a_field_rule_with_no_target_is_nowhere(root: Path) -> None:
    cfg = _cfg([{"page_type": "person", "fields": ["emails"], "to": []}])
    got = record_placement(root, cfg, UID, field="emails")
    assert got.kind == "nowhere" and got.targets == ()
    assert record_placement(root, cfg, UID, field="title").kind == "ordinary"


def test_a_whole_page_type_route(root: Path) -> None:
    vault = _cfg([{"page_type": "person", "to": ["vault", "vault2"]}])
    got = record_placement(root, vault, UID, field="title")
    assert (got.kind, got.targets) == ("vault", ("vault", "vault2"))
    assert record_placement(root, vault, UID).kind == "vault"
    gone = _cfg([{"page_type": "person", "to": []}])
    assert record_placement(root, gone, UID, field="title").kind == "nowhere"


def test_text_quoting_a_routed_value_follows_that_fields_route(root: Path) -> None:
    cfg = _cfg(EMAILS_TO_VAULT)
    got = record_placement(root, cfg, UID, texts=[f"asked to change to {EMAIL.upper()}"])
    assert (got.kind, got.targets, got.reason) == ("vault", ("vault",), "text-route")
    assert record_placement(root, cfg, UID, texts=["nothing quoted here"]).kind == "ordinary"


def test_text_check_never_reads_a_vault_directly(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = pii.read_entity

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("read_entity")
        assert kwargs["include_excluded"] is True
        return real(*args, **kwargs)

    monkeypatch.setattr(pii, "read_entity", spy)
    record_placement(root, _cfg(EMAILS_TO_VAULT), UID, texts=[EMAIL])
    assert seen == ["read_entity"]


def test_limit_another_entitys_routed_value_is_not_detected(root: Path) -> None:
    got = record_placement(root, _cfg(EMAILS_TO_VAULT), UID, texts=[f"mentions {OTHER_EMAIL}"])
    assert got.kind == "ordinary"


def test_limit_a_discarded_value_is_not_on_file(root: Path) -> None:
    (root / "vault" / f"{UID}.md").unlink()
    got = record_placement(root, _cfg(EMAILS_TO_VAULT), UID, texts=[f"mentions {EMAIL}"])
    assert got.kind == "ordinary"


def test_sensitive_goes_to_the_primary_vault_or_nowhere(root: Path) -> None:
    cfg = _cfg([{"page_type": "person", "fields": ["emails"], "to": ["vault2", "vault"]}])
    got = record_placement(root, cfg, UID, sensitive=True)
    assert (got.kind, got.targets) == ("vault", ("vault2",))
    assert record_placement(root, _cfg(), UID, sensitive=True).kind == "nowhere"
    # A record that is already routed keeps its own route.
    both = record_placement(root, _cfg(EMAILS_TO_VAULT), UID, field="emails", sensitive=True)
    assert both.targets == ("vault",)


@pytest.mark.parametrize("kwargs", [{"field": "emails"}, {"texts": ["x"]}, {"sensitive": True}])
@pytest.mark.parametrize("subject", [None, "", "no-such-uid"])
def test_a_subject_that_is_not_one_entity_is_unplaceable(
    root: Path, subject: str | None, kwargs: dict[str, Any]
) -> None:
    got = record_placement(root, _cfg(EMAILS_TO_VAULT), subject, **kwargs)
    assert got.kind == "unplaceable" and got.targets == ()


def test_two_pages_with_one_uid_are_unplaceable(root: Path) -> None:
    (root / "wiki" / "dup.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Twin\n---\n", encoding="utf-8"
    )
    got = record_placement(root, _cfg(EMAILS_TO_VAULT), UID, field="title")
    # Either the index refuses the duplicate or only one page wins; never a guess at a vault.
    assert got.kind in ("unplaceable", "ordinary")


def test_no_subject_field_or_text_is_ordinary(root: Path) -> None:
    assert record_placement(root, _cfg(EMAILS_TO_VAULT), None).kind == "ordinary"
    assert record_placement(root, _cfg(EMAILS_TO_VAULT), "").kind == "ordinary"


def test_refuse_write_gives_nowhere(root: Path) -> None:
    for kwargs in ({"field": "emails"}, {"field": "title"}, {}):
        got = record_placement(
            root, _cfg(EMAILS_TO_VAULT), UID, retention_action="refuse-write", **kwargs
        )
        assert got.kind == "nowhere"


def test_store_off_corpus_keeps_targets_outside_git(root: Path) -> None:
    cfg = _cfg([{"page_type": "person", "fields": ["emails"], "to": ["vault", "vault2"]}])
    got = record_placement(root, cfg, UID, field="emails", retention_action="store-off-corpus")
    assert got.targets == ("vault", "vault2")
    (root / "vault").mkdir(exist_ok=True)
    (root / "vault" / ".git").mkdir()
    got = record_placement(root, cfg, UID, field="emails", retention_action="store-off-corpus")
    assert (got.kind, got.targets) == ("vault", ("vault2",))
    (root / "vault2").mkdir(exist_ok=True)
    (root / "vault2" / ".git").mkdir()
    got = record_placement(root, cfg, UID, field="emails", retention_action="store-off-corpus")
    assert got.kind == "nowhere"


def test_retention_never_loosens(root: Path) -> None:
    gone = _cfg([{"page_type": "person", "fields": ["emails"], "to": []}])
    assert (
        record_placement(root, gone, UID, field="emails", retention_action="store-off-corpus").kind
        == "nowhere"
    )
    assert (
        record_placement(root, _cfg(), UID, field="title", retention_action="store-off-corpus").kind
        == "nowhere"
    )
    assert (
        record_placement(root, _cfg(), UID, field="title", retention_action="demote-cold").kind
        == "ordinary"
    )
    assert (
        record_placement(root, _cfg(), "nope", field="title", retention_action="refuse-write").kind
        == "unplaceable"
    )


def test_docstring_states_the_limits_and_the_no_fallback_rule() -> None:
    doc = inspect.getdoc(record_placement) or ""
    assert "DIFFERENT entity" in doc
    assert "discarded" in doc
    assert "must not substitute" in doc


def test_routing_comes_only_from_route_for(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake(page_type: Any, field: Any = None, **_: Any) -> storage.RouteDecision:
        calls.append((page_type, field))
        if field == "emails":
            return storage.RouteDecision(("vault2",), "fake", True)
        return storage.RouteDecision((), None, False)

    monkeypatch.setattr(pii, "route_for", fake)
    # The real configuration routes nothing, so any vault answer came from route_for.
    got = record_placement(root, _cfg(), UID, field="emails")
    assert (got.kind, got.targets) == ("vault", ("vault2",))
    assert ("person", "emails") in calls


def test_the_helper_names_no_routing_key() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(record_placement)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) else fn.body  # skip docstring
    literals = {
        node.value
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    keys = {"storage", "routes", "mapping", "excluded_fields", "excluded_read_mapping", "adapters"}
    assert not literals & keys
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for stmt in body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
    }
    assert not called & {
        "configured_routes",
        "configured_excluded_fields",
        "configured_excluded_read_mapping",
        "resolve_storage_mapping",
    }
