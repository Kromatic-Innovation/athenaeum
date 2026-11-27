# SPDX-License-Identifier: Apache-2.0
"""Undeclared vault data: the record scan and the rules that reproduce the old default.

Nothing is private unless the operator declares it (athenaeum#3170). Until that
release a class mapped to a location outside the corpus got a field list it
never wrote: the contact fields ``emails``, ``phones``, ``former_emails`` and
``alt_emails`` for the ``pii`` class, and every field on the record for any
other class. That default is gone. An installation that relied on it is
stopped before it writes, and told exactly what to declare.

Two halves:

* the configuration half, :func:`athenaeum.storage.undeclared_vault_mappings`,
  needs no file access and runs when a writing command starts and on a
  sensitive read;
* the record scan, :func:`scan_undeclared_fields`, reads the records held
  outside the corpus and runs in ``athenaeum doctor`` and ``athenaeum storage
  status``, not on every run.

:func:`suggested_routes` rebuilds the previous effective field list with the
rules that were removed, so the printed ``storage.routes`` entries reproduce the
previous behaviour exactly. Athenaeum never edits the configuration: it prints.
Nothing here moves, rewrites or deletes a record.

Layering: L3 service. Imports :mod:`athenaeum.pii` (L3) and
:mod:`athenaeum.storage` / :mod:`athenaeum.config` below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum import pii
from athenaeum.storage import (
    PREVIOUS_READ_MAPPING,
    SUGGEST_ROUTES_COMMAND,
    UndeclaredMapping,
    UndeclaredVaultDataError,
    available_adapters,
    configured_routes,
    routes_yaml,
    undeclared_vault_mappings,
)

__all__ = [
    "EXIT_UNDECLARED_VAULT_DATA",
    "SuggestedRoute",
    "UncoveredField",
    "previous_field_list",
    "refusal_message",
    "refuse_if_undeclared",
    "require_declared_vault_data",
    "scan_undeclared_fields",
    "suggested_routes",
    "suggested_routes_text",
    "undeclared_report",
]


@dataclass(frozen=True)
class SuggestedRoute:
    """One ``storage.routes`` entry that reproduces the previous behaviour.

    Attributes:
        page_type: The page type the rule names.
        fields: The previous effective field list, in its previous order;
            ``None`` for a whole-page rule.
        location: The location the class mapped to.
        surface_class: The ``storage.mapping`` key the rule stands in for.
        replaces: Configuration keys the rule cannot sit beside and replaces
            (for example ``storage.mapping.<type>``); empty for the common case.
    """

    page_type: str
    fields: tuple[str, ...] | None
    location: str
    surface_class: str
    replaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class UncoveredField:
    """A field held in a location outside the corpus that no rule covers.

    Counts and names only: never a value.
    """

    location: str
    field: str
    records: int


def _record_meta(path: Path) -> dict[str, Any]:
    return pii.read_bounce_record(path)


def _is_page_root(root: Path, knowledge_root: Path) -> bool:
    """True when *root* is the wiki tree or inside it: it holds pages, not records."""
    try:
        wiki = (Path(knowledge_root) / "wiki").resolve()
        resolved = root.resolve()
    except OSError:
        return False
    return resolved == wiki or wiki in resolved.parents


def previous_field_list(
    surface_class: str, config: dict[str, Any] | None, knowledge_root: Path
) -> tuple[str, ...]:
    """The field list the previous release used for *surface_class*.

    The four contact fields for the ``pii`` class, else the field names actually present on the
    class's records (first-seen order over the records in sorted path order,
    bookkeeping keys left out). Reads the records only for the last case.
    """
    if surface_class == pii.PII_ENTITY_CLASS:
        return pii.BUILTIN_CONTACT_FIELDS
    root = pii.excluded_surface_root(surface_class, Path(knowledge_root), config)
    if _is_page_root(root, knowledge_root):
        return ()
    names: dict[str, None] = {}
    for path in pii.iter_contact_records(root):
        for name in _record_meta(path):
            key = str(name)
            if key not in pii.EXCLUDED_RECORD_BOOKKEEPING_FIELDS:
                names.setdefault(key, None)
    return tuple(names)


def _page_types_for_surface(surface_class: str) -> list[str]:
    """The page types whose excluded fields are read from *surface_class*."""
    table = PREVIOUS_READ_MAPPING
    types = [p for p, s in table.items() if s == surface_class and p != surface_class]
    if not types and table.get(surface_class, surface_class) == surface_class:
        types = [surface_class]
    return types


def suggested_routes(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    mappings: tuple[UndeclaredMapping, ...] | None = None,
) -> tuple[SuggestedRoute, ...]:
    """The ``storage.routes`` entries that reproduce the previous behaviour.

    A class read through other page types (``pii``, read by ``person``) gets a
    field rule per page type. A class that is itself the page type
    (``storage.mapping.<type>``) gets a whole-page rule that replaces the
    mapping entry, so its pages keep their location and corpus policy; when
    the location is a vault holding records with fields, a field rule is added
    so the read of those fields is unchanged too.
    """
    if mappings is None:
        mappings = undeclared_vault_mappings(config)
    adapters = available_adapters(config)
    out: list[SuggestedRoute] = []
    for mapping in mappings:
        fields = previous_field_list(mapping.surface_class, config, knowledge_root)
        sensitive = bool(getattr(adapters.get(mapping.location), "sensitive", False))
        for page_type in _page_types_for_surface(mapping.surface_class):
            replaces: list[str] = []
            if page_type == mapping.surface_class:
                replaces.append(f"storage.mapping.{page_type}")
                out.append(
                    SuggestedRoute(
                        page_type=page_type,
                        fields=None,
                        location=mapping.location,
                        surface_class=mapping.surface_class,
                        replaces=tuple(replaces),
                    )
                )
                if not (fields and sensitive):
                    continue
            elif not fields:
                continue
            out.append(
                SuggestedRoute(
                    page_type=page_type,
                    fields=fields,
                    location=mapping.location,
                    surface_class=mapping.surface_class,
                    replaces=tuple(replaces),
                )
            )
    return tuple(out)


def suggested_routes_text(routes: tuple[SuggestedRoute, ...]) -> str:
    """*routes* as YAML to paste into ``athenaeum.yaml`` (append to an existing list)."""
    return routes_yaml([(r.page_type, r.fields, r.location) for r in routes])


CHOOSE_OTHERWISE = (
    "To choose otherwise: delete a field from a printed rule to keep it on the page "
    "from now on (values already stored stay in the location, and `athenaeum storage "
    "status` lists them); or move it to its own rule with an empty target, "
    "`- {page_type: <type>, fields: [<field>], to: []}`, to stop storing it. "
    "Athenaeum does not edit athenaeum.yaml."
)


def refusal_message(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    mappings: tuple[UndeclaredMapping, ...] | None = None,
) -> str:
    """The full refusal: what is undeclared, the exact rules, and the choices."""
    if mappings is None:
        mappings = undeclared_vault_mappings(config)
    routes = suggested_routes(config, knowledge_root, mappings)
    lines = [
        "Undeclared vault data. These storage.mapping entries send records to a "
        "location outside the corpus, but no rule says which fields are kept there:",
    ]
    for mapping in mappings:
        lines.append(f"  storage.mapping.{mapping.surface_class} -> {mapping.location}")
    lines.append(
        "Athenaeum no longer assumes a built-in list of private fields. Nothing was "
        "written. To keep the previous behaviour, add these entries to storage.routes "
        "in athenaeum.yaml:"
    )
    lines.append("")
    if routes:
        lines.append(suggested_routes_text(routes))
    else:
        lines.append("  (no stored field to declare)")
    replaced = sorted({key for route in routes for key in route.replaces})
    if replaced:
        lines.append("")
        lines.append("and remove these keys, which the rules replace: " + ", ".join(replaced))
    suggested = {route.surface_class for route in routes}
    empty = [m for m in mappings if m.surface_class not in suggested]
    for mapping in empty:
        lines.append("")
        lines.append(
            f"storage.mapping.{mapping.surface_class} -> {mapping.location} holds no "
            "field: add a rule for the fields you mean to keep there, or remove the entry."
        )
    lines.append("")
    lines.append(CHOOSE_OTHERWISE)
    lines.append(f"The same rules are printed by `{SUGGEST_ROUTES_COMMAND}`.")
    return "\n".join(lines)


def require_declared_vault_data(config: dict[str, Any] | None, knowledge_root: Path) -> None:
    """Refuse to start a writing command on undeclared vault data (athenaeum#3170).

    The configuration half only (no file access) decides; the rules in the
    message are rebuilt from the records only when it refuses.

    Raises:
        UndeclaredVaultDataError: with :func:`refusal_message` as its message.
    """
    mappings = undeclared_vault_mappings(config)
    if mappings:
        raise UndeclaredVaultDataError(refusal_message(config, knowledge_root, mappings), mappings)


def _covered_fields(config: dict[str, Any] | None) -> dict[str, set[str]]:
    """Location -> the field names some rule keeps there."""
    covered: dict[str, set[str]] = {}
    for route in configured_routes(config):
        if route.fields is None:
            continue
        for location in route.to:
            covered.setdefault(location, set()).update(route.fields)
    return covered


def _whole_page_types(config: dict[str, Any] | None) -> dict[str, set[str]]:
    """Location -> the page types a rule sends there whole."""
    whole: dict[str, set[str]] = {}
    for route in configured_routes(config):
        if route.page_type is None or route.fields is not None or route.discard:
            continue
        for location in route.to:
            whole.setdefault(location, set()).add(route.page_type)
    return whole


def scan_undeclared_fields(
    config: dict[str, Any] | None, knowledge_root: Path
) -> tuple[UncoveredField, ...]:
    """Every field held outside the corpus that no rule covers, with its record count.

    Scans each location outside the corpus. A record whose ``type`` a rule
    sends there whole is a page, covered as a whole. Bookkeeping keys are the
    record's own machinery and never count. Names and counts only. Read-only.
    """
    covered = _covered_fields(config)
    whole = _whole_page_types(config)
    out: list[UncoveredField] = []
    roots_seen: set[Path] = set()
    for name, adapter in sorted(available_adapters(config).items()):
        if adapter.corpus_policy.in_corpus:
            continue
        root = adapter.resolve_root(Path(knowledge_root))
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in roots_seen or not root.is_dir() or _is_page_root(root, knowledge_root):
            continue
        roots_seen.add(resolved)
        counts: dict[str, int] = {}
        for path in pii.iter_contact_records(root):
            meta = _record_meta(path)
            if str(meta.get("type") or "") in whole.get(name, set()):
                continue
            for field, value in meta.items():
                key = str(field)
                if key in pii.EXCLUDED_RECORD_BOOKKEEPING_FIELDS:
                    continue
                if key in covered.get(name, set()):
                    continue
                if value is None or value == [] or value == "":
                    continue
                counts[key] = counts.get(key, 0) + 1
        for field, records in sorted(counts.items()):
            out.append(UncoveredField(location=name, field=field, records=records))
    return tuple(out)


def undeclared_report(config: dict[str, Any] | None, knowledge_root: Path) -> dict[str, Any]:
    """Both halves as one plain dict, for ``doctor`` and ``storage status``.

    ``undeclared`` is true when either half finds something.
    """
    mappings = undeclared_vault_mappings(config)
    uncovered = scan_undeclared_fields(config, knowledge_root)
    routes = suggested_routes(config, knowledge_root, mappings)
    return {
        "undeclared": bool(mappings or uncovered),
        "mappings": [{"surface_class": m.surface_class, "location": m.location} for m in mappings],
        "uncovered_fields": [
            {"location": u.location, "field": u.field, "records": u.records} for u in uncovered
        ],
        "suggested_routes": [
            {
                "page_type": r.page_type,
                "fields": list(r.fields) if r.fields is not None else None,
                "to": [r.location],
                "replaces": list(r.replaces),
            }
            for r in routes
        ],
        "suggested_routes_text": suggested_routes_text(routes) if routes else "",
    }


#: Exit status of a command refused for undeclared vault data.
EXIT_UNDECLARED_VAULT_DATA = 2


def refuse_if_undeclared(config: dict[str, Any] | None, knowledge_root: Path) -> int | None:
    """For a writing command: print the refusal to stderr and return its exit status.

    Returns ``None`` when the installation declares all its vault data, so the
    command goes on. Call it after loading the configuration and before taking
    a lock or writing anything.
    """
    import sys

    try:
        require_declared_vault_data(config, knowledge_root)
    except UndeclaredVaultDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNDECLARED_VAULT_DATA
    return None
