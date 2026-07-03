# SPDX-License-Identifier: Apache-2.0
"""Pending-merge proposal sidecar (issue #169, Lane 3).

Mirrors the ``_pending_questions.md`` sidecar but for resolver-proposed
memory merges. When the resolver returns ``action="propose_merge"``, the
proposal is appended to ``wiki/_pending_merges.md`` for human approval —
NOT auto-applied.

Block format (mirrors ``_pending_questions.md``):

::

    ## [YYYY-MM-DD] Merge: "<merge-target-name>"
    - [ ] Approve this merge? Sources: <path-a>, <path-b>
    **Rationale**: <one sentence>
    **Sources**:
    - <absolute path to source memory a>
    - <absolute path to source memory b>
    **Confidence**: 0.92
    **Draft**:
    ```markdown
    <draft_merged_body>
    ```

The human approves by:

1. Flipping ``- [ ]`` to ``- [x]`` and calling
   :func:`resolve_merge` ("approve") via the MCP tool, OR
2. Calling :func:`resolve_merge` ("reject") with a note. Rejection
   writes a ``refines:`` declaration into one source file so the
   detector's declared-relationship short-circuit stops re-flagging
   the pair (see :mod:`athenaeum.merge` Lane 1 / #167).

On the next ``athenaeum ingest-answers`` run (or a dedicated
``ingest-merges`` invocation), approved/rejected blocks are moved to
``_pending_merges_archive.md``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from athenaeum.models import parse_frontmatter, render_frontmatter, slugify

log = logging.getLogger(__name__)


# Header grammar — ``## [ISO-DATE] Merge: "{name}"``.
_HEADER_RE = re.compile(
    r"^## \[(?P<date>[^\]]+)\] Merge: \"" r"(?P<target>(?:[^\"\\]|\\.)*)" r"\"$"
)
_CHECKBOX_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s*(?P<question>.*)$")


@dataclass
class PendingMerge:
    """Parsed view of one block in ``_pending_merges.md``."""

    id: str
    merge_target_name: str
    sources: list[str]
    rationale: str
    draft_merged_body: str
    confidence: float
    created_at: str
    resolved: bool
    raw_block: str
    decision: Literal["approve", "reject", ""] = ""
    note: str = ""
    also_affects: list[str] = field(default_factory=list)


def _make_id(sources: list[str], target_name: str) -> str:
    """Stable id derived from source paths + merge target name.

    Stability contract: id stable across rationale/draft edits; changes
    when the source set or target name changes.
    """
    key = "\n".join(sorted(sources)) + "\n" + target_name.strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _escape_quotes(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape_quotes(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def render_block(
    *,
    merge_target_name: str,
    sources: list[str],
    rationale: str,
    draft_merged_body: str,
    confidence: float,
    created_at: str | None = None,
) -> str:
    """Render one pending-merge block as markdown."""
    today = created_at or date.today().isoformat()
    target_escaped = _escape_quotes(merge_target_name)
    sources_line = ", ".join(Path(s).name for s in sources) or "(none)"
    parts: list[str] = [
        f'## [{today}] Merge: "{target_escaped}"',
        f"- [ ] Approve this merge? Sources: {sources_line}",
        "",
        f"**Rationale**: {rationale or '(none provided)'}",
        "**Sources**:",
    ]
    for src in sources:
        parts.append(f"- {src}")
    parts.append(f"**Confidence**: {confidence:.2f}")
    parts.append("**Draft**:")
    parts.append("```markdown")
    parts.append(draft_merged_body.rstrip("\n"))
    parts.append("```")
    return "\n".join(parts)


def _split_blocks(text: str) -> list[str]:
    """Split ``_pending_merges.md`` text into per-merge blocks.

    A block's ``**Draft**:`` field is a fenced ```` ```markdown ... ``` ````
    section whose CONTENTS may legitimately contain bare ``---`` lines
    (YAML frontmatter) or ``## `` subheadings. While inside that fence,
    lines are never treated as block/paragraph delimiters — they are
    always appended as content, mirroring the fence-tracking already
    done downstream in :func:`_parse_block`.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_fence:
            if stripped == "```":
                in_fence = False
                if current:
                    current.append(line)
                continue
            if _HEADER_RE.match(line):
                # A canonical block header (``## [DATE] Merge: "name"``)
                # appearing while a fence is still "open" means a prior
                # block's ```markdown fence was left unclosed (malformed
                # input) — real headers never legitimately appear inside
                # fenced draft content. Recover the boundary here instead
                # of silently swallowing every subsequent block into the
                # malformed one.
                log.warning(
                    "pending_merges: unclosed ```markdown fence before "
                    "block header %r; recovering block boundary",
                    line[:80],
                )
                in_fence = False
                if current:
                    blocks.append("\n".join(current).rstrip())
                current = [line]
                continue
            if current:
                current.append(line)
            continue
        if stripped == "```markdown":
            in_fence = True
            if current:
                current.append(line)
            continue
        if line.startswith("## "):
            if current:
                blocks.append("\n".join(current).rstrip())
            current = [line]
        elif stripped == "---":
            if current:
                blocks.append("\n".join(current).rstrip())
                current = []
        else:
            if current:
                current.append(line)
    if in_fence:
        log.warning(
            "pending_merges: reached end of file with an unclosed "
            "```markdown fence in the last block; flushing anyway"
        )
    if current:
        blocks.append("\n".join(current).rstrip())
    return [b for b in blocks if b.startswith("## ")]


def _parse_block(block_text: str) -> PendingMerge | None:
    lines = block_text.splitlines()
    if not lines:
        return None
    header_match = _HEADER_RE.match(lines[0])
    if not header_match:
        log.warning("Skipping merge block with malformed header: %r", lines[0][:80])
        return None
    target_name = _unescape_quotes(header_match.group("target"))
    created_at = header_match.group("date")

    # First non-blank checkbox line determines resolved state.
    resolved = False
    cb_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "":
            continue
        m = _CHECKBOX_RE.match(lines[idx])
        if m:
            resolved = m.group("state").lower() == "x"
            cb_idx = idx
        break
    if cb_idx is None:
        log.warning("Skipping merge block without checkbox: %r", lines[0][:80])
        return None

    rationale = ""
    confidence = 0.0
    sources: list[str] = []
    draft_lines: list[str] = []
    decision = ""
    note = ""

    in_sources = False
    in_draft = False
    in_fence = False

    for raw_line in lines[cb_idx + 1 :]:
        s = raw_line.strip()
        if in_draft:
            if not in_fence and s == "```markdown":
                in_fence = True
                continue
            if in_fence and s == "```":
                in_fence = False
                in_draft = False
                continue
            if in_fence:
                draft_lines.append(raw_line)
                continue
            # Fence opened with no leading marker — accept any content
            # until the next ``**Key**:`` line or block end.
            if s.startswith("**"):
                in_draft = False
            else:
                draft_lines.append(raw_line)
                continue
        if s.startswith("**Rationale**:"):
            in_sources = False
            rationale = s.removeprefix("**Rationale**:").strip()
            continue
        if s.startswith("**Confidence**:"):
            in_sources = False
            raw = s.removeprefix("**Confidence**:").strip()
            try:
                confidence = float(raw)
            except (TypeError, ValueError):
                confidence = 0.0
            continue
        if s.startswith("**Sources**:"):
            in_sources = True
            continue
        if s.startswith("**Draft**:"):
            in_sources = False
            in_draft = True
            in_fence = False
            continue
        if s.startswith("**Decision**:"):
            in_sources = False
            decision = s.removeprefix("**Decision**:").strip()
            continue
        if s.startswith("**Note**:"):
            in_sources = False
            note = s.removeprefix("**Note**:").strip()
            continue
        if in_sources and s.startswith("- "):
            sources.append(s[2:].strip())
            continue
        if in_sources and not s:
            continue
        if in_sources:
            in_sources = False

    draft_body = "\n".join(draft_lines).strip("\n")

    return PendingMerge(
        id=_make_id(sources, target_name),
        merge_target_name=target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_body,
        confidence=confidence,
        created_at=created_at,
        resolved=resolved,
        raw_block=block_text,
        decision=decision if decision in ("approve", "reject") else "",
        note=note,
    )


def parse_pending_merges(merges_path: Path) -> list[PendingMerge]:
    """Parse ``_pending_merges.md`` into :class:`PendingMerge` objects."""
    if not merges_path.exists():
        return []
    text = merges_path.read_text(encoding="utf-8")
    return [pm for b in _split_blocks(text) if (pm := _parse_block(b)) is not None]


def write_pending_merge(
    merges_path: Path,
    *,
    merge_target_name: str,
    sources: list[str],
    rationale: str,
    draft_merged_body: str,
    confidence: float,
    created_at: str | None = None,
) -> str:
    """Append one merge-proposal block to ``_pending_merges.md``.

    Returns the rendered block text (without surrounding separator).
    Creates the file lazily with a ``# Pending Merges`` header. Idempotent:
    if a block with the same id already exists in the file (resolved or
    not), nothing is appended.
    """
    block = render_block(
        merge_target_name=merge_target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_merged_body,
        confidence=confidence,
        created_at=created_at,
    )
    block_id = _make_id(sources, merge_target_name)

    if merges_path.exists():
        text = merges_path.read_text(encoding="utf-8")
        existing_ids = {pm.id for pm in parse_pending_merges(merges_path)}
        if block_id in existing_ids:
            log.info("pending_merges: id %s already present; skipping", block_id)
            return block
        combined = text.rstrip() + "\n\n---\n\n" + block + "\n"
    else:
        merges_path.parent.mkdir(parents=True, exist_ok=True)
        combined = "# Pending Merges\n\n" + block + "\n"
    merges_path.write_text(combined, encoding="utf-8")
    return block


def list_pending_merges(merges_path: Path) -> list[dict]:
    """Return unresolved merges as MCP-friendly dicts."""
    return [
        {
            "id": pm.id,
            "merge_target_name": pm.merge_target_name,
            "sources": list(pm.sources),
            "rationale": pm.rationale,
            "draft_merged_body": pm.draft_merged_body,
            "confidence": pm.confidence,
            "created_at": pm.created_at,
        }
        for pm in parse_pending_merges(merges_path)
        if not pm.resolved
    ]


def _rewrite_block_resolved(
    block_text: str,
    decision: Literal["approve", "reject"],
    note: str,
) -> str:
    """Flip the checkbox and tag the block with decision + note."""
    lines = block_text.splitlines()
    new_lines: list[str] = []
    flipped = False
    for line in lines:
        if not flipped:
            m = _CHECKBOX_RE.match(line)
            if m:
                new_lines.append(f"- [x] {m.group('question').strip()}")
                flipped = True
                continue
        new_lines.append(line)
    new_lines.append("")
    new_lines.append(f"**Decision**: {decision}")
    if note:
        new_lines.append(f"**Note**: {note}")
    return "\n".join(new_lines).rstrip() + "\n"


def _add_refines_declaration(source_path: Path, other_name: str) -> bool:
    """Append ``other_name`` to ``refines:`` in ``source_path``'s frontmatter.

    Used by ``resolve_merge(reject)`` so Lane 1's declared-refinement
    short-circuit suppresses future detector firings on this pair.
    Returns True when the file was modified.
    """
    if not source_path.is_file():
        log.warning("pending_merges: source file missing: %s", source_path)
        return False
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    target_slug = slugify(other_name)
    refines_raw = meta.get("refines")
    if isinstance(refines_raw, list):
        existing = [str(r) for r in refines_raw]
    elif isinstance(refines_raw, str) and refines_raw.strip():
        existing = [refines_raw.strip()]
    else:
        existing = []
    if any(slugify(r) == target_slug for r in existing):
        return False
    existing.append(other_name)
    meta["refines"] = existing
    new_text = render_frontmatter(meta) + body
    source_path.write_text(new_text, encoding="utf-8")
    return True


def resolve_merge(
    merges_path: Path,
    merge_id: str,
    decision: Literal["approve", "reject"],
    note: str = "",
    *,
    wiki_root: Path | None = None,
) -> dict:
    """Mark a pending-merge block as resolved.

    Args:
        merges_path: Path to ``_pending_merges.md``.
        merge_id: Id returned by :func:`list_pending_merges`.
        decision: ``"approve"`` writes ``wiki/<target-slug>.md`` (or under
            ``wiki_root`` when supplied) with ``draft_merged_body`` and
            then flips the checkbox; the source memories are NOT archived
            here — the human reviews the wiki write before any source
            deletion. ``"reject"`` flips the checkbox and writes a
            ``refines:`` declaration into the first source memory so the
            detector's declared-refinement short-circuit suppresses the
            pair on future runs.
        note: Optional human note attached to the decision block.
        wiki_root: Optional wiki root override (defaults to
            ``merges_path.parent``).

    Returns:
        ``{"ok": bool, "error_code": str | None, "message": str,
           "resolved_block": str | None}``.
    """
    if decision not in ("approve", "reject"):
        return {
            "ok": False,
            "error_code": "invalid_decision",
            "message": f"decision must be 'approve' or 'reject', got {decision!r}",
            "resolved_block": None,
        }
    if not merges_path.exists():
        return {
            "ok": False,
            "error_code": "file_missing",
            "message": f"pending merges file not found: {merges_path}",
            "resolved_block": None,
        }
    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": "no pending merge blocks in file",
            "resolved_block": None,
        }

    target_pm: PendingMerge | None = None
    resolved_text: str | None = None
    rewritten: list[str] = []
    for block_text in blocks:
        pm = _parse_block(block_text)
        if pm is None or pm.id != merge_id:
            rewritten.append(block_text)
            continue
        if pm.resolved:
            return {
                "ok": False,
                "error_code": "already_resolved",
                "message": f"merge {merge_id} already resolved",
                "resolved_block": None,
            }
        target_pm = pm
        resolved_text = _rewrite_block_resolved(block_text, decision, note)
        rewritten.append(resolved_text)

    if target_pm is None:
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": f"merge id not found: {merge_id}",
            "resolved_block": None,
        }

    warning: str | None = None

    # Apply the side-effect tied to the decision BEFORE flushing the file.
    if decision == "approve":
        root = wiki_root or merges_path.parent
        root.mkdir(parents=True, exist_ok=True)
        target_path = root / f"{slugify(target_pm.merge_target_name)}.md"
        if target_path.exists():
            # Fail closed: do NOT flip the checkbox; the human must rename
            # the merge_target_name or resolve the existing wiki entry.
            return {
                "ok": False,
                "error_code": "target_exists",
                "message": (
                    f"{target_path} already exists; rename merge_target_name "
                    "or resolve the existing memory first"
                ),
                "resolved_block": None,
            }
        target_path.write_text(target_pm.draft_merged_body, encoding="utf-8")
    elif decision == "reject" and len(target_pm.sources) >= 2:
        # Write a `refines:` declaration into the first source memory
        # naming the second one. Lane 1 / #167's declared-refinement
        # short-circuit then suppresses the pair on future detector runs.
        src_a = Path(target_pm.sources[0])
        src_b = Path(target_pm.sources[1])
        # Prefer source B's frontmatter `name:` so renames / custom slugs
        # round-trip; fall back to the filename stem (minus conventional
        # prefix) only when the frontmatter is missing/unreadable.
        other_name: str | None = None
        if src_b.is_file():
            try:
                b_text = src_b.read_text(encoding="utf-8")
                b_meta, _ = parse_frontmatter(b_text)
                if isinstance(b_meta, dict):
                    raw_name = b_meta.get("name")
                    if isinstance(raw_name, str) and raw_name.strip():
                        other_name = raw_name.strip()
            except (OSError, UnicodeDecodeError):
                other_name = None
        if other_name is None:
            other_stem = src_b.stem
            for prefix in (
                "feedback_",
                "project_",
                "reference_",
                "user_",
                "recall_",
            ):
                if other_stem.startswith(prefix):
                    other_stem = other_stem[len(prefix) :]
                    break
            other_name = other_stem
        if not src_a.is_file():
            warning = (
                "refines_write_failed: source A unavailable or unwritable; "
                "merge will re-propose on next run"
            )
        else:
            try:
                _add_refines_declaration(src_a, other_name)
            except OSError:
                warning = (
                    "refines_write_failed: source A unavailable or "
                    "unwritable; merge will re-propose on next run"
                )

    primary_parts = ["# Pending Merges", *rewritten]
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    merges_path.write_text(primary_body, encoding="utf-8")

    response: dict = {
        "ok": True,
        "error_code": None,
        "message": "ok",
        "resolved_block": resolved_text,
    }
    if warning is not None:
        response["warning"] = warning
    return response


def ingest_resolved_merges(merges_path: Path) -> int:
    """Move resolved (``[x]``) blocks from primary file to archive.

    Same shape as :func:`athenaeum.answers.ingest_answers` for the
    questions sidecar. Idempotent. Returns the number of merges archived
    on this run.
    """
    if not merges_path.exists():
        return 0
    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        return 0

    archive_path = merges_path.parent / "_pending_merges_archive.md"
    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    remaining: list[str] = []
    archived: list[str] = []
    for block_text in blocks:
        pm = _parse_block(block_text)
        if pm is None or not pm.resolved:
            remaining.append(block_text)
            continue
        archived.append(f"{block_text}\n\n**Archived**: {iso_ts}\n")

    if not archived:
        return 0

    primary_parts = ["# Pending Merges", *remaining]
    merges_path.write_text(
        "\n\n---\n\n".join(primary_parts) + "\n",
        encoding="utf-8",
    )

    existing_archive = ""
    if archive_path.exists():
        existing_archive = archive_path.read_text(encoding="utf-8")
    new_section = "\n\n---\n\n".join(archived)
    if existing_archive.strip():
        if existing_archive.startswith("# Archived Merges"):
            _, _, rest = existing_archive.partition("\n")
            combined = (
                "# Archived Merges\n\n" + new_section + "\n\n---\n\n" + rest.lstrip()
            )
        else:
            combined = (
                "# Archived Merges\n\n"
                + new_section
                + "\n\n---\n\n"
                + existing_archive.lstrip()
            )
    else:
        combined = "# Archived Merges\n\n" + new_section + "\n"
    archive_path.write_text(combined, encoding="utf-8")
    return len(archived)
