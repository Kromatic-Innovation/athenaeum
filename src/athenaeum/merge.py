# SPDX-License-Identifier: Apache-2.0
"""Auto-memory merge pass (issue athenaeum#197, C3) — L4 domain/pipeline.

Consumes the JSONL cluster report produced by C2
(:mod:`athenaeum.clusters`) and emits ONE canonical wiki entry per
cluster at ``wiki/auto-<topic-slug>.md``. Every member's content is
concatenated into a synthesized body; every member's ``sources[]`` is
unioned into a single deduped cited list. **Issue athenaeum#1256 retired
the C4 contradiction-detection lane from this module**: this pass no
longer calls any LLM, runs no detector/resolver, and writes no
escalation. The tiered reasoning-pass screens (T1/T2) it used to drive at
the merge-proposal seam already moved OUT of this module under issue
athenaeum#1257 specifically so this retirement could not orphan them — they
survive in :mod:`athenaeum.reasoning_screens` with their own callers, and
this module neither defines nor names either of them any more.

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
up into this hub. Issue athenaeum#1256 removed this module's own
``write_pending_merge`` import along with the C4 merge-proposal lane, so the
``merge`` -> ``pending_merges`` edge is gone from this side too and the former
SCC is fully dissolved.

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
- Contradiction flag: ``contradictions_detected`` is written to
  frontmatter but this module never sets it true. The cohesion proxy the
  original athenaeum#197 scope described (flagging a cluster whose
  ``centroid_score`` fell below :data:`CONTRADICTION_COHESION_THRESHOLD`)
  was already inert before issue athenaeum#1256 — nothing gated on the
  threshold — and the C4 lane that did set the flag has now been retired.
  Real contradiction detection is the cluster-domain comparator lane's job
  now (:mod:`athenaeum.cluster_comparator` and the verdict-effects lane it
  feeds); the field and its frontmatter projection are retained so a
  comparator-sourced verdict can still populate them.

Out of scope (deliberate — later lanes):

- LLM-based body synthesis. C3's strategy is deterministic:
  concatenate member bodies, drop identical paragraphs, prefix each
  block with a scope/filename header. Rich paraphrase is a follow-up.
- Contradiction detection. Issue athenaeum#1256 retired the C4 lane from
  this module; the comparator lane owns it now.
- Rewrites to ``raw/auto-memory/*`` — raw is append-only; the wiki is
  the compiled view.
- A cross-scope ``wiki/MEMORY.md`` — Phase B explicitly removed it and
  this module does NOT recreate it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from athenaeum._lint import (
    _strip_self_reference,
    _strip_self_reference_merge_rejected_with,
)
from athenaeum.atomic_io import atomic_write_text
from athenaeum.clusters import resolve_cluster_output_path
from athenaeum.config import (
    load_config,
    resolve_decay_horizon_days,
    resolve_ephemeral_scopes,
    resolve_extra_intake_roots,
    resolve_heartbeat_interval,
    resolve_min_cluster_cohesion,
    resolve_min_cluster_cohesion_scopes,
    resolve_operational_markers,
)
from athenaeum.declared_relationships import DeclaredRelationshipFacts, declared_relationship
from athenaeum.ephemeral import classify_ephemeral
from athenaeum.footnote_markers import attach_markers, marker_label
from athenaeum.intake import RAW_FILE_RE, discover_auto_memory_files
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    AutoMemoryFile,
    ContradictionResult,
    coerce_source_type,
    parse_bucket,
    parse_deprecated,
    parse_frontmatter,
    parse_merge_rejected_with,
    parse_refines,
    parse_superseded_by,
    parse_supersedes,
    render_frontmatter,
    safe_source_ref,
    validity_bound_str,
)
from athenaeum.progress import PhaseHeartbeat

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
        active and never count as a conflict). ``"declared-merge-rejection"``
        (issue athenaeum#715) when one side names the other in its
        ``merge_rejected_with`` list — a human REJECTED a merge proposal
        for this pair, an honest non-directional fact that is distinct
        from both of the above and must never be conflated with
        ``"declared-refinement"``: a refinement is an adjudicated
        specialization claim, a rejection is only "these are not the same
        claim". ``None`` when no declaration applies.
    """
    # athenaeum#1682: the actual comparison logic now lives in
    # :func:`athenaeum.declared_relationships.declared_relationship`, so
    # `comparator.py`'s Gate 1 can reuse it without importing this L4
    # module. This function is now purely an adapter -- unpack each side's
    # four declared-relationship facts off its `AutoMemoryFile` and hand
    # them to the shared primitive. Name, signature, and docstring are
    # unchanged; only the body moved.
    return declared_relationship(
        DeclaredRelationshipFacts(
            name=a.name,
            refines=a.refines,
            supersedes_names=a.supersedes_names(),
            merge_rejected_with=a.merge_rejected_with,
        ),
        DeclaredRelationshipFacts(
            name=b.name,
            refines=b.refines,
            supersedes_names=b.supersedes_names(),
            merge_rejected_with=b.merge_rejected_with,
        ),
    )


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
      (supersession beats refinement beats merge-rejection when more than
      one appears — issue athenaeum#715 added the third class).
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
    saw_rejection = False
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
            elif verdict == "declared-refinement":
                saw_refinement = True
            else:
                # issue athenaeum#715: "declared-merge-rejection" — the only
                # other slug _declared_relationship can return. Named
                # explicitly (not folded into the refinement branch) so a
                # future fourth slug cannot silently fall through here.
                saw_rejection = True
    if not saw_undeclared:
        # Fully declared — short-circuit the detector entirely.
        if saw_supersession:
            return [], "declared-supersession"
        if saw_refinement:
            return [], "declared-refinement"
        if saw_rejection:
            return [], "declared-merge-rejection"
        return [], None
    pruned = [m for m, keep in zip(members, has_undeclared_partner) if keep]
    return pruned, None


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
    compatibility with the C3 wire (tests + callers that read it). Issue
    athenaeum#1256 retired the C4 detector that used to set both this and
    ``contradiction`` from a real :class:`ContradictionResult`: the merge
    pass (:func:`merge_clusters_to_wiki`) no longer populates either field,
    so they are ``False``/``None`` in practice from this lane. Both fields
    are RETAINED (not removed) because ``contradiction``'s structured
    output still feeds the frontmatter projection in
    :func:`render_merged_entry` — a capability a comparator lane is meant
    to populate going forward — and ``contradictions_detected`` is still
    read by existing tests/callers of the C3 wire.
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
    # by a MOST-DURABLE-WINS fold (issue athenaeum#1840, superseding athenaeum#904's
    # first-non-empty-member-wins rule — see :func:`_fold_bucket` for why the
    # direction matters); ties keep the first member at the winning level, so
    # a cluster whose members all agree is unaffected.
    bucket: str = ""
    # Issue athenaeum#1840: page-level ``valid_until`` DERIVED from
    # :attr:`bucket` (never a second validity concept — the value lands in
    # the same ``valid_until:`` key ``athenaeum.models.valid_until_expired``
    # has always read, which is what lets ``athenaeum.decay_sweep`` and
    # ``mcp_server._is_deprioritized_for_currency`` act on a compiled page at
    # all). ``""`` (unset) for a ``durable``/unbucketed page, and for a
    # bucketed one whose winning member declares no window and yields no
    # anchor. Computed by :func:`merge_cluster_row` from the SAME member that
    # won the bucket fold, so the bucket and its horizon can never come from
    # two different members. Rendered next to ``bucket`` by
    # :func:`render_merged_entry`, omitted entirely at the default.
    valid_until: str = ""
    # Resolved :class:`AutoMemoryFile` records backing this cluster. Populated
    # by :func:`merge_cluster_row` so the outer orchestrator does not need to
    # re-resolve filesystem paths to run the C4 contradiction detector.
    # Not rendered into wiki frontmatter; kept off the public docstring in
    # render_merged_entry by only touching ``sources``/``origin_scopes``.
    resolved_members: list[AutoMemoryFile] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{AUTO_WIKI_PREFIX}{self.topic_slug}.md"

    @property
    def member_names(self) -> list[str]:
        """Original per-member ``name:`` values, in member order, deduped.

        Issue athenaeum#1596: ``topic_slug`` (the compiled page's own
        ``name:``) is synthesized from token frequency across ALL member
        filenames — for a fused cluster it matches none of the individual
        members' written names. This property recovers the name each
        member's author actually wrote (``AutoMemoryFile.name``, falling
        back to the file's stem when a legacy member never carried one) so
        :func:`render_merged_entry` can preserve them as ``aliases:``. A
        singleton cluster's one member typically already equals
        ``topic_slug`` — the caller filters that case out.
        """
        seen: set[str] = set()
        names: list[str] = []
        for am in self.resolved_members:
            name = (am.name or am.path.stem).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names


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


def source_dedupe_key(entry: dict[str, Any]) -> tuple[str, Any]:
    """The ``(session, turn)`` identity two source citations collapse on.

    Extracted from :func:`dedupe_sources` (issue athenaeum#1730) because a
    SECOND caller now needs the same identity: to attach a compiled page's
    ``[^src-N]`` marker to the sentences of the member that cited it,
    :func:`merge_cluster_row` has to find each member source's position in the
    deduped list. Deriving that key independently would let the marker index
    and the dedupe disagree — a marker pointing at the wrong footnote is worse
    than no marker, so there is exactly one definition.
    """
    return (str(entry.get("session", "")), entry.get("turn"))


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
        key = source_dedupe_key(entry)
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
    member_labels: Sequence[Sequence[str]] | None = None,
) -> str:
    """Concatenate member bodies, dropping paragraphs seen verbatim before.

    Args:
        member_bodies: list of ``(scope, filename, body)`` triples, in
            cluster input order. Scope + filename become the section
            header so readers can trace a paragraph back to its origin
            raw file without hunting.
        member_labels: issue athenaeum#1730 — optional per-member footnote
            labels, index-aligned with *member_bodies*. When given, every
            prose sentence a member contributes is stamped with THAT
            member's own ``[^src-N]`` markers
            (:func:`athenaeum.footnote_markers.attach_markers`), so a
            sentence resolves to the source of the claim it came from
            instead of to the page-level union of every member's sources.
            Omitted (the default) the body is concatenated exactly as
            before — every existing caller keeps its current output.

    The dedupe is exact-match paragraph level (whitespace-trimmed). Two
    files saying "X causes Y" with identical wording contribute that
    paragraph once; variant phrasings are kept. This is the deliberately
    simple strategy documented in the PR body — LLM paraphrase/merge is
    a follow-up in C4+.

    Dedupe runs on the UNMARKED paragraph text, before markers are
    attached: two members wording a claim identically must still collapse
    to one paragraph, and stamping first would make their markers differ
    and defeat the exact-match compare. The surviving copy therefore
    carries the FIRST citing member's markers only — the same first-wins
    rule :func:`dedupe_sources` applies to the sources themselves.
    """
    seen_paragraphs: set[str] = set()
    sections: list[str] = []
    for index, (scope, filename, body) in enumerate(member_bodies):
        labels: Sequence[str] = ()
        if member_labels is not None and index < len(member_labels):
            labels = member_labels[index]
        kept_paragraphs: list[str] = []
        for para in re.split(r"\n\s*\n", body):
            canonical = " ".join(para.split())
            if not canonical:
                continue
            if canonical in seen_paragraphs:
                continue
            seen_paragraphs.add(canonical)
            kept_paragraphs.append(attach_markers(para.strip(), labels))
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


# ---------------------------------------------------------------------------
# Issue athenaeum#1840: bucket -> page-level ``valid_until``
# ---------------------------------------------------------------------------

#: Durability ORDER over :data:`athenaeum.models.MEMORY_BUCKETS`, least to
#: most durable. Used ONLY to fold a cluster's members down to one page-level
#: bucket (:func:`_fold_bucket`); it is not a validity concept and nothing
#: outside this module reads it.
#:
#: ``daily`` < ``weekly`` < ``durable`` is the ordering the bucket names
#: already imply, and most-durable-wins is the only safe direction: the fold
#: decides how long the COMPILED page lives, so a single transient member
#: must not be able to put an expiry on a cluster that also holds a durable
#: claim (the pre-athenaeum#1840 first-non-empty-member-wins fold could, purely
#: as an artifact of cluster-row member order). The converse direction is
#: harmless — an over-long horizon only means the sweep looks at the page
#: later, whereas an over-short one deletes a durable fact.
_BUCKET_DURABILITY: dict[str, int] = {"daily": 1, "weekly": 2, "durable": 3}


def _fold_bucket(members: list[tuple[str, AutoMemoryFile]]) -> AutoMemoryFile | None:
    """Return the member whose ``bucket`` wins the cluster fold, or ``None``.

    Most-durable-wins over :data:`_BUCKET_DURABILITY` (issue athenaeum#1840),
    replacing the first-non-empty-member-wins rule athenaeum#904 shipped.
    Ties are broken by member order — the STRICT ``>`` below keeps the first
    member at the winning durability level, so a cluster whose members all
    agree folds to exactly the member the old rule picked and every
    single-bucket cluster's output is unchanged.

    Returns the winning MEMBER (not just its bucket string) because the
    page's derived ``valid_until`` must be anchored on the SAME member that
    supplied the bucket — otherwise a ``{daily, weekly}`` cluster could take
    ``weekly`` from one member and a 7-day horizon from another member's
    date. Members with no bucket at all are skipped; ``None`` means the
    cluster is unbucketed, exactly as before this issue.
    """
    winner: AutoMemoryFile | None = None
    for _mp, am in members:
        if not am.bucket:
            continue
        if winner is None or _BUCKET_DURABILITY.get(am.bucket, 0) > _BUCKET_DURABILITY.get(
            winner.bucket, 0
        ):
            winner = am
    return winner


def _raw_filename_anchor_date(path: Path) -> date | None:
    """The ``YYYYMMDD`` half of a raw-intake filename stamp, or ``None``.

    Raw intake is named ``<YYYYMMDDTHHMMSSZ>-<uuid8>.md``
    (:data:`athenaeum.intake.RAW_FILE_RE`), so the file's own name records
    when the memory was written. Any other naming convention (auto-memory's
    ``<type>_<slug>.md``, a hand-written page) returns ``None`` and falls
    through to the next anchor.
    """
    m = RAW_FILE_RE.match(path.name)
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1)[:8], "%Y%m%d").date()
    except ValueError:
        # RAW_FILE_RE pins EIGHT DIGITS, not a valid calendar date, so a
        # hand-renamed ``20261399T...`` file reaches here. Fall through to
        # the next anchor rather than raising — one malformed filename must
        # not abort a whole nightly compile.
        return None


def _decay_anchor_date(am: AutoMemoryFile, *, today: date | None = None) -> date:
    """The date a member's decay horizon is measured FROM (issue athenaeum#1840).

    Precedence: the member's declared ``valid_from`` > its raw-filename
    stamp (:func:`_raw_filename_anchor_date`) > *today*. Anchoring on the
    memory's OWN date rather than the compile date is the whole point: a
    ``daily`` memory written on 2026-05-10 expired on 2026-05-11, and a
    compile run in September must derive exactly that, not tomorrow — a
    today-anchored horizon would silently renew every transient page on
    every nightly run and it could never expire.
    """
    if am.valid_from:
        try:
            return date.fromisoformat(am.valid_from)
        except ValueError:
            pass
    stamped = _raw_filename_anchor_date(am.path)
    if stamped is not None:
        return stamped
    return today if today is not None else date.today()


def _derive_page_valid_until(
    am: AutoMemoryFile | None,
    *,
    config: dict[str, Any] | None = None,
    today: date | None = None,
) -> str:
    """Page-level ``valid_until`` for the fold-winning member *am*, or ``""``.

    ``""`` — i.e. NO ``valid_until`` key at any layer — whenever:

    - there is no bucketed member (*am* is ``None``), or
    - the winning bucket is ``durable``. A durable page must never acquire a
      derived expiry, and :func:`athenaeum.config.resolve_decay_horizon_days`
      has no knob that could give it one (it returns ``0`` for every bucket
      but ``daily``/``weekly``), so this is enforced in two places rather
      than one.

    Only-fill-never-override: a member that ALREADY declares ``valid_until``
    keeps its own bound verbatim — the derivation fills an absent bound and
    never rewrites a declared one, the same posture
    :func:`_stamp_member_validity` takes at the per-source layer.
    """
    if am is None or not am.bucket:
        return ""
    # The durable gate comes FIRST, before the only-fill-never-override branch
    # below: a member carrying BOTH ``bucket: durable`` and its own
    # ``valid_until:`` must still compile to a page with no bound at all.
    # "No ``valid_until`` at any layer" is a property of the DURABLE bucket,
    # not merely of the derivation — so inheriting a declared bound here would
    # hand a durable page an expiry and make it sweepable. The per-source
    # record keeps its declared bound byte-identical either way; only the
    # page-level key is suppressed.
    horizon = resolve_decay_horizon_days(am.bucket, config)
    if horizon <= 0:
        return ""
    if am.valid_until:
        return am.valid_until
    return (_decay_anchor_date(am, today=today) + timedelta(days=horizon)).isoformat()


def merge_cluster_row(
    row: dict[str, Any],
    *,
    extra_roots: list[Path],
    am_by_path: dict[str, AutoMemoryFile],
    ephemeral_scopes: list[str] | None = None,
    operational_markers: list[str] | None = None,
    as_of: date | None = None,
    config: dict[str, Any] | None = None,
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

    ``config`` (issue athenaeum#1840) is read for ONE thing only: the
    operator-adjustable decay horizons behind the derived page-level
    ``valid_until`` (:func:`athenaeum.config.resolve_decay_horizon_days`).
    Left ``None`` the code defaults apply, so every existing caller's output
    is unchanged.

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
                shim_merge_rejected_with = parse_merge_rejected_with(
                    meta if meta else None
                )
            except ValueError as exc:
                log.warning(
                    "cluster %s shim: invalid refines/supersedes/merge_rejected_with "
                    "on %s (%s); treating as empty",
                    cluster_id,
                    resolved,
                    exc,
                )
                shim_refines = []
                shim_supersedes = []
                shim_merge_rejected_with = []
            # Issue athenaeum#181: same self-reference lint as discover_auto_memory_files.
            shim_name = str(meta.get("name", "")) if meta else ""
            shim_refines, shim_supersedes = _strip_self_reference(
                shim_name, shim_refines, shim_supersedes, resolved
            )
            shim_merge_rejected_with = _strip_self_reference_merge_rejected_with(
                shim_name, shim_merge_rejected_with, resolved
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
                merge_rejected_with=shim_merge_rejected_with,
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

    # Issue athenaeum#904 / athenaeum#1840: page-level decay bucket, folded
    # MOST-DURABLE-WINS over the ACTIVE members (deterministic — ``members``
    # is already filtered to active-only, in cluster-row order; ties keep the
    # first member at the winning level). athenaeum#904 shipped
    # first-non-empty-member-wins, which let one transient member mark a
    # cluster that also holds a durable claim ``daily`` purely by being
    # earlier in the row. The page-level ``valid_until`` is derived from the
    # SAME winning member so bucket and horizon can never disagree.
    bucket_member = _fold_bucket(members)
    bucket = bucket_member.bucket if bucket_member is not None else ""
    valid_until = _derive_page_valid_until(bucket_member, config=config, today=as_of)

    # Sources: parse each member's sources[] from frontmatter (source of
    # truth), plus a synthetic entry from originSessionId/turn when a
    # member has no sources[] at all.
    raw_sources: list[dict[str, Any]] = []
    # Issue athenaeum#1730: the SAME sources, kept partitioned by the member
    # that cited them. The flat ``raw_sources`` list is what dedupes into the
    # page's footnote definitions; this parallel list is what lets each
    # member's own sentences cite its own sources instead of the page-level
    # union. Index-aligned with ``members``.
    member_sources: list[list[dict[str, Any]]] = []
    for _mp, am in members:
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        meta, _ = parse_frontmatter(text) if text else ({}, "")
        sources_raw = meta.get("sources") if meta else None
        own: list[dict[str, Any]] = []
        if isinstance(sources_raw, list) and sources_raw:
            for s in sources_raw:
                parsed = _parse_one_source(s, am.origin_scope)
                if parsed is not None:
                    # Issue athenaeum#308 (slice 4): the member's temporal validity window
                    # travels with each claim it cites into the compiled entry.
                    _stamp_member_validity(parsed, am)
                    raw_sources.append(parsed)
                    own.append(parsed)
        else:
            implicit = _am_as_implicit_source(am)
            if implicit is not None:
                _stamp_member_validity(implicit, am)
                raw_sources.append(implicit)
                own.append(implicit)
        member_sources.append(own)

    deduped = dedupe_sources(raw_sources)

    # Issue athenaeum#1730: resolve each member's sources to the ``[^src-N]``
    # labels ``render_source_footnotes`` will define for them. The label is
    # the source's 1-based position in ``deduped``, found through the SAME
    # ``(session, turn)`` identity the dedupe collapsed on
    # (:func:`source_dedupe_key`) — so a member whose citation was deduped
    # away against an earlier member's still marks its sentences with the
    # surviving footnote rather than losing its citation.
    label_by_key: dict[tuple[str, Any], str] = {}
    for position, source in enumerate(deduped, 1):
        label_by_key.setdefault(source_dedupe_key(source), marker_label(position))
    member_labels: list[list[str]] = []
    for own in member_sources:
        labels: list[str] = []
        for source in own:
            label = label_by_key.get(source_dedupe_key(source))
            if label is not None and label not in labels:
                labels.append(label)
        member_labels.append(labels)

    # Body: concatenate member bodies (minus frontmatter) with a scope/
    # filename header and paragraph-level dedupe, each member's prose stamped
    # with that member's own footnote markers.
    member_bodies: list[tuple[str, str, str]] = []
    body_labels: list[list[str]] = []
    for index, (_mp, am) in enumerate(members):
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        _, body = parse_frontmatter(text)
        member_bodies.append((am.origin_scope, am.path.name, body))
        body_labels.append(member_labels[index])

    body = synthesize_body(member_bodies, body_labels)

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
        valid_until=valid_until,
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
    - ``aliases``: issue athenaeum#1596. A fused cluster's own ``name`` is a
      synthesized slug (:func:`derive_topic_slug`) that matches none of the
      individual members' written names — the addressability harm the issue
      describes. Every member name from :attr:`MergedWikiEntry.member_names`
      OTHER than the chosen ``topic_slug`` itself is preserved here, so the
      page stays findable under every name a caller originally wrote it
      under (FTS5/keyword backends already index ``aliases:`` — see
      ``search.py``'s ``_row_for`` / ``_extract_frontmatter_fields``).
      Omitted entirely when empty (a singleton cluster, or a fused cluster
      whose members all happened to share one name) — same omit-at-default
      convention every optional field in this dict follows.
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
    aliases = [n for n in entry.member_names if n != entry.topic_slug]
    if aliases:
        meta["aliases"] = aliases
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
    # Issue athenaeum#1840: the bucket's DERIVED horizon, rendered immediately
    # next to the bucket it came from. This is the existing athenaeum#308
    # ``valid_until`` key, not a parallel one — which is precisely what makes
    # the compiled page legible to ``athenaeum.decay_sweep`` and to
    # ``mcp_server._is_deprioritized_for_currency``, both of which already
    # key on ``models.valid_until_expired``. Omitted entirely when unset
    # (always, for a ``durable`` or unbucketed page).
    if entry.valid_until:
        meta["valid_until"] = entry.valid_until
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
    as_of: date | None = None,
    out_wiki_root: Path | None = None,
    only_cluster_ids: set[str] | None = None,
    deadline: float | None = None,
    out_stats: dict | None = None,
    projects_root: Path | None = None,
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
            caller (:func:`athenaeum.librarian.run`) guarantees these ids do
            not slug-collide with any unaffected entry before scoping the
            merge. ``None`` (the default) merges every cluster — today's
            whole-corpus behaviour, byte-for-byte.
        out_stats: Issue athenaeum#464 (slice E of athenaeum#460). Optional mutable out-param
            (mirrors :func:`athenaeum.librarian._compile_auto_memory`'s
            ``out_delta_taken`` convention). When given, populated immediately
            before return with ``entries_merged`` (``len(entries)``) so the
            run-level profile summary (athenaeum#464) can thread this counter up
            without recomputing it. Issue athenaeum#1256 retired the other nine
            keys this used to carry (``haiku_calls``, ``resolve_calls``,
            ``chunks_run``, ``pairs_added_via_similarity``,
            ``escalations_written``, ``c4_swept_full``, and friends) along
            with the C4 detector/resolver they described.
        projects_root: Issue athenaeum#1452. Claude Code transcript/memory home,
            forwarded to :func:`~athenaeum.intake.discover_auto_memory_files`
            so a member written by Claude Code's NATIVE memory writer — which
            emits neither ``sources[]`` nor ``originSessionId`` — can have its
            originating session recovered from the scope's own transcripts.
            Without it :func:`_am_as_implicit_source` returns ``None`` for
            every such member and the page compiles with ``sources: []``.
            Ignored when ``auto_memory_files`` is supplied (that list was
            already discovered, recovery and all). Defaults to
            ``~/.claude/projects``; inject a temp dir in tests.

    Returns:
        The list of :class:`MergedWikiEntry` records in cluster-file order.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    # Issue athenaeum#398: resolved once and threaded into the merge-write
    # PhaseHeartbeat below so an operator can tune the tick cadence via
    # ATHENAEUM_HEARTBEAT_INTERVAL / yaml without touching call sites.
    heartbeat_interval = resolve_heartbeat_interval(resolved_config)
    cluster_path = resolve_cluster_output_path(knowledge_root, config=resolved_config)
    rows = read_cluster_rows(cluster_path)
    if not rows:
        log.info("merge pass: no clusters at %s — nothing to merge", cluster_path)
        return []

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

    if auto_memory_files is not None and projects_root is not None:
        # Issue athenaeum#1452: the caller supplied BOTH a pre-discovered member
        # list and a transcript root. The list was already discovered — with
        # whatever recovery its own discovery call did or did not do — so this
        # ``projects_root`` reaches nothing. Say so: a silent no-op here is
        # indistinguishable from "recovery ran and resolved nothing", which is
        # exactly the confusion that would hide a mis-wired caller.
        log.debug(
            "merge pass: projects_root ignored — auto_memory_files was supplied, "
            "so origin-session recovery belongs to that discovery call"
        )

    if auto_memory_files is None:
        auto_memory_files = discover_auto_memory_files(
            knowledge_root,
            config=resolved_config,
            # Issue athenaeum#1452: this is the discovery whose records feed
            # ``_am_as_implicit_source`` below, so the origin-session recovery
            # has to reach THIS call — a memory whose session is recovered only
            # in C1's discovery would still compile with ``sources: []`` here.
            projects_root=projects_root,
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
            config=resolved_config,
        )
        if entry is None:
            continue
        entries.append(entry)

    # Issue athenaeum#278: cluster-cohesion floor. Refuse to materialize a low-cohesion
    # cross-scope OVER-CLUSTER -- a single-linkage chain that blends a coherent
    # source doc with vaguely-similar operational notes from many scopes -- into
    # a durable wiki page. Suppressed entries are dropped from ``entries`` here,
    # BEFORE the write loop, and so never reach the returned list the retire
    # pass walks: their raw members are left in place (NOT retired, NOT lost)
    # for a coherent cluster to absorb on a later run. They remain in
    # ``auto_memory_files``. (Issue athenaeum#1256 retired the C4 detector and
    # its cross-scope similarity sweep, so the old note here about which
    # cross-scope mode would re-examine a suppressed member no longer applies;
    # contradiction detection over these members is the comparator lane's
    # concern now.) The gate is default-off (floor 0.0) -- when off this loop
    # is a no-op pass-through.
    cohesion_floor = resolve_min_cluster_cohesion(resolved_config)
    cohesion_min_scopes = resolve_min_cluster_cohesion_scopes(resolved_config)
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

    wiki_root = out_wiki_root if out_wiki_root is not None else knowledge_root / "wiki"

    # Issue athenaeum#462 / athenaeum#1256: write the deterministic C3 merge
    # output to disk. This used to be the FIRST of two writes -- the page was
    # written unflagged here, then possibly re-written once the (now-retired)
    # C4 contradiction detector decided whether to flag it. Issue athenaeum#1256
    # retired that write-before-detect / re-write pairing along with the
    # detector itself, so this is now simply THE write.
    #
    # C3 stays ATOMIC: the build loop + cohesion floor + run-global slug
    # resolution all complete before this pass, so no page is written
    # mid-build with a not-yet-final slug.
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
            page_path = _route_merged_entry_write(
                entry,
                text,
                wiki_root=wiki_root,
                knowledge_root=knowledge_root,
                config=resolved_config,
                erasure_class_slugs=erasure_class_slugs,
            )
            log.info(
                "merge: wrote %s (cluster %s, %d source(s)) "
                "[write-before-detect retired, athenaeum#1256]",
                page_path if page_path is not None else f"{entry.filename} (off-corpus)",
                entry.cluster_id,
                len(entry.sources),
            )
            write_heartbeat.tick(entry.cluster_id or entry.topic_slug, compiled=1)
        write_heartbeat.done()

    if dry_run:
        for entry in entries:
            log.info(
                "  [DRY RUN] merge %s → wiki/%s (%d source(s))",
                entry.cluster_id,
                entry.filename,
                len(entry.sources),
            )
        if out_stats is not None:
            out_stats.update({"entries_merged": len(entries)})
        return entries

    if out_stats is not None:
        out_stats.update({"entries_merged": len(entries)})

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

    - No LLM calls: issue athenaeum#1256 retired :func:`merge_clusters_to_wiki`'s
      contradiction detector/resolver entirely, so there is no API spend and
      no escalation is written on ANY call, this one included. The blend is
      fully deterministic over the current cluster assignments.
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
        as_of=as_of,
        out_wiki_root=out_dir,
    )
