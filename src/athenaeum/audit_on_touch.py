# SPDX-License-Identifier: Apache-2.0
"""Audit-on-touch — librarian-side integration for the athenaeum#1624 audit pass
(issue athenaeum#1627).

Before the librarian proposes a change to a wiki page — a tier-3 merge
rewriting it (:func:`athenaeum.tiers.tier3_merge`), a merge proposal being
written for it (:func:`athenaeum.pending_merges.write_pending_merge`), or a
name-collision writeup naming it (:func:`athenaeum.name_collisions.
resolve_name_collisions`) — this module re-audits that page via
:func:`athenaeum.audit.audit_page` so ``last_audited``/``audit_version``
stay current and any determinable empty coordinate (``valid_from``/
``valid_until``/``claimed_scope``) gets filled before the separator-
dimension comparator (:mod:`athenaeum.dimensions`) needs to read it (issue
athenaeum#1244).

**Pure w.r.t. disk** — mirrors :func:`athenaeum.audit.audit_page`'s own
I/O-free contract deliberately: :func:`audit_on_touch` mutates the *meta*
dict the caller already holds in memory (via :func:`athenaeum.audit.
apply_verdict_to_meta`, the ONE stamping implementation — also used by
:func:`athenaeum.audit.apply_audit_report`) but never reads or writes a
file itself. The three call sites each already have their own write path
for the page they are about to change (a tier-3 merge's pending-updates
flush, ``write_pending_merge``'s parse/render/atomic-write cycle, ...); the
caller decides whether/how to persist the stamped ``meta``, exactly as
:mod:`athenaeum.audit`'s own module docstring documents for
:func:`~athenaeum.audit.audit_page`.

**Freshness** (issue plan step 4): a page whose ``last_audited`` is inside
:data:`DEFAULT_FRESHNESS_HOURS` (or a caller-supplied window) of *now* is
skipped and ``skipped_fresh`` is counted — this is what keeps a page
touched repeatedly in one run from being audited more than once, since a
successful audit stamps ``last_audited`` onto the SAME in-memory ``meta``
object before returning, so the very next touch of that page — even from a
different caller sharing the same object — sees a fresh timestamp; and
because the caller's own write path (which every one of the three
integration points already has) commits that stamp to disk, a later touch
of the SAME page from a re-read copy sees it too.

**Graceful degradation** (issue plan step 5): never raises, and never
blocks the touch it is guarding. ``client is None`` counts
``skipped_unavailable`` and the caller proceeds exactly as it did before
this issue landed — same posture :func:`athenaeum.audit.audit_page` itself
already takes for a bad response or a transport failure (``error`` set on
the verdict, never a raise).

Layering: L4 domain/pipeline — same tier as :mod:`athenaeum.audit`,
:mod:`athenaeum.tiers`, :mod:`athenaeum.pending_merges`,
:mod:`athenaeum.name_collisions`, and :mod:`athenaeum.librarian`, the four
modules that import this one. Imports :mod:`athenaeum.audit` (L4) only,
and only via a deferred, function-local import (matching this codebase's
convention for L4-to-L4 edges — see e.g. ``librarian.py``'s
``_run_wiki_dedup_phase``) so a module that never reaches an audited touch
point pays nothing for this import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from athenaeum.audit import AuditVerdict

#: The issue's suggested default freshness window, in hours. A page whose
#: ``last_audited`` is newer than this (relative to the call's *now*) is
#: skipped rather than re-audited. Configurable per call via
#: :func:`audit_on_touch`'s own *freshness_hours* parameter, and — for the
#: librarian's own three call sites — via
#: :func:`athenaeum.config.resolve_audit_on_touch_freshness_hours` (env >
#: yaml ``librarian.audit_on_touch_freshness_hours`` > this constant).
DEFAULT_FRESHNESS_HOURS: float = 24.0

#: The ``last_audited`` timestamp shape :func:`athenaeum.audit._now_iso`
#: stamps — parsed here (not re-derived) to decide freshness.
_LAST_AUDITED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class AuditOnTouchCounters:
    """Run-scoped, mutable accumulator for the five ``audit_on_touch.*``
    run-summary counters (issue athenaeum#1627 plan step 6).

    A single instance is meant to be shared across every touch this run
    makes (the entity/tier-3 phase, the name-collision phase, ...) — the
    counters are a per-RUN total, not per-call-site. See
    :meth:`as_profile_fields` for how this becomes a
    :attr:`athenaeum.librarian.RunContext.run_profile` entry.
    """

    audited: int = 0
    skipped_fresh: int = 0
    skipped_unavailable: int = 0
    coordinates_filled: int = 0
    coordinates_undeterminable: int = 0

    @property
    def is_zero(self) -> bool:
        return not (
            self.audited
            or self.skipped_fresh
            or self.skipped_unavailable
            or self.coordinates_filled
            or self.coordinates_undeterminable
        )

    def as_profile_fields(self) -> dict[str, int]:
        """Field dict for an ``("audit_on_touch", secs, fields)``
        :attr:`~athenaeum.librarian.RunContext.run_profile` entry — bare
        keys (``audited``, not ``audit_on_touch.audited``), matching every
        other phase's fields dict; the ``audit_on_touch.`` prefix in the
        issue body is the phase-qualified name a reader uses to talk about
        the field, not a literal dict key (a literal ``.`` would not match
        :data:`athenaeum.run_summary_log._KV_RE`'s ``\\w+`` key pattern).
        """
        return {
            "audited": self.audited,
            "skipped_fresh": self.skipped_fresh,
            "skipped_unavailable": self.skipped_unavailable,
            "coordinates_filled": self.coordinates_filled,
            "coordinates_undeterminable": self.coordinates_undeterminable,
        }


def _is_fresh(meta: dict[str, Any], *, freshness_hours: float, now: datetime) -> bool:
    """True when *meta*'s ``last_audited`` is inside *freshness_hours* of *now*."""
    raw = meta.get("last_audited")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        stamped = datetime.strptime(raw.strip(), _LAST_AUDITED_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return (now - stamped) < timedelta(hours=freshness_hours)


def audit_on_touch(
    client: Any,
    *,
    uid: str,
    path: Path,
    meta: dict[str, Any],
    body: str,
    model: str,
    counters: AuditOnTouchCounters,
    freshness_hours: float = DEFAULT_FRESHNESS_HOURS,
    audit_version: str | None = None,
    now: Callable[[], datetime] | None = None,
    max_tokens: int | None = None,
) -> "AuditVerdict | None":
    """Re-audit ONE page before the caller proposes a change to it.

    Returns the :class:`~athenaeum.audit.AuditVerdict` on an actual audit
    attempt (including an errored one — see ``AuditVerdict.error``), or
    ``None`` on a skip (fresh, or no client available) — see the module
    docstring's "Freshness" / "Graceful degradation" sections for the two
    skip paths. ``meta`` is mutated in place ONLY when the audit both ran
    and succeeded (``error is None``); a skip or an errored audit leaves it
    untouched, mirroring :func:`athenaeum.audit.apply_audit_report`'s own
    "only a successful verdict is ever written" rule — an errored audit
    must not stamp a ``last_audited`` that would make the page look
    checked when it was not.

    Never raises. The caller's own touch (a merge write, a proposal write,
    ...) must proceed unconditionally after this call returns, whatever it
    returned — this function's two skip branches are ordinary control flow,
    not exceptions, and :func:`~athenaeum.audit.audit_page` itself already
    turns a bad response or transport failure into an ``error``-carrying
    verdict rather than a raise.
    """
    now_dt = now() if now is not None else datetime.now(timezone.utc)
    if _is_fresh(meta, freshness_hours=freshness_hours, now=now_dt):
        counters.skipped_fresh += 1
        return None
    if client is None:
        counters.skipped_unavailable += 1
        return None

    from athenaeum.audit import AUDIT_VERSION, apply_verdict_to_meta, audit_page

    call_kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens

    verdict = audit_page(
        client,
        uid=uid,
        path=path,
        meta=meta,
        body=body,
        model=model,
        audit_version=audit_version if audit_version is not None else AUDIT_VERSION,
        now=lambda: now_dt,
        **call_kwargs,
    )
    counters.audited += 1

    if verdict.error is None:
        filled, undeterminable = apply_verdict_to_meta(meta, verdict)
        counters.coordinates_filled += filled
        counters.coordinates_undeterminable += undeterminable

    return verdict


__all__ = [
    "DEFAULT_FRESHNESS_HOURS",
    "AuditOnTouchCounters",
    "audit_on_touch",
]
