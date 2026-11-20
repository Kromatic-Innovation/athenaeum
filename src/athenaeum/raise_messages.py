# SPDX-License-Identifier: Apache-2.0
"""The raise message codec: parse, validate and render six record kinds (athenaeum#2937).

A raise-protocol message is a record in the one conformance format, the
envelope-first JSONL batch of ``docs/design/field-corrections.md`` §3
(``docs/design/federation-raise-retract.md`` §6.1, §14 slice S2). This module
adds record kinds to that format and nothing else: the envelope check is
:func:`athenaeum.corrections.parse_batch_envelope` (its single definition of a
valid envelope and its one ``schema_version`` rule), and ids are validated with
:func:`athenaeum.raise_projection.is_raise_id` and
:func:`athenaeum.raise_projection.is_message_id`. There is no envelope, version
key or file-name rule of its own here.

The six kinds are ``raise``, ``propose-fact``, ``supersede``, ``retract``,
``lapse`` and ``attest``. A retract's ``reason`` is one of the fixed
:data:`RETRACT_REASONS`; no free-text reason is accepted. No message may name
the source page: a field called ``uid``, ``path``, ``revision`` or
``source_hash`` (and the other names in :data:`SOURCE_NAMING_KEYS`) is refused
at any depth.

**Nothing here raises on input.** A file or record that does not conform (no
valid envelope, an unknown record kind, an unknown key, a bad id, a refused
field) comes back as a :class:`Fallthrough`, which the caller routes up the
ladder (``field-corrections.md`` §8). Only :func:`render_message` and
:func:`render_batch` raise, and only :class:`ValueError` for a message the
caller built wrongly.

Pure: no file access, no git, no model call.

Layering: L2. Imports :mod:`athenaeum.corrections` (L2),
:mod:`athenaeum.raise_projection` (L2) and the standard library; nothing above.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from athenaeum.corrections import KNOWN_SCHEMA_VERSIONS, parse_batch_envelope
from athenaeum.raise_projection import PROJECTED_KEYS, is_message_id, is_raise_id

#: The retract reason codes (§6.1). A sentence about the source can quote it,
#: so there is no free-text reason.
RETRACT_REASONS: frozenset[str] = frozenset(
    {
        "withdrawn",
        "source_removed",
        "source_merged",
        "superseded_at_source",
        "no_longer_selected",
        "erasure",
    }
)

KIND_RAISE = "raise"
KIND_PROPOSE_FACT = "propose-fact"
KIND_SUPERSEDE = "supersede"
KIND_RETRACT = "retract"
KIND_LAPSE = "lapse"
KIND_ATTEST = "attest"

#: Every record kind this codec knows.
RECORD_KINDS: frozenset[str] = frozenset(
    {KIND_RAISE, KIND_PROPOSE_FACT, KIND_SUPERSEDE, KIND_RETRACT, KIND_LAPSE, KIND_ATTEST}
)

#: Field names that would name the source page. Refused at any depth (§6.1:
#: "no uid, path, revision or source hash crosses").
SOURCE_NAMING_KEYS: frozenset[str] = frozenset(
    {
        "uid",
        "path",
        "revision",
        "source_hash",
        "source_uid",
        "source_path",
        "source_revision",
        "source",
        "sources",
        "raised_from",
    }
)

#: Keys of the ``via`` values a message may carry besides ``review:<name>``.
_PLAIN_VIA = frozenset({"proposer", "intake"})

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_HEADER_KEYS = (
    "record",
    "raise_id",
    "seq",
    "message_id",
    "origin",
    "target",
    "proposer",
    "via",
    "sent_at",
)
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    KIND_RAISE: frozenset(_HEADER_KEYS + ("content_hash", "page", "body")),
    KIND_PROPOSE_FACT: frozenset(_HEADER_KEYS + ("content_hash", "fact", "body")),
    KIND_SUPERSEDE: frozenset(_HEADER_KEYS + ("content_hash", "page", "body", "successor")),
    KIND_RETRACT: frozenset(_HEADER_KEYS + ("reason",)),
    KIND_LAPSE: frozenset(_HEADER_KEYS),
    KIND_ATTEST: frozenset({"record", "message_id", "origin", "target", "proposer", "sent_at"}),
}
_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    KIND_RAISE: frozenset(_HEADER_KEYS + ("content_hash", "page", "body")),
    KIND_PROPOSE_FACT: frozenset(_HEADER_KEYS + ("content_hash", "fact", "body")),
    KIND_SUPERSEDE: frozenset(_HEADER_KEYS),
    KIND_RETRACT: frozenset(_HEADER_KEYS + ("reason",)),
    KIND_LAPSE: frozenset(_HEADER_KEYS),
    KIND_ATTEST: _ALLOWED_KEYS[KIND_ATTEST],
}

_FACT_KEYS = frozenset({"type", "tags"})


@dataclass(frozen=True)
class Fallthrough:
    """A message or file that does not conform. Never an error: route it up the ladder."""

    reason: str
    line: int | None = None  #: 1-based line in the file, when known


@dataclass(frozen=True)
class RaiseMessage:
    """One parsed message. Fields a kind does not carry are ``None``."""

    kind: str
    message_id: str
    origin: str
    target: str
    proposer: dict[str, str]
    sent_at: str
    raise_id: str | None = None
    seq: int | None = None
    via: str | None = None
    content_hash: str | None = None
    page: dict[str, Any] | None = None
    fact: dict[str, Any] | None = None
    body: str | None = None
    reason: str | None = None
    successor: str | None = None


@dataclass(frozen=True)
class ParsedBatch:
    """A conformant file: its envelope and its messages, in file order."""

    envelope: dict[str, Any]
    messages: tuple[RaiseMessage, ...] = field(default_factory=tuple)


def _is_str(v: object) -> bool:
    return isinstance(v, str) and bool(v)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _find_source_naming_key(obj: object) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SOURCE_NAMING_KEYS:
                return str(k)
            hit = _find_source_naming_key(v)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_source_naming_key(v)
            if hit is not None:
                return hit
    return None


def _valid_time(v: object) -> bool:
    if not isinstance(v, str) or _TIME_RE.match(v) is None:
        return False
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_proposer(v: object) -> bool:
    return (
        isinstance(v, dict) and set(v) == {"iss", "sub"} and _is_str(v["iss"]) and _is_str(v["sub"])
    )


def _valid_via(v: object) -> bool:
    if not isinstance(v, str):
        return False
    if v in _PLAIN_VIA:
        return True
    return v.startswith("review:") and len(v) > len("review:") and v.strip() == v


def _valid_page(v: object) -> bool:
    if not isinstance(v, dict) or not set(v) <= set(PROJECTED_KEYS):
        return False
    for k, val in v.items():
        if k in ("aliases", "tags"):
            ok = _str_list(val)
        elif k == "audience":
            ok = isinstance(val, str) or _str_list(val)
        else:
            ok = isinstance(val, str)
        if not ok:
            return False
    return True


def _valid_fact(v: object) -> bool:
    if not isinstance(v, dict) or not set(v) <= _FACT_KEYS:
        return False
    if "type" in v and not isinstance(v["type"], str):
        return False
    return "tags" not in v or _str_list(v["tags"])


def _check_record(rec: dict[str, Any]) -> str | None:
    """The reason *rec* does not conform, or ``None`` when it does."""
    kind = rec.get("record")
    if not isinstance(kind, str) or kind not in RECORD_KINDS:
        return f"unknown record kind: {kind!r}"
    named = _find_source_naming_key(rec)
    if named is not None:
        return f"field names the source page: {named!r}"
    unknown = sorted(set(rec) - _ALLOWED_KEYS[kind])
    if unknown:
        return f"unknown key(s) on {kind} record: {unknown}"
    missing = sorted(_REQUIRED_KEYS[kind] - set(rec))
    if missing:
        return f"missing key(s) on {kind} record: {missing}"
    if not is_message_id(rec["message_id"]):
        return "message_id is not a prefix plus 20 lowercase base32 characters"
    for key in ("origin", "target"):
        if not _is_str(rec[key]):
            return f"{key} is not a non-empty string"
    if not _valid_proposer(rec["proposer"]):
        return "proposer is not an object with exactly iss and sub"
    if not _valid_time(rec["sent_at"]):
        return "sent_at is not an RFC-3339 UTC time"
    if kind == KIND_ATTEST:
        return None
    if not is_raise_id(rec["raise_id"]):
        return "raise_id is not a prefix plus 20 lowercase base32 characters"
    if not _is_int(rec["seq"]) or rec["seq"] < 1:
        return "seq is not a positive integer"
    if not _valid_via(rec["via"]):
        return "via is not proposer, intake or review:<name>"
    if "content_hash" in rec and (
        not isinstance(rec["content_hash"], str) or _HASH_RE.match(rec["content_hash"]) is None
    ):
        return "content_hash is not sha256: plus 64 hex characters"
    if "body" in rec and not isinstance(rec["body"], str):
        return "body is not a string"
    if "page" in rec and not _valid_page(rec["page"]):
        return "page is not a projection mapping"
    if "fact" in rec and not _valid_fact(rec["fact"]):
        return "fact is not a type and tags mapping"
    if kind in (KIND_RAISE, KIND_PROPOSE_FACT) and rec["seq"] != 1:
        return f"{kind} must carry seq 1"
    if kind == KIND_RETRACT and rec["reason"] not in RETRACT_REASONS:
        return "retract reason is not one of the fixed reason codes"
    if kind == KIND_SUPERSEDE:
        if rec["seq"] < 2:
            return "supersede must carry a higher seq (2 or more)"
        payload = [k for k in ("content_hash", "page", "body") if k in rec]
        if "successor" in rec:
            if payload:
                return "supersede carries either a payload or a successor, not both"
            if not is_raise_id(rec["successor"]):
                return "successor is not a raise_id"
            if rec["successor"] == rec["raise_id"]:
                return "successor names the superseded raise"
        elif len(payload) != 3:
            return "supersede carries neither a whole payload nor a successor"
    return None


def _to_message(rec: dict[str, Any]) -> RaiseMessage:
    return RaiseMessage(
        kind=rec["record"],
        message_id=rec["message_id"],
        origin=rec["origin"],
        target=rec["target"],
        proposer=dict(rec["proposer"]),
        sent_at=rec["sent_at"],
        raise_id=rec.get("raise_id"),
        seq=rec.get("seq"),
        via=rec.get("via"),
        content_hash=rec.get("content_hash"),
        page=rec.get("page"),
        fact=rec.get("fact"),
        body=rec.get("body"),
        reason=rec.get("reason"),
        successor=rec.get("successor"),
    )


def parse_message(record: object) -> RaiseMessage | Fallthrough:
    """Validate one decoded record (not the envelope) as a message."""
    if not isinstance(record, dict):
        return Fallthrough("record is not a JSON object")
    reason = _check_record(record)
    if reason is not None:
        return Fallthrough(reason)
    return _to_message(record)


def parse_batch(text: str) -> ParsedBatch | Fallthrough:
    """Parse a whole ``.jsonl`` file. Any non-conformance falls the whole file through."""
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return Fallthrough("file is empty", 1)
    envelope = parse_batch_envelope(lines[0])
    if envelope is None:
        return Fallthrough("no valid batch envelope", 1)
    messages: list[RaiseMessage] = []
    for n, raw in enumerate(lines[1:], start=2):
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):
            return Fallthrough("line is not valid JSON", n)
        parsed = parse_message(rec)
        if isinstance(parsed, Fallthrough):
            return Fallthrough(parsed.reason, n)
        messages.append(parsed)
    return ParsedBatch(envelope=envelope, messages=tuple(messages))


def _to_record(msg: RaiseMessage) -> dict[str, Any]:
    """The record dict for *msg*, keys in the fixed order of §6.1."""
    values: dict[str, Any] = {
        "record": msg.kind,
        "raise_id": msg.raise_id,
        "seq": msg.seq,
        "message_id": msg.message_id,
        "origin": msg.origin,
        "target": msg.target,
        "proposer": msg.proposer,
        "via": msg.via,
        "sent_at": msg.sent_at,
        "content_hash": msg.content_hash,
        "page": msg.page,
        "fact": msg.fact,
        "body": msg.body,
        "reason": msg.reason,
        "successor": msg.successor,
    }
    return {k: v for k, v in values.items() if v is not None}


def render_message(msg: RaiseMessage) -> str:
    """One JSONL line (no newline) for *msg*. :class:`ValueError` if it would not parse."""
    rec = _to_record(msg)
    reason = _check_record(rec)
    if reason is not None:
        raise ValueError(f"not a conformant {msg.kind} message: {reason}")
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def render_batch(
    messages: list[RaiseMessage] | tuple[RaiseMessage, ...],
    *,
    batch_id: str,
    created_at: str,
    submitter: str = "raise",
) -> str:
    """A whole file: the existing batch envelope, then one line per message.

    The envelope is the field-corrections one at the lowest ``schema_version``
    this build knows; no version of the raise protocol's own is added.
    """
    envelope = {
        "record": "batch",
        "schema_version": min(KNOWN_SCHEMA_VERSIONS),
        "submitter": submitter,
        "batch_id": batch_id,
        "created_at": created_at,
    }
    first = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if parse_batch_envelope(first) is None:
        raise ValueError("batch_id and created_at must be non-empty")
    return "\n".join([first, *(render_message(m) for m in messages)]) + "\n"
