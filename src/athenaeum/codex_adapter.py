# SPDX-License-Identifier: Apache-2.0
"""Tier-2 static context-file adapter for OpenAI Codex (issue athenaeum#1364).

Renders one periodic snapshot of Athenaeum recall to a plain markdown file
(default ``~/.codex/athenaeum-context.md``) that ``~/.codex/AGENTS.md``
points Codex at. Built against the documented sidecar contract
(``docs/extending/sidecar-adapter-contract.md`` §3.1) and changes nothing in
the core.

Contract shape (§3.1): one :func:`athenaeum.context.build_context` call per
refresh with an empty ``exclude`` set and ``use_llm=False``; the envelope's
``render.text`` is written verbatim; nothing but that text, the backend name
and adapter-owned header lines reach disk (§1.1). The per-turn wrapper and
its seen-file bookkeeping are deliberately NOT used: a snapshot replaced each
cycle would shrink to empty.

Findings against the contract (none needs a core change):

- F1: push telemetry is shaped for Tier 1 (hard-coded source, needs a session
  id), so a Tier-2 refresh records none.
- F2: the overflow breadcrumb lives inside ``render.text``; it is written
  verbatim and the adapter's own preamble points at the ``athenaeum recall``
  CLI so that line stays actionable.
- F3: the contract does not distinguish a file the host loads from a file the
  agent is told to read; this adapter is the second kind.

Egress (option B, fail closed): if any candidate's audience is non-public the
whole snapshot is withheld. The decision is whole-snapshot; no bullet is ever
filtered or re-rendered (§2.2).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import RECALL_CAP_CEILING_DEFAULT
from athenaeum.context import build_context
from athenaeum.killswitch import is_disabled
from athenaeum.models import parse_audience_index_string

RENDER_VERSION = "v1"
SESSION_ID = "codex-context-refresh"
DEFAULT_OUTPUT = Path("~/.codex/athenaeum-context.md")
DEFAULT_INTERVAL_SECONDS = 900

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DISABLED = "disabled"
STATUS_FAILED = "refresh-failed"
STATUS_WITHHELD = "withheld"

HEADER_MARKER = (
    f"<!-- athenaeum:codex-context {RENDER_VERSION} — machine-written by "
    "`athenaeum codex-context`; replaced wholesale on every refresh; do not edit -->"
)
END_MARKER = "<!-- athenaeum:codex-context end -->"

FRESHNESS_RULE = (
    "Freshness rule: this is a periodic snapshot, not a per-turn lookup. If the "
    "current UTC time is later than stale_after, treat everything below as STALE: "
    'do not rely on it as current, and run `athenaeum recall "<topic>"` for live '
    "results."
)
PREAMBLE = (
    "Wiki pages relevant to the configured topics (snapshot; run "
    '`athenaeum recall "<topic>"` for details):'
)

_BODIES = {
    STATUS_EMPTY: "No wiki pages matched the configured topics at generated_at.",
    STATUS_DISABLED: "Athenaeum recall is disabled (kill switch); no context is being pushed.",
    STATUS_FAILED: (
        "STALE: the last refresh failed; no snapshot is available. "
        "Run `athenaeum recall` for live results."
    ),
    STATUS_WITHHELD: "Snapshot withheld: at least one matching page is not public.",
}


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_snapshot(
    status: str,
    *,
    render_text: str,
    backend: str,
    interval_seconds: int,
    generated_at: datetime,
    stale_after: datetime,
) -> str:
    """Pure render of the snapshot file. Only ``ok`` carries bullets."""
    body = f"{PREAMBLE}\n{render_text}" if status == STATUS_OK else _BODIES[status]
    return (
        "\n".join(
            [
                HEADER_MARKER,
                "# Athenaeum knowledge snapshot",
                "",
                f"- status: {status}",
                f"- refresh_interval_seconds: {interval_seconds}",
                f"- generated_at: {_utc(generated_at)}",
                f"- stale_after: {_utc(stale_after)}",
                f"- backend: {backend}",
                "",
                FRESHNESS_RULE,
                "",
                body,
                END_MARKER,
            ]
        )
        + "\n"
    )


def _has_non_public(candidates: list[dict[str, Any]]) -> bool:
    for cand in candidates:
        _roles, is_public = parse_audience_index_string(str(cand.get("audience", "") or ""))
        if not is_public:
            return True
    return False


def _classify(envelope: dict[str, Any], cache_dir: Path) -> str:
    candidates = envelope.get("candidates") or []
    if candidates:
        return STATUS_WITHHELD if _has_non_public(candidates) else STATUS_OK
    if is_disabled("recall", cache_dir=cache_dir):
        return STATUS_DISABLED
    return STATUS_EMPTY


def refresh(
    seed: str,
    *,
    cache_dir: Path,
    output: Path | None = None,
    n: int | None = None,
    backend: str = "fts5",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    max_age_seconds: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    """Run one refresh and write the snapshot. Returns the process exit code.

    ``output=None`` means the default path; a path of ``-`` prints to stdout.
    """
    generated_at = now()
    max_age = max_age_seconds if max_age_seconds is not None else 2 * interval_seconds
    stale_after = generated_at + timedelta(seconds=max_age)
    target = output if output is not None else DEFAULT_OUTPUT

    code = 0
    try:
        envelope = build_context(
            seed,
            SESSION_ID,
            cache_dir=cache_dir,
            n=n if n is not None else RECALL_CAP_CEILING_DEFAULT,
            search_backend=backend,
            exclude=frozenset(),
            use_llm=False,
        )
        status = _classify(envelope, cache_dir)
        text = render_snapshot(
            status,
            render_text=str((envelope.get("render") or {}).get("text", "")),
            backend=str(envelope.get("backend", backend)),
            interval_seconds=interval_seconds,
            generated_at=generated_at,
            stale_after=stale_after,
        )
    except Exception as exc:  # noqa: BLE001 — any failure becomes a stale tombstone
        print(f"codex-context: refresh failed: {type(exc).__name__}", file=sys.stderr)
        code = 1
        text = render_snapshot(
            STATUS_FAILED,
            render_text="",
            backend=backend,
            interval_seconds=interval_seconds,
            generated_at=generated_at,
            stale_after=generated_at,
        )

    if str(target) == "-":
        sys.stdout.write(text)
    else:
        atomic_write_text(Path(target).expanduser(), text)
    return code
