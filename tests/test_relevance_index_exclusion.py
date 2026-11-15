# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 6 (athenaeum#2648): never-matching raws stay out of recall."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from athenaeum.config import resolve_extra_intake_roots
from athenaeum.init import init_knowledge_dir
from athenaeum.mcp_server import _refresh_extra_root_index, recall_search, remember_write

POLICY = "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | | personal | |\n"
SECRET = "zorblaxsecretgizmo"
VISIBLE = "quibbletonordinarywidget"


def _raw(tag: str, body: str, extra_fm: str = "") -> str:
    # ``description`` carries the body so the hook (which matches on page
    # metadata columns, not bodies) can find it.
    return (
        f"---\ntype: person\nname: Ada Example\ndescription: {body}\n"
        f"tags: [{tag}]\n{extra_fm}---\n{body}\n"
    )


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    init_knowledge_dir(root)
    schema = root / "wiki" / "_schema"
    (schema / "relevance-policy.md").write_text(POLICY)
    tags = (schema / "tags.md").read_text()
    if "personal" not in tags:
        (schema / "tags.md").write_text(tags.rstrip("\n") + "\n| personal | operator |\n")
    return root


def _recall(kb: Path, cache: Path, query: str) -> str:
    return recall_search(
        kb / "wiki",
        query,
        top_k=10,
        search_backend="fts5",
        cache_dir=cache,
        extra_roots=resolve_extra_intake_roots(kb),
    )


def _plant(kb: Path, name: str, text: str) -> None:
    d = kb / "raw" / "claude-session"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def _hook_output(monkeypatch, capsys, cache: Path, prompt: str) -> str:
    import functools

    import athenaeum.claude_code_adapter as adapter
    import athenaeum.context as context_mod

    # The real hook entrypoint, with only the LLM topic-extraction leg off
    # (no network in tests); the index query and the render are the real ones.
    monkeypatch.setattr(
        context_mod,
        "build_context_for_turn",
        functools.partial(context_mod.build_context_for_turn, use_llm=False),
    )

    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    monkeypatch.setenv("SEARCH_BACKEND", "fts5")
    monkeypatch.setenv("AUTO_RECALL", "true")
    monkeypatch.setattr(adapter, "DEFAULT_KNOWLEDGE_ROOT", cache.parent / "knowledge")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": prompt.split()[3]}))
    )
    capsys.readouterr()
    assert adapter.main() == 0
    return capsys.readouterr().out


def test_never_raw_is_not_recalled_or_injected_but_ordinary_raw_is(
    kb: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    cache = tmp_path / "cache"
    _plant(kb, "20260101T000000Z-aaaaaaaa.md", _raw("personal", f"Ada keeps {SECRET} close."))
    _plant(kb, "20260101T000000Z-bbbbbbbb.md", _raw("active", f"Ada also knows {VISIBLE} well."))
    _refresh_extra_root_index("fts5", wiki_root=kb / "wiki", cache_dir=cache, config=None)

    assert "Ada keeps" not in _recall(kb, cache, SECRET)
    # Positive control: the same path DOES return the ordinary raw.
    assert VISIBLE in _recall(kb, cache, VISIBLE)

    hook_secret = _hook_output(monkeypatch, capsys, cache, f"tell me about {SECRET} please")
    assert SECRET not in hook_secret and "Ada keeps" not in hook_secret
    hook_visible = _hook_output(monkeypatch, capsys, cache, f"tell me about {VISIBLE} please")
    assert VISIBLE in hook_visible


def test_remember_refresh_path_excludes_unflagged_and_keeps_explicit(
    kb: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    raw, wiki = kb / "raw", kb / "wiki"
    remember_write(
        raw, _raw("personal", f"Plain {SECRET}"), sources="user-stated:t", wiki_root=wiki
    )
    remember_write(
        raw,
        _raw("personal", "Asked-for quibbletonian fact"),
        sources="user-stated:t",
        wiki_root=wiki,
        explicit_request=True,
    )
    _refresh_extra_root_index("fts5", wiki_root=wiki, cache_dir=cache, config=None)
    assert "Plain" not in _recall(kb, cache, SECRET)
    assert "quibbletonian" in _recall(kb, cache, "quibbletonian")


def test_full_rebuild_over_raw_applies_the_same_filter(kb: Path, tmp_path: Path) -> None:
    """A full ``build_index`` over raw/ (the reindex path) filters the same way."""
    from athenaeum.search import get_backend

    cache = tmp_path / "cache"
    _plant(kb, "20260101T000000Z-aaaaaaaa.md", _raw("personal", f"Ada keeps {SECRET} close."))
    get_backend("fts5").build_index(
        kb / "wiki", cache, extra_roots=resolve_extra_intake_roots(kb), incremental=False
    )
    assert "Ada keeps" not in _recall(kb, cache, SECRET)


def test_no_policy_file_indexes_exactly_as_before(kb: Path, tmp_path: Path) -> None:
    (kb / "wiki" / "_schema" / "relevance-policy.md").unlink()
    cache = tmp_path / "cache"
    _plant(kb, "20260101T000000Z-aaaaaaaa.md", _raw("personal", f"Ada keeps {SECRET} close."))
    _refresh_extra_root_index("fts5", wiki_root=kb / "wiki", cache_dir=cache, config=None)
    assert SECRET in _recall(kb, cache, SECRET)


def test_invalid_policy_fails_open(kb: Path, tmp_path: Path) -> None:
    (kb / "wiki" / "_schema" / "relevance-policy.md").write_text(
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| bogus | | personal | |\n"
    )
    cache = tmp_path / "cache"
    _plant(kb, "20260101T000000Z-aaaaaaaa.md", _raw("personal", f"Ada keeps {SECRET} close."))
    _refresh_extra_root_index("fts5", wiki_root=kb / "wiki", cache_dir=cache, config=None)
    assert SECRET in _recall(kb, cache, SECRET)


def test_mixed_raw_is_kept_out_whole(kb: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    mixed = _raw("active", f"Kept {VISIBLE}.") + _raw("personal", f"Covered {SECRET}.")
    _plant(kb, "20260101T000000Z-aaaaaaaa.md", mixed)
    _refresh_extra_root_index("fts5", wiki_root=kb / "wiki", cache_dir=cache, config=None)
    # A hit renders the raw file itself, so a raw with ANY covered item stays out whole.
    for query in (VISIBLE, SECRET):
        out = _recall(kb, cache, query)
        assert "Covered" not in out and "Kept" not in out
