"""Sensitive storage locations -- the vault flag (athenaeum#3161)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum import doctor
from athenaeum.pii import assemble_excluded_read
from athenaeum.storage import (
    WIKI_MARKDOWN_EMBEDDED,
    CorpusPolicy,
    StorageAdapter,
    StorageConfigError,
    available_adapters,
    is_sensitive,
    register_adapter,
    route_for,
)


def _cfg(**entry: Any) -> dict[str, Any]:
    body = {
        "backing_store": "markdown",
        "surface_root": "vault",
        "corpus_policy": {"embedded": False, "recallable": False, "merge_eligible": False},
        **entry,
    }
    return {
        "storage": {
            "adapters": {"vault": body},
            "mapping": {"pii": "vault"},
            "excluded_fields": {"pii": ["emails", "phones", "former_emails", "alt_emails"]},
        }
    }


def test_default_false_and_carried() -> None:
    assert available_adapters(_cfg())["vault"].sensitive is False
    assert available_adapters(_cfg(sensitive=True))["vault"].sensitive is True


@pytest.mark.parametrize("bad", ["yes", 1, "true", []])
def test_non_boolean_refused(bad: object) -> None:
    with pytest.raises(StorageConfigError, match="sensitive must be a boolean"):
        available_adapters(_cfg(sensitive=bad))


def test_builtin_excluded_sensitive_wiki_not() -> None:
    assert is_sensitive("excluded", None) is True
    assert is_sensitive("wiki-markdown-embedded", None) is False
    assert WIKI_MARKDOWN_EMBEDDED.sensitive is False


def test_wiki_cannot_be_made_sensitive() -> None:
    cfg = {
        "storage": {
            "adapters": {
                "wiki-markdown-embedded": {
                    "backing_store": "markdown",
                    "surface_root": "wiki",
                    "sensitive": True,
                }
            }
        }
    }
    with pytest.raises(StorageConfigError):
        available_adapters(cfg)
    with pytest.raises(StorageConfigError):
        register_adapter(
            StorageAdapter(
                name="wiki-markdown-embedded",
                backing_store="markdown",
                surface_root="wiki",
                corpus_policy=CorpusPolicy.none(),
                sensitive=True,
            ),
            replace=True,
        )


@pytest.mark.parametrize("cap", ["embedded", "recallable", "merge_eligible"])
def test_sensitive_with_corpus_capability_refused(cap: str) -> None:
    cfg = _cfg(sensitive=True)
    cfg["storage"]["adapters"]["vault"]["corpus_policy"][cap] = True
    with pytest.raises(StorageConfigError, match="never in the main index"):
        available_adapters(cfg)


def test_register_sensitive_with_capability_refused() -> None:
    with pytest.raises(StorageConfigError):
        register_adapter(
            StorageAdapter(
                name="bad-vault",
                backing_store="markdown",
                surface_root="v",
                corpus_policy=CorpusPolicy.all(),
                sensitive=True,
            )
        )


def test_is_sensitive_unknown_location() -> None:
    with pytest.raises(StorageConfigError, match="unknown storage location"):
        is_sensitive("nope", None)


def test_route_decision_uses_is_sensitive() -> None:
    assert route_for("pii", config=_cfg(sensitive=True)).sensitive is True
    # Outside the corpus but unmarked (an archive): not sensitive.
    assert route_for("pii", config=_cfg()).sensitive is False
    declared = {
        "storage": {
            "mapping": {"pii": "excluded"},
            "excluded_fields": {"pii": ["emails", "phones", "former_emails", "alt_emails"]},
        }
    }
    assert route_for("pii", config={"storage": {"mapping": {"pii": "excluded"}}}).sensitive
    assert route_for("pii", "emails", config=declared).sensitive


@pytest.mark.parametrize("marked", [False, True])
def test_read_of_custom_outside_corpus_location_redacts_unless_flag(
    tmp_path: Path, marked: bool
) -> None:
    cfg = _cfg(sensitive=marked)
    page = tmp_path / "wiki" / "alex.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nuid: alex\nname: Alex\n---\n", encoding="utf-8")
    meta = {"uid": "alex", "emails": ["alex@example.org"]}
    fields, redactions, _ = assemble_excluded_read(
        page, {"uid": "alex"}, meta, config=cfg, surface_class="pii", include_excluded=False
    )
    assert fields == {} and [(m.field, m.value_count) for m in redactions] == [("emails", 1)]
    fields, redactions, _ = assemble_excluded_read(
        page, {"uid": "alex"}, meta, config=cfg, surface_class="pii", include_excluded=True
    )
    assert fields == {"emails": ["alex@example.org"]} and redactions == ()


def test_doctor_names_unmarked_locations_only() -> None:
    row = doctor.check_unmarked_vault_locations(_cfg())
    assert row.status == "warn" and "vault" in row.detail
    assert doctor.check_unmarked_vault_locations(_cfg(sensitive=True)).status == "ok"
    assert doctor.check_unmarked_vault_locations(
        {"storage": {"mapping": {"pii": "excluded"}}}
    ).status == "ok"
    assert doctor.check_unmarked_vault_locations({}).status == "ok"
