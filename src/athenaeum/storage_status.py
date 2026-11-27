# SPDX-License-Identifier: Apache-2.0
"""The read-only report behind ``athenaeum storage status`` (athenaeum#3181).

What an operator who has set up vaults needs to see: what the configuration
actually does, including the parts that are weaker than they look (a vault
inside a git repository, a field with nowhere to go, a value that stays in raw
intake).

This module holds no logic of its own. Every row is read from the one place
that already answers the question:

* locations: :func:`athenaeum.storage.available_adapters`;
* routes and discards: :func:`athenaeum.storage.configured_routes` and
  :func:`athenaeum.storage.route_for`;
* who may read a vault: the store declaration
  (:func:`athenaeum.store_access.load_store_declaration`) confirmed through the
  access predicate :func:`athenaeum.access.can`;
* the history setting: :func:`athenaeum.config.resolve_storage_history`;
* raw intake: :mod:`athenaeum.raw_screening`, the same function the run's
  notice is printed from.

It writes nothing: no file under the knowledge root, the cache directory or any
state directory. The fixed sentences below are the report's only prose.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.access` (L3),
:mod:`athenaeum.raw_screening` (L4), :mod:`athenaeum.storage` and
:mod:`athenaeum.config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athenaeum import pii
from athenaeum.access import can
from athenaeum.config import resolve_storage_history
from athenaeum.raw_screening import (
    REACH_TEXT,
    hold_active,
    hold_messages,
    knowledge_dir_is_git_repo,
    raw_in_git_tree,
    unscreened_raw_files,
)
from athenaeum.storage import (
    available_adapters,
    configured_routes,
    inside_git_repository,
    route_for,
)
from athenaeum.store_access import StoreDeclaration, StoreDeclarationInvalid, load_store_declaration

__all__ = ["build_status", "format_status"]

VAULT_IN_GIT_WARNING = (
    "Vault {name!r} is inside a git working tree: an erasure does not reach its history."
)
RAW_INTAKE_WARNING = (
    "Raw intake is {where} a git working tree; {unscreened} raw file(s) are unscreened, "
    "{held} held out of commits."
)
MACHINE_ACCESS_LINE = (
    "On this machine, any process running as this account can read a vault's files directly."
)
HISTORY_MEANING = {
    "git": {
        "restore": (
            "History is kept in git: a snapshot is committed before a destructive "
            "operation, and a restore point can be returned to."
        ),
        "retirement": (
            "Retirement commits each raw file first, so its removal can be undone."
        ),
    },
    "none": {
        "restore": (
            "No history is kept (plain files): there is no restore point and no undo."
        ),
        "retirement": "Retirement removes a raw file with no way to get it back.",
    },
}
OWNER_ONLY = "owner only"
KEPT_ON_PAGE = "kept on the page"
DISCARDED = "discarded"


def _readers(
    name: str, root: Path, config: dict[str, Any] | None, knowledge_root: Path
) -> dict[str, Any]:
    """Who holds read on the vault *name*, from its declaration and the predicate."""
    declaration = load_store_declaration(root)
    if declaration is None or isinstance(declaration, StoreDeclarationInvalid):
        return {
            "declaration": "none" if declaration is None else "invalid",
            "owner_only": True,
            "principals": [],
            "groups": [],
            "any_caller": False,
        }
    assert isinstance(declaration, StoreDeclaration)
    principals: list[list[str]] = []
    groups: list[str] = []
    any_caller = False
    for role in declaration.roles:
        if "read" not in role.rights:
            continue
        for member in role.members:
            if member.kind == "any":
                any_caller = True
            elif member.kind == "group" and member.group is not None:
                if member.group not in groups:
                    groups.append(member.group)
            elif member.kind == "principal" and member.principal is not None:
                key = list(member.principal.key)
                if key not in principals and can(
                    member.principal.key, name, "read", config=config, knowledge_root=knowledge_root
                ):
                    principals.append(key)
    return {
        "declaration": "valid",
        "owner_only": False,
        "principals": principals,
        "groups": groups,
        "any_caller": any_caller,
    }


def build_status(config: dict[str, Any] | None, knowledge_root: Path) -> dict[str, Any]:
    """The whole report as a plain dict (the ``--json`` payload). Read-only."""
    knowledge_root = Path(knowledge_root)
    adapters = available_adapters(config)
    warnings: list[str] = []

    locations: list[dict[str, Any]] = []
    vaults: list[dict[str, Any]] = []
    for name, adapter in sorted(adapters.items()):
        root = adapter.resolve_root(knowledge_root)
        in_git = inside_git_repository(root)
        declaration = load_store_declaration(root)
        locations.append(
            {
                "name": name,
                "root": str(root),
                "sensitive": adapter.sensitive,
                "in_corpus": adapter.corpus_policy.in_corpus,
                "read_only": adapter.read_only,
                "purgeable": adapter.purgeable,
                "inside_git_working_tree": in_git,
                "has_store_declaration": declaration is not None,
                "history": adapter.history,
            }
        )
        if adapter.sensitive:
            vaults.append(
                {
                    "name": name,
                    "readers": _readers(name, root, config, knowledge_root),
                }
            )
            if in_git:
                warnings.append(VAULT_IN_GIT_WARNING.format(name=name))

    routes: list[dict[str, Any]] = []
    for route in configured_routes(config):
        routes.append(
            {
                "rule": route.rule_name,
                "page_type": route.page_type,
                "sensitivity_class": route.sensitivity_class,
                "fields": list(route.fields) if route.fields is not None else None,
                "targets": list(route.to),
                "discarded": route.discard,
            }
        )

    contact_fields: list[dict[str, Any]] = []
    for field in pii.BUILTIN_CONTACT_FIELDS:
        decision = route_for("person", field, config=config)
        if decision.discarded:
            where = DISCARDED
        elif decision.targets:
            where = ", ".join(decision.targets)
        else:
            where = KEPT_ON_PAGE
        contact_fields.append(
            {
                "page_type": "person",
                "field": field,
                "rule": decision.rule,
                "targets": list(decision.targets),
                "disposition": where,
            }
        )

    history = resolve_storage_history(config)
    unscreened = unscreened_raw_files(knowledge_root, config)
    in_tree = raw_in_git_tree(knowledge_root)
    messages = hold_messages(config, knowledge_root)
    notices = [m.text for m in messages if m.kind == "notice"]
    warnings.extend(m.text for m in messages if m.kind == "warning" and m.text != REACH_TEXT)
    held = len(unscreened) if hold_active(config) else 0
    raw_intake = {
        "inside_git_working_tree": in_tree,
        "knowledge_dir_is_git_repository": knowledge_dir_is_git_repo(knowledge_root),
        "hold_on": hold_active(config),
        "unscreened": len(unscreened),
        "held": held,
    }
    warnings.append(
        RAW_INTAKE_WARNING.format(
            where="inside" if in_tree else "not inside",
            unscreened=len(unscreened),
            held=held,
        )
    )
    warnings.append(REACH_TEXT)

    return {
        "history": {"declared": history, **HISTORY_MEANING[history]},
        "locations": locations,
        "routes": routes,
        "contact_fields": contact_fields,
        "vaults": vaults,
        "raw_intake": raw_intake,
        "machine_access": MACHINE_ACCESS_LINE,
        "notices": notices,
        "warnings": warnings,
    }


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def format_status(status: dict[str, Any]) -> list[str]:
    """The report as lines for stdout (warnings are printed separately, to stderr)."""
    lines: list[str] = []
    history = status["history"]
    lines.append(f"History: {history['declared']}")
    lines.append(f"  Restore: {history['restore']}")
    lines.append(f"  Retirement: {history['retirement']}")
    lines.append("Locations:")
    for loc in status["locations"]:
        lines.append(
            f"  {loc['name']}: root {loc['root']}; sensitive {_yes(loc['sensitive'])}; "
            f"in the corpus {_yes(loc['in_corpus'])}; read-only {_yes(loc['read_only'])}; "
            f"purgeable {_yes(loc['purgeable'])}; inside a git working tree "
            f"{_yes(loc['inside_git_working_tree'])}; store declaration "
            f"{_yes(loc['has_store_declaration'])}"
        )
    lines.append("Routes:")
    if not status["routes"]:
        lines.append("  (none configured)")
    for route in status["routes"]:
        subject = route["page_type"] or f"class {route['sensitivity_class']}"
        fields = ", ".join(route["fields"]) if route["fields"] is not None else "(whole page)"
        where = DISCARDED if route["discarded"] else ", ".join(route["targets"])
        lines.append(f"  {route['rule']}: {subject}; fields {fields}; {where}")
    lines.append("Built-in contact fields:")
    for row in status["contact_fields"]:
        lines.append(f"  {row['page_type']}.{row['field']}: {row['disposition']}")
    lines.append("Vaults (who may read):")
    if not status["vaults"]:
        lines.append("  (none)")
    for vault in status["vaults"]:
        readers = vault["readers"]
        if readers["owner_only"]:
            who = OWNER_ONLY
        else:
            parts = [" | ".join(p) for p in readers["principals"]]
            parts += [f"group {g}" for g in readers["groups"]]
            if readers["any_caller"]:
                parts.append("any caller the host admits")
            who = "; ".join(parts) or "no principal"
        lines.append(f"  {vault['name']}: {who}")
    raw = status["raw_intake"]
    lines.append("Raw intake:")
    lines.append(f"  inside a git working tree: {_yes(raw['inside_git_working_tree'])}")
    lines.append(f"  unscreened raw files: {raw['unscreened']}; held out of commits: {raw['held']}")
    for notice in status["notices"]:
        lines.append(f"  {notice}")
    lines.append(status["machine_access"])
    return lines
