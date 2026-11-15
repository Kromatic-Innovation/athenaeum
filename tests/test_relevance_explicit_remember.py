# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 5 (athenaeum#2646): explicit-remember override flag."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athenaeum.models import parse_frontmatter
from athenaeum.relevance_policy import (
    EXPLICIT_REMEMBER_KEY,
    gate_raw_content,
    load_relevance_policy,
)
from athenaeum.relevance_policy_edit import audit_path, read_audit

POLICY = (
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
    "| never | | personal | |\n"
)
PERSONAL = "---\ntype: person\nname: Ada Example\ntags: [personal]\n---\nLikes oat milk.\n"
WORK = "---\ntype: person\nname: Cy Example\ntags: [active]\n---\nRuns the platform team.\n"


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
    (wiki / "_schema" / "tags.md").write_text("| Tag |\n|---|\n| active |\n| personal |\n")
    (wiki / "_schema" / "relevance-policy.md").write_text(POLICY)
    (tmp_path / "raw").mkdir()
    return tmp_path


def _remember(kb: Path, transport: str | None, **kwargs) -> str:
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    server = create_server(raw_root=kb / "raw", wiki_root=kb / "wiki", transport=transport)

    async def _run():
        return (await server.get_tool("remember")).fn(**kwargs)

    return asyncio.run(_run())


def _raws(kb: Path) -> list[Path]:
    return sorted((kb / "raw").rglob("*.md"))


def _gate(kb: Path, path: Path):
    return gate_raw_content(load_relevance_policy(kb / "wiki"), path.read_text())


TRANSPORTS = ["stdio", "http"]


@pytest.mark.parametrize("transport", TRANSPORTS)
class TestOverride:
    def test_flagged_fact_is_stamped_kept_and_audited(self, kb: Path, transport: str) -> None:
        out = _remember(kb, transport, content=PERSONAL, explicit_request=True)
        assert out.startswith("Saved to")
        (raw,) = _raws(kb)
        meta, _ = parse_frontmatter(raw.read_text())
        assert meta[EXPLICIT_REMEMBER_KEY] is True
        gate = _gate(kb, raw)
        assert gate.drops == () and not gate.fully_dropped
        assert len(gate.overrides) == 1
        (rec,) = read_audit(kb / "wiki")
        assert rec["kind"] == "override"
        assert rec["transport"] == transport
        assert rec["rule"] == {"verdict": "never", "type": None, "tag": "personal", "subject": None}
        assert rec["rule_number"] == 1 and rec["at"]

    def test_unflagged_fact_is_dropped_by_the_gate_and_not_audited(
        self, kb: Path, transport: str
    ) -> None:
        assert _remember(kb, transport, content=PERSONAL).startswith("Saved to")
        (raw,) = _raws(kb)
        meta, _ = parse_frontmatter(raw.read_text())
        assert EXPLICIT_REMEMBER_KEY not in meta
        gate = _gate(kb, raw)
        assert gate.fully_dropped and gate.overrides == ()
        assert read_audit(kb / "wiki") == []

    def test_flag_on_non_matching_fact_writes_no_audit(self, kb: Path, transport: str) -> None:
        _remember(kb, transport, content=WORK, explicit_request=True)
        assert read_audit(kb / "wiki") == []
        assert _gate(kb, _raws(kb)[0]).overrides == ()


def test_stdio_and_http_agree_apart_from_transport(kb: Path, tmp_path_factory) -> None:
    other = tmp_path_factory.mktemp("kb2")
    (other / "wiki").mkdir()
    (other / "raw").mkdir()
    import shutil

    shutil.copytree(kb / "wiki" / "_schema", other / "wiki" / "_schema")
    for root, transport in ((kb, "stdio"), (other, "http")):
        _remember(root, transport, content=PERSONAL, explicit_request=True)
    a, b = (read_audit(r / "wiki")[0] for r in (kb, other))
    assert {k: v for k, v in a.items() if k not in ("at", "transport")} == {
        k: v for k, v in b.items() if k not in ("at", "transport")
    }
    assert (a["transport"], b["transport"]) == ("stdio", "http")


def test_default_transport_is_audited_as_stdio(kb: Path) -> None:
    _remember(kb, None, content=PERSONAL, explicit_request=True)
    assert read_audit(kb / "wiki")[0]["transport"] == "stdio"


def test_no_policy_file_is_a_noop(kb: Path) -> None:
    (kb / "wiki" / "_schema" / "relevance-policy.md").unlink()
    assert _remember(kb, "stdio", content=PERSONAL, explicit_request=True).startswith("Saved to")
    assert not audit_path(kb / "wiki").exists()


def test_unwritable_audit_log_refuses_the_write(kb: Path) -> None:
    audit_path(kb / "wiki").mkdir()  # a directory where the ledger should be
    out = _remember(kb, "stdio", content=PERSONAL, explicit_request=True)
    assert out.startswith("Error:") and _raws(kb) == []


def test_marker_in_content_frontmatter_is_audited_too(kb: Path) -> None:
    content = PERSONAL.replace("tags: [personal]", "tags: [personal]\nexplicit_remember: true")
    _remember(kb, "http", content=content)
    (rec,) = read_audit(kb / "wiki")
    assert rec["kind"] == "override" and rec["transport"] == "http"


def test_gate_stays_pure_and_mixed_items_only_exempt_flagged() -> None:
    policy = load_relevance_policy_from_text(POLICY)
    flagged = PERSONAL.replace("tags: [personal]", "tags: [personal]\nexplicit_remember: true")
    result = gate_raw_content(policy, flagged)
    assert result.kept_content == flagged and len(result.overrides) == 1
    assert json.loads(json.dumps(result.overrides[0].rule.to_dict()))["verdict"] == "never"


def test_tool_description_says_to_set_only_when_explicitly_asked(kb: Path) -> None:
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    server = create_server(raw_root=kb / "raw", wiki_root=kb / "wiki")
    tool = asyncio.run(server.get_tool("remember"))
    prop = tool.parameters["properties"]["explicit_request"]
    assert "ONLY when the user explicitly" in " ".join(prop["description"].split())


def load_relevance_policy_from_text(text: str):
    from athenaeum.relevance_policy import parse_relevance_policy

    return parse_relevance_policy(text, {"person"}, {"active", "personal"})
