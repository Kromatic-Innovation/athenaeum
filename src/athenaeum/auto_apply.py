# SPDX-License-Identifier: Apache-2.0
"""Composition layer: execute an AUTHORIZED reversible-verdict auto-apply
(issue athenaeum#716, closing the gap lane 716-C reported).

**The gap this module closes.** :mod:`athenaeum.verdict_effects`'s
``duplicate`` branch computes whether a fold may auto-apply
(``librarian.reversible_verdict_auto_apply_enabled`` on, plus a FRESH
verdict basis — :func:`athenaeum.verdicts.can_authorize_auto_operation`) but
cannot itself WRITE the fold: its own module docstring forbids importing
:mod:`athenaeum.pending_merges` (the only module that owns the fold-write
machinery) or taking any LLM client, and
:func:`athenaeum.pending_merges.resolve_merge` — the only fold-write entry
point — is shaped for a HUMAN approval (a mandatory ``confidence`` and a
draft body this module's sibling is banned from fabricating). Lane 716-C
found this honestly and stopped there rather than override either
constraint; this module is the missing piece, composing both without either
composed module importing the other — exactly how :mod:`athenaeum.decisions`
sits over :mod:`athenaeum.answers` / :mod:`athenaeum.pending_merges` /
:mod:`athenaeum.quarantine` / :mod:`athenaeum.retraction_cascade` /
:mod:`athenaeum.rule_proposals` without any of THEM importing each other.

**The merged body is the canonical side's own text, byte-for-byte — never
synthesized.** A ``duplicate`` verdict means the two sides are EQUIVALENT
content, not merely similar. Gate 2 already decided that; a human-shaped
fold normally still asks a reviewer to confirm it and supply a draft, but an
auto-applied fold has no reviewer to ask. The only choice that is both
faithful to a ``duplicate`` verdict and adds no synthesis of its own is to
reuse :mod:`athenaeum.verdict_effects`'s existing, deterministic
:func:`~athenaeum.verdict_effects._canonical_side` pick and pass that side's
OWN current full text (:attr:`athenaeum.comparator.ComparatorPage.text` —
frontmatter and body, unmodified) straight through as
``draft_merged_body``. Concatenating or stapling the two bodies together
(the athenaeum#658 finding D2 shape) would fabricate content neither source
actually states; synthesizing a fresh draft would need an LLM this module
is equally forbidden from calling. **Do not "improve" this into a
synthesized merge** — a `duplicate` verdict already means there is nothing
to synthesize; passing the canonical side's text through verbatim is the
only option that is both correct and auditable. Confidence for the
mandatory :func:`~athenaeum.pending_merges.write_pending_merge` field is
pinned to ``1.0`` for the same reason :mod:`athenaeum.name_collisions`
pins it for its own deterministic, unscreened, no-LLM fold proposals: the
certainty here is structural (a fresh verdict-ledger basis under an
operator-enabled gate), not a model-reported score, and
:mod:`athenaeum.verdict_effects`'s own module docstring already forbids
this general lineage of code from ever emitting a confidence-shaped
OUTPUT — this is the one MANDATORY INPUT field of a sidecar schema this
module does not own, filled with the same fixed sentinel its sibling
deterministic caller already uses, not a reintroduction of a confidence
scalar as a decision input.

**Reuses, does not reimplement.** The actual write goes through
:func:`athenaeum.pending_merges.write_pending_merge` +
:func:`athenaeum.pending_merges.resolve_merge` (``decision="approve"``,
``auto_applied=True``) — the SAME fold machinery a human approval or
:mod:`athenaeum.name_collisions`'s own deterministic auto-merge path uses:
the provenance-snapshot commit, alias accumulation, inbound-link rewrite,
tombstone stamping, vector purge, coordinate-widening check, and the
fold-graph invariant checks. ``auto_applied=True`` is the SAME marker
(issue athenaeum#602) :func:`athenaeum.reasoning_screens.t2_screen_merge_proposal`
and :func:`athenaeum.name_collisions.resolve_name_collisions` already use
for their own non-human-approved applies — reused here rather than a second
marker invented for this call site, per this issue's own instruction.
:func:`athenaeum.verdict_effects.apply_verdict_effect` and
:func:`athenaeum.verdict_effects._canonical_side` /
:func:`athenaeum.verdict_effects._duplicate_auto_apply_authorization` are
imported directly rather than re-derived — the same "import the sibling
module's private single-source-of-truth helper, documented" shape already
established in this codebase (:mod:`athenaeum.pending_merges` imports
:func:`athenaeum.comparator._widen_dimension`;
:mod:`athenaeum.reasoning_screens` imports
:func:`athenaeum.pending_merges._make_id`) — recomputing the two-gate
authorization check or the canonical-side tiebreak here would risk the two
call sites silently diverging, which the issue explicitly forbids ("do not
add a second freshness test").

**Fall-through, never a silent no-op.** Any verdict other than
``duplicate`` is passed straight through to
:func:`athenaeum.verdict_effects.apply_verdict_effect` unchanged — auto-
apply only ever arms the ``fold-on-duplicate`` operation
(:data:`athenaeum.verdict_effects.AUTO_APPLY_FOLD_ON_DUPLICATE`); nothing
outside :data:`athenaeum.verdict_effects.AUTO_APPLY_OPERATIONS` can reach
this module's write path, structurally — the check in
:func:`enact_verdict_effect` below happens before authorization is even
consulted. A ``duplicate`` verdict that is NOT authorized (gate off, no
ledger entry, or a stale basis), or for which the caller has no on-disk
path for one or both sides, also falls through unchanged to
:func:`~athenaeum.verdict_effects.apply_verdict_effect`'s existing
evidence-plus-queue behavior — the exact pre-this-issue behavior, byte for
byte, so every existing test of that function is untouched by this module's
existence. A source already tombstoned (defensive — not reachable from
today's live pipeline, which does not re-compare a folded page, but this
module's own "no silent no-op, no double-fold" discipline requires it fail
LOUDLY rather than let :func:`athenaeum.pending_merges.resolve_merge`'s own
``fold_source_already_tombstoned``/``fold_target_is_tombstone`` refusal be
the only signal) is refused the same way, named in the result.

Layering: L4 domain/pipeline, a peer of (not above, in the numbered sense)
:mod:`athenaeum.verdict_effects` and :mod:`athenaeum.pending_merges` —
same-layer imports are not upward edges
(``tests/test_layer_boundary.py`` only forbids ``importer_layer <
imported_layer``), and neither composed module imports this one or the
other, so :func:`tests.test_import_graph_acyclic.build_import_graph` gains
no cycle. Mirrors :mod:`athenaeum.decisions`'s own "L4 aggregates several
L4 modules" shape.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.comparator import VERDICT_DUPLICATE, ComparatorPage, CompareOutcome
from athenaeum.models import is_tombstone
from athenaeum.pending_merges import parse_pending_merges, resolve_merge, write_pending_merge
from athenaeum.verdict_effects import (
    EffectResult,
    _canonical_side,
    _duplicate_auto_apply_authorization,
    apply_verdict_effect,
)
from athenaeum.verdicts import make_pair_key

#: The :class:`~athenaeum.verdict_effects.EffectResult.action` value this
#: module stamps on a ``duplicate`` verdict whose fold was REALLY executed
#: (as opposed to ``"fold-proposal"``, the pre-existing evidence+queue
#: action every unauthorized/unavailable duplicate still gets).
AUTO_FOLD_EXECUTED_ACTION = "fold-auto-applied"


def _find_open_merge_id(merges_path: Path, sources: list[str], target_name: str) -> str | None:
    """Find the unresolved :class:`~athenaeum.pending_merges.PendingMerge` id
    matching *sources* + *target_name*.

    The identical lookup :func:`athenaeum.name_collisions._find_open_merge_id`
    performs for its own deterministic auto-merge path, duplicated here
    rather than cross-imported: neither module owns the other, and
    :mod:`athenaeum.pending_merges` (the module that actually would own a
    shared version) does not expose one today — promoting it there is a
    reasonable future cleanup, out of this issue's scope.
    """
    wanted_sources = sorted(sources)
    for pm in parse_pending_merges(merges_path):
        if (
            not pm.resolved
            and pm.merge_target_name == target_name
            and sorted(pm.sources) == wanted_sources
        ):
            return pm.id
    return None


def enact_verdict_effect(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    path_a: Path | None = None,
    path_b: Path | None = None,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    search_backend: str | None = None,
    embedding_model: str | None = None,
    now: datetime | None = None,
) -> EffectResult:
    """Enact *outcome* exactly like
    :func:`athenaeum.verdict_effects.apply_verdict_effect`, additionally
    EXECUTING an authorized ``duplicate`` fold rather than only recording
    that it could have run.

    This is the function the live pipeline should call in
    :func:`apply_verdict_effect`'s place (see
    :mod:`athenaeum.wiki_dedupe`'s wiring) — same positional/keyword shape
    for every parameter that function already takes, plus
    ``cache_dir``/``search_backend``/``embedding_model``, forwarded
    verbatim to :func:`athenaeum.pending_merges.resolve_merge`'s own
    same-named parameters for its optional vector-purge hygiene on a real
    fold (see that function's docstring) — ``None`` (the default) for all
    three is the same "skip the purge" behavior ``resolve_merge`` already
    defines.

    Returns the SAME :class:`~athenaeum.verdict_effects.EffectResult` shape
    :func:`apply_verdict_effect` returns in every case where auto-apply does
    not execute (see module docstring, "Fall-through, never a silent
    no-op"). When a ``duplicate`` fold really executes, returns a fresh
    :class:`EffectResult` with ``action=``:data:`AUTO_FOLD_EXECUTED_ACTION`,
    ``details`` carrying the same ``auto_apply_authorized`` /
    ``auto_apply_reason`` / ``canonical_side`` keys
    :func:`apply_verdict_effect` would have recorded plus
    ``folded_sources`` / ``aliases_added`` / ``links_rewritten`` /
    ``merge_id`` from the real fold.
    """
    wiki_root = Path(wiki_root)

    if outcome.verdict != VERDICT_DUPLICATE:
        # Auto-apply only ever arms fold-on-duplicate (module docstring) —
        # every other verdict is untouched by this module's existence.
        return apply_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            path_a=path_a,
            path_b=path_b,
            config=config,
            now=now,
        )

    pair_key = make_pair_key(page_a.id, page_b.id)
    authorized, reason = _duplicate_auto_apply_authorization(
        wiki_root=wiki_root, pair_key=pair_key, config=config
    )

    if not authorized or path_a is None or path_b is None:
        effect = apply_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            path_a=path_a,
            path_b=path_b,
            config=config,
            now=now,
        )
        if authorized:
            # Authorized, but this caller cannot name a real on-disk path
            # for one or both sides — there is no file to read a verbatim
            # body from or tombstone. Named loudly rather than silently
            # indistinguishable from "not authorized".
            details = dict(effect.details)
            details["auto_apply_blocked_reason"] = "no_on_disk_path_known"
            effect = dataclasses.replace(effect, details=details)
        return effect

    path_a, path_b = Path(path_a), Path(path_b)
    side, rule, _rows = _canonical_side(page_a, page_b, outcome)
    canonical_page, canonical_path = (page_a, path_a) if side == "a" else (page_b, path_b)
    other_page, other_path = (page_b, path_b) if side == "a" else (page_a, path_a)

    details = {
        "auto_apply_authorized": True,
        "auto_apply_reason": reason,
        "canonical_side": side,
        "canonical_id": canonical_page.id,
        "rule": rule,
    }

    if is_tombstone(canonical_page.meta) or is_tombstone(other_page.meta):
        details["auto_apply_blocked_reason"] = "source_already_tombstoned"
        return EffectResult(verdict=VERDICT_DUPLICATE, action="noop", details=details)

    merges_path = wiki_root / "_pending_merges.md"
    sources = [str(path_a), str(path_b)]
    target_name = canonical_path.stem

    write_pending_merge(
        merges_path,
        merge_target_name=target_name,
        sources=sources,
        rationale=(
            f"athenaeum#716 reversible-verdict auto-apply: duplicate verdict "
            f"{pair_key} authorized by a fresh basis in the issue athenaeum#712 "
            f"verdict ledger (librarian.reversible_verdict_auto_apply_enabled). "
            f"{rule}"
        ),
        draft_merged_body=canonical_page.text,
        confidence=1.0,
        write_kind=None,
    )
    merge_id = _find_open_merge_id(merges_path, sources, target_name)
    if merge_id is None:
        # Defensive — write_pending_merge just wrote this id by value; a
        # miss here means the id computation and this lookup have drifted
        # apart. Never silent: the pair is left queued (same as any other
        # unresolved proposal), loudly named.
        details["auto_apply_blocked_reason"] = "auto_fold_proposal_not_found"
        return EffectResult(
            verdict=VERDICT_DUPLICATE,
            action="noop",
            artifacts=[str(merges_path)],
            details=details,
        )

    result = resolve_merge(
        merges_path,
        merge_id,
        "approve",
        note="athenaeum#716 reversible-verdict auto-apply (fold-on-duplicate)",
        wiki_root=wiki_root,
        cache_dir=cache_dir,
        search_backend=search_backend,
        embedding_model=embedding_model,
        auto_applied=True,
    )
    if not result.get("ok"):
        details["auto_apply_blocked_reason"] = result.get("error_code") or "auto_fold_failed"
        details["auto_fold_error_message"] = result.get("message")
        return EffectResult(
            verdict=VERDICT_DUPLICATE,
            action="noop",
            queued=[pair_key],
            details=details,
        )

    details.update(
        {
            "merge_id": merge_id,
            "folded_sources": result.get("folded_sources"),
            "aliases_added": result.get("aliases_added"),
            "links_rewritten": result.get("links_rewritten"),
        }
    )
    return EffectResult(
        verdict=VERDICT_DUPLICATE,
        action=AUTO_FOLD_EXECUTED_ACTION,
        artifacts=[str(other_path)],
        queued=[],
        details=details,
    )


__all__ = [
    "AUTO_FOLD_EXECUTED_ACTION",
    "enact_verdict_effect",
]
