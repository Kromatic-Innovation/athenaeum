# SPDX-License-Identifier: Apache-2.0
"""Cluster-domain comparator adapter + dark candidate-pairs driver (athenaeum#1255)
— L4 domain/pipeline.

Step S2 of the athenaeum#715 phase-4 plan to retire ``merge.py``'s C4 detector
(:func:`athenaeum.contradictions.detect_contradictions`). Operator-verified at
source, the two detectors run over disjoint input domains today:
:func:`athenaeum.comparator.compare_pages` is called only from
``wiki_dedupe.py``, ``comparator.py`` (:func:`~athenaeum.comparator.record_comparison`)
and ``recompare.py`` — all PAIRWISE over already-compiled wiki pages.
``detect_contradictions`` is called only from ``merge.py`` — N-ARY over raw
auto-memory clusters. The comparator has no cluster-domain call site; this
module builds it, dark, so a later step (athenaeum#715 issue D) can measure
whether the comparator could replace C4 without spending a single model
call to find out how expensive that would be.

Two pieces:

- :func:`page_from_auto_memory_file` — the missing adapter from one
  :class:`~athenaeum.models.AutoMemoryFile` cluster member to a
  :class:`~athenaeum.comparator.ComparatorPage`, built on
  :func:`athenaeum.comparator.page_from_text` exactly as
  :func:`athenaeum.comparator.page_from_path` already does for wiki pages.
- :func:`run_cluster_comparator` — the driver. Given one auto-memory
  cluster's resolved members, it forms the same candidate-pair set
  :mod:`athenaeum.wiki_dedupe` forms over wiki-page clusters
  (:func:`itertools.combinations`, complete pairwise) and, ONLY when
  :func:`athenaeum.config.resolve_comparator_enabled` is on, runs
  :func:`athenaeum.comparator.record_comparison` over every pair (issue
  athenaeum#1678 — memoized via the verdict ledger, the SAME call
  :mod:`athenaeum.recompare` and :mod:`athenaeum.wiki_dedupe` already
  use, rather than a direct, never-recorded
  :func:`~athenaeum.comparator.compare_pages` call). The pair count
  itself — the N-ary-to-pairwise multiplier issue D needs to size — is
  pure combinatorics (:func:`planned_pair_count`) and is always computed
  and recorded, gate on or off: sizing the multiplier never requires the
  gate to be on, let alone an LLM call.

**The T1 reasoning screen lives on this lane (issue athenaeum#1257).**
:func:`athenaeum.reasoning_screens.t1_screen_rejects_merge_proposal` — a
pure boolean gate over ``member_paths`` + ``merge_target_name``, needing
neither a ``confidence`` scalar nor a ``draft_merged_body`` — is called
over every candidate pair inside the ``resolve_comparator_enabled``
branch below, before the pair's content is read and before any
:func:`~athenaeum.comparator.compare_pages` call. It is armed only by an
explicit :class:`ClusterScreenContext` AND its own default-OFF knob
``reasoning_tier_auditing_enabled``, so with either absent (today: both)
nothing changes.

Its sibling :func:`athenaeum.reasoning_screens.t2_screen_merge_proposal`
is **deliberately NOT called from this module.** T2's auto-finalize path
requires a ``confidence`` scalar and a ``draft_merged_body``, and this
lane produces neither: :func:`run_cluster_comparator` emits
:class:`~athenaeum.comparator.CompareOutcome` objects and never calls
:func:`athenaeum.pending_merges.write_pending_merge`. Fabricating those
two fields here to make T2 fire is the anti-pattern athenaeum#658
finding D2 recorded and athenaeum#715 banned, so T2 keeps only its
existing C4 call site until athenaeum#1256 retires that lane.
``tests/test_cluster_comparator_t1_screen.py`` pins the absence.

**Dark by design.** Nothing in :mod:`athenaeum.librarian` calls
:func:`run_cluster_comparator` yet — this issue only builds the call site
that makes the cluster domain reachable at all. ``athenaeum run`` behaviour
is therefore byte-identical whether or not ``resolve_comparator_enabled``
is on: with the driver unwired, the flag controls nothing this module does
that any existing pipeline phase observes. ``src/athenaeum/merge.py`` is
untouched by this module — the C4 detector it drives keeps running exactly
as before until a later step (gated on the athenaeum#715 issue-D
go/no-go) decides otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.comparator import ComparatorPage, CompareOutcome, page_from_text, record_comparison
from athenaeum.config import resolve_comparator_enabled, resolve_reasoning_tier_auditing_enabled
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.reasoning_screens import t1_screen_rejects_merge_proposal
from athenaeum.reasoning_tiers import load_authority_manifest_for_pipeline
from athenaeum.verdicts import page_id_for_path

if TYPE_CHECKING:
    from athenaeum.provider import LLMBackend
    from athenaeum.runlock import RunLock

__all__ = [
    "ClusterComparatorResult",
    "ClusterScreenContext",
    "auto_memory_root",
    "candidate_pairs",
    "page_from_auto_memory_file",
    "planned_pair_count",
    "run_cluster_comparator",
]


def auto_memory_root(member: AutoMemoryFile) -> Path:
    """The corpus root a cluster member's :attr:`~AutoMemoryFile.path` lives
    under, for disambiguating :func:`~athenaeum.verdicts.page_id_for_path`
    ids across ``origin_scope``\\ s (athenaeum#1677).

    ``member.path`` is normally ``<root>/<origin_scope>/<prefix>_<slug>.md``
    (see :class:`~athenaeum.models.AutoMemoryFile`'s docstring), so when the
    immediate parent directory's name matches ``origin_scope`` verbatim, the
    root one level up is ``<root>`` — passing that as ``page_id_for_path``'s
    *root* is exactly what turns the id into ``<origin_scope>/<stem>``
    instead of the bare ``<stem>``, which is the disambiguation two same-stem
    members from different ``origin_scope``\\ s under one root need to avoid
    colliding onto one id (and therefore one :func:`~athenaeum.verdicts.make_pair_key`
    pairing). If the parent directory's name does NOT match ``origin_scope``
    — a flat layout, or a test fixture that skips the scope subdirectory —
    there is no scope segment to peel off, so this falls back to the
    immediate parent directory, defensively: still a valid *root*, just one
    with nothing to disambiguate.
    """
    parent = member.path.parent
    if parent.name == member.origin_scope:
        return parent.parent
    return parent


def page_from_auto_memory_file(
    member: AutoMemoryFile, *, root: Path | None = None
) -> ComparatorPage:
    """Adapt one auto-memory cluster member into a :class:`ComparatorPage`.

    Mirrors :func:`athenaeum.comparator.page_from_path` exactly, but for a
    member that is already resolved in memory rather than a bare path: the
    id is :func:`athenaeum.verdicts.page_id_for_path` (the SAME durable
    slug identity the verdict ledger already keys pairs on — reusing it
    here rather than inventing a second id space for the cluster domain),
    and the text is ``member.content`` (the full raw markdown, frontmatter
    included — :attr:`~athenaeum.models.AutoMemoryFile.content` lazily
    reads it from disk once and caches it, so repeated pairings of the
    same member across candidate pairs cost one read, not one per pair).

    *root* scopes the id to the corpus/knowledge root the member's path is
    resolved under (athenaeum#1677) — without it, two members that share a
    filename stem but sit under different ``origin_scope``\\ s collide onto
    one id. Defaults to :func:`auto_memory_root` when omitted; pass it
    explicitly to use a caller-supplied root verbatim instead (the seam a
    future wiring step, athenaeum#1678, uses to thread a real ``wiki_root``
    through instead of this module's own best-effort derivation).
    """
    resolved_root = auto_memory_root(member) if root is None else root
    return page_from_text(page_id_for_path(member.path, root=resolved_root), member.content)


def _json_safe_page(page: ComparatorPage) -> ComparatorPage:
    """Narrow *page*'s ``meta`` values to JSON-safe scalars before a
    :func:`~athenaeum.comparator.record_comparison` call (issue athenaeum#1678).

    :func:`athenaeum.models.parse_frontmatter`'s own docstring documents its
    metadata dict as carrying "arbitrary YAML-scalar/list/dict values ...
    callers that need narrower types should validate the fields they depend
    on". An unquoted ``valid_from: 2026-03-01``-shaped frontmatter value is
    YAML's implicit date type, so it parses to a real ``datetime.date`` --
    which :func:`~athenaeum.comparator.record_comparison` eventually
    ``json.dumps``\\ s into the verdict ledger via its ``VALID_TIME``
    coordinate snapshot, and cannot. Raw auto-memory intake files have no
    compilation step guaranteeing a string round-trip the way a compiled
    wiki page might, so this call site narrows the one type that reaches
    the ledger un-stringified. ``.isoformat()`` is byte-identical to what
    ``str()``-ing a ``date``/``datetime`` already produces, so no
    comparator decision (Gate 1's date coercion already normalizes either
    representation identically -- see :func:`athenaeum.models.parse_valid_from`)
    or reader-visible behavior changes; only the ledger write stops raising.
    """
    safe_meta = {
        # datetime.datetime IS-A datetime.date, so this catches both.
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in page.meta.items()
    }
    if safe_meta == page.meta:
        return page
    return replace(page, meta=safe_meta)


def candidate_pairs(
    members: list[AutoMemoryFile],
) -> list[tuple[AutoMemoryFile, AutoMemoryFile]]:
    """Every unordered candidate pair within one cluster's members.

    Complete pairwise (:func:`itertools.combinations`, size 2) — the same
    candidate-generation shape :mod:`athenaeum.wiki_dedupe` already uses
    for wiki-page clusters. A cluster of fewer than two members yields no
    pairs (nothing here special-cases the singleton: ``combinations``
    already returns an empty list).
    """
    return list(combinations(members, 2))


def planned_pair_count(members: list[AutoMemoryFile]) -> int:
    """The number of comparator calls one cluster WOULD issue, gate on or off.

    Pure combinatorics — ``len(candidate_pairs(members))`` — so this can be
    computed (and, via :meth:`ClusterComparatorResult.to_row`, recorded)
    with zero model spend and independently of
    :func:`athenaeum.config.resolve_comparator_enabled`. This is the number
    a later issue (athenaeum#715 issue D) needs to size the N-ary
    (:func:`athenaeum.contradictions.detect_contradictions`, one call per
    cluster) to pairwise (:func:`athenaeum.comparator.compare_pages`, this
    count per cluster) cost multiplier, without running a single comparison
    to find out.
    """
    return len(candidate_pairs(members))


@dataclass(frozen=True)
class ClusterScreenContext:
    """Everything T1 needs to screen a cluster-domain candidate pair (athenaeum#1257).

    Bundled into one optional argument rather than five loose keyword
    parameters so :func:`run_cluster_comparator`'s signature stays readable
    and so "T1 is armed" is a single, explicit, caller-supplied fact —
    passing ``screen=None`` (the default) is the ONLY posture this module
    has today, and it runs no screen at all.

    The caller resolves these ONCE per run, exactly as
    :func:`athenaeum.merge.merge_clusters_to_wiki` already resolves them for
    the C4 lane: ``wiki_root``/``knowledge_root`` from the run's roots,
    ``provider`` from :func:`athenaeum.provider.resolve_provider`, ``client``
    from the ``reasoning_t1`` knob (issue athenaeum#841), and ``dry_run``
    from the run mode. ``merge_target_name`` is the name the cluster would
    merge into — supplied by the caller, never synthesized here: this module
    has no merge target of its own and inventing one would be the same
    fabrication athenaeum#658/athenaeum#715 banned for ``confidence`` and
    ``draft_merged_body``.

    Note the second gate: even with a context supplied, T1 still only runs
    when its OWN knob :func:`~athenaeum.config.resolve_reasoning_tier_auditing_enabled`
    (``reasoning_tier_auditing_enabled``, default OFF) is on. Arming the
    comparator does not arm the screen.
    """

    wiki_root: Path
    knowledge_root: Path
    provider: str = ""
    client: "LLMBackend | None" = None
    merge_target_name: str = ""
    dry_run: bool = False


@dataclass
class ClusterComparatorResult:
    """Outcome of running (or dry-sizing) the comparator over one cluster.

    ``pair_count`` is always populated, whether or not the gate was on —
    see :func:`planned_pair_count`. ``gate_enabled`` records which branch
    produced ``outcomes``: when ``False``, ``outcomes`` is always empty and
    no :func:`~athenaeum.comparator.record_comparison` call was made for
    this cluster at all.

    ``screened_out`` (issue athenaeum#1257) names the pairs T1 confidently
    rejected before any comparison was attempted. It is always empty unless
    a :class:`ClusterScreenContext` was supplied AND T1's own default-OFF
    knob is on. Without it a T1 reject would be indistinguishable from a
    pair that was never formed: ``len(outcomes) < pair_count`` alone does
    not say WHICH pairs were dropped, or why.

    Issue athenaeum#1678 wires the gate-on branch to
    :func:`~athenaeum.comparator.record_comparison` instead of calling
    :func:`~athenaeum.comparator.compare_pages` directly, which splits what
    used to be a single ``outcomes`` bucket into three, keyed on
    ``record_comparison``'s own return shape (``{"ok", "skipped", ...}``):

    - ``outcomes`` — UNCHANGED CONTRACT: only a pair ``record_comparison``
      freshly decided this call (``ok=True`` and ``skipped`` falsy) lands
      here, carrying the real :class:`~athenaeum.comparator.CompareOutcome`
      it computed. Existing readers of this field see byte-identical
      entries to before; they just may now see FEWER of them, with the
      difference accounted for below rather than silently dropped.
    - ``memoised`` (new) — a pair whose verdict was already ledgered and
      still FRESH (``skipped="fresh"``): ``record_comparison`` deliberately
      does not re-decide it, so there is no fresh ``CompareOutcome`` to
      hand back — only the pair's ids and its (reused) verdict string. A
      memoised pair IS a decided verdict for a downstream reader (e.g. a
      future ``retire.py`` move-eligibility check) — it must not read as
      absent just because it carries no ``CompareOutcome``.
    - ``unresolved`` (new) — a pair ``record_comparison`` could not decide
      at all this call (``ok=False``): either Gate 2 was unavailable (no
      client, or a post-dispatch failure) or the pair was refused as
      erasure-class (PII-flagged). Nothing was ledgered for these; the
      ``reason`` string is whatever ``record_comparison`` reported, so a
      caller can tell "examined, no verdict" apart from "never examined"
      (mirrors :mod:`athenaeum.wiki_dedupe`'s own ``OUTCOME_NO_VERDICT``
      handling of the identical return shape).
    """

    cluster_id: str
    pair_count: int
    gate_enabled: bool
    outcomes: list[tuple[str, str, CompareOutcome]] = field(default_factory=list)
    screened_out: list[tuple[str, str]] = field(default_factory=list)
    memoised: list[tuple[str, str, str]] = field(default_factory=list)
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        """JSONL-shaped row for observability — mirrors
        :meth:`athenaeum.clusters.Cluster.to_row`'s convention. Not written
        anywhere by this dark module; provided for the caller that wires
        this driver in later.
        """
        return {
            "cluster_id": self.cluster_id,
            "pair_count": self.pair_count,
            "gate_enabled": self.gate_enabled,
            "outcomes": [
                {"a": id_a, "b": id_b, "verdict": outcome.verdict}
                for id_a, id_b, outcome in self.outcomes
            ],
            "screened_out": [{"a": id_a, "b": id_b} for id_a, id_b in self.screened_out],
            "memoised": [
                {"a": id_a, "b": id_b, "verdict": verdict}
                for id_a, id_b, verdict in self.memoised
            ],
            "unresolved": [
                {"a": id_a, "b": id_b, "reason": reason}
                for id_a, id_b, reason in self.unresolved
            ],
        }


def run_cluster_comparator(
    members: list[AutoMemoryFile],
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    *,
    cluster_id: str = "",
    screen: ClusterScreenContext | None = None,
    wiki_root: Path | None = None,
    lock: "RunLock | None" = None,
) -> ClusterComparatorResult:
    """Run (or dry-size) the comparator over one auto-memory cluster's members.

    Gated on :func:`athenaeum.config.resolve_comparator_enabled` — DEFAULT
    OFF, the SAME master switch the rest of the comparator subsystem
    already ships behind (mirrors :mod:`athenaeum.wiki_dedupe`'s posture:
    "not a new flag"). With it off, this returns immediately after
    computing ``pair_count`` — no adapter call, no
    :func:`~athenaeum.comparator.record_comparison` call, no LLM spend of
    any kind, and neither *wiki_root* nor *lock* is required. This function
    has no caller in :mod:`athenaeum.librarian` yet (issue athenaeum#1255
    is dark); a live caller is future work.

    **Single-appender contract (issue athenaeum#1678).** Once the gate is
    on and a candidate pair reaches :func:`~athenaeum.comparator.record_comparison`
    (i.e. it was not dropped by the T1 screen), this function requires an
    ALREADY-ACQUIRED *lock* — it never acquires one itself. This mirrors
    every other :func:`~athenaeum.comparator.record_comparison` caller in
    this codebase (:func:`athenaeum.recompare.recompare_pending_merges`,
    :func:`athenaeum.wiki_dedupe.propose_wiki_page_merges`) and the run-level
    lifecycle :func:`athenaeum.merge.merge_clusters_to_wiki` sits inside:
    the caller acquires ONE :class:`~athenaeum.runlock.RunLock` for the
    whole run (``with RunLock(knowledge_root) as lock:``) and holds it for
    the run's duration, passing the SAME instance into every mutating call
    the run makes — this function's *lock* parameter is that same instance,
    not a lock this function manages. Two (or more) calls to this function
    sharing one caller-held *lock* are "the same run" for memoization
    purposes: a pair compared in an earlier call and still fresh is
    memoized on a later call, exactly as within a single call.

    Args:
        members: The cluster's resolved members (e.g.
            :attr:`athenaeum.merge.MergedWikiEntry.resolved_members`, or
            any other resolved :class:`~athenaeum.models.AutoMemoryFile`
            list for one cluster). Fewer than two members yields
            ``pair_count=0`` and, when the gate is on, an empty
            ``outcomes`` list — not an error, and neither *wiki_root* nor
            *lock* is required in that case either (there is no pair to
            record).
        client: A live LLM client, or ``None``. Passed straight through to
            :func:`~athenaeum.comparator.record_comparison` (and, inside
            it, to :func:`~athenaeum.comparator.compare_pages`), which
            never raises for an unavailable client (Gate 2 degrades to
            ``verdict=None``, landing this pair in ``unresolved`` — see
            :class:`ClusterComparatorResult`).
        config: Optional resolved ``athenaeum.yaml`` dict — read once here
            for the gate, then passed straight through to
            :func:`~athenaeum.comparator.record_comparison` for its own
            model resolution.
        usage: Optional run-level :class:`~athenaeum.models.TokenUsage`;
            accumulates across every pair in this cluster exactly as it
            does in :mod:`athenaeum.wiki_dedupe`/:mod:`athenaeum.recompare`.
        cluster_id: Carried onto the returned result for the caller's own
            bookkeeping; this function does not interpret it.
        screen: Optional :class:`ClusterScreenContext` arming the T1
            reasoning screen (issue athenaeum#1257) over this cluster's
            candidate pairs. ``None`` (the default) runs no screen — no
            behaviour change of any kind. Even when supplied, T1 still
            obeys its OWN default-OFF knob
            (:func:`~athenaeum.config.resolve_reasoning_tier_auditing_enabled`);
            see :func:`_t1_rejects_pair`.
        wiki_root: Issue athenaeum#1678. The corpus/wiki root this run's
            ledger keys and page ids are resolved against — passed
            VERBATIM as :func:`~athenaeum.comparator.record_comparison`'s
            first positional argument (where the verdict ledger under
            ``<wiki_root>/_verdicts/`` lives) AND as
            :func:`page_from_auto_memory_file`'s ``root=`` (and the
            screened-out branch's own :func:`~athenaeum.verdicts.page_id_for_path`
            calls), so the ledger's pair keys and the ids this run computes
            for the SAME members always agree. ``None`` (the default)
            falls back to each member's own best-effort
            :func:`auto_memory_root` for id purposes — unchanged from
            athenaeum#1677 — but is only viable at all while the gate is
            off or every pair in the cluster is screened out; the first
            pair that actually reaches ``record_comparison`` with
            *wiki_root* still ``None`` raises :class:`ValueError`.
        lock: Issue athenaeum#1678. The caller's already-acquired
            :class:`~athenaeum.runlock.RunLock` — see "Single-appender
            contract" above. Like *wiki_root*, only required once a pair
            actually reaches :func:`~athenaeum.comparator.record_comparison`.

    Returns:
        A :class:`ClusterComparatorResult`.

    Raises:
        ValueError: A candidate pair survived the T1 screen (or no screen
            ran) and reached :func:`~athenaeum.comparator.record_comparison`
            while *wiki_root* or *lock* was still ``None``.
    """
    pair_count = planned_pair_count(members)

    if not resolve_comparator_enabled(config):
        return ClusterComparatorResult(
            cluster_id=cluster_id, pair_count=pair_count, gate_enabled=False
        )

    # Issue athenaeum#1257: T1's own knob, read ONCE per cluster rather than
    # per pair, mirroring merge.py's single ``reasoning_t1_enabled`` read.
    # The authority manifest is loaded only when the screen will actually
    # run (a missing manifest is an inert empty one, so this never rejects
    # on the live-source-duplicate check by accident).
    t1_enabled = screen is not None and resolve_reasoning_tier_auditing_enabled(config)
    authority_manifest = (
        load_authority_manifest_for_pipeline(screen.knowledge_root)
        if (t1_enabled and screen is not None)
        else None
    )

    outcomes: list[tuple[str, str, CompareOutcome]] = []
    screened_out: list[tuple[str, str]] = []
    memoised: list[tuple[str, str, str]] = []
    unresolved: list[tuple[str, str, str]] = []
    for member_a, member_b in candidate_pairs(members):
        # T1 is screened on the PATHS, before ``page_from_auto_memory_file``
        # reads either member's content and before any comparator call — a
        # confident reject costs neither a file read nor a model call, the
        # cluster-domain analogue of C4's "drop before the human queue".
        if t1_enabled and screen is not None and _t1_rejects_pair(
            member_a,
            member_b,
            cluster_id=cluster_id,
            config=config,
            usage=usage,
            screen=screen,
            authority_manifest=authority_manifest,
            fallback_client=client,
        ):
            root_a = wiki_root if wiki_root is not None else auto_memory_root(member_a)
            root_b = wiki_root if wiki_root is not None else auto_memory_root(member_b)
            screened_out.append(
                (
                    page_id_for_path(member_a.path, root=root_a),
                    page_id_for_path(member_b.path, root=root_b),
                )
            )
            continue

        if wiki_root is None or lock is None:
            raise ValueError(
                "run_cluster_comparator: a cluster-domain pair reached "
                "record_comparison without both wiki_root= and an "
                "already-acquired lock= -- every athenaeum.verdicts mutator "
                "is single-appender by contract (issue athenaeum#712), the "
                "SAME contract athenaeum.recompare.recompare_pending_merges "
                "and athenaeum.wiki_dedupe.propose_wiki_page_merges already "
                "enforce for their own record_comparison calls. Acquire a "
                "RunLock(knowledge_root) once per run (mirroring "
                "athenaeum.merge.merge_clusters_to_wiki's caller-held lock "
                "lifecycle -- see this function's own docstring) and pass "
                "both wiki_root= and lock= through."
            )

        page_a = page_from_auto_memory_file(member_a, root=wiki_root)
        page_b = page_from_auto_memory_file(member_b, root=wiki_root)
        record = record_comparison(
            wiki_root,
            _json_safe_page(page_a),
            _json_safe_page(page_b),
            client=client,
            config=config,
            usage=usage,
            lock=lock,
        )
        if not record["ok"]:
            # Gate 2 unavailable, or an erasure-class (PII-flagged) refusal
            # -- nothing was ledgered. Recorded here rather than dropped so
            # a caller can tell "examined, no verdict" apart from "never
            # examined" (mirrors wiki_dedupe.py's OUTCOME_NO_VERDICT
            # handling of the identical record_comparison return shape).
            unresolved.append((page_a.id, page_b.id, str(record.get("reason") or "")))
            continue
        if record.get("skipped") == "fresh":
            # Memoized on a prior run (or an earlier call sharing this same
            # lock) -- a DECIDED verdict, just not one this call re-derived.
            # No CompareOutcome to hand back; the reused verdict string is
            # all record_comparison returns for this branch.
            memoised.append((page_a.id, page_b.id, str(record.get("verdict") or "")))
            continue
        outcome = record.get("outcome")
        if outcome is None:
            # Defensive: ok=True and not skipped should always carry an
            # outcome. If that contract ever breaks, the pair is still
            # accounted for rather than vanishing (mirrors wiki_dedupe.py's
            # identical defensive branch).
            unresolved.append((page_a.id, page_b.id, "no-outcome-returned"))
            continue
        outcomes.append((page_a.id, page_b.id, outcome))

    return ClusterComparatorResult(
        cluster_id=cluster_id,
        pair_count=pair_count,
        gate_enabled=True,
        outcomes=outcomes,
        screened_out=screened_out,
        memoised=memoised,
        unresolved=unresolved,
    )


def _t1_rejects_pair(
    member_a: AutoMemoryFile,
    member_b: AutoMemoryFile,
    *,
    cluster_id: str,
    config: dict[str, Any] | None,
    usage: TokenUsage | None,
    screen: ClusterScreenContext,
    authority_manifest: Any,
    fallback_client: "LLMBackend | None",
) -> bool:
    """Run T1 over one cluster-domain candidate pair (issue athenaeum#1257).

    A thin adapter, not a second screen: it converts the pair into the
    ``member_paths`` + ``merge_target_name`` shape
    :func:`~athenaeum.reasoning_screens.t1_screen_rejects_merge_proposal`
    already takes, and returns that function's own boolean verbatim. Every
    degradation path (no client, dry-run, a tripped spend ceiling, a
    pass-up) is the screen's own and returns ``False`` — the pair is
    compared exactly as it would be with no screen at all. Nothing here
    can turn a screen malfunction into a dropped pair.

    T1 needs neither a ``confidence`` scalar nor a ``draft_merged_body``,
    which is precisely why it — and not
    :func:`~athenaeum.reasoning_screens.t2_screen_merge_proposal` — is the
    screen this lane can carry; see this module's own docstring and
    :mod:`athenaeum.reasoning_screens`.
    """
    return t1_screen_rejects_merge_proposal(
        member_paths=[str(member_a.path), str(member_b.path)],
        merge_target_name=screen.merge_target_name,
        cluster_id=cluster_id,
        client=screen.client if screen.client is not None else fallback_client,
        usage=usage,
        wiki_root=screen.wiki_root,
        config=config,
        provider=screen.provider,
        authority_manifest=authority_manifest,
        enabled=True,
        dry_run=screen.dry_run,
    )
