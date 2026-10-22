# SPDX-License-Identifier: Apache-2.0
"""Split pass over oversized wiki pages (issue athenaeum#2078 AC3).

Operator ruling (2026-10-15): merges into an oversized page are allowed
(:func:`athenaeum.tiers.check_page_size_gate` now flags the page
``needs_split: true`` instead of refusing); *then* the page is split into a
hub plus linked atomic pages. This module is that second step.

It deliberately reuses :func:`athenaeum.tiers._perform_oversize_page_split` --
the heading-based, atomic (all-or-nothing) split the reactive
``librarian.oversize_page_action: split`` disposition already runs -- rather
than a second splitter, and makes no LLM call: no page body is rewritten, the
existing sections are moved verbatim. (``decompose-page`` was considered and
not reused: it redistributes the facts of one *aggregate* page by a
caller-supplied subject regex, which is a different job from structuring an
ordinary page that merely grew past the size invariant.)

Approval model:

- ``--uid U`` (repeatable): the operator approved splitting that page.
- ``--auto``: additionally split every candidate whose body is over the
  configurable hard ceiling (``librarian.oversize_hard_ceiling_chars``)
  without per-page approval.
- anything else flagged but not approved is only reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import (
    resolve_oversize_hard_ceiling_chars,
    resolve_page_size_threshold_chars,
)
from athenaeum.models import EntityAction, parse_frontmatter

log = logging.getLogger(__name__)

#: Frontmatter key :func:`athenaeum.tiers.check_page_size_gate` sets on a page
#: it let a merge into while over the page-size threshold.
NEEDS_SPLIT_KEY = "needs_split"


@dataclass
class SplitCandidate:
    """One page awaiting a split."""

    path: Path
    uid: str
    chars: int
    flagged: bool  # carries ``needs_split: true``
    over_ceiling: bool  # body above the hard ceiling


@dataclass
class SplitOutcome:
    """What the pass did (or would do) for one candidate/named page."""

    path: Path
    uid: str
    chars: int
    status: str  # split | would_split | no_heading | not_approved | missing | error


def find_split_candidates(
    wiki_root: Path, config: dict[str, Any] | None = None
) -> list[SplitCandidate]:
    """Pages flagged ``needs_split`` or over the hard ceiling, largest first."""
    ceiling = resolve_oversize_hard_ceiling_chars(config)
    out: list[SplitCandidate] = []
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            meta, body = parse_frontmatter(fpath.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        flagged = meta.get(NEEDS_SPLIT_KEY) is True
        over = len(body) > ceiling
        if flagged or over:
            out.append(
                SplitCandidate(
                    path=fpath,
                    uid=str(meta.get("uid") or ""),
                    chars=len(body),
                    flagged=flagged,
                    over_ceiling=over,
                )
            )
    out.sort(key=lambda c: c.chars, reverse=True)
    return out


def _find_by_uid(wiki_root: Path, uid: str) -> Path | None:
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            meta, _ = parse_frontmatter(fpath.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if str(meta.get("uid") or "") == uid:
            return fpath
    return None


def split_pages(
    wiki_root: Path,
    *,
    uids: set[str],
    auto: bool,
    config: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> list[SplitOutcome]:
    """Run the split pass. See the module docstring for the approval model.

    Never raises for a single page: a page that cannot be split (no heading to
    split on, I/O failure) is reported and left untouched, because the
    underlying split is all-or-nothing.
    """
    from athenaeum.tiers import _perform_oversize_page_split

    threshold = resolve_page_size_threshold_chars(config)
    candidates = {c.uid: c for c in find_split_candidates(wiki_root, config) if c.uid}
    outcomes: list[SplitOutcome] = []

    targets: list[tuple[str, Path | None]] = []
    seen: set[str] = set()
    for uid in sorted(uids):
        seen.add(uid)
        cand = candidates.get(uid)
        targets.append((uid, cand.path if cand else _find_by_uid(wiki_root, uid)))
    for uid, cand in candidates.items():
        if uid in seen:
            continue
        if auto and cand.over_ceiling:
            targets.append((uid, cand.path))
        else:
            outcomes.append(SplitOutcome(cand.path, uid, cand.chars, "not_approved"))

    for uid, path in targets:
        if path is None or not path.exists():
            outcomes.append(SplitOutcome(Path(uid), uid, 0, "missing"))
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            outcomes.append(SplitOutcome(path, uid, 0, "error"))
            continue
        chars = len(body)
        if chars <= threshold and meta.get(NEEDS_SPLIT_KEY) is not True:
            # Operator named a page that is neither over the invariant nor
            # flagged: nothing to split, and not ours to restructure.
            outcomes.append(SplitOutcome(path, uid, chars, "not_oversize"))
            continue
        if dry_run:
            outcomes.append(SplitOutcome(path, uid, chars, "would_split"))
            continue
        action = EntityAction(
            kind="update",
            name=str(meta.get("name") or path.stem),
            entity_type=str(meta.get("type") or ""),
            tags=[],
            access=str(meta.get("access") or ""),
            existing_uid=uid,
            observations="",
        )
        try:
            ok = _perform_oversize_page_split(
                action=action,
                existing_body=body,
                existing_meta=meta,
                existing_path=path,
                wiki_root=wiki_root,
            )
        except OSError as exc:
            log.warning("split-oversize: %s failed and was rolled back: %s", path.name, exc)
            outcomes.append(SplitOutcome(path, uid, chars, "error"))
            continue
        outcomes.append(SplitOutcome(path, uid, chars, "split" if ok else "no_heading"))
    return outcomes
