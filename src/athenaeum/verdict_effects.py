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
  body, never creates a merge proposal, never sets a conflict flag.
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
"""

from __future__ import annotations

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
from athenaeum.dimensions import DEFAULT_REGISTRY, coordinate_value
from athenaeum.models import EscalationItem, parse_frontmatter, render_frontmatter, slugify
from athenaeum.tiers import tier4_escalate
from athenaeum.verdicts import make_pair_key

#: Sub-directory of ``wiki_root`` where fold-adjudication evidence files are
#: written (issue athenaeum#715 / athenaeum#658 D2).
FOLD_EVIDENCE_DIRNAME = "_fold_evidence"

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
) -> None:
    """Append one framed item to ``<wiki_root>/_pending_questions.md``.

    See the module docstring, "Queue routing", for why ``tier4_escalate``
    over :mod:`athenaeum.pending_merges`.
    """
    item = EscalationItem(
        raw_ref=raw_ref,
        entity_name=entity_name,
        conflict_type=conflict_type,
        description=description,
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
    evidence_path = write_fold_evidence(page_a, page_b, outcome, wiki_root=wiki_root, now=now)
    side, reason, _rows = _canonical_side(page_a, page_b, outcome)
    canonical_id = page_a.id if side == "a" else page_b.id
    pair_key = make_pair_key(page_a.id, page_b.id)
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
    return EffectResult(
        verdict=VERDICT_DUPLICATE,
        action="fold-proposal",
        artifacts=[str(evidence_path)],
        queued=[pair_key],
        details={"canonical_side": side, "canonical_id": canonical_id, "rule": reason},
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
            conflict_type="ambiguous",
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
            conflict_type="ambiguous",
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
) -> EffectResult:
    pair_key = make_pair_key(page_a.id, page_b.id)
    request = build_coordinate_request(page_a, page_b, outcome)
    description = "\n".join(
        [
            request["question"],
            f'Side A: {request["sides"]["a"]["id"]} ("{request["sides"]["a"]["title"]}")',
            f'Side B: {request["sides"]["b"]["id"]} ("{request["sides"]["b"]["title"]}")',
            f"Missing dimensions: {', '.join(request['dimensions']) or '(none named)'}",
        ]
    )
    _queue(
        wiki_root,
        config=config,
        entity_name=f'"{_title(page_a)}" / "{_title(page_b)}"',
        conflict_type="ambiguous",
        raw_ref=f"comparator:{pair_key}",
        description=description,
    )
    return EffectResult(
        verdict=VERDICT_UNDERDETERMINED,
        action="queued",
        queued=[pair_key],
        details={"missing": list(outcome.missing), "request": request},
    )


# ---------------------------------------------------------------------------
# contradiction -> supersession, else queue
# ---------------------------------------------------------------------------


def _queue_contradiction(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
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
        conflict_type="principled",
        raw_ref=f"comparator:{pair_key}",
        description="\n".join(lines),
    )
    return EffectResult(
        verdict=VERDICT_CONTRADICTION,
        action="queued",
        queued=[pair_key],
        details=details,
    )


def _apply_contradiction(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
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
            config=config,
            details={"supersession_available": False},
        )

    decision = decide_supersession(
        page_a, page_b, outcome, wiki_root=wiki_root, config=config, now=now
    )
    if decision.action == SUPERSESSION_APPLIED:
        # Record only -- enactment belongs to athenaeum.supersession, never here.
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
        return _apply_underdetermined(page_a, page_b, outcome, wiki_root=wiki_root, config=config)
    return _apply_contradiction(
        page_a, page_b, outcome, wiki_root=wiki_root, config=config, now=now
    )


__all__ = [
    "FOLD_EVIDENCE_DIRNAME",
    "EffectResult",
    "apply_verdict_effect",
    "build_coordinate_request",
    "build_fold_evidence",
    "write_fold_evidence",
    "write_refines_declaration",
]
