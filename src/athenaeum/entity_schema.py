# SPDX-License-Identifier: Apache-2.0
"""Entity-class registry resolver — the read-path schema-discovery service.

Issue athenaeum#964: ``recall``'s new ``type`` filter needs a way for BOTH the tool
schema itself (the ``type`` parameter's description, computed at server
construction) and a new schema-query MCP tool to answer "what kinds of things
exist here, and what is queryable?" without hardcoding a class list anywhere.

This module owns exactly that read: :func:`resolve_entity_classes` scans the
compiled wiki once and returns, per entity class, whether it is DECLARED
(present in the operator-editable ``wiki/_schema/types.md`` registry),
OBSERVED (at least one live page carries that ``type:``), its page count, and
the union of frontmatter field KEYS its pages carry (values are never
reported — see the excluded-field omission below). The declared/observed
split matters because the two can legitimately drift: an operator adds a new
class to the corpus before updating ``types.md``, or vice versa — the issue's
own evidence found ``auto-memory`` (559 pages live) absent from a real
``types.md``. Neither state is an error; the resolver reports both facts
rather than picking one as canonical (AC amendment 3).

Layering: a read-path leaf service (peer to :mod:`athenaeum.identity_resolution`
/ :mod:`athenaeum.provenance`). Imports :mod:`athenaeum.models` (frontmatter
primitives, ``load_schema_list``, the ``resolve_page_type`` precedence
resolver — the SAME one :mod:`athenaeum.search` uses for the FTS5/vector
``type`` columns, so a page is found/classified identically by both), and
:mod:`athenaeum.pii` (:data:`~athenaeum.pii.CONTACT_FRONTMATTER_FIELDS`, for
the excluded-field omission rule below — never any PII *value*, only the
field-name allowlist). Never imports :mod:`athenaeum.mcp_server` — the MCP
tool wiring is the CALLER's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from athenaeum.models import (
    is_page_authorized,
    load_schema_list,
    parse_frontmatter,
    resolve_page_type,
)
from athenaeum.pii import CONTACT_FRONTMATTER_FIELDS
from athenaeum.schemas import KNOWN_TYPES

#: The set of frontmatter field NAMES this module will never report in an
#: :class:`EntityClassInfo.fields` tuple, even though the corpus may carry
#: them inline (issue athenaeum#427's "belt-and-suspenders" case — the same fields
#: :func:`athenaeum.pii.is_pii_flagged`/the contact-surface architecture treat
#: as off-corpus). Keys only are ever considered elsewhere in this module;
#: this additionally drops the specific keys that would identify an
#: excluded-surface field's NAME, so the schema-query tool can never become a
#: "which PII fields exist" oracle even by naming (not valuing) them.
_EXCLUDED_SURFACE_FIELD_NAMES: frozenset[str] = frozenset(CONTACT_FRONTMATTER_FIELDS)

#: The fields the ``type`` filter (issue athenaeum#964) implements today. The ONE
#: new MCP tool this issue adds must report this set literally, per the
#: issue's own AC ("must not advertise any field the type filter does not
#: implement") — a single source so the schema-query tool and any future
#: query-capability addition update this list, not a scattered literal.
QUERYABLE_FIELDS: tuple[str, ...] = ("type",)


@dataclass(frozen=True)
class EntityClassInfo:
    """One entity class's declared/observed status, count, and field keys."""

    name: str
    count: int
    declared: bool
    observed: bool
    fields: tuple[str, ...]


def _load_declared_types(wiki_root: Path) -> frozenset[str]:
    """Return the declared entity classes from ``<wiki_root>/_schema/types.md``.

    Falls back to :data:`athenaeum.schemas.KNOWN_TYPES` — the SAME collapsed
    fallback :mod:`athenaeum.librarian` now uses (issue athenaeum#964 AC 9's "one
    shared fallback", replacing the two independently-drifted copies) — when
    the file is absent OR present-but-empty, both of which
    :func:`~athenaeum.models.load_schema_list` reports as ``[]``. This is the
    "does not hard-fail" degrade path: a deployment with no (or an empty)
    ``types.md`` still gets a non-empty declared set rather than an error.
    """
    schema_dir = wiki_root / "_schema"
    declared = load_schema_list(schema_dir, "types.md")
    return frozenset(declared) if declared else frozenset(KNOWN_TYPES)


def resolve_entity_classes(
    wiki_root: Path,
    *,
    caller_audience: set[str] | None = None,
) -> tuple[EntityClassInfo, ...]:
    """Return every entity class this deployment declares and/or observes.

    A single flat scan of ``wiki_root`` (same shallow, underscore-excluding
    convention as :class:`athenaeum.models.EntityIndex`) computes, per class:
    the live page count, whether it is declared in ``types.md``, whether at
    least one page observes it, and the union of frontmatter field KEYS its
    pages carry (excluded-surface field names omitted — see
    :data:`_EXCLUDED_SURFACE_FIELD_NAMES`; values are never read for this
    purpose at all).

    ``caller_audience`` (issues athenaeum#312/#538): ``None`` is the owner (every
    page counted). A non-None restricted caller only has pages it is
    authorized to read counted/observed — the same fail-closed
    :func:`~athenaeum.models.is_page_authorized` predicate every other read
    tool applies, so a restricted caller's class list cannot leak the
    existence of pages it cannot read.

    Result is sorted by class name for a deterministic, diff-friendly return
    shape (this backs both the ``recall`` tool schema and the schema-query
    tool — both want a stable order).
    """
    declared = _load_declared_types(wiki_root)
    counts: dict[str, int] = {}
    fields: dict[str, set[str]] = {}

    if wiki_root.is_dir():
        for fpath in sorted(wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue
            if not is_page_authorized(meta, caller_audience):
                continue
            cls = resolve_page_type(meta)
            if not cls:
                continue
            counts[cls] = counts.get(cls, 0) + 1
            keys = {
                str(k) for k in meta.keys() if str(k) not in _EXCLUDED_SURFACE_FIELD_NAMES
            }
            fields.setdefault(cls, set()).update(keys)

    names = sorted(declared | set(counts))
    return tuple(
        EntityClassInfo(
            name=name,
            count=counts.get(name, 0),
            declared=name in declared,
            observed=name in counts,
            fields=tuple(sorted(fields.get(name, set()))),
        )
        for name in names
    )
