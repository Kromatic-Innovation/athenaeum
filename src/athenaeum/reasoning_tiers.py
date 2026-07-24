# SPDX-License-Identifier: Apache-2.0
"""Tiered reasoning-pass pipeline for merge proposals (issue #423).

NOT to be confused with :mod:`athenaeum.tiers` — that module is the T0-T4
*entity-compilation* pipeline (raw intake -> wiki entity pages). This module
is a DIFFERENT pipeline: it sits between the mechanical merge-proposal
machinery (:mod:`athenaeum.merge`, :mod:`athenaeum.wiki_dedupe`, both of
which call :func:`athenaeum.pending_merges.write_pending_merge`) and the
human decision queue (:func:`athenaeum.decisions.list_pending_decisions`),
adding a cheap-to-expensive cascade of LLM "reasoning" tiers that can reject
an obviously-bad proposal before it ever reaches a human, or pass it further
up the cascade. To avoid any confusion with ``tiers.py``'s ``Tier1``/``Tier2``/
etc. naming, every type here is prefixed ``Reasoning`` (:class:`ReasoningTier`,
:class:`ReasoningTierDecision`, ...).

Governing rule (settled product decision, do not re-litigate): **write
authority increases with tier; cheap tiers only reject and route, never
approve.** Concretely:

- **T1** (this issue): haiku/sonnet-class model, bounded input (titles +
  frontmatter + first ~100 words per source — NEVER full bodies). Can only
  REJECT (with a logged reason) or PASS UP. Approval is structurally
  unrepresentable in its output type — see :class:`ReasoningTierVerdict`.
- **T2** (issue #432, not built here): a more capable tier that may also
  only reject or pass up, per the same governing rule — it is NOT gated
  with approval authority either. The skeleton here is deliberately generic
  ("a tier is anything that rejects or passes up") so #432 can slot a T2
  handler into :data:`DEFAULT_TIER_CHAIN` (or an explicit chain the caller
  builds) without reworking anything in this module.
- **Human** — the only actor that can ever approve. Until #432 lands, a
  T1 pass-up flows straight to the existing human queue
  (:func:`athenaeum.decisions.list_pending_decisions` /
  :func:`athenaeum.pending_merges.list_pending_merges`) UNCHANGED — see
  :func:`run_reasoning_pipeline`.

Every tier decision — reject or pass-up, at any tier — is recorded as a
machine-readable, queryable event: ``(tier, decision, reason, model,
proposal_id)`` plus a timestamp. The log format mirrors
:mod:`athenaeum.provenance`'s merge-provenance ledger (append-only JSONL,
``O_APPEND`` + fsync, tolerant reader that skips a torn trailing line) —
same durability discipline, same "queryable append-only sidecar" shape,
just a different filename/record schema.

Out of scope here (see the issue body for the re-scope rationale):

- T2 itself (#432).
- The calibration sampler that watches T1/T2 accuracy over time (#438).
- Wiring this pipeline into the live ``merge.py`` / ``wiki_dedupe.py`` call
  sites that currently write straight to ``_pending_merges.md`` — that is a
  follow-up once #432 exists (running only a T1-reject-or-pass-up tier with
  no T2 is a real, useful configuration, but the issue scopes THIS change to
  the pipeline + T1 tier building blocks, not the call-site rewiring).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from athenaeum._retry import with_retry
from athenaeum.authority import (
    AuthorityManifest,
    find_duplicate_source,
    load_authority_manifest,
)
from athenaeum.config import resolve_model
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import PendingMerge

log = logging.getLogger("athenaeum")

# ---------------------------------------------------------------------------
# Model selection (issue #423) — resolves via the existing provider-aware
# config chain (env > yaml `models.<knob>` > code default, issue #232),
# exactly like every other tier/classifier in the codebase. NEVER hardcode
# a model id at a call site — see athenaeum.config.resolve_model.
# ---------------------------------------------------------------------------

#: T1 is the cheap reject-and-route tier — haiku-class by default. Overridable
#: via ``ATHENAEUM_REASONING_T1_MODEL`` env or ``models.reasoning_t1`` yaml.
DEFAULT_T1_MODEL = "claude-haiku-4-5-20251001"


def get_t1_model(config: dict[str, Any] | None = None) -> str:
    """Resolve the T1 tier's model id (env > yaml > default, issue #232)."""
    return resolve_model(
        "reasoning_t1", "ATHENAEUM_REASONING_T1_MODEL", DEFAULT_T1_MODEL, config
    )


# ---------------------------------------------------------------------------
# Bounded source view — titles + frontmatter + first ~100 words. NEVER full
# bodies. This is a hard requirement (tested): the T1 prompt payload must
# never carry a source's complete body text.
# ---------------------------------------------------------------------------

#: Word cap per source body excerpt. "~100 words" per the issue; capped
#: (not padded) — a shorter body is used in full.
BODY_EXCERPT_WORD_LIMIT = 100


@dataclass(frozen=True)
class BoundedSourceView:
    """The ONLY view of a proposal source a reasoning tier may consume.

    Deliberately excludes the full body — ``body_excerpt`` is capped at
    :data:`BODY_EXCERPT_WORD_LIMIT` words. Any caller that wants more must
    go outside this module (and outside T1's authority) to get it.
    """

    path: str
    title: str
    frontmatter: dict[str, Any]
    body_excerpt: str


def _first_n_words(text: str, n: int) -> str:
    words = text.split()
    if len(words) <= n:
        return " ".join(words)
    return " ".join(words[:n])


def build_bounded_source_view(path: str) -> BoundedSourceView:
    """Read *path* and reduce it to title + frontmatter + first ~100 words.

    Missing/unreadable files degrade to an empty view (title falls back to
    the filename stem) rather than raising — a T1 pass over a proposal
    whose source vanished mid-run should still be able to reject/pass-up,
    not crash the whole batch.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return BoundedSourceView(
            path=path, title=p.stem, frontmatter={}, body_excerpt=""
        )
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    title = ""
    raw_name = meta.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        title = raw_name.strip()
    if not title:
        title = p.stem
    excerpt = _first_n_words(body.strip(), BODY_EXCERPT_WORD_LIMIT)
    return BoundedSourceView(
        path=path, title=title, frontmatter=meta, body_excerpt=excerpt
    )


@dataclass(frozen=True)
class ReasoningProposal:
    """Minimal shape a reasoning tier needs from a merge proposal.

    Deliberately narrower than :class:`athenaeum.pending_merges.PendingMerge`
    — a tier consumes only what it is allowed to see. ``proposal_id`` mirrors
    :attr:`PendingMerge.id`. ``sources`` are raw source PATHS (bounded views
    are built lazily per-tier via :func:`build_bounded_source_view`) so a
    caller can construct this straight from a freshly-detected cluster
    BEFORE a :class:`~athenaeum.pending_merges.PendingMerge` even exists.
    """

    proposal_id: str
    merge_target_name: str
    sources: tuple[str, ...]

    @classmethod
    def from_pending_merge(cls, pm: "PendingMerge") -> "ReasoningProposal":
        """Project a :class:`~athenaeum.pending_merges.PendingMerge` down to
        the narrow shape a reasoning tier is allowed to see.

        This is the glue a caller uses to run the mechanical layer's actual
        proposals through :func:`run_reasoning_pipeline`: parse
        ``_pending_merges.md`` via
        :func:`athenaeum.pending_merges.parse_pending_merges`, convert each
        unresolved :class:`PendingMerge` with this constructor, run the
        pipeline, and — on a pass-up — leave the original block exactly as
        :func:`athenaeum.decisions.list_pending_decisions` already reads it
        (this projection never mutates or re-writes the source block).
        """
        return cls(
            proposal_id=pm.id,
            merge_target_name=pm.merge_target_name,
            sources=tuple(pm.sources),
        )


def bounded_views_for(proposal: ReasoningProposal) -> tuple[BoundedSourceView, ...]:
    """Build the bounded (title + frontmatter + ~100-word excerpt) source views."""
    return tuple(build_bounded_source_view(s) for s in proposal.sources)


# ---------------------------------------------------------------------------
# T1's output type — approval is UNREPRESENTABLE, not merely discouraged.
# ---------------------------------------------------------------------------

#: The only two verdicts a reasoning tier may ever return. There is no
#: "approve" member on this enum-like Literal — a tier's write authority is
#: capped at reject/pass-up by the TYPE, not by convention or a runtime
#: check. Adding an "approve" value would require editing this Literal
#: (and every exhaustive ``match``/``if`` over it) in a way that is easy to
#: grep for and impossible to do by accident.
ReasoningTierVerdict = Literal["reject", "pass_up"]

REASONING_TIER_VERDICTS: frozenset[str] = frozenset({"reject", "pass_up"})

#: T1's reject bins (issue #423). A rejection's ``reason_code`` is one of
#: these three, or ``"other"`` for a bin-less structured rejection (kept
#: open so a future tier can add reasons without a schema break).
REJECT_REASON_DIFFERENT_ENTITIES = "different_entities"
REJECT_REASON_CROSS_MEMORY_CLASS = "cross_memory_class"
REJECT_REASON_LIVE_SOURCE_DUPLICATE = "live_source_duplicate"
REJECT_REASON_OTHER = "other"

REJECT_REASON_CODES: frozenset[str] = frozenset(
    {
        REJECT_REASON_DIFFERENT_ENTITIES,
        REJECT_REASON_CROSS_MEMORY_CLASS,
        REJECT_REASON_LIVE_SOURCE_DUPLICATE,
        REJECT_REASON_OTHER,
    }
)


@dataclass(frozen=True)
class ReasoningTierDecision:
    """One tier's decision on one proposal.

    ``verdict`` is structurally limited to :data:`ReasoningTierVerdict` —
    there is no code path that can construct a decision meaning "approved"
    (no such field value exists to assign). ``reason`` is always populated
    (never blank) — a reject or a pass-up must always carry a reason a
    human or the next tier can read.
    """

    tier: str
    verdict: ReasoningTierVerdict
    reason: str
    model: str | None
    proposal_id: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in REASONING_TIER_VERDICTS:
            raise ValueError(
                f"invalid ReasoningTierDecision.verdict: {self.verdict!r} "
                f"(must be one of {sorted(REASONING_TIER_VERDICTS)!r})"
            )
        if not self.reason or not self.reason.strip():
            raise ValueError("ReasoningTierDecision.reason must be non-empty")


# ---------------------------------------------------------------------------
# Decision log — append-only JSONL, queryable. Mirrors
# athenaeum.provenance's merge-provenance ledger discipline exactly (same
# O_APPEND + fsync durability, same tolerant-reader-skips-torn-line
# contract) but is a SEPARATE sidecar/schema: a tier decision is not a
# completed-merge record.
# ---------------------------------------------------------------------------

#: Schema version stamped on every record so a future reader can migrate.
REASONING_TIER_LOG_VERSION = 1

#: Sidecar filename, alongside ``_pending_merges.md`` under ``wiki/``.
REASONING_TIER_LOG_FILENAME = "_reasoning_tier_decisions.jsonl"


def default_reasoning_tier_log_path(wiki_root: Path) -> Path:
    """Default decision-log path: ``<wiki_root>/_reasoning_tier_decisions.jsonl``."""
    return Path(wiki_root) / REASONING_TIER_LOG_FILENAME


def _build_log_record(
    decision: ReasoningTierDecision, *, ts: datetime | None = None
) -> dict[str, Any]:
    stamp = (ts if ts is not None else datetime.now(tz=timezone.utc)).astimezone(
        timezone.utc
    )
    return {
        "v": REASONING_TIER_LOG_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "tier": decision.tier,
        "decision": decision.verdict,
        "reason": decision.reason,
        "reason_code": decision.reason_code,
        "model": decision.model,
        "proposal_id": decision.proposal_id,
    }


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Same discipline as :func:`athenaeum.provenance._append_jsonl_line` /
    :mod:`athenaeum.spend`: a single small ``O_APPEND`` write is atomic on
    local filesystems, so a crash can at worst leave a torn TRAILING line
    (which the reader skips), never corrupt an already-written record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def record_reasoning_tier_decision(
    wiki_root: Path,
    decision: ReasoningTierDecision,
    *,
    log_path: Path | None = None,
    ts: datetime | None = None,
) -> bool:
    """Append one tier-decision record to the decision log. Best-effort.

    Never raises — a logging failure must not block the pipeline whose
    decision has already been made by the time this runs; failures are
    logged and swallowed, mirroring
    :func:`athenaeum.provenance.record_merge_provenance`'s discipline.
    Returns ``True`` when a record was written.
    """
    try:
        record = _build_log_record(decision, ts=ts)
        target = (
            log_path if log_path is not None else default_reasoning_tier_log_path(wiki_root)
        )
        _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — ledger write must never break the pipeline
        log.debug(
            "reasoning tier decision log write skipped (%s): %s",
            type(exc).__name__,
            exc,
        )
        return False


def read_reasoning_tier_decisions(
    wiki_root: Path,
    *,
    log_path: Path | None = None,
    proposal_id: str | None = None,
    tier: str | None = None,
) -> list[dict[str, Any]]:
    """Read tier-decision records, tolerating a torn/partial trailing line.

    Optional ``proposal_id`` / ``tier`` filter the returned records (exact
    match). Returns ``[]`` when the log does not exist. Malformed lines (a
    crash mid-write, or hand-editing) are skipped, not fatal — mirrors
    :func:`athenaeum.provenance.read_merge_provenance`.
    """
    target = (
        log_path if log_path is not None else default_reasoning_tier_log_path(wiki_root)
    )
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
        if proposal_id is not None and record.get("proposal_id") != proposal_id:
            continue
        if tier is not None and record.get("tier") != tier:
            continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# T1 tier — reject-with-logged-reason or pass-up. NEVER approve.
# ---------------------------------------------------------------------------

T1_TIER_NAME = "T1"

T1_SYSTEM_PROMPT = """You are a cheap, fast pre-screener for a memory-merge proposal queue.

You will be shown a SHORT, BOUNDED summary of each candidate source (its
title, its frontmatter metadata, and the first ~100 words of its body only
— never the full text). Your job is to reject proposals that are obviously
wrong BEFORE they reach a human reviewer, or pass them up the chain when you
cannot confidently reject them.

You do NOT have the authority to approve a merge. You may only:
- "reject" the proposal, with a short, specific reason, OR
- "pass_up" the proposal (let the next tier or a human decide).

Reject when you are confident the sources:
- describe DIFFERENT entities/topics (not the same thing being merged), or
- carry incompatible `memory_class` values (cross-memory_class pairing), or
- one of the sources duplicates an already-registered live/authoritative
  source (a duplicate detector may flag this for you directly).

If you are not confident it is safe to reject, pass_up. Never invent an
"approve" — that option does not exist for you.

Respond with ONLY a JSON object of the shape:
{"verdict": "reject" | "pass_up", "reason": "<one sentence>"}"""


def _render_source_summary(view: BoundedSourceView) -> str:
    fm_lines = "\n".join(f"  {k}: {v!r}" for k, v in sorted(view.frontmatter.items()))
    return (
        f"- path: {view.path}\n"
        f"  title: {view.title}\n"
        f"  frontmatter:\n{fm_lines or '  (none)'}\n"
        f"  body_excerpt (first ~{BODY_EXCERPT_WORD_LIMIT} words only): "
        f"{view.body_excerpt!r}"
    )


def build_t1_request_params(
    proposal: ReasoningProposal,
    views: Sequence[BoundedSourceView],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Messages API kwargs for one T1 pre-screen call.

    The payload is built EXCLUSIVELY from bounded ``views`` (title +
    frontmatter + ~100-word excerpt) — never from a source's full body.
    Kept as a separate function (mirrors ``tier2_request_params`` /
    ``tier3_create_params`` in :mod:`athenaeum.tiers`) so a batch-mode
    caller (a future need, not built here) could reuse it identically.
    """
    sources_block = "\n".join(_render_source_summary(v) for v in views)
    user_msg = (
        f"## Candidate merge target\n{proposal.merge_target_name}\n\n"
        f"## Candidate sources ({len(views)})\n{sources_block}\n\n"
        "## Instructions\nDecide reject or pass_up per the system prompt. "
        "Return ONLY the JSON object."
    )
    return {
        "model": get_t1_model(config),
        "max_tokens": 256,
        "system": T1_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }


def _parse_t1_response(text: str) -> tuple[ReasoningTierVerdict, str]:
    """Parse the T1 model's JSON response into (verdict, reason).

    Defensive parsing (mirrors :func:`athenaeum.tiers.parse_tier2_entities`):
    malformed/missing JSON, or a verdict outside the two allowed values,
    degrades to a ``pass_up`` — T1 can only ever reject when it is
    confidently able to say so; anything it cannot parse is NOT treated as
    a rejection (that would be a false-negative failure mode with much
    higher cost than an extra pass-up).
    """
    text = text.strip()
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return "pass_up", f"T1 response unparseable, passing up: {text[:200]!r}"
    if not isinstance(payload, dict):
        return "pass_up", "T1 response was not a JSON object; passing up"
    verdict = payload.get("verdict")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "(no reason given by T1 model)"
    if verdict == "reject":
        return "reject", reason
    # Anything else (including "approve", garbage, or missing) -> pass_up.
    # T1's output type has no "approve" branch, so even if the model text
    # says "approve" it is coerced to a pass-up, never surfaced as approval.
    return "pass_up", reason


def _duplicate_check_reason(
    views: Sequence[BoundedSourceView],
    manifest: AuthorityManifest,
) -> str | None:
    """Return a rejection reason if any source duplicates a live authority source.

    Deterministic LOOKUP via :func:`athenaeum.authority.find_duplicate_source`
    — issue #426's detector — over each source's bounded frontmatter view.
    Never semantic similarity, matching that module's own contract.
    """
    for view in views:
        source = find_duplicate_source(view.frontmatter, manifest)
        if source is not None:
            return (
                f"source {view.path!r} duplicates live authoritative source "
                f"{source.slug!r} (topic match)"
            )
    return None


def _cross_memory_class_reason(views: Sequence[BoundedSourceView]) -> str | None:
    """Return a rejection reason if sources carry incompatible ``memory_class``.

    Two sources with a present, differing, non-empty ``memory_class`` are
    an incompatible pairing (issue #424 taxonomy) — clustering a ``fact``
    with an ``axiom``, for instance, is never a valid merge target. A
    source with an ABSENT ``memory_class`` is not itself disqualifying
    (legacy/untyped memories are tolerated by the taxonomy) — only an
    actual mismatch between two PRESENT values rejects.
    """
    seen: dict[str, str] = {}
    for view in views:
        raw = view.frontmatter.get("memory_class")
        if not isinstance(raw, str) or not raw.strip():
            continue
        mclass = raw.strip()
        for other_path, other_class in seen.items():
            if other_class != mclass:
                return (
                    f"cross-memory_class pairing: {view.path!r} is "
                    f"{mclass!r} but {other_path!r} is {other_class!r}"
                )
        seen[view.path] = mclass
    return None


def run_t1_tier(
    proposal: ReasoningProposal,
    *,
    client: Any | None,
    authority_manifest: AuthorityManifest | None = None,
    config: dict[str, Any] | None = None,
    usage: Any | None = None,
) -> ReasoningTierDecision:
    """Run the T1 (cheap, reject-and-route) tier over one proposal.

    Structurally limited to reject-with-logged-reason or pass-up (see
    :class:`ReasoningTierDecision` / :data:`ReasoningTierVerdict`) — there
    is no return path that produces an approval.

    Cheap deterministic checks run BEFORE any model call (never spend a
    token on a rejection a lookup can already make with certainty):

    1. **Cross-`memory_class` pairing** (#424 taxonomy) —
       :func:`_cross_memory_class_reason`.
    2. **Live-source duplicate** (#426 detector) —
       :func:`_duplicate_check_reason`, only when *authority_manifest* is
       supplied (an absent/empty manifest never rejects — an unconfigured
       knowledge base has no authoritative sources registered, matching
       :func:`athenaeum.authority.load_authority_manifest`'s own
       "missing file -> empty manifest" contract).

    Only when neither deterministic check fires does this fall through to
    the model call for the harder "different entities" judgment (and as a
    general backstop) — *client* ``None`` (no LLM configured) short-circuits
    straight to a ``pass_up`` at that point, mirroring every other
    tier/classifier's ``client is None`` degradation in this codebase.

    The model's payload is built EXCLUSIVELY from
    :func:`bounded_views_for` — titles + frontmatter + first ~100 words per
    source. Full source bodies are never read into the prompt.
    """
    views = bounded_views_for(proposal)

    cross_class_reason = _cross_memory_class_reason(views)
    if cross_class_reason is not None:
        return ReasoningTierDecision(
            tier=T1_TIER_NAME,
            verdict="reject",
            reason=cross_class_reason,
            reason_code=REJECT_REASON_CROSS_MEMORY_CLASS,
            model=None,
            proposal_id=proposal.proposal_id,
        )

    if authority_manifest is not None:
        dup_reason = _duplicate_check_reason(views, authority_manifest)
        if dup_reason is not None:
            return ReasoningTierDecision(
                tier=T1_TIER_NAME,
                verdict="reject",
                reason=dup_reason,
                reason_code=REJECT_REASON_LIVE_SOURCE_DUPLICATE,
                model=None,
                proposal_id=proposal.proposal_id,
            )

    if client is None:
        return ReasoningTierDecision(
            tier=T1_TIER_NAME,
            verdict="pass_up",
            reason="no LLM client configured for T1; passing up",
            model=None,
            proposal_id=proposal.proposal_id,
        )

    params = build_t1_request_params(proposal, views, config=config)
    response = with_retry(
        lambda: client.messages.create(**params),
        description=f"t1_reasoning_tier {proposal.proposal_id}",
    )
    if usage is not None and hasattr(response, "usage"):
        from athenaeum.models import cache_usage_counts

        input_toks, output_toks, cache_creation, cache_read = cache_usage_counts(
            response
        )
        usage.add(
            input_toks, output_toks, cache_creation, cache_read, model=params["model"]
        )

    verdict, reason = _parse_t1_response(response.content[0].text)
    reason_code = (
        REJECT_REASON_DIFFERENT_ENTITIES if verdict == "reject" else None
    )
    return ReasoningTierDecision(
        tier=T1_TIER_NAME,
        verdict=verdict,
        reason=reason,
        reason_code=reason_code,
        model=params["model"],
        proposal_id=proposal.proposal_id,
    )


# ---------------------------------------------------------------------------
# Pipeline skeleton — ordered tier handlers, tolerant of an absent T2.
# ---------------------------------------------------------------------------

#: A tier handler takes a proposal and returns its decision. Any callable
#: matching this shape can be slotted into a chain passed to
#: :func:`run_reasoning_pipeline` — #432's T2 handler needs only to match
#: this signature, no rework of the skeleton required.
TierHandler = Callable[[ReasoningProposal], ReasoningTierDecision]

#: The default tier chain: T1 only, until #432 adds a T2 handler here (or a
#: caller passes an explicit chain). An empty/absent T2 is not a special
#: case the skeleton has to know about — it is simply a chain of length 1.
DEFAULT_TIER_CHAIN: tuple[TierHandler, ...] = ()


@dataclass
class ReasoningPipelineResult:
    """Outcome of running the tier chain over one proposal.

    ``rejected`` is true iff some tier in the chain returned ``"reject"`` —
    in that case ``rejecting_decision`` names which one and why, and the
    proposal must NOT reach the human queue. When no tier rejects,
    ``rejected`` is false and the proposal is a pass-up: with T2 absent
    (the default, until #432 lands) it should be handed to the existing
    human queue unchanged, exactly as if this pipeline did not run at all.
    """

    proposal_id: str
    rejected: bool
    rejecting_decision: ReasoningTierDecision | None
    decisions: tuple[ReasoningTierDecision, ...] = field(default_factory=tuple)

    @property
    def passed_up(self) -> bool:
        """True when the proposal cleared every configured tier (a pass-up)."""
        return not self.rejected


def run_reasoning_pipeline(
    proposal: ReasoningProposal,
    *,
    tier_chain: Sequence[TierHandler] = DEFAULT_TIER_CHAIN,
    wiki_root: Path | None = None,
    log_path: Path | None = None,
) -> ReasoningPipelineResult:
    """Run *proposal* through an ORDERED list of tier handlers.

    Each handler is tried in order; the first ``"reject"`` short-circuits
    the chain (a later, more expensive tier is never invoked once a cheaper
    one has already rejected — that is the entire point of a cost-ordered
    cascade). If every handler in the chain returns ``"pass_up"`` (including
    the trivial empty-chain case — see below), the proposal is a pass-up.

    **Tolerates an absent T2 by construction, not by a special case**: until
    issue #432 adds a T2 handler, callers either pass ``tier_chain=()`` (no
    tiers configured at all) or a chain containing only a T1 handler (e.g.
    ``tier_chain=(functools.partial(run_t1_tier, client=..., ...),)``).
    Either way, when the chain is exhausted without a reject, this function
    returns a pass-up result — there is no "T2 is missing" branch to write
    or forget to write, because the loop just runs however many handlers it
    was given. The CALLER is responsible for then routing a pass-up result
    to :func:`athenaeum.pending_merges.write_pending_merge` /
    :func:`athenaeum.decisions.list_pending_decisions` exactly as it does
    today when no reasoning pipeline exists at all.

    Every decision from every tier that actually ran (reject or pass-up) is
    recorded via :func:`record_reasoning_tier_decision` when *wiki_root* (or
    an explicit *log_path*) is supplied; omitting both skips logging
    entirely (useful for a pure in-memory unit test of the chain logic).
    """
    decisions: list[ReasoningTierDecision] = []
    for handler in tier_chain:
        decision = handler(proposal)
        decisions.append(decision)
        if wiki_root is not None or log_path is not None:
            # ``wiki_root`` is a required positional param on
            # record_reasoning_tier_decision, but when an explicit
            # ``log_path`` is supplied it takes precedence there and this
            # placeholder is never actually consulted.
            record_reasoning_tier_decision(
                wiki_root if wiki_root is not None else Path(),
                decision,
                log_path=log_path,
            )
        if decision.verdict == "reject":
            return ReasoningPipelineResult(
                proposal_id=proposal.proposal_id,
                rejected=True,
                rejecting_decision=decision,
                decisions=tuple(decisions),
            )
    return ReasoningPipelineResult(
        proposal_id=proposal.proposal_id,
        rejected=False,
        rejecting_decision=None,
        decisions=tuple(decisions),
    )


def load_authority_manifest_for_pipeline(
    knowledge_root: Path, manifest_path: Path | None = None
) -> AuthorityManifest:
    """Convenience loader so a caller doesn't have to import :mod:`athenaeum.authority`.

    Delegates to :func:`athenaeum.authority.load_authority_manifest`, which
    already returns an empty (inert) manifest when the file is missing — a
    knowledge base with no manifest configured never rejects on the
    live-source-duplicate check, matching that module's own contract.
    """
    path = manifest_path or (knowledge_root / "authority-manifest.yaml")
    return load_authority_manifest(path)


__all__ = [
    "BODY_EXCERPT_WORD_LIMIT",
    "DEFAULT_T1_MODEL",
    "DEFAULT_TIER_CHAIN",
    "REASONING_TIER_LOG_FILENAME",
    "REASONING_TIER_LOG_VERSION",
    "REASONING_TIER_VERDICTS",
    "REJECT_REASON_CODES",
    "REJECT_REASON_CROSS_MEMORY_CLASS",
    "REJECT_REASON_DIFFERENT_ENTITIES",
    "REJECT_REASON_LIVE_SOURCE_DUPLICATE",
    "REJECT_REASON_OTHER",
    "T1_SYSTEM_PROMPT",
    "T1_TIER_NAME",
    "BoundedSourceView",
    "ReasoningPipelineResult",
    "ReasoningProposal",
    "ReasoningTierDecision",
    "ReasoningTierVerdict",
    "TierHandler",
    "bounded_views_for",
    "build_bounded_source_view",
    "build_t1_request_params",
    "default_reasoning_tier_log_path",
    "get_t1_model",
    "load_authority_manifest_for_pipeline",
    "read_reasoning_tier_decisions",
    "record_reasoning_tier_decision",
    "run_reasoning_pipeline",
    "run_t1_tier",
]
