# SPDX-License-Identifier: Apache-2.0
"""Per-call audit log for remote MCP tool calls (issue athenaeum#2603).

A tunnel-based remote deployment (a Cloudflare Tunnel, for instance) has no
origin access log, and a hosted authorization server on a free plan may keep
its own logs for as little as a day. Neither is a substitute for a record
athenaeum controls: after a suspected token leak or a prompt-injection
exfiltration attempt, the operator needs to answer "what did a remote caller
actually read" from THIS process's own disk, not from someone else's log
retention policy.

This module owns the SHAPE of that record and the append primitive. It is
deliberately small and has exactly two call sites, both already the sole
choke points for the events they describe:

- :mod:`athenaeum.mcp_server`'s ``_guarded`` wrapper (the one place every
  scope-gated tool call dispatches through) — records ``ok``/``error``/a
  scope ``refused``.
- :mod:`athenaeum.mcp_auth`'s ``_IdentityAllowlistVerifier.verify_token`` —
  the one place a bearer token is accepted or refused before any tool is
  even named — records a bad-token or non-allowlisted-identity ``refused``.

Neither site is a tool body. Nothing here is called from inside
``create_server``'s 15 registered tools, which is what keeps this "wrap the
dispatch point once", not a per-tool sprinkle.

**What is never written.** No raw bearer token, no argument text and no page
body ever reaches :func:`append_audit_record` — only a SHA-256 digest of the
canonicalised call arguments (:func:`canonical_argument_digest`) and the
``uid`` values :func:`extract_page_uids` can find in a tool's return value.
An exception message is never logged verbatim either (:func:`record_tool_call`
logs only the exception's TYPE NAME on an ``error`` outcome) — an arbitrary
``ValueError`` raised deep in a tool body could otherwise echo back the very
argument or page text this module exists to keep out.

**File mode.** The log is created at ``0600`` — unlike
:func:`athenaeum.store.append_line_durable`'s ``0644`` — because this file
sits immediately downstream of a verified identity and a content digest;
every other append-only ledger in this package is operational telemetry with
no such adjacency. A separate ``os.open`` call, not a reuse of that shared
helper, is what makes the narrower mode a property of this module rather
than a shared default every other ledger would also have to carry.

**Concurrency.** Appending is a single ``os.open(O_APPEND|O_CREAT)`` +
``os.write`` of one already-newline-terminated JSON line, mirroring
``append_line_durable``'s own reasoning: a single ``write(2)`` call to an
``O_APPEND`` file descriptor is what local POSIX filesystems serialize
against other writers (thread or process), so two callers racing this
function can interleave lines but never interleave WITHIN a line.

Layering: L1 — stdlib-only (``hashlib``/``json``/``os``/``pathlib``) plus one
L1 import (:func:`athenaeum.store.now_iso`, the package's one UTC-ISO
renderer); no L2-or-above import, so both :mod:`athenaeum.mcp_server` and
:mod:`athenaeum.mcp_auth` (L5) can depend on it without reaching through
each other.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: Default filename for the audit log, under the resolved cache directory.
AUDIT_LOG_FILENAME = "remote-audit.jsonl"

#: The three outcomes a record's ``outcome`` field may carry (issue
#: athenaeum#2603's acceptance criterion names exactly these three words).
VALID_OUTCOMES: frozenset[str] = frozenset({"ok", "refused", "error"})

#: File mode the audit log is created with. Never group/world readable.
_AUDIT_LOG_MODE = 0o600


def default_audit_log_path(cache_dir: Path) -> Path:
    """``<cache_dir>/remote-audit.jsonl`` — the unconfigured default location."""
    return Path(cache_dir) / AUDIT_LOG_FILENAME


def resolve_audit_log_file(configured: Path | None, cache_dir: Path) -> Path:
    """Fold an explicit override with the cache-dir default.

    *configured* is whatever :func:`athenaeum.config.resolve_serve_audit_log`
    already resolved (CLI > env > yaml), or ``None``. Mirrors
    :func:`athenaeum.spend.resolve_ledger_path`'s override-else-default shape
    for the same reason: the yaml/env/CLI precedence lives in ``config.py``
    (gate: ``tests/test_config_resolver_parity_generic.py``), and this
    function's only job is "what does ``None`` mean here".
    """
    if configured is not None:
        return Path(configured).expanduser()
    return default_audit_log_path(cache_dir)


def canonical_argument_digest(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonicalised call arguments.

    Canonicalised: a ``{"args": [...], "kwargs": {...}}`` envelope rendered
    with sorted keys and compact separators, so the SAME logical call always
    hashes to the SAME digest regardless of kwarg ordering. ``default=str``
    tolerates a non-JSON-native argument (a ``Path``, for instance) without
    raising — the stringified form is only ever fed to the hash, never
    itself returned or logged, so a caller cannot use an exotic argument
    type to defeat the "no argument text" property this digest exists to
    hold.
    """
    canonical = json.dumps(
        {"args": list(args), "kwargs": dict(kwargs)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_page_uids(result: Any) -> list[str]:
    """Best-effort, tool-shape-agnostic ``uid`` extraction from a tool result.

    Deliberately conservative in two directions, because this is the one
    function in this module that is handed a tool's full, unredacted return
    value:

    - From a structured result (``dict``/``list``/``tuple``), only a dict
      key literally named ``uid`` is collected — never ``id`` (a pending-
      merge proposal id is not a page identity), never any other field, and
      never the dict's other VALUES, which is what keeps a page body or a
      merge's ``draft_merged_body`` from ever being walked into the return.
    - From a plain string result (``recall``'s and ``read_entity``'s
      rendered markdown), only the ``**Uid:** <token>`` marker every hit
      renderer in :mod:`athenaeum.mcp_server` already emits is matched — not
      a bare hex-shaped substring, which could otherwise coincide with text
      inside a snippet and give a false impression of precision.

    Returns uids in first-seen order, de-duplicated. A tool whose result
    carries no ``uid`` key and no ``**Uid:**`` marker (``entity_schema``,
    a bare confirmation string from ``remember``, …) yields ``[]`` — an
    honest answer: that call did not hand back page identities, so there is
    nothing to report in the one field this exists to hold a lie out of.
    """
    import re

    uid_marker = re.compile(r"\*\*Uid:\*\*\s*(\S+)")
    seen: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, Mapping):
            uid = value.get("uid")
            if isinstance(uid, str) and uid not in seen:
                seen.append(uid)
            for inner in value.values():
                _walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                _walk(inner)
        elif isinstance(value, str):
            for match in uid_marker.finditer(value):
                token = match.group(1)
                if token not in seen:
                    seen.append(token)

    _walk(result)
    return seen


def append_audit_record(path: Path, record: Mapping[str, Any]) -> None:
    """Append one JSON line to *path*, creating it (mode 0600) if needed.

    A SEPARATE implementation of the ``O_APPEND``/``fsync`` primitive
    :func:`athenaeum.store.append_line_durable` already provides — not a
    call to that shared helper — because this file must never be created at
    that helper's ``0644``. ``os.chmod`` runs on every call (not only at
    creation) so a pre-existing file left over at a wider mode by an older
    build is narrowed the next time anything appends to it, at the cost of a
    harmless redundant syscall on every other call.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, _AUDIT_LOG_MODE)
    try:
        os.fchmod(fd, _AUDIT_LOG_MODE)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def build_audit_record(
    *,
    tool_name: str | None,
    outcome: str,
    claims: Mapping[str, Any] | None = None,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    result: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one audit record dict (pure — no I/O; split out for testing).

    *claims* is the verified token's own claim set, or ``None`` when no
    token reached this call at all (a malformed/invalid-signature bearer
    token never produces claims — see
    ``_IdentityAllowlistVerifier.verify_token``). *args*/*kwargs* are the
    call's own positional/keyword arguments; ``None`` for a refusal that
    never reached a tool body, so there is nothing to hash. *result* is the
    tool's return value, scanned only by :func:`extract_page_uids` — never
    stored itself.
    """
    from athenaeum.clock import now_iso

    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome={outcome!r} must be one of {sorted(VALID_OUTCOMES)}")

    claims = claims or {}
    record: dict[str, Any] = {
        "ts": now_iso(),
        "sub": claims.get("sub"),
        "client_id": claims.get("azp") or claims.get("client_id"),
        "tool": tool_name,
        "outcome": outcome,
        "result_count": 0,
        "page_uids": [],
    }
    jti = claims.get("jti")
    if jti is not None:
        record["jti"] = jti
    if args is not None or kwargs is not None:
        record["args_sha256"] = canonical_argument_digest(args or (), kwargs or {})
    if reason is not None:
        record["reason"] = reason
    if outcome == "ok":
        page_uids = extract_page_uids(result)
        record["page_uids"] = page_uids
        record["result_count"] = len(page_uids)
    return record


def record_tool_call(
    path: Path,
    *,
    tool_name: str | None,
    outcome: str,
    claims: Mapping[str, Any] | None = None,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    result: Any = None,
    reason: str | None = None,
) -> None:
    """Build and append one audit record. The one entrypoint both call sites use."""
    record = build_audit_record(
        tool_name=tool_name,
        outcome=outcome,
        claims=claims,
        args=args,
        kwargs=kwargs,
        result=result,
        reason=reason,
    )
    append_audit_record(path, record)


#: Outcomes of a sensitive read's vault check (athenaeum#3176).
SENSITIVE_READ_OUTCOMES: frozenset[str] = frozenset({"granted", "denied"})


def build_sensitive_read_record(
    *,
    principal: Sequence[str] | None,
    vault: str,
    outcome: str,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """One audit record for the read-grant check on a vault (athenaeum#3176).

    Names the principal (its identity key), the vault and the outcome
    (``granted`` / ``denied``). It carries no value, no field name and no
    page text: the check happens before any value is read, so there is none to
    leave out by accident.
    """
    from athenaeum.clock import now_iso

    if outcome not in SENSITIVE_READ_OUTCOMES:
        raise ValueError(f"outcome={outcome!r} must be one of {sorted(SENSITIVE_READ_OUTCOMES)}")
    return {
        "ts": now_iso(),
        "event": "sensitive_read",
        "tool": tool_name,
        "principal": list(principal) if principal else None,
        "vault": vault,
        "outcome": outcome,
    }


def record_sensitive_read(
    path: Path,
    *,
    principal: Sequence[str] | None,
    vault: str,
    outcome: str,
    tool_name: str | None = None,
) -> None:
    """Build and append one sensitive-read record (athenaeum#3176)."""
    append_audit_record(
        path,
        build_sensitive_read_record(
            principal=principal, vault=vault, outcome=outcome, tool_name=tool_name
        ),
    )
