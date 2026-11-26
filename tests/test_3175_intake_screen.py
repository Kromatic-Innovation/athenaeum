# SPDX-License-Identifier: Apache-2.0
"""Routed and discarded values are screened before a raw note is written (athenaeum#3175).

All values are synthetic markers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum import sensitivity
from athenaeum.init import init_knowledge_dir
from athenaeum.mcp_server import remember_write
from athenaeum.models import parse_frontmatter
from athenaeum.sensitivity_routing import SensitivityRoutingError, screen_intake_content
from athenaeum.storage import (
    discard_counts,
    reset_discard_counts,
    write_raw_intake,
    write_raw_intake_screened,
)

ROUTED = "zz-routed-3175@example.invalid"
DISCARDED = "zz-discarded-3175-token"
FIELD_ROUTED = "zz-field-routed-3175"
FIELD_DISCARDED = "zz-field-discarded-3175"

_ADAPTERS: dict[str, Any] = {
    "vault": {
        "backing_store": "markdown",
        "surface_root": "vault",
        "corpus_policy": {"embedded": False, "recallable": False, "merge_eligible": False},
        "sensitive": True,
    }
}

CFG: dict[str, Any] = {
    "sensitivity": {
        "routing": {"enabled": True},
        "classes": {
            "tokenish": {
                "recognizers": ["tokenish-3175"],
                "read_policy": {"access": "confidential"},
            },
        },
    },
    "storage": {
        "adapters": _ADAPTERS,
        "routes": [
            {"sensitivity_class": "pii", "to": ["vault"]},
            {"sensitivity_class": "tokenish", "to": []},
            {"page_type": "reference", "fields": ["routed_field"], "to": ["vault"]},
            {"page_type": "reference", "fields": ["dropped_field"], "to": []},
        ],
    },
}


class _TokenRecognizer:
    """A throwaway recogniser bound to the discarded class (test-only)."""

    name = "tokenish-3175"

    def detect(self, *, text: str, frontmatter: Any) -> list[Any]:
        start = text.find(DISCARDED)
        if start == -1:
            return []
        span = (start, start + len(DISCARDED))
        return [sensitivity.SensitivityMatch(recognizer=self.name, value=DISCARDED, span=span)]


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sensitivity, "_REGISTERED_RECOGNIZERS", dict(sensitivity._REGISTERED_RECOGNIZERS)
    )
    sensitivity.register_recognizer(_TokenRecognizer(), replace=True)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    reset_discard_counts()


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "knowledge"
    init_knowledge_dir(kb)
    return kb


def _all_files(*roots: Path) -> list[Path]:
    return [p for r in roots if r.exists() for p in r.rglob("*") if p.is_file()]


def _remember(kb: Path, content: str, cfg: dict[str, Any] | None) -> str:
    return remember_write(
        kb / "raw",
        content,
        "sessions",
        wiki_root=kb / "wiki",
        sources="user-stated:test",
        config=cfg,
        knowledge_root=kb,
    )


def test_sentinel_values_reach_no_file_but_the_vault_record(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    body = f"Write to {ROUTED} and keep {DISCARDED} secret."
    result = _remember(kb, body, CFG)
    assert result.startswith("Saved to ")
    holders = [
        p
        for p in _all_files(kb, tmp_path / "cache", state)
        if ROUTED in p.read_text(encoding="utf-8", errors="replace")
        or DISCARDED in p.read_text(encoding="utf-8", errors="replace")
    ]
    vault = kb / "vault" / "sensitivity" / "pii"
    assert [p.parent for p in holders] == [vault]
    assert DISCARDED not in holders[0].read_text(encoding="utf-8")
    (raw,) = (kb / "raw" / "sessions").glob("*.md")
    text = raw.read_text(encoding="utf-8")
    assert "[sensitive:pii:" in text and "[sensitive:tokenish — value not stored" in text
    assert discard_counts() == {"storage.routes[1]": 1}


def test_recall_does_not_return_routed_or_discarded_values(tmp_path: Path) -> None:
    from athenaeum.mcp_server import recall_search

    kb = _kb(tmp_path)
    _remember(kb, f"Mail {ROUTED} about {DISCARDED}.", CFG)
    out = recall_search(kb / "wiki", "zz-routed-3175 zz-discarded-3175", top_k=5)
    assert ROUTED not in out and DISCARDED not in out


def test_declared_fields_are_routed_and_discarded(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    note = (
        "---\ntype: reference\nname: Ref\nrouted_field: "
        f"{FIELD_ROUTED}\ndropped_field: {FIELD_DISCARDED}\nkept: plain\n---\n\nBody.\n"
    )
    assert _remember(kb, note, CFG).startswith("Saved to ")
    (raw,) = (kb / "raw" / "sessions").glob("*.md")
    meta, _ = parse_frontmatter(raw.read_text(encoding="utf-8"))
    assert "dropped_field" not in meta
    assert str(meta["routed_field"]).startswith("[sensitive:field_reference:")
    assert meta["kept"] == "plain" and meta["type"] == "reference"
    for p in _all_files(kb):
        text = p.read_text(encoding="utf-8", errors="replace")
        assert FIELD_DISCARDED not in text
        if FIELD_ROUTED in text:
            assert p.parent == kb / "vault" / "sensitivity" / "field_reference"


def test_failed_vault_write_fails_the_call_and_writes_nothing(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    (kb / "vault").write_text("a file where the vault directory must go", encoding="utf-8")
    result = _remember(kb, f"Mail {ROUTED}.", CFG)
    assert result.startswith("Error:") and ROUTED not in result
    assert not list((kb / "raw").rglob("*.md"))


def test_retry_converges_on_the_same_vault_record(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    body = f"Mail {ROUTED} please."
    _remember(kb, body, CFG)
    first = sorted((kb / "vault" / "sensitivity" / "pii").glob("*.md"))
    _remember(kb, body, CFG)
    assert sorted((kb / "vault" / "sensitivity" / "pii").glob("*.md")) == first


def test_two_values_with_the_same_surrounding_text_do_not_share_a_record(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    _remember(kb, "Mail zz-one-3175@example.invalid please.", CFG)
    _remember(kb, "Mail zz-two-3175@example.invalid please.", CFG)
    records = list((kb / "vault" / "sensitivity" / "pii").glob("*.md"))
    assert len(records) == 2


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        {},
        {"sensitivity": {"routing": {"enabled": False}}},
        {"storage": {"routes": []}},
    ],
)
def test_no_rule_is_byte_identical_to_the_unscreened_path(
    tmp_path: Path, cfg: dict[str, Any] | None
) -> None:
    content = f"---\ntype: reference\nname: Ref\nx: {FIELD_ROUTED}\n---\n\nMail {ROUTED}.\n"
    screened = write_raw_intake_screened(
        tmp_path / "a", content, config=cfg, knowledge_root=tmp_path
    )
    plain = write_raw_intake(tmp_path / "b", content)
    assert screened.read_bytes() == plain.read_bytes()
    assert screen_intake_content(content, config=cfg, knowledge_root=tmp_path) == content


def test_screened_entry_point_routes_for_any_caller(tmp_path: Path) -> None:
    path = write_raw_intake_screened(
        tmp_path / "raw" / "adapter",
        f"Mail {ROUTED}.",
        config=CFG,
        knowledge_root=tmp_path,
        raw_ref="adapter:item-1",
    )
    assert ROUTED not in path.read_text(encoding="utf-8")
    assert len(list((tmp_path / "vault" / "sensitivity" / "pii").glob("*.md"))) == 1


def test_screened_entry_point_failure_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "vault").write_text("blocker", encoding="utf-8")
    with pytest.raises(SensitivityRoutingError):
        write_raw_intake_screened(
            tmp_path / "raw", f"Mail {ROUTED}.", config=CFG, knowledge_root=tmp_path
        )
    assert not (tmp_path / "raw").exists() or not list((tmp_path / "raw").rglob("*.md"))
