# SPDX-License-Identifier: Apache-2.0
"""Knowledge librarian — the nightly run loop, L4 domain/pipeline.

Contract: owns the top-level ``athenaeum run`` orchestration — discovering
raw intake, driving it through the four programmatic/LLM tiers below, and
calling out to the other seven SCC siblings for the passes that happen
around that loop (clustering/merge, wiki-page dedup, retire, reresolve,
batch mode, status/page-size checks, pending-merge revalidation). It is the
run loop's HUB, not a general dumping ground: helper logic that belongs to
one of those passes belongs in ITS module, not here.

  Tier 1: Programmatic entity matching (no LLM)
  Tier 2: Classification via fast LLM
  Tier 3: Content writing via capable LLM
  Tier 4: Human escalation to _pending_questions.md

Usage:
  athenaeum run [--raw-root PATH] [--wiki-root PATH] [--dry-run]

Environment:
  ANTHROPIC_API_KEY          Required for Tier 2/3 LLM calls.
  ATHENAEUM_CLASSIFY_MODEL   Override the Tier 2 model (default: config.DEFAULT_CLASSIFY_MODEL)
  ATHENAEUM_WRITE_MODEL      Override the Tier 3 model (default: tiers.DEFAULT_WRITE_MODEL)
  ATHENAEUM_MAX_FILES        Override the per-run intake batch size (default: 50)
  ATHENAEUM_BATCH_MODE       Opt into Batch API mode for tier-2/3 calls (default: off)

Layering and the SCC (read this before touching any of librarian / merge /
tiers / pending_merges / batch / status / retire / wiki_dedupe): these
modules are L4 domain/pipeline. Historically they formed ONE ~12,000-line
mutually-recursive cycle held together by function-local (deferred) imports
BACK into ``librarian`` for three shared raw-intake primitives. Issue athenaeum#545
HOISTED those three primitives — ``discover_raw_files``,
``discover_auto_memory_files``, ``tier0_passthrough`` — DOWN to the
:mod:`athenaeum.intake` leaf module (``vecmath`` from athenaeum#542 is the precedent),
so the modules that need them import from ``intake`` at TOP level and the
librarian-centered back-edges are gone. ``batch``, ``status``, ``retire``, and
``wiki_dedupe`` are now fully free of the librarian cycle.

``librarian.py`` is still the run-loop hub: it imports :mod:`athenaeum.merge`
at TOP level (the C1-C4 cluster/merge pass, a normal downward dependency) and
re-exports the three hoisted primitives from ``intake`` for backward
compatibility. It still owns these deferred (function-local) imports, which
are NOT librarian<->sibling cycle back-edges and stay deferred:

- ``_run_retire_pass``: local ``from athenaeum.retire import
  run_retire_pass``. ``retire.py`` never imports librarian back; deferred so
  a retire failure stays best-effort/isolated.
- ``_run_reresolve_pass``: local ``from athenaeum.tiers import
  reresolve_open_questions``. ``tiers.py`` no longer imports librarian back
  (its ``discover_auto_memory_files`` now comes from ``intake`` at top level);
  deferred to keep the reresolve pass isolated.
- run loop: local ``from athenaeum.wiki_dedupe import
  propose_wiki_page_merges`` and ``from athenaeum.batch import
  process_batch_run`` — neither module imports librarian back; deferred for
  best-effort/optional-branch isolation, not cycle-breaking.
- run loop: local ``from athenaeum.status import scan_page_sizes``.
  ``status.py`` no longer imports librarian (it gets ``discover_raw_files``
  from ``intake`` now); this is a one-way edge kept deferred for best-effort
  page-size guardrail isolation.
- run loop: local ``from athenaeum.pending_merges import
  revalidate_pending_merges`` — ``pending_merges`` does not import librarian;
  deferred for best-effort framing.
- run loop: local ``from athenaeum.drain_advisor import build_advisory``
  (backlog-drain advisor). ``drain_advisor`` is a low leaf that does NOT import
  librarian back, so this is a one-way edge. Issue athenaeum#640 moved ``build_advisory``
  there from the ``drain`` orchestrator precisely so this run-loop call no longer
  reaches up into ``drain`` (``drain`` still imports ``librarian.run`` back,
  function-locally — now a one-directional ``drain`` -> ``librarian`` edge).

The three residual SCCs that athenaeum#545 left in place were all dissolved in issue athenaeum#640
(``{librarian, drain, status}``, ``{merge, pending_merges, calibration,
reasoning_tiers}`` and ``{tiers, contradictions, resolutions, answers}``); the
full-graph SCC is now empty and ``tests/test_import_graph_acyclic.py`` pins the
baseline at ``[]``. Do not reintroduce a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from athenaeum import detection_state, spend
from athenaeum._retry import TransientAPIError
from athenaeum.atomic_io import atomic_write_text
from athenaeum.clusters import (
    cluster_auto_memory_files,
    prune_cluster_rotations,
    resolve_cluster_output_path,
    resolve_cluster_threshold,
    resolve_rotation_retention,
    write_cluster_report,
)
from athenaeum.config import (
    DEFAULT_KNOWLEDGE_ROOT as _DEFAULT_KNOWLEDGE_ROOT_TEMPLATE,
)
from athenaeum.config import (
    load_config,
    resolve_delta_enabled,
    resolve_delta_max_affected_clusters,
    resolve_delta_max_affected_members,
    resolve_extra_intake_roots,
    resolve_full_compile_every_days,
    resolve_live_delta_enabled,
    resolve_pull_before_run,
    resolve_push_after_run,
    resolve_push_branch,
    resolve_push_remote,
    resolve_retire,
)
from athenaeum.config import resolve_cache_dir as _resolve_cache_dir_config
from athenaeum.delta import (
    _relpath_for,
    compute_affected_clusters,
    splice_cluster_report,
)
from athenaeum.intake import (  # noqa: F401 — AUTO_MEMORY_FILE_RE/RAW_FILE_RE re-exported for back-compat
    _AUTO_MEMORY_SKIP_NAMES,
    AUTO_MEMORY_FILE_RE,
    RAW_FILE_RE,
    discover_auto_memory_files,
    discover_raw_files,
    tier0_passthrough,
)
from athenaeum.merge import (
    RunDeadlineExceeded,
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
    load_schema_list,
    parse_access,
    parse_frontmatter,
    render_frontmatter,
)
from athenaeum.provider import (
    ProviderConfigError,
    build_llm_client,
    capabilities_for,
    preflight_provider,
    resolve_provider,
)
from athenaeum.registry import collect_handles
from athenaeum.schemas import validate_wiki_meta
from athenaeum.self_resolving import flag_self_resolving_claims
from athenaeum.tiers import (
    Tier2ParseStats,
    partition_code_artifact_classifications,
    schema_fragment_state,
    tier1_programmatic_match,
    tier2_classify,
    tier3_write,
    tier4_escalate,
)

log = logging.getLogger(__name__)


# Defaults — can be overridden via CLI args or the run() API.
# The pre-expanded runtime default, derived from the single tilde-template
# source of truth in ``config`` (issue athenaeum#537). ``.expanduser()`` yields the same
# value as the former ``Path.home() / "knowledge"`` literal, but the
# ``~/knowledge`` string now lives in exactly one module. These constants are
# used directly as real filesystem paths (function defaults below), so they must
# be expanded here rather than left in tilde form.
DEFAULT_KNOWLEDGE_ROOT = _DEFAULT_KNOWLEDGE_ROOT_TEMPLATE.expanduser()
DEFAULT_RAW_ROOT = DEFAULT_KNOWLEDGE_ROOT / "raw"
DEFAULT_WIKI_ROOT = DEFAULT_KNOWLEDGE_ROOT / "wiki"

# Run-level API call budget.
# Raised 200 -> 800 (issue athenaeum#220): the 2026-06-11 nightly observed 404 calls
# hit the 200 cap with intake remaining — now that the athenaeum#187 confirmation
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

# Per-run intake batch size (issue athenaeum#232). Precedence: `--max-files` (CLI
# flag, wins) > `ATHENAEUM_MAX_FILES` (env) > `librarian.max_files` (yaml)
# > this default. Resolved by `librarian_max_files()` below.
DEFAULT_MAX_FILES = 50

# Run-level wall-clock deadline in seconds (issue athenaeum#396). Budget caps
# (`--max-files` / `--max-api-calls`) bound how MUCH a run does, but nothing
# bounded how LONG it ran: a post-checkpoint phase that stopped making
# progress (the athenaeum#396 incident: a hung `claude -p` merge subprocess) ran ~15h
# holding the run-lock until externally killed. The nightly run's ~1h cap
# came from an external `timeout` wrapper, not athenaeum itself, so any
# un-wrapped run (a manual backlog drain) was unbounded. This default gives
# every run an INTERNAL deadline of roughly the nightly external cap so a
# manual/un-wrapped run is bounded by default. Precedence: `--max-runtime`
# (CLI flag, wins) > `ATHENAEUM_MAX_RUNTIME` (env) > `librarian.max_runtime`
# (yaml) > this default. Resolved by `librarian_max_runtime()` below. A
# resolved value <= 0 disables the deadline (explicit opt-out escape hatch).
DEFAULT_MAX_RUNTIME = 3600  # 1 hour

# Fraction of ``max_runtime`` the ENTITY phase may spend claiming new files
# (issue athenaeum#440). ``run_deadline`` is a single run-level budget shared by every
# phase, and the entity loop only stops when that WHOLE budget is gone — so a
# slow entity phase starves everything downstream of it. Measured on the live
# corpus: entity consumed 3690s of a 3944s window (93.6%), and the C4
# contradiction detector — which runs after it — got 0 seconds on every one of
# 10+ consecutive nights. Reserving a tail makes the downstream phases'
# budget structural instead of "whatever entity happens to leave": the entity
# loop stops CLAIMING new files once ``share * max_runtime`` is spent, defers
# the rest (resumable, exactly like the athenaeum#220 budget trip), and lets the run
# fall through to the auto-memory / C2-C4 block.
#
# NOTE the granularity: the check sits at the per-file boundary, so a file
# already started may overrun the share by its own duration. This bounds when
# the phase stops TAKING work, not when it stops working.
#
# Precedence: ``ATHENAEUM_ENTITY_RUNTIME_SHARE`` (env) >
# ``librarian.entity_runtime_share`` (yaml) > this default. A resolved value
# outside ``0 < share < 1`` disables the reserve entirely (entity may use the
# whole window) — the explicit opt-out that restores pre-athenaeum#440 behaviour.
DEFAULT_ENTITY_RUNTIME_SHARE = 0.6

# Manifest written next to _pending_questions.md when a budget-tripped run
# defers intake (issue athenaeum#220). Overwritten on every tripped run; removed by
# the next clean run.
DEFERRED_MANIFEST_NAME = "_deferred_work.md"

# Issue athenaeum#663: a persistent, cross-run ledger of raw files whose processing has
# failed on the same content N consecutive runs. A single reliably-failing LLM
# call (e.g. an entity page large enough to time out every night) otherwise
# makes a raw file a PERMANENT no-progress loop: it is retried whole every
# night, does ~17 units of successful merge work, throws all of it away on the
# one timeout, and is retried identically forever — silently. This ledger
# counts consecutive failures per file (keyed by ref + content hash, so a
# re-edited file gets a fresh count) so a file that has failed ``>=`` the
# threshold can be SKIPPED (it stops burning the entity-phase budget every
# night) and SURFACED as machine-detectable run state instead of vanishing
# into a per-run warning. Written under wiki_root beside the deferred manifest;
# the ``_`` prefix + ``.json`` suffix keep it out of ``rebuild_index`` (which
# only globs ``*.md`` and skips ``_``-prefixed names). Removed when empty.
STUCK_MANIFEST_NAME = "_stuck_files.json"

# Consecutive-failure count at which a raw file is treated as stuck (skipped +
# surfaced) rather than retried again. Resolved via
# :func:`librarian_stuck_file_threshold` (env > yaml > this default).
DEFAULT_STUCK_FILE_THRESHOLD = 3

# Stable, machine-greppable prefix for the WARNING emitted when a file is
# surfaced as stuck (crossing the threshold, or skipped on a later run). A
# log-scraper / watchdog can grep this without parsing prose — the athenaeum#663
# requirement that a permanently-stuck file be LOUD, not merely logged.
STUCK_FILE_PREFIX = "librarian-stuck-file"

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

# Raw-intake discovery + tier-0 passthrough primitives (RAW_FILE_RE,
# AUTO_MEMORY_FILE_RE, _AUTO_MEMORY_SKIP_NAMES, discover_raw_files,
# discover_auto_memory_files, tier0_passthrough) moved DOWN to the
# :mod:`athenaeum.intake` leaf module in issue athenaeum#545 to dissolve the
# librarian-centered import SCC. They are re-imported at the top of this
# module (``from athenaeum.intake import ...``) so this module's own call
# sites, the public ``athenaeum.discover_raw_files`` re-export, and existing
# ``from athenaeum.librarian import ...`` call sites all keep working.


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
        etype = str(meta.get("type", "unknown"))
        uid = str(meta.get("uid", ""))
        name = str(meta.get("name", fpath.stem))
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

    atomic_write_text(wiki_root / "_index.md", "\n".join(lines))
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


def _maybe_pull_before_run(
    knowledge_root: Path,
    *,
    config: dict | None,
    pull_before_run: bool,
    dry_run: bool,
) -> None:
    """Pull the knowledge repo before the run starts, iff opted in.

    Issue athenaeum#399 gating, symmetric to :func:`_maybe_push_after_run`: (a)
    explicit opt-in, (b) not a ``--dry-run``, (c) a real git repo exists at
    ``knowledge_root``. Reuses the SAME remote/branch resolvers as the
    post-run push (``resolve_push_remote`` / ``resolve_push_branch``) — pull
    and push address the same knowledge remote, so there is no separate
    pull_remote/pull_branch config surface. Pull failure is non-fatal —
    ``git_pull`` logs a warning; the run proceeds against the local tree.
    """
    if not pull_before_run or dry_run:
        return
    if not (knowledge_root / ".git").exists():
        return
    git_pull(
        knowledge_root,
        remote=resolve_push_remote(config),
        branch=resolve_push_branch(config),
    )


def _maybe_push_after_run(
    knowledge_root: Path,
    *,
    config: dict | None,
    push_after_run: bool,
    dry_run: bool,
    head_at_start: str | None,
) -> None:
    """Push the knowledge repo iff the run committed at least one new commit.

    Issue athenaeum#284 gating: (a) explicit opt-in, (b) not a ``--dry-run``,
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

    Used by the post-run push hook (issue athenaeum#284) to detect whether the run
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
    """Push the knowledge repo's current branch to *remote* (issue athenaeum#284).

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


def git_pull(
    knowledge_root: Path,
    remote: str = "origin",
    branch: str | None = None,
) -> bool:
    """Pull the knowledge repo's current branch from *remote* (issue athenaeum#399).

    Runs ``git pull --ff-only --autostash``. ``--ff-only`` refuses to create
    a merge commit on divergent history — this is a compilation pipeline,
    not a collaborative branch, so a fast-forward is the only sync shape we
    want. ``--autostash`` handles the librarian's common dirty-working-tree
    starting state (uncommitted raw intake from ``remember`` appends,
    ``.athenaeum.lock``, contact-sync state): it stashes local changes
    before the ff-only merge and re-applies them after, so a routine dirty
    tree does not block the pull.

    Returns ``True`` when the pull succeeded, ``False`` otherwise. A failure
    — diverged history that ``--ff-only`` rejects, an autostash re-apply
    conflict, or any other non-zero exit — is logged as a clearly-marked
    WARNING and is NEVER fatal: the run proceeds against the local tree
    exactly as it would have before this pull existed. Athenaeum performs no
    credential handling; the operator's ambient git credentials (credential
    helper / SSH) are used.

    When *branch* is ``None``, ``git pull`` defaults to the configured
    upstream for the current branch. Passing an explicit branch makes the
    refspec deterministic.
    """
    if not (knowledge_root / ".git").exists():
        log.warning("No .git in %s — skipping git pull", knowledge_root)
        return False

    cmd = ["git", "pull", "--ff-only", "--autostash", remote]
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
        # remote/branch failed to fast-forward and why. The run proceeds
        # against the local tree — never abort, never roll back.
        log.warning(
            "athenaeum-pull-failed: git pull %s%s exited %d (run proceeds "
            "against local tree): %s",
            remote,
            f" {branch}" if branch else "",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    log.info(
        "Pulled knowledge repo from %s%s",
        remote,
        f" {branch}" if branch else "",
    )
    return True


def tier0_handle_upsert(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    valid_types: list[str],
    dry_run: bool = False,
) -> tuple[WikiEntity, bool] | None:
    """Deterministically merge a pre-structured seed's source-handle keys onto
    an EXISTING entity page, LLM-free (issue athenaeum#486).

    athenaeum#454 seeds source handles (athenaeum#453's schema) by writing raw intake that carries
    ``uid``/``type``/``name`` plus the source-handle frontmatter keys. When the
    entity is NEW, :func:`tier0_passthrough` promotes it verbatim and the handles
    land as frontmatter. When it already EXISTS, tier0 declines (uid in index,
    the idempotency gate) and — before this path existed — the raw fell through
    to the Tier 2/3 LLM tiers, which classify the handle block as prose and fold
    it into the page body. The structured ``source_handles`` schema (athenaeum#453) was
    lost, so ``registry.json`` could not resolve the seeded entity and athenaeum#454's
    "seed via raw intake, no hand-edit" acceptance was unreachable.

    This path applies the seed's populated source-handle keys directly onto the
    existing page's frontmatter, verbatim — never through the LLM — so a re-seed
    onto a known entity lands as frontmatter, exactly like a first seed does
    through tier0 passthrough.

    Entity resolution (issue athenaeum#692): a seed rarely knows the wiki's internal
    ``uid`` — an agent or athenaeum#454 source-handle seed names the company but supplies
    only ``type``/``name`` plus the handle keys. When the raw declares a ``uid``
    it is used directly (the athenaeum#486 path); when it does not, the EXISTING entity is
    resolved deterministically by name/alias via the index, and the handles are
    upserted onto that page. Before athenaeum#692 the uid-less shape fell through to the
    LLM tiers, which flattened the handle block into the page body as prose and
    lost the athenaeum#453 schema — the bug this closes.

    Eligibility (ALL required, else return ``None`` so the caller falls through
    to Tier 1/2/3 with today's behaviour intact): frontmatter parses;
    ``type``/``name`` are non-empty; ``type`` is in the allowlist; the raw
    carries at least one *populated* source handle; and the entity ALREADY
    exists — resolved by ``uid`` when the raw declares one, otherwise by
    name/alias (matching an entity-format page of the same ``type``). A
    pre-structured raw that carries no source handle (an ordinary note re-intake)
    is left to the LLM tiers untouched. A handle seed that resolves to no
    existing entity (or a cross-type / non-entity page) is logged at WARNING and
    declined — it fails loudly rather than silently degrading to body prose.

    Idempotent (AC #4 — "re-running the same seed is a no-op"): the write is
    gated on an actual source-handle delta. When the seed introduces no new or
    changed handle value, the page is left byte-for-byte unchanged and this
    returns ``(entity, False)`` — no rewrite, no ``updated`` bump. When a handle
    is added or changed, the page is rewritten with the merged frontmatter (and
    ``updated`` stamped to today) and ``(entity, True)`` is returned.
    """
    meta, _ = parse_frontmatter(raw.content)
    if not meta:
        return None
    uid = str(meta.get("uid", "") or "").strip()
    etype = str(meta.get("type", "") or "").strip()
    name = str(meta.get("name", "") or "").strip()
    # type + name are always required; uid is resolved by name below when the
    # seed does not self-declare one (issue athenaeum#692).
    if not etype or not name:
        return None
    if etype not in valid_types:
        return None

    incoming = collect_handles(meta)
    if not incoming:
        # Not a handle seed — leave it to the LLM tiers unchanged.
        return None

    if uid:
        existing_path = index.get_by_uid(uid)
        if existing_path is None or not existing_path.exists():
            # New entity — tier0_passthrough owns it; nothing to upsert onto.
            return None
    else:
        # athenaeum#692: a source-handle seed that carries the handle frontmatter +
        # ``type``/``name`` but NO internal ``uid`` (the normal shape — an agent
        # or seed naming a company does not know its wiki uid). Before this, such
        # a raw fell through to the LLM tiers and its handle block was flattened
        # into the page BODY as prose, silently losing the athenaeum#453 schema. Resolve
        # the EXISTING entity deterministically by name/alias and upsert onto it,
        # exactly as the uid-bearing path does.
        resolved = index.lookup(name)
        if resolved is None:
            # Names no existing entity — this deterministic path only UPSERTS
            # onto an existing page (creating a new entity is tier0_passthrough's
            # job, which requires a uid). Fail LOUDLY rather than let the handle
            # block degrade to prose downstream (the actual athenaeum#692 defect).
            log.warning(
                "  T0 handle-upsert: seed for %r (%s) carries source handles "
                "%s but names no existing entity and declares no uid — not "
                "placed as frontmatter; fix the seed's name/uid",
                name,
                etype,
                sorted(incoming),
            )
            return None
        resolved_uid, existing_path = resolved
        if (
            not resolved_uid
            or not existing_path.exists()
            or not index.has_entity_format(existing_path)
        ):
            # Matched a name-only (non-entity-format) page — no uid to key on and
            # not a source-handle target. Surface it, then leave it unchanged.
            log.warning(
                "  T0 handle-upsert: seed for %r (%s) matched a non-entity page "
                "%s — source handles %s not placed; fix the seed's name/uid",
                name,
                etype,
                existing_path.name,
                sorted(incoming),
            )
            return None
        uid = resolved_uid

    existing_meta, existing_body = parse_frontmatter(existing_path.read_text(encoding="utf-8"))
    if not existing_meta:
        return None

    # Guard against a cross-type upsert: a name can resolve to a same-named
    # entity of a different type. Only upsert when the existing page's type
    # matches the seed's declared type (a uid-bearing seed is trusted as before).
    existing_type = str(existing_meta.get("type", "") or "").strip()
    if existing_type and existing_type != etype:
        log.warning(
            "  T0 handle-upsert: seed for %r declares type %s but the matched "
            "page %s is type %s — source handles not placed",
            name,
            etype,
            existing_path.name,
            existing_type,
        )
        return None

    # Merge the seed's populated handle keys onto the existing frontmatter, in
    # cleaned/canonical form (so the compiled page matches athenaeum#453's schema and the
    # registry resolves it). Only keys the seed actually populates are touched.
    existing_handles = collect_handles(existing_meta)
    changed = any(existing_handles.get(key) != value for key, value in incoming.items())

    merged_meta = dict(existing_meta)
    for key, value in incoming.items():
        merged_meta[key] = value

    existing_aliases = existing_meta.get("aliases")
    existing_tags = existing_meta.get("tags")
    entity = WikiEntity(
        uid=uid,
        type=etype,
        name=name,
        aliases=[
            str(a)
            for a in (existing_aliases if isinstance(existing_aliases, list) else [])
            if a
        ],
        access=str(existing_meta.get("access", "internal")),
        tags=[
            str(t) for t in (existing_tags if isinstance(existing_tags, list) else []) if t
        ],
        created=str(existing_meta.get("created", date.today().isoformat())),
        updated=str(existing_meta.get("updated", date.today().isoformat())),
        body=existing_body,
    )

    if not changed:
        # True no-op: the handles already match. Do not rewrite (byte-for-byte
        # stable across re-seeds), do not bump ``updated``.
        return entity, False

    updated_today = date.today().isoformat()
    merged_meta["updated"] = updated_today
    entity.updated = updated_today

    # Schema-gate the merged frontmatter before write — same guarantee tier0
    # passthrough gives. Source-handle keys ride through via extra="allow".
    validate_wiki_meta(merged_meta)

    if dry_run:
        return entity, True

    atomic_write_text(
        existing_path,
        render_frontmatter(merged_meta) + "\n" + existing_body,
    )
    return entity, True


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

    ``config`` is the resolved athenaeum.yaml dict (issue athenaeum#232) — it routes
    the ``models:`` section to the Tier 2/3 calls. ``None`` (legacy/test
    callers) keeps env > code-default model resolution.
    """
    result = ProcessingResult(raw_file=raw)

    # Sticky intake access (issue athenaeum#320 §5): an `access:` stamped on the raw
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

    # --- Tier 0 (upsert): deterministic source-handle seed onto an existing
    # entity (issue athenaeum#486). A pre-structured raw carrying athenaeum#453's source-handle
    # keys for a uid already in the wiki merges those keys onto the page's
    # frontmatter directly, instead of falling through to the LLM tiers (which
    # would flatten the handle block into prose and lose the structured schema).
    upsert = tier0_handle_upsert(
        raw,
        index,
        wiki_root,
        valid_types,
        dry_run=dry_run,
    )
    if upsert is not None:
        entity, changed = upsert
        if changed:
            log.info(
                "  T0 handle-upsert: %s → %s (source handles merged)",
                entity.name,
                entity.filename,
            )
            result.updated.append(entity.uid)
        else:
            log.info(
                "  T0 handle-upsert: %s → %s (no handle delta, no-op)",
                entity.name,
                entity.filename,
            )
        return result

    # --- Tier 1: Programmatic matching ---
    # Issue athenaeum#662: pass config so junk-name matches (here/get/main/reach/lane a
    # and operator-tuned stopwords) are filtered before they cost a tier-3 call.
    matched = tier1_programmatic_match(raw, index, config=config)
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

    # Deterministic self-resolving-document guard (issue athenaeum#300 follow-up,
    # athenaeum#304): flag embedded self-confirmation claims BEFORE any LLM stage
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
    # athenaeum#472: thread a stats object so a response that drops all entities on
    # unparseable JSON (even after the repair pass + one retry) is counted and
    # surfaced in the run summary instead of vanishing into a warning log.
    t2_stats = Tier2ParseStats()
    assert client is not None, "client required for non-dry-run"
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
        stats=t2_stats,
    )
    result.degraded += t2_stats.degraded
    result.truncated += t2_stats.truncated  # issue athenaeum#476
    log.info("  T2 classified %d new entities", len(classified))

    # Enforce the sticky intake access (issue athenaeum#320 §5) on every NEW entity the
    # LLM created from this raw: the screener's label is authoritative and is
    # never downgraded — take the more restrictive of (raw label, LLM guess).
    # Scoped to new entities only; a merge into a pre-existing page (below) does
    # not relabel that page from this one raw file.
    if sticky_access:
        from athenaeum.screening import more_restrictive

        for c in classified:
            c.access = more_restrictive(c.access, sticky_access)

    # Issue athenaeum#680: a candidate whose name is a filename/path (a code artifact)
    # must NOT become a wiki entity — the repo is the source of truth for its own
    # code, so a memory of it is stale by construction and costs a session to
    # disprove. Drop it AT CREATION, before the tier-3 create call (complementary
    # to, and no change to, athenaeum#662's read-side stopword gate).
    classified, _dropped_code = partition_code_artifact_classifications(
        classified, config
    )
    for _name in _dropped_code:
        log.info("  T3 create skipped (issue athenaeum#680, code artifact): %s", _name)

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
        atomic_write_text(page_path, rendered)
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
        # (issue athenaeum#156) sees the operator's yaml settings.
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
    """Write *clusters* to the canonical report and prune old rotations (athenaeum#311)."""
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
        except Exception as exc:  # noqa: BLE001 — rotation prune is non-fatal
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
        - ``set[str]`` when the delta path (issue athenaeum#370 PR2) engaged: the NEW
          cluster ids that were (re)clustered and written this pass, so the
          merge pass can recompile ONLY those and leave every unaffected
          ``wiki/auto-*.md`` untouched.

    ``changed_paths`` (issue athenaeum#370 PR2) is the set of absolute auto-memory paths
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

    # Issue athenaeum#370: a dry-run must not cluster at all — ``cluster_auto_memory_files``
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

    cache_dir = _resolve_cache_dir_config()
    output_path = resolve_cluster_output_path(knowledge_root, config=resolved_config)

    # Issue athenaeum#569 (H6): fold any cluster carrying a detection-incomplete marker
    # (its detector/resolver gave up after retries on a PRIOR run) into this
    # run's delta set, REGARDLESS of whether its member files changed. Live-delta
    # only re-examines clusters whose files changed, so without this a cluster
    # that hit one transient error would not be looked at again until the
    # periodic full compile (default 7 days). Unioning the marked members into
    # ``changed_paths`` lets the existing delta closure re-cluster + re-detect
    # them; merge clears each marker once the cluster is examined to completion.
    # Only meaningful on the delta path (``changed_paths is not None``); a
    # whole-corpus compile re-examines every cluster anyway.
    if changed_paths is not None:
        incomplete_members = detection_state.incomplete_member_paths(cache_dir)
        if incomplete_members:
            changed_paths = changed_paths | incomplete_members
            log.info(
                "delta: %d member file(s) across detection-incomplete cluster(s) "
                "forced into the delta set for re-detection (issue athenaeum#569)",
                len(incomplete_members),
            )

    # Issue athenaeum#370 PR2: delta-scoped cluster pass. Only reachable when a caller
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
    """Delta-scoped cluster pass (issue athenaeum#370 PR2). ``None`` = fall back to full.

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

    # Issue athenaeum#681: complete-linkage formation splits a re-clustered pool into
    # several cliques, most of which are byte-identical to their prior rows
    # (a change usually re-partitions only one corner of a component). The
    # merge pass rewrites the wiki entry for every cluster id we return here,
    # so returning the WHOLE pool would churn untouched entries (new mtime,
    # identical bytes) — diverging from a full run, which only rewrites what
    # actually changed. Return exactly the cliques that are genuinely new /
    # re-partitioned (a content-addressed id absent from the prior report) OR
    # whose membership is unchanged but contains a file whose body changed
    # (same path-derived id, new content). Everything else is left untouched.
    prior_ids = {str(row.get("cluster_id", "")) for row in prior_rows}
    changed_relpaths = {_relpath_for(p, extra_roots) for p in changed_paths}
    recompile_ids = {
        c.cluster_id
        for c in new_partial
        if c.cluster_id not in prior_ids
        or any(mp in changed_relpaths for mp in c.member_paths)
    }

    log.info(
        "delta cluster pass: %d changed file(s), %d pooled member(s) → "
        "%d affected cluster(s) re-clustered, %d changed clique(s) to recompile; "
        "%d total cluster(s) in report",
        len(changed_paths),
        len(scope.pool),
        len(new_partial),
        len(recompile_ids),
        len(spliced),
    )
    _write_cluster_report_and_prune(
        spliced, output_path, knowledge_root, resolved_config
    )
    return recompile_ids


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
    deadline: float | None = None,
    max_api_calls: int | None = None,
    full_compile_due: bool = False,
    out_delta_taken: dict[str, bool] | None = None,
    out_merge_stats: dict | None = None,
) -> list:
    """Cluster (C2) + merge (C3/C4) the auto-memory corpus. Returns the entries.

    Issue athenaeum#370 PR2: this is the single choke point for the delta-scoped compile,
    extracted from :func:`run` so the equivalence test can drive the EXACT
    orchestration on the deterministic ``client=None`` path (run's own
    pre-flight refuses a keyless ``api``-provider full pipeline, so the test
    cannot reach this logic through run()).

    Delta cadence contract (issue athenaeum#463, slice D of athenaeum#460, supersedes the
    original athenaeum#370 PR2 D5 fallback): the deterministic ``client is None`` path
    (session_end / ingest tier0, no LLM) is delta-eligible whenever
    ``librarian.delta.enabled`` allows it, unconditionally. The nightly LLM
    run — a live client — is now ALSO delta-eligible by default, gated by BOTH
    ``librarian.delta.live_client`` (:func:`athenaeum.config.
    resolve_live_delta_enabled`, default True) AND ``not full_compile_due``.
    ``full_compile_due`` is ``True`` when the periodic whole-corpus
    reconciliation cadence (:func:`athenaeum.config.
    resolve_full_compile_every_days`, default every 7 days — see :func:`run`'s
    ``full_compile_due`` computation) is due, or when the caller forces it
    (``--full-compile`` / ``full_compile=True``). This periodic full compile is
    the corpus-consistency backstop for the live-client delta path: it is the
    ONLY mechanism that re-enters TTL-decayed (athenaeum#251) auto ``not_a_conflict``
    suppressions and reconciles any drift a scoped delta merge could not see
    (the cross-scope contradiction sweep, run-global slug resolution, etc.).
    Issue athenaeum#251 TTL expiry does NOT, by itself, force affected-cluster
    re-detection on an otherwise-eligible delta night — only the scheduled
    full-compile reconciliation does. All existing delta fallbacks (F6 slug
    collision, the D2 affected-cluster/member caps inside
    :func:`_run_cluster_pass`, the empty-delta no-op) are unchanged and still
    apply on the live-client delta path exactly as they do on the deterministic
    path — any uncertainty in the delta closure still falls back to a full
    whole-corpus compile. All new params default to the whole-corpus
    behaviour, so a call with ``changed_paths=None`` (or
    ``full_compile_due=False`` with no live client) is byte-identical to the
    pre-athenaeum#370 pipeline.

    ``max_api_calls`` (issue athenaeum#461) is threaded straight through to
    :func:`athenaeum.merge.merge_clusters_to_wiki`'s C4 budget guard — see
    there for the degrade semantics. ``None`` (the default) preserves the
    pre-athenaeum#461 unbounded C4 behaviour byte-for-byte.

    ``out_delta_taken`` (issue athenaeum#463) is an optional mutable out-param
    (mirrors :func:`_raw_hash_snapshot`'s ``out_stats`` convention): when
    given, this function sets ``out_delta_taken["taken"]`` to whether the
    merge that just ran was ACTUALLY delta-scoped (``only_cluster_ids is not
    None`` at the merge call site) rather than whole-corpus. This is the one
    reliable signal for "whole-corpus ran" — it reflects every fallback
    (ineligible gate, D1-D3 inside :func:`_run_cluster_pass`, F6 slug
    collision) uniformly, unlike re-deriving it from the input arguments.
    ``run()`` uses it to decide whether to reset the full-compile cadence
    stamp. ``None`` (the default) skips the out-param write entirely.

    ``out_merge_stats`` (issue athenaeum#464, slice E of athenaeum#460) is threaded straight
    through as :func:`athenaeum.merge.merge_clusters_to_wiki`'s ``out_stats``
    out-param, so the caller gets the detector/resolver call-count breakdown
    (``haiku_calls``, ``resolve_calls``, ``chunks_run``,
    ``pairs_added_via_similarity``, ``entries_merged``,
    ``escalations_written``) without recomputing it. ``None`` (the default)
    skips the out-param write entirely.
    """
    delta_enabled = resolve_delta_enabled(config)
    live_delta_enabled = resolve_live_delta_enabled(config) and not full_compile_due
    delta_eligible = (
        not dry_run
        and changed_paths is not None
        and delta_enabled
        and (client is None or live_delta_enabled)
    )
    if changed_paths is not None and not delta_eligible and not dry_run:
        if client is not None and full_compile_due:
            log.info(
                "delta: periodic full-compile reconciliation due "
                "(librarian.full_compile_every_days) — whole-corpus compile"
            )
        elif client is not None:
            log.warning(
                "delta: live LLM client delta disabled via "
                "librarian.delta.live_client — whole-corpus compile so "
                "contradiction escalations stay corpus-consistent"
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

    # Issue athenaeum#463: report whether this compile actually took the delta path
    # (reflects every fallback uniformly — see the ``out_delta_taken`` docstring
    # above) BEFORE the merge call, since ``only_cluster_ids`` is fully settled
    # here.
    if out_delta_taken is not None:
        out_delta_taken["taken"] = only_cluster_ids is not None

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
        deadline=deadline,
        max_api_calls=max_api_calls,
        out_stats=out_merge_stats,
    )


def _run_retire(
    merged_entries: list,
    knowledge_root: Path,
    *,
    config: dict[str, object] | None,
    dry_run: bool,
    projects_root: Path | None,
):
    """Run the move-then-retire pass (issue athenaeum#261) over the merged entries.

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
    """Re-resolve open, proposal-less pending questions (issue athenaeum#188).

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

    Issue athenaeum#220. Environment override wins over the YAML setting so an
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

    Issue athenaeum#232. Mirrors :func:`librarian_max_api_calls` (athenaeum#220): the
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


def librarian_max_runtime(config: dict[str, object] | None = None) -> int:
    """Resolve the run-level wall-clock deadline (seconds) from env > config > default.

    Issue athenaeum#396. Mirrors :func:`librarian_max_files` (athenaeum#232): the
    ``ATHENAEUM_MAX_RUNTIME`` env override wins over the YAML
    ``librarian.max_runtime`` key so a cron deployment can tune the deadline
    on a single run without editing config. The ``--max-runtime`` CLI flag
    (resolved by the caller) wins over both. Non-numeric values fall back to
    :data:`DEFAULT_MAX_RUNTIME`. Unlike the budget resolvers a non-positive
    value is a VALID explicit choice — it disables the deadline entirely
    (the escape hatch for an operator who wants an unbounded run) — so it is
    returned verbatim rather than clamped to the default.
    """
    env = os.environ.get("ATHENAEUM_MAX_RUNTIME")
    if env is not None:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("max_runtime")
            # bool is an int subclass — `max_runtime: yes` in yaml must not
            # silently become a 1-second deadline.
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
    return DEFAULT_MAX_RUNTIME


def librarian_entity_runtime_share(config: dict[str, object] | None = None) -> float:
    """Resolve the entity phase's share of ``max_runtime`` from env > config > default.

    Issue athenaeum#440. Mirrors :func:`librarian_max_runtime` (athenaeum#396) in precedence, but
    the value is a FRACTION of the run deadline rather than a duration, so the
    reserve scales automatically when an operator retunes ``max_runtime``.

    Only ``0 < share < 1`` reserves anything. Any other resolved value —
    including a non-numeric string, a bool (``entity_runtime_share: yes``
    parses as ``True``, an int subclass, and must not become a 100% share), or
    an out-of-range number — disables the reserve and is returned as ``0.0``,
    restoring the pre-athenaeum#440 behaviour where the entity phase may consume the
    entire window. That is a valid explicit choice, not an error.
    """

    def _coerce(value: object) -> float | None:
        # bool is an int subclass — reject it before the numeric check.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            share = float(value)
        except (TypeError, ValueError):
            return None
        return share if 0.0 < share < 1.0 else 0.0

    env = os.environ.get("ATHENAEUM_ENTITY_RUNTIME_SHARE")
    if env is not None:
        resolved = _coerce(env)
        if resolved is not None:
            return resolved
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict) and "entity_runtime_share" in cfg:
            resolved = _coerce(cfg.get("entity_runtime_share"))
            if resolved is not None:
                return resolved
    return DEFAULT_ENTITY_RUNTIME_SHARE


def librarian_batch_mode(config: dict[str, object] | None = None) -> bool:
    """Resolve the Batch API opt-in from env > config > default off.

    Issue athenaeum#236. Mirrors :func:`librarian_max_files` (athenaeum#232): the
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


def librarian_stuck_file_threshold(config: dict[str, object] | None = None) -> int:
    """Resolve the consecutive-failure threshold before a raw file is stuck (athenaeum#663).

    Mirrors :func:`librarian_max_files` (athenaeum#232): the
    ``ATHENAEUM_STUCK_FILE_THRESHOLD`` env override wins over the yaml
    ``librarian.stuck_file_threshold`` key so a cron deployment can tune it on
    a single run. A file that has failed this many CONSECUTIVE runs on the same
    content is treated as stuck — skipped (so it stops burning the entity-phase
    budget every night) and surfaced as run state — rather than retried again.
    Must be ``>= 1`` (a threshold below 1 would quarantine a file on its very
    first transient failure, defeating the "N nights running" contract);
    non-numeric, non-positive, or bool values fall back to
    :data:`DEFAULT_STUCK_FILE_THRESHOLD`.
    """
    env = os.environ.get("ATHENAEUM_STUCK_FILE_THRESHOLD")
    if env is not None:
        try:
            value = int(env)
            if value >= 1:
                return value
        except (TypeError, ValueError):
            pass
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("stuck_file_threshold")
            # bool is an int subclass — `stuck_file_threshold: yes` in yaml must
            # not silently become a threshold of 1.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
                return raw
    return DEFAULT_STUCK_FILE_THRESHOLD


def _stuck_content_hash(raw: Any) -> str:
    """Stable short hash of a raw file's content (athenaeum#663 stuck-file ledger key).

    Keying the ledger on (ref, content-hash) means a re-edited raw file — one
    whose author fixed whatever made it time out — starts a FRESH consecutive
    count instead of inheriting the old file's stuck verdict. Best-effort: any
    read error hashes the empty string, which simply means the entry never
    matches and the file is retried (fail-open, never fail-stuck)."""
    try:
        payload = raw.content
    except Exception:  # noqa: BLE001 — a raw we cannot read is retried, not stuck
        payload = ""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _load_stuck_ledger(wiki_root: Path) -> dict[str, dict[str, Any]]:
    """Load the persistent stuck-file ledger (athenaeum#663). Missing/corrupt → empty.

    A corrupt ledger must never wedge a run — a parse error is treated as "no
    stuck files known", so at worst a genuinely-stuck file gets one more retry
    while the ledger rebuilds, never a crash."""
    path = wiki_root / STUCK_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    # Keep only well-shaped entries; drop anything a future/older schema wrote.
    return {
        ref: entry
        for ref, entry in files.items()
        if isinstance(entry, dict) and isinstance(entry.get("failures"), int)
    }


def _write_stuck_ledger(wiki_root: Path, ledger: dict[str, dict[str, Any]]) -> None:
    """Persist the stuck-file ledger (athenaeum#663), or remove it when empty.

    Written beside the deferred manifest under wiki_root so it rides the run's
    git snapshot (it is durable cross-run state, exactly like the deferred
    manifest). An empty ledger removes the file so a corpus that has recovered
    leaves no stale stuck record behind."""
    path = wiki_root / STUCK_MANIFEST_NAME
    if not ledger:
        if path.exists():
            path.unlink()
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"updated": now, "files": ledger}
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _record_stuck_failure(
    ledger: dict[str, dict[str, Any]],
    raw: Any,
    *,
    error: str,
    action: str | None,
    threshold: int,
) -> dict[str, Any] | None:
    """Increment a raw file's consecutive-failure count in the ledger (athenaeum#663).

    Keyed by ``raw.ref`` + content hash: a content change (author re-edited the
    file) resets the count. Returns the entry when this failure is the one that
    CROSSES the threshold for the first time (so the caller surfaces it exactly
    once), else ``None``. The ``escalated`` flag makes the crossing idempotent
    across runs — a file that stays stuck is not re-surfaced as "newly stuck"
    every night, only skipped."""
    key = raw.ref
    content_hash = _stuck_content_hash(raw)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = ledger.get(key)
    if not isinstance(entry, dict) or entry.get("hash") != content_hash:
        # New file, or the content changed since the last failure — fresh count.
        entry = {"hash": content_hash, "failures": 0, "first_failed": now, "escalated": False}
    entry["failures"] = int(entry.get("failures", 0)) + 1
    entry["last_failed"] = now
    entry["last_error"] = error
    if action:
        entry["last_action"] = action
    ledger[key] = entry
    if entry["failures"] >= threshold and not entry.get("escalated"):
        entry["escalated"] = True
        return entry
    return None


def _surface_newly_stuck(ctx: "RunContext", raw: Any, entry: dict[str, Any]) -> None:
    """Record + loudly log a raw file that just crossed the stuck threshold (athenaeum#663).

    Appends a machine-detectable record to ``ctx.stuck_files`` (exported to
    ``out_run_stats["stuck_files"]``) and emits the greppable
    :data:`STUCK_FILE_PREFIX` WARNING naming the file and its failing action, so
    a permanent no-progress loop surfaces the night it becomes one instead of
    only after someone notices the silent starvation."""
    ctx.stuck_files.append(
        {
            "ref": raw.ref,
            "failures": int(entry.get("failures", 0)),
            "action": entry.get("last_action"),
            "error": entry.get("last_error"),
        }
    )
    log.warning(
        "%s: %s has now failed %d consecutive run(s) on action %s (%s) — STUCK; "
        "it will be skipped until its content changes or a human intervenes "
        "(issue athenaeum#663)",
        STUCK_FILE_PREFIX,
        raw.ref,
        int(entry.get("failures", 0)),
        entry.get("last_action") or "unknown",
        entry.get("last_error") or "unknown",
    )


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
    reason: str = "budget",
) -> Path:
    """Write the deferred-work manifest after a budget- or deadline-tripped run.

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

    ``reason`` (issue athenaeum#396) selects the header wording: ``"budget"`` (the
    athenaeum#220 API-call-budget trip), ``"deadline"`` (the wall-clock deadline trip),
    or ``"entity-share"`` (issue athenaeum#440 — the entity phase yielded the rest of
    the window to the downstream C4 detector). The rest of the manifest — the
    counts and the deferred-file list — is identical either way; only the
    explanatory header differs.
    """
    path = wiki_root / DEFERRED_MANIFEST_NAME
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_deferred = len(deferred_refs) + beyond_window
    if reason == "deadline":
        header = [
            "# Deferred work — librarian run wall-clock deadline exceeded",
            "",
            "The last librarian run stopped early because the run-level",
            "wall-clock deadline (librarian.max_runtime, issue athenaeum#396) was",
            "exceeded. The raw files below were NOT processed this run; they",
            "remain on disk and the next run picks them up automatically. This",
            "file is overwritten on every tripped run and removed by the next",
            "clean run.",
        ]
    elif reason == "entity-share":
        header = [
            "# Deferred work — librarian entity phase yielded to the C4 detector",
            "",
            "The last librarian run stopped its ENTITY phase early because that",
            "phase had spent its share of the run window",
            "(librarian.entity_runtime_share, issue athenaeum#440). This is deliberate,",
            "not a failure: the remainder of the window is reserved for the",
            "auto-memory compile and the C4 contradiction detector downstream.",
            "The raw files below were NOT processed this run; they remain on",
            "disk and the next run picks them up automatically. This file is",
            "overwritten on every tripped run and removed by the next clean run.",
        ]
    else:
        header = [
            "# Deferred work — librarian run budget exhausted",
            "",
            "The last librarian run stopped early because the run-level API call",
            "budget was exhausted. The raw files below were NOT processed this",
            "run; they remain on disk and the next run picks them up",
            "automatically. This file is overwritten on every budget-tripped run",
            "and removed by the next clean run.",
        ]
    lines = [
        *header,
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
    atomic_write_text(path, "\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Issue athenaeum#464 (slice E of athenaeum#460) — permanent per-phase run summary.
#
# The athenaeum#440 nightly-cost profiling epic needs a durable, greppable record of
# where a run's wall-clock and LLM-call spend actually went. This is pure
# observability: `run()` times each phase it controls (wiki-dedup, the
# per-file entity loop, the auto-memory C2-C4 compile, retire, athenaeum#188
# reresolve) and snapshots `usage.api_calls` before/after each phase for a
# call-count delta; the auto-memory phase's detector/resolver/similarity-
# sweep breakdown comes from `merge_clusters_to_wiki`'s `out_stats` (threaded
# via `_compile_auto_memory`'s `out_merge_stats`), not from re-deriving it.
# The stable ``librarian-run-summary`` prefix lets a watchdog / log-scraper
# grep it out of a busy nightly log without parsing prose. No phase logic,
# ordering, or exit code is affected by any of this.
# ---------------------------------------------------------------------------

#: Stable prefix for the one-line, key=value, machine-greppable run summary.
RUN_SUMMARY_PREFIX = "librarian-run-summary"


def _render_schema_fragment_attribution(
    state: "dict[str, tuple[str, bool]]",
) -> str:
    """Render ``schema_fragment_state`` as one comma-joined ``name:token`` value.

    ``token`` is ``default`` when the live fragment is byte-identical to the
    bundled default, else the first 8 hex chars of its sha256 — so an operator's
    edited copy is attributable to a specific byte-state from the run log alone
    (issue athenaeum#567). ``name`` drops the redundant ``.md`` suffix; every attributed
    fragment name is otherwise free of the space/``=``/``,``/``:`` separators the
    run-summary line uses, so it stays unambiguously greppable.
    """
    pairs = []
    for fname, (sha_hex, is_default) in state.items():
        label = fname[:-3] if fname.endswith(".md") else fname
        token = "default" if is_default else sha_hex[:8]
        pairs.append(f"{label}:{token}")
    return ",".join(pairs)


def _render_run_summary(
    profile: "list[tuple[str, float, dict]]",
    *,
    schema_fragments: "dict[str, tuple[str, bool]] | None" = None,
    prompt_manifest_hash: "str | None" = None,
) -> str:
    """Render the accumulated per-phase *profile* into ONE greppable line.

    ``profile`` is an ordered list of ``(phase_name, elapsed_seconds,
    fields)`` tuples — only phases that actually ran are included (an early
    deadline trip naturally omits phases that never got a turn). ``fields``
    is a flat ``dict[str, object]`` of the extra key=value tokens to render
    for that phase (call counts, work counts); order is preserved via normal
    dict iteration so the rendered line is stable given the same input.

    Format::

        librarian-run-summary total_secs=12.3 \
            schema_fragments=observation-filter:default,_entity-template:a1b2c3d4 \
            prompt_manifest=9f8e7d6c | wiki-dedup secs=0.1 | \
            entity secs=4.2 calls=6 created=2 updated=1 escalated=0 files=3 | \
            auto-memory secs=7.8 detector_haiku=4 resolver_opus=1 \
            sweep_pairs=0 clusters_merged=2 escalations=0 | retire secs=0.1 | \
            reresolve secs=0.05 calls=0

    ``total_secs`` sums the per-phase elapsed times (NOT independently timed)
    so it is always internally consistent with the phase breakdown.

    Attribution (issue athenaeum#567) rides the head segment, right after ``total_secs``:
    ``schema_fragments=`` attributes the operator-tunable fragment bytes and
    ``prompt_manifest=`` the shipped-prompt bytes this run used. Both are
    omitted when their argument is ``None`` (the pure formatting default), so
    the pre-athenaeum#567 head and the direct unit-test callers are byte-unchanged. No
    phase logic, ordering, or exit code is affected.
    """
    total_secs = sum(secs for _phase, secs, _fields in profile)
    head = f"{RUN_SUMMARY_PREFIX} total_secs={total_secs:.3f}"
    if schema_fragments is not None:
        head += (
            f" schema_fragments={_render_schema_fragment_attribution(schema_fragments)}"
        )
    if prompt_manifest_hash is not None:
        head += f" prompt_manifest={prompt_manifest_hash}"
    parts = [head]
    for phase, secs, fields in profile:
        tokens = " ".join(f"{k}={v}" for k, v in fields.items())
        segment = f"{phase} secs={secs:.3f}"
        if tokens:
            segment += f" {tokens}"
        parts.append(segment)
    return " | ".join(parts)


@dataclass
class RunContext:
    """Mutable state threaded through the ``run()`` phase functions (athenaeum#546).

    Carries exactly the locals that used to live in ``run()``'s own frame and
    cross a ``# ---`` section boundary — resolved config, paths, the shared
    ``TokenUsage``/client, deadline state, and the entity-phase accumulators.
    Phase functions receive this BY REFERENCE and mutate its fields in place
    (never snapshot-copy it) so a later phase always observes an earlier
    phase's mutations, exactly like the original locals-in-one-frame closures
    did. ``run()`` itself is now just the ordered sequence of phase calls;
    see each phase function's docstring for what it reads/writes.

    Two fields intentionally hold small mutable containers rather than plain
    values so nested/deferred closures (``_stop_on_deadline``,
    ``_commit_partial_and_exit``) can flip them without ``nonlocal`` across a
    dataclass-method boundary: ``summary_emitted`` and ``run_profile`` are
    mutated via their own methods below.
    """

    # --- run() parameters, verbatim -------------------------------------
    raw_root: Path
    wiki_root: Path
    knowledge_root: Path
    dry_run: bool
    max_files: int | None
    max_api_calls: int | None
    max_runtime: int | None
    cluster_only: bool
    merge_only: bool
    strict_budget: bool
    batch_mode: bool | None
    retire: bool | None
    push_after_run: bool | None
    pull_before_run: bool | None
    projects_root: Path | None
    install_signal_handlers: bool
    changed_paths: set[Path] | None
    full_compile: bool
    now: datetime | None
    heartbeat: Callable[[], None] | None
    out_run_stats: dict[str, Any] | None

    # --- resolved at the top of the run ----------------------------------
    skip_entity_tiers: bool = False
    api_key: str | None = None
    config: dict[str, Any] | None = None
    provider: str = "api"
    head_at_start: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    merge_client: Any = None
    run_deadline: float | None = None
    # Issue athenaeum#440: absolute monotonic instant after which the entity phase stops
    # CLAIMING new files, reserving the remainder of ``run_deadline`` for the
    # phases downstream of it (auto-memory C2/C3 and the C4 contradiction
    # detector). ``None`` when the run deadline is disabled or the share is
    # opted out of — in both cases the entity phase behaves exactly as it did
    # before athenaeum#440 and is bounded only by ``run_deadline``.
    entity_deadline: float | None = None

    # --- per-phase profiling / summary state ------------------------------
    run_profile: list[tuple[str, float, dict]] = field(default_factory=list)
    summary_emitted: bool = False

    # --- entity-phase accumulators (issue athenaeum#461: shared with auto-memory) --
    total_created: int = 0
    total_updated: int = 0
    total_escalated: int = 0
    total_skipped: int = 0
    total_degraded: int = 0
    total_truncated: int = 0
    failed_files: list[str] = field(default_factory=list)
    deferred_refs: list[str] = field(default_factory=list)
    beyond_window: int = 0
    processed_count: int = 0
    deadline_tripped: bool = False
    # Issue athenaeum#440: the entity phase stopped on its OWN runtime share rather than
    # on the run deadline. Deliberately distinct from ``deadline_tripped``:
    # that flag skips the auto-memory block and exits 124, which is the exact
    # starvation this reserve exists to prevent. An entity-budget stop is a
    # athenaeum#220-style deferral — remaining intake is resumable, the run continues
    # into C2-C4, and it exits 0 unless a LATER phase trips the real deadline.
    entity_budget_tripped: bool = False
    raw_files: list[Any] = field(default_factory=list)
    # Issue athenaeum#663: raw files surfaced as STUCK this run — either they crossed the
    # consecutive-failure threshold this run, or they were already over it and
    # were skipped. Each entry is
    # ``{"ref", "failures", "action", "error"}``. Exported to
    # ``out_run_stats["stuck_files"]`` and counted in the entity run-profile so
    # a permanently-failing file is machine-detectable, not merely logged.
    stuck_files: list[dict[str, Any]] = field(default_factory=list)

    # --- auto-memory / retire handoff -------------------------------------
    merged_entries: list = field(default_factory=list)

    # --- finalize-phase handoff --------------------------------------------
    files_processed_count: int = 0

    def deadline_exceeded(self) -> bool:
        return self.run_deadline is not None and time.monotonic() >= self.run_deadline

    def entity_budget_exceeded(self) -> bool:
        """True once the entity phase has spent its share of the run window (athenaeum#440)."""
        return (
            self.entity_deadline is not None
            and time.monotonic() >= self.entity_deadline
        )

    def tick_heartbeat(self) -> None:
        # Issue athenaeum#526 (H10): refresh the run lock's heartbeat at phase/file
        # boundaries so ``heartbeat_age_seconds`` reflects PROGRESS, not
        # merely the acquire time.
        if self.heartbeat is not None:
            self.heartbeat()

    def export_run_stats(self) -> None:
        if self.out_run_stats is not None:
            self.out_run_stats["beyond_window"] = self.beyond_window
            self.out_run_stats["deferred_refs"] = list(self.deferred_refs)
            self.out_run_stats["failed_files"] = list(self.failed_files)
            # Issue athenaeum#663: stuck files (crossed the consecutive-failure threshold
            # or skipped because they already had) as machine-detectable state,
            # so a consumer can distinguish a permanent no-progress loop from a
            # one-off failure without parsing log text.
            self.out_run_stats["stuck_files"] = list(self.stuck_files)
            # Issue athenaeum#669: surface the entity-phase share yield (athenaeum#440) as
            # machine-detectable run state. cron-fleet#94 detects a capped run by
            # DURATION (`LIBRARIAN_CAP_DEADLINE`), which the athenaeum#440 yield made inert
            # — the entity phase now yields at its share and the run ends well
            # under the cap, so a athenaeum#440-shaped stall goes undetected. Emitting the
            # flag lets a consumer distinguish "entity yielded on purpose" from
            # "API budget exhausted" WITHOUT parsing WARNING text or the deferred
            # manifest header. The boolean alone can't judge whether the backlog
            # is growing, so the files-claimed / files-deferred counts ride
            # alongside it (this run's compiled count and the intake the yield
            # deferred). This is purely additive observability — the yield
            # BEHAVIOR from athenaeum#440 is unchanged.
            self.out_run_stats["entity_budget_tripped"] = self.entity_budget_tripped
            self.out_run_stats["entity_files_claimed"] = self.processed_count
            self.out_run_stats["entity_files_deferred"] = len(self.deferred_refs)

    def emit_run_summary(self) -> None:
        if self.summary_emitted:
            return
        self.summary_emitted = True
        # Issue athenaeum#567: attribute the operator-fragment + shipped-prompt bytes
        # this run used, on the same greppable line. Computing them touches
        # the wiki (fragment reads) and the prompt registry — neither may
        # ever change an exit code, so any failure degrades to omitting the
        # key, never raises.
        try:
            frag_state: "dict[str, tuple[str, bool]] | None" = schema_fragment_state(
                self.wiki_root
            )
        except Exception as exc:  # noqa: BLE001 — pragma: no cover - defensive; helper is hardened
            log.debug("run-summary: schema_fragment_state skipped: %s", exc)
            frag_state = None
        try:
            from athenaeum.prompt_registry import prompt_manifest_hash

            manifest_hash: "str | None" = prompt_manifest_hash()
        except Exception as exc:  # noqa: BLE001 — pragma: no cover - defensive
            log.debug("run-summary: prompt_manifest_hash skipped: %s", exc)
            manifest_hash = None
        log.info(
            "%s",
            _render_run_summary(
                self.run_profile,
                schema_fragments=frag_state,
                prompt_manifest_hash=manifest_hash,
            ),
        )

    def stop_on_deadline(self, phase: str) -> int:
        """Commit partial progress and return 124 when the deadline trips in
        a pre-entity phase — mirrors the athenaeum#337 interrupt-checkpoint path
        (greppable partial commit, exit 124, resumable). The run-lock is
        released by the CLI caller's ``finally`` on return; the deferred
        intake / un-run phases are picked up by the next run."""
        log.warning(
            "librarian: wall-clock deadline (%ds) exceeded during %s — "
            "committing partial progress and stopping (resumable, issue athenaeum#396)",
            self.max_runtime,
            phase,
        )
        if not self.dry_run:
            git_snapshot(
                self.knowledge_root,
                f"librarian: partial run (deadline {self.max_runtime}s exceeded "
                f"during {phase})",
            )
        # Issue athenaeum#464: emit the per-phase summary for whatever ran BEFORE the
        # trip — the 124 exit paths are exactly the case the athenaeum#440 profiling
        # epic most needs visibility into (a run that stopped early).
        self.emit_run_summary()
        return 124


def _run_preconditions(ctx: RunContext) -> int | None:
    """Git/config preconditions gate: provider resolution + preflight, the
    ANTHROPIC_API_KEY/wiki-root/.git existence checks. Issue athenaeum#330/#545 seam.

    Returns a nonzero exit code to short-circuit ``run()`` on failure, or
    ``None`` to continue. Mutates ``ctx.provider``.
    """
    # Issue athenaeum#330: resolve the active LLM provider (env ATHENAEUM_LLM_PROVIDER >
    # yaml llm.provider > api). A misconfigured value raises — surface it as a
    # clean run failure rather than a traceback.
    try:
        ctx.provider = resolve_provider(ctx.config)
    except ProviderConfigError as exc:
        log.error("%s", exc)
        return 1

    # Issue athenaeum#330: fail loudly at startup if the claude-cli binary is missing,
    # instead of silently deferring every file to an rc-0 no-op run.
    preflight_err = preflight_provider(ctx.provider)
    if preflight_err:
        log.error("%s", preflight_err)
        return 1

    # The ANTHROPIC_API_KEY requirement applies ONLY to the ``api`` backend.
    # The ``claude-cli`` backend authenticates via the operator's ambient
    # Claude Code subscription login and needs no key (issue athenaeum#330).
    if (
        ctx.provider == "api"
        and not ctx.api_key
        and not ctx.dry_run
        and not ctx.skip_entity_tiers
    ):
        log.error("ANTHROPIC_API_KEY not set (required unless dry_run=True)")
        return 1

    if not ctx.wiki_root.exists() and not ctx.skip_entity_tiers:
        log.error("Wiki root does not exist: %s", ctx.wiki_root)
        return 1

    if (
        not ctx.dry_run
        and not ctx.skip_entity_tiers
        and not (ctx.knowledge_root / ".git").exists()
    ):
        log.error(
            "No .git in %s — refusing to run without a writable git repo. "
            "The librarian's pre-processing snapshot is load-bearing for raw-file "
            "recovery. Either point knowledge_root at a real git repo, or pass "
            "dry_run=True to inspect without writing.",
            ctx.knowledge_root,
        )
        return 1
    return None


def _resolve_run_config(ctx: RunContext) -> int | None:
    """Resolve the several run-level config knobs, in the SAME order the
    original ``run()`` body resolved them (later resolutions — e.g. the
    batch-mode/provider capability check — depend on earlier ones, e.g.
    ``ctx.provider``). Issue athenaeum#220/#232/#396/#236/#261/#284/#399/#235 seam.

    Returns a nonzero exit code to short-circuit ``run()`` on failure
    (the batch-mode/provider incompatibility), or ``None`` to continue.
    """
    # Issue athenaeum#220: resolve the run-level API call budget (explicit arg >
    # env > yaml > default).
    if ctx.max_api_calls is None:
        ctx.max_api_calls = librarian_max_api_calls(ctx.config)

    # Issue athenaeum#232: resolve the per-run intake batch size the same way
    # (explicit arg > env > yaml > default).
    if ctx.max_files is None:
        ctx.max_files = librarian_max_files(ctx.config)

    # Issue athenaeum#396: resolve the run-level wall-clock deadline the same way
    # (explicit arg > env > yaml > default). A non-positive resolved value
    # disables the deadline (unbounded run — the explicit escape hatch).
    if ctx.max_runtime is None:
        ctx.max_runtime = librarian_max_runtime(ctx.config)

    # Issue athenaeum#236: resolve the Batch API opt-in the same way (explicit arg >
    # env > yaml > default off).
    if ctx.batch_mode is None:
        ctx.batch_mode = librarian_batch_mode(ctx.config)

    # Issue athenaeum#330/#573: batch mode is API-only — the Messages Batch API is an
    # Anthropic-endpoint feature with no ``claude`` CLI equivalent. This is now
    # a DECLARED capability (``supports_batches``) rather than an inline
    # provider-id test: reject the combination LOUDLY at startup rather than
    # silently falling back to the api backend or silently dropping the batch
    # request.
    if ctx.batch_mode and not capabilities_for(ctx.provider).supports_batches:
        log.error(
            "batch mode (ATHENAEUM_BATCH_MODE / librarian.batch_mode / "
            "--batch-mode) is incompatible with the claude-cli provider: the "
            "Messages Batch API is Anthropic-endpoint-only. Use provider=api "
            "for batch runs, or disable batch mode for the subscription backend."
        )
        return 1

    # Issue athenaeum#261/#259: resolve the move-then-retire opt-out (explicit arg >
    # yaml `librarian.retire` > default ON). When off, the retire pass is
    # skipped at both call sites below; the destructive `git rm` of raw
    # auto-memory never runs.
    if ctx.retire is None:
        ctx.retire = resolve_retire(ctx.config)
    if not ctx.retire:
        log.info(
            "retire pass disabled (librarian.retire / --no-retire) — raw "
            "auto-memory will not be moved or git-removed this run"
        )

    # Issue athenaeum#284: resolve the post-run push opt-in (explicit arg >
    # yaml `librarian.push_after_run` > default OFF). Default off so a
    # fresh install never side-effects an operator's git remote. The
    # actual push fires after the final commit, only when the run
    # produced at least one new commit and is not a dry-run.
    if ctx.push_after_run is None:
        ctx.push_after_run = resolve_push_after_run(ctx.config)

    # Issue athenaeum#399: resolve the pre-run pull opt-in the same way (explicit arg
    # > yaml `librarian.pull_before_run` > default OFF). Symmetric to the
    # push resolution above.
    if ctx.pull_before_run is None:
        ctx.pull_before_run = resolve_pull_before_run(ctx.config)

    # Issue athenaeum#235: a resolved budget of 0 is a valid defer-everything cap
    # (env/yaml zero — the CLI flag rejects it), but it is also the most
    # likely accidental misconfiguration: every LLM tier is skipped and the
    # whole intake is deferred. Flag it loudly at run start so an
    # unintended 0 is diagnosable immediately, not from the DEGRADED
    # summary at the end of the run.
    if ctx.max_api_calls == 0:
        log.warning(
            "API budget is 0 — all LLM tiers deferred this run; set "
            "ATHENAEUM_MAX_API_CALLS / librarian.max_api_calls to a "
            "positive value if unintended"
        )
    return None


def _run_git_vcs_io(ctx: RunContext) -> None:
    """Pre-run VCS I/O: optional ``git pull --ff-only``, then capture
    ``head_at_start``. Issue athenaeum#399/#284 seam.

    Must run AFTER config resolution (needs ``ctx.pull_before_run``) and
    BEFORE anything that could commit, so ``ctx.head_at_start`` reflects the
    post-pull state and the post-run push only pushes commits THIS run made.
    """
    # ``_resolve_run_config`` runs before this phase and always resolves the
    # opt-in to a concrete bool (athenaeum#546: narrows the ``bool | None`` field to
    # ``bool`` — a true post-resolution invariant, never fires for a valid run).
    assert ctx.pull_before_run is not None
    # Issue athenaeum#399: pull before capturing HEAD so (a) the run starts from
    # origin's latest and (b) head_at_start reflects the post-pull state, so
    # the existing post-run push (issue athenaeum#284) only pushes commits THIS run
    # produced, not commits picked up by the pull.
    _maybe_pull_before_run(
        ctx.knowledge_root,
        config=ctx.config,
        pull_before_run=ctx.pull_before_run,
        dry_run=ctx.dry_run,
    )

    # Issue athenaeum#284: capture HEAD at run-start (before ANY commit site fires)
    # so the post-run push can detect whether the run produced any commit
    # across librarian.git_snapshot, retire._commit_paths_if_staged, and
    # the merge-only / cluster-only early-return paths. Per-call-site
    # tracking would miss the commits inside the retire pass.
    ctx.head_at_start = _capture_head(ctx.knowledge_root) if not ctx.dry_run else None


def _arm_run_deadline(ctx: RunContext) -> None:
    """Build the shared LLM client, initialize ``usage``, and arm the
    run-level wall-clock deadline. Issue athenaeum#330/#396 seam.

    Must run after config resolution (needs ``ctx.provider``,
    ``ctx.max_runtime``) and before any phase that spends budget or checks
    the deadline.
    """
    # ``_resolve_run_config`` runs before this phase and always resolves
    # ``max_runtime`` to a concrete int (athenaeum#546: narrows ``int | None`` to
    # ``int`` — a resolved ``<= 0`` still disables the deadline below, but the
    # value is never None post-resolution, so this assert never fires).
    assert ctx.max_runtime is not None
    # One run-level TokenUsage threaded through every phase (cluster, merge
    # incl. the C4 detector + resolver, athenaeum#188 reresolve, entity tiers) so
    # ``max_api_calls`` is a genuine run-level ceiling. Earlier phases
    # increment the counter; the entity-tier loop below is the enforcement
    # point that defers remaining intake when the budget is spent.
    ctx.usage = TokenUsage()
    if ctx.provider == "claude-cli":
        # Subscription pays for the tokens (issue athenaeum#330): counts still
        # accumulate and appear in the run summary, but estimated_cost_usd
        # reports $0 instead of pricing them at API list rates.
        ctx.usage.subscription_covered = True

    # Build the shared LLM client early (issue athenaeum#330 provider seam) so both the
    # entity tiers and the C4 contradiction detector can share it. ``None`` for
    # the api backend when the key is unset (detector degrades deterministically);
    # for claude-cli it is the subscription CLI adapter. ``max_retries=3``
    # preserves the pre-athenaeum#330 api-backend construction byte-for-byte.
    ctx.merge_client = build_llm_client(ctx.config, api_key=ctx.api_key, max_retries=3)

    # Issue athenaeum#396: arm the run-level wall-clock deadline. ``run_deadline`` is an
    # absolute :func:`time.monotonic` value (or ``None`` when disabled) covering
    # every phase below — the post-compile phases AND the entity loop — so a
    # phase that stops making progress (the athenaeum#396 incident wedged ~3.5h in a
    # post-checkpoint merge subprocess holding the run-lock) is bounded instead
    # of running until externally killed. Checked at file/cluster/phase
    # boundaries; the merge pass additionally checks it inside its per-cluster
    # loops (see ``deadline=`` below) since that is where the incident wedged.
    ctx.run_deadline = (
        (time.monotonic() + ctx.max_runtime) if ctx.max_runtime > 0 else None
    )

    # Issue athenaeum#440: carve the entity phase's share out of that same window, so the
    # phases AFTER it (auto-memory C2/C3, the C4 contradiction detector) have a
    # structural budget instead of whatever the entity loop happens to leave.
    # Derived from the run deadline rather than from the entity phase's own
    # start instant on purpose: an upstream phase that ran long (the athenaeum#290
    # wiki-dedup pass) eats into the ENTITY share, never into the reserve C4
    # depends on. ``None`` whenever the run deadline is disabled or the share is
    # opted out of.
    _entity_share = librarian_entity_runtime_share(ctx.config)
    ctx.entity_deadline = (
        ctx.run_deadline - (1.0 - _entity_share) * ctx.max_runtime
        if ctx.run_deadline is not None and _entity_share > 0.0
        else None
    )

    # Issue athenaeum#530 (H2): surface truncation/deferral to callers (e.g. ingest())
    # so a max_files-truncated OR budget/deadline-deferred run — which still
    # exits 0 — is not mistaken for a fully-drained one. ``ingest`` gates its
    # stamp on these: a run that left files uncompiled must never stamp them as
    # seen, or the next ingest takes the false no-op fast path and those notes
    # are silently never compiled and never recallable. Defaults are seeded here
    # (before the merge-only / cluster-only early returns, which cannot truncate)
    # and overwritten with the true figures by ``ctx.export_run_stats()`` once
    # the entity phase has run.
    if ctx.out_run_stats is not None:
        ctx.out_run_stats.setdefault("beyond_window", 0)
        ctx.out_run_stats.setdefault("deferred_refs", [])
        ctx.out_run_stats.setdefault("failed_files", [])


def _run_wiki_dedup_phase(ctx: RunContext) -> int | None:
    """Issue athenaeum#290 wiki-page dedup pass, then the post-phase deadline check.

    Clusters compiled wiki/*.md concept/reference/principle pages against
    EACH OTHER (not against raw/auto-memory intake) and proposes merges via
    the shared wiki/_pending_merges.md sidecar. Independent of the C1-C4
    auto-memory pipeline, so it runs on every mode (full run, --cluster-only,
    --merge-only) whenever wiki/ exists — same cadence as the rest of the
    scheduled librarian pipeline. A failure here is logged and swallowed
    rather than aborting the run: this pass is diagnostic (it only appends
    human-reviewed proposals), not load-bearing for the rest of the pipeline.

    Returns 124 (via ``ctx.stop_on_deadline``) to short-circuit ``run()``
    when the deadline trips immediately after this phase, or ``None`` to
    continue.
    """
    if ctx.wiki_root.is_dir():
        _wiki_dedup_start = time.monotonic()
        try:
            from athenaeum.wiki_dedupe import propose_wiki_page_merges

            propose_wiki_page_merges(
                ctx.knowledge_root, config=ctx.config, dry_run=ctx.dry_run
            )
        except Exception:
            log.exception("wiki-page dedup pass failed; continuing run")
        finally:
            # Issue athenaeum#464: recorded even on the swallowed-exception path so the
            # summary still reflects the wall-clock this phase actually spent.
            ctx.run_profile.append(
                ("wiki-dedup", time.monotonic() - _wiki_dedup_start, {})
            )

    # Issue athenaeum#396: deadline boundary check after the athenaeum#290 wiki-dedup pass. That
    # pass swallows its own exceptions (diagnostic, non-load-bearing), so a
    # deadline raised inside it would be lost — the between-phase check here is
    # how the deadline "covers" wiki-dedup: if it ran long, the run stops now
    # rather than starting the (heavier) merge + entity phases past the cap.
    ctx.tick_heartbeat()  # issue athenaeum#526: progress past the athenaeum#290 wiki-dedup phase
    if ctx.deadline_exceeded():
        return ctx.stop_on_deadline("post-compile (after athenaeum#290 wiki-dedup)")
    return None


def _run_merge_only_phase(ctx: RunContext) -> int:
    """The ``merge_only`` early-return path: C3 merge from a prior C2 cluster
    JSONL, retire, reresolve, push, and summary emit. Issue athenaeum#461 seam.

    Only called when ``ctx.merge_only`` is True; always returns (this phase
    IS the merge-only run and always ends the run with an int exit code).
    """
    # Resolved to concrete values by ``_resolve_run_config`` before any phase
    # runs (athenaeum#546: narrows the ``... | None`` fields — never fires for a valid
    # run).
    assert ctx.max_api_calls is not None
    assert ctx.push_after_run is not None
    _merge_only_stats: dict = {}
    _merge_only_start = time.monotonic()
    try:
        ctx.merged_entries = merge_clusters_to_wiki(
            ctx.knowledge_root,
            config=ctx.config,
            dry_run=ctx.dry_run,
            client=ctx.merge_client,
            usage=ctx.usage,
            deadline=ctx.run_deadline,  # issue athenaeum#396
            max_api_calls=ctx.max_api_calls,  # issue athenaeum#461
            out_stats=_merge_only_stats,  # issue athenaeum#464
        )
    except RunDeadlineExceeded as exc:
        return ctx.stop_on_deadline(exc.phase)
    ctx.run_profile.append(
        (
            "auto-memory",
            time.monotonic() - _merge_only_start,
            {
                "detector_haiku": _merge_only_stats.get("haiku_calls", 0),
                "resolver_opus": _merge_only_stats.get("resolve_calls", 0),
                "sweep_pairs": _merge_only_stats.get(
                    "pairs_added_via_similarity", 0
                ),
                "clusters_merged": _merge_only_stats.get("entries_merged", 0),
                "escalations": _merge_only_stats.get("escalations_written", 0),
            },
        )
    )
    # Issue athenaeum#261 (slice B of athenaeum#259): move-then-retire. Non-contradictory
    # raw is moved into its wiki entry (origin-traced footnote) and git
    # rm'd; contradictory raw is held in the queue. No-op without .git.
    # Skipped entirely when retire is disabled (athenaeum#259 opt-out).
    if ctx.retire:
        _retire_start = time.monotonic()
        _retire_report = _run_retire(
            ctx.merged_entries,
            ctx.knowledge_root,
            config=ctx.config,
            dry_run=ctx.dry_run,
            projects_root=ctx.projects_root,
        )
        # Issue athenaeum#682: surface MEMORY.md pointer pruning in the run-summary.
        ctx.run_profile.append(
            (
                "retire",
                time.monotonic() - _retire_start,
                {
                    "index_pruned": (
                        len(_retire_report.index_pruned)
                        if _retire_report is not None
                        else 0
                    )
                },
            )
        )
    # Issue athenaeum#188: self-heal proposal-less open questions (a prior
    # budget-exhausted / offline run leaves raw blocks; re-resolve them
    # now that this run has budget). No-op on dry-run / offline.
    if not ctx.dry_run:
        _reresolve_start = time.monotonic()
        _reresolve_calls_before = ctx.usage.api_calls
        _run_reresolve_pass(
            ctx.knowledge_root, config=ctx.config, client=ctx.merge_client, usage=ctx.usage
        )
        ctx.run_profile.append(
            (
                "reresolve",
                time.monotonic() - _reresolve_start,
                {"calls": ctx.usage.api_calls - _reresolve_calls_before},
            )
        )
        # A merge-only run is a clean run from the manifest's
        # perspective: clear a stale deferred-work manifest left by a
        # prior budget-tripped run (v0.7.3 release-gate review).
        _clear_stale_deferred_manifest(ctx.wiki_root)
    _maybe_push_after_run(
        ctx.knowledge_root,
        config=ctx.config,
        push_after_run=ctx.push_after_run,
        dry_run=ctx.dry_run,
        head_at_start=ctx.head_at_start,
    )
    ctx.emit_run_summary()
    return 0


def _run_entity_tier_phase(ctx: RunContext) -> None:
    """The ENTITY phase (C1 raw discovery, tier1-4 routing, INCLUDING the
    Batch API fan-out branch) — issue athenaeum#461/#337/#396/#378/#236 seam.

    Issue athenaeum#461: this phase runs AHEAD of the auto-memory block (C2 cluster /
    C3 merge / C4 detect) so it gets first claim on the shared
    ``max_runtime`` deadline and ``max_api_calls`` budget. Skipped entirely
    for ``cluster_only`` (``merge_only`` already returned before this phase
    is ever reached).

    Installs (opt-in, CLI-only) the SIGTERM/SIGINT partial-commit handler
    for the duration of the per-file writing loop and restores the prior
    handlers in a ``finally`` on every exit path — the install/removal
    timing is UNCHANGED from the original inline code, just moved into this
    function unchanged. Mutates ``ctx`` accumulators
    (``total_created``/... /``processed_count``/``deadline_tripped``/
    ``raw_files``) that the finalize phase and (on a deadline trip) the
    caller's early-return both read.
    """
    # Resolved to concrete values by ``_resolve_run_config`` before this phase
    # runs (athenaeum#546: narrows the ``int | None`` budget fields — never fires for a
    # valid run).
    assert ctx.max_files is not None
    assert ctx.max_api_calls is not None
    _entity_phase_start = time.monotonic()  # issue athenaeum#464
    _entity_phase_calls_before = ctx.usage.api_calls  # issue athenaeum#464
    # Issue athenaeum#490 (slice A): snapshot output tokens too, so the entity segment
    # can render output-tokens-per-call — the one figure that makes the silent
    # full-page-echo fallback (a ~10x output-cost degrade) visible in the run
    # summary without a by-hand token-ratio calculation next time.
    _entity_phase_output_before = ctx.usage.output_tokens
    if not ctx.cluster_only:
        ctx.raw_files = discover_raw_files(ctx.raw_root)
        if not ctx.raw_files:
            # An empty entity intake is no longer a whole-run early return
            # (issue athenaeum#461): auto-memory compiles independently of raw
            # entity intake and must still run below. Only clear the stale
            # deferred-work manifest here and skip the per-file machinery;
            # the manifest-clear also happens again (harmlessly) after a
            # clean auto-memory pass, but doing it here too preserves the
            # pre-athenaeum#461 "empty intake is a clean run" contract even if the
            # auto-memory block below is skipped for some reason.
            if not ctx.dry_run:
                _clear_stale_deferred_manifest(ctx.wiki_root)
            log.info("No raw files to process. Nothing to do.")
        else:
            total_intake = len(ctx.raw_files)
            log.info("Found %d raw file(s) to process", total_intake)

            if total_intake > ctx.max_files:
                log.info(
                    "Budget cap: processing %d of %d files this run",
                    ctx.max_files,
                    total_intake,
                )
                ctx.raw_files = ctx.raw_files[: ctx.max_files]
            # Files discovery found but the max_files window excluded from
            # this run entirely. Counted into the deferred manifest on a
            # budget trip so the manifest reports the TRUE backlog, not just
            # the in-window remainder.
            ctx.beyond_window = total_intake - len(ctx.raw_files)

            schema_path = ctx.wiki_root / "_schema"
            valid_types = load_schema_list(schema_path, "types.md") or FALLBACK_TYPES
            valid_tags = load_schema_list(schema_path, "tags.md") or FALLBACK_TAGS
            valid_access = (
                load_schema_list(schema_path, "access-levels.md") or FALLBACK_ACCESS
            )

            index = EntityIndex(ctx.wiki_root)
            log.info("Loaded %d wiki entries into index", len(index))

            client = ctx.merge_client  # shared with C4 contradiction detector

            if not ctx.dry_run:
                git_snapshot(ctx.knowledge_root, "librarian: pre-processing snapshot")

            # Issue athenaeum#337: a wall-clock timeout (the pre-dawn sweep's
            # `timeout`, which SIGTERMs then, after a grace, KILLs) would
            # otherwise kill the run between the pre-processing snapshot
            # above and the terminal `processed N file(s)` commit below,
            # stranding every wiki page written so far as an uncommitted
            # tree for the NEXT run's `git add -A` snapshot to absorb under
            # a misleading "pre-processing snapshot" message. Install a
            # SIGTERM/SIGINT handler for the writing phase that commits the
            # partial progress with a distinct, greppable message and exits
            # 124 (matching coreutils `timeout`). A normally-completing run
            # restores the handlers right after the terminal commit and
            # commits exactly once, unchanged. Opt-in (CLI-only via
            # `install_signal_handlers`) so in-process callers (the MCP
            # server, tests) never have their signal handling hijacked.
            _prev_handlers: list[tuple[int, Any]] = []

            def _commit_partial_and_exit(signum: int, _frame: Any) -> None:
                log.warning(
                    "librarian: interrupted by signal %d after %d file(s) — "
                    "committing partial progress (issue athenaeum#337)",
                    signum,
                    ctx.processed_count,
                )
                # Restore first so a second signal during the commit can't
                # recurse into this handler.
                for _s, _prev in _prev_handlers:
                    signal.signal(_s, _prev)
                # Issue athenaeum#483: record whatever spend accrued before the
                # interrupt. The terminal `record_spend` (end of a clean run)
                # is skipped on this path, so without this an operator who
                # kills a run that is spending too much — or a run the spend
                # ceiling itself tripped — leaves NO ledger entry, and
                # `athenaeum spend` reports $0 for it forever. Best-effort
                # (record_spend swallows every error and no-ops when nothing
                # was spent), so it can never block the partial-progress
                # commit below or the exit.
                spend.record_spend(
                    ctx.usage,
                    run_type="librarian",
                    provider=ctx.provider,
                    files_processed=ctx.processed_count,
                )
                git_snapshot(
                    ctx.knowledge_root,
                    f"librarian: partial run (interrupted after {ctx.processed_count} "
                    f"file(s), {ctx.total_created}C {ctx.total_updated}U "
                    f"{ctx.total_escalated}E {len(ctx.failed_files)}F)",
                )
                sys.exit(124)

            if ctx.install_signal_handlers and not ctx.dry_run:
                try:
                    for _sig in (signal.SIGTERM, signal.SIGINT):
                        _prev_handlers.append(
                            (_sig, signal.signal(_sig, _commit_partial_and_exit))
                        )
                except ValueError:
                    # Not the main thread (e.g. an in-process caller) —
                    # signal handlers can't be installed here. Skip the
                    # guard rather than fail an otherwise-valid run.
                    log.debug(
                        "librarian: interrupt-commit guard skipped (not main thread)"
                    )
                    _prev_handlers = []

            # Issue athenaeum#337: the interrupt handler installed above stays active
            # through the terminal commit; the `finally` restores it on
            # EVERY exit path (normal, interrupt, or an exception from
            # `rebuild_index` / the terminal `git_snapshot`), so it can
            # never outlive the run for an in-process caller. A no-op when
            # no handler was installed (dry-run / not opt-in / not the main
            # thread).
            try:
                if ctx.batch_mode and ctx.dry_run:
                    log.info(
                        "Batch mode requested but --dry-run makes no API calls — "
                        "using the synchronous dry-run path"
                    )

                if ctx.batch_mode and not ctx.dry_run and client is not None:
                    # Issue athenaeum#236: phased fan-out via the Messages Batch API.
                    # The synchronous loop below is untouched when the flag
                    # is off. Issue athenaeum#337 note: `processed_count` is
                    # incremented only by the synchronous loop, so an
                    # interrupt during a BATCH run reports "0 file(s)" in
                    # the partial-commit message even though any pages
                    # already written are still committed by the handler's
                    # `git_snapshot` (git add -A) — the tree stays clean.
                    # Accurate batch-interrupt accounting is athenaeum#236-adjacent
                    # and out of scope for athenaeum#337 (batch mode is API-only and
                    # off for the nightly run).
                    from athenaeum.batch import process_batch_run

                    log.info(
                        "Batch mode: tier-2/tier-3 calls via the Messages Batch API"
                    )
                    outcome = process_batch_run(
                        ctx.raw_files,
                        index,
                        ctx.wiki_root,
                        client,
                        valid_types,
                        valid_tags,
                        valid_access,
                        usage=ctx.usage,
                        config=ctx.config,
                        max_api_calls=ctx.max_api_calls,
                        provider=ctx.provider,
                    )
                    ctx.total_created = outcome.created
                    ctx.total_updated = outcome.updated
                    ctx.total_escalated = outcome.escalated
                    ctx.total_skipped = outcome.skipped
                    ctx.total_degraded = outcome.degraded
                    ctx.total_truncated = outcome.truncated  # issue athenaeum#476
                    ctx.failed_files = outcome.failed_refs
                    ctx.deferred_refs = outcome.deferred_refs
                else:
                    # Issue athenaeum#663: the persistent stuck-file ledger for this
                    # phase. A raw file that has failed the same content on
                    # ``stuck_threshold`` consecutive runs is a permanent
                    # no-progress loop — it is skipped below (so it stops
                    # consuming the entity budget every night) and surfaced as
                    # run state, rather than retried identically forever.
                    stuck_ledger = _load_stuck_ledger(ctx.wiki_root)
                    stuck_threshold = librarian_stuck_file_threshold(ctx.config)
                    for i, raw in enumerate(ctx.raw_files):
                        # Issue athenaeum#526 (H10): heartbeat at every per-file boundary
                        # so a long healthy entity phase keeps the lock's
                        # heartbeat fresh and is never mistaken for wedged.
                        ctx.tick_heartbeat()
                        # Issue athenaeum#663: a file already over the stuck threshold (on
                        # unchanged content) is a known permanent failure — skip
                        # it so it never consumes an LLM call again, and surface
                        # it LOUDLY. It stays on disk (a distinct category from
                        # "deferred" and "failed") for a human to fix or remove; a
                        # content edit resets its count via the hash-keyed ledger.
                        _stuck = stuck_ledger.get(raw.ref)
                        if (
                            not ctx.dry_run
                            and _stuck is not None
                            and int(_stuck.get("failures", 0)) >= stuck_threshold
                            and _stuck.get("hash") == _stuck_content_hash(raw)
                        ):
                            ctx.stuck_files.append(
                                {
                                    "ref": raw.ref,
                                    "failures": int(_stuck.get("failures", 0)),
                                    "action": _stuck.get("last_action"),
                                    "error": _stuck.get("last_error"),
                                }
                            )
                            log.warning(
                                "%s: skipping %s — failed %d consecutive run(s) on "
                                "action %s (%s); stuck, needs a human (issue athenaeum#663)",
                                STUCK_FILE_PREFIX,
                                raw.ref,
                                int(_stuck.get("failures", 0)),
                                _stuck.get("last_action") or "unknown",
                                _stuck.get("last_error") or "unknown",
                            )
                            continue
                        if not ctx.dry_run and ctx.usage.api_calls >= ctx.max_api_calls:
                            log.warning(
                                "API call budget exhausted (%d/%d) — stopping early",
                                ctx.usage.api_calls,
                                ctx.max_api_calls,
                            )
                            # Issue athenaeum#220: everything from here on is
                            # deferred to the next run — record it so the
                            # manifest + summary surface it.
                            ctx.deferred_refs = [r.ref for r in ctx.raw_files[i:]]
                            break

                        # Issue athenaeum#396: wall-clock deadline check at the
                        # per-file boundary. Mirrors the budget-exhaustion
                        # path — defer the remaining intake and record it in
                        # the manifest — but marks the run as
                        # deadline-tripped so it exits 124 (resumable), not
                        # 0. Placed BEFORE the file's LLM work so a run
                        # already past the deadline does not start another
                        # (potentially slow) file.
                        if not ctx.dry_run and ctx.deadline_exceeded():
                            log.warning(
                                "librarian: wall-clock deadline (%ds) exceeded after "
                                "%d file(s) — deferring %d remaining file(s) and "
                                "stopping (resumable, issue athenaeum#396)",
                                ctx.max_runtime,
                                i,
                                len(ctx.raw_files) - i,
                            )
                            ctx.deferred_refs = [r.ref for r in ctx.raw_files[i:]]
                            ctx.deadline_tripped = True
                            break

                        # Issue athenaeum#440: the entity phase's OWN share of the run
                        # window is spent. Stop claiming new files and defer the
                        # rest, but do NOT set ``deadline_tripped`` -- the whole
                        # point of the reserve is that the run keeps going into
                        # the auto-memory / C4 block the entity phase has been
                        # starving. Placed AFTER the run-deadline check so a run
                        # that blew the real deadline still reports that (the
                        # more severe condition), never this.
                        if not ctx.dry_run and ctx.entity_budget_exceeded():
                            log.warning(
                                "librarian: entity phase runtime share exhausted "
                                "after %d file(s) - deferring %d remaining file(s) "
                                "and yielding the rest of the window to the "
                                "auto-memory / C4 phases (resumable, issue athenaeum#440)",
                                i,
                                len(ctx.raw_files) - i,
                            )
                            ctx.deferred_refs = [r.ref for r in ctx.raw_files[i:]]
                            ctx.entity_budget_tripped = True
                            break

                        # Issue athenaeum#378: the spend ceiling is the actual
                        # mitigation — a monitor reports after the fact,
                        # this STOPS the burn. Tokens bound the subscription
                        # path, dollars the API path. On breach we log
                        # loudly and defer the rest (never silently
                        # continue).
                        if not ctx.dry_run:
                            _ceiling = spend.ceiling_tripped(
                                ctx.usage, provider=ctx.provider, config=ctx.config
                            )
                            if _ceiling is not None:
                                log.error(
                                    "Spend ceiling reached (%s) — stopping early",
                                    _ceiling,
                                )
                                ctx.deferred_refs = [r.ref for r in ctx.raw_files[i:]]
                                break

                        log.info("Processing: %s", raw.ref)
                        try:
                            result = process_one(
                                raw,
                                index,
                                ctx.wiki_root,
                                client,
                                valid_types,
                                valid_tags,
                                valid_access,
                                dry_run=ctx.dry_run,
                                usage=ctx.usage,
                                config=ctx.config,
                            )
                        except TransientAPIError as exc:
                            # Issue athenaeum#193: the Anthropic API was overloaded
                            # (429/529) and the bounded retry was exhausted.
                            # Defer to the next run exactly like a
                            # malformed-file failure, but log it distinctly
                            # so health reporting can tell "API was
                            # overloaded" (transient) apart from "this file
                            # is broken".
                            log.error(
                                "Gave up after %d retries (transient API overload) %s: %s",
                                exc.attempts,
                                raw.ref,
                                type(exc.last_error).__name__,
                            )
                            ctx.failed_files.append(raw.ref)
                            # Issue athenaeum#663: a genuinely transient overload will NOT
                            # recur on the same file N nights running, so counting
                            # it toward "stuck" is safe — only a RELIABLY-failing
                            # file (e.g. a page large enough to time out every
                            # night; a timeout surfaces here as TransientAPIError,
                            # see provider.py) crosses the threshold.
                            if not ctx.dry_run:
                                _crossed = _record_stuck_failure(
                                    stuck_ledger,
                                    raw,
                                    error=f"TransientAPIError:{type(exc.last_error).__name__}",
                                    action=getattr(exc, "athenaeum_failing_action", None),
                                    threshold=stuck_threshold,
                                )
                                if _crossed is not None:
                                    _surface_newly_stuck(ctx, raw, _crossed)
                            continue
                        except Exception as exc:
                            log.exception("Failed to process %s", raw.ref)
                            ctx.failed_files.append(raw.ref)
                            # Issue athenaeum#663: same stuck-file accounting for a
                            # non-transient processing failure (malformed file, a
                            # persistently-failing action). The failing action is
                            # named via the annotation tier3_write set on the
                            # exception; ``None`` when the failure happened outside
                            # the tier-3 action loop.
                            if not ctx.dry_run:
                                _crossed = _record_stuck_failure(
                                    stuck_ledger,
                                    raw,
                                    error=type(exc).__name__,
                                    action=getattr(exc, "athenaeum_failing_action", None),
                                    threshold=stuck_threshold,
                                )
                                if _crossed is not None:
                                    _surface_newly_stuck(ctx, raw, _crossed)
                            continue

                        ctx.total_created += len(result.created)
                        ctx.total_updated += len(result.updated)
                        ctx.total_escalated += len(result.escalated)
                        ctx.total_skipped += len(result.skipped)
                        # athenaeum#472: ``process_one`` is a widely-stubbed test seam;
                        # tolerate a double that predates the ``degraded`` field
                        # (the real ProcessingResult always carries it, default 0).
                        ctx.total_degraded += getattr(result, "degraded", 0)
                        ctx.total_truncated += getattr(result, "truncated", 0)  # athenaeum#476

                        if not ctx.dry_run:
                            raw.path.unlink()
                            log.info("  Deleted: %s", raw.path)
                            ctx.processed_count += 1
                            # Issue athenaeum#663: this file made progress — drop any
                            # failure history so a future failure starts a fresh
                            # consecutive count rather than inheriting a stale one.
                            stuck_ledger.pop(raw.ref, None)

                    # Issue athenaeum#663: persist the updated stuck-file ledger (or remove
                    # it when empty). Durable cross-run state, committed with the
                    # run's git snapshot exactly like the deferred manifest.
                    if not ctx.dry_run:
                        _write_stuck_ledger(ctx.wiki_root, stuck_ledger)

                # Issue athenaeum#220: a budget-tripped run must be visibly DEGRADED,
                # not "Done". Exit code stays 0 (not a crash — the deferred
                # files are picked up by the next run), but the summary line
                # is machine-greppable and a manifest records exactly what
                # was deferred. A clean run clears any stale manifest left
                # by a previous tripped run.
                if ctx.deferred_refs:
                    # Issue athenaeum#396: the entity loop defers remaining intake
                    # for either reason; label the manifest + summary with
                    # the actual trigger.
                    if ctx.deadline_tripped:
                        degraded_reason = "wall-clock deadline exceeded"
                        manifest_reason = "deadline"
                    elif ctx.entity_budget_tripped:
                        # Issue athenaeum#440: distinct from both siblings -- the run is
                        # still healthy and still executing; only the entity
                        # phase stopped, on purpose, to leave C4 a window.
                        degraded_reason = "entity phase runtime share exhausted"
                        manifest_reason = "entity-share"
                    else:
                        degraded_reason = "budget exhausted"
                        manifest_reason = "budget"
                    manifest_path = _write_deferred_manifest(
                        ctx.wiki_root,
                        ctx.deferred_refs,
                        api_calls=ctx.usage.api_calls,
                        budget=ctx.max_api_calls,
                        beyond_window=ctx.beyond_window,
                        failed_refs=ctx.failed_files,
                        reason=manifest_reason,
                    )
                    log.warning(
                        "Done (DEGRADED — %s): %d created, %d updated, "
                        "%d escalated, %d skipped, %d failed, %d deferred (manifest: %s)",
                        degraded_reason,
                        ctx.total_created,
                        ctx.total_updated,
                        ctx.total_escalated,
                        ctx.total_skipped,
                        len(ctx.failed_files),
                        len(ctx.deferred_refs) + ctx.beyond_window,
                        manifest_path,
                    )
                else:
                    if not ctx.dry_run:
                        _clear_stale_deferred_manifest(ctx.wiki_root)
                    log.info(
                        "Done: %d created, %d updated, %d escalated, %d skipped, %d failed",
                        ctx.total_created,
                        ctx.total_updated,
                        ctx.total_escalated,
                        ctx.total_skipped,
                        len(ctx.failed_files),
                    )
                # Issue athenaeum#461: the run-level "Token usage:" summary log and the
                # athenaeum#378 spend-ledger write are DELIBERATELY not here. The entity
                # phase now runs BEFORE the auto-memory (C2-C4) block, and the
                # shared ``usage`` keeps accruing the C4 detector/resolver spend
                # after this point — the exact spend the athenaeum#460 epic exists to
                # observe. Recording here would drop all of it. Both moved to
                # the finalize section below so they reflect the WHOLE run.
                if not ctx.dry_run and (ctx.total_created > 0 or ctx.total_updated > 0):
                    rebuild_index(ctx.wiki_root)

                if not ctx.dry_run:
                    _processed_n = len(ctx.raw_files) - len(ctx.deferred_refs)
                    msg = (
                        f"librarian: processed {_processed_n} file(s) "
                        f"({ctx.total_created}C {ctx.total_updated}U "
                        f"{ctx.total_escalated}E {len(ctx.failed_files)}F)"
                    )
                    git_snapshot(ctx.knowledge_root, msg)
            finally:
                for _s, _prev in _prev_handlers:
                    signal.signal(_s, _prev)
                _prev_handlers = []

        # Issue athenaeum#464: recorded once for the WHOLE entity phase (not per-file)
        # — matches the profile's phase granularity. Skipped entirely when
        # ``cluster_only`` (the phase never ran, so it is absent from the
        # summary rather than a misleading zero).
        _entity_calls = ctx.usage.api_calls - _entity_phase_calls_before
        # athenaeum#490 (slice A): output tokens per entity call. A silent full-page-echo
        # fallback re-emits a whole 16-23KB page, so this figure spikes when the
        # fallback fires often — the entity-cost regression the WARNINGs above
        # now name is visible here in one number. Integer division; 0 when the
        # phase made no calls (avoids a divide-by-zero).
        _entity_out_tok_per_call = (
            (ctx.usage.output_tokens - _entity_phase_output_before) // _entity_calls
            if _entity_calls
            else 0
        )
        ctx.run_profile.append(
            (
                "entity",
                time.monotonic() - _entity_phase_start,
                {
                    "calls": _entity_calls,
                    "created": ctx.total_created,
                    "updated": ctx.total_updated,
                    "escalated": ctx.total_escalated,
                    "files": ctx.processed_count,
                    "out_tok_per_call": _entity_out_tok_per_call,
                    # athenaeum#472: only render when non-zero so a clean run's summary
                    # line is unchanged, but an operator watching a drain sees
                    # "degraded=N" (files whose classification JSON dropped
                    # every entity) without grepping warnings.
                    **({"degraded": ctx.total_degraded} if ctx.total_degraded else {}),
                    # athenaeum#476: a truncation drop (max_tokens) is surfaced
                    # separately from a parse ``degraded`` so the two are
                    # never conflated in the summary either.
                    **({"truncated": ctx.total_truncated} if ctx.total_truncated else {}),
                    # athenaeum#663: files skipped/surfaced as stuck this run. Only
                    # rendered when non-zero, so a clean run's summary line is
                    # unchanged, but a permanent no-progress loop shows "stuck=N".
                    **({"stuck": len(ctx.stuck_files)} if ctx.stuck_files else {}),
                    # athenaeum#669: the entity phase yielded its window share (athenaeum#440).
                    # Rendered only when it happened, so a clean run's summary
                    # line is unchanged, but a consumer sees the yield alongside
                    # the existing degraded/truncated/stuck flags.
                    **(
                        {"entity_budget_tripped": True}
                        if ctx.entity_budget_tripped
                        else {}
                    ),
                },
            )
        )


def _stamp_unclassified_claim_kinds(
    auto_memory_files: list[AutoMemoryFile],
    client: Any,
    config: dict[str, object] | None,
    usage: TokenUsage | None,
) -> None:
    """Stamp ``claim_kind:`` onto each not-yet-classified auto-memory file (athenaeum#742).

    Wires :func:`athenaeum.claim_kind.stamp_claim_kind` into the nightly
    intake path: the single natural point where the run already holds a live
    ``anthropic`` client (``ctx.merge_client``, shared with the C4
    contradiction detector) AND iterates every raw auto-memory file exactly
    once per run. Called from :func:`_run_auto_memory_phase` right after C1
    discovery and BEFORE the C2 cluster pass, so a freshly-stamped
    ``claim_kind`` is visible to clustering, C3 merge, and (via
    :func:`athenaeum.resolutions._stance_attribution_verdict`) the C4
    resolver in the SAME run it was stamped.

    ``stamp_claim_kind`` is itself idempotent and fail-open (see
    ``claim_kind.py``): a file that already carries a valid ``claim_kind:``
    is skipped with NO LLM call (an author-supplied value is never
    overwritten), and a classification failure leaves the file unstamped
    rather than raising. This wrapper additionally short-circuits on
    ``am.claim_kind`` (already populated by :func:`discover_auto_memory_files`
    from the on-disk frontmatter) so an already-classified file costs not
    even a frontmatter re-read.

    On a successful stamp, updates ``am.claim_kind`` on the (mutable)
    in-memory :class:`AutoMemoryFile` record directly — cheaper than
    re-running discovery, and the in-memory record is what clustering/merge/
    resolution consume for the rest of this run.

    No-op when ``client`` is ``None`` (dry-run / keyless run) or the list is
    empty. Never raises: a per-file stamp failure is logged by
    ``stamp_claim_kind`` itself and simply leaves that file unclassified.
    ``getattr(am, "claim_kind"/"path", ...)`` (rather than direct attribute
    access) tolerates the ``SimpleNamespace(origin_scope=...)`` doubles
    several pre-existing budget/deadline tests substitute for
    ``discover_auto_memory_files`` — those tests exercise unrelated run-loop
    machinery and never intended to opt into claim_kind stamping; a bare
    double is treated the same as an already-classified/unpathed record
    (skipped, no call, no crash).
    """
    if client is None or not auto_memory_files:
        return
    # Lazy import (issue athenaeum#742 AC): keeps the claim_kind classifier — and the
    # athenaeum.llm_schemas / pydantic weight it pulls in via observe_claim_kind
    # — off every import path that does not reach this run-loop phase,
    # matching the deferred-import pattern already used for
    # athenaeum.batch.process_batch_run just above in this module. In
    # particular this must NEVER be imported at athenaeum.librarian module
    # scope, since librarian is reachable (indirectly) from CLI startup.
    from athenaeum.claim_kind import stamp_claim_kind

    stamped = 0
    for am in auto_memory_files:
        if getattr(am, "claim_kind", ""):
            continue
        path = getattr(am, "path", None)
        if path is None:
            continue
        kind = stamp_claim_kind(path, client, config=config, usage=usage)
        if kind:
            am.claim_kind = kind
            stamped += 1
    if stamped:
        log.info("claim_kind: stamped %d previously-unclassified auto-memory file(s)", stamped)


def _run_auto_memory_phase(ctx: RunContext) -> int | None:
    """The auto-memory block: C1 discover + C2 cluster / C3 merge / C4
    detect, the post-compile deadline check, retire, and athenaeum#188 reresolve.
    Issue athenaeum#461/#463/#396/#261/#188 seam.

    Gated on ``not ctx.deadline_tripped`` by the caller — if the entity loop
    already tripped the wall-clock deadline, this phase must not run at all
    (mirrors the original inline ``if not deadline_tripped:`` guard).

    Returns a nonzero exit code (via ``ctx.stop_on_deadline`` on a
    mid-compile deadline trip) to short-circuit ``run()``, or ``None`` to
    continue. Mutates ``ctx.merged_entries`` for the caller's cluster_only
    early return check (cluster_only never reaches merged_entries use) and
    reads it isn't needed there — retire below is the only consumer.
    """
    # C1 + C2: auto-memory discovery followed by the C2 cluster pass.
    # Clustering must run BEFORE any tier routing so that downstream C3
    # merge has a fresh grouping to consume. Scope identity is preserved
    # on each record so the tier pipeline and the cluster pass both see
    # the same routing key.
    auto_memory_files = discover_auto_memory_files(ctx.knowledge_root, config=ctx.config)
    if not auto_memory_files:
        return None
    by_scope: dict[str, int] = {}
    for am in auto_memory_files:
        by_scope[am.origin_scope] = by_scope.get(am.origin_scope, 0) + 1
    log.info(
        "Discovered %d auto-memory file(s) across %d scope(s)",
        len(auto_memory_files),
        len(by_scope),
    )
    if ctx.dry_run:
        for scope, count in sorted(by_scope.items()):
            log.info(
                "  [DRY RUN] auto-memory scope %s: %d file(s)", scope, count
            )
    else:
        # Issue athenaeum#742: stamp claim_kind on every not-yet-classified member
        # BEFORE clustering, so the freshly-stamped kind is visible to C2/C3
        # and to the C4 resolver's opinion-attribution short-circuit
        # (resolutions._stance_attribution_verdict) in this SAME run. No-op
        # (no LLM call, no write) when the run has no client (dry-run/keyless)
        # — mirrors every other LLM-bearing step in this phase, which is
        # already skipped above for dry-run.
        _stamp_unclassified_claim_kinds(
            auto_memory_files, ctx.merge_client, ctx.config, ctx.usage
        )

    # Issue athenaeum#463 (slice D of athenaeum#460): the nightly run's own delta
    # baseline. A caller that already threads an explicit
    # ``changed_paths`` (ingest / session_end, issue athenaeum#370 PR2) is left
    # untouched — this only computes a baseline when the run wasn't
    # already given one, so every existing caller's behaviour is
    # unaffected. ``full_compile_due`` gates the live-client delta
    # path (see :func:`_compile_auto_memory`); ``run_changed_paths``
    # is threaded through instead of the raw ``changed_paths`` arg so
    # the manifest-stamp write below can tell whether THIS run
    # computed its own baseline.
    run_changed_paths = ctx.changed_paths
    full_compile_due = ctx.full_compile
    auto_memory_manifest_path = _resolve_cache_dir(None) / AUTO_MEMORY_MANIFEST_NAME
    full_compile_stamp_path = _resolve_cache_dir(None) / FULL_COMPILE_STAMP_NAME
    run_now = ctx.now if ctx.now is not None else datetime.now(timezone.utc)
    if not ctx.dry_run and not ctx.cluster_only and ctx.changed_paths is None:
        try:
            run_changed_paths = _auto_memory_changed_paths(
                auto_memory_files, ctx.knowledge_root, auto_memory_manifest_path
            )
        except Exception as exc:  # noqa: BLE001 — stamp read must not break the run
            log.warning(
                "auto-memory delta baseline read failed (non-fatal, "
                "falling back to whole-corpus): %s",
                exc,
            )
            run_changed_paths = None
        if not full_compile_due:
            try:
                full_compile_every_days = resolve_full_compile_every_days(ctx.config)
                stamp = _load_full_compile_stamp(full_compile_stamp_path)
                if stamp is None:
                    full_compile_due = True
                else:
                    stamp_at = datetime.strptime(
                        stamp["at"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    age_days = (run_now - stamp_at).total_seconds() / 86400.0
                    full_compile_due = age_days >= full_compile_every_days
            except Exception as exc:  # noqa: BLE001 — must not break the run
                log.warning(
                    "full-compile stamp read failed (non-fatal, forcing "
                    "whole-corpus reconciliation this run): %s",
                    exc,
                )
                full_compile_due = True

    # C2 + C3 + C4: cluster, merge, and detect. Issue athenaeum#370 PR2 threads the
    # optional ``changed_paths`` delta through this one call — see
    # :func:`_compile_auto_memory` for the delta-eligibility gate (issue
    # athenaeum#463 cadence contract), the cluster pass, the F6 slug-collision guard,
    # and the merge. Issue athenaeum#396: ``deadline`` is threaded into the merge
    # pass's per-cluster loops (the athenaeum#396 wedge site); a trip there raises
    # RunDeadlineExceeded, caught here.
    _delta_taken_out: dict[str, bool] = {}
    _merge_stats: dict = {}  # issue athenaeum#464
    _auto_memory_start = time.monotonic()  # issue athenaeum#464
    try:
        ctx.merged_entries = _compile_auto_memory(
            auto_memory_files,
            ctx.knowledge_root,
            config=ctx.config,
            dry_run=ctx.dry_run,
            client=ctx.merge_client,
            usage=ctx.usage,
            changed_paths=run_changed_paths,
            deadline=ctx.run_deadline,
            max_api_calls=ctx.max_api_calls,  # issue athenaeum#461
            full_compile_due=full_compile_due,  # issue athenaeum#463
            out_delta_taken=_delta_taken_out,  # issue athenaeum#463
            out_merge_stats=_merge_stats,  # issue athenaeum#464
        )
    except RunDeadlineExceeded as exc:
        # Issue athenaeum#464: record the auto-memory phase's partial elapsed
        # time (and whatever detector/resolver counts landed in
        # ``_merge_stats`` before the trip — usually none, since the
        # merge call raised, but this stays correct either way)
        # before the deadline-stop path emits the summary.
        ctx.run_profile.append(
            (
                "auto-memory",
                time.monotonic() - _auto_memory_start,
                {
                    "detector_haiku": _merge_stats.get("haiku_calls", 0),
                    "resolver_opus": _merge_stats.get("resolve_calls", 0),
                    "sweep_pairs": _merge_stats.get(
                        "pairs_added_via_similarity", 0
                    ),
                    "clusters_merged": _merge_stats.get("entries_merged", 0),
                    "escalations": _merge_stats.get(
                        "escalations_written", 0
                    ),
                },
            )
        )
        return ctx.stop_on_deadline(exc.phase)
    ctx.run_profile.append(
        (
            "auto-memory",
            time.monotonic() - _auto_memory_start,
            {
                "detector_haiku": _merge_stats.get("haiku_calls", 0),
                "resolver_opus": _merge_stats.get("resolve_calls", 0),
                "sweep_pairs": _merge_stats.get(
                    "pairs_added_via_similarity", 0
                ),
                "clusters_merged": _merge_stats.get("entries_merged", 0),
                "escalations": _merge_stats.get("escalations_written", 0),
            },
        )
    )

    # Issue athenaeum#463: on a successful (no deadline trip, not dry_run)
    # auto-memory compile that computed its OWN delta baseline (i.e.
    # not an ingest/session_end call that threads its own
    # ``changed_paths``), refresh the auto-memory manifest stamp so
    # the next nightly run's delta baseline is fresh. When the compile
    # that just ran was ACTUALLY whole-corpus (``out_delta_taken`` —
    # covers every path: an ineligible gate, ``full_compile_due``, and
    # any mid-flight fallback such as D1-D3/F6 that made
    # ``_compile_auto_memory`` fall back internally even though the
    # gate looked delta-eligible), also reset the full-compile cadence
    # stamp. A delta compile must NOT reset the full-compile stamp —
    # only a real whole-corpus pass resets the cadence clock.
    # Best-effort: a write failure never breaks the run (mirrors the
    # ingest manifest's tolerance).
    if not ctx.dry_run and not ctx.cluster_only and ctx.changed_paths is None:
        try:
            current_snapshot = _auto_memory_hash_snapshot(
                auto_memory_files, ctx.knowledge_root
            )
            _write_auto_memory_manifest(
                auto_memory_manifest_path, current_snapshot
            )
        except Exception as exc:  # noqa: BLE001 — stamp write must not break the run
            log.warning(
                "auto-memory delta baseline write failed (non-fatal): %s", exc
            )
        if not _delta_taken_out.get("taken", False):
            try:
                _write_full_compile_stamp(
                    full_compile_stamp_path,
                    run_now,
                    _capture_head(ctx.knowledge_root),
                )
            except Exception as exc:  # noqa: BLE001 — must not break the run
                log.warning(
                    "full-compile stamp write failed (non-fatal): %s", exc
                )

    # Issue athenaeum#396: deadline check at the post-compile phase boundary,
    # before the retire + reresolve passes (both can commit / make
    # LLM calls).
    ctx.tick_heartbeat()  # issue athenaeum#526: progress into the retire/reresolve phase
    if ctx.deadline_exceeded():
        return ctx.stop_on_deadline("post-compile (before retire/reresolve)")

    # Issue athenaeum#261 (slice B of athenaeum#259): move-then-retire lifecycle. Runs
    # after merge + C4 detection. Non-contradictory raw is moved
    # into its wiki entry (origin-traced footnote) and git rm'd;
    # contradictory raw is held for human confirmation. Skipped for
    # the cluster_only diagnostic mode, when retire is disabled
    # (athenaeum#259 opt-out), and a no-op without a git repo.
    if ctx.retire and not ctx.cluster_only:
        _retire_start = time.monotonic()  # issue athenaeum#464
        _retire_report = _run_retire(
            ctx.merged_entries,
            ctx.knowledge_root,
            config=ctx.config,
            dry_run=ctx.dry_run,
            projects_root=ctx.projects_root,
        )
        ctx.run_profile.append(
            (
                "retire",
                time.monotonic() - _retire_start,
                # Issue athenaeum#682: surface MEMORY.md pointer pruning in the
                # run-summary so a pruning event is visible in the same
                # greppable line as every other phase result.
                {
                    "index_pruned": (
                        len(_retire_report.index_pruned)
                        if _retire_report is not None
                        else 0
                    )
                },
            )
        )

    # Issue athenaeum#188: re-resolve open, proposal-less pending questions
    # so a prior cap-hit / offline escalation self-heals on this
    # (budgeted) run.
    if not ctx.dry_run:
        _reresolve_start = time.monotonic()  # issue athenaeum#464
        _reresolve_calls_before = ctx.usage.api_calls
        _run_reresolve_pass(
            ctx.knowledge_root, config=ctx.config, client=ctx.merge_client, usage=ctx.usage
        )
        ctx.run_profile.append(
            (
                "reresolve",
                time.monotonic() - _reresolve_start,
                {"calls": ctx.usage.api_calls - _reresolve_calls_before},
            )
        )
    return None


def _run_finalize_phase(ctx: RunContext) -> int:
    """Finalize: run-level spend summary + athenaeum#378 ledger write, post-run push,
    the athenaeum#310 page-size guardrail, the athenaeum#481 pending-merge revalidation
    advisor, the summary emit, the athenaeum#470 backlog-drain advisory, and the
    terminal return-code selection. Issue athenaeum#461/#378/#284/#310/#481/#464/
    athenaeum#470/#396/#227 seam.

    This is the LAST phase; every remaining ``run()`` return code (124 for a
    deadline trip, 1 for failed files, 1 for ``strict_budget``, else 0) is
    decided here, in the same precedence order as the original inline code.
    """
    # Resolved to a concrete bool by ``_resolve_run_config`` before any phase
    # runs (athenaeum#546: narrows ``bool | None`` — never fires for a valid run).
    assert ctx.push_after_run is not None
    # Issue athenaeum#461: run-level spend summary + athenaeum#378 ledger write, moved here from
    # the (now-earlier) entity phase so ``usage`` reflects BOTH phases — the
    # entity tiers AND the auto-memory C2-C4 detector/resolver spend that
    # accrues after the entity loop. Recording inside the entity phase (its
    # pre-athenaeum#461 home, when it ran LAST) would silently undercount every run by
    # the entire C4 cost, defeating the observability the athenaeum#460 epic needs.
    # Kept after the merge_only/cluster_only early returns, matching the
    # pre-athenaeum#461 placement (those paths never recorded run spend). Best-effort
    # (athenaeum#378): never breaks the run; skipped on dry-run (counters are zero).
    if ctx.usage.api_calls > 0:
        log.info(
            "Token usage: %d API calls, %d input + %d output = %d total"
            " (cache: %d written, %d read) (~$%.4f estimated)",
            ctx.usage.api_calls,
            ctx.usage.input_tokens,
            ctx.usage.output_tokens,
            ctx.usage.total_tokens,
            ctx.usage.cache_creation_input_tokens,
            ctx.usage.cache_read_input_tokens,
            ctx.usage.estimated_cost_usd,
        )
    # Issue athenaeum#470: files actually drained this run (removed from intake) — the
    # in-window count minus what was deferred (budget/deadline/ceiling trip) and
    # what failed (not consumed). Recorded on the ledger so the backlog-drain
    # advisor can read observed files-per-run throughput across runs.
    ctx.files_processed_count = max(
        0, len(ctx.raw_files) - len(ctx.deferred_refs) - len(ctx.failed_files)
    )
    if not ctx.dry_run:
        # Issue athenaeum#568 (H1): do NOT discard record_spend's return. When this run
        # actually spent budget and the ledger is enabled, a False return means
        # the append FAILED (spend.record_spend logs the cause at WARNING) — the
        # cumulative drain ceiling (drain.run_drain) and the athenaeum#487 cross-repo
        # accounting contract both re-read this ledger, so an unrecorded run
        # makes them silently under-count. Surface it loudly at the run level.
        _ledger_written = spend.record_spend(
            ctx.usage,
            run_type="librarian",
            provider=ctx.provider,
            files_processed=ctx.files_processed_count,
        )
        if not _ledger_written and (ctx.usage.api_calls > 0 or ctx.usage.total_tokens > 0):
            from athenaeum.config import resolve_spend_ledger_enabled

            if resolve_spend_ledger_enabled(ctx.config):
                log.warning(
                    "spend ledger did NOT record this librarian run despite "
                    "%d API call(s) / %d token(s) spent — cumulative spend "
                    "ceilings and the athenaeum#487 cross-repo accounting contract will "
                    "under-count this run (issue athenaeum#568)",
                    ctx.usage.api_calls,
                    ctx.usage.total_tokens,
                )

    _maybe_push_after_run(
        ctx.knowledge_root,
        config=ctx.config,
        push_after_run=ctx.push_after_run,
        dry_run=ctx.dry_run,
        head_at_start=ctx.head_at_start,
    )

    # Issue athenaeum#310: warn-only page-size guardrail. Log a WARNING for each wiki
    # entity page over the flag threshold so a nightly run surfaces pages that
    # want splitting into linked sub-entities. Never fatal, never mutating —
    # any failure here degrades to a single non-fatal note. The split-proposal
    # workflow is explicitly out of scope (issue athenaeum#310, moscow:could).
    try:
        from athenaeum.config import resolve_page_flag_bytes, resolve_page_warn_bytes
        from athenaeum.status import scan_page_sizes

        _pw_bytes = resolve_page_warn_bytes(ctx.config)
        _pf_bytes = resolve_page_flag_bytes(ctx.config)
        _, _pages_flag = scan_page_sizes(ctx.wiki_root, _pw_bytes, _pf_bytes)
        # Issue athenaeum#490 (slice A) / athenaeum#310: aggregate into ONE health-signal count
        # line rather than one WARNING per page — a corpus with ~35 oversized
        # pages previously buried the rest of the nightly log under 35 near-
        # identical lines. The count leads (the greppable health signal); the
        # per-page names/sizes trail on the same line so a purge stays
        # auditable. Emitted only when at least one page is over the flag.
        if _pages_flag:
            log.warning(
                "oversized wiki pages: %d over flag %d bytes — consider "
                "splitting into linked sub-entities: %s",
                len(_pages_flag),
                _pf_bytes,
                ", ".join(
                    f"{_name} ({_size}B)"
                    for _name, _size in sorted(
                        _pages_flag, key=lambda p: p[1], reverse=True
                    )
                ),
            )
    except Exception as exc:  # noqa: BLE001 — guardrail must never break a run
        log.warning("page-size guardrail check failed (non-fatal): %s", exc)

    # Issue athenaeum#481: pending-merge revalidation advisor. athenaeum#480 stopped NEW
    # degenerate over-cluster proposals from being written; this surfaces
    # entries queued BEFORE the athenaeum#400/#421 gate tightened that the pipeline
    # would never propose today. Runs in DRY-RUN here (never mutates the queue
    # unprompted) so a withdrawn-and-regrown queue's junk is visible from the
    # first night, and names the one-command ``athenaeum merges revalidate
    # --apply`` remedy. Best-effort: never breaks a run.
    if not ctx.dry_run:
        try:
            from athenaeum.pending_merges import revalidate_pending_merges

            _merges_path = ctx.wiki_root / "_pending_merges.md"
            _reval = revalidate_pending_merges(
                _merges_path, config=ctx.config, apply=False
            )
            if _reval.retired:
                log.warning(
                    "pending-merge queue: %d unresolved proposal(s) the current "
                    "suppression gate would retire (queued before the gate "
                    "tightened) — run `athenaeum merges revalidate --apply` to "
                    "archive them",
                    len(_reval.retired),
                )
        except Exception as exc:  # noqa: BLE001 — advisor must never break a run
            log.warning("pending-merge revalidation advisor failed (non-fatal): %s", exc)

    # Issue athenaeum#464: normal finalize path — every return below this point
    # (the entity-loop deadline_tripped 124, the failed-files 1, the
    # strict-budget 1, and the clean 0) shares this one emit. `_emit_run_summary`
    # is idempotent (`_summary_emitted` guard), so this is safe even though
    # `_stop_on_deadline` above already emits on its own early-return paths —
    # those paths `return` before reaching here, so in practice this only ever
    # fires once per run.
    ctx.emit_run_summary()

    # Issue athenaeum#470: backlog-drain ETA advisor. At the end of any real run that
    # leaves raw intake undrained, project time-to-drain from OBSERVED
    # throughput (the athenaeum#378 ledger — including THIS run's record just written
    # above) and WARN when it exceeds ``librarian.drain_warn_days``, naming the
    # one-command ``athenaeum drain`` remedy. Uses the TRUE remaining backlog
    # (live intake count), so it also catches a run that cleanly processed its
    # ``max_files`` window but left files beyond it — the silent-backlog-growth
    # case the DEGRADED summary never surfaced. Best-effort: never breaks a run.
    if not ctx.dry_run and not ctx.cluster_only:
        try:
            from athenaeum.config import resolve_drain_warn_days
            from athenaeum.drain_advisor import build_advisory

            _advisory = build_advisory(
                backlog=len(discover_raw_files(ctx.raw_root)),
                ledger_records=spend.read_ledger(spend.resolve_ledger_path(ctx.config)),
                warn_days=resolve_drain_warn_days(ctx.config),
                this_run_files=ctx.files_processed_count,
                config=ctx.config,
            )
            if _advisory is not None:
                log.warning("%s", _advisory.line)
        except Exception as exc:  # noqa: BLE001 — advisor must never break a run
            log.debug(
                "backlog-drain advisor skipped (%s): %s", type(exc).__name__, exc
            )

    # Issue athenaeum#396: the entity loop hit the wall-clock deadline and deferred the
    # remaining intake. The partial progress is committed (terminal commit
    # above) and the deferred files are picked up by the next run — exit 124
    # (matching coreutils `timeout` and the athenaeum#337 interrupt path) so the trip is
    # a distinct, resumable non-zero signal rather than a silent success. Takes
    # precedence over the failed-files / strict-budget codes below: a deadline
    # trip is the more actionable signal.
    # Issue athenaeum#530 (H2): export the final truncation/deferral figures before ANY
    # of the entity-phase exit paths so a caller (ingest) can tell a fully
    # drained run from a partial one regardless of exit code.
    ctx.export_run_stats()

    if ctx.deadline_tripped:
        log.warning(
            "librarian: run stopped at the wall-clock deadline — exiting 124 "
            "(partial progress committed, remaining intake resumable next run)"
        )
        return 124

    if ctx.failed_files:
        log.warning("Failed files (will retry next run): %s", ", ".join(ctx.failed_files))
        return 1

    # Issue athenaeum#227: opt-in strict mode for exit-code-based alerting. The
    # default stays 0 (a trip is not a crash — the next run picks the
    # deferred files up), but operators who alert on exit codes can ask
    # for a nonzero exit when the budget tripped.
    if ctx.deferred_refs and ctx.strict_budget:
        log.warning("strict_budget: budget-tripped run — exiting nonzero")
        return 1

    return 0


def run(
    raw_root: Path = DEFAULT_RAW_ROOT,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    dry_run: bool = False,
    max_files: int | None = None,
    max_api_calls: int | None = None,
    max_runtime: int | None = None,
    cluster_only: bool = False,
    merge_only: bool = False,
    strict_budget: bool = False,
    batch_mode: bool | None = None,
    retire: bool | None = None,
    push_after_run: bool | None = None,
    pull_before_run: bool | None = None,
    projects_root: Path | None = None,
    install_signal_handlers: bool = False,
    changed_paths: set[Path] | None = None,
    full_compile: bool = False,
    now: datetime | None = None,
    heartbeat: Callable[[], None] | None = None,
    out_run_stats: dict[str, Any] | None = None,
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

    ``max_api_calls`` is the run-level API call budget (issue athenaeum#220). When
    ``None`` (the default) it resolves via env ``ATHENAEUM_MAX_API_CALLS`` >
    yaml ``librarian.max_api_calls`` > :data:`DEFAULT_MAX_API_CALLS`. An
    explicit value (e.g. from the CLI flag) wins over all three.

    ``max_runtime`` is the run-level wall-clock deadline in seconds (issue
    athenaeum#396). When ``None`` (the default) it resolves via env
    ``ATHENAEUM_MAX_RUNTIME`` > yaml ``librarian.max_runtime`` >
    :data:`DEFAULT_MAX_RUNTIME`; an explicit value (e.g. from the CLI
    ``--max-runtime`` flag) wins. It bounds the WHOLE run — the post-compile
    phases (C4 contradiction detector, athenaeum#290 wiki-dedup, C3 merge/resolver)
    AND the per-file entity loop — checked at file/cluster/phase boundaries.
    On trip the run commits partial progress, releases the lock (via the CLI
    caller's ``finally``), and exits ``124`` (matching coreutils ``timeout``
    and the athenaeum#337 interrupt-checkpoint path) — resumable: the deferred intake
    and any un-run phases are picked up by the next run. A resolved value of
    ``<= 0`` disables the deadline entirely (unbounded run, the escape hatch).

    ``strict_budget`` (issue athenaeum#227) makes a budget-tripped (DEGRADED) run
    return 1 instead of the default 0, for exit-code-based alerting (e.g.
    the CLI ``--strict-budget`` flag). All other DEGRADED-path behavior —
    warning summary, deferred-work manifest, git snapshot — is unchanged.

    ``batch_mode`` (issue athenaeum#236) routes the entity-tier LLM calls through
    the Anthropic Messages Batch API (50% token discount, latency-tolerant)
    instead of the synchronous per-file loop. When ``None`` (the default)
    it resolves via env ``ATHENAEUM_BATCH_MODE`` > yaml
    ``librarian.batch_mode`` > off; an explicit value (e.g. from the CLI
    ``--batch-mode`` flag) wins over both. Off keeps the synchronous path
    untouched; dry-run always uses the synchronous (call-free) path. See
    :mod:`athenaeum.batch` for phase layout and budget semantics.

    ``retire`` (issue athenaeum#261) opts out of the move-then-retire pass. DEFAULT
    ON (owner-confirmed): when ``None`` it resolves via yaml
    ``librarian.retire`` (default on); an explicit ``False`` (e.g. from the
    CLI ``--no-retire`` flag) wins. When off, the retire pass is skipped
    entirely — non-contradictory raw auto-memory is neither moved into the
    wiki nor ``git rm``'d, so the raw stays in the intake queue.

    ``push_after_run`` (issue athenaeum#284) opts INTO a post-run ``git push`` that
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

    ``pull_before_run`` (issue athenaeum#399) opts INTO a pre-run ``git pull
    --ff-only --autostash`` that starts the run from origin's latest instead
    of a possibly-stale local checkout. DEFAULT OFF: when ``None`` it
    resolves via yaml ``librarian.pull_before_run`` (default off); an
    explicit ``True`` (e.g. from the CLI ``--pull`` flag) wins. When on AND
    it is not a ``--dry-run``, the librarian invokes ``git pull`` (remote/
    branch from the SAME ``librarian.push_remote`` / ``librarian.push_branch``
    resolvers the push uses, defaulting to ``origin`` and the current
    branch's upstream) immediately before capturing ``head_at_start``, so
    the post-run push only pushes commits this run produced. A pull failure
    — including a diverged history ``--ff-only`` refuses to fast-forward —
    is logged as a non-fatal warning and the run proceeds against the local
    tree; ``--autostash`` protects the librarian's routine dirty-tree
    starting state. Athenaeum performs no credential handling; the
    operator's ambient git auth is used.

    ``full_compile`` (issue athenaeum#463, slice D of athenaeum#460, CLI ``--full-compile``)
    forces a whole-corpus auto-memory compile regardless of the delta gate or
    the ``librarian.full_compile_every_days`` cadence — the manual escape
    hatch for an operator who wants an immediate full reconciliation. DEFAULT
    ``False``. Only meaningful for a real (non-``cluster_only``/``merge_only``,
    non-dry-run) compile; see the ``full_compile_due`` computation ahead of
    the auto-memory block for the full cadence contract (also driven by the
    ``FULL_COMPILE_STAMP_NAME`` cache-dir stamp and
    :func:`athenaeum.config.resolve_full_compile_every_days`, default 7 days).

    ``now`` (issue athenaeum#463) is an optional injected "run start" timestamp for
    the full-compile cadence check, mirroring
    :func:`athenaeum.merge.merge_clusters_to_wiki`'s ``now=`` parameter.
    Defaults to ``datetime.now(timezone.utc)`` (frozen once here); tests pass
    a fixed value so no wall-clock leaks into cadence assertions.
    """
    # Issue athenaeum#540 (M25): stamp a fresh per-run correlation id so every log line
    # this run emits carries the same id (via the logconf run-id filter) — even
    # in a long-lived process that performs several runs — making a run's lines
    # attributable and untangleable from an overlapping run's.
    from athenaeum.logconf import new_run_id

    new_run_id()

    # Issue athenaeum#546: run() is now the ORDERED SEQUENCE of named phase calls over
    # one shared, mutable RunContext (see the class docstring above) — a
    # future phase reorder (issue athenaeum#461-style) is a code change here, not an
    # inline comment. Every phase function mutates `ctx` in place; nothing is
    # snapshot-copied, so a later phase always observes an earlier phase's
    # mutations exactly as the original single-frame locals did.
    ctx = RunContext(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=dry_run,
        max_files=max_files,
        max_api_calls=max_api_calls,
        max_runtime=max_runtime,
        cluster_only=cluster_only,
        merge_only=merge_only,
        strict_budget=strict_budget,
        batch_mode=batch_mode,
        retire=retire,
        push_after_run=push_after_run,
        pull_before_run=pull_before_run,
        projects_root=projects_root,
        install_signal_handlers=install_signal_handlers,
        changed_paths=changed_paths,
        full_compile=full_compile,
        now=now,
        heartbeat=heartbeat,
        out_run_stats=out_run_stats,
    )
    ctx.skip_entity_tiers = cluster_only or merge_only
    ctx.api_key = os.environ.get("ANTHROPIC_API_KEY")
    ctx.config = load_config(knowledge_root)

    # Phase: git precondition + config resolution (provider, budgets,
    # retire/push/pull opt-ins) — any failure here is a clean run failure.
    _rc = _run_preconditions(ctx)
    if _rc is not None:
        return _rc
    _rc = _resolve_run_config(ctx)
    if _rc is not None:
        return _rc

    # Phase: pre-run git VCS I/O (optional pull, HEAD capture).
    _run_git_vcs_io(ctx)

    # Phase: build the shared LLM client, seed `usage`, arm the run-level
    # wall-clock deadline.
    _arm_run_deadline(ctx)

    # Phase: OS signal handling is installed/removed INSIDE the entity-tier
    # phase below (`_run_entity_tier_phase`), not as its own standalone
    # phase — its install/removal timing relative to deadline arming (just
    # above) and the tier loop (which it wraps) is load-bearing and MUST NOT
    # shift; see that function's docstring.

    # Phase: athenaeum#290 wiki-page dedup pass (independent of the C1-C4 auto-memory
    # pipeline; runs on every mode) + the deadline check right after it.
    _rc = _run_wiki_dedup_phase(ctx)
    if _rc is not None:
        return _rc

    if merge_only:
        # merge_only short-circuits the rest of the pipeline entirely.
        return _run_merge_only_phase(ctx)

    # Issue athenaeum#461: shared state hoisted above BOTH the entity phase and the
    # auto-memory block. Safe defaults so the finalize return-code logic
    # (deadline_tripped / failed_files / deferred_refs) is well-defined even
    # on cluster_only (which skips the entity phase entirely) and on an
    # empty raw intake (which falls through to auto-memory instead of
    # returning early). `RunContext` field defaults already cover these; no
    # explicit reset needed here.

    # Phase: the ENTITY tier loop (tier1-4 routing INCLUDING the Batch API
    # fan-out branch), ahead of auto-memory so it claims the shared deadline
    # / budget first (issue athenaeum#461).
    _run_entity_tier_phase(ctx)

    # Phase: auto-memory block (C1 discover + C2 cluster / C3 merge / C4
    # detect, retire, athenaeum#188 reresolve) — skipped entirely if the entity loop
    # already tripped the wall-clock deadline.
    if not ctx.deadline_tripped:
        _rc = _run_auto_memory_phase(ctx)
        if _rc is not None:
            return _rc

    if cluster_only:
        # Same contract as the merge-only early return above: a clean
        # cluster-only run must not preserve a stale deferred manifest.
        if not dry_run:
            _clear_stale_deferred_manifest(wiki_root)
        # Resolved to a concrete bool by ``_resolve_run_config`` above (athenaeum#546:
        # narrows ``bool | None`` — never fires for a valid run).
        assert ctx.push_after_run is not None
        _maybe_push_after_run(
            knowledge_root,
            config=ctx.config,
            push_after_run=ctx.push_after_run,
            dry_run=dry_run,
            head_at_start=ctx.head_at_start,
        )
        ctx.emit_run_summary()  # issue athenaeum#464
        return 0

    # Phase: finalize (spend summary + ledger, post-run push, page-size
    # guardrail, pending-merge revalidation advisor, summary emit, drain
    # advisory, terminal return-code selection).
    return _run_finalize_phase(ctx)


# ---------------------------------------------------------------------------
# On-demand ingest (issue athenaeum#349) — manual/escape-hatch compile of new/changed
# raw intake, with a content-hash stamp manifest so an incremental run is a
# fast no-op when nothing has changed since the last successful ingest. The
# SessionEnd path (issue athenaeum#350) reuses `ingest()` directly.
# ---------------------------------------------------------------------------

#: Stamp-manifest filename recording the raw-intake content hashes seen by the
#: last successful ingest. Lives in the cache dir alongside the athenaeum#348 index
#: manifests (kept out of the knowledge git repo). Shape mirrors the search
#: manifests: ``{"version": 1, "hashes": {relpath: sha256}}``.
INGEST_MANIFEST_NAME = "ingest-manifest.json"


@dataclass
class IngestResult:
    """Summary of an :func:`ingest` invocation (issue athenaeum#349).

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
    """Resolve the athenaeum cache dir (arg > env > ``~/.cache/athenaeum``).

    Thin wrapper over :func:`athenaeum.config.resolve_cache_dir` (issue athenaeum#521):
    the canonical resolver lives in ``config`` so every site agrees.
    """
    return _resolve_cache_dir_config(cache_dir)


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
    matches are included (the per-session incremental gate athenaeum#350 needs).
    Unreadable files are skipped.

    Issue athenaeum#370 stat pre-filter: ``prior_stats`` maps ``relpath ->
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
    """Load the ingest stamp's ``relpath -> (mtime_ns, size)`` stat map (athenaeum#370).

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

    ``stats`` (issue athenaeum#370) persists per-file ``(mtime_ns, size)`` so the next
    run's stat pre-filter can skip re-reading unchanged raw files. Bumped to
    ``version: 2`` when stats are written; ``hashes`` stays present for readers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    version = 2 if stats is not None else 1
    payload: dict[str, Any] = {"version": version, "hashes": hashes}
    if stats is not None:
        payload["stats"] = {k: [v[0], v[1]] for k, v in stats.items()}
    atomic_write_text(path, json.dumps(payload))


# ---------------------------------------------------------------------------
# Live-client delta cadence (issue athenaeum#463, slice D of athenaeum#460). Two more cache-dir
# stamps, siblings of ``ingest-manifest.json`` (same "outside the knowledge git
# repo" rationale): a content-hash snapshot of the auto-memory intake (the
# delta baseline for computing ``changed_paths`` on the nightly run, which —
# unlike ingest/session_end — never received an explicit caller-supplied
# delta), and a "last successful whole-corpus auto-memory compile" stamp that
# drives the periodic full-compile reconciliation cadence.
# ---------------------------------------------------------------------------

#: Content-hash stamp over the auto-memory intake files, keyed by path relative
#: to ``knowledge_root`` (mirrors :data:`INGEST_MANIFEST_NAME`'s shape:
#: ``{"version": 1, "hashes": {relpath: sha256}}``). Used by
#: :func:`_auto_memory_changed_paths` to compute the nightly run's delta
#: baseline.
AUTO_MEMORY_MANIFEST_NAME = "auto-memory-manifest.json"

#: Stamp recording the LAST successful whole-corpus (non-delta) auto-memory
#: compile: ``{"at": <ISO-8601 UTC timestamp>, "head": <knowledge_root HEAD
#: sha or null>}``. ``at`` drives the :func:`athenaeum.config.
#: resolve_full_compile_every_days` cadence; ``head`` is audit-only.
FULL_COMPILE_STAMP_NAME = "full-compile-stamp.json"


def _auto_memory_hash_snapshot(
    auto_memory_files: list[AutoMemoryFile],
    knowledge_root: Path,
) -> dict[str, str]:
    """Map ``relpath -> sha256`` for the given auto-memory intake files.

    Keys are POSIX paths relative to *knowledge_root*, mirroring
    :func:`_raw_hash_snapshot`'s shape so the two stamp families stay
    consistent. Takes the already-discovered/filtered
    :class:`AutoMemoryFile` list (issue athenaeum#278 ephemeral drop already applied)
    rather than re-walking the filesystem, so the hashed set is exactly what
    this run considers auto-memory intake. Unreadable files are skipped
    (best-effort, mirrors the ingest snapshot's tolerance).
    """
    snapshot: dict[str, str] = {}
    for am in auto_memory_files:
        try:
            data = am.path.read_bytes()
        except OSError:
            continue
        try:
            rel = am.path.relative_to(knowledge_root).as_posix()
        except ValueError:
            rel = str(am.path)
        snapshot[rel] = hashlib.sha256(data).hexdigest()
    return snapshot


def _load_auto_memory_manifest(path: Path) -> dict[str, str] | None:
    """Load the auto-memory stamp's ``relpath -> hash`` map.

    Returns ``None`` when the manifest is absent/unreadable/malformed (no
    prior successful stamp — the delta baseline is unknown), or the
    ``{relpath: hash}`` map (possibly empty) when a stamp exists. Mirrors
    :func:`_load_ingest_manifest`.
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


def _write_auto_memory_manifest(path: Path, hashes: dict[str, str]) -> None:
    """Atomically write the auto-memory stamp manifest (temp file + rename).

    Mirrors :func:`_write_ingest_manifest`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 1, "hashes": hashes}
    atomic_write_text(path, json.dumps(payload))


def _auto_memory_changed_paths(
    auto_memory_files: list[AutoMemoryFile],
    knowledge_root: Path,
    manifest_path: Path,
) -> set[Path] | None:
    """Compute the nightly run's auto-memory delta baseline (issue athenaeum#463).

    Compares the current auto-memory intake hash snapshot against the loaded
    :data:`AUTO_MEMORY_MANIFEST_NAME` stamp. ``changed = added ∪
    (hash-differs)``. Deletion-only deltas count too: a file present in the
    prior manifest but absent from the current snapshot is included (by its
    prior, now-nonexistent absolute path) so its former cluster is still
    considered "touched" and recompiles — a member removal must recompile
    that cluster, not be silently ignored because the file itself is gone.

    Returns ``None`` when no prior manifest exists (unknown baseline — the
    caller's gate must fall back to whole-corpus; this run establishes the
    baseline via :func:`_write_auto_memory_manifest`). Returns a (possibly
    empty) absolute-path ``set[Path]`` otherwise — an empty set is a valid
    delta ("nothing changed"), distinct from ``None``.
    """
    stored = _load_auto_memory_manifest(manifest_path)
    if stored is None:
        return None
    current = _auto_memory_hash_snapshot(auto_memory_files, knowledge_root)
    changed: set[Path] = set()
    for rel, h in current.items():
        prior_h = stored.get(rel)
        if prior_h is None or prior_h != h:
            changed.add((knowledge_root / rel).resolve())
    for rel in stored:
        if rel not in current:
            changed.add((knowledge_root / rel).resolve())
    return changed


def _load_full_compile_stamp(path: Path) -> dict[str, Any] | None:
    """Load the last-whole-corpus-compile stamp (issue athenaeum#463).

    Returns ``None`` when absent/unreadable/malformed/missing ``at`` — treated
    by the caller as "never full-compiled" (a full compile is due). ``head``
    defaults to ``None`` when absent (pre-existing / hand-edited stamp).
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    at = data.get("at")
    if not isinstance(at, str) or not at:
        return None
    head = data.get("head")
    return {"at": at, "head": head if isinstance(head, str) else None}


def _write_full_compile_stamp(path: Path, at: datetime, head: str | None) -> None:
    """Atomically write the last-whole-corpus-compile stamp (issue athenaeum#463).

    ``at`` is stored as an ISO-8601 UTC timestamp (``%Y-%m-%dT%H:%M:%SZ``,
    matching the deferred-manifest convention at :func:`_write_deferred_manifest`
    et al.); ``head`` is the knowledge_root git HEAD sha (or ``None``) for
    audit purposes only — the cadence itself is timestamp-driven.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "at": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head": head,
    }
    atomic_write_text(path, json.dumps(payload))


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
    """Compile new/changed raw intake into the wiki on demand (issue athenaeum#349).

    The on-demand counterpart to the nightly :func:`run`: an agent (or the
    operator, via ``athenaeum ingest``) forces freshly-``remember``ed raw
    files through the librarian compile step so the knowledge becomes
    recallable *now*, decoupled from the nightly cadence. Issue athenaeum#350's
    SessionEnd hook reuses this exact function — it is the single reusable
    incremental-ingest engine; the CLI is a thin wrapper.

    ``incremental`` (default) diffs the current raw-intake set against a
    content-hash stamp manifest (``<cache_dir>/ingest-manifest.json``,
    mirroring the athenaeum#348 index manifest). When a prior stamp exists and nothing
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
    # Issue athenaeum#370: reuse the stored hash for raw files whose (mtime_ns, size) are
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

    # Issue athenaeum#370: a dry-run is a pure manifest-diff PREVIEW — report the delta
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

    # Issue athenaeum#370 PR2: thread the auto-memory delta into ``run`` so the cluster +
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

    # Issue athenaeum#530 (H2): capture whether the compile left any raw file
    # uncompiled — files beyond the max_files window, or budget/deadline
    # deferrals — so the stamp below is not written for a partial run.
    run_stats: dict[str, Any] = {}
    exit_code = run(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=dry_run,
        changed_paths=auto_changed,
        out_run_stats=run_stats,
        **run_kwargs,
    )

    after_all = _raw_hash_snapshot(raw_root, knowledge_root)
    compiled = len(set(before_all) - set(after_all))

    # Stamp the pre-compile snapshot ONLY on a clean, COMPLETE, non-dry run:
    # everything we just processed is now "seen". Consumed (deleted) files stay
    # recorded — harmless, and it keeps a re-run with no new intake a fast
    # no-op. Files that appeared mid-run are absent here, so they correctly
    # surface as ``added`` next run.
    #
    # Issue athenaeum#530 (H2): a ``max_files``-truncated run still exits 0, but the
    # pre-compile snapshot (``before_all``) includes the ``beyond_window``
    # remainder that was NEVER compiled. Stamping it would make the next ingest
    # take the no-op fast path and silently drop those notes forever. So gate
    # the stamp on a fully-drained run: no beyond-window remainder AND no
    # in-window deferrals. A failed compile (nonzero) already leaves the stamp
    # untouched; this extends the same "leave it for retry" guarantee to the
    # degraded exit-0 case the authors guarded the adjacent path against but
    # missed here.
    beyond_window = int(run_stats.get("beyond_window", 0) or 0)
    run_deferred = run_stats.get("deferred_refs") or []
    fully_drained = beyond_window == 0 and not run_deferred
    if exit_code == 0 and not dry_run and fully_drained:
        _write_ingest_manifest(manifest_path, before_all, stats=before_all_stats)
    elif exit_code == 0 and not dry_run and not fully_drained:
        log.info(
            "ingest: run left %d file(s) uncompiled (beyond_window=%d, "
            "deferred=%d) — leaving the stamp manifest untouched so the next "
            "ingest retries the backlog instead of a false no-op (issue athenaeum#530)",
            beyond_window + len(run_deferred),
            beyond_window,
            len(run_deferred),
        )

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
    (:func:`session_end`, issue athenaeum#350) share, so both apply the *same* backend
    resolution, extra-intake roots, and index globs. ``incremental`` (default,
    issue athenaeum#348) applies only the add/change/delete hash-diff delta — a fast
    no-op when the wiki has not changed since the last build. A ``vector``
    backend that is not installed raises ``ImportError`` to the caller.
    """
    from athenaeum.config import (
        resolve_embedding_model,
        resolve_index_globs,
        resolve_reindex_full_rehash_max_age_days,
    )
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
    full_rehash_max_age_days = resolve_reindex_full_rehash_max_age_days(
        knowledge_root, config
    )

    if backend_name == "vector":
        pages = build_vector_index(
            wiki_root,
            resolved_cache,
            extra_roots=extra_roots,
            incremental=incremental,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            embedding_model=resolve_embedding_model(config),
            full_rehash_max_age_days=full_rehash_max_age_days,
            config=config,
        )
    else:
        pages = build_fts5_index(
            wiki_root,
            resolved_cache,
            extra_roots=extra_roots,
            incremental=incremental,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            full_rehash_max_age_days=full_rehash_max_age_days,
            config=config,
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
    """Cheap dry-run preview of how many pages a reindex would touch (athenaeum#370).

    Diffs the current wiki against the vector/fts5 index manifest — the SAME
    ``added + changed + removed`` delta :func:`reindex` would apply — but WITHOUT
    opening chromadb or loading any embedding model. The scan reuses the athenaeum#370
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
    """Summary of a :func:`session_end` invocation (issue athenaeum#350).

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
    # Issue athenaeum#370: on a dry-run, a cheap manifest hash-diff of how many pages a
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
    """Change-gated SessionEnd compile-then-index composition (issue athenaeum#350).

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
        # Issue athenaeum#370: announce the planned work BEFORE the (potentially minutes-
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
        # Issue athenaeum#370: cheap dry-run preview — count how many pages a reindex
        # WOULD touch via a manifest hash-diff (the SAME delta reindex applies)
        # WITHOUT opening chromadb or loading any embedding model.
        reindex_would_change = _reindex_would_change(
            knowledge_root,
            wiki_root,
            cache_dir=cache_dir,
            config=config,
            backend=backend,
        )

    # Issue athenaeum#711: reference determination — mark which of THIS session's
    # pushed ids were actually referenced afterward, so precision
    # (referenced / pushed) accrues per session. Scoped to `session` (the
    # same originating-session id `session_end` already takes for its
    # incremental-ingest scoping) and skipped on a dry-run (no durable writes
    # on a preview). Best-effort: `run_reference_determination` swallows its
    # own failures and must never break session_end.
    if session and not dry_run:
        from athenaeum import push_metrics

        push_metrics.run_reference_determination(
            session, cache_dir=cache_dir, config=config
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
