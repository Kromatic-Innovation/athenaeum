# SPDX-License-Identifier: Apache-2.0
"""Athenaeum status — inspect current state of a knowledge base."""

from __future__ import annotations

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


def scan_page_sizes(
    wiki_root: Path,
    warn_bytes: int,
    flag_bytes: int,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Bucket oversized wiki entity pages (issue #310).

    Walks the same ``wiki/*.md`` set the entity count walks — skipping
    ``_``-prefixed files and non-entity pages (no frontmatter ``name``) — and
    measures each page's UTF-8 body length in bytes. Returns
    ``(pages_warn, pages_flag)`` where each list holds ``(filename, byte_size)``
    tuples sorted largest-first. A page whose size exceeds ``flag_bytes`` lands
    only in ``pages_flag`` (not also in ``pages_warn``); a page over
    ``warn_bytes`` but at/under ``flag_bytes`` lands in ``pages_warn``. Purely
    observational — it reads, measures, and reports; it never modifies or logs.
    """
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

    return {
        "raw_pending": raw_pending,
        "entity_count": entity_count,
        "entities_by_type": entities_by_type,
        "last_commit_date": last_commit_date,
        "last_commit_message": last_commit_message,
        "pending_questions": pending_questions,
        "pages_warn": pages_warn,
        "pages_flag": pages_flag,
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

    # Issue #310: oversized-page summary. Use ``.get`` so pre-#310 status
    # dicts (missing these keys) still format cleanly.
    pages_warn = info.get("pages_warn", [])
    pages_flag = info.get("pages_flag", [])
    lines.append(f"Oversized pages (warn/flag): {len(pages_warn)}/{len(pages_flag)}")
    for name, size in pages_flag:
        lines.append(f"  [flag] {name} ({size} bytes)")
    for name, size in pages_warn:
        lines.append(f"  [warn] {name} ({size} bytes)")

    if info["last_commit_date"]:
        lines.append(f"Last commit:          {info['last_commit_date']}")
        lines.append(f"  {info['last_commit_message']}")
    else:
        lines.append("Last commit:          (no git history)")

    return "\n".join(lines)
