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

# Issue #261 / Quine M1: ``detect_contradictions`` returns ``detected=False``
# both for a genuine clean verdict AND when it degraded (offline, API error,
# unparseable response). A degraded not-detected verdict is NOT trustworthy —
# retiring a genuinely-contradictory cluster on a degraded verdict would delete
# raw that a working detector would have held. These rationales mark the
# degraded paths (see ``athenaeum.contradictions`` + the merge C4 loop); a
# cluster carrying one of them is HELD, never moved. A legitimate
# ``singleton`` / ``declared-*`` / empty (real clean) rationale still moves.
DEGRADED_RATIONALES: frozenset[str] = frozenset(
    {
        "llm-unavailable",
        "detector-returned-no-json",
        "detector-invalid-conflict-type",
        "detector-malformed-response",
    }
)


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


def _commit_paths_if_staged(
    knowledge_root: Path, rel_paths: list[str], message: str
) -> bool:
    """Stage ONLY ``rel_paths`` and commit iff that produces staged changes.

    Quine C2/nit3: the provenance snapshot must not ``git add -A`` — that
    sweeps unrelated working-tree edits and any prior staged deletion into a
    misleadingly-labelled "provenance" commit. We stage exactly the
    auto-memory raw intake paths being snapshotted and commit only if the
    add actually staged something (already-committed unchanged files stage
    nothing → no empty commit). Returns True when a commit was made.
    """
    if not rel_paths:
        return False
    _git(knowledge_root, "add", "--", *rel_paths)
    # diff --cached --quiet exits 1 when there are staged changes, 0 when clean.
    staged = _git(knowledge_root, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return False
    _git(knowledge_root, "commit", "-m", message)
    log.info("retire: git commit: %s", message)
    return True


def _move_eligibility(entry: MergedWikiEntry) -> tuple[bool, str]:
    """Decide whether a cluster may be MOVED, and the HOLD reason if not.

    Quine M1: MOVE only on a TRUSTWORTHY not-detected verdict. A cluster is
    held when the detector flagged it, when there is no verdict at all, or
    when the verdict is one of :data:`DEGRADED_RATIONALES` (offline / API
    error / unparseable). Everything else (real clean verdict, ``singleton``,
    declared resolutions) is move-eligible.
    """
    if entry.contradictions_detected:
        return False, "contradiction flagged — queued for human confirmation"
    c = entry.contradiction
    if c is None:
        return False, "no contradiction verdict available — not safe to retire"
    if c.rationale in DEGRADED_RATIONALES:
        return False, f"degraded detection ({c.rationale}) — not safe to retire"
    return True, ""


def _open_pending_text(wiki_root: Path) -> str:
    """Concatenated text of the open pending-confirmation sidecars.

    Quine S1: the retire decision must also respect prior-run human
    confirmations still in flight. We read ``_pending_questions.md`` and
    ``_pending_merges.md`` and let the caller substring-check member refs;
    a member referenced anywhere in them is held (conservative — "if in
    doubt, keep it"). Missing files contribute the empty string.
    """
    parts: list[str] = []
    for name in ("_pending_questions.md", "_pending_merges.md"):
        p = wiki_root / name
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


def _member_in_pending(member: Path, pending_text: str) -> bool:
    """True when a member is referenced by an open pending entry (Quine S1).

    Matches both writer formats: the ``<scope>/<filename>`` ref used in
    ``_pending_questions.md`` ("Members involved: …") and the absolute path
    used in ``_pending_merges.md`` (sources list).
    """
    if not pending_text:
        return False
    ref = f"{member.parent.name}/{member.name}"
    return ref in pending_text or str(member) in pending_text


def _member_landed(member: Path, body: str) -> bool:
    """True when a member's section is present in the synthesized body (Quine S2).

    ``synthesize_body`` drops a member whose paragraphs were all seen verbatim
    in an earlier member (or whose body is empty). If a member's fact did not
    land in the wiki entry, its raw must NOT be git rm'd. The section header is
    the deterministic ``## From `<scope>/<filename>` `` form.
    """
    header = f"## From `{member.parent.name}/{member.name}`"
    return (header + "\n") in body


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


# Issue #262 (slice C of #259): the contradiction rationales that represent a
# genuine RESOLVED verdict worth persisting on the moved fact's footnote. A
# plain clean move (``singleton`` / empty rationale) carries no verdict; only
# these settled outcomes do. ``confirmation-pass-cleared`` is the merge.py
# label for a detector over-fire that the Opus confirmation pass cleared as
# ``not_a_conflict`` — we persist the resolver's verdict, not the internal
# label.
_VERDICT_RATIONALES: dict[str, str] = {
    "confirmation-pass-cleared": "not_a_conflict",
    "not_a_conflict": "not_a_conflict",
    "declared-supersession": "declared-supersession",
    "declared-refinement": "declared-refinement",
    # merge.py prunes a chunk to <2 members when every undeclared pair has been
    # removed (a declared refines/supersedes resolution lived in the text). The
    # surviving entry is move-eligible and genuinely settled, so its declared
    # resolution is persisted too (Quine #3a). The specific class (refinement
    # vs supersession) is not carried on this rationale, so it records the
    # generic "declared-resolution".
    "declared-pruned-to-singleton": "declared-resolution",
}


def _resolved_verdict(entry: MergedWikiEntry) -> str | None:
    """Return the persistable resolved verdict for a moved entry, or None.

    Issue #262: when a contradiction was settled (the confirmation pass
    cleared a detector over-fire, or the members declared a
    supersession/refinement) the resulting verdict is recorded on the wiki
    fact's footnote so a future memory can reuse it instead of re-paying to
    re-adjudicate. A plain clean move (no contradiction, ``singleton``)
    returns None — there is no verdict to persist.
    """
    c = entry.contradiction
    if c is None:
        return None
    return _VERDICT_RATIONALES.get((c.rationale or "").strip())


def _enrich_entry(entry: MergedWikiEntry, projects_root: Path | None) -> None:
    """Upgrade footnote provenance via transcript verification + attach markers.

    For each resolved member we ask
    :func:`athenaeum.transcript_verify.verify_user_stated` whether the
    transcript confirms the claim. A verified result (``user-stated`` /
    ``external`` / ``document``) UPGRADES the matching source; we never
    downgrade an already-verified source back to ``inferred`` (append-only
    provenance). Then per-fact inline markers are attached and the entry is
    flagged ``retired``.

    Issue #262 (slice C of #259): every moved member also stamps the granular
    ``claim`` text (and a resolved ``verdict``, when one exists) onto its
    source so the wiki footnote becomes the diff target the contradiction
    engine compares future intake against — the retired raw atom is gone.
    Append-only: an existing ``claim`` / ``verdict`` is never overwritten.
    """
    index_map = _source_index_map(entry)
    by_key = {
        (str(src.get("session", "")), src.get("turn")): src for src in entry.sources
    }
    verdict = _resolved_verdict(entry)
    for am in entry.resolved_members:
        if am.origin_session_id is None:
            continue
        claim = _member_claim(am.path, am.description or am.name)
        src = by_key.get((str(am.origin_session_id), am.origin_turn))
        if src is not None:
            # Persist the granular diff target FIRST — independent of whether
            # transcript verification upgrades the provenance below.
            if claim and "claim" not in src:
                src["claim"] = claim
            if verdict and "verdict" not in src:
                src["verdict"] = verdict
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
    kr = knowledge_root.resolve()
    pending_text = _open_pending_text(wiki_root)

    # (entry, members_to_retire) — only the members whose fact actually landed
    # in the wiki body AND that live under knowledge_root are retire-eligible.
    retiring: list[tuple[MergedWikiEntry, list[Path]]] = []
    for entry in entries:
        members = _resolve_members(entry, extra_roots)

        eligible, hold_reason = _move_eligibility(entry)
        if not eligible:
            # HOLD: contradictory OR a degraded/absent verdict — a delete must
            # never race a pending confirmation, and a degraded verdict is not
            # trustworthy enough to retire on.
            for m in members:
                report.held.append(str(m))
                report.dispositions.append(
                    FileDisposition(str(m), HOLD, entry.cluster_id, hold_reason)
                )
            continue

        if not members:
            # No live members resolved (already retired / removed) — nothing
            # to move. Idempotency falls out of this branch.
            continue

        # Quine S1: never retire raw still referenced by an open pending entry.
        if any(_member_in_pending(m, pending_text) for m in members):
            for m in members:
                report.held.append(str(m))
                report.dispositions.append(
                    FileDisposition(
                        str(m),
                        HOLD,
                        entry.cluster_id,
                        "open pending confirmation references this cluster — "
                        "not retired",
                    )
                )
            continue

        # Quine S2 + S3: a member is retire-eligible only when its fact landed
        # in the wiki body AND it lives under knowledge_root (git-recoverable
        # from this repo). Otherwise retain its raw.
        retire_members: list[Path] = []
        for m in members:
            if not _member_landed(m, entry.body):
                report.dispositions.append(
                    FileDisposition(
                        str(m),
                        SKIP,
                        entry.cluster_id,
                        "section dropped (empty/duplicate) — raw retained",
                    )
                )
                continue
            try:
                m.resolve().relative_to(kr)
            except ValueError:
                report.dispositions.append(
                    FileDisposition(
                        str(m),
                        SKIP,
                        entry.cluster_id,
                        "member outside knowledge_root — raw retained",
                    )
                )
                continue
            retire_members.append(m)
            report.moved.append(str(m))
            report.dispositions.append(
                FileDisposition(
                    str(m),
                    MOVE,
                    entry.cluster_id,
                    f"non-contradictory — moved into wiki/{entry.filename}",
                )
            )

        if retire_members:
            retiring.append((entry, retire_members))
            report.wiki_updated.append(entry.filename)

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
        _log_report(report)  # Quine nit1: surface why nothing happened.
        return report

    if not retiring:
        # Holds only (or empty) — nothing to commit. No-op run.
        _log_report(report)
        return report

    # Commit A — provenance snapshot. Commits ONLY the raw intake about to be
    # retired (scoped add, Quine C2) so every file we are about to git rm is
    # recoverable from history.
    snapshot_rel = [
        str(m.resolve().relative_to(kr)) for _e, members in retiring for m in members
    ]
    _commit_paths_if_staged(
        knowledge_root,
        snapshot_rel,
        "librarian: auto-memory raw intake provenance snapshot",
    )

    # Move: enrich each retiring entry's wiki page (verified footnotes +
    # per-fact markers + retired flag) and overwrite it.
    wiki_rel: list[str] = []
    for entry, _members in retiring:
        _enrich_entry(entry, projects_root)
        page = wiki_root / entry.filename
        page.write_text(render_merged_entry(entry), encoding="utf-8")
        wiki_rel.append(str(page.resolve().relative_to(kr)))

    # Retire: git rm the moved raw files (staged deletion, recoverable).
    del_rel = [
        str(m.resolve().relative_to(kr)) for _e, members in retiring for m in members
    ]
    _git(knowledge_root, "rm", "--quiet", "--", *del_rel)

    # Commit B — wiki updates + raw deletions TOGETHER (single recoverable
    # commit). Scoped staging (Quine C2): the deletions are already staged by
    # ``git rm``; we add exactly the wiki entries we rewrote, not ``-A``.
    _git(knowledge_root, "add", "--", *wiki_rel)
    _git(
        knowledge_root,
        "commit",
        "-m",
        f"librarian: move-then-retire ({len(report.moved)} moved, "
        f"{len(report.held)} held)",
    )
    report.committed = True
    log.info(
        "retire: moved %d raw file(s) into %d wiki entr(y/ies); held %d; committed",
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
