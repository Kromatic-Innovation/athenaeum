# SPDX-License-Identifier: Apache-2.0
"""``list_pending_decisions`` applies the per-vault read check (athenaeum#3220).

``recall`` and ``read_entity`` ask, on a served process, whether the request's
principal holds read on a vault before one of its values is returned
(athenaeum#3176). ``list_pending_decisions(include_sensitive=True)`` resolves a
vault-held field-correction value (athenaeum#3180) and must ask the same
question, in the same order, with the same audit record.

The fixture is the two-vault, two-principal one of ``test_vault_read_grant``:
``vault_a`` is readable by Alice only and holds both the person's contact
values and the pending correction value.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from athenaeum.librarian import _run_correction_phase
from athenaeum.remote_policy import build_remote_policy
from tests.test_librarian_corrections import _batch, _make_ctx
from tests.test_mcp_oauth_resource_server import _FakeIssuer
from tests.test_vault_read_grant import (
    ALICE,
    BOB,
    NOBODY,
    PERSON_VALUES,
    _fresh_rights,
    _no_ambient_serve_auth_env,
    _Served,
    issuer,
    seed_two_vaults,
)
from tests.test_vault_read_grant import (
    CONFIG as GRANT_CONFIG,
)

# Fixtures used here by name; imported so pytest registers them in this module.
__all__ = ["_fresh_rights", "_no_ambient_serve_auth_env", "issuer"]

SUBMITTER = "enrichment-service"
VALUE = "zz-pending-title-sentinel-zz"
MARKER = "redacted: current_title"
TOOL = "list_pending_decisions"


def _config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(GRANT_CONFIG))
    cfg["librarian"] = {
        "corrections": {"fields": {"current_title": {"shape": "scalar", "writers": [SUBMITTER]}}}
    }
    person_route = cfg["storage"]["routes"][0]
    person_route["fields"] = [*person_route["fields"], "current_title"]
    return cfg


CONFIG = _config()


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    """The two-vault fixture plus one escalated correction whose value is kept in ``vault_a``."""
    root = seed_two_vaults(tmp_path / "knowledge")
    record = root / "vault_a" / "alex-contact.md"
    text = record.read_text(encoding="utf-8")
    # An undated tie against an api: incumbent is escalated by the precedence rules.
    record.write_text(
        text.replace(
            "pii: true\n",
            "pii: true\ncurrent_title: CTO\nfield_sources:\n  current_title: 'api:apollo'\n",
            1,
        ),
        encoding="utf-8",
    )
    raw = root / "raw" / SUBMITTER
    raw.mkdir(parents=True)
    (raw / "20260806T030000Z-1a2b3c4d.jsonl").write_text(
        _batch(
            {
                "record": "correction",
                "target": {"uid": "alex"},
                "op": "set",
                "field": "current_title",
                "value": VALUE,
                "source": "api:enrichment-vendor",
                "observed_at": "2026-08-06T05:58:40Z",
            },
            submitter=SUBMITTER,
        ),
        encoding="utf-8",
    )
    _run_correction_phase(_make_ctx(root, CONFIG))
    pending = (root / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")
    assert "vault:" in pending and VALUE not in pending  # the block holds a reference
    # The tool loads its configuration from disk; YAML is a superset of JSON.
    (root / "athenaeum.yaml").write_text(json.dumps(CONFIG), encoding="utf-8")
    return root


def _serve(knowledge: Path, issuer: _FakeIssuer, mp: pytest.MonkeyPatch, **kw: Any) -> _Served:
    mp.setattr("tests.test_vault_read_grant.CONFIG", CONFIG)
    return _Served(knowledge, issuer, mp, **kw)


@pytest.fixture
def served(knowledge: Path, issuer: _FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> _Served:
    return _serve(knowledge, issuer, monkeypatch)


@pytest.fixture
def ceiling_off(knowledge: Path, issuer: _FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> _Served:
    return _serve(
        knowledge, issuer, monkeypatch, remote_policy=build_remote_policy(sensitive_reads=False)
    )


def _decisions(srv: _Served, principal: Any, **args: Any) -> dict[str, Any]:
    return json.loads(srv.call(principal, TOOL, **args))


def _correction(envelope: dict[str, Any]) -> dict[str, Any]:
    (item,) = [i for i in envelope["items"] if "Field: current_title" in json.dumps(i)]
    return item


class TestListPendingDecisionsPerVault:
    def test_a_caller_without_a_grant_gets_the_redaction_marker(self, served: _Served) -> None:
        for principal in (BOB, NOBODY, None):
            item = json.dumps(_correction(_decisions(served, principal, include_sensitive=True)))
            assert VALUE not in item
            assert MARKER in item  # the item is still listed, with the marker

    def test_a_granted_caller_gets_the_value(self, served: _Served) -> None:
        item = json.dumps(_correction(_decisions(served, ALICE, include_sensitive=True)))
        assert VALUE in item and MARKER not in item

    def test_a_refusal_reads_exactly_as_a_call_without_include_sensitive(
        self, served: _Served
    ) -> None:
        without = _decisions(served, ALICE)
        assert VALUE not in json.dumps(without)
        assert _decisions(served, BOB, include_sensitive=True) == without

    def test_ceiling_off_decides_before_any_grant_is_asked(self, ceiling_off: _Served) -> None:
        item = json.dumps(_correction(_decisions(ceiling_off, ALICE, include_sensitive=True)))
        assert VALUE not in item and MARKER in item
        assert ceiling_off.records() == []


class TestAuditLog:
    def test_both_calls_are_recorded_under_the_tool_name(self, served: _Served) -> None:
        _decisions(served, ALICE, include_sensitive=True)
        _decisions(served, BOB, include_sensitive=True)
        records = [r for r in served.records() if r["tool"] == TOOL]
        by = {(tuple(r["principal"] or ()), r["vault"], r["outcome"]) for r in records}
        assert by == {(ALICE, "vault_a", "granted"), (BOB, "vault_a", "denied")}
        assert VALUE not in served.audit.read_text(encoding="utf-8")

    def test_a_call_without_include_sensitive_records_nothing(self, served: _Served) -> None:
        _decisions(served, ALICE)
        assert served.records() == []


class TestStdioUnchanged:
    def test_stdio_flag_alone_decides(self, knowledge: Path) -> None:
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=knowledge / "raw",
            wiki_root=knowledge / "wiki",
            cache_dir=knowledge.parent / "cache",
            config=CONFIG,
            audit_log_path=knowledge.parent / "audit.jsonl",
        )
        fn = asyncio.run(server.get_tool(TOOL)).fn
        # vault_a names one reader; a stdio caller holds no grant, and needs none
        assert VALUE in json.dumps(_correction(fn(include_sensitive=True)))
        hidden = json.dumps(_correction(fn()))
        assert VALUE not in hidden and MARKER in hidden
        assert not (knowledge.parent / "audit.jsonl").exists()


# One case per tool that can return a vault-held value with include_sensitive.
# A tool added later that reads vault values must join this table, which means
# it must pass the same three assertions -- and the completeness test below
# fails until it does.
_Call = Callable[[_Served, Any], str]
GATED_TOOLS: dict[str, tuple[_Call, tuple[str, ...]]] = {
    "recall": (
        lambda srv, who: srv.call(who, "recall", query="shared widgets", include_sensitive=True),
        PERSON_VALUES,
    ),
    "read_entity": (
        lambda srv, who: srv.call(
            who, "read_entity", uid="alex", entity_class="person", include_sensitive=True
        ),
        PERSON_VALUES,
    ),
    "list_pending_decisions": (
        lambda srv, who: srv.call(who, TOOL, include_sensitive=True),
        (VALUE,),
    ),
}

# Tools that take include_sensitive but never open a vault. Joining this table
# needs a reason, and the behaviour test below holds each one to it.
READS_NO_VAULT: dict[str, str] = {
    # `google_contact_*` is gated as an on-page join key: the value is read from
    # the wiki page's own frontmatter, never from an excluded-surface record.
    "enumerate_entities": "gates on-page join-key fields only",
}


class TestEveryToolReadingVaultValuesIsGated:
    def test_every_include_sensitive_tool_is_in_the_table(self, knowledge: Path) -> None:
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=knowledge / "raw",
            wiki_root=knowledge / "wiki",
            cache_dir=knowledge.parent / "cache",
            config=CONFIG,
        )
        with_flag = {
            t.name
            for t in asyncio.run(server.list_tools())
            if "include_sensitive" in (t.parameters or {}).get("properties", {})
        }
        assert with_flag == set(GATED_TOOLS) | set(READS_NO_VAULT)

    def test_a_tool_that_reads_no_vault_asks_about_none(self, served: _Served) -> None:
        out = served.call(ALICE, "enumerate_entities", entity_type="person", include_sensitive=True)
        assert "alex" in out
        assert not any(v in out for v in (*PERSON_VALUES, VALUE))
        assert served.records() == []

    @pytest.mark.parametrize("tool", sorted(GATED_TOOLS))
    def test_tool_applies_the_vault_check(self, served: _Served, tool: str) -> None:
        call, values = GATED_TOOLS[tool]
        granted = call(served, ALICE)
        assert all(v in granted for v in values)
        denied = call(served, NOBODY)
        assert not any(v in denied for v in values)
        outcomes = {
            (tuple(r["principal"] or ()), r["vault"], r["outcome"])
            for r in served.records()
            if r["tool"] == tool
        }
        assert (ALICE, "vault_a", "granted") in outcomes
        assert (NOBODY, "vault_a", "denied") in outcomes
