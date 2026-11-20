# SPDX-License-Identifier: Apache-2.0
"""The read-only check behind ``athenaeum store access check``.

Layering: L3 (imports :mod:`athenaeum.store_rights`, L2, :mod:`athenaeum.store_access`,
L2, :mod:`athenaeum.store_access_evidence`, L2, and :mod:`athenaeum.storage`, L1).

Design: ``docs/design/federation-store-access.md`` §9 ("The check"), §7 (the
one-writer rule, a software librarian) and §13 ruling 5, slice S5.

For each mounted store that has a declaration the check reports:

* the rights this install holds by the declaration, and the local ``read_only``
  setting;
* per location, whether the last fetch completed and the recorded outcome of the
  last real push, from :mod:`athenaeum.store_access_evidence`. Nothing is probed:
  no push and no dry-run push is made, so an install that has never pushed reports
  ``push: not yet observed``;
* every *difference* between that evidence and the declared rights;
* every *refusal*, a declaration the rules of section 7 do not accept.

It reads files and changes nothing. athenaeum never provisions access, so a
finding is something for the operator to settle on the host or in the
declaration.

A group the directory cannot resolve (a declared directory other than ``static``)
is counted as holding its right but never expanded, so the check neither invents a
second writer nor proves a software-only librarian from it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.storage import available_adapters
from athenaeum.store import remote_url
from athenaeum.store_access import (
    RIGHTS,
    StoreDeclaration,
    StoreDeclarationInvalid,
    load_store_declaration,
)
from athenaeum.store_rights import resolve_rights

__all__ = ["LocationRow", "StoreRow", "AccessReport", "check_access"]

NOT_OBSERVED = "not yet observed"

#: A push failure that reads as the host refusing this install: clear permission
#: evidence only. Transport failures ("Could not read from remote repository",
#: "Permission denied (publickey)" with no host verdict, DNS, timeouts) and bare
#: numbers do not match; those are reported but not flagged as a difference.
_REFUSED_RE = re.compile(
    r"remote: permission denied|permission to \S+ denied|access denied|forbidden|"
    r"returned error: 40[13]\b|http(?:/\d(?:\.\d)?)? 40[13]\b|"
    r"authentication failed|protected branch hook declined|protected branch",
    re.IGNORECASE,
)


@dataclass
class LocationRow:
    """One location of a store and what this install last saw of it."""

    role: str  # pages | bench | raw
    where: str
    fetch: str = NOT_OBSERVED  # ok | failed | not yet observed
    push: str = NOT_OBSERVED  # accepted | failed | not yet observed
    push_detail: str = ""


@dataclass
class StoreRow:
    store: str
    store_id: str | None
    root: str
    rights: list[str] = field(default_factory=list)
    read_only: bool = False
    locations: list[LocationRow] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


@dataclass
class AccessReport:
    stores: list[StoreRow] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(s.differences or s.refusals for s in self.stores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stores": [
                {
                    "store": s.store,
                    "store_id": s.store_id,
                    "rights": s.rights,
                    "read_only": s.read_only,
                    "locations": [loc.__dict__ for loc in s.locations],
                    "differences": s.differences,
                    "refusals": s.refusals,
                }
                for s in self.stores
            ],
        }


def _git_toplevel(root: Path) -> Path:
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            return candidate
    return root


def _holders(
    declaration: StoreDeclaration, right: str
) -> tuple[dict[tuple[str, ...], str | None], int]:
    """Principals holding *right*: ``{identity key: kind}``, plus a count of
    groups the directory cannot expand. ``"*"`` never carries more than read."""
    static = declaration.directory == "static"
    groups = {g.name: g.members for g in declaration.groups}
    found: dict[tuple[str, ...], str | None] = {}
    unresolved = 0
    for role in declaration.roles:
        if right not in role.rights:
            continue
        for m in role.members:
            if m.kind == "principal" and m.principal is not None:
                found[m.principal.key] = m.principal.kind
            elif m.kind == "group" and m.group is not None:
                if static and m.group in groups:
                    for p in groups[m.group]:
                        found[p.key] = p.kind
                else:
                    unresolved += 1
    return found, unresolved


def _declaration_refusals(declaration: StoreDeclaration) -> list[str]:
    out: list[str] = []
    compilers, compile_unknown = _holders(declaration, "compile")
    answerers, answer_unknown = _holders(declaration, "answer")
    if len(compilers) + compile_unknown > 1 and declaration.sync.mode != "integrate":
        out.append(
            "one-writer rule: compile is held by more than one principal and the store "
            "does not declare sync.mode: integrate"
        )
    if (set(answerers) - set(compilers) or answer_unknown) and declaration.locations.bench is None:
        out.append(
            "a principal holds answer without compile and the store declares no bench"
        )
    software_only = (
        bool(compilers)
        and not compile_unknown
        and all(kind == "software_agent" for kind in compilers.values())
    )
    person_answers = answer_unknown > 0 or any(k != "software_agent" for k in answerers.values())
    if software_only and not person_answers:
        out.append(
            "every principal holding compile is a software agent and no person holds answer"
        )
    return out


def _locations(declaration: StoreDeclaration, root: Path) -> list[LocationRow]:
    rows = [LocationRow("pages", str(_git_toplevel(root)))]
    if declaration.locations.bench:
        rows.append(LocationRow("bench", declaration.locations.bench))
    raw = declaration.locations.raw
    if isinstance(raw, str):
        rows.append(LocationRow("raw", raw))
    elif raw:
        rows.extend(LocationRow("raw", r.at) for r in raw)
    return rows


def _apply_evidence(row: LocationRow, evidence: Mapping[str, Mapping[str, Any]]) -> None:
    entry = evidence.get(row.where)
    if entry is None:
        entry = next(
            (
                e
                for path, e in evidence.items()
                if Path(path).is_dir()
                and remote_url(Path(path), str(e.get("remote", ""))) == row.where
            ),
            None,
        )
    if entry is None:
        return
    fetch = entry.get("fetch")
    if isinstance(fetch, dict):
        row.fetch = "ok" if fetch.get("ok") else "failed"
    push = entry.get("push")
    if isinstance(push, dict):
        row.push = "accepted" if push.get("ok") else "failed"
        row.push_detail = "" if push.get("ok") else str(push.get("reason") or "")


def _differences(rights: list[str], read_only: bool, pages: LocationRow | None) -> list[str]:
    out: list[str] = []
    compiles = "compile" in rights
    if not read_only and not compiles:
        out.append(
            "local read_only is false but the declaration does not give this install compile"
        )
    if pages is None:
        return out
    if pages.push == "accepted" and not compiles:
        out.append(
            "a push was accepted by the host but the declaration does not give this "
            "install compile"
        )
    if pages.push == "failed" and compiles and _REFUSED_RE.search(pages.push_detail):
        out.append("the declaration gives this install compile but the host refused the last push")
    if pages.fetch == "ok" and "read" not in rights:
        out.append("a fetch completed but the declaration does not give this install read")
    return out


def check_access(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    asserter: Mapping[str, object] | None,
    evidence: Mapping[str, Mapping[str, Any]],
) -> AccessReport:
    """Build the report for every mounted store that has a declaration."""
    by_root: dict[Path, list[tuple[str, bool]]] = {}
    for name, adapter in sorted(available_adapters(config).items()):
        root = adapter.resolve_root(knowledge_root).resolve()
        by_root.setdefault(root, []).append((name, adapter.read_only))
    report = AccessReport()
    for root, members in by_root.items():
        if not root.is_dir():
            continue
        declaration = load_store_declaration(root)
        if declaration is None:
            continue
        name = "/".join(n for n, _ in members)
        read_only = all(ro for _, ro in members)
        if isinstance(declaration, StoreDeclarationInvalid):
            report.stores.append(
                StoreRow(
                    name, None, str(root), ["read"], read_only,
                    refusals=[f"declaration is invalid: {m}" for m in declaration.messages],
                )
            )
            continue
        held = resolve_rights(declaration, asserter)
        rights = [r for r in RIGHTS if r in held]
        row = StoreRow(name, declaration.store_id, str(root), rights, read_only)
        row.locations = _locations(declaration, root)
        for loc in row.locations:
            _apply_evidence(loc, evidence)
        row.differences = _differences(rights, read_only, row.locations[0])
        row.refusals = _declaration_refusals(declaration)
        report.stores.append(row)
    return report
