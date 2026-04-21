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
from pathlib import Path
from typing import Any

from athenaeum.clusters import resolve_cluster_output_path
from athenaeum.config import load_config, resolve_extra_intake_roots
from athenaeum.models import (
    AutoMemoryFile,
    parse_frontmatter,
    render_frontmatter,
)

log = logging.getLogger(__name__)

# Centroid score below which a cluster is flagged for C4 human review.
# Cohesive clusters (identical/near-identical members) sit near 1.0;
# members that share a topic but disagree drift below. 0.75 is the
# documented first-pass heuristic for this PR — C4 replaces it with
# real contradiction detection. Keeping it here (not in config) makes
# the PR diff self-contained; C4 can promote to config when it lands.
CONTRADICTION_COHESION_THRESHOLD = 0.75

# Filesystem prefix that distinguishes auto-memory wiki entries from
# entity-schema entries (``<uid>-<kebab>.md``). Callers reading the
# wiki directory can branch on this prefix without parsing frontmatter.
AUTO_WIKI_PREFIX = "auto-"

# Stopword-ish tokens dropped when deriving a topic slug from member
# filenames — these carry no semantic weight and would otherwise win
# the frequency contest on naturally-clustered files (``feedback_`` is
# the dominant prefix across memories, for example).
_SLUG_BORING_TOKENS: frozenset[str] = frozenset({
    "feedback", "project", "reference", "user", "recall", "auto",
    "memory", "note", "the", "and", "for", "with", "file", "files",
    "md",
})


@dataclass
class MergedWikiEntry:
    """In-memory shape of one consolidated wiki entry."""

    topic_slug: str
    cluster_id: str
    cluster_centroid_score: float
    contradictions_detected: bool
    origin_scopes: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    member_paths: list[str] = field(default_factory=list)

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
                    jsonl_path, exc,
                )
    return rows


# ---------------------------------------------------------------------------
# Member-path resolution
# ---------------------------------------------------------------------------


def resolve_member_path(
    member_ref: str, extra_roots: list[Path],
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
    member_paths: list[str], cluster_id: str,
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


def _parse_one_source(raw: Any, fallback_scope: str) -> dict[str, Any] | None:
    """Normalize one ``sources[]`` entry into a plain dict + origin_scope.

    Accepts dict (the shape defined in
    ``policies/auto-memory-citation.md``) or raw string (legacy bare
    session UUID). Returns ``None`` for unparseable input.
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
        return entry
    if isinstance(raw, str):
        return {"session": raw, "origin_scope": fallback_scope}
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
                cluster_id, mp,
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
                    cluster_id, resolved,
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
            am = AutoMemoryFile(
                path=resolved,
                origin_scope=scope_guess,
                memory_type="unknown",
                name=str(meta.get("name", "")) if meta else "",
                description=str(meta.get("description", "")) if meta else "",
                origin_session_id=(
                    str(origin_session_id)
                    if origin_session_id is not None else None
                ),
                origin_turn=origin_turn,
                sources=sources,
            )
        members.append((mp, am))
        resolved_member_paths.append(mp)

    if not members:
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
        contradictions_detected=centroid_score < CONTRADICTION_COHESION_THRESHOLD,
        origin_scopes=origin_scopes_set,
        sources=deduped,
        body=body,
        member_paths=resolved_member_paths,
    )


def render_merged_entry(entry: MergedWikiEntry) -> str:
    """Render a :class:`MergedWikiEntry` as a full wiki markdown file."""
    meta: dict[str, Any] = {
        "name": entry.topic_slug,
        "type": "auto-memory",
        "cluster_id": entry.cluster_id,
        "cluster_centroid_score": round(entry.cluster_centroid_score, 4),
        "contradictions_detected": bool(entry.contradictions_detected),
        "origin_scopes": list(entry.origin_scopes),
        "sources": list(entry.sources),
    }
    return render_frontmatter(meta) + "\n" + entry.body


def merge_clusters_to_wiki(
    knowledge_root: Path,
    *,
    auto_memory_files: Iterable[AutoMemoryFile] | None = None,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
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
            knowledge_root, config=resolved_config,
        )

    am_by_path = _collect_am_by_path(auto_memory_files)

    entries: list[MergedWikiEntry] = []
    for row in rows:
        entry = merge_cluster_row(
            row, extra_roots=extra_roots, am_by_path=am_by_path,
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
            entry.topic_slug = f"{base}-{suffix}" if suffix else f"{base}-{slug_counts[base]}"
        else:
            slug_counts[base] = 1

    if dry_run:
        for entry in entries:
            log.info(
                "  [DRY RUN] merge %s → wiki/%s (%d source(s), contradictions=%s)",
                entry.cluster_id, entry.filename,
                len(entry.sources), entry.contradictions_detected,
            )
        return entries

    wiki_root = knowledge_root / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        page_path = wiki_root / entry.filename
        page_path.write_text(render_merged_entry(entry), encoding="utf-8")
        log.info(
            "merge: wrote %s (cluster %s, %d source(s))",
            page_path, entry.cluster_id, len(entry.sources),
        )
    return entries
