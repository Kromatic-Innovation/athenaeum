# SPDX-License-Identifier: Apache-2.0
"""The hash-chained witness ledger: v1 line schema, appender, tolerant reader,
chain verifier, and the ``LedgerIndex`` lookup (athenaeum#2147).

Layering: L1 (depends only on :mod:`promptwitness.canon`, stdlib). Standard
library only at import time — no ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``).

See ``docs/design/witness-ledger.md`` for the full design, the security
boundary, and the operator rulings this module implements.

**Directory layout** (one per harness, under :func:`resolve_ledger_dir`)::

    <ledger_dir>/
        ledger.lock        # exclusive-lock file guarding every append
        <YYYY-MM>.jsonl     # monthly partitions, one continuous hash chain
        anchors/            # populated by athenaeum#2151 (anchoring)
        state/              # non-authoritative per-session caches (athenaeum#2149)

**Line schema v1.** One ``canon.canonical_json`` object per line, UTF-8,
terminated by ``\\n``. Common fields on every line: ``v``, ``ledger_id``,
``seq``, ``kind``, ``ts``, ``harness``, ``harness_version``, ``hook_version``,
``prev``, ``key``, and ``sig`` (omitted when unsigned). ``kind`` is one of
``"turn"``, ``"session-close"``, ``"anchor"``, ``"key-rotate"``, ``"gap"``.
Turn-only fields (``session``, ``turn``, ``prompt_id``, ``prompt_kind``,
``prompt_digest``, ``raw_digest``, ``prompt_chars``, ``segments``,
``channel``, ``resumed``, ``signals``) are present ONLY on ``kind == "turn"``
lines. ``session-close`` carries ``turns``/``last_seq``/``turns_witnessed``;
``gap`` carries ``reason``. ``anchor`` and ``key-rotate`` carry whatever
``extra`` fields their owning issue (athenaeum#2151, athenaeum#2150) passes
through :func:`append_line`'s ``extra`` parameter — this module does not
know their shape, per the additive-schema rule ("readers ignore unknown
fields").

**No prompt text, ever.** Only digests, lengths, offsets and kind labels are
recorded — never prompt text. :func:`find_oversized_fields` is the
mechanical check (see ``tests/test_promptwitness_ledger.py``).

**Chain.** ``line_hash = sha256(canonical_json(line_with_sig))``; ``prev``
is the previous line's ``line_hash``; the genesis ``prev`` (the very first
line of a ledger) is ``sha256(b"promptwitness-genesis\\x00" + ledger_id)``.
``sig`` covers ``canonical_json(line_without_sig)`` (including ``prev``).

**Reversible default (recorded for the lane report):** ``ledger_id`` is
derived deterministically from the ledger directory's resolved absolute
path (``"pw-" + sha256(path)[:16]``) rather than minted once and persisted
in a side file. This needs no extra state file, is race-free, and is easy
to change later (schema additive) if a directory-independent identity is
ever needed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from promptwitness import __version__, canon

__all__ = [
    "SCHEMA_VERSION",
    "KINDS",
    "TURN_ONLY_FIELDS",
    "Segment",
    "KeyInfo",
    "KeyProviderLike",
    "KeyUnavailable",
    "WitnessLine",
    "ChainVerification",
    "LedgerIndex",
    "resolve_ledger_dir",
    "append_line",
    "iter_lines",
    "verify_chain",
    "find_oversized_fields",
    "list_partitions",
]

SCHEMA_VERSION = 1

KINDS: frozenset[str] = frozenset({"turn", "session-close", "anchor", "key-rotate", "gap"})

TURN_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "session",
        "turn",
        "prompt_id",
        "prompt_kind",
        "prompt_digest",
        "raw_digest",
        "prompt_chars",
        "segments",
        "channel",
        "resumed",
        "signals",
    }
)

_SESSION_CLOSE_FIELDS: frozenset[str] = frozenset({"turns", "last_seq", "turns_witnessed"})
_GAP_FIELDS: frozenset[str] = frozenset({"reason"})

_PARTITION_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_DIGEST_LEN = len("sha256:" + "0" * 64)  # 71

#: Dotted "label field" paths allowed to exceed digest length — short,
#: enumerated, bounded-cardinality strings, never free text. Anything else
#: longer than a digest is presumed to be (or to risk leaking) prompt text.
_LABEL_FIELD_PATHS: frozenset[str] = frozenset(
    {
        "ledger_id",
        "kind",
        "ts",
        "harness",
        "harness_version",
        "hook_version",
        "session",
        "prompt_id",
        "prompt_kind",
        "channel",
        "reason",
        "sig",  # a signature is opaque bytes, not text, but can exceed 71 chars
        "key.id",
        "key.alg",
        "key.strength",
        "key.error",
        "ledger_meta.protection",
        "ledger_meta.detail",
    }
)


# ---------------------------------------------------------------------------
# Locking — POSIX fcntl.flock, Windows msvcrt.locking (mock-tested)
# ---------------------------------------------------------------------------


def _lock_exclusive(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_exclusive(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on *lock_path* for the duration of the block.

    Required because concurrent sessions append to one chain (unlike
    ``athenaeum.store.append_line_durable``'s single-appender assumption) —
    see ``tests/test_promptwitness_ledger.py``'s eight-concurrent-appenders
    test.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _lock_exclusive(fd)
        yield
    finally:
        _unlock_exclusive(fd)
        os.close(fd)


# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------


def resolve_ledger_dir(
    harness: str, *, override: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve the ledger directory for *harness*.

    Precedence: ``override`` argument (if given) > ``PROMPTWITNESS_DIR`` env
    (a full override — already the harness-specific directory, like
    ``ATHENAEUM_CACHE_DIR``'s own full-override precedent in
    ``athenaeum.config.resolve_cache_dir``) > ``$XDG_STATE_HOME/promptwitness/
    <harness>`` > ``~/.local/state/promptwitness/<harness>``.

    Builds every path from :data:`os.environ` / :meth:`pathlib.Path.home`,
    never from a hardcoded machine-specific home-directory literal (the
    public-safe-lint gate rejects exactly that shape, including in test
    fixtures).
    """
    if override is not None:
        return Path(override).expanduser()
    env_dir = os.environ.get("PROMPTWITNESS_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        base = Path(xdg_state).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "promptwitness" / harness


# ---------------------------------------------------------------------------
# Segment / KeyInfo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One typed/pasted/slash-args span within a turn's prompt text."""

    kind: str  # "typed" | "pasted" | "slash-args"
    start: int
    end: int
    digest: str
    paste_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "digest": self.digest,
        }
        if self.paste_id is not None:
            out["paste_id"] = self.paste_id
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Segment:
        return cls(
            kind=str(d.get("kind", "")),
            start=int(d.get("start", 0)),
            end=int(d.get("end", 0)),
            digest=str(d.get("digest", "")),
            paste_id=d.get("paste_id"),
        )


@dataclass(frozen=True)
class KeyInfo:
    """``key.{id, alg, strength, presence}`` — see athenaeum#2150.

    ``strength: "none"`` (the default) is what a line written with no key
    provider carries; ``sig`` is omitted entirely in that case.
    """

    id: str | None = None
    alg: str | None = None
    strength: str = "none"
    presence: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "alg": self.alg,
            "strength": self.strength,
            "presence": self.presence,
        }
        if self.error is not None:
            out["error"] = self.error
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> KeyInfo:
        return cls(
            id=d.get("id"),
            alg=d.get("alg"),
            strength=str(d.get("strength", "none")),
            presence=bool(d.get("presence", False)),
            error=d.get("error"),
        )


@runtime_checkable
class KeyProviderLike(Protocol):
    """The signing seam athenaeum#2150 implements. Duck-typed on purpose —
    any object with this shape works, whichever module defines it."""

    def key_id(self) -> str: ...

    def algorithm(self) -> str: ...

    def strength(self) -> str: ...

    def presence_required(self) -> bool: ...

    def sign(self, payload: bytes) -> bytes: ...


class KeyUnavailable(Exception):
    """Raised by a :class:`KeyProviderLike` when it cannot sign right now."""


# ---------------------------------------------------------------------------
# WitnessLine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WitnessLine:
    """A parsed (or about-to-be-written) line of the witness ledger.

    This is an ergonomic, typed VIEW. It is not the integrity-critical
    representation — :func:`verify_chain` recomputes hashes over the raw
    parsed JSON object exactly as read from disk, never through this
    dataclass, so an unrecognized future field can never silently change
    what a line hashes to from this class's point of view. ``extra`` holds
    every field this dataclass does not itself model (e.g. a future
    ``anchor``/``key-rotate`` payload), so round-tripping through
    :meth:`from_dict` / :meth:`to_dict` is still lossless for display.

    :meth:`line_hash` is a convenience for a line you just built (its
    ``to_dict()`` output is exactly what will be — or was — written); it is
    NOT guaranteed byte-stable for an arbitrary line parsed off disk whose
    ``extra`` fields were written in a different key order than this class
    reconstructs them in. Use :func:`verify_chain` for integrity checks.
    """

    v: int
    ledger_id: str
    seq: int
    kind: str
    ts: str
    harness: str
    harness_version: str
    hook_version: str
    prev: str
    key: KeyInfo = field(default_factory=KeyInfo)
    sig: str | None = None
    session: str | None = None
    turn: int | None = None
    prompt_id: str | None = None
    prompt_kind: str | None = None
    prompt_digest: str | None = None
    raw_digest: str | None = None
    prompt_chars: int | None = None
    segments: tuple[Segment, ...] = ()
    channel: str | None = None
    resumed: bool | None = None
    signals: Mapping[str, Any] = field(default_factory=dict)
    turns: int | None = None
    last_seq: int | None = None
    turns_witnessed: int | None = None
    reason: str | None = None
    ledger_meta: Mapping[str, Any] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "v": self.v,
            "ledger_id": self.ledger_id,
            "seq": self.seq,
            "kind": self.kind,
            "ts": self.ts,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "hook_version": self.hook_version,
            "prev": self.prev,
            "key": self.key.to_dict(),
        }
        if self.kind == "turn":
            out["session"] = self.session
            out["turn"] = self.turn
            out["prompt_id"] = self.prompt_id
            out["prompt_kind"] = self.prompt_kind
            out["prompt_digest"] = self.prompt_digest
            out["raw_digest"] = self.raw_digest
            out["prompt_chars"] = self.prompt_chars
            out["segments"] = [s.to_dict() for s in self.segments]
            out["channel"] = self.channel
            out["resumed"] = self.resumed
            out["signals"] = dict(self.signals)
        elif self.kind == "session-close":
            out["session"] = self.session
            out["turns"] = self.turns
            out["last_seq"] = self.last_seq
            out["turns_witnessed"] = self.turns_witnessed
        elif self.kind == "gap":
            out["reason"] = self.reason
        if self.ledger_meta is not None:
            out["ledger_meta"] = dict(self.ledger_meta)
        out.update(self.extra)
        if self.sig is not None:
            out["sig"] = self.sig
        return out

    def line_hash(self) -> str:
        return "sha256:" + hashlib.sha256(canon.canonical_json(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> WitnessLine:
        known = {
            "v",
            "ledger_id",
            "seq",
            "kind",
            "ts",
            "harness",
            "harness_version",
            "hook_version",
            "prev",
            "key",
            "sig",
            "session",
            "turn",
            "prompt_id",
            "prompt_kind",
            "prompt_digest",
            "raw_digest",
            "prompt_chars",
            "segments",
            "channel",
            "resumed",
            "signals",
            "turns",
            "last_seq",
            "turns_witnessed",
            "reason",
            "ledger_meta",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        segments_raw = d.get("segments") or []
        return cls(
            v=int(d.get("v", SCHEMA_VERSION)),
            ledger_id=str(d.get("ledger_id", "")),
            seq=int(d.get("seq", 0)),
            kind=str(d.get("kind", "")),
            ts=str(d.get("ts", "")),
            harness=str(d.get("harness", "")),
            harness_version=str(d.get("harness_version", "")),
            hook_version=str(d.get("hook_version", "")),
            prev=str(d.get("prev", "")),
            key=KeyInfo.from_dict(d.get("key") or {}),
            sig=d.get("sig"),
            session=d.get("session"),
            turn=d.get("turn"),
            prompt_id=d.get("prompt_id"),
            prompt_kind=d.get("prompt_kind"),
            prompt_digest=d.get("prompt_digest"),
            raw_digest=d.get("raw_digest"),
            prompt_chars=d.get("prompt_chars"),
            segments=tuple(Segment.from_dict(s) for s in segments_raw),
            channel=d.get("channel"),
            resumed=d.get("resumed"),
            signals=dict(d.get("signals") or {}),
            turns=d.get("turns"),
            last_seq=d.get("last_seq"),
            turns_witnessed=d.get("turns_witnessed"),
            reason=d.get("reason"),
            ledger_meta=d.get("ledger_meta"),
            extra=extra,
        )


# ---------------------------------------------------------------------------
# No-prompt-text schema check
# ---------------------------------------------------------------------------


def find_oversized_fields(line: Mapping[str, Any], *, _path: str = "") -> list[str]:
    """Return the dotted paths of every string field longer than a digest
    (71 chars, ``"sha256:" + 64 hex``) that is not an enumerated label field.

    Walks dicts and lists recursively. List items are checked under the same
    parent path (e.g. ``segments`` items share the ``segments`` path) so a
    stray long string in any position is caught, not just index 0.
    """
    hits: list[str] = []
    for key, value in line.items():
        path = f"{_path}.{key}" if _path else key
        if isinstance(value, str):
            if len(value) > _DIGEST_LEN and path not in _LABEL_FIELD_PATHS:
                hits.append(path)
        elif isinstance(value, Mapping):
            hits.extend(find_oversized_fields(value, _path=path))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    hits.extend(find_oversized_fields(item, _path=path))
                elif (
                    isinstance(item, str)
                    and len(item) > _DIGEST_LEN
                    and path not in _LABEL_FIELD_PATHS
                ):
                    hits.append(path)
    return hits


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _genesis_prev(ledger_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        b"promptwitness-genesis\x00" + ledger_id.encode("utf-8")
    ).hexdigest()


def _recompute_line_hash(raw: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canon.canonical_json(raw)).hexdigest()


def _derive_ledger_id(ledger_dir: Path) -> str:
    resolved = str(ledger_dir.resolve())
    return "pw-" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _now_iso_ms(when: datetime | None = None) -> str:
    dt = when if when is not None else datetime.now(timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _partition_name_for(ts: str) -> str:
    return ts[:7] + ".jsonl"


# ---------------------------------------------------------------------------
# Append-only protection (best effort; defence against accidents, not the
# model — see docs/design/witness-ledger.md's security boundary)
# ---------------------------------------------------------------------------


def _apply_append_only_protection(path: Path) -> dict[str, Any]:
    if sys.platform == "darwin":
        cmd, label = ["chflags", "uappnd", str(path)], "chflags"
    elif sys.platform.startswith("linux"):
        cmd, label = ["chattr", "+a", str(path)], "chattr"
    else:
        return {
            "protection": "unsupported",
            "detail": f"no append-only mechanism for {sys.platform!r}",
        }
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"protection": f"{label}:failed", "detail": str(exc)}
    if result.returncode == 0:
        return {"protection": f"{label}:applied"}
    detail = (result.stderr or result.stdout).strip()[:200]
    return {"protection": f"{label}:failed", "detail": detail}


# ---------------------------------------------------------------------------
# Raw tolerant reading
# ---------------------------------------------------------------------------


def _existing_partitions(ledger_dir: Path) -> list[str]:
    if not ledger_dir.is_dir():
        return []
    return sorted(p.name for p in ledger_dir.glob("*.jsonl") if _PARTITION_RE.match(p.name))


def list_partitions(ledger_dir: str | os.PathLike[str]) -> list[str]:
    """Sorted partition filenames (``<YYYY-MM>.jsonl``) actually present
    under *ledger_dir* — the public form of :func:`_existing_partitions`,
    for callers like ``promptwitness.cli``'s ``status`` command."""
    return _existing_partitions(Path(ledger_dir))


def _iter_raw_bytes_lines(raw: bytes) -> Iterator[dict[str, Any]]:
    import json

    for chunk in raw.split(b"\n"):
        if not chunk.strip():
            continue
        try:
            obj = json.loads(chunk.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # tolerant reader: skip torn/garbage physical lines
        if isinstance(obj, dict):
            yield obj


def _iter_raw(ledger_dir: Path) -> Iterator[dict[str, Any]]:
    for name in _existing_partitions(ledger_dir):
        raw = (ledger_dir / name).read_bytes()
        yield from _iter_raw_bytes_lines(raw)


def iter_lines(ledger_dir: str | os.PathLike[str]) -> Iterator[WitnessLine]:
    """Tolerant reader: every well-formed line across all partitions, in
    chain order.

    Skips a physically torn or unparseable line (``_iter_raw_bytes_lines``
    handles that) AND a line that parses as JSON but whose field TYPES are
    not the v1 schema's — ``{"seq": "nope"}``, a string where ``key`` should
    be an object, and so on. :meth:`WitnessLine.from_dict` coerces, so it
    raises on those; a reader over a file that corruption and tampering can
    both reach must degrade to "skip the line", never to a crashed caller
    (:meth:`LedgerIndex.build` and ``promptwitness status`` are both callers).

    This cannot be used to HIDE a line from verification: :func:`verify_chain`
    reads the raw dicts, not ``WitnessLine`` objects, so a line skipped here
    is still hashed there and still breaks the chain at its ``seq``.
    """
    for raw in _iter_raw(Path(ledger_dir)):
        try:
            line = WitnessLine.from_dict(raw)
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
        yield line


def _split_tail(raw: bytes) -> tuple[bytes, bytes]:
    """``(well_terminated_region, torn_fragment)``. ``torn_fragment`` is the
    trailing bytes after the last ``\\n`` when *raw* does not itself end in
    ``\\n`` (empty bytes when the tail is clean or *raw* is empty)."""
    if not raw or raw.endswith(b"\n"):
        return raw, b""
    idx = raw.rfind(b"\n")
    if idx == -1:
        return b"", raw
    return raw[: idx + 1], raw[idx + 1 :]


def _scan_last_good(good: bytes) -> tuple[int, str] | tuple[None, None]:
    lines = [ln for ln in good.split(b"\n") if ln.strip()]
    for chunk in reversed(lines):
        for obj in _iter_raw_bytes_lines(chunk + b"\n"):
            seq = obj.get("seq")
            if isinstance(seq, int):
                return seq, _recompute_line_hash(obj)
    return None, None


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def _run_sign_with_timeout(
    key_provider: KeyProviderLike, payload: bytes, timeout: float
) -> bytes:
    """``key_provider.sign(payload)``, abandoned after *timeout* seconds.

    Deliberately a bare DAEMON thread rather than a ``ThreadPoolExecutor``:
    the executor's context manager calls ``shutdown(wait=True)`` on the way
    out, so a timeout would raise and then BLOCK until the slow signer
    returned anyway — with the ledger lock still held, which is the one
    thing the timeout exists to prevent (a hook must never wait on a key).
    ``shutdown(wait=False, cancel_futures=True)`` would fix the blocking but
    not the exit: a pool's worker threads are non-daemon and the interpreter
    joins them at shutdown, so a hung signer would hold up the hook process
    on its way out instead. A daemon thread is abandoned on both paths.

    Abandoning a thread mid-``sign`` is safe here because the signer is
    handed an immutable ``bytes`` payload and its return value is dropped;
    nothing it can still do is observable. Signing is not retried — the
    caller degrades the line to unsigned.
    """
    result: list[bytes] = []
    failure: list[BaseException] = []
    done = threading.Event()

    def _sign() -> None:
        try:
            result.append(key_provider.sign(payload))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=_sign, name="promptwitness-sign", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError(f"signing exceeded {timeout}s")
    if failure:
        raise failure[0]
    return result[0]


def _apply_key_and_sig(
    line: dict[str, Any], key_provider: KeyProviderLike | None, timeout: float
) -> None:
    """Mutate *line* in place: add ``key`` always, ``sig`` only when signed.

    A line written with no key provider carries ``key.strength: "none"`` and
    omits ``sig`` entirely (athenaeum#2147 AC). A key provider that raises,
    times out, or is otherwise unavailable degrades to the same unsigned
    shape plus ``key.error`` — the append never blocks or fails because
    signing failed (athenaeum#2150's "the hook never blocks" rule, honored
    here even though no real adapter calls this yet).

    The signed payload is ``canonical_json(line_without_sig)``, so ``key`` is
    written into *line* BEFORE the payload is serialized. Signing the
    pre-``key`` dict instead would leave ``key.strength`` outside the
    signature, and a custody claim could then be upgraded (or a key id
    swapped) on an otherwise valid line — the one field the later trust
    graph reads. Nothing forces that ordering: every field the provider
    contributes is knowable without signing (``key_id``/``algorithm``/
    ``strength``/``presence_required`` are plain queries). See
    ``TestSigningSeam::test_sig_covers_the_key_block``.
    """
    if key_provider is None:
        line["key"] = KeyInfo().to_dict()
        return
    try:
        key_info = KeyInfo(
            id=key_provider.key_id(),
            alg=key_provider.algorithm(),
            strength=key_provider.strength(),
            presence=key_provider.presence_required(),
        )
        line["key"] = key_info.to_dict()
        payload = canon.canonical_json(line)
        sig_bytes = _run_sign_with_timeout(key_provider, payload, timeout)
        import base64

        line["sig"] = base64.b64encode(sig_bytes).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - signing must never block an append
        line["key"] = KeyInfo(strength="none", error=type(exc).__name__).to_dict()
        line.pop("sig", None)


def _write_line_bytes(path: Path, line: Mapping[str, Any]) -> None:
    payload = canon.canonical_json(line) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_trailing_newline(path: Path) -> None:
    fd = os.open(path, os.O_APPEND | os.O_WRONLY)
    try:
        os.write(fd, b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class _TailState:
    last_seq: int
    last_hash: str
    is_genesis: bool  # True iff no line has ever existed before this point


def _tail_state_before(ledger_dir: Path, ledger_id: str, before_name: str) -> _TailState:
    """Chain state immediately before partition *before_name* would start,
    repairing a torn tail on the latest existing partition at or before it.
    """
    existing = [n for n in _existing_partitions(ledger_dir) if n <= before_name]
    if not existing:
        return _TailState(0, _genesis_prev(ledger_id), True)
    latest_name = max(existing)
    latest_path = ledger_dir / latest_name
    raw = latest_path.read_bytes()
    good, torn = _split_tail(raw)
    last_seq, last_hash = _scan_last_good(good)
    is_genesis = last_seq is None
    if last_seq is None:
        earlier = [n for n in existing if n < latest_name]
        if earlier:
            prev_path = ledger_dir / max(earlier)
            prev_raw = prev_path.read_bytes()
            prev_good, _prev_torn = _split_tail(prev_raw)
            last_seq, last_hash = _scan_last_good(prev_good)
            is_genesis = last_seq is None
        if last_seq is None:
            last_seq, last_hash = 0, _genesis_prev(ledger_id)
    assert last_hash is not None
    if torn:
        _ensure_trailing_newline(latest_path)
        gap_line: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "ledger_id": ledger_id,
            "seq": last_seq + 1,
            "kind": "gap",
            "ts": _now_iso_ms(),
            "harness": "promptwitness",
            "harness_version": "",
            "hook_version": f"promptwitness {__version__}",
            "prev": last_hash,
            "reason": "torn-tail",
        }
        if is_genesis:
            gap_line["ledger_meta"] = _apply_append_only_protection(latest_path)
        _apply_key_and_sig(gap_line, None, 2.0)
        _write_line_bytes(latest_path, gap_line)
        return _TailState(gap_line["seq"], _recompute_line_hash(gap_line), False)
    return _TailState(last_seq, last_hash, is_genesis)


def append_line(
    ledger_dir: str | os.PathLike[str],
    *,
    kind: str,
    harness: str,
    harness_version: str,
    hook_version: str,
    ts: str | None = None,
    key_provider: KeyProviderLike | None = None,
    sign_timeout: float = 2.0,
    session: str | None = None,
    turn: int | None = None,
    prompt_id: str | None = None,
    prompt_kind: str | None = None,
    prompt_digest: str | None = None,
    raw_digest: str | None = None,
    prompt_chars: int | None = None,
    segments: Sequence[Segment] | None = None,
    channel: str | None = None,
    resumed: bool | None = None,
    signals: Mapping[str, Any] | None = None,
    turns: int | None = None,
    last_seq: int | None = None,
    turns_witnessed: int | None = None,
    reason: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> WitnessLine:
    """Append one witness line to the ledger under *ledger_dir*.

    Takes an exclusive lock on ``ledger.lock`` for the duration of the call
    (read tail, write, fsync) because concurrent sessions append to one
    chain. A torn tail on the current (or, failing that, the immediately
    preceding) partition is repaired first — terminated with ``\\n``, then
    bridged by a ``gap`` line — and this append always chains onto that
    repaired state, never onto a torn line.

    Raises :class:`ValueError` for an unknown *kind*, or for a field that
    belongs to a different ``kind`` than the one given (turn-only fields on
    a non-turn line, etc.) — the schema's "fields are omitted on non-turn
    lines" rule is enforced, not merely documented.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown ledger line kind {kind!r}; must be one of {sorted(KINDS)}")

    # `session` is deliberately checked separately from the rest of
    # `turn_kwargs` (athenaeum#2149 fix): `WitnessLine.to_dict()` already
    # writes a `session` field for BOTH `kind="turn"` and
    # `kind="session-close"` (a session-close line has to name which session
    # it closes), but bucketing `session` inside `turn_kwargs` unconditionally
    # rejected it on a session-close line before this fix -- the one call
    # shape a `SessionEnd` hook adapter actually needs. `gap`/`anchor`/
    # `key-rotate` still reject it, with the same "turn-only fields" message,
    # so `TestAppendLineValidation::test_rejects_turn_fields_on_non_turn_kind`
    # (a `gap` line) is unaffected.
    turn_kwargs = {
        "turn": turn,
        "prompt_id": prompt_id,
        "prompt_kind": prompt_kind,
        "prompt_digest": prompt_digest,
        "raw_digest": raw_digest,
        "prompt_chars": prompt_chars,
        "segments": segments,
        "channel": channel,
        "resumed": resumed,
        "signals": signals,
    }
    close_kwargs = {"turns": turns, "last_seq": last_seq, "turns_witnessed": turns_witnessed}
    gap_kwargs = {"reason": reason}

    if kind != "turn" and any(v is not None for v in turn_kwargs.values()):
        raise ValueError(f"turn-only fields were given for kind={kind!r}")
    if kind not in ("turn", "session-close") and session is not None:
        raise ValueError(f"turn-only fields were given for kind={kind!r}")
    if kind != "session-close" and any(v is not None for v in close_kwargs.values()):
        raise ValueError(f"session-close-only fields were given for kind={kind!r}")
    if kind != "gap" and any(v is not None for v in gap_kwargs.values()):
        raise ValueError(f"gap-only fields were given for kind={kind!r}")

    ledger_dir_p = Path(ledger_dir)
    ledger_dir_p.mkdir(parents=True, exist_ok=True)
    ledger_id = _derive_ledger_id(ledger_dir_p)
    ts_value = ts or _now_iso_ms()
    partition_name = _partition_name_for(ts_value)
    partition_path = ledger_dir_p / partition_name
    lock_path = ledger_dir_p / "ledger.lock"

    with _exclusive_lock(lock_path):
        tail = _tail_state_before(ledger_dir_p, ledger_id, partition_name)
        seq = tail.last_seq + 1

        line: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "ledger_id": ledger_id,
            "seq": seq,
            "kind": kind,
            "ts": ts_value,
            "harness": harness,
            "harness_version": harness_version,
            "hook_version": hook_version,
            "prev": tail.last_hash,
        }
        if kind == "turn":
            line.update(
                {
                    "session": session,
                    "turn": turn,
                    "prompt_id": prompt_id,
                    "prompt_kind": prompt_kind,
                    "prompt_digest": prompt_digest,
                    "raw_digest": raw_digest,
                    "prompt_chars": prompt_chars,
                    "segments": [s.to_dict() for s in (segments or ())],
                    "channel": channel,
                    "resumed": resumed,
                    "signals": dict(signals or {}),
                }
            )
        elif kind == "session-close":
            line.update(
                {
                    "session": session,
                    "turns": turns,
                    "last_seq": last_seq,
                    "turns_witnessed": turns_witnessed,
                }
            )
        elif kind == "gap":
            line["reason"] = reason

        if extra:
            overlap = set(extra) & set(line)
            if overlap:
                raise ValueError(f"extra fields collide with reserved fields: {sorted(overlap)}")
            line.update(extra)

        newly_created = not partition_path.exists()
        if tail.is_genesis and newly_created:
            partition_path.touch(exist_ok=True)
            line["ledger_meta"] = _apply_append_only_protection(partition_path)

        _apply_key_and_sig(line, key_provider, sign_timeout)
        _write_line_bytes(partition_path, line)

    return WitnessLine.from_dict(line)


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainVerification:
    """Result of :func:`verify_chain`.

    ``first_break_seq`` is the ``seq`` of the first line, in on-disk order,
    whose ``prev`` does not match the recomputed hash of the line
    immediately preceding it in the file. A one-byte mutation of line N
    surfaces here as a break at N+1 (N's own ``prev`` is untouched by the
    mutation); a deleted line surfaces at the ``seq`` of the line that
    physically follows the gap; a reordered pair surfaces at the ``seq`` of
    whichever of the two now appears earlier in the file.
    """

    ok: bool
    first_break_seq: int | None
    lines_checked: int


def verify_chain(ledger_dir: str | os.PathLike[str]) -> ChainVerification:
    first_break: int | None = None
    checked = 0
    expected_prev: str | None = None
    for raw in _iter_raw(Path(ledger_dir)):
        checked += 1
        if expected_prev is None:
            expected_prev = _genesis_prev(str(raw.get("ledger_id", "")))
        actual_prev = raw.get("prev")
        if actual_prev != expected_prev and first_break is None:
            seq = raw.get("seq")
            first_break = seq if isinstance(seq, int) else -1
        expected_prev = _recompute_line_hash(raw)
    return ChainVerification(
        ok=first_break is None, first_break_seq=first_break, lines_checked=checked
    )


# ---------------------------------------------------------------------------
# LedgerIndex
# ---------------------------------------------------------------------------


class LedgerIndex:
    """One-pass index over a ledger directory, by ``prompt_id`` and by
    ``(session, prompt_digest)``. Only ``kind == "turn"`` lines are indexed.
    """

    def __init__(self) -> None:
        self._by_prompt_id: dict[str, WitnessLine] = {}
        self._by_session_digest: dict[tuple[str, str], list[WitnessLine]] = {}

    @classmethod
    def build(cls, ledger_dir: str | os.PathLike[str]) -> LedgerIndex:
        idx = cls()
        for line in iter_lines(ledger_dir):
            if line.kind != "turn":
                continue
            if line.prompt_id:
                idx._by_prompt_id[line.prompt_id] = line
            if line.session and line.prompt_digest:
                key = (line.session, line.prompt_digest)
                idx._by_session_digest.setdefault(key, []).append(line)
        return idx

    def by_prompt_id(self, prompt_id: str) -> WitnessLine | None:
        return self._by_prompt_id.get(prompt_id)

    def by_session_digest(self, session: str, prompt_digest: str) -> list[WitnessLine]:
        return list(self._by_session_digest.get((session, prompt_digest), ()))

    @staticmethod
    def is_witnessable_human(line: WitnessLine) -> bool:
        """``channel == "interactive"`` and ``prompt_kind`` human-countable.

        Headless and bridge channels are never human (operator ruling) —
        this is the seam athenaeum#2149's channel/kind classifier is tested
        against (``LedgerIndex`` human filter rejects headless/bridge).
        """
        return (
            line.kind == "turn"
            and line.channel == "interactive"
            and line.prompt_kind in ("human", "slash-command")
        )

    def human_lines(self) -> list[WitnessLine]:
        return [line for line in self._by_prompt_id.values() if self.is_witnessable_human(line)]
