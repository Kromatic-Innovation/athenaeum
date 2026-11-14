# SPDX-License-Identifier: Apache-2.0
"""The drop ledger — records every raw file the librarian's entity loop
consumes WITHOUT creating or updating anything (issue athenaeum#2609).

**The gap this closes.** Before this module existed, a raw file whose
Tier-2 classify response dropped every entity (``[]``, often because
``wiki/_schema/observation-filter.md`` says there is nothing here worth a
page), or whose content never-ingest permanently refuses, took the exact
same code path as a successful compile: logged ``"Deleted"``, counted
toward ``processed_count``, and ticked the entity heartbeat's ``unchanged``
counter. No ledger, queue item, or run-summary entry named WHAT was
dropped or WHY. A user experienced that as silent data loss (see
``docs/measurements/clean-machine-first-recall-linux-2026-11-14.md``'s Fact
A: an invented preference compiled to 0 entities and vanished with no
message).

**Mechanism only.** This module makes every not-kept decision visible and
recoverable, whatever :mod:`athenaeum.rules` / the observation filter /
``athenaeum.never_ingest`` decide should be dropped — it changes no
capture policy (that is issue athenaeum#2598, explicitly out of scope here).

**Stage vocabulary.** :data:`DROP_STAGE_CLASSIFY` / :data:`DROP_STAGE_NEVER_INGEST`
/ :data:`DROP_STAGE_OTHER` are the three stages the entity loop can actually
tell apart by construction. ``DROP_STAGE_CLASSIFY`` covers BOTH "Tier 2's
classify call returned zero entities" and "the observation filter is why it
returned zero" — the two are mechanically indistinguishable from the entity
loop's vantage point: the filter is a prompt fragment injected INTO the
classify call (see :mod:`athenaeum.tiers`'s ``tier2_classify``,
``docs/modules/intake.md``'s "The observation filter" section); there is no
separate return value or flag recording that the filter specifically is why
a response came back empty. Rather than invent an unobservable distinction,
every all-skip classify outcome (the code-artifact filter, the
address-named-entity filter, a Tier 1 old-format skip reaching the unlink
branch, or a bare empty classify response) is tagged ``DROP_STAGE_CLASSIFY``
unless it carries its own ``result.skipped`` entry, in which case it is
tagged :data:`DROP_STAGE_OTHER` and that entry's text becomes the reason.
A never-ingest refusal is tagged :data:`DROP_STAGE_NEVER_INGEST`
unconditionally — it is the one drop cause the entity loop CAN name
precisely (``ProcessingResult.retain_raw_by_design``).

**Excerpt safety.** :func:`safe_excerpt` reuses the SAME contact-address
detectors the PII/excluded-field routing already applies elsewhere
(:func:`athenaeum.pii.find_inline_emails` / ``find_inline_phones`` /
``mask_contact_address`` — the identical trio :func:`athenaeum.pii.
mask_contact_address`'s own docstring cites for "any context outside the
excluded surface") before truncating to :data:`DROP_EXCERPT_MAX_CHARS`. A
drop row is not the excluded surface, so a contact address must never land
in it verbatim — this is the SAME mask an escalation uses to name an
address a human can recognize without the full value ever reaching
``_pending_questions.md`` (issue athenaeum#2209). The excerpt is taken from
``RawFile.content`` AFTER ``athenaeum.sensitivity_routing.
route_sensitive_values`` has already run (every ``process_one`` caller runs
it first, unconditionally, before any tier reads the body) — so a routed/
redacted sensitive value is also never captured here.

Layering: L3 (service). Imports :mod:`athenaeum.store` (L1, ``now_iso`` /
``append_line_durable``) and :mod:`athenaeum.pii` (L3, the excerpt-masking
detectors) — both at or below this module's own tier. Imported by
:mod:`athenaeum.librarian` (L4) and :mod:`athenaeum._cmd_drops` (L5), both
strictly above.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.pii import (
    find_inline_emails,
    find_inline_phones,
    is_service_address,
    mask_contact_address,
)
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

__all__ = [
    "DROP_EXCERPT_MAX_CHARS",
    "DROP_LEDGER_FILENAME",
    "DROP_QUARANTINE_DIRNAME",
    "DROP_STAGE_CLASSIFY",
    "DROP_STAGE_NEVER_INGEST",
    "DROP_STAGE_OTHER",
    "DROP_STAGES",
    "DropRecord",
    "count_for_run",
    "drop_ledger_path",
    "find_drop",
    "iter_drops",
    "latest_run_id",
    "quarantine_dir",
    "quarantine_raw_file",
    "record_drop",
    "restore_drop",
    "safe_excerpt",
]

#: The drop ledger lives in the cache dir (design note §5.2 "operational"
#: ledgers that are machine-local-adjacent but still durable fact records —
#: see this module's artifact-registry row in ``store.ARTIFACT_REGISTRY``,
#: ``name="drop-ledger"``). One JSONL row per drop, append-only.
DROP_LEDGER_FILENAME = "_drops.jsonl"

#: Issue athenaeum#2609: the ``librarian.drop_action: quarantine`` subdirectory,
#: also under the cache dir — holds the dropped raw file's ORIGINAL bytes
#: (not the ledger's capped excerpt) so ``athenaeum drops restore`` can
#: recover it verbatim instead of from a truncated, PII-masked copy.
DROP_QUARANTINE_DIRNAME = "drop-quarantine"

#: Capped length (characters) of the content excerpt a drop row stores.
#: Short on purpose: the ledger is a durable, append-only, git-committed-
#: adjacent record, not a second copy of raw intake -- it exists to tell an
#: operator WHAT was dropped and WHY, not to double as a backup (that is
#: what ``librarian.drop_action: quarantine`` is for).
DROP_EXCERPT_MAX_CHARS = 500

#: Tier 2's classify call returned nothing worth a page (empty response, or
#: every candidate filtered before a Tier-3 action existed) -- see this
#: module's docstring for why the observation filter is folded into this
#: same stage rather than distinguished from it.
DROP_STAGE_CLASSIFY = "classify"

#: ``ProcessingResult.retain_raw_by_design`` -- a never-ingest manifest match
#: (:mod:`athenaeum.never_ingest`). The raw file is RETAINED on disk (never
#: deleted, by that module's own contract) and re-evaluated every run, but
#: this run's content still produced no page -- worth its own drop row.
DROP_STAGE_NEVER_INGEST = "never-ingest"

#: Every other all-skip outcome that carries its own ``result.skipped``
#: entry (today: a Tier-1 old-entity-format match with no create/update).
DROP_STAGE_OTHER = "other"

#: Every stage :func:`record_drop` will accept.
DROP_STAGES = (DROP_STAGE_CLASSIFY, DROP_STAGE_NEVER_INGEST, DROP_STAGE_OTHER)


@dataclass(frozen=True)
class DropRecord:
    """One drop-ledger row (issue athenaeum#2609)."""

    ts: str
    run_id: str
    raw_ref: str
    stage: str
    reason: str | None
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "raw_ref": self.raw_ref,
            "stage": self.stage,
            "reason": self.reason,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DropRecord | None":
        raw_ref = data.get("raw_ref")
        if not isinstance(raw_ref, str) or not raw_ref:
            return None
        return cls(
            ts=str(data.get("ts", "")),
            run_id=str(data.get("run_id", "")),
            raw_ref=raw_ref,
            stage=str(data.get("stage", DROP_STAGE_OTHER)),
            reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
            excerpt=str(data.get("excerpt", "")),
        )


def drop_ledger_path(cache_dir: Path) -> Path:
    """The drop ledger's path under *cache_dir*."""
    return Path(cache_dir) / DROP_LEDGER_FILENAME


def quarantine_dir(cache_dir: Path) -> Path:
    """The ``librarian.drop_action: quarantine`` directory under *cache_dir*."""
    return Path(cache_dir) / DROP_QUARANTINE_DIRNAME


def safe_excerpt(content: str, max_chars: int = DROP_EXCERPT_MAX_CHARS) -> str:
    """Build a drop-ledger-safe excerpt of *content*.

    Reuses the existing contact-address detectors
    (:func:`athenaeum.pii.find_inline_emails` / ``find_inline_phones``) and
    masks every match with :func:`athenaeum.pii.mask_contact_address` BEFORE
    truncating — so a long address that straddles the truncation point is
    still masked (the detectors run over the full text, not the already-cut
    excerpt), and a service identifier (``git@github.com``, a calendar
    group address) is left alone via :func:`athenaeum.pii.is_service_address`,
    matching every other consumer of this detector pair. Whitespace-
    stripped before (NOT after) the length cap, so the cap is an honest
    character count of visible content.
    """
    text = (content or "").strip()
    for email in find_inline_emails(text):
        if is_service_address(email):
            continue
        text = text.replace(email, mask_contact_address(email))
    for phone in find_inline_phones(text):
        text = text.replace(phone, mask_contact_address(phone))
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def record_drop(
    cache_dir: Path,
    *,
    raw_ref: str,
    content: str,
    stage: str,
    reason: str | None,
    run_id: str,
    now: datetime | None = None,
) -> DropRecord:
    """Append one drop row and return it (issue athenaeum#2609).

    *content* is reduced to a capped, PII-masked excerpt via
    :func:`safe_excerpt` before it ever reaches the ledger line -- the
    ledger never holds the raw file's full, unmasked body. Durable append
    (:func:`athenaeum.store.append_line_durable`): ``O_APPEND`` + ``fsync``,
    the same primitive every other ledger in this codebase uses.

    Callers are responsible for checking :func:`athenaeum.config.
    resolve_drop_log` first -- this function always writes when called (it
    has no opinion about the off switch, matching every other ledger
    writer in this codebase: the resolve_*_enabled check lives at the call
    site, not duplicated into every writer).
    """
    if stage not in DROP_STAGES:
        raise ValueError(f"stage={stage!r} not in {DROP_STAGES}")
    record = DropRecord(
        ts=now_iso(now),
        run_id=run_id,
        raw_ref=raw_ref,
        stage=stage,
        reason=reason,
        excerpt=safe_excerpt(content),
    )
    line = json.dumps(record.to_dict(), sort_keys=True)
    append_line_durable(drop_ledger_path(cache_dir), (line + "\n").encode("utf-8"))
    return record


def iter_drops(cache_dir: Path) -> list[DropRecord]:
    """Read every well-formed drop row, oldest first.

    Tolerates a torn trailing line (a crash mid-write) or a hand-edit —
    skipped, not fatal, mirroring every other JSONL ledger reader in this
    codebase (e.g. :func:`athenaeum.pii._read_jsonl`). Returns ``[]`` when
    the ledger does not exist.
    """
    path = drop_ledger_path(cache_dir)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[DropRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rec = DropRecord.from_dict(data)
            if rec is not None:
                records.append(rec)
    return records


def find_drop(cache_dir: Path, raw_ref: str) -> DropRecord | None:
    """The MOST RECENT drop row for *raw_ref*, or ``None`` if it was never dropped."""
    match: DropRecord | None = None
    for rec in iter_drops(cache_dir):
        if rec.raw_ref == raw_ref:
            match = rec
    return match


def latest_run_id(cache_dir: Path) -> str | None:
    """The ``run_id`` of the most recently written drop row, or ``None``."""
    records = iter_drops(cache_dir)
    return records[-1].run_id if records else None


def count_for_run(cache_dir: Path, run_id: str) -> int:
    """How many drop rows *run_id* wrote."""
    return sum(1 for rec in iter_drops(cache_dir) if rec.run_id == run_id)


def quarantine_raw_file(cache_dir: Path, raw: Any) -> Path:
    """Move a dropped raw file's bytes into the quarantine dir, preserving
    its ``source/filename`` shape so :func:`restore_drop` can find it by
    ``raw.ref`` later (issue athenaeum#2609, ``librarian.drop_action:
    quarantine``). Returns the new path. The source file no longer exists
    at ``raw.path`` after this call — this is a MOVE, not a copy, matching
    ``librarian.drop_action: delete``'s own "the raw no longer lives under
    raw/" contract; only the destination differs.
    """
    target = quarantine_dir(cache_dir) / raw.source / raw.path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    raw.path.replace(target)
    return target


def restore_drop(cache_dir: Path, raw_root: Path, raw_ref: str) -> Path:
    """Re-queue a dropped file's content as fresh raw intake (issue
    athenaeum#2609, ``athenaeum drops restore``).

    Recovery source, in preference order:

    1. The quarantine directory (``librarian.drop_action: quarantine``) --
       the file's ORIGINAL, unmasked, untruncated bytes, if still there.
    2. The drop ledger's own stored excerpt -- capped and PII-masked (see
       :func:`safe_excerpt`), the only copy available when
       ``librarian.drop_action: delete`` (the default) was in effect, or the
       quarantined copy has since been cleared. This is honestly a PARTIAL
       recovery when the original content exceeded
       :data:`DROP_EXCERPT_MAX_CHARS` or contained a masked contact address
       — re-queuing it is still strictly better than the pre-athenaeum#2609
       "the content is simply gone" outcome, and is what the ledger's own
       AC text asks to be re-queued ("a dropped file's content").

    Raises :class:`LookupError` when *raw_ref* was never dropped (nothing to
    restore). Writes a NEW raw-intake file under
    ``raw_root/<raw_ref's source>/`` with a fresh
    ``<timestamp>-<uuid8>.md`` name (the same shape
    :func:`athenaeum.intake.discover_raw_files` expects) — never reuses the
    original filename, so this is unambiguously a re-QUEUE, not an in-place
    undelete. Returns the new file's path.
    """
    record = find_drop(cache_dir, raw_ref)
    if record is None:
        raise LookupError(f"{raw_ref!r} has no recorded drop in {drop_ledger_path(cache_dir)}")

    source = raw_ref.split("/", 1)[0]
    quarantined = quarantine_dir(cache_dir) / raw_ref
    if quarantined.is_file():
        content = quarantined.read_text(encoding="utf-8")
    else:
        content = record.excerpt

    dest_dir = Path(raw_root) / source
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.md"
    dest.write_text(content, encoding="utf-8")

    if quarantined.is_file():
        quarantined.unlink()

    log.info("drops restore: %s -> %s", raw_ref, dest)
    return dest
