# SPDX-License-Identifier: Apache-2.0
"""Scrub contact data out of ``wiki/_pending_merges.md`` (issue athenaeum#1276).

A merge proposal stores its ``draft_merged_body`` **verbatim** — a copy of the
source pages' text at proposal time. So migrating a page's PII off-corpus
(``athenaeum storage migrate-pii``, issue athenaeum#479/#427) left the raw
addresses sitting in the sidecar: the entity page read clean, an ``excluded/``
record existed, the index was refreshed, and a plain-text copy of the same
contact data stayed under ``wiki/`` where ``lint-pii`` (issue athenaeum#495)
still finds it. The migration looked complete and was not.

This module closes that hole from both ends:

* :func:`scrub_pending_merges` with ``values=`` — the migration-coupled pass.
  ``migrate-pii --apply`` hands it exactly the tokens it just moved off the
  page, and they are redacted from every proposal that embedded them.
* :func:`scrub_pending_merges` with ``values=None`` — the standalone,
  **zero-LLM** sweep behind ``athenaeum merges scrub-pii``. It detects
  contact data with the same detectors ``lint-pii`` gates on
  (:func:`~athenaeum.pii.find_inline_emails` /
  :func:`~athenaeum.pii.find_inline_phones`), so clearing a backlog here
  actually moves that gate. This is the purge path issue athenaeum#1276 asked
  for: a proposal's stale body can be cleaned **without** approving,
  rejecting or withdrawing the merge, and without paying for a nightly
  ``athenaeum run`` (whose entity phase is the documented ~94% of runtime and
  spend).

Design choices worth keeping:

**Redact, never delete.** A scrubbed value is replaced by
:data:`~athenaeum.storage_migrate.INLINE_REDACTION_MARKER` — the same marker
the page itself now carries — so an approved proposal writes a merged page
consistent with its already-migrated sources, the surrounding prose survives,
and :mod:`athenaeum.pii_restore` recognises the marker. Dropping the sentence
would destroy true non-PII content; that is the mistake athenaeum#691 paid for
twice.

**Structural lines are never rewritten.** The block header
(``## [DATE] Merge: "name"``), its checkbox line, and the ``**Sources**:``
paths carry the proposal's IDENTITY — :func:`athenaeum.pending_merges._make_id`
hashes the sources and target name, and the fold path derives its target slug
from the same header. Rewriting one would silently re-id the proposal or point
the fold at a path that does not exist. A value found only there is reported
as a :class:`ProposalPiiResidual` instead — visible, never silently dropped,
never silently rewritten. (Measured 2026-09-02 against the live 741-block
sidecar: zero email-shaped tokens outside draft/prose lines, so this is a
guard, not a routine partial outcome.)

**The allowlist is honoured.** A value with a reasoned entry in
``_pii-allowlist.yml`` (issue athenaeum#936) is adjudicated *not* PII, does not
fail ``lint-pii``, and is left alone here — redacting it would destroy a true
non-personal fact and strand the allowlist entry as stale. Only the
detector-driven sweep consults the allowlist; an explicit ``values=`` list
from ``migrate-pii`` is an operator instruction about values that are already
off-corpus, so it is obeyed as given.

**Dry-run by default**, mirroring :func:`~athenaeum.pending_merges.revalidate_pending_merges`
and ``authority convert``: the result reports what WOULD change and nothing is
written unless ``apply=True``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.pending_merges import _parse_block, _split_blocks
from athenaeum.pii import (
    PII_ALLOWLIST_FILENAME,
    find_inline_emails,
    find_inline_phones,
    is_service_address,
    load_pii_allowlist,
)
from athenaeum.storage_migrate import INLINE_REDACTION_MARKER

log = logging.getLogger(__name__)

__all__ = [
    "PII_ALLOWLIST_FILENAME",
    "PendingMergeScrubResult",
    "ProposalPiiResidual",
    "ScrubbedProposal",
    "scrub_pending_merges",
]


@dataclass(frozen=True)
class ScrubbedProposal:
    """One proposal block whose text carried (and lost) contact data."""

    id: str
    merge_target_name: str
    #: The distinct raw values redacted from this block, in first-seen order.
    values: tuple[str, ...]


@dataclass(frozen=True)
class ProposalPiiResidual:
    """A value left in place because it sits on an identity-bearing line.

    See the module docstring: the header / checkbox / ``**Sources**:`` paths
    define the proposal's id and its fold target, so they are reported rather
    than rewritten. An operator clears one by renaming the underlying page
    (``migrate-pii --rename-name-email``, issue athenaeum#505) and re-proposing
    — never by editing the id out from under the queue.
    """

    id: str
    merge_target_name: str
    values: tuple[str, ...]


@dataclass
class PendingMergeScrubResult:
    """What a scrub pass did (or, on a dry run, would do)."""

    scrubbed: list[ScrubbedProposal] = field(default_factory=list)
    residual: list[ProposalPiiResidual] = field(default_factory=list)
    blocks_scanned: int = 0
    applied: bool = False

    @property
    def values_redacted(self) -> int:
        """Total distinct values redacted across all blocks."""
        return sum(len(s.values) for s in self.scrubbed)

    @property
    def is_clean(self) -> bool:
        """True when the sidecar needed no change at all."""
        return not self.scrubbed and not self.residual


def _is_structural(line: str, *, in_sources: bool, first: bool) -> bool:
    """True when *line* carries the proposal's identity rather than content.

    Only ever consulted for lines BEFORE ``**Draft**:`` — inside the draft
    fence a ``- `` bullet or a ``## `` heading is page content, not block
    structure (:func:`athenaeum.pending_merges._split_blocks` documents the
    same asymmetry).
    """
    stripped = line.strip()
    if first and stripped.startswith("## "):
        return True
    if stripped.startswith("- [") and "] Approve this merge?" in stripped:
        return True
    if stripped.startswith("**Sources**:"):
        return True
    return in_sources and stripped.startswith("- ")


def _partition_block_lines(block_text: str) -> tuple[list[str], list[bool]]:
    """Split *block_text* into lines plus a per-line "is structural" flag."""
    lines = block_text.split("\n")
    flags: list[bool] = []
    in_sources = False
    in_draft = False
    for idx, line in enumerate(lines):
        if in_draft:
            flags.append(False)
            continue
        stripped = line.strip()
        structural = _is_structural(line, in_sources=in_sources, first=idx == 0)
        if stripped.startswith("**Sources**:"):
            in_sources = True
        elif in_sources and stripped and not stripped.startswith("- "):
            in_sources = False
        if stripped.startswith("**Draft**:"):
            in_draft = True
        flags.append(structural)
    return lines, flags


def _redact(text: str, values: Iterable[str]) -> str:
    """Replace each raw *values* token in *text* with the redaction marker.

    Longest-first so a token that is a substring of another cannot partially
    rewrite it, and idempotent because the marker contains no email- or
    phone-shaped token of its own — the same contract (and the same marker) as
    :func:`athenaeum.storage_migrate._redact_inline_tokens`.
    """
    out = text
    for value in sorted(values, key=len, reverse=True):
        out = out.replace(value, INLINE_REDACTION_MARKER)
    return out


def _detect_values(text: str, allowed: frozenset[str]) -> list[str]:
    """Contact values in *text* that are genuine, unadjudicated PII.

    Uses the detectors ``lint-pii`` gates on (:func:`scan_corpus_pii`'s pair),
    minus service identifiers (``git@github.com``, calendar group ids — issue
    athenaeum#507) and minus anything the allowlist has adjudicated.
    """
    found: list[str] = []
    for email in find_inline_emails(text):
        if is_service_address(email):
            continue
        if email.casefold() in allowed:
            continue
        if email not in found:
            found.append(email)
    for phone in find_inline_phones(text):
        if phone.casefold() in allowed:
            continue
        if phone not in found:
            found.append(phone)
    return found


def _scrub_one_block(
    block_text: str, *, values: list[str] | None, allowed: frozenset[str]
) -> tuple[str, list[str], list[str]]:
    """Scrub one block. Returns ``(new_text, redacted, residual)``."""
    lines, flags = _partition_block_lines(block_text)
    content = "\n".join(line for line, structural in zip(lines, flags) if not structural)
    structural_text = "\n".join(line for line, structural in zip(lines, flags) if structural)

    if values is None:
        targets = _detect_values(content, allowed)
        residual = _detect_values(structural_text, allowed)
    else:
        targets = [v for v in values if v and v in content]
        residual = [v for v in values if v and v in structural_text]

    if not targets:
        return block_text, [], residual

    new_lines = [
        line if structural else _redact(line, targets)
        for line, structural in zip(lines, flags)
    ]
    return "\n".join(new_lines), targets, residual


def _resolve_allowed(
    merges_path: Path, allowlist_path: Path | None, *, consult: bool
) -> frozenset[str]:
    if not consult:
        return frozenset()
    path = allowlist_path or (merges_path.parent / PII_ALLOWLIST_FILENAME)
    entries, errors = load_pii_allowlist(path)
    for err in errors:
        log.warning("pending_merges_pii: allowlist entry ignored -- %s", err)
    return frozenset(entry.value.casefold() for entry in entries)


def scrub_pending_merges(
    merges_path: Path,
    *,
    values: Iterable[str] | None = None,
    allowlist_path: Path | None = None,
    apply: bool = False,
    config: dict[str, Any] | None = None,
) -> PendingMergeScrubResult:
    """Redact contact data out of ``_pending_merges.md`` proposal bodies.

    Two modes, one transform (issue athenaeum#1276):

    * ``values`` given — redact exactly those tokens. This is the pass
      ``storage migrate-pii --apply`` runs with the emails/phones it just
      moved to the excluded surface, so a migration can no longer leave a
      verbatim copy behind in the sidecar.
    * ``values=None`` — detect contact data with ``lint-pii``'s own
      detectors and redact what is neither a service identifier nor
      allowlist-adjudicated. Zero LLM calls, no network, no merge decision
      forced: a stale proposal body is cleaned in place and the proposal
      stays exactly as unresolved as it was.

    RESOLVED blocks are scrubbed too. A resolved proposal's body is still a
    verbatim copy sitting under ``wiki/``, so leaving it would defeat the
    migration just as thoroughly as an unresolved one — this is a redaction,
    not a queue-hygiene sweep, so it has no reason to care whether the merge
    decision has been taken. (This is the deliberate difference from
    :func:`~athenaeum.pending_merges.revalidate_pending_merges`, which
    archives whole proposals and therefore must leave resolved ones alone.)

    Dry-run by default; pass ``apply=True`` to write. The write is atomic and
    rebuilds the sidecar the same way ``revalidate`` does, so blocks with
    nothing to redact round-trip unchanged. Returns a
    :class:`PendingMergeScrubResult`; a missing file is not an error (an
    empty, clean result).

    *config* is accepted for symmetry with the other sidecar entry points and
    for future recogniser wiring; the detectors used here are deliberately the
    ones ``lint-pii`` gates on, so a scrub that reports clean means that gate
    can actually reach exit 0.
    """
    result = PendingMergeScrubResult()
    if not merges_path.exists():
        return result

    value_list = [v for v in values] if values is not None else None
    allowed = _resolve_allowed(merges_path, allowlist_path, consult=value_list is None)

    text = merges_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    result.blocks_scanned = len(blocks)

    rewritten: list[str] = []
    changed = False
    for block_text in blocks:
        new_text, redacted, residual = _scrub_one_block(
            block_text, values=value_list, allowed=allowed
        )
        rewritten.append(new_text)
        if not (redacted or residual):
            continue
        pm = _parse_block(block_text)
        block_id = pm.id if pm is not None else ""
        target = pm.merge_target_name if pm is not None else block_text.split("\n", 1)[0][:80]
        if redacted:
            changed = True
            result.scrubbed.append(
                ScrubbedProposal(
                    id=block_id, merge_target_name=target, values=tuple(redacted)
                )
            )
        if residual:
            result.residual.append(
                ProposalPiiResidual(
                    id=block_id, merge_target_name=target, values=tuple(residual)
                )
            )

    if not apply or not changed:
        return result

    new_text = "\n\n---\n\n".join(["# Pending Merges", *rewritten]) + "\n"
    atomic_write_text(merges_path, new_text)
    result.applied = True
    log.info(
        "pending_merges_pii: redacted %d contact value(s) across %d proposal(s) in %s",
        result.values_redacted,
        len(result.scrubbed),
        merges_path.name,
    )
    return result
