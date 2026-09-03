# SPDX-License-Identifier: Apache-2.0
"""The T1/T2 reasoning-tier merge screens (issue athenaeum#1257) — L4 domain/pipeline.

Home of the two merge-proposal reasoning screens that used to live in
:mod:`athenaeum.merge`:

- :func:`t1_screen_rejects_merge_proposal` (issue athenaeum#518) — a pure
  boolean gate over a candidate proposal's ``member_paths`` +
  ``merge_target_name``. A confident ``reject`` drops the proposal before
  it reaches the human queue; every other outcome (disabled, dry-run, no
  client, no members, a tripped spend ceiling, a pass-up) returns
  ``False`` and the caller writes the proposal exactly as it would with
  no screen at all.
- :func:`t2_screen_merge_proposal` (issue athenaeum#602) — consulted on a
  T1 pass-up, and the only path that can AUTO-FINALIZE a safe-class
  ``approve`` without human review.

**Why they moved (issue athenaeum#1257).** Both screens are
domain-agnostic: neither reads anything C4-specific, and both were called
from exactly two places, both inside :mod:`athenaeum.merge`'s
propose-merge lane. Retiring that lane (issue athenaeum#1256, part of the
athenaeum#715 phase-4 plan) would have orphaned them. Siting them here —
above nothing, below :mod:`athenaeum.merge` and
:mod:`athenaeum.cluster_comparator` alike — lets the cluster-domain
comparator adapter reach T1 without importing :mod:`athenaeum.merge`,
while C4 keeps reaching both by import until athenaeum#1256 retires it.

**T1 is wired into the cluster-domain lane; T2 deliberately is NOT.**
:func:`athenaeum.cluster_comparator.run_cluster_comparator` produces
:class:`~athenaeum.comparator.CompareOutcome` objects. It does not call
:func:`athenaeum.pending_merges.write_pending_merge` and it fabricates
neither a ``confidence`` scalar nor a ``draft_merged_body`` — the two
fields T2's auto-finalize path requires. Inventing them at the comparator
seam is exactly the anti-pattern athenaeum#658 finding D2 recorded and
athenaeum#715 banned ("no confidence thresholds anywhere — the two
highest-confidence historical merge proposals were both wrong"). So T1,
which needs neither, is wired into the cluster-domain pair path, and T2
is relocated here with it but keeps only its existing C4 call site.
``tests/test_cluster_comparator_t1_screen.py`` pins that absence.

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.pending_merges`
(L4) for the proposal id / write / resolve surface, plus
:mod:`athenaeum.calibration` (L3), :mod:`athenaeum.spend` (L3),
:mod:`athenaeum.models` (L1), :mod:`athenaeum.provider` (L3) and
:mod:`athenaeum.reasoning_tiers`. It imports nothing from
:mod:`athenaeum.merge` or :mod:`athenaeum.cluster_comparator` — both of
those import THIS module, and neither back-edge exists, so no cycle is
introduced (``tests/test_import_graph_acyclic.py`` stays pinned at
``[]``).
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum import spend
from athenaeum.calibration import sample_tier_decision
from athenaeum.models import TokenUsage
from athenaeum.pending_merges import _make_id, resolve_merge, write_pending_merge
from athenaeum.reasoning_tiers import (
    ReasoningProposal,
    record_reasoning_tier_t2_decision,
    run_reasoning_pipeline,
    run_t1_tier,
    run_t2_tier,
)

if TYPE_CHECKING:
    from athenaeum.provider import LLMBackend

__all__ = [
    "t1_screen_rejects_merge_proposal",
    "t2_screen_merge_proposal",
]

log = logging.getLogger(__name__)


def t1_screen_rejects_merge_proposal(
    *,
    member_paths: list[str],
    merge_target_name: str,
    cluster_id: str,
    client: "LLMBackend | None",
    usage: TokenUsage | None,
    wiki_root: Path,
    config: dict[str, Any] | None,
    provider: str,
    authority_manifest: Any,
    enabled: bool,
    dry_run: bool,
) -> bool:
    """Run the T1 reasoning tier over one merge proposal (issue athenaeum#518).

    Returns ``True`` when the proposal should be DROPPED before the human queue
    — i.e. the tier returned a confident ``reject``. Returns ``False`` (write
    the proposal to ``_pending_merges.md`` as usual) in every other case:
    disabled, dry-run, no client, no members, a tripped spend ceiling (athenaeum#568 —
    degrade to an unscreened write rather than block the queue), or a pass-up.

    On a reject it also surfaces the decision for the human-audit calibration
    loop via :func:`athenaeum.calibration.sample_tier_decision` (a no-op below
    the configured sample rate), so the governance loop actually fires. The
    ``proposal_id`` is derived with the SAME :func:`_make_id` that
    :func:`write_pending_merge` uses, so the tier log / audit sample correlate
    with the human-facing :class:`~athenaeum.pending_merges.PendingMerge`.
    """
    if not (enabled and client is not None and not dry_run and member_paths):
        return False

    # Issue athenaeum#568: the reasoning screen adds LLM calls to the merge phase, so it
    # participates in the spend ceiling. A tripped budget degrades to today's
    # unscreened write — it must never block the merge queue.
    if usage is not None:
        ceiling = spend.ceiling_tripped(usage, provider=provider, config=config)
        if ceiling is not None:
            log.warning(
                "resolutions: spend ceiling reached (%s) — skipping T1 reasoning "
                "screen for cluster %s; writing proposal unscreened",
                ceiling,
                cluster_id,
            )
            return False

    proposal = ReasoningProposal(
        proposal_id=_make_id(member_paths, merge_target_name),
        merge_target_name=merge_target_name,
        sources=tuple(member_paths),
    )
    # Count the attempt against the run budget, mirroring the Opus-resolver
    # convention in _maybe_propose (run_t1_tier itself only adds token counts
    # via usage.add, not api_calls).
    if usage is not None:
        usage.api_calls += 1
    tier_chain = (
        functools.partial(
            run_t1_tier,
            client=client,
            config=config,
            usage=usage,
            authority_manifest=authority_manifest,
        ),
    )
    result = run_reasoning_pipeline(proposal, tier_chain=tier_chain, wiki_root=wiki_root)
    if result.rejected and result.rejecting_decision is not None:
        decision = result.rejecting_decision
        sample_tier_decision(
            wiki_root,
            tier=decision.tier,
            verdict=decision.verdict,
            proposal_id=decision.proposal_id,
            reason=decision.reason,
            config=config,
        )
        log.info(
            "resolutions: T1 reasoning tier REJECTED merge proposal for cluster "
            "%s (%s: %s); dropped before the human queue",
            cluster_id,
            decision.reason_code or "reject",
            decision.reason,
        )
        return True
    return False


def t2_screen_merge_proposal(
    *,
    member_paths: list[str],
    merge_target_name: str,
    rationale: str,
    draft_merged_body: str,
    confidence: float,
    write_kind: str,
    cluster_id: str,
    client: "LLMBackend | None",
    usage: TokenUsage | None,
    wiki_root: Path,
    config: dict[str, Any] | None,
    provider: str,
    authority_manifest: Any,
    enabled: bool,
    dry_run: bool,
) -> bool:
    """Run the T2 reasoning tier over a T1 pass-up and auto-finalize a safe-class
    approval (issue athenaeum#602).

    Returns ``True`` when the proposal has ALREADY been fully handled — either
    auto-finalized (written to ``_pending_merges.md`` AND immediately resolved
    as an auto-applied approve) or (defensively) dropped — so the caller must
    NOT also call :func:`athenaeum.pending_merges.write_pending_merge`. Returns
    ``False`` in every case where the proposal should still be written to the
    human queue as usual: disabled, dry-run, no client, no members, a tripped
    spend ceiling, an escalate/amend/draft verdict, or an ``approve`` that
    fails the safe-class gate.

    FAIL-SAFE DIRECTION (issue athenaeum#602, absolute): every degradation path here
    returns ``False`` — ceiling tripped, unparseable/unexpected model output,
    tier disabled, or a safe-class violation all fall through to the SAME
    unscreened ``write_pending_merge`` the caller already uses when T2 is
    absent. There is no path from a T2 malfunction to an unreviewed write:
    the ONLY way this function causes a wiki write is
    ``run_t2_tier`` returning ``verdict == "approve"``, and
    :func:`athenaeum.reasoning_tiers.run_t2_tier` /
    ``_t2_decision_from_model_verdict`` already make that verdict structurally
    unreachable whenever :func:`athenaeum.reasoning_tiers.safe_class_violation`
    fires — this function does not re-implement that gate, it only trusts the
    decision object's own ``verdict`` field, which cannot lie.

    Auto-finalize (the ``approve`` branch) reuses the EXACT SAME write path a
    human approval uses: :func:`athenaeum.pending_merges.write_pending_merge`
    followed by :func:`athenaeum.pending_merges.resolve_merge` (``decision=
    "approve"``, ``auto_applied=True``) — the fold/create ``write_kind``
    mechanics (issue athenaeum#421/#425) are untouched, no second write path is added.
    ``auto_applied=True`` durably marks the resolved block and the provenance
    ledger record so a human can always tell this write was never reviewed.

    A sampled ``approve`` (per
    :func:`athenaeum.config.resolve_audit_sample_rate_t2_approvals`, default
    7.5%) is surfaced to the calibration ledger exactly like a T1 reject is —
    via :func:`athenaeum.calibration.sample_tier_decision` — so an
    auto-applied merge can be later reviewed and, if wrong, recorded as an
    overturn of an APPLIED merge (see
    :func:`athenaeum.calibration.record_audit_review`'s ``applied`` handling).
    """
    if not (enabled and client is not None and not dry_run and member_paths):
        return False

    # Same spend-ceiling participation as T1 (athenaeum#568): T2 is an Opus call, so a
    # tripped ceiling must degrade to the human queue, never to an unreviewed
    # write. Checked BEFORE any T2 call is attempted.
    if usage is not None:
        ceiling = spend.ceiling_tripped(usage, provider=provider, config=config)
        if ceiling is not None:
            log.warning(
                "resolutions: spend ceiling reached (%s) — skipping T2 reasoning "
                "tier for cluster %s; writing proposal unscreened to the human "
                "queue",
                ceiling,
                cluster_id,
            )
            return False

    proposal = ReasoningProposal(
        proposal_id=_make_id(member_paths, merge_target_name),
        merge_target_name=merge_target_name,
        sources=tuple(member_paths),
    )
    # Count the attempt against the run budget, mirroring T1's convention —
    # run_t2_tier itself only adds token counts via usage.add, not api_calls.
    if usage is not None:
        usage.api_calls += 1

    decision = run_t2_tier(
        proposal,
        client=client,
        authority_manifest=authority_manifest,
        config=config,
        usage=usage,
    )
    record_reasoning_tier_t2_decision(wiki_root, decision)

    if decision.verdict != "approve":
        # escalate / amend / draft — including every safe-class-violation
        # downgrade and every parse/verdict failure, all of which
        # run_t2_tier already coerced to "escalate". Fall through to the
        # human queue unchanged; the decision is already logged above.
        log.info(
            "resolutions: T2 reasoning tier verdict %r for cluster %s (%s); "
            "writing proposal to the human queue",
            decision.verdict,
            cluster_id,
            decision.reason,
        )
        return False

    # decision.verdict == "approve" here, which run_t2_tier/
    # _t2_decision_from_model_verdict only ever construct when
    # safe_class_violation(...) was None — the safe-class gate has already
    # been consulted and passed by the time we reach this branch.
    merges_path = wiki_root / "_pending_merges.md"
    write_pending_merge(
        merges_path,
        merge_target_name=merge_target_name,
        sources=member_paths,
        rationale=rationale,
        draft_merged_body=draft_merged_body,
        confidence=confidence,
        write_kind=write_kind,
    )
    merge_id = _make_id(member_paths, merge_target_name)
    result = resolve_merge(
        merges_path,
        merge_id,
        "approve",
        note=f"T2 auto-finalized (safe class): {decision.reason}",
        wiki_root=wiki_root,
        auto_applied=True,
    )
    if not result.get("ok"):
        # Defense in depth: a resolve_merge failure (e.g. target_exists —
        # a slug collision that snuck past the athenaeum#421 precheck between
        # classification and this call) must NOT be silently swallowed as
        # a successful auto-apply. Fall through to the human queue: the
        # block written above is already there, unresolved, exactly as an
        # ordinary T1-pass-up-with-no-T2-approval proposal would be.
        log.warning(
            "resolutions: T2 auto-finalize failed for cluster %s (%s: %s); "
            "leaving proposal unresolved in the human queue",
            cluster_id,
            result.get("error_code"),
            result.get("message"),
        )
        return True  # already written to _pending_merges.md above; caller
        # must not write it again (it would be rejected as a dup id anyway,
        # since write_pending_merge is idempotent on id, but returning True
        # keeps the caller's control flow simple and avoids double-logging).

    log.info(
        "resolutions: T2 reasoning tier AUTO-APPLIED merge proposal for "
        "cluster %s (target=%s); bypassing the human queue",
        cluster_id,
        merge_target_name,
    )
    sample_tier_decision(
        wiki_root,
        tier=decision.tier,
        verdict=decision.verdict,
        proposal_id=decision.proposal_id,
        reason=decision.reason,
        config=config,
        applied=True,
    )
    return True
