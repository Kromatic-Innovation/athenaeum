# SPDX-License-Identifier: Apache-2.0
"""Poll surface for the decision queue: the digest (athenaeum#2529).

Design athenaeum#2489 section 3. :func:`queue_digest` answers "what needs the
operator right now?" in one small, host-agnostic dict, so every surface (CLI,
MCP, a hook) reads the same thing and a host can notify only when
``digest_id`` changes.

What it is and is not:

* **No model, no framing.** The queue is read through
  :func:`athenaeum.decisions.collect_unframed_decisions` (the pre-framing half
  of the unified read) and ranked with deterministic arithmetic. It never
  calls :func:`athenaeum.decision_framing.frame_decision`; the persisted
  presentation store is only consulted to say whether an offered item already
  has stored framing (``framed``).
* **Actionable = ``state: pending`` AND ``answerable``.** Held items are
  counted in ``held_total`` and never offered. Items no applier can take
  (``retraction``, ``quarantine``, ...) are counted in ``pending_total`` /
  ``by_type`` and ``unanswerable_total`` and never offered.
* **Caps limit what is offered, never what may be answered.** Per-type caps
  come from ``decisions.surface_caps`` (see
  :func:`athenaeum.config.resolve_decisions_surface_caps`). Nothing in this
  module touches the answer path.
* **``surfaced_today`` is the type's answered-today count.** There is no
  surfacing ledger: the budget ledger records answers only, and a digest
  that wrote one on every poll would let a second host's poll spend the
  first host's cap and would make the digest impure. So a cap reads as "this
  many of this type per day get through the operator's attention": once the
  day's answers reach the cap, no more of that type are offered until the
  next UTC day. ``remaining = max(0, cap - surfaced_today)``.
* **``digest_id`` hashes the actionable set only** (each offered item's id
  and state digest), never ``generated_at`` or ages, so it changes iff the
  offered set changes.
* **``backlog`` carries non-decision backlog counts.** ``backlog.page_recompile``
  is the number of oversized pages awaiting a recompile plan (the same figure
  ``athenaeum status`` reports as the recompile backlog). Pages already
  awaiting approval are decision items and appear in ``by_type`` instead.
  Withheld (``{}``) from a restricted audience.
* **``by_routing`` is type-level.** Routing is read from the per-type table;
  an item that framing would reclassify as ``scheduled-review`` because its
  context overflows the per-item cap is only reclassified once framed.

Layering: L4 domain module. Imports :mod:`athenaeum.decisions`,
:mod:`athenaeum.decision_framing`, :mod:`athenaeum.decision_budget` and
:mod:`athenaeum.decision_presentations` (all L4) plus :mod:`athenaeum.config`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.clock import now_iso
from athenaeum.config import (
    resolve_decisions_digest_batch_size,
    resolve_decisions_max_sources_per_merge,
    resolve_decisions_surface_caps,
)
from athenaeum.decision_budget import read_decision_budget_events
from athenaeum.decision_framing import (
    ROUTING_CLASSES,
    STATE_HELD,
    STATE_PENDING,
    answerable_as,
    priority_for,
    reversibility_for,
    routing_for,
    state_digest_for,
)
from athenaeum.decision_presentations import load_presentations, stored_presentation
from athenaeum.decisions import age_days, collect_unframed_decisions

log = logging.getLogger(__name__)

#: ``contract_version`` of the digest (design athenaeum#2489 section 3).
DIGEST_CONTRACT_VERSION = 1


def _budget_key(decision_type: str) -> str:
    """Type a budget-ledger event and a queue item share.

    The ledger records the applier's type (a confirmation is answered as a
    ``question``), so both sides are normalised through
    :func:`athenaeum.decision_framing.answerable_as`.
    """
    return answerable_as(decision_type) or decision_type


def _answered_today(wiki_root: Path, today: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in read_decision_budget_events(wiki_root):
        answered_at = event.get("answered_at")
        if isinstance(answered_at, str) and answered_at[:10] == today:
            counts[_budget_key(str(event.get("decision_type") or ""))] += 1
    return counts


def _page_recompile_backlog(wiki_root: Path, config: dict[str, Any] | None) -> int | None:
    try:
        from athenaeum.page_recompile import find_recompile_candidates

        return len(find_recompile_candidates(wiki_root, config).candidates)
    except Exception as exc:  # noqa: BLE001 - a digest must not fail on a backlog count
        log.warning("decision digest: page-recompile backlog unavailable: %s", exc)
        return None


def _headline(actionable: list[dict[str, Any]]) -> str:
    if not actionable:
        return "No decisions need you"
    counts = Counter(str(item["type"]) for item in actionable)
    parts = [
        f"{n} {t}" if n == 1 else f"{n} {t}s"
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    total = len(actionable)
    noun = "decision needs" if total == 1 else "decisions need"
    return f"{total} {noun} you — {', '.join(parts)}"


def queue_digest(
    wiki_root: Path,
    caller_audience: set[str] | None = None,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The decision-queue digest for ``caller_audience`` (``None`` = owner).

    ``config`` is the loaded ``athenaeum.yaml`` mapping (caps, batch size,
    recompile thresholds); ``None`` uses the built-in defaults. ``now`` is
    injectable for tests. See the module docstring for the semantics of each
    field.
    """
    when = now or datetime.now(timezone.utc)
    today = when.astimezone(timezone.utc).date()
    caps = resolve_decisions_surface_caps(config)
    batch_size = resolve_decisions_digest_batch_size(config)

    items = collect_unframed_decisions(
        wiki_root,
        max_sources_per_merge=resolve_decisions_max_sources_per_merge(config),
        caller_audience=caller_audience,
    )
    stored = load_presentations(wiki_root)

    pending: list[dict[str, Any]] = []
    held_total = 0
    for item in items:
        state = item.get("state") or STATE_PENDING
        if state == STATE_HELD:
            held_total += 1
        elif state == STATE_PENDING:
            pending.append(item)

    by_type: Counter[str] = Counter(str(i.get("type") or "") for i in pending)
    by_routing: dict[str, int] = {r: 0 for r in ROUTING_CLASSES}
    for item in pending:
        route = routing_for(str(item.get("type") or ""))
        by_routing[route] = by_routing.get(route, 0) + 1
    unanswerable_total = sum(
        1 for i in pending if answerable_as(str(i.get("type") or "")) is None
    )

    all_ages = (age_days(str(i.get("created_at") or ""), today=today) for i in pending)
    ages = [a for a in all_ages if a is not None]

    answered = _answered_today(wiki_root, today.isoformat())
    per_type_daily = {
        t: {
            "cap": cap,
            "surfaced_today": answered[_budget_key(t)],
            "remaining": max(0, cap - answered[_budget_key(t)]),
        }
        for t, cap in sorted(caps.items())
    }

    ranked: list[tuple[float, str, str, dict[str, Any], dict[str, Any]]] = []
    for item in pending:
        dtype = str(item.get("type") or "")
        if answerable_as(dtype) is None:
            continue
        prio = priority_for(item, routing=routing_for(dtype), today=today)
        created = str(item.get("created_at") or "")
        ranked.append((-prio["score"], created, str(item.get("id")), item, prio))
    ranked.sort(key=lambda r: r[:3])

    remaining = {t: v["remaining"] for t, v in per_type_daily.items()}
    actionable: list[dict[str, Any]] = []
    digest_basis: list[list[str]] = []
    for _neg, _created, _iid, item, prio in ranked:
        if len(actionable) >= batch_size:
            break
        dtype = str(item.get("type") or "")
        if dtype in remaining:
            if remaining[dtype] <= 0:
                continue
            remaining[dtype] -= 1
        digest = state_digest_for(item)
        digest_basis.append([str(item.get("id")), digest])
        actionable.append(
            {
                "id": item.get("id"),
                "type": dtype,
                "summary": item.get("summary"),
                "age_days": age_days(str(item.get("created_at") or ""), today=today),
                "reversibility": reversibility_for(dtype),
                "priority": prio,
                "framed": stored_presentation(stored, item.get("id"), digest) is not None,
            }
        )

    digest_id = "sha256:" + hashlib.sha256(
        json.dumps(sorted(digest_basis), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    backlog: dict[str, int] = {}
    if caller_audience is None:
        count = _page_recompile_backlog(wiki_root, config)
        if count is not None:
            backlog["page_recompile"] = count

    return {
        "contract_version": DIGEST_CONTRACT_VERSION,
        "digest_id": digest_id,
        "generated_at": now_iso(when),
        "pending_total": len(pending),
        "held_total": held_total,
        "unanswerable_total": unanswerable_total,
        "by_type": dict(sorted(by_type.items())),
        "by_routing": by_routing,
        "backlog": backlog,
        "budget": {"per_type_daily": per_type_daily, "answered_today": sum(answered.values())},
        "actionable_now": actionable,
        "oldest_age_days": max(ages) if ages else 0,
        "headline": _headline(actionable),
    }


__all__ = ["DIGEST_CONTRACT_VERSION", "queue_digest"]
