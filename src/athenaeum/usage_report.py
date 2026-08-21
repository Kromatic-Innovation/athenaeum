# SPDX-License-Identifier: Apache-2.0
"""Per-claim usage report: pushed / referenced / last-referenced (issue athenaeum#968).

Part 1 of athenaeum#968 (memory-model v6's reshaped athenaeum#430): the "usage sensor for tier
movement" memory-model §6.5 names. Extends the shipped push-metrics
instrumentation (:mod:`athenaeum.push_metrics`, issues athenaeum#711/#734/#795) — which
already durably records every push and every session-end reference
determination — into a per-CLAIM aggregate: how many times a given pushed id
has been pushed, how many times it was actually referenced afterward, and
when it was last referenced. This module computes and reports that signal.
It does NOT decide what to do with it — no policy, no tier-movement rule,
nothing that writes to a wiki page. That consumer is issue athenaeum#718; see "The
interface athenaeum#718 consumes" below.

**Redaction discipline** (issue athenaeum#968 AC1): every field on
:class:`ClaimUsage` is an id, a count, or a timestamp — never claim content,
never a name-derived slug. This holds for free: push-metrics ids are already
opaque (:func:`athenaeum.push_metrics.opaque_push_id` — a wiki entity's
frontmatter ``uid``, or a raw-intake filename, which is timestamp+hash, never
name-derived), so this module reads those same ids straight through without
adding any redaction logic of its own.

**The interface athenaeum#718 consumes** (issue athenaeum#968 AC3): athenaeum#718's tier-movement
rules MUST call :func:`get_claim_usage` (single-claim) or
:func:`compute_usage_report` (bulk) — never re-read
``_push_records.jsonl``/``_push_references.jsonl`` directly. This is the one
seam through which usage data crosses from the push-metrics ledgers to any
tier-movement consumer; a future ledger-format change only has to update
this module, not every downstream reader. See ``docs/configuration.md``
"Usage report (athenaeum#968)" for the operator-facing writeup and the CLI
(``athenaeum usage-report``) this module backs.

**No deletion, no mutation** (issue athenaeum#968 AC4): every function in this
module is read-only over the push-metrics ledgers — it computes an
in-memory aggregate and returns it. Nothing here writes to a wiki page,
moves a tier, or removes anything from disk.

Layering: L3 service, alongside :mod:`athenaeum.push_metrics` itself (which
this module imports for its two ledger readers only — never the private
``_read_jsonl``, never the ledger paths directly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaimUsage:
    """One claim's usage aggregate — pushed-count, referenced-count, last-referenced.

    ``last_pushed``/``last_referenced`` are ISO-8601 UTC strings (the same
    ``ts`` format the push-metrics ledgers already stamp), or ``None`` when
    that side has zero records in the window queried.
    """

    id: str
    pushed_count: int
    referenced_count: int
    last_pushed: str | None
    last_referenced: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pushed_count": self.pushed_count,
            "referenced_count": self.referenced_count,
            "last_pushed": self.last_pushed,
            "last_referenced": self.last_referenced,
        }


def _parse_ts(raw: Any) -> datetime | None:
    """Mirrors :func:`athenaeum.push_metrics._parse_ts`'s contract exactly
    (a small, stable helper duplicated rather than imported private — same
    convention :mod:`athenaeum.never_ingest` follows for ``_append_line``).
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_usage_report(
    *,
    cache_dir: Path | None = None,
    since: datetime | None = None,
    wiki_root: Path | None = None,
) -> dict[str, ClaimUsage]:
    """Compute the per-claim usage report from the push-metrics ledgers.

    ``since=None`` (default) means the whole ledger — mirrors
    :func:`athenaeum.push_metrics.compute_baseline`'s own "whole ledger by
    default" contract. Returns a ``{id: ClaimUsage}`` map covering every id
    that appears in EITHER ledger within the window (an id pushed but never
    referenced yet has ``referenced_count=0``, ``last_referenced=None`` — the
    honest state, never fabricated).

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`read_push_records`.
    The reference-determination ledger keeps resolving under *cache_dir*
    unchanged — see :func:`athenaeum.push_metrics.compute_baseline`'s
    docstring for why.
    """
    from athenaeum.push_metrics import read_push_records, read_reference_records

    pushes = read_push_records(cache_dir, wiki_root=wiki_root)
    refs = read_reference_records(cache_dir)

    def _in_window(ts_raw: Any) -> bool:
        if since is None:
            return True
        ts = _parse_ts(ts_raw)
        return ts is not None and ts >= since

    pushed_count: dict[str, int] = {}
    last_pushed: dict[str, str] = {}
    for rec in pushes:
        ts = rec.get("ts")
        if not _in_window(ts):
            continue
        for item in rec.get("items", []):
            pid = item.get("id")
            if not isinstance(pid, str) or not pid:
                continue
            pushed_count[pid] = pushed_count.get(pid, 0) + 1
            if isinstance(ts, str) and (pid not in last_pushed or ts > last_pushed[pid]):
                last_pushed[pid] = ts

    referenced_count: dict[str, int] = {}
    last_referenced: dict[str, str] = {}
    for rec in refs:
        ts = rec.get("ts")
        if not _in_window(ts):
            continue
        referenced_ids = rec.get("referenced_ids") or []
        if not isinstance(referenced_ids, list):
            continue
        for pid in referenced_ids:
            if not isinstance(pid, str) or not pid:
                continue
            referenced_count[pid] = referenced_count.get(pid, 0) + 1
            if isinstance(ts, str) and (
                pid not in last_referenced or ts > last_referenced[pid]
            ):
                last_referenced[pid] = ts

    all_ids = set(pushed_count) | set(referenced_count)
    return {
        pid: ClaimUsage(
            id=pid,
            pushed_count=pushed_count.get(pid, 0),
            referenced_count=referenced_count.get(pid, 0),
            last_pushed=last_pushed.get(pid),
            last_referenced=last_referenced.get(pid),
        )
        for pid in all_ids
    }


def get_claim_usage(
    claim_id: str,
    *,
    cache_dir: Path | None = None,
    since: datetime | None = None,
    wiki_root: Path | None = None,
) -> ClaimUsage | None:
    """Single-claim usage lookup — THE documented interface issue athenaeum#718's
    tier-movement rules must call (see the module docstring). Returns
    ``None`` when *claim_id* has zero push or reference records in the
    window queried (an honest "never seen", never a fabricated zero-usage
    record for an id that was never pushed at all).

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`compute_usage_report`.
    """
    return compute_usage_report(cache_dir=cache_dir, since=since, wiki_root=wiki_root).get(
        claim_id
    )


def usage_report_to_list(report: dict[str, ClaimUsage]) -> list[dict[str, Any]]:
    """Render a usage report as a sorted (by id), JSON-serializable list —
    the CLI's ``--json`` output shape and this module's own deterministic
    test fixture shape.
    """
    return [report[pid].to_dict() for pid in sorted(report)]


__all__ = [
    "ClaimUsage",
    "compute_usage_report",
    "get_claim_usage",
    "usage_report_to_list",
]
