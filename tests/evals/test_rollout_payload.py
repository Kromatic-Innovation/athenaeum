# SPDX-License-Identifier: Apache-2.0
"""``RolloutRecord.to_payload()``/``from_payload()`` round-trip (issue
athenaeum#1523's persistence bridge — the north-star report reads rollout
rows back out of ``tests.evals.containment.ResultStore``, so every field the
report consumes must survive a JSON round-trip losslessly).

NOT ``rollout``-marked (issue athenaeum#1742) — fully offline: no network
call, no subprocess spawn, no live client.
"""

from __future__ import annotations

import json

import pytest

from tests.evals.rollout import Arm, RolloutRecord, ToolCall, TurnTokenUsage


def _full_record() -> RolloutRecord:
    """A record with every field populated non-trivially — the emptiest
    default would round-trip a bug (e.g. ``[] == []``) without exercising
    it."""
    return RolloutRecord(
        arm=Arm.PULL,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="The PTO allowance is 20 days per **Uid:** policy-pto.",
        turn_tokens=[
            TurnTokenUsage(turn=1, input_tokens=120, output_tokens=40),
            TurnTokenUsage(turn=2, input_tokens=80, output_tokens=15),
        ],
        tool_calls=[ToolCall(name="mcp__athenaeum__recall", query="PTO allowance policy")],
        recall_called=True,
        injected_context_tokens=None,
        turn_count=2,
        transcript=[
            {"type": "system", "subtype": "init", "mcp_servers": [{"name": "athenaeum"}]},
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 120, "output_tokens": 40},
                    "content": [{"type": "tool_use", "name": "recall", "input": {"query": "x"}}],
                },
            },
        ],
    )


def test_to_payload_is_json_safe() -> None:
    payload = _full_record().to_payload()
    # Must survive an actual json.dumps/json.loads cycle, not just be a
    # plain dict of Python objects that happens to look serializable.
    reloaded = json.loads(json.dumps(payload))
    assert reloaded == payload


def test_round_trip_is_lossless_for_every_field() -> None:
    original = _full_record()
    payload = json.loads(json.dumps(original.to_payload()))
    restored = RolloutRecord.from_payload(payload)

    assert restored == original


@pytest.mark.parametrize(
    "arm",
    [
        Arm.NONE,
        Arm.PUSH_PAGES_UPPER_BOUND,
        Arm.PUSH_BREADCRUMB,
        Arm.PUSH_BREADCRUMB_PULL,
        Arm.ORACLE,
        Arm.PULL,
    ],
)
def test_round_trip_preserves_arm_enum_identity(arm: Arm) -> None:
    record = RolloutRecord(
        arm=arm,
        probe_id="p",
        probe_class="single_hop",
        corpus_scale="core",
        answer="a",
    )
    restored = RolloutRecord.from_payload(json.loads(json.dumps(record.to_payload())))
    assert restored.arm is arm


def test_from_payload_resolves_the_legacy_push_arm_value() -> None:
    """Issue athenaeum#1574 AC5: a payload persisted with the pre-rename
    ``"arm": "push"`` string (exactly what ``RolloutRecord.to_payload()``
    wrote before this issue) must still deserialize -- to the arm that
    value actually named, ``push_pages_upper_bound`` -- not raise
    ``ValueError``."""
    payload = {
        "arm": "push",
        "probe_id": "p",
        "probe_class": "single_hop",
        "corpus_scale": "core",
        "answer": "a",
    }
    restored = RolloutRecord.from_payload(json.loads(json.dumps(payload)))
    assert restored.arm is Arm.PUSH_PAGES_UPPER_BOUND


def test_round_trip_preserves_empty_defaults() -> None:
    """The emptiest legal record (NONE/PUSH/ORACLE shape: no tool calls, no
    recall) must round-trip its empty lists/None fields exactly, not
    coerce them to a different falsy value."""
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id="p",
        probe_class="abstention",
        corpus_scale="core",
        answer="I don't know.",
    )
    restored = RolloutRecord.from_payload(json.loads(json.dumps(record.to_payload())))

    assert restored.turn_tokens == []
    assert restored.tool_calls == []
    assert restored.recall_called is False
    assert restored.injected_context_tokens is None
    assert restored.turn_count == 0
    assert restored.transcript == []
    assert restored == record


def test_round_trip_preserves_hybrid_active() -> None:
    """Issue athenaeum#1816: ``hybrid_active`` (whether the vector backend's
    RRF fusion had a real FTS5 index to fuse against) must round-trip
    losslessly for BOTH non-default values, not just survive as ``None``
    the way ``test_round_trip_is_lossless_for_every_field`` already covers.
    """
    for value in (True, False):
        record = RolloutRecord(
            arm=Arm.PULL,
            probe_id="p",
            probe_class="single_hop",
            corpus_scale="medium",
            answer="a",
            search_backend="vector",
            hybrid_active=value,
        )
        restored = RolloutRecord.from_payload(json.loads(json.dumps(record.to_payload())))
        assert restored.hybrid_active is value
        assert restored == record


def test_from_payload_defaults_hybrid_active_for_a_pre_1816_payload() -> None:
    """A row persisted before issue athenaeum#1816 carries no
    ``hybrid_active`` key at all -- must decode as ``None`` ("unknown/not
    applicable"), never raise ``KeyError``."""
    payload = _full_record().to_payload()
    del payload["hybrid_active"]

    restored = RolloutRecord.from_payload(payload)

    assert restored.hybrid_active is None


def test_from_payload_ignores_extra_persisted_keys() -> None:
    """A stored ResultStore row carries ``cell_key``/``replicate`` alongside
    the record's own fields (see
    ``tests.evals.north_star_report.append_rollout_row``) — decoding must
    not choke on those extra keys."""
    payload = _full_record().to_payload()
    payload["cell_key"] = '["pto_allowance","pull","core",0]'
    payload["replicate"] = 0

    restored = RolloutRecord.from_payload(payload)

    assert restored == _full_record()
