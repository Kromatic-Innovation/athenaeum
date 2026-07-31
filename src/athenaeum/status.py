# SPDX-License-Identifier: Apache-2.0
"""Athenaeum status — read-only snapshot of a knowledge base, L4 domain/pipeline.

Contract: computes a point-in-time :class:`StatusInfo` snapshot (raw-intake
backlog, entity counts, pending-question count, oversized-page scan, drain
ETA advisory, schema-fragment attribution) by READING the knowledge
directory and git log — it never mutates anything. Factoring rule: any
check here must stay side-effect-free and cheap enough to run between
librarian runs (it backs both the ``athenaeum status`` CLI command and the
MCP status surface); a check that needs to WRITE (e.g. actually retiring a
stale page, actually running a drain) belongs in the module that owns that
mutation (``retire.py``, ``athenaeum.drain``), not here — this module may
only advise, never act.

SCC membership (L4, one of 8 mutually-recursive modules — librarian,
merge, tiers, pending_merges, batch, status, retire, wiki_dedupe — behaving
as one ~12,000-line module split for readability, not independence).
``status.py`` imports ``athenaeum.librarian.discover_raw_files`` and
``athenaeum.tiers.schema_fragment_state`` at TOP level — normal downward
dependencies. It is the OTHER half of the librarian<->status cycle:
``librarian.py`` itself function-locally imports this module's
``scan_page_sizes`` inside its run loop's page-size guardrail (~line 3108)
specifically because this module already imports librarian at top level,
so that side must defer or the package fails to import. ``status.py``
itself has no deferred imports of its own.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TypedDict

from athenaeum.config import (
    load_config,
    resolve_page_flag_bytes,
    resolve_page_warn_bytes,
)
from athenaeum.librarian import discover_raw_files
from athenaeum.models import parse_frontmatter
from athenaeum.tiers import schema_fragment_state

log = logging.getLogger(__name__)


class StatusInfo(TypedDict):
    """Shape of the dict returned by :func:`status`.

    Public API — downstream tooling (dashboards, CI gates) can import this
    for type-checked access to the status payload. Keys are only ever ADDED
    here (never renamed/removed) so existing consumers stay valid.
    """

    raw_pending: int
    entity_count: int
    entities_by_type: dict[str, int]
    last_commit_date: str
    last_commit_message: str
    pending_questions: int
    # Issue #310: wiki entity pages over the soft size thresholds, each a
    # ``(filename, byte_size)`` tuple sorted largest-first. ``pages_warn`` and
    # ``pages_flag`` are disjoint — a page over the flag threshold appears in
    # ``pages_flag`` only.
    pages_warn: list[tuple[str, int]]
    pages_flag: list[tuple[str, int]]
    # Issue #470: backlog-drain ETA advisory — a human sentence projecting
    # time-to-drain and naming the ``athenaeum drain`` remedy, or ``None`` when
    # the backlog is empty or its projected ETA is at/below
    # ``librarian.drain_warn_days``. Surfaces the same signal the end-of-run
    # WARNING emits, so status/MCP surfaces show it BETWEEN runs.
    drain_advisory: str | None
    # Issue #567: live-vs-default state of each operator-tunable schema fragment,
    # as ``{filename: (sha256_hex, is_default)}`` from
    # :func:`athenaeum.tiers.schema_fragment_state`. Surfaces which fragment
    # bytes are in play between runs, the same attribution the run-summary line
    # records; a fragment matching its bundled default has ``is_default=True``.
    schema_fragments: dict[str, tuple[str, bool]]


def scan_page_sizes(
    wiki_root: Path,
    warn_bytes: int,
    flag_bytes: int,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Bucket oversized wiki entity pages (issue #310).

    Walks the same ``wiki/*.md`` set the entity count walks — skipping
    ``_``-prefixed files and non-entity pages (no frontmatter ``name``) — and
    measures each page's UTF-8 size (frontmatter + body) in bytes. Returns
    ``(pages_warn, pages_flag)`` where each list holds ``(filename, byte_size)``
    tuples sorted largest-first. A page whose size exceeds ``flag_bytes`` lands
    only in ``pages_flag`` (not also in ``pages_warn``); a page over
    ``warn_bytes`` but at/under ``flag_bytes`` lands in ``pages_warn``.

    Guards an inverted config: if ``flag_bytes <= warn_bytes`` (which would make
    the flag bucket no stricter than warn and invert severity), ``flag_bytes``
    is clamped up to ``warn_bytes`` and a single ``WARNING`` names the
    misconfiguration. Aside from that guard it is observational — it reads,
    measures, and reports; it never modifies any file.
    """
    if flag_bytes <= warn_bytes:
        log.warning(
            "page_flag_bytes (%d) <= page_warn_bytes (%d): flag threshold "
            "clamped up to warn; fix librarian.page_flag_bytes / "
            "page_warn_bytes so flag > warn",
            flag_bytes,
            warn_bytes,
        )
        flag_bytes = max(flag_bytes, warn_bytes)

    pages_warn: list[tuple[str, int]] = []
    pages_flag: list[tuple[str, int]] = []
    if not wiki_root.exists():
        return pages_warn, pages_flag
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or not meta.get("name"):
            continue
        size = len(text.encode("utf-8"))
        if size > flag_bytes:
            pages_flag.append((fpath.name, size))
        elif size > warn_bytes:
            pages_warn.append((fpath.name, size))
    pages_warn.sort(key=lambda item: item[1], reverse=True)
    pages_flag.sort(key=lambda item: item[1], reverse=True)
    return pages_warn, pages_flag


def status(knowledge_root: Path) -> StatusInfo:
    """Gather status information about a knowledge base."""
    wiki_root = knowledge_root / "wiki"
    raw_root = knowledge_root / "raw"

    # Raw files pending
    raw_files = discover_raw_files(raw_root)
    raw_pending = len(raw_files)

    # Entity counts
    entities_by_type: dict[str, int] = {}
    entity_count = 0
    if wiki_root.exists():
        for fpath in sorted(wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta or not meta.get("name"):
                continue
            entity_count += 1
            etype = meta.get("type", "unknown")
            entities_by_type[etype] = entities_by_type.get(etype, 0) + 1

    # Last git commit
    last_commit_date = ""
    last_commit_message = ""
    if (knowledge_root / ".git").exists():
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ai|||%s"],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|||", 1)
            last_commit_date = parts[0].strip()
            last_commit_message = parts[1].strip() if len(parts) > 1 else ""

    # Pending questions
    pending_questions = 0
    pq_path = wiki_root / "_pending_questions.md"
    if pq_path.exists():
        text = pq_path.read_text(encoding="utf-8")
        pending_questions = text.count("## [")

    # Oversized wiki pages (issue #310). Thresholds come from config so an
    # operator can tune them; the scan is warn-only and never mutates anything.
    config = load_config(knowledge_root)
    warn_bytes = resolve_page_warn_bytes(config)
    flag_bytes = resolve_page_flag_bytes(config)
    pages_warn, pages_flag = scan_page_sizes(wiki_root, warn_bytes, flag_bytes)

    # Backlog-drain ETA advisor (issue #470): surface the same projection the
    # end-of-run WARNING emits, so status/MCP surfaces show it between runs.
    # Best-effort — a ledger/estimator hiccup must never break status.
    drain_advisory: str | None = None
    try:
        from athenaeum import drain as _drain
        from athenaeum import spend as _spend
        from athenaeum.config import resolve_drain_warn_days

        advisory = _drain.build_advisory(
            backlog=raw_pending,
            ledger_records=_spend.read_ledger(_spend.resolve_ledger_path(config)),
            warn_days=resolve_drain_warn_days(config),
            config=config,
        )
        if advisory is not None:
            drain_advisory = advisory.summary
    except Exception as exc:
        log.debug(
            "status: backlog-drain advisor skipped (%s): %s",
            type(exc).__name__,
            exc,
        )

    # Issue #567: schema-fragment divergence — which operator-tunable fragments
    # differ from the bundled default. Observational (read-only); mirrors the
    # attribution the librarian run-summary line records.
    schema_fragments = schema_fragment_state(wiki_root)

    return {
        "raw_pending": raw_pending,
        "entity_count": entity_count,
        "entities_by_type": entities_by_type,
        "last_commit_date": last_commit_date,
        "last_commit_message": last_commit_message,
        "pending_questions": pending_questions,
        "pages_warn": pages_warn,
        "pages_flag": pages_flag,
        "drain_advisory": drain_advisory,
        "schema_fragments": schema_fragments,
    }


def format_status(info: StatusInfo) -> str:
    """Format status dict as human-readable output."""
    lines = ["Athenaeum Status", "=" * 40]

    lines.append(f"Raw files pending:    {info['raw_pending']}")
    lines.append(f"Wiki entities:        {info['entity_count']}")

    if info["entities_by_type"]:
        for etype in sorted(info["entities_by_type"]):
            lines.append(f"  {etype}: {info['entities_by_type'][etype]}")

    lines.append(f"Pending questions:    {info['pending_questions']}")

    # Issue #470: backlog-drain ETA advisory. Use ``.get`` so pre-#470 status
    # dicts (missing the key) still format cleanly; shown only when set (a
    # non-empty backlog projected to exceed librarian.drain_warn_days).
    drain_advisory = info.get("drain_advisory")
    if drain_advisory:
        lines.append(f"Backlog drain:        {drain_advisory}")

    # Issue #310: oversized-page summary. Use ``.get`` so pre-#310 status
    # dicts (missing these keys) still format cleanly.
    pages_warn = info.get("pages_warn", [])
    pages_flag = info.get("pages_flag", [])
    lines.append(f"Oversized pages (warn/flag): {len(pages_warn)}/{len(pages_flag)}")
    for name, size in pages_flag:
        lines.append(f"  [flag] {name} ({size} bytes)")
    for name, size in pages_warn:
        lines.append(f"  [warn] {name} ({size} bytes)")

    # Issue #567: one divergence line per operator-tunable schema fragment —
    # ``default`` when it matches the bundled copy, ``edited (sha8 …)`` when the
    # live bytes differ. ``.get`` keeps pre-#567 status dicts formatting cleanly.
    schema_fragments = info.get("schema_fragments") or {}
    if schema_fragments:
        lines.append("Schema fragments:")
        for name in sorted(schema_fragments):
            sha_hex, is_default = schema_fragments[name]
            detail = "default" if is_default else f"edited (sha8 {sha_hex[:8]})"
            lines.append(f"  {name}: {detail}")

    if info["last_commit_date"]:
        lines.append(f"Last commit:          {info['last_commit_date']}")
        lines.append(f"  {info['last_commit_message']}")
    else:
        lines.append("Last commit:          (no git history)")

    return "\n".join(lines)
