# SPDX-License-Identifier: Apache-2.0
"""Email-shaped recall falls back to registry handles, then page bodies (athenaeum#2714)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from athenaeum import pii
from athenaeum._cmd_index import cmd_registry
from athenaeum.mcp_server import recall_search
from athenaeum.registry import BODY_EMAIL_INDEX_NAME

CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _page(knowledge: Path, uid: str, name: str, body: str = "", extra: str = "") -> None:
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / f"{uid}-page.md").write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n{extra}---\n\n{name}. {body}\n",
        encoding="utf-8",
    )


def _contact(knowledge: Path, uid: str, address: str) -> None:
    root = pii.contacts_surface_root(knowledge, CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{uid}-contact.md").write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n  - {address}\n---\n\nArchival.\n",
        encoding="utf-8",
    )


def _build(knowledge: Path) -> None:
    rc = cmd_registry(argparse.Namespace(path=knowledge, out=None, stdout=False))
    assert rc == 0


def _recall(knowledge: Path, query: str, **kw: object) -> dict:
    return json.loads(recall_search(knowledge / "wiki", query, config=CONFIG, **kw))


def test_contact_store_hit_unchanged(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0001", "Alex Widget")
    _contact(tmp_path, "aaaa0001", "alex@example.org")
    _build(tmp_path)
    payload = _recall(tmp_path, "alex@example.org")
    assert payload["resolved"] is True
    assert payload["uid"] == "aaaa0001"


def test_registry_handle_only_hit(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0002", "Bea Gadget", extra="alt_emails:\n  - bea@example.org\n")
    _build(tmp_path)
    (tmp_path / BODY_EMAIL_INDEX_NAME).unlink()  # prove it is the registry, not the sidecar
    payload = _recall(tmp_path, "bea@example.org")
    assert payload["resolved"] is True
    assert payload["uid"] == "aaaa0002"


def test_body_only_hit(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0003", "Cy Sprocket", body="Reach me at Cy.Sprocket@Example.com today.")
    _build(tmp_path)
    payload = _recall(tmp_path, "cy.sprocket@example.com")
    assert payload["resolved"] is True
    assert payload["uid"] == "aaaa0003"
    assert payload["display_name"] == "Cy Sprocket"


def test_ambiguous_body_match(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0004", "Di One", body="team@example.org")
    _page(tmp_path, "aaaa0005", "Di Two", body="also team@example.org")
    _build(tmp_path)
    payload = _recall(tmp_path, "team@example.org")
    assert payload["resolved"] is False
    assert payload["candidate_uids"] == ["aaaa0004", "aaaa0005"]


def test_true_no_match(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0006", "Ed Nobody", body="ed@example.org")
    _build(tmp_path)
    payload = _recall(tmp_path, "stranger@example.org")
    assert payload["resolved"] is False
    assert payload["reason"] == "no-match"
    assert payload["candidate_uids"] == []


def test_near_miss_domain_does_not_match(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0007", "Flo Near", body="flo@example.com")
    _build(tmp_path)
    assert _recall(tmp_path, "flo@example.co")["reason"] == "no-match"


def test_absent_sidecar_degrades_to_no_match(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0008", "Gus Old", body="gus@example.org")
    payload = _recall(tmp_path, "gus@example.org")  # never built
    assert payload["resolved"] is False
    assert payload["reason"] == "no-match"


def test_stale_sidecar_entry_is_reverified(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0009", "Hal Moved", body="hal@example.org")
    _build(tmp_path)
    _page(tmp_path, "aaaa0009", "Hal Moved", body="no address now")
    assert _recall(tmp_path, "hal@example.org")["reason"] == "no-match"


def test_sidecar_stores_no_raw_address(tmp_path: Path) -> None:
    _page(tmp_path, "aaaa0010", "Ivy Hidden", body="ivy@example.org")
    _build(tmp_path)
    assert "ivy@example.org" not in (tmp_path / BODY_EMAIL_INDEX_NAME).read_text()


def test_no_per_query_body_scan(tmp_path: Path, monkeypatch) -> None:
    """Only the candidate page is read per query, never every page body."""
    for i in range(20):
        _page(tmp_path, f"bbbb{i:04d}", f"Person {i}", body=f"p{i}@example.org")
    _build(tmp_path)
    read: set[str] = set()
    orig = Path.read_text

    def spy(self: Path, *a: object, **k: object) -> str:
        if self.parent.name == "wiki":
            read.add(self.name)
        return orig(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    assert _recall(tmp_path, "p3@example.org", cache_dir=tmp_path / ".cache")["uid"] == "bbbb0003"
    assert read == {"bbbb0003-page.md"}


def test_with_pii_gating_on_body_resolved_hit(tmp_path: Path) -> None:
    """Contact values flow only through the sanctioned join, gated by with_pii."""
    _page(tmp_path, "aaaa0011", "Jo Gated", body="Old address jo.old@example.org")
    _contact(tmp_path, "aaaa0011", "jo@example.net")
    _build(tmp_path)

    off = _recall(tmp_path, "jo.old@example.org")
    assert off["resolved"] is True and off["with_pii"] is False
    assert off["contact_values"] == []
    assert off["redactions"] == [{"field": "emails", "value_count": 1, "redacted": True}]
    assert "jo@example.net" not in json.dumps(off["contact_values"] + off["redactions"])

    on = _recall(tmp_path, "jo.old@example.org", with_pii=True)
    assert on["resolved"] is True and on["redactions"] == []
    assert [v["identifier"] for v in on["contact_values"]] == ["jo@example.net"]
