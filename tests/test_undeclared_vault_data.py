# SPDX-License-Identifier: Apache-2.0
"""Removing the built-in private field list, and the guard for installations that relied on it.

athenaeum#3170. Nothing is private unless the operator declares it: a class
mapped to a location outside the corpus no longer gets the four contact fields
(``pii``) or every field on its records (any other class) for free. An
installation that relied on either is refused before it writes, a sensitive
read raises a typed error, and the refusal prints the ``storage.routes``
entries that reproduce the previous behaviour.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import pii
from athenaeum.cli import main
from athenaeum.doctor import check_undeclared_vault_data
from athenaeum.mcp_server import UNDECLARED_VAULT_NOTICE_KEY, _with_undeclared_vault_notice
from athenaeum.schemas import validate_wiki_meta
from athenaeum.storage import (
    UndeclaredMapping,
    require_declared_for_sensitive_read,
    route_for,
    undeclared_vault_mappings,
)
from athenaeum.storage_status import build_status
from athenaeum.vault_declaration import (
    previous_field_list,
    refusal_message,
    require_declared_vault_data,
    scan_undeclared_fields,
    suggested_routes,
    suggested_routes_text,
)

OLD_PII_CONFIG: dict[str, Any] = {"storage": {"mapping": {"pii": "excluded"}}}

VENDOR_ADAPTER = {
    "backing_store": "markdown",
    "surface_root": "vendor-vault",
    "sensitive": True,
    "corpus_policy": {"embedded": False, "recallable": False, "merge_eligible": False},
}

PERSON_PAGE = "---\nuid: person-ann\nname: Ann Example\ntype: person\n---\n\nNotes about Ann.\n"
PII_RECORD = (
    "---\nuid: person-ann\npii: true\nname: Ann Example\nemails:\n- ann@example.org\n"
    "- ann.alt@example.org\nphones:\n- +1-555-0100\nformer_emails:\n- old@example.org\n"
    "notes_field: kept\n---\n\nArchival data.\n"
)

#: What the previous release (develop before athenaeum#3170) returned for the
#: fixture below under ``storage.mapping: {pii: excluded}``, captured by running
#: that release's code. The pasted suggestion must reproduce it exactly.
PREVIOUS_RELEASE_READ = {
    True: (
        [
            ("emails", ["ann@example.org", "ann.alt@example.org"]),
            ("phones", ["+1-555-0100"]),
            ("former_emails", ["old@example.org"]),
        ],
        [],
    ),
    False: ([], [("emails", 2), ("phones", 1), ("former_emails", 1)]),
}
PREVIOUS_RELEASE_ROUTES = {
    "emails": ("excluded",),
    "phones": ("excluded",),
    "former_emails": ("excluded",),
    "alt_emails": ("excluded",),
    "name": (),
    "notes_field": (),
}


def _undeclared_error() -> type[Exception]:
    """The error class as the live ``athenaeum.storage`` defines it.

    Resolved at call time: another test module reloads ``athenaeum.storage``,
    which would leave a class imported at the top of this file stale.
    """
    return importlib.import_module("athenaeum.storage").UndeclaredVaultDataError


def _write_config(root: Path, config: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _fixture(root: Path, config: dict[str, Any] = OLD_PII_CONFIG) -> Path:
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "person-ann.md").write_text(PERSON_PAGE, encoding="utf-8")
    (root / "excluded").mkdir(parents=True, exist_ok=True)
    (root / "excluded" / "ann.md").write_text(PII_RECORD, encoding="utf-8")
    _write_config(root, config)
    return root


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _pasted(config: dict[str, Any], text: str) -> dict[str, Any]:
    """Paste the printed YAML into *config* the way an operator would: append the routes."""
    printed = yaml.safe_load(text)
    merged = json.loads(json.dumps(config))
    storage = merged.setdefault("storage", {})
    storage["routes"] = list(storage.get("routes") or []) + printed["storage"]["routes"]
    return merged


def _read(root: Path, config: dict[str, Any], include: bool) -> tuple[list, list]:
    surface = pii.surface_class_for_page_class("person", config)
    read = pii.read_entity(
        root, config, "person-ann", surface_class=surface, include_excluded=include
    )
    assert read is not None
    return (
        [(name, list(values)) for name, values in read.contact.items()],
        [(m.field, m.value_count) for m in read.redactions],
    )


class TestNoBuiltInList:
    def test_pii_class_without_a_declaration_has_no_fields(self) -> None:
        assert (
            pii.resolve_excluded_fields("pii", OLD_PII_CONFIG, {"emails": ["a@example.org"]}) == ()
        )

    def test_unlisted_class_no_longer_takes_every_field(self) -> None:
        assert pii.resolve_excluded_fields("vendor", None, {"account": ["x"], "uid": "v"}) == ()

    def test_only_declared_fields_are_routed(self) -> None:
        config = {
            "storage": {
                "mapping": {"pii": "excluded"},
                "routes": [{"page_type": "person", "fields": ["phones"], "to": ["excluded"]}],
            }
        }
        surface = pii.surface_class_for_page_class("person", config)
        assert pii.resolve_excluded_fields(surface, config, None) == ("phones",)
        assert route_for("person", "emails", config=config).targets == ()
        assert route_for("person", "phones", config=config).targets == ("excluded",)


class TestConfigurationHalf:
    def test_mapping_with_no_rule_is_undeclared(self) -> None:
        assert undeclared_vault_mappings(OLD_PII_CONFIG) == (UndeclaredMapping("pii", "excluded"),)

    def test_route_naming_the_location_declares_it(self) -> None:
        config = {
            "storage": {
                "mapping": {"pii": "excluded"},
                "routes": [{"page_type": "person", "fields": ["emails"], "to": ["excluded"]}],
            }
        }
        assert undeclared_vault_mappings(config) == ()

    @pytest.mark.parametrize(
        "config",
        [None, {}, {"storage": {}}, {"storage": {"mapping": {"pii": "wiki-markdown-embedded"}}}],
    )
    def test_no_location_outside_the_corpus_is_unaffected(self, config: Any) -> None:
        assert undeclared_vault_mappings(config) == ()
        require_declared_for_sensitive_read(config)

    def test_needs_no_file_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the configuration half read a file")

        monkeypatch.setattr(Path, "read_text", boom)
        monkeypatch.setattr(Path, "rglob", boom)
        assert undeclared_vault_mappings(OLD_PII_CONFIG)


class TestSensitiveReadRaises:
    def test_read_entity_with_sensitive_values_raises(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        with pytest.raises(_undeclared_error()) as info:
            pii.read_entity(
                root, OLD_PII_CONFIG, "person-ann", surface_class="pii", include_excluded=True
            )
        assert [(m.surface_class, m.location) for m in info.value.mappings] == [
            ("pii", "excluded")
        ]
        assert "--suggest-routes" in str(info.value)

    def test_read_entities_and_assemble_raise(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        with pytest.raises(_undeclared_error()):
            list(
                pii.read_entities(
                    root, OLD_PII_CONFIG, ["person-ann"], surface_class="pii", include_excluded=True
                )
            )
        with pytest.raises(_undeclared_error()):
            pii.assemble_excluded_read(
                root / "wiki" / "person-ann.md",
                {"uid": "person-ann"},
                {"uid": "person-ann", "emails": ["ann@example.org"]},
                surface_class="pii",
                config=OLD_PII_CONFIG,
                include_excluded=True,
            )

    def test_redacted_read_reports_no_field_it_cannot_account_for(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        read = pii.read_entity(root, OLD_PII_CONFIG, "person-ann", surface_class="pii")
        assert read is not None
        assert read.redactions == ()
        assert read.contact == {}

    def test_cli_prints_the_typed_error_as_a_refusal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _fixture(tmp_path)
        code = main(
            [
                "entity",
                "--uid",
                "person-ann",
                "--class",
                "person",
                "--path",
                str(root),
                "--include-sensitive",
            ]
        )
        assert code == 2
        assert "undeclared vault data" in capsys.readouterr().err


#: Each writing command named by the issue, as argv after the knowledge root.
WRITING_COMMANDS = [
    pytest.param(lambda r: ["run", "--knowledge-root", r], id="librarian-run"),
    pytest.param(lambda r: ["ingest-answers", "--path", r], id="ingest-answers"),
    pytest.param(
        lambda r: ["repair", "--bounce-fold", "--apply", "--knowledge-root", r], id="bounce-fold"
    ),
    pytest.param(
        lambda r: ["repair", "--contact-records", "--apply", "--knowledge-root", r],
        id="contact-records",
    ),
    pytest.param(
        lambda r: ["storage", "migrate-pii", "--all", "--apply", "--path", r],
        id="migrate-pii-bulk",
    ),
    pytest.param(
        lambda r: [
            "storage",
            "migrate-pii",
            "--page",
            str(Path(r) / "wiki" / "person-ann.md"),
            "--apply",
            "--path",
            r,
        ],
        id="migrate-pii-single",
    ),
]


class TestWritersRefuse:
    @pytest.mark.parametrize("argv", WRITING_COMMANDS)
    def test_refuses_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: Any
    ) -> None:
        root = _fixture(tmp_path / "kb")
        before = _snapshot(root)
        code = main(argv(str(root)))
        err = capsys.readouterr().err
        assert code != 0
        assert "Undeclared vault data" in err
        assert "page_type: person" in err
        assert _snapshot(root) == before

    def test_librarian_run_refuses_at_load(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.librarian import run

        root = _fixture(tmp_path / "kb")
        before = _snapshot(root)
        assert run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root) == 1
        assert _snapshot(root) == before
        assert "fields: [emails, phones, former_emails, alt_emails]" in capsys.readouterr().err

    def test_declared_installation_is_not_refused(self, tmp_path: Path) -> None:
        config = {
            "storage": {
                "mapping": {"pii": "excluded"},
                "routes": [{"page_type": "person", "fields": ["emails"], "to": ["excluded"]}],
            }
        }
        require_declared_vault_data(config, tmp_path)


class TestRefusalMessage:
    def test_prints_the_contact_fields_for_pii(self, tmp_path: Path) -> None:
        message = refusal_message(OLD_PII_CONFIG, tmp_path)
        assert "storage.mapping.pii -> excluded" in message
        assert suggested_routes_text(suggested_routes(OLD_PII_CONFIG, tmp_path)) in message
        assert "fields: [emails, phones, former_emails, alt_emails]" in message

    def test_says_how_to_choose_otherwise(self, tmp_path: Path) -> None:
        message = refusal_message(OLD_PII_CONFIG, tmp_path)
        assert "delete a field from a printed rule to keep it on the page" in message
        assert "to: []" in message
        assert "does not edit athenaeum.yaml" in message

    def test_other_class_takes_the_field_names_on_its_records(self, tmp_path: Path) -> None:
        config = {
            "storage": {
                "mapping": {"vendor": "vendor-vault"},
                "adapters": {"vendor-vault": VENDOR_ADAPTER},
            }
        }
        vault = tmp_path / "vendor-vault"
        vault.mkdir()
        (vault / "a.md").write_text(
            "---\nuid: v1\ntype: vendor\naccount: [A-1]\niban: X\n---\n", encoding="utf-8"
        )
        (vault / "b.md").write_text("---\nuid: v2\nsort_code: '00'\n---\n", encoding="utf-8")
        assert previous_field_list("vendor", config, tmp_path) == ("account", "iban", "sort_code")
        routes = suggested_routes(config, tmp_path)
        # A whole-page rule keeps the pages where they were; the field rule keeps
        # the read of the vault's record fields.
        assert [(r.page_type, r.fields, r.location, r.replaces) for r in routes] == [
            ("vendor", None, "vendor-vault", ("storage.mapping.vendor",)),
            (
                "vendor",
                ("account", "iban", "sort_code"),
                "vendor-vault",
                ("storage.mapping.vendor",),
            ),
        ]
        assert "remove these keys, which the rules replace: storage.mapping.vendor" in (
            refusal_message(config, tmp_path)
        )
        # The printed rules are a valid configuration once the mapping is removed.
        pasted = _pasted(config, suggested_routes_text(routes))
        del pasted["storage"]["mapping"]["vendor"]
        assert undeclared_vault_mappings(pasted) == ()
        assert pii.resolve_excluded_fields(
            pii.surface_class_for_page_class("vendor", pasted), pasted, None
        ) == ("account", "iban", "sort_code")
        assert route_for("vendor", config=pasted).targets == ("vendor-vault",)

    def test_whole_page_mapping_inside_the_wiki_gets_a_whole_page_rule(
        self, tmp_path: Path
    ) -> None:
        unembedded = dict(VENDOR_ADAPTER, surface_root="wiki", sensitive=False)
        config = {
            "storage": {"mapping": {"note": "notes-off"}, "adapters": {"notes-off": unembedded}}
        }
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "n.md").write_text(
            "---\nuid: n\ntype: note\nname: N\n---\n", encoding="utf-8"
        )
        (route,) = suggested_routes(config, tmp_path)
        assert (route.page_type, route.fields, route.location) == ("note", None, "notes-off")
        # Pages in the wiki tree are pages, not records: the scan leaves them alone.
        assert scan_undeclared_fields(config, tmp_path) == ()


class TestPastingReproducesThePreviousRelease:
    def test_reads_and_writes_are_byte_identical(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        pasted = _pasted(
            OLD_PII_CONFIG, suggested_routes_text(suggested_routes(OLD_PII_CONFIG, root))
        )
        assert undeclared_vault_mappings(pasted) == ()
        for include in (True, False):
            assert _read(root, pasted, include) == PREVIOUS_RELEASE_READ[include]
        for field, targets in PREVIOUS_RELEASE_ROUTES.items():
            assert route_for("person", field, config=pasted).targets == targets
        # Writers resolve the same record location as before.
        assert pii.contacts_surface_root(root, pasted) == root / "excluded"
        assert (
            pii.excluded_surface_root(
                pii.surface_class_for_page_class("person", pasted), root, pasted
            )
            == root / "excluded"
        )

    def test_a_bounce_mark_writes_the_same_bytes(self, tmp_path: Path) -> None:
        """A contact write after pasting lands the same bytes as one under the old config."""
        old_root = _fixture(tmp_path / "old")
        new_root = _fixture(tmp_path / "new")
        pasted = _pasted(
            OLD_PII_CONFIG, suggested_routes_text(suggested_routes(OLD_PII_CONFIG, new_root))
        )
        for root, config in ((old_root, OLD_PII_CONFIG), (new_root, pasted)):
            pii.mark_bounced(
                pii.contacts_surface_root(root, config),
                "ann@example.org",
                diagnostic="550 5.1.1 user unknown",
                observed_at="2026-11-27",
                source="script:test",
            )
        old_bytes = (old_root / "excluded" / "ann.md").read_bytes()
        assert (new_root / "excluded" / "ann.md").read_bytes() == old_bytes


class TestReporting:
    def test_record_scan_lists_uncovered_fields_without_values(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        rows = {
            (u.location, u.field): u.records for u in scan_undeclared_fields(OLD_PII_CONFIG, root)
        }
        assert rows == {
            ("excluded", "emails"): 1,
            ("excluded", "phones"): 1,
            ("excluded", "former_emails"): 1,
            ("excluded", "name"): 1,
            ("excluded", "notes_field"): 1,
        }

    def test_record_scan_honours_rules(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        pasted = _pasted(
            OLD_PII_CONFIG, suggested_routes_text(suggested_routes(OLD_PII_CONFIG, root))
        )
        fields = {u.field for u in scan_undeclared_fields(pasted, root)}
        assert fields == {"name", "notes_field"}

    def test_scan_finds_records_with_no_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "excluded").mkdir()
        (tmp_path / "excluded" / "x.md").write_text(
            "---\nuid: x\nsecret: s\n---\n", encoding="utf-8"
        )
        assert [(u.location, u.field) for u in scan_undeclared_fields({}, tmp_path)] == [
            ("excluded", "secret")
        ]

    def test_doctor_reports(self, tmp_path: Path) -> None:
        root = _fixture(tmp_path)
        result = check_undeclared_vault_data(OLD_PII_CONFIG, root)
        assert result.status == "fail"
        assert "storage.mapping.pii -> excluded" in result.detail
        assert "ann@example.org" not in result.detail
        assert "--suggest-routes" in (result.fix or "")

    def test_doctor_ok_when_nothing_is_outside_the_corpus(self, tmp_path: Path) -> None:
        assert check_undeclared_vault_data({}, tmp_path).status == "ok"

    def test_storage_status_reports_and_suggests(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _fixture(tmp_path)
        before = _snapshot(root)
        assert main(["storage", "status", "--path", str(root)]) == 0
        captured = capsys.readouterr()
        assert "storage.mapping.pii -> excluded: no rule" in captured.out
        assert "Undeclared vault data" in captured.err
        assert main(["storage", "status", "--path", str(root), "--suggest-routes"]) == 0
        out = capsys.readouterr().out
        assert suggested_routes_text(suggested_routes(OLD_PII_CONFIG, root)) in out
        assert "excluded: notes_field (1 record(s))" in out
        assert "ann@example.org" not in out
        assert _snapshot(root) == before

    def test_storage_status_unaffected_install_sees_no_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "wiki").mkdir()
        assert main(["storage", "status", "--path", str(tmp_path)]) == 0
        captured = capsys.readouterr()
        assert "ndeclared vault data:\n  (none)" in captured.out
        assert "Undeclared" not in captured.err
        status = build_status({}, tmp_path)
        assert status["undeclared"]["undeclared"] is False

    def test_recall_notice(self) -> None:
        assert _with_undeclared_vault_notice("hits", {}) == "hits"
        assert "_Notice: undeclared vault data" in _with_undeclared_vault_notice(
            "hits", OLD_PII_CONFIG
        )
        payload = json.loads(_with_undeclared_vault_notice('{"a": 1}', OLD_PII_CONFIG))
        assert UNDECLARED_VAULT_NOTICE_KEY in payload

    def test_cli_recall_keeps_running_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _fixture(tmp_path)
        main(["recall", "Ann", "--path", str(root), "--backend", "fts5"])
        assert "warning: undeclared vault data" in capsys.readouterr().err


class TestInlineContactLint:
    META = {"uid": "person-ann", "type": "person", "name": "Ann", "emails": ["ann@example.org"]}

    def test_warns_when_a_rule_routes_the_field(self) -> None:
        config = {
            "storage": {
                "routes": [{"page_type": "person", "fields": ["emails"], "to": ["excluded"]}]
            }
        }
        with pytest.warns(UserWarning, match="inline contact data"):
            validate_wiki_meta(dict(self.META), config=config)

    @pytest.mark.parametrize("config", [None, {}, OLD_PII_CONFIG])
    def test_silent_without_a_rule(self, config: Any) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            validate_wiki_meta(dict(self.META), config=config)

    def test_silent_when_routed_but_no_value_on_the_page(self) -> None:
        config = {
            "storage": {
                "routes": [{"page_type": "person", "fields": ["phones"], "to": ["excluded"]}]
            }
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            validate_wiki_meta(dict(self.META), config=config)
