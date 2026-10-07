# SPDX-License-Identifier: Apache-2.0
"""Storage-side effects of the five-verdict comparator (issue athenaeum#715, phase 2).

:mod:`athenaeum.comparator` DECIDES a verdict (``duplicate`` | ``contradiction``
| ``specialization`` | ``distinct`` | ``underdetermined``) but never enacts
anything beyond appending to the verdict ledger. This module is the next
layer down: given a decided :class:`~athenaeum.comparator.CompareOutcome`,
what does the corpus/queue actually DO about it? Five branches, one per
verdict, each returning an :class:`EffectResult` that says what happened —
never a silent no-op (see "No silent no-ops" below).

**Direct fix for two athenaeum#658 findings:**

- **D2 ("`**Draft**` is a mechanical staple, not a merged body").** Approving
  a stapled draft (the pre-existing :mod:`athenaeum.pending_merges` fold
  path) produced a page strictly worse than its sources — the draft was
  never actually synthesized, just concatenated. This module's
  ``duplicate`` branch does not repeat that mistake by going the other way
  and having an LLM draft a merged body either: :func:`apply_verdict_effect`
  takes NO LLM client parameter at all. Instead it produces an EVIDENCE
  artifact (:func:`build_fold_evidence` / :func:`write_fold_evidence`) — the
  overlapping passages side by side, a deterministically-chosen canonical
  side, and a coordinate-match table — for a HUMAN to adjudicate. Applying
  the fold (writing the actual merged page) is out of scope: a separate,
  future child of the memory-model v6 epic (athenaeum#709).
- **D3 (`reject` wrote a false `refines:`).** A fabricated directional claim
  — the resolver's ``reject`` action wrote ``refines:`` on a pair that was
  never adjudicated as general/specific. In this module ``refines:`` is
  RESERVED for the ``specialization`` verdict alone: it is the ONLY branch
  that ever calls :func:`write_refines_declaration`, and it only does so
  when the comparator itself named a ``specific_side`` — never guessed,
  never written as a rejection record, never written by any other verdict.

**Branch summary** (see each function's docstring for the full rationale):

- ``duplicate`` -> :func:`write_fold_evidence` (evidence, not a merged body)
  + queue the fold proposal for human approval.
- ``specialization`` -> :func:`write_refines_declaration` on the SPECIFIC
  side, naming the general one. ``specific_side is None`` (or its file path
  is unknown) queues instead of guessing.
- ``distinct`` -> ledger-only. Both pages are untouched; a breadcrumb
  naming the separating dimension(s) — including the synthetic
  ``content:coexist`` marker — is recorded in ``EffectResult.details``.
- ``underdetermined`` -> :func:`build_coordinate_request`, a SMALL,
  answerable question naming the missing dimension(s). Never embeds a page
  body, never creates a merge proposal, never sets a conflict flag. Issue
  athenaeum#1991: queueing is BATCHED, never per-pair — a caller iterating
  many pairs (:mod:`athenaeum.wiki_dedupe`) supplies a ``coordinate_sink``
  so this branch defers queueing; :func:`queue_coordinate_batch` is the
  only call site that ever appends an item, keyed to a batch ref, never a
  bare ``pair_key``.
- ``contradiction`` -> routed to :mod:`athenaeum.supersession` (a PARALLEL
  lane, imported lazily and defensively — see "Supersession is optional"
  below) when available and it can decide; otherwise queued with the
  LOCATED conflicting passages, never a page-global verdict.

**No confidence, no similarity, no LLM call, anywhere in this module.**
Issue athenaeum#715 bans confidence as a verdict INPUT; this module goes
further and never emits a confidence-shaped scalar as an OUTPUT either —
nothing in :class:`EffectResult`, :func:`build_fold_evidence`, or
:func:`build_coordinate_request` carries a numeric threshold or
model-reported score. The only numbers this module produces are plain
structural counts (e.g. how many widened dimensions a side's own coordinate
already matched) used purely for a deterministic tie-break, never as a
gate. There is no import of :mod:`athenaeum.provider`, ``anthropic``, or any
other LLM backend anywhere in this file — ``apply_verdict_effect`` does not
even accept a client parameter (contrast
:func:`athenaeum.comparator.compare_pages`, which requires one).

**Supersession is optional.** :mod:`athenaeum.supersession` is being built
by a parallel lane and does not exist in every checkout of this branch. The
``contradiction`` branch imports it LAZILY, inside the branch, guarded by
``try/except ImportError`` — never at module scope — so this module loads
and this module's own test suite runs whether or not that sibling module
has landed yet. When it is present and returns ``"applied"``, this module
only RECORDS that fact in ``EffectResult.details``; it never enacts the
supersession itself (that decision — and its own read/write of the
corpus — belongs entirely to :mod:`athenaeum.supersession`).

**Queue routing.** The queue child of epic athenaeum#709 has not landed yet,
so every branch that needs a human decision routes through the EXISTING
unified pending-decisions surface (:mod:`athenaeum.decisions` reads it;
:mod:`athenaeum.tiers`'s :func:`~athenaeum.tiers.tier4_escalate` writes it)
rather than inventing a second queue file. Concretely: an
:class:`~athenaeum.models.EscalationItem` appended to
``<wiki_root>/_pending_questions.md``. This was chosen over
:mod:`athenaeum.pending_merges` (``_pending_merges.md``) deliberately:
that writer's :func:`~athenaeum.pending_merges.write_pending_merge` requires
a mandatory ``confidence: float`` and a ``draft_merged_body`` — exactly the
two things this module is banned from fabricating (no confidence scalar, no
LLM-drafted body). ``tier4_escalate`` needs no LLM call for a plain
escalation (no ``proposal``, no ``config["resolve"]["auto_apply"]``) and is
already exercised offline by ``tests/test_answers.py``'s
``test_tier4_render_round_trips_through_parser``, so routing through it
keeps this module's own test suite offline too.

**All I/O stays under ``wiki_root``.** Every write this module performs —
the fold-evidence file, the pending-questions append, the ``refines:``
frontmatter edit on a caller-supplied page path — is parameterized by a
path the caller passes in. Nothing here reads ``Path.home()`` or otherwise
reaches for ``~/knowledge`` on its own.

**No silent no-ops.** A branch that cannot enact its effect — no
``specific_side``, an unknown file path, an unavailable supersession module
— always falls through to the queue path and records WHY in
``EffectResult.details``. There is no code path in this module that returns
a success-shaped :class:`EffectResult` having done nothing and said
nothing about it.

Layering: L4, a peer of :mod:`athenaeum.comparator` sitting one step closer
to storage. Consumes :class:`athenaeum.comparator.ComparatorPage` /
:class:`athenaeum.comparator.CompareOutcome` and the verdict-ledger identity
helpers (:mod:`athenaeum.verdicts`), and reuses
:mod:`athenaeum.atomic_io`'s atomic-write primitive and
:mod:`athenaeum.tiers`'s existing escalation writer rather than inventing
either. Does NOT import :mod:`athenaeum.pending_merges`,
:mod:`athenaeum.decisions`, or any LLM backend.

**Ported resolver actions (issue athenaeum#1680).** Alongside the five-verdict
dispatch above, this module also exposes
:func:`apply_suppress_or_attribute_both_effect` and
:func:`apply_propose_merge_effect` -- a narrow port of three of the
resolver's own ledger actions (``not_a_conflict`` / ``attribute_both`` /
``propose_merge``, see :mod:`athenaeum.resolutions`) onto this module's
:class:`EffectResult` shape, per the operator decision recorded in
athenaeum#1663's adjudication document and disposed in athenaeum#1680. The
first two may auto-apply under thresholds MIRRORED (not imported -- see
those functions' docstrings for why) from ``resolutions.py``'s own
per-action table; ``propose_merge`` always queues for a human at any
confidence, structurally (see :func:`apply_propose_merge_effect`'s
docstring) rather than merely by convention.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.comparator import (
    COEXIST_SEPARATOR,
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
    ComparatorPage,
    CompareOutcome,
)
from athenaeum.config import resolve_reversible_verdict_auto_apply_enabled
from athenaeum.dimensions import DEFAULT_REGISTRY, coordinate_value
from athenaeum.models import EscalationItem, parse_frontmatter, render_frontmatter, slugify
from athenaeum.tiers import tier4_escalate
from athenaeum.verdicts import can_authorize_auto_operation, lookup_pair, make_pair_key

#: Sub-directory of ``wiki_root`` where fold-adjudication evidence files are
#: written (issue athenaeum#715 / athenaeum#658 D2).
FOLD_EVIDENCE_DIRNAME = "_fold_evidence"

#: ``raw_ref`` prefix for a batched coordinate-request queue item (issue
#: athenaeum#1991). Deliberately NOT ``"comparator:"`` -- that prefix is
#: reserved for the single-pair key a lone call writes (see
#: :func:`queue_coordinate_batch`'s docstring); a reader that filters queue
#: items on this prefix can tell a batched item apart from a pre-athenaeum#1991
#: one without parsing the description.
COORDINATE_BATCH_PREFIX = "coordinate-batch"

#: Marker preceding the machine-parseable members JSON blob
#: :func:`queue_coordinate_batch` embeds in a batch's description (issue
#: athenaeum#1991 AC4) and :func:`parse_coordinate_batch_members` reads back.
_MEMBERS_MARKER = "athenaeum:coordinate-batch-members"

#: Issue athenaeum#716: the complete, enumerated set of operations this module
#: may ever AUTO-apply (write something to the corpus with no human in the
#: loop) rather than queue for one. Every member is REQUIRED to be
#: REVERSIBLE and gated on a FRESH verdict basis
#: (:func:`athenaeum.verdicts.can_authorize_auto_operation`) — see
#: :func:`athenaeum.config.resolve_reversible_verdict_auto_apply_enabled`'s
#: docstring for the full product rule this encodes, and
#: ``tests/test_verdict_effects_auto_apply.py`` for the test asserting the
#: dispatch path refuses anything outside this set.
#:
#: ``specialization-refines`` is enumerated here for completeness even though
#: its write has been UNCONDITIONAL (no gate, no freshness check) since issue
#: athenaeum#715 shipped the comparator — :func:`_apply_specialization` never
#: calls :func:`_check_auto_apply_operation` and
#: ``tests/test_verdict_effects.py::TestEF6SpecializationWritesRefines``
#: pins that pre-existing behavior; this issue does not change it.
#: ``supersession-marking`` is likewise enumerated for completeness: the
#: actual auto-apply DECISION for a ``contradiction`` belongs entirely to
#: :mod:`athenaeum.supersession` under issue athenaeum#715's own conditions —
#: this module only RECORDS what it decided (see :func:`_apply_contradiction`).
#: Only ``fold-on-duplicate`` is actually gated by this module today (see
#: :func:`_duplicate_auto_apply_authorization`).
AUTO_APPLY_FOLD_ON_DUPLICATE = "fold-on-duplicate"
AUTO_APPLY_SPECIALIZATION_REFINES = "specialization-refines"
AUTO_APPLY_SUPERSESSION_MARKING = "supersession-marking"

AUTO_APPLY_OPERATIONS: frozenset[str] = frozenset(
    {
        AUTO_APPLY_FOLD_ON_DUPLICATE,
        AUTO_APPLY_SPECIALIZATION_REFINES,
        AUTO_APPLY_SUPERSESSION_MARKING,
    }
)


def _check_auto_apply_operation(operation: str) -> None:
    """Raise :class:`ValueError` unless *operation* is one of
    :data:`AUTO_APPLY_OPERATIONS`.

    The single checkpoint every call site that is about to treat something as
    auto-appliable (rather than routing it to a human) passes through.
    Issue athenaeum#716 AC — "everything irreversible still routes to a
    human" — this is the structural half of that guarantee: a deliberate,
    reviewable frozenset any future auto-apply addition must edit by hand,
    plus a loud refusal for anything else. Matches this module's existing
    "no silent no-ops" rule (module docstring): an unrecognized operation is
    a caller bug, raised, never swallowed into a queued result or ignored.
    """
    if operation not in AUTO_APPLY_OPERATIONS:
        raise ValueError(
            f"{operation!r} is not an auto-appliable operation; only "
            f"{sorted(AUTO_APPLY_OPERATIONS)!r} may ever auto-apply — "
            "everything else (an undecided/queued contradiction, distinct, "
            "underdetermined, and every ported resolver action outside this "
            "set) routes to a human. No silent no-ops."
        )


def _duplicate_auto_apply_authorization(
    *, wiki_root: Path, pair_key: str, config: dict[str, Any] | None
) -> tuple[bool, str]:
    """Whether a ``duplicate`` verdict's fold may auto-apply right now.

    Two gates, both required (issue athenaeum#716): the operator opt-in
    (:func:`athenaeum.config.resolve_reversible_verdict_auto_apply_enabled`,
    default off) AND a FRESH verdict basis in the issue athenaeum#712 ledger
    (:func:`athenaeum.verdicts.can_authorize_auto_operation` — "a stale
    verdict cannot authorize a new automatic operation"; this function reuses
    it rather than writing a second freshness predicate). A pair with no
    verdict-ledger entry at all (the common case while
    ``librarian.verdict_ledger_enabled`` is off, or before issue athenaeum#712
    is wired to the comparator in a given deployment) fails CLOSED, same as
    an explicitly stale one — there is no fresh basis to point to either way.
    """
    _check_auto_apply_operation(AUTO_APPLY_FOLD_ON_DUPLICATE)
    if not resolve_reversible_verdict_auto_apply_enabled(config):
        return False, "auto_apply_disabled"
    entry = lookup_pair(wiki_root, pair_key)
    if entry is None:
        return False, "no_verdict_ledger_entry"
    if not can_authorize_auto_operation(entry):
        return False, "stale_verdict"
    return True, "authorized"

#: The five real comparator verdicts this module knows how to route. A
#: ``None`` verdict (Gate 2 was unavailable — see
#: :mod:`athenaeum.comparator`'s module docstring, "Offline / LLM-unavailable
#: Gate 2") or any other value is a caller error, not a branch to silently
#: absorb — see :func:`apply_verdict_effect`.
_KNOWN_VERDICTS = {
    VERDICT_DUPLICATE,
    VERDICT_CONTRADICTION,
    VERDICT_SPECIALIZATION,
    VERDICT_DISTINCT,
    VERDICT_UNDERDETERMINED,
}


@dataclass(frozen=True)
class EffectResult:
    """What :func:`apply_verdict_effect` did for one verdicted pair.

    ``action`` is a short machine token (e.g. ``"fold-proposal"``,
    ``"queued"``, ``"refines-written"``, ``"noop"``, ``"superseded"``) —
    never itself a confidence or similarity value. ``artifacts`` are paths
    this call WROTE to disk (evidence files, an edited page). ``queued`` are
    the titles/ids of items this call routed to the pending-decisions
    surface. ``details`` carries the verdict-specific facts (separator
    dimensions, the canonical-side rule, why a branch queued instead of
    enacting, etc.) — always non-empty when ``action`` is anything other
    than a clean primary enactment, per the "no silent no-ops" rule (see
    module docstring).
    """

    verdict: str
    action: str
    artifacts: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _title(page: ComparatorPage) -> str:
    """A short human-readable label for *page* — its frontmatter ``name:``,
    falling back to its id. Never the body (issue athenaeum#715's
    underdetermined branch explicitly forbids embedding page bodies in a
    queued item; other branches follow the same discipline for consistency)."""
    meta = page.meta if isinstance(page.meta, dict) else {}
    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return page.id


def _queue(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None,
    entity_name: str,
    conflict_type: str,
    raw_ref: str,
    description: str,
    decision_kind: str = "question",
) -> None:
    """Append one framed item to ``<wiki_root>/_pending_questions.md``.

    See the module docstring, "Queue routing", for why ``tier4_escalate``
    over :mod:`athenaeum.pending_merges``.

    ``decision_kind`` (issue athenaeum#1993, keyword-only, defaults to the
    pre-existing ``"question"``) is forwarded to
    :class:`athenaeum.models.EscalationItem` so :func:`tier4_escalate` tags
    the rendered block's ``**Decision kind**:`` line -- the signal
    :mod:`athenaeum.decisions` dispatches on
    (:func:`athenaeum.decisions.question_to_decision`) to give the item a
    ``type`` other than the generic ``"question"`` in the unified queue.
    Every OTHER caller in this module omits it and keeps writing a plain
    question item, unchanged.
    """
    item = EscalationItem(
        raw_ref=raw_ref,
        entity_name=entity_name,
        conflict_type=conflict_type,
        description=description,
        decision_kind=decision_kind,
    )
    tier4_escalate([item], wiki_root / "_pending_questions.md", config=config)


# ---------------------------------------------------------------------------
# duplicate -> fold evidence (never a merged body)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]


def _normalize_passage(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _shared_passages(body_a: str, body_b: str) -> list[tuple[str, str]]:
    """Structurally-overlapping sentences between two bodies — NO LLM.

    Splits each body into sentence-ish units, normalizes whitespace/case,
    and returns the pairs whose normalized form matches, in side-A order.
    This is deliberately dumb and mechanical (issue athenaeum#658 D2's whole
    point is that a fold's evidence must not be a synthesized artifact) —
    it will miss paraphrased overlap (that is Gate 2's job, already done,
    which is WHY this pair is ``duplicate``), it only surfaces the overlap a
    human can verify with their own eyes without re-reading both full
    bodies.
    """
    sentences_b = _sentences(body_b)
    norm_to_b: dict[str, str] = {}
    for s in sentences_b:
        norm_to_b.setdefault(_normalize_passage(s), s)
    seen: set[str] = set()
    shared: list[tuple[str, str]] = []
    for s in _sentences(body_a):
        norm = _normalize_passage(s)
        if norm in norm_to_b and norm not in seen:
            seen.add(norm)
            shared.append((s, norm_to_b[norm]))
    return shared


def _canonical_side(
    page_a: ComparatorPage, page_b: ComparatorPage, outcome: CompareOutcome
) -> tuple[str, str, list[tuple[str, Any, Any, Any]]]:
    """Deterministically pick which side is canonical for a ``duplicate`` fold.

    Rule (structural, not a model-scored value — see module docstring, "No
    confidence, no similarity"): for every dimension the comparator widened
    (:attr:`CompareOutcome.widened_coords`), check which side's OWN recorded
    coordinate already equalled the widened bound — that side did not need
    widening on this dimension, i.e. it was already the wider one. One point
    per dimension where exactly one side matches. The side with the most
    points is canonical. Ties break on the earlier ``recorded_at`` (the
    longer-standing page), then on the lexicographically smaller page id —
    both totally ordered and computable with no model call.

    Returns ``(side, human_readable_reason, per_dimension_rows)`` where each
    row is ``(dimension_name, side_a_raw, side_b_raw, widened)`` for the
    coordinate-match table.
    """
    score_a = 0
    score_b = 0
    rows: list[tuple[str, Any, Any, Any]] = []
    for name, widened in outcome.widened_coords.items():
        dim = DEFAULT_REGISTRY.get(name)
        if dim is None:
            continue
        raw_a = coordinate_value(dim, page_a.meta)
        raw_b = coordinate_value(dim, page_b.meta)
        a_matches = raw_a == widened
        b_matches = raw_b == widened
        if a_matches and not b_matches:
            score_a += 1
        elif b_matches and not a_matches:
            score_b += 1
        rows.append((name, raw_a, raw_b, widened))

    if score_a > score_b:
        side = "a"
    elif score_b > score_a:
        side = "b"
    else:
        rec_a = str((page_a.meta or {}).get("recorded_at") or "")
        rec_b = str((page_b.meta or {}).get("recorded_at") or "")
        if rec_a and rec_b and rec_a != rec_b:
            side = "a" if rec_a < rec_b else "b"
        else:
            side = "a" if page_a.id <= page_b.id else "b"

    reason = (
        f"Side A scored {score_a} wide-dimension point(s), side B scored "
        f"{score_b}, across {sorted(outcome.widened_coords)}."
    )
    if score_a == score_b:
        reason += " Tied on points -- broke the tie via recorded_at, then page id."
    return side, reason, rows


def build_fold_evidence(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    now: datetime | None = None,
) -> str:
    """Render the ``duplicate`` verdict's adjudication evidence as markdown.

    NEVER a merged body (issue athenaeum#658 D2) -- this is evidence for a
    human to read and decide from, not a draft to rubber-stamp. Contains:
    the structurally-overlapping passages side by side
    (:func:`_shared_passages`), the deterministically-chosen canonical side
    and the rule that chose it (:func:`_canonical_side`), and a coordinate
    match table (side A's raw value / side B's raw value / the widened
    value) for every dimension :attr:`CompareOutcome.widened_coords` names.
    Takes no LLM client -- there is nothing here for one to do.
    """
    now = now or datetime.now(timezone.utc)
    pair_key = make_pair_key(page_a.id, page_b.id)
    side, reason, rows = _canonical_side(page_a, page_b, outcome)
    canonical_id = page_a.id if side == "a" else page_b.id
    shared = _shared_passages(page_a.body, page_b.body)

    lines: list[str] = [
        f"# Fold Evidence -- {pair_key}",
        "",
        "Verdict: duplicate (issue athenaeum#715). This file is EVIDENCE for a "
        "human to adjudicate a fold, never a merged page body (issue "
        'athenaeum#658, finding D2: "**Draft** is a mechanical staple, not a '
        'merged body" -- approving a stapled draft produced a page strictly '
        "worse than its sources). Nothing below was written by an LLM; every "
        "field is computed structurally from the two pages' own frontmatter "
        "and bodies.",
        "",
        f"Generated: {now.isoformat()}",
        "",
        "## Pages",
        "",
        f'- Side A -- id `{page_a.id}`, title "{_title(page_a)}"',
        f'- Side B -- id `{page_b.id}`, title "{_title(page_b)}"',
        "",
        "## Canonical side",
        "",
        f"**Chosen**: side {side} (`{canonical_id}`)",
        "",
        "Rule (structural, not a model-scored value): for each dimension the "
        "comparator widened, the side whose OWN recorded coordinate already "
        "equalled the widened bound scores a point on that dimension; the "
        "side with more points is canonical. Ties break on the earlier "
        "`recorded_at`, then on the lexicographically smaller page id.",
        "",
        reason,
        "",
        "## Coordinate match table",
        "",
        "| Dimension | Side A | Side B | Widened |",
        "|---|---|---|---|",
    ]
    if rows:
        for name, raw_a, raw_b, widened in rows:
            lines.append(f"| {name} | {raw_a!r} | {raw_b!r} | {widened!r} |")
    else:
        lines.append("| (none -- outcome.widened_coords was empty) | | | |")
    lines += ["", "## Overlapping passages", ""]
    if shared:
        for a_text, b_text in shared:
            lines.append(f"- Side A: {a_text}")
            lines.append(f"  Side B: {b_text}")
    else:
        lines.append(
            "No structurally-shared sentence was found -- Gate 2's judged-cold "
            "content-relation call found these equivalent by paraphrase, not "
            "exact text overlap. A human should still read both bodies before "
            "approving the fold."
        )
    lines += [
        "",
        "## What this is not",
        "",
        "This file is not a merged body, and applying the fold (writing the "
        "actual combined page) is out of scope of this module -- a separate, "
        "future child of the memory-model v6 epic (athenaeum#709). A human "
        "decides the fold from the evidence above.",
        "",
    ]
    return "\n".join(lines)


def write_fold_evidence(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    now: datetime | None = None,
) -> Path:
    """Write :func:`build_fold_evidence`'s markdown to
    ``<wiki_root>/_fold_evidence/<pair-key>.md`` and return the path."""
    wiki_root = Path(wiki_root)
    pair_key = make_pair_key(page_a.id, page_b.id)
    text = build_fold_evidence(page_a, page_b, outcome, now=now)
    path = wiki_root / FOLD_EVIDENCE_DIRNAME / f"{pair_key}.md"
    atomic_write_text(path, text)
    return path


def _apply_duplicate(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None,
    now: datetime | None,
) -> EffectResult:
    pair_key = make_pair_key(page_a.id, page_b.id)
    auto_apply_authorized, auto_apply_reason = _duplicate_auto_apply_authorization(
        wiki_root=wiki_root, pair_key=pair_key, config=config
    )
    details: dict[str, Any] = {
        "auto_apply_authorized": auto_apply_authorized,
        "auto_apply_reason": auto_apply_reason,
    }
    if auto_apply_authorized:
        # Issue athenaeum#716 lane C cross-lane finding, recorded loudly rather
        # than papered over: authorization (gate on + a fresh verdict basis)
        # is the part THIS module can decide on its own. Actually WRITING the
        # fold (tombstoning the source, rewriting inbound links, ledgering a
        # reversible merge-provenance record) belongs to
        # :mod:`athenaeum.pending_merges`'s fold-into-existing path — a
        # concurrent sibling lane's surface for this same issue, and a module
        # this one's own docstring already states it does NOT import. That
        # module has no standalone entry point for a non-human-approved fold
        # today (only the ``PendingMerge`` human-approval flow via
        # :func:`athenaeum.pending_merges.resolve_merge`, which requires a
        # mandatory ``confidence``/draft body this module is banned from
        # fabricating — see the module docstring's "Queue routing"). Rather
        # than duplicate that write path's git-recoverability safety gate
        # here (a real regression risk if done hastily), this branch falls
        # back to the existing evidence+queue behavior and records the
        # authorization so the gap is visible in every call's own result
        # rather than hidden — see also the repair-debt instrumentation on
        # ``athenaeum status``, which reports this count honestly (today
        # always zero via THIS code path) rather than inflating it.
        details["auto_apply_blocked_reason"] = "fold_write_primitive_unavailable_to_this_module"
    evidence_path = write_fold_evidence(page_a, page_b, outcome, wiki_root=wiki_root, now=now)
    side, reason, _rows = _canonical_side(page_a, page_b, outcome)
    canonical_id = page_a.id if side == "a" else page_b.id
    title_a, title_b = _title(page_a), _title(page_b)
    description = (
        f'Approve folding "{title_a}" and "{title_b}" into one page? See the '
        f"structural overlap evidence at {evidence_path} -- no page body was "
        f"synthesized; a human writes the actual fold. Proposed canonical "
        f"side: {side} ({canonical_id})."
    )
    _queue(
        wiki_root,
        config=config,
        entity_name=f'"{title_a}" / "{title_b}"',
        conflict_type="duplicate",
        raw_ref=f"comparator:{pair_key}",
        description=description,
    )
    details.update({"canonical_side": side, "canonical_id": canonical_id, "rule": reason})
    return EffectResult(
        verdict=VERDICT_DUPLICATE,
        action="fold-proposal",
        artifacts=[str(evidence_path)],
        queued=[pair_key],
        details=details,
    )


# ---------------------------------------------------------------------------
# specialization -> refines: on the specific side
# ---------------------------------------------------------------------------


def write_refines_declaration(specific_path: Path, general_id: str) -> Path:
    """Append *general_id* to *specific_path*'s frontmatter ``refines:`` list.

    Reserved for the ``specialization`` verdict (issue athenaeum#658 D3: a
    prior code path wrote a false ``refines:`` as part of a REJECTION
    record -- a fabricated directional claim. This function is the only
    writer of ``refines:`` in this module and :func:`apply_verdict_effect`
    only ever calls it from the ``specialization`` branch, and only when the
    comparator itself named a ``specific_side``). Idempotent: a
    slug-equivalent entry already present is not duplicated.
    """
    specific_path = Path(specific_path)
    text = specific_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    raw = meta.get("refines")
    if isinstance(raw, list):
        existing = [str(r) for r in raw]
    elif isinstance(raw, str) and raw.strip():
        existing = [raw.strip()]
    else:
        existing = []
    if not any(slugify(str(r)) == slugify(general_id) for r in existing):
        existing.append(general_id)
    meta["refines"] = existing
    atomic_write_text(specific_path, render_frontmatter(meta) + body)
    return specific_path


def _apply_specialization(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    path_a: Path | None,
    path_b: Path | None,
    config: dict[str, Any] | None,
) -> EffectResult:
    pair_key = make_pair_key(page_a.id, page_b.id)
    title_a, title_b = _title(page_a), _title(page_b)

    if outcome.specific_side not in ("a", "b"):
        # No silent no-op (module docstring): the comparator could not tell
        # us which side is specific -- queue rather than guess.
        _queue(
            wiki_root,
            config=config,
            entity_name=f'"{title_a}" / "{title_b}"',
            conflict_type=outcome.conflict_type or "ambiguous",
            raw_ref=f"comparator:{pair_key}",
            description=(
                "Which side is the more specific claim? The comparator found "
                f"strict containment on {outcome.separator} but could not "
                "determine direction from the recorded coordinates.\n"
                f"Side A: {page_a.id}\nSide B: {page_b.id}"
            ),
        )
        return EffectResult(
            verdict=VERDICT_SPECIALIZATION,
            action="queued",
            queued=[pair_key],
            details={
                "reason": "no_specific_side_determined",
                "separator": list(outcome.separator),
            },
        )

    specific_path = path_a if outcome.specific_side == "a" else path_b
    general_id = page_b.id if outcome.specific_side == "a" else page_a.id

    if specific_path is None:
        _queue(
            wiki_root,
            config=config,
            entity_name=f'"{title_a}" / "{title_b}"',
            conflict_type=outcome.conflict_type or "ambiguous",
            raw_ref=f"comparator:{pair_key}",
            description=(
                f"Side {outcome.specific_side} is the more specific claim "
                f"(general: {general_id}) but its file path was not "
                "supplied to the effect layer, so `refines:` could not be "
                "written automatically -- please add it by hand."
            ),
        )
        return EffectResult(
            verdict=VERDICT_SPECIALIZATION,
            action="queued",
            queued=[pair_key],
            details={
                "reason": "specific_side_path_missing",
                "specific_side": outcome.specific_side,
                "general_id": general_id,
            },
        )

    written_path = write_refines_declaration(specific_path, general_id)
    return EffectResult(
        verdict=VERDICT_SPECIALIZATION,
        action="refines-written",
        artifacts=[str(written_path)],
        details={
            "specific_side": outcome.specific_side,
            "general_id": general_id,
            "specific_path": str(written_path),
        },
    )


# ---------------------------------------------------------------------------
# distinct -> ledger-only breadcrumb
# ---------------------------------------------------------------------------


def _apply_distinct(outcome: CompareOutcome) -> EffectResult:
    """``distinct`` writes nothing -- the ledger entry (already appended by
    :func:`athenaeum.comparator.record_comparison`) IS the record. This just
    breadcrumbs the separating dimension(s), including the synthetic
    ``content:coexist`` marker, into ``details`` for a caller that wants to
    explain the noop without re-reading the ledger."""
    return EffectResult(
        verdict=VERDICT_DISTINCT,
        action="noop",
        details={
            "separator": list(outcome.separator),
            "coexist": COEXIST_SEPARATOR in outcome.separator,
        },
    )


# ---------------------------------------------------------------------------
# underdetermined -> a small coordinate request
# ---------------------------------------------------------------------------


def build_coordinate_request(
    page_a: ComparatorPage, page_b: ComparatorPage, outcome: CompareOutcome
) -> dict[str, Any]:
    """A SMALL, answerable question for an ``underdetermined`` pair.

    Deliberately NOT an editorial adjudication over page bodies -- no body
    text is embedded, only a short id/title per side (issue athenaeum#715:
    the missing information is a coordinate, not a content judgement).
    Names :attr:`CompareOutcome.missing` explicitly so the human knows
    exactly which dimension(s) to supply.
    """
    dims = list(outcome.missing)
    question = (
        f"Do these two pages actually differ by {', '.join(dims)}, and if so which side is which?"
        if dims
        else "Do these two pages actually differ, and on what dimension?"
    )
    return {
        "kind": "coordinate-request",
        "pair": make_pair_key(page_a.id, page_b.id),
        "dimensions": dims,
        "sides": {
            "a": {"id": page_a.id, "title": _title(page_a)},
            "b": {"id": page_b.id, "title": _title(page_b)},
        },
        "question": question,
    }


def _apply_underdetermined(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None,
    coordinate_sink: list[dict[str, Any]] | None = None,
) -> EffectResult:
    """Build an ``underdetermined`` pair's coordinate request.

    Issue athenaeum#1991: this branch no longer queues a single-pair item
    directly keyed to ``pair_key`` -- that was the exact premise issue
    athenaeum#717's own survey found already false ("nothing queues
    per-pair"). What this branch does now depends on *coordinate_sink*:

    - **A sink is supplied** (the normal path: a caller iterating many
      pairs, e.g. :mod:`athenaeum.wiki_dedupe`'s per-cluster loop) -- the
      request is appended to the sink and NOTHING is queued yet. The
      caller owns flushing the sink through :func:`queue_coordinate_batch`
      (see that function's docstring for the per-claim/per-cluster
      aggregation it applies) once every pair in scope has been examined.
      Returned action is ``"coordinate-pending"`` -- a decided verdict
      with a deferred, not yet silently-dropped, effect.
    - **No sink** (a direct, single-pair caller -- existing tests, or any
      future caller that does not batch) -- this is still never a silent
      no-op (module docstring): the request is queued immediately as a
      batch-of-one via :func:`queue_coordinate_batch`, so the queue item is
      keyed to a stable batch ref, never to the bare ``pair_key`` string
      the old ``comparator:<pair_key>`` shape used.
    """
    pair_key = make_pair_key(page_a.id, page_b.id)
    request = build_coordinate_request(page_a, page_b, outcome)
    member = {
        "pair_key": pair_key,
        "request": request,
        "conflict_type": outcome.conflict_type or "ambiguous",
    }
    if coordinate_sink is not None:
        coordinate_sink.append(member)
        return EffectResult(
            verdict=VERDICT_UNDERDETERMINED,
            action="coordinate-pending",
            queued=[],
            details={
                "missing": list(outcome.missing),
                "request": request,
                "pair_key": pair_key,
                "batched": True,
            },
        )
    return queue_coordinate_batch([member], wiki_root=wiki_root, config=config)


def _batch_ref(pair_keys: list[str]) -> str:
    """Stable id for a coordinate batch, independent of member order.

    A hash (not a concatenation of the keys themselves) because a batch
    can cover many pairs -- the ref must stay short regardless of batch
    size, and it must be the SAME ref for the same set of pairs across
    runs (idempotent re-queue), never ``comparator:<pair_key>``.
    """
    digest = hashlib.sha1("|".join(sorted(pair_keys)).encode("utf-8")).hexdigest()[:12]
    return f"{COORDINATE_BATCH_PREFIX}:{digest}"


def queue_coordinate_batch(
    members: list[dict[str, Any]],
    *,
    wiki_root: Path,
    config: dict[str, Any] | None,
) -> EffectResult:
    """Queue ONE pending-decision item covering every member in *members*.

    Issue athenaeum#1991 AC3: this is the ONLY place left in this module
    that writes an ``underdetermined`` item to the queue, and it never
    constructs one keyed to a single ``pair_key`` -- the ``raw_ref`` is
    :func:`_batch_ref`, a hash of the WHOLE member set, prefixed
    :data:`COORDINATE_BATCH_PREFIX` rather than the retired
    ``"comparator:"`` single-pair prefix. A batch of one (the no-sink path
    in :func:`_apply_underdetermined`) still goes through this function,
    so there is exactly one code path that ever appends an underdetermined
    item, and it is this one.

    ``members`` is a list of ``{"pair_key", "request", "conflict_type"}``
    dicts, each the same shape :func:`_apply_underdetermined` builds. The
    per-cluster/per-claim GROUPING of members into one or more calls to
    this function is the caller's job (see
    :func:`athenaeum.wiki_dedupe._coordinate_batches_for_cluster`) -- this
    function itself always produces exactly one queue item per call.

    Per-member provenance (issue athenaeum#1991 AC4) is recorded BOTH in
    the returned :class:`EffectResult`'s ``details["members"]`` (for a
    same-process caller) AND in the queued description itself, as a
    machine-parseable JSON blob :func:`parse_coordinate_batch_members`
    reads back (for a later process that only has the on-disk block) --
    each entry carries its own ``pair`` and ``dimensions`` -- so a caller
    answering this batch later can stamp ``decided_by: human-batch:<ref>``
    per member (:func:`member_provenance_for_batch`) without re-deriving
    which pairs the batch covered. Writing that stamp
    to the verdict ledger's ``coord_origins`` (issue athenaeum#717's
    coord_origins blast radius) is out of this issue's scope -- the
    inbound coordinate-answer loop is a separate, blocked-by-this child
    (issue athenaeum#1993, issue athenaeum#1994) -- this function's job ends at making the
    batch ref a stable, answer-id-shaped reference (AC5) that slice can
    read back.
    """
    if not members:
        return EffectResult(
            verdict=VERDICT_UNDERDETERMINED,
            action="noop",
            details={"reason": "empty_batch"},
        )

    pair_keys = [m["pair_key"] for m in members]
    ref = _batch_ref(pair_keys)
    dims = sorted({d for m in members for d in m["request"]["dimensions"]})
    conflict_types = {m.get("conflict_type") or "ambiguous" for m in members}
    conflict_type = next(iter(conflict_types)) if len(conflict_types) == 1 else "ambiguous"

    plural = "pair" if len(members) == 1 else "pairs"
    dims_text = ", ".join(dims) or "an unnamed dimension"
    # No BLANK lines anywhere in this block: ``answers._parse_block``'s
    # ``**Description**:`` continuation window closes on the first blank
    # line (a blank line is a pure section terminator there), so a
    # multi-paragraph description would lose everything after its first
    # paragraph on round-trip through ``_pending_questions.md`` -- the
    # members list AND the machine-parseable marker below both depend on
    # staying inside that window.
    lines = [
        f"Do these {len(members)} {plural} actually differ by {dims_text}, "
        f"and if so which side is which for each? Missing dimension(s) "
        f"named across the batch: {dims_text}.",
        "Members:",
    ]
    for m in members:
        req = m["request"]
        lines.append(
            f'- {m["pair_key"]}: {req["question"]} '
            f'(A: {req["sides"]["a"]["id"]} "{req["sides"]["a"]["title"]}", '
            f'B: {req["sides"]["b"]["id"]} "{req["sides"]["b"]["title"]}")'
        )
    # Machine-parseable mirror of the bullet list above, so a caller that
    # only has the on-disk ``_pending_questions.md`` block (the inbound
    # coordinate-answer loop, issue athenaeum#1993, which runs in a later
    # process with no access to this call's in-memory EffectResult) can
    # still recover exactly which pairs this batch covers, for
    # :func:`member_provenance_for_batch`. An HTML comment so it renders
    # invisibly for a human reading the queue.
    members_json = json.dumps(
        [{"pair": m["pair_key"], "dimensions": m["request"]["dimensions"]} for m in members],
        sort_keys=True,
    )
    lines.append(f"<!-- {_MEMBERS_MARKER} {members_json} -->")
    description = "\n".join(lines)

    _queue(
        wiki_root,
        config=config,
        entity_name=f"coordinate batch: {len(members)} pair(s)",
        conflict_type=conflict_type,
        raw_ref=ref,
        description=description,
        # Issue athenaeum#1993: tag this item ``coordinate``, not the generic
        # ``question`` every other ``_queue`` call site still gets -- the
        # ONLY change needed here for `athenaeum.decisions` to give the
        # unified queue a real ``type: "coordinate"`` and for
        # `decision_framing.answerable_as("coordinate")` to route an answer
        # to the dedicated applier instead of the free-text question path.
        decision_kind="coordinate",
    )
    return EffectResult(
        verdict=VERDICT_UNDERDETERMINED,
        action="queued",
        queued=[ref],
        details={
            "batch_ref": ref,
            "members": [
                {"pair": m["pair_key"], "dimensions": m["request"]["dimensions"]}
                for m in members
            ],
        },
    )


def parse_coordinate_batch_members(description: str) -> list[dict[str, Any]]:
    """Recover a batch's member list from its queued block's description.

    Issue athenaeum#1991 AC4: the inbound coordinate-answer loop (issue
    athenaeum#1993) runs in a LATER process with no access to the
    in-memory :class:`EffectResult` :func:`queue_coordinate_batch` returned
    when the item was queued -- by the time a human answers it, only the
    on-disk ``_pending_questions.md`` block (a
    :class:`athenaeum.answers.PendingQuestion`'s ``description``) exists.
    This reads back the ``_MEMBERS_MARKER`` JSON blob that function embeds,
    so that process can still recover exactly which pairs the batch
    covers. Returns ``[]`` (never raises) when the marker is absent or the
    embedded JSON is malformed -- a hand-edited or pre-athenaeum#1991 block
    has no members to recover, not a crash.
    """
    marker = re.search(
        rf"<!--\s*{re.escape(_MEMBERS_MARKER)}\s*(\[.*?\])\s*-->", description, re.DOTALL
    )
    if marker is None:
        return []
    try:
        parsed = json.loads(marker.group(1))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [m for m in parsed if isinstance(m, dict) and "pair" in m]


def member_provenance_for_batch(
    members: list[dict[str, Any]], *, answer_ref: str
) -> list[dict[str, Any]]:
    """Per-member provenance stamps for a batch answer (issue athenaeum#1991 AC4).

    Pure function over a plain member list -- takes no action and writes
    nothing. *members* is either a :func:`queue_coordinate_batch` return
    value's ``details["members"]`` (same process) or
    :func:`parse_coordinate_batch_members`'s return value (recovered from
    disk in a later process) -- both share the same
    ``{"pair": ..., "dimensions": [...]}`` shape. Returns one dict per
    member: ``{"pair": <pair_key>, "decided_by": "human-batch:<answer_ref>",
    "dimensions": [...]}``.

    This is the handoff point for the inbound coordinate-answer loop
    (issue athenaeum#1993) and the ``coord_origins`` ledger wiring (issue
    athenaeum#1994) -- neither exists yet (issue athenaeum#717's 2026-10-06
    survey, group 4), so this function stops at computing the stamp a
    human-batch answer SHOULD apply per member; actually writing it to
    :class:`athenaeum.verdicts.VerdictEntry.basis.coord_origins` is that
    slice's own call to make, against the verdict ledger this module does
    not touch (module docstring, "No confidence ... no LLM call").
    """
    return [
        {
            "pair": m["pair"],
            "decided_by": f"human-batch:{answer_ref}",
            "dimensions": list(m.get("dimensions") or []),
        }
        for m in members
    ]


# ---------------------------------------------------------------------------
# contradiction -> supersession, else queue
# ---------------------------------------------------------------------------

# Mirrors athenaeum.merge.CONTRADICTION_STATUS_FLAGGED (merge.py:213) exactly.
# Not imported directly: merge.py is layer 4 (same as this module, per both
# modules' own docstrings) and owned by a parallel lane tonight -- importing
# it would drag its full transitive import set into this module's pinned
# import-budget set (tests/test_import_budget.py) for one string constant.
# tests/test_verdict_effects.py asserts these two literals stay equal.
CONTRADICTION_STATUS_FLAGGED = "contradiction-flagged"


def write_contested_flag(path: Path) -> Path:
    """Set BOTH contested-page trigger fields on *path*'s frontmatter.

    Issue athenaeum#1679 (§3.3): the recall header
    (``athenaeum.mcp_server``, ``mcp_server.py:736-740``) trips on EITHER
    ``status == "contradiction-flagged"`` or a truthy
    ``contradictions_detected`` -- this writes BOTH, matching the original
    C4 write shape (:func:`athenaeum.merge.render_merged_entry`, which always
    sets ``contradictions_detected`` and sets ``status`` alongside it when
    true) rather than the bare minimum needed to satisfy the OR by itself.
    Idempotent: re-running on an already-flagged page rewrites the same two
    values.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    meta["status"] = CONTRADICTION_STATUS_FLAGGED
    meta["contradictions_detected"] = True
    atomic_write_text(path, render_frontmatter(meta) + body)
    return path


def _queue_contradiction(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    path_a: Path | None = None,
    path_b: Path | None = None,
    config: dict[str, Any] | None,
    details: dict[str, Any],
) -> EffectResult:
    pair_key = make_pair_key(page_a.id, page_b.id)
    passages = list(outcome.conflicting_passages)
    blocked_by = details.get("blocked_by") or []
    lines = [
        f'Do "{_title(page_a)}" and "{_title(page_b)}" actually conflict, and '
        "if so which one supersedes the other?",
        f"Side A: {page_a.id}",
        f"Side B: {page_b.id}",
    ]
    if passages:
        lines.append("Located conflicting passages:")
        for p in passages[:2]:
            lines.append(f"- {p}")
    if blocked_by:
        lines.append(f"Blocked by: {', '.join(str(b) for b in blocked_by)}")
    _queue(
        wiki_root,
        config=config,
        entity_name=f'"{_title(page_a)}" / "{_title(page_b)}"',
        conflict_type=outcome.conflict_type or "principled",
        raw_ref=f"comparator:{pair_key}",
        description="\n".join(lines),
    )
    # Issue athenaeum#1679 (§3.3): flag BOTH real files this pair identifies,
    # when their paths are known -- an unresolved contradiction implicates
    # either side equally, so a caller that later reads EITHER page alone
    # (mcp_server's recall header) must see it as contested. No paths
    # supplied -> nothing to write; recorded either way, never a silent gap.
    contested_pages = [str(cp) for cp in (path_a, path_b) if cp is not None]
    for cp in (path_a, path_b):
        if cp is not None:
            write_contested_flag(cp)
    return EffectResult(
        verdict=VERDICT_CONTRADICTION,
        action="queued",
        queued=[pair_key],
        details={**details, "contested_pages": contested_pages},
    )


def _apply_contradiction(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    path_a: Path | None = None,
    path_b: Path | None = None,
    config: dict[str, Any] | None,
    now: datetime | None,
) -> EffectResult:
    """Route a ``contradiction`` to :mod:`athenaeum.supersession` when it is
    present and can decide; otherwise queue the LOCATED conflicting passages
    (never a page-global verdict) plus any ``blocked_by`` reasons.

    ``athenaeum.supersession`` is a PARALLEL lane's module and may not exist
    in this checkout -- imported lazily, inside this function, guarded by
    ``try/except ImportError`` (never at module scope) so this module's own
    import and test suite never depend on it landing first.
    """
    try:
        from athenaeum.supersession import SUPERSESSION_APPLIED, decide_supersession
    except ImportError:
        return _queue_contradiction(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            path_a=path_a,
            path_b=path_b,
            config=config,
            details={"supersession_available": False},
        )

    decision = decide_supersession(
        page_a, page_b, outcome, wiki_root=wiki_root, config=config, now=now
    )
    if decision.action == SUPERSESSION_APPLIED:
        # Record only -- enactment belongs to athenaeum.supersession, never
        # here, and the contested-page flag is deliberately NOT written on
        # this branch: a resolved (superseded) pair is no longer an open
        # conflict, so flagging it "contested" would misrepresent a decided
        # pair as still pending.
        return EffectResult(
            verdict=VERDICT_CONTRADICTION,
            action="superseded",
            details={
                "supersession_available": True,
                "winner_id": decision.winner_id,
                "loser_id": decision.loser_id,
                "located_passages": list(decision.located_passages or []),
                "conditions": list(decision.conditions or []),
                "reason": decision.reason,
            },
        )
    return _queue_contradiction(
        page_a,
        page_b,
        outcome,
        wiki_root=wiki_root,
        path_a=path_a,
        path_b=path_b,
        config=config,
        details={
            "supersession_available": True,
            "blocked_by": list(decision.blocked_by or []),
            "reason": decision.reason,
            "rate_limited": bool(decision.rate_limited),
        },
    )


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------


def apply_verdict_effect(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    path_a: Path | None = None,
    path_b: Path | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
    coordinate_sink: list[dict[str, Any]] | None = None,
) -> EffectResult:
    """Enact the storage-side effect of one decided :class:`CompareOutcome`.

    Dispatches on ``outcome.verdict`` to one of the five branches documented
    in the module docstring. ``path_a``/``path_b`` are the real on-disk
    paths for ``page_a``/``page_b`` when known -- only the ``specialization``
    branch needs them (to write ``refines:`` on the specific side's actual
    file); every other branch ignores them.

    Raises :class:`ValueError` when ``outcome.verdict`` is not one of the
    five real verdicts -- most importantly when it is ``None`` (Gate 2 was
    unavailable). This is a loud failure, not a silent no-op: a ``None``
    verdict means :func:`athenaeum.comparator.record_comparison` itself
    wrote nothing to the ledger, so there is nothing here to have an effect
    ABOUT yet; a caller that reaches this function with such an outcome has
    a bug to fix, not a branch this module should quietly absorb.

    ``coordinate_sink`` (issue athenaeum#1991, keyword-only, ``None``
    default) is forwarded verbatim to the ``underdetermined`` branch (see
    :func:`_apply_underdetermined`) and ignored by every other branch --
    only that verdict defers its queueing to a caller-owned batch.
    """
    if outcome.verdict not in _KNOWN_VERDICTS:
        raise ValueError(
            "apply_verdict_effect requires a decided verdict (one of "
            f"{sorted(_KNOWN_VERDICTS)!r}); got outcome.verdict="
            f"{outcome.verdict!r}. A None verdict means Gate 2 was "
            "unavailable and athenaeum.comparator itself ledgers nothing "
            "for it -- this module must not silently apply an effect either."
        )

    wiki_root = Path(wiki_root)
    if outcome.verdict == VERDICT_DUPLICATE:
        return _apply_duplicate(
            page_a, page_b, outcome, wiki_root=wiki_root, config=config, now=now
        )
    if outcome.verdict == VERDICT_SPECIALIZATION:
        return _apply_specialization(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            path_a=path_a,
            path_b=path_b,
            config=config,
        )
    if outcome.verdict == VERDICT_DISTINCT:
        return _apply_distinct(outcome)
    if outcome.verdict == VERDICT_UNDERDETERMINED:
        return _apply_underdetermined(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki_root,
            config=config,
            coordinate_sink=coordinate_sink,
        )
    return _apply_contradiction(
        page_a,
        page_b,
        outcome,
        wiki_root=wiki_root,
        path_a=path_a,
        path_b=path_b,
        config=config,
        now=now,
    )


# ---------------------------------------------------------------------------
# Ported resolver actions (issue athenaeum#1680): suppress (`not_a_conflict`)
# and `attribute_both` may auto-apply; `propose_merge` always queues for a
# human. These three are RESOLVER ACTIONS -- a namespace distinct from the
# five VERDICT_* comparator verdicts :func:`apply_verdict_effect` dispatches
# on above. A caller that has already attached one of these three actions to
# a comparator-driven pair (e.g. a resolver-shaped proposal riding alongside
# a ``duplicate``/``specialization`` outcome) enacts it through the two
# functions below instead of going through ``apply_verdict_effect``.
#
# Mirrored, not imported, from :mod:`athenaeum.resolutions`: that module
# transitively imports :mod:`athenaeum.provider` (the LLM backend), which
# this module's own docstring ("No confidence, no similarity, no LLM call,
# anywhere in this module") names explicitly as forbidden. Mirroring instead
# of importing keeps that guarantee intact; the values below are pinned
# equal to ``resolutions.py``'s own table by
# ``tests/test_verdict_effects_resolver_actions.py::
# TestResolverActionMirrorMatchesResolutions`` so the two cannot silently
# diverge.
# ---------------------------------------------------------------------------

#: Ledger action tokens. Mirrors ``athenaeum.resolutions.SUPPRESS_ACTION`` /
#: ``ATTRIBUTE_BOTH_ACTION`` / ``PROPOSE_MERGE_ACTION``.
RESOLVER_SUPPRESS_ACTION = "not_a_conflict"
RESOLVER_ATTRIBUTE_BOTH_ACTION = "attribute_both"
RESOLVER_PROPOSE_MERGE_ACTION = "propose_merge"

#: Mirrors ``athenaeum.resolutions.DEFAULT_AUTO_APPLY_THRESHOLD`` -- the
#: general auto-apply floor, documented here for parity even though both
#: ported auto-apply-capable actions below carry an explicit per-action
#: override rather than falling back to it.
RESOLVER_DEFAULT_AUTO_APPLY_THRESHOLD = 0.90

#: Mirrors the destructive 0.95 floor ``resolutions.py`` reserves for
#: ``correct_*``/``forget_*``. Neither ported action below reaches it --
#: kept here so a threshold-band test can assert the ``[0.90, 0.95)``
#: escalation semantics stay aligned with ``resolutions.py`` without
#: importing it, and so this port does not reclassify ``not_a_conflict``/
#: ``attribute_both`` onto the destructive band (operator decision, issue
#: athenaeum#1680).
RESOLVER_DESTRUCTIVE_AUTO_APPLY_THRESHOLD = 0.95

#: Per-action auto-apply floor for the two actions that may auto-apply.
#: Mirrors ``athenaeum.resolutions.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION``'s
#: entries for these same two keys: ``not_a_conflict`` sits BELOW the 0.90
#: default (0.75 -- issue athenaeum#170's "cheap to be wrong" rationale, a
#: false-suppress just re-detects next run) while ``attribute_both`` sits AT
#: the 0.90 default (a non-destructive marking verdict).
RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION: dict[str, float] = {
    RESOLVER_SUPPRESS_ACTION: 0.75,
    RESOLVER_ATTRIBUTE_BOTH_ACTION: 0.90,
}

#: Sentinel set of ported actions that never auto-apply, regardless of
#: confidence. Mirrors ``athenaeum.resolutions._NEVER_AUTO_APPLY_ACTIONS``.
#: :func:`apply_suppress_or_attribute_both_effect` checks this FIRST,
#: unconditionally, and refuses to run for a member of this set -- the
#: structural half of the guard. The other half is that
#: :func:`apply_propose_merge_effect` (the function that actually enacts
#: this action) has no ``confidence`` parameter on its signature at all, so
#: a future refactor cannot add a confidence-gated auto-apply branch to it
#: without first changing that signature -- a visible, reviewable diff --
#: rather than merely adding a comparison against this set. Same protection
#: ``resolutions.py:200-206``'s docstring describes for the resolver lane.
RESOLVER_NEVER_AUTO_APPLY_ACTIONS: frozenset[str] = frozenset({RESOLVER_PROPOSE_MERGE_ACTION})


def apply_suppress_or_attribute_both_effect(
    action: str,
    confidence: float,
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
) -> EffectResult:
    """Enact the ported ``not_a_conflict`` (suppress) or ``attribute_both``
    resolver action for a comparator-driven pair.

    ``confidence`` is the caller's own resolver-shaped proposal confidence
    (e.g. from a ``ResolutionProposal``) -- this module still never COMPUTES
    a confidence itself; the module docstring's "no confidence ... anywhere
    in this module" describes the five-verdict comparator path above, which
    never emits one, and remains true of that path. This function's
    ``confidence`` parameter belongs entirely to the ported action's own
    domain.

    At or above the per-action threshold (:data:`RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION`),
    returns an ``action="auto-applied"`` :class:`EffectResult` and enacts
    nothing further (mirrors the resolver lane: auto-apply here means "mark
    resolved", not "mutate a page" -- neither ``not_a_conflict`` nor
    ``attribute_both`` edits a page body). Below it, queues for a human via
    the same :func:`_queue` helper every other branch in this module uses.

    Raises :class:`ValueError` for ``propose_merge`` (use
    :func:`apply_propose_merge_effect`, which has no ``confidence``
    parameter to gate on) or any other unrecognized action -- not a silent
    no-op, matching this module's "no silent no-ops" discipline.
    """
    if action not in RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION:
        if action in RESOLVER_NEVER_AUTO_APPLY_ACTIONS:
            raise ValueError(
                f"{action!r} never auto-applies -- call apply_propose_merge_effect() "
                "instead, which has no confidence parameter to gate on."
            )
        raise ValueError(
            "apply_suppress_or_attribute_both_effect only knows "
            f"{sorted(RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION)!r}; got action={action!r}."
        )

    wiki_root = Path(wiki_root)
    threshold = RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION[action]
    pair_key = make_pair_key(page_a.id, page_b.id)
    title_a, title_b = _title(page_a), _title(page_b)

    if confidence >= threshold:
        return EffectResult(
            verdict=action,
            action="auto-applied",
            details={"resolver_action": action, "confidence": confidence, "threshold": threshold},
        )

    description = (
        f'Resolver proposed "{action}" for "{title_a}" / "{title_b}" at '
        f"confidence {confidence:.2f}, below the {threshold:.2f} auto-apply "
        "floor -- please confirm."
    )
    _queue(
        wiki_root,
        config=config,
        entity_name=f'"{title_a}" / "{title_b}"',
        conflict_type=action,
        raw_ref=f"comparator:{pair_key}",
        description=description,
    )
    return EffectResult(
        verdict=action,
        action="queued",
        queued=[pair_key],
        details={
            "resolver_action": action,
            "confidence": confidence,
            "threshold": threshold,
            "reason": "below_auto_apply_threshold",
        },
    )


def apply_propose_merge_effect(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
) -> EffectResult:
    """Enact the ported ``propose_merge`` resolver action -- ALWAYS queues
    for a human, at any confidence, unconditionally.

    Deliberately takes NO ``confidence`` parameter and constructs no
    ``draft_merged_body`` anywhere in its body: this is the structural half
    of the never-auto-apply guarantee (the other half is
    :data:`RESOLVER_NEVER_AUTO_APPLY_ACTIONS`, consulted by
    :func:`apply_suppress_or_attribute_both_effect`). A future refactor
    cannot add a confidence-gated auto-apply branch to THIS function
    without first changing its signature -- a visible, reviewable diff --
    so a merge proposal can never "slip past on confidence alone" the way
    ``resolutions.py:200-206``'s docstring warns against for the resolver
    lane. This is also why the comparator's own ``duplicate``/
    ``specialization`` verdicts are never handed a fabricated ``confidence``
    scalar or ``draft_merged_body`` here to drive an auto-finalize --
    exactly the athenaeum#658-D2 / athenaeum#715-banned anti-pattern
    :mod:`athenaeum.cluster_comparator`'s own module docstring records.
    """
    wiki_root = Path(wiki_root)
    pair_key = make_pair_key(page_a.id, page_b.id)
    title_a, title_b = _title(page_a), _title(page_b)
    description = (
        f'Resolver proposed merging "{title_a}" and "{title_b}" -- a human '
        "must draft and approve the merged body; propose_merge never "
        "auto-applies, at any confidence."
    )
    _queue(
        wiki_root,
        config=config,
        entity_name=f'"{title_a}" / "{title_b}"',
        conflict_type=RESOLVER_PROPOSE_MERGE_ACTION,
        raw_ref=f"comparator:{pair_key}",
        description=description,
    )
    return EffectResult(
        verdict=RESOLVER_PROPOSE_MERGE_ACTION,
        action="queued",
        queued=[pair_key],
        details={"resolver_action": RESOLVER_PROPOSE_MERGE_ACTION, "reason": "never_auto_apply"},
    )


__all__ = [
    "AUTO_APPLY_FOLD_ON_DUPLICATE",
    "AUTO_APPLY_OPERATIONS",
    "AUTO_APPLY_SPECIALIZATION_REFINES",
    "AUTO_APPLY_SUPERSESSION_MARKING",
    "CONTRADICTION_STATUS_FLAGGED",
    "FOLD_EVIDENCE_DIRNAME",
    "RESOLVER_ATTRIBUTE_BOTH_ACTION",
    "RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION",
    "RESOLVER_DEFAULT_AUTO_APPLY_THRESHOLD",
    "RESOLVER_DESTRUCTIVE_AUTO_APPLY_THRESHOLD",
    "RESOLVER_NEVER_AUTO_APPLY_ACTIONS",
    "RESOLVER_PROPOSE_MERGE_ACTION",
    "RESOLVER_SUPPRESS_ACTION",
    "COORDINATE_BATCH_PREFIX",
    "EffectResult",
    "apply_propose_merge_effect",
    "apply_suppress_or_attribute_both_effect",
    "apply_verdict_effect",
    "build_coordinate_request",
    "build_fold_evidence",
    "member_provenance_for_batch",
    "parse_coordinate_batch_members",
    "queue_coordinate_batch",
    "write_contested_flag",
    "write_fold_evidence",
    "write_refines_declaration",
]
