# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2978: the push-path relevance floor in ``build_context``.

``build_context_for_turn`` resolves
``resolve_recall_relevance_floor(config, backend, unprompted=True)`` per
candidate backend and drops candidates that fail ``meets_relevance_floor``
before the count cap. Unset, the envelope is unchanged. Fixtures are invented.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import athenaeum.context as context_mod
from athenaeum.context import build_context, build_context_for_turn

PROMPT = "zebrafish migration notes"

#: Three pages that all match the prompt, with clearly different BM25 ranks:
#: the more prompt words a page's indexed columns repeat, the better its rank.
_ROWS = [
    ("aaaa1111-best.md", "Zebrafish Migration Notes", "zebrafish migration notes"),
    ("bbbb2222-mid.md", "Zebrafish Migration", "zebrafish"),
    ("cccc3333-weak.md", "Aquarium Log", "a zebrafish"),
]

_FLOOR_ENVS = (
    "ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5",
    "ATHENAEUM_RECALL_MIN_SCORE_FTS5",
    "ATHENAEUM_RECALL_PUSH_MIN_SCORE_KEYWORD",
    "ATHENAEUM_RECALL_MIN_SCORE_KEYWORD",
)


def _build_index(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, "
        "description, audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, audience, type) "
        "VALUES (?, ?, ?, '', ?, '|__access_open__|', 'reference')",
        [(fn, name, tags, f"{name} page") for fn, name, tags in _ROWS],
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _no_ambient_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _FLOOR_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def cache(tmp_path: Path) -> Path:
    _build_index(tmp_path / "wiki-index.db")
    return tmp_path


def _turn(cache: Path, session: str = "s", **kw: Any) -> dict[str, Any]:
    kw.setdefault("search_backend", "fts5")
    kw.setdefault("n", 7)
    return build_context_for_turn(PROMPT, session, cache_dir=cache, use_llm=False, **kw)


def _stable(envelope: dict[str, Any]) -> str:
    """The envelope as bytes, minus the one wall-clock field."""
    return json.dumps({k: v for k, v in envelope.items() if k != "elapsed_ms"}, sort_keys=True)


def _names(envelope: dict[str, Any]) -> list[str]:
    return [c["filename"] for c in envelope["candidates"]]


def _ranks(cache: Path) -> list[float]:
    env = build_context(PROMPT, "probe", cache_dir=cache, n=7, search_backend="fts5", use_llm=False)
    assert _names(env) == [fn for fn, _, _ in _ROWS], "fixture ordering assumption broke"
    ranks = [c["relevance"] for c in env["candidates"]]
    assert ranks[0] < ranks[1] < ranks[2], "fixture needs three distinct BM25 ranks"
    return ranks


# ---------------------------------------------------------------------------
# No floor set: unchanged
# ---------------------------------------------------------------------------


def test_no_floor_envelope_is_byte_identical(cache: Path) -> None:
    """With no floor, the per-turn envelope equals the one ``build_context``
    produces without the new argument at all (the pre-change call)."""
    pre = build_context(PROMPT, "s", cache_dir=cache, n=7, search_backend="fts5", use_llm=False)
    turn = _turn(cache)
    assert _stable(turn) == _stable(pre)
    assert len(turn["candidates"]) == 3


def test_no_floor_passes_no_floor_to_build_context(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    real = context_mod.build_context

    def spy(*a: Any, **k: Any) -> dict[str, Any]:
        seen.append(k.get("relevance_floor", "absent"))
        return real(*a, **k)

    monkeypatch.setattr(context_mod, "build_context", spy)
    _turn(cache)
    assert seen == [None]


def test_explicit_recall_floor_does_not_reach_the_push(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the ``unprompted=True`` (push) floor applies here; the explicit
    ``recall_search`` floor is a different setting."""
    monkeypatch.setenv("ATHENAEUM_RECALL_MIN_SCORE_FTS5", "-1000")
    assert len(_turn(cache)["candidates"]) == 3


# ---------------------------------------------------------------------------
# FTS5 floor: lower (more negative) rank is better
# ---------------------------------------------------------------------------


def test_fts5_candidate_at_floor_is_kept_and_one_past_it_is_dropped(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranks = _ranks(cache)
    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", repr(ranks[1]))
    env = _turn(cache)
    assert _names(env) == ["aaaa1111-best.md", "bbbb2222-mid.md"]
    assert env["candidates"][1]["relevance"] == ranks[1]  # exactly at the floor: kept


def test_fts5_floor_from_yaml_config(cache: Path) -> None:
    ranks = _ranks(cache)
    config = {"recall": {"relevance_floor": {"push": {"fts5": ranks[0]}}}}
    assert _names(_turn(cache, config=config)) == ["aaaa1111-best.md"]


def test_dropped_candidates_are_not_counted_in_the_overflow_breadcrumb(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranks = _ranks(cache)
    # Control: with n=1 and no floor, the two others are advertised as withheld.
    unfloored = _turn(cache, session="control", n=1)
    assert "2 more" in unfloored["render"]["text"]

    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", repr(ranks[1]))
    floored = _turn(cache, session="floored", n=1)
    assert _names(floored) == ["aaaa1111-best.md"]
    # Only the at-floor candidate was withheld by the cap; the dropped one
    # is not advertised.
    assert "1 more" in floored["render"]["text"]
    assert "2 more" not in floored["render"]["text"]

    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", repr(ranks[0]))
    only = _turn(cache, session="only", n=1)
    assert _names(only) == ["aaaa1111-best.md"]
    assert "more" not in only["render"]["text"]


def test_floor_that_drops_everything_renders_nothing(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", "-1000")
    env = _turn(cache)
    assert env["candidates"] == []
    assert env["render"]["text"] == ""


def test_floor_never_reorders(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ranks = _ranks(cache)
    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", repr(ranks[2]))
    assert _names(_turn(cache)) == [fn for fn, _, _ in _ROWS]


# ---------------------------------------------------------------------------
# Vector floor: cosine distance, lower is better, kept off the envelope
# ---------------------------------------------------------------------------


def _fake_vector(monkeypatch: pytest.MonkeyPatch, cache: Path, hits: list[tuple]) -> None:
    import athenaeum.search as search_mod

    (cache / "wiki-vectors").mkdir(exist_ok=True)
    monkeypatch.setattr(search_mod, "query_vector_index", lambda *_a, **_k: list(hits))


_VECTOR_HITS = [
    ("vvvv0001-near.md", "Near Page", 0.30),
    ("vvvv0002-edge.md", "Edge Page", 0.50),
    ("vvvv0003-past.md", "Past Page", 0.5000001),
]


def test_vector_candidate_at_floor_is_kept_and_one_past_it_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_vector(monkeypatch, tmp_path, _VECTOR_HITS)
    config = {"recall": {"relevance_floor": {"push": {"vector": 0.5}}}}
    env = _turn(tmp_path, search_backend="vector", config=config)
    assert _names(env) == ["vvvv0001-near.md", "vvvv0002-edge.md"]


def test_vector_distance_is_not_in_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_vector(monkeypatch, tmp_path, _VECTOR_HITS)
    env = _turn(tmp_path, search_backend="vector")
    assert len(env["candidates"]) == 3
    for c in env["candidates"]:
        assert "distance" not in c
        assert c["relevance"] is None
    assert "0.5000001" not in json.dumps(env)


def test_each_backend_uses_its_own_floor_and_direction(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vector floor leaves FTS5 candidates alone (and the reverse): the
    BM25 ranks here are all below 0.5, so a vector floor misapplied to them
    in the vector direction would keep them anyway, and one in the wrong
    direction would drop them all; neither must happen."""
    _fake_vector(monkeypatch, cache, _VECTOR_HITS)
    ranks = _ranks(cache)

    vector_only = {"recall": {"relevance_floor": {"push": {"vector": 0.5}}}}
    env = _turn(cache, session="v", search_backend="vector", config=vector_only)
    assert _names(env) == [fn for fn, _, _ in _ROWS] + ["vvvv0001-near.md", "vvvv0002-edge.md"]

    fts_only = {"recall": {"relevance_floor": {"push": {"fts5": ranks[0]}}}}
    env = _turn(cache, session="f", search_backend="vector", config=fts_only)
    assert _names(env) == ["aaaa1111-best.md"] + [fn for fn, _, _ in _VECTOR_HITS]


def test_candidate_without_a_score_is_kept() -> None:
    """The floor removes candidates shown to be below it, never ones it
    cannot judge."""
    c = context_mod.Candidate(
        filename="x.md", name="X", description="", backend="vector", relevance=None, audience=""
    )
    assert context_mod._apply_relevance_floor([c], {"vector": 0.1}) == [c]


# ---------------------------------------------------------------------------
# The static-refresh caller is unaffected
# ---------------------------------------------------------------------------


def test_static_refresh_caller_ignores_the_push_floor(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_context`` called directly (the Tier-2 static-refresh shape, and
    ``codex_adapter.refresh``'s call) never resolves the push floor."""
    monkeypatch.setenv("ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5", "-1000")
    config = {"recall": {"relevance_floor": {"push": {"fts5": -1000.0, "vector": 0.0}}}}
    env = build_context(
        PROMPT, "", cache_dir=cache, n=7, search_backend="fts5", use_llm=False, config=config
    )
    assert len(env["candidates"]) == 3
    # Positive control: the same setting does apply on the push path.
    assert _turn(cache)["candidates"] == []

