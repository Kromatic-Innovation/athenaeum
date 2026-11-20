# SPDX-License-Identifier: Apache-2.0
"""The ``reviews`` block: which reviews are declared, may run, and are due.

Layering: L2 (imports :mod:`athenaeum.store_access`, :mod:`athenaeum.store_rights`,
L2, and :mod:`athenaeum.store`, L1).

Design: ``docs/design/federation-store-access.md`` §8 (scheduled cross-store
review), §12 slice S10.

The target store declares each review (``name``, ``source``, ``every``,
``criteria``; the shape is validated by :mod:`athenaeum.store_access`). This
module gives that block meaning and nothing more:

* **Find the source** by ``store_id`` among the install's mounted stores. A
  review whose source is not mounted is reported as :data:`NOT_MOUNTED`.
* **Both stores must agree.** A review is :data:`RUNNABLE` only when the owner
  holds ``read`` in the *source's own* declaration. Otherwise it is
  :data:`NO_GRANT`, even when the clone is on disk. A source whose declaration
  does not validate grants nothing here.
* **Due** is computed from ``every`` and the time of the last completed run of
  that review against that source. No previous run means due.
* **The cursor and run record** live in the install's own state, one file per
  target store (``_store_reviews/<store_id>.jsonl`` under the knowledge root).
  A record holds the review name, the source ``store_id``, the time, the source
  revision reviewed and integer counts. It never holds source text.
  :func:`record_review_run` refuses a review that is not runnable, so a review
  with no grant never advances its cursor.

Nothing is written to the source and no model is called. The review run itself
(prompt, criteria, records) is access S11 and is not built here.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from athenaeum.models import asserter_identity_key
from athenaeum.store import append_line_durable, now_iso
from athenaeum.store_access import (
    Review,
    StoreDeclaration,
    is_store_id,
    load_store_declaration,
)
from athenaeum.store_rights import DirectoryAdapter, StaticDirectory

__all__ = [
    "REVIEW_STATE_DIRNAME",
    "RUNNABLE",
    "NOT_MOUNTED",
    "NO_GRANT",
    "BAD_INTERVAL",
    "ReviewRefused",
    "ReviewRun",
    "ReviewStatus",
    "parse_every",
    "state_path",
    "load_review_runs",
    "last_run",
    "evaluate_reviews",
    "record_review_run",
]

log = logging.getLogger(__name__)

#: Directory under the knowledge root holding one review-state file per target.
REVIEW_STATE_DIRNAME = "_store_reviews"

#: The source is mounted and the owner holds read in its declaration.
RUNNABLE = "runnable"
#: No mounted store carries the review's ``source`` store id.
NOT_MOUNTED = "not-mounted"
#: The source is mounted but its own declaration does not grant the owner read.
NO_GRANT = "no-grant"
#: ``every`` is not ``<number><m|h|d|w>``; the review is reported, never run.
BAD_INTERVAL = "bad-interval"

_EVERY_RE = re.compile(r"^([1-9][0-9]*)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class ReviewRefused(ValueError):
    """A run could not be recorded."""


@dataclass(frozen=True)
class ReviewRun:
    """One completed run: when, against which revision, and how much."""

    review: str
    source: str
    at: str
    revision: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ReviewStatus:
    """One declared review with its state and whether it is due."""

    review: Review
    state: str
    due: bool
    last_run: ReviewRun | None

    @property
    def cursor(self) -> str | None:
        """The last source revision reviewed, or ``None`` before any run."""
        return self.last_run.revision if self.last_run else None

    @property
    def should_run(self) -> bool:
        return self.state == RUNNABLE and self.due


def parse_every(value: str) -> timedelta | None:
    """``7d`` to a timedelta (units ``m``, ``h``, ``d``, ``w``); ``None`` if malformed."""
    m = _EVERY_RE.match(value.strip())
    if m is None:
        return None
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def state_path(knowledge_root: Path, target_store_id: str) -> Path:
    """The review-state file of one target store."""
    if not is_store_id(target_store_id):
        raise ReviewRefused(f"not a store id: {target_store_id!r}")
    return Path(knowledge_root) / REVIEW_STATE_DIRNAME / f"{target_store_id}.jsonl"


def _parse_time(text: str) -> datetime | None:
    try:
        when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return when.replace(tzinfo=timezone.utc)


def load_review_runs(knowledge_root: Path, target_store_id: str) -> list[ReviewRun]:
    """Every run record of one target store, oldest first.

    A line that does not parse, or lacks a field, is skipped (a torn trailing
    line after a crash is tolerated).
    """
    path = state_path(knowledge_root, target_store_id)
    if not path.is_file():
        return []
    runs: list[ReviewRun] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        counts = rec.get("counts")
        if not (
            isinstance(rec.get("review"), str)
            and isinstance(rec.get("source"), str)
            and isinstance(rec.get("revision"), str)
            and _parse_time(rec.get("at", "")) is not None
            and isinstance(counts, dict)
        ):
            continue
        runs.append(
            ReviewRun(
                rec["review"],
                rec["source"],
                rec["at"],
                rec["revision"],
                {k: v for k, v in counts.items() if isinstance(v, int)},
            )
        )
    return runs


def last_run(runs: list[ReviewRun], review: Review) -> ReviewRun | None:
    """The newest run of *review* against its current source.

    A run recorded against a different source (the declaration was edited to
    point elsewhere) does not count: its cursor names another store's revision.
    """
    for run in reversed(runs):
        if run.review == review.name and run.source == review.source:
            return run
    return None


def _holds_read(
    declaration: StoreDeclaration,
    asserter: Mapping[str, object] | None,
    adapters: Mapping[str, DirectoryAdapter] | None,
) -> bool:
    """Whether the declaration itself grants the owner read.

    Unlike :func:`athenaeum.store_rights.resolve_rights`, a directory that
    cannot answer (no adapter, unknown group) is no grant here: that function
    falls back to reader-only, which is right for an install's own reads but
    would let a review run without an explicit grant in the source.
    """
    directory: DirectoryAdapter | None
    if declaration.directory == "static":
        directory = StaticDirectory(declaration)
    else:
        directory = (adapters or {}).get(declaration.directory)
    identity = asserter_identity_key(dict(asserter) if asserter else None)
    for role in declaration.roles:
        if "read" not in role.rights:
            continue
        for member in role.members:
            if member.kind == "any":
                return True
            if member.kind == "principal" and member.principal is not None:
                if identity and member.principal.key == identity:
                    return True
            elif member.kind == "group" and member.group is not None and directory is not None:
                found = directory.members(member.group)
                if found is not None and identity and identity in found:
                    return True
    return False


def _state(
    review: Review,
    mounts: Mapping[str, Path],
    asserter: Mapping[str, object] | None,
    adapters: Mapping[str, DirectoryAdapter] | None,
) -> str:
    if parse_every(review.every) is None:
        return BAD_INTERVAL
    root = mounts.get(review.source)
    if root is None:
        return NOT_MOUNTED
    declaration = load_store_declaration(root)
    if not isinstance(declaration, StoreDeclaration):
        return NO_GRANT  # no usable declaration grants nothing
    if not _holds_read(declaration, asserter, adapters):
        return NO_GRANT
    return RUNNABLE


def evaluate_reviews(
    target: StoreDeclaration,
    mounts: Mapping[str, Path],
    asserter: Mapping[str, object] | None,
    knowledge_root: Path,
    now: datetime | None = None,
    adapters: Mapping[str, DirectoryAdapter] | None = None,
) -> tuple[ReviewStatus, ...]:
    """The state of every review the *target* declares, in declaration order.

    *mounts* is ``store_id`` to surface root (see
    :func:`athenaeum.store_decisions.resolve_mounts`). Read-only: it reads the
    source's declaration and this install's own state, writes nothing and
    calls no model.
    """
    when = now or datetime.now(timezone.utc)
    runs = load_review_runs(knowledge_root, target.store_id)
    out: list[ReviewStatus] = []
    for review in target.reviews:
        state = _state(review, mounts, asserter, adapters)
        prev = last_run(runs, review)
        interval = parse_every(review.every)
        if interval is None:
            due = False
        elif prev is None:
            due = True
        else:
            at = _parse_time(prev.at)
            due = at is None or when >= at + interval
        out.append(ReviewStatus(review, state, due, prev))
    return tuple(out)


def record_review_run(
    knowledge_root: Path,
    target_store_id: str,
    status: ReviewStatus,
    revision: str,
    counts: Mapping[str, int],
    now: datetime | None = None,
) -> ReviewRun:
    """Record a completed run and advance the cursor to *revision*.

    Refuses a review that is not :data:`RUNNABLE`, so a review the source does
    not grant never moves its cursor. *counts* holds integers only: no source
    text can enter the record.
    """
    if status.state != RUNNABLE:
        raise ReviewRefused(f"review {status.review.name!r} is {status.state}; not recording")
    if not isinstance(revision, str) or not revision.strip():
        raise ReviewRefused("a run needs the source revision it reviewed")
    if any(not isinstance(k, str) or type(v) is not int for k, v in counts.items()):
        raise ReviewRefused("counts must map names to integers")
    run = ReviewRun(
        status.review.name,
        status.review.source,
        now_iso(now),
        revision.strip(),
        dict(counts),
    )
    rec = {
        "review": run.review,
        "source": run.source,
        "at": run.at,
        "revision": run.revision,
        "counts": dict(run.counts),
    }
    append_line_durable(
        state_path(knowledge_root, target_store_id),
        (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"),
    )
    return run
