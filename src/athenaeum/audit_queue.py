# SPDX-License-Identifier: Apache-2.0
"""Stale-page review queue + bounded nightly re-audit (issue athenaeum#1630).

Two halves, both built on the audit machinery athenaeum#1624 shipped
(:mod:`athenaeum.audit`) rather than a second read/write path of their own:

* **Report/queue** (:func:`compute_stale_pages`, :func:`render_stale_table`):
  a read-only scan of ``wiki_root`` listing every page whose ``last_audited``
  is missing or older than a configured age
  (:func:`athenaeum.config.resolve_audit_stale_after_days`), or whose
  ``audit_version`` is behind :data:`athenaeum.audit.AUDIT_VERSION` even when
  ``last_audited`` is recent. Sorted never-audited first, then oldest
  ``last_audited``, ties broken by usage
  (:func:`athenaeum.usage_report.compute_usage_report` — THE documented
  usage-aggregate interface, never a second read of the push-metrics
  ledgers; see that module's own "interface athenaeum#718 consumes" note,
  which this module obeys identically). The head of this list IS the
  re-audit queue (issue athenaeum#1630's Plan item 2) — there is no separate
  queue data structure, just this same ordered list truncated to
  ``audit.nightly_max_pages``.
* **Nightly drain** (:func:`run_nightly_drain`): re-audits up to
  ``audit.nightly_max_pages`` pages off the head of that queue through
  :func:`athenaeum.audit.audit_pages_via_batch` — the SAME Batch-API
  transport :func:`athenaeum.audit.build_audit_report` uses for
  ``athenaeum audit --batch`` — and writes results via
  :func:`athenaeum.audit.apply_audit_report`, the SAME write path every
  other audit caller uses. Deliberately calls ``audit_pages_via_batch``
  directly rather than going through ``build_audit_report``:
  ``build_audit_report`` ALSO unconditionally writes a
  ``spend.record_spend(run_type=RUN_TYPE_AUDIT, ...)`` ledger row for
  whatever ``TokenUsage`` it is given — correct for its standalone CLI
  caller, but this module is called from INSIDE an already-in-flight
  ``athenaeum.librarian`` run (``librarian._run_audit_nightly_drain_phase``),
  which books tokens into the run's own shared ``ctx.usage`` and writes
  exactly ONE spend-ledger row for the WHOLE run at finalize
  (``spend.record_spend_per_knob_provider``). Routing through
  ``build_audit_report`` would double-record this phase's tokens under two
  ledger rows. Calling ``audit_pages_via_batch`` directly and wrapping its
  verdicts in a plain :class:`athenaeum.audit.AuditReport` before handing
  it to :func:`~athenaeum.audit.apply_audit_report` gets the same batch
  routing and write path with none of that side effect — the caller alone
  decides spend accounting, exactly like every other librarian phase
  (``athenaeum.rule_proposals.run_rule_proposal_detection``, e.g.) already
  does with its own ``usage=ctx.usage`` parameter.

  Off by default, opt-in by config: :func:`athenaeum.config.
  resolve_audit_nightly_max_pages` returns ``None`` when
  ``audit.nightly_max_pages`` is unset, and :func:`run_nightly_drain`
  returns ``None`` immediately in that case — no wiki scan, no client
  touched, no page read. Stops selecting further pages once the running
  ESTIMATED cost of the pages already selected would cross a configured
  share (:func:`athenaeum.config.resolve_audit_nightly_spend_share`) of the
  daily spend ceiling (:func:`athenaeum.config.resolve_spend_max_usd_per_day`)
  net of what has already been spent today
  (:func:`athenaeum.spend.spend_today`) — the remainder of the page window
  is reported as skipped for budget, never submitted. The estimate
  (:func:`estimate_page_audit_cost_usd`) prices the SAME prompt text
  :func:`athenaeum.audit.render_audit_prompt` builds for the real call, at
  the Batch API's discounted rate (mirrors
  :mod:`athenaeum.batch`'s own submit-time reservation estimate,
  ``batch._estimate_batch_tokens`` — this module does not import that
  private helper, it independently estimates against the SAME prompt-render
  function so the two estimates price the identical text).

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.audit` (L4) for the
report/write/batch machinery, :mod:`athenaeum.usage_report` (L3) for the
usage tie-break, :mod:`athenaeum.models` (L1) for ``TokenUsage`` and
``parse_frontmatter``, and :mod:`athenaeum.config` (L2, function-local) for
its own three resolvers. Never imports :mod:`athenaeum.librarian` (L4) —
the librarian phase (``librarian._run_audit_nightly_drain_phase``) imports
THIS module, never the other way, keeping the import graph acyclic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.audit import (
    AUDIT_SYSTEM,
    AUDIT_VERSION,
    COORDINATE_FIELDS,
    AuditReport,
    apply_audit_report,
    audit_pages_via_batch,
    discover_wiki_pages,
    render_audit_prompt,
)
from athenaeum.models import TokenUsage, parse_frontmatter

log = logging.getLogger(__name__)

#: Characters per token for the pre-submission cost estimate — the same
#: approximation :mod:`athenaeum.batch`'s own submit-time reservation
#: estimate uses (``batch._CHARS_PER_TOKEN``), duplicated rather than
#: imported (that name is private to its module).
_CHARS_PER_TOKEN = 4.0

#: Output-token estimate per page, for the SAME pre-submission budget
#: check. Audit responses are small structured JSON (at most three
#: coordinate fields plus a retirement verdict — see
#: :data:`athenaeum.audit.AUDIT_SYSTEM`), so this is deliberately far below
#: :mod:`athenaeum.drain_advisor`'s whole-corpus
#: ``DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE`` default (1,500) — that figure is
#: for the general entity-tier write/merge responses, not this narrow
#: schema, and using it here would over-reserve budget for every page.
_ESTIMATED_OUTPUT_TOKENS_PER_PAGE = 300.0


def _read(path: Path) -> str | None:
    """Duplicated from :mod:`athenaeum.audit`'s own private ``_read`` —
    same convention :mod:`athenaeum.usage_report` documents for its own
    small duplicated helpers rather than importing a private name across
    modules.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        log.warning("audit-queue: unreadable page %s: %s", path, exc)
        return None


def _parse_ts(raw: Any) -> datetime | None:
    """Mirrors :func:`athenaeum.usage_report._parse_ts`'s contract exactly."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class StalePageEntry:
    """One page on the stale-page review queue.

    ``reason`` is ``"never-audited"`` (no ``last_audited`` at all),
    ``"stale-version"`` (``audit_version`` does not match the current
    :data:`athenaeum.audit.AUDIT_VERSION`, however recent
    ``last_audited`` is), or ``"stale-age"`` (``last_audited`` older than
    the configured threshold). ``referenced_count``/``last_referenced``
    come straight from :func:`athenaeum.usage_report.compute_usage_report`
    — ``0``/``None`` for a uid with zero usage records, the same honest
    "never seen" contract that module documents for itself.
    """

    uid: str
    path: Path
    last_audited: str | None
    audit_version: str | None
    reason: str
    referenced_count: int
    last_referenced: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "path": str(self.path),
            "last_audited": self.last_audited,
            "audit_version": self.audit_version,
            "reason": self.reason,
            "referenced_count": self.referenced_count,
            "last_referenced": self.last_referenced,
        }


def compute_stale_pages(
    wiki_root: Path,
    *,
    stale_after_days: int,
    audit_version: str = AUDIT_VERSION,
    now: datetime | None = None,
    cache_dir: Path | None = None,
) -> list[StalePageEntry]:
    """Scan *wiki_root* and return every stale page, in queue order.

    Order (issue athenaeum#1630 AC1/AC2): never-audited pages first, then
    pages sorted by ``last_audited`` ascending (oldest first — a
    ``stale-version`` page with a recent ``last_audited`` sorts among the
    audited pages by that timestamp, since it genuinely was audited
    recently; only its STALENESS, not its queue position, is driven by the
    version check), ties broken by ``referenced_count`` descending (more
    used sorts first — the AC's own tie-break), then ``uid`` for full
    determinism.

    Read-only: never writes to a page, never mutates the usage ledgers it
    reads via :func:`athenaeum.usage_report.compute_usage_report`.
    """
    from athenaeum.usage_report import compute_usage_report

    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=stale_after_days)

    usage = compute_usage_report(cache_dir=cache_dir, wiki_root=wiki_root)

    entries: list[StalePageEntry] = []
    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        meta, _body = parse_frontmatter(text)
        uid = meta.get("uid") if meta else None
        if not isinstance(uid, str) or not uid.strip():
            continue

        raw_last_audited = meta.get("last_audited")
        last_audited = (
            raw_last_audited
            if isinstance(raw_last_audited, str) and raw_last_audited.strip()
            else None
        )
        raw_audit_version = meta.get("audit_version")
        page_audit_version = (
            raw_audit_version
            if isinstance(raw_audit_version, str) and raw_audit_version.strip()
            else None
        )

        if last_audited is None:
            reason = "never-audited"
        elif page_audit_version != audit_version:
            reason = "stale-version"
        else:
            parsed = _parse_ts(last_audited)
            if parsed is None or parsed < cutoff:
                reason = "stale-age"
            else:
                continue  # fresh enough, current version -- not stale

        page_usage = usage.get(uid)
        entries.append(
            StalePageEntry(
                uid=uid,
                path=path,
                last_audited=last_audited,
                audit_version=page_audit_version,
                reason=reason,
                referenced_count=page_usage.referenced_count if page_usage else 0,
                last_referenced=page_usage.last_referenced if page_usage else None,
            )
        )

    def _sort_key(entry: StalePageEntry) -> tuple[int, str, int, str]:
        tier = 0 if entry.reason == "never-audited" else 1
        return (
            tier,
            entry.last_audited or "",
            -entry.referenced_count,
            entry.uid,
        )

    entries.sort(key=_sort_key)
    return entries


def render_stale_table(entries: list[StalePageEntry]) -> str:
    """Render *entries* as a plain-text table (issue athenaeum#1630 AC "table")."""
    if not entries:
        return "0 stale page(s)"
    lines = [f"{len(entries)} stale page(s):"]
    for e in entries:
        lines.append(
            f"  {e.uid}: reason={e.reason} last_audited={e.last_audited or 'never'} "
            f"audit_version={e.audit_version or '(none)'} referenced_count="
            f"{e.referenced_count} last_referenced={e.last_referenced or 'never'}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Nightly drain (issue athenaeum#1630 Plan item 3)
# --------------------------------------------------------------------------- #


@dataclass
class NightlyDrainSummary:
    """One nightly-drain run's counters (issue athenaeum#1630 Plan item 4)."""

    stale_queue_size: int = 0
    candidates_considered: int = 0
    reaudited: int = 0
    skipped_budget: int = 0
    failed: int = 0
    stale_remaining: int = 0
    cost_usd: float = 0.0
    reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_queue_size": self.stale_queue_size,
            "candidates_considered": self.candidates_considered,
            "reaudited": self.reaudited,
            "skipped_budget": self.skipped_budget,
            "failed": self.failed,
            "stale_remaining": self.stale_remaining,
            "cost_usd": self.cost_usd,
            "reason": self.reason,
        }


def estimate_page_audit_cost_usd(
    meta: dict[str, Any], body: str, *, model: str | None, max_tokens: int
) -> float:
    """Pre-submission cost estimate for one page's audit request.

    Prices the SAME prompt text :func:`athenaeum.audit.render_audit_prompt`
    builds for the real call, at the Batch API's discounted rate
    (:meth:`athenaeum.models.TokenUsage.add_batch_tokens`) — see the module
    docstring for why this never calls the model.

    Uses ALL of :data:`athenaeum.audit.COORDINATE_FIELDS` as the "fields to
    determine" list rather than re-deriving this page's actual empty
    fields — a deliberate, safe OVERESTIMATE (a page with fewer empty
    fields costs at most this much, never more) that avoids reaching into
    :mod:`athenaeum.audit`'s private per-page emptiness helper from another
    module.
    """
    prompt = render_audit_prompt(meta, body, list(COORDINATE_FIELDS))
    input_tokens = int((len(AUDIT_SYSTEM) + len(prompt)) / _CHARS_PER_TOKEN)
    output_tokens = int(min(_ESTIMATED_OUTPUT_TOKENS_PER_PAGE, max_tokens))
    usage = TokenUsage()
    usage.add_batch_tokens(input_tokens, output_tokens, model=model)
    return usage.estimated_cost_usd


def _select_within_budget(
    candidates: list[StalePageEntry],
    *,
    budget_usd: float | None,
    pages: dict[str, tuple[dict[str, Any], str]],
    model: str,
    max_tokens: int,
) -> tuple[list[StalePageEntry], list[StalePageEntry]]:
    """Split *candidates* into ``(selected, skipped_for_budget)``.

    ``budget_usd is None`` means no daily ceiling is configured at all —
    every candidate is selected, nothing is skipped for budget (mirrors
    every other ceiling's "unset means unlimited" contract in
    :mod:`athenaeum.config`). Otherwise walks *candidates* in queue order,
    accumulating each one's :func:`estimate_page_audit_cost_usd` against a
    running remaining-budget counter — the FIRST candidate whose estimate
    would exceed what remains stops the walk; every candidate from there
    on (not just that one) is reported skipped for budget, since none of
    them were ever submitted (issue athenaeum#1630 AC "records the rest as
    skipped for budget").
    """
    if budget_usd is None:
        return list(candidates), []

    selected: list[StalePageEntry] = []
    remaining = budget_usd
    for idx, entry in enumerate(candidates):
        page = pages.get(entry.uid)
        if page is None:
            continue
        meta, body = page
        cost = estimate_page_audit_cost_usd(meta, body, model=model, max_tokens=max_tokens)
        if cost > remaining:
            return selected, candidates[idx:]
        selected.append(entry)
        remaining -= cost
    return selected, []


def run_nightly_drain(
    wiki_root: Path,
    *,
    client: Any,
    model: str,
    config: dict[str, Any] | None,
    now: datetime | None = None,
    run_usage: TokenUsage | None = None,
    max_tokens: int = 1024,
    cache_dir: Path | None = None,
) -> NightlyDrainSummary | None:
    """Run the bounded nightly re-audit drain. ``None`` when config-gated off.

    Returns ``None`` immediately — no wiki scan, no client touched — when
    ``audit.nightly_max_pages`` is unset
    (:func:`athenaeum.config.resolve_audit_nightly_max_pages`); this is the
    caller's (``athenaeum.librarian._run_audit_nightly_drain_phase``)
    signal that the phase is disabled, distinct from "ran and found zero
    stale pages" (:attr:`NightlyDrainSummary.reason` ``"empty-queue"``) or
    "ran with no LLM client configured" (``"no-client"``).

    See the module docstring for why this calls
    :func:`athenaeum.audit.audit_pages_via_batch` directly (never
    :func:`athenaeum.audit.build_audit_report`) and why *run_usage*, when
    supplied, is never handed to a second spend-ledger write here — the
    caller owns spend accounting.
    """
    from athenaeum.config import (
        resolve_audit_nightly_max_pages,
        resolve_audit_nightly_spend_share,
        resolve_audit_stale_after_days,
        resolve_spend_max_usd_per_day,
    )

    max_pages = resolve_audit_nightly_max_pages(config)
    if max_pages is None:
        return None

    stale_after_days = resolve_audit_stale_after_days(config)
    queue = compute_stale_pages(
        wiki_root, stale_after_days=stale_after_days, now=now, cache_dir=cache_dir
    )
    if not queue:
        return NightlyDrainSummary(reason="empty-queue")

    candidates = queue[:max_pages]

    pages: dict[str, tuple[dict[str, Any], str]] = {}
    for entry in candidates:
        text = _read(entry.path)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        pages[entry.uid] = (meta, body)

    if client is None:
        return NightlyDrainSummary(
            stale_queue_size=len(queue),
            candidates_considered=len(candidates),
            stale_remaining=len(queue),
            reason="no-client",
        )

    daily_cap = resolve_spend_max_usd_per_day(config)
    budget_usd: float | None = None
    if daily_cap is not None:
        from athenaeum import spend

        share = resolve_audit_nightly_spend_share(config)
        ledger_path = spend.resolve_ledger_path(config, cache_dir=cache_dir, wiki_root=wiki_root)
        spent_today = spend.spend_today(ledger_path, config=config, now=now)["api_usd"]
        budget_usd = max(0.0, daily_cap * share - spent_today)

    selected, skipped = _select_within_budget(
        candidates,
        budget_usd=budget_usd,
        pages=pages,
        model=model,
        max_tokens=max_tokens,
    )

    if not selected:
        return NightlyDrainSummary(
            stale_queue_size=len(queue),
            candidates_considered=len(candidates),
            skipped_budget=len(skipped),
            stale_remaining=len(queue),
            reason="completed",
        )

    usage = run_usage if run_usage is not None else TokenUsage()
    batch_pages = [
        (entry.uid, entry.path, pages[entry.uid][0], pages[entry.uid][1]) for entry in selected
    ]
    verdicts = audit_pages_via_batch(
        client,
        batch_pages,
        model=model,
        max_tokens=max_tokens,
        audit_version=AUDIT_VERSION,
        # `audit_pages_via_batch` wants a NO-ARG CALLABLE returning a
        # datetime (see `athenaeum.audit._now_iso`) -- unlike every other
        # `now` in this module, which is the point-in-time value itself
        # (matching `athenaeum.spend`'s / this module's own convention).
        # Bridged here, at the one call site that needs the callable shape.
        now=(lambda: now) if now is not None else None,
        usage=usage,
    )
    report = AuditReport(scanned=len(selected), verdicts=verdicts, used_batch=True)
    apply_audit_report(report, wiki_root)

    return NightlyDrainSummary(
        stale_queue_size=len(queue),
        candidates_considered=len(candidates),
        reaudited=len(report.audited),
        skipped_budget=len(skipped),
        failed=len(report.failed),
        stale_remaining=len(queue) - len(report.audited),
        cost_usd=report.total_cost_usd,
        reason="completed",
    )


__all__ = [
    "NightlyDrainSummary",
    "StalePageEntry",
    "compute_stale_pages",
    "estimate_page_audit_cost_usd",
    "render_stale_table",
    "run_nightly_drain",
]
