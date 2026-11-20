# SPDX-License-Identifier: Apache-2.0
"""A decision queue per store, and the join that lists them (athenaeum#2934).

Layering: L2 (imports :mod:`athenaeum.store_access`, :mod:`athenaeum.store_rights`,
L2, and :mod:`athenaeum.storage`, :mod:`athenaeum.models`, :mod:`athenaeum.store`,
L1 and below).

Design: ``docs/design/federation-store-access.md`` §7 (the compile role and the
decision queue), §12 slice S7.

* **A decision belongs to exactly one store**, the store whose page it would
  change. :func:`file_decision` appends it to that store's own queue file
  (``_store_decisions/<store_id>.jsonl`` under the knowledge root, local state:
  nothing here is committed to, or shared through, a store). The queues are
  separate files, so reading one never reads another.
* **An item quotes only its owning store's pages.** Every page handed to
  :func:`file_decision` is a :class:`PageQuote` carrying its own ``store_id``.
  Those of the owning store are quoted (uid, title, gist, path relative to the
  store root). Every other is reduced, at write time, to ``{store_id, uid}``:
  no text, no title, no path. The reduction is in the writer, so no caller can
  forget it.
* **Other stores' pages are filled in on display** (:func:`render_item`), from
  the viewer's own mounts and only when the viewer holds ``read`` there. A page
  the viewer cannot read is shown only as an unreadable page.
* **Only an install that holds compile files a decision** about a store's
  pages (a store with a declaration; an install whose rights resolve to anything
  less is refused).
* **One listing.** :func:`collect_store_decisions` returns the items of every
  mounted store on which the owner holds ``answer``. A store on which the owner
  does not hold it contributes nothing. :mod:`athenaeum.decisions` joins this
  with the personal store's queue; the personal queue keeps its own existing
  files and is labelled :data:`PERSONAL_STORE_ID`.

Single compiling install only. Answering from a second install (the bench) is
access S9 and is not built here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter
from athenaeum.storage import available_adapters
from athenaeum.store import append_line_durable
from athenaeum.store_access import StoreDeclaration, is_store_id, load_store_declaration
from athenaeum.store_rights import resolve_rights

__all__ = [
    "PERSONAL_STORE_ID",
    "QUEUE_DIRNAME",
    "ITEM_TYPE",
    "PageQuote",
    "PageRef",
    "StoreDecisionRefused",
    "resolve_mounts",
    "queue_path",
    "file_decision",
    "load_queue",
    "render_item",
    "collect_store_decisions",
]

log = logging.getLogger(__name__)

#: The ``store_id`` the listing gives items of the personal store (the default
#: wiki and its existing queue files). It is not ``st_``-shaped, so it can never
#: collide with a declared store.
PERSONAL_STORE_ID = "personal"

#: Directory under the knowledge root holding one queue file per store.
QUEUE_DIRNAME = "_store_decisions"

#: ``type`` of an item built from a per-store queue record.
ITEM_TYPE = "store-decision"

#: A uid is a filename stem prefix; refuse anything that could widen a glob or
#: leave the store root.
_UID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.\-]*$")

_GIST_LIMIT = 160


class StoreDecisionRefused(ValueError):
    """A decision could not be filed with the store it names."""


@dataclass(frozen=True)
class PageQuote:
    """A page a decision involves, with the text that may be quoted.

    ``store_id`` is the store the page lives in. The text is quoted only when
    that is the decision's owning store.
    """

    store_id: str
    uid: str
    title: str = ""
    gist: str = ""
    path: str = ""


@dataclass(frozen=True)
class PageRef:
    """A page of another store, by ``store_id`` and uid alone."""

    store_id: str
    uid: str


def _check_uid(uid: str) -> str:
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise StoreDecisionRefused(f"not a usable page uid: {uid!r}")
    return uid


def _one_line(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _GIST_LIMIT else flat[: _GIST_LIMIT - 1].rstrip() + "…"


def _relative(path: str) -> str:
    """A path as stored: never absolute, never climbing."""
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        return ""
    return p.as_posix()


# -- mounts -----------------------------------------------------------------


def resolve_mounts(knowledge_root: Path, config: dict[str, Any] | None) -> dict[str, Path]:
    """The stores this install holds: ``store_id`` to the root of the surface.

    A surface counts when its root is a directory carrying a declaration that
    validated. Where two surfaces declare the same ``store_id`` the first by
    surface name wins, so the answer does not depend on iteration order. A bad
    storage config yields no mounts rather than raising: this runs on a read.
    """
    mounts: dict[str, Path] = {}
    try:
        adapters = available_adapters(config)
    except Exception as exc:  # noqa: BLE001 - a read must not fail on config
        log.warning("store decisions: storage config unusable (%s)", type(exc).__name__)
        return mounts
    for name in sorted(adapters):
        root = adapters[name].resolve_root(knowledge_root)
        if not root.is_dir():
            continue
        declaration = load_store_declaration(root)
        if isinstance(declaration, StoreDeclaration):
            mounts.setdefault(declaration.store_id, root)
    return mounts


def _rights(
    store_id: str,
    mounts: Mapping[str, Path],
    asserter: Mapping[str, object] | None,
) -> frozenset[str]:
    """What the owner holds on a mounted store; empty when it is not mounted."""
    root = mounts.get(store_id)
    if root is None:
        return frozenset()
    declaration = load_store_declaration(root)
    if not isinstance(declaration, StoreDeclaration):
        return frozenset({"read"})  # an invalid declaration resolves as a reader
    return resolve_rights(declaration, asserter)


# -- the per-store queue ----------------------------------------------------


def queue_path(knowledge_root: Path, store_id: str) -> Path:
    """The queue file of one store. Refuses a value that is not a store id."""
    if not is_store_id(store_id):
        raise StoreDecisionRefused(f"not a store id: {store_id!r}")
    return Path(knowledge_root) / QUEUE_DIRNAME / f"{store_id}.jsonl"


def load_queue(knowledge_root: Path, store_id: str) -> list[dict]:
    """Every record of one store's queue (``[]`` when it has none).

    Reads that one file and no other. A line that does not parse is skipped.
    """
    path = queue_path(knowledge_root, store_id)
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def file_decision(
    knowledge_root: Path,
    *,
    store_id: str,
    question: str,
    pages: Iterable[PageQuote | PageRef] = (),
    kind: str = "question",
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
    created_at: str | None = None,
) -> dict:
    """File one decision with the store whose page it would change.

    Pages of *store_id* are quoted; a page of any other store is stored as its
    ``store_id`` and uid only. A :class:`PageRef` may name only another store.

    When *mounts* is given the store must be mounted and the owner (*asserter*)
    must hold ``compile`` on it: only a compiling install raises decisions about
    a store's pages. Returns the stored record.
    """
    if mounts is not None and "compile" not in _rights(store_id, mounts, asserter):
        raise StoreDecisionRefused(
            f"this install does not hold compile on {store_id}; it raises no decision about it"
        )
    path = queue_path(knowledge_root, store_id)
    quoted: list[dict[str, str]] = []
    others: list[dict[str, str]] = []
    seen_other: set[tuple[str, str]] = set()
    for page in pages:
        uid = _check_uid(page.uid)
        if not is_store_id(page.store_id):
            raise StoreDecisionRefused(f"not a store id: {page.store_id!r}")
        if page.store_id == store_id:
            if isinstance(page, PageRef):
                raise StoreDecisionRefused(
                    "a PageRef names a page of another store; quote the owning store's page"
                )
            quoted.append(
                {
                    "uid": uid,
                    "title": _one_line(page.title),
                    "gist": _one_line(page.gist),
                    "path": _relative(page.path),
                }
            )
        elif (page.store_id, uid) not in seen_other:
            seen_other.add((page.store_id, uid))
            others.append({"store_id": page.store_id, "uid": uid})
    record = {
        "id": f"{store_id}-{uuid.uuid4().hex[:8]}",
        "store_id": store_id,
        "kind": str(kind),
        "question": str(question),
        "created_at": created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disposition": "pending",
        "pages": quoted,
        "other_pages": others,
    }
    # One O_APPEND write per record (review on PR athenaeum#2980): concurrent
    # filers never overwrite each other, and a torn trailing line is skipped by
    # :func:`load_queue`.
    append_line_durable(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    return record


# -- display ----------------------------------------------------------------


def _find_page(root: Path, uid: str) -> Path | None:
    """The page whose filename is ``<uid>.md`` or ``<uid>-<slug>.md`` under *root*."""
    try:
        for hit in sorted(root.rglob(f"{uid}*.md")):
            if "_store" in hit.relative_to(root).parts or not hit.is_file():
                continue
            if hit.stem == uid or hit.stem.startswith(uid + "-"):
                return hit
    except OSError:
        return None
    return None


def _fill(root: Path, uid: str) -> dict[str, str] | None:
    page = _find_page(root, uid)
    if page is None:
        return None
    try:
        meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    title = str(meta.get("name") or "") if isinstance(meta, dict) else ""
    gist = str(meta.get("description") or "") if isinstance(meta, dict) else ""
    if not gist:
        gist = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return {"title": _one_line(title or page.stem), "gist": _one_line(gist)}


def render_item(
    record: Mapping[str, Any],
    mounts: Mapping[str, Path],
    asserter: Mapping[str, object] | None,
) -> dict:
    """The listing item for one stored record, as the viewer is entitled to see it.

    Each other-store page is filled in from the viewer's own mount of that store
    when the viewer holds ``read`` there. Otherwise it is reported as
    ``readable: False`` with its ``store_id`` and uid and nothing else.
    """
    shown: list[dict[str, Any]] = []
    for ref in record.get("other_pages", []):
        entry: dict[str, Any] = {"store_id": ref["store_id"], "uid": ref["uid"], "readable": False}
        if "read" in _rights(ref["store_id"], mounts, asserter):
            filled = _fill(mounts[ref["store_id"]], ref["uid"])
            if filled is not None:
                entry.update(filled)
                entry["readable"] = True
        shown.append(entry)
    return {
        "type": ITEM_TYPE,
        "id": record["id"],
        "store_id": record["store_id"],
        "created_at": record.get("created_at"),
        "summary": record.get("question", ""),
        "confidence": None,
        "payload": {
            "kind": record.get("kind", "question"),
            "pages": [dict(p) for p in record.get("pages", [])],
            "other_pages": shown,
        },
    }


def collect_store_decisions(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    asserter: Mapping[str, object] | None,
    *,
    mounts: Mapping[str, Path] | None = None,
) -> list[dict]:
    """Pending items of every mounted store on which the owner holds ``answer``.

    A store without that right is not even read: its queue file is never opened.
    """
    held = dict(mounts) if mounts is not None else resolve_mounts(knowledge_root, config)
    items: list[dict] = []
    for store_id in sorted(held):
        if "answer" not in _rights(store_id, held, asserter):
            continue
        for rec in load_queue(knowledge_root, store_id):
            if rec.get("disposition") == "pending" and rec.get("store_id") == store_id:
                items.append(render_item(rec, held, asserter))
    return items


def stores_with_queue(knowledge_root: Path) -> Sequence[str]:
    """Store ids that have a queue file (for diagnostics and tests)."""
    base = Path(knowledge_root) / QUEUE_DIRNAME
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("st_*.jsonl"))
