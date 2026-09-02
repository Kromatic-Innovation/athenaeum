# SPDX-License-Identifier: Apache-2.0
"""Auto-memory merge pass (issue athenaeum#197, C3) — L4 domain/pipeline.

Consumes the JSONL cluster report produced by C2
(:mod:`athenaeum.clusters`) and emits ONE canonical wiki entry per
cluster at ``wiki/auto-<topic-slug>.md``. Every member's content is
concatenated into a synthesized body; every member's ``sources[]`` is
unioned into a single deduped cited list. It also owns the tiered
reasoning-pass screen at the merge-proposal seam: T1
(:func:`t1_screen_rejects_merge_proposal`, issue athenaeum#518) drops a confident
reject before the human queue, and T2 (:func:`t2_screen_merge_proposal`,
issue athenaeum#602) consults a T1 pass-up and AUTO-FINALIZES a safe-class
``approve`` — bypassing ``_pending_merges.md`` entirely via
:func:`athenaeum.pending_merges.resolve_merge`'s existing approve-time
fold, marked ``auto_applied`` in provenance. See each function's own
docstring and :mod:`athenaeum.reasoning_tiers` for the gated/default-OFF
wiring detail. **Independently gated as of issue athenaeum#1200:** T1 by
``reasoning_tier_auditing_enabled`` (default OFF), T2's auto-apply by its
own ``reasoning_tier_t2_auto_apply_enabled`` (default OFF, independent of
T1's value) — before athenaeum#1200 one flag armed both together.

SCC membership (L4 domain/pipeline). ``merge.py`` is imported at TOP level by
``librarian.py``, ``retire.py``, and ``wiki_dedupe.py`` (normal downward
dependencies from their side). Issue athenaeum#545 hoisted ``discover_auto_memory_files``
to the :mod:`athenaeum.intake` leaf, so this module now imports it from
``intake`` at TOP level and the former deferred ``from athenaeum.librarian
import discover_auto_memory_files`` back-edge (the librarian<->merge cycle) is
GONE.

(A local import in ``merge_clusters_to_wiki`` — ``from athenaeum.clusters
import DEFAULT_CACHE_DIR`` — is unrelated to any cycle: :mod:`athenaeum.clusters`
is an L3 service module that does not import this module back; deferred for
cost/ordering, not cycle-breaking.)

``merge.py`` was formerly in a PRE-EXISTING residual SCC that athenaeum#545 did NOT
target (out of its named scope): ``{merge, pending_merges, calibration,
reasoning_tiers}``. ``pending_merges.revalidate_pending_merges`` function-
locally imported ``_merge_proposal_suppression_reason`` FROM this module while
this module imports ``write_pending_merge`` FROM ``pending_merges`` at top level
— a ``pending_merges`` <-> ``merge`` back-edge. Issue athenaeum#640 dissolved that cycle
by hoisting ``_merge_proposal_suppression_reason`` DOWN to the
:mod:`athenaeum.merge_type_gate` gate leaf (which both this module and
``pending_merges`` already sit above), so ``pending_merges`` no longer reaches
up into this hub.

Scope for this module (kept narrow on purpose — see issue athenaeum#197):

- Input: canonical cluster JSONL path + knowledge root.
- Output: ``wiki/auto-<topic-slug>.md`` per cluster.
- Dedupe key for ``sources[]``: ``(session, turn)``. Two turns in the
  same session stay distinct; duplicate citations of the same turn are
  collapsed. ``(session, date)`` is explicitly NOT used.
- ``origin_scope`` is propagated from C1's record onto every source
  entry.
- Singletons ARE emitted (size-1 clusters → size-1 source list). There
  is no minimum-cluster-size filter; the wiki read path wants a uniform
  surface.
- Contradiction heuristic: the PR flags ``contradictions_detected: true``
  in frontmatter when the cluster's ``centroid_score`` falls below
  :data:`CONTRADICTION_COHESION_THRESHOLD` (0.75). C4 (athenaeum#198) replaces
  this with real contradiction detection — this module is only the
  cheap proxy so the human-review queue has a seed.

Out of scope (deliberate — later lanes):

- LLM-based body synthesis. C3's strategy is deterministic:
  concatenate member bodies, drop identical paragraphs, prefix each
  block with a scope/filename header. Rich paraphrase is a follow-up.
- Real contradiction detection (C4, athenaeum#198).
- Rewrites to ``raw/auto-memory/*`` — raw is append-only; the wiki is
  the compiled view.
- A cross-scope ``wiki/MEMORY.md`` — Phase B explicitly removed it and
  this module does NOT recreate it.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from athenaeum import detection_state, spend
from athenaeum._lint import _strip_self_reference
from athenaeum.atomic_io import atomic_write_text
from athenaeum.calibration import sample_tier_decision
from athenaeum.clusters import resolve_cluster_output_path, resolve_cluster_threshold
from athenaeum.config import (
    load_config,
    resolve_ephemeral_scopes,
    resolve_extra_intake_roots,
    resolve_heartbeat_interval,
    resolve_min_cluster_cohesion,
    resolve_min_cluster_cohesion_scopes,
    resolve_operational_markers,
    resolve_reasoning_tier_auditing_enabled,
    resolve_reasoning_tier_t2_auto_apply_enabled,
)
from athenaeum.contradictions import detect_contradictions
from athenaeum.cross_scope import (
    candidate_to_auto_memory_files,
    chunk_by_cap,
    cross_scope_similarity_pairs,
    pool_cluster_with_ancestors,
    resolve_cluster_size_cap,
    resolve_cross_scope_mode,
    resolve_similarity_threshold,
)
from athenaeum.ephemeral import classify_ephemeral
from athenaeum.fingerprint import (
    _member_key_str,
    _pair_text_from_passages,
    claim_pair_fingerprint,
    is_stale_auto_suppression,
    load_resolved_records,
    normalize_side,
    record_resolution,
    resolve_not_a_conflict_ttl_days,
)
from athenaeum.intake import discover_auto_memory_files
from athenaeum.merge_type_gate import (
    _merge_proposal_suppression_reason,
    build_cite_proposal,
    cross_class_precheck,
)
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    AutoMemoryFile,
    ContradictionResult,
    EscalationItem,
    TokenUsage,
    coerce_source_type,
    parse_bucket,
    parse_deprecated,
    parse_frontmatter,
    parse_refines,
    parse_superseded_by,
    parse_supersedes,
    render_frontmatter,
    safe_source_ref,
    slugify,
    validity_bound_str,
    validity_windows_disjoint,
)
from athenaeum.pending_merges import (
    _make_id,
    classify_write_kind,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.progress import PhaseHeartbeat
from athenaeum.provider import LLMBackend, resolve_provider
from athenaeum.reasoning_tiers import (
    ReasoningProposal,
    load_authority_manifest_for_pipeline,
    record_reasoning_tier_t2_decision,
    run_reasoning_pipeline,
    run_t1_tier,
    run_t2_tier,
)
from athenaeum.resolutions import (
    ATTRIBUTE_BOTH_ACTION,
    PROPOSE_MERGE_ACTION,
    SUPPRESS_ACTION,
    MergeProposal,
    ResolutionProposal,
    enact_resolution,
    propose_resolution,
    render_proposal_block,
    resolve_max_per_run,
)
from athenaeum.tiers import tier4_escalate

log = logging.getLogger(__name__)


class RunDeadlineExceeded(Exception):
    """Raised inside the merge pass when the run-level wall-clock deadline trips.

    Issue athenaeum#396. The merge/detect loops are the post-compile phase where the
    athenaeum#396 incident wedged (a hung ``claude -p`` merge subprocess). When
    :func:`merge_clusters_to_wiki` is armed with a ``deadline`` (an absolute
    :func:`time.monotonic` value) it checks it at each cluster/chunk boundary
    and raises this so the caller (:func:`athenaeum.librarian.run`) can commit
    the partial progress and exit non-zero (resumable), mirroring the athenaeum#337
    interrupt-checkpoint path. ``phase`` names where the trip occurred for the
    commit message and the run log.
    """

    def __init__(self, phase: str) -> None:
        super().__init__(f"run-level wall-clock deadline exceeded during {phase}")
        self.phase = phase


# Legacy centroid-cohesion constant from C3. C4 replaces this with real
# claim-level contradiction detection via
# :func:`athenaeum.contradictions.detect_contradictions`, but the constant
# stays exported (at its historical value) so any downstream consumer that
# imports it does not break. New code should NOT read it.
CONTRADICTION_COHESION_THRESHOLD = 0.75

# Frontmatter marker written when the detector finds a contradiction. When
# the detector returns ``detected=False`` the key is OMITTED entirely (not
# rendered as ``status: clean``) -- absence is the clean signal. This
# mirrors C3's treatment of the old ``contradictions_detected`` flag on
# cohesive clusters and keeps ``wiki/auto-*.md`` frontmatter minimal.
CONTRADICTION_STATUS_FLAGGED = "contradiction-flagged"


def _declared_relationship(a: "AutoMemoryFile", b: "AutoMemoryFile") -> str | None:
    """Return a rationale slug when ``a`` and ``b`` declare each other.

    Lane 1 / athenaeum#167. Matches by ``AutoMemoryFile.name`` (the documented
    frontmatter slug). A declaration on EITHER side suppresses the pair.

    Returns:
        ``"declared-supersession"`` when one side names the other in its
        ``supersedes`` list (the resolution is in the text — no human
        review needed). ``"declared-refinement"`` when one side names the
        other in its ``refines`` list (general + exception; both stay
        active and never count as a conflict). ``None`` when no
        declaration applies.
    """
    a_name = (a.name or "").strip()
    b_name = (b.name or "").strip()
    if not a_name or not b_name:
        return None
    # Quine review athenaeum#171 / SHOULD #4: compare via slugify so a case- or
    # punctuation-mismatched declaration still matches.
    a_slug = slugify(a_name)
    b_slug = slugify(b_name)
    a_super = {slugify(n) for n in a.supersedes_names()}
    b_super = {slugify(n) for n in b.supersedes_names()}
    a_refines = {slugify(n) for n in (a.refines or [])}
    b_refines = {slugify(n) for n in (b.refines or [])}
    a_supersedes_b = b_slug in a_super
    b_supersedes_a = a_slug in b_super
    # MUST #3: mutual supersedes is itself a declared contradiction —
    # neither side wins deterministically. Log and refuse to declare;
    # the pair falls through to the detector/resolver path.
    if a_supersedes_b and b_supersedes_a:
        log.warning(
            "merge: mutual supersedes between %r and %r — not a declarable relationship",
            a_name,
            b_name,
        )
        return None
    if a_supersedes_b or b_supersedes_a:
        return "declared-supersession"
    if b_slug in a_refines or a_slug in b_refines:
        return "declared-refinement"
    return None


def _filter_declared_pairs(
    members: list["AutoMemoryFile"],
) -> tuple[list["AutoMemoryFile"], str | None]:
    """Prune declared pairs from a chunk before the detector sees it.

    Issue athenaeum#172: previously this was all-or-nothing — one undeclared pair
    sent the WHOLE chunk (including already-declared pairs) to Haiku.
    Now we prune: drop any member whose every partner in the chunk has
    a declaration. The remaining members still form ≥1 undeclared pair
    and are exactly what Haiku should see.

    Returns ``(pruned_members, rationale)``:

    * Fully declared chunk → ``([], rationale)``. Caller short-circuits.
      Rationale records the strongest declaration class observed
      (supersession beats refinement when both appear).
    * Partially declared chunk → ``(pruned_members, None)``. Members
      involved only in declared pairs are removed. Rationale is
      ``None`` because the caller still runs the detector on the
      remainder. If only one undeclared pair survives, ``pruned_members``
      contains exactly those two members.
    * No declarations → ``(members, None)`` unchanged.
    * Singletons → ``(members, None)`` unchanged (no pairs to evaluate).
    """
    if len(members) < 2:
        return members, None
    n = len(members)
    # Bookkeep per-member: does this member participate in ANY undeclared
    # pair? If yes, keep it. If every one of its partners is declared,
    # the member can be dropped from the Haiku batch.
    has_undeclared_partner = [False] * n
    saw_supersession = False
    saw_refinement = False
    saw_undeclared = False
    for i in range(n):
        for j in range(i + 1, n):
            verdict = _declared_relationship(members[i], members[j])
            if verdict is None:
                saw_undeclared = True
                has_undeclared_partner[i] = True
                has_undeclared_partner[j] = True
            elif verdict == "declared-supersession":
                saw_supersession = True
            else:
                saw_refinement = True
    if not saw_undeclared:
        # Fully declared — short-circuit the detector entirely.
        if saw_supersession:
            return [], "declared-supersession"
        if saw_refinement:
            return [], "declared-refinement"
        return [], None
    pruned = [m for m, keep in zip(members, has_undeclared_partner) if keep]
    return pruned, None


def _am_validity_meta(am: "AutoMemoryFile") -> dict[str, str]:
    """Return an :class:`AutoMemoryFile`'s validity bounds as a meta dict (athenaeum#324).

    Mirrors :meth:`AutoMemoryFile.is_inactive`, which feeds the stored raw
    ``valid_until`` string back through the dict predicate — so the disjoint
    check re-parses the SAME normalized bounds the inactive predicate sees and
    the two cannot drift.
    """
    return {"valid_from": am.valid_from, "valid_until": am.valid_until}


def _all_pairs_disjoint(members: list["AutoMemoryFile"]) -> bool:
    """True when EVERY pair among ``members`` has disjoint validity windows (athenaeum#324).

    Two claims whose validity windows never overlap are sequential states of the
    world (A valid through March, B valid from April) and cannot contradict.
    When the whole cluster is pairwise-disjoint the detector LLM call is skipped
    entirely (mirroring the declared-relationship short-circuit). ANY pair with
    an overlapping or open window returns ``False`` so detection proceeds —
    matching the fail-open posture of
    :func:`athenaeum.models.validity_windows_disjoint`. Fewer than two members
    => ``False`` (nothing to short-circuit; the singleton path handles that).
    """
    if len(members) < 2:
        return False
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            if not validity_windows_disjoint(
                _am_validity_meta(members[i]), _am_validity_meta(members[j])
            ):
                return False
    return True


def _members_from_result(
    result: ContradictionResult,
    members: list["AutoMemoryFile"],
) -> list["AutoMemoryFile"]:
    """Resolve the detector's ``members_involved`` refs back to member records.

    Matching mirrors :func:`_order_member_paths` /
    :func:`athenaeum.resolutions._declared_winner`: a member matches a ref when
    ``"<origin_scope>/<filename>"`` equals the ref or shares its trailing
    filename component. Returns the matched records in the detector's flagged
    ``a``/``b`` order; unmatched refs are dropped.
    """
    matched: list[AutoMemoryFile] = []
    used: set[int] = set()
    for ref in result.members_involved:
        ref_tail = ref.rsplit("/", 1)[-1]
        for i, am in enumerate(members):
            if i in used:
                continue
            tag = f"{am.origin_scope}/{am.path.name}"
            if tag == ref or tag.endswith("/" + ref_tail):
                matched.append(am)
                used.add(i)
                break
    return matched


def _detected_pair_disjoint(
    result: ContradictionResult,
    members: list["AutoMemoryFile"],
) -> bool:
    """True when the detector's two flagged members have disjoint windows (athenaeum#324).

    Post-detection guard for the overlapping-cluster case: the pre-filter
    (:func:`_all_pairs_disjoint`) only fires when the WHOLE cluster is
    pairwise-disjoint, but a cluster with some overlapping pairs can still have
    the detector flag a SPECIFIC disjoint pair. Guards for the 0/1-member echo
    the detector sometimes returns — fewer than two resolved members => ``False``
    (no downgrade, the escalation proceeds).
    """
    if not result.detected:
        return False
    matched = _members_from_result(result, members)
    if len(matched) < 2:
        return False
    return validity_windows_disjoint(
        _am_validity_meta(matched[0]), _am_validity_meta(matched[1])
    )


def _order_member_paths(
    result: ContradictionResult,
    members: list["AutoMemoryFile"] | None,
) -> list[str]:
    """Return member file paths in the detector's flagged ``a``/``b`` order.

    The resolver labels the two flagged snippets ``a`` and ``b`` in the
    order they appear in ``result.members_involved`` — the SAME order
    :func:`athenaeum.resolutions._build_user_message` presents them to the
    model. The enactment lane (athenaeum#166 follow-up) keys ``forget_*`` /
    ``correct_*`` on those labels, so it needs the member PATHS in exactly
    that order, not the (arbitrary) cluster/chunk order.

    Matching mirrors ``_build_user_message`` / ``_declared_winner``: a
    member matches a ref when ``"<origin_scope>/<filename>"`` equals the
    ref or shares its trailing filename component. Unmatched refs and
    members are dropped — a short/empty list makes the enactment lane
    no-op, which is the safe default. Returns absolute path strings.
    """
    if not members:
        return []
    ordered: list[str] = []
    used: set[int] = set()
    for ref in result.members_involved:
        ref_tail = ref.rsplit("/", 1)[-1]
        for i, am in enumerate(members):
            if i in used:
                continue
            tag = f"{am.origin_scope}/{am.path.name}"
            if tag == ref or tag.endswith("/" + ref_tail):
                ordered.append(str(am.path))
                used.add(i)
                break
    return ordered


def _result_claim_fingerprint(result: ContradictionResult) -> str | None:
    """Claim-pair fingerprint for a detector result (issue athenaeum#249).

    Returns ``None`` when fewer than two conflicting passages are present —
    no stable pair to fingerprint, so the caller must NOT cache or skip.
    """
    passages = result.conflicting_passages or []
    if len(passages) < 2:
        return None
    return claim_pair_fingerprint(passages[0], passages[1], result.conflict_type)


# Filesystem prefix that distinguishes auto-memory wiki entries from
# entity-schema entries (``<uid>-<kebab>.md``). Callers reading the
# wiki directory can branch on this prefix without parsing frontmatter.
AUTO_WIKI_PREFIX = "auto-"

# Stopword-ish tokens dropped when deriving a topic slug from member
# filenames — these carry no semantic weight and would otherwise win
# the frequency contest on naturally-clustered files (``feedback_`` is
# the dominant prefix across memories, for example).
_SLUG_BORING_TOKENS: frozenset[str] = frozenset(
    {
        "feedback",
        "project",
        "reference",
        "user",
        "recall",
        "auto",
        "memory",
        "note",
        "the",
        "and",
        "for",
        "with",
        "file",
        "files",
        "md",
    }
)


@dataclass
class MergedWikiEntry:
    """In-memory shape of one consolidated wiki entry.

    ``contradictions_detected`` is retained on the dataclass for backwards
    compatibility with the C3 wire (tests + callers that read it); C4 now
    sets it from the real :class:`ContradictionResult`. ``contradiction``
    carries the structured detector output when one was run.
    """

    topic_slug: str
    cluster_id: str
    cluster_centroid_score: float
    contradictions_detected: bool
    # Issue athenaeum#421: minimum pairwise cosine among cluster members (complete-
    # linkage coherence). Carried from the cluster JSONL row; 1.0 for
    # singletons and pre-athenaeum#421 rows without the field. The merge-proposal gate
    # suppresses a proposal whose min pairwise falls below the cluster
    # threshold (a single-linkage chain, not a complete-linkage clique).
    min_pairwise_score: float = 1.0
    origin_scopes: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    member_paths: list[str] = field(default_factory=list)
    contradiction: ContradictionResult | None = None
    # Issue athenaeum#261 (slice B of athenaeum#259): set by the move-then-retire pass when the
    # cluster's raw intake has been MOVED into this wiki entry (long-term
    # memory) and the raw files retired (git rm). Rendered as ``retired: true``
    # in frontmatter so a reader can tell the fact now lives here permanently
    # rather than in the expiring intake queue. Default False keeps every
    # non-retire write byte-identical to the pre-athenaeum#261 output.
    retired: bool = False
    # Issue athenaeum#904: page-level decay classification, one of
    # ``athenaeum.models.MEMORY_BUCKETS`` or ``""`` (unset — the default,
    # behaves exactly as before this field existed). Unlike ``valid_from``/
    # ``valid_until`` (per-CLAIM, carried per-source — see
    # ``_stamp_member_validity``), ``bucket`` is a per-PAGE decay policy: a
    # compiled page is either "daily churn" or it isn't, not per-citation.
    # Computed by :func:`merge_cluster_row` from the ACTIVE resolved members
    # (first non-empty wins, deterministic member order — the same
    # first-wins convention this module already uses for citation merges;
    # see the comment on that rule near ``dedupe_sources``).
    bucket: str = ""
    # Resolved :class:`AutoMemoryFile` records backing this cluster. Populated
    # by :func:`merge_cluster_row` so the outer orchestrator does not need to
    # re-resolve filesystem paths to run the C4 contradiction detector.
    # Not rendered into wiki frontmatter; kept off the public docstring in
    # render_merged_entry by only touching ``sources``/``origin_scopes``.
    resolved_members: list[AutoMemoryFile] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{AUTO_WIKI_PREFIX}{self.topic_slug}.md"


# ---------------------------------------------------------------------------
# Cluster JSONL reader
# ---------------------------------------------------------------------------


def read_cluster_rows(jsonl_path: Path) -> list[dict[str, Any]]:
    """Read the canonical cluster JSONL; return rows in file order.

    The canonical file is always the latest run (C2 atomically replaces
    it). Timestamped siblings (``<stem>-<iso>.jsonl``) are NOT read —
    historical runs are for auditing, not for merging.
    """
    if not jsonl_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning(
                    "skipping malformed cluster row in %s: %s",
                    jsonl_path,
                    exc,
                )
    return rows


# ---------------------------------------------------------------------------
# Member-path resolution
# ---------------------------------------------------------------------------


def resolve_member_path(
    member_ref: str,
    extra_roots: list[Path],
) -> Path | None:
    """Resolve a cluster row's ``member_paths`` entry to an absolute file.

    C2 writes each member_path as a POSIX path relative to the FIRST
    configured extra intake root (i.e. ``<scope>/<filename>.md`` under
    ``raw/auto-memory/``). If a member_path is already absolute (stale
    fallback from a reloaded-config path), it is returned as-is. Otherwise
    we try each configured extra root in order and return the first hit.
    """
    candidate = Path(member_ref)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for root in extra_roots:
        attempt = (root / candidate).resolve()
        if attempt.is_file():
            return attempt
    return None


def _rows_touched_since(
    rows: list[dict[str, Any]],
    extra_roots: list[Path],
    since: datetime,
) -> set[str]:
    """Cluster ids (issue athenaeum#909) with at least one member file modified
    at/after *since*.

    Pure row-level query used by :func:`merge_clusters_to_wiki`'s C4-since
    scope — deliberately computed straight from the raw cluster JSONL rows
    (via :func:`resolve_member_path`), the SAME source :func:`read_cluster_rows`
    returns and the row-filter step already consumes, rather than from the
    (more expensive to build) ``MergedWikiEntry`` list. A member ref that no
    longer resolves to a file (retired/moved/deleted since C2 last ran) is
    skipped, not an error — a cluster is "touched" by what is still there to
    look at. Tolerant of a file vanishing mid-scan (``OSError`` on ``stat()``,
    e.g. a concurrent retire pass) — skipped exactly like an unresolvable
    ref, mirroring :func:`athenaeum.intake.discover_raw_backlog_bytes`'s
    stat-failure tolerance.
    """
    since_ts = since.timestamp()
    touched: set[str] = set()
    for row in rows:
        cluster_id = str(row.get("cluster_id", "") or "")
        if not cluster_id:
            continue
        member_paths = row.get("member_paths") or []
        for member_ref in member_paths:
            resolved = resolve_member_path(str(member_ref), extra_roots)
            if resolved is None:
                continue
            try:
                mtime = resolved.stat().st_mtime
            except OSError:
                continue
            if mtime >= since_ts:
                touched.add(cluster_id)
                break
    return touched


# ---------------------------------------------------------------------------
# Topic-slug derivation
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _slug_tokens_from_filename(filename: str) -> list[str]:
    stem = filename.lower()
    if stem.endswith(".md"):
        stem = stem[:-3]
    # Split on non-alnum so ``project_foo_bar`` → foo, bar.
    return [t for t in _TOKEN_RE.findall(stem) if t not in _SLUG_BORING_TOKENS]


def derive_topic_slug(
    member_paths: list[str],
    cluster_id: str,
) -> str:
    """Derive a filesystem-safe topic slug from cluster member filenames.

    Strategy (intentionally simple — see PR body for rationale):

    1. Tokenize each member's filename (drop ``.md``, split on non-alnum,
       drop boring prefixes like ``feedback_``/``project_`` and words
       shorter than 3 chars).
    2. Rank tokens by member-frequency (in how many files the token
       appears), break ties by total-frequency, then alphabetical.
    3. Take up to 3 top-ranked tokens, join with ``-``.
    4. If no usable tokens (every member is pure boring-prefix), fall
       back to ``cluster_id`` sanitized to slug form.

    Rationale vs. LLM-picked slug: the cheap heuristic gets the
    regression fixture right (the near-duplicate slug from five
    near-duplicate files) while staying deterministic and
    testable without network. LLM polish can ride on top in C4+.
    """
    member_freq: dict[str, int] = {}
    total_freq: dict[str, int] = {}
    for mp in member_paths:
        filename = Path(mp).name
        seen_in_file: set[str] = set()
        for tok in _slug_tokens_from_filename(filename):
            if len(tok) < 3:
                continue
            total_freq[tok] = total_freq.get(tok, 0) + 1
            if tok not in seen_in_file:
                member_freq[tok] = member_freq.get(tok, 0) + 1
                seen_in_file.add(tok)

    if member_freq:
        ranked = sorted(
            member_freq.items(),
            key=lambda kv: (-kv[1], -total_freq.get(kv[0], 0), kv[0]),
        )
        top = [tok for tok, _ in ranked[:3]]
        slug = "-".join(top)
        if slug:
            return slug

    # Fallback: sanitize cluster_id to slug form. cluster_id format is
    # ``<scope_hint>-<seq>`` from clusters.py — already slug-ish.
    fallback = re.sub(r"[^a-z0-9]+", "-", cluster_id.lower()).strip("-")
    return fallback or "unknown"


# ---------------------------------------------------------------------------
# Source parsing + dedupe
# ---------------------------------------------------------------------------


def _default_source_ref(entry: dict[str, Any]) -> str:
    """Best-effort ``source_ref`` from session+turn — NEVER the raw filename.

    Issue athenaeum#260: when a source carries no explicit ``source_ref``, we
    synthesize one from ``session`` (+ ``turn`` when present) so the
    citation always points at the originating session, never at the raw
    ``auto-memory/...`` file. Returns ``""`` only when there is no session
    to cite.
    """
    session = entry.get("session")
    if not session:
        return ""
    turn = entry.get("turn")
    if turn is not None:
        return f"{session}#turn{turn}"
    return str(session)


def _parse_one_source(raw: Any, fallback_scope: str) -> dict[str, Any] | None:
    """Normalize one ``sources[]`` entry into a plain dict + origin_scope.

    Accepts dict (the shape defined in
    ``policies/auto-memory-citation.md``) or raw string (legacy bare
    session UUID). Returns ``None`` for unparseable input.

    Issue athenaeum#260 (slice A of athenaeum#259): every parsed source carries an
    origin-traced ``source_type`` (one of :data:`SOURCE_TYPES`, default
    ``inferred``) and a ``source_ref`` — the ULTIMATE reference
    (session-id+turn / URL / document path), back-filled from session+turn
    when not explicitly supplied. ``source_ref`` is NEVER the raw
    ``auto-memory/...`` filename. Legacy sources without these keys still
    parse cleanly (missing ``source_type`` => ``inferred``).
    """
    if isinstance(raw, dict):
        entry: dict[str, Any] = {}
        session = raw.get("session")
        if session is None:
            return None
        entry["session"] = str(session)
        turn = raw.get("turn")
        if turn is not None:
            try:
                entry["turn"] = int(turn)
            except (TypeError, ValueError):
                entry["turn"] = turn
        date = raw.get("date")
        if date is not None:
            entry["date"] = str(date)
        excerpt = raw.get("excerpt")
        if excerpt is not None:
            entry["excerpt"] = str(excerpt)
        entry["origin_scope"] = str(raw.get("origin_scope", fallback_scope))
        entry["source_type"] = coerce_source_type(raw.get("source_type"))
        # Guard the EXPLICIT path too: a producer that stamps a raw filename
        # into source_ref is rejected and back-filled from session+turn.
        entry["source_ref"] = safe_source_ref(
            raw.get("source_ref"), _default_source_ref(entry)
        )
        # Issue athenaeum#262 (slice C of athenaeum#259): carry the granular diff target. When a
        # fact is moved into a wiki entry, ``retire.py`` stamps the atomic
        # ``claim`` text (and a resolved ``verdict``/disposition when one
        # exists) onto the source so a future memory has a footnote-level
        # thing to diff against. Both are OPTIONAL — sources written before
        # slice C carry neither and still round-trip unchanged.
        claim = raw.get("claim")
        if claim is not None and str(claim).strip():
            entry["claim"] = str(claim)
        verdict = raw.get("verdict")
        if verdict is not None and str(verdict).strip():
            entry["verdict"] = str(verdict)
        # Issue athenaeum#308 (slice 4): carry per-claim temporal validity through the
        # compiled source record so a claim's window round-trips byte-for-byte
        # through a render + reparse (same contract as claim/verdict above).
        # Bounds are normalized to ``YYYY-MM-DD`` via ``validity_bound_str``;
        # an unparseable value coerces to ``""`` (dropped — open bound).
        vf = validity_bound_str(raw, "valid_from")
        if vf:
            entry["valid_from"] = vf
        vu = validity_bound_str(raw, "valid_until")
        if vu:
            entry["valid_until"] = vu
        return entry
    if isinstance(raw, str):
        return {
            "session": raw,
            "origin_scope": fallback_scope,
            "source_type": DEFAULT_SOURCE_TYPE,
            # The legacy bare-UUID ref is the session id itself — a valid
            # ultimate ref, never a filename (no better fallback exists for
            # a bare string, so it passes through as the session ref).
            "source_ref": raw,
        }
    return None


def _am_as_implicit_source(am: AutoMemoryFile) -> dict[str, Any] | None:
    """Fallback source entry when an auto-memory file has no sources[].

    If the file carries ``originSessionId`` + ``originTurn`` we emit a
    synthetic source citing the original write. This preserves the
    AC that every consolidated entry can cite every member — even
    members written before the citation policy landed (Phase A).
    """
    if am.origin_session_id is None:
        return None
    entry: dict[str, Any] = {
        "session": am.origin_session_id,
        "origin_scope": am.origin_scope,
    }
    if am.origin_turn is not None:
        entry["turn"] = int(am.origin_turn)
    # Issue athenaeum#260: carry origin-traced provenance. An implicit source recovered
    # from originSessionId/turn is unverified at this layer, so honor the
    # file's own declared source_type (default ``inferred``) and back-fill a
    # session+turn ref — never the raw filename. The guard also rejects a
    # filename-shaped source_ref the file may carry.
    entry["source_type"] = coerce_source_type(am.source_type)
    entry["source_ref"] = safe_source_ref(am.source_ref, _default_source_ref(entry))
    return entry


def _stamp_member_validity(src: dict[str, Any], am: AutoMemoryFile) -> None:
    """Stamp a member's temporal validity window onto its compiled source (athenaeum#308 slice 4).

    Per-claim (vs per-page) compiled validity: each raw member IS one claim,
    and its ``valid_from`` / ``valid_until`` window travels WITH the claim into
    the compiled entry's per-source record — rather than the whole compiled
    page being a single valid/invalid unit. All sources a member cites share
    the member's window (the window belongs to the claim, applied to each of
    its citations).

    Only-fill-never-override: a bound the source ALREADY declares (a future
    explicit per-source window) is left untouched; the member value fills only
    an absent bound. ``am.valid_from`` / ``am.valid_until`` are already the
    normalized ``YYYY-MM-DD`` strings (``validity_bound_str`` at construction),
    ``""`` for an open/malformed bound — which is skipped, adding no key.
    """
    if am.valid_from and not src.get("valid_from"):
        src["valid_from"] = am.valid_from
    if am.valid_until and not src.get("valid_until"):
        src["valid_until"] = am.valid_until


def _validity_window_phrase(src: dict[str, Any]) -> str:
    """Human-readable validity window for a compiled source, or ``""`` (athenaeum#308 slice 4).

    Renders the per-claim window carried on the source dict:

    - both bounds  => ``"2026-04-01 to 2026-12-31"``
    - lower only   => ``"from 2026-04-01"``
    - upper only   => ``"until 2026-12-31"``
    - neither      => ``""`` (open interval — the footnote omits the clause)
    """
    vf = str(src.get("valid_from") or "").strip()
    vu = str(src.get("valid_until") or "").strip()
    if vf and vu:
        return f"{vf} to {vu}"
    if vf:
        return f"from {vf}"
    if vu:
        return f"until {vu}"
    return ""


def dedupe_sources(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe on ``(session, turn)``. First occurrence wins.

    ``(session, turn)`` is the Phase-A granularity lock — two turns
    within the same session are distinct memories. Two citations of
    the same (session, turn) are merged (first wins, stable order).
    Entries missing a turn fall back to ``(session, None)`` and only
    collapse among themselves.

    Provenance note (athenaeum#260): the dedupe key is ``(session, turn)`` ONLY — it
    ignores ``source_type`` / ``source_ref``. So two entries citing the same
    (session, turn) with *different* provenance collapse to the FIRST one
    (input order). Callers that want the verified provenance to win must
    order the verified entry first before deduping.

    This first-wins rule extends to the athenaeum#308-slice-4 ``valid_from`` /
    ``valid_until`` window: two citations of the same (session, turn) keep the
    first entry's window. In practice both come from the same raw member and
    carry the same window, so the collapse is loss-free.
    """
    seen: set[tuple[str, Any]] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("session", "")),
            entry.get("turn"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Body synthesis (deterministic concatenate-with-dedupe)
# ---------------------------------------------------------------------------


def synthesize_body(
    member_bodies: list[tuple[str, str, str]],
) -> str:
    """Concatenate member bodies, dropping paragraphs seen verbatim before.

    Args:
        member_bodies: list of ``(scope, filename, body)`` triples, in
            cluster input order. Scope + filename become the section
            header so readers can trace a paragraph back to its origin
            raw file without hunting.

    The dedupe is exact-match paragraph level (whitespace-trimmed). Two
    files saying "X causes Y" with identical wording contribute that
    paragraph once; variant phrasings are kept. This is the deliberately
    simple strategy documented in the PR body — LLM paraphrase/merge is
    a follow-up in C4+.
    """
    seen_paragraphs: set[str] = set()
    sections: list[str] = []
    for scope, filename, body in member_bodies:
        kept_paragraphs: list[str] = []
        for para in re.split(r"\n\s*\n", body):
            canonical = " ".join(para.split())
            if not canonical:
                continue
            if canonical in seen_paragraphs:
                continue
            seen_paragraphs.add(canonical)
            kept_paragraphs.append(para.strip())
        if not kept_paragraphs:
            continue
        header = f"## From `{scope}/{filename}`"
        sections.append(header + "\n\n" + "\n\n".join(kept_paragraphs))
    return "\n\n".join(sections) + ("\n" if sections else "")


# ---------------------------------------------------------------------------
# Top-level merge orchestration
# ---------------------------------------------------------------------------


def _collect_am_by_path(
    auto_memory_files: Iterable[AutoMemoryFile],
) -> dict[str, AutoMemoryFile]:
    """Index :class:`AutoMemoryFile` records by resolved absolute-path string."""
    by_path: dict[str, AutoMemoryFile] = {}
    for am in auto_memory_files:
        try:
            by_path[str(am.path.resolve())] = am
        except OSError:
            by_path[str(am.path)] = am
    return by_path


def merge_cluster_row(
    row: dict[str, Any],
    *,
    extra_roots: list[Path],
    am_by_path: dict[str, AutoMemoryFile],
    ephemeral_scopes: list[str] | None = None,
    operational_markers: list[str] | None = None,
    as_of: date | None = None,
) -> MergedWikiEntry | None:
    """Build one :class:`MergedWikiEntry` from a cluster JSONL row.

    Returns ``None`` when every member path fails to resolve to a live
    file on disk — C2's rotated reports may reference files that have
    been removed between runs, and we prefer to skip such rows with a
    log line rather than crash the whole merge pass.

    ``as_of`` (issue athenaeum#359, compile-as-of) rewinds the per-member active
    predicate: a member is excluded when ``is_inactive(as_of)`` — its
    ``valid_until`` had already passed on ``as_of`` OR it carries a
    tombstone. Left ``None`` (the default) the predicate keys on today,
    matching the live compile. This is VALID-time, not transaction-time:
    a member ingested after ``as_of`` but whose validity window covers
    ``as_of`` is still blended (see :func:`compile_as_of`).

    C4 (athenaeum#198): contradiction detection is NOT performed here — the caller
    (:func:`merge_clusters_to_wiki`) runs it against the resolved member
    list and sets ``contradictions_detected`` + ``contradiction`` on the
    return value before rendering. This keeps ``merge_cluster_row`` a pure
    function over the JSONL row and member bodies.
    """
    cluster_id = str(row.get("cluster_id", ""))
    member_paths_raw: list[str] = [str(m) for m in row.get("member_paths", [])]
    centroid_score_raw = row.get("centroid_score", 1.0)
    try:
        centroid_score = float(centroid_score_raw)
    except (TypeError, ValueError):
        centroid_score = 1.0
    # Issue athenaeum#421: complete-linkage coherence metric. Pre-athenaeum#421 rows lack the
    # field; default 1.0 (treated as a clique — nothing to suppress).
    min_pairwise_raw = row.get("min_pairwise_score", 1.0)
    try:
        min_pairwise_score = float(min_pairwise_raw)
    except (TypeError, ValueError):
        min_pairwise_score = 1.0

    members: list[tuple[str, AutoMemoryFile]] = []
    resolved_member_paths: list[str] = []
    for mp in member_paths_raw:
        resolved = resolve_member_path(mp, extra_roots)
        if resolved is None:
            log.warning(
                "cluster %s: member %s did not resolve; skipping that member",
                cluster_id,
                mp,
            )
            continue
        key = str(resolved)
        am = am_by_path.get(key)
        if am is None:
            # The clusters file referenced a real file that C1 didn't
            # discover (e.g. intermediate edits mid-run). Build a minimal
            # shim so we can still read its body + frontmatter — this
            # keeps C3 resilient to discovery skew.
            try:
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                log.warning(
                    "cluster %s: %s unreadable; skipping that member",
                    cluster_id,
                    resolved,
                )
                continue
            meta, _ = parse_frontmatter(text)
            scope_guess = resolved.parent.name
            origin_session_id = meta.get("originSessionId") if meta else None
            origin_turn_raw = meta.get("originTurn") if meta else None
            try:
                origin_turn = (
                    int(cast(Any, origin_turn_raw)) if origin_turn_raw is not None else None
                )
            except (TypeError, ValueError):
                origin_turn = None
            sources_raw = meta.get("sources") if meta else None
            if isinstance(sources_raw, list):
                sources = [str(s) for s in sources_raw if isinstance(s, str)]
            else:
                sources = []
            try:
                shim_refines = parse_refines(meta if meta else None)
                shim_supersedes = parse_supersedes(meta if meta else None)
            except ValueError as exc:
                log.warning(
                    "cluster %s shim: invalid refines/supersedes on %s (%s); treating as empty",
                    cluster_id,
                    resolved,
                    exc,
                )
                shim_refines = []
                shim_supersedes = []
            # Issue athenaeum#181: same self-reference lint as discover_auto_memory_files.
            shim_name = str(meta.get("name", "")) if meta else ""
            shim_refines, shim_supersedes = _strip_self_reference(
                shim_name, shim_refines, shim_supersedes, resolved
            )
            am = AutoMemoryFile(
                path=resolved,
                origin_scope=scope_guess,
                memory_type="unknown",
                name=shim_name,
                description=str(meta.get("description", "")) if meta else "",
                origin_session_id=(
                    str(origin_session_id) if origin_session_id is not None else None
                ),
                origin_turn=origin_turn,
                sources=sources,
                refines=shim_refines,
                supersedes=shim_supersedes,
                # Issue athenaeum#191: non-destructive inactive markers.
                superseded_by=parse_superseded_by(meta if meta else None),
                deprecated=parse_deprecated(meta if meta else None),
                # Issue athenaeum#308: claim-level temporal validity bounds.
                valid_from=validity_bound_str(meta if meta else None, "valid_from"),
                valid_until=validity_bound_str(meta if meta else None, "valid_until"),
                # Issue athenaeum#904: optional decay bucket.
                bucket=parse_bucket(meta if meta else None),
            )
        # Issue athenaeum#278: secondary ephemeral guard. discover_auto_memory_files
        # already drops ephemeral intake, so the only way one reaches here is
        # a STALE cluster JSONL row referencing a file C1 no longer discovers
        # (the shim path above). Re-classify every resolved member so such a
        # stray can never materialize a durable page. Reads the member's own
        # frontmatter + body when the C1 record (which has no body) is the
        # shim; the strong scope-glob / ``ephemeral:true`` signals fire either
        # way. No-op when no patterns are configured.
        if ephemeral_scopes or operational_markers:
            try:
                _mtext = am.path.read_text(encoding="utf-8")
                _mmeta, _mbody = parse_frontmatter(_mtext)
            except (OSError, UnicodeDecodeError):
                _mmeta, _mbody = {}, ""
            eph_reason = classify_ephemeral(
                am.origin_scope,
                _mmeta,
                _mbody,
                ephemeral_scopes=ephemeral_scopes or [],
                operational_markers=operational_markers or [],
            )
            if eph_reason is not None:
                log.info(
                    "cluster %s: member %s is ephemeral (%s); excluding from compile",
                    cluster_id,
                    mp,
                    eph_reason,
                )
                continue
        # Issue athenaeum#191: skip members marked inactive (superseded_by / deprecated)
        # so their bodies are never composed into the wiki entry and they do
        # not contribute sources. Inactive files stay on disk for audit.
        # Issue athenaeum#359: ``as_of`` rewinds this member predicate for compile-as-of.
        if am.is_inactive(as_of):
            log.info(
                "cluster %s: member %s is inactive (superseded/deprecated); excluding from compile",
                cluster_id,
                mp,
            )
            continue
        members.append((mp, am))
        resolved_member_paths.append(mp)

    if not members:
        # Either no members resolved, or every resolved member is inactive
        # (athenaeum#191) — skip the row entirely; there is no live claim to compile.
        log.info("cluster %s: no active members; skipping row", cluster_id)
        return None

    topic_slug = derive_topic_slug(resolved_member_paths, cluster_id)
    origin_scopes_set: list[str] = []
    for _mp, am in members:
        if am.origin_scope not in origin_scopes_set:
            origin_scopes_set.append(am.origin_scope)

    # Issue athenaeum#904: page-level decay bucket, first non-empty ACTIVE member
    # wins (deterministic — ``members`` is already filtered to active-only,
    # in cluster-row order). Members disagreeing on bucket is not validated
    # here — the rare case an operator's own rules would need to reconcile,
    # not something this compile step should escalate or silently average.
    bucket = ""
    for _mp, am in members:
        if am.bucket:
            bucket = am.bucket
            break

    # Sources: parse each member's sources[] from frontmatter (source of
    # truth), plus a synthetic entry from originSessionId/turn when a
    # member has no sources[] at all.
    raw_sources: list[dict[str, Any]] = []
    for _mp, am in members:
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        meta, _ = parse_frontmatter(text) if text else ({}, "")
        sources_raw = meta.get("sources") if meta else None
        if isinstance(sources_raw, list) and sources_raw:
            for s in sources_raw:
                parsed = _parse_one_source(s, am.origin_scope)
                if parsed is not None:
                    # Issue athenaeum#308 (slice 4): the member's temporal validity window
                    # travels with each claim it cites into the compiled entry.
                    _stamp_member_validity(parsed, am)
                    raw_sources.append(parsed)
        else:
            implicit = _am_as_implicit_source(am)
            if implicit is not None:
                _stamp_member_validity(implicit, am)
                raw_sources.append(implicit)

    deduped = dedupe_sources(raw_sources)

    # Body: concatenate member bodies (minus frontmatter) with a scope/
    # filename header and paragraph-level dedupe.
    member_bodies: list[tuple[str, str, str]] = []
    for _mp, am in members:
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        _, body = parse_frontmatter(text)
        member_bodies.append((am.origin_scope, am.path.name, body))

    body = synthesize_body(member_bodies)

    return MergedWikiEntry(
        topic_slug=topic_slug,
        cluster_id=cluster_id,
        cluster_centroid_score=centroid_score,
        min_pairwise_score=min_pairwise_score,
        # Default False here; merge_clusters_to_wiki() overrides based on
        # the C4 contradiction-detector result before rendering.
        contradictions_detected=False,
        origin_scopes=origin_scopes_set,
        sources=deduped,
        body=body,
        member_paths=resolved_member_paths,
        resolved_members=[am for _mp, am in members],
        bucket=bucket,
    )


def _is_low_cohesion_cross_scope(
    entry: MergedWikiEntry,
    *,
    floor: float,
    min_scopes: int,
) -> bool:
    """True when *entry* matches the low-cohesion cross-scope over-cluster signature.

    Issue athenaeum#278. The cross-scope ``similarity`` clustering path over-clusters:
    single-linkage chains a coherent source doc with vaguely-similar
    operational notes from many scopes into one low-cohesion blend page. The
    gate fires only when ALL hold:

    * the floor is active (``floor > 0`` -- the feature is opt-in);
    * the cluster's mean intra-cohesion is STRICTLY below the floor
      (``cluster_centroid_score < floor`` -- a cluster sitting exactly at the
      floor materializes; the boundary is inclusive-keep); and
    * the cluster spans at least *min_scopes* distinct origin scopes (the
      cross-scope signature).

    Gating on BOTH low cohesion AND multi-scope origin is deliberate: a
    low-cohesion SINGLE-scope cluster (legitimately diverse intake from one
    project) and a small coherent cluster must NOT be suppressed. Singletons
    (``cluster_centroid_score == 1.0``, one scope) never trip either arm.
    """
    if floor <= 0.0:
        return False
    if entry.cluster_centroid_score >= floor:
        return False
    return len(entry.origin_scopes) >= min_scopes


def _classify_merge_write_kind(merge_target_name: str, wiki_root: Path) -> str:
    """Classify a merge proposal by whether its target slug already exists (athenaeum#421).

    Thin delegator to
    :func:`athenaeum.pending_merges.classify_write_kind` — the single source
    of truth for this classification (issue athenaeum#748). Kept as a named
    entry point in this module for the proposal-time callers (and their tests)
    that already import it; the logic lives in ``pending_merges`` so
    :func:`~athenaeum.pending_merges.write_pending_merge` can derive the same
    value at write time without reintroducing the ``pending_merges`` ->
    ``merge`` back-edge dissolved by issue athenaeum#640.
    """
    return classify_write_kind(merge_target_name, wiki_root)


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


def render_source_footnotes(sources: list[dict[str, Any]]) -> str:
    """Render ``[^name]: **Source:** ...`` footnotes for a source list (athenaeum#260).

    Each origin-traced source becomes one Markdown footnote definition
    carrying its ``source_type`` + ``source_ref``, matching the worked
    example's ``[^name]: **Source:** ...`` style
    (``wiki/0a1b2c3d-ada-lovelace.md``). Labels are stable (``src-1``,
    ``src-2``, ...) over the deterministic deduped source order.

    The ULTIMATE-source rule is preserved here: the rendered ref is the
    source's ``source_ref`` (session+turn / URL / document path), back-filled
    from session+turn when absent — never the raw ``auto-memory/...``
    filename. Returns ``""`` for an empty source list.

    Issue athenaeum#262 (slice C of athenaeum#259): when a source carries the granular
    ``claim`` text moved into this entry (and a resolved ``verdict`` /
    disposition, when one exists), they are appended to the footnote so the
    wiki fact keeps a footnote-level diff target for future intake — the
    contradiction engine now compares new memories against THIS, not the
    retired raw atom. Both are optional; pre-slice-C sources render exactly
    as before.

    Issue athenaeum#308 (slice 4): when a source carries a per-claim temporal validity
    window (``valid_from`` / ``valid_until``, stamped from the contributing
    member), a ``— **Valid:** <window>`` clause is appended. Optional — a
    source with no window (open interval) renders exactly as before.
    """
    lines: list[str] = []
    for i, src in enumerate(sources, 1):
        source_type = coerce_source_type(src.get("source_type"))
        source_ref = src.get("source_ref") or _default_source_ref(src)
        text = f"**Source:** {source_type}"
        if source_ref:
            text += f" — `{source_ref}`"
        scope = src.get("origin_scope")
        if scope:
            text += f" (origin scope `{scope}`)"
        excerpt = src.get("excerpt")
        if excerpt:
            text += f': "{excerpt}"'
        claim = src.get("claim")
        if claim is not None and str(claim).strip():
            text += f' — **Claim:** "{str(claim).strip()}"'
        verdict = src.get("verdict")
        if verdict is not None and str(verdict).strip():
            text += f" — **Verdict:** {str(verdict).strip()}"
        # Issue athenaeum#308 (slice 4): per-claim compiled validity window. Optional —
        # a source with no window (open interval) renders exactly as before.
        window = _validity_window_phrase(src)
        if window:
            text += f" — **Valid:** {window}"
        lines.append(f"[^src-{i}]: {text}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def render_merged_entry(entry: MergedWikiEntry) -> str:
    """Render a :class:`MergedWikiEntry` as a full wiki markdown file.

    Frontmatter shape:
    - Always present: ``name``, ``type``, ``cluster_id``,
      ``cluster_centroid_score``, ``contradictions_detected``,
      ``origin_scopes``, ``sources``.
    - When ``contradictions_detected`` is true: ``status`` is set to
      :data:`CONTRADICTION_STATUS_FLAGGED`. When false, the ``status`` key
      is OMITTED entirely (absence = clean) — see module-level comment.
    """
    meta: dict[str, Any] = {
        "name": entry.topic_slug,
        "type": "auto-memory",
        "cluster_id": entry.cluster_id,
        "cluster_centroid_score": round(entry.cluster_centroid_score, 4),
        "contradictions_detected": bool(entry.contradictions_detected),
        "origin_scopes": list(entry.origin_scopes),
        "sources": list(entry.sources),
    }
    if entry.contradictions_detected:
        meta["status"] = CONTRADICTION_STATUS_FLAGGED
        if entry.contradiction is not None and entry.contradiction.conflict_type:
            meta["contradiction_type"] = entry.contradiction.conflict_type
    # Issue athenaeum#261: mark the entry as a retired-on-move long-term memory.
    if entry.retired:
        meta["retired"] = True
    # Issue athenaeum#904: page-level decay bucket, alongside the existing
    # ``valid_from``/``valid_until`` validity fields (athenaeum#308, per-source —
    # see ``render_source_footnotes``). Omitted entirely when unset, same
    # omit-at-default rule every optional field in this dict follows.
    if entry.bucket:
        meta["bucket"] = entry.bucket
    # Issue athenaeum#260: append origin-traced source footnotes to the BODY (sources
    # already render to frontmatter above; the footnotes give the human-
    # readable, ultimate-source citation the worked example used).
    body = entry.body
    footnotes = render_source_footnotes(entry.sources)
    if footnotes:
        sep = "" if body.endswith("\n") or not body else "\n"
        body = f"{body}{sep}\n{footnotes}"
    return render_frontmatter(meta) + "\n" + body


def _off_corpus_erasure_class_slugs(
    config: dict[str, Any] | None, knowledge_root: Path
) -> set[str]:
    """Slugs already present in the off-corpus store (issue athenaeum#1116 AC1).

    :mod:`athenaeum.erasure`'s ``classify_inference_taint`` needs the set of
    slugs that are ALREADY erasure-class to decide whether a compiled
    ``## Inference`` block's basis taints its page. This module has no
    page-level ``data_class`` classification of its own to consult (that is
    :mod:`athenaeum.erasure`'s territory, out of scope here) — the live
    signal a wired system actually has is off-corpus STORE MEMBERSHIP
    itself: a page already routed off-corpus (by this same routing, by the
    answers lane's re-ingestion classification, or by an operator) is
    exactly what "erasure-class content" cashes out to once a real
    off-corpus surface exists. Empty when off-corpus is not configured (the
    common case today) — see :func:`_route_merged_entry_write`'s docstring
    for the off-corpus-absent posture that follows from that.
    """
    from athenaeum.off_corpus import off_corpus_adapter, off_corpus_store

    store = off_corpus_store(config, knowledge_root)
    if store is None:
        return set()
    adapter = off_corpus_adapter(config)
    assert adapter is not None  # off_corpus_store already returned non-None
    slugs: set[str] = set()
    for meta in store.iter_meta(adapter.name):
        stem = Path(meta.key.key).stem
        slugs.add(stem)
        if stem.startswith(AUTO_WIKI_PREFIX):
            slugs.add(stem[len(AUTO_WIKI_PREFIX) :])
    return slugs


def _route_merged_entry_write(
    entry: MergedWikiEntry,
    text: str,
    *,
    wiki_root: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None,
    erasure_class_slugs: set[str],
) -> Path | None:
    """Write one compiled entry, routing a derivation-tainted page off-corpus
    instead of the ordinary git-tracked corpus (issue athenaeum#1116 AC1).

    A page is tainted when one of its ``## Inference`` blocks' ``**Basis**``
    cites a slug in *erasure_class_slugs*
    (:func:`athenaeum.erasure.classify_inference_taint`) — "a paraphrase in
    git is the same leak as a quote" (that function's docstring).

    **Reversible default (issue athenaeum#1116).** When off-corpus IS
    configured, a tainted page is written there instead of under
    ``wiki_root`` and this function returns ``None`` — nothing lands in the
    ordinary corpus. When off-corpus is NOT configured, there is nothing to
    route to; hard-failing every deployment that has not configured
    off-corpus would be worse than the gap this issue closes, so the page
    still lands in the ordinary corpus exactly as it did before this
    wiring, but a structured, greppable WARNING names the taint and the
    page so the gap is visible in logs instead of silent. This default is
    reversible — revisit if review prefers a hard failure instead.

    Returns the path written under ``wiki_root``, or ``None`` when the page
    was routed off-corpus instead (so callers can log accordingly).
    """
    from athenaeum.erasure import classify_inference_taint

    tainted = classify_inference_taint(text, erasure_class_slugs=erasure_class_slugs)
    if tainted:
        basis_slugs = sorted({basis for block in tainted for basis in block.basis})
        from athenaeum.off_corpus import off_corpus_adapter, off_corpus_store
        from athenaeum.store import StoreKey

        store = off_corpus_store(config, knowledge_root)
        if store is not None:
            adapter = off_corpus_adapter(config)
            assert adapter is not None  # off_corpus_store already returned non-None
            store.put(
                StoreKey(surface=adapter.name, key=entry.filename), text.encode("utf-8")
            )
            # Propagate within this same run: a later entry's basis may cite
            # THIS entry, and it must see it as erasure-class too.
            erasure_class_slugs.add(entry.topic_slug)
            log.info(
                "merge: routed %s off-corpus (athenaeum#1116 AC1 - %d inference "
                "block(s) derived from erasure-class basis %s)",
                entry.filename,
                len(tainted),
                basis_slugs,
            )
            return None
        log.warning(
            "erasure-taint-not-routed: %s carries %d inference block(s) derived "
            "from erasure-class basis %s but no off-corpus surface is configured "
            "(off_corpus.enabled=false) - writing to the ordinary corpus "
            "(athenaeum#1116)",
            entry.filename,
            len(tainted),
            basis_slugs,
        )

    page_path = wiki_root / entry.filename
    atomic_write_text(page_path, text)
    return page_path


def merge_clusters_to_wiki(
    knowledge_root: Path,
    *,
    auto_memory_files: Iterable[AutoMemoryFile] | None = None,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    client: "LLMBackend | None" = None,
    resolve_client: "LLMBackend | None" = None,
    reasoning_t1_client: "LLMBackend | None" = None,
    reasoning_t2_client: "LLMBackend | None" = None,
    usage: TokenUsage | None = None,
    now: datetime | None = None,
    as_of: date | None = None,
    out_wiki_root: Path | None = None,
    only_cluster_ids: set[str] | None = None,
    deadline: float | None = None,
    max_api_calls: int | None = None,
    out_stats: dict | None = None,
    heartbeat: Callable[[], None] | None = None,
    c4_since: datetime | None = None,
    c4_full_sweep: bool = False,
) -> list[MergedWikiEntry]:
    """Read the canonical cluster JSONL and emit one wiki entry per cluster.

    Args:
        knowledge_root: Root of the knowledge directory (where ``wiki/``,
            ``raw/``, and ``athenaeum.yaml`` live).
        auto_memory_files: Optional pre-discovered list of
            :class:`AutoMemoryFile` records (pass the exact list C1's
            discovery returned in the same run to avoid double-scanning).
            When ``None``, this function lazily imports and calls
            :func:`athenaeum.librarian.discover_auto_memory_files`.
        config: Optional resolved config dict.
        dry_run: If True, build the entries in memory but do NOT write
            to ``wiki/``. Returns the entries for caller inspection.
        client: Optional live LLM client for the ``classify`` knob, used by
            the C4 contradiction detector. When ``None`` (e.g.
            ``ANTHROPIC_API_KEY`` unset), the detector is skipped with a
            deterministic ``detected=False`` fallback — see
            :func:`athenaeum.contradictions.detect_contradictions`.
        resolve_client: Issue athenaeum#841. The ``resolve`` knob's client, used by
            the Opus resolver (:func:`athenaeum.resolutions.propose_resolution`).
            ``None`` (every pre-athenaeum#841 caller) falls back to *client* —
            byte-identical to the pre-athenaeum#841 single-client behavior.
        reasoning_t1_client: Issue athenaeum#841. The ``reasoning_t1`` knob's
            client, used by the T1 merge-proposal screen
            (:func:`t1_screen_rejects_merge_proposal` /
            :func:`athenaeum.reasoning_tiers.run_t1_tier`). ``None`` falls
            back to *client*.
        reasoning_t2_client: Issue athenaeum#841. The ``reasoning_t2`` knob's
            client, used by the T2 merge-proposal screen
            (:func:`t2_screen_merge_proposal` /
            :func:`athenaeum.reasoning_tiers.run_t2_tier`). ``None`` falls
            back to *client*.
        usage: Optional run-level :class:`TokenUsage` (issue athenaeum#220). When
            provided AND a live client is present, every detector (Haiku)
            and resolver (Opus) call increments ``usage.api_calls`` so the
            librarian's run-level budget sees this phase's spend. Each
            response's token + cache counts are accumulated by the callee
            (athenaeum#239), so the run summary's cache line also reflects this
            phase's traffic.
        now: Optional run-start timestamp (issue athenaeum#251). Injected for
            deterministic read-time decay of stale auto ``not_a_conflict``
            suppressions — a single frozen ``now`` is compared against each
            cached row's ``resolved_at``. Defaults to ``datetime.now(UTC)``
            (frozen once here so all clusters in the run share one clock).
            Tests pass a fixed value so no wall-clock leaks into assertions.
        as_of: Issue athenaeum#359 (compile-as-of). Rewinds the per-member active
            predicate (``is_inactive(as_of)``) so the deterministic C3 blend
            re-derives each entry from only the members valid on ``as_of`` —
            a member expired now but valid then is RE-INCLUDED. ``None`` (the
            default) keys on today, matching the live compile. Distinct from
            slice 3's read-time ``--as-of`` filter, which only hides
            already-compiled pages and cannot resurrect a dropped member's
            content. See :func:`compile_as_of`.
        out_wiki_root: Issue athenaeum#359. Redirect the wiki write target (and the
            ``_pending_*`` sidecars) to this directory instead of
            ``knowledge_root / "wiki"``. Used by compile-as-of to write a
            recompiled snapshot into a scratch dir WITHOUT mutating the live
            wiki. ``None`` (the default) writes to the live wiki.
        only_cluster_ids: Issue athenaeum#370 PR2 (delta compile). When set, ONLY the
            cluster rows whose ``cluster_id`` is in this set are merged and
            written — every unaffected ``wiki/auto-*.md`` is left untouched. The
            caller (:func:`athenaeum.librarian.run` on the deterministic
            ``client=None`` path) guarantees these ids do not slug-collide with
            any unaffected entry before scoping the merge, and the cross-scope
            similarity sweep is skipped (it is whole-corpus by nature and only
            runs on the full path). ``None`` (the default) merges every cluster
            — today's whole-corpus behaviour, byte-for-byte.
        max_api_calls: Issue athenaeum#461. Optional run-level API call ceiling. When
            set AND not a dry-run AND ``usage`` is provided, the C4 detector
            call sites (primary per-cluster pass and the cross-scope
            similarity sweep) are skipped once ``usage.api_calls`` has already
            reached this ceiling — degrading exactly like the deterministic
            ``detected=False`` short-circuits above (``rationale=
            "budget-exhausted"``), so a run whose entity phase already spent
            the shared budget does not let C4 burn further past it. ``None``
            (the default) preserves today's unbounded behaviour byte-for-byte.
        out_stats: Issue athenaeum#464 (slice E of athenaeum#460). Optional mutable out-param
            (mirrors :func:`athenaeum.librarian._compile_auto_memory`'s
            ``out_delta_taken`` convention). When given, populated immediately
            before EITHER return site with the detector/resolver call-count
            breakdown this call accumulated — ``haiku_calls``,
            ``resolve_calls``, ``chunks_run``, ``pairs_added_via_similarity``,
            ``entries_merged`` (``len(entries)``), and ``escalations_written``
            (``len(escalations)``) — so the run-level profile summary (athenaeum#464)
            can thread these counters up without recomputing them. Issue
            athenaeum#1177 (AC4) additionally sets ``haiku_calls_succeeded`` and
            ``resolve_calls_succeeded`` — the SUBSET of ``haiku_calls`` /
            ``resolve_calls`` (attempted) whose response actually landed
            (tokens recorded), so a run where every attempt errored reports
            that disagreement explicitly rather than only ever exposing the
            attempted count under a name a reader could mistake for results.
            Purely additive; ``None`` (every pre-athenaeum#464 caller) is byte-identical.
            Issue athenaeum#909 additionally sets ``c4_swept_full`` (``bool``) — whether
            this call's EFFECTIVE ``only_cluster_ids`` (after ``c4_since``
            scoping below, if it engaged) ended up ``None``, i.e. a true
            whole-corpus C4 pass ran. The caller (:func:`athenaeum.librarian.run`)
            only advances the contradiction-sweep-completed stamp when this
            is ``True``.
        c4_since: Issue athenaeum#909. A C4-SPECIFIC "scope to clusters touched
            since this timestamp" gate, ORTHOGONAL to ``only_cluster_ids``
            above (the athenaeum#370/#463 auto-memory delta gate C4 otherwise just
            piggybacks on). Only takes effect when ``only_cluster_ids is
            None`` on entry (the delta gate already left this call
            unscoped) AND ``c4_full_sweep`` is ``False`` — in that case this
            function computes which cluster rows have at least one member
            file modified at/after ``c4_since`` and uses THAT set exactly
            like an ``only_cluster_ids`` the caller had passed in (same row
            filter, same "unaffected wiki pages stay untouched" property,
            same whole-corpus-similarity-sweep skip). ``None`` (the default,
            and every pre-athenaeum#909 caller) never engages this — byte-identical
            to today.
        c4_full_sweep: Issue athenaeum#909. When ``True``, ``c4_since`` is ignored —
            this call runs C4 over EVERY cluster regardless (the explicit
            ``--full-contradiction-sweep`` escape hatch, AC6). Only
            meaningful together with a caller that also then advances the
            contradiction-sweep stamp (via ``out_stats["c4_swept_full"]``
            above); this flag alone has no stamp side effect. Default
            ``False``.

    Returns:
        The list of :class:`MergedWikiEntry` records in cluster-file order.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    # Issue athenaeum#841: each knob-specific client falls back to *client* (the
    # ``classify`` knob's client, used for C4 detect) when the caller did not
    # pass one — every pre-athenaeum#841 caller only ever set ``client=``, so this
    # keeps their behavior byte-identical while a caller that DOES resolve
    # per-knob clients (:func:`athenaeum.librarian._run_merge_only_phase` /
    # ``_compile_auto_memory``) gets genuine per-knob routing. Reassigning the
    # parameter names directly (rather than introducing ``_effective_*``
    # locals) lets every nested closure below keep referencing them unchanged.
    resolve_client = resolve_client if resolve_client is not None else client
    reasoning_t1_client = (
        reasoning_t1_client if reasoning_t1_client is not None else client
    )
    reasoning_t2_client = (
        reasoning_t2_client if reasoning_t2_client is not None else client
    )
    # Issue athenaeum#568 (H7): the active provider, resolved once so both C4 loop heads
    # below can consult ``spend.ceiling_tripped`` (the ceiling's UNIT — tokens
    # for the subscription path, dollars for the metered API path — is keyed on
    # it). Mirrors ``librarian.run``'s single ``resolve_provider(config)`` read.
    resolved_provider = resolve_provider(resolved_config)
    # Issue athenaeum#518 / athenaeum#1200: the two reasoning-tier screens, resolved
    # ONCE EACH, from their OWN independent flags — before athenaeum#1200 both were
    # fed by a single combined boolean, which meant arming the harmless T1
    # screen also armed T2's unreviewed auto-apply. Both still default OFF —
    # production merge behavior is byte-identical to today until an operator
    # opts in to EACH. When T1 is on, it screens each merge proposal before
    # it reaches the human queue (a confident reject drops it; a pass-up
    # flows through unchanged). When T2 is on, a T1 pass-up may be
    # auto-finalized within the safe class (see `t2_screen_merge_proposal`'s
    # own docstring) with NO human review. The authority manifest (loaded
    # once, only when EITHER screen is enabled) feeds T1's
    # live-source-duplicate check and T2's own tier call; a missing manifest
    # is an inert empty one.
    reasoning_t1_enabled = resolve_reasoning_tier_auditing_enabled(resolved_config)
    reasoning_t2_auto_apply_enabled = resolve_reasoning_tier_t2_auto_apply_enabled(
        resolved_config
    )
    reasoning_authority_manifest = (
        load_authority_manifest_for_pipeline(knowledge_root)
        if (reasoning_t1_enabled or reasoning_t2_auto_apply_enabled)
        else None
    )
    # Issue athenaeum#398: resolved once and threaded into every dark-zone
    # PhaseHeartbeat below (merge-detect, merge-write) so an operator can
    # tune the tick cadence via ATHENAEUM_HEARTBEAT_INTERVAL / yaml without
    # touching call sites.
    heartbeat_interval = resolve_heartbeat_interval(resolved_config)
    cluster_path = resolve_cluster_output_path(knowledge_root, config=resolved_config)
    rows = read_cluster_rows(cluster_path)
    if not rows:
        log.info("merge pass: no clusters at %s — nothing to merge", cluster_path)
        return []

    # Issue athenaeum#909: the C4-specific "since last completed sweep" gate.
    # Only engages when the athenaeum#370/#463 delta gate above left this call
    # UNSCOPED (``only_cluster_ids is None``) and the caller did not force a
    # true full sweep — computed BEFORE the row filter below so it reuses
    # that exact same filter (same untouched-page-stays-untouched property,
    # same whole-corpus-similarity-sweep skip guard).
    if only_cluster_ids is None and c4_since is not None and not c4_full_sweep:
        since_extra_roots = resolve_extra_intake_roots(
            knowledge_root, config=resolved_config
        )
        only_cluster_ids = _rows_touched_since(rows, since_extra_roots, c4_since)

    # Issue athenaeum#370 PR2: delta-scoped merge. Filter to the affected cluster rows
    # BEFORE building any entry so unaffected entries are neither rebuilt nor
    # rewritten (proving the "untouched entries stay byte + mtime identical"
    # equivalence property). Order among the surviving rows is preserved.
    if only_cluster_ids is not None:
        rows = [r for r in rows if str(r.get("cluster_id", "")) in only_cluster_ids]
        if not rows:
            log.info(
                "merge pass: delta scope matched no cluster rows — nothing to merge"
            )
            return []

    extra_roots = resolve_extra_intake_roots(knowledge_root, config=resolved_config)

    if auto_memory_files is None:
        auto_memory_files = discover_auto_memory_files(
            knowledge_root,
            config=resolved_config,
        )

    am_by_path = _collect_am_by_path(auto_memory_files)

    # Issue athenaeum#278: resolve the secondary ephemeral guard inputs once.
    ephemeral_scopes = resolve_ephemeral_scopes(resolved_config)
    operational_markers = resolve_operational_markers(resolved_config)

    entries: list[MergedWikiEntry] = []
    for row in rows:
        # Issue athenaeum#396: wall-clock deadline check at the C3 cluster-merge
        # boundary. Cheap (a monotonic read) and only active when the run
        # armed a deadline; keeps a stalled/slow merge pass from running past
        # the run-level cap. Raised so run() commits partial + exits
        # EXIT_GRACEFUL_PARTIAL (75, issue athenaeum#897).
        if deadline is not None and time.monotonic() >= deadline:
            raise RunDeadlineExceeded("C3 cluster merge")
        entry = merge_cluster_row(
            row,
            extra_roots=extra_roots,
            am_by_path=am_by_path,
            ephemeral_scopes=ephemeral_scopes,
            operational_markers=operational_markers,
            as_of=as_of,
        )
        if entry is None:
            continue
        entries.append(entry)

    # Issue athenaeum#278: cluster-cohesion floor. Refuse to materialize a low-cohesion
    # cross-scope OVER-CLUSTER -- a single-linkage chain that blends a coherent
    # source doc with vaguely-similar operational notes from many scopes -- into
    # a durable wiki page. Suppressed entries are dropped from ``entries`` here,
    # BEFORE contradiction detection and the write loop, and so never reach the
    # returned list the retire pass walks: their raw members are left in place
    # (NOT retired, NOT lost) for a coherent cluster to absorb on a later run.
    # They remain in ``auto_memory_files``, so the similarity sweep (modes
    # ``similarity``/``both``) can still detect contradictions involving them;
    # in the DEFAULT ``ancestor`` mode only a suppressed member in an ancestor
    # scope of a KEPT cluster is re-examined (pooled into that cluster), so a
    # contradiction internal to a suppressed blend is not re-detected by
    # default. The gate is default-off (floor 0.0) -- when off this loop is a
    # no-op pass-through.
    cohesion_floor = resolve_min_cluster_cohesion(resolved_config)
    cohesion_min_scopes = resolve_min_cluster_cohesion_scopes(resolved_config)
    # Issue athenaeum#421: the clustering threshold the merge-proposal complete-linkage
    # gate compares each cluster's minimum pairwise cosine against. Resolved
    # once (same value the C2 cluster pass used) and closed over by
    # ``_emit_escalation`` below.
    merge_cluster_threshold = resolve_cluster_threshold(
        knowledge_root, config=resolved_config
    )
    if cohesion_floor > 0.0:
        kept: list[MergedWikiEntry] = []
        for entry in entries:
            if _is_low_cohesion_cross_scope(
                entry, floor=cohesion_floor, min_scopes=cohesion_min_scopes
            ):
                log.info(
                    "merge: SUPPRESSED low-cohesion cross-scope cluster %s "
                    "(centroid=%.4f < floor=%.4f, scopes=%d >= %d); leaving raw "
                    "members in place (not materialized, not retired)",
                    entry.cluster_id,
                    entry.cluster_centroid_score,
                    cohesion_floor,
                    len(entry.origin_scopes),
                    cohesion_min_scopes,
                )
                continue
            kept.append(entry)
        entries = kept

    # Topic-slug collisions: if two clusters derive the same slug, suffix
    # each after the first with a short cluster_id tail so filenames stay
    # distinct. Rare but possible when two clusters share dominant tokens.
    slug_counts: dict[str, int] = {}
    for entry in entries:
        base = entry.topic_slug
        if base in slug_counts:
            slug_counts[base] += 1
            suffix = re.sub(r"[^a-z0-9]+", "-", entry.cluster_id.lower()).strip("-")
            entry.topic_slug = (
                f"{base}-{suffix}" if suffix else f"{base}-{slug_counts[base]}"
            )
        else:
            slug_counts[base] = 1

    # C4 (athenaeum#198 + athenaeum#125): claim-level contradiction detection.
    #
    # Mode toggle (issue athenaeum#125, ATHENAEUM_CROSS_SCOPE_MODE):
    # - off: per-cluster only (legacy behavior).
    # - ancestor (default): pool each cluster with ancestor-scope members
    #   then chunk by cap before running the detector.
    # - similarity: per-cluster pass + cosine sweep over raw + wiki.
    # - both: ancestor pooling THEN similarity sweep over remaining pairs.
    wiki_root = out_wiki_root if out_wiki_root is not None else knowledge_root / "wiki"
    escalations: list[EscalationItem] = []
    mode = resolve_cross_scope_mode(resolved_config)
    cluster_size_cap = resolve_cluster_size_cap(resolved_config)
    similarity_threshold = resolve_similarity_threshold(resolved_config)

    haiku_calls = 0
    # Issue athenaeum#1177 (AC4): ATTEMPTED (``haiku_calls`` above, unchanged)
    # vs SUCCEEDED, tracked separately so a run where the detector's calls
    # all errored (e.g. credits exhausted) cannot report ``detector_haiku``
    # as if it reflected real detections when the token ledger recorded
    # zero tokens for every one of them. Incremented via
    # ``usage.succeeded_calls`` (bumped only when a REAL response's tokens
    # were recorded, see ``TokenUsage.add_tokens``) diffed immediately
    # around each ``detect_contradictions`` call, rather than threading a
    # new return value through it — keeps that function's contract
    # unchanged.
    haiku_calls_succeeded = 0
    pairs_added_via_similarity = 0
    chunks_run = 0

    # Track which (path_a, path_b) pairs are already covered by a single
    # detector call so the similarity sweep can skip them.
    covered_pair_keys: set[tuple[str, str]] = set()

    # Issue athenaeum#146: dedup escalations by the SET OF FLAGGED SOURCE MEMBER FILES
    # across the whole run. The same source-file pair is pulled into many
    # overlapping clusters; detection runs per cluster, so without this set
    # one real conflict escalates once per cluster (28 questions → 9 distinct
    # conflicts on 2026-05-22). The key is the sorted flagged members from
    # the detector result (`members_involved`, i.e. source-file identity),
    # NOT the cluster `topic_slug`. Both the primary cluster pass and the
    # similarity sweep route through `_emit_escalation`, so a single set
    # there dedupes both passes.
    escalated_member_keys: set[tuple[str, ...]] = set()

    # Issue athenaeum#249: fingerprints already settled as not_a_conflict (auto OR human)
    # BEFORE this run started. Skipping ONLY this verdict is safe: other verdicts
    # (keep_a, correct_*, ...) must still flow to tier4_escalate so a prior HUMAN
    # verdict gets auto-enacted on the new page. load_resolved_records applies
    # human-supersedes-auto precedence, so a pair later overridden by a human
    # keep_a is NOT in this set.
    #
    # This is the SKIP gate and is frozen at run start on purpose: a pair the
    # resolver suppresses mid-run must NOT begin short-circuiting later clusters
    # in the SAME run, or it would silently drop a later cluster that the
    # resolver would genuinely re-detect (athenaeum#145/#146 contract — see
    # ``test_suppressed_pair_does_not_block_later_genuine_detection``). Only a
    # FUTURE run, reloading the cache fresh, treats this run's clearances as
    # settled.
    #
    # Issue athenaeum#251: read-time decay. With a positive
    # ``contradiction.not_a_conflict_ttl_days``, an AUTO suppression older
    # than the ttl is DROPPED from this skip set (treated as absent) so the
    # pair re-enters the Opus confirmation path. ``now`` is frozen once here
    # — the same instant ``record_resolution`` compares against and the same
    # run-start freeze the skip gate already uses — so every cluster in the
    # run decays against one clock. The cache file is NEVER mutated: an
    # expired row stays as history and is simply re-interpreted. Human and
    # enacting auto verdicts never decay (see ``is_stale_auto_suppression``).
    decay_now = now if now is not None else datetime.now(timezone.utc)
    ttl_days = resolve_not_a_conflict_ttl_days(resolved_config)
    cleared_not_a_conflict_fps = {
        fp
        for fp, rec in load_resolved_records(knowledge_root).items()
        if (rec.get("action") or rec.get("verdict")) == SUPPRESS_ACTION
        and not is_stale_auto_suppression(rec, ttl_days, decay_now)
    }
    # Write-dedup set (issue athenaeum#249, open-question #2): fingerprints written to the
    # cache during THIS run. Bounds file growth without feeding the skip gate
    # above — a mid-run clearance is recorded once but does not suppress later
    # re-detection of the same pair within the run.
    recorded_not_a_conflict_fps: set[str] = set()

    def _record_pair_keys(members: list[AutoMemoryFile]) -> None:
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                path_a, path_b = sorted((str(members[i].path), str(members[j].path)))
                covered_pair_keys.add((path_a, path_b))

    def _emit_escalation(
        entry: MergedWikiEntry,
        result: ContradictionResult,
        proposal: "ResolutionProposal | MergeProposal | None" = None,
        members: list[AutoMemoryFile] | None = None,
    ) -> None:
        if not result.detected:
            return
        # Lane 3 / issue athenaeum#169: resolver proposes the two snippets should
        # merge into a single canonical memory. Route the proposal to
        # ``wiki/_pending_merges.md`` for human approval (NOT auto-applied)
        # and DROP the would-be pending-question escalation — the same
        # conflict should not appear in both sidecars.
        if proposal is not None and proposal.action == PROPOSE_MERGE_ACTION:
            assert isinstance(proposal, MergeProposal)
            member_paths = [str(m.path) for m in (members or [])]
            # Issue athenaeum#400: suppress degenerate over-cluster merge proposals
            # (huge source count / low confidence) BEFORE they reach the human
            # queue. Dropping entirely — neither a merge proposal nor a
            # fallback pending-question escalation — is deliberate: a 1,700-
            # source blend is not a coherent contradiction either, so routing
            # it to the questions sidecar would just move the noise.
            _suppress = _merge_proposal_suppression_reason(
                n_sources=len(member_paths),
                confidence=proposal.confidence,
                config=resolved_config,
                mean_similarity=entry.cluster_centroid_score,
                min_pairwise=entry.min_pairwise_score,
                cluster_threshold=merge_cluster_threshold,
            )
            if _suppress is not None:
                # Issue athenaeum#1085: n_sources is recorded as its own structured
                # field, UNCONDITIONALLY — not parsed out of `_suppress`, which
                # only embeds the source count when the size-cap gate happens
                # to be the one that fired (gate ordering is pinned by
                # TestGateOrdering, so a cohesion/confidence/chain suppression
                # previously recorded no size at all).
                log.info(
                    "resolutions: SUPPRESSED degenerate merge proposal for "
                    "cluster %s (%s); n_sources=%d; not written to "
                    "_pending_merges.md",
                    entry.cluster_id,
                    _suppress,
                    len(member_paths),
                )
                return
            # Issue athenaeum#433: type-compatibility precheck. A cluster spanning >1
            # distinct memory_class values (docs/memory-taxonomy.md #3) may
            # not be mechanically merged — same-class only. Untyped members
            # (the overwhelming majority of raw auto-memory intake, which
            # carries no memory_class frontmatter) never trip this gate; it
            # only fires when 2+ DISTINCT typed classes are present (e.g. a
            # ``fact`` wiki page clustering with a ``guideline`` wiki page
            # via the merge-time cluster shim). Rejected clusters get a cite
            # proposal instead of a destructive merge.
            _rejection = cross_class_precheck(member_paths)
            if _rejection is not None:
                cite = build_cite_proposal(member_paths, _rejection)
                log.info(
                    "resolutions: REJECTED cross-class merge proposal for "
                    "cluster %s (%s); cite proposal built instead "
                    "(citing=%d, cited=%d); dropping pending-question "
                    "escalation",
                    entry.cluster_id,
                    _rejection.reason,
                    len(cite.citing),
                    len(cite.cited),
                )
                return
            # Issue athenaeum#518: T1 reasoning-tier screen (opt-in, default OFF). A
            # confident reject drops the proposal before the human queue —
            # mirroring the _suppress / cross_class_precheck drops above —
            # rather than spending human review on a merge the tier is certain
            # is wrong. A pass-up (or a disabled/ceiling-tripped screen) flows
            # through to write_pending_merge below unchanged.
            if t1_screen_rejects_merge_proposal(
                member_paths=member_paths,
                merge_target_name=proposal.merge_target_name,
                cluster_id=entry.cluster_id,
                # Issue athenaeum#841: the ``reasoning_t1`` knob's own client.
                client=reasoning_t1_client,
                usage=usage,
                wiki_root=wiki_root,
                config=resolved_config,
                provider=resolved_provider,
                authority_manifest=reasoning_authority_manifest,
                enabled=reasoning_t1_enabled,
                dry_run=dry_run,
            ):
                return
            # Issue athenaeum#421: slug-collision precheck. Classify the proposal by
            # whether its derived target slug already exists in wiki/ so a
            # ``create-merged`` proposal can never fail ``target_exists`` at
            # approve. Only the CLASSIFICATION lives here; the fold WRITE path
            # is athenaeum#425.
            write_kind = _classify_merge_write_kind(
                proposal.merge_target_name, wiki_root
            )
            # Issue athenaeum#602: T2 reasoning-tier screen — a second, more expensive
            # tier consulted ONLY on a T1 pass-up (a T1 reject already
            # returned above, so an already-rejected proposal never reaches
            # this Opus call). **Independently gated as of issue athenaeum#1200** by
            # its OWN ``reasoning_tier_t2_auto_apply_enabled`` flag — NOT the
            # same flag as T1 (that was the athenaeum#1200 defect: one key armed
            # both a harmless screen and unreviewed auto-apply together).
            # T2's flag defaults OFF independent of T1's value. A safe-class
            # ``approve`` auto-applies the merge (bypassing the human queue)
            # via the exact same write_pending_merge + resolve_merge
            # mechanics used below, marked auto_applied in provenance. Every
            # other outcome — disabled, dry-run, no client, ceiling tripped,
            # escalate/amend/draft, or an approve that fails
            # safe_class_violation — returns False and falls through to the
            # unscreened write below unchanged, matching T1's own
            # degrade-to-human-queue contract.
            if t2_screen_merge_proposal(
                member_paths=member_paths,
                merge_target_name=proposal.merge_target_name,
                rationale=proposal.rationale,
                draft_merged_body=proposal.draft_merged_body,
                confidence=proposal.confidence,
                write_kind=write_kind,
                cluster_id=entry.cluster_id,
                # Issue athenaeum#841: the ``reasoning_t2`` knob's own client.
                client=reasoning_t2_client,
                usage=usage,
                wiki_root=wiki_root,
                config=resolved_config,
                provider=resolved_provider,
                authority_manifest=reasoning_authority_manifest,
                enabled=reasoning_t2_auto_apply_enabled,
                dry_run=dry_run,
            ):
                return
            try:
                write_pending_merge(
                    wiki_root / "_pending_merges.md",
                    merge_target_name=proposal.merge_target_name,
                    sources=member_paths,
                    rationale=proposal.rationale,
                    draft_merged_body=proposal.draft_merged_body,
                    confidence=proposal.confidence,
                    write_kind=write_kind,
                )
                log.info(
                    "resolutions: propose_merge written to _pending_merges.md "
                    "(target=%s, confidence=%.2f); dropping pending-question "
                    "escalation for cluster %s",
                    proposal.merge_target_name,
                    proposal.confidence,
                    entry.cluster_id,
                )
            except OSError as exc:
                log.warning(
                    "resolutions: failed to write propose_merge for cluster %s "
                    "(%s); falling through to pending-question escalation",
                    entry.cluster_id,
                    exc,
                )
            else:
                return
        # Confirmation pass (issue athenaeum#145): the stronger resolver model
        # gets a second opinion on every detected=True cluster. When it
        # returns the suppress verdict, the cheap detector over-fired —
        # this is a refinement / restatement / supersession /
        # different-scenario pair, not a real contradiction — so drop
        # the escalation instead of writing a pending question. The
        # budget-exhausted path (proposal is None) and the deterministic
        # fallback (action="retain_both_with_context") both fall through
        # and escalate as before, so cost stays bounded and an offline
        # run still escalates.
        if proposal is not None and proposal.action == SUPPRESS_ACTION:
            # Issue athenaeum#249: record this clearance so future nights skip the Opus
            # confirmation for this settled pair. Dedup against the in-memory
            # set bounds file growth (open-question #2). Best-effort — the
            # writer swallows OSError and must never block the drop below.
            fp = _result_claim_fingerprint(result)
            if (
                fp
                and fp not in cleared_not_a_conflict_fps
                and fp not in recorded_not_a_conflict_fps
            ):
                passages = result.conflicting_passages or []
                side_a = normalize_side(passages[0]) if len(passages) >= 2 else None
                side_b = normalize_side(passages[1]) if len(passages) >= 2 else None
                mk = _member_key_str(tuple(sorted(result.members_involved)))
                pt = (
                    _pair_text_from_passages(passages[0], passages[1])
                    if len(passages) >= 2
                    else None
                )
                record_resolution(
                    knowledge_root,
                    fingerprint=fp,
                    verdict=SUPPRESS_ACTION,
                    resolved_by="auto",
                    # Issue athenaeum#251: stamp the run-start ``now`` so the decay
                    # clock is single-sourced — a re-cleared expired pair's
                    # fresh row resets the clock against the SAME instant the
                    # skip gate decayed against (deterministic refresh).
                    resolved_at=decay_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    side_a_norm=side_a,
                    side_b_norm=side_b,
                    member_key=mk,
                    pair_text=pt,
                )
                recorded_not_a_conflict_fps.add(fp)
            log.info(
                "contradictions: confirmation pass cleared cluster %s "
                "(resolver verdict not_a_conflict); escalation dropped",
                entry.cluster_id,
            )
            return
        # Opinion-attribution verdict (athenaeum#327): BOTH sides are evaluative
        # opinions kept-both-with-attribution. Like the suppress/refines
        # short-circuit, this is NOT a human-facing conflict — both stay
        # active, each attributed to its asserter — so ENACT the non-
        # destructive attribution stamp and DROP the pending-question
        # escalation. The pair never re-queues to the human; a re-detected
        # opinion pair hits the deterministic stance short-circuit again next
        # run (cheap, no Opus call) and is dropped identically.
        if proposal is not None and proposal.action == ATTRIBUTE_BOTH_ACTION:
            member_paths = _order_member_paths(result, members)
            if member_paths and isinstance(proposal, ResolutionProposal):
                enact_resolution(proposal, member_paths)
            log.info(
                "contradictions: opinion pair kept-both-with-attribution for "
                "cluster %s (resolver verdict attribute_both); escalation "
                "dropped, both members stay active",
                entry.cluster_id,
            )
            return
        # Mutating single-side verdicts (athenaeum#166 follow-up): correct_a /
        # correct_b (the losing side was WRONG — remove its claim) and
        # forget_a / forget_b (one side is transient — delete it cleanly).
        # These are genuine contradictions, NOT suppressions and NOT
        # merge proposals, so they intentionally fall through to the
        # normal pending-question escalation below. The auto-apply gate in
        # tier4_escalate (per-action threshold 0.90, same as keep_a/keep_b)
        # decides whether the resolution is applied in-place or left for
        # the human — no special routing is needed here. Noted explicitly
        # so a future reader greps the contract and does not add a branch.
        # Issue athenaeum#146: run-scoped dedup by the flagged source-file set. The
        # check sits AFTER the suppress-verdict return on purpose: a
        # suppressed cluster never reaches here, so it does not consume a
        # member key — a later, genuinely-detected cluster covering the same
        # pair can still escalate. Recording on suppression would let one
        # false-positive suppression silently hide a real later conflict.
        # A result with fewer than 2 flagged members cannot form a stable
        # pair key (the detector occasionally echoes only one member); such
        # results escalate without being recorded, preserving prior
        # behavior and never suppressing a distinct conflict.
        member_key = tuple(sorted(result.members_involved))
        if len(member_key) >= 2:
            if member_key in escalated_member_keys:
                log.info(
                    "contradictions: source-file pair %s already escalated "
                    "this run; skipping duplicate escalation for cluster %s",
                    member_key,
                    entry.cluster_id,
                )
                return
            escalated_member_keys.add(member_key)
        wiki_ref = f"wiki/{entry.filename}"
        description_parts: list[str] = []
        if result.rationale:
            description_parts.append(result.rationale)
        if result.conflicting_passages:
            for i, passage in enumerate(result.conflicting_passages, start=1):
                description_parts.append(f"Passage {i}: {passage}")
        if result.members_involved:
            description_parts.append(
                "Members involved: " + ", ".join(result.members_involved)
            )
        description = "\n".join(description_parts) or (
            f"Cluster {entry.cluster_id} flagged by contradiction detector."
        )
        # Append the OPTIONAL Opus-resolver proposal block (issue athenaeum#126).
        # render_proposal_block returns "" for the deterministic fallback,
        # so entries without a real proposal stay byte-identical to the
        # pre-athenaeum#126 escalation format.
        if proposal is not None and isinstance(proposal, ResolutionProposal):
            block = render_proposal_block(proposal)
            if block:
                description = description + "\n" + block
        escalations.append(
            EscalationItem(
                raw_ref=wiki_ref,
                entity_name=entry.topic_slug,
                conflict_type=result.conflict_type or "factual",
                description=description,
                proposal=proposal,
                # Flagged member paths in resolver a/b order so the
                # enactment lane can delete the target on a high-confidence
                # forget_*/correct_* auto-apply (athenaeum#166 follow-up).
                members=_order_member_paths(result, members),
            )
        )

    # Issue athenaeum#191: drop inactive members (superseded_by / deprecated) from the
    # detector pool so a superseded/deprecated claim cannot generate fresh
    # contradiction escalations. ``am_by_path`` (the row-builder body lookup)
    # is left intact — the row-level skip in ``merge_cluster_row`` handles
    # compile exclusion.
    auto_memory_list = [am for am in auto_memory_files if not am.is_inactive(as_of)]
    use_ancestor = mode in ("ancestor", "both")

    # Issue athenaeum#126: Opus-backed resolver budget. The resolver is opt-in
    # via ANTHROPIC_API_KEY (no client → fallback path); the per-run cap
    # caps Opus calls even when a key is present, so a noisy detector
    # cannot run away with cost. When the budget is exhausted, the
    # remaining contradictions are escalated WITHOUT a proposal —
    # `render_proposal_block` is a no-op on the fallback proposal so the
    # block stays byte-identical to the pre-athenaeum#126 format.
    resolve_budget = resolve_max_per_run(resolved_config)
    resolve_calls = 0
    # Issue athenaeum#1177 (AC4): attempted (``resolve_calls`` above) vs
    # succeeded, mirroring ``haiku_calls_succeeded`` below -- see its
    # comment for why this is a before/after diff on
    # ``usage.succeeded_calls`` rather than a new ``propose_resolution``
    # return value.
    resolve_calls_succeeded = 0
    resolve_budget_exhausted_logged = False

    def _maybe_propose(
        result: ContradictionResult,
        members: list[AutoMemoryFile],
    ) -> ResolutionProposal | MergeProposal | None:
        nonlocal resolve_calls, resolve_calls_succeeded, resolve_budget_exhausted_logged
        if not result.detected:
            return None
        # Issue athenaeum#249: a pair already settled as not_a_conflict (auto or human)
        # skips the expensive Opus confirmation entirely. Synthesize the
        # SUPPRESS proposal so existing code drops the escalation (the loop
        # sets ``suppressed`` and ``_emit_escalation`` returns) WITHOUT
        # consuming budget or an api_call.
        fp = _result_claim_fingerprint(result)
        if fp and fp in cleared_not_a_conflict_fps:
            log.info(
                "contradictions: claim-pair already settled as not_a_conflict "
                "(fingerprint=%s); skipping Opus confirmation (issue athenaeum#249)",
                fp,
            )
            return ResolutionProposal(
                recommended_winner="neither",
                action=SUPPRESS_ACTION,
                rationale="cached not_a_conflict (issue athenaeum#249)",
                confidence=1.0,
            )
        if resolve_calls >= resolve_budget:
            if not resolve_budget_exhausted_logged:
                log.warning(
                    "resolutions: per-run cap of %d Opus calls reached; "
                    "escalating remaining contradictions without proposal",
                    resolve_budget,
                )
                resolve_budget_exhausted_logged = True
            else:
                log.warning(
                    "resolutions: budget-exhausted; escalating without proposal"
                )
            return None
        resolve_calls += 1
        # Issue athenaeum#841: gate on resolve_client (the client propose_resolution
        # actually calls below), not the classify-knob ``client`` — the two
        # can now differ.
        if usage is not None and resolve_client is not None:
            usage.api_calls += 1
            # Issue athenaeum#1177: also record it on the run-level attempted-vs-
            # succeeded counter (see TokenUsage.record_attempt's docstring) —
            # additive to, not a replacement for, this call site's own
            # ``api_calls``/``resolve_calls`` accounting above.
            usage.record_attempt()
        _succeeded_before = usage.succeeded_calls if usage is not None else 0
        proposal = propose_resolution(
            result, members, resolve_client, usage=usage, wiki_root=wiki_root
        )
        if usage is not None and usage.succeeded_calls > _succeeded_before:
            resolve_calls_succeeded += 1
        return proposal

    # Issue athenaeum#462: FIRST WRITE — persist the deterministic C3 merge output to
    # disk BEFORE C4 detection runs. Until this change the page write loop sat
    # AFTER the deadline-checked C4 detector/resolver loop, so a C4 deadline
    # trip (10+ consecutive nights per athenaeum#440) raised before any page was
    # written and threw away the ENTIRE C3 build — every night re-paid C3 and
    # banked nothing. Writing here means a later C4 trip keeps the compiled
    # pages on disk (``_stop_on_deadline`` commits them); C4 then re-writes
    # only the entries whose contradiction state actually changed.
    #
    # C3 stays ATOMIC: the build loop + cohesion floor + run-global slug
    # resolution all complete before this pass, so no page is written mid-build
    # with a not-yet-final slug. Every page is written UNFLAGGED here
    # (``contradiction`` defaults to ``detected=False``), byte-identical to
    # what a deterministic ``client=None`` compile already writes — the athenaeum#145
    # contract ("no contradiction-flagged status without a pending question")
    # holds because the flag is only rendered after detection + escalation.
    # A page flagged by a PRIOR run whose cluster now clears is overwritten
    # unflagged right here, so the flag-clear lifecycle is preserved too.
    #
    # ``first_write_render`` caches each page's rendered bytes so the C4 loop
    # can re-write ONLY the entries whose render changed (flag added/cleared),
    # keeping the "one extra write for flagged pages only" cost the issue
    # budgeted. Dry-run writes nothing (guarded here and at every re-write).
    first_write_render: dict[str, str] = {}
    if not dry_run:
        write_heartbeat = PhaseHeartbeat(
            "merge-write", total=len(entries), interval_s=heartbeat_interval
        )
        write_heartbeat.start()
        wiki_root.mkdir(parents=True, exist_ok=True)
        # Issue athenaeum#1116 AC1: the set of slugs a tainted ``## Inference``
        # basis is checked against, seeded from current off-corpus store
        # membership and grown in-run as entries get routed off-corpus below.
        erasure_class_slugs = _off_corpus_erasure_class_slugs(resolved_config, knowledge_root)
        for entry in entries:
            text = render_merged_entry(entry)
            first_write_render[entry.filename] = text
            page_path = _route_merged_entry_write(
                entry,
                text,
                wiki_root=wiki_root,
                knowledge_root=knowledge_root,
                config=resolved_config,
                erasure_class_slugs=erasure_class_slugs,
            )
            log.info(
                "merge: wrote %s (cluster %s, %d source(s), contradictions=%s) "
                "[pre-C4 first write, athenaeum#462]",
                page_path if page_path is not None else f"{entry.filename} (off-corpus)",
                entry.cluster_id,
                len(entry.sources),
                entry.contradictions_detected,
            )
            write_heartbeat.tick(entry.cluster_id or entry.topic_slug, compiled=1)
        write_heartbeat.done()

    # Issue athenaeum#398: the C4 contradiction-detection loop is the region that went
    # dark for 3.5h in the 2026-07-19 incident (per-cluster `claude -p`
    # detector/resolver subprocess calls with no progress logging). Emit a
    # heartbeat per cluster processed so a wedge here is visible in the log.
    detect_heartbeat = PhaseHeartbeat(
        "merge-detect", total=len(entries), interval_s=heartbeat_interval
    )
    detect_heartbeat.start()

    # Issue athenaeum#762: refresh the RUN LOCK's heartbeat from inside the C4
    # detector/resolver loop. `detect_heartbeat` (PhaseHeartbeat) above only
    # LOGS progress; it does not touch `~/knowledge/.athenaeum.lock`'s
    # `heartbeat:` field. That field is refreshed by the `heartbeat` callable
    # (RunLock.heartbeat, threaded from run() via ctx.heartbeat), which was
    # never wired into this phase — so a run that spent 25+ minutes in C4 kept
    # a healthy, working lock looking wedged to every consumer that reads
    # heartbeat age (athenaeum#397's contended-acquire auto-break). Ticking here,
    # per cluster AND per chunk (the finest boundary, right where the slow
    # `claude -p` detector/resolver calls happen), makes heartbeat age track C4
    # progress. Best-effort by contract: a refresh failure must never break or
    # slow the run — RunLock.heartbeat already swallows OSError, and this guard
    # swallows anything else defensively.
    def _beat() -> None:
        if heartbeat is None:
            return
        try:
            heartbeat()
        except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort (athenaeum#762)
            log.debug("librarian: C4 heartbeat refresh skipped: %s", exc)

    # Issue athenaeum#569 (H6): resolved once for the per-cluster detection-incomplete
    # marker writes/clears below (same cache-dir resolution the cluster pass
    # reads with, so writes here and reads in _run_cluster_pass agree).
    _incomplete_cache_dir = detection_state.resolve_cache_dir()
    for entry in entries:
        detect_heartbeat.tick(entry.cluster_id)
        _beat()  # athenaeum#762: per-cluster run-lock heartbeat refresh
        if use_ancestor:
            pooled = pool_cluster_with_ancestors(
                entry.resolved_members,
                auto_memory_list,
            )
            chunks = chunk_by_cap(pooled, cluster_size_cap)
        else:
            chunks = [list(entry.resolved_members)]

        # Track aggregate result across chunks: any chunk that detects
        # wins. The first detected result is the canonical one for the
        # entry's frontmatter.
        aggregate: ContradictionResult | None = None
        # Set when the confirmation pass cleared a detected cluster — the
        # entry must NOT be flagged even though the detector fired.
        suppressed = False
        # Issue athenaeum#569 (H6): set when the detector OR resolver gave up after its
        # transient-error retries for this cluster. Drives the per-cluster
        # detection-incomplete marker below so a cluster that hit one transient
        # error is force-re-queued into the next run's delta set.
        entry_incomplete = False
        for chunk in chunks:
            # Issue athenaeum#762: refresh the run-lock heartbeat at the per-chunk
            # boundary too — this is the finest granularity, immediately around
            # the slow `claude -p` detector (Haiku) + resolver (Opus) calls, so
            # the max gap between heartbeat refreshes during C4 is bounded by a
            # single chunk's detector+resolver latency instead of the whole
            # phase's duration.
            _beat()
            # Issue athenaeum#396: wall-clock deadline check at the C4 detector/resolver
            # chunk boundary — the EXACT site the athenaeum#396 incident wedged in
            # (cycling `claude -p` merge subprocesses for ~3.5h). Bounds a
            # stalled detector/resolver loop to the run-level deadline.
            if deadline is not None and time.monotonic() >= deadline:
                raise RunDeadlineExceeded("C4 contradiction detector / resolver")
            chunks_run += 1
            # Lane 1 / athenaeum#167: short-circuit when every pair in the chunk
            # declares the other via refines/supersedes. Saves a Haiku
            # call and prevents the over-fire path from flagging
            # already-resolved pairs.
            filtered, declared = _filter_declared_pairs(chunk)
            if declared is not None and not filtered:
                # Fully-declared chunk — no Haiku call at all.
                _record_pair_keys(chunk)
                result = ContradictionResult(detected=False, rationale=declared)
                continue
            # Issue athenaeum#172: partial prune — Haiku only sees members that
            # have at least one undeclared partner. _record_pair_keys
            # still uses the original chunk so declared pairs are
            # marked covered for the similarity sweep.
            if len(filtered) < 2:
                _record_pair_keys(chunk)
                result = ContradictionResult(
                    detected=False,
                    rationale="declared-pruned-to-singleton",
                )
                continue
            # Issue athenaeum#324: skip the detector when EVERY undeclared pair is
            # validity-disjoint — sequential states of the world cannot
            # conflict. Mirrors the declared-pair short-circuit above: no
            # Haiku call, no escalation, already-settled pairs stay settled.
            if _all_pairs_disjoint(filtered):
                _record_pair_keys(chunk)
                log.info(
                    "contradictions: skipping detector for disjoint-validity "
                    "cluster of %d member(s)",
                    len(filtered),
                )
                result = ContradictionResult(
                    detected=False, rationale="disjoint-validity"
                )
                continue
            # Issue athenaeum#461: run-level budget guard. The entity phase now claims
            # the shared ``max_api_calls`` ceiling FIRST (it runs before this
            # whole-corpus C4 pass — see the librarian.run() reorder), so a
            # spent budget must stop the detector here too, rather than
            # burning further past the ceiling. Mirrors the deterministic
            # ``detected=False`` short-circuits above — same degrade path,
            # same pair-key bookkeeping, no escalation.
            if (
                max_api_calls is not None
                and not dry_run
                and usage is not None
                and usage.api_calls >= max_api_calls
            ):
                _record_pair_keys(chunk)
                result = ContradictionResult(
                    detected=False, rationale="budget-exhausted"
                )
                continue
            # Issue athenaeum#568 (H7): the shared ``max_api_calls`` count is not the
            # only bound — an operator's spend ceiling (tokens or dollars) must
            # STOP this phase too. The C4 pass runs the Haiku detector AND the
            # Opus resolver, the most expensive phase, yet historically checked
            # no ceiling. Mirrors ``librarian.py``'s ``spend.ceiling_tripped``
            # early-exit exactly: same log-line shape, degrade to the settled
            # ``detected=False`` path used by the co-located budget guard above
            # (no escalation, same pair-key bookkeeping).
            if not dry_run and usage is not None:
                _ceiling = spend.ceiling_tripped(
                    usage, provider=resolved_provider, config=resolved_config
                )
                if _ceiling is not None:
                    log.error(
                        "Spend ceiling reached (%s) — stopping early", _ceiling
                    )
                    _record_pair_keys(chunk)
                    result = ContradictionResult(
                        detected=False, rationale="ceiling-tripped"
                    )
                    continue
            haiku_calls += 1
            if usage is not None and client is not None:
                usage.api_calls += 1
                # Issue athenaeum#1177: run-level attempted counter, additive to
                # the api_calls/haiku_calls accounting above.
                usage.record_attempt()
            _succeeded_before = usage.succeeded_calls if usage is not None else 0
            result = detect_contradictions(
                filtered, client, config=resolved_config, usage=usage, wiki_root=wiki_root
            )
            if usage is not None and usage.succeeded_calls > _succeeded_before:
                haiku_calls_succeeded += 1
            # Issue athenaeum#569 (H6): capture the detector's transient give-up BEFORE
            # any downgrade reassigns `result`, so a cluster whose detection was
            # cut short by an overload window is re-queued next run.
            if result.incomplete:
                entry_incomplete = True
            # Issue athenaeum#324: post-detection guard — an otherwise-overlapping
            # cluster can still have the detector flag a SPECIFIC disjoint
            # pair. Downgrade to not-detected BEFORE the escalation/pending-
            # question write so the settled pair is never re-queued.
            if _detected_pair_disjoint(result, filtered):
                log.info(
                    "contradictions: downgrading detected pair to "
                    "disjoint-validity (no escalation)"
                )
                result = ContradictionResult(
                    detected=False, rationale="disjoint-validity"
                )
            _record_pair_keys(chunk)
            if result.detected and aggregate is None:
                proposal = _maybe_propose(result, filtered)
                # Issue athenaeum#569 (H6): a resolver that gave up after its retries
                # leaves the contradiction un-resolved — re-queue the cluster.
                if getattr(proposal, "incomplete", False):
                    entry_incomplete = True
                # When the confirmation pass suppresses the cluster, the
                # detector over-fired: leave `aggregate` unset so the
                # wiki entry frontmatter is NOT tagged
                # contradiction-flagged. Otherwise a suppressed cluster
                # would carry a "contradiction-flagged" status with no
                # pending question to point at (issue athenaeum#145).
                # `_emit_escalation` independently drops the escalation
                # for the suppress verdict.
                if proposal is not None and proposal.action == SUPPRESS_ACTION:
                    suppressed = True
                elif proposal is not None and proposal.action == PROPOSE_MERGE_ACTION:
                    # Lane 3: routed to _pending_merges.md, not a contradiction.
                    suppressed = True
                elif proposal is not None and proposal.action == ATTRIBUTE_BOTH_ACTION:
                    # Issue athenaeum#327: an opinion pair kept-both-with-attribution is
                    # not a live contradiction — leave `aggregate` unset so the
                    # wiki entry is not tagged contradiction-flagged (the
                    # escalation is dropped in _emit_escalation).
                    suppressed = True
                else:
                    aggregate = result
                _emit_escalation(entry, result, proposal, members=filtered)
        if aggregate is None:
            if suppressed:
                # Detector fired but the confirmation pass cleared it —
                # record a clean not-detected verdict so the wiki entry
                # frontmatter is coherent (issue athenaeum#145).
                aggregate = ContradictionResult(
                    detected=False,
                    rationale="confirmation-pass-cleared",
                )
            else:
                # Use the last result so rationale (e.g. "singleton" /
                # "llm-unavailable") is preserved on the entry.
                aggregate = result if chunks else ContradictionResult(detected=False)
        entry.contradiction = aggregate
        entry.contradictions_detected = bool(aggregate.detected)
        # Issue athenaeum#569 (H6): record or clear the per-cluster detection-incomplete
        # marker. Only when detection was actually ATTEMPTED (a live client) and
        # this is not a dry run — otherwise we neither examined the cluster nor
        # should churn the marker. A cluster whose detector/resolver gave up
        # after retries is marked so the next run's delta set re-examines it
        # regardless of file changes; a cluster examined to completion has any
        # stale marker cleared.
        if client is not None and not dry_run:
            if entry_incomplete:
                detection_state.mark_incomplete(
                    _incomplete_cache_dir,
                    entry.cluster_id,
                    [str(am.path) for am in entry.resolved_members],
                )
            else:
                detection_state.clear_incomplete(
                    _incomplete_cache_dir, entry.cluster_id
                )
        # Issue athenaeum#462: re-write this page IMMEDIATELY if C4 changed its rendered
        # bytes (contradiction flag added, or a stale flag cleared) relative to
        # the pre-C4 first write. Doing it per-entry inside the loop — rather
        # than in a trailing batch — means a C4 deadline trip at a LATER chunk
        # leaves every already-detected entry persisted with its flag, while
        # the still-unprocessed entries keep their durable unflagged C3 page.
        # A no-op re-render (the common case: an unflagged entry stays
        # unflagged) is skipped, so cost stays at "flagged pages only".
        if not dry_run:
            new_text = render_merged_entry(entry)
            if new_text != first_write_render.get(entry.filename):
                # Issue athenaeum#1116 AC1: same taint check/routing as the
                # first write above — a re-write must not silently un-route a
                # tainted page back into the ordinary corpus.
                _route_merged_entry_write(
                    entry,
                    new_text,
                    wiki_root=wiki_root,
                    knowledge_root=knowledge_root,
                    config=resolved_config,
                    erasure_class_slugs=erasure_class_slugs,
                )
                first_write_render[entry.filename] = new_text
                log.info(
                    "merge: re-wrote %s after C4 (cluster %s, contradictions=%s) "
                    "[athenaeum#462]",
                    entry.filename,
                    entry.cluster_id,
                    entry.contradictions_detected,
                )
    detect_heartbeat.done()

    # Similarity sweep (mode in {similarity, both}).
    # Issue athenaeum#370 PR2: the sweep is whole-corpus by nature (it scans ALL raw
    # intake and wiki entries for cross-pair contradictions), so it is skipped
    # on the delta path — that path is the deterministic ``client=None`` compile
    # where the detector returns ``detected=False`` regardless and the sweep can
    # therefore have no effect on the written bytes.
    if mode in ("similarity", "both") and only_cluster_ids is None:
        from athenaeum.clusters import DEFAULT_CACHE_DIR

        wiki_files: list[Path] = []
        if wiki_root.is_dir():
            wiki_files = sorted(wiki_root.glob("auto-*.md"))
        candidates = cross_scope_similarity_pairs(
            auto_memory_list,
            wiki_files=wiki_files,
            wiki_root=wiki_root,
            extra_roots=extra_roots,
            cache_dir=DEFAULT_CACHE_DIR,
            threshold=similarity_threshold,
            excluded_pair_keys=covered_pair_keys,
            # Issue athenaeum#262: only compare NEW raw intake against the matching
            # wiki entry. Wiki-vs-wiki pairs are dropped, so an unchanged
            # corpus with zero new intake costs ~0 detector calls instead of
            # one per wiki-pair (O(new intake + open) not O(corpus²)).
            require_raw_side=True,
        )
        for cand in candidates:
            # Issue athenaeum#762: the similarity sweep is the OTHER C4-family loop
            # that runs a per-pair `claude -p` detector and can run long — tick
            # the run-lock heartbeat here too so it does not go dark either.
            _beat()
            pair = candidate_to_auto_memory_files(cand)
            # Lane 1 / athenaeum#167: skip similarity-sweep pairs that declare
            # each other. Mirrors the primary-pass short-circuit so a
            # declared-supersession pair never reaches the detector.
            _filtered, declared = _filter_declared_pairs(list(pair))
            if declared is not None and not _filtered:
                continue
            # Issue athenaeum#324: skip validity-disjoint similarity pairs too — a
            # 2-member disjoint pair is settled and must not reach Haiku.
            if _all_pairs_disjoint(list(pair)):
                continue
            # Issue athenaeum#461: same run-level budget guard as the primary detector
            # call site above — a spent shared budget skips the similarity
            # sweep's detector call too (degrades to a no-op: no escalation,
            # since a "budget-exhausted" verdict is never `.detected`).
            if (
                max_api_calls is not None
                and not dry_run
                and usage is not None
                and usage.api_calls >= max_api_calls
            ):
                continue
            # Issue athenaeum#568 (H7): same spend-ceiling guard as the primary detector
            # call site above — a breached ceiling skips the similarity sweep's
            # detector call too (a no-op degrade: no escalation is written when
            # the detector never runs). Mirrors ``librarian.py``'s early-exit.
            if not dry_run and usage is not None:
                _ceiling = spend.ceiling_tripped(
                    usage, provider=resolved_provider, config=resolved_config
                )
                if _ceiling is not None:
                    log.error(
                        "Spend ceiling reached (%s) — stopping early", _ceiling
                    )
                    continue
            haiku_calls += 1
            if usage is not None and client is not None:
                usage.api_calls += 1
                # Issue athenaeum#1177: run-level attempted counter, additive to
                # the api_calls/haiku_calls accounting above.
                usage.record_attempt()
            _succeeded_before = usage.succeeded_calls if usage is not None else 0
            result = detect_contradictions(
                pair, client, config=resolved_config, usage=usage, wiki_root=wiki_root
            )
            if usage is not None and usage.succeeded_calls > _succeeded_before:
                haiku_calls_succeeded += 1
            if result.detected:
                pairs_added_via_similarity += 1
                # Synthesize a thin escalation entry; we don't have a
                # MergedWikiEntry for cross-pair similarity hits, so
                # build a minimal one tied to the first member's name.
                synthetic = MergedWikiEntry(
                    topic_slug=cand.a_path.stem,
                    cluster_id=f"similarity-{cand.a_path.stem}-{cand.b_path.stem}",
                    cluster_centroid_score=cand.similarity,
                    # A 2-member pair's min pairwise IS its similarity.
                    min_pairwise_score=cand.similarity,
                    contradictions_detected=True,
                    contradiction=result,
                )
                proposal = _maybe_propose(result, list(pair))
                _emit_escalation(synthetic, result, proposal, members=list(pair))

    log.info(
        "contradictions: mode=%s; haiku_calls=%d; chunks_run=%d; pairs_added_via_similarity=%d",
        mode,
        haiku_calls,
        chunks_run,
        pairs_added_via_similarity,
    )

    if dry_run:
        for entry in entries:
            log.info(
                "  [DRY RUN] merge %s → wiki/%s (%d source(s), contradictions=%s)",
                entry.cluster_id,
                entry.filename,
                len(entry.sources),
                entry.contradictions_detected,
            )
        if out_stats is not None:
            out_stats.update(
                {
                    "haiku_calls": haiku_calls,
                    # Issue athenaeum#1177 (AC4): attempted-vs-succeeded split, so a
                    # run where the detector's calls all errored cannot
                    # report "20 detections" that the token ledger shows
                    # zero tokens for.
                    "haiku_calls_succeeded": haiku_calls_succeeded,
                    "resolve_calls": resolve_calls,
                    "resolve_calls_succeeded": resolve_calls_succeeded,
                    "chunks_run": chunks_run,
                    "pairs_added_via_similarity": pairs_added_via_similarity,
                    "entries_merged": len(entries),
                    "escalations_written": len(escalations),
                    "c4_swept_full": only_cluster_ids is None,
                }
            )
        return entries

    # Issue athenaeum#462: every page is already on disk — written unflagged before C4
    # (first write) and re-written per-entry as C4 changed its flag. The
    # trailing write loop that used to live here (AFTER the deadline-checked C4
    # loop) is gone: it was the sole reason a C4 trip discarded the compile.
    # Only the escalation batch remains to flush.
    #
    # Escalations stay a single end-of-pass ``tier4_escalate`` batch (unchanged
    # semantics). A C4 deadline trip therefore loses only THIS run's pending
    # escalation batch, never the compiled pages — and the athenaeum#157 open-block
    # dedup + athenaeum#249 resolved-records cache make a re-detection next run
    # idempotent, so a dropped batch re-escalates cleanly rather than
    # duplicating.
    if escalations:
        tier4_escalate(
            escalations,
            wiki_root / "_pending_questions.md",
            config=resolved_config,
        )

    if out_stats is not None:
        out_stats.update(
            {
                "haiku_calls": haiku_calls,
                # Issue athenaeum#1177 (AC4): see the dry-run branch above's comment.
                "haiku_calls_succeeded": haiku_calls_succeeded,
                "resolve_calls": resolve_calls,
                "resolve_calls_succeeded": resolve_calls_succeeded,
                "chunks_run": chunks_run,
                "pairs_added_via_similarity": pairs_added_via_similarity,
                "entries_merged": len(entries),
                "escalations_written": len(escalations),
                "c4_swept_full": only_cluster_ids is None,
            }
        )

    return entries


def compile_as_of(
    knowledge_root: Path,
    as_of: date,
    out_dir: Path,
    *,
    config: dict[str, Any] | None = None,
) -> list[MergedWikiEntry]:
    """Recompile a historical wiki snapshot as it would have stood on ``as_of``.

    Issue athenaeum#359 (§8.7). This is the COMPILE-as-of capability, distinct from
    slice 3's read-time ``--as-of`` filter:

    - **Slice 3** (``recall --as-of`` / ``reindex --as-of``) filters the
      ALREADY-compiled live wiki at read/index time. It can only HIDE
      compiled pages whose frontmatter falls outside the as-of window; it
      cannot resurrect a member's content that the live compile already
      dropped (an expired member is not in any compiled page for a read
      filter to reveal).
    - **compile-as-of** RE-RUNS the deterministic C3 blend
      (:func:`merge_clusters_to_wiki`) with ``as_of`` threaded into the
      per-member ``is_inactive`` predicate, so a member expired now but
      valid on ``as_of`` is RE-INCLUDED and the merged prose / fields /
      sources are re-derived as they would have compiled on that date. The
      result is written to ``out_dir`` — the live wiki and raw tree are
      never touched.

    Safety and scope:

    - ``client`` is fixed to ``None``: no LLM contradiction detector runs, so
      there is no API spend and no escalation is written. The blend is fully
      deterministic over the current cluster assignments.
    - Raw members are never retired or mutated (retire is a separate
      librarian pass, not part of the merge).
    - It reuses the CURRENT cluster JSONL (C1 output); clusters are not
      re-derived as-of ``as_of``. The rewind is over which members within
      each cluster contribute.
    - The rewind is **valid-time**, not transaction-time. Raw members carry
      no reliable ingestion timestamp (only ``valid_from`` / ``valid_until``
      real-world validity + dated ``valid_until`` supersession closes), so
      compile-as-of cannot exclude a claim merely because it was *ingested*
      after ``as_of``, nor un-apply an undated ``superseded_by`` tombstone.
      A temporally-superseded loser (slice-2 dated ``valid_until`` close)
      DOES correctly reappear when ``as_of`` precedes the close.

    Args:
        knowledge_root: Root of the knowledge directory.
        as_of: The historical date to recompile as of (inclusive upper bound).
        out_dir: Scratch directory to write the recompiled wiki into. MUST NOT
            be the live ``wiki/`` directory — a :class:`ValueError` is raised
            if it is.
        config: Optional resolved config dict.

    Returns:
        The list of :class:`MergedWikiEntry` records written to ``out_dir``.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    out_dir = out_dir.expanduser().resolve()
    live_wiki = (knowledge_root / "wiki").expanduser().resolve()
    if out_dir == live_wiki:
        raise ValueError(
            "compile_as_of: out_dir must not be the live wiki directory "
            f"({live_wiki}); point --out at a scratch path"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    return merge_clusters_to_wiki(
        knowledge_root,
        config=resolved_config,
        client=None,
        as_of=as_of,
        out_wiki_root=out_dir,
    )
