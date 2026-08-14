# SPDX-License-Identifier: Apache-2.0
"""Per-file quarantine for poison raw-intake files (issue athenaeum#898).

A raw intake file that keeps exceeding its per-file bound (byte size,
LLM-call count, or wall-clock — the consecutive-count accounting lives in
:mod:`athenaeum.librarian`, mirroring the athenaeum#663 stuck-file ledger's shape) is
QUARANTINED: physically moved out of ``raw_root`` into
``<wiki_root>/_quarantine/<source>/<filename>`` so it drops out of
:func:`athenaeum.intake.discover_raw_files`'s discovery set without any
change to that function itself, and recorded in a durable, append-only audit
ledger naming the file and the bound it exceeded.

This is deliberately a HEAVIER, more visible action than the athenaeum#663 stuck-file
skip: a stuck file stays on disk and is merely skipped in place (an
unbounded-content processing FAILURE may be transient, so leaving it for
inspection costs nothing); a bound violation is a measured RESOURCE fact, and
crossing the consecutive-run threshold means the file has already cost
:data:`athenaeum.librarian.DEFAULT_QUARANTINE_THRESHOLD` (or its configured
override) nights of budget for zero durable progress. Removing it from the
discovery set is what actually stops the bleed; the athenaeum#663 skip alone would
not, since a skip-in-place file is still discovered and checked every run.

Persistence mirrors the other librarian ledgers (JSONL, ``O_APPEND`` + fsync,
tolerant reader that skips a torn trailing line, per-module private
``_append_jsonl_line`` copy — see :mod:`athenaeum.calibration` /
:mod:`athenaeum.retraction_cascade`, the two closest precedents). One ledger,
``<wiki_root>/_quarantine.jsonl``, carries two record kinds: ``quarantine``
(the file was moved out) and ``release`` (an operator's reversing decision —
AC6: quarantine is reversible, and an operator decision is the ONLY way back;
there is no automatic un-quarantine, by design). Surfaced through
:func:`athenaeum.decisions.list_pending_decisions` as ``type: "quarantine"``
items via :func:`athenaeum.decisions.quarantine_to_decision`.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.calibration`
and :mod:`athenaeum.retraction_cascade`. Imports only stdlib at module scope
so it stays trivially importable; :mod:`athenaeum.librarian` (the entity
phase runner) is the one caller that resolves the consecutive-violation
threshold and decides WHEN to call :func:`quarantine_file` — this module only
executes the mechanical action (move + ledger write) and the reversal, and
never imports ``librarian`` back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record so a future reader can migrate.
QUARANTINE_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_calibration.jsonl`` and
#: ``_pending_retractions.jsonl``.
QUARANTINE_LEDGER_FILENAME = "_quarantine.jsonl"

#: Holding directory under ``wiki_root`` a quarantined file is moved into,
#: preserving its ``<source>/<filename>`` shape so a release can restore it
#: to the exact ``raw_root``-relative path it came from.
QUARANTINE_DIR_NAME = "_quarantine"

#: Record kinds in the single quarantine ledger.
QUARANTINE_KIND = "quarantine"
RELEASE_KIND = "release"


def default_quarantine_ledger_path(wiki_root: Path) -> Path:
    """Default quarantine ledger path: ``<wiki_root>/_quarantine.jsonl``."""
    return Path(wiki_root) / QUARANTINE_LEDGER_FILENAME


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def quarantine_item_id(ref: str, created_at: str) -> str:
    """Deterministic idempotency key for one quarantine EVENT.

    Keyed on ``(ref, created_at)`` rather than ``ref`` alone: the same ref can
    legitimately be quarantined, released, and quarantined again later (a
    fresh run of consecutive bound violations on edited-then-still-bad
    content), and each event needs its own id for the ledger's
    quarantine/release pairing to stay unambiguous.
    """
    digest = hashlib.sha1(f"{ref}\x00{created_at}".encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Same discipline as :func:`athenaeum.calibration._append_jsonl_line` /
    :func:`athenaeum.retraction_cascade._append_jsonl_line`: a single small
    ``O_APPEND`` write is atomic on local filesystems, so a crash can at
    worst leave a torn TRAILING line (which the reader skips), never corrupt
    an already-written record. Duplicated (not imported) per this codebase's
    per-module-ledger house style.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_quarantine_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Returns ``[]`` when the ledger does not exist. Malformed lines (a crash
    mid-write, or a hand-edit) are skipped, not fatal.
    """
    target = (
        ledger_path if ledger_path is not None else default_quarantine_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            records.append(record)
    return records


def _released_ids(records: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") == RELEASE_KIND}


def list_pending_quarantine(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Quarantined files awaiting an operator decision (issue athenaeum#898, AC 4/5).

    Excludes any quarantine event that already has a matching ``release``
    record — the same "unreviewed" filter shape as
    :func:`athenaeum.calibration.list_pending_audit`.
    """
    records = read_quarantine_ledger(wiki_root, ledger_path=ledger_path)
    released = _released_ids(records)
    return [
        r
        for r in records
        if r.get("kind") == QUARANTINE_KIND and str(r.get("id")) not in released
    ]


def quarantine_file(
    raw: Any,
    *,
    wiki_root: Path,
    raw_root: Path,
    bound: str,
    detail: str,
    violations: int,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Move *raw* out of the discovery set and record the quarantine (issue athenaeum#898, AC 4).

    ``raw`` is an :class:`athenaeum.models.RawFile` (or anything duck-typing
    its ``path``/``source``/``ref``). The file is moved — not copied — from
    its ``raw_root``-relative location to the mirrored path under
    ``<wiki_root>/_quarantine/``, so a subsequent
    :func:`athenaeum.intake.discover_raw_files` call over ``raw_root`` no
    longer finds it (AC 4: "moves the file out of the discovery set"). A
    ``quarantine`` record is appended to the ledger naming the ref, the
    ``bound`` that was exceeded (``"bytes"`` / ``"llm_calls"`` /
    ``"wall_clock"``), a human-readable ``detail`` string, and the
    consecutive-violation count that triggered this — the audit trail AC 4
    requires. Both the ``original_path`` (for :func:`release_quarantine`) and
    the ``quarantine_path`` are recorded, ``wiki_root``-relative and
    ``raw_root``-relative respectively.

    Returns the appended record (also the shape :func:`list_pending_quarantine`
    and :func:`athenaeum.decisions.quarantine_to_decision` consume).
    """
    wiki_root = Path(wiki_root)
    raw_root = Path(raw_root)
    try:
        original_relpath = raw.path.relative_to(raw_root)
    except ValueError:
        # Defensive: a raw file not actually rooted under raw_root (e.g. a
        # hand-built test double) still gets a sensible, reconstructable
        # relative path rather than raising here.
        original_relpath = Path(raw.source) / raw.path.name

    quarantine_dir = wiki_root / QUARANTINE_DIR_NAME
    quarantine_path = quarantine_dir / original_relpath
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(raw.path), str(quarantine_path))

    created_at = _now_iso()
    record = {
        "v": QUARANTINE_LEDGER_VERSION,
        "kind": QUARANTINE_KIND,
        "id": quarantine_item_id(raw.ref, created_at),
        "created_at": created_at,
        "ref": raw.ref,
        "source": raw.source,
        "bound": bound,
        "detail": detail,
        "violations": violations,
        "original_path": str(original_relpath),
        "quarantine_path": str(quarantine_path.relative_to(wiki_root)),
    }
    target = (
        ledger_path if ledger_path is not None else default_quarantine_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    log.info(
        "athenaeum#898: quarantined %s -> %s (bound=%s violations=%d)",
        raw.ref,
        quarantine_path,
        bound,
        violations,
    )
    return record


def release_quarantine(
    wiki_root: Path,
    raw_root: Path,
    *,
    quarantine_id: str,
    note: str = "",
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Reverse a quarantine: move the file back into the discovery set (issue athenaeum#898, AC 6).

    Looks up the quarantine event by ``quarantine_id`` (the id
    :func:`quarantine_file` returned/recorded); moves the file from its
    quarantine holding path back to its original ``raw_root``-relative
    location, so the NEXT :func:`athenaeum.intake.discover_raw_files` call
    finds it again — the only path back into the discovery set (no automatic
    un-quarantine exists anywhere in this module, by design: AC 6 requires an
    operator decision). Appends a ``release`` record to the ledger.

    Raises ``ValueError`` if ``quarantine_id`` is unknown or already
    released — each quarantine event is released at most once, mirroring
    :func:`athenaeum.calibration.record_audit_review`'s guard.

    If the quarantined file is no longer present on disk at release time
    (manually deleted, moved by an operator, etc.), the release record is
    still written — a decision that can never be marked resolved because its
    file evaporated would be a worse failure mode than a release record that
    describes a file the operator must restore some other way — but a
    WARNING is logged naming exactly what happened.
    """
    wiki_root = Path(wiki_root)
    raw_root = Path(raw_root)
    records = read_quarantine_ledger(wiki_root, ledger_path=ledger_path)
    quarantined = next(
        (
            r
            for r in records
            if r.get("kind") == QUARANTINE_KIND and str(r.get("id")) == quarantine_id
        ),
        None,
    )
    if quarantined is None:
        raise ValueError(f"unknown quarantine item id: {quarantine_id!r}")
    if quarantine_id in _released_ids(records):
        raise ValueError(f"quarantine item already released: {quarantine_id!r}")

    quarantine_path = wiki_root / str(quarantined.get("quarantine_path", ""))
    original_path = raw_root / str(quarantined.get("original_path", ""))
    if quarantine_path.exists():
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(quarantine_path), str(original_path))
    else:
        log.warning(
            "athenaeum#898: quarantined file missing on disk at release time: %s "
            "(ledger record %s) — releasing the ledger record anyway so the "
            "decision does not stay stuck forever; the file itself cannot be "
            "restored by this call.",
            quarantine_path,
            quarantine_id,
        )

    record = {
        "v": QUARANTINE_LEDGER_VERSION,
        "kind": RELEASE_KIND,
        "id": quarantine_id,
        "created_at": _now_iso(),
        "ref": quarantined.get("ref"),
        "note": note,
    }
    target = (
        ledger_path if ledger_path is not None else default_quarantine_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    log.info(
        "athenaeum#898: released quarantine %s (ref=%s)",
        quarantine_id,
        quarantined.get("ref"),
    )
    return record


__all__ = [
    "QUARANTINE_LEDGER_VERSION",
    "QUARANTINE_LEDGER_FILENAME",
    "QUARANTINE_DIR_NAME",
    "QUARANTINE_KIND",
    "RELEASE_KIND",
    "default_quarantine_ledger_path",
    "quarantine_item_id",
    "read_quarantine_ledger",
    "list_pending_quarantine",
    "quarantine_file",
    "release_quarantine",
]
