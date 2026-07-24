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
from athenaeum.provenance import record_merge_provenance

log = logging.getLogger(__name__)

# Same overall grammar as :data:`athenaeum.resolutions._WIKILINK_RE`
# (Obsidian-style ``[[slug]]`` / ``[[slug|alias]]``) but with the optional
# ``|alias`` suffix captured as its own group (group 2, including the
# leading ``|``) so a rewrite can repoint the target (group 1) while
# preserving the rendered alias text verbatim. Kept as a SEPARATE pattern
# object rather than adding a capturing group to the shared one — that
# regex is a public module attribute other code may already rely on
# matching group(1) as "the whole match's only group".
_WIKILINK_REWRITE_RE = re.compile(r"\[\[([^\[\]|\n]+?)(\|[^\[\]\n]*)?\]\]")


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


def _preview_draft_body(draft_merged_body: str, preview_chars: int) -> tuple[str, bool]:
    """Bound ``draft_merged_body`` to ``preview_chars``.

    Returns ``(text, truncated)``. When the body already fits, ``text`` is
    returned byte-identical (no truncation marker appended) so a normal-sized
    merge's payload is unchanged from before this cap existed (issue #431).
    ``preview_chars <= 0`` disables truncation (the resolver already coerces
    non-positive config values back to the default, so this is a defensive
    fallback, not a normal path).
    """
    if preview_chars <= 0 or len(draft_merged_body) <= preview_chars:
        return draft_merged_body, False
    return draft_merged_body[:preview_chars], True


def list_pending_merges(
    merges_path: Path,
    *,
    config: dict | None = None,
    full_body: bool = False,
) -> list[dict]:
    """Return unresolved merges as MCP-friendly dicts.

    Issue #431 (read-path defense-in-depth, complementing the #400 write-path
    ``max_merge_sources`` suppression): a single oversized pending merge — the
    withdrawn runaway that prompted this issue had a ~878 KB draft body — blew
    out the payload of every ``list_pending_merges`` call because
    ``draft_merged_body`` was returned in full, unbounded. By default this
    truncates ``draft_merged_body`` to
    :func:`athenaeum.config.resolve_merge_body_preview_chars` (env > yaml
    ``librarian.merge_body_preview_chars`` > 2000) characters and adds
    ``draft_merged_body_truncated: True`` plus the untruncated
    ``draft_merged_body_full_length`` so a caller can tell a preview from the
    real thing and decide whether to re-fetch in full.

    Args:
        merges_path: Path to ``wiki/_pending_merges.md``.
        config: Resolved athenaeum config dict (as from
            :func:`athenaeum.config.load_config`), or ``None`` to use the
            resolver's env/default fallback with no yaml override.
        full_body: When ``True``, skip truncation entirely and return the
            complete ``draft_merged_body`` for every item — the on-demand
            escape hatch for a caller that specifically needs the full draft
            (e.g. immediately before approving a merge).

    A body already at or under the cap is returned byte-identical to the
    pre-#431 behavior (no truncation marker fields added beyond the two
    always-present booleans/lengths), so normal-sized merges are unaffected.
    """
    from athenaeum.config import resolve_merge_body_preview_chars

    preview_chars = resolve_merge_body_preview_chars(config)
    out = []
    for pm in parse_pending_merges(merges_path):
        if pm.resolved:
            continue
        full = pm.draft_merged_body
        if full_body:
            body, truncated = full, False
        else:
            body, truncated = _preview_draft_body(full, preview_chars)
        out.append(
            {
                "id": pm.id,
                "merge_target_name": pm.merge_target_name,
                "sources": list(pm.sources),
                "rationale": pm.rationale,
                "draft_merged_body": body,
                "draft_merged_body_truncated": truncated,
                "draft_merged_body_full_length": len(full),
                "confidence": pm.confidence,
                "created_at": pm.created_at,
                "write_kind": pm.write_kind,
            }
        )
    return out


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


def _source_slugs(sources: list[str]) -> list[str]:
    """Derive the wiki slug each source path would use, deduped, order-preserved.

    Sources on a ``fold-into-existing`` proposal are wiki-tree pages being
    folded away (see :func:`athenaeum.merge._classify_merge_write_kind` —
    the pre-existing-target check that produced this write_kind implies the
    cluster's members are themselves wiki entries, not raw intake). The
    slug is derived from the filename stem exactly like
    :func:`athenaeum.resolutions._build_sibling_index`'s fallback, so it
    matches how the same file would be looked up as a wikilink target.
    """
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        stem = Path(src).stem
        slug = slugify(stem)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _rewrite_inbound_wikilinks(
    wiki_root: Path,
    old_slugs: list[str],
    canonical_slug: str,
    *,
    skip: Path | None = None,
) -> int:
    """Rewrite every ``[[old-slug]]`` / ``[[old-slug|text]]`` link to canonical.

    Walks every ``*.md`` directly under ``wiki_root`` (sidecars like
    ``_pending_merges.md`` are skipped — filenames starting with ``_`` are
    never link targets) and repoints any wikilink whose slugified target
    matches one of ``old_slugs`` at ``canonical_slug``. The rendered
    ``|alias-text`` portion (if present) is preserved verbatim — only the
    link TARGET changes, not the displayed text. ``skip`` excludes the
    canonical page itself (it may legitimately reference its own former
    slug in body prose describing the merge).

    Returns the number of files modified. Best-effort: unreadable files are
    skipped, not fatal.
    """
    if not old_slugs:
        return 0
    old_slug_set = set(old_slugs)
    n = 0
    try:
        skip_resolved = skip.resolve() if skip is not None else None
    except OSError:
        skip_resolved = skip
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        if skip is not None:
            try:
                if path.resolve() == skip_resolved:
                    continue
            except OSError:
                if path == skip:
                    continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        def _replace(m: "re.Match[str]") -> str:
            target = m.group(1).strip()
            if slugify(target) not in old_slug_set:
                return m.group(0)
            alias_suffix = m.group(2) or ""
            return f"[[{canonical_slug}{alias_suffix}]]"

        new_text = _WIKILINK_REWRITE_RE.sub(_replace, text)
        if new_text != text:
            atomic_write_text(path, new_text)
            n += 1
    return n


def _add_aliases_to_frontmatter(meta: dict, new_aliases: list[str]) -> dict:
    """Return ``meta`` with ``new_aliases`` unioned into ``aliases:``, deduped.

    Existing ``aliases:`` entries are preserved in order; new ones are
    appended, skipping any already present (by slug equivalence, so
    ``"Old Topic"`` and ``"old-topic"`` are not both recorded). Non-list
    (or absent) existing ``aliases:`` is treated as empty rather than
    raising — a malformed sidecar field should not block the fold.
    """
    existing_raw = meta.get("aliases")
    existing = [str(a) for a in existing_raw] if isinstance(existing_raw, list) else []
    existing_slugs = {slugify(a) for a in existing}
    merged = list(existing)
    for alias in new_aliases:
        if slugify(alias) not in existing_slugs:
            existing_slugs.add(slugify(alias))
            merged.append(alias)
    out = dict(meta)
    if merged:
        out["aliases"] = merged
    return out


def resolve_alias_slug(wiki_root: Path, slug: str) -> str:
    """Resolve ``slug`` to its canonical slug via wiki ``aliases:`` frontmatter.

    Link-time resolution for issue #425: a ``[[old-slug]]`` wikilink in a
    not-yet-processed ``raw/`` memory (or any body prose) should resolve to
    the canonical page once ``old-slug`` has been folded away and recorded
    in the canonical page's ``aliases:`` list. Scans every ``*.md`` directly
    under ``wiki_root`` (sidecars excluded) for an ``aliases:`` entry whose
    slugified form matches ``slug``; returns that page's own slug (its
    filename stem) on a hit, else returns ``slug`` unchanged (not an alias,
    or already canonical). First match wins on a (should-not-happen)
    multi-hit; unreadable/malformed files are skipped.
    """
    target = slugify(slug)
    if not target:
        return slug
    try:
        candidates = sorted(wiki_root.glob("*.md"))
    except OSError:
        return slug
    for path in candidates:
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not isinstance(meta, dict):
            continue
        aliases_raw = meta.get("aliases")
        if not isinstance(aliases_raw, list):
            continue
        for alias in aliases_raw:
            if slugify(str(alias)) == target:
                return path.stem
    return slug


def _purge_vector_ids(
    slugs: list[str],
    *,
    cache_dir: Path | None,
    search_backend: str | None,
    embedding_model: str | None,
) -> int:
    """Best-effort vector-store purge for deleted wiki slugs (issue #425).

    A no-op (returns 0) when ``cache_dir`` is not supplied, the configured
    backend is not ``"vector"``, or chromadb is unavailable — vector purge
    is opportunistic hygiene, never a hard dependency of ``resolve_merge``.
    Filenames are the vector store's id space (see
    :meth:`athenaeum.search.VectorBackend._add_records`), so a slug's id is
    ``"<slug>.md"``.
    """
    if not slugs or cache_dir is None:
        return 0
    if search_backend is not None and search_backend != "vector":
        return 0
    try:
        from athenaeum.search import VectorBackend
    except ImportError:
        return 0
    ids = [f"{slug}.md" for slug in slugs]
    try:
        return VectorBackend(embedding_model=embedding_model).purge_ids(ids, cache_dir)
    except Exception:  # noqa: BLE001 — purge must never break the merge
        log.debug("pending_merges: vector purge skipped for ids=%s", ids)
        return 0


def _apply_fold_into_existing(
    pm: PendingMerge,
    *,
    target_path: Path,
    target_slug: str,
    wiki_root: Path,
    cache_dir: Path | None,
    search_backend: str | None,
    embedding_model: str | None,
) -> dict:
    """Execute the ``fold-into-existing`` write path (issue #425).

    The target IS the canonical existing page. Steps, in order:

    1. Write ``draft_merged_body`` to ``target_path`` (the merged content —
       same convention as the ``create-merged`` path's body write).
    2. Derive the folded-away source slugs (the OTHER sources — a source
       whose own slug already equals the target is the canonical page
       itself reappearing in its own cluster and is not folded away).
    3. Union those slugs into the canonical page's ``aliases:``
       frontmatter, deduped.
    4. Rewrite every inbound ``[[old-slug]]`` wikilink under ``wiki_root``
       (excluding the canonical page itself) to ``target_slug``.
    5. Delete the old source wiki files.
    6. Best-effort purge their vectors from the search index.

    Returns ``{"ok": True, "folded_sources", "aliases_added",
    "links_rewritten"}`` on success. ``target_exists`` is unreachable from
    here by construction — the caller only takes this path for
    ``write_kind == "fold-into-existing"``, and #421's proposal-time
    classification only assigns that write_kind when the slug already
    exists; this function does not re-check.
    """
    # Read the PRE-EXISTING target's frontmatter first — its ``aliases:``
    # (accumulated by any prior fold) must survive the draft-body overwrite
    # in step 1 below, so this MUST happen before that write.
    try:
        prior_target_text = target_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        prior_target_text = ""
    prior_meta, _ = parse_frontmatter(prior_target_text)
    if not isinstance(prior_meta, dict):
        prior_meta = {}
    existing_alias_slugs = {
        slugify(str(a)) for a in (prior_meta.get("aliases") or [])
    }

    # Step 1 — write the merged draft body to the canonical target.
    target_path.write_text(pm.draft_merged_body, encoding="utf-8")

    # Step 2 — folded-away source slugs, excluding the canonical page
    # reappearing among its own sources.
    all_source_slugs = _source_slugs(pm.sources)
    folded_slugs = [s for s in all_source_slugs if s != target_slug]
    folded_sources = [
        src for src in pm.sources if slugify(Path(src).stem) != target_slug
    ]

    # Step 3 — alias map, deduped. The draft body just written in step 1 may
    # carry its OWN frontmatter (a merge draft can legitimately open with
    # one) — that becomes the base we add aliases: onto, with the prior
    # target's aliases: carried forward first so a second fold accumulates
    # rather than resetting.
    target_text = target_path.read_text(encoding="utf-8")
    target_meta, target_body = parse_frontmatter(target_text)
    if not isinstance(target_meta, dict):
        target_meta = {}
    carried_meta = _add_aliases_to_frontmatter(
        target_meta, list(prior_meta.get("aliases") or [])
    )
    new_meta = _add_aliases_to_frontmatter(carried_meta, folded_slugs)
    if new_meta != target_meta:
        new_target_text = render_frontmatter(new_meta) + target_body
        atomic_write_text(target_path, new_target_text)
    aliases_added = [s for s in folded_slugs if s not in existing_alias_slugs]

    # Step 4 — rewrite inbound wikilinks pointing at any folded slug.
    links_rewritten = _rewrite_inbound_wikilinks(
        wiki_root, folded_slugs, target_slug, skip=target_path
    )

    # Step 5 — delete the old source wiki files (reference rewrite, not
    # stub pages — a content-bearing stub would get re-embedded and create
    # a near-duplicate retrieval hit).
    deleted_paths: list[str] = []
    for src in folded_sources:
        src_path = Path(src)
        try:
            if src_path.is_file():
                src_path.unlink()
                deleted_paths.append(src)
        except OSError as exc:
            log.warning(
                "pending_merges: could not delete folded source %s: %s", src_path, exc
            )

    # Step 6 — best-effort vector purge for the deleted slugs.
    _purge_vector_ids(
        [s for s in folded_slugs if any(slugify(Path(p).stem) == s for p in deleted_paths)],
        cache_dir=cache_dir,
        search_backend=search_backend,
        embedding_model=embedding_model,
    )

    return {
        "ok": True,
        "folded_sources": deleted_paths,
        "aliases_added": aliases_added,
        "links_rewritten": links_rewritten,
    }


def resolve_merge(
    merges_path: Path,
    merge_id: str,
    decision: Literal["approve", "reject"],
    note: str = "",
    *,
    wiki_root: Path | None = None,
    cache_dir: Path | None = None,
    search_backend: str | None = None,
    embedding_model: str | None = None,
) -> dict:
    """Mark a pending-merge block as resolved.

    Args:
        merges_path: Path to ``_pending_merges.md``.
        merge_id: Id returned by :func:`list_pending_merges`.
        decision: ``"approve"`` dispatches on the proposal's ``write_kind``
            (issue #421 classification, issue #425 write paths):

            - ``"create-merged"`` (unchanged behavior): writes
              ``wiki/<target-slug>.md`` (or under ``wiki_root`` when
              supplied) with ``draft_merged_body``. Fails closed with
              ``target_exists`` if the slug is already taken — including a
              MISCLASSIFIED create-kind proposal, as defense in depth.
            - ``"fold-into-existing"``: the target slug is the CANONICAL
              existing page; sources fold INTO it. Writes
              ``draft_merged_body`` to the existing target, rewrites every
              inbound ``[[old-slug]]`` wikilink under ``wiki_root`` to the
              canonical slug, adds the folded-away source slugs to the
              canonical page's ``aliases:`` frontmatter (deduped), deletes
              the old source wiki files, and (when ``cache_dir`` +
              ``search_backend="vector"`` are supplied) purges their
              vectors from the vector store. ``target_exists`` is
              unreachable here for a correctly-classified proposal — the
              precheck at proposal time (`_classify_merge_write_kind`)
              already confirmed the slug exists.

            Either way, on success a provenance record is appended (see
            :func:`athenaeum.provenance.record_merge_provenance`) naming
            the canonical slug, source paths, merge id, and write_kind.
            The source memories are NOT archived/deleted for
            ``create-merged`` — the human reviews the wiki write before
            any source deletion; ``fold-into-existing`` DOES delete the
            (wiki-tree) source files as part of consolidation, since the
            merge target already existed and review has already happened
            at approval time.

            ``"reject"`` flips the checkbox and writes a ``refines:``
            declaration into the first source memory so the detector's
            declared-refinement short-circuit suppresses the pair on
            future runs.
        note: Optional human note attached to the decision block.
        wiki_root: Optional wiki root override (defaults to
            ``merges_path.parent``).
        cache_dir: Optional search-index cache dir. When supplied together
            with ``search_backend="vector"``, a ``fold-into-existing``
            approve purges the deleted sources' vectors from the store
            (issue #425 embedding hygiene). ``None`` (default) skips the
            purge — vector hygiene is opportunistic, never a hard
            dependency of resolving a merge.
        search_backend: The configured search backend name (``"vector"``
            enables the purge above; anything else, including ``None``,
            skips it).
        embedding_model: Embedding model name passed through to the vector
            backend purge call, matching the model the live index was
            built with (see :class:`athenaeum.search.VectorBackend`).

    Returns:
        ``{"ok": bool, "error_code": str | None, "message": str,
           "resolved_block": str | None}``. A ``fold-into-existing``
        approve additionally sets ``"folded_sources"`` (the deleted source
        paths), ``"aliases_added"`` (the new alias slugs recorded), and
        ``"links_rewritten"`` (the count of sibling wiki files whose
        inbound wikilinks were repointed).
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
    extra_response: dict = {}

    # Apply the side-effect tied to the decision BEFORE flushing the file.
    if decision == "approve":
        root = wiki_root or merges_path.parent
        root.mkdir(parents=True, exist_ok=True)
        target_slug = slugify(target_pm.merge_target_name)
        target_path = root / f"{target_slug}.md"
        write_kind = target_pm.write_kind

        if write_kind == "fold-into-existing":
            fold_result = _apply_fold_into_existing(
                target_pm,
                target_path=target_path,
                target_slug=target_slug,
                wiki_root=root,
                cache_dir=cache_dir,
                search_backend=search_backend,
                embedding_model=embedding_model,
            )
            if not fold_result["ok"]:
                return {
                    "ok": False,
                    "error_code": fold_result["error_code"],
                    "message": fold_result["message"],
                    "resolved_block": None,
                }
            extra_response = {
                "folded_sources": fold_result["folded_sources"],
                "aliases_added": fold_result["aliases_added"],
                "links_rewritten": fold_result["links_rewritten"],
            }
        else:
            # ``create-merged`` path — UNCHANGED behavior. A misclassified
            # create-kind proposal that hits an existing slug still fails
            # closed here (defense in depth): the #421 precheck should have
            # classified it fold-into-existing, but a stale/hand-edited
            # block's write_kind is not trusted blindly.
            if target_path.exists():
                # Fail closed: do NOT flip the checkbox; the human must
                # rename the merge_target_name or resolve the existing
                # wiki entry.
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

        record_merge_provenance(
            root,
            merge_id=target_pm.id,
            write_kind=write_kind,
            canonical_slug=target_slug,
            source_paths=list(target_pm.sources),
        )
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
    response.update(extra_response)
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
