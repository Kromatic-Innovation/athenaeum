# SPDX-License-Identifier: Apache-2.0
"""Resolved-contradiction fingerprint cache (issue #198).

The contradiction detector compares passage PAIRS per page, so an
already-adjudicated claim re-escalates as a brand-new pending question on
every new page that carries it. This module gives a settled claim-pair a
stable, page-independent fingerprint and a small append-only cache so the
escalation pass can suppress conflicts that a human (or the auto-apply lane)
already resolved.

Two pieces:

- :func:`claim_pair_fingerprint` — an ORDER-INDEPENDENT, normalization-stable
  hash over the two conflicting claim texts plus the conflict type. It reuses
  the ``sha1`` notion from :func:`athenaeum.answers._make_id` and the
  issue-#157 source-memory-pair grouping (sort-then-hash) rather than
  inventing an unrelated id scheme. A *cosmetic* edit (whitespace, case) does
  NOT change the fingerprint; a *material* edit to either claim DOES — which
  is exactly the desired re-escalation trigger.

- The JSONL cache helpers (:func:`record_resolution`, :func:`load_resolved`,
  :func:`is_resolved`) over ``raw/_resolved_contradictions.jsonl``. One JSON
  object per line, appended on every resolution (human OR auto). The
  ``resolved_by`` field ("human" | "auto") is load-bearing for sibling #199
  (only human verdicts auto-apply there) — record it accurately.

The module deliberately has NO heavy imports so both the escalation path
(:mod:`athenaeum.tiers`) and the resolution path (:mod:`athenaeum.answers`,
auto-apply in :mod:`athenaeum.tiers`) can import it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("athenaeum")

# Cache file lives under the knowledge root's ``raw/`` tree, beside the
# answers intake. It is append-only JSONL.
RESOLVED_CONTRADICTIONS_RELPATH = ("raw", "_resolved_contradictions.jsonl")

_WS_RE = re.compile(r"\s+")


def _normalize_claim(text: str) -> str:
    """Normalize a claim/passage for stable, cosmetic-churn-proof hashing.

    Trims, collapses internal whitespace runs to a single space, and
    casefolds. A cosmetic change (extra spaces, capitalization) maps to the
    same normalized string; a material wording change does not. Mirrors the
    sort-then-hash normalization the #157 dedup key applies to passages.
    """
    return _WS_RE.sub(" ", (text or "").strip()).casefold()


def normalize_side(text: str) -> str:
    """Public alias for the per-side claim normalization (issue #199).

    ``claim_pair_fingerprint`` applies :func:`_normalize_claim` to each side
    before sorting+hashing. The #199 orientation-reconciliation path needs the
    SAME normalization to compare a new conflict's per-side text against the
    stored ``side_a_norm`` / ``side_b_norm`` anchors. Exposing one helper keeps
    record-time and match-time normalization identical by construction.
    """
    return _normalize_claim(text)


def claim_pair_fingerprint(text_a: str, text_b: str, conflict_type: str | None) -> str:
    """Return a stable, order-independent fingerprint for a claim pair.

    The fingerprint is a 16-hex-char SHA-1 prefix over the two normalized
    claim texts (SORTED so ``(X, Y)`` and ``(Y, X)`` hash identically) joined
    with the normalized ``conflict_type``. Guarantees:

    - ORDER-INDEPENDENT: the same pair surfaced on different pages, in either
      a/b order, yields one fingerprint.
    - NORMALIZATION-STABLE: whitespace/case churn does not change it.
    - MATERIAL-CHANGE-SENSITIVE: editing the substance of either claim
      changes the normalized text, hence the fingerprint — so the edited
      claim is NOT found in the cache and re-escalates normally.

    ``conflict_type`` is part of the input so the same two texts flagged as
    ``factual`` vs ``prescriptive`` are treated as distinct adjudications.
    """
    norm = sorted((_normalize_claim(text_a), _normalize_claim(text_b)))
    ctype = _normalize_claim(conflict_type or "")
    payload = f"{norm[0]}\n##\n{norm[1]}\n##type##\n{ctype}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def _cache_path(knowledge_root: Path) -> Path:
    return knowledge_root.joinpath(*RESOLVED_CONTRADICTIONS_RELPATH)


def knowledge_root_from_pending(pending_path: Path) -> Path:
    """Derive the knowledge root from a ``wiki/_pending_questions.md`` path.

    Production layout is ``<root>/wiki/_pending_questions.md`` (every caller
    passes ``wiki_root / "_pending_questions.md"``), so the knowledge root is
    the grandparent — that's where the ``raw/`` cache tree lives.

    Fallback: when the immediate parent is NOT named ``wiki`` (legacy/test
    callers that drop the pending file at an arbitrary path), use the parent
    directory itself as the root. This keeps the cache co-located with the
    pending file and, crucially, isolated per directory — otherwise sibling
    pytest ``tmp_path`` directories would share a grandparent and cross-
    contaminate the cache.
    """
    if pending_path.parent.name == "wiki":
        return pending_path.parent.parent
    return pending_path.parent


def record_resolution(
    knowledge_root: Path,
    *,
    fingerprint: str,
    verdict: str,
    resolved_by: str,
    source_verdict_id: str | None = None,
    resolved_at: str | None = None,
    side_a_norm: str | None = None,
    side_b_norm: str | None = None,
) -> None:
    """Append one resolved-contradiction record to the JSONL cache.

    Best-effort: a write failure is logged and swallowed — recording the
    fingerprint must never block the human-answer or auto-apply path. The
    ``resolved_by`` value MUST be ``"human"`` or ``"auto"`` (load-bearing for
    sibling #199).

    ``side_a_norm`` / ``side_b_norm`` (issue #199) persist the per-side
    NORMALIZED claim text in the verdict's ORIGINAL a/b orientation. The
    fingerprint is order-independent, but enacting verdicts
    (``correct_a``/``correct_b``/``keep_a``/``keep_b``/``forget_a``/``forget_b``)
    are orientation-DEPENDENT, so the auto-apply lane needs these anchors to
    decide whether a new conflict's a/b order matches the stored verdict's or
    is reversed (and the action must be flipped). Callers SHOULD pass the
    output of :func:`normalize_side` so record-time and match-time
    normalization are identical. Omitted -> stored as ``None`` (orientation
    unknown; the consumer falls back to safe escalation).
    """
    if not fingerprint:
        return
    record = {
        "fingerprint": fingerprint,
        "verdict": verdict,
        "action": verdict,
        "resolved_by": resolved_by,
        "source_verdict_id": source_verdict_id,
        "resolved_at": resolved_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "side_a_norm": side_a_norm,
        "side_b_norm": side_b_norm,
    }
    try:
        path = _cache_path(knowledge_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.warning(
            "fingerprint: failed to record resolved contradiction %s (%s)",
            fingerprint,
            exc,
        )


def load_resolved(knowledge_root: Path) -> set[str]:
    """Return the set of resolved fingerprints from the JSONL cache.

    Missing file → empty set (no suppression). Malformed lines are skipped
    so a single corrupt row cannot disable the whole cache.
    """
    path = _cache_path(knowledge_root)
    resolved: set[str] = set()
    if not path.exists():
        return resolved
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.warning("fingerprint: failed to read resolved cache (%s)", exc)
        return resolved
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        fp = obj.get("fingerprint")
        if isinstance(fp, str) and fp:
            resolved.add(fp)
    return resolved


def load_resolved_records(knowledge_root: Path) -> dict[str, dict]:
    """Return a ``fingerprint -> record`` map collapsed by precedence (#199).

    Unlike :func:`load_resolved` (which only needs the set of resolved
    fingerprints for #198 suppression), the auto-apply lane (#199) needs the
    full record per fingerprint — ``resolved_by`` (only ``"human"`` verdicts
    auto-apply), ``action`` (the verdict to enact; authoritative over the
    duplicate ``verdict`` key), and ``source_verdict_id`` (for the audit log).

    Precedence when a fingerprint appears multiple times in the append-only
    cache: a HUMAN record always wins over an AUTO record (a human
    ratification supersedes a prior auto-resolution). Among records of the
    same ``resolved_by`` class, the LAST one wins (most-recent append). This
    is the operative "human verdict wins" rule — no pre-existing page-level
    do-not-edit/locked flag exists in the codebase, so the ordering guardrail
    reduces to human authority.

    Missing file -> empty map. Malformed lines are skipped.
    """
    path = _cache_path(knowledge_root)
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.warning("fingerprint: failed to read resolved cache (%s)", exc)
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        fp = obj.get("fingerprint")
        if not (isinstance(fp, str) and fp):
            continue
        prior = records.get(fp)
        # Human supersedes auto; otherwise last-write-wins within a class.
        if (
            prior is not None
            and prior.get("resolved_by") == "human"
            and obj.get("resolved_by") != "human"
        ):
            continue
        records[fp] = obj
    return records


def is_resolved(knowledge_root: Path, fingerprint: str) -> bool:
    """True when ``fingerprint`` is present in the resolved cache."""
    if not fingerprint:
        return False
    return fingerprint in load_resolved(knowledge_root)


def extract_passages(description: str) -> list[str]:
    """Return the ``Passage N:`` blobs from an escalation description.

    Mirrors the passage extraction in
    :func:`athenaeum.tiers._pair_key_from_description` so the fingerprint is
    computed over the SAME two texts the #157 dedup key groups on.
    """
    passages: list[str] = []
    for raw in (description or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("Passage ") and ":" in stripped:
            _, _, body = stripped.partition(":")
            body = body.strip()
            if body:
                passages.append(body)
    return passages


def fingerprint_from_description(
    description: str, conflict_type: str | None
) -> str | None:
    """Compute the claim-pair fingerprint from an escalation description.

    Returns ``None`` when fewer than two passages can be recovered (no stable
    pair to fingerprint — caller should NOT suppress).
    """
    passages = extract_passages(description)
    if len(passages) < 2:
        return None
    return claim_pair_fingerprint(passages[0], passages[1], conflict_type)


def fingerprints_from_descriptions(
    descriptions: Iterable[tuple[str, str | None]],
) -> list[str]:
    """Batch helper: fingerprints for ``(description, conflict_type)`` pairs.

    Skips entries that yield no fingerprint. Used by tests and callers that
    want every recoverable fingerprint for a set of blocks.
    """
    out: list[str] = []
    for desc, ctype in descriptions:
        fp = fingerprint_from_description(desc, ctype)
        if fp:
            out.append(fp)
    return out
