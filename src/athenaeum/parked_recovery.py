# SPDX-License-Identifier: Apache-2.0
"""One-shot recovery of parked observations (issue athenaeum#2078 AC5).

Background: tier-3 merges into oversized pages (athenaeum#1182) and into
pages past the full-echo window (athenaeum#1180) were refused and parked as
``_pending_questions.md`` notices. The observation text was supposed to ride
along but did not survive the block round-trip (fixed by AC4), and the raw
source files were consumed, so the only copy is in the knowledge repo's git
history.

Two phases, driven by a host-side manifest (a JSON list of
``{question_id, conflict_type, entity, raw_path, last_commit}`` rows --
never committed, it names entities):

``restore``
    For each row with a ``last_commit``, write ``raw_path`` back from git
    history if it is not already on disk. The normal librarian run then
    ingests it under the AC1/AC2 merge paths (no special ingest code).
    A row with no commit is a source the adapter must re-fetch (drive files):
    counted, not restored. Rows whose file exists are recorded as "tracked".

``resolve``
    Close (archive, with a provenance note) each tracked question whose raw
    file has since been consumed by a librarian run. Questions whose file is
    still on disk are left open -- not yet ingested.

Both phases are idempotent and dry-run unless told to apply. State (the set
of tracked question ids) lives beside the manifest, never in the corpus.
Output is counts only.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)


class RecoveryError(Exception):
    """A manifest/state problem the operator must fix before anything runs."""


@dataclass(frozen=True)
class ManifestRow:
    question_id: str
    conflict_type: str
    entity: str
    raw_path: str
    last_commit: str | None


def load_manifest(path: Path) -> list[ManifestRow]:
    """Load and validate the manifest. Raises :class:`RecoveryError`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RecoveryError("manifest must be a JSON list of row objects")
    rows: list[ManifestRow] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise RecoveryError(f"manifest row {i} is not an object")
        qid, raw_path = item.get("question_id"), item.get("raw_path")
        commit = item.get("last_commit")
        if not isinstance(qid, str) or not qid or not isinstance(raw_path, str):
            raise RecoveryError(f"manifest row {i} lacks question_id/raw_path")
        if commit is not None and not isinstance(commit, str):
            raise RecoveryError(f"manifest row {i} has a non-string last_commit")
        rows.append(
            ManifestRow(
                question_id=qid,
                conflict_type=str(item.get("conflict_type") or ""),
                entity=str(item.get("entity") or ""),
                raw_path=raw_path,
                last_commit=commit or None,
            )
        )
    return rows


def _safe_raw_target(knowledge_root: Path, raw_path: str) -> Path:
    """Resolve *raw_path* under ``<root>/raw``, refusing anything that escapes."""
    rel = PurePosixPath(raw_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "raw":
        raise RecoveryError("manifest raw_path must be a relative path under raw/")
    target = (knowledge_root / Path(*rel.parts)).resolve()
    if not target.is_relative_to((knowledge_root / "raw").resolve()):
        raise RecoveryError("manifest raw_path escapes the raw/ tree")
    return target


def _git_show(knowledge_root: Path, commit: str, raw_path: str) -> bytes | None:
    """File bytes at *commit*, or just before it (a deleting commit), else None."""
    for rev in (f"{commit}:{raw_path}", f"{commit}^:{raw_path}"):
        proc = subprocess.run(
            ["git", "-C", str(knowledge_root), "show", rev],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout
    return None


def _load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {x for x in data.get("tracked", []) if isinstance(x, str)}
    except (OSError, ValueError, AttributeError) as exc:
        raise RecoveryError(f"cannot read recovery state {path}: {exc}") from exc


def _save_state(path: Path, tracked: set[str]) -> None:
    atomic_write_text(path, json.dumps({"tracked": sorted(tracked)}, indent=2) + "\n")


def restore_raw_files(
    knowledge_root: Path,
    rows: list[ManifestRow],
    state_path: Path,
    *,
    apply: bool,
) -> dict[str, int]:
    """Phase ``restore``. Returns counts (see the module docstring)."""
    counts = {
        "restored": 0,
        "already_present": 0,
        "needs_refetch": 0,
        "not_in_git": 0,
    }
    tracked = _load_state(state_path)
    for row in rows:
        target = _safe_raw_target(knowledge_root, row.raw_path)
        if target.exists():
            counts["already_present"] += 1
            tracked.add(row.question_id)
            continue
        if row.last_commit is None:
            counts["needs_refetch"] += 1
            continue
        data = _git_show(knowledge_root, row.last_commit, row.raw_path)
        if data is None:
            counts["not_in_git"] += 1
            continue
        counts["restored"] += 1
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            tracked.add(row.question_id)
    if apply:
        _save_state(state_path, tracked)
    return counts


def resolve_recovered(
    knowledge_root: Path,
    pending_path: Path,
    rows: list[ManifestRow],
    state_path: Path,
    *,
    apply: bool,
) -> dict[str, int]:
    """Phase ``resolve``: archive tracked questions whose raw file was consumed."""
    from athenaeum.answers import parse_pending_questions
    from athenaeum.tiers import archive_pending_blocks

    counts = {"closed": 0, "awaiting_ingest": 0, "untracked": 0, "not_open": 0}
    tracked = _load_state(state_path)
    questions = parse_pending_questions(pending_path)
    open_by_id = {pq.id: pq for pq in questions if not pq.answered}
    to_close: set[str] = set()
    for row in rows:
        if row.question_id not in tracked:
            counts["untracked"] += 1
            continue
        pq = open_by_id.get(row.question_id)
        if pq is None:
            counts["not_open"] += 1
            continue
        if _safe_raw_target(knowledge_root, row.raw_path).exists():
            counts["awaiting_ingest"] += 1
            continue
        counts["closed"] += 1
        to_close.add(pq.raw_block)
    if apply and to_close:
        archive_pending_blocks(
            pending_path,
            questions,
            to_close,
            reason="resolved: parked observation restored from git history and "
            "re-ingested under the oversize-merge paths, issue athenaeum#2078",
        )
    return counts
