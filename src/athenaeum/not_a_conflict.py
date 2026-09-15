# SPDX-License-Identifier: Apache-2.0
"""Fingerprint-keyed not-a-conflict adapter over the verdict ledger (issue
athenaeum#1679, §3.7) — L3 service.

C4's own not-a-conflict suppression cache
(:func:`athenaeum.fingerprint.claim_pair_fingerprint` plus the flat
``raw/_resolved_contradictions.jsonl`` file it keys) never landed a comparator-
side equivalent. Rather than build a SECOND ledger, this module is a thin
adapter over the verdict store :mod:`athenaeum.verdicts` already owns —
:func:`~athenaeum.verdicts.append_verdict`,
:func:`~athenaeum.verdicts.lookup_pair` (via
:func:`~athenaeum.verdicts.get_verdict_status`), and the ledger's own
``fresh``/``stale`` notion — keyed differently from every other write to that
store.

**Keying is the whole adapter.** Every other verdict writer keys entries with
:func:`athenaeum.verdicts.make_pair_key`, a ``"<page_id_a>+<page_id_b>"``
string — a verdict about two SPECIFIC pages. A not-a-conflict adjudication is
about a CLAIM pair, independent of which page happens to carry it (the exact
problem :mod:`athenaeum.fingerprint`'s module docstring names: "an already-
adjudicated claim re-escalates as a brand-new pending question on every new
page that carries it"). So this module keys with
:func:`athenaeum.fingerprint.claim_pair_fingerprint` instead — a 16-hex-char
SHA-1 prefix over the two normalized claim texts (order-independent) and the
conflict type. Structurally disjoint from ``make_pair_key``'s output (which
always contains a literal ``+`` a hex digest never does), so a not-a-conflict
entry can never collide with, or be mistaken for, an ordinary page-pair
verdict sharing the same live partitions.

**Verdict value, not a new one.** :data:`athenaeum.verdicts.VERDICT_VALUES`
is a closed five-value tuple with no ``not_a_conflict`` literal, and
:func:`athenaeum.verdicts.build_verdict_entry` raises on anything outside it.
Of the five, ``distinct`` already means "this pair was adjudicated as NOT
actually conflicting" (see :mod:`athenaeum.comparator`'s ``VERDICT_DISTINCT``
branch) -- reusing it keeps this adapter inside the existing schema instead
of widening the ledger's vocabulary. :data:`DECIDED_BY` stamps every entry
this adapter writes so a ledger reader can tell a fingerprint-keyed
not-a-conflict adjudication apart from an ordinary page-pair ``distinct``
verdict at a glance.

**TTL decay is this adapter's own addition, layered on top.** The ledger's
own freshness notion (``VerdictEntry.stale``) only flips when something
explicitly calls :func:`athenaeum.verdicts.mark_pairs_stale` for one of its
existing invalidation triggers (changed page, dimension change, coordinate
challenge, tree/comparator epoch bump, authority revoked) -- none of which is
"elapsed wall-clock time since decided", and none of which this module may
add to ``verdicts.py`` (a parallel lane owns that file tonight; §3.7's own
brief directs building an adapter, not editing the store). So
:func:`is_not_a_conflict` computes TTL expiry itself at READ time from
``VerdictEntry.at``, entirely without touching the ledger's own ``stale``
field or requiring a write to expire an entry.

Out of scope (per athenaeum#1679's own "Out of scope" section, §3.6): wiring
this adapter into the resolver's ``not_a_conflict``/``propose_merge``/
``attribute_both`` action lane. That is a pending, separate operator
decision. This module only proves the primitive -- record and query a
fingerprint-keyed not-a-conflict adjudication -- exists and works.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.fingerprint import claim_pair_fingerprint
from athenaeum.runlock import RunLock
from athenaeum.verdicts import Basis, VerdictEntry, append_verdict, get_verdict_status

#: See "Verdict value, not a new one" above.
NOT_A_CONFLICT_VERDICT = "distinct"

#: ``decided_by`` stamp for entries this adapter writes.
DECIDED_BY = "not_a_conflict_adapter"


def not_a_conflict_key(text_a: str, text_b: str, conflict_type: str | None) -> str:
    """The ledger ``pair`` key this adapter reads/writes under.

    A thin, named wrapper over :func:`athenaeum.fingerprint.claim_pair_fingerprint`
    so callers of this module never need to import ``fingerprint`` directly
    just to predict the key :func:`mark_not_a_conflict` will use.
    """
    return claim_pair_fingerprint(text_a, text_b, conflict_type)


def mark_not_a_conflict(
    wiki_root: Path,
    text_a: str,
    text_b: str,
    conflict_type: str | None,
    *,
    lock: RunLock,
    decided_by: str = DECIDED_BY,
    at: str | None = None,
) -> str:
    """Adjudicate ``(text_a, text_b, conflict_type)`` as not a real conflict.

    Appends ONE :class:`~athenaeum.verdicts.VerdictEntry` to the same live
    monthly partition every other verdict writer appends to
    (:func:`athenaeum.verdicts.append_verdict`) -- no second ledger, no new
    file. Keyed by :func:`not_a_conflict_key` rather than
    :func:`athenaeum.verdicts.make_pair_key`'s page-id pair, so the SAME
    claim pair resurfacing on a brand-new page is recognized by
    :func:`is_not_a_conflict` without re-escalating.

    Requires an ALREADY-ACQUIRED *lock* -- identical contract to
    :func:`athenaeum.verdicts.append_verdict` (raises
    :class:`athenaeum.verdicts.LockNotHeld` otherwise). Returns the
    fingerprint key written.
    """
    fp = claim_pair_fingerprint(text_a, text_b, conflict_type)
    entry = VerdictEntry(
        pair=fp,
        verdict=NOT_A_CONFLICT_VERDICT,
        basis=Basis(comparator_version=DECIDED_BY),
        at=at or date.today().isoformat(),
        decided_by=decided_by,
    )
    append_verdict(wiki_root, entry, lock=lock)
    return fp


def _parse_at(value: str | None) -> date | None:
    """Best-effort ``VerdictEntry.at`` -> :class:`date`.

    Fail-open to ``None`` on anything unparseable rather than raising --
    :func:`is_not_a_conflict` treats ``None`` as expired (re-escalate), never
    as "still fresh", so a malformed date can never silently suppress a
    conflict forever.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def is_not_a_conflict(
    wiki_root: Path,
    text_a: str,
    text_b: str,
    conflict_type: str | None,
    *,
    ttl_days: int | None = None,
    now: date | None = None,
) -> bool:
    """True iff this claim pair is a FRESH not-a-conflict adjudication.

    Fresh means: decided, the decided verdict is :data:`NOT_A_CONFLICT_VERDICT`,
    the ledger's own ``fresh`` flag is true (i.e. nothing has explicitly
    called :func:`athenaeum.verdicts.mark_pairs_stale` on it), AND -- the TTL
    decay this adapter adds on top, see the module docstring -- when
    *ttl_days* is given, the entry was decided no more than *ttl_days* ago
    relative to *now* (defaults to :func:`datetime.date.today`).

    ``ttl_days=None`` (the default) disables this adapter's own decay and
    defers entirely to the ledger's ``fresh`` flag, matching plain
    :func:`athenaeum.verdicts.get_verdict_status` semantics.
    """
    fp = claim_pair_fingerprint(text_a, text_b, conflict_type)
    status = get_verdict_status(wiki_root, fp)
    if not status["decided"] or status["verdict"] != NOT_A_CONFLICT_VERDICT:
        return False
    if not status["fresh"]:
        return False
    if ttl_days is not None:
        decided_at = _parse_at(status["at"])
        if decided_at is None:
            return False
        today = now or date.today()
        if (today - decided_at).days > ttl_days:
            return False
    return True


__all__ = [
    "DECIDED_BY",
    "NOT_A_CONFLICT_VERDICT",
    "is_not_a_conflict",
    "mark_not_a_conflict",
    "not_a_conflict_key",
]
