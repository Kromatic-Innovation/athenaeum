# SPDX-License-Identifier: Apache-2.0
"""Move every page of one entity type into another configured surface (athenaeum#2620).

``athenaeum storage move-type <type> --to <surface>`` relocates the existing
pages of a type from the default ``wiki/`` surface into a configured, indexed
surface that lives in its OWN git repository -- the step that turns a per-type
store (athenaeum#2618 routes NEW pages, athenaeum#2617 locates pages across
surfaces) into the home of pages that already exist.

Three guarantees, each a seam of its own:

* **Plan first** (:func:`plan_move`). Read-only. Lists every page that would
  move and every link that would break the store-link rule of athenaeum#2619
  once the pages sit on the target surface's visibility rank. The link
  resolution and the rule are those of :mod:`athenaeum.store_links`
  (:func:`~athenaeum.store_links.scan_links_after_move`); nothing is
  re-implemented here. A plan with any such link, any destination collision,
  or nothing to move cannot be applied (:attr:`MovePlan.can_apply`).
* **Apply is reversible** (:func:`apply_move`). Both repositories -- the
  wiki's and the target surface's own -- are committed first (the restore
  points), the files are moved byte-for-byte (so every page uid is
  unchanged), and each repository gets a commit. A failure part-way restores
  both. A :class:`MoveRecord` carrying the before/after SHAs is written to
  the cache directory.
* **Undo** (:func:`undo_move`) resets both repositories to the restore points,
  and refuses when either repository has moved on since the apply.

Every target-surface rule lives in :func:`validate_move_target`: the one place
to add a further refusal (for example a read-only surface).

This module does not touch the search index: the caller (the CLI) reindexes
after an apply or an undo, because page names in the index change with the
surface.

Layering: L4 domain module. Imports :mod:`athenaeum.storage`,
:mod:`athenaeum.store`, :mod:`athenaeum.store_links`, :mod:`athenaeum.models`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_cache_dir, resolve_storage_mapping
from athenaeum.models import PageLocator, parse_frontmatter
from athenaeum.storage import (
    DEFAULT_ADAPTER_NAME,
    StorageAdapter,
    available_adapters,
    open_store,
    resolve_locator_surfaces,
)
from athenaeum.store import commit_repo, repo_head, repo_is_clean, restore_repo
from athenaeum.store_links import LinkViolation, scan_links_after_move

#: Where move records live under the cache directory.
MOVE_RECORD_DIR_NAME = "storage_moves"


class MoveRefused(ValueError):
    """The move cannot proceed; the message says why and what to do."""


@dataclass(frozen=True)
class MoveTarget:
    """A validated target surface: its adapter, root, and the two repositories."""

    adapter: StorageAdapter
    root: Path
    repo: Path
    wiki_repo: Path


@dataclass(frozen=True)
class PlannedMove:
    uid: str
    src: Path
    dst: Path


@dataclass(frozen=True)
class MovePlan:
    entity_type: str
    target_name: str
    target: MoveTarget
    moves: tuple[PlannedMove, ...]
    #: Destination files that already exist (the move would overwrite them).
    collisions: tuple[Path, ...]
    #: Links that break the store-link rule once the pages have moved.
    violations: tuple[LinkViolation, ...]
    #: ``True`` when ``storage.mapping`` already routes the type to the target.
    mapping_routes_type: bool

    @property
    def can_apply(self) -> bool:
        return bool(self.moves) and not self.collisions and not self.violations


@dataclass(frozen=True)
class MoveRecord:
    """What an apply did: enough to undo it. Serialised as JSON."""

    record_id: str
    entity_type: str
    target_name: str
    created_at: str
    #: ``{"wiki"|"target": {"repo": path, "before": sha, "after": sha}}``
    repos: dict[str, dict[str, str]]
    moved: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "record_id": self.record_id,
                "entity_type": self.entity_type,
                "target_name": self.target_name,
                "created_at": self.created_at,
                "repos": self.repos,
                "moved": list(self.moved),
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> MoveRecord:
        raw = json.loads(text)
        return cls(
            record_id=raw["record_id"],
            entity_type=raw["entity_type"],
            target_name=raw["target_name"],
            created_at=raw["created_at"],
            repos=raw["repos"],
            moved=tuple(raw["moved"]),
        )


def validate_move_target(
    config: dict[str, Any] | None, knowledge_root: Path, target_name: str
) -> MoveTarget:
    """Resolve *target_name* to a surface a type can be moved into, or raise
    :class:`MoveRefused`.

    The target must be a configured surface that is indexed
    (``embedded: true``), sits outside ``wiki/``, and lives in its OWN git
    repository (not the wiki's); the wiki must itself be in a git repository.
    Every rule about where pages may be moved belongs here.
    """
    adapters = available_adapters(config)
    adapter = adapters.get(target_name)
    if adapter is None or target_name == DEFAULT_ADAPTER_NAME:
        raise MoveRefused(
            f"surface {target_name!r} is not a configured target surface; "
            f"configured: {sorted(n for n in adapters if n != DEFAULT_ADAPTER_NAME)}. "
            "Define it under storage.adapters first."
        )
    if not adapter.corpus_policy.embedded:
        raise MoveRefused(
            f"surface {target_name!r} is not indexed (embedded: false); moving pages "
            "to a surface recall cannot see is not supported"
        )
    root = adapter.resolve_root(knowledge_root)
    wiki_root = (knowledge_root / "wiki").resolve()
    if root.resolve() == wiki_root or wiki_root in root.resolve().parents:
        raise MoveRefused(
            f"surface {target_name!r} lives inside wiki/, so it is not a separate store"
        )
    store = open_store(config, knowledge_root)
    wiki_repo = store.surface_repo(DEFAULT_ADAPTER_NAME)
    if wiki_repo is None:
        raise MoveRefused(
            "the wiki is not in a git repository, so there is no restore point to "
            "take before the move; put it under git first"
        )
    repo = store.surface_repo(target_name)
    if repo is None or repo.resolve() == wiki_repo.resolve():
        raise MoveRefused(
            f"surface {target_name!r} is not in its own git repository (root {root}); "
            "give it one (git init in the surface root) so the move has its own "
            "restore point and the store can be shared"
        )
    return MoveTarget(adapter=adapter, root=root, repo=repo, wiki_repo=wiki_repo)


def plan_move(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    entity_type: str,
    target_name: str,
) -> MovePlan:
    """Plan moving every wiki page of *entity_type* to *target_name* (read-only)."""
    entity_type = entity_type.strip()
    if not entity_type:
        raise MoveRefused("an entity type is required")
    target = validate_move_target(config, knowledge_root, target_name)
    wiki_root = knowledge_root / "wiki"
    locator = PageLocator(wiki_root, resolve_locator_surfaces(knowledge_root, config))
    moves: list[PlannedMove] = []
    for key, path in locator.iter_pages():
        if "/" in key:  # a page already on an extra surface
            continue
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict) or str(meta.get("type") or "").strip() != entity_type:
            continue
        moves.append(
            PlannedMove(uid=str(meta.get("uid") or ""), src=path, dst=target.root / path.name)
        )
    collisions = tuple(m.dst for m in moves if m.dst.exists())
    violations: tuple[LinkViolation, ...] = ()
    if moves:
        violations = scan_links_after_move(
            knowledge_root, {m.src: target_name for m in moves}, config
        ).violations
    mapped = resolve_storage_mapping(config).get(entity_type) == target_name
    return MovePlan(
        entity_type=entity_type,
        target_name=target_name,
        target=target,
        moves=tuple(moves),
        collisions=collisions,
        violations=violations,
        mapping_routes_type=mapped,
    )


def record_path(record_id: str, cache_dir: Path | None = None) -> Path:
    return resolve_cache_dir(cache_dir) / MOVE_RECORD_DIR_NAME / f"{record_id}.json"


def apply_move(
    plan: MovePlan,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    cache_dir: Path | None = None,
) -> MoveRecord:
    """Apply *plan*: snapshot both repos, move the pages, commit both repos.

    Raises :class:`MoveRefused` when the plan cannot be applied (links that
    break the store rule, collisions, nothing to move) or a repository has no
    commit to restore to. If moving fails part-way both repositories are
    restored to their snapshots before the error propagates.
    """
    if plan.violations:
        raise MoveRefused(
            f"{len(plan.violations)} link(s) would break the store-link rule; "
            "fix them (or move the linked pages too) and re-run"
        )
    if plan.collisions:
        raise MoveRefused(
            f"{len(plan.collisions)} destination file(s) already exist on "
            f"{plan.target_name!r}; nothing was moved"
        )
    if not plan.moves:
        raise MoveRefused(f"no page of type {plan.entity_type!r} on the wiki surface to move")
    repos = {"wiki": plan.target.wiki_repo, "target": plan.target.repo}
    label = f"athenaeum: snapshot before storage move-type {plan.entity_type} -> {plan.target_name}"
    before: dict[str, str] = {}
    for role, repo in repos.items():
        commit_repo(repo, label)
        sha = repo_head(repo)
        if sha is None:
            raise MoveRefused(f"the {role} repository {repo} has no commit to restore to")
        before[role] = sha
    done = f"athenaeum: storage move-type {plan.entity_type} -> {plan.target_name}"
    after: dict[str, str] = {}
    try:
        plan.target.root.mkdir(parents=True, exist_ok=True)
        for m in plan.moves:
            shutil.move(str(m.src), str(m.dst))
        for role, repo in repos.items():
            commit_repo(repo, done)
            head = repo_head(repo)
            if head is None:
                raise MoveRefused(f"the {role} repository {repo} has no HEAD after the commit")
            after[role] = head
    except BaseException:
        # Any failure (a move, a commit hook, identity or lock trouble) puts
        # BOTH repositories back at their snapshots; no record is written.
        for role, repo in repos.items():
            restore_repo(repo, before[role])
        raise
    record = MoveRecord(
        record_id=f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        entity_type=plan.entity_type,
        target_name=plan.target_name,
        created_at=datetime.now(UTC).isoformat(),
        repos={
            role: {"repo": str(repo), "before": before[role], "after": after[role]}
            for role, repo in repos.items()
        },
        moved=tuple(m.src.name for m in plan.moves),
    )
    path = record_path(record.record_id, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, record.to_json() + "\n")
    return record


def load_record(ref: str, cache_dir: Path | None = None) -> MoveRecord:
    """Load a record by id (looked up in the cache dir) or by file path."""
    candidate = Path(ref).expanduser()
    path = candidate if candidate.is_file() else record_path(ref, cache_dir)
    try:
        return MoveRecord.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise MoveRefused(f"cannot read move record {ref!r} ({path}): {exc}") from exc


def undo_move(record: MoveRecord, *, force: bool = False) -> None:
    """Restore both repositories to the snapshots *record* took before the move.

    Refuses (nothing is changed) unless each repository is still at the commit
    the apply left and has a clean tree -- later work would otherwise be
    discarded. ``force=True`` skips that check and resets regardless.
    """
    repos = {role: (Path(info["repo"]), info) for role, info in record.repos.items()}
    if not force:
        for role, (repo, info) in repos.items():
            if repo_head(repo) != info["after"]:
                raise MoveRefused(
                    f"the {role} repository {repo} has moved on since the apply "
                    f"(expected HEAD {info['after']}); undoing would discard later "
                    "commits. Re-run with --force to reset anyway."
                )
            if not repo_is_clean(repo):
                raise MoveRefused(
                    f"the {role} repository {repo} has uncommitted changes; commit or "
                    "discard them, or re-run with --force to reset anyway."
                )
    try:
        for repo, info in repos.values():
            restore_repo(repo, info["before"])
    except subprocess.CalledProcessError as exc:
        raise MoveRefused(f"restoring a repository failed: {exc}") from exc
