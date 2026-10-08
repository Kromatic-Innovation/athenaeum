# SPDX-License-Identifier: Apache-2.0
"""Sticky PII-classification verdicts, recorded through the athenaeum#712
ledger (issue athenaeum#689).

Issue athenaeum#689's policy (``docs/design/pii-classification-policy.md``)
and schema (:mod:`athenaeum.pii_classification_decision`) decide WHAT a
PII-shaped value is. This module is the part that makes a decided value
**sticky**: once judged, a value does not resurface on a later sweep.
**This module implements no ledger of its own** — every write and read
below goes through :mod:`athenaeum.verdicts` (issue athenaeum#712) or, for
the one case that ledger refuses by its own design, through
:mod:`athenaeum.off_corpus` (issue athenaeum#984). No new storage format is
introduced here.

**Why the two PII verdict directions are NOT symmetric.** A pairwise
comparator verdict (``duplicate | contradiction | specialization | distinct
| underdetermined``) compares two pages and is never itself personal data.
A PII classification verdict is different in one direction only:

* A **not-PII** verdict (one of
  :data:`athenaeum.pii_classification_decision.NOT_PII_CLASSES` — an SSH
  host alias, a calendar id, a page-purpose exemption, a test/role
  account) says the value is NOT a person's contact data. Recording it —
  including the value itself — in the in-git ledger carries the same
  privacy weight as the existing ``wiki/_pii-allowlist.yml`` artifact
  (issue athenaeum#936), which already stores adjudicated-safe values in
  plaintext. :func:`record_not_pii` therefore writes a normal
  :class:`athenaeum.verdicts.VerdictEntry` to the in-git ledger via
  :func:`athenaeum.verdicts.append_verdict`.

* An **is-PII** verdict (:data:`athenaeum.pii_classification_decision.CLASS_IS_PII`)
  says the value IS a person's genuine contact data. That verdict's own
  subject is erasure-class content by construction — exactly the case
  issue athenaeum#712's Out-of-scope section refuses:
  "this issue must not write erasure-class content or plain hashes of
  short low-entropy personal facts into the in-git ledger." Recording an
  is-PII verdict's value in Git would be the exact leak this policy
  exists to stop, just relocated into the ledger instead of a page body.
  :func:`record_is_pii` therefore NEVER writes to the in-git ledger: it
  routes to the off-corpus ledger shard
  (:func:`athenaeum.off_corpus.append_verdict_off_corpus`) when off-corpus
  is configured, exactly mirroring
  :func:`athenaeum.verdicts.record_pair_decision`'s own erasure-class
  routing, and otherwise refuses and reports, never silently drops.

This asymmetry is this issue's own worked instance of the policy
document's "over-restoring is worse than under-restoring": the direction
that is cheap to get wrong (recording a true non-PII value) is recorded
plainly; the direction that is catastrophic to get wrong (a real address
landing in Git history) is refused by construction, not by convention.

**Pair key.** A PII verdict has one subject (a value on a page), not a
pair of pages, so it does not fit
:func:`athenaeum.verdicts.make_pair_key`'s two-page-id contract literally.
This module reuses it anyway — ``make_pair_key(page_id, value)`` for a
not-PII verdict — because the comparator's own pair keys are always two
REAL page ids drawn from the corpus; a contact value is not a page id, so
collision with a comparator pair is not just unlikely but requires a page
literally titled after someone's email address, which the comparator's own
slugging would also choke on. :func:`lookup_not_pii_verdict` additionally
filters by verdict class, so even a theoretical key collision could not
read a comparator verdict as a PII one or vice versa.

**Basis.** The comparator's ``content_hashes`` basis element is the
*page's* content hash — appropriate for a verdict about two pages, wrong
for a verdict about one value: editing an unrelated sentence elsewhere on
the page would then invalidate (and silently un-stick) a verdict about a
value that never changed. A PII verdict's basis instead carries the
classified value and its policy class directly in ``authority_basis``
(``"pii-classification:<class>"``) with ``content_hashes`` explicitly
``None``, recorded in ``null_reasons`` — correct per :class:`Basis`'s own
"populated or explicitly null with a documented reason" contract, and it
means a verdict only goes stale when THIS module says so (it currently
never auto-invalidates; re-judging a value is an explicit human/agent act).

Layering: L4 domain/pipeline module, same tier as :mod:`athenaeum.recompare`
and :mod:`athenaeum.storage_migrate`. Imports L2 (:mod:`athenaeum.verdicts`,
:mod:`athenaeum.runlock`) and L1/L2 (:mod:`athenaeum.off_corpus`) services.
Deliberately NOT imported by :mod:`athenaeum.pii` (L1) or
:mod:`athenaeum.verdicts` (L2) — both sit below this module in the
dependency order, so a caller that needs a sticky-verdict read must sit
ABOVE this module too (the ``lint-pii`` CLI command and
:mod:`athenaeum.recompare`'s hazard check both qualify; see their call
sites for the wiring).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.off_corpus import OffCorpusConfigError, append_verdict_off_corpus, off_corpus_root
from athenaeum.pii_classification_decision import (
    CLASS_IS_PII,
    NOT_PII_CLASSES,
    PII_CLASSES,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    Basis,
    VerdictEntry,
    append_verdict,
    iter_live_entries,
    ledger_dir,
    make_pair_key,
)

#: Prefix stamped on a PII verdict's ``authority_basis`` so it is
#: unambiguously distinguishable from a comparator verdict's basis, which
#: never uses this prefix (comparator bases carry ``"implicit-superuser"``
#: or an explicit human ref — see :mod:`athenaeum.verdicts`' tests).
_AUTHORITY_BASIS_PREFIX = "pii-classification:"


class PiiVerdictError(RuntimeError):
    """Base class for errors raised by this module."""


def _pii_pair_key(page_id: str, value: str) -> str:
    """The ledger pair key a PII verdict about *value* on *page_id* is stored under."""
    return make_pair_key(page_id, value)


def _basis_for(verdict_class: str) -> Basis:
    return Basis(
        content_hashes=[None, None],
        null_reasons={
            "content_hashes": (
                "pii-classification verdict: the basis is the classified "
                "value and policy class (see authority_basis), not a page "
                "content hash — see athenaeum.pii_verdicts module docstring"
            )
        },
        authority_basis=f"{_AUTHORITY_BASIS_PREFIX}{verdict_class}",
    )


def record_not_pii(
    wiki_root: Path,
    *,
    page_id: str,
    value: str,
    verdict_class: str,
    decided_by: str,
    lock: RunLock,
    at: str | None = None,
) -> VerdictEntry:
    """Record a sticky "not PII" verdict in the in-git athenaeum#712 ledger.

    *verdict_class* must be one of
    :data:`athenaeum.pii_classification_decision.NOT_PII_CLASSES`. Safe to
    write to the in-git ledger in plaintext — see module docstring. Raises
    :class:`athenaeum.verdicts.LockNotHeld` if *lock* is not acquired
    (:func:`athenaeum.verdicts.append_verdict`'s own guard).
    """
    if verdict_class not in NOT_PII_CLASSES:
        raise PiiVerdictError(
            f"record_not_pii: verdict_class must be one of {sorted(NOT_PII_CLASSES)}, "
            f"got {verdict_class!r} (is-PII verdicts must use record_is_pii)"
        )
    entry = VerdictEntry(
        pair=_pii_pair_key(page_id, value),
        verdict=verdict_class,
        basis=_basis_for(verdict_class),
        # The raw value, verbatim -- NOT a comparator separator dimension.
        # make_pair_key() sorts its two arguments, so the pair key alone
        # cannot be un-sorted back into (page_id, value) without already
        # knowing one side. Recording the value here is what lets
        # load_not_pii_allowlist() enumerate every sticky not-PII value
        # across the whole ledger without a page_id in hand.
        separator=[value],
        decided_by=decided_by,
        at=at or date.today().isoformat(),
    )
    append_verdict(wiki_root, entry, lock=lock)
    return entry


def record_is_pii(
    *,
    page_id: str,
    value: str,
    decided_by: str,
    knowledge_root: Path | None = None,
    config: dict[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Record a sticky "is PII" verdict — NEVER into the in-git ledger.

    An is-PII verdict is erasure-class content by construction (see module
    docstring), so this function refuses the in-git ledger unconditionally
    and routes to the off-corpus ledger shard instead, mirroring
    :func:`athenaeum.verdicts.record_pair_decision`'s existing erasure-class
    routing exactly:

    * ``config``/``knowledge_root`` supplied and off-corpus enabled: written
      to :func:`athenaeum.off_corpus.append_verdict_off_corpus`. Returns
      ``{"ok": True, "error_code": None, "pair": <pair_key>}``.
    * otherwise: refused and reported, never silently dropped. Returns
      ``{"ok": False, "error_code": "erasure_class_refused", "pair": None}``
      — the exact pre-athenaeum#984 contract
      :func:`athenaeum.verdicts.record_pair_decision` documents for the
      same case.
    * the off-corpus write itself raises (disk/fsync failure, or a
      misconfigured adapter caught only at write time): returns
      ``{"ok": False, "error_code": "off_corpus_write_failed", "pair": None}``
      — this function's documented contract is to never raise and always
      return a result dict (Seer finding, PR athenaeum#2013), so a write
      failure is reported through the same shape as every other refusal
      rather than propagated as an exception.
    """
    pair_key = _pii_pair_key(page_id, value)
    entry = VerdictEntry(
        pair=pair_key,
        verdict=CLASS_IS_PII,
        basis=_basis_for(CLASS_IS_PII),
        # See record_not_pii()'s comment: the raw value, verbatim, so a
        # reader can recover it without already knowing page_id. Safe here
        # because this entry is NEVER written to the in-git ledger (below).
        separator=[value],
        decided_by=decided_by,
        at=at or date.today().isoformat(),
    )

    if config is not None and knowledge_root is not None:
        try:
            root = off_corpus_root(config, knowledge_root)
        except OffCorpusConfigError:
            root = None
        if root is not None:
            try:
                append_verdict_off_corpus(config, knowledge_root, entry.to_dict(), at=entry.at)
            except Exception:  # noqa: BLE001 - never-raise contract; reported via error_code
                return {"ok": False, "error_code": "off_corpus_write_failed", "pair": None}
            return {"ok": True, "error_code": None, "pair": pair_key}

    return {"ok": False, "error_code": "erasure_class_refused", "pair": None}


def lookup_not_pii_verdict(wiki_root: Path, *, page_id: str, value: str) -> VerdictEntry | None:
    """The live in-git "not PII" verdict for *value* on *page_id*, or ``None``.

    Filters by verdict class as well as pair key — see module docstring's
    "Pair key" section for why this belt-and-braces check is cheap and
    worth keeping even though a real collision is not realistically
    reachable.
    """
    pair_key = _pii_pair_key(page_id, value)
    candidates = [
        e
        for _, e in iter_live_entries(wiki_root)
        if e.pair == pair_key and e.verdict in NOT_PII_CLASSES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.at)


def ledger_scan_exclusions(wiki_root: Path) -> list[Path]:
    """Every file under the in-git ledger directory, to exclude from a PII scan.

    The ledger legitimately stores a not-PII value's raw text (see module
    docstring) PLUS that value concatenated into a pair key
    (``"<page_id>+<value>"`` — see :func:`athenaeum.verdicts.make_pair_key`),
    which a naive email/phone scanner can match as its OWN, different-looking
    token. Scanning the ledger's own files would therefore turn every sticky
    verdict into a fresh, unexplained finding about itself — exactly the
    self-referential trap ``_pii-allowlist.yml`` already avoids by excluding
    itself from its own scan (see :func:`athenaeum._cmd_storage._cmd_storage_lint_pii`).
    This is that same exclusion, extended to this ledger's directory.
    """
    d = ledger_dir(wiki_root)
    if not d.is_dir():
        return []
    return sorted(d.glob("*"))


def load_not_pii_allowlist(wiki_root: Path) -> dict[str, str]:
    """Every sticky "not PII" verdict in the in-git ledger, as ``{value: reason}``.

    Shaped EXACTLY like :func:`athenaeum.pii.load_pii_allowlist`'s return
    value — the ``{value: reason}`` mapping both
    :func:`athenaeum._cmd_storage._cmd_storage_lint_pii` and
    :func:`athenaeum.recompare.identify_pii_hazards` already accept, so a
    caller merges this ledger-backed result into the existing
    ``_pii-allowlist.yml``-derived mapping with a single ``dict`` update —
    no new parameter shape, no second adjudication code path. This is AC3's
    "a value once judged keep never resurfaces" made queryable in bulk,
    the counterpart to :func:`is_marked_not_pii`'s single-value check.

    When the same value was judged not-PII more than once (re-classified on
    different pages, or re-affirmed), the MOST RECENT verdict's reason wins
    — :func:`athenaeum.verdicts.iter_live_entries` has no stable order
    across pages, so entries are sorted by ``at`` before folding, mirroring
    :func:`lookup_not_pii_verdict`'s own "latest wins" rule.
    """
    entries = [
        e for _, e in iter_live_entries(wiki_root) if e.verdict in NOT_PII_CLASSES and e.separator
    ]
    entries.sort(key=lambda e: e.at)
    out: dict[str, str] = {}
    for entry in entries:
        value = entry.separator[0]
        out[value] = (
            f"pii-classification verdict ({entry.verdict}), decided_by={entry.decided_by}"
        )
    return out


def is_marked_not_pii(wiki_root: Path, *, page_id: str, value: str) -> bool:
    """True when *value* on *page_id* carries a sticky "not PII" verdict."""
    return lookup_not_pii_verdict(wiki_root, page_id=page_id, value=value) is not None


def _iter_off_corpus_pii_entries(
    config: dict[str, Any] | None, knowledge_root: Path
) -> list[VerdictEntry]:
    try:
        root = off_corpus_root(config, knowledge_root)
    except OffCorpusConfigError:
        return []
    if root is None:
        return []
    ledger_dir = root / "_verdicts"
    if not ledger_dir.is_dir():
        return []
    out: list[VerdictEntry] = []
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            import json

            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("verdict") in PII_CLASSES:
                    out.append(VerdictEntry.from_dict(record))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return out


def lookup_is_pii_verdict(
    *,
    page_id: str,
    value: str,
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> VerdictEntry | None:
    """The live off-corpus "is PII" verdict for *value* on *page_id*, or ``None``.

    Reads ONLY the off-corpus ledger shard — an is-PII verdict is never in
    the in-git ledger (see module docstring). ``None`` when off-corpus is
    not configured, exactly as :func:`record_is_pii` refuses to write one
    in that case.
    """
    pair_key = _pii_pair_key(page_id, value)
    candidates = [
        e
        for e in _iter_off_corpus_pii_entries(config, knowledge_root)
        if e.pair == pair_key and e.verdict == CLASS_IS_PII
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.at)


def is_marked_is_pii_migrated(
    *,
    page_id: str,
    value: str,
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> bool:
    """True when *value* on *page_id* already carries a sticky "is PII" verdict.

    This is AC4's other direction's suppression check: a caller (the
    migration-proposal seam — see :mod:`athenaeum.pending_merges_pii` /
    :mod:`athenaeum.storage_migrate`'s call sites) gating on this returning
    ``True`` is honoring "an is-PII verdict stops re-proposing an
    already-migrated value" — the value has already been judged and
    (by the time this is checked) migrated off-corpus, so it should not be
    re-raised as a fresh classification question.
    """
    return (
        lookup_is_pii_verdict(
            page_id=page_id, value=value, knowledge_root=knowledge_root, config=config
        )
        is not None
    )


def load_is_pii_values(
    *, knowledge_root: Path, config: dict[str, Any] | None
) -> frozenset[str]:
    """Every value already carrying a sticky off-corpus "is PII" verdict.

    The counterpart to :func:`load_not_pii_allowlist`, for AC4's other
    direction: a caller that has a set of candidate values about to be
    raised as a fresh migration/classification question (the
    :mod:`athenaeum.pending_merges_pii` / :mod:`athenaeum.storage_migrate`
    seam) excludes any value already in this set — it is not a new
    question, it is already judged and already migrated. ``frozenset()``
    when off-corpus is not configured, exactly as :func:`record_is_pii`
    would refuse to write one in that case.

    Filters explicitly by ``CLASS_IS_PII`` (not just "any PII_CLASSES
    entry", which ``_iter_off_corpus_pii_entries`` already narrows to) —
    today the off-corpus ledger only ever receives ``CLASS_IS_PII``
    entries via :func:`record_is_pii`, but this keeps the guarantee
    explicit rather than incidental if a future writer ever appends a
    different PII class to the same shard (Seer finding, PR
    athenaeum#2013).
    """
    return frozenset(
        e.separator[0]
        for e in _iter_off_corpus_pii_entries(config, knowledge_root)
        if e.separator and e.verdict == CLASS_IS_PII
    )


__all__ = [
    "PiiVerdictError",
    "record_not_pii",
    "record_is_pii",
    "lookup_not_pii_verdict",
    "is_marked_not_pii",
    "load_not_pii_allowlist",
    "ledger_scan_exclusions",
    "lookup_is_pii_verdict",
    "is_marked_is_pii_migrated",
    "load_is_pii_values",
]
