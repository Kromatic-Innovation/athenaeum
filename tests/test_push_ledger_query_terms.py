# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2916: the push ledger records the extracted
query terms and which extractor produced them."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from athenaeum import push_metrics
from athenaeum.config import resolve_push_record_query_terms
from athenaeum.context import build_context_for_turn

PROMPT = "please summarise the zebrafish migration notes"


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("ATHENAEUM_PUSH_RECORD_QUERY_TERMS", raising=False)
    monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
    conn = sqlite3.connect(tmp_path / "wiki-index.db")
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, "
        "description, audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.execute(
        "INSERT INTO wiki VALUES ('aaaa1111-zebrafish.md', 'Zebrafish Migration', "
        "'zebrafish', '', 'zebrafish notes', '|__access_open__|', 'reference')"
    )
    conn.commit()
    conn.close()
    return tmp_path


def _rows(cache: Path) -> list[dict[str, Any]]:
    return push_metrics.read_push_records(cache)


def _turn(cache: Path, **kw: Any) -> dict[str, Any]:
    return build_context_for_turn(
        PROMPT, "sess-t", cache_dir=cache, search_backend="fts5", **kw
    )


def test_model_path_records_terms_and_extractor(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.query_topics as qt

    monkeypatch.setattr(qt, "extract_topics", lambda *a, **k: ["zebrafish migration"])
    _turn(cache, use_llm=True)
    (row,) = _rows(cache)
    assert row["extractor"] == "model"
    assert sorted(row["query_terms"]) == ["migration", "zebrafish"]
    assert PROMPT not in json.dumps(row)  # the prompt itself is never stored


def test_fallback_path_records_regex_fallback(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.query_topics as qt

    monkeypatch.setattr(qt, "extract_topics", lambda *a, **k: [])
    _turn(cache, use_llm=True)
    (row,) = _rows(cache)
    assert row["extractor"] == "regex_fallback"
    assert "zebrafish" in row["query_terms"]


def test_model_raising_is_a_fallback(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.query_topics as qt

    def boom(*a: Any, **k: Any) -> list[str]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(qt, "extract_topics", boom)
    _turn(cache, use_llm=True)
    assert _rows(cache)[0]["extractor"] == "regex_fallback"


def test_llm_not_tried_records_regex_only(cache: Path) -> None:
    _turn(cache, use_llm=False)
    assert _rows(cache)[0]["extractor"] == "regex_only"


def test_disabled_by_env_omits_both_fields(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATHENAEUM_PUSH_RECORD_QUERY_TERMS", "0")
    _turn(cache, use_llm=False)
    (row,) = _rows(cache)
    assert "query_terms" not in row and "extractor" not in row
    assert row["pushed_count"] == 1  # the push itself is still recorded


def test_disabled_by_yaml_config(cache: Path) -> None:
    _turn(cache, use_llm=False, config={"push_metrics": {"record_query_terms": False}})
    (row,) = _rows(cache)
    assert "query_terms" not in row and "extractor" not in row


def test_resolver_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_PUSH_RECORD_QUERY_TERMS", raising=False)
    assert resolve_push_record_query_terms(None) is True
    assert resolve_push_record_query_terms({"push_metrics": {"record_query_terms": False}}) is False
    assert resolve_push_record_query_terms({"push_metrics": {"record_query_terms": "no"}}) is True
    monkeypatch.setenv("ATHENAEUM_PUSH_RECORD_QUERY_TERMS", "off")
    assert resolve_push_record_query_terms({"push_metrics": {"record_query_terms": True}}) is False


def test_recorded_terms_are_truncated(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.query_topics as qt

    monkeypatch.setattr(qt, "extract_topics", lambda *a, **k: ["zebrafish", "a" * 200])
    _turn(cache, use_llm=True)
    terms = _rows(cache)[0]["query_terms"]
    assert "a" * 64 in terms and all(len(t) <= 64 for t in terms)


def test_old_ledger_row_without_fields_reads_cleanly(cache: Path) -> None:
    old = {
        "v": 1,
        "session_id": "s-old",
        "ts": "2026-01-01T00:00:00Z",
        "query_hash": "abc",
        "backend": "fts5",
        "items": [{"id": "x1", "tier": "internal", "scope": "open", "token_cost": 5}],
        "pushed_count": 1,
        "token_cost": 5,
        "token_cost_estimated": True,
        "source": "sidecar",
    }
    push_metrics.push_records_path(cache).write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert push_metrics.read_push_records(cache) == [old]
    shaped = list(push_metrics.tail_records(cache_dir=cache))
    assert len(shaped) == 1
    assert "query_terms" not in shaped[0] and "extractor" not in shaped[0]


def test_tail_shows_terms_and_extractor(cache: Path) -> None:
    _turn(cache, use_llm=False)
    (shaped,) = list(push_metrics.tail_records(cache_dir=cache))
    assert shaped["extractor"] == "regex_only"
    assert "zebrafish" in shaped["query_terms"]


def test_viewer_last_turn_prefers_recorded_terms(cache: Path) -> None:
    from athenaeum._cmd_viewer import enrich_payload, shape_viewer_payload

    _turn(cache, use_llm=False)
    records = list(push_metrics.tail_records(cache_dir=cache))
    payload = shape_viewer_payload(session_id="sess-t", records=records)
    payload = enrich_payload(payload, wiki_root=cache / "wiki", cache_dir=cache)
    turn = payload["last_turn"]
    assert turn["topics_status"] == "ok"
    assert "zebrafish" in turn["topics"]
    assert turn["extractor"] == "regex_only"
