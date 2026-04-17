# SPDX-License-Identifier: Apache-2.0
"""Athenaeum status — inspect current state of a knowledge base."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict

from athenaeum.librarian import discover_raw_files
from athenaeum.models import parse_frontmatter


class StatusInfo(TypedDict):
    """Shape of the dict returned by :func:`status`.

    Public API — downstream tooling (dashboards, CI gates) can import this
    for type-checked access to the status payload.
    """

    raw_pending: int
    entity_count: int
    entities_by_type: dict[str, int]
    last_commit_date: str
    last_commit_message: str
    pending_questions: int


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
            capture_output=True, text=True,
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

    return {
        "raw_pending": raw_pending,
        "entity_count": entity_count,
        "entities_by_type": entities_by_type,
        "last_commit_date": last_commit_date,
        "last_commit_message": last_commit_message,
        "pending_questions": pending_questions,
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

    if info["last_commit_date"]:
        lines.append(f"Last commit:          {info['last_commit_date']}")
        lines.append(f"  {info['last_commit_message']}")
    else:
        lines.append("Last commit:          (no git history)")

    return "\n".join(lines)
