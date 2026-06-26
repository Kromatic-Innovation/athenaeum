# SPDX-License-Identifier: Apache-2.0
"""Move-then-retire lifecycle for raw auto-memory (issue #261, slice B of #259).

``raw/auto-memory/`` is an *expiring intake queue*, not a permanent source.
Per nightly run, once the C3 merge has compiled each cluster into a canonical
``wiki/auto-<topic>.md`` entry and the C4 detector has run, this pass decides
the fate of every cluster's raw intake:

- **non-contradictory** → the fact is *moved* into the wiki entry (with an
  origin-traced footnote and a ``retired: true`` marker) and the raw files are
  retired with ``git rm`` so they no longer re-enter the nightly loop.
- **contradictory** → the raw files are *held* in the queue for human
  confirmation. They are NEVER deleted while a contradiction is pending.

Hard rules (see issue #261):

- A delete must NEVER race a pending confirmation. A raw file is retired only
  when its cluster produced no contradiction AND its members resolved to live
  files that were merged into the wiki entry. If in doubt, keep it.
- Recovery is git-only. We ``git rm`` (recoverable from history), never
  hard-unlink. The pass refuses to act when ``knowledge_root`` is not a git
  repo.
- The provenance snapshot (commit A) commits the raw intake before deletion so
  every retired file is recoverable; the wiki updates + raw deletions then land
  TOGETHER in a single commit (commit B).
- ``--dry-run`` computes the exact same plan, logs a structured report, and
  writes NOTHING (no wiki edits, no ``git rm``, no commit).
- Idempotent: a second run with no new intake finds no resolvable members for
  already-retired clusters → nothing to move → no commit.

Origin tracing reuses slice A's :func:`athenaeum.transcript_verify.verify_user_stated`
to upgrade a source from the honest ``inferred`` default to ``user-stated`` /
``external`` when the session transcript confirms it. The ultimate-source
invariant still holds: a footnote never cites the raw ``auto-memory/...``
filename.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.config import load_config, resolve_extra_intake_roots
from athenaeum.merge import (
    MergedWikiEntry,
    render_merged_entry,
    resolve_member_path,
)
from athenaeum.models import DEFAULT_SOURCE_TYPE, parse_frontmatter
from athenaeum.transcript_verify import verify_user_stated

log = logging.getLogger(__name__)

# Disposition labels for the per-file plan, surfaced in the dry-run report.
MOVE = "move"
HOLD = "hold"
SKIP = "skip"


@dataclass
class FileDisposition:
    """One raw file's planned fate in the move-then-retire pass."""

    path: str
    disposition: str  # MOVE | HOLD | SKIP
    cluster_id: str
    reason: str = ""


@dataclass
class RetireReport:
    """Structured outcome of one retire pass (used for the dry-run report)."""

    dry_run: bool = False
    committed: bool = False
    moved: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    wiki_updated: list[str] = field(default_factory=list)
    dispositions: list[FileDisposition] = field(default_factory=list)


def _git(
    knowledge_root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
        check=check,
    )


def _git_snapshot(knowledge_root: Path, message: str) -> bool:
    """Stage all changes and commit if any. Returns True when a commit was made.

    Mirrors :func:`athenaeum.librarian.git_snapshot` but is inlined here to
    avoid importing ``librarian`` (which imports this module — a cycle).
    """
    if not (knowledge_root / ".git").exists():
        return False
    status = _git(knowledge_root, "status", "--porcelain")
    if not status.stdout.strip():
        return False
    _git(knowledge_root, "add", "-A")
    _git(knowledge_root, "commit", "-m", message)
    log.info("retire: git commit: %s", message)
    return True


def _member_claim(am_path: Path, fallback: str) -> str:
    """Return the member file's body text (frontmatter stripped) for verification."""
    try:
        text = am_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return fallback
    _, body = parse_frontmatter(text)
    body = body.strip()
    return body or fallback


def _source_index_map(entry: MergedWikiEntry) -> dict[tuple[str, object], int]:
    """Map ``(session, turn)`` → 1-based index into ``entry.sources``."""
    out: dict[tuple[str, object], int] = {}
    for i, src in enumerate(entry.sources, start=1):
        key = (str(src.get("session", "")), src.get("turn"))
        out.setdefault(key, i)
    return out


def _attach_inline_markers(
    entry: MergedWikiEntry, index_map: dict[tuple[str, object], int]
) -> str:
    """Append a per-fact ``[^src-N]`` marker to each member's body section.

    Slice A rendered the footnotes as a trailing appendix only; slice B wires
    the per-fact inline attachment the policy deferred. The section header
    format is the deterministic one produced by
    :func:`athenaeum.merge.synthesize_body`
    (``## From `<scope>/<filename>` ``), so the match is exact, not heuristic.
    Members whose section was dropped (empty / all-duplicate paragraphs) are
    skipped silently.
    """
    body = entry.body
    for am in entry.resolved_members:
        key = (str(am.origin_session_id or ""), am.origin_turn)
        idx = index_map.get(key)
        if idx is None:
            continue
        header = f"## From `{am.origin_scope}/{am.path.name}`"
        marked = f"{header} [^src-{idx}]"
        # Replace only the header line (followed by a newline) once.
        if header + "\n" in body:
            body = body.replace(header + "\n", marked + "\n", 1)
    return body


def _enrich_entry(entry: MergedWikiEntry, projects_root: Path | None) -> None:
    """Upgrade footnote provenance via transcript verification + attach markers.

    For each resolved member we ask
    :func:`athenaeum.transcript_verify.verify_user_stated` whether the
    transcript confirms the claim. A verified result (``user-stated`` /
    ``external`` / ``document``) UPGRADES the matching source; we never
    downgrade an already-verified source back to ``inferred`` (append-only
    provenance). Then per-fact inline markers are attached and the entry is
    flagged ``retired``.
    """
    index_map = _source_index_map(entry)
    by_key = {
        (str(src.get("session", "")), src.get("turn")): src for src in entry.sources
    }
    for am in entry.resolved_members:
        if am.origin_session_id is None:
            continue
        claim = _member_claim(am.path, am.description or am.name)
        stype, sref = verify_user_stated(
            am.origin_scope,
            am.origin_session_id,
            am.origin_turn,
            claim=claim,
            projects_root=projects_root,
        )
        if stype == DEFAULT_SOURCE_TYPE:
            # Unverifiable / transcript rolled off — leave the source's
            # existing (possibly already-verified) provenance untouched.
            continue
        src = by_key.get((str(am.origin_session_id), am.origin_turn))
        if src is None:
            continue
        src["source_type"] = stype
        if sref:
            src["source_ref"] = sref

    entry.body = _attach_inline_markers(entry, index_map)
    entry.retired = True


def _resolve_members(entry: MergedWikiEntry, extra_roots: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for mp in entry.member_paths:
        hit = resolve_member_path(mp, extra_roots)
        if hit is not None:
            resolved.append(hit)
    return resolved


def run_retire_pass(
    entries: list[MergedWikiEntry],
    knowledge_root: Path,
    *,
    config: dict[str, object] | None = None,
    dry_run: bool = False,
    projects_root: Path | None = None,
) -> RetireReport:
    """Move non-contradictory raw into the wiki, hold contradictory, git rm moved.

    Args:
        entries: The :class:`MergedWikiEntry` list returned by
            :func:`athenaeum.merge.merge_clusters_to_wiki` THIS run. Each entry
            already carries ``contradictions_detected`` (from the C4 detector)
            plus ``member_paths`` / ``resolved_members``.
        knowledge_root: Root of the knowledge directory (``wiki/`` + ``raw/``
            + ``.git`` live here).
        config: Optional resolved ``athenaeum.yaml`` dict; resolves the intake
            roots used to map ``member_paths`` back to live files.
        dry_run: When True, compute + log the plan and return without writing
            anything (no wiki edits, no ``git rm``, no commit).
        projects_root: Transcript root for origin verification (injectable in
            tests). Defaults to ``~/.claude/projects`` inside
            :func:`verify_user_stated`.

    Returns:
        A :class:`RetireReport` describing what was (or, on dry-run, would be)
        moved, held, and committed.
    """
    report = RetireReport(dry_run=dry_run)
    resolved_config = config if config is not None else load_config(knowledge_root)
    extra_roots = resolve_extra_intake_roots(knowledge_root, config=resolved_config)
    wiki_root = knowledge_root / "wiki"

    retiring: list[tuple[MergedWikiEntry, list[Path]]] = []
    for entry in entries:
        members = _resolve_members(entry, extra_roots)
        if entry.contradictions_detected:
            # HOLD: a delete must never race a pending confirmation.
            for m in members:
                report.held.append(str(m))
                report.dispositions.append(
                    FileDisposition(
                        str(m),
                        HOLD,
                        entry.cluster_id,
                        "contradiction flagged — queued for human confirmation",
                    )
                )
            continue
        if not members:
            # No live members resolved (already retired / removed) — nothing
            # to move. Idempotency falls out of this branch.
            continue
        retiring.append((entry, members))
        report.wiki_updated.append(entry.filename)
        for m in members:
            report.moved.append(str(m))
            report.dispositions.append(
                FileDisposition(
                    str(m),
                    MOVE,
                    entry.cluster_id,
                    f"non-contradictory — moved into wiki/{entry.filename}",
                )
            )

    if dry_run:
        _log_report(report)
        return report

    if not (knowledge_root / ".git").exists():
        # Recovery is git-only: refuse to retire without a writable git repo.
        log.warning(
            "retire: no .git in %s — refusing to retire raw (recovery is "
            "git-only); leaving %d candidate file(s) in place",
            knowledge_root,
            len(report.moved),
        )
        for disp in report.dispositions:
            if disp.disposition == MOVE:
                disp.disposition = SKIP
                disp.reason = "no git repo — not retired (recovery is git-only)"
        report.moved = []
        report.wiki_updated = []
        return report

    if not retiring:
        # Holds only (or empty) — nothing to commit. No-op run.
        _log_report(report)
        return report

    kr = knowledge_root.resolve()

    # Commit A — provenance snapshot. Commits the raw intake (and the merge's
    # freshly written wiki entries) so every file we are about to git rm is
    # recoverable from history.
    _git_snapshot(knowledge_root, "librarian: auto-memory provenance snapshot")

    # Move: enrich each retiring entry's wiki page (verified footnotes +
    # per-fact markers + retired flag) and overwrite it.
    for entry, _members in retiring:
        _enrich_entry(entry, projects_root)
        page = wiki_root / entry.filename
        page.write_text(render_merged_entry(entry), encoding="utf-8")

    # Retire: git rm the moved raw files (staged deletion, recoverable).
    rel_paths: list[str] = []
    for _entry, members in retiring:
        for m in members:
            rel_paths.append(str(m.resolve().relative_to(kr)))
    _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)

    # Commit B — wiki updates + raw deletions TOGETHER (single recoverable commit).
    _git(knowledge_root, "add", "-A")
    _git(
        knowledge_root,
        "commit",
        "-m",
        f"librarian: move-then-retire ({len(report.moved)} moved, "
        f"{len(report.held)} held)",
    )
    report.committed = True
    log.info(
        "retire: moved %d raw file(s) into %d wiki entr(y/ies); held %d; " "committed",
        len(report.moved),
        len(report.wiki_updated),
        len(report.held),
    )
    return report


def _log_report(report: RetireReport) -> None:
    prefix = "[DRY RUN] " if report.dry_run else ""
    log.info(
        "%sretire plan: %d to move, %d held, %d wiki entr(y/ies) updated",
        prefix,
        len(report.moved),
        len(report.held),
        len(report.wiki_updated),
    )
    for disp in report.dispositions:
        log.info(
            "  %s%s: %s (cluster %s) — %s",
            prefix,
            disp.disposition,
            disp.path,
            disp.cluster_id,
            disp.reason,
        )
