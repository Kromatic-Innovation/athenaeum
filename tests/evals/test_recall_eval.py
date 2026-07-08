# SPDX-License-Identifier: Apache-2.0
"""Recall-sidecar live-API eval (issue #331).

The recall pipeline this covers:

    prompt --> query_topics.extract_topics (LIVE Haiku call) --> topics
           --> recall_search (fixture wiki, keyword backend, offline)
           --> formatted output text asserted against expected page uids
               and the #325 provenance header

Only ``extract_topics`` hits the network; :func:`recall_search` runs
against the ``tests/evals/data/recall/wiki/`` fixture with the keyword
backend, so results are deterministic once the topic list is fixed.

Aggregate floor: ≥ 5/6 (acceptance criteria).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.mcp_server import recall_search
from athenaeum.query_topics import DEFAULT_TOPIC_MODEL, extract_topics
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_RECALL,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


RECALL_FLOOR = 5  # ≥ 5/6 per acceptance criteria


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "recall" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _fixture_wiki_root() -> Path:
    return EVAL_DATA_ROOT / "recall" / "wiki"


def _uid_of(page_path: Path) -> str:
    """Extract the ``uid:`` line from a wiki page — used to map recall hits
    (which render by ``**Path:**`` filename) back to golden-set uids so a
    fixture rename does not silently break assertions."""
    text = page_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("uid:"):
            return line.split(":", 1)[1].strip()
    return ""


def _hits_by_uid(output: str) -> set[str]:
    """Return the set of fixture-wiki uids referenced by a ``recall_search``
    output. Uses the ``**Path:** wiki/<filename>`` line the formatter emits
    to map filenames back to uids."""
    uids: set[str] = set()
    wiki_root = _fixture_wiki_root()
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("**Path:**"):
            continue
        _, _, path_part = line.partition("**Path:**")
        path_str = path_part.strip()
        # Strip the ``wiki/`` display prefix ``recall_search`` prepends for
        # bare wiki entries (see ``_resolve_hit_path``).
        if path_str.startswith("wiki/"):
            filename = path_str[len("wiki/") :]
        else:
            filename = path_str
        page_path = wiki_root / filename
        if page_path.is_file():
            uids.add(_uid_of(page_path))
    return uids


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_recall_case(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
    tmp_path: Path,
) -> None:
    """Run one recall case end-to-end (extract_topics → recall_search)."""
    real_client = build_live_client()
    recording = RecordingClient(real_client, record=eval_record, layer=LAYER_RECALL)
    recording.start_case(case["id"])

    original_create = recording.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    recording.messages.create = _create  # type: ignore[method-assign]

    # ``extract_topics`` builds its own ``anthropic.Anthropic(...)`` — route
    # that construction through our recording wrapper so the response lands
    # on disk in ``--record`` mode without changing the production signature.
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: recording)
    # ``extract_topics`` short-circuits when ``ANTHROPIC_API_KEY`` is unset
    # (see query_topics.py); the ``_live_ready`` fixture guarantees it is
    # set here, so the fake-Anthropic constructor is what actually runs.
    topics = extract_topics(case["prompt"], timeout=15.0)
    recording.end_case()

    # Compose the recall query from the extracted topics; fall back to the
    # bare prompt when the topic extractor returned nothing (same fallback
    # the real recall hook uses).
    query = " ".join(topics) if topics else case["prompt"]
    wiki_root = _fixture_wiki_root()
    # Point the keyword backend at a scratch cache dir so an operator
    # running evals locally does not pollute their real ``~/.cache/athenaeum``.
    output = recall_search(
        wiki_root,
        query,
        top_k=6,
        search_backend="keyword",
        cache_dir=tmp_path / "cache",
    )

    hit_uids = _hits_by_uid(output)
    expected = case["expected"]
    expected_hits = set(expected.get("hits") or [])
    passed = True
    detail_parts: list[str] = []
    if expected_hits and not expected_hits.issubset(hit_uids):
        passed = False
        missing = expected_hits - hit_uids
        detail_parts.append(f"missing_hits={sorted(missing)}")
    min_distinct = int(expected.get("min_distinct_pages") or 0)
    if min_distinct and len(hit_uids) < min_distinct:
        passed = False
        detail_parts.append(
            f"distinct_pages={len(hit_uids)} < {min_distinct}"
        )
    if expected.get("contradiction_flag"):
        # #325 header: recall output must surface the flag for the
        # expected page. Substring match is sufficient — the formatter
        # renders exactly ``**Status:** contradiction-flagged (see
        # _pending_questions.md)`` (see ``_recall_metadata_lines``).
        if "**Status:** contradiction-flagged" not in output:
            passed = False
            detail_parts.append("missing_contradiction_flag_header")

    eval_session.record_case(
        LAYER_RECALL,
        case["id"],
        expected=(
            f"hits={sorted(expected_hits)} "
            f"min_distinct={min_distinct} "
            f"flag={bool(expected.get('contradiction_flag'))}"
        ),
        observed=(
            f"topics={topics} hits={sorted(hit_uids)} "
            f"output_len={len(output)}"
        ),
        passed=passed,
        detail="; ".join(detail_parts) or "ok",
    )


def test_recall_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the recall layer meets the ≥ 5/6 aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_RECALL)
    assert total > 0, "recall eval collected no cases"
    assert passed >= RECALL_FLOOR, (
        f"recall below aggregate floor: {passed}/{total} "
        f"(need ≥ {RECALL_FLOOR}). Topic model: {DEFAULT_TOPIC_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
