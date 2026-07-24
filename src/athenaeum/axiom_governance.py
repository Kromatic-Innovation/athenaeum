# SPDX-License-Identifier: Apache-2.0
"""Axiom promotion/demotion governance + assignment audit (issue #434).

Issue #424 added ``memory_class: axiom`` as one of the 7 recognized
taxonomy values (see :mod:`athenaeum.schemas`) — but that issue's validator
only checks that the *value itself* is recognized; it says nothing about
*who is allowed to mint one*. An axiom is meant to be untouchable ("treat
this as bedrock"), so an LLM/librarian write path silently setting
``memory_class: axiom`` on a page is exactly the failure mode #434 closes:
axiom status must be an EXPLICIT, RECORDED, HUMAN-APPROVED act, with a
symmetric way back out (demotion) — no dogma without an exit.

Design:

- **Ledger, not a schema field.** Whether a promotion record exists for a
  given slug requires looking outside the single frontmatter dict being
  validated (across the whole ledger, keyed by slug) — that is I/O a
  ``pydantic`` ``field_validator`` cannot do (see
  :mod:`athenaeum.schemas`'s ``_validate_memory_class``, which is a pure,
  no-I/O per-value check). So this module mirrors the established
  "external-context lint" shape used by
  :mod:`athenaeum.merge_type_gate` (:func:`read_memory_class`) and
  :mod:`athenaeum._lint` (:func:`lint_untyped_memory_class`): a plain
  function a caller invokes with the page's slug + the ledger, separate
  from the pydantic boundary.
- **Authorization: CLI acknowledgement, not the decisions-queue.**
  :mod:`athenaeum.decisions` (issue #401) unifies contradiction-detector
  questions and resolver merge proposals — both are LLM/resolver-authored
  proposals a human triages asynchronously. A promotion is different: it
  is the human's OWN act ("I am declaring this axiomatic"), not something
  the librarian proposed and queued for later review. Modeling it as a
  decisions-queue item would require a proposer step with no natural
  author (the librarian must NOT be the one proposing its own axiom, per
  the issue's central constraint) and would round-trip a same-session
  human decision through an async queue for no reason. A direct CLI
  acknowledgement (``athenaeum axiom promote --slug ... --reason ... --by
  ...``) records the same fields with less machinery, and is symmetric
  with ``athenaeum axiom demote``. (MCP tools MAY still be added later for
  a human-in-the-loop agent session to invoke on the human's explicit
  instruction — see :func:`record_promotion` / :func:`record_demotion`,
  which the CLI itself calls and which an MCP tool could call identically
  — but no MCP *tool* is registered by this issue beyond the read-only
  audit listing; see ``mcp_server.list_axiom_audit``.)
- **Ledger shape mirrors** :mod:`athenaeum.provenance`'s merge-provenance
  ledger (``_merge_provenance.jsonl``, issue #425): append-only JSONL,
  ``O_APPEND`` + fsync per line, a tolerant reader that skips a torn
  trailing line. Lives beside it under ``wiki/`` as
  ``_axiom_governance.jsonl`` — governance state is wiki state, not a
  cache artifact.
- **Flagging is recoverable**, matching the #93 ``KNOWN_TYPES`` / #424
  ``memory_class`` / #427 inline-PII precedent: a page carrying
  ``memory_class: axiom`` with no active promotion record is FLAGGED via
  :class:`UserWarning` when routed through this module's
  :func:`warn_if_unbacked_axiom`, not rejected outright — a hard crash here
  would break loading an already-existing (legacy or manually-authored)
  page, which the repo convention explicitly avoids for this class of
  problem (see :mod:`athenaeum.schemas` module docstring).

Scope (issue #434 point 4): an axiom may carry a ``scope`` (e.g. "applies
to resume work") narrowing where it is treated as axiomatic. ``scope`` is a
frontmatter field (see :mod:`athenaeum.schemas`'s ``WikiBase.scope``) that
round-trips through parse/serialize like ``observed_at`` — this module also
accepts an optional ``scope`` on a promotion record (the scope the human
approved AT THAT TIME), but enforcement of scope (deciding when a consumer
should or should not treat a page as axiomatic in context) is explicitly
OUT OF SCOPE here, per the issue.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Schema version stamped on every record so a future reader can migrate.
AXIOM_LEDGER_VERSION = 1

#: Sidecar filename, alongside ``_pending_merges.md`` / ``_merge_provenance.jsonl``
#: under ``wiki/``.
AXIOM_LEDGER_FILENAME = "_axiom_governance.jsonl"

#: The two recorded actions. Symmetric by design — promotion always has a
#: demotion path back out (issue #434: "no dogma without an exit").
ACTION_PROMOTE = "promote"
ACTION_DEMOTE = "demote"
VALID_ACTIONS: frozenset[str] = frozenset({ACTION_PROMOTE, ACTION_DEMOTE})


def default_axiom_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_axiom_governance.jsonl``."""
    return Path(wiki_root) / AXIOM_LEDGER_FILENAME


def build_axiom_record(
    *,
    slug: str,
    action: str,
    reason: str,
    by: str,
    scope: str | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Build one promotion/demotion record.

    Required fields (issue #434): ``slug`` (the wiki page's slug),
    ``action`` (``"promote"`` or ``"demote"``), ``reason`` (why — a human
    sentence, not optional; there is no dogma-by-default), ``by`` (who
    authorized it — a name/handle/identifier), ``at`` (stamped here from
    ``ts`` or now). ``scope`` is optional — the scope the human approved,
    if any (issue #434 point 4); ``None`` means unscoped (applies
    everywhere the page is consulted).

    Raises :class:`ValueError` if ``action`` is not one of
    :data:`VALID_ACTIONS`, or if ``slug``/``reason``/``by`` is empty.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)!r}, got {action!r}")
    if not slug or not slug.strip():
        raise ValueError("slug must be a non-empty string")
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string (no dogma without a reason)")
    if not by or not by.strip():
        raise ValueError("by must be a non-empty string (who authorized this)")

    stamp = (ts if ts is not None else datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    record: dict[str, Any] = {
        "v": AXIOM_LEDGER_VERSION,
        "slug": slug.strip(),
        "action": action,
        "reason": reason.strip(),
        "by": by.strip(),
        "at": stamp.isoformat().replace("+00:00", "Z"),
    }
    if scope is not None and scope.strip():
        record["scope"] = scope.strip()
    return record


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Mirrors :func:`athenaeum.provenance._append_jsonl_line` /
    :mod:`athenaeum.spend`'s ``_append_line``: a single small ``O_APPEND``
    write is atomic on local filesystems, so a crash can at worst leave a
    torn TRAILING line (which the reader skips), never corrupt an
    already-written record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _record_action(
    wiki_root: Path,
    *,
    slug: str,
    action: str,
    reason: str,
    by: str,
    scope: str | None,
    ledger_path: Path | None,
    ts: datetime | None,
) -> dict[str, Any]:
    record = build_axiom_record(slug=slug, action=action, reason=reason, by=by, scope=scope, ts=ts)
    target = ledger_path if ledger_path is not None else default_axiom_ledger_path(wiki_root)
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def record_promotion(
    wiki_root: Path,
    *,
    slug: str,
    reason: str,
    by: str,
    scope: str | None = None,
    ledger_path: Path | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Append a promotion record. Returns the record written.

    This is the ONLY sanctioned way a page's ``memory_class: axiom`` is
    backed by governance — see the module docstring. Unlike
    :func:`athenaeum.provenance.record_merge_provenance`, this raises on
    failure rather than swallowing it: a promotion write silently failing
    would leave a human believing an axiom is on record when it is not,
    which is the exact silent-mint failure mode #434 exists to close.
    """
    return _record_action(
        wiki_root,
        slug=slug,
        action=ACTION_PROMOTE,
        reason=reason,
        by=by,
        scope=scope,
        ledger_path=ledger_path,
        ts=ts,
    )


def record_demotion(
    wiki_root: Path,
    *,
    slug: str,
    reason: str,
    by: str,
    ledger_path: Path | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Append a demotion record. Returns the record written.

    Symmetric with :func:`record_promotion` — an axiom is never
    untouchable forever; demoting it is an equally explicit, reasoned,
    recorded act. ``scope`` is not carried on a demotion record (it demotes
    the whole promotion, not a narrower slice of it).
    """
    return _record_action(
        wiki_root,
        slug=slug,
        action=ACTION_DEMOTE,
        reason=reason,
        by=by,
        scope=None,
        ledger_path=ledger_path,
        ts=ts,
    )


def read_axiom_ledger(
    wiki_root: Path,
    *,
    ledger_path: Path | None = None,
    slug: str | None = None,
) -> list[dict[str, Any]]:
    """Read axiom governance records, tolerating a torn/partial trailing line.

    Optional ``slug`` filters to just that page's records. Returns ``[]``
    when the ledger does not exist. Malformed lines (a crash mid-write, or
    hand-editing) are skipped, not fatal — mirrors
    :func:`athenaeum.provenance.read_merge_provenance`.
    """
    target = ledger_path if ledger_path is not None else default_axiom_ledger_path(wiki_root)
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if not isinstance(record, dict):
            continue
        if slug is not None and record.get("slug") != slug:
            continue
        records.append(record)
    return records


def is_axiom_promoted(
    wiki_root: Path,
    slug: str,
    *,
    ledger_path: Path | None = None,
) -> bool:
    """True when ``slug``'s MOST RECENT ledger action is a promotion.

    Chronological last-action-wins: a promote followed later by a demote
    is no longer active (the axiom was explicitly walked back); a demote
    followed later by a re-promote IS active again — the ledger is a full
    history, not just a single flag, so a page can be promoted, demoted,
    and re-promoted, and this always reflects the latest decision.
    Records are read in file (append) order, which is write order given
    the ``O_APPEND`` discipline; ties in ``at`` fall back to that order.
    """
    records = read_axiom_ledger(wiki_root, ledger_path=ledger_path, slug=slug)
    if not records:
        return False
    return records[-1].get("action") == ACTION_PROMOTE


def warn_if_unbacked_axiom(
    meta: dict[str, Any],
    wiki_root: Path,
    *,
    slug: str | None = None,
    ledger_path: Path | None = None,
) -> bool:
    """Flag (via :class:`UserWarning`) a ``memory_class: axiom`` page with no
    active promotion record. Returns ``True`` when it flagged.

    Recoverable, NOT a raise — mirrors the #93 ``KNOWN_TYPES`` / #424
    ``memory_class`` / #427 inline-PII precedent (see module docstring):
    loading an already-existing page must not become a hard failure just
    because governance catches up with it later.

    ``slug`` defaults to the page's own ``uid`` (falls back to
    ``meta.get("name")`` slugified) when not given explicitly — callers
    that already know the on-disk slug (the wiki filename stem) should
    pass it explicitly since that's the identifier :func:`record_promotion`
    keys on.

    A page whose ``memory_class`` is anything other than ``"axiom"`` is
    never flagged by this function (nothing to check) — the check is
    additive to, not a replacement for, #424's ``_validate_memory_class``.
    """
    if meta.get("memory_class") != "axiom":
        return False
    resolved_slug = slug
    if not resolved_slug:
        from athenaeum.models import slugify

        raw_uid = meta.get("uid")
        raw_name = meta.get("name")
        if isinstance(raw_uid, str) and raw_uid.strip():
            resolved_slug = raw_uid.strip()
        elif isinstance(raw_name, str) and raw_name.strip():
            resolved_slug = slugify(raw_name)
    if resolved_slug and is_axiom_promoted(wiki_root, resolved_slug, ledger_path=ledger_path):
        return False
    import warnings

    warnings.warn(
        f"memory_class: axiom on {resolved_slug or '(unknown slug)'!r} with no active "
        f"promotion record — axiom assignment must be an explicit, recorded, "
        f"human-approved act (see #434; use `athenaeum axiom promote`)",
        UserWarning,
        stacklevel=2,
    )
    return True


def list_axiom_audit(
    wiki_root: Path,
    *,
    ledger_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Queryable audit listing: every slug's current status + its full history.

    Returns one entry per distinct slug that appears in the ledger, each
    shaped ``{"slug", "active", "history": [<records...>]}`` — ``active``
    is the same last-action-wins rule as :func:`is_axiom_promoted`, and
    ``history`` is every promote/demote record for that slug in
    chronological (append) order, so "when/why/by-whom promoted" (and any
    subsequent demotion) is fully visible. Ordered by each slug's earliest
    record's ``at`` (oldest first) for stable, readable output.
    """
    all_records = read_axiom_ledger(wiki_root, ledger_path=ledger_path)
    by_slug: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for record in all_records:
        slug = record.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if slug not in by_slug:
            by_slug[slug] = []
            order.append(slug)
        by_slug[slug].append(record)
    audit = [
        {
            "slug": slug,
            "active": by_slug[slug][-1].get("action") == ACTION_PROMOTE,
            "history": by_slug[slug],
        }
        for slug in order
    ]
    return audit


__all__ = [
    "AXIOM_LEDGER_VERSION",
    "AXIOM_LEDGER_FILENAME",
    "ACTION_PROMOTE",
    "ACTION_DEMOTE",
    "VALID_ACTIONS",
    "default_axiom_ledger_path",
    "build_axiom_record",
    "record_promotion",
    "record_demotion",
    "read_axiom_ledger",
    "is_axiom_promoted",
    "warn_if_unbacked_axiom",
    "list_axiom_audit",
]
