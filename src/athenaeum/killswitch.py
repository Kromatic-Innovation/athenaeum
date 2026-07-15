# SPDX-License-Identifier: Apache-2.0
"""Kill switch — one discoverable, reversible way to stop athenaeum (issue #379).

Athenaeum runs unattended background work: the ``session-end`` compile pass
(which fans out ``claude -p`` contradiction detectors), the per-turn recall
hooks, and the MCP capture/resolve tools. Before this module the only way to
stop it was to hand-edit the three hook commands out of the operator's global
``~/.claude/settings.json`` and ``pkill`` the in-flight detectors — undiscoverable,
error-prone, and reversible only by remembering exactly what was edited (#379).

The kill switch replaces that with a single state file
(``$ATHENAEUM_CACHE_DIR/disabled``, default ``~/.cache/athenaeum/disabled``) plus
an ``ATHENAEUM_DISABLED`` environment override that **every entry point checks** —
the CLI (``session-end``), the MCP server tools, and the shell hooks (which read
the same file directly with plain ``grep`` so they add no Python startup cost on
the per-turn recall path).

Two scopes:

``all``
    Everything off — compile, contradiction detection, recall, capture,
    notifications. Written by ``athenaeum disable``.

``compile``
    Only the expensive compile/detect pass off; recall stays on. Written by
    ``athenaeum disable --compile``.

The env override wins over the file. ``ATHENAEUM_DISABLED`` values ``1`` / ``true``
/ ``yes`` / ``on`` / ``all`` mean scope ``all``; ``compile`` means scope
``compile``; ``0`` / ``false`` / ``no`` / ``off`` / unset / empty / anything
unrecognised means "defer to the state file" — an explicit ``ATHENAEUM_DISABLED=0``
does NOT force-enable past a state file, it just declines to force-disable (the
file is the durable state).

The shell hooks in ``examples/claude-code/`` reimplement :func:`is_disabled`
with ``aspect="recall"`` in a few lines of ``bash``; keep the two in sync — the
file format is deliberately trivial (presence ⇒ ``all`` unless the JSON/text
says ``compile``) so a plain ``grep`` and this reader agree.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text

SCOPE_ALL = "all"
SCOPE_COMPILE = "compile"
VALID_SCOPES = (SCOPE_ALL, SCOPE_COMPILE)

ENV_VAR = "ATHENAEUM_DISABLED"
_ENV_ALL = frozenset({"1", "true", "yes", "on", "all"})
_ENV_COMPILE = frozenset({"compile"})


@dataclass(frozen=True)
class DisabledState:
    """Resolved kill-switch state.

    ``disabled`` is the top-level flag; ``scope`` is ``"all"`` | ``"compile"``
    | ``None``; ``source`` is ``"env"`` | ``"file"`` | ``None`` (records which
    input decided the state, for :func:`format_status_line`). ``since`` /
    ``reason`` are populated only from a file-backed state.
    """

    disabled: bool
    scope: str | None = None
    source: str | None = None
    since: str | None = None
    reason: str | None = None


def state_path(cache_dir: Path | None = None) -> Path:
    """Path to the kill-switch state file.

    Mirrors :func:`athenaeum.librarian._resolve_cache_dir` precedence
    (arg > ``ATHENAEUM_CACHE_DIR`` env > ``~/.cache/athenaeum``) so the state
    file always lands where the rest of athenaeum keeps its cache — and where
    the shell hooks look for it.
    """
    if cache_dir is not None:
        base = Path(cache_dir).expanduser()
    else:
        base = Path(
            os.environ.get("ATHENAEUM_CACHE_DIR")
            or (Path.home() / ".cache" / "athenaeum")
        ).expanduser()
    return base / "disabled"


def _env_scope() -> str | None:
    """Effective scope forced by ``ATHENAEUM_DISABLED``, or ``None`` to defer."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _ENV_COMPILE:
        return SCOPE_COMPILE
    if val in _ENV_ALL:
        return SCOPE_ALL
    return None  # off / empty / unrecognised -> defer to the file


def _read_file_state(path: Path) -> DisabledState | None:
    """Read a file-backed state, or ``None`` when no (readable) file exists.

    Tolerant by design: a present-but-empty or hand-created file counts as
    scope ``all`` (an emergency ``touch $cache/disabled`` must turn everything
    off), and only an explicit ``compile`` scope narrows it. Accepts either the
    JSON this module writes or a plain first-line scope token.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # missing / unreadable -> not disabled by file

    scope = SCOPE_ALL
    since: str | None = None
    reason: str | None = None
    stripped = raw.strip()
    if stripped:
        data: object = None
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            token = str(data.get("scope", SCOPE_ALL)).strip().lower()
            scope = token if token in VALID_SCOPES else SCOPE_ALL
            raw_since = data.get("since")
            raw_reason = data.get("reason")
            since = str(raw_since) if raw_since else None
            reason = str(raw_reason) if raw_reason else None
        else:
            token = stripped.splitlines()[0].strip().lower()
            scope = token if token in VALID_SCOPES else SCOPE_ALL
    return DisabledState(
        disabled=True, scope=scope, source="file", since=since, reason=reason
    )


def current_state(cache_dir: Path | None = None) -> DisabledState:
    """Resolve the effective kill-switch state (env override > file > enabled)."""
    env = _env_scope()
    if env is not None:
        return DisabledState(disabled=True, scope=env, source="env")
    file_state = _read_file_state(state_path(cache_dir))
    if file_state is not None:
        return file_state
    return DisabledState(disabled=False)


def is_disabled(aspect: str = "all", *, cache_dir: Path | None = None) -> bool:
    """Return ``True`` when *aspect* of athenaeum is currently disabled.

    ``aspect``:
        ``"compile"``
            The expensive ``session-end`` compile / contradiction-detection
            fan-out. Disabled when the effective scope is ``all`` OR ``compile``.
        ``"recall"`` / ``"capture"`` / ``"all"`` (default)
            The recall hooks and MCP capture/resolve tools. Disabled only when
            the effective scope is ``all`` (``compile`` leaves recall on).
    """
    state = current_state(cache_dir)
    if not state.disabled:
        return False
    if aspect == "compile":
        return state.scope in (SCOPE_ALL, SCOPE_COMPILE)
    return state.scope == SCOPE_ALL


def disable(
    scope: str = SCOPE_ALL,
    *,
    reason: str | None = None,
    cache_dir: Path | None = None,
) -> Path:
    """Write the state file for *scope* and return its path.

    Idempotent: re-disabling overwrites the previous state (e.g. narrowing
    ``all`` -> ``compile`` or vice versa). Raises :class:`ValueError` on an
    unknown scope.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
    path = state_path(cache_dir)
    payload = {
        "scope": scope,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def enable(*, cache_dir: Path | None = None) -> bool:
    """Remove the state file. Returns ``True`` if a file was actually removed.

    Does not (and cannot) clear an ``ATHENAEUM_DISABLED`` env override — callers
    should check :func:`current_state` afterwards and warn if the env still
    forces a disabled state.
    """
    path = state_path(cache_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def format_status_line(cache_dir: Path | None = None) -> str:
    """One-block human summary of the kill-switch state for ``athenaeum status``."""
    state = current_state(cache_dir)
    if not state.disabled:
        return "Kill switch:          enabled (athenaeum is running normally)"
    if state.scope == SCOPE_COMPILE:
        what = "compile/detect pass OFF, recall ON"
    else:
        what = "ALL background work OFF"
    lines = [f"Kill switch:          DISABLED — {what}"]
    detail = f"  scope={state.scope} source={state.source}"
    if state.since:
        detail += f" since={state.since}"
    lines.append(detail)
    if state.reason:
        lines.append(f"  reason: {state.reason}")
    if state.source == "env":
        lines.append(f"  (forced by {ENV_VAR}; unset it to defer to the state file)")
    else:
        lines.append("  run 'athenaeum enable' to restore")
    return "\n".join(lines)
