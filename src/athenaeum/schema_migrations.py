# SPDX-License-Identifier: Apache-2.0
"""Declared page ``schema_version`` and the kernel-field migration registry
(issue athenaeum#1628).

Every wiki page carries an integer ``schema_version:`` describing which
DECLARED kernel fields it is expected to carry. A page with no
``schema_version`` at all reads as version 0 (:func:`page_schema_version`) —
nothing has ever stamped it. :data:`MIGRATIONS` is the one place a new
kernel-field migration is declared; nothing else in this codebase should
hard-code a list of "fields the audit pass fills" or "fields a rule
backfill stamps" — read them off a page's :func:`pending_migrations`
instead (see :mod:`athenaeum.audit`, which does exactly that for its own
fields-to-determine, and :mod:`athenaeum.schema_migrate`, which does it for
the eager rule path).

Two kinds of migration, distinguished by *how* the field is filled and
*when* the version bumps (issue athenaeum#1628 Decisions 3/4):

- ``derivation="rule"`` / ``timing="eager"``: a deterministic
  :attr:`Migration.apply` callable computes the field(s) from a page's own
  frontmatter, with no model call. Applied corpus-wide, dry-run by default,
  by the ``athenaeum schema migrate`` command
  (:mod:`athenaeum.schema_migrate` / ``_cmd_schema.py``).
- ``derivation="model"`` / ``timing="on-audit"``: the field(s) are decided
  by the audit pass's LLM call (:mod:`athenaeum.audit`) — there is no
  ``apply`` callable; a model migration NEVER carries one (see
  :func:`validate_migrations`). The page's ``schema_version`` only advances
  past a model migration once every one of its fields is either filled or
  recorded ``undeterminable`` in ``audit_findings`` — :mod:`athenaeum.audit`
  owns that bump, using this registry to know which fields and which
  version to check.

A YAML-declared extension for operator-defined, non-kernel migrations is
explicitly OUT of scope here (issue athenaeum#1628's own "Out of scope"
section) — this module is the Python registry for KERNEL fields only.

Import-time validation (:func:`validate_migrations`, run once against
:data:`MIGRATIONS` at import time below) enforces three invariants so a
malformed entry fails loudly at import rather than silently corrupting a
page's version arithmetic: versions are contiguous starting from 0 (each
migration's ``from_version`` equals the previous migration's
``to_version``, and the first is 0), every migration id is unique, and
``derivation="rule"`` entries carry an ``apply`` callable while
``derivation="model"`` entries never do.

Layering: L0 primitive (leaf). Stdlib-only (``dataclasses`` + ``typing``) —
no athenaeum import, no I/O, no network. Every function here is pure over
an already-parsed frontmatter dict; nothing reads or writes a page. Imported
by :mod:`athenaeum.audit` (L4), :mod:`athenaeum.schema_migrate` (L2) and
``_cmd_schema.py`` (L5) — being L0 means those upward imports are all
downward-into-this-module, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

Derivation = Literal["rule", "model"]
Timing = Literal["eager", "on-audit"]


@dataclass(frozen=True)
class Migration:
    """One declared kernel-field schema migration.

    ``fields`` — the frontmatter keys this migration is responsible for.
    Empty for a migration that stamps nothing of its own beyond the version
    marker itself (the v0->v1 worked example below).

    ``apply`` — required (non-``None``) for ``derivation="rule"``, and
    REFUSED (must be ``None``) for ``derivation="model"`` —
    :func:`validate_migrations` enforces both directions. A rule's
    ``apply(meta)`` returns a ``dict`` of the field values it derives from
    *meta*'s own already-populated fields; it never performs I/O and never
    mutates *meta* itself (the caller — :mod:`athenaeum.schema_migrate` —
    owns merging the result into a page, honoring the same "never overwrite
    a populated field" invariant every other in-place page editor in this
    codebase holds).
    """

    id: str
    fields: tuple[str, ...]
    from_version: int
    to_version: int
    derivation: Derivation
    timing: Timing
    apply: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _stamp_only(meta: dict[str, Any]) -> dict[str, Any]:
    """v0->v1's ``apply``: the worked rule-based example (issue athenaeum#1628
    Plan item 2). Produces no field of its own — advancing past this
    migration means only the version marker itself moves, which is exactly
    what a page with no declared kernel fields yet needs to record "this
    page has entered schema-version tracking"."""
    return {}


#: One model-derivation migration's fields, unioned in declaration order —
#: :mod:`athenaeum.audit` re-derives its own ``COORDINATE_FIELDS`` from this
#: exact mechanism (union over every ``derivation="model"`` entry in
#: :data:`MIGRATIONS`), never from a literal tuple. Declared here, ahead of
#: :data:`MIGRATIONS`, purely to keep the two initial entries below
#: self-documenting about which fields the v1->v2 entry names.
_COORDINATE_FIELDS: tuple[str, ...] = ("valid_from", "valid_until", "claimed_scope")

#: The two initial entries (issue athenaeum#1628 Plan item 2).
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="schema-v1-stamp",
        fields=(),
        from_version=0,
        to_version=1,
        derivation="rule",
        timing="eager",
        apply=_stamp_only,
    ),
    Migration(
        id="coordinate-fields-v2",
        fields=_COORDINATE_FIELDS,
        from_version=1,
        to_version=2,
        derivation="model",
        timing="on-audit",
        apply=None,
    ),
)


def validate_migrations(migrations: tuple[Migration, ...]) -> None:
    """Enforce the three import-time invariants over *migrations*.

    Raises :class:`ValueError` on the first violation found, naming the
    offending migration id. Exposed as a public function (rather than only
    ever running privately against :data:`MIGRATIONS`) specifically so a
    test can prove each failure mode without needing to reimport this
    module with a monkeypatched registry — see ``tests/test_schema_migrations.py``.
    """
    seen_ids: set[str] = set()
    expected_from = 0
    for migration in migrations:
        if migration.id in seen_ids:
            raise ValueError(f"schema_migrations: duplicate migration id {migration.id!r}")
        seen_ids.add(migration.id)

        if migration.from_version != expected_from:
            raise ValueError(
                f"schema_migrations: version gap before migration {migration.id!r} — "
                f"starts at {migration.from_version}, expected {expected_from} "
                "(versions must be contiguous from 0)"
            )
        if migration.to_version <= migration.from_version:
            raise ValueError(
                f"schema_migrations: migration {migration.id!r} does not advance "
                f"the version ({migration.from_version} -> {migration.to_version})"
            )

        if migration.derivation == "rule" and migration.apply is None:
            raise ValueError(
                f"schema_migrations: rule migration {migration.id!r} needs an apply callable"
            )
        if migration.derivation == "model" and migration.apply is not None:
            raise ValueError(
                f"schema_migrations: model migration {migration.id!r} must not carry "
                "an apply callable — model fields are decided by the audit pass, "
                "never applied mechanically"
            )

        expected_from = migration.to_version


validate_migrations(MIGRATIONS)

#: The highest version any migration in the registry advances to — a page
#: at this version has no pending migration left at all.
CURRENT_SCHEMA_VERSION: int = max((m.to_version for m in MIGRATIONS), default=0)


def page_schema_version(meta: dict[str, Any]) -> int:
    """*meta*'s declared schema version, defaulting to 0 when absent.

    A missing key, a non-numeric value, or a boolean (YAML's ``true``/
    ``false`` are ``int`` subclasses in Python — explicitly excluded so a
    stray ``schema_version: true`` reads as "absent", not "version 1")
    all read as version 0, matching issue athenaeum#1628 Decision 1: "a page
    without it is version 0".
    """
    raw = meta.get("schema_version")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return 0


def pending_migrations(meta: dict[str, Any]) -> tuple[Migration, ...]:
    """Every migration *meta* has not yet fully incorporated, in declared
    (ascending) order — i.e. every migration whose ``to_version`` is
    strictly greater than :func:`page_schema_version`'s reading of *meta*.
    """
    current = page_schema_version(meta)
    return tuple(m for m in MIGRATIONS if m.to_version > current)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "Derivation",
    "Migration",
    "Timing",
    "page_schema_version",
    "pending_migrations",
    "validate_migrations",
]
