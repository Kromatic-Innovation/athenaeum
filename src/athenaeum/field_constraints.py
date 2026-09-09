# SPDX-License-Identifier: Apache-2.0
"""Schema-declared per-type field constraints (issue athenaeum#1416).

Athenaeum ships no opinion about which frontmatter fields may appear on
which entity ``type``. This module is the missing member of the
``wiki/_schema/`` family that ``types.md`` / ``tags.md`` / ``access-levels.md``
already establish (see :func:`athenaeum.models.load_schema_list` and
:mod:`athenaeum.entity_schema`): the OPERATOR declares a per-type field
constraint, by hand, in ``wiki/_schema/field-constraints.md``; this module
is the only thing that reads that file, and the only thing that checks a
page against it.

**No entity type, field name, or value pattern is hardcoded here.** A
deployment with no ``field-constraints.md`` (or an empty one) gets
:func:`load_field_constraints` returning ``()``, and every check/guard
function below is a fast, unconditional no-op against an empty constraint
tuple — see each function's docstring for exactly where the fast-path
``return`` sits. The company/e-mail rule this issue was motivated by is
used ONLY as a worked example in this module's own tests and in
``docs/guides/field-constraints.md`` — never loaded by default, never
referenced by entity type or field name anywhere in this file.

## Schema format

``wiki/_schema/field-constraints.md`` is a markdown table, the same style
every other ``_schema/`` file already uses:

| Type | Field | Rule | Pattern |
|------|-------|------|---------|
| \\<entity type\\> | \\<field name\\> | forbidden / allow-pattern / deny-pattern | \\<regex\\> |

``Pattern`` is a Python regex, required for the two pattern rules.

- ``forbidden`` — the field must not appear (non-empty) at all on a page of
  this type.
- ``allow-pattern`` — every value in the field must match ``Pattern`` (via
  ``re.search``); a non-matching value is a violation.
- ``deny-pattern`` — any value matching ``Pattern`` is a violation.

A malformed row (unrecognized rule, a pattern rule with no ``Pattern``
column, or an invalid regex) is skipped with a ``log.warning`` naming the
row. It never raises, and it never silently falls back to a different
rule — an operator typo degrades to "this row enforces nothing," never to
an unintended enforcement.

``Field`` is normally a frontmatter key (``emails``, ``phones``, or any
other field a deployment's pages carry). One value is reserved and
structural rather than a frontmatter key: ``body`` (:data:`BODY_PSEUDO_FIELD`)
checks the page's rendered body markdown text as a single string. This
matters because a Tier-3 create or merge write is LLM-authored BODY
text — :func:`athenaeum.tiers.tier3_entity_from_text` and
:func:`athenaeum.tiers.tier3_merge` never emit a new frontmatter key — so
``field: body`` is how an operator's constraint reaches the actual byte
range a Tier-3 writer controls, rather than only catching contact data
that entered through a structured frontmatter field (e.g. an external
adapter import).

## What this mechanism can and cannot express (AC6 honesty requirement)

``allow-pattern`` / ``deny-pattern`` are STRING-SHAPE tests only — a regex
against the literal frontmatter value. They carry no semantic notion of
*who owns* an address (a person vs. an organisation). A real corpus can
contain genuine personal addresses whose local part is shape-identical to
a role address (short initials at a company domain, indistinguishable
from ``info@`` / ``sales@`` by pattern alone). **This mechanism cannot
reliably tell those apart, and does not try to.** An operator who declares
a pattern rule aimed at "role addresses only" is choosing a heuristic with
known false positives/negatives on short local parts; nothing in this
module hides that trade-off behind a confident-sounding rule name — see
``docs/guides/field-constraints.md`` for the same caveat spelled out for
the operator.

Layering: L2. Imports only :mod:`athenaeum.models` (frontmatter
primitives — the same table-row scanning convention
:func:`~athenaeum.models.load_schema_list` uses), :mod:`athenaeum.atomic_io`,
and :mod:`athenaeum.store` (the shared durable-ledger primitives every
other write-boundary guard in this layer already uses, e.g.
:mod:`athenaeum.wiki_write_guard`). Imports nothing from L3+
(:mod:`athenaeum.tiers`, :mod:`athenaeum.librarian`), so either may import
this module at write time without an import cycle.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, resolve_page_type
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Filename this module reads from ``<wiki_root>/_schema/``. Absent by
#: default in every install this package ships — see this module's
#: docstring: shipping a populated default here would be shipping a policy.
FIELD_CONSTRAINTS_FILENAME = "field-constraints.md"

_VALID_RULES: frozenset[str] = frozenset({"forbidden", "allow-pattern", "deny-pattern"})
_PATTERN_RULES: frozenset[str] = frozenset({"allow-pattern", "deny-pattern"})

#: Sidecar dir/ledger a refused write's rendered content is parked in —
#: mirrors :mod:`athenaeum.wiki_write_guard`'s ``_type_rejected/`` /
#: ``_type_rejected.jsonl`` shape, kept as its OWN distinct name (a
#: different object: a page that violated a declared FIELD constraint, not
#: a TYPE guard refusal) so a future reader never conflates the two
#: ledgers' record shapes.
FIELD_CONSTRAINT_REJECTED_DIR_NAME = "_field_constraint_rejected"
FIELD_CONSTRAINT_LEDGER_NAME = "_field_constraint_violations.jsonl"


@dataclass(frozen=True)
class FieldConstraint:
    """One declared row: ``<entity_type, field>`` may/must not carry certain values."""

    entity_type: str
    field: str
    rule: str
    pattern: str | None = None


@dataclass(frozen=True)
class ConstraintViolation:
    """One page's one broken declared constraint — report-only, never auto-repaired.

    ``value`` is the specific offending value for a pattern rule, or the
    (first-seen) present value for ``forbidden`` — always the raw string
    that tripped the rule, never a summary, so a ledger reader can see
    exactly what was rejected.
    """

    entity_type: str
    field: str
    rule: str
    value: str | None
    uid: str = ""
    name: str = ""
    path: str = ""


def _split_table_row(stripped: str) -> list[str]:
    """Split one already-stripped ``|``-delimited row into cells.

    Honors ``\\|`` as an escaped, literal pipe (the standard markdown-table
    escaping convention) rather than a cell boundary — needed because a
    regex ``Pattern`` cell legitimately wants a bare ``|`` for alternation
    (``^(info|sales|support)@``), and a naive split-on-``|`` would tear
    such a pattern into extra cells.
    """
    cells: list[str] = []
    current: list[str] = []
    body = stripped.strip("|")
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def _iter_table_rows(text: str) -> list[list[str]]:
    """Return every markdown-table data row's cells, header/separator skipped.

    Same row-classification convention as
    :func:`athenaeum.models.load_schema_list` (a separator row is one where
    every non-pipe/space character is ``-``; the row immediately before a
    separator is a header). Unlike ``load_schema_list`` — which keeps only
    the first non-empty cell, enough for a flat vocabulary list — this
    returns every cell (via :func:`_split_table_row`, escaped-pipe aware),
    because one field constraint needs (type, field, rule, pattern) together.
    """
    lines = text.splitlines()
    separator_indices: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and all(c in "-| " for c in stripped):
            separator_indices.add(i)

    rows: list[list[str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if i in separator_indices or (i + 1) in separator_indices:
            continue
        rows.append(_split_table_row(stripped))
    return rows


def load_field_constraints(wiki_root: Path) -> tuple[FieldConstraint, ...]:
    """Load declared per-type field constraints from ``_schema/field-constraints.md``.

    Returns ``()`` when the file is absent, unreadable, or carries no valid
    data rows — the SAME "absent file yields today's behaviour" contract
    every other ``_schema/`` loader in this codebase gives
    (:func:`athenaeum.models.load_schema_list`,
    :func:`athenaeum.entity_schema.declared_entity_classes`). This is what
    makes a fresh install with an empty ``_schema/`` a true no-op (AC2):
    every check/guard function below fast-returns on an empty tuple before
    examining a single page.
    """
    path = wiki_root / "_schema" / FIELD_CONSTRAINTS_FILENAME
    if not path.exists():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()

    constraints: list[FieldConstraint] = []
    for row in _iter_table_rows(text):
        if len(row) < 3:
            continue
        entity_type, field, rule = row[0], row[1], row[2]
        pattern: str | None = row[3] if len(row) > 3 else ""
        if not entity_type or not field or not rule:
            continue
        if rule not in _VALID_RULES:
            log.warning(
                "field-constraints.md: unrecognized rule %r for %s.%s -- "
                "row skipped, not enforced (issue athenaeum#1416)",
                rule,
                entity_type,
                field,
            )
            continue
        if rule in _PATTERN_RULES:
            if not pattern:
                log.warning(
                    "field-constraints.md: %s rule for %s.%s has no Pattern "
                    "column -- row skipped, not enforced",
                    rule,
                    entity_type,
                    field,
                )
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                log.warning(
                    "field-constraints.md: invalid regex %r for %s.%s (%s) "
                    "-- row skipped, not enforced",
                    pattern,
                    entity_type,
                    field,
                    exc,
                )
                continue
        else:
            pattern = None
        constraints.append(
            FieldConstraint(entity_type=entity_type, field=field, rule=rule, pattern=pattern)
        )
    return tuple(constraints)


#: Reserved ``Field`` column value meaning "the page's body markdown text,"
#: rather than a frontmatter key. Not a hardcoded TYPE/FIELD opinion (it
#: names no entity type and asserts nothing about what belongs there) —
#: it is a structural pseudo-field, the same kind of name ``type``/``uid``
#: already are among frontmatter keys, available to a constraint on ANY
#: declared type. This is what makes AC5's own counter-example
#: expressible at all: Tier-3 create/merge writes are LLM-authored BODY
#: text (see :func:`athenaeum.tiers.tier3_entity_from_text` /
#: :func:`athenaeum.tiers.tier3_merge` — neither ever emits a new
#: frontmatter key), so a constraint that only ever inspected frontmatter
#: could never catch that counter-example no matter how it was declared.
BODY_PSEUDO_FIELD = "body"


def _field_values(meta: dict[str, Any], body: str, field: str) -> list[str]:
    """Return the non-empty string value(s) one declared *field* carries.

    ``field == "body"`` (:data:`BODY_PSEUDO_FIELD`) checks the page's
    rendered body text as a single value. Every other *field* name is
    looked up in *meta*, mirroring
    :func:`athenaeum.pii._frontmatter_contact_values`'s scalar-or-list
    normalisation: a frontmatter field is stored either as a bare scalar
    or as a list; both shapes are treated identically here.
    """
    if field == BODY_PSEUDO_FIELD:
        return [body] if body.strip() else []
    raw = meta.get(field)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).strip() for v in values if str(v).strip()]


def check_entity_fields(
    meta: dict[str, Any],
    constraints: Sequence[FieldConstraint],
    *,
    body: str = "",
    uid: str = "",
    name: str = "",
    path: str = "",
) -> list[ConstraintViolation]:
    """Check one page's frontmatter (and, via ``field: body``, its body text)
    against every DECLARED constraint for its type.

    Returns ``[]`` immediately when *constraints* is empty (AC2's fast
    path — the caller need not even resolve the page's type) or when the
    page's resolved type has no declared row (a deployment that
    constrains ``company`` never touches a ``person`` page). Never
    mutates *meta* or *body* — report-only (AC7): callers decide whether a
    violation blocks a write; this function only says what is wrong.
    """
    if not constraints:
        return []
    etype = resolve_page_type(meta)
    if not etype:
        return []
    relevant = [c for c in constraints if c.entity_type == etype]
    if not relevant:
        return []

    violations: list[ConstraintViolation] = []
    _uid = uid or str(meta.get("uid", "") or "")
    _name = name or str(meta.get("name", "") or "")
    for c in relevant:
        values = _field_values(meta, body, c.field)
        if not values:
            continue
        if c.rule == "forbidden":
            for v in values:
                violations.append(
                    ConstraintViolation(
                        entity_type=etype,
                        field=c.field,
                        rule=c.rule,
                        value=v,
                        uid=_uid,
                        name=_name,
                        path=path,
                    )
                )
        elif c.rule == "allow-pattern":
            assert c.pattern is not None  # guaranteed by load_field_constraints
            rx = re.compile(c.pattern)
            for v in values:
                if not rx.search(v):
                    violations.append(
                        ConstraintViolation(
                            entity_type=etype,
                            field=c.field,
                            rule=c.rule,
                            value=v,
                            uid=_uid,
                            name=_name,
                            path=path,
                        )
                    )
        elif c.rule == "deny-pattern":
            assert c.pattern is not None  # guaranteed by load_field_constraints
            rx = re.compile(c.pattern)
            for v in values:
                if rx.search(v):
                    violations.append(
                        ConstraintViolation(
                            entity_type=etype,
                            field=c.field,
                            rule=c.rule,
                            value=v,
                            uid=_uid,
                            name=_name,
                            path=path,
                        )
                    )
    return violations


def scan_field_constraint_violations(wiki_root: Path) -> list[ConstraintViolation]:
    """Scan every page for a violation of a DECLARED field constraint (AC4).

    Cheap: with nothing declared this costs exactly one ``stat()`` (the
    ``field-constraints.md`` existence check in
    :func:`load_field_constraints`) and no filesystem walk at all. When
    constraints ARE declared, this is a single flat pass over
    ``wiki_root`` — same shallow, underscore-excluding convention as
    :func:`athenaeum.entity_schema.resolve_entity_classes` — cheap enough
    to run on every intake pass or a routine cron sweep. The issue's own
    motivating evidence (146 violations undetected for three months) is a
    LATENCY failure; this closes it by being fast enough to run OFTEN, not
    by being exhaustive over history.
    """
    constraints = load_field_constraints(wiki_root)
    if not constraints:
        return []
    if not wiki_root.is_dir():
        return []
    violations: list[ConstraintViolation] = []
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if not meta:
            continue
        violations.extend(
            check_entity_fields(meta, constraints, body=body, path=fpath.name)
        )
    return violations


def guard_entity_field_constraints(
    wiki_root: Path,
    filename: str,
    rendered: str,
    meta: dict[str, Any],
    *,
    source: str = "unknown",
) -> tuple[bool, tuple[ConstraintViolation, ...]]:
    """Admit or refuse one entity write by its declared per-type field constraints.

    Mirrors :func:`athenaeum.wiki_write_guard.guard_entity_write_type`'s
    refuse-and-surface shape (AC5 — the write is CONSTRAINED, not merely
    prompted). Returns ``(True, ())`` when the write is clean — including,
    per AC2, EVERY write when *wiki_root* has no declared constraints at
    all, without this function even parsing *meta*.

    Returns ``(False, violations)`` when :func:`check_entity_fields` finds
    one or more. The caller MUST skip its normal ``atomic_write_text`` for
    this write and count the refusal — this function itself parks
    *rendered* byte-for-byte at
    ``<wiki_root>/_field_constraint_rejected/<filename>`` and appends one
    ledger record per violation to
    ``<wiki_root>/_field_constraint_violations.jsonl`` before returning, so
    nothing is lost and nothing is silently repaired (AC7 — refusing the
    write, rather than stripping the offending field and writing the rest,
    is exactly what avoids deleting a value that exists nowhere else). For
    a refused MERGE, the pre-existing page on disk is left byte-for-byte
    untouched, since the caller never reaches its own write call.
    """
    constraints = load_field_constraints(wiki_root)
    if not constraints:
        return True, ()
    # Re-parse *rendered* for its body rather than trusting a caller-supplied
    # body string, mirroring the "re-parse to see exactly what would land on
    # disk" round-trip already used at both librarian.py call sites (see
    # their own comments) — this is what makes a ``field: body`` constraint
    # check the ACTUAL bytes about to be written, not a stale copy.
    _, body = parse_frontmatter(rendered)
    violations = tuple(
        check_entity_fields(meta, constraints, body=body, path=filename)
    )
    if not violations:
        return True, ()

    rejected_dir = wiki_root / FIELD_CONSTRAINT_REJECTED_DIR_NAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(rejected_dir / filename, rendered)

    for v in violations:
        record = {
            "ts": now_iso(),
            "filename": filename,
            "type": v.entity_type,
            "field": v.field,
            "rule": v.rule,
            "value": v.value,
            "uid": v.uid,
            "name": v.name,
            "source": source,
        }
        append_line_durable(
            wiki_root / FIELD_CONSTRAINT_LEDGER_NAME,
            (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
        )
    log.warning(
        "wiki write REJECTED (issue athenaeum#1416): %d declared field "
        "constraint violation(s) for %s -- parked at %s/%s, recorded in %s",
        len(violations),
        filename,
        FIELD_CONSTRAINT_REJECTED_DIR_NAME,
        filename,
        FIELD_CONSTRAINT_LEDGER_NAME,
    )
    return False, violations


def list_field_constraint_violations(wiki_root: Path) -> list[dict[str, Any]]:
    """Read every well-formed record from the field-constraint ledger.

    Tolerant reader (a torn trailing line from a crash mid-append is
    skipped, not raised on), the same convention as
    :func:`athenaeum.wiki_write_guard.list_type_rejected`. Returns ``[]``
    when the ledger does not exist.
    """
    path = wiki_root / FIELD_CONSTRAINT_LEDGER_NAME
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


__all__ = [
    "FIELD_CONSTRAINTS_FILENAME",
    "FIELD_CONSTRAINT_REJECTED_DIR_NAME",
    "FIELD_CONSTRAINT_LEDGER_NAME",
    "BODY_PSEUDO_FIELD",
    "FieldConstraint",
    "ConstraintViolation",
    "load_field_constraints",
    "check_entity_fields",
    "scan_field_constraint_violations",
    "guard_entity_field_constraints",
    "list_field_constraint_violations",
]
