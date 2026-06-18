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
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

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
    member_key: str | None = None,
    pair_text: str | None = None,
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

    ``member_key`` (issue #211) is the sorted-tuple member-pair joined with
    ``"||"`` (see :func:`_member_key_str`).  When present, the decision-log
    matcher can suppress re-detected contradictions that share the same
    member pair even when the passage text drifted (different fingerprint).

    ``pair_text`` (issue #211) is the canonical normalized pair-text string
    (``"{norm_side0}\\n##\\n{norm_side1}"`` from :func:`_pair_text_from_passages`).
    Persisted so the embedding similarity path can embed it at match time
    without re-running the detector.  Old records lacking this field are
    still matched by exact fingerprint.
    """
    if not fingerprint:
        return
    record = {
        "fingerprint": fingerprint,
        # ``action`` is the single authoritative key (issue #207). Consumers
        # (load_resolved_records / the #199 auto-apply path) read ``action``;
        # the ``verdict`` parameter is the value, not a second stored key.
        "action": verdict,
        "resolved_by": resolved_by,
        "source_verdict_id": source_verdict_id,
        "resolved_at": resolved_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "side_a_norm": side_a_norm,
        "side_b_norm": side_b_norm,
        "member_key": member_key,
        "pair_text": pair_text,
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
    auto-apply), ``action`` (the authoritative verdict to enact; a legacy or
    external ``verdict``-only record is tolerated as a read-time fallback),
    and ``source_verdict_id`` (for the audit log).

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


# ---------------------------------------------------------------------------
# Resolved-similarity threshold (issue #211)
# ---------------------------------------------------------------------------

_ENV_RESOLVED_SIMILARITY_THRESHOLD = "ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD"
_DEFAULT_RESOLVED_SIMILARITY_THRESHOLD = 0.83


def resolve_resolved_similarity_threshold(
    config: dict[str, Any] | None = None,
) -> float:
    """Resolve the cosine similarity threshold for decision-log matching.

    Precedence: env var ``ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD`` >
    ``contradiction.resolved_similarity_threshold`` in config > default
    (0.83).  Mirrors :func:`athenaeum.cross_scope.resolve_similarity_threshold`.
    """
    env_val = os.environ.get(_ENV_RESOLVED_SIMILARITY_THRESHOLD)
    if env_val:
        try:
            return float(env_val.strip())
        except ValueError:
            pass
    if config is not None:
        contradiction_cfg = config.get("contradiction")
        if not isinstance(contradiction_cfg, dict):
            contradiction_cfg = {}
        raw = contradiction_cfg.get("resolved_similarity_threshold")
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            pass
    return _DEFAULT_RESOLVED_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# Read-time decay of stale auto not_a_conflict suppressions (issue #251)
# ---------------------------------------------------------------------------

_ENV_NOT_A_CONFLICT_TTL_DAYS = "ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS"
# Code default 0 == disabled (no decay; current behavior). Per the #231
# rule this is NOT seeded into config._DEFAULTS — it resolves through the
# env > yaml > code-default chain below.
_DEFAULT_NOT_A_CONFLICT_TTL_DAYS = 0

# The suppress verdict value. Re-declared locally (not imported from
# :mod:`athenaeum.resolutions`) so this module keeps its no-heavy-imports
# contract; the literal is locked in resolutions.SUPPRESS_ACTION and must
# stay in sync.
_SUPPRESS_VERDICT = "not_a_conflict"

# Resolved-at timestamp format stamped by :func:`record_resolution`.
_RESOLVED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def resolve_not_a_conflict_ttl_days(config: dict[str, Any] | None = None) -> int:
    """Resolve the auto-suppression TTL (days) from env > yaml > default.

    Issue #251. Precedence mirrors
    :func:`athenaeum.resolutions.resolve_max_per_run`: the env var
    ``ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS`` wins over
    ``contradiction.not_a_conflict_ttl_days`` in the yaml, which wins over
    the code default ``0`` (disabled). Per the #231 rule the key is NOT
    seeded in ``config._DEFAULTS``. Negative, non-numeric, or boolean
    values fall back to the default. ``0`` disables decay entirely (current
    behavior — an auto suppression never expires).
    """
    env = os.environ.get(_ENV_NOT_A_CONFLICT_TTL_DAYS)
    if env is not None:
        try:
            value = int(env)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    if isinstance(config, dict):
        cfg = config.get("contradiction")
        if isinstance(cfg, dict):
            raw = cfg.get("not_a_conflict_ttl_days")
            # bool is an int subclass — ``not_a_conflict_ttl_days: yes`` in
            # yaml must not silently become a ttl of 1 (mirrors
            # resolve_max_per_run).
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return raw
    return _DEFAULT_NOT_A_CONFLICT_TTL_DAYS


def is_stale_auto_suppression(
    record: dict[str, Any],
    ttl_days: int,
    now: datetime,
) -> bool:
    """Return True when an AUTO ``not_a_conflict`` record has decayed.

    Issue #251. A stale record is treated as ABSENT when building the
    confirmation-pass skip set, so the pair re-enters the Opus confirmation
    path — but the row itself is NEVER mutated (append-only contract).

    Returns True only when ALL hold:

    * ``ttl_days > 0`` (0 == disabled; current no-decay behavior).
    * ``resolved_by == "auto"`` — human verdicts NEVER decay.
    * ``(action or verdict) == "not_a_conflict"`` — enacting auto verdicts
      (``keep_*`` / ``correct_*`` / ``forget_*`` / ``deprecate_both``) NEVER
      decay; only suppressions do.
    * ``resolved_at`` parses as ``%Y-%m-%dT%H:%M:%SZ`` AND is older than
      ``ttl_days`` before ``now``.

    Fail-safe: a missing or unparseable ``resolved_at`` returns False (the
    record keeps suppressing) so legacy/external undated rows are never
    expired.

    ``now`` is INJECTED — the helper reads no wall-clock — so callers can
    freeze a single run-start timestamp and tests stay deterministic.
    """
    if ttl_days <= 0:
        return False
    if record.get("resolved_by") != "auto":
        return False
    if (record.get("action") or record.get("verdict")) != _SUPPRESS_VERDICT:
        return False
    raw = record.get("resolved_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        resolved_at = datetime.strptime(raw, _RESOLVED_AT_FORMAT)
    except ValueError:
        return False
    # record_resolution stamps UTC without an offset; compare in UTC. If the
    # caller's ``now`` is tz-aware, normalize the parsed value to match.
    if now.tzinfo is not None:
        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    age = now - resolved_at
    return age > timedelta(days=ttl_days)


# ---------------------------------------------------------------------------
# Semantic matching (issue #211)
# ---------------------------------------------------------------------------


def _member_key_str(member_paths: list[str] | tuple[str, ...]) -> str | None:
    """Return a stable string key from a list/tuple of member paths.

    Sorts and deduplicates the paths, joins with ``"||"`` so the result is
    unambiguous and safe to compare by equality.  Returns ``None`` for
    empty / single-element inputs.
    """
    deduped = sorted(set(str(p).strip() for p in member_paths if str(p).strip()))
    if len(deduped) < 2:
        return None
    return "||".join(deduped)


def _pair_text_from_passages(passage_a: str, passage_b: str) -> str:
    """Return the canonical pair-text string for two normalized passages.

    Uses the SAME sorted+normalized ordering that ``claim_pair_fingerprint``
    applies, so the pair_text is recoverable and embeddable at match time
    independent of a/b order.
    """
    norm = sorted((_normalize_claim(passage_a), _normalize_claim(passage_b)))
    return f"{norm[0]}\n##\n{norm[1]}"


def find_resolved_record(
    knowledge_root: Path,
    *,
    fingerprint: str | None,
    member_key: str | None,
    pair_text: str | None,
    threshold: float,
    embedder: Callable[[list[str]], list[list[float]] | None] | None = None,
) -> dict[str, Any] | None:
    """Find the best matching resolved record for a re-detected contradiction.

    Tries three strategies in order; returns the first match:

    1. **Exact fingerprint** — fast path, backward-compatible.
    2. **Member-pair key** — deterministic. Matches when both the stored
       record and the new item carry a ``member_key`` referencing the same
       sorted set of member file paths.  This collapses the live drift cases
       (same pair, drifting passages) even when chromadb is absent.
    3. **Embedding cosine similarity** — general fuzzy fix.  Uses ``embedder``
       (defaults to :func:`athenaeum.search.embed_texts`) to embed the new
       item's ``pair_text`` and every stored record's ``pair_text``.  Picks the
       record with the highest cosine above ``threshold``.  Skipped silently
       when ``embedder`` returns ``None`` (chromadb absent).

    The records are loaded once via :func:`load_resolved_records` (human
    supersedes auto, last-write-wins within a class).  Old records lacking
    ``member_key`` / ``pair_text`` fall through to the embedding strategy (or
    are skipped there too when embedding is unavailable) — they are still
    matched by the exact fingerprint path.

    ``embedder`` is an injectable callable ``list[str] -> list[list[float]] | None``
    so tests can supply a deterministic stub without real chromadb.
    """
    records = load_resolved_records(knowledge_root)
    if not records:
        return None

    # Strategy 1: exact fingerprint
    if fingerprint:
        rec = records.get(fingerprint)
        if rec is not None:
            return rec

    # Strategy 2: member-pair key
    if member_key:
        for rec in records.values():
            stored_mk = rec.get("member_key")
            if isinstance(stored_mk, str) and stored_mk == member_key:
                return rec

    # Strategy 3: embedding cosine similarity
    if not pair_text:
        return None

    # Resolve embedder default lazily to avoid import at module load time.
    if embedder is None:
        try:
            from athenaeum.search import embed_texts  # noqa: PLC0415

            embedder = embed_texts
        except ImportError:
            return None

    # Collect stored records that have pair_text for embedding comparison.
    candidates: list[tuple[float, dict[str, Any]]] = []
    stored_pair_texts = [rec.get("pair_text") for rec in records.values()]
    # We need to embed the new pair_text plus all stored ones in one batch.
    texts_to_embed = [pair_text] + [
        pt for pt in stored_pair_texts if isinstance(pt, str) and pt
    ]
    if len(texts_to_embed) < 2:
        return None  # no stored pair_texts to compare against

    vecs = embedder(texts_to_embed)
    if vecs is None or len(vecs) < 2:
        return None

    from athenaeum.cross_scope import _cosine  # noqa: PLC0415

    new_vec = vecs[0]
    stored_recs_with_pt = [
        rec
        for rec in records.values()
        if isinstance(rec.get("pair_text"), str) and rec["pair_text"]
    ]
    for idx, rec in enumerate(stored_recs_with_pt):
        stored_vec = vecs[idx + 1]
        sim = _cosine(new_vec, stored_vec)
        if sim >= threshold:
            candidates.append((sim, rec))

    if not candidates:
        return None

    # Return the record with the highest similarity.
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
