# SPDX-License-Identifier: Apache-2.0
"""Generalized ENUMERATION primitive (issue athenaeum#965).

``recall`` narrows a **relevance-ranked** search — it cannot answer "give me
every entity of type X whose field Y matches, ordered by field Z" because
there is no query text to rank against (see ``docs/recall-architecture.md``
§"Type filter..." for why the type filter alone does not solve this). This
module is the distinct code path the issue asks for: given a DECLARED entity
type, zero or more field predicates, a sort key, and a limit — and no query
string at all — return every matching page, deterministically ordered and
paginated.

**Backend (AC amendment 3).** Enumeration reads the converged filterable-
metadata store issue athenaeum#964 built: :class:`athenaeum.search.FTS5Backend`'s
SQLite ``wiki`` table, specifically its ``type UNINDEXED`` column — the SAME
column ``recall``'s ``type_filter`` predicate already applies (see
:meth:`athenaeum.search.FTS5Backend.candidates_by_type`). This is a plain
indexed ``WHERE type = ?``, never routed through FTS5 ``MATCH``/BM25 ranking,
and it is what makes the athenaeum#964 -> athenaeum#965 dependency real: enumeration
cannot run without that column existing. It is deliberately NOT the
``keyword`` backend's full-corpus scan-on-query traversal (the issue's own
original Plan step 2, superseded by the 2026-08-20 amendment) — that would be
a second, independently-drifting frontmatter scanner duplicating work the
type-indexed SQL query already does for free.

That store has only seven columns (``filename``, ``name``, ``tags``,
``aliases``, ``description``, ``audience``, ``type``) — it does not, and per
the issue's "no new index structures" constraint must not, carry arbitrary
frontmatter fields like ``current_company`` or ``do_not_email``. So the type
column narrows the CANDIDATE set (bounded to pages of the requested type,
not the whole corpus), and this module reads each candidate's frontmatter
fresh from disk — the same "trust the index for narrowing, re-read fresh
frontmatter for content and authorization" pattern every other read layer in
this codebase already uses (Layer C in ``recall``, the CLI's ``cmd_recall``,
``entity_schema``'s field-key scan). It is bounded by the type-filtered
candidate count, not a fresh unconditional corpus-wide scan.

**Type values and field names (AC 3).** Permitted type values are derived
from :func:`athenaeum.entity_schema.resolve_entity_classes` — the SAME
resolver ``recall``'s dynamic tool-schema description and the ``entity_schema``
MCP tool already use (issue athenaeum#964) — never a hardcoded list. Predicate
and output field names are intentionally OPAQUE at the API boundary, exactly
like ``recall``'s ``type`` filter already is (``docs/recall-architecture.md``:
"never validated against wiki/_schema/types.md"): frontmatter is open-schema
(:func:`athenaeum.models.parse_frontmatter`'s own contract), so a predicate on
a field this deployment doesn't use simply matches nothing rather than being
rejected. ``entity_schema``'s reported ``fields`` per class is where a caller
DISCOVERS what is predicable — this module does not re-validate against it.

**PII-gated fields (AC amendment 1; narrowed by athenaeum#1122).** The gate
exists for fields whose VALUE is not itself sensitive but whose presence is a
durable, cross-system identifier — a key that lets a holder join this
person's wiki page to an out-of-band store. ``google_contact_*`` is exactly
that: a join key into a contact system outside the corpus. Gating it means a
caller must opt in (``with_pii=True``, the identical flag CONTRACT
``recall(with_pii=...)`` already uses — a boolean gate a caller opts into,
not the excluded-surface RECORD JOIN ``recall(with_pii=True)`` performs for a
person's contact data, see ``pii.assemble_excluded_read``) before this module
will use it as a predicate or return it as an output field, from the SAME
on-page frontmatter every other field is read from. Referencing a gated
field without the flag raises ``ValueError`` up front rather than silently
omitting it, so a caller cannot mistake "I forgot the flag" for "this field
has no value".

``do_not_email`` was gated here since ``athenaeum.enumeration``'s own
introduction (issue athenaeum#965 AC amendment 1) until athenaeum#1122, on the
theory that anything touching a person's email relationship warranted the
same guard as a contact join key. The operator
ruled that theory wrong: ``do_not_email`` is a suppression-opt-out BOOLEAN on
ordinary on-page frontmatter, with no excluded-surface record join and no
durable identifier value to protect — the same shape as ``current_company``
or any other plain field this module never gated. Gating it bought no
privacy protection while imposing a real cost: it made the SAFEST possible
question — "who may I *not* contact" (``enumerate_entities(predicates=["do_not_email
!= true"])``, the `ne`-predicate's own motivating example above) — require a
*broader* grant than asking who someone works for. athenaeum#1122 removed
``do_not_email`` from ``_PII_GATED_EXACT_FIELDS`` (now empty) and
deliberately did not add ``do_not_email_reason`` / ``do_not_email_date`` in
its place, for the same reason: a reason string and a date are no more a
durable cross-system identifier than the boolean they annotate. This is
unrelated to, and does not change, the separate ``recall`` / ``read_entity``
reverse-lookup path, where ``with_pii=True`` stays required because there the
lookup KEY is an email address on the excluded surface (see
``docs/authorized-reader-contract.md``) — this module never looks anything
up by address.

A caller that needs the full excluded-surface record for an enumerated hit
still follows up with ``recall(with_pii=True)`` or ``read_entity`` by the
returned ``uid`` — reusing that join here for every type-filtered candidate
would be a materially heavier operation with no acceptance criterion asking
for it, and this module's job is discovery (which uids match), not the deep
contact read.

**Audience scoping (issue athenaeum#538).** Fail-closed, identical to every
other read tool: :func:`athenaeum.models.is_page_authorized` re-checked
against each candidate's freshly-read frontmatter (Layer C) — a restricted
``caller_audience`` never sees a page it may not read, matching
``recall``/``read_entity``/``entity_schema``.

**Pagination (AC amendment 2).** ``limit`` (0 = unlimited, matching the
CLI's pre-existing ``--limit 0`` convention from ``athenaeum people``) plus
an opaque continuation cursor. Ordering is stable under pagination: results
are always sorted primarily by the caller-named ``sort_key`` (descending by
default) and secondarily — as the documented deterministic tiebreak — by
``uid`` ascending, regardless of the primary direction. The cursor encodes
the last-returned item's sort position; resuming re-derives the full
candidate/predicate/sort computation and skips forward, so it is a
best-effort continuation over a live corpus (like any cursor that is not a
frozen snapshot), not a transactionally consistent view.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.entity_schema import resolve_entity_classes
from athenaeum.models import is_page_authorized, parse_frontmatter, resolve_page_type
from athenaeum.search import FTS5Backend

#: Default row limit when the caller doesn't specify one (matches
#: ``athenaeum people``'s ``--limit`` default). ``0`` means unlimited,
#: matching that same command's ``--limit 0`` convention (AC amendment 2).
DEFAULT_LIMIT = 50

#: The three field-predicate kinds every AC test covers directly. ``ne``
#: (not-equal) is not a fourth independent kind — it is ``eq`` with
#: ``FieldPredicate.negate=True`` — but is accepted as a kind token at the
#: CLI/MCP parsing boundary for the ergonomic shape amendment 1's own example
#: uses (``do_not_email != true``).
PREDICATE_KINDS: tuple[str, ...] = ("eq", "substring", "regex")

#: Field names gated behind ``with_pii=True`` (AC amendment 1): an exact-name
#: set plus a prefix set (``google_contact``, ``google_contact_kromatic``, ...
#: are all durable-identifier fields sharing this one prefix). Checked at the
#: API boundary before any candidate is read, so a caller that forgets the
#: flag gets a loud, immediate error rather than a field that is silently
#: absent from every hit. Empty on purpose (athenaeum#1122 ungated
#: ``do_not_email``, its last member) — kept, rather than deleted, as the
#: honest declaration that the exact-name gate mechanism still exists and is
#: simply unpopulated today; see the "PII-gated fields" section below for
#: why nothing currently belongs here.
_PII_GATED_EXACT_FIELDS: frozenset[str] = frozenset()
_PII_GATED_FIELD_PREFIXES: tuple[str, ...] = ("google_contact",)


def is_pii_gated_field(field_name: str) -> bool:
    """True iff *field_name* requires ``with_pii=True`` to predicate/select on."""
    if field_name in _PII_GATED_EXACT_FIELDS:
        return True
    return any(field_name.startswith(p) for p in _PII_GATED_FIELD_PREFIXES)


@dataclass(frozen=True)
class FieldPredicate:
    """One field predicate: match *value* against a named field, or an
    ordered list of FALLBACK fields (AC: "an ordered list of fallback
    fields" — the shape ``athenaeum people --company`` uses across
    ``current_company`` / ``linkedin_company_at_connect``, generalized).

    ``fields`` is tried in order and the predicate matches if ANY of them
    matches (OR across fallback fields) — the "person-specific OR behaviour
    expressible generically" the AC asks for. Multiple ``FieldPredicate``
    instances passed to :func:`enumerate_entities` are combined with AND.
    """

    fields: tuple[str, ...]
    kind: str  # one of PREDICATE_KINDS
    value: str
    negate: bool = False

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("FieldPredicate.fields must be non-empty")
        if self.kind not in PREDICATE_KINDS:
            raise ValueError(
                f"Unknown predicate kind {self.kind!r}; expected one of {PREDICATE_KINDS}"
            )
        if self.kind == "regex":
            try:
                re.compile(self.value)
            except re.error as exc:
                raise ValueError(f"Invalid regex {self.value!r}: {exc}") from exc


def predicate_from_dict(raw: dict[str, Any]) -> FieldPredicate:
    """Build a :class:`FieldPredicate` from a JSON-shaped dict.

    The MCP ``enumerate_entities`` tool's wire shape for one predicate:
    ``{"fields": [...] | "field-name", "kind": "eq"|"ne"|"substring"|"regex",
    "value": "..."}`` — ``fields`` accepts either a bare string (single
    field, the common case) or a list (the ordered fallback-field shape).
    ``kind: "ne"`` is the same ergonomic negated-``eq`` convenience the CLI's
    ``--where ...:ne:...`` accepts (see ``_cmd_enumerate._parse_where``) —
    kept in sync deliberately so the two surfaces never drift.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"Predicate must be an object, got {type(raw).__name__}")
    fields_raw = raw.get("fields")
    if isinstance(fields_raw, str):
        fields: tuple[str, ...] = (fields_raw,)
    elif isinstance(fields_raw, list) and fields_raw:
        fields = tuple(str(f) for f in fields_raw)
    else:
        raise ValueError(
            f"Predicate 'fields' must be a non-empty string or list, got {fields_raw!r}"
        )
    kind = str(raw.get("kind", "")).strip().lower()
    negate = bool(raw.get("negate", False))
    if kind == "ne":
        kind = "eq"
        negate = True
    if "value" not in raw:
        raise ValueError("Predicate is missing required key 'value'")
    return FieldPredicate(fields=fields, kind=kind, value=str(raw["value"]), negate=negate)


@dataclass(frozen=True)
class EnumerationResult:
    """The return shape of :func:`enumerate_entities`."""

    hits: tuple[dict[str, Any], ...]
    next_cursor: str | None
    #: Populated (non-empty) only when ``entity_type`` was NOT one of this
    #: deployment's declared/observed classes (AC 4) — the "escalate rather
    #: than reject" list of classes this deployment DOES have. Empty on
    #: every ordinary (recognized-type) call, including one with zero hits.
    known_classes: tuple[str, ...] = ()


def _coerce_field_values(raw: object) -> list[str]:
    """Flatten a frontmatter value (scalar, bool, or list) to string tokens."""
    if raw is None:
        return []
    if isinstance(raw, bool):
        return ["true" if raw else "false"]
    if isinstance(raw, list):
        out: list[str] = []
        for v in raw:
            if isinstance(v, bool):
                out.append("true" if v else "false")
            else:
                s = str(v).strip()
                if s:
                    out.append(s)
        return out
    s = str(raw).strip()
    return [s] if s else []


def _value_matches(value: str, kind: str, needle: str) -> bool:
    """Single-value/single-kind match. Exact and substring are
    case-insensitive (regex is explicitly case-insensitive per the AC;
    exact/substring follow the same lenient convention so a boolean-ish
    frontmatter scalar like ``do_not_email: true`` matches a predicate value
    of ``"True"``/``"true"`` interchangeably)."""
    if kind == "eq":
        return value.lower() == needle.lower()
    if kind == "substring":
        return needle.lower() in value.lower()
    if kind == "regex":
        return re.search(needle, value, re.IGNORECASE) is not None
    # pragma: no cover — guarded by FieldPredicate.__post_init__
    raise ValueError(f"Unknown predicate kind: {kind}")


def _predicate_matches(meta: dict[str, object], predicate: FieldPredicate) -> bool:
    """True iff *meta* satisfies one predicate (OR across its fallback fields)."""
    matched = False
    for fname in predicate.fields:
        values = _coerce_field_values(meta.get(fname))
        if any(_value_matches(v, predicate.kind, predicate.value) for v in values):
            matched = True
            break
    return (not matched) if predicate.negate else matched


def _sort_value(meta: dict[str, object], sort_key: str) -> tuple[int, float, str]:
    """Canonical, universally-comparable sort key for one page.

    A raw frontmatter value that parses as a float sorts numerically
    (category 1); everything else — including a missing/empty value —
    sorts as its lowercased string form (category 0, empty string for
    missing). Documented deterministic behavior: mixing numeric-looking and
    non-numeric values for the same ``sort_key`` across a class's pages is
    an authoring inconsistency, not a crash — the two categories simply
    sort as distinct groups.
    """
    raw = meta.get(sort_key)
    if raw is None:
        return (0, 0.0, "")
    if isinstance(raw, bool):
        return (0, 0.0, "true" if raw else "false")
    if isinstance(raw, (int, float)):
        return (1, float(raw), "")
    text = str(raw).strip()
    if not text:
        return (0, 0.0, "")
    try:
        return (1, float(text), "")
    except ValueError:
        return (0, 0.0, text.lower())


def _resolve_candidate_path(
    filename: str, wiki_root: Path, extra_roots: Sequence[Path]
) -> Path | None:
    """Resolve an FTS5-indexed filename back to an on-disk path.

    Mirrors :func:`athenaeum.mcp_server._resolve_hit_path`'s two indexed
    filename shapes (bare wiki entry vs. ``<root_name>/<relpath>``) —
    duplicated rather than imported because ``mcp_server`` imports this
    module (to register the ``enumerate_entities`` tool) and importing
    back would cycle. Kept intentionally tiny so the two stay easy to
    keep in sync by inspection.
    """
    if "/" not in filename:
        return wiki_root / filename
    root_name, _, rel = filename.partition("/")
    for root in extra_roots:
        if root.name == root_name:
            return root / rel
    return None


def _json_safe(value: object) -> object:
    """Coerce an arbitrary frontmatter value to a JSON-serializable shape."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _encode_cursor(
    *,
    entity_type: str,
    sort_key: str,
    descending: bool,
    sort_tuple: tuple[int, float, str],
    uid: str,
) -> str:
    payload = {
        "entity_type": entity_type,
        "sort_key": sort_key,
        "descending": descending,
        "sort_tuple": list(sort_tuple),
        "uid": uid,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        # Any malformed cursor (bad base64, bad JSON, ...) is a caller error;
        # re-raised as ValueError below, so BLE001 does not flag this site.
        raise ValueError(f"Malformed enumeration cursor: {cursor!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Malformed enumeration cursor: {cursor!r}")
    return payload


def enumerate_entities(
    wiki_root: Path,
    cache_dir: Path,
    *,
    entity_type: str,
    predicates: Sequence[FieldPredicate] = (),
    sort_key: str = "name",
    descending: bool = True,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    fields: Sequence[str] = (),
    with_pii: bool = False,
    caller_audience: set[str] | None = None,
    extra_roots: Sequence[Path] | None = None,
    config: dict[str, Any] | None = None,
    known_classes: Collection[str] | None = None,
) -> EnumerationResult:
    """Return every ``entity_type`` page matching *predicates*, paginated.

    See the module docstring for the full contract (backend, field-name
    scoping, PII gating, audience scoping, pagination). Never raises for an
    unrecognized ``entity_type`` (AC 4) or an empty match — it raises
    :class:`ValueError` only for a genuine caller error: a PII-gated field
    referenced without ``with_pii=True``, or a cursor that does not belong
    to this exact ``(entity_type, sort_key, descending)`` triple.

    Args:
        wiki_root: Compiled wiki directory.
        cache_dir: Search index cache directory (the FTS5 index is built/
            refreshed here — same directory ``recall``/``rebuild-index`` use).
        entity_type: The declared entity class to enumerate (a page's
            ``type:``). Required — there is no "enumerate everything" mode;
            see the issue's "no proliferating typed interfaces" motivation
            for why type is a parameter, not an omittable one.
        predicates: Field predicates, AND-combined. Empty means "every page
            of this type" (AC 1's base case).
        sort_key: Frontmatter field name to sort by. Default ``"name"``.
        descending: Sort direction (default ``True`` per the AC). Ties are
            always broken by ``uid`` ascending, regardless of direction.
        limit: Max rows to return. ``0`` = unlimited (AC amendment 2, same
            convention as ``athenaeum people --limit 0``). Default
            :data:`DEFAULT_LIMIT`.
        cursor: Opaque continuation token from a prior call's
            ``next_cursor``. ``None`` starts from the beginning.
        fields: Additional declared field names to include per hit, beyond
            the always-present ``uid``/``type``/``name`` (AC amendment 1).
            A requested field absent from a page's frontmatter is included
            with value ``None`` — never silently dropped — so the output
            shape is stable across hits.
        with_pii: Required to reference a PII-gated field name
            (:func:`is_pii_gated_field`) as either a predicate field or a
            requested output field (AC amendment 1). Default ``False``.
        caller_audience: Fail-closed read-scope pin (issue athenaeum#538).
            ``None`` is the owner (unrestricted).
        extra_roots: Additional intake roots indexed alongside the wiki.
        config: Resolved ``athenaeum.yaml``, forwarded to the FTS5 index
            build.
        known_classes: Pre-computed declared/observed class name set. When
            ``None`` (the CLI's usage — a one-shot process), this function
            computes it via :func:`athenaeum.entity_schema.resolve_entity_classes`.
            The MCP server passes its OWN once-per-process-computed set
            (mirroring how ``recall``'s dynamic type description is computed
            once at ``create_server`` time, never per call) rather than
            paying a full corpus scan on every tool call.
    """
    extra_roots_list = list(extra_roots or [])

    all_predicate_fields = [f for p in predicates for f in p.fields]
    gated_requested = [f for f in (*all_predicate_fields, *fields) if is_pii_gated_field(f)]
    if gated_requested and not with_pii:
        raise ValueError(
            "Field(s) "
            f"{sorted(set(gated_requested))} require with_pii=True to use as a "
            "predicate or requested output field (issue athenaeum#965 AC amendment 1)."
        )

    if known_classes is None:
        known_classes = {
            c.name for c in resolve_entity_classes(wiki_root, caller_audience=caller_audience)
        }
    if entity_type not in known_classes:
        return EnumerationResult(
            hits=(), next_cursor=None, known_classes=tuple(sorted(known_classes))
        )

    if not wiki_root.is_dir():
        return EnumerationResult(hits=(), next_cursor=None)

    # Issue athenaeum#965 (AC amendment 3): read the converged filterable-metadata
    # store athenaeum#964 built. Ensure it is current (cheap incremental build —
    # the same manifest-diff machinery `recall`/`rebuild-index` already use),
    # then narrow to this type's candidates with a plain indexed WHERE — never
    # FTS5 MATCH/ranking.
    backend = FTS5Backend()
    backend.build_index(wiki_root, cache_dir, extra_roots=extra_roots_list, config=config)
    candidate_filenames = backend.candidates_by_type(cache_dir, entity_type)

    scored: list[tuple[tuple[int, float, str], str, dict[str, Any]]] = []
    for filename in candidate_filenames:
        page_path = _resolve_candidate_path(filename, wiki_root, extra_roots_list)
        if page_path is None or not page_path.is_file():
            continue
        try:
            text = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if not meta:
            continue
        # Layer C fail-closed audience re-check (issue athenaeum#538), identical
        # predicate to every other read tool.
        if not is_page_authorized(meta, caller_audience):
            continue
        if resolve_page_type(meta) != entity_type:
            # Defensive: a stale index entry (schema-version mismatch window,
            # concurrent edit) should never leak a wrong-typed hit.
            continue
        if not all(_predicate_matches(meta, p) for p in predicates):
            continue
        uid = str(meta.get("uid") or "").strip()
        if not uid:
            # Every hit must carry a uid (AC: "so a caller can call read_entity
            # without parsing the filename") — a page with none cannot satisfy
            # that contract, so it is excluded rather than returned incomplete.
            continue
        name = str(meta.get("name") or page_path.stem)
        hit: dict[str, Any] = {"uid": uid, "type": entity_type, "name": name}
        for f in fields:
            hit[f] = _json_safe(meta.get(f))
        scored.append((_sort_value(meta, sort_key), uid, hit))

    # Stable double sort: uid-ascending baseline, then the primary key —
    # Python's sort is stable, so ties on the primary key retain their
    # uid-ascending relative order regardless of `descending` (the
    # documented deterministic tiebreak, AC amendment 2).
    scored.sort(key=lambda row: row[1])
    scored.sort(key=lambda row: row[0], reverse=descending)

    start = 0
    if cursor is not None:
        payload = _decode_cursor(cursor)
        if (
            payload.get("entity_type") != entity_type
            or payload.get("sort_key") != sort_key
            or payload.get("descending") != descending
        ):
            raise ValueError(
                "Cursor does not match this call's (entity_type, sort_key, "
                "descending) — resume with the identical query shape."
            )
        cursor_key = (tuple(payload.get("sort_tuple", [])), payload.get("uid"))
        for idx, (sort_tuple, uid, _hit) in enumerate(scored):
            if (tuple(sort_tuple), uid) == cursor_key:
                start = idx + 1
                break
        else:
            # Stale cursor (referenced row no longer matches, or the corpus
            # changed) — best-effort resume from the start rather than
            # raising; pagination here is not a frozen snapshot.
            start = 0

    remaining = scored[start:]
    if limit and limit > 0:
        page = remaining[:limit]
        has_more = len(remaining) > limit
    else:
        page = remaining
        has_more = False

    next_cursor = None
    if has_more and page:
        last_sort_tuple, last_uid, _hit = page[-1]
        next_cursor = _encode_cursor(
            entity_type=entity_type,
            sort_key=sort_key,
            descending=descending,
            sort_tuple=last_sort_tuple,
            uid=last_uid,
        )

    return EnumerationResult(
        hits=tuple(hit for _st, _uid, hit in page),
        next_cursor=next_cursor,
    )
