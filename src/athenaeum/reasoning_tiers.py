# SPDX-License-Identifier: Apache-2.0
"""Tiered reasoning-pass pipeline for merge proposals (issues athenaeum#423, athenaeum#432).

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
:class:`ReasoningTierDecision`, :class:`ReasoningTierT2Decision`, ...).

Governing rule (settled product decision, do not re-litigate): **write
authority increases with tier; cheap tiers only reject and route, never
approve.** Concretely:

- **T1** (issue athenaeum#423): haiku/sonnet-class model, bounded input (titles +
  frontmatter + first ~100 words per source — NEVER full bodies). Can only
  REJECT (with a logged reason) or PASS UP. Approval is structurally
  unrepresentable in its output type — see :class:`ReasoningTierVerdict`.
- **T2** (issue athenaeum#432): opus-class model, FULL bodies, T1 survivors (pass-ups)
  only. Unlike T1, T2 DOES gain a form of write authority — but only inside
  a narrow, structurally-enforced SAFE CLASS (same ``memory_class``, <=3
  pages, no ``pii`` flag, no ``axiom`` member — see
  :func:`safe_class_violation`). Outside the safe class, or when T2 tries to
  pair an approval with rewritten content, the pipeline itself downgrades
  the outcome (to escalate/draft) regardless of what the model returned —
  see :func:`run_t2_tier` / :func:`_t2_decision_from_model_verdict` and
  :class:`ReasoningTierT2Decision`'s own broader verdict space
  (:data:`ReasoningTierT2Verdict`: approve / amend / draft / escalate).
  T1's own type (:class:`ReasoningTierVerdict`) is untouched by this —
  T2's authority is a NEW, separate type, not an extension of T1's.
- **Human** — the only actor that can approve OUTSIDE the T2 safe class,
  and the only actor that can ever approve a proposal whose content T2
  amended/drafted. A T1 pass-up with no T2 configured flows straight to the
  existing human queue (:func:`athenaeum.decisions.list_pending_decisions` /
  :func:`athenaeum.pending_merges.list_pending_merges`) UNCHANGED — see
  :func:`run_reasoning_pipeline`. A T2 "escalate" or "draft" decision joins
  that SAME queue (:func:`athenaeum.decisions.list_pending_decisions`) via
  the caller's existing pass-up-to-human wiring; T2 does not add a second
  queue.

Every tier decision — at any tier, whatever its verdict — is recorded as a
machine-readable, queryable event: ``(tier, decision, reason, model,
proposal_id)`` plus a timestamp. The log format mirrors
:mod:`athenaeum.provenance`'s merge-provenance ledger (append-only JSONL,
``O_APPEND`` + fsync, tolerant reader that skips a torn trailing line) —
same durability discipline, same "queryable append-only sidecar" shape,
just a different filename/record schema. T1 and T2 write into the exact
same log file/schema (:func:`_build_log_record_fields` is the single shared
record-shape builder) — only the ``tier`` tag and the set of legal
``decision`` values differ per tier's own type.

Out of scope here (see the issue body for the re-scope rationale):

- The calibration sampler that watches T1/T2 accuracy over time (athenaeum#438).
- Wiring ANY tier into ``wiki_dedupe.py``'s call sites — those still write
  straight to ``_pending_merges.md`` unscreened. (T2 IS wired into
  ``merge.py``'s merge path as of athenaeum#602 — see below.)

Production status (current, do not let this go stale again — athenaeum#525
corrected a prior overclaim in the docs, this restates it against the
code): :data:`DEFAULT_TIER_CHAIN` is genuinely the empty tuple — nothing
calls :func:`run_reasoning_pipeline` with tiers configured by default.
BUT this module IS reached in production, through exactly TWO callers in
``merge.py``, gated behind two INDEPENDENT flags (issue athenaeum#1200 — before
it, one shared ``resolve_reasoning_tier_auditing_enabled`` flag armed both):
T1 by ``resolve_reasoning_tier_auditing_enabled``, T2's auto-apply by its
own ``resolve_reasoning_tier_t2_auto_apply_enabled`` (both in
:mod:`athenaeum.config`). Both default OFF, so an unconfigured install still
sees every proposal pass straight to the human queue exactly as if this
module did not exist:

- ``t1_screen_rejects_merge_proposal`` (athenaeum#518) builds an explicit
  ``tier_chain=(functools.partial(run_t1_tier, ...),)`` and calls
  :func:`run_reasoning_pipeline` with it directly — bypassing the empty
  default. A confident T1 reject drops the proposal before T2 is ever
  consulted (no Opus spend on an already-rejected proposal).
- ``t2_screen_merge_proposal`` (athenaeum#602) calls :func:`run_t2_tier` directly
  (NOT through :func:`run_reasoning_pipeline` — T2's decision type,
  :class:`ReasoningTierT2Decision`, is a different shape than the
  reject/pass_up-only :class:`ReasoningTierDecision` the pipeline skeleton
  above is built around) on a T1 pass-up. A safe-class ``approve`` auto-
  finalizes the merge via :func:`athenaeum.pending_merges.resolve_merge`
  (``auto_applied=True``); every other verdict — including every
  safe-class-violation downgrade — falls through to the human queue.

Do not describe this module as having "no production caller" (stale as of
athenaeum#518) and do not describe T2 as "unwired" (stale as of athenaeum#602) — both
tiers are wired, each opt-in behind its OWN flag (athenaeum#1200), each
defaulting to the identical unscreened behavior when it is off.

**M17 retrofit (athenaeum#609), applying athenaeum#608's decided strictness posture to this
authority boundary.** T1/T2 now parse the model's raw JSON verdict through a
Pydantic response model (:class:`T1VerdictResponse` / :class:`T2VerdictResponse`)
before constructing a decision — the same one-model-per-contract, ``Literal``-
for-vocabulary convention :mod:`athenaeum.llm_schemas` established for its six
observe-only contracts. Two things about it are DELIBERATELY NOT that
module's convention, per athenaeum#608's decision (there is no ledger measurement
for these contracts, and the asymmetry at an authority boundary means a
tolerated unknown key is a widening risk, not a neutral one):

- ``model_config = ConfigDict(extra="forbid")`` by default, not
  ``extra="allow"``. An unexpected key at this boundary is never "signal to
  observe later" — it is validation failure, immediately.
- A validation failure drives the EXISTING safe fallback (T1: ``pass_up``;
  T2: ``escalate``) directly, in place of the hand-rolled defensive parsing
  it replaces. It is never logged-and-passed-through the way
  :func:`athenaeum.llm_schemas.observe` treats every other contract's
  mismatch — that posture is correct there and would be an erosion here (see
  athenaeum#609's issue body).

This is a SCHEMA-SHAPE-AND-PARSE-DIRECTION change only. Every structural
guarantee this module already carried is unchanged and unweakened by the
retrofit: :data:`ReasoningTierVerdict`'s two-member ``Literal`` (T1) and
:func:`run_t2_tier`'s :func:`safe_class_violation` gate (T2) remain the
enforcement points — see ``tests/test_reasoning_tiers_m17_retrofit.py`` for
the M17-retrofit-specific adversarial/directional/single-enforcement-point/
negative-control tests (this is the load-bearing pointer: that is where the
proof lives). The pre-existing, unmodified adversarial coverage this
retrofit builds on top of is still in ``tests/test_reasoning_tiers.py`` /
``tests/test_t2_reasoning_tier.py``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from athenaeum._retry import with_retry
from athenaeum.authority import (
    AuthorityManifest,
    find_duplicate_source,
    load_authority_manifest,
)
from athenaeum.config import (
    DEFAULT_CLASSIFY_MODEL,
    resolve_model,
    resolve_reasoning_tier_auditing_enabled,
    resolve_reasoning_tier_t2_auto_apply_enabled,
)
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import PendingMerge
from athenaeum.pii import is_pii_flagged
from athenaeum.prompt_safety import data_only_clause, fence_untrusted
from athenaeum.provider import resolve_max_tokens, resolve_thinking, response_text
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)


def _fmt_pydantic_error(err: Mapping[str, Any]) -> str:
    """Render one pydantic error dict as ``field.path: message`` (athenaeum#609).

    Takes a ``Mapping`` (not ``dict``) so pydantic's ``ErrorDetails``
    TypedDict — returned by ``ValidationError.errors()`` — is accepted
    directly, mirroring :func:`athenaeum.llm_schemas._fmt_error`'s own
    signature. Rendering logic is restated locally rather than imported —
    this module stays free of any :mod:`athenaeum.llm_schemas` dependency
    (that module's own docstring explicitly excludes the T1/T2 contracts, so
    pulling from it here would be backwards: this is a DIFFERENT, stricter
    posture, not an extension of the observe-only one).
    """
    loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
    return f"{loc}: {err.get('msg', 'invalid')}"


# Reasoning-tier output budgets (issue athenaeum#575): formerly bare literals in the
# request-param dicts below; named and resolved through the provider seam so
# each is a config-overridable knob. Values unchanged.
_T1_MAX_TOKENS = 256
_T2_MAX_TOKENS = 4096

# ---------------------------------------------------------------------------
# Model selection (issue athenaeum#423) — resolves via the existing provider-aware
# config chain (env > yaml `models.<knob>` > code default, issue athenaeum#232),
# exactly like every other tier/classifier in the codebase. NEVER hardcode
# a model id at a call site — see athenaeum.config.resolve_model.
# ---------------------------------------------------------------------------

#: T1 is the cheap reject-and-route tier — haiku-class by default. Overridable
#: via ``ATHENAEUM_REASONING_T1_MODEL`` env or ``models.reasoning_t1`` yaml.
#: The DEFAULT is single-sourced from ``config.DEFAULT_CLASSIFY_MODEL`` (issue
#: athenaeum#571, M19; relocated from ``tiers`` to the ``config`` leaf in athenaeum#640) so a
#: haiku-class bump touches one file, not four; the T1 env knob above is
#: unchanged.
DEFAULT_T1_MODEL = DEFAULT_CLASSIFY_MODEL


def _warn_if_tier_model_knob_inert(
    knob: str,
    env_var: str,
    config: dict[str, Any] | None,
    *,
    enabled: bool,
    enabling_hint: str,
) -> None:
    """Warn when a reasoning-tier model knob is set but has no effect (athenaeum#780).

    ``ATHENAEUM_REASONING_T1_MODEL`` / ``T2_MODEL`` (and their ``models.<knob>``
    yaml equivalents) are read regardless of whether that tier's own screen
    is armed, but only matter once it is — see ``docs/configuration.md``'s
    "Reasoning-tier screening" section. With the tier's screen off (the
    default), setting its model knob silently does nothing. This is NOT a
    claim that the tiers are dead: both have real production callers in
    ``merge.py`` (``t1_screen_rejects_merge_proposal``, athenaeum#518;
    ``t2_screen_merge_proposal``, athenaeum#602) that run whenever an operator
    opts in — see this module's "Production status" note above. The knob is
    opt-in-only, not inert-by-design.

    *enabled* is the CALLER's own tier-specific resolved flag (issue
    athenaeum#1200: T1 and T2 are independently armed, so this function no
    longer resolves a single shared flag itself — a T2 config with T1 off
    and T2 on must not warn about the T2 model knob being inert). *enabling_hint*
    names the env var / yaml key that arms THIS tier, for the warning text.
    """
    if enabled:
        return
    explicit = os.environ.get(env_var) is not None
    if not explicit and isinstance(config, dict):
        models = config.get("models")
        if isinstance(models, dict):
            raw = models.get(knob)
            explicit = isinstance(raw, str) and bool(raw.strip())
    if explicit:
        log.warning(
            "%s is set but has no effect: %s tier is disabled, so it never "
            "runs. %s",
            env_var,
            knob,
            enabling_hint,
        )


def get_t1_model(config: dict[str, Any] | None = None) -> str:
    """Resolve the T1 tier's model id (env > yaml > default, issue athenaeum#232)."""
    model = resolve_model(
        "reasoning_t1", "ATHENAEUM_REASONING_T1_MODEL", DEFAULT_T1_MODEL, config
    )
    _warn_if_tier_model_knob_inert(
        "reasoning_t1",
        "ATHENAEUM_REASONING_T1_MODEL",
        config,
        enabled=resolve_reasoning_tier_auditing_enabled(config),
        enabling_hint=(
            "Set ATHENAEUM_REASONING_TIER_AUDITING_ENABLED=1 (or yaml "
            "librarian.reasoning_tier_auditing_enabled: true) to enable it."
        ),
    )
    return model


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

#: T1's reject bins (issue athenaeum#423). A rejection's ``reason_code`` is one of
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


def _build_log_record_fields(
    *,
    tier: str,
    verdict: str,
    reason: str,
    reason_code: str | None,
    model: str | None,
    proposal_id: str,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Build one log record dict from raw fields (the shared record shape).

    Both T1 (:func:`record_reasoning_tier_decision`, fed from the
    reject/pass_up-only :class:`ReasoningTierDecision`) and T2 (fed from the
    broader approve/amend/draft/escalate :class:`ReasoningTierT2Decision`)
    write through this SAME field shape/schema — only the allowed
    ``verdict`` values differ per tier's own type, never the log record
    shape itself. This is what "T2 logs in the same shape as T1" means
    concretely: one schema, one file, a ``tier`` tag distinguishing rows.
    """
    stamp = (ts if ts is not None else datetime.now(tz=timezone.utc)).astimezone(
        timezone.utc
    )
    return {
        "v": REASONING_TIER_LOG_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "tier": tier,
        "decision": verdict,
        "reason": reason,
        "reason_code": reason_code,
        "model": model,
        "proposal_id": proposal_id,
    }


def _build_log_record(
    decision: ReasoningTierDecision, *, ts: datetime | None = None
) -> dict[str, Any]:
    return _build_log_record_fields(
        tier=decision.tier,
        verdict=decision.verdict,
        reason=decision.reason,
        reason_code=decision.reason_code,
        model=decision.model,
        proposal_id=decision.proposal_id,
        ts=ts,
    )


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


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
        "max_tokens": resolve_max_tokens(
            "reasoning_t1", "ATHENAEUM_REASONING_T1_MAX_TOKENS", _T1_MAX_TOKENS, config
        ),
        # Issue athenaeum#578: T1 is a cheap Haiku pre-screen (reject or pass_up) —
        # same posture as tier2_classify. Disabled explicitly.
        "thinking": resolve_thinking(
            "reasoning_t1", "ATHENAEUM_REASONING_T1_THINKING", "disabled", config
        ),
        "system": T1_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }


class T1VerdictResponse(BaseModel):
    """M17 response model (athenaeum#570 convention, athenaeum#609 retrofit) for the T1
    JSON contract — ``{"verdict": "reject" | "pass_up", "reason": "..."}``.

    ``verdict`` is a two-member ``Literal`` — the SAME two-member vocabulary
    as :data:`ReasoningTierVerdict` — so "approve" (or any other string) is
    unrepresentable here exactly as it is on :class:`ReasoningTierDecision`
    itself; a payload claiming it fails Pydantic validation rather than
    silently coercing. ``reason`` is required (unlike the llm_schemas.py
    ``Optional`` convention for a site that defaults a missing field): at
    this authority boundary a response carrying no reason at all is itself
    treated as malformed input, not defaulted through.

    ``extra="forbid"`` (see the module docstring's M17-retrofit note): an
    unexpected key is validation failure here, not an observe-only signal —
    the decided posture for a contract with no measured mismatch window.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["reject", "pass_up"]
    reason: str


def _parse_t1_response(text: str) -> tuple[ReasoningTierVerdict, str]:
    """Parse the T1 model's JSON response into (verdict, reason).

    Defensive parsing (mirrors :func:`athenaeum.tiers.parse_tier2_entities`):
    malformed/missing JSON, a payload that fails :class:`T1VerdictResponse`
    validation (missing field, wrong type, out-of-vocabulary verdict, extra
    key, empty payload, ...), degrades to a ``pass_up`` — T1 can only ever
    reject when it is confidently able to say so; anything it cannot parse
    AND validate is NOT treated as a rejection (that would be a
    false-negative failure mode with much higher cost than an extra
    pass-up). This is the retrofit's enforcement point (athenaeum#609): a
    schema mismatch here drives the fallback directly, unlike
    :mod:`athenaeum.llm_schemas`'s observe-only sites, where a mismatch is
    only ever logged.
    """
    text = text.strip()
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return "pass_up", f"T1 response unparseable, passing up: {text[:200]!r}"
    try:
        validated = T1VerdictResponse.model_validate(payload)
    except ValidationError as exc:
        # Includes: missing "verdict", "verdict" outside {"reject",
        # "pass_up"} (which covers "approve" and any other hallucinated
        # string), wrong-typed "verdict"/"reason", a missing "reason", an
        # unexpected extra key, and a non-dict/empty payload. T1's output
        # type has no "approve" branch under any of these, so even if the
        # model text says "approve" it is coerced to a pass-up, never
        # surfaced as approval.
        return (
            "pass_up",
            f"T1 response failed schema validation, passing up: {exc.error_count()} "
            f"error(s): {'; '.join(_fmt_pydantic_error(e) for e in exc.errors())}",
        )
    return validated.verdict, validated.reason


def _duplicate_check_reason(
    views: Sequence[BoundedSourceView],
    manifest: AuthorityManifest,
) -> str | None:
    """Return a rejection reason if any source duplicates a live authority source.

    Deterministic LOOKUP via :func:`athenaeum.authority.find_duplicate_source`
    — issue athenaeum#426's detector — over each source's bounded frontmatter view.
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
    an incompatible pairing (issue athenaeum#424 taxonomy) — clustering a ``fact``
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

    1. **Cross-`memory_class` pairing** (athenaeum#424 taxonomy) —
       :func:`_cross_memory_class_reason`.
    2. **Live-source duplicate** (athenaeum#426 detector) —
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
            input_toks,
            output_toks,
            cache_creation,
            cache_read,
            model=params["model"],
            knob="reasoning_t1",
        )

    # Issue athenaeum#578: response_text skips any leading thinking block (T1 runs
    # disabled today; the helper is text-block-equivalent for a text-only
    # response and keeps the site robust if the posture changes).
    verdict, reason = _parse_t1_response(response_text(response))
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
# T2 tier (issue athenaeum#432) — opus-class model, FULL bodies, T1 survivors only.
#
# Governing rule still applies (write authority increases with tier), but
# T2's decision space is DELIBERATELY BROADER than T1's reject/pass_up —
# see :data:`ReasoningTierT2Verdict`. T2 may:
#
#   - APPROVE   — ONLY inside the SAFE CLASS (see
#                 :func:`safe_class_violation` / :func:`run_t2_tier`):
#                 same memory_class, <=3 pages, no `pii` flag, no `axiom`
#                 member. Any violation makes the approve outcome
#                 structurally unreachable for that proposal — see
#                 :func:`_t2_decision_from_model_verdict` below, which is
#                 the ONLY place an approving :class:`ReasoningTierT2Decision`
#                 is ever constructed, and it refuses to do so when
#                 ``safe_class_violation(...)`` is non-None, regardless of
#                 what the model returned.
#   - AMEND     — proposes a different source SET (drop/add sources), but
#                 never rewrites body content. See
#                 :class:`ReasoningTierT2Decision.amended_sources`.
#   - DRAFT     — proposes a merge BODY for human review. See
#                 :class:`ReasoningTierT2Decision.drafted_body`.
#   - ESCALATE  — hands off to the human queue with a reason, no proposed
#                 change.
#
# Rewrite-then-self-approve is structurally impossible: the approve outcome
# and a populated ``drafted_body`` can never coexist on the same decision —
# see :class:`ReasoningTierT2Decision.__post_init__`. A decision that
# carries a drafted body is, by construction, always the draft outcome,
# never the approve one — there is no field state representing "I rewrote
# the content AND I approved it".
# ---------------------------------------------------------------------------

T2_TIER_NAME = "T2"

#: T2 is the opus-class deep-reasoning tier. Overridable via
#: ``ATHENAEUM_REASONING_T2_MODEL`` env or ``models.reasoning_t2`` yaml —
#: same env/yaml/default precedence as :func:`get_t1_model` (issue athenaeum#232).
DEFAULT_T2_MODEL = "claude-opus-4-8"  # Opus 4.8 (was 4.1, retiring; athenaeum#633)


def get_t2_model(config: dict[str, Any] | None = None) -> str:
    """Resolve the T2 tier's model id (env > yaml > default, issue athenaeum#232)."""
    model = resolve_model(
        "reasoning_t2", "ATHENAEUM_REASONING_T2_MODEL", DEFAULT_T2_MODEL, config
    )
    _warn_if_tier_model_knob_inert(
        "reasoning_t2",
        "ATHENAEUM_REASONING_T2_MODEL",
        config,
        enabled=resolve_reasoning_tier_t2_auto_apply_enabled(config),
        enabling_hint=(
            "Set ATHENAEUM_REASONING_TIER_T2_AUTO_APPLY_ENABLED=1 (or yaml "
            "librarian.reasoning_tier_t2_auto_apply_enabled: true) to enable "
            "it (issue athenaeum#1200 — T2 auto-apply is a separate opt-in "
            "from T1's reasoning_tier_auditing_enabled)."
        ),
    )
    return model


#: T2's decision space — DIFFERENT and BROADER than T1's
#: :data:`ReasoningTierVerdict`. T1's type is left untouched (per the
#: governing rule, cheap tiers never gain approval authority); T2 gets its
#: own, separate Literal so T1's guarantees cannot be weakened by extending
#: its enum.
ReasoningTierT2Verdict = Literal["approve", "amend", "draft", "escalate"]

REASONING_TIER_T2_VERDICTS: frozenset[str] = frozenset(
    {"approve", "amend", "draft", "escalate"}
)

#: Safe-class violation reason codes (one per predicate in the issue's safe
#: class: same memory_class, <=3 pages, no pii flag, no axiom member).
SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS = "cross_memory_class"
SAFE_CLASS_VIOLATION_TOO_MANY_PAGES = "too_many_pages"
SAFE_CLASS_VIOLATION_PII_FLAGGED = "pii_flagged"
SAFE_CLASS_VIOLATION_AXIOM_MEMBER = "axiom_member"
#: Live-source duplicate is a T1 reject bin, but a T1 pass-up does not
#: guarantee T1 even ran a manifest check (an absent authority_manifest is
#: tolerated at T1) — T2 re-checks with its OWN (possibly supplied)
#: manifest, per the issue's "T2 consults the athenaeum#426 authority manifest"
#: instruction, and treats a hit as a safe-class violation (never approve a
#: duplicate of a live source, no matter how small/homogeneous the cluster).
SAFE_CLASS_VIOLATION_LIVE_SOURCE_DUPLICATE = "live_source_duplicate"

#: Maximum number of source pages a T2 approval may span (issue athenaeum#432).
SAFE_CLASS_MAX_PAGES = 3


def safe_class_violation(
    views: Sequence[BoundedSourceView],
    *,
    authority_manifest: AuthorityManifest | None = None,
) -> str | None:
    """Return the violated safe-class reason code, or ``None`` if all pass.

    The SAFE CLASS (issue athenaeum#432) is ALL of: same ``memory_class`` across
    every source, at most :data:`SAFE_CLASS_MAX_PAGES` pages, no source
    carrying a truthy ``pii`` flag (:func:`athenaeum.pii.is_pii_flagged`),
    and no source with ``memory_class: axiom``. This function is the
    SINGLE gate consulted by :func:`run_t2_tier` before an "approve" verdict
    may be constructed — see that function for how a violation makes
    approval structurally unreachable regardless of the model's own output.

    Order of checks is cheapest/most-certain first (page count needs no
    parsing; pii/axiom/cross-class need frontmatter already loaded in
    *views*); the first violation found is returned (a decision only needs
    ONE reason, and returning the first keeps behavior deterministic).
    """
    if len(views) > SAFE_CLASS_MAX_PAGES:
        return SAFE_CLASS_VIOLATION_TOO_MANY_PAGES

    for view in views:
        if is_pii_flagged(view.frontmatter):
            return SAFE_CLASS_VIOLATION_PII_FLAGGED

    for view in views:
        raw_class = view.frontmatter.get("memory_class")
        if isinstance(raw_class, str) and raw_class.strip().lower() == "axiom":
            return SAFE_CLASS_VIOLATION_AXIOM_MEMBER

    cross_class_reason = _cross_memory_class_reason(views)
    if cross_class_reason is not None:
        return SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS

    if authority_manifest is not None:
        dup_reason = _duplicate_check_reason(views, authority_manifest)
        if dup_reason is not None:
            return SAFE_CLASS_VIOLATION_LIVE_SOURCE_DUPLICATE

    return None


@dataclass(frozen=True)
class ReasoningTierT2Decision:
    """One T2 decision on one proposal.

    Broader decision space than T1's :class:`ReasoningTierDecision` —
    ``verdict`` ranges over :data:`ReasoningTierT2Verdict` (approve / amend
    / draft / escalate), not just reject/pass_up. Kept as a SEPARATE
    dataclass (not an extension of T1's) so T1's type is never weakened.

    Structural (not merely conventional) guarantees, enforced in
    ``__post_init__``:

    - **No self-approve-rewrite.** ``drafted_body`` (a proposed rewrite of
      the merge content) and ``verdict == "approve"`` can never coexist on
      the same instance. A decision that carries drafted content is always
      ``verdict == "draft"`` — routed to human review — never "approve".
      This makes "T2 rewrote the draft and self-approved it" unrepresentable
      in the type, not just discouraged by a prompt.
    - **Approval always carries the safe-class fields as evidence.**
      ``safe_class_violation`` must be ``None`` whenever ``verdict ==
      "approve"`` — an approval can never be constructed while flagging its
      own disqualifying violation (belt-and-suspenders alongside
      :func:`run_t2_tier`'s gate, which is the actual enforcement point
      callers rely on; this guards the type itself against a future
      caller that tries to hand-construct an approval directly).
    - ``amended_sources`` (a proposed different source SET) is allowed
      alongside "amend" only — amending which sources are considered is
      explicitly NOT the same act as rewriting content, but it still may
      never coexist with "approve" in the same decision (an amendment
      changes what a human/next pass reviews; it is not itself an
      approval).
    - ``reason`` is always non-empty, mirroring T1's contract.
    """

    tier: str
    verdict: ReasoningTierT2Verdict
    reason: str
    model: str | None
    proposal_id: str
    reason_code: str | None = None
    safe_class_violation: str | None = None
    amended_sources: tuple[str, ...] | None = None
    drafted_body: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in REASONING_TIER_T2_VERDICTS:
            raise ValueError(
                f"invalid ReasoningTierT2Decision.verdict: {self.verdict!r} "
                f"(must be one of {sorted(REASONING_TIER_T2_VERDICTS)!r})"
            )
        if not self.reason or not self.reason.strip():
            raise ValueError("ReasoningTierT2Decision.reason must be non-empty")
        if self.verdict == "approve":
            if self.drafted_body is not None:
                raise ValueError(
                    "ReasoningTierT2Decision: an 'approve' verdict may never "
                    "carry a drafted_body (rewrite-then-self-approve is "
                    "structurally forbidden) — use verdict='draft' instead"
                )
            if self.amended_sources is not None:
                raise ValueError(
                    "ReasoningTierT2Decision: an 'approve' verdict may never "
                    "carry amended_sources — use verdict='amend' instead"
                )
            if self.safe_class_violation is not None:
                raise ValueError(
                    "ReasoningTierT2Decision: an 'approve' verdict may never "
                    f"carry a safe_class_violation ({self.safe_class_violation!r})"
                )


# The FULL source bodies T2 sees are untrusted content (audit M21): T1 gets
# some protection from repr-quoting (reasoning_tiers.py `f"  {k}: {v!r}"`), but
# T2's full bodies had no delimiter, no defang, and — critically — no data-only
# clause, while T2's output space includes `approve`, which finalizes a merge.
# The clause names the <source_body> fence that `_render_full_source` wraps each
# body in, so an injected body cannot forge the boundary or issue instructions.
T2_SYSTEM_PROMPT = (
    """You are a careful, deep-reasoning reviewer for a memory-merge proposal
queue. You see proposals that a cheaper pre-screener already passed up as
NOT confidently rejectable. You are shown FULL source bodies (not excerpts).

"""
    + data_only_clause("source_body")
    + """

You may return exactly one of:
- "approve": the merge is correct and safe to finalize automatically. Only
  ever appropriate for a small, homogeneous, non-sensitive cluster.
- "amend": the merge is directionally right but the SOURCE SET should
  change (drop or add sources) before anyone finalizes it. You may name a
  revised source list. You may NOT rewrite the merge body content yourself.
- "draft": write a proposed merged body for a human to review and finalize.
  This is the ONLY way to propose new merged content — drafting NEVER
  self-approves; a human still decides.
- "escalate": you are not confident enough to do any of the above; hand off
  to a human with your reasoning.

Respond with ONLY a JSON object of the shape:
{"verdict": "approve" | "amend" | "draft" | "escalate",
 "reason": "<one or two sentences>",
 "amended_sources": ["path", ...] | null,
 "drafted_body": "<merged body text>" | null}"""
)

# Generous cap on the fenced full body (audit M21). T2's privilege is FULL
# bodies, so this is set far above any realistic memory file — it only bounds a
# pathologically large body from blowing up the request, and defangs the
# <source_body> fence so the body cannot forge its own boundary. T2 does not
# copy anchors to real files (its output is a verdict / human-reviewed draft),
# so — unlike tiers.py's merge path — defang here is safe (defang default=True).
_T2_FULL_BODY_MAX_CHARS = 100_000


def _render_full_source(view: BoundedSourceView, full_body: str) -> str:
    fm_lines = "\n".join(f"  {k}: {v!r}" for k, v in sorted(view.frontmatter.items()))
    fenced_body = fence_untrusted(
        full_body, tag="source_body", max_chars=_T2_FULL_BODY_MAX_CHARS
    )
    return (
        f"- path: {view.path}\n"
        f"  title: {view.title}\n"
        f"  frontmatter:\n{fm_lines or '  (none)'}\n"
        f"  body (FULL):\n{fenced_body}"
    )


def _read_full_body(path: str) -> str:
    """Read a source's full body text (post-frontmatter). T2-only privilege.

    Degrades to an empty string on a missing/unreadable file, mirroring
    :func:`build_bounded_source_view`'s own degrade-not-crash contract — a
    vanished source should still let T2 reach a decision (most likely
    escalate), not blow up the whole batch.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    _meta, body = parse_frontmatter(text)
    return body.strip()


def build_t2_request_params(
    proposal: ReasoningProposal,
    views: Sequence[BoundedSourceView],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Messages API kwargs for one T2 deep-reasoning call.

    Unlike T1 (:func:`build_t1_request_params`, bounded-excerpt only), T2's
    payload includes each source's FULL body — T2 sees "full bodies, T1
    survivors only" per the issue's settled decision surface.
    """
    sources_block = "\n".join(
        _render_full_source(v, _read_full_body(v.path)) for v in views
    )
    user_msg = (
        f"## Candidate merge target\n{proposal.merge_target_name}\n\n"
        f"## Candidate sources ({len(views)})\n{sources_block}\n\n"
        "## Instructions\nDecide approve/amend/draft/escalate per the "
        "system prompt. Return ONLY the JSON object."
    )
    return {
        "model": get_t2_model(config),
        "max_tokens": resolve_max_tokens(
            "reasoning_t2", "ATHENAEUM_REASONING_T2_MAX_TOKENS", _T2_MAX_TOKENS, config
        ),
        # Issue athenaeum#578: T2 already defaults to Opus 4.8 (deep reasoning over
        # full source bodies — approve/amend/draft/escalate) and adaptive
        # thinking benefits it now, not just after a future model bump.
        # Enabled explicitly. NOTE: this stage's max_tokens (4096) was not in
        # issue athenaeum#578's named re-baseline table (only the resolver and
        # merge-patch stages were flagged there) — left unchanged here; a
        # future re-baseline for this stage is deferred, same as the spend-
        # ceiling re-baseline called out in issue athenaeum#578's out-of-scope list.
        "thinking": resolve_thinking(
            "reasoning_t2", "ATHENAEUM_REASONING_T2_THINKING", "adaptive", config
        ),
        "system": T2_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }


class T2VerdictResponse(BaseModel):
    """M17 response model (athenaeum#570 convention, athenaeum#609 retrofit) for the T2
    JSON contract — ``{"verdict": ..., "reason": ..., "amended_sources": ...,
    "drafted_body": ...}``.

    ``verdict`` ranges over the SAME four-member vocabulary as
    :data:`ReasoningTierT2Verdict` — including ``"approve"``, which IS a
    legitimate T2 outcome (unlike T1, T2 has real, bounded write authority).
    This model does not, and must not, gate approval; it only proves the
    payload is well-formed enough to trust as input to
    :func:`_t2_decision_from_model_verdict`, which is the sole place
    :func:`run_t2_tier`'s :func:`safe_class_violation` gate is applied.
    ``reason`` is required, same rationale as :class:`T1VerdictResponse`.

    ``extra="forbid"`` (see the module docstring's M17-retrofit note) is the
    load-bearing difference from :mod:`athenaeum.llm_schemas`'s equivalent
    contracts: an extra key here is validation failure, not an
    observe-and-continue signal — a payload combining a legitimate-looking
    ``"approve"`` with an unrecognized key must never reach the safe-class
    gate as a trusted approval; it fails validation and escalates instead.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "amend", "draft", "escalate"]
    reason: str
    amended_sources: list[str] | None = None
    drafted_body: str | None = None


def _parse_t2_response(
    text: str,
) -> tuple[ReasoningTierT2Verdict, str, tuple[str, ...] | None, str | None]:
    """Parse the T2 model's JSON response.

    Returns ``(verdict, reason, amended_sources, drafted_body)``. Defensive
    parsing mirrors :func:`_parse_t1_response`: malformed/missing JSON, or a
    payload that fails :class:`T2VerdictResponse` validation (missing field,
    wrong type, out-of-vocabulary verdict, extra key, empty payload, ...),
    degrades to ``"escalate"`` — T2's failure mode is "ask a human", never a
    silent approval. This is the retrofit's enforcement point (athenaeum#609): a
    schema mismatch here drives the fallback directly rather than being
    logged-and-passed-through the way :mod:`athenaeum.llm_schemas` treats
    every other contract's mismatch.

    Note what this function does NOT do: it never inspects ``violation`` or
    downgrades an "approve" verdict itself — that stays exclusively
    :func:`_t2_decision_from_model_verdict`'s job (called from
    :func:`run_t2_tier`, gated on :func:`safe_class_violation`), so the
    safe-class enforcement point remains singular even after this retrofit.
    """
    text = text.strip()
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return "escalate", f"T2 response unparseable, escalating: {text[:200]!r}", None, None
    try:
        validated = T2VerdictResponse.model_validate(payload)
    except ValidationError as exc:
        # Includes: missing "verdict", "verdict" outside the four allowed
        # values (a model hallucinating some other string), wrong-typed
        # fields, a missing "reason", an unexpected extra key (including one
        # riding alongside an otherwise-legitimate-looking "approve"), and a
        # non-dict/empty payload. Never coerced to "approve" — the safest
        # fallback (escalate) is used instead, matching T1's "cannot parse
        # confidently -> least-authority fallback" discipline.
        return (
            "escalate",
            f"T2 response failed schema validation, escalating: {exc.error_count()} "
            f"error(s): {'; '.join(_fmt_pydantic_error(e) for e in exc.errors())}",
            None,
            None,
        )

    amended_sources = (
        tuple(validated.amended_sources) if validated.amended_sources else None
    )
    drafted_body = (
        validated.drafted_body
        if validated.drafted_body and validated.drafted_body.strip()
        else None
    )
    return validated.verdict, validated.reason, amended_sources, drafted_body


def _t2_decision_from_model_verdict(
    *,
    proposal_id: str,
    model: str,
    verdict: ReasoningTierT2Verdict,
    reason: str,
    amended_sources: tuple[str, ...] | None,
    drafted_body: str | None,
    violation: str | None,
) -> ReasoningTierT2Decision:
    """Build the final T2 decision, enforcing both structural rules.

    This is the ONLY place in the module that turns a model's raw verdict
    into a :class:`ReasoningTierT2Decision`. Both structural guarantees are
    enforced HERE, independent of what the (possibly mocked) model
    returned:

    1. **Safe-class gate.** If *violation* is non-None, "approve" is
       downgraded to "escalate" — no matter what the model said. A
       cross-class / >3-page / pii-flagged / axiom-member proposal cannot
       reach an approved :class:`ReasoningTierT2Decision`, full stop.
    2. **No self-approve-rewrite.** If the model tried to pair "approve"
       with a ``drafted_body`` (rewriting content and approving in the same
       breath), the verdict is downgraded to "draft" — the drafted content
       is preserved for human review, but it can never carry approval
       authority in the same decision.

    Downgrades always route to the LOWER-authority option ("escalate" is
    lower authority than "approve"; "draft" — human-reviewed — is lower
    authority than a self-approval), never silently discarded.
    """
    reason_code = None
    effective_verdict: ReasoningTierT2Verdict = verdict

    if verdict == "approve" and violation is not None:
        effective_verdict = "escalate"
        reason_code = violation
        reason = (
            f"model returned 'approve' but safe-class violation "
            f"{violation!r} makes approval structurally unreachable; "
            f"escalating. Model reason was: {reason}"
        )
    elif verdict == "approve" and drafted_body is not None:
        effective_verdict = "draft"
        reason = (
            "model returned 'approve' alongside a drafted_body; "
            "rewrite-then-self-approve is structurally forbidden, so this "
            "is routed to human review as a draft instead. Model reason "
            f"was: {reason}"
        )

    # amended_sources/drafted_body are only meaningful on their own verdicts
    # — strip them when the effective verdict does not carry that field, so
    # __post_init__'s "approve never carries these" invariant holds and the
    # log record only reflects the ACTUAL outcome.
    final_amended = amended_sources if effective_verdict == "amend" else None
    final_drafted = drafted_body if effective_verdict == "draft" else None

    return ReasoningTierT2Decision(
        tier=T2_TIER_NAME,
        verdict=effective_verdict,
        reason=reason,
        reason_code=reason_code,
        model=model,
        proposal_id=proposal_id,
        safe_class_violation=violation if effective_verdict == "escalate" else None,
        amended_sources=final_amended,
        drafted_body=final_drafted,
    )


def record_reasoning_tier_t2_decision(
    wiki_root: Path,
    decision: ReasoningTierT2Decision,
    *,
    log_path: Path | None = None,
    ts: datetime | None = None,
) -> bool:
    """Append one T2 decision to the SAME decision log T1 writes to.

    Same file, same JSONL record shape (:func:`_build_log_record_fields`),
    same append/fsync/best-effort-never-raises discipline as
    :func:`record_reasoning_tier_decision` — the only difference is the
    source object's (broader) verdict type. Every T2 decision — approve,
    amend, draft, or escalate — is logged, mirroring T1's "every decision
    (reject or pass-up) is recorded" contract.
    """
    try:
        record = _build_log_record_fields(
            tier=decision.tier,
            verdict=decision.verdict,
            reason=decision.reason,
            reason_code=decision.reason_code,
            model=decision.model,
            proposal_id=decision.proposal_id,
            ts=ts,
        )
        target = (
            log_path if log_path is not None else default_reasoning_tier_log_path(wiki_root)
        )
        _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — ledger write must never break the pipeline
        log.debug(
            "T2 reasoning tier decision log write skipped (%s): %s",
            type(exc).__name__,
            exc,
        )
        return False


def run_t2_tier(
    proposal: ReasoningProposal,
    *,
    client: Any | None,
    authority_manifest: AuthorityManifest | None = None,
    config: dict[str, Any] | None = None,
    usage: Any | None = None,
) -> ReasoningTierT2Decision:
    """Run the T2 (opus, full-bodies, T1-survivors-only) tier over one proposal.

    T2 sees FULL source bodies (:func:`build_t2_request_params`) — a
    materially different input scope than T1's bounded excerpts. Its output
    space is also materially broader (:data:`ReasoningTierT2Verdict`), but
    two rules are enforced STRUCTURALLY, not just by the system prompt:

    1. **Approval requires the safe class.** :func:`safe_class_violation` is
       evaluated BEFORE trusting the model's verdict; any violation
       downgrades a would-be "approve" to "escalate" regardless of what the
       model actually returned (see :func:`_t2_decision_from_model_verdict`).
    2. **No self-approve-rewrite.** A model response pairing "approve" with
       a ``drafted_body`` is downgraded to "draft", never surfaced as an
       approval.

    *client* ``None`` (no LLM configured) short-circuits straight to
    "escalate" — mirroring T1's ``client is None`` degradation, except T2's
    least-authority fallback is escalate (T1's is pass_up; T2 has no
    pass_up option, so escalate — routing straight to the human queue — is
    the equivalent floor).
    """
    views = bounded_views_for(proposal)
    violation = safe_class_violation(views, authority_manifest=authority_manifest)

    if client is None:
        return ReasoningTierT2Decision(
            tier=T2_TIER_NAME,
            verdict="escalate",
            reason="no LLM client configured for T2; escalating",
            model=None,
            proposal_id=proposal.proposal_id,
        )

    params = build_t2_request_params(proposal, views, config=config)
    response = with_retry(
        lambda: client.messages.create(**params),
        description=f"t2_reasoning_tier {proposal.proposal_id}",
    )
    if usage is not None and hasattr(response, "usage"):
        from athenaeum.models import cache_usage_counts

        input_toks, output_toks, cache_creation, cache_read = cache_usage_counts(
            response
        )
        usage.add(
            input_toks,
            output_toks,
            cache_creation,
            cache_read,
            model=params["model"],
            knob="reasoning_t2",
        )

    # Issue athenaeum#578: T2 enables adaptive thinking — response_text skips any
    # leading thinking block and returns the verdict JSON answer.
    verdict, reason, amended_sources, drafted_body = _parse_t2_response(
        response_text(response)
    )
    return _t2_decision_from_model_verdict(
        proposal_id=proposal.proposal_id,
        model=params["model"],
        verdict=verdict,
        reason=reason,
        amended_sources=amended_sources,
        drafted_body=drafted_body,
        violation=violation,
    )


# ---------------------------------------------------------------------------
# Pipeline skeleton — ordered tier handlers, tolerant of an absent T2.
# ---------------------------------------------------------------------------

#: A tier handler takes a proposal and returns its decision. Any callable
#: matching this shape can be slotted into a chain passed to
#: :func:`run_reasoning_pipeline` — athenaeum#432's T2 handler needs only to match
#: this signature, no rework of the skeleton required.
TierHandler = Callable[[ReasoningProposal], ReasoningTierDecision]

#: The default tier chain: T1 only, until athenaeum#432 adds a T2 handler here (or a
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
    (the default, until athenaeum#432 lands) it should be handed to the existing
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
    issue athenaeum#432 adds a T2 handler, callers either pass ``tier_chain=()`` (no
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
    "DEFAULT_T2_MODEL",
    "DEFAULT_TIER_CHAIN",
    "REASONING_TIER_LOG_FILENAME",
    "REASONING_TIER_LOG_VERSION",
    "REASONING_TIER_T2_VERDICTS",
    "REASONING_TIER_VERDICTS",
    "REJECT_REASON_CODES",
    "REJECT_REASON_CROSS_MEMORY_CLASS",
    "REJECT_REASON_DIFFERENT_ENTITIES",
    "REJECT_REASON_LIVE_SOURCE_DUPLICATE",
    "REJECT_REASON_OTHER",
    "SAFE_CLASS_MAX_PAGES",
    "SAFE_CLASS_VIOLATION_AXIOM_MEMBER",
    "SAFE_CLASS_VIOLATION_CROSS_MEMORY_CLASS",
    "SAFE_CLASS_VIOLATION_LIVE_SOURCE_DUPLICATE",
    "SAFE_CLASS_VIOLATION_PII_FLAGGED",
    "SAFE_CLASS_VIOLATION_TOO_MANY_PAGES",
    "T1_SYSTEM_PROMPT",
    "T1_TIER_NAME",
    "T1VerdictResponse",
    "T2_SYSTEM_PROMPT",
    "T2_TIER_NAME",
    "T2VerdictResponse",
    "BoundedSourceView",
    "ReasoningPipelineResult",
    "ReasoningProposal",
    "ReasoningTierDecision",
    "ReasoningTierT2Decision",
    "ReasoningTierT2Verdict",
    "ReasoningTierVerdict",
    "TierHandler",
    "bounded_views_for",
    "build_bounded_source_view",
    "build_t1_request_params",
    "build_t2_request_params",
    "default_reasoning_tier_log_path",
    "get_t1_model",
    "get_t2_model",
    "load_authority_manifest_for_pipeline",
    "read_reasoning_tier_decisions",
    "record_reasoning_tier_decision",
    "record_reasoning_tier_t2_decision",
    "run_reasoning_pipeline",
    "run_t1_tier",
    "run_t2_tier",
    "safe_class_violation",
]
