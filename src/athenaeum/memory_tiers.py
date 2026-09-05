# SPDX-License-Identifier: Apache-2.0
"""Retrieval-cost memory tiers + push budget/ranking (issue athenaeum#718, split (a)
of the athenaeum#911 memory-model v6 three-way split — see that issue's re-scope
comment for the (a)/(b)/(c) boundary; (b) off-corpus storage is athenaeum#984, (c)
erasure classification/taint is athenaeum#985).

**NOT to be confused with :mod:`athenaeum.tiers`** — that module is the
UNRELATED T0-T4 *entity-compilation* pipeline (tier1 programmatic matching /
tier2 LLM classify / tier3 LLM write / tier4 human escalation). This module
has nothing to do with compilation; it classifies an already-compiled wiki
page's *retrieval cost* — how eagerly it should be surfaced to a consuming
agent. Both modules cross-reference this warning in their own docstrings so
the collision cannot silently reappear.

## The four tiers

Retrieval-COST classes, not storage classes — every tier shares the same
disk (`docs/extending/whole-store-adapter-design.md` §8 governs storage-adapter
placement; this module never decides where bytes live):

- **hot** — indexed, eligible for unprompted push under the token budget
  (:data:`MEMORY_TIERS`), reachable by everything.
- **warm** — indexed, never pushed unprompted, reachable by explicit recall
  only (an ordinary `recall_search(..., unprompted=False)` call, today's
  default for every existing caller).
- **cold** — NOT indexed. Reuses the existing class+config
  :func:`athenaeum.storage.is_embedded` mechanism exactly as shipped by
  issue athenaeum#429/athenaeum#911 — **not** a new per-page flag. A page whose
  `type:` resolves to a non-embedded storage surface is cold; nothing in
  this module can move a page into or out of cold, since that is a
  class+config decision, not per-page metadata. See "Known gap" below.
- **refused** — never written at all. This module does not re-implement
  refusal; :func:`is_refused` is a thin, read-only bridge onto the already-
  shipped never-ingest gate (:mod:`athenaeum.never_ingest`, issue
  athenaeum#968) so a caller can ask "would this content have been refused"
  through the same module that answers every other tier question.

**Known gap, deliberately not closed here** (per the athenaeum#718 re-scope
comment 2): the issue's original text assumed a *per-page* `embedded: false`
override. The shipped mechanism is class+config only
(`storage.is_embedded(entity_class, config)`); a per-page override is net-new
work nominated to land once in athenaeum#716, with this issue as its second
consumer. Until then, `resolve_tier` can only ever report "cold" when the
page's whole `type:` class is configured non-embedded — never for one page
in isolation.

## Tier movement

Only the hot <-> warm edge is automatable by :func:`run_tier_sweep` — cold
requires an operator config change (see above) and refused claims are never
written, so neither is a sweep target. Movement is metadata (a
`memory_tier:` frontmatter scalar), reversible, and mostly automatic:

- **Demote by class default** — a claim carrying a tombstone-shaped signal
  (`superseded_by` set, or `deprecated: true` — issue athenaeum#191's existing
  vocabulary; there is no separate "completed-transitory decision" frontmatter
  field in this codebase, so a superseded/deprecated `decision`-class claim is
  the concrete realization of that AC phrase) demotes unconditionally,
  regardless of age or usage.
- **Demote by age without use** — a hot claim with no push/reference record
  at all (:func:`athenaeum.usage_report.get_claim_usage` returns `None`, or
  `pushed_count == 0`) whose `updated`/`created` timestamp is older than
  :func:`athenaeum.config.resolve_memory_tier_demote_after_days`.
- **Demote by measured recall precision** — a hot claim that HAS been pushed
  but never referenced (`pushed_count > 0`, `referenced_count == 0`), whose
  `last_pushed` is older than the same threshold. Pushed-but-never-used sinks.
- **Promote on use** — a warm claim with `referenced_count > 0` promotes back
  to hot; an agent found it useful despite it not being in the push pool.
- **Promote on human pin** — an explicit `memory_tier: hot` frontmatter value
  is read as-is by :func:`resolve_tier` (and left alone by the sweep, which
  only ever moves TOWARD what `resolve_tier` already reports as current).

**The `axiom` class never demotes without its governance ledger.**
:func:`run_tier_sweep` refuses to auto-demote (or auto-promote) any page
whose `memory_class == "axiom"` outright — see :func:`evaluate_tier_movement`
and the `skipped_axiom` counter on :class:`TierSweepReport`. The ONLY path
that can move an axiom-class page's tier is :func:`demote_axiom_tier`, which
requires a human-supplied reason/by and records the demotion into
:mod:`athenaeum.axiom_governance`'s ledger (`_axiom_governance.jsonl`)
*before* touching the page's `memory_tier:` field — structurally, no axiom
tier change can happen without a matching governance-ledger row.

## Push selection

**Push selection = relevance x tier x coordinate fit**
(:func:`push_score`). Only a *hot*-tier hit ever has a nonzero
:func:`tier_weight` — warm/cold/refused are excluded from an unprompted push
by construction, matching "warm: explicit recall only." Coordinate fit
(:func:`coordinate_fit_weight`) rewards a claim whose `claimed_scope`
CONTAINS (or is contained by — :mod:`athenaeum.dimensions`' relation
vocabulary is deliberately undirected, see its module docstring) the
session's scope over a sibling (DISJOINT) scope claim.

**The push budget is ONE documented config key, tokens per turn**
(`push_budget.tokens_per_turn`, :func:`athenaeum.config.resolve_push_token_budget`)
— deliberately the only push-budget dial. :func:`select_for_push` enforces
it at the boundary: candidates are ranked by :func:`push_score` descending,
then greedily included while the running token total stays within budget; a
candidate that would push the total over budget is skipped (not truncated),
never included partially.

Relevance itself, and the fail-closed filtering of superseded/expired/
unauthorized claims, are untouched by this module — both already live in
:mod:`athenaeum.search` (`_is_recall_inactive`) and
:mod:`athenaeum.mcp_server` (Layer B/C audience + `recallable` drops); this
module only re-weights and re-ranks what those layers already produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athenaeum.authority import AuthorityManifest
    from athenaeum.usage_report import ClaimUsage

#: The four retrieval-cost tiers (issue athenaeum#718). See the module
#: docstring's "The four tiers" section.
MEMORY_TIERS: frozenset[str] = frozenset({"hot", "warm", "cold", "refused"})

#: The subset of :data:`MEMORY_TIERS` a `memory_tier:` frontmatter value may
#: directly set. `cold` is class+config-derived (never per-page, see the
#: module docstring's "Known gap") and `refused` content is never written at
#: all, so neither is ever a literal frontmatter value.
SETTABLE_TIERS: frozenset[str] = frozenset({"hot", "warm"})

#: Class-default starting tier, consulted only when a page carries no
#: explicit `memory_tier:` value. `axiom`/`guideline`/`decision` default hot
#: (bedrock/policy/active-decision content is exactly what unprompted push
#: exists for); the rest default warm. Deliberately a plain dict, not a
#: frozen mapping type — mirrors :data:`athenaeum.memory_class.TYPE_TO_MEMORY_CLASS`'s
#: shape.
DEFAULT_TIER_BY_MEMORY_CLASS: dict[str, str] = {
    "axiom": "hot",
    "guideline": "hot",
    "decision": "hot",
    "fact": "warm",
    "reference": "warm",
    "entity": "warm",
    "procedure": "warm",
}

#: Fallback default for an unrecognized/absent `memory_class` — warm, the
#: less eager choice (see :data:`DEFAULT_TIER_BY_MEMORY_CLASS`'s docstring:
#: unprompted push is the "expensive and noisy" side, so an unclassified
#: claim starts on the conservative side of that line).
_FALLBACK_DEFAULT_TIER = "warm"

#: Push-selection tier weight. Only `hot` is nonzero — this is what makes
#: "warm: explicit recall only" true for the unprompted-push path: a warm
#: (or cold/refused, neither of which can appear as a recall hit anyway)
#: candidate's :func:`push_score` is always exactly 0.
TIER_WEIGHTS: dict[str, float] = {
    "hot": 1.0,
    "warm": 0.0,
    "cold": 0.0,
    "refused": 0.0,
}

#: Coordinate-fit weight per :class:`athenaeum.dimensions.Relation` value.
#: CONTAINS outranks EQUAL (a broader-scope claim generalizes further, so it
#: is favored over an exact-scope duplicate when both otherwise tie) which
#: outranks OVERLAPS/UNKNOWN which outranks DISJOINT (sibling scopes — the
#: AC's explicit "outranks a sibling-scope claim" case). `None` (no session
#: scope supplied, or the page carries no `claimed_scope`) is neutral: it
#: neither rewards nor penalizes, so a caller that never passes
#: `session_scope` gets pure relevance x tier ranking, byte-identical to
#: this module not existing.
COORDINATE_FIT_WEIGHTS: dict[str | None, float] = {
    "contains": 1.25,
    "equal": 1.0,
    "overlaps": 0.85,
    "unknown": 0.75,
    "disjoint": 0.6,
    None: 1.0,
}


def tier_weight(tier: str) -> float:
    """Push-selection weight for *tier*. Unrecognized values weight 0 (fail closed)."""
    return TIER_WEIGHTS.get(tier, 0.0)


def coordinate_fit_weight(relation: str | None) -> float:
    """Push-selection weight for a :mod:`athenaeum.dimensions` *relation* value
    (or `None` — no scope information available)."""
    return COORDINATE_FIT_WEIGHTS.get(relation, COORDINATE_FIT_WEIGHTS[None])


def push_score(relevance: float, tier: str, scope_relation: str | None) -> float:
    """Push selection formula: relevance x tier x coordinate fit (issue athenaeum#718 AC).

    A non-hot tier always scores exactly 0, regardless of relevance or
    coordinate fit — see :data:`TIER_WEIGHTS`.
    """
    return relevance * tier_weight(tier) * coordinate_fit_weight(scope_relation)


def resolve_tier(fm: dict[str, Any] | None, *, config: dict[str, Any] | None = None) -> str:
    """Resolve the effective retrieval-cost tier for one page's frontmatter.

    Order of resolution:

    1. **Cold** — `storage.is_embedded(type, config)` is `False`. Checked
       first because it is class+config authoritative and cannot be
       overridden by a per-page `memory_tier:` value (see the module
       docstring's "Known gap" — there is no per-page override to check).
    2. **Explicit pin** — an on-page `memory_tier:` value in
       :data:`SETTABLE_TIERS` (`hot`/`warm`) is returned as-is (human pin,
       or a value the sweep itself previously wrote).
    3. **Class default** — :data:`DEFAULT_TIER_BY_MEMORY_CLASS` keyed by
       `memory_class` (explicit frontmatter value, else derived from
       `type:` via :func:`athenaeum.memory_class.memory_class_for_type`),
       falling back to :data:`_FALLBACK_DEFAULT_TIER` for an unrecognized
       or absent class.

    Never raises — an empty/`None` *fm* resolves to the fallback default,
    same posture as every other fail-open frontmatter reader in this
    codebase (see :mod:`athenaeum.schemas`'s module docstring).
    """
    from athenaeum.storage import is_embedded

    meta = fm if isinstance(fm, dict) else {}
    entity_type = meta.get("type")
    if not is_embedded(entity_type if isinstance(entity_type, str) else None, config):
        return "cold"

    explicit = meta.get("memory_tier")
    if isinstance(explicit, str) and explicit.strip() in SETTABLE_TIERS:
        return explicit.strip()

    memory_class = meta.get("memory_class")
    if not isinstance(memory_class, str) or not memory_class.strip():
        from athenaeum.memory_class import memory_class_for_type

        memory_class = memory_class_for_type(entity_type)

    if isinstance(memory_class, str):
        return DEFAULT_TIER_BY_MEMORY_CLASS.get(memory_class, _FALLBACK_DEFAULT_TIER)
    return _FALLBACK_DEFAULT_TIER


def scope_relation(fm: dict[str, Any] | None, session_scope: str | None) -> str | None:
    """Compare a page's `claimed_scope` (issue athenaeum#714's `scope` dimension)
    against *session_scope*. Returns `None` when either side is absent — "no
    scope information," never a fabricated relation.

    Reads the page's coordinate through
    :func:`athenaeum.dimensions.coordinate_value` (never `fm.get("claimed_scope")`
    raw) so the frontmatter-key <-> dimension-name binding stays owned by
    that one function, per `dimensions.py`'s own write-discipline note.
    """
    if not session_scope or not isinstance(fm, dict):
        return None
    from athenaeum.dimensions import SCOPE, compare_hierarchy, coordinate_value

    page_scope = coordinate_value(SCOPE, fm)
    if not isinstance(page_scope, str) or not page_scope.strip():
        return None
    return compare_hierarchy(page_scope, session_scope)


def tier_scope_header_line(tier: str, relation: str | None) -> str:
    """Render the recall hit header's tier + matched-scope segment (issue athenaeum#718 AC).

    Always includes the tier (the "why was this pushed/shown" signal); the
    scope half only appears when a relation was actually computed (i.e. a
    `session_scope` was supplied to the call AND the page carries a
    `claimed_scope`) — omit-at-default, matching every other optional
    segment on this header (see `mcp_server._recall_metadata_lines`).
    """
    if relation is None:
        return f"**Tier:** {tier}"
    return f"**Tier:** {tier} · **Scope:** {relation}"


def is_refused(
    meta: dict[str, Any] | None,
    body: str,
    *,
    manifest: "AuthorityManifest",
) -> bool:
    """Would *meta*/*body* be refused at ingestion (the "refused" tier)?

    A thin, read-only bridge onto the already-shipped never-ingest gate
    (:mod:`athenaeum.never_ingest`, issue athenaeum#968) — this module does not
    reimplement refusal classification, only exposes it through the same
    surface that answers every other tier question. `False` when the
    manifest declares no never-ingest classes (dark by default, same as the
    underlying gate).
    """
    from athenaeum.never_ingest import classify_never_ingest

    return classify_never_ingest(meta, body, manifest=manifest) is not None


# ---------------------------------------------------------------------------
# Push selection (token budget enforcement)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushCandidate:
    """One recall hit's inputs to the push-selection formula.

    *key* is an opaque caller-defined identifier (e.g. a list index) used
    only to report which candidates were selected, in ranked order —
    :func:`select_for_push` never inspects it.
    """

    key: Any
    relevance: float
    tier: str
    scope_relation: str | None
    tokens: int


def select_for_push(
    candidates: list[PushCandidate], *, token_budget: int
) -> list[Any]:
    """Rank *candidates* by :func:`push_score` and select within *token_budget*.

    Enforced at the boundary: candidates are visited in descending
    push-score order (ties broken by original input order, for
    determinism); a candidate is included and its token cost added to the
    running total ONLY if doing so keeps the total `<= token_budget`.
    A candidate that would exceed the budget is skipped (never truncated,
    never included partially) — later, smaller candidates are still
    considered, so the budget is packed rather than cut off at the first
    miss. A `push_score` of exactly 0 (any non-hot tier) is never selected,
    regardless of remaining budget.

    Returns the selected candidates' `key`s, in the order they were
    selected (highest push_score first) — the order a caller should render
    them in.
    """
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (
            -push_score(pair[1].relevance, pair[1].tier, pair[1].scope_relation),
            pair[0],
        ),
    )
    selected: list[Any] = []
    total_tokens = 0
    budget = max(0, token_budget)
    for _input_index, candidate in ranked:
        if push_score(candidate.relevance, candidate.tier, candidate.scope_relation) <= 0:
            continue
        if total_tokens + max(0, candidate.tokens) > budget:
            continue
        selected.append(candidate.key)
        total_tokens += max(0, candidate.tokens)
    return selected


# ---------------------------------------------------------------------------
# Tier movement (automatic sweep)
# ---------------------------------------------------------------------------

#: Ledger filename, alongside `_decay_sweep_records.jsonl` /
#: `_axiom_governance.jsonl` under `wiki/`.
TIER_SWEEP_LEDGER_FILENAME = "_tier_sweep_records.jsonl"

#: Schema version stamped on every ledger record.
TIER_SWEEP_LEDGER_VERSION = 1


@dataclass(frozen=True)
class TierChange:
    """One page's proposed or applied tier movement."""

    path: Path
    old_tier: str
    new_tier: str
    reason: str


@dataclass
class TierSweepReport:
    """Outcome of one :func:`run_tier_sweep` call."""

    scanned: int = 0
    changed: list[TierChange] = field(default_factory=list)
    skipped_axiom: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "changed": [
                {
                    "path": str(c.path),
                    "old_tier": c.old_tier,
                    "new_tier": c.new_tier,
                    "reason": c.reason,
                }
                for c in self.changed
            ],
            "skipped_axiom": self.skipped_axiom,
            "errors": list(self.errors),
        }


def _parse_iso_ts(raw: Any) -> datetime | None:
    """Mirrors :func:`athenaeum.usage_report._parse_ts`'s contract exactly — a
    small, stable helper duplicated rather than imported private, the same
    convention that module and :mod:`athenaeum.never_ingest` both follow."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _demotion_trigger_reason(fm: dict[str, Any]) -> str | None:
    """Class-default demotion trigger: a tombstone-shaped signal (issue
    athenaeum#191's existing `superseded_by`/`deprecated` vocabulary — see the
    module docstring's "Demote by class default" note for why this, and not
    a separate "completed-transitory decision" field, is the concrete
    realization of that AC phrase)."""
    from athenaeum.models import parse_deprecated, parse_superseded_by

    if parse_superseded_by(fm):
        return "class-default: superseded"
    if parse_deprecated(fm):
        return "class-default: deprecated"
    return None


def evaluate_tier_movement(
    fm: dict[str, Any],
    *,
    usage: "ClaimUsage | None",
    now: datetime,
    demote_after_days: int,
    config: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Decide whether one page's tier should move. Returns `(new_tier, reason)`,
    or `(None, None)` when no movement applies.

    **The `axiom` class never demotes (or promotes) automatically** — see the
    module docstring. `memory_class == "axiom"` always returns `(None, None)`
    here; the only path that can move an axiom's tier is
    :func:`demote_axiom_tier`.

    Only the hot <-> warm edge is evaluated; a page currently resolving
    "cold" is a class+config decision this function cannot act on (see the
    module docstring's "Known gap"), so it is always a no-op here too.
    """
    if fm.get("memory_class") == "axiom":
        return (None, None)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    current = resolve_tier(fm, config=config)
    if current not in ("hot", "warm"):
        return (None, None)

    if current == "hot":
        trigger = _demotion_trigger_reason(fm)
        if trigger is not None:
            return ("warm", trigger)

        updated_ts = _parse_iso_ts(fm.get("updated")) or _parse_iso_ts(fm.get("created"))
        age_days = (now - updated_ts).total_seconds() / 86400 if updated_ts else None

        if (usage is None or usage.pushed_count == 0) and age_days is not None:
            if age_days > demote_after_days:
                return ("warm", "age-without-use")
            return (None, None)

        if usage is not None and usage.pushed_count > 0 and usage.referenced_count == 0:
            last_pushed_ts = _parse_iso_ts(usage.last_pushed)
            if last_pushed_ts is not None:
                pushed_age_days = (now - last_pushed_ts).total_seconds() / 86400
                if pushed_age_days > demote_after_days:
                    return ("warm", "pushed-but-never-used")
        return (None, None)

    # current == "warm": promote on use.
    if usage is not None and usage.referenced_count > 0:
        return ("hot", "promote-on-use")
    return (None, None)


#: Mirrors :data:`athenaeum.memory_class_backfill._FRONTMATTER_RE` exactly —
#: both modules need match spans `athenaeum.models.render_frontmatter`'s
#: round-trip does not preserve, so each keeps its own copy rather than
#: importing a private name (same duplicate-a-small-stable-helper convention
#: :mod:`athenaeum.usage_report` and :mod:`athenaeum.never_ingest` follow).
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)
_MEMORY_TIER_LINE_RE = re.compile(r"(?m)^memory_tier:\s*.*$")


def set_memory_tier_text(text: str, memory_tier: str) -> str | None:
    """Return *text* with `memory_tier:` set to *memory_tier* in its frontmatter.

    Mirrors :func:`athenaeum.memory_class_backfill.insert_memory_class`'s
    textual-insertion contract exactly (same regex shape, same no-frontmatter
    -> `None` contract, same byte-level no-op on a second identical write) —
    but additionally UPDATES an existing `memory_tier:` line in place rather
    than only ever appending, since tier movement (unlike the one-shot
    `memory_class` backfill) may run repeatedly against the same page.
    Returns `None` when *text* has no frontmatter block — the caller must
    skip, never synthesize one.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    fm_body = match.group(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"

    line_match = _MEMORY_TIER_LINE_RE.search(fm_body)
    if line_match is not None:
        new_fm_body = (
            fm_body[: line_match.start()]
            + f"memory_tier: {memory_tier}"
            + fm_body[line_match.end() :]
        )
        return f"{text[: match.start(1)]}{new_fm_body}{text[match.end(1) :]}"

    end = match.end(1)
    return f"{text[:end]}{newline}memory_tier: {memory_tier}{text[end:]}"


def _tier_sweep_ledger_path(cache_dir: Path | None) -> Path:
    from athenaeum.config import resolve_cache_dir

    return resolve_cache_dir(cache_dir) / TIER_SWEEP_LEDGER_FILENAME


def _append_tier_sweep_ledger(change: TierChange, *, cache_dir: Path | None, swept_at: str) -> None:
    """Best-effort ledger append (mirrors :mod:`athenaeum.push_metrics`'s
    posture, not :mod:`athenaeum.decay_sweep`'s fail-closed one — tier
    movement is non-destructive metadata, unlike a `git rm` archival, so a
    ledger-write failure here logs and continues rather than aborting the
    sweep)."""
    import json
    import logging

    from athenaeum.store import append_line_durable

    log = logging.getLogger(__name__)
    record = {
        "v": TIER_SWEEP_LEDGER_VERSION,
        "path": str(change.path),
        "old_tier": change.old_tier,
        "new_tier": change.new_tier,
        "reason": change.reason,
        "swept_at": swept_at,
    }
    try:
        append_line_durable(
            _tier_sweep_ledger_path(cache_dir),
            (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
        )
    except OSError:
        log.warning("memory-tiers: sweep-ledger append failed for %s", change.path, exc_info=True)


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every non-underscore-prefixed markdown page under *wiki_root*, deep
    (mirrors :func:`athenaeum.memory_class_backfill.discover_wiki_pages` —
    tier movement, like the memory-class backfill, applies to compiled
    entity pages wherever they live, not just the top-level flat layer)."""
    return sorted(p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_"))


def run_tier_sweep(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> TierSweepReport:
    """Scan *wiki_root* and apply automatic hot<->warm tier movement.

    Pure read-only when `dry_run=True` (or when nothing qualifies): computes
    :class:`TierSweepReport` without writing anything. Otherwise writes each
    qualifying page's `memory_tier:` field via
    :func:`athenaeum.atomic_io.atomic_write_text` and appends one ledger
    record per change to `_tier_sweep_records.jsonl` under the cache dir.

    Consumes usage exclusively through
    :func:`athenaeum.usage_report.get_claim_usage` — never re-reads
    `_push_records.jsonl`/`_push_references.jsonl` directly, per that
    module's documented interface contract (issue athenaeum#968 AC3).
    """
    from athenaeum.atomic_io import atomic_write_text
    from athenaeum.config import resolve_memory_tier_demote_after_days
    from athenaeum.models import parse_frontmatter
    from athenaeum.push_metrics import opaque_push_id
    from athenaeum.store import now_iso
    from athenaeum.usage_report import get_claim_usage

    resolved_now = now if now is not None else datetime.now(tz=timezone.utc)
    demote_after_days = resolve_memory_tier_demote_after_days(config)
    report = TierSweepReport()

    for path in discover_wiki_pages(wiki_root):
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue

        fm, _body = parse_frontmatter(text)
        if not fm:
            continue

        if fm.get("memory_class") == "axiom":
            report.skipped_axiom += 1
            continue

        current_tier = resolve_tier(fm, config=config)
        if current_tier not in ("hot", "warm"):
            continue

        claim_id = opaque_push_id(str(path.relative_to(wiki_root)), fm)
        usage = get_claim_usage(claim_id, cache_dir=cache_dir, wiki_root=wiki_root)

        new_tier, reason = evaluate_tier_movement(
            fm,
            usage=usage,
            now=resolved_now,
            demote_after_days=demote_after_days,
            config=config,
        )
        if new_tier is None or new_tier == current_tier:
            continue

        change = TierChange(
            path=path, old_tier=current_tier, new_tier=new_tier, reason=reason or ""
        )
        report.changed.append(change)

        if dry_run:
            continue

        # Re-read at write time rather than trusting the earlier scan (mirrors
        # `memory_class_backfill.apply_backfill`'s re-check discipline).
        try:
            fresh_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        updated_text = set_memory_tier_text(fresh_text, new_tier)
        if updated_text is None or updated_text == fresh_text:
            continue
        atomic_write_text(path, updated_text)
        swept_at = now_iso(resolved_now)
        _append_tier_sweep_ledger(change, cache_dir=cache_dir, swept_at=swept_at)

    return report


def demote_axiom_tier(
    wiki_root: Path,
    path: Path,
    fm: dict[str, Any],
    *,
    reason: str,
    by: str,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> TierChange:
    """The ONLY sanctioned way to demote an `axiom`-class page's tier.

    Requires a human-supplied *reason*/*by*, exactly like
    :func:`athenaeum.axiom_governance.record_demotion` (which this function
    calls FIRST, before touching the page) — structurally, no axiom tier
    change can happen without a matching governance-ledger row. Raises
    :class:`ValueError` if *fm* is not `memory_class: axiom`, or if the
    current tier is not `hot` (nothing to demote).
    """
    if fm.get("memory_class") != "axiom":
        raise ValueError("demote_axiom_tier: fm is not memory_class: axiom")
    current_tier = resolve_tier(fm, config=config)
    if current_tier != "hot":
        raise ValueError(f"demote_axiom_tier: current tier is {current_tier!r}, not 'hot'")

    from athenaeum.atomic_io import atomic_write_text
    from athenaeum.axiom_governance import record_demotion
    from athenaeum.store import now_iso

    slug = path.stem
    record_demotion(wiki_root, slug=slug, reason=reason, by=by, ledger_path=ledger_path, ts=now)

    change = TierChange(
        path=path, old_tier="hot", new_tier="warm", reason=f"axiom-governance: {reason}"
    )
    text = path.read_text(encoding="utf-8")
    updated_text = set_memory_tier_text(text, "warm")
    if updated_text is not None and updated_text != text:
        atomic_write_text(path, updated_text)
        swept_at = now_iso(now)
        _append_tier_sweep_ledger(change, cache_dir=cache_dir, swept_at=swept_at)
    return change


__all__ = [
    "MEMORY_TIERS",
    "SETTABLE_TIERS",
    "DEFAULT_TIER_BY_MEMORY_CLASS",
    "TIER_WEIGHTS",
    "COORDINATE_FIT_WEIGHTS",
    "TIER_SWEEP_LEDGER_FILENAME",
    "TIER_SWEEP_LEDGER_VERSION",
    "PushCandidate",
    "TierChange",
    "TierSweepReport",
    "tier_weight",
    "coordinate_fit_weight",
    "push_score",
    "resolve_tier",
    "scope_relation",
    "tier_scope_header_line",
    "is_refused",
    "select_for_push",
    "evaluate_tier_movement",
    "set_memory_tier_text",
    "discover_wiki_pages",
    "run_tier_sweep",
    "demote_axiom_tier",
]
