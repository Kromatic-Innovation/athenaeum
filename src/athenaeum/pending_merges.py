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

Nested fences in a Draft body
------------------------------

A Draft body is written between a ```` ```markdown ```` opener and a
bare ```` ``` ```` closer (three backticks). If the draft content itself
needs a fenced snippet (e.g. documenting a shell command), that inner
fence MUST use a different backtick-run length than three — four
backticks (` ```` `) by convention — so it cannot be mistaken for the
outer fence's closer. See :func:`_scan_fence_state` (#292).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, render_frontmatter, slugify

log = logging.getLogger(__name__)


# Header grammar — ``## [ISO-DATE] Merge: "{name}"``.
_HEADER_RE = re.compile(
    r"^## \[(?P<date>[^\]]+)\] Merge: \"" r"(?P<target>(?:[^\"\\]|\\.)*)" r"\"$"
)
_CHECKBOX_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s*(?P<question>.*)$")

# ```markdown fence-opener / bare-fence-closer, shared by _split_blocks and
# _parse_block (#292) so the two block-boundary state machines can't diverge
# on what counts as "inside a fenced Draft body".
_FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,})markdown$")
_FENCE_CLOSE_RE = re.compile(r"^(?P<fence>`{3,})$")


def _scan_fence_state(line: str, fence_len: int) -> int:
    """Return the updated open-fence backtick-length after ``line``.

    ``fence_len`` is the backtick count of the currently open
    ```markdown fence, or ``0`` when no fence is open. Used identically
    by :func:`_split_blocks` and :func:`_parse_block` so a Draft body's
    fence boundaries are recognized the same way in both places.

    A fence only closes on a bare-backtick line whose length EXACTLY
    matches the opening fence's length (not CommonMark's "at least as
    many" rule). This lets a Draft body nest its own fenced snippet by
    opening it with a *different* backtick-run length than the
    enclosing ```markdown fence (e.g. a four-backtick inner fence
    inside the three-backtick outer fence) without prematurely closing
    the outer fence — see the module docstring's nested-fence
    convention.
    """
    stripped = line.strip()
    if fence_len:
        close_match = _FENCE_CLOSE_RE.match(stripped)
        if close_match and len(close_match.group("fence")) == fence_len:
            return 0
        return fence_len
    open_match = _FENCE_OPEN_RE.match(stripped)
    return len(open_match.group("fence")) if open_match else 0


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
    # Issue #421: mechanical slug-collision classification recorded at proposal
    # time. ``create-merged`` (slug free) or ``fold-into-existing`` (slug taken
    # by an existing wiki page). Pre-#421 blocks lack the line and default to
    # ``create-merged``.
    write_kind: str = "create-merged"


def _make_id(sources: list[str], target_name: str) -> str:
    """Stable id derived from source paths + merge target name.

    Stability contract: id stable across rationale/draft edits; changes
    when the source set or target name changes.
    """
    key = "\n".join(sorted(sources)) + "\n" + target_name.strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _outer_draft_fence(draft_body: str) -> str:
    """Pick a ```markdown fence run longer than any backtick run in the body.

    The ``**Draft**:`` field wraps ``draft_body`` in a ```` ```markdown ````
    ... ```` ``` ```` fence. The reader closes that fence on the first bare
    backtick line whose length EXACTLY matches the opener (see
    :func:`_scan_fence_state`). A merged draft body synthesized by
    :func:`athenaeum.merge.synthesize_body` copies source-memory bodies
    verbatim, so it may itself contain a bare ```` ``` ```` code fence. If the
    outer fence used the same three backticks, that inner fence would close it
    prematurely — leaking the draft's ``## From `<scope>/<file>` `` subsections
    out as bogus top-level blocks that the reader then rejects as "malformed
    headers" and can never archive (issue #394, the #299/#303 regression).

    Choosing an outer fence one backtick longer than the longest run inside the
    body makes the nested-fence convention documented in the module docstring
    automatic instead of hand-maintained: an inner fence can never match the
    outer fence's length, so it can never close it.
    """
    longest_run = 0
    for match in re.finditer(r"`+", draft_body):
        longest_run = max(longest_run, len(match.group(0)))
    return "`" * max(3, longest_run + 1)


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
    write_kind: str = "create-merged",
) -> str:
    """Render one pending-merge block as markdown.

    Issue #421: ``write_kind`` records the mechanical slug-collision
    classification decided at proposal time — ``create-merged`` (the target
    slug is free) or ``fold-into-existing`` (a wiki page already owns the
    slug). It is CLASSIFICATION only; the fold WRITE path is #425.
    """
    today = created_at or date.today().isoformat()
    target_escaped = _escape_quotes(merge_target_name)
    sources_line = ", ".join(Path(s).name for s in sources) or "(none)"
    parts: list[str] = [
        f'## [{today}] Merge: "{target_escaped}"',
        f"- [ ] Approve this merge? Sources: {sources_line}",
        "",
        f"**Rationale**: {rationale or '(none provided)'}",
        f"**Write kind**: {write_kind}",
        "**Sources**:",
    ]
    for src in sources:
        parts.append(f"- {src}")
    parts.append(f"**Confidence**: {confidence:.2f}")
    parts.append("**Draft**:")
    fence = _outer_draft_fence(draft_merged_body)
    parts.append(f"{fence}markdown")
    parts.append(draft_merged_body.rstrip("\n"))
    parts.append(fence)
    return "\n".join(parts)


def _split_blocks(text: str) -> list[str]:
    """Split ``_pending_merges.md`` text into per-merge blocks.

    A block's ``**Draft**:`` field is a fenced ```` ```markdown ... ``` ````
    section whose CONTENTS may legitimately contain bare ``---`` lines
    (YAML frontmatter), ``## `` subheadings, or a nested fenced snippet
    (see the module docstring). While inside that fence, lines are never
    treated as block/paragraph delimiters — they are always appended as
    content. Fence tracking is shared with :func:`_parse_block` via
    :func:`_scan_fence_state` so the two can't diverge (#292).

    Only a CANONICAL merge header (``## [DATE] Merge: "name"`` — the
    :data:`_HEADER_RE` shape) starts a new top-level block. A bare ``## ``
    line that is not a canonical header — most importantly the
    ``## From `<scope>/<file>` `` subsections that
    :func:`athenaeum.merge.synthesize_body` writes into a draft body — is
    NOT a block boundary: it is appended to the current block when one is
    open, or dropped as inter-block preamble when none is. This is what
    lets a draft whose fence was broken by an inner code fence (issue #394)
    re-absorb its leaked ``## From`` subsections into the parent block
    instead of spraying thousands of "malformed header" warnings, and lets
    orphan ``## From`` fragments left behind by an already-archived merge
    drain out of the sidecar on the next rewrite rather than accreting
    forever.
    """
    blocks: list[str] = []
    current: list[str] = []
    fence_len = 0
    for line in text.splitlines():
        stripped = line.strip()
        new_fence_len = _scan_fence_state(line, fence_len)
        if fence_len:
            if new_fence_len == 0:
                fence_len = 0
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
                fence_len = 0
                if current:
                    blocks.append("\n".join(current).rstrip())
                current = [line]
                continue
            if current:
                current.append(line)
            continue
        if new_fence_len:
            fence_len = new_fence_len
            if current:
                current.append(line)
            continue
        if _HEADER_RE.match(line):
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
    if fence_len:
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
    write_kind = "create-merged"

    in_sources = False
    in_draft = False
    fence_len = 0

    for raw_line in lines[cb_idx + 1 :]:
        s = raw_line.strip()
        if in_draft:
            new_fence_len = _scan_fence_state(raw_line, fence_len)
            if fence_len:
                if new_fence_len == 0:
                    fence_len = 0
                    in_draft = False
                    continue
                draft_lines.append(raw_line)
                continue
            if new_fence_len:
                fence_len = new_fence_len
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
        if s.startswith("**Write kind**:"):
            in_sources = False
            parsed_kind = s.removeprefix("**Write kind**:").strip()
            if parsed_kind:
                write_kind = parsed_kind
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
            fence_len = 0
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
        write_kind=write_kind,
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
    write_kind: str = "create-merged",
) -> str:
    """Append one merge-proposal block to ``_pending_merges.md``.

    Returns the rendered block text (without surrounding separator).
    Creates the file lazily with a ``# Pending Merges`` header. Idempotent:
    if a block with the same id already exists in the file (resolved or
    not), nothing is appended.

    Issue #421: ``write_kind`` carries the proposal-time slug-collision
    classification (``create-merged`` | ``fold-into-existing``).
    """
    block = render_block(
        merge_target_name=merge_target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_merged_body,
        confidence=confidence,
        created_at=created_at,
        write_kind=write_kind,
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
    atomic_write_text(merges_path, combined)
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
            "write_kind": pm.write_kind,
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
    atomic_write_text(merges_path, primary_body)

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

    Also COMPACTS the primary file every run: it is rewritten from the
    blocks :func:`_split_blocks` still recognizes as canonical merge
    blocks, which drops any orphan ``## From`` fragments a broken draft
    fence leaked in an earlier version (issue #394). The recomposed form
    is stable — re-splitting it yields the same blocks — so once the
    backlog has drained the file stops changing and no needless rewrite
    happens. This is what makes the 13 MB regressed sidecar shrink on the
    next run instead of only when a human happens to resolve a merge.
    """
    if not merges_path.exists():
        return 0
    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)

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

    # Recompose the primary file from recognized blocks. Any leaked orphan
    # ``## From`` fragment that _split_blocks no longer treats as a block is
    # dropped here, draining the sidecar (issue #394). Compact even when
    # nothing was archived this run, but only actually write when the bytes
    # would change, so a clean file is left untouched.
    primary_parts = ["# Pending Merges", *remaining]
    new_primary = "\n\n---\n\n".join(primary_parts) + "\n"
    if not archived:
        if new_primary != text:
            atomic_write_text(merges_path, new_primary)
            log.info(
                "pending_merges: compacted sidecar %s (%d -> %d bytes), "
                "no resolved blocks to archive",
                merges_path.name,
                len(text),
                len(new_primary),
            )
        return 0

    atomic_write_text(merges_path, new_primary)

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
    atomic_write_text(archive_path, combined)
    return len(archived)
