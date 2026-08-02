"""Shared fence-aware splitter for markdown-sidecar files (athenaeum#527).

Both :mod:`athenaeum.pending_merges` (``_pending_merges.md``) and
:mod:`athenaeum.answers` (``_pending_questions.md``) store one record per
markdown block, separated by ``## `` headers and bare ``---`` dividers, where a
record's body may legitimately contain fenced content whose ``---`` / ``## ``
lines must NOT be treated as block boundaries.

Historically each module carried its OWN copy of this splitter, and they
diverged: ``pending_merges`` gained full fence tracking plus unclosed-fence
recovery to fix athenaeum#394, while ``answers`` still split naively on
``startswith("## ")`` and bare ``---``. A ``---`` or ``## `` inside a fenced
answer body therefore split an ``answers`` block and silently dropped the tail —
taking a human's answer with it (audit M11 / athenaeum#527). This module is now the
single implementation of that split so the two callers cannot diverge again; the
athenaeum#394 fence-tracking fix and its unclosed-fence recovery live here once.

Callers parameterize the things that genuinely differ between the sidecars:

- ``block_header_re`` — what starts a new top-level block OUTSIDE a fence.
  Pending merges uses its canonical ``## [DATE] Merge: "..."`` header, so a
  non-canonical ``## `` line (a ``## From `<scope>/<file>`` subsection written
  into a merged Draft body) is absorbed as body content. Pending questions uses
  ``^## `` (any level-2 heading), so a block with a *malformed* header is
  preserved verbatim as its own block for a human to fix, rather than being
  silently absorbed into its neighbor.
- ``fence_open_re`` — what opens a fence. Pending merges wraps its Draft body in
  a ``` ```markdown ``` fence (:data:`MARKDOWN_FENCE_OPEN_RE`); a pending
  question's free-form answer body may contain any backtick code fence
  (:data:`GENERIC_FENCE_OPEN_RE`).
- ``recovery_header_re`` — the header that RECOVERS a block boundary when a
  fence was left unclosed. This is always the *canonical* header pattern (a real
  record header never legitimately appears inside fenced body content), which
  for pending questions is narrower than ``block_header_re``: a ``## `` heading
  a human typed inside an (as-yet-unclosed) fenced answer must stay body
  content, only a real ``Entity:`` header forces recovery. Defaults to
  ``block_header_re`` when the two coincide (pending merges).

The FENCE tracking and its unclosed-fence recovery — the logic that actually
diverged and caused the athenaeum#394/#527 data loss — is shared and identical for both.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("athenaeum.sidecar_blocks")

# A fence closes on a bare backtick-run line, and closes the currently open
# fence only when its length EXACTLY matches the opener's (not CommonMark's "at
# least as many" rule). This lets a body nest an inner fence of a *different*
# backtick-run length inside the enclosing fence without prematurely closing it.
FENCE_CLOSE_RE = re.compile(r"^(?P<fence>`{3,})$")

# Pending-merges Draft convention: a fence opens only on ``` ```markdown ```.
# The Draft wrapper is always ``` ```markdown ```, so a bare ``` inside the body
# never opens (or closes, unless it matches the outer length) a fence.
MARKDOWN_FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,})markdown$")

# Generic backtick code fence: 3+ backticks then an optional info string that
# contains no backtick (``` , ```python , ````toml , ...). Used for a pending
# question's free-form answer body, which may embed any fenced code block.
GENERIC_FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,})(?P<info>[^`]*)$")


def scan_fence_state(line: str, fence_len: int, *, fence_open_re: re.Pattern[str]) -> int:
    """Return the updated open-fence backtick-length after ``line``.

    ``fence_len`` is the backtick count of the currently open fence, or ``0``
    when no fence is open. ``fence_open_re`` selects what opens a fence (see the
    module docstring). A fence closes only on a bare backtick-run line whose
    length EXACTLY matches the opener's, so a body can nest a shorter or longer
    inner fence without closing the outer one.

    This is the single fence state machine used by both sidecar splitters and by
    ``pending_merges._parse_block`` so a Draft/answer body's fence boundaries are
    recognized the same way everywhere (athenaeum#292/#527).
    """
    stripped = line.strip()
    if fence_len:
        close_match = FENCE_CLOSE_RE.match(stripped)
        if close_match and len(close_match.group("fence")) == fence_len:
            return 0
        return fence_len
    open_match = fence_open_re.match(stripped)
    return len(open_match.group("fence")) if open_match else 0


def split_blocks(
    text: str,
    *,
    block_header_re: re.Pattern[str],
    fence_open_re: re.Pattern[str],
    recovery_header_re: re.Pattern[str] | None = None,
    context: str = "sidecar",
) -> list[str]:
    """Split ``text`` into per-record markdown blocks, fence-aware (athenaeum#394/#527).

    Only a line matching ``block_header_re`` starts a new top-level block. A bare
    ``---`` OUTSIDE a fence ends the current block. While a fence is open (opened
    per ``fence_open_re``), ``---`` / ``## `` lines are body content, never block
    delimiters — this is what stops a fenced Draft body or a human's fenced
    answer from being split and silently truncated.

    An unclosed fence is recovered at the next ``recovery_header_re`` line
    (defaults to ``block_header_re``) rather than swallowing every following
    block into the malformed one; a real record header never legitimately
    appears inside fenced body content. ``context`` names the caller in the
    recovery warnings.

    Returns the block texts, each beginning with its ``## `` header; inter-block
    preamble (the file leader, stray text) is discarded.
    """
    recovery_re = recovery_header_re or block_header_re
    blocks: list[str] = []
    current: list[str] = []
    fence_len = 0
    for line in text.splitlines():
        stripped = line.strip()
        new_fence_len = scan_fence_state(line, fence_len, fence_open_re=fence_open_re)
        if fence_len:
            if new_fence_len == 0:
                fence_len = 0
                if current:
                    current.append(line)
                continue
            if recovery_re.match(line):
                # A canonical header appearing while a fence is still "open"
                # means a prior block's fence was left unclosed (malformed
                # input). Recover the boundary here instead of silently
                # swallowing every subsequent block into the malformed one.
                log.warning(
                    "%s: unclosed fence before block header %r; recovering block boundary",
                    context,
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
        if block_header_re.match(line):
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
            "%s: reached end of file with an unclosed fence in the last block; flushing anyway",
            context,
        )
    if current:
        blocks.append("\n".join(current).rstrip())
    return [b for b in blocks if b.startswith("## ")]
