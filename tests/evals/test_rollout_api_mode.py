# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the API-backed tool-use mode (issue athenaeum#1733).

UNMARKED — runs in ``ci.yml``'s default job. Every test here replays a
stub client: no network call, no subprocess spawn, no token spent, so it
carries no deselecting marker (issue athenaeum#1742 — ``rollout`` means
token cost, not module family; see ``tests/evals/README.md``).

Two of the three tests replay a pre-scripted (\"recorded\") sequence of
Anthropic Messages API responses through a queued stub client
(:class:`_QueuedApiClient`) rather than ``tests.evals.harness.replay_client``
-- that helper enforces a single-response prompt-hash contract and cannot
model a multi-turn tool-use loop. The stub's response objects are
attribute-shaped (``block.type``/``.name``/``.input``/``.text``,
``response.stop_reason``, ``response.usage``) to match what
:func:`tests.evals.rollout._api_response_blocks` and
:meth:`tests.evals.harness.EvalSession.observe_response` actually read off a
real SDK response -- the same discipline ``harness.py``'s own
``_ReplayBlock`` uses for its single-response replay.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    NATIVE_INDEX_MAX_CHARS,
    NATIVE_INDEX_MAX_LINES,
    READ_ENTITY_TOOL_NAME,
    RECALL_TOOL_NAME,
    materialize_native_memory,
    run_native_grep_api,
    run_pull_api,
    run_push_breadcrumb_pull_api,
    truncate_native_index,
)

# ---------------------------------------------------------------------------
# Queued stub client -- a "recorded" turn-by-turn response sequence
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(*, id: str, name: str, input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _usage(input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


@dataclasses.dataclass
class _RecordedTurn:
    """One "recorded" turn in a scripted api-mode transcript."""

    content: list[SimpleNamespace]
    stop_reason: str
    usage: SimpleNamespace = dataclasses.field(default_factory=_usage)


class _QueuedApiClient:
    """A ``client.messages.create(**params)`` stub that returns a fixed,
    pre-scripted sequence of responses in order -- the queued-turn analogue
    of :func:`tests.evals.harness.replay_client` for a multi-turn loop.
    Raises ``AssertionError`` if the loop asks for more turns than were
    recorded (a scripting bug in the test, never a silent pass)."""

    def __init__(self, turns: list[_RecordedTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def create(inner_self, **params: Any) -> SimpleNamespace:
                self.calls.append(params)
                assert self._turns, "queued client exhausted -- recorded too few turns"
                turn = self._turns.pop(0)
                return SimpleNamespace(
                    content=turn.content, usage=turn.usage, stop_reason=turn.stop_reason
                )

        self.messages = _Messages()


# ---------------------------------------------------------------------------
# 1. Truncation rule, pinned against the `medium` corpus
# ---------------------------------------------------------------------------


def test_truncate_native_index_pins_the_200_line_25kb_cap_at_medium_scale(
    tmp_path: Path,
) -> None:
    """`medium` (1,000 pages, per docs/design/native-memory-baseline.md §3)
    is well past the documented cap, so the written index is expected to
    overflow both limits and truncation must actually engage -- the
    "truncation is the finding, not a confound" case the design doc names.
    """
    corpus = build_corpus("medium")
    memory_dir = materialize_native_memory(corpus, tmp_path, write_index=True)
    written = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")

    # The full index really does overflow both caps at this scale -- if it
    # did not, this test would be pinning a truncation that never engages.
    # Counted by Python str length (Quine review, issue athenaeum#1733), NOT
    # UTF-8 encoded bytes -- the actual cap truncate_native_index enforces;
    # this corpus's index lines contain an em dash (3 UTF-8 bytes, 1 char),
    # so a byte-encoded assertion here would test a DIFFERENT cap than the
    # one the function actually applies.
    assert written.count("\n- ") + (1 if written.startswith("- ") else 0) > NATIVE_INDEX_MAX_LINES
    assert len(written) > NATIVE_INDEX_MAX_CHARS

    truncated = truncate_native_index(written)

    bullet_lines = [line for line in truncated.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) <= NATIVE_INDEX_MAX_LINES
    assert len(truncated) <= NATIVE_INDEX_MAX_CHARS
    # Never a partial line: every line in the truncated text is a COMPLETE
    # line that also appears, verbatim, in the untruncated index.
    written_lines = set(written.splitlines())
    for line in truncated.splitlines():
        assert line in written_lines
    # And it is a genuine PREFIX of the written index (whole lines dropped
    # off the end, never reordered or altered).
    assert (
        written.startswith(truncated.rstrip("\n"))
        or written.splitlines()[: len(truncated.splitlines())] == truncated.splitlines()
    )


def test_truncate_native_index_is_a_no_op_under_the_cap() -> None:
    small_index = "- a — b\n- c — d\n"
    assert truncate_native_index(small_index) == small_index


# ---------------------------------------------------------------------------
# 2. Recorded-fixture NATIVE_GREP replay, end to end
# ---------------------------------------------------------------------------


def test_native_grep_api_recorded_fixture_replay_end_to_end(tmp_path: Path) -> None:
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    target_filename = f"{probe.expected_uids[0]}.md"

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(id="toolu_grep_1", name="grep", input={"pattern": "PTO allowance"})
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[
                _tool_use_block(id="toolu_read_1", name="read", input={"path": target_filename})
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[_text_block("The PTO allowance is 25 days per year.")],
            stop_reason="end_turn",
        ),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    record = run_native_grep_api(
        probe, tmp_path, "core", client=client, session=session, model="test-model"
    )

    assert record.mode == "api"
    assert record.answer == "The PTO allowance is 25 days per year."
    assert record.turn_count == 3
    assert [c.name for c in record.tool_calls] == ["grep", "read"]
    # No "query" key on the grep tool's input, so ToolCall.query falls back
    # to the stringified input dict -- the SAME extraction
    # `tests.evals.rollout.parse_stream` uses for a non-recall tool_use
    # block (`str(tool_input.get("query", tool_input))`).
    assert record.tool_calls[0].query == str({"pattern": "PTO allowance"})
    # The read tool actually found and returned the real materialized page.
    native_memory = record.transcript[0]["native_memory"]
    assert target_filename in native_memory["loaded_memory_files"]
    assert "25 days" in native_memory["loaded_memory_files"][target_filename]
    # Round-trips through to_payload/from_payload with mode preserved.
    from tests.evals.rollout import RolloutRecord

    round_tripped = RolloutRecord.from_payload(record.to_payload())
    assert round_tripped.mode == "api"
    assert round_tripped.answer == record.answer
    # Quine review, issue athenaeum#1733 SHOULD item 3: the native arm's
    # system prompt must mention the memory directory (never the single-shot
    # arms' "answer using only the context supplied" prompt).
    sent_system = client.calls[0]["system"]
    assert str(tmp_path) in sent_system


# ---------------------------------------------------------------------------
# 3. Recorded-fixture PULL rollout: a recall tool call with its query captured
# ---------------------------------------------------------------------------


def test_pull_api_recorded_fixture_shows_recall_tool_call_with_query_captured(
    tmp_path: Path,
) -> None:
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    wiki_root = corpus.materialize(tmp_path)
    cache_dir = tmp_path / "cache"

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_recall_1",
                    name=RECALL_TOOL_NAME,
                    input={"query": "PTO allowance days per year"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[_text_block("The PTO allowance is 25 days per year.")],
            stop_reason="end_turn",
        ),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    record = run_pull_api(
        probe,
        wiki_root,
        cache_dir,
        "core",
        client=client,
        session=session,
        model="test-model",
        search_backend="keyword",
    )

    assert record.mode == "api"
    assert record.recall_called is True
    assert len(record.tool_calls) == 1
    call = record.tool_calls[0]
    assert call.name == RECALL_TOOL_NAME
    assert call.query == "PTO allowance days per year"
    assert record.answer == "The PTO allowance is 25 days per year."

    # The transcript matches the SAME stream-json-derived shape the CLI path
    # produces -- north_star_report._pull_delivered_text scans exactly this
    # shape and must find the recall tool's own delivered content.
    from tests.evals.north_star_report import _pull_delivered_text

    delivered = _pull_delivered_text(record)
    assert delivered, f"expected non-empty delivered text; transcript={record.transcript!r}"
    # Quine review, issue athenaeum#1733 SHOULD item 3: the PULL-style system
    # prompt must mention the recall tool (never the single-shot arms'
    # "answer using only the context supplied" prompt, which actively
    # discourages calling one).
    sent_system = client.calls[0]["system"]
    assert "recall" in sent_system
    # Issue athenaeum#1756: and the SECOND tool the arm now serves. A prompt
    # that offers `read_entity` in `tools=` but never mentions it is how a
    # model ends up never reaching for it.
    assert "read_entity" in sent_system


# ---------------------------------------------------------------------------
# 4. Recorded-fixture PULL rollout reaching a tag recall's snippet cannot show
#    (issue athenaeum#1756)
# ---------------------------------------------------------------------------


def _tool_result_texts(record, tool_use_id: str) -> str:
    """The delivered text of ONE tool_result block, read out of the transcript
    by its ``tool_use_id`` -- so the assertions below are about what each tool
    actually returned, not about what the scripted final answer happened to
    say."""
    for event in record.transcript:
        if event.get("type") != "user":
            continue
        for block in event["message"]["content"]:
            if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id:
                return str(block["content"])
    raise AssertionError(f"no tool_result for {tool_use_id!r} in transcript")


def test_pull_api_reaches_a_reference_tag_recall_alone_cannot_show(tmp_path: Path) -> None:
    """Issue athenaeum#1756, the whole point of serving ``read_entity``.

    ``person_not_repo``'s planted tag (``Ashcaldera``) sits on the last line
    of a 587-character page, outside the 400-character window
    ``athenaeum.mcp_server._snippet`` gives every ``recall`` hit. Under the
    reference-tag grading contract (issue athenaeum#1753) an arm can only
    grade correct by citing that tag -- so before this issue, api-mode PULL
    could not answer this probe correctly however good its retrieval was.

    Every rendering here is the REAL one: the core corpus materialized to
    disk, the real FTS5 index, the real ``recall_search``, and the real
    ``entity_read``. Only the model's turns are scripted.
    """
    from athenaeum.search import get_backend
    from tests.evals.north_star_report import grade_correctness

    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "person_not_repo")
    tag = probe.answer_tokens[0]
    assert tag == "Ashcaldera"  # read off the corpus, not assumed

    wiki_root = corpus.materialize(tmp_path)
    cache_dir = tmp_path / "cache"
    # Without this the fts5 backend has no index and returns "No wiki pages
    # matched" for every query -- the shape assertions below would still pass
    # and prove nothing.
    get_backend("fts5").build_index(wiki_root, cache_dir)

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_recall_1",
                    name=RECALL_TOOL_NAME,
                    input={"query": "Rowan Wrenfield pricing decision"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_read_1",
                    name=READ_ENTITY_TOOL_NAME,
                    input={"uid": "person-rowan-wrenfield", "entity_class": "person"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[
                _text_block(
                    "Rowan Wrenfield, who sits on the pricing committee, decided to hold "
                    "the day rate flat through FY2026 and absorb the indirect cost "
                    f"increase.\n\n[ref: {tag}]"
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = _QueuedApiClient(turns)

    record = run_pull_api(
        probe,
        wiki_root,
        cache_dir,
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="fts5",
    )

    assert [c.name for c in record.tool_calls] == [RECALL_TOOL_NAME, READ_ENTITY_TOOL_NAME]

    recall_result = _tool_result_texts(record, "toolu_recall_1")
    read_entity_result = _tool_result_texts(record, "toolu_read_1")
    # The premise: recall DID find the right page -- and still could not show
    # the tag, because the snippet window cuts the page short.
    assert "Rowan Wrenfield" in recall_result
    assert tag not in recall_result
    assert tag in read_entity_result

    assert grade_correctness(record, probe, corpus) is True


# ---------------------------------------------------------------------------
# 5. The SECOND api-mode PULL arm serves read_entity too (issue athenaeum#1756)
# ---------------------------------------------------------------------------


def test_push_breadcrumb_pull_api_serves_read_entity_in_process(tmp_path: Path) -> None:
    """``run_push_breadcrumb_pull_api`` builds its OWN executor and ``tools=``
    list, so serving ``read_entity`` there is a separate fact from serving it
    in :func:`run_pull_api` -- pinned here END TO END rather than only at the
    offered-tool-surface seam: the tool_result this arm delivers must be the
    real page ``entity_read`` returns.

    Drop the ``READ_ENTITY_TOOL_NAME`` branch from that arm's executor and the
    call falls through to its ``unknown tool`` string, which carries none of
    the page -- this test fails.
    """
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "person_not_repo")
    tag = probe.answer_tokens[0]

    knowledge_root = tmp_path / "knowledge"
    corpus.materialize(knowledge_root)

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_read_1",
                    name=READ_ENTITY_TOOL_NAME,
                    input={"uid": "person-rowan-wrenfield", "entity_class": "person"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[_text_block(f"Rowan held the day rate flat.\n\n[ref: {tag}]")],
            stop_reason="end_turn",
        ),
    ]
    client = _QueuedApiClient(turns)

    record = run_push_breadcrumb_pull_api(
        probe,
        knowledge_root,
        tmp_path / "hook-home",
        tmp_path / "cache",
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="keyword",
        # The breadcrumb assembly is run_push_breadcrumb_pull's own contract
        # (and its own tests') -- stubbed so this test pins only the tool.
        context_fn=lambda *_args: "",
    )

    assert [c.name for c in record.tool_calls] == [READ_ENTITY_TOOL_NAME]
    # This arm prepends its breadcrumb event, which carries no "type" key --
    # asserted rather than assumed, since it is the only reason the
    # transcript walk below skips it instead of raising.
    assert "type" not in record.transcript[0]
    assert "pushed_context" in record.transcript[0]
    delivered = _tool_result_texts(record, "toolu_read_1")
    # The real `entity_read` rendering of the real page: its uid, its body,
    # and the tag that lives on its last line.
    assert "person-rowan-wrenfield" in delivered
    assert "Wrenfield Associates" in delivered
    assert tag in delivered
    assert "unknown tool" not in delivered
