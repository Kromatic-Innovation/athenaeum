"""Resolve recorded push ids against the local corpus (issue athenaeum#1528).

Push records carry an opaque uid and nothing else — :func:`athenaeum.push_metrics.opaque_push_id`
records a frontmatter ``uid`` precisely so the *ledger artifact* holds no
name-derived content. That redaction is about the artifact, which may be
aggregated, shipped, or read off-machine.

This module does the other half, for the viewer only: given a uid, find that
page in the operator's own ``~/knowledge`` and read its name, one-line
description and path. The join happens at render time, in a localhost-only
process, against a corpus the operator already owns — the same thing
``read_entity`` does. **The ledger stays content-free; the view does the join.**
Nothing here ever writes to the corpus or to a ledger.

Why a whole-tree index rather than a targeted glob: pages live as
``<uid>-<slug>.md`` and the slug is unknown from the uid alone, so a direct
path cannot be constructed. Measured on the reference corpus: 24,116 uids
indexed from 25,507 files in 0.21s, which is cheap enough to do per request and
keeps the viewer stateless. Frontmatter is parsed only for the handful of pages
actually displayed, never for the whole corpus.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A recorded push id: the 8-hex-char frontmatter ``uid`` every compiled page
#: carries. Anchored and length-bounded because this value reaches
#: :func:`resolve_path`, which is a filesystem lookup — a permissive pattern
#: here is how a traversal attempt would get a chance to matter.
UID_RE = re.compile(r"^[0-9a-f]{6,32}$")

#: Filename shape the corpus compiles to: ``<uid>-<slug>.md``.
_PAGE_RE = re.compile(r"^([0-9a-f]{6,32})-.*\.md$")

#: The OTHER id shape in the ledger. :func:`athenaeum.push_metrics.opaque_push_id`
#: falls back to the bare filename for a page carrying no frontmatter ``uid``
#: (auto-memory pages, chiefly), so ~18% of recorded ids on the reference
#: deployment are filenames rather than uids. Without this they all render as
#: "no page found", which looks like corpus rot rather than a second id shape.
#:
#: A BASENAME only — no directory separators, no ``..`` — because this value
#: reaches :func:`resolve_path`, which is the editor-open security boundary.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md$")

#: Cap on a rendered description. The frontmatter value is already clamped
#: upstream, but a hand-edited page can carry anything and the viewer must not
#: be the place a 40KB paragraph lands in a table cell.
_DESCRIPTION_LIMIT = 240


@dataclass
class PageInfo:
    """What the viewer can say about one recorded uid.

    ``resolved`` False means the uid is in the ledger but no page carries it
    any more — a deleted or renamed page, or a ledger row older than a corpus
    rebuild. The viewer must render that state visibly rather than as a blank
    cell: "we pushed something and can no longer say what" is information.
    """

    uid: str
    resolved: bool = False
    name: str = ""
    description: str = ""
    path: str = ""
    related: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "name": self.name,
            "description": self.description,
            "path": self.path,
        }


def build_uid_index(wiki_root: Path) -> dict[str, list[str]]:
    """Map every recorded id under *wiki_root* to its absolute path.

    Indexes BOTH id shapes the ledger uses: the frontmatter uid parsed out of
    ``<uid>-<slug>.md``, and the bare filename (see :data:`_FILENAME_RE`).

    *wiki_root* is deliberately the ``wiki/`` subdirectory and not the
    knowledge root. Sibling surfaces — ``excluded/`` above all, which is where
    the operator routes material off-corpus — are therefore outside the tree
    this function walks AND outside the containment check in
    :func:`resolve_path`. An id whose page lives in ``excluded/`` stays
    unresolved and unopenable here, which is the intended behaviour and not an
    oversight: excluded material is read through ``recall(with_pii=True)`` or
    ``read_entity``, never by a viewer opening a path.

    A missing or unreadable root yields an empty index rather than raising:
    the viewer degrades to unresolved ids, which is legible on screen, instead
    of failing the whole page over a corpus problem.
    """
    index: dict[str, list[str]] = {}
    root = Path(wiki_root)
    if not root.is_dir():
        return index
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            full = os.path.join(dirpath, filename)
            # EVERY candidate is kept, not just the first. A uid prefix is not
            # unique in practice: `2dbd1b8c` matches both the real page
            # `2dbd1b8c-verify-list-parity.md` and the merge artifact
            # `2dbd1b8c-3ac82004-a90ce50f.md`, which carries no frontmatter at
            # all. First-writer-wins silently picked the artifact and rendered
            # a nameless row. :func:`load_page_info` disambiguates by looking
            # for a usable page among the candidates -- which it can afford to
            # do because it only ever runs for pages actually on screen.
            match = _PAGE_RE.match(filename)
            if match:
                index.setdefault(match.group(1), []).append(full)
            if _FILENAME_RE.match(filename):
                index.setdefault(filename, []).append(full)
    for paths in index.values():
        paths.sort()
    return index


def _read_frontmatter_block(path: Path) -> list[str]:
    """Return the raw frontmatter lines, or ``[]``.

    Reads only until the closing delimiter — page bodies run to many KB and
    nothing below the frontmatter is ever displayed, so there is no reason to
    pull a whole corpus's worth of prose through this function.
    """
    lines: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
            if first.strip() != "---":
                return []
            for line in handle:
                if line.strip() == "---":
                    break
                lines.append(line.rstrip("\n"))
                if len(lines) > 400:  # pathological frontmatter; stop reading
                    break
    except OSError:
        return []
    return lines


def _scalar(lines: list[str], key: str) -> str:
    """First top-level ``key: value`` scalar, unquoted and length-clamped.

    Deliberately a line scan rather than a YAML parse: this module is on the
    render path for every page shown, the two fields it wants are always plain
    top-level scalars, and a malformed page must degrade to "no description"
    rather than raise a parse error into the viewer.
    """
    prefix = key + ":"
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            return value[:_DESCRIPTION_LIMIT]
    return ""


def _related_uids(lines: list[str]) -> list[str]:
    """Every uid appearing in the page's ``related:`` block.

    ``related`` is a list of ``{uid, role}`` mappings, which the corpus writes
    both inline and block-style, so this collects uid-shaped tokens from the
    lines following the key until the next top-level key. Over-collecting is
    the safe direction here: the result only ever *widens* what counts as a
    breadcrumb, and a wrong uid simply never matches anything pulled.
    """
    out: list[str] = []
    in_block = False
    for line in lines:
        if line.startswith("related:"):
            in_block = True
            out.extend(re.findall(r"[0-9a-f]{6,32}", line))
            continue
        if in_block:
            # A new top-level key ends the block; list items are indented.
            if line and not line[0].isspace() and not line.lstrip().startswith("-"):
                break
            out.extend(re.findall(r"[0-9a-f]{6,32}", line))
    return out


def is_recorded_id(value: str) -> bool:
    """Whether *value* is one of the two id shapes the ledger records."""
    return bool(UID_RE.match(value or "") or _FILENAME_RE.match(value or ""))


def _first_heading(path: Path) -> str:
    """First markdown ``# `` heading, for a page carrying no frontmatter name.

    Merge artifacts and some auto-memory pages have no frontmatter at all but
    do open with a heading. Using it beats showing the operator a bare id.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(60):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return line[2:].strip()[:_DESCRIPTION_LIMIT]
    except OSError:
        return ""
    return ""


def load_page_info(uid: str, index: dict[str, list[str]]) -> PageInfo:
    """Resolve one recorded id to its name/description/related, via *index*.

    When several files carry the id, the one whose frontmatter actually names
    the page wins over one that merely shares the prefix -- see
    :func:`build_uid_index` for why a prefix collision is routine rather than
    exotic. Falling back through frontmatter name -> first heading -> filename
    stem means a resolved page is never rendered as if it did not exist.
    """
    if not is_recorded_id(uid):
        return PageInfo(uid=uid or "")
    candidates = index.get(uid) or []
    if not candidates:
        return PageInfo(uid=uid)

    best: PageInfo | None = None
    for path_str in candidates:
        path = Path(path_str)
        lines = _read_frontmatter_block(path)
        info = PageInfo(
            uid=uid,
            resolved=True,
            name=_scalar(lines, "name"),
            description=_scalar(lines, "description"),
            path=path_str,
            related=_related_uids(lines),
        )
        if info.name:
            return info
        if best is None:
            best = info

    assert best is not None  # candidates was non-empty
    # No candidate carried a frontmatter name: derive something legible rather
    # than leaving the row looking like a missing page.
    best.name = _first_heading(Path(best.path)) or Path(best.path).stem
    return best


def resolve_path(
    uid: str, *, wiki_root: Path, index: dict[str, list[str]] | None = None
) -> Path | None:
    """Absolute path for *uid*, or ``None`` — confined to *wiki_root*.

    This is the function behind the editor-open route, so it is the security
    boundary and not merely a lookup:

    - the id must match :data:`UID_RE` or :data:`_FILENAME_RE` — both reject
      anything containing a separator or ``..`` — so nothing path-shaped is
      ever looked up in the first place;
    - the result is ``resolve()``d and re-checked against the ``resolve()``d
      root, which collapses ``..`` AND follows symlinks — so a symlink inside
      the corpus pointing at ``/etc/passwd`` fails the check rather than
      passing it, which a purely textual prefix test would not catch.
    """
    if not is_recorded_id(uid):
        return None
    lookup = build_uid_index(wiki_root) if index is None else index
    # Resolve through the same preference order the display uses, so clicking a
    # row opens the page whose name is on that row rather than a prefix-sharing
    # sibling.
    chosen = load_page_info(uid, lookup).path
    if not chosen:
        return None
    try:
        candidate = Path(chosen).resolve(strict=True)
        root = Path(wiki_root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(root):
        return None
    if not candidate.is_file():
        return None
    return candidate
