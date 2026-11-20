# SPDX-License-Identifier: Apache-2.0
"""A store's raw intake, read by the intake run and dispatched by message shape.

``docs/design/federation-intake-routing.md`` §8.1 (the place), §4.5 (a record
addressed to a store is raw intake and is not routed again), §13 slice S15.

A single compiling install keeps what is addressed to a store it compiles in
``raw/_stores/<store_id>/`` (:func:`athenaeum.intake.store_raw_dir`). The
discovery that feeds classify skips that tree, so nothing in it reaches the
capture gates, ``decide`` or the member's personal classify path. It is read
here instead: :func:`dispatch_store_intake` walks each valid ``<store_id>``
directory, reads each file in the conformance format
(:mod:`athenaeum.raise_messages`), and hands each message to the handler
registered for its kind, with the directory's ``store_id``.

* **Registry keyed by message kind.** :class:`HandlerRegistry` maps a kind
  (``raise``, ``retract``, ...) to a handler. The only handler registered here is
  ``raise`` -> :func:`athenaeum.raise_acceptance.submit_raise`. A later slice
  adds its own with ``register_handler(KIND_RETRACT, fn)``; a kind with no
  handler is recorded ``unhandled`` and left to be read again once one exists.
* **A message names its own store.** One whose ``target`` is not the directory's
  ``store_id`` is **not applied** and is recorded ``misaddressed``.
* **A file that does not conform is raised, never refused and never classified**:
  it becomes a decision in that store's own queue
  (:func:`athenaeum.store_decisions.file_decision`), kind
  :data:`DECISION_KIND_NONCONFORMING`.
* **Nothing is deleted or moved.** A small local ledger in the cache dir
  (:data:`DISPATCH_LEDGER_FILENAME`) records what became of each message, keyed
  by file content hash, so a later run does not hand the same message to its
  handler twice. A message whose store is not mounted here, or whose handler
  refused or failed, is not marked done and is retried next run.

A directory whose name is not a valid ``store_id`` is reported and never read.

Layering: L3 (service). Imports :mod:`athenaeum.raise_acceptance` (L3),
:mod:`athenaeum.raise_messages`, :mod:`athenaeum.store_decisions`,
:mod:`athenaeum.store_access`, :mod:`athenaeum.store` (L2 and below),
:mod:`athenaeum.config` and :mod:`athenaeum.intake` (path helpers only).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_owner_asserter, resolve_raw_file_max_bytes
from athenaeum.intake import discover_store_raw_dirs
from athenaeum.raise_acceptance import DECISION_KIND_HELD, submit_raise
from athenaeum.raise_acceptance_ledger import DISPOSITION_HELD, disposition_of
from athenaeum.raise_messages import (
    KIND_RAISE,
    Fallthrough,
    RaiseMessage,
    parse_batch,
)
from athenaeum.store import append_line_durable
from athenaeum.store_decisions import (
    StoreDecisionRefused,
    file_decision,
    load_queue,
    resolve_mounts,
)

__all__ = [
    "DECISION_KIND_NONCONFORMING",
    "DEFAULT_REGISTRY",
    "DISPATCH_LEDGER_FILENAME",
    "DispatchReport",
    "HandlerRegistry",
    "HandlerResult",
    "MessageContext",
    "dispatch_store_intake",
    "register_handler",
]

log = logging.getLogger(__name__)

#: The decision kind a non-conforming file in a store's raw intake is raised under.
DECISION_KIND_NONCONFORMING = "intake_nonconforming"

#: Local state: what became of each message read from a store's raw intake.
DISPATCH_LEDGER_FILENAME = "_store_intake_dispatch.jsonl"

#: Per-message / per-file outcomes. FINAL ones are never handed on again.
OUTCOME_HANDLED = "handled"
OUTCOME_MISADDRESSED = "misaddressed"
OUTCOME_NONCONFORMING = "nonconforming"
OUTCOME_UNHANDLED = "unhandled"
OUTCOME_NOT_MOUNTED = "not_mounted"
OUTCOME_ERROR = "error"
_FINAL = frozenset({OUTCOME_HANDLED, OUTCOME_MISADDRESSED, OUTCOME_NONCONFORMING})


@dataclass(frozen=True)
class MessageContext:
    """What a handler is given besides the message itself."""

    knowledge_root: Path
    #: The ``store_id`` of the directory the message sits in.
    store_id: str
    #: The root of the store, where this install mounts it (``None`` if it does not).
    target_root: Path | None
    cache_dir: Path
    #: The file the message was read from.
    path: Path
    mounts: Mapping[str, Path] = field(default_factory=dict)
    asserter: Mapping[str, object] | None = None
    #: The acceptance-ledger location: a store's own raw tree on this install.
    location: str = "install_raw"


@dataclass(frozen=True)
class HandlerResult:
    """What a handler did with one message. ``final`` False means: read it again next run."""

    detail: str = ""
    final: bool = True


#: ``handler(context, message, record)``: *message* is the parsed message and
#: *record* the decoded JSON object it came from (some handlers re-parse it).
Handler = Callable[[MessageContext, RaiseMessage, dict[str, Any]], HandlerResult]


class HandlerRegistry:
    """Message kind -> handler. One handler per kind."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler, *, replace: bool = False) -> None:
        """Register *handler* for *kind*; a second registration needs ``replace=True``."""
        if kind in self._handlers and not replace:
            raise ValueError(f"a handler is already registered for kind {kind!r}")
        self._handlers[kind] = handler

    def get(self, kind: str) -> Handler | None:
        return self._handlers.get(kind)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


#: The registry :func:`dispatch_store_intake` uses unless given another.
DEFAULT_REGISTRY = HandlerRegistry()


def register_handler(kind: str, handler: Handler, *, replace: bool = False) -> None:
    """Register *handler* for message *kind* on :data:`DEFAULT_REGISTRY`."""
    DEFAULT_REGISTRY.register(kind, handler, replace=replace)


def _handle_raise(ctx: MessageContext, msg: RaiseMessage, record: dict[str, Any]) -> HandlerResult:
    """The existing raise acceptance: rule checks, then a decision in the store's queue."""
    assert ctx.target_root is not None  # the dispatcher never calls without a mount
    outcome = submit_raise(
        ctx.knowledge_root,
        record,
        target_store_id=ctx.store_id,
        target_root=ctx.target_root,
        cache_dir=ctx.cache_dir,
        location=ctx.location,
        mounts=dict(ctx.mounts),
        asserter=ctx.asserter,
    )
    if outcome.status == "duplicate":
        _refile_if_unfiled(ctx, msg)
    return HandlerResult(detail=f"{outcome.status}:{outcome.reason or ''}")


def _decision_names(rec: dict[str, Any], message_id: str) -> bool:
    detail = rec.get("detail")
    if not isinstance(detail, dict):
        return False
    inner = detail.get("message")
    return detail.get("message_id") == message_id or (
        isinstance(inner, dict) and inner.get("message_id") == message_id
    )


def _refile_if_unfiled(ctx: MessageContext, msg: RaiseMessage) -> None:
    """Never let a re-delivery hide a message that has no decision.

    ``submit_raise`` writes a ``held`` acceptance row and then files the decision;
    when filing fails (for example this install does not hold compile) the row
    stays, and the next delivery reads as a ``duplicate`` that files nothing. A
    message still ``held`` with no decision naming it is filed here (Seer review
    on PR athenaeum#3026). A failure here raises, so the message stays deferred.
    """
    row = disposition_of(ctx.cache_dir, msg.message_id)
    if row is None or row.disposition != DISPOSITION_HELD:
        return
    queue = load_queue(ctx.knowledge_root, ctx.store_id)
    if any(_decision_names(r, msg.message_id) for r in queue):
        return
    file_decision(
        ctx.knowledge_root,
        store_id=ctx.store_id,
        question="A raised message could not be applied by rule (reason: not_filed).",
        kind=DECISION_KIND_HELD,
        mounts=dict(ctx.mounts),
        asserter=ctx.asserter,
        detail={
            "reason": row.reason or "not_filed",
            "raise_id": msg.raise_id,
            "message_id": msg.message_id,
            "seq": msg.seq,
        },
    )


DEFAULT_REGISTRY.register(KIND_RAISE, _handle_raise)


# -- the dispatch ledger -----------------------------------------------------


def _ledger_rows(cache_dir: Path) -> Iterator[dict[str, Any]]:
    path = Path(cache_dir) / DISPATCH_LEDGER_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn trailing line
        if isinstance(row, dict):
            yield row


def _key(store_id: str, sha: str, message_id: str | None) -> tuple[str, str, str | None]:
    return (store_id, sha, message_id)


def _load_state(cache_dir: Path) -> tuple[set[tuple], dict[tuple, str]]:
    final: set[tuple] = set()
    last: dict[tuple, str] = {}
    for row in _ledger_rows(cache_dir):
        k = _key(row.get("store_id", ""), row.get("sha256", ""), row.get("message_id"))
        outcome = str(row.get("outcome", ""))
        last[k] = outcome
        if outcome in _FINAL:
            final.add(k)
    return final, last


def _record(
    cache_dir: Path,
    *,
    store_id: str,
    path: str,
    sha: str,
    outcome: str,
    message_id: str | None = None,
    kind: str | None = None,
    detail: str = "",
) -> None:
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "store_id": store_id,
        "file": path,
        "sha256": sha,
        "message_id": message_id,
        "kind": kind,
        "outcome": outcome,
        "detail": detail,
    }
    append_line_durable(
        Path(cache_dir) / DISPATCH_LEDGER_FILENAME,
        (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"),
    )


# -- the report --------------------------------------------------------------


@dataclass
class DispatchReport:
    """What one :func:`dispatch_store_intake` pass did."""

    #: Directory names under ``raw/_stores/`` that are not a valid ``store_id`` (not read).
    invalid_dirs: list[str] = field(default_factory=list)
    files_read: int = 0
    handled: int = 0
    misaddressed: int = 0
    nonconforming: int = 0
    unhandled: int = 0
    deferred: int = 0  #: not final this run (store not mounted, or a handler refused/failed)
    skipped: int = 0  #: already final in the ledger


# -- the walk ----------------------------------------------------------------


def _store_files(store_dir: Path) -> list[Path]:
    """Regular files under *store_dir* (recursive, sorted, no dotfiles, no symlinks)."""
    out: list[Path] = []
    for p in sorted(store_dir.rglob("*")):
        rel_parts = p.relative_to(store_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        out.append(p)
    return out


def _records_of(text: str) -> list[dict[str, Any]]:
    """The decoded records after the envelope, for a text :func:`parse_batch` accepted."""
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return [json.loads(line) for line in lines[1:]]


def _raise_nonconforming(
    ctx_root: Path,
    store_id: str,
    rel: str,
    reason: str,
    line: int | None,
    mounts: Mapping[str, Path],
    asserter: Mapping[str, object] | None,
) -> None:
    where = f" (line {line})" if line is not None else ""
    file_decision(
        ctx_root,
        store_id=store_id,
        question=(
            f"A file in this store's raw intake does not conform to the message format: "
            f"{rel}: {reason}{where}. It was not applied and was not sent anywhere else."
        ),
        kind=DECISION_KIND_NONCONFORMING,
        mounts=dict(mounts),
        asserter=asserter,
        detail={"file": rel, "reason": reason, "line": line},
    )


def dispatch_store_intake(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    *,
    cache_dir: Path,
    registry: HandlerRegistry | None = None,
) -> DispatchReport:
    """Hand each file under ``raw/_stores/<store_id>/`` to its per-store handler.

    See the module docstring. Never raises for a bad file or a refusing handler:
    those become a decision or a deferred row. Does not touch ``raw/``'s other
    sources.
    """
    reg = registry if registry is not None else DEFAULT_REGISTRY
    report = DispatchReport()
    raw_root = Path(knowledge_root) / "raw"
    stores, invalid = discover_store_raw_dirs(raw_root)
    report.invalid_dirs = invalid
    for name in invalid:
        log.warning("store intake: %r under raw/_stores is not a store id; not read", name)
    if not stores:
        return report

    mounts = resolve_mounts(Path(knowledge_root), config)
    asserter = resolve_owner_asserter(config)
    max_bytes = resolve_raw_file_max_bytes(config)
    final, last = _load_state(cache_dir)

    for store_id, store_dir in stores.items():
        ctx = MessageContext(
            knowledge_root=Path(knowledge_root),
            store_id=store_id,
            target_root=mounts.get(store_id),
            cache_dir=Path(cache_dir),
            path=store_dir,
            mounts=mounts,
            asserter=asserter,
        )
        for path in _store_files(store_dir):
            rel = path.relative_to(store_dir).as_posix()
            try:
                data = path.read_bytes()[: max_bytes + 1]
            except OSError as exc:
                log.warning("store intake: cannot read %s (%s)", path.name, type(exc).__name__)
                continue
            report.files_read += 1
            sha = hashlib.sha256(data).hexdigest()
            file_key = _key(store_id, sha, None)
            if file_key in final:
                report.skipped += 1
                continue
            parsed: Any
            if len(data) > max_bytes:
                parsed = Fallthrough("file is larger than the raw-intake size limit")
            else:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    parsed = Fallthrough("file is not UTF-8 text")
                else:
                    parsed = parse_batch(text)
            if isinstance(parsed, Fallthrough):
                try:
                    _raise_nonconforming(
                        Path(knowledge_root), store_id, rel, parsed.reason, parsed.line,
                        mounts, asserter,
                    )
                except StoreDecisionRefused as exc:
                    _defer(cache_dir, last, report, store_id, rel, sha, None, None,
                           OUTCOME_NOT_MOUNTED, str(exc))
                    continue
                _record(cache_dir, store_id=store_id, path=rel, sha=sha,
                        outcome=OUTCOME_NONCONFORMING, detail=parsed.reason)
                final.add(file_key)
                report.nonconforming += 1
                continue

            records = _records_of(text)
            for msg, record in zip(parsed.messages, records):
                mkey = _key(store_id, sha, msg.message_id)
                if mkey in final:
                    report.skipped += 1
                    continue
                if msg.target != store_id:
                    log.warning(
                        "store intake: a message in raw/_stores/%s names another store; "
                        "not applied", store_id,
                    )
                    _record(cache_dir, store_id=store_id, path=rel, sha=sha,
                            outcome=OUTCOME_MISADDRESSED, message_id=msg.message_id,
                            kind=msg.kind, detail=f"names {msg.target}")
                    final.add(mkey)
                    report.misaddressed += 1
                    continue
                handler = reg.get(msg.kind)
                if handler is None:
                    _defer(cache_dir, last, report, store_id, rel, sha, msg.message_id,
                           msg.kind, OUTCOME_UNHANDLED, f"no handler for {msg.kind}")
                    report.unhandled += 1
                    continue
                if ctx.target_root is None:
                    _defer(cache_dir, last, report, store_id, rel, sha, msg.message_id,
                           msg.kind, OUTCOME_NOT_MOUNTED, "store is not mounted on this install")
                    continue
                try:
                    result = handler(
                        replace(ctx, path=path),
                        msg,
                        record,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad message never stops the run
                    log.warning("store intake: handler for %s failed (%s)", msg.kind,
                                type(exc).__name__)
                    _defer(cache_dir, last, report, store_id, rel, sha, msg.message_id,
                           msg.kind, OUTCOME_ERROR, type(exc).__name__)
                    continue
                if not result.final:
                    _defer(cache_dir, last, report, store_id, rel, sha, msg.message_id,
                           msg.kind, OUTCOME_ERROR, result.detail)
                    continue
                _record(cache_dir, store_id=store_id, path=rel, sha=sha,
                        outcome=OUTCOME_HANDLED, message_id=msg.message_id,
                        kind=msg.kind, detail=result.detail)
                final.add(mkey)
                report.handled += 1
    return report


def _defer(
    cache_dir: Path,
    last: dict[tuple, str],
    report: DispatchReport,
    store_id: str,
    rel: str,
    sha: str,
    message_id: str | None,
    kind: str | None,
    outcome: str,
    detail: str,
) -> None:
    """Note a non-final outcome once; it is read again next run."""
    report.deferred += 1
    k = _key(store_id, sha, message_id)
    if last.get(k) == outcome:
        return
    last[k] = outcome
    _record(cache_dir, store_id=store_id, path=rel, sha=sha, outcome=outcome,
            message_id=message_id, kind=kind, detail=detail)
