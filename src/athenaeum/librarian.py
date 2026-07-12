# SPDX-License-Identifier: Apache-2.0
"""Knowledge librarian — tiered compilation pipeline.

Processes raw intake files from a knowledge directory's raw/ folder into wiki
entity pages using a four-tier approach:

  Tier 1: Programmatic entity matching (no LLM)
  Tier 2: Classification via fast LLM
  Tier 3: Content writing via capable LLM
  Tier 4: Human escalation to _pending_questions.md

Usage:
  athenaeum run [--raw-root PATH] [--wiki-root PATH] [--dry-run]

Environment:
  ANTHROPIC_API_KEY          Required for Tier 2/3 LLM calls.
  ATHENAEUM_CLASSIFY_MODEL   Override the Tier 2 model (default: claude-haiku-4-5-20251001)
  ATHENAEUM_WRITE_MODEL      Override the Tier 3 model (default: claude-sonnet-4-6)
  ATHENAEUM_MAX_FILES        Override the per-run intake batch size (default: 50)
  ATHENAEUM_BATCH_MODE       Opt into Batch API mode for tier-2/3 calls (default: off)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from athenaeum._lint import _strip_self_reference
from athenaeum._retry import TransientAPIError
from athenaeum.clusters import (
    cluster_auto_memory_files,
    prune_cluster_rotations,
    resolve_cluster_output_path,
    resolve_cluster_threshold,
    resolve_rotation_retention,
    write_cluster_report,
)
from athenaeum.config import (
    load_config,
    resolve_delta_enabled,
    resolve_delta_max_affected_clusters,
    resolve_delta_max_affected_members,
    resolve_ephemeral_scopes,
    resolve_extra_intake_roots,
    resolve_operational_markers,
    resolve_push_after_run,
    resolve_push_branch,
    resolve_push_remote,
    resolve_retire,
)
from athenaeum.delta import compute_affected_clusters, splice_cluster_report
from athenaeum.ephemeral import classify_ephemeral
from athenaeum.merge import (
    derive_topic_slug,
    merge_clusters_to_wiki,
    read_cluster_rows,
)
from athenaeum.models import (
    AutoMemoryFile,
    EntityAction,
    EntityIndex,
    ProcessingResult,
    RawFile,
    TokenUsage,
    WikiEntity,
    coerce_source_type,
    load_schema_list,
    parse_access,
    parse_asserter,
    parse_claim_kind,
    parse_deprecated,
    parse_frontmatter,
    parse_model,
    parse_on_behalf_of,
    parse_refines,
    parse_superseded_by,
    parse_supersedes,
    render_frontmatter,
    safe_source_ref,
    slugify,
    validity_bound_str,
)
from athenaeum.provider import (
    ProviderConfigError,
    build_llm_client,
    preflight_provider,
    resolve_provider,
)
from athenaeum.schemas import validate_wiki_meta
from athenaeum.self_resolving import flag_self_resolving_claims
from athenaeum.tiers import (
    tier1_programmatic_match,
    tier2_classify,
    tier3_write,
    tier4_escalate,
)

log = logging.getLogger("athenaeum")


# Defaults — can be overridden via CLI args or the run() API
DEFAULT_KNOWLEDGE_ROOT = Path.home() / "knowledge"
DEFAULT_RAW_ROOT = DEFAULT_KNOWLEDGE_ROOT / "raw"
DEFAULT_WIKI_ROOT = DEFAULT_KNOWLEDGE_ROOT / "wiki"

# Run-level API call budget.
# Raised 200 -> 800 (issue #220): the 2026-06-11 nightly observed 404 calls
# hit the 200 cap with intake remaining — now that the #187 confirmation
# pass runs at full coverage, a busy night legitimately needs more than 200
# calls, and the budget-tripped run stopped early while reporting success.
# The cap is a ceiling, not a target: quiet runs never approach it and pay
# nothing extra. Operators can override via `librarian.max_api_calls`
# (yaml), `ATHENAEUM_MAX_API_CALLS` (env), or `--max-api-calls` (CLI flag,
# wins over both).
# The budget is run-level: one TokenUsage is created at run start and
# threaded through the cluster/merge/reresolve phases, so their API spend
# counts against the cap. The entity-tier loop is the enforcement point —
# it is the last phase, so it defers remaining intake when the budget is
# spent. The merge-phase resolver additionally has its own per-run cap
# (`contradiction.resolve_max_per_run`).
DEFAULT_MAX_API_CALLS = 800

# Per-run intake batch size (issue #232). Precedence: `--max-files` (CLI
# flag, wins) > `ATHENAEUM_MAX_FILES` (env) > `librarian.max_files` (yaml)
# > this default. Resolved by `librarian_max_files()` below.
DEFAULT_MAX_FILES = 50

# Manifest written next to _pending_questions.md when a budget-tripped run
# defers intake (issue #220). Overwritten on every tripped run; removed by
# the next clean run.
DEFERRED_MANIFEST_NAME = "_deferred_work.md"

# Fallback valid values if schema files are missing
FALLBACK_TYPES = [
    "person",
    "company",
    "project",
    "concept",
    "tool",
    "reference",
    "source",
    "preference",
    "principle",
]
FALLBACK_ACCESS = ["open", "internal", "confidential", "personal"]
FALLBACK_TAGS = [
    "active",
    "archived",
    "blocked",
]

# Raw file naming: {timestamp}-{uuid8}.md
RAW_FILE_RE = re.compile(r"^(\d{8}T\d{6}Z?)-([0-9a-f]{8})\.md$", re.IGNORECASE)

# Auto-memory file naming: <prefix>_<slug>.md where prefix is one of
# feedback|project|reference|user|Recall. Slug is underscore-separated
# lowercase, but the regex only constrains the prefix — typo bodies
# (e.g. project_foo_bar.md) must still match so C2 clustering
# can dedupe them downstream. The ``Recall`` prefix is capitalized in
# production (see raw/auto-memory/.../Recall_architecture.md); lowercase
# ``recall_`` is also accepted defensively.
AUTO_MEMORY_FILE_RE = re.compile(
    r"^(feedback|project|reference|user|Recall|recall)_(.+)\.md$"
)

# Filenames to skip in auto-memory scope scan. ``MEMORY.md`` is the
# per-scope curated index generated by build-per-scope-memory-index.py
# (mirrors search.py's _INTAKE_SKIP_NAMES contract). Non-.md files are
# already filtered by the glob, but ``_migration-log.jsonl`` lives at
# raw/auto-memory/ root — excluded by the directory-only iteration below.
_AUTO_MEMORY_SKIP_NAMES: frozenset[str] = frozenset({"MEMORY.md"})


def discover_auto_memory_files(
    knowledge_root: Path | None = None,
    config: dict[str, object] | None = None,
) -> list[AutoMemoryFile]:
    """Find all auto-memory intake files under ``raw/auto-memory/<scope>/``.

    Uses :func:`resolve_extra_intake_roots` to pick up the auto-memory
    root from config (``recall.extra_intake_roots``) — does NOT hard-code
    the path. This keeps the config surface single-sourced with the
    recall index builder.

    Returns a list of :class:`AutoMemoryFile` records sorted by
    ``(scope, filename)``. ``MEMORY.md`` files and non-directory entries
    at the auto-memory root (e.g. ``_migration-log.jsonl``) are excluded.
    The ``_unscoped/`` directory is included as a scope alongside named
    scopes — its files are first-class memories, not metadata.
    """
    if knowledge_root is None:
        knowledge_root = Path.home() / "knowledge"

    # resolve_extra_intake_roots returns absolute paths for every
    # configured intake root; in the default config the only entry is
    # raw/auto-memory but callers can configure more, so we iterate all.
    roots = resolve_extra_intake_roots(knowledge_root, config=config)
    if not roots:
        return []

    # Issue #278: resolve the ephemeral/operational classifier inputs once.
    # An ephemeral-scope OR ``ephemeral: true``-flagged intake is dropped
    # HERE -- the cleanest choke point -- so it is never clustered or
    # materialized into a durable ``wiki/auto-*.md`` page. Drops are logged
    # with their reason; the raw file stays on disk (the move-then-retire
    # pass only touches members that landed in a wiki entry), so a dropped
    # file is simply re-evaluated (and re-dropped) idempotently next run.
    resolved_config = config if config is not None else load_config(knowledge_root)
    ephemeral_scopes = resolve_ephemeral_scopes(resolved_config)
    operational_markers = resolve_operational_markers(resolved_config)
    dropped_ephemeral = 0

    files: list[AutoMemoryFile] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Directory-only iteration at the root level. This is how we
        # skip _migration-log.jsonl and any other non-scope sibling
        # files without relying on the .md glob alone.
        for scope_dir in sorted(root.iterdir()):
            if not scope_dir.is_dir():
                continue
            scope = scope_dir.name
            for fpath in sorted(scope_dir.glob("*.md")):
                if fpath.name in _AUTO_MEMORY_SKIP_NAMES:
                    continue
                m = AUTO_MEMORY_FILE_RE.match(fpath.name)
                if not m:
                    # Defensive: anything not matching the auto-memory
                    # convention is skipped here. Entity-schema files
                    # (<timestamp>-<uuid8>.md) naturally fall through
                    # because they lack the prefix.
                    continue
                memory_type = m.group(1).lower()
                try:
                    text = fpath.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                meta, _body = parse_frontmatter(text)
                # Issue #278: drop ephemeral/operational intake before it can
                # be clustered + merged into a permanent wiki entity.
                drop_reason = classify_ephemeral(
                    scope,
                    meta,
                    _body,
                    ephemeral_scopes=ephemeral_scopes,
                    operational_markers=operational_markers,
                )
                if drop_reason is not None:
                    dropped_ephemeral += 1
                    log.info(
                        "auto-memory: dropping ephemeral intake %s - %s",
                        fpath,
                        drop_reason,
                    )
                    continue
                name = str(meta.get("name", "")) if meta else ""
                description = str(meta.get("description", "")) if meta else ""
                origin_session_id = meta.get("originSessionId") if meta else None
                if origin_session_id is not None:
                    origin_session_id = str(origin_session_id)
                origin_turn_raw = meta.get("originTurn") if meta else None
                origin_turn: int | None
                try:
                    origin_turn = (
                        int(origin_turn_raw) if origin_turn_raw is not None else None
                    )
                except (TypeError, ValueError):
                    origin_turn = None
                sources_raw = meta.get("sources") if meta else None
                if isinstance(sources_raw, list):
                    sources = [str(s) for s in sources_raw]
                else:
                    sources = []
                # Issue #260 (slice A of #259): origin-traced provenance.
                # Missing source_type defaults to ``inferred``; source_ref is
                # the ultimate reference and is never this file's own name.
                source_type = coerce_source_type(
                    meta.get("source_type") if meta else None
                )
                # Guard the explicit path: a frontmatter source_ref that is a
                # raw filename (or any ``.md``) is rejected to "" rather than
                # cited as the ultimate source (#260 invariant).
                source_ref = safe_source_ref(
                    meta.get("source_ref") if meta else None, ""
                )
                # Lane 1 / #167: declared refines/supersedes relationships.
                # Malformed entries raise — surfacing the bad file rather
                # than silently dropping the declaration.
                try:
                    refines = parse_refines(meta if meta else None)
                    supersedes = parse_supersedes(meta if meta else None)
                except ValueError as exc:
                    log.warning(
                        "auto-memory %s: invalid refines/supersedes (%s); "
                        "treating as empty",
                        fpath,
                        exc,
                    )
                    refines = []
                    supersedes = []
                # Issue #173 / #181: drop refines/supersedes self-references.
                refines, supersedes = _strip_self_reference(
                    name, refines, supersedes, fpath
                )
                # Issue #191: non-destructive inactive markers.
                meta_for_markers = meta if meta else None
                files.append(
                    AutoMemoryFile(
                        path=fpath,
                        origin_scope=scope,
                        memory_type=memory_type,
                        name=name,
                        description=description,
                        origin_session_id=origin_session_id,
                        origin_turn=origin_turn,
                        sources=sources,
                        refines=refines,
                        supersedes=supersedes,
                        superseded_by=parse_superseded_by(meta_for_markers),
                        deprecated=parse_deprecated(meta_for_markers),
                        source_type=source_type,
                        source_ref=source_ref,
                        # Issue #326: channel-split provenance annotations.
                        model=parse_model(meta_for_markers),
                        on_behalf_of=parse_on_behalf_of(meta_for_markers),
                        asserter=parse_asserter(meta_for_markers),
                        # Issue #327: epistemic claim kind (fail-open when
                        # absent/unrecognized → "" unclassified).
                        claim_kind=parse_claim_kind(meta_for_markers),
                        # Issue #308: claim-level temporal validity bounds.
                        valid_from=validity_bound_str(meta_for_markers, "valid_from"),
                        valid_until=validity_bound_str(meta_for_markers, "valid_until"),
                    )
                )
    if dropped_ephemeral:
        log.info(
            "auto-memory: dropped %d ephemeral/operational intake file(s) "
            "before clustering (issue #278)",
            dropped_ephemeral,
        )
    return files


def discover_raw_files(raw_root: Path) -> list[RawFile]:
    """Find all raw intake files, sorted by timestamp."""
    files: list[RawFile] = []
    if not raw_root.exists():
        return files

    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir():
            continue
        source = source_dir.name
        for fpath in sorted(source_dir.glob("*.md")):
            if fpath.name == ".gitkeep":
                continue
            m = RAW_FILE_RE.match(fpath.name)
            if m:
                files.append(
                    RawFile(
                        path=fpath,
                        source=source,
                        timestamp=m.group(1),
                        uuid8=m.group(2),
                    )
                )
            else:
                files.append(
                    RawFile(
                        path=fpath,
                        source=source,
                        timestamp="",
                        uuid8="",
                    )
                )
    return files


def rebuild_index(wiki_root: Path) -> None:
    """Rebuild _index.md from all entity pages in the wiki."""
    from datetime import date

    by_type: dict[str, list[tuple[str, str, str]]] = {}
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or not meta.get("name"):
            continue
        etype = meta.get("type", "unknown")
        uid = meta.get("uid", "")
        name = meta.get("name", fpath.stem)
        by_type.setdefault(etype, []).append((name, uid, fpath.name))

    lines = [
        "# Knowledge Wiki Index",
        "",
        "Auto-maintained by the knowledge librarian. Lists all entity pages",
        "grouped by type.",
        "",
        f"_Last updated: {date.today().isoformat()}_",
        f"_Total entities: {sum(len(v) for v in by_type.values())}_",
        "",
    ]
    for etype in sorted(by_type.keys()):
        lines.append(f"## {etype.title()}")
        lines.append("")
        for name, uid, filename in sorted(by_type[etype], key=lambda x: x[0].lower()):
            label = f"`{uid}` " if uid else ""
            lines.append(f"- {label}[{name}]({filename})")
        lines.append("")

    (wiki_root / "_index.md").write_text("\n".join(lines), encoding="utf-8")
    log.info(
        "Rebuilt _index.md with %d entities", sum(len(v) for v in by_type.values())
    )


def git_snapshot(knowledge_root: Path, message: str) -> bool:
    """Stage all changes and commit if there are any. Returns True if committed."""
    if not (knowledge_root / ".git").exists():
        log.warning("No .git in %s — skipping git snapshot", knowledge_root)
        return False

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return False

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(knowledge_root),
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(knowledge_root),
        check=True,
    )
    log.info("Git commit: %s", message)
    return True


def _maybe_push_after_run(
    knowledge_root: Path,
    *,
    config: dict | None,
    push_after_run: bool,
    dry_run: bool,
    head_at_start: str | None,
) -> None:
    """Push the knowledge repo iff the run committed at least one new commit.

    Issue #284 gating: (a) explicit opt-in, (b) not a ``--dry-run``,
    (c) HEAD moved during the run. Push failure is non-fatal — ``git_push``
    logs a warning; the run's exit code is unchanged.
    """
    if not push_after_run or dry_run or head_at_start is None:
        return
    head_now = _capture_head(knowledge_root)
    if head_now is None or head_now == head_at_start:
        return
    git_push(
        knowledge_root,
        remote=resolve_push_remote(config),
        branch=resolve_push_branch(config),
    )


def _capture_head(knowledge_root: Path) -> str | None:
    """Return the HEAD sha of the knowledge repo, or ``None`` if unreachable.

    Used by the post-run push hook (issue #284) to detect whether the run
    produced any commit across librarian / retire / future commit sites
    without threading a flag through each one.
    """
    if not (knowledge_root / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_push(
    knowledge_root: Path,
    remote: str = "origin",
    branch: str | None = None,
) -> bool:
    """Push the knowledge repo's current branch to *remote* (issue #284).

    Returns ``True`` when the push succeeded, ``False`` otherwise. A failure
    is logged as a clearly-marked WARNING and does NOT roll back the
    committed run — commits remain locally and the next run's push picks
    them up (``git push`` is idempotent). The push uses the operator's
    ambient git credentials (credential helper / SSH); athenaeum itself
    handles no tokens or secrets.

    When *branch* is ``None``, ``git push`` defaults to the configured
    upstream for the current branch (the conventional nightly setup).
    Passing an explicit branch makes the refspec deterministic.
    """
    if not (knowledge_root / ".git").exists():
        log.warning("No .git in %s — skipping git push", knowledge_root)
        return False

    cmd = ["git", "push", remote]
    if branch:
        cmd.append(branch)
    result = subprocess.run(
        cmd,
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Non-fatal: surface the failure with a distinct log line so an
        # operator (or the routine watching the run) can see exactly which
        # remote rejected the push and why. Commits remain intact locally.
        log.warning(
            "athenaeum-push-failed: git push %s%s exited %d (commits "
            "remain local; next run retries): %s",
            remote,
            f" {branch}" if branch else "",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    log.info(
        "Pushed knowledge commits to %s%s",
        remote,
        f" {branch}" if branch else "",
    )
    return True


def tier0_passthrough(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    valid_types: list[str],
    dry_run: bool = False,
) -> WikiEntity | None:
    """Promote a pre-structured raw-intake file to wiki/ verbatim.

    Some upstream producers (e.g. ``generate_warm_wiki.py``, contact-sync
    scripts) emit raw-intake markdown that is *already* in valid wiki
    schema — has ``uid``, ``type``, ``name``, plus rich custom-namespace
    frontmatter (``relationship:``, ``exclude:``, ``apollo_*``,
    ``current_title``, ``linkedin_url``, etc.). Sending such files through
    Tier 2/3 is wasteful (one Haiku + one Sonnet call per file) AND lossy:
    the LLM-driven path rebuilds frontmatter from a fixed allowlist and
    drops any field outside it.

    This passthrough writes the raw frontmatter + body to ``wiki/``
    byte-for-byte, only stamping ``created`` (if missing) and ``updated``
    to today. No classification runs; the index is updated so later raw
    files in the same pipeline can match against it.

    Returns the new :class:`WikiEntity` on success, or ``None`` if the
    raw is unstructured / ineligible (caller should fall through to
    Tier 1/2/3). Eligibility gate: frontmatter parses, ``uid``/``type``/
    ``name`` are non-empty, ``type`` is in the schema's allowlist, and the
    uid is not already present in the index (idempotent re-runs).
    """
    meta, body = parse_frontmatter(raw.content)
    if not meta:
        return None
    uid = str(meta.get("uid", "") or "").strip()
    etype = str(meta.get("type", "") or "").strip()
    name = str(meta.get("name", "") or "").strip()
    if not uid or not etype or not name:
        return None
    if etype not in valid_types:
        return None
    if index.get_by_uid(uid) is not None:
        return None

    today = date.today().isoformat()
    if not meta.get("created"):
        meta["created"] = today
    meta["updated"] = today

    filename = f"{uid}-{slugify(name)}.md"
    out_path = wiki_root / filename
    if out_path.exists():
        # Filename collision with a different uid would be a real bug,
        # but a same-uid existing file is already covered by the index
        # check above. Defer to Tier 1/2/3 rather than overwrite blindly.
        return None

    aliases_raw = meta.get("aliases") or []
    tags_raw = meta.get("tags") or []
    entity = WikiEntity(
        uid=uid,
        type=etype,
        name=name,
        aliases=[str(a) for a in aliases_raw if a],
        access=str(meta.get("access", "internal")),
        tags=[str(t) for t in tags_raw if t],
        created=str(meta.get("created", today)),
        updated=str(meta.get("updated", today)),
        body=body,
    )

    # Validate frontmatter against the Pydantic schema before write. This
    # is the schema gate for the byte-for-byte passthrough — malformed
    # custom-namespace fields are still accepted (extra="allow"), but the
    # uid/type/name contract is enforced. Raises pydantic.ValidationError
    # on failure; caller treats that as a real bug, not a fall-through.
    validate_wiki_meta(meta)

    if dry_run:
        return entity

    out_path.write_text(
        render_frontmatter(meta) + "\n" + body,
        encoding="utf-8",
    )
    index.register(entity)
    return entity


def process_one(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    client: anthropic.Anthropic | None,
    valid_types: list[str],
    valid_tags: list[str],
    valid_access: list[str],
    dry_run: bool = False,
    usage: TokenUsage | None = None,
    config: dict[str, object] | None = None,
) -> ProcessingResult:
    """Process a single raw file through all tiers.

    ``config`` is the resolved athenaeum.yaml dict (issue #232) — it routes
    the ``models:`` section to the Tier 2/3 calls. ``None`` (legacy/test
    callers) keeps env > code-default model resolution.
    """
    result = ProcessingResult(raw_file=raw)

    # Sticky intake access (issue #320 §5): an `access:` stamped on the raw
    # file at remember() time by the intake screener is CALLER-AUTHORITATIVE —
    # it must survive compile onto the wiki page, not be re-guessed by the LLM
    # tiers (which classify access from scratch and can drop or widen it). Read
    # it from the ORIGINAL content before the self-resolving-claims mutation
    # below touches raw._content. Tier 0 already honors raw `access:` verbatim;
    # this pins the same guarantee onto the Tier-2/3 LLM path for the unstructured
    # medical notes that never reach Tier 0. Empty when the raw carries none.
    raw_meta, _ = parse_frontmatter(raw.content)
    sticky_access = parse_access(raw_meta)

    # --- Tier 0: passthrough for pre-structured raw-intake ---
    # When upstream producers already emit valid wiki-schema frontmatter,
    # promote verbatim without LLM classification. Preserves custom
    # namespaces the LLM tiers would otherwise drop.
    passthrough = tier0_passthrough(
        raw,
        index,
        wiki_root,
        valid_types,
        dry_run=dry_run,
    )
    if passthrough is not None:
        log.info(
            "  T0 passthrough: %s → %s",
            passthrough.name,
            passthrough.filename,
        )
        result.created.append(passthrough)
        return result

    # --- Tier 1: Programmatic matching ---
    matched = tier1_programmatic_match(raw, index)
    matched_names = [name for name, _, _ in matched]

    for name, uid_or_name, fpath in matched:
        if index.has_entity_format(fpath):
            log.info("  T1 match (entity format): %s → %s", name, fpath.name)
        else:
            log.info("  T1 match (old format, skip): %s → %s", name, fpath.name)
            result.skipped.append(name)

    if dry_run:
        log.info(
            "  [DRY RUN] T1 matched %d, skipped %d — LLM tiers skipped",
            len(matched),
            len(result.skipped),
        )
        log.info(
            "  [DRY RUN] Raw content preview: %s", raw.content[:120].replace("\n", " ")
        )
        return result

    # Deterministic self-resolving-document guard (issue #300 follow-up,
    # #304): flag embedded self-confirmation claims BEFORE any LLM stage
    # sees the text, so the untrusted-data boundary doesn't depend on the
    # model choosing to notice the claim itself. Mutates only this
    # in-memory RawFile's cached content, not the raw file on disk, so
    # each future run re-reads the real, unflagged raw file — but the
    # flagged text DOES persist downstream into this run's wiki writes
    # (Tier 2's own observations, and the raw.content[:2000] fallback
    # below), by design: the warning is meant to survive into whatever
    # Tier 3 sees, not just the classify prompt.
    raw._content = flag_self_resolving_claims(raw.content)

    # --- Tier 2: Classification ---
    classified = tier2_classify(
        raw,
        matched_names,
        valid_types,
        valid_tags,
        valid_access,
        client,
        wiki_root=wiki_root,
        usage=usage,
        config=config,
    )
    log.info("  T2 classified %d new entities", len(classified))

    # Enforce the sticky intake access (issue #320 §5) on every NEW entity the
    # LLM created from this raw: the screener's label is authoritative and is
    # never downgraded — take the more restrictive of (raw label, LLM guess).
    # Scoped to new entities only; a merge into a pre-existing page (below) does
    # not relabel that page from this one raw file.
    if sticky_access:
        from athenaeum.screening import more_restrictive

        for c in classified:
            c.access = more_restrictive(c.access, sticky_access)

    # Build actions
    actions: list[EntityAction] = []
    for c in classified:
        actions.append(
            EntityAction(
                kind="create",
                name=c.name,
                entity_type=c.entity_type,
                tags=c.tags,
                access=c.access,
                existing_uid=c.existing_uid,
                observations=c.observations or raw.content[:2000],
            )
        )

    for name, uid_or_name, fpath in matched:
        if index.has_entity_format(fpath):
            actions.append(
                EntityAction(
                    kind="update",
                    name=name,
                    entity_type="",
                    tags=[],
                    access="",
                    existing_uid=uid_or_name,
                    observations=raw.content[:2000],
                )
            )

    if not actions:
        log.info("  No actions needed for %s", raw.ref)
        return result

    # --- Tier 3: Content writing ---
    assert client is not None, "client required for non-dry-run"
    new_entities, updated_uids, escalations = tier3_write(
        raw,
        actions,
        index,
        wiki_root,
        client,
        usage=usage,
        config=config,
    )

    for entity in new_entities:
        page_path = wiki_root / entity.filename
        rendered = entity.render()
        # Schema-gate the LLM-produced entity before write. Re-parse the
        # rendered frontmatter so the validator sees exactly the bytes
        # that would land on disk — this round-trip catches YAML-render
        # quirks (numeric coercion, quoting drift, key reordering edge
        # cases) that a direct dict-validate would miss. Deliberate; do
        # NOT collapse to validating ``entity`` directly without first
        # re-parsing ``rendered``.
        rendered_meta, _ = parse_frontmatter(rendered)
        validate_wiki_meta(rendered_meta)
        page_path.write_text(rendered, encoding="utf-8")
        index.register(entity)
        result.created.append(entity)
        log.info("  Created: %s → %s", entity.name, entity.filename)

    result.updated.extend(updated_uids)
    result.escalated.extend(escalations)

    # --- Tier 4: Escalation ---
    if escalations:
        # wiki_root is <knowledge_root>/wiki; the config sits at the
        # knowledge_root level. Reuse the caller's resolved config when
        # provided; otherwise resolve it here so the auto-apply lane
        # (issue #156) sees the operator's yaml settings.
        tier4_escalate(
            escalations,
            wiki_root / "_pending_questions.md",
            config=config if config is not None else load_config(wiki_root.parent),
        )

    return result


def _write_cluster_report_and_prune(
    clusters: list,
    output_path: Path,
    knowledge_root: Path,
    resolved_config: dict[str, object] | None,
) -> None:
    """Write *clusters* to the canonical report and prune old rotations (#311)."""
    canonical, timestamped = write_cluster_report(clusters, output_path)
    log.info(
        "cluster report written: %s (rotated copy: %s)",
        canonical,
        timestamped,
    )
    if timestamped is not None:
        retention = resolve_rotation_retention(knowledge_root, config=resolved_config)
        try:
            pruned = prune_cluster_rotations(output_path, keep=retention)
            if pruned:
                log.info(
                    "pruned %d old cluster rotation(s) (retention=%d)",
                    len(pruned),
                    retention,
                )
        except Exception as exc:  # noqa: BLE001 — prune must not abort the run
            log.warning("cluster rotation prune failed (non-fatal): %s", exc)


def _run_cluster_pass(
    auto_memory_files: list[AutoMemoryFile],
    knowledge_root: Path,
    *,
    config: dict[str, object] | None = None,
    dry_run: bool = False,
    changed_paths: set[Path] | None = None,
) -> set[str] | None:
    """Cluster discovered auto-memory files and write the JSONL report.

    Reuses the recall-index chromadb collection via
    :class:`athenaeum.search.VectorBackend`; falls back to a hashing-
    trick vector if the index is unavailable.

    Returns:
        - ``None`` in the whole-corpus mode (the merge pass should recompile
          every cluster), including the dry-run, empty-input, and
          no-extra-roots short circuits, AND every delta fallback (D1-D3/D2).
        - ``set[str]`` when the delta path (issue #370 PR2) engaged: the NEW
          cluster ids that were (re)clustered and written this pass, so the
          merge pass can recompile ONLY those and leave every unaffected
          ``wiki/auto-*.md`` untouched.

    ``changed_paths`` (issue #370 PR2) is the set of absolute auto-memory paths
    that changed this run. When provided AND delta is viable, only those files
    and their affected clusters are re-clustered + spliced into the existing
    report. ``None`` (the default) preserves the whole-corpus behaviour
    byte-for-byte.
    """
    if not auto_memory_files:
        return None

    resolved_config = config if config is not None else load_config(knowledge_root)
    extra_roots = resolve_extra_intake_roots(knowledge_root, config=resolved_config)
    if not extra_roots:
        log.info("cluster pass: no extra intake roots configured — skipping")
        return None

    threshold = resolve_cluster_threshold(knowledge_root, config=resolved_config)

    # Issue #370: a dry-run must not cluster at all — ``cluster_auto_memory_files``
    # opens the chromadb collection (loading ONNX) to fetch embeddings. Return
    # BEFORE that call so a dry-run stays a cheap preview even if some other
    # caller reaches this pass under dry_run (defense-in-depth: ``run()`` also
    # guards the call site, and ``ingest(dry_run=True)`` no longer invokes run).
    if dry_run:
        log.info(
            "  [DRY RUN] cluster pass: %d auto-memory file(s) — skipping "
            "clustering (no chromadb/model load)",
            len(auto_memory_files),
        )
        return None

    cache_dir = Path(
        os.environ.get("ATHENAEUM_CACHE_DIR") or (Path.home() / ".cache" / "athenaeum")
    )
    output_path = resolve_cluster_output_path(knowledge_root, config=resolved_config)

    # Issue #370 PR2: delta-scoped cluster pass. Only reachable when a caller
    # threads ``changed_paths`` (ingest / session_end); the nightly ``run``
    # never does, so it always takes the whole-corpus path below. An EMPTY set
    # is a valid delta ("no auto-memory changed this run") — distinct from
    # ``None`` (whole-corpus) — and resolves to a no-op merge that rewrites
    # nothing, so an entity-only ingest never churns the auto-memory wiki.
    if changed_paths is not None:
        affected_ids = _delta_cluster_pass(
            auto_memory_files,
            changed_paths,
            output_path,
            extra_roots=extra_roots,
            cache_dir=cache_dir,
            threshold=threshold,
            knowledge_root=knowledge_root,
            resolved_config=resolved_config,
        )
        if affected_ids is not None:
            return affected_ids
        # else: a fallback trigger fired (already logged) — fall through to the
        # whole-corpus compile below, which is always correct.

    clusters = cluster_auto_memory_files(
        auto_memory_files,
        extra_roots=extra_roots,
        cache_dir=cache_dir,
        threshold=threshold,
    )

    log.info(
        "cluster pass: %d auto-memory file(s) → %d cluster(s) at cos>=%.2f",
        len(auto_memory_files),
        len(clusters),
        threshold,
    )

    _write_cluster_report_and_prune(
        clusters, output_path, knowledge_root, resolved_config
    )
    return None


def _delta_cluster_pass(
    auto_memory_files: list[AutoMemoryFile],
    changed_paths: set[Path],
    output_path: Path,
    *,
    extra_roots: list[Path],
    cache_dir: Path,
    threshold: float,
    knowledge_root: Path,
    resolved_config: dict[str, object] | None,
) -> set[str] | None:
    """Delta-scoped cluster pass (issue #370 PR2). ``None`` = fall back to full.

    Reads the prior cluster report, computes the affected scope, re-clusters
    only the affected pool, splices the result back into the report, and returns
    the NEW cluster ids for the merge pass to recompile. Any fallback trigger
    (D1-D3/D2) returns ``None`` after logging its reason.
    """
    prior_rows = read_cluster_rows(output_path)
    scope = compute_affected_clusters(
        changed_paths,
        prior_rows,
        auto_memory_files,
        extra_roots=extra_roots,
        cache_dir=cache_dir,
        threshold=threshold,
        max_affected_clusters=resolve_delta_max_affected_clusters(resolved_config),
        max_affected_members=resolve_delta_max_affected_members(resolved_config),
    )
    if scope is None:
        return None

    new_partial = cluster_auto_memory_files(
        scope.pool,
        extra_roots=extra_roots,
        cache_dir=cache_dir,
        threshold=threshold,
    )
    spliced = splice_cluster_report(prior_rows, scope.affected_ids, new_partial)
    log.info(
        "delta cluster pass: %d changed file(s), %d pooled member(s) → "
        "%d affected cluster(s) re-clustered; %d total cluster(s) in report",
        len(changed_paths),
        len(scope.pool),
        len(new_partial),
        len(spliced),
    )
    _write_cluster_report_and_prune(
        spliced, output_path, knowledge_root, resolved_config
    )
    return {c.cluster_id for c in new_partial}


def _delta_slug_collision(
    knowledge_root: Path,
    config: dict[str, object] | None,
    affected_ids: set[str],
) -> bool:
    """F6 guard: does any affected cluster's slug collide run-globally?

    A full merge resolves topic-slug collisions run-globally — the first row (in
    report order) with a given base slug keeps it, later ones are suffixed. A
    delta merge only writes the affected subset, so if an affected entry's
    derived base slug also belongs to ANOTHER corpus cluster (affected or not),
    a subset merge could assign a different final slug than the whole-corpus
    merge would. Detecting that here lets :func:`run` fall back to a full
    whole-corpus compile (always correct) rather than risk a divergent slug.
    ``derive_topic_slug`` is pure over ``member_paths`` + ``cluster_id``, so this
    reads the (already spliced) report and needs no re-clustering.
    """
    output_path = resolve_cluster_output_path(knowledge_root, config=config)
    rows = read_cluster_rows(output_path)
    slug_to_ids: dict[str, set[str]] = {}
    for row in rows:
        cid = str(row.get("cluster_id", ""))
        member_paths = [str(m) for m in row.get("member_paths", [])]
        slug = derive_topic_slug(member_paths, cid)
        slug_to_ids.setdefault(slug, set()).add(cid)
    for ids in slug_to_ids.values():
        if len(ids) > 1 and ids & affected_ids:
            return True
    return False


def _compile_auto_memory(
    auto_memory_files: list[AutoMemoryFile],
    knowledge_root: Path,
    *,
    config: dict[str, object] | None,
    dry_run: bool,
    client: Any,
    usage: TokenUsage | None,
    changed_paths: set[Path] | None,
) -> list:
    """Cluster (C2) + merge (C3/C4) the auto-memory corpus. Returns the entries.

    Issue #370 PR2: this is the single choke point for the delta-scoped compile,
    extracted from :func:`run` so the equivalence test can drive the EXACT
    orchestration on the deterministic ``client=None`` path (run's own
    pre-flight refuses a keyless ``api``-provider full pipeline, so the test
    cannot reach this logic through run()).

    Delta is enabled ONLY on the deterministic ``client is None`` path
    (session_end / ingest tier0, no LLM). The nightly LLM run — any live client —
    MUST stay whole-corpus (fallback trigger D5). This gates on ``client is
    None`` rather than the cross-scope mode because the PRIMARY per-cluster
    contradiction detector (``detect_contradictions`` inside the merge loop) runs
    for EVERY mode, including ``cross_scope_mode == 'off'`` — only the extra
    cross-scope similarity sweep is mode-gated. Scoping the merge to the affected
    clusters would therefore make a live-client run's escalation sidecars
    (``_pending_questions.md`` / ``_pending_merges.md`` / the resolved cache)
    diverge from a full compile. All new params default to the whole-corpus
    behaviour, so a call with ``changed_paths=None`` is byte-identical to the
    pre-#370 pipeline.
    """
    delta_enabled = resolve_delta_enabled(config)
    delta_eligible = (
        not dry_run and changed_paths is not None and delta_enabled and client is None
    )
    if changed_paths is not None and not delta_eligible and not dry_run:
        if client is not None:
            log.warning(
                "delta: live LLM client — whole-corpus compile so contradiction "
                "escalations stay corpus-consistent (D5)"
            )
        elif not delta_enabled:
            log.info(
                "delta: disabled via librarian.delta.enabled — whole-corpus compile"
            )

    only_cluster_ids = _run_cluster_pass(
        auto_memory_files,
        knowledge_root,
        config=config,
        dry_run=dry_run,
        changed_paths=changed_paths if delta_eligible else None,
    )

    # F6: run-global slug-collision guard. If any affected entry's slug would
    # collide with another corpus entry, a subset merge could assign a different
    # final slug than the whole-corpus merge — fall back to a full compile, which
    # resolves the collision deterministically.
    if only_cluster_ids is not None and _delta_slug_collision(
        knowledge_root, config, only_cluster_ids
    ):
        log.warning(
            "delta: affected slug collides run-globally (F6) — re-running "
            "whole-corpus cluster + merge"
        )
        _run_cluster_pass(
            auto_memory_files, knowledge_root, config=config, dry_run=dry_run
        )
        only_cluster_ids = None

    # C3: merge clusters into canonical wiki/auto-*.md entries. C4 contradiction
    # detection runs inside merge_clusters_to_wiki and reuses the shared client.
    # When ``only_cluster_ids`` is set (delta path), only the affected entries
    # are merged + written; every unaffected wiki page is left untouched.
    return merge_clusters_to_wiki(
        knowledge_root,
        auto_memory_files=auto_memory_files,
        config=config,
        dry_run=dry_run,
        client=client,
        usage=usage,
        only_cluster_ids=only_cluster_ids,
    )


def _run_retire(
    merged_entries: list,
    knowledge_root: Path,
    *,
    config: dict[str, object] | None,
    dry_run: bool,
    projects_root: Path | None,
):
    """Run the move-then-retire pass (issue #261) over the merged entries.

    Thin wrapper around :func:`athenaeum.retire.run_retire_pass` so the run
    loop stays readable. Lazy-imports ``retire`` to avoid a hard import cycle
    (retire imports merge, not librarian). A retire hiccup must never abort the
    nightly compile — the held raw simply stays in the queue for the next run —
    so the exception is caught, but it is logged at ERROR with the traceback
    (Quine C1) so a persistently-failing retire is visible to monitoring rather
    than buried in a WARNING. Returns the :class:`RetireReport` on success, or
    ``None`` when the pass raised.
    """
    from athenaeum.retire import run_retire_pass

    try:
        return run_retire_pass(
            merged_entries,
            knowledge_root,
            config=config,
            dry_run=dry_run,
            projects_root=projects_root,
        )
    except Exception:
        log.exception(
            "retire pass failed; leaving raw intake in place (nothing retired)"
        )
        return None


def _run_reresolve_pass(
    knowledge_root: Path,
    *,
    config: dict[str, object] | None,
    client: anthropic.Anthropic | None,
    usage: TokenUsage | None = None,
) -> int:
    """Re-resolve open, proposal-less pending questions (issue #188).

    Thin wrapper around :func:`athenaeum.tiers.reresolve_open_questions` so the
    nightly librarian self-heals transient cap-hit / offline escalations on a
    later, budgeted run. No-op (returns 0) when the pending file is absent or
    when ``client`` is ``None`` (offline → leave blocks raw, re-resolvable).
    Failures are swallowed: a re-resolve hiccup must never block the run.
    """
    from athenaeum.tiers import reresolve_open_questions

    pending_path = knowledge_root / "wiki" / "_pending_questions.md"
    if not pending_path.exists():
        return 0
    try:
        return reresolve_open_questions(
            pending_path, client=client, config=config, usage=usage
        )
    except Exception as exc:  # noqa: BLE001 — heal pass must not fail the run
        log.warning("reresolve pass failed (%s); leaving questions untouched", exc)
        return 0


def librarian_max_api_calls(config: dict[str, object] | None = None) -> int:
    """Resolve the run-level API call cap from env > config > default.

    Issue #220. Environment override wins over the YAML setting so an
    operator can bump the cap on a single run without editing config.
    Negative or non-numeric values fall back to
    :data:`DEFAULT_MAX_API_CALLS`. Mirrors
    :func:`athenaeum.resolutions.resolve_max_per_run`.
    """
    env = os.environ.get("ATHENAEUM_MAX_API_CALLS")
    if env is not None:
        try:
            value = int(env)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("max_api_calls")
            # bool is an int subclass — `max_api_calls: yes` in yaml must
            # not silently become a cap of 1.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return raw
    return DEFAULT_MAX_API_CALLS


def librarian_max_files(config: dict[str, object] | None = None) -> int:
    """Resolve the per-run intake batch size from env > config > default.

    Issue #232. Mirrors :func:`librarian_max_api_calls` (#220): the
    environment override wins over the YAML setting so a cron deployment
    can tune the window on a single run without editing config or the
    crontab command line. Negative or non-numeric values fall back to
    :data:`DEFAULT_MAX_FILES`.
    """
    env = os.environ.get("ATHENAEUM_MAX_FILES")
    if env is not None:
        try:
            value = int(env)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("max_files")
            # bool is an int subclass — `max_files: yes` in yaml must
            # not silently become a window of 1.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return raw
    return DEFAULT_MAX_FILES


def librarian_batch_mode(config: dict[str, object] | None = None) -> bool:
    """Resolve the Batch API opt-in from env > config > default off.

    Issue #236. Mirrors :func:`librarian_max_files` (#232): the
    ``ATHENAEUM_BATCH_MODE`` env var wins over the yaml
    ``librarian.batch_mode`` key so a cron deployment can flip the mode on
    a single run; the CLI ``--batch-mode`` flag (resolved by the caller)
    wins over both. Unrecognized env values fall through to the yaml key;
    non-bool yaml values fall through to the default (off).
    """
    env = os.environ.get("ATHENAEUM_BATCH_MODE")
    if env is not None:
        normalized = env.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("batch_mode")
            if isinstance(raw, bool):
                return raw
    return False


def _clear_stale_deferred_manifest(wiki_root: Path) -> None:
    """Remove a stale deferred-work manifest left by a budget-tripped run.

    Every clean (non-dry-run) exit path must call this — the full entity
    run, the empty-intake early return, and the merge-only / cluster-only
    early returns — so a stale manifest cannot outlive the backlog it
    described.
    """
    stale = wiki_root / DEFERRED_MANIFEST_NAME
    if stale.exists():
        stale.unlink()


def _write_deferred_manifest(
    wiki_root: Path,
    deferred_refs: list[str],
    *,
    api_calls: int,
    budget: int,
    beyond_window: int = 0,
    failed_refs: list[str] | None = None,
) -> Path:
    """Write the deferred-work manifest after a budget-tripped run (#220).

    Lists the raw files the run did NOT process so an operator (or the next
    run's health reporting) can see what was silently deferred. The deferred
    files stay on disk and are picked up automatically by the next run; this
    manifest is informational. Overwritten on every tripped run; the next
    clean run removes it.

    ``deferred_count`` is the TRUE backlog: the in-window refs listed below
    plus ``beyond_window`` files that discovery found but the ``max_files``
    window excluded from this run entirely (counted, not listed).
    ``failed_refs`` are files that errored this run (transient API overload
    or processing exception); they also stay on disk and are retried next
    run, but they are not "deferred by budget" so they get their own section.
    """
    path = wiki_root / DEFERRED_MANIFEST_NAME
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_deferred = len(deferred_refs) + beyond_window
    lines = [
        "# Deferred work — librarian run budget exhausted",
        "",
        "The last librarian run stopped early because the run-level API call",
        "budget was exhausted. The raw files below were NOT processed this",
        "run; they remain on disk and the next run picks them up",
        "automatically. This file is overwritten on every budget-tripped run",
        "and removed by the next clean run.",
        "",
        f"- run: {now}",
        f"- api_calls_used: {api_calls}",
        f"- api_call_budget: {budget}",
        f"- deferred_count: {total_deferred}",
    ]
    if beyond_window:
        lines += [
            f"- deferred_in_window: {len(deferred_refs)}",
            f"- deferred_beyond_window: {beyond_window}",
        ]
    lines += [
        "",
        "## Deferred raw files",
        "",
        *[f"- {ref}" for ref in deferred_refs],
    ]
    if beyond_window:
        lines.append(
            f"- plus {beyond_window} more beyond the max_files window "
            "(discovered but not listed; next runs pick them up)"
        )
    lines.append("")
    if failed_refs:
        lines += [
            "## Failed this run (retried next run)",
            "",
            *[f"- {ref}" for ref in failed_refs],
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(
    raw_root: Path = DEFAULT_RAW_ROOT,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    dry_run: bool = False,
    max_files: int | None = None,
    max_api_calls: int | None = None,
    cluster_only: bool = False,
    merge_only: bool = False,
    strict_budget: bool = False,
    batch_mode: bool | None = None,
    retire: bool | None = None,
    push_after_run: bool | None = None,
    projects_root: Path | None = None,
    install_signal_handlers: bool = False,
    changed_paths: set[Path] | None = None,
) -> int:
    """Run the librarian pipeline. Returns 0 on success, 1 on error.

    When ``cluster_only`` is True, only the C2 auto-memory discovery +
    clustering pass runs; the entity tier pipeline is skipped entirely.
    This is the clustering-focused entrypoint for operators validating
    the C2 output before shipping C3.

    When ``merge_only`` is True, only the C3 merge pass runs: it reads
    the canonical cluster JSONL from a previous C2 run and writes
    ``wiki/auto-<topic-slug>.md`` entries. Neither discovery, clustering,
    nor the entity tier pipeline runs. Useful for iterating on the merge
    output without re-embedding or re-clustering.

    ``max_api_calls`` is the run-level API call budget (issue #220). When
    ``None`` (the default) it resolves via env ``ATHENAEUM_MAX_API_CALLS`` >
    yaml ``librarian.max_api_calls`` > :data:`DEFAULT_MAX_API_CALLS`. An
    explicit value (e.g. from the CLI flag) wins over all three.

    ``strict_budget`` (issue #227) makes a budget-tripped (DEGRADED) run
    return 1 instead of the default 0, for exit-code-based alerting (e.g.
    the CLI ``--strict-budget`` flag). All other DEGRADED-path behavior —
    warning summary, deferred-work manifest, git snapshot — is unchanged.

    ``batch_mode`` (issue #236) routes the entity-tier LLM calls through
    the Anthropic Messages Batch API (50% token discount, latency-tolerant)
    instead of the synchronous per-file loop. When ``None`` (the default)
    it resolves via env ``ATHENAEUM_BATCH_MODE`` > yaml
    ``librarian.batch_mode`` > off; an explicit value (e.g. from the CLI
    ``--batch-mode`` flag) wins over both. Off keeps the synchronous path
    untouched; dry-run always uses the synchronous (call-free) path. See
    :mod:`athenaeum.batch` for phase layout and budget semantics.

    ``retire`` (issue #261) opts out of the move-then-retire pass. DEFAULT
    ON (owner-confirmed): when ``None`` it resolves via yaml
    ``librarian.retire`` (default on); an explicit ``False`` (e.g. from the
    CLI ``--no-retire`` flag) wins. When off, the retire pass is skipped
    entirely — non-contradictory raw auto-memory is neither moved into the
    wiki nor ``git rm``'d, so the raw stays in the intake queue.

    ``push_after_run`` (issue #284) opts INTO a post-run ``git push`` that
    closes the move-then-retire recovery gap on multi-machine setups. DEFAULT
    OFF: when ``None`` it resolves via yaml ``librarian.push_after_run``
    (default off); an explicit ``True`` (e.g. from the CLI ``--push`` flag)
    wins. When on AND the run produced at least one new commit AND it is not
    a ``--dry-run``, the librarian invokes ``git push`` (remote/branch from
    ``librarian.push_remote`` / ``librarian.push_branch``, defaulting to
    ``origin`` and the current branch's upstream). A push failure is logged
    as a non-fatal warning — commits remain locally and the next run retries
    (``git push`` is idempotent). Athenaeum performs no credential handling;
    the operator's ambient git auth (credential helper / SSH) is used.
    """
    skip_entity_tiers = cluster_only or merge_only
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    config = load_config(knowledge_root)

    # Issue #330: resolve the active LLM provider (env ATHENAEUM_LLM_PROVIDER >
    # yaml llm.provider > api). A misconfigured value raises — surface it as a
    # clean run failure rather than a traceback.
    try:
        provider = resolve_provider(config)
    except ProviderConfigError as exc:
        log.error("%s", exc)
        return 1

    # Issue #330: fail loudly at startup if the claude-cli binary is missing,
    # instead of silently deferring every file to an rc-0 no-op run.
    preflight_err = preflight_provider(provider)
    if preflight_err:
        log.error("%s", preflight_err)
        return 1

    # The ANTHROPIC_API_KEY requirement applies ONLY to the ``api`` backend.
    # The ``claude-cli`` backend authenticates via the operator's ambient
    # Claude Code subscription login and needs no key (issue #330).
    if provider == "api" and not api_key and not dry_run and not skip_entity_tiers:
        log.error("ANTHROPIC_API_KEY not set (required unless dry_run=True)")
        return 1

    if not wiki_root.exists() and not skip_entity_tiers:
        log.error("Wiki root does not exist: %s", wiki_root)
        return 1

    if not dry_run and not skip_entity_tiers and not (knowledge_root / ".git").exists():
        log.error(
            "No .git in %s — refusing to run without a writable git repo. "
            "The librarian's pre-processing snapshot is load-bearing for raw-file "
            "recovery. Either point knowledge_root at a real git repo, or pass "
            "dry_run=True to inspect without writing.",
            knowledge_root,
        )
        return 1

    # Issue #220: resolve the run-level API call budget (explicit arg >
    # env > yaml > default).
    if max_api_calls is None:
        max_api_calls = librarian_max_api_calls(config)

    # Issue #232: resolve the per-run intake batch size the same way
    # (explicit arg > env > yaml > default).
    if max_files is None:
        max_files = librarian_max_files(config)

    # Issue #236: resolve the Batch API opt-in the same way (explicit arg >
    # env > yaml > default off).
    if batch_mode is None:
        batch_mode = librarian_batch_mode(config)

    # Issue #330: batch mode is API-only — the Messages Batch API is an
    # Anthropic-endpoint feature with no ``claude`` CLI equivalent. Reject the
    # combination LOUDLY at startup rather than silently falling back to the
    # api backend or silently dropping the batch request.
    if batch_mode and provider == "claude-cli":
        log.error(
            "batch mode (ATHENAEUM_BATCH_MODE / librarian.batch_mode / "
            "--batch-mode) is incompatible with the claude-cli provider: the "
            "Messages Batch API is Anthropic-endpoint-only. Use provider=api "
            "for batch runs, or disable batch mode for the subscription backend."
        )
        return 1

    # Issue #261/#259: resolve the move-then-retire opt-out (explicit arg >
    # yaml `librarian.retire` > default ON). When off, the retire pass is
    # skipped at both call sites below; the destructive `git rm` of raw
    # auto-memory never runs.
    if retire is None:
        retire = resolve_retire(config)
    if not retire:
        log.info(
            "retire pass disabled (librarian.retire / --no-retire) — raw "
            "auto-memory will not be moved or git-removed this run"
        )

    # Issue #284: resolve the post-run push opt-in (explicit arg >
    # yaml `librarian.push_after_run` > default OFF). Default off so a
    # fresh install never side-effects an operator's git remote. The
    # actual push fires after the final commit, only when the run
    # produced at least one new commit and is not a dry-run.
    if push_after_run is None:
        push_after_run = resolve_push_after_run(config)

    # Issue #235: a resolved budget of 0 is a valid defer-everything cap
    # (env/yaml zero — the CLI flag rejects it), but it is also the most
    # likely accidental misconfiguration: every LLM tier is skipped and the
    # whole intake is deferred. Flag it loudly at run start so an
    # unintended 0 is diagnosable immediately, not from the DEGRADED
    # summary at the end of the run.
    if max_api_calls == 0:
        log.warning(
            "API budget is 0 — all LLM tiers deferred this run; set "
            "ATHENAEUM_MAX_API_CALLS / librarian.max_api_calls to a "
            "positive value if unintended"
        )

    # Issue #284: capture HEAD at run-start (before ANY commit site fires)
    # so the post-run push can detect whether the run produced any commit
    # across librarian.git_snapshot, retire._commit_paths_if_staged, and
    # the merge-only / cluster-only early-return paths. Per-call-site
    # tracking would miss the commits inside the retire pass.
    head_at_start = _capture_head(knowledge_root) if not dry_run else None

    # One run-level TokenUsage threaded through every phase (cluster, merge
    # incl. the C4 detector + resolver, #188 reresolve, entity tiers) so
    # ``max_api_calls`` is a genuine run-level ceiling. Earlier phases
    # increment the counter; the entity-tier loop below is the enforcement
    # point that defers remaining intake when the budget is spent.
    usage = TokenUsage()
    if provider == "claude-cli":
        # Subscription pays for the tokens (issue #330): counts still
        # accumulate and appear in the run summary, but estimated_cost_usd
        # reports $0 instead of pricing them at API list rates.
        usage.subscription_covered = True

    # Build the shared LLM client early (issue #330 provider seam) so both the
    # entity tiers and the C4 contradiction detector can share it. ``None`` for
    # the api backend when the key is unset (detector degrades deterministically);
    # for claude-cli it is the subscription CLI adapter. ``max_retries=3``
    # preserves the pre-#330 api-backend construction byte-for-byte.
    merge_client = build_llm_client(config, api_key=api_key, max_retries=3)

    # Issue #290: wiki-page dedup pass. Clusters compiled wiki/*.md
    # concept/reference/principle pages against EACH OTHER (not against
    # raw/auto-memory intake) and proposes merges via the shared
    # wiki/_pending_merges.md sidecar. Independent of the C1-C4 auto-memory
    # pipeline below, so it runs on every mode (full run, --cluster-only,
    # --merge-only) whenever wiki/ exists — same cadence as the rest of
    # the scheduled librarian pipeline. A failure here is logged and
    # swallowed rather than aborting the run: this pass is diagnostic
    # (it only appends human-reviewed proposals), not load-bearing for
    # the rest of the pipeline.
    if wiki_root.is_dir():
        try:
            from athenaeum.wiki_dedupe import propose_wiki_page_merges

            propose_wiki_page_merges(knowledge_root, config=config, dry_run=dry_run)
        except Exception:
            log.exception("wiki-page dedup pass failed; continuing run")

    if merge_only:
        # Merge-only path skips discovery + clustering entirely; it reads
        # the canonical cluster JSONL written by a prior C2 run and
        # compiles ``wiki/auto-*.md`` entries from it. Discovery still
        # happens inside merge_clusters_to_wiki() for source propagation.
        merged_entries = merge_clusters_to_wiki(
            knowledge_root,
            config=config,
            dry_run=dry_run,
            client=merge_client,
            usage=usage,
        )
        # Issue #261 (slice B of #259): move-then-retire. Non-contradictory
        # raw is moved into its wiki entry (origin-traced footnote) and git
        # rm'd; contradictory raw is held in the queue. No-op without .git.
        # Skipped entirely when retire is disabled (#259 opt-out).
        if retire:
            _run_retire(
                merged_entries,
                knowledge_root,
                config=config,
                dry_run=dry_run,
                projects_root=projects_root,
            )
        # Issue #188: self-heal proposal-less open questions (a prior
        # budget-exhausted / offline run leaves raw blocks; re-resolve them
        # now that this run has budget). No-op on dry-run / offline.
        if not dry_run:
            _run_reresolve_pass(
                knowledge_root, config=config, client=merge_client, usage=usage
            )
            # A merge-only run is a clean run from the manifest's
            # perspective: clear a stale deferred-work manifest left by a
            # prior budget-tripped run (v0.7.3 release-gate review).
            _clear_stale_deferred_manifest(wiki_root)
        _maybe_push_after_run(
            knowledge_root,
            config=config,
            push_after_run=push_after_run,
            dry_run=dry_run,
            head_at_start=head_at_start,
        )
        return 0

    # C1 + C2: auto-memory discovery followed by the C2 cluster pass.
    # Clustering must run BEFORE any tier routing so that downstream C3
    # merge has a fresh grouping to consume. Scope identity is preserved
    # on each record so the tier pipeline and the cluster pass both see
    # the same routing key.
    auto_memory_files = discover_auto_memory_files(knowledge_root, config=config)
    if auto_memory_files:
        by_scope: dict[str, int] = {}
        for am in auto_memory_files:
            by_scope[am.origin_scope] = by_scope.get(am.origin_scope, 0) + 1
        log.info(
            "Discovered %d auto-memory file(s) across %d scope(s)",
            len(auto_memory_files),
            len(by_scope),
        )
        if dry_run:
            for scope, count in sorted(by_scope.items()):
                log.info("  [DRY RUN] auto-memory scope %s: %d file(s)", scope, count)

        # C2 + C3 + C4: cluster, merge, and detect. Issue #370 PR2 threads the
        # optional ``changed_paths`` delta through this one call — see
        # :func:`_compile_auto_memory` for the delta-eligibility (D5) gate, the
        # cluster pass, the F6 slug-collision guard, and the merge.
        merged_entries = _compile_auto_memory(
            auto_memory_files,
            knowledge_root,
            config=config,
            dry_run=dry_run,
            client=merge_client,
            usage=usage,
            changed_paths=changed_paths,
        )

        # Issue #261 (slice B of #259): move-then-retire lifecycle. Runs after
        # merge + C4 detection. Non-contradictory raw is moved into its wiki
        # entry (origin-traced footnote) and git rm'd; contradictory raw is
        # held for human confirmation. Skipped for the cluster_only diagnostic
        # mode, when retire is disabled (#259 opt-out), and a no-op without a
        # git repo.
        if retire and not cluster_only:
            _run_retire(
                merged_entries,
                knowledge_root,
                config=config,
                dry_run=dry_run,
                projects_root=projects_root,
            )

        # Issue #188: re-resolve open, proposal-less pending questions so a
        # prior cap-hit / offline escalation self-heals on this (budgeted) run.
        if not dry_run:
            _run_reresolve_pass(
                knowledge_root, config=config, client=merge_client, usage=usage
            )

    if cluster_only:
        # Same contract as the merge-only early return above: a clean
        # cluster-only run must not preserve a stale deferred manifest.
        if not dry_run:
            _clear_stale_deferred_manifest(wiki_root)
        _maybe_push_after_run(
            knowledge_root,
            config=config,
            push_after_run=push_after_run,
            dry_run=dry_run,
            head_at_start=head_at_start,
        )
        return 0

    raw_files = discover_raw_files(raw_root)
    if not raw_files:
        # An empty intake is a clean run: clear any stale deferred-work
        # manifest left by a previous budget-tripped run. Without this the
        # early return below would preserve the stale manifest forever once
        # the backlog drains without new intake.
        if not dry_run:
            _clear_stale_deferred_manifest(wiki_root)
        log.info("No raw files to process. Nothing to do.")
        _maybe_push_after_run(
            knowledge_root,
            config=config,
            push_after_run=push_after_run,
            dry_run=dry_run,
            head_at_start=head_at_start,
        )
        return 0

    total_intake = len(raw_files)
    log.info("Found %d raw file(s) to process", total_intake)

    if total_intake > max_files:
        log.info(
            "Budget cap: processing %d of %d files this run",
            max_files,
            total_intake,
        )
        raw_files = raw_files[:max_files]
    # Files discovery found but the max_files window excluded from this run
    # entirely. Counted into the deferred manifest on a budget trip so the
    # manifest reports the TRUE backlog, not just the in-window remainder.
    beyond_window = total_intake - len(raw_files)

    schema_path = wiki_root / "_schema"
    valid_types = load_schema_list(schema_path, "types.md") or FALLBACK_TYPES
    valid_tags = load_schema_list(schema_path, "tags.md") or FALLBACK_TAGS
    valid_access = load_schema_list(schema_path, "access-levels.md") or FALLBACK_ACCESS

    index = EntityIndex(wiki_root)
    log.info("Loaded %d wiki entries into index", len(index))

    client = merge_client  # shared with C4 contradiction detector

    if not dry_run:
        git_snapshot(knowledge_root, "librarian: pre-processing snapshot")

    total_created = 0
    total_updated = 0
    total_escalated = 0
    total_skipped = 0
    failed_files: list[str] = []
    deferred_refs: list[str] = []
    processed_count = 0

    # Issue #337: a wall-clock timeout (the pre-dawn sweep's `timeout`, which
    # SIGTERMs then, after a grace, KILLs) would otherwise kill the run
    # between the pre-processing snapshot above and the terminal
    # `processed N file(s)` commit below, stranding every wiki page written
    # so far as an uncommitted tree for the NEXT run's `git add -A` snapshot
    # to absorb under a misleading "pre-processing snapshot" message. Install
    # a SIGTERM/SIGINT handler for the writing phase that commits the partial
    # progress with a distinct, greppable message and exits 124 (matching
    # coreutils `timeout`). A normally-completing run restores the handlers
    # right after the terminal commit and commits exactly once, unchanged.
    # Opt-in (CLI-only via `install_signal_handlers`) so in-process callers
    # (the MCP server, tests) never have their signal handling hijacked.
    _prev_handlers: list[tuple[int, Any]] = []

    def _commit_partial_and_exit(signum: int, _frame: Any) -> None:
        log.warning(
            "librarian: interrupted by signal %d after %d file(s) — "
            "committing partial progress (issue #337)",
            signum,
            processed_count,
        )
        # Restore first so a second signal during the commit can't recurse
        # into this handler.
        for _s, _prev in _prev_handlers:
            signal.signal(_s, _prev)
        git_snapshot(
            knowledge_root,
            f"librarian: partial run (interrupted after {processed_count} "
            f"file(s), {total_created}C {total_updated}U {total_escalated}E "
            f"{len(failed_files)}F)",
        )
        sys.exit(124)

    if install_signal_handlers and not dry_run:
        try:
            for _s in (signal.SIGTERM, signal.SIGINT):
                _prev_handlers.append((_s, signal.signal(_s, _commit_partial_and_exit)))
        except ValueError:
            # Not the main thread (e.g. an in-process caller) — signal
            # handlers can't be installed here. Skip the guard rather than
            # fail an otherwise-valid run.
            log.debug("librarian: interrupt-commit guard skipped (not main thread)")
            _prev_handlers = []

    # Issue #337: the interrupt handler installed above stays active through
    # the terminal commit; the `finally` restores it on EVERY exit path
    # (normal, interrupt, or an exception from `rebuild_index` / the terminal
    # `git_snapshot`), so it can never outlive the run for an in-process
    # caller. A no-op when no handler was installed (dry-run / not opt-in /
    # not the main thread).
    try:
        if batch_mode and dry_run:
            log.info(
                "Batch mode requested but --dry-run makes no API calls — "
                "using the synchronous dry-run path"
            )

        if batch_mode and not dry_run and client is not None:
            # Issue #236: phased fan-out via the Messages Batch API. The
            # synchronous loop below is untouched when the flag is off.
            # Issue #337 note: `processed_count` is incremented only by the
            # synchronous loop, so an interrupt during a BATCH run reports
            # "0 file(s)" in the partial-commit message even though any pages
            # already written are still committed by the handler's
            # `git_snapshot` (git add -A) — the tree stays clean. Accurate
            # batch-interrupt accounting is #236-adjacent and out of scope
            # for #337 (batch mode is API-only and off for the nightly run).
            from athenaeum.batch import process_batch_run

            log.info("Batch mode: tier-2/tier-3 calls via the Messages Batch API")
            outcome = process_batch_run(
                raw_files,
                index,
                wiki_root,
                client,
                valid_types,
                valid_tags,
                valid_access,
                usage=usage,
                config=config,
                max_api_calls=max_api_calls,
            )
            total_created = outcome.created
            total_updated = outcome.updated
            total_escalated = outcome.escalated
            total_skipped = outcome.skipped
            failed_files = outcome.failed_refs
            deferred_refs = outcome.deferred_refs
        else:
            for i, raw in enumerate(raw_files):
                if not dry_run and usage.api_calls >= max_api_calls:
                    log.warning(
                        "API call budget exhausted (%d/%d) — stopping early",
                        usage.api_calls,
                        max_api_calls,
                    )
                    # Issue #220: everything from here on is deferred to the
                    # next run — record it so the manifest + summary surface it.
                    deferred_refs = [r.ref for r in raw_files[i:]]
                    break

                log.info("Processing: %s", raw.ref)
                try:
                    result = process_one(
                        raw,
                        index,
                        wiki_root,
                        client,
                        valid_types,
                        valid_tags,
                        valid_access,
                        dry_run=dry_run,
                        usage=usage,
                        config=config,
                    )
                except TransientAPIError as exc:
                    # Issue #193: the Anthropic API was overloaded (429/529)
                    # and the bounded retry was exhausted. Defer to the next
                    # run exactly like a malformed-file failure, but log it
                    # distinctly so health reporting can tell "API was
                    # overloaded" (transient) apart from "this file is broken".
                    log.error(
                        "Gave up after %d retries (transient API overload) %s: %s",
                        exc.attempts,
                        raw.ref,
                        type(exc.last_error).__name__,
                    )
                    failed_files.append(raw.ref)
                    continue
                except Exception:
                    log.exception("Failed to process %s", raw.ref)
                    failed_files.append(raw.ref)
                    continue

                total_created += len(result.created)
                total_updated += len(result.updated)
                total_escalated += len(result.escalated)
                total_skipped += len(result.skipped)

                if not dry_run:
                    raw.path.unlink()
                    log.info("  Deleted: %s", raw.path)
                    processed_count += 1

        # Issue #220: a budget-tripped run must be visibly DEGRADED, not
        # "Done". Exit code stays 0 (not a crash — the deferred files are
        # picked up by the next run), but the summary line is machine-
        # greppable and a manifest records exactly what was deferred. A clean
        # run clears any stale manifest left by a previous tripped run.
        if deferred_refs:
            manifest_path = _write_deferred_manifest(
                wiki_root,
                deferred_refs,
                api_calls=usage.api_calls,
                budget=max_api_calls,
                beyond_window=beyond_window,
                failed_refs=failed_files,
            )
            log.warning(
                "Done (DEGRADED — budget exhausted): %d created, %d updated, "
                "%d escalated, %d skipped, %d failed, %d deferred (manifest: %s)",
                total_created,
                total_updated,
                total_escalated,
                total_skipped,
                len(failed_files),
                len(deferred_refs) + beyond_window,
                manifest_path,
            )
        else:
            if not dry_run:
                _clear_stale_deferred_manifest(wiki_root)
            log.info(
                "Done: %d created, %d updated, %d escalated, %d skipped, %d failed",
                total_created,
                total_updated,
                total_escalated,
                total_skipped,
                len(failed_files),
            )
        if usage.api_calls > 0:
            log.info(
                "Token usage: %d API calls, %d input + %d output = %d total"
                " (cache: %d written, %d read) (~$%.4f estimated)",
                usage.api_calls,
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.cache_creation_input_tokens,
                usage.cache_read_input_tokens,
                usage.estimated_cost_usd,
            )

        if not dry_run and (total_created > 0 or total_updated > 0):
            rebuild_index(wiki_root)

        if not dry_run:
            msg = (
                f"librarian: processed {len(raw_files) - len(deferred_refs)} file(s) "
                f"({total_created}C {total_updated}U {total_escalated}E {len(failed_files)}F)"
            )
            git_snapshot(knowledge_root, msg)
    finally:
        for _s, _prev in _prev_handlers:
            signal.signal(_s, _prev)
        _prev_handlers = []

    _maybe_push_after_run(
        knowledge_root,
        config=config,
        push_after_run=push_after_run,
        dry_run=dry_run,
        head_at_start=head_at_start,
    )

    # Issue #310: warn-only page-size guardrail. Log a WARNING for each wiki
    # entity page over the flag threshold so a nightly run surfaces pages that
    # want splitting into linked sub-entities. Never fatal, never mutating —
    # any failure here degrades to a single non-fatal note. The split-proposal
    # workflow is explicitly out of scope (issue #310, moscow:could).
    try:
        from athenaeum.config import resolve_page_flag_bytes, resolve_page_warn_bytes
        from athenaeum.status import scan_page_sizes

        _pw_bytes = resolve_page_warn_bytes(config)
        _pf_bytes = resolve_page_flag_bytes(config)
        _, _pages_flag = scan_page_sizes(wiki_root, _pw_bytes, _pf_bytes)
        for _name, _size in _pages_flag:
            log.warning(
                "oversized wiki page %s (%d bytes > flag %d): consider "
                "splitting into linked sub-entities",
                _name,
                _size,
                _pf_bytes,
            )
    except Exception as exc:  # noqa: BLE001 — guardrail must never break a run
        log.warning("page-size guardrail check failed (non-fatal): %s", exc)

    if failed_files:
        log.warning("Failed files (will retry next run): %s", ", ".join(failed_files))
        return 1

    # Issue #227: opt-in strict mode for exit-code-based alerting. The
    # default stays 0 (a trip is not a crash — the next run picks the
    # deferred files up), but operators who alert on exit codes can ask
    # for a nonzero exit when the budget tripped.
    if deferred_refs and strict_budget:
        log.warning("strict_budget: budget-tripped run — exiting nonzero")
        return 1

    return 0


# ---------------------------------------------------------------------------
# On-demand ingest (issue #349) — manual/escape-hatch compile of new/changed
# raw intake, with a content-hash stamp manifest so an incremental run is a
# fast no-op when nothing has changed since the last successful ingest. The
# SessionEnd path (issue #350) reuses `ingest()` directly.
# ---------------------------------------------------------------------------

#: Stamp-manifest filename recording the raw-intake content hashes seen by the
#: last successful ingest. Lives in the cache dir alongside the #348 index
#: manifests (kept out of the knowledge git repo). Shape mirrors the search
#: manifests: ``{"version": 1, "hashes": {relpath: sha256}}``.
INGEST_MANIFEST_NAME = "ingest-manifest.json"


@dataclass
class IngestResult:
    """Summary of an :func:`ingest` invocation (issue #349).

    ``new_or_changed`` is the count of raw files added/changed versus the
    last ingest stamp (scoped to ``session`` when given). ``compiled`` is the
    number of raw files actually consumed (compiled into the wiki and removed
    from the intake queue) this run. ``noop`` is True when an incremental run
    found nothing new and skipped the compile entirely. ``exit_code``
    propagates the underlying compile's exit status.
    """

    mode: str
    new_or_changed: int
    compiled: int
    noop: bool
    exit_code: int
    duration_ms: int
    session: str | None = None

    def summary(self) -> dict[str, object]:
        """One-line JSON-serializable summary (counts + duration)."""
        data: dict[str, object] = {
            "command": "ingest",
            "mode": self.mode,
            "new_or_changed": self.new_or_changed,
            "compiled": self.compiled,
            "noop": self.noop,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
        }
        if self.session is not None:
            data["session"] = self.session
        return data


def _resolve_cache_dir(cache_dir: Path | None) -> Path:
    """Resolve the athenaeum cache dir (arg > env > ``~/.cache/athenaeum``)."""
    if cache_dir is not None:
        return Path(cache_dir).expanduser()
    return Path(
        os.environ.get("ATHENAEUM_CACHE_DIR") or (Path.home() / ".cache" / "athenaeum")
    ).expanduser()


def _raw_hash_snapshot(
    raw_root: Path,
    knowledge_root: Path,
    *,
    session: str | None = None,
    prior_stats: dict[str, tuple[int, int, str]] | None = None,
    out_stats: dict[str, tuple[int, int]] | None = None,
) -> dict[str, str]:
    """Map ``relpath -> sha256`` for every raw intake ``*.md`` under *raw_root*.

    Keys are POSIX paths relative to *knowledge_root* so the stamp manifest is
    stable regardless of the absolute checkout location. ``.gitkeep`` and
    per-scope ``MEMORY.md`` index files are skipped — they are not intake.
    When *session* is given, only files whose frontmatter ``originSessionId``
    matches are included (the per-session incremental gate #350 needs).
    Unreadable files are skipped.

    Issue #370 stat pre-filter: ``prior_stats`` maps ``relpath ->
    (mtime_ns, size, hash)`` from the last ingest manifest. When a file's
    ``(mtime_ns, size)`` matches AND no ``session`` filter is active, the stored
    hash is reused without reading the body. A ``session`` filter still reads
    every file (it must parse ``originSessionId``) but reuses those bytes for the
    hash. ``out_stats``, when provided, collects ``relpath -> (mtime_ns, size)``
    for every included file so the caller can persist it for the next run.
    """
    snapshot: dict[str, str] = {}
    if not raw_root.exists():
        return snapshot
    for fpath in sorted(raw_root.rglob("*.md")):
        if not fpath.is_file():
            continue
        if fpath.name == ".gitkeep" or fpath.name in _AUTO_MEMORY_SKIP_NAMES:
            continue
        try:
            st = fpath.stat()
        except OSError:
            continue
        mtime_ns, size = st.st_mtime_ns, st.st_size
        try:
            rel = fpath.relative_to(knowledge_root).as_posix()
        except ValueError:
            rel = str(fpath)
        prior = prior_stats.get(rel) if prior_stats else None
        if (
            session is None
            and prior is not None
            and prior[0] == mtime_ns
            and prior[1] == size
        ):
            # Content unchanged since last stamp — reuse the stored hash.
            snapshot[rel] = prior[2]
            if out_stats is not None:
                out_stats[rel] = (mtime_ns, size)
            continue
        try:
            data = fpath.read_bytes()
        except OSError:
            continue
        if session is not None:
            meta, _ = parse_frontmatter(data.decode("utf-8", errors="replace"))
            if str((meta or {}).get("originSessionId") or "") != session:
                continue
        snapshot[rel] = hashlib.sha256(data).hexdigest()
        if out_stats is not None:
            out_stats[rel] = (mtime_ns, size)
    return snapshot


def _load_ingest_manifest(path: Path) -> dict[str, str] | None:
    """Load the ingest stamp's ``relpath -> hash`` map.

    Returns ``None`` when the manifest is absent/unreadable/malformed (no
    prior successful ingest — the incremental gate must NOT no-op), or the
    ``{relpath: hash}`` map (possibly empty) when a stamp exists.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hashes = data.get("hashes")
    if isinstance(hashes, dict):
        return {str(k): str(v) for k, v in hashes.items()}
    return {}


def _load_ingest_manifest_stats(path: Path) -> dict[str, tuple[int, int]]:
    """Load the ingest stamp's ``relpath -> (mtime_ns, size)`` stat map (#370).

    Absent (a v1 manifest with no ``stats``) or malformed => ``{}``, which
    forces a one-time full read+hash of every raw file and upgrades the manifest
    to v2 on the next stamp. Rows that do not parse are skipped, never crashing.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    stats = data.get("stats")
    if not isinstance(stats, dict):
        return {}
    out: dict[str, tuple[int, int]] = {}
    for k, v in stats.items():
        try:
            out[str(k)] = (int(v[0]), int(v[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _write_ingest_manifest(
    path: Path,
    hashes: dict[str, str],
    stats: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Atomically write the ingest stamp manifest (temp file + rename).

    ``stats`` (issue #370) persists per-file ``(mtime_ns, size)`` so the next
    run's stat pre-filter can skip re-reading unchanged raw files. Bumped to
    ``version: 2`` when stats are written; ``hashes`` stays present for readers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    version = 2 if stats is not None else 1
    payload: dict[str, Any] = {"version": version, "hashes": hashes}
    if stats is not None:
        payload["stats"] = {k: [v[0], v[1]] for k, v in stats.items()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def ingest(
    raw_root: Path = DEFAULT_RAW_ROOT,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    *,
    incremental: bool = True,
    session: str | None = None,
    cache_dir: Path | None = None,
    config: dict[str, object] | None = None,
    dry_run: bool = False,
    **run_kwargs: Any,
) -> IngestResult:
    """Compile new/changed raw intake into the wiki on demand (issue #349).

    The on-demand counterpart to the nightly :func:`run`: an agent (or the
    operator, via ``athenaeum ingest``) forces freshly-``remember``ed raw
    files through the librarian compile step so the knowledge becomes
    recallable *now*, decoupled from the nightly cadence. Issue #350's
    SessionEnd hook reuses this exact function — it is the single reusable
    incremental-ingest engine; the CLI is a thin wrapper.

    ``incremental`` (default) diffs the current raw-intake set against a
    content-hash stamp manifest (``<cache_dir>/ingest-manifest.json``,
    mirroring the #348 index manifest). When a prior stamp exists and nothing
    is new or changed, it returns a fast no-op WITHOUT invoking the heavy
    compile. ``incremental=False`` (``--full``) always recompiles. ``session``
    scopes the new/changed detection to one ``originSessionId``.

    ``tier0_passthrough`` structured raw compiles with no LLM cost — the
    underlying :func:`run` routes pre-structured intake (uid/type/name) through
    Tier 0, which never calls the model.

    The compile itself delegates to :func:`run` (extra keyword arguments are
    forwarded verbatim, e.g. ``install_signal_handlers=True`` from the CLI).
    On a nonzero compile the stamp manifest is left UNTOUCHED so the next run
    retries; a ``dry_run`` never stamps.
    """
    start = time.monotonic()
    if config is None:
        config = load_config(knowledge_root)
    manifest_path = _resolve_cache_dir(cache_dir) / INGEST_MANIFEST_NAME
    mode = "incremental" if incremental else "full"

    stored = _load_ingest_manifest(manifest_path)
    stored_stats = _load_ingest_manifest_stats(manifest_path)
    # Issue #370: reuse the stored hash for raw files whose (mtime_ns, size) are
    # unchanged since the last stamp, skipping the read+hash. Only names present
    # in BOTH the stat map and the hash map are eligible (a v1 manifest has no
    # stats → empty prior → every file is read once, then upgraded to v2).
    raw_prior: dict[str, tuple[int, int, str]] = {
        rel: (stored_stats[rel][0], stored_stats[rel][1], stored[rel])
        for rel in stored_stats
        if stored is not None and rel in stored
    }

    # Snapshot the raw intake BEFORE compiling (keyed relpath -> hash). The
    # session-scoped view drives the new/changed gate; the unscoped view drives
    # both the stamp we persist and the consumed-file (``compiled``) count.
    # ``before_all_stats`` collects the unscoped (mtime_ns, size) for the stamp.
    before_all_stats: dict[str, tuple[int, int]] = {}
    before = _raw_hash_snapshot(
        raw_root,
        knowledge_root,
        session=session,
        prior_stats=raw_prior,
        out_stats=(before_all_stats if session is None else None),
    )
    before_all = (
        before
        if session is None
        else _raw_hash_snapshot(
            raw_root,
            knowledge_root,
            prior_stats=raw_prior,
            out_stats=before_all_stats,
        )
    )

    baseline = stored or {}
    added = [k for k in before if k not in baseline]
    changed = [k for k, h in before.items() if k in baseline and baseline[k] != h]
    new_or_changed = len(added) + len(changed)

    # Issue #370: a dry-run is a pure manifest-diff PREVIEW — report the delta
    # counts WITHOUT invoking the heavy compile (no clustering, no merge, no
    # chromadb/ONNX). ``noop`` preserves its meaning (nothing new/changed) and a
    # dry-run never stamps. Returning here also keeps ``run()`` off the dry-run
    # path entirely (the cluster/merge guards below are defense-in-depth).
    if dry_run:
        return IngestResult(
            mode=mode,
            new_or_changed=new_or_changed,
            compiled=0,
            noop=new_or_changed == 0,
            exit_code=0,
            duration_ms=int((time.monotonic() - start) * 1000),
            session=session,
        )

    # Incremental fast no-op: a prior stamp exists and nothing is new/changed.
    # Never invoke the compile — and leave the stamp untouched. Rewriting it
    # here would let a session-scoped no-op absorb OTHER sessions' still-pending
    # files as "seen" (they were never compiled). The stamp only grows when a
    # real compile runs and actually processes every pending file.
    if incremental and stored is not None and new_or_changed == 0:
        return IngestResult(
            mode=mode,
            new_or_changed=0,
            compiled=0,
            noop=True,
            exit_code=0,
            duration_ms=int((time.monotonic() - start) * 1000),
            session=session,
        )

    # Issue #370 PR2: thread the auto-memory delta into ``run`` so the cluster +
    # merge passes scope to only the changed files' affected clusters. Map the
    # new/changed relpaths that live under an auto-memory intake root to absolute
    # paths; entity raw (``raw/<ts>-<uuid>.md``) is excluded, so an entity-only
    # ingest yields an EMPTY set — a valid delta ("no auto-memory changed") that
    # leaves the auto-memory wiki untouched instead of recompiling it. ``run``
    # itself vetoes delta under LLM contradiction mode (D5) and falls back to a
    # full compile when the delta is not viable (D1-D3/F6), so this is always a
    # safe optimisation hint. Only passed when a real compile runs (below), never
    # via the dry-run / no-op early returns above.
    extra_roots = resolve_extra_intake_roots(knowledge_root, config=config)
    auto_changed: set[Path] = set()
    for rel in (*added, *changed):
        abspath = (knowledge_root / rel).resolve()
        for root in extra_roots:
            try:
                abspath.relative_to(root.resolve())
            except ValueError:
                continue
            auto_changed.add(abspath)
            break
    run_kwargs.pop("changed_paths", None)

    exit_code = run(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=dry_run,
        changed_paths=auto_changed,
        **run_kwargs,
    )

    after_all = _raw_hash_snapshot(raw_root, knowledge_root)
    compiled = len(set(before_all) - set(after_all))

    # Stamp the pre-compile snapshot on a clean, non-dry run: everything we
    # just processed is now "seen". Consumed (deleted) files stay recorded —
    # harmless, and it keeps a re-run with no new intake a fast no-op. Files
    # that appeared mid-run are absent here, so they correctly surface as
    # ``added`` next run. A failed compile leaves the stamp untouched (retry).
    if exit_code == 0 and not dry_run:
        _write_ingest_manifest(manifest_path, before_all, stats=before_all_stats)

    return IngestResult(
        mode=mode,
        new_or_changed=new_or_changed,
        compiled=compiled,
        noop=False,
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - start) * 1000),
        session=session,
    )


def reindex(
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    wiki_root: Path | None = None,
    *,
    cache_dir: Path | None = None,
    config: dict[str, object] | None = None,
    backend: str | None = None,
    incremental: bool = True,
) -> tuple[str, int]:
    """Rebuild the search index; return ``(backend_name, pages_indexed)``.

    The reusable core the ``reindex`` CLI and the SessionEnd composition
    (:func:`session_end`, issue #350) share, so both apply the *same* backend
    resolution, extra-intake roots, and index globs. ``incremental`` (default,
    issue #348) applies only the add/change/delete hash-diff delta — a fast
    no-op when the wiki has not changed since the last build. A ``vector``
    backend that is not installed raises ``ImportError`` to the caller.
    """
    from athenaeum.config import resolve_embedding_model, resolve_index_globs
    from athenaeum.search import build_fts5_index, build_vector_index

    if wiki_root is None:
        wiki_root = knowledge_root / "wiki"
    if config is None:
        config = load_config(knowledge_root)
    resolved_cache = _resolve_cache_dir(cache_dir)
    resolved_cache.mkdir(parents=True, exist_ok=True)
    backend_name = backend or str(config.get("search_backend", "fts5"))
    extra_roots = resolve_extra_intake_roots(knowledge_root, config)
    include_globs, exclude_globs = resolve_index_globs(config)

    if backend_name == "vector":
        pages = build_vector_index(
            wiki_root,
            resolved_cache,
            extra_roots=extra_roots,
            incremental=incremental,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            embedding_model=resolve_embedding_model(config),
        )
    else:
        pages = build_fts5_index(
            wiki_root,
            resolved_cache,
            extra_roots=extra_roots,
            incremental=incremental,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
        )
    return backend_name, pages


def _reindex_would_change(
    knowledge_root: Path,
    wiki_root: Path,
    *,
    cache_dir: Path | None,
    config: dict[str, object],
    backend: str | None,
) -> int | None:
    """Cheap dry-run preview of how many pages a reindex would touch (#370).

    Diffs the current wiki against the vector/fts5 index manifest — the SAME
    ``added + changed + removed`` delta :func:`reindex` would apply — but WITHOUT
    opening chromadb or loading any embedding model. The scan reuses the #370
    stat pre-filter, so it re-hashes only changed files. Returns the delta count,
    or ``None`` when it cannot be computed cheaply (no wiki dir).
    """
    from athenaeum.config import resolve_index_globs
    from athenaeum.search import (
        _FTS5_MANIFEST,
        _VECTOR_MANIFEST,
        _compute_delta,
        _load_manifest,
        _manifest_hashes,
        _scan_indexed_records,
        _scan_prior,
    )

    if not wiki_root.is_dir():
        return None
    resolved_cache = _resolve_cache_dir(cache_dir)
    backend_name = backend or str(config.get("search_backend", "fts5"))
    manifest_name = _VECTOR_MANIFEST if backend_name == "vector" else _FTS5_MANIFEST
    manifest_path = resolved_cache / manifest_name
    extra_roots = resolve_extra_intake_roots(knowledge_root, config)
    include_globs, exclude_globs = resolve_index_globs(config)

    stored = _load_manifest(manifest_path)
    prior = _scan_prior(stored) if stored is not None else None
    current_hashes = {
        name: h
        for name, _p, h, _t, _m, _s in _scan_indexed_records(
            wiki_root,
            extra_roots,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            prior=prior,
        )
    }
    stored_hashes = _manifest_hashes(stored)
    added, changed, removed = _compute_delta(current_hashes, stored_hashes)
    return len(added) + len(changed) + len(removed)


@dataclass
class SessionEndResult:
    """Summary of a :func:`session_end` invocation (issue #350).

    Wraps the underlying :class:`IngestResult` and records whether the reindex
    step ran (it is change-gated on the compile actually having run) and how
    many pages it touched. ``exit_code`` propagates the ingest compile's status
    — the SessionEnd path never indexes a half-compiled wiki.
    """

    ingest: IngestResult
    reindexed: bool
    reindex_pages: int
    backend: str
    duration_ms: int
    session: str | None = None
    # Issue #370: on a dry-run, a cheap manifest hash-diff of how many pages a
    # real reindex WOULD touch — computed WITHOUT opening chromadb or loading a
    # model. ``None`` on a non-dry-run, or when it could not be computed cheaply.
    dry_run: bool = False
    reindex_would_change: int | None = None

    @property
    def exit_code(self) -> int:
        return self.ingest.exit_code

    def summary(self) -> dict[str, object]:
        """One-line JSON-serializable summary (nests the ingest summary)."""
        data: dict[str, object] = {
            "command": "session-end",
            "mode": self.ingest.mode,
            "ingest": self.ingest.summary(),
            "reindexed": self.reindexed,
            "reindex_pages": self.reindex_pages,
            "backend": self.backend,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
        }
        if self.dry_run:
            # Cheap preview: how many pages a reindex would touch. ``null`` here
            # means it could not be computed without chromadb (never loaded one).
            data["reindex_would_change"] = self.reindex_would_change
            if self.reindex_would_change is None:
                data["reindex_would_change_note"] = (
                    "not computed (no index manifest / wiki); " "chromadb not opened"
                )
        if self.session is not None:
            data["session"] = self.session
        return data


def session_end(
    raw_root: Path = DEFAULT_RAW_ROOT,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    *,
    session: str | None = None,
    incremental: bool = True,
    cache_dir: Path | None = None,
    config: dict[str, object] | None = None,
    backend: str | None = None,
    dry_run: bool = False,
    **run_kwargs: Any,
) -> SessionEndResult:
    """Change-gated SessionEnd compile-then-index composition (issue #350).

    The single command the cwc SessionEnd hook and the nightly-after-librarian
    path invoke so a memory ``remember``ed by one agent becomes recallable by
    every other agent after that session ends — closing the ~24h gap where a
    fact sat in ``raw/`` unseen until the next nightly librarian run.

    Two steps, both change-gated so an idle SessionEnd is cheap:

    1. **Incremental** :func:`ingest` of this session's new/changed raw intake.
       Internally a fast no-op (zero LLM) when nothing is new; ``tier0``
       structured entries compile with no model cost.
    2. **Then** :func:`reindex` — but *only when the compile actually ran*
       (``ingest`` was not a no-op) and succeeded. An idle SessionEnd (no new
       raw), a failed compile, or a ``dry_run`` never touches the index, per
       the issue's per-session cost bound.

    ``session`` scopes the new/changed detection to one ``originSessionId``
    (the SessionEnd use-case). ``incremental=False`` forces a full recompile +
    full reindex (an operator escape hatch). Extra keyword arguments forward to
    :func:`run` (e.g. ``install_signal_handlers``). Single-flight locking is the
    caller's responsibility — the CLI wrapper holds the run lock across both
    steps.
    """
    start = time.monotonic()
    if config is None:
        config = load_config(knowledge_root)

    ingest_result = ingest(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        incremental=incremental,
        session=session,
        cache_dir=cache_dir,
        config=config,
        dry_run=dry_run,
        **run_kwargs,
    )

    backend_name = backend or str(config.get("search_backend", "fts5"))
    reindexed = False
    reindex_pages = 0
    reindex_would_change: int | None = None

    # Change-gate the index step: reindex only when the compile actually ran
    # (wiki may have changed) AND succeeded, and never on a dry-run. An idle
    # no-op ingest short-circuits here → no reindex, per the acceptance bound.
    should_reindex = (
        not ingest_result.noop and ingest_result.exit_code == 0 and not dry_run
    )
    if should_reindex:
        # Issue #370: announce the planned work BEFORE the (potentially minutes-
        # long) reindex so the run does not look like a silent hang.
        log.info(
            "session-end: %d new/changed raw (compiled %d); reindexing wiki "
            "(%s backend)…",
            ingest_result.new_or_changed,
            ingest_result.compiled,
            backend_name,
        )
        sys.stdout.flush()
        sys.stderr.flush()
        backend_name, reindex_pages = reindex(
            knowledge_root=knowledge_root,
            wiki_root=wiki_root,
            cache_dir=cache_dir,
            config=config,
            backend=backend,
            incremental=incremental,
        )
        log.info("session-end: reindex complete — %d page(s) indexed", reindex_pages)
        reindexed = True
    elif dry_run:
        # Issue #370: cheap dry-run preview — count how many pages a reindex
        # WOULD touch via a manifest hash-diff (the SAME delta reindex applies)
        # WITHOUT opening chromadb or loading any embedding model.
        reindex_would_change = _reindex_would_change(
            knowledge_root,
            wiki_root,
            cache_dir=cache_dir,
            config=config,
            backend=backend,
        )

    return SessionEndResult(
        ingest=ingest_result,
        reindexed=reindexed,
        reindex_pages=reindex_pages,
        backend=backend_name,
        duration_ms=int((time.monotonic() - start) * 1000),
        session=session,
        dry_run=dry_run,
        reindex_would_change=reindex_would_change,
    )
