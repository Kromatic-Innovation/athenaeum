# SPDX-License-Identifier: Apache-2.0
"""Relevance policy — the validated writer, its audit trail and remote proposals.

Issue athenaeum#2649 (slice 7 of the relevance-policy epic athenaeum#2598). The
``set_capture_rule`` MCP tool records "always remember travel preferences" as
**policy**, never as a fact: it appends one rule to
``wiki/_schema/relevance-policy.md`` (:mod:`athenaeum.relevance_policy`) and
nothing enters the corpus.

Two routes, chosen by the transport the server is running under:

* **Local (stdio)** — :func:`apply_rule_edit` edits the file directly and
  appends one record to ``wiki/_relevance_policy_audit.jsonl`` (rules before
  and after, client transport, time).
* **Remote (``--transport http``)** — :func:`file_rule_edit_proposal` only
  appends a proposal to ``wiki/_relevance_policy_edit_proposals.jsonl``
  (modelled on :mod:`athenaeum.policy_pack_edit_proposals`). It surfaces in the
  decision queue as a ``relevance-policy-edit`` item; an operator's
  ``approve`` runs :func:`resolve_rule_edit_proposal`, which applies the edit
  through the same writer (so the audit record is identical, plus the
  proposal id) and a ``reject`` discards it.

**Deterministic.** The caller (a model) supplies a structured rule — a
verdict plus type / tag / subject. There is no natural-language parsing and no
LLM call here.

**Semantics preserved.** The file is read top to bottom and the last matching
rule wins, so a new rule is APPENDED: it overrides any earlier rule it
overlaps and every earlier line (including operator comments) is untouched.
The new text is parsed back through :func:`parse_relevance_policy` with the
loader's own vocabulary before anything is written; an invalid rule, or a
policy file that is already invalid, writes nothing. Forward-only: nothing
here touches existing pages.

Layering: L4 domain/pipeline module. Imports :mod:`athenaeum.relevance_policy`
(L2), :mod:`athenaeum.atomic_io` (L0) and :mod:`athenaeum.store` (L1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.relevance_policy import (
    POLICY_FILENAME,
    RelevancePolicy,
    RelevancePolicyError,
    load_relevance_policy,
    parse_relevance_policy,
    policy_vocabulary,
)
from athenaeum.store import append_line_durable, now_iso

#: Schema version stamped on every audit / proposal record.
LEDGER_VERSION = 1

#: Sidecars under ``wiki_root``.
AUDIT_FILENAME = "_relevance_policy_audit.jsonl"
PROPOSALS_FILENAME = "_relevance_policy_edit_proposals.jsonl"

PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"

#: Decision-queue type string (see ``decision_framing`` / ``decision_answers``).
DECISION_TYPE = "relevance-policy-edit"

_HEADER_ROW = "| Verdict | Type | Tag | Subject |"
_SEPARATOR_ROW = "|---------|------|-----|---------|"


def audit_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / AUDIT_FILENAME


def proposals_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / PROPOSALS_FILENAME


def _policy_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / "_schema" / POLICY_FILENAME


RuleKey = tuple[str, "str | None", "str | None", "str | None"]


def normalize_rule(
    verdict: str, type_: str | None, tag: str | None, subject: str | None
) -> RuleKey:
    """Canonical ``(verdict, type, tag, subject)``: lowercase verdict, blank -> ``None``.

    Raises :class:`RelevancePolicyError` for a value that cannot live in one
    markdown table cell (``|`` or a line break).
    """

    def clean(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value in ("", "-"):
            return None
        if any(ch in value for ch in ("|", "\n", "\r", "`")):
            raise RelevancePolicyError(
                f"invalid rule value {value!r}: must not contain '|', a backtick or a line break"
            )
        return value

    return ((verdict or "").strip().lower(), clean(type_), clean(tag), clean(subject))


def _rule_dict(key: RuleKey) -> dict[str, str | None]:
    return {"verdict": key[0], "type": key[1], "tag": key[2], "subject": key[3]}


def _rule_row(key: RuleKey) -> str:
    return "| " + " | ".join(c or "" for c in key) + " |"


def _keys(policy: RelevancePolicy) -> list[RuleKey]:
    return [(r.verdict, r.type, r.tag, r.subject) for r in policy.rules]


def _snapshot(policy: RelevancePolicy) -> dict[str, Any]:
    return {
        "hash": policy.canonical_hash()[:8] if policy.present else None,
        "rules": [list(k) for k in _keys(policy)],
    }


def _append_row(existing: str | None, row: str) -> str:
    """Insert *row* after the last table row of *existing* (or create the table)."""
    if not existing:
        return f"{_HEADER_ROW}\n{_SEPARATOR_ROW}\n{row}\n"
    lines = existing.splitlines()
    last_table = max((i for i, ln in enumerate(lines) if ln.strip().startswith("|")), default=None)
    if last_table is None:
        sep = "" if existing.endswith("\n") else "\n"
        return f"{existing}{sep}\n{_HEADER_ROW}\n{_SEPARATOR_ROW}\n{row}\n"
    lines.insert(last_table + 1, row)
    return "\n".join(lines) + "\n"


def _plan_edit(
    wiki_root: Path, key: RuleKey
) -> tuple[RelevancePolicy, RelevancePolicy | None, str]:
    """``(before, after, new_text)``; ``after`` is ``None`` for a no-op.

    Validates the rule by parsing the candidate text with the loader's own
    vocabulary and checking it round-trips (old rules unchanged, new rule
    last). Raises :class:`RelevancePolicyError` on any invalid input,
    including a policy file that is already invalid.
    """
    before = load_relevance_policy(wiki_root)
    old_keys = _keys(before)
    if old_keys and old_keys[-1] == key:
        return before, None, ""
    path = _policy_path(wiki_root)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    new_text = _append_row(existing, _rule_row(key))
    valid_types, valid_tags = policy_vocabulary(wiki_root)
    after = parse_relevance_policy(new_text, valid_types, valid_tags)
    if _keys(after) != [*old_keys, key]:  # pragma: no cover - defensive round-trip check
        raise RelevancePolicyError("rule did not round-trip through the policy loader")
    return before, after, new_text


def validate_rule(
    wiki_root: Path, verdict: str, type_: str | None, tag: str | None, subject: str | None
) -> RuleKey:
    """Normalize and validate a rule against the current policy file and vocabulary."""
    key = normalize_rule(verdict, type_, tag, subject)
    _plan_edit(Path(wiki_root), key)
    return key


@dataclass(frozen=True)
class EditResult:
    """Outcome of :func:`apply_rule_edit`."""

    changed: bool
    rule: dict[str, str | None]
    before: dict[str, Any]
    after: dict[str, Any]
    policy: RelevancePolicy


def rule_edit_is_noop(
    wiki_root: Path,
    verdict: str,
    type_: str | None = None,
    tag: str | None = None,
    subject: str | None = None,
) -> bool:
    """True when the rule is identical to the policy file's current last rule.

    Such an edit changes nothing, so the remote path must not queue a proposal
    for it (an approval would be a no-op). Raises :class:`RelevancePolicyError`
    for an invalid rule, exactly as :func:`validate_rule` does.
    """
    key = normalize_rule(verdict, type_, tag, subject)
    return _plan_edit(Path(wiki_root), key)[1] is None


def apply_rule_edit(
    wiki_root: Path,
    verdict: str,
    type_: str | None = None,
    tag: str | None = None,
    subject: str | None = None,
    *,
    transport: str,
    proposal_id: str | None = None,
    now: datetime | None = None,
) -> EditResult:
    """Append one validated rule to the policy file and record the audit entry.

    A rule identical to the file's current last rule is a no-op (nothing
    written, no audit record). Raises :class:`RelevancePolicyError` for an
    invalid rule or an already-invalid policy file; nothing is written then.
    """
    wiki_root = Path(wiki_root)
    key = normalize_rule(verdict, type_, tag, subject)
    before, after, new_text = _plan_edit(wiki_root, key)
    if after is None:
        return EditResult(False, _rule_dict(key), _snapshot(before), _snapshot(before), before)
    atomic_write_text(_policy_path(wiki_root), new_text)
    record = {
        "v": LEDGER_VERSION,
        "kind": "edit",
        "at": now_iso(now),
        "transport": transport,
        "rule": _rule_dict(key),
        "before": _snapshot(before),
        "after": _snapshot(after),
        "proposal_id": proposal_id,
    }
    append_line_durable(
        audit_path(wiki_root), (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    )
    return EditResult(True, _rule_dict(key), _snapshot(before), _snapshot(after), after)


def read_audit(wiki_root: Path) -> list[dict[str, Any]]:
    """Every well-formed audit record, oldest first (torn trailing line tolerated)."""
    return _read_jsonl(audit_path(wiki_root))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Remote proposals
# ---------------------------------------------------------------------------


def read_proposals_ledger(wiki_root: Path) -> list[dict[str, Any]]:
    return _read_jsonl(proposals_path(wiki_root))


def _resolved_ids(records: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") in (APPROVE_KIND, REJECT_KIND)}


def list_pending_proposals(wiki_root: Path) -> list[dict[str, Any]]:
    """Filed ``relevance-policy-edit`` proposals awaiting approval."""
    records = read_proposals_ledger(wiki_root)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


def _proposal_id(key: RuleKey, created_at: str) -> str:
    payload = json.dumps({"rule": list(key), "at": created_at}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def file_rule_edit_proposal(
    wiki_root: Path,
    verdict: str,
    type_: str | None = None,
    tag: str | None = None,
    subject: str | None = None,
    *,
    rationale: str = "",
    transport: str = "http",
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """File a proposal; ``(record, created)``.

    The rule is validated up front so an invalid proposal is never queued.
    A second request for a rule that already has a pending proposal returns
    that proposal (``created=False``). The policy file is NOT touched.
    """
    wiki_root = Path(wiki_root)
    key = validate_rule(wiki_root, verdict, type_, tag, subject)
    for rec in list_pending_proposals(wiki_root):
        if tuple(rec.get("rule_key") or ()) == key:
            return rec, False
    created_at = now_iso(now)
    record = {
        "v": LEDGER_VERSION,
        "kind": PROPOSAL_KIND,
        "id": _proposal_id(key, created_at),
        "created_at": created_at,
        "transport": transport,
        "rule": _rule_dict(key),
        "rule_key": list(key),
        "rationale": rationale,
    }
    append_line_durable(
        proposals_path(wiki_root), (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    )
    return record, True


@dataclass(frozen=True)
class ResolveResult:
    applied: bool
    error_code: str | None
    message: str


def resolve_rule_edit_proposal(
    wiki_root: Path,
    proposal_id: str,
    *,
    approve: bool,
    note: str = "",
    now: datetime | None = None,
) -> ResolveResult:
    """Approve (apply the edit, audited) or reject a pending proposal."""
    wiki_root = Path(wiki_root)
    records = read_proposals_ledger(wiki_root)
    proposal = next(
        (r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id),
        None,
    )
    if proposal is None:
        return ResolveResult(
            False, "id_not_found", f"unknown relevance-policy-edit id: {proposal_id}"
        )
    if proposal_id in _resolved_ids(records):
        return ResolveResult(
            False, "already_resolved", f"relevance-policy-edit {proposal_id} already resolved"
        )
    ledger = proposals_path(wiki_root)

    def _record(kind: str) -> None:
        rec = {
            "v": LEDGER_VERSION,
            "kind": kind,
            "id": proposal_id,
            "created_at": now_iso(now),
            "answered_at": now_iso(now),
            "note": note,
        }
        append_line_durable(ledger, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))

    if not approve:
        _record(REJECT_KIND)
        return ResolveResult(True, None, "relevance-policy edit rejected; the policy is unchanged")

    rule = proposal.get("rule") or {}
    already_applied = any(a.get("proposal_id") == proposal_id for a in read_audit(wiki_root))
    if not already_applied:  # crash-between-writes guard: edit first, ledger second
        try:
            apply_rule_edit(
                wiki_root,
                str(rule.get("verdict") or ""),
                rule.get("type"),
                rule.get("tag"),
                rule.get("subject"),
                transport=str(proposal.get("transport") or "http"),
                proposal_id=proposal_id,
                now=now,
            )
        except RelevancePolicyError as exc:
            return ResolveResult(False, "invalid_rule", str(exc))
    _record(APPROVE_KIND)
    return ResolveResult(True, None, "relevance-policy edit approved and applied")


__all__ = [
    "APPROVE_KIND",
    "AUDIT_FILENAME",
    "DECISION_TYPE",
    "EditResult",
    "PROPOSALS_FILENAME",
    "PROPOSAL_KIND",
    "REJECT_KIND",
    "ResolveResult",
    "apply_rule_edit",
    "audit_path",
    "file_rule_edit_proposal",
    "list_pending_proposals",
    "normalize_rule",
    "proposals_path",
    "read_audit",
    "read_proposals_ledger",
    "resolve_rule_edit_proposal",
    "validate_rule",
]
