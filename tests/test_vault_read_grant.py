# SPDX-License-Identifier: Apache-2.0
"""A sensitive read through a served process needs a read grant on the vault (athenaeum#3176).

Two vaults, two principals. ``vault_a`` holds the excluded record of a person
page and ``vault_b`` that of a vendor page; each vault declares who may read
it. The principal of a request is chosen by the test, standing in for the
verified token of the network server (``identity_provider.request_principal``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import identity_provider as ip
from athenaeum import pii, store_rights
from athenaeum.remote_policy import build_remote_policy
from tests.test_mcp_oauth_resource_server import (
    _FakeIssuer,
    _mcp_client,
    _serve_auth,
)
from tests.test_mcp_oauth_resource_server import (
    _no_ambient_serve_auth_env as _env_fixture,
)
from tests.test_mcp_oauth_resource_server import (
    issuer as _issuer_fixture,
)

_no_ambient_serve_auth_env = _env_fixture
issuer = _issuer_fixture

ISS = "https://git.example.net"
ALICE = (ISS, "alice")  # read on vault_a only
BOB = (ISS, "bob")  # read on vault_b only
NOBODY = (ISS, "nobody")

PERSON_VALUES = ("sentinel-person-7q@example.org", "sentinel-person-8r@example.net")
VENDOR_VALUES = ("sentinel-vendor-3x@example.org", "sentinel-vendor-4y@example.net")
ALL_VALUES = PERSON_VALUES + VENDOR_VALUES

OFF = {"embedded": False, "recallable": False, "merge_eligible": False}


def _vault(root: str) -> dict[str, Any]:
    return {
        "backing_store": "markdown",
        "surface_root": root,
        "corpus_policy": OFF,
        "sensitive": True,
    }


CONFIG: dict[str, Any] = {
    "storage": {
        "adapters": {"vault_a": _vault("vault_a"), "vault_b": _vault("vault_b")},
        "mapping": {"pii": "vault_a", "supplier": "vault_b"},
        "excluded_read_mapping": {"vendor": "supplier"},
        "excluded_fields": {
            "pii": ["emails", "phones", "former_emails", "alt_emails"],
            "supplier": ["emails"],
        },
    }
}


def _declaration(reader: tuple[str, str]) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "store_id": "st_q7m2xk4vwz5d",
            "roles": {
                "reader": {"rights": ["read"], "members": [{"iss": reader[0], "sub": reader[1]}]}
            },
        }
    )


@pytest.fixture(autouse=True)
def _fresh_rights() -> None:
    store_rights.reset_run_rights()


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (root / "raw").mkdir()
    (wiki / "alex.md").write_text(
        "---\nuid: alex\nname: Alex Widget\ntype: person\n---\n\n"
        "Alex Widget builds shared widgets.\n",
        encoding="utf-8",
    )
    (wiki / "acme.md").write_text(
        "---\nuid: acme\nname: Acme Gadget\ntype: vendor\n---\n\n"
        "Acme Gadget sells shared widgets.\n",
        encoding="utf-8",
    )
    for vault, uid, values in (
        ("vault_a", "alex", PERSON_VALUES),
        ("vault_b", "acme", VENDOR_VALUES),
    ):
        base = root / vault
        base.mkdir()
        (base / f"{uid}-contact.md").write_text(
            f"---\nuid: {uid}\npii: true\nemails:\n"
            + "".join(f"  - {v}\n" for v in values)
            + "---\n\nArchival data.\n",
            encoding="utf-8",
        )
    (root / "vault_a" / "_store").mkdir()
    (root / "vault_a" / "_store" / "access.yaml").write_text(_declaration(ALICE), encoding="utf-8")
    (root / "vault_b" / "_store").mkdir()
    (root / "vault_b" / "_store" / "access.yaml").write_text(_declaration(BOB), encoding="utf-8")
    return root


class _Served:
    """A network server whose request principal the test chooses."""

    def __init__(
        self,
        knowledge: Path,
        issuer: _FakeIssuer,
        monkeypatch: pytest.MonkeyPatch,
        *,
        remote_policy: Any = None,
    ) -> None:
        from athenaeum.mcp_server import create_server

        self.principal: tuple[str, ...] | None = None
        self.audit = knowledge.parent / "audit.jsonl"
        self.issuer = issuer
        monkeypatch.setattr(
            ip, "request_principal", lambda provider, access_token, *, config=None: self.principal
        )
        server = create_server(
            raw_root=knowledge / "raw",
            wiki_root=knowledge / "wiki",
            cache_dir=knowledge.parent / "cache",
            config=CONFIG,
            serve_auth=_serve_auth(),
            jwks_http_client=issuer.http_client(),
            audit_log_path=self.audit,
            remote_policy=remote_policy,
        )
        self.app = server.http_app(path="/mcp")

    def call(self, principal: tuple[str, ...] | None, tool: str, **args: Any) -> str:
        self.principal = principal

        async def run() -> str:
            async with self.app.router.lifespan_context(self.app):
                async with _mcp_client(self.app, self.issuer.mint()) as client:
                    result = await client.call_tool(tool, args)
                    return str(result.content[0].text)

        return asyncio.run(run())

    def records(self) -> list[dict[str, Any]]:
        if not self.audit.exists():
            return []
        lines = self.audit.read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines if '"sensitive_read"' in ln]


@pytest.fixture
def served(knowledge: Path, issuer: _FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> _Served:
    return _Served(knowledge, issuer, monkeypatch)


@pytest.fixture
def ceiling_off(knowledge: Path, issuer: _FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> _Served:
    return _Served(
        knowledge, issuer, monkeypatch, remote_policy=build_remote_policy(sensitive_reads=False)
    )


def _read(srv: _Served, principal: Any, uid: str, cls: str) -> str:
    return srv.call(principal, "read_entity", uid=uid, entity_class=cls, include_sensitive=True)


def _recall(srv: _Served, principal: Any, query: str = "shared widgets") -> str:
    return srv.call(principal, "recall", query=query, include_sensitive=True)


class TestPerVault:
    """Two vaults x two principals."""

    @pytest.mark.parametrize(
        ("principal", "gets_person", "gets_vendor"),
        [(ALICE, True, False), (BOB, False, True), (NOBODY, False, False)],
    )
    def test_read_entity_per_vault(
        self, served: _Served, principal: Any, gets_person: bool, gets_vendor: bool
    ) -> None:
        person = _read(served, principal, "alex", "person")
        vendor = _read(served, principal, "acme", "vendor")
        assert all(v in person for v in PERSON_VALUES) is gets_person
        assert all(v in vendor for v in VENDOR_VALUES) is gets_vendor
        # a vault one may not read leaks nothing of the other's values either
        assert not any(v in person for v in VENDOR_VALUES)
        assert not any(v in vendor for v in PERSON_VALUES)

    @pytest.mark.parametrize(
        ("principal", "gets_person", "gets_vendor"),
        [(ALICE, True, False), (BOB, False, True), (NOBODY, False, False)],
    )
    def test_recall_per_vault(
        self, served: _Served, principal: Any, gets_person: bool, gets_vendor: bool
    ) -> None:
        out = _recall(served, principal)
        assert "Alex Widget" in out and "Acme Gadget" in out  # pages are still returned
        assert all(v in out for v in PERSON_VALUES) is gets_person
        assert all(v in out for v in VENDOR_VALUES) is gets_vendor

    def test_handle_shaped_recall_is_gated_on_the_contact_vault(self, served: _Served) -> None:
        handle = PERSON_VALUES[0]
        granted = json.loads(_recall(served, ALICE, handle))
        assert all(v in json.dumps(granted["contact_values"]) for v in PERSON_VALUES)
        denied = _recall(served, BOB, handle)
        assert not any(v in denied for v in PERSON_VALUES[1:])
        assert json.loads(denied)["contact_values"] == []

    def test_no_principal_receives_markers(self, served: _Served) -> None:
        payload = json.loads(_read(served, None, "alex", "person"))
        assert payload["contact"] == {}
        assert payload["contact_included"] is False
        assert payload["redactions"]
        assert not any(v in json.dumps(payload) for v in ALL_VALUES)
        assert not any(v in _recall(served, None) for v in ALL_VALUES)

    def test_a_granted_read_returns_values(self, served: _Served) -> None:
        payload = json.loads(_read(served, ALICE, "alex", "person"))
        assert payload["contact_included"] is True
        assert "sensitive_reads_withheld" not in payload


class TestCeilingFirst:
    def test_ceiling_off_returns_no_value_even_to_a_principal_with_a_grant(
        self, ceiling_off: _Served
    ) -> None:
        assert not any(v in _read(ceiling_off, ALICE, "alex", "person") for v in ALL_VALUES)
        assert not any(v in _recall(ceiling_off, ALICE) for v in ALL_VALUES)
        assert ceiling_off.records() == []  # the ceiling decides before any grant is asked

    def test_denied_read_entity_is_identical_to_ceiling_off(
        self, served: _Served, ceiling_off: _Served
    ) -> None:
        for principal in (None, NOBODY, BOB):
            denied = _read(served, principal, "alex", "person")
            assert denied == _read(ceiling_off, ALICE, "alex", "person")
            assert "sensitive_reads_withheld" in json.loads(denied)

    def test_denied_recall_is_identical_to_ceiling_off(
        self, served: _Served, ceiling_off: _Served
    ) -> None:
        off = _recall(ceiling_off, ALICE)
        for principal in (None, NOBODY):
            assert _recall(served, principal) == off

    def test_denied_handle_recall_is_identical_to_ceiling_off(
        self, served: _Served, ceiling_off: _Served
    ) -> None:
        handle = PERSON_VALUES[0]
        assert _recall(served, NOBODY, handle) == _recall(ceiling_off, ALICE, handle)


class TestCheckPrecedesTheVaultFile:
    def test_recall_opens_no_record_of_a_vault_it_refused(
        self, served: _Served, knowledge: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[tuple[str, str]] = []
        real = pii.ExcludedRecordIndex.by_uid

        def _by_uid(self: Any, uid: str) -> Any:
            events.append(("open", Path(self.contacts_root).name))
            return real(self, uid)

        monkeypatch.setattr(pii.ExcludedRecordIndex, "by_uid", _by_uid)
        real_read = pii.read_bounce_record
        monkeypatch.setattr(
            pii,
            "read_bounce_record",
            lambda path: (events.append(("read", Path(path).parent.name)), real_read(path))[1],
        )
        _recall(served, ALICE)
        assert ("open", "vault_a") in events
        assert not [e for e in events if e[1] == "vault_b"]

    def test_the_decision_is_recorded_before_the_vault_is_opened(
        self, served: _Served, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []
        real = pii.ExcludedRecordIndex.by_uid

        def _by_uid(self: Any, uid: str) -> Any:
            seen.append(len(served.records()))
            return real(self, uid)

        monkeypatch.setattr(pii.ExcludedRecordIndex, "by_uid", _by_uid)
        _recall(served, ALICE)
        assert seen and all(n >= 1 for n in seen)

    def test_read_entity_never_takes_the_include_path_for_a_refused_vault(
        self, served: _Served, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        includes: list[bool] = []
        real = pii.assemble_excluded_read

        def _assemble(*args: Any, **kwargs: Any) -> Any:
            includes.append(bool(kwargs.get("include_excluded")))
            return real(*args, **kwargs)

        monkeypatch.setattr(pii, "assemble_excluded_read", _assemble)
        _read(served, BOB, "alex", "person")
        assert includes and not any(includes)
        includes.clear()
        _read(served, ALICE, "alex", "person")
        assert any(includes)

    def test_an_unwritable_audit_log_fails_the_read_closed(
        self, served: _Served, knowledge: Path
    ) -> None:
        served.audit.mkdir()  # a directory where the log file should be
        with pytest.raises(Exception):
            _read(served, ALICE, "alex", "person")


class TestUnchangedWithoutAServedProcess:
    def _stdio(self, knowledge: Path) -> Any:
        from athenaeum.mcp_server import create_server

        return create_server(
            raw_root=knowledge / "raw",
            wiki_root=knowledge / "wiki",
            cache_dir=knowledge.parent / "cache",
            config=CONFIG,
        )

    def test_stdio_server_flag_alone_decides(self, knowledge: Path) -> None:
        server = self._stdio(knowledge)

        async def fn(name: str) -> Any:
            return (await server.get_tool(name)).fn

        recall = asyncio.run(fn("recall"))
        read_entity = asyncio.run(fn("read_entity"))
        # vault_a and vault_b each name one reader; a stdio caller holds neither
        out = recall(query="shared widgets", include_sensitive=True)
        assert all(v in out for v in ALL_VALUES)
        payload = json.loads(read_entity(uid="alex", entity_class="person", include_sensitive=True))
        assert payload["contact_included"] is True
        without = json.loads(read_entity(uid="alex", entity_class="person"))
        assert without["contact_included"] is False

    def test_command_line_path_is_unchanged(self, knowledge: Path) -> None:
        from athenaeum.mcp_server import recall_search

        out = recall_search(knowledge / "wiki", "shared widgets", config=CONFIG, with_pii=True)
        assert all(v in out for v in ALL_VALUES)

    def test_no_tool_takes_a_principal(self, served: _Served, knowledge: Path) -> None:
        server = self._stdio(knowledge)
        for tool in asyncio.run(server.list_tools()):
            props = {n.lower() for n in (tool.parameters or {}).get("properties", {})}
            assert not {"principal", "identity", "vault", "grant"} & props


class TestAuditLog:
    def test_records_principal_vault_and_outcome_and_no_value(self, served: _Served) -> None:
        _read(served, ALICE, "alex", "person")
        _read(served, ALICE, "acme", "vendor")
        _recall(served, None)
        records = served.records()
        by = {(tuple(r["principal"] or ()), r["vault"], r["outcome"]) for r in records}
        assert (ALICE, "vault_a", "granted") in by
        assert (ALICE, "vault_b", "denied") in by
        assert ((), "vault_a", "denied") in by
        assert all(
            set(r) == {"ts", "event", "tool", "principal", "vault", "outcome"} for r in records
        )
        text = served.audit.read_text(encoding="utf-8")
        assert not any(v in text for v in ALL_VALUES)

    def test_a_read_that_asked_for_no_sensitive_value_records_nothing(
        self, served: _Served
    ) -> None:
        served.call(ALICE, "read_entity", uid="alex", entity_class="person")
        served.call(ALICE, "recall", query="shared widgets")
        assert served.records() == []


class TestHelpers:
    def test_surface_read_permitted(self) -> None:
        from athenaeum.storage import surface_read_permitted

        asked: list[str] = []

        def check(vault: str) -> bool:
            asked.append(vault)
            return False

        assert surface_read_permitted(None, "pii", CONFIG) is True
        assert surface_read_permitted(check, "pii", CONFIG) is False
        assert asked == ["vault_a"]
        # a class that is not outside the corpus has no vault: nothing is asked
        assert surface_read_permitted(check, "organization", CONFIG) is True
        assert asked == ["vault_a"]

    def test_audit_record_shape_refuses_an_unknown_outcome(self) -> None:
        from athenaeum.audit_log import build_sensitive_read_record

        with pytest.raises(ValueError):
            build_sensitive_read_record(principal=ALICE, vault="v", outcome="ok")


ROUTING_ON = {
    "sensitivity": {"routing": {"enabled": True}},
    "storage": {"routes": [{"sensitivity_class": "pii", "to": ["excluded"]}]},
}


class TestRoutedValuePointer:
    """A routed value's pointer resolves only when the gate grants its vault."""

    def _routed(self, tmp_path: Path) -> tuple[str, str, str]:
        from athenaeum.sensitivity_routing import _POINTER_RE, route_sensitive_values

        email = "sentinel-routed-5z@example.org"
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=f"Reach {email} please.",
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        match = _POINTER_RE.search(out)
        assert match is not None
        return match.group(1), match.group(2), email

    def test_gate_decides_before_the_record_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.sensitivity_routing import resolve_sensitive_record

        cfg = ROUTING_ON
        cls, record_id, email = self._routed(tmp_path)
        asked: list[str] = []
        assert resolve_sensitive_record(cls, record_id, cfg, tmp_path) == email

        reads: list[Path] = []
        real = Path.read_text

        def _spy(self: Path, *a: Any, **k: Any) -> str:
            reads.append(self)
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _spy)

        def deny(vault: str) -> bool:
            asked.append(vault)
            return False

        assert (
            resolve_sensitive_record(cls, record_id, cfg, tmp_path, vault_read_allowed=deny) is None
        )
        assert asked and not [p for p in reads if p.name == f"{record_id}.md"]
        assert (
            resolve_sensitive_record(
                cls, record_id, cfg, tmp_path, vault_read_allowed=lambda v: True
            )
            == email
        )
