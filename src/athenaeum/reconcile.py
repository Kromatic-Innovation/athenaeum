# SPDX-License-Identifier: Apache-2.0
"""Dual-write raw-intake reconcile (issue athenaeum#1143 / athenaeum-adapters#154).

L4 domain/pipeline.

Contract: on 2026-04-16 the ``streak-to-wiki`` producer (an
``athenaeum-adapters`` adapter, tracked on that repo) dual-wrote — for a large
fraction of the entities it exported, it wrote BOTH a Lane A raw-intake file
under ``raw/<source>/`` AND the compiled wiki page directly, instead of
letting the raw file flow through the normal tiered compile. The raw copies
were never retired, so they sit in the intake backlog forever: every nightly
``athenaeum run`` re-offers them to the tier ladder, and — because at least
one wiki-side cleanup (athenaeum#279, which removed a duplicated "Kromatic
Sales Pipeline" template from ~453 person pages) has already deliberately
REMOVED content the raw copies still carry — compiling one of these files
does not merely waste LLM spend on a no-op, it can reinstate content a human
already decided to delete.

This module is the fix: for each pending raw file under a given
``raw/<source>/`` tree, decide whether the wiki *already, genuinely* holds
its content, and if so retire it — never compiling it. Zero LLM calls; the
decision is a pure git-history predicate.

**"Already holds" — the exact definition.** A raw file is removed only when
ALL of the following hold:

1. Its entity resolves, via the LIVE :class:`~athenaeum.models.EntityIndex`
   (never a filename-prefix guess) — by ``uid`` frontmatter field first via
   :meth:`~athenaeum.models.EntityIndex.get_by_uid`, then by ``name``
   frontmatter field via :meth:`~athenaeum.models.EntityIndex.lookup` — to an
   existing wiki page.
2. The raw file's CURRENT on-disk bytes are identical to its bytes AT
   ``import_commit`` (the dual-write commit). A file touched since import is
   not describable by import-time evidence, so it is retained rather than
   trusted.
3. The resolved wiki page EXISTED at ``import_commit``.
4. The raw file's bytes AT ``import_commit`` were byte-identical to the wiki
   page's bytes AT ``import_commit``.

Only when (1)-(4) all hold is the pair provably the SAME dual-write event —
the producer wrote the same content twice, once to each tree, at the same
commit. Nothing about a LATER edit to either side changes that: the raw
file's content was captured into the wiki at the moment they were written
together, so compiling the raw file again is pure duplication regardless of
what the wiki page looks like today. This is exactly why the athenaeum#279
template-removal case is removed correctly even though the CURRENT wiki page
no longer contains that template: the deletion was deliberate and later, and
condition (4) only ever compares import-time snapshots.

Every other raw file — no resolvable uid at all (genuinely new content),
resolved but modified since import, resolved but wasn't part of the import
commit, or resolved but diverged from the wiki page even at import time — is
retained untouched and flows through the ordinary compile pipeline exactly
as before this module existed. A raw file whose uid appears in more than one
pending file (issue's "278 uids across 565 files" case) needs no special
casing: each file is independently tested against its OWN import-time bytes,
so every copy that was byte-identical at import is removed and any copy that
was not is retained — the duplicate-uid case is handled by construction, not
deferred.

Recovery is git-only, mirroring :mod:`athenaeum.retire`: ``git rm`` (never a
hard unlink), refused entirely when the knowledge root is not a versioned
store. Removed files stay recoverable from git history —
``git show <commit>^:<path>`` (or checking out the parent of the reconcile
commit) restores the exact bytes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.config import load_config
from athenaeum.intake import discover_raw_files
from athenaeum.models import EntityIndex, parse_frontmatter

# Disposition labels — one is the removal outcome, the rest are retention
# reasons (issue's "removed / retained / genuinely-new" report contract,
# with retention broken out by reason for operator legibility).
REMOVED = "removed"
GENUINELY_NEW = "genuinely_new"  # uid/name did not resolve to any wiki page
MODIFIED_SINCE_IMPORT = "modified_since_import"  # raw bytes changed post-import
NOT_IN_IMPORT_COMMIT = "not_in_import_commit"  # raw path absent at import_commit
WIKI_ABSENT_AT_IMPORT = "wiki_absent_at_import"  # wiki page didn't exist yet
DIVERGED_AT_IMPORT = "diverged_at_import"  # raw != wiki bytes at import_commit
NOT_VERSIONED = "not_versioned"  # knowledge_root has no git repo — refuse to remove

_RETAINED_DISPOSITIONS = frozenset(
    {
        GENUINELY_NEW,
        MODIFIED_SINCE_IMPORT,
        NOT_IN_IMPORT_COMMIT,
        WIKI_ABSENT_AT_IMPORT,
        DIVERGED_AT_IMPORT,
        NOT_VERSIONED,
    }
)


@dataclass
class ReconcileDisposition:
    """One raw file's planned (or enacted) fate."""

    ref: str  # "<source>/<filename>" — matches RawFile.ref
    path: Path
    disposition: str
    uid: str = ""
    wiki_path: str = ""
    reason: str = ""


@dataclass
class ReconcileReport:
    """Structured outcome of one reconcile pass."""

    dry_run: bool = True
    committed: bool = False
    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    genuinely_new: list[str] = field(default_factory=list)
    dispositions: list[ReconcileDisposition] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _git(knowledge_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
        check=check,
    )


def _git_show_text(knowledge_root: Path, commit: str, rel_path: str) -> str | None:
    """Return *rel_path*'s text at *commit*, or None if it doesn't exist there.

    ``git show <commit>:<path>`` exits non-zero both for "path never existed
    at that commit" and a handful of other lookup failures (bad commit,
    detached-object issues) — all fold to "cannot prove identity", which is
    the fail-closed (retain) branch every caller here takes on None.
    """
    proc = _git(knowledge_root, "show", f"{commit}:{rel_path}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def resolve_entity_path(index: EntityIndex, meta: dict[str, Any]) -> Path | None:
    """Resolve a raw file's frontmatter to a live wiki page path, or None.

    ``uid`` is tried first via :meth:`EntityIndex.get_by_uid` (exact,
    unambiguous — the producer stamped the entity's own uid into the raw
    file). Falls back to ``name``/alias lookup via :meth:`EntityIndex.lookup`
    when there is no uid, or the uid no longer resolves (entity renamed/
    merged/deleted since import). Returns None when neither resolves —
    the "genuinely new" case: this raw file's uid does not exist in the
    live wiki at all.
    """
    uid = meta.get("uid")
    if isinstance(uid, str) and uid:
        hit = index.get_by_uid(uid)
        if hit is not None:
            return hit
    name = meta.get("name")
    if isinstance(name, str) and name:
        looked = index.lookup(name)
        if looked is not None:
            return looked[1]
    return None


def plan_reconcile(
    knowledge_root: Path,
    *,
    source: str,
    import_commit: str,
    config: dict[str, Any] | None = None,
) -> ReconcileReport:
    """Compute the reconcile plan for every pending ``raw/<source>/`` file.

    Read-only — never touches disk or git state. :func:`run_reconcile` is
    the write-capable wrapper that enacts a plan's ``removed`` set.
    """
    report = ReconcileReport()
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    resolved_config = config if config is not None else load_config(knowledge_root)
    index = EntityIndex(wiki_root)
    kr = knowledge_root.resolve()
    # Checked ONCE, upfront — never inferred from a git command's exit code.
    # A knowledge_root with no `.git` of its own could still sit inside a
    # PARENT git repo (`git show` walks up), which would make the
    # byte-identity checks below silently compare against the wrong
    # repository's history. Recovery is git-only (mirrors
    # athenaeum.retire's identical rule), so a missing `.git` here fails
    # closed and explicitly (NOT_VERSIONED) rather than falling through to
    # per-file git lookups that might resolve against the wrong tree.
    is_versioned = (knowledge_root / ".git").is_dir()

    candidates = [
        raw for raw in discover_raw_files(raw_root, resolved_config) if raw.source == source
    ]
    for raw in sorted(candidates, key=lambda r: r.path):
        try:
            rel_raw = str(raw.path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append((raw.ref, "raw file is outside knowledge_root"))
            continue
        try:
            current_text = raw.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append((raw.ref, f"read failed: {exc}"))
            continue

        meta, _ = parse_frontmatter(current_text)
        uid_raw = meta.get("uid")
        uid = uid_raw if isinstance(uid_raw, str) else ""

        wiki_path = resolve_entity_path(index, meta)
        if wiki_path is None:
            report.genuinely_new.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    GENUINELY_NEW,
                    uid=uid,
                    reason="uid/name does not resolve to any live wiki entity",
                )
            )
            continue

        if not is_versioned:
            report.retained.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    NOT_VERSIONED,
                    uid=uid,
                    wiki_path=str(wiki_path),
                    reason="knowledge_root has no .git — refusing to verify import-time identity",
                )
            )
            continue

        raw_at_import = _git_show_text(knowledge_root, import_commit, rel_raw)
        if raw_at_import is None:
            report.retained.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    NOT_IN_IMPORT_COMMIT,
                    uid=uid,
                    wiki_path=str(wiki_path),
                    reason=f"raw path did not exist at {import_commit}",
                )
            )
            continue
        if raw_at_import != current_text:
            report.retained.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    MODIFIED_SINCE_IMPORT,
                    uid=uid,
                    wiki_path=str(wiki_path),
                    reason="on-disk bytes differ from import-commit bytes",
                )
            )
            continue

        try:
            rel_wiki = str(wiki_path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append((raw.ref, "resolved wiki path is outside knowledge_root"))
            continue

        wiki_at_import = _git_show_text(knowledge_root, import_commit, rel_wiki)
        if wiki_at_import is None:
            report.retained.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    WIKI_ABSENT_AT_IMPORT,
                    uid=uid,
                    wiki_path=str(wiki_path),
                    reason=f"wiki page did not exist at {import_commit}",
                )
            )
            continue

        if raw_at_import != wiki_at_import:
            report.retained.append(raw.ref)
            report.dispositions.append(
                ReconcileDisposition(
                    raw.ref,
                    raw.path,
                    DIVERGED_AT_IMPORT,
                    uid=uid,
                    wiki_path=str(wiki_path),
                    reason="raw and wiki bytes differed even at import_commit",
                )
            )
            continue

        report.removed.append(raw.ref)
        report.dispositions.append(
            ReconcileDisposition(
                raw.ref,
                raw.path,
                REMOVED,
                uid=uid,
                wiki_path=str(wiki_path),
                reason=f"byte-identical to wiki page at {import_commit}",
            )
        )

    return report


def run_reconcile(
    knowledge_root: Path,
    *,
    source: str,
    import_commit: str,
    config: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> ReconcileReport:
    """Plan, then (unless ``dry_run``) enact the reconcile via ``git rm``.

    Idempotent: a removed file is gone on the next call to
    :func:`plan_reconcile`, so a second ``run_reconcile`` with no new intake
    finds nothing left to remove.

    :func:`plan_reconcile` already fails closed on a non-versioned
    ``knowledge_root`` (see its ``NOT_VERSIONED`` bucket) — nothing ever
    lands in ``report.removed`` without a real ``.git``, so this function
    has no separate versioned check of its own to defend a git operation
    that can never run.
    """
    report = plan_reconcile(
        knowledge_root, source=source, import_commit=import_commit, config=config
    )
    report.dry_run = dry_run
    if dry_run or not report.removed:
        return report

    kr = knowledge_root.resolve()
    rel_paths = [
        str(disp.path.resolve().relative_to(kr))
        for disp in report.dispositions
        if disp.disposition == REMOVED
    ]
    _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
    _git(
        knowledge_root,
        "commit",
        "-m",
        f"reconcile: retire {len(rel_paths)} {source} raw file(s) already "
        f"materialized in wiki at {import_commit}\n\n"
        "Removed because each file's bytes were byte-identical to its "
        "corresponding wiki page's bytes at the dual-write commit. "
        "Recoverable via `git show <this commit>^:<path>`.",
    )
    report.committed = True
    return report
