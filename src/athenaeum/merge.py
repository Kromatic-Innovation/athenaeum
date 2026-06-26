# SPDX-License-Identifier: Apache-2.0
"""Auto-memory merge pass (issue #197, C3).

Consumes the JSONL cluster report produced by C2
(:mod:`athenaeum.clusters`) and emits ONE canonical wiki entry per
cluster at ``wiki/auto-<topic-slug>.md``. Every member's content is
concatenated into a synthesized body; every member's ``sources[]`` is
unioned into a single deduped cited list.

Scope for this module (kept narrow on purpose — see issue #197):

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
  :data:`CONTRADICTION_COHESION_THRESHOLD` (0.75). C4 (#198) replaces
  this with real contradiction detection — this module is only the
  cheap proxy so the human-review queue has a seed.

Out of scope (deliberate — later lanes):

- LLM-based body synthesis. C3's strategy is deterministic:
  concatenate member bodies, drop identical paragraphs, prefix each
  block with a scope/filename header. Rich paraphrase is a follow-up.
- Real contradiction detection (C4, #198).
- Rewrites to ``raw/auto-memory/*`` — raw is append-only; the wiki is
  the compiled view.
- A cross-scope ``wiki/MEMORY.md`` — Phase B explicitly removed it and
  this module does NOT recreate it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._lint import _strip_self_reference
from athenaeum.clusters import resolve_cluster_output_path
from athenaeum.config import load_config, resolve_extra_intake_roots
from athenaeum.contradictions import ContradictionResult, detect_contradictions
from athenaeum.cross_scope import (
    candidate_to_auto_memory_files,
    chunk_by_cap,
    cross_scope_similarity_pairs,
    pool_cluster_with_ancestors,
    resolve_cluster_size_cap,
    resolve_cross_scope_mode,
    resolve_similarity_threshold,
)
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
from athenaeum.models import (
    DEFAULT_SOURCE_TYPE,
    AutoMemoryFile,
    EscalationItem,
    TokenUsage,
    coerce_source_type,
    parse_deprecated,
    parse_frontmatter,
    parse_refines,
    parse_superseded_by,
    parse_supersedes,
    render_frontmatter,
    slugify,
)
from athenaeum.pending_merges import write_pending_merge
from athenaeum.resolutions import (
    PROPOSE_MERGE_ACTION,
    SUPPRESS_ACTION,
    MergeProposal,
    ResolutionProposal,
    propose_resolution,
    render_proposal_block,
    resolve_max_per_run,
)
from athenaeum.tiers import tier4_escalate

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

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

    Lane 1 / #167. Matches by ``AutoMemoryFile.name`` (the documented
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
    # Quine review #171 / SHOULD #4: compare via slugify so a case- or
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

    Issue #172: previously this was all-or-nothing — one undeclared pair
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


def _order_member_paths(
    result: ContradictionResult,
    members: list["AutoMemoryFile"] | None,
) -> list[str]:
    """Return member file paths in the detector's flagged ``a``/``b`` order.

    The resolver labels the two flagged snippets ``a`` and ``b`` in the
    order they appear in ``result.members_involved`` — the SAME order
    :func:`athenaeum.resolutions._build_user_message` presents them to the
    model. The enactment lane (#166 follow-up) keys ``forget_*`` /
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
    """Claim-pair fingerprint for a detector result (issue #249).

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
    origin_scopes: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    member_paths: list[str] = field(default_factory=list)
    contradiction: ContradictionResult | None = None
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


# ---------------------------------------------------------------------------
# Topic-slug derivation
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _slug_tokens_from_filename(filename: str) -> list[str]:
    stem = filename.lower()
    if stem.endswith(".md"):
        stem = stem[:-3]
    # Split on non-alnum so ``project_voltair_nanoclaw`` → voltair, nanoclaw.
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
    regression fixture right (``voltaire-nanoclaw`` from five
    voltaire/nanoclaw files) while staying deterministic and
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

    Issue #260: when a source carries no explicit ``source_ref``, we
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

    Issue #260 (slice A of #259): every parsed source carries an
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
        source_ref = raw.get("source_ref")
        entry["source_ref"] = (
            str(source_ref) if source_ref else _default_source_ref(entry)
        )
        return entry
    if isinstance(raw, str):
        return {
            "session": raw,
            "origin_scope": fallback_scope,
            "source_type": DEFAULT_SOURCE_TYPE,
            # The legacy bare-UUID ref is the session id itself — a valid
            # ultimate ref, never a filename.
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
    # Issue #260: carry origin-traced provenance. An implicit source recovered
    # from originSessionId/turn is unverified at this layer, so honor the
    # file's own declared source_type (default ``inferred``) and back-fill a
    # session+turn ref — never the raw filename.
    entry["source_type"] = coerce_source_type(getattr(am, "source_type", None))
    entry["source_ref"] = getattr(am, "source_ref", "") or _default_source_ref(entry)
    return entry


def dedupe_sources(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe on ``(session, turn)``. First occurrence wins.

    ``(session, turn)`` is the Phase-A granularity lock — two turns
    within the same session are distinct memories. Two citations of
    the same (session, turn) are merged (first wins, stable order).
    Entries missing a turn fall back to ``(session, None)`` and only
    collapse among themselves.
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
) -> MergedWikiEntry | None:
    """Build one :class:`MergedWikiEntry` from a cluster JSONL row.

    Returns ``None`` when every member path fails to resolve to a live
    file on disk — C2's rotated reports may reference files that have
    been removed between runs, and we prefer to skip such rows with a
    log line rather than crash the whole merge pass.

    C4 (#198): contradiction detection is NOT performed here — the caller
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
                    int(origin_turn_raw) if origin_turn_raw is not None else None
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
            # Issue #181: same self-reference lint as discover_auto_memory_files.
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
                # Issue #191: non-destructive inactive markers.
                superseded_by=parse_superseded_by(meta if meta else None),
                deprecated=parse_deprecated(meta if meta else None),
            )
        # Issue #191: skip members marked inactive (superseded_by / deprecated)
        # so their bodies are never composed into the wiki entry and they do
        # not contribute sources. Inactive files stay on disk for audit.
        if am.is_inactive():
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
        # (#191) — skip the row entirely; there is no live claim to compile.
        log.info("cluster %s: no active members; skipping row", cluster_id)
        return None

    topic_slug = derive_topic_slug(resolved_member_paths, cluster_id)
    origin_scopes_set: list[str] = []
    for _mp, am in members:
        if am.origin_scope not in origin_scopes_set:
            origin_scopes_set.append(am.origin_scope)

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
                    raw_sources.append(parsed)
        else:
            implicit = _am_as_implicit_source(am)
            if implicit is not None:
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
        # Default False here; merge_clusters_to_wiki() overrides based on
        # the C4 contradiction-detector result before rendering.
        contradictions_detected=False,
        origin_scopes=origin_scopes_set,
        sources=deduped,
        body=body,
        member_paths=resolved_member_paths,
        resolved_members=[am for _mp, am in members],
    )


def render_source_footnotes(sources: list[dict[str, Any]]) -> str:
    """Render ``[^name]: **Source:** ...`` footnotes for a source list (#260).

    Each origin-traced source becomes one Markdown footnote definition
    carrying its ``source_type`` + ``source_ref``, matching the worked
    example's ``[^name]: **Source:** ...`` style
    (``wiki/a545c038-tristan-kromer.md``). Labels are stable (``src-1``,
    ``src-2``, ...) over the deterministic deduped source order.

    The ULTIMATE-source rule is preserved here: the rendered ref is the
    source's ``source_ref`` (session+turn / URL / document path), back-filled
    from session+turn when absent — never the raw ``auto-memory/...``
    filename. Returns ``""`` for an empty source list.
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
    # Issue #260: append origin-traced source footnotes to the BODY (sources
    # already render to frontmatter above; the footnotes give the human-
    # readable, ultimate-source citation the worked example used).
    body = entry.body
    footnotes = render_source_footnotes(entry.sources)
    if footnotes:
        sep = "" if body.endswith("\n") or not body else "\n"
        body = f"{body}{sep}\n{footnotes}"
    return render_frontmatter(meta) + "\n" + body


def merge_clusters_to_wiki(
    knowledge_root: Path,
    *,
    auto_memory_files: Iterable[AutoMemoryFile] | None = None,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    client: "anthropic.Anthropic | None" = None,
    usage: TokenUsage | None = None,
    now: datetime | None = None,
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
        client: Optional live Anthropic client used for the C4
            contradiction detector. When ``None`` (e.g. ``ANTHROPIC_API_KEY``
            unset), the detector is skipped with a deterministic
            ``detected=False`` fallback — see
            :func:`athenaeum.contradictions.detect_contradictions`.
        usage: Optional run-level :class:`TokenUsage` (issue #220). When
            provided AND a live client is present, every detector (Haiku)
            and resolver (Opus) call increments ``usage.api_calls`` so the
            librarian's run-level budget sees this phase's spend. Each
            response's token + cache counts are accumulated by the callee
            (#239), so the run summary's cache line also reflects this
            phase's traffic.
        now: Optional run-start timestamp (issue #251). Injected for
            deterministic read-time decay of stale auto ``not_a_conflict``
            suppressions — a single frozen ``now`` is compared against each
            cached row's ``resolved_at``. Defaults to ``datetime.now(UTC)``
            (frozen once here so all clusters in the run share one clock).
            Tests pass a fixed value so no wall-clock leaks into assertions.

    Returns:
        The list of :class:`MergedWikiEntry` records in cluster-file order.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    cluster_path = resolve_cluster_output_path(knowledge_root, config=resolved_config)
    rows = read_cluster_rows(cluster_path)
    if not rows:
        log.info("merge pass: no clusters at %s — nothing to merge", cluster_path)
        return []

    extra_roots = resolve_extra_intake_roots(knowledge_root, config=resolved_config)

    if auto_memory_files is None:
        # Lazy import to avoid a circular dep on librarian when this
        # module is imported standalone from a test.
        from athenaeum.librarian import discover_auto_memory_files

        auto_memory_files = discover_auto_memory_files(
            knowledge_root,
            config=resolved_config,
        )

    am_by_path = _collect_am_by_path(auto_memory_files)

    entries: list[MergedWikiEntry] = []
    for row in rows:
        entry = merge_cluster_row(
            row,
            extra_roots=extra_roots,
            am_by_path=am_by_path,
        )
        if entry is None:
            continue
        entries.append(entry)

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

    # C4 (#198 + #125): claim-level contradiction detection.
    #
    # Mode toggle (issue #125, ATHENAEUM_CROSS_SCOPE_MODE):
    # - off: per-cluster only (legacy behavior).
    # - ancestor (default): pool each cluster with ancestor-scope members
    #   then chunk by cap before running the detector.
    # - similarity: per-cluster pass + cosine sweep over raw + wiki.
    # - both: ancestor pooling THEN similarity sweep over remaining pairs.
    wiki_root = knowledge_root / "wiki"
    escalations: list[EscalationItem] = []
    mode = resolve_cross_scope_mode(resolved_config)
    cluster_size_cap = resolve_cluster_size_cap(resolved_config)
    similarity_threshold = resolve_similarity_threshold(resolved_config)

    haiku_calls = 0
    pairs_added_via_similarity = 0
    chunks_run = 0

    # Track which (path_a, path_b) pairs are already covered by a single
    # detector call so the similarity sweep can skip them.
    covered_pair_keys: set[tuple[str, str]] = set()

    # Issue #146: dedup escalations by the SET OF FLAGGED SOURCE MEMBER FILES
    # across the whole run. The same source-file pair is pulled into many
    # overlapping clusters; detection runs per cluster, so without this set
    # one real conflict escalates once per cluster (28 questions → 9 distinct
    # conflicts on 2026-05-22). The key is the sorted flagged members from
    # the detector result (`members_involved`, i.e. source-file identity),
    # NOT the cluster `topic_slug`. Both the primary cluster pass and the
    # similarity sweep route through `_emit_escalation`, so a single set
    # there dedupes both passes.
    escalated_member_keys: set[tuple[str, ...]] = set()

    # Issue #249: fingerprints already settled as not_a_conflict (auto OR human)
    # BEFORE this run started. Skipping ONLY this verdict is safe: other verdicts
    # (keep_a, correct_*, ...) must still flow to tier4_escalate so a prior HUMAN
    # verdict gets auto-enacted on the new page. load_resolved_records applies
    # human-supersedes-auto precedence, so a pair later overridden by a human
    # keep_a is NOT in this set.
    #
    # This is the SKIP gate and is frozen at run start on purpose: a pair the
    # resolver suppresses mid-run must NOT begin short-circuiting later clusters
    # in the SAME run, or it would silently drop a later cluster that the
    # resolver would genuinely re-detect (#145/#146 contract — see
    # ``test_suppressed_pair_does_not_block_later_genuine_detection``). Only a
    # FUTURE run, reloading the cache fresh, treats this run's clearances as
    # settled.
    #
    # Issue #251: read-time decay. With a positive
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
    # Write-dedup set (issue #249, open-question #2): fingerprints written to the
    # cache during THIS run. Bounds file growth without feeding the skip gate
    # above — a mid-run clearance is recorded once but does not suppress later
    # re-detection of the same pair within the run.
    recorded_not_a_conflict_fps: set[str] = set()

    def _record_pair_keys(members: list[AutoMemoryFile]) -> None:
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                covered_pair_keys.add(
                    tuple(sorted((str(members[i].path), str(members[j].path))))
                )

    def _emit_escalation(
        entry: MergedWikiEntry,
        result: ContradictionResult,
        proposal: "ResolutionProposal | MergeProposal | None" = None,
        members: list[AutoMemoryFile] | None = None,
    ) -> None:
        if not result.detected:
            return
        # Lane 3 / issue #169: resolver proposes the two snippets should
        # merge into a single canonical memory. Route the proposal to
        # ``wiki/_pending_merges.md`` for human approval (NOT auto-applied)
        # and DROP the would-be pending-question escalation — the same
        # conflict should not appear in both sidecars.
        if proposal is not None and proposal.action == PROPOSE_MERGE_ACTION:
            assert isinstance(proposal, MergeProposal)
            member_paths = [str(m.path) for m in (members or [])]
            try:
                write_pending_merge(
                    wiki_root / "_pending_merges.md",
                    merge_target_name=proposal.merge_target_name,
                    sources=member_paths,
                    rationale=proposal.rationale,
                    draft_merged_body=proposal.draft_merged_body,
                    confidence=proposal.confidence,
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
        # Confirmation pass (issue #145): the stronger resolver model
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
            # Issue #249: record this clearance so future nights skip the Opus
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
                    # Issue #251: stamp the run-start ``now`` so the decay
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
        # Mutating single-side verdicts (#166 follow-up): correct_a /
        # correct_b (the losing side was WRONG — remove its claim) and
        # forget_a / forget_b (one side is transient — delete it cleanly).
        # These are genuine contradictions, NOT suppressions and NOT
        # merge proposals, so they intentionally fall through to the
        # normal pending-question escalation below. The auto-apply gate in
        # tier4_escalate (per-action threshold 0.90, same as keep_a/keep_b)
        # decides whether the resolution is applied in-place or left for
        # the human — no special routing is needed here. Noted explicitly
        # so a future reader greps the contract and does not add a branch.
        # Issue #146: run-scoped dedup by the flagged source-file set. The
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
        # Append the OPTIONAL Opus-resolver proposal block (issue #126).
        # render_proposal_block returns "" for the deterministic fallback,
        # so entries without a real proposal stay byte-identical to the
        # pre-#126 escalation format.
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
                # forget_*/correct_* auto-apply (#166 follow-up).
                members=_order_member_paths(result, members),
            )
        )

    # Issue #191: drop inactive members (superseded_by / deprecated) from the
    # detector pool so a superseded/deprecated claim cannot generate fresh
    # contradiction escalations. ``am_by_path`` (the row-builder body lookup)
    # is left intact — the row-level skip in ``merge_cluster_row`` handles
    # compile exclusion.
    auto_memory_list = [am for am in auto_memory_files if not am.is_inactive()]
    use_ancestor = mode in ("ancestor", "both")

    # Issue #126: Opus-backed resolver budget. The resolver is opt-in
    # via ANTHROPIC_API_KEY (no client → fallback path); the per-run cap
    # caps Opus calls even when a key is present, so a noisy detector
    # cannot run away with cost. When the budget is exhausted, the
    # remaining contradictions are escalated WITHOUT a proposal —
    # `render_proposal_block` is a no-op on the fallback proposal so the
    # block stays byte-identical to the pre-#126 format.
    resolve_budget = resolve_max_per_run(resolved_config)
    resolve_calls = 0
    resolve_budget_exhausted_logged = False

    def _maybe_propose(
        result: ContradictionResult,
        members: list[AutoMemoryFile],
    ) -> ResolutionProposal | MergeProposal | None:
        nonlocal resolve_calls, resolve_budget_exhausted_logged
        if not result.detected:
            return None
        # Issue #249: a pair already settled as not_a_conflict (auto or human)
        # skips the expensive Opus confirmation entirely. Synthesize the
        # SUPPRESS proposal so existing code drops the escalation (the loop
        # sets ``suppressed`` and ``_emit_escalation`` returns) WITHOUT
        # consuming budget or an api_call.
        fp = _result_claim_fingerprint(result)
        if fp and fp in cleared_not_a_conflict_fps:
            log.info(
                "contradictions: claim-pair already settled as not_a_conflict "
                "(fingerprint=%s); skipping Opus confirmation (issue #249)",
                fp,
            )
            return ResolutionProposal(
                recommended_winner="neither",
                action=SUPPRESS_ACTION,
                rationale="cached not_a_conflict (issue #249)",
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
        if usage is not None and client is not None:
            usage.api_calls += 1
        return propose_resolution(result, members, client, usage=usage)

    for entry in entries:
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
        for chunk in chunks:
            chunks_run += 1
            # Lane 1 / #167: short-circuit when every pair in the chunk
            # declares the other via refines/supersedes. Saves a Haiku
            # call and prevents the over-fire path from flagging
            # already-resolved pairs.
            filtered, declared = _filter_declared_pairs(chunk)
            if declared is not None and not filtered:
                # Fully-declared chunk — no Haiku call at all.
                _record_pair_keys(chunk)
                result = ContradictionResult(detected=False, rationale=declared)
                continue
            # Issue #172: partial prune — Haiku only sees members that
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
            haiku_calls += 1
            if usage is not None and client is not None:
                usage.api_calls += 1
            result = detect_contradictions(
                filtered, client, config=resolved_config, usage=usage
            )
            _record_pair_keys(chunk)
            if result.detected and aggregate is None:
                proposal = _maybe_propose(result, filtered)
                # When the confirmation pass suppresses the cluster, the
                # detector over-fired: leave `aggregate` unset so the
                # wiki entry frontmatter is NOT tagged
                # contradiction-flagged. Otherwise a suppressed cluster
                # would carry a "contradiction-flagged" status with no
                # pending question to point at (issue #145).
                # `_emit_escalation` independently drops the escalation
                # for the suppress verdict.
                if proposal is not None and proposal.action == SUPPRESS_ACTION:
                    suppressed = True
                elif proposal is not None and proposal.action == PROPOSE_MERGE_ACTION:
                    # Lane 3: routed to _pending_merges.md, not a contradiction.
                    suppressed = True
                else:
                    aggregate = result
                _emit_escalation(entry, result, proposal, members=filtered)
        if aggregate is None:
            if suppressed:
                # Detector fired but the confirmation pass cleared it —
                # record a clean not-detected verdict so the wiki entry
                # frontmatter is coherent (issue #145).
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

    # Similarity sweep (mode in {similarity, both}).
    if mode in ("similarity", "both"):
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
        )
        for cand in candidates:
            pair = candidate_to_auto_memory_files(cand)
            # Lane 1 / #167: skip similarity-sweep pairs that declare
            # each other. Mirrors the primary-pass short-circuit so a
            # declared-supersession pair never reaches the detector.
            _filtered, declared = _filter_declared_pairs(list(pair))
            if declared is not None and not _filtered:
                continue
            haiku_calls += 1
            if usage is not None and client is not None:
                usage.api_calls += 1
            result = detect_contradictions(
                pair, client, config=resolved_config, usage=usage
            )
            if result.detected:
                pairs_added_via_similarity += 1
                # Synthesize a thin escalation entry; we don't have a
                # MergedWikiEntry for cross-pair similarity hits, so
                # build a minimal one tied to the first member's name.
                synthetic = MergedWikiEntry(
                    topic_slug=cand.a_path.stem,
                    cluster_id=f"similarity-{cand.a_path.stem}-{cand.b_path.stem}",
                    cluster_centroid_score=cand.similarity,
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
        return entries

    wiki_root.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        page_path = wiki_root / entry.filename
        page_path.write_text(render_merged_entry(entry), encoding="utf-8")
        log.info(
            "merge: wrote %s (cluster %s, %d source(s), contradictions=%s)",
            page_path,
            entry.cluster_id,
            len(entry.sources),
            entry.contradictions_detected,
        )

    if escalations:
        tier4_escalate(
            escalations,
            wiki_root / "_pending_questions.md",
            config=resolved_config,
        )

    return entries
