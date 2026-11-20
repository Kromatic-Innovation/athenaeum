# SPDX-License-Identifier: Apache-2.0
"""Raise message codec (athenaeum#2937)."""

from __future__ import annotations

import json

import pytest

from athenaeum import raise_messages as rm
from athenaeum.corrections import KNOWN_SCHEMA_VERSIONS
from athenaeum.raise_messages import Fallthrough, ParsedBatch, RaiseMessage
from athenaeum.raise_projection import mint_message_id, mint_raise_id

PROPOSER = {"iss": "https://git.example.net", "sub": "1203"}
HASH = "sha256:" + "a" * 64
NOW = "2026-11-18T09:30:00Z"
PAGE = {"type": "principle", "name": "Year-end report", "tags": ["policy"], "aliases": []}


def _base(kind: str, **kw: object) -> RaiseMessage:
    args: dict[str, object] = dict(
        kind=kind,
        message_id=mint_message_id(),
        origin="st_f2n6rb5hjc3t",
        target="st_q7m2xk4vwz5d",
        proposer=dict(PROPOSER),
        sent_at=NOW,
        raise_id=mint_raise_id(),
        seq=1,
        via="proposer",
    )
    args.update(kw)
    return RaiseMessage(**args)  # type: ignore[arg-type]


def _all() -> list[RaiseMessage]:
    return [
        _base("raise", content_hash=HASH, page=dict(PAGE), body="Text."),
        _base(
            "propose-fact",
            via="intake",
            content_hash=HASH,
            fact={"type": "person", "tags": ["hr"]},
            body="Sam moves.",
        ),
        _base("supersede", seq=2, content_hash=HASH, page=dict(PAGE), body="New."),
        _base("supersede", seq=2, successor=mint_raise_id()),
        _base("retract", seq=2, reason="erasure"),
        _base("lapse", seq=3, via="review:weekly"),
        rm.RaiseMessage(
            kind="attest",
            message_id=mint_message_id(),
            origin="st_f2n6rb5hjc3t",
            target="st_q7m2xk4vwz5d",
            proposer=dict(PROPOSER),
            sent_at=NOW,
        ),
    ]


def _batch(*records: object) -> str:
    env = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "raise",
        "batch_id": "20261118T093000Z-9f3ac1d2",
        "created_at": NOW,
    }
    return "\n".join(json.dumps(r) for r in (env, *records)) + "\n"


def _rec(msg: RaiseMessage) -> dict[str, object]:
    return json.loads(rm.render_message(msg))


@pytest.mark.parametrize("msg", _all(), ids=lambda m: m.kind)
def test_each_kind_round_trips(msg: RaiseMessage) -> None:
    parsed = rm.parse_message(json.loads(rm.render_message(msg)))
    assert parsed == msg
    batch = rm.parse_batch(rm.render_batch([msg], batch_id="b-1", created_at=NOW))
    assert isinstance(batch, ParsedBatch)
    assert batch.messages == (msg,)


def test_batch_round_trip_all_kinds_uses_existing_envelope() -> None:
    msgs = _all()
    text = rm.render_batch(msgs, batch_id="20261118T093000Z-9f3ac1d2", created_at=NOW)
    env = json.loads(text.split("\n", 1)[0])
    assert env["record"] == "batch"
    assert env["schema_version"] in KNOWN_SCHEMA_VERSIONS
    assert set(env) == {"record", "schema_version", "submitter", "batch_id", "created_at"}
    parsed = rm.parse_batch(text)
    assert isinstance(parsed, ParsedBatch)
    assert list(parsed.messages) == msgs


@pytest.mark.parametrize("reason", sorted(rm.RETRACT_REASONS))
def test_retract_accepts_each_fixed_reason(reason: str) -> None:
    rec = _rec(_base("retract", reason=reason))
    assert isinstance(rm.parse_message(rec), RaiseMessage)


@pytest.mark.parametrize("reason", ["", "because", "Withdrawn", None, 3, "withdrawn "])
def test_retract_refuses_free_text_reason(reason: object) -> None:
    rec = _rec(_base("retract", reason="withdrawn"))
    rec["reason"] = reason
    assert isinstance(rm.parse_message(rec), Fallthrough)


@pytest.mark.parametrize(
    "bad",
    ["rs_" + "a" * 19, "rs_" + "a" * 21, "rs_" + "A" * 20, "rs_" + "1" * 20, "rm_" + "a" * 20, ""],
)
def test_raise_id_validated(bad: str) -> None:
    rec = _rec(_base("lapse"))
    rec["raise_id"] = bad
    assert isinstance(rm.parse_message(rec), Fallthrough)


@pytest.mark.parametrize(
    "bad", ["rm_" + "a" * 19, "rm_" + "a" * 21, "RM_" + "a" * 20, "rs_" + "a" * 20]
)
def test_message_id_validated(bad: str) -> None:
    rec = _rec(_base("lapse"))
    rec["message_id"] = bad
    assert isinstance(rm.parse_message(rec), Fallthrough)


def test_supersede_needs_payload_xor_successor() -> None:
    both = _rec(_base("supersede", seq=2, content_hash=HASH, page=dict(PAGE), body="x"))
    both["successor"] = mint_raise_id()
    assert isinstance(rm.parse_message(both), Fallthrough)
    neither = _rec(_base("supersede", seq=2, successor=mint_raise_id()))
    del neither["successor"]
    assert isinstance(rm.parse_message(neither), Fallthrough)
    partial = _rec(_base("supersede", seq=2, content_hash=HASH, page=dict(PAGE), body="x"))
    del partial["body"]
    assert isinstance(rm.parse_message(partial), Fallthrough)


def test_supersede_needs_higher_seq_and_real_successor() -> None:
    assert isinstance(
        rm.parse_message(_rec(_base("supersede", seq=2, successor=mint_raise_id()))), RaiseMessage
    )
    low = _rec(_base("supersede", seq=2, content_hash=HASH, page=dict(PAGE), body="x"))
    low["seq"] = 1
    assert isinstance(rm.parse_message(low), Fallthrough)
    bad = _rec(_base("supersede", seq=2, successor=mint_raise_id()))
    bad["successor"] = "nope"
    assert isinstance(rm.parse_message(bad), Fallthrough)
    same = _rec(_base("supersede", seq=2, successor=mint_raise_id()))
    same["successor"] = same["raise_id"]
    assert isinstance(rm.parse_message(same), Fallthrough)


def test_raise_must_be_seq_one() -> None:
    rec = _rec(_base("raise", content_hash=HASH, page=dict(PAGE), body="x"))
    rec["seq"] = 2
    assert isinstance(rm.parse_message(rec), Fallthrough)


# -- counter-examples: fallthrough, never an exception -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "not json\n",
        "[]\n",
        '{"record":"raise"}\n',
        '{"record":"batch","schema_version":99,"batch_id":"b","created_at":"t"}\n',
        '{"record":"batch","schema_version":1,"created_at":"t"}\n',
        '{"record":"batch","schema_version":true,"batch_id":"b","created_at":"t"}\n',
    ],
)
def test_no_valid_envelope_is_fallthrough(text: str) -> None:
    assert isinstance(rm.parse_batch(text), Fallthrough)


def test_unknown_record_kind_is_fallthrough() -> None:
    res = rm.parse_batch(_batch({"record": "correction", "x": 1}))
    assert isinstance(res, Fallthrough)
    assert res.line == 2


def test_unknown_key_is_fallthrough() -> None:
    rec = _rec(_base("lapse"))
    rec["typo"] = 1
    res = rm.parse_batch(_batch(rec))
    assert isinstance(res, Fallthrough)


def test_one_bad_record_falls_the_whole_file_through() -> None:
    good = _rec(_base("lapse"))
    res = rm.parse_batch(_batch(good, {"record": "wat"}))
    assert isinstance(res, Fallthrough) and res.line == 3


@pytest.mark.parametrize("junk", ["{", "null", "3", '"s"', "[1]", "{}"])
def test_malformed_record_lines_do_not_raise(junk: str) -> None:
    env = json.dumps({"record": "batch", "schema_version": 1, "batch_id": "b", "created_at": NOW})
    assert isinstance(rm.parse_batch(env + "\n" + junk + "\n"), Fallthrough)


def test_envelope_only_batch_is_conformant_and_empty() -> None:
    res = rm.parse_batch(_batch())
    assert isinstance(res, ParsedBatch) and res.messages == ()


def test_corrections_batch_is_not_a_raise_batch() -> None:
    assert isinstance(rm.parse_batch(_batch({"record": "correction"})), Fallthrough)


# -- source naming is refused -------------------------------------------------


@pytest.mark.parametrize(
    "key", ["uid", "path", "revision", "source_hash", "source_uid", "source_path", "sources"]
)
def test_source_naming_top_level_refused(key: str) -> None:
    rec = _rec(_base("lapse"))
    rec[key] = "x"
    res = rm.parse_message(rec)
    assert isinstance(res, Fallthrough) and "source page" in res.reason


@pytest.mark.parametrize("key", ["uid", "path", "revision", "source_hash"])
def test_source_naming_nested_refused(key: str) -> None:
    rec = _rec(_base("raise", content_hash=HASH, page=dict(PAGE), body="x"))
    rec["page"] = {**PAGE, key: "x"}  # type: ignore[dict-item]
    res = rm.parse_message(rec)
    assert isinstance(res, Fallthrough) and "source page" in res.reason
    prop = _rec(_base("lapse"))
    prop["proposer"] = {**PROPOSER, key: "x"}  # type: ignore[dict-item]
    assert isinstance(rm.parse_message(prop), Fallthrough)


def test_render_refuses_a_bad_message() -> None:
    with pytest.raises(ValueError):
        rm.render_message(_base("retract", reason="because I said so"))
    with pytest.raises(ValueError):
        rm.render_batch([], batch_id="", created_at=NOW)


def test_attest_carries_no_raise_id() -> None:
    rec = _rec(_all()[-1])
    assert "raise_id" not in rec
    rec["raise_id"] = mint_raise_id()
    assert isinstance(rm.parse_message(rec), Fallthrough)
