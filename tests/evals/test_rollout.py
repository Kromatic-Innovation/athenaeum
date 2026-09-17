# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the six-arm rollout runner (issues athenaeum#1522,
athenaeum#1574).

NOT ``rollout``-marked (issue athenaeum#1742) — runs in the default
selection, same as every other offline test under this directory: it makes
no network call and spawns no subprocess. The stream-json parser tests
replay a committed, redacted fixture
(``tests/evals/data/rollout/pull_stream_spike.jsonl``), and the arm tests
use ``tests.conftest.FakeLLMClient`` (the repo's canonical anthropic-shaped
test double) instead of a live client, plus (for the two breadcrumb arms) an
injected ``context_fn``/``breadcrumb_pull_runner`` stub instead of actually
shelling out to the shipped hooks. The credential-gated tool-use-loop test
and the real-``claude``-binary spike test live in
``tests/evals/test_rollout_pull_spike.py``; the real-hook byte-equivalence
test lives in ``tests/evals/test_rollout_push_breadcrumb_spike.py`` — this
module never needs any of those gates.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.corpus import build_corpus
from tests.evals.harness import EVAL_TOKEN_CEILING, EvalSession
from tests.evals.north_star_report import grade_correctness
from tests.evals.rollout import (
    ALL_ARMS,
    RECALL_TOOL_NAME,
    Arm,
    RolloutRecord,
    ToolCall,
    build_pull_argv,
    build_pull_mcp_config,
    materialize_native_memory,
    parse_pull_stream,
    run_none,
    run_oracle,
    run_probe_all_arms,
    run_push_breadcrumb,
    run_push_pages_upper_bound,
)

FIXTURE_PATH = Path(__file__).parent / "data" / "rollout" / "pull_stream_spike.jsonl"
FOLLOW_THROUGH_FIXTURE_PATH = (
    Path(__file__).parent / "data" / "rollout" / "follow_through_stream_spike.jsonl"
)


def _corpus():
    return build_corpus("core")


def _probe(probe_id: str):
    corpus = _corpus()
    return next(p for p in corpus.probes if p.id == probe_id), corpus


# ---------------------------------------------------------------------------
# Arm enumeration
# ---------------------------------------------------------------------------


def test_arm_enumeration_has_exactly_eight_arms() -> None:
    assert set(ALL_ARMS) == {
        Arm.NONE,
        Arm.PUSH_PAGES_UPPER_BOUND,
        Arm.PUSH_BREADCRUMB,
        Arm.PUSH_BREADCRUMB_PULL,
        Arm.ORACLE,
        Arm.PULL,
        Arm.NATIVE_INDEX,
        Arm.NATIVE_GREP,
    }
    assert len(ALL_ARMS) == 8
    # str-subclass so an arm round-trips through GridCell.arm (a plain str).
    assert Arm("push_pages_upper_bound") is Arm.PUSH_PAGES_UPPER_BOUND
    assert Arm.PULL.value == "pull"
    # Appended, not interleaved -- the pre-athenaeum#1725 six-arm order is a
    # prefix of the current eight-arm order.
    assert ALL_ARMS[:6] == (
        Arm.NONE,
        Arm.PUSH_PAGES_UPPER_BOUND,
        Arm.PUSH_BREADCRUMB,
        Arm.PUSH_BREADCRUMB_PULL,
        Arm.ORACLE,
        Arm.PULL,
    )
    assert ALL_ARMS[6:] == (Arm.NATIVE_INDEX, Arm.NATIVE_GREP)


def test_native_arms_round_trip_through_arm_value() -> None:
    assert Arm("native_index") is Arm.NATIVE_INDEX
    assert Arm("native_grep") is Arm.NATIVE_GREP
    assert Arm.NATIVE_INDEX.value == "native_index"
    assert Arm.NATIVE_GREP.value == "native_grep"


def test_arm_legacy_push_value_resolves_to_the_renamed_upper_bound_arm() -> None:
    """Issue athenaeum#1574 AC5: a result-store row written before this
    issue persisted the bare ``"push"`` string. ``Arm("push")`` must still
    resolve (to the arm that value actually named — the five-page delivery,
    now called ``push_pages_upper_bound``), not raise ``ValueError``."""
    assert Arm("push") is Arm.PUSH_PAGES_UPPER_BOUND


# ---------------------------------------------------------------------------
# Offline stream-json parser — recorded fixture (AC: offline unit coverage)
# ---------------------------------------------------------------------------


def test_parse_pull_stream_from_recorded_fixture() -> None:
    """Parses the committed, redacted fixture derived from the proven
    athenaeum#1522 spike shape: init event names athenaeum connected and
    recall available, one tool_use call, a final text answer."""
    lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    parsed = parse_pull_stream(lines)

    assert parsed.mcp_connected is True
    assert parsed.recall_tool_available is True
    assert parsed.recall_called is True
    assert parsed.tool_calls == [
        ToolCall(name=RECALL_TOOL_NAME, query="Zebra protocol quorum size")
    ]
    assert parsed.answer == "The Zebra protocol quorum size is 5."
    # Two assistant messages in the fixture -> two turns, NOT summed into one.
    assert parsed.turn_count == 2
    assert [t.turn for t in parsed.turn_tokens] == [1, 2]
    assert parsed.turn_tokens[0].input_tokens == 512
    assert parsed.turn_tokens[0].output_tokens == 48
    assert parsed.turn_tokens[1].input_tokens == 180
    assert parsed.turn_tokens[1].output_tokens == 22
    # Raw transcript is preserved event-for-event (5 lines in the fixture).
    assert len(parsed.transcript) == 5


def test_parse_pull_stream_follow_through_fixture_shows_two_recalls_and_grades_correct() -> None:
    """AC (issue athenaeum#1737): a PULL transcript for one
    ``follow_through`` probe — ``fenwick_relationship_history`` — shows TWO
    ``recall`` calls (the breadcrumb page, then the second-hop page reached
    by following its link), and the resulting answer grades correct only
    because it carries BOTH planted tokens (see
    ``test_follow_through_grading_requires_every_planted_token`` in
    ``tests/evals/test_north_star_report.py`` for the token-omission half of
    this contract).

    No live rollout was run to capture this transcript (the surrounding
    ``system``/``assistant``/``result`` events are hand-authored), but both
    ``tool_result`` bodies are the ACTUAL, unedited return value of
    ``athenaeum.mcp_server.recall_search`` called against the real ``core``
    corpus materialized to a temp dir and indexed with the real ``fts5``
    backend — not a hand-typed approximation of the rendering shape. Byte
    for byte: ``Found N matching pages:`` / ``### 1. {name} (score: N)`` /
    ``**Path:**`` / ``**Tags:**`` / ``**Uid:**`` / ``**Type:**`` /
    ``**Source:** ... · **Updated:** ...`` / ``**Links:**`` (present only on
    the first hit, whose body carries the ``[[wikilink]]``; absent from the
    second, which has none) / the page's own H1 / its body. Reuses the SAME
    offline stream-json replay mechanism as
    ``test_parse_pull_stream_from_recorded_fixture`` above (issue
    athenaeum#1725/#1729) — no new replay machinery."""
    lines = FOLLOW_THROUGH_FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    parsed = parse_pull_stream(lines)

    assert parsed.recall_called is True
    assert len(parsed.tool_calls) == 2
    assert [call.name for call in parsed.tool_calls] == [RECALL_TOOL_NAME, RECALL_TOOL_NAME]

    corpus = build_corpus(scale="core")
    probe = next(p for p in corpus.probes if p.id == "fenwick_relationship_history")
    assert probe.probe_class == "follow_through"

    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale="core",
        answer=parsed.answer,
        turn_tokens=parsed.turn_tokens,
        tool_calls=parsed.tool_calls,
        recall_called=parsed.recall_called,
        injected_context_tokens=None,
        turn_count=parsed.turn_count,
        transcript=parsed.transcript,
    )
    assert grade_correctness(record, probe, corpus) is True


def test_parse_pull_stream_choosing_not_to_call_recall_is_recorded_not_error() -> None:
    """AC: PULL choosing NOT to call recall is a recorded outcome, never an
    error. A transcript with zero tool_use blocks parses cleanly."""
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Bash", RECALL_TOOL_NAME],
                "mcp_servers": [{"name": "athenaeum", "status": "connected"}],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "I already know the answer: 42."}],
                    "usage": {"input_tokens": 60, "output_tokens": 12},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "I already know the answer: 42.",
            }
        ),
    ]

    parsed = parse_pull_stream(lines)

    assert parsed.recall_called is False
    assert parsed.tool_calls == []
    assert parsed.answer == "I already know the answer: 42."
    assert parsed.turn_count == 1


def test_parse_pull_stream_skips_blank_lines_and_non_assistant_events() -> None:
    lines = [
        "",
        "   ",
        json.dumps({"type": "user", "message": {"content": []}}),
        json.dumps({"type": "result", "subtype": "success", "result": "ok"}),
    ]
    parsed = parse_pull_stream(lines)
    assert parsed.answer == "ok"
    assert parsed.turn_count == 0
    assert parsed.mcp_connected is False
    assert parsed.recall_tool_available is False


# ---------------------------------------------------------------------------
# PULL argv / mcp-config shape
# ---------------------------------------------------------------------------


def test_build_pull_argv_inverts_only_the_two_pinning_flags() -> None:
    argv = build_pull_argv("claude", Path("/tmp/example/mcp-config.json"), "claude-haiku-4-5")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--mcp-config" in argv
    assert "--strict-mcp-config" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # The athenaeum#906 text-only pin (`--tools ""`) must NOT appear — PULL's
    # entire point is that the model's tool set is available.
    assert "--tools" not in argv


def test_build_pull_mcp_config_scopes_to_the_athenaeum_server_only(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    cache_dir = tmp_path / "cache"
    config = build_pull_mcp_config(
        knowledge_root, cache_dir, athenaeum_bin="/opt/venv/bin/athenaeum"
    )

    servers = config["mcpServers"]
    assert set(servers) == {"athenaeum"}
    entry = servers["athenaeum"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/opt/venv/bin/athenaeum"
    assert entry["args"] == [
        "serve",
        "--path",
        str(knowledge_root),
        "--cache-dir",
        str(cache_dir),
    ]


# ---------------------------------------------------------------------------
# NONE / PUSH_PAGES_UPPER_BOUND / PUSH_BREADCRUMB / ORACLE single-shot arms,
# offline via FakeLLMClient
# ---------------------------------------------------------------------------


def test_run_none_sends_no_context() -> None:
    probe, _corpus = _probe("pto_allowance")
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "I don't know.", usage=make_llm_usage(input_tokens=30, output_tokens=6)
        )
    )

    record = run_none(probe, "core", client=client, session=session, model="stub-model")

    assert record.arm is Arm.NONE
    assert record.injected_context_tokens is None
    assert record.recall_called is False
    assert record.tool_calls == []
    assert record.turn_count == 1
    assert len(record.turn_tokens) == 1
    assert record.turn_tokens[0].input_tokens == 30
    assert record.turn_tokens[0].output_tokens == 6
    # No "Context:" prefix in the single call the fake client recorded.
    [call] = client.calls
    assert "Context:" not in call["messages"][0]["content"]


def test_run_push_pages_upper_bound_delivers_recall_search_output(tmp_path: Path) -> None:
    probe, corpus = _probe("pto_allowance")
    wiki_root = corpus.materialize(tmp_path)
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "10 days per year.", usage=make_llm_usage(input_tokens=400, output_tokens=15)
        )
    )

    record = run_push_pages_upper_bound(
        probe,
        "core",
        wiki_root=wiki_root,
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        client=client,
        session=session,
        model="stub-model",
    )

    assert record.arm is Arm.PUSH_PAGES_UPPER_BOUND
    assert record.injected_context_tokens is not None
    assert record.injected_context_tokens >= 0
    [call] = client.calls
    assert "Context:" in call["messages"][0]["content"]
    assert record.transcript[0]["pushed_context"]


def test_run_push_breadcrumb_uses_the_injected_context_fn(tmp_path: Path) -> None:
    """Offline: no subprocess spawn — a stub ``context_fn`` stands in for
    :func:`build_push_breadcrumb_context`, matching this module's own
    no-subprocess discipline. The real-hook proof lives in
    ``test_rollout_push_breadcrumb_spike.py``."""
    probe, _corpus = _probe("pto_allowance")
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "10 days per year.", usage=make_llm_usage(input_tokens=90, output_tokens=10)
        )
    )
    seen_args: dict[str, object] = {}

    def _stub_context_fn(knowledge_root: Path, hook_home: Path, query: str) -> str:
        seen_args["knowledge_root"] = knowledge_root
        seen_args["hook_home"] = hook_home
        seen_args["query"] = query
        return (
            "[Knowledge context] Wiki pages relevant to this message "
            "(use `recall` MCP tool for full details):\n"
            "  - PTO policy — 25 days per year\n"
        )

    record = run_push_breadcrumb(
        probe,
        "core",
        knowledge_root=tmp_path / "knowledge",
        hook_home=tmp_path / "hook_home",
        client=client,
        session=session,
        model="stub-model",
        context_fn=_stub_context_fn,
    )

    assert record.arm is Arm.PUSH_BREADCRUMB
    assert seen_args["query"] == probe.query
    assert record.injected_context_tokens is not None
    assert record.injected_context_tokens > 0
    [call] = client.calls
    assert "PTO policy" in call["messages"][0]["content"]
    assert "Context:" in call["messages"][0]["content"]
    assert record.transcript[0]["pushed_context"].startswith("[Knowledge context]")


def test_run_push_breadcrumb_empty_hook_output_injects_nothing(tmp_path: Path) -> None:
    """The shipped hook returns ``""`` when it declines to inject (short
    prompt, no index, no match) — never an error. PUSH_BREADCRUMB must
    treat that the same way NONE treats "nothing to inject"."""
    probe, _corpus = _probe("pto_allowance")
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "I don't know.", usage=make_llm_usage(input_tokens=20, output_tokens=6)
        )
    )

    record = run_push_breadcrumb(
        probe,
        "core",
        knowledge_root=tmp_path / "knowledge",
        hook_home=tmp_path / "hook_home",
        client=client,
        session=session,
        model="stub-model",
        context_fn=lambda *args, **kwargs: "",
    )

    assert record.injected_context_tokens == 0
    [call] = client.calls
    assert "Context:" not in call["messages"][0]["content"]


def test_run_oracle_uses_ground_truth_pages_directly() -> None:
    probe, corpus = _probe("pto_allowance")
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "10 days per year.", usage=make_llm_usage(input_tokens=200, output_tokens=15)
        )
    )

    record = run_oracle(probe, corpus, "core", client=client, session=session, model="stub-model")

    assert record.arm is Arm.ORACLE
    assert record.injected_context_tokens is not None
    assert record.injected_context_tokens > 0
    oracle_context = record.transcript[0]["oracle_context"]
    for uid in probe.expected_uids:
        assert uid in oracle_context


def test_run_oracle_abstention_probe_gets_zero_injected_tokens() -> None:
    probe, corpus = _probe("abstain_unknown_client")
    assert probe.expected_uids == ()
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "I don't know.", usage=make_llm_usage(input_tokens=20, output_tokens=6)
        )
    )

    record = run_oracle(probe, corpus, "core", client=client, session=session, model="stub-model")

    # Zero, not None -- ORACLE always populates a count; there was just
    # nothing to inject for an abstention probe.
    assert record.injected_context_tokens == 0
    [call] = client.calls
    assert "Context:" not in call["messages"][0]["content"]


# ---------------------------------------------------------------------------
# materialize_native_memory (issue athenaeum#1725) — offline, no subprocess
# ---------------------------------------------------------------------------


def test_materialize_native_memory_writes_one_topic_file_per_page(tmp_path: Path) -> None:
    corpus = build_corpus("core")

    memory_dir = materialize_native_memory(corpus, tmp_path, write_index=True)

    assert memory_dir == tmp_path / "memory"
    topic_files = {p.name for p in memory_dir.glob("*.md")} - {"MEMORY.md"}
    assert topic_files == {page.filename for page in corpus.pages}
    # Each topic file is the FULL page markdown, not a summary.
    sample = corpus.pages[0]
    assert (memory_dir / sample.filename).read_text(encoding="utf-8") == sample.to_markdown()


def test_materialize_native_memory_without_index_writes_no_memory_md(tmp_path: Path) -> None:
    corpus = build_corpus("core")

    memory_dir = materialize_native_memory(corpus, tmp_path, write_index=False)

    assert not (memory_dir / "MEMORY.md").exists()
    # Topic files are still written -- only the index is skipped.
    assert any(memory_dir.glob("*.md"))


def test_materialize_native_memory_index_has_one_line_per_page(tmp_path: Path) -> None:
    corpus = build_corpus("core")

    memory_dir = materialize_native_memory(corpus, tmp_path, write_index=True)

    index_text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    bullet_lines = [line for line in index_text.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) == len(corpus.pages)
    for page in corpus.pages:
        assert any(line.startswith(f"- {page.name} —") for line in bullet_lines), page.name


def test_materialize_native_memory_writes_nothing_outside_root(tmp_path: Path) -> None:
    corpus = build_corpus("small")
    root = tmp_path / "sandbox"
    root.mkdir()

    materialize_native_memory(corpus, root, write_index=True)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert str(path).startswith(str(root)), path


# ---------------------------------------------------------------------------
# Runner entrypoint wiring — offline via injected stub client + pull_runner
# ---------------------------------------------------------------------------


def test_run_probe_all_arms_dispatches_all_eight_arms_offline(tmp_path: Path) -> None:
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "stub answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )

    def _stub_pull_runner(
        probe, knowledge_root, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub pull answer",
            recall_called=False,
        )

    def _stub_breadcrumb_context_fn(knowledge_root, hook_home, query) -> str:
        return "  - stub breadcrumb\n"

    def _stub_breadcrumb_pull_runner(
        probe, knowledge_root, hook_home, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub breadcrumb-pull answer",
            recall_called=False,
        )

    def _stub_native_index_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.NATIVE_INDEX,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native index answer",
        )

    def _stub_native_grep_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.NATIVE_GREP,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native grep answer",
        )

    records = run_probe_all_arms(
        "pto_allowance",
        "core",
        session=session,
        materialize_root=tmp_path,
        search_backend="keyword",
        client=client,
        mode="cli",
        pull_runner=_stub_pull_runner,
        breadcrumb_context_fn=_stub_breadcrumb_context_fn,
        breadcrumb_pull_runner=_stub_breadcrumb_pull_runner,
        native_index_runner=_stub_native_index_runner,
        native_grep_runner=_stub_native_grep_runner,
    )

    assert set(records) == {
        "none",
        "push_pages_upper_bound",
        "push_breadcrumb",
        "push_breadcrumb_pull",
        "oracle",
        "pull",
        "native_index",
        "native_grep",
    }
    for arm_value, record in records.items():
        assert record.arm.value == arm_value
        assert record.probe_id == "pto_allowance"
        assert record.corpus_scale == "core"
    assert records["pull"].answer == "stub pull answer"
    assert records["pull"].recall_called is False  # a legitimate, recorded choice
    assert records["push_breadcrumb_pull"].answer == "stub breadcrumb-pull answer"
    assert records["native_index"].answer == "stub native index answer"
    assert records["native_grep"].answer == "stub native grep answer"
    # No live subprocess was ever spawned -- the stubs stood in entirely.
    # No network call either: pull/breadcrumb/native runners are the only
    # arms that would spawn a subprocess, all three stubbed.


def test_run_probe_all_arms_native_arms_get_dedicated_subdirectories(tmp_path: Path) -> None:
    """NATIVE_INDEX and NATIVE_GREP must not share a materialize root — each
    writes its own ``memory/``, ``claude-config/`` and settings/mcp-config
    files, and both run within the SAME ``run_probe_all_arms`` call, so
    sharing a root would let one clobber the other's config."""
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "stub answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )
    seen_roots: dict[str, Path] = {}

    def _capturing_native_index_runner(probe, materialize_root, corpus_scale, **kwargs):
        seen_roots["native_index"] = materialize_root
        return RolloutRecord(
            arm=Arm.NATIVE_INDEX,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="a",
        )

    def _capturing_native_grep_runner(probe, materialize_root, corpus_scale, **kwargs):
        seen_roots["native_grep"] = materialize_root
        return RolloutRecord(
            arm=Arm.NATIVE_GREP,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="b",
        )

    def _stub_pull_runner(probe, knowledge_root, cache_dir, corpus_scale, **kwargs):
        return RolloutRecord(
            arm=Arm.PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="c",
        )

    def _stub_breadcrumb_pull_runner(
        probe, knowledge_root, hook_home, cache_dir, corpus_scale, **kwargs
    ):
        return RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="d",
        )

    run_probe_all_arms(
        "pto_allowance",
        "core",
        session=session,
        materialize_root=tmp_path,
        search_backend="keyword",
        client=client,
        mode="cli",
        pull_runner=_stub_pull_runner,
        breadcrumb_context_fn=lambda *a, **k: "",
        breadcrumb_pull_runner=_stub_breadcrumb_pull_runner,
        native_index_runner=_capturing_native_index_runner,
        native_grep_runner=_capturing_native_grep_runner,
    )

    assert seen_roots["native_index"] == tmp_path / "native_index"
    assert seen_roots["native_grep"] == tmp_path / "native_grep"
    assert seen_roots["native_index"] != seen_roots["native_grep"]


def test_pull_arm_receives_the_knowledge_root_not_the_wiki_root(tmp_path: Path) -> None:
    """PULL and PUSH_PAGES_UPPER_BOUND take DIFFERENT roots, and that
    asymmetry is load-bearing.

    ``recall_search`` (PUSH_PAGES_UPPER_BOUND) takes the wiki root directly,
    whereas PULL drives ``athenaeum serve --path``, which takes the
    KNOWLEDGE root and derives ``<path>/wiki`` and ``<path>/raw`` from it.
    An automated reviewer read the difference as a bug on issue
    athenaeum#1522's PR; it is not, and this test pins it so the "fix" that
    would actually break it -- handing ``serve`` the wiki root, leaving it
    looking for ``<root>/wiki/wiki`` and serving an empty corpus -- fails
    loudly instead of shipping. PUSH_BREADCRUMB(_PULL) matches PULL's
    choice (the knowledge root, not the wiki root — see
    ``build_push_breadcrumb_context``'s own ``knowledge_root`` param), pinned
    here too via the stubbed ``breadcrumb_context_fn``.
    """
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "stub answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )
    seen: dict[str, Path] = {}

    def _capturing_pull_runner(
        probe, knowledge_root, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        seen["knowledge_root"] = knowledge_root
        return RolloutRecord(
            arm=Arm.PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub pull answer",
        )

    def _capturing_breadcrumb_context_fn(knowledge_root, hook_home, query) -> str:
        seen["breadcrumb_knowledge_root"] = knowledge_root
        return ""

    def _stub_breadcrumb_pull_runner(
        probe, knowledge_root, hook_home, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        seen["breadcrumb_pull_knowledge_root"] = knowledge_root
        return RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub breadcrumb-pull answer",
        )

    # Stubbed too (Quine review, issue athenaeum#1733 SHOULD item 8): with no
    # native_index_runner/native_grep_runner override, mode="cli" falls
    # through to the REAL run_native_index/run_native_grep, which spawn a
    # genuine `claude -p` and read the operator's own `~/.claude.json` on any
    # host that happens to have the CLI installed -- exactly the network/home
    # -reading escape this offline suite must never have.
    def _stub_native_index_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.NATIVE_INDEX,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native index answer",
        )

    def _stub_native_grep_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=Arm.NATIVE_GREP,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native grep answer",
        )

    run_probe_all_arms(
        "pto_allowance",
        "core",
        session=session,
        materialize_root=tmp_path,
        search_backend="keyword",
        client=client,
        mode="cli",
        pull_runner=_capturing_pull_runner,
        breadcrumb_context_fn=_capturing_breadcrumb_context_fn,
        breadcrumb_pull_runner=_stub_breadcrumb_pull_runner,
        native_index_runner=_stub_native_index_runner,
        native_grep_runner=_stub_native_grep_runner,
    )

    # The knowledge root is the PARENT of the materialized wiki tree, and the
    # wiki tree really is where the corpus landed -- asserting both directions
    # so this cannot pass by both sides being wrong in the same way.
    assert seen["knowledge_root"] == tmp_path
    assert seen["breadcrumb_knowledge_root"] == tmp_path
    assert seen["breadcrumb_pull_knowledge_root"] == tmp_path
    assert (seen["knowledge_root"] / "wiki").is_dir()
    assert any((seen["knowledge_root"] / "wiki").glob("*.md"))
    assert not (seen["knowledge_root"] / "wiki" / "wiki").exists()


# ---------------------------------------------------------------------------
# Token ceiling separation — complements test_rollout_ceiling_separation.py
# by exercising the SEPARATION through rollout.py's own call path (run_none),
# not just bare EvalSession bookkeeping.
# ---------------------------------------------------------------------------


def test_rollout_arm_usage_never_touches_a_separate_eval_session(tmp_path: Path) -> None:
    component_eval_session = EvalSession()
    rollout_accumulator = EvalSession()
    probe, _corpus = _probe("pto_allowance")

    client = FakeLLMClient(
        response=make_llm_response(
            "answer", usage=make_llm_usage(input_tokens=EVAL_TOKEN_CEILING, output_tokens=1)
        )
    )

    run_none(probe, "core", client=client, session=rollout_accumulator, model="stub-model")

    assert rollout_accumulator.input_tokens == EVAL_TOKEN_CEILING
    assert component_eval_session.input_tokens == 0
    assert component_eval_session.output_tokens == 0
