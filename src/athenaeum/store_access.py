# SPDX-License-Identifier: Apache-2.0
"""Parse and validate a store's declaration, ``_store/access.yaml``.

Layering: L2 (imports :mod:`athenaeum.models`, L1, for ``asserter_identity_key``).

Design: ``docs/design/federation-store-access.md`` §5 (the declaration), §11
(failure classes), §12 slice S1; ``docs/design/federation-multi-writer-sync.md``
§3.3 and §5 for the ``sync`` block. Format reference:
``docs/reference/store-declaration.md``.

This slice only reads. Nothing calls it yet, so no install behaves differently.

**Fail closed.** :func:`load_store_declaration` returns one of three things: the
frozen :class:`StoreDeclaration`, ``None`` when the surface has no declaration
file, or a :class:`StoreDeclarationInvalid` naming every problem. It never raises
on bad content and never fills in a default for a key that is present and
wrong. The two result types are unrelated classes: an invalid result carries no
roles, no rights and no partial declaration, so a caller cannot read a right
out of a file that failed to validate.

**Roles are the store's own; the rights are the engine's.** The parser keys on no
role name. The only rights surface is :func:`declared_rights`, which maps a
principal's identity key to the union of the rights of every role that lists
them directly.

**Versioning.** Versions 1 and 2 are understood; version 2 introduces the
``intake`` block (a version-1 file carrying one is invalid). A new top-level key
arrives only with a higher version, so a file carrying an unknown key
(``sync_mod``, the old ``grants``, a bare ``sync_mode``) is invalid, as is any
other version. An install treats an invalid store as read-only.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import asserter_identity_key

__all__ = [
    "DECLARATION_RELPATH",
    "STORE_ID_ALPHABET",
    "StoreDeclaration",
    "StoreDeclarationInvalid",
    "Problem",
    "Principal",
    "Member",
    "Role",
    "Group",
    "SyncDeclaration",
    "RawLocation",
    "Locations",
    "Review",
    "IntakeRule",
    "IntakePolicy",
    "declared_rights",
    "is_store_id",
    "load_store_declaration",
    "mint_store_id",
    "parse_store_declaration",
]

#: Path of the declaration, relative to a surface root.
DECLARATION_RELPATH = "_store/access.yaml"

SUPPORTED_VERSION = 1
#: The declaration version that introduces the ``intake`` block. A version-1
#: file may not carry one; an install that reads only version 1 treats a
#: version-2 store as read-only (design note federation-intake-routing §7).
INTAKE_VERSION = 2
SUPPORTED_VERSIONS = (SUPPORTED_VERSION, INTAKE_VERSION)
INTAKE_VERDICTS = ("accept", "refuse")
RIGHTS = ("read", "propose", "compile", "answer")
SYNC_MODES = ("fast_forward", "integrate")
DEFAULT_DIRECTORY = "static"

#: Lowercase RFC 4648 base32: ``a-z`` and ``2-7``. 32 symbols, so 12 characters
#: carry 60 random bits.
STORE_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_STORE_ID_LEN = 12
_STORE_ID_RE = re.compile(rf"^st_[{STORE_ID_ALPHABET}]{{{_STORE_ID_LEN}}}$")

_TOP_KEYS = frozenset(
    {
        "version", "store_id", "directory", "groups", "roles", "sync", "locations",
        "reviews", "intake",
    }
)
_PRINCIPAL_KEYS = frozenset({"iss", "sub", "provider_ids"})
_REVIEW_KEYS = ("name", "source", "every", "criteria")
_PURPOSES = ("review",)
_GROUP_PREFIX = "group:"


def mint_store_id() -> str:
    """A fresh store id: ``st_`` plus 12 random lowercase base32 characters."""
    return "st_" + "".join(secrets.choice(STORE_ID_ALPHABET) for _ in range(_STORE_ID_LEN))


def is_store_id(value: object) -> bool:
    """True when *value* has the shape of a store id."""
    return isinstance(value, str) and _STORE_ID_RE.match(value) is not None


# -- typed result -----------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """One validation failure: the offending *key* (dotted path) and a message."""

    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.key}: {self.message}"


@dataclass(frozen=True)
class StoreDeclarationInvalid:
    """The declaration is present but cannot be trusted. Carries problems only."""

    problems: tuple[Problem, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(p.key for p in self.problems)

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(str(p) for p in self.problems)


@dataclass(frozen=True)
class Principal:
    """One principal block. ``key`` is the ``asserter_identity_key`` of it."""

    iss: str
    sub: str
    key: tuple[str, ...]
    provider_ids: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Member:
    """One entry in a role's ``members``.

    ``kind`` is ``"principal"`` (``principal`` set), ``"group"`` (``group``
    set) or ``"any"`` (the ``"*"`` wildcard). ``purpose`` is ``None`` or
    ``"review"``.
    """

    kind: str
    principal: Principal | None = None
    group: str | None = None
    purpose: str | None = None


@dataclass(frozen=True)
class Role:
    """A role the deployment named. Its name is data; no code keys on it."""

    name: str
    rights: frozenset[str]
    members: tuple[Member, ...]


@dataclass(frozen=True)
class Group:
    name: str
    members: tuple[Principal, ...]


@dataclass(frozen=True)
class SyncDeclaration:
    mode: str = "fast_forward"
    always_ask: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawLocation:
    """One ``{for, at}`` entry of a per-proposer ``locations.raw`` list."""

    proposer: Principal
    at: str


@dataclass(frozen=True)
class Locations:
    """``bench`` is a string or ``None``. ``raw`` is ``None``, one shared
    string, or a tuple of per-proposer :class:`RawLocation` entries."""

    bench: str | None = None
    raw: str | tuple[RawLocation, ...] | None = None


@dataclass(frozen=True)
class Review:
    """A review's shape only; nothing here runs one."""

    name: str
    source: str
    every: str
    criteria: str


@dataclass(frozen=True)
class IntakeRule:
    """One ``intake.rules`` entry: a verdict and a ``type``, a ``tag`` or both.

    There is no ``subject``: a subject is a page uid, and a proposer may not be
    able to read the store's pages.
    """

    verdict: str
    type: str | None = None
    tag: str | None = None


@dataclass(frozen=True)
class IntakePolicy:
    """The ``intake`` block. ``rules`` are ordered; the last match wins."""

    default: str
    rules: tuple[IntakeRule, ...] = ()


@dataclass(frozen=True)
class StoreDeclaration:
    """A declaration that validated in full."""

    version: int
    store_id: str
    directory: str
    groups: tuple[Group, ...]
    roles: tuple[Role, ...]
    sync: SyncDeclaration
    locations: Locations
    reviews: tuple[Review, ...]
    #: ``None`` when the declaration has no ``intake`` block.
    intake: IntakePolicy | None = None


# -- public readers ---------------------------------------------------------


def declared_rights(declaration: StoreDeclaration) -> dict[tuple[str, ...], frozenset[str]]:
    """Per directly-listed principal (by identity key), the union of rights.

    Groups and ``"*"`` are not expanded here: resolving them needs the
    directory adapter and belongs to the rights resolution slice.
    """
    out: dict[tuple[str, ...], frozenset[str]] = {}
    for role in declaration.roles:
        for member in role.members:
            if member.kind == "principal" and member.principal is not None:
                key = member.principal.key
                out[key] = out.get(key, frozenset()) | role.rights
    return out


def load_store_declaration(
    surface_root: Path,
) -> StoreDeclaration | StoreDeclarationInvalid | None:
    """Read ``<surface_root>/_store/access.yaml``.

    ``None`` when the file is absent. A file that cannot be read or decoded is
    an invalid result, never an exception.
    """
    path = Path(surface_root) / DECLARATION_RELPATH
    if not path.exists() and not path.is_symlink():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return StoreDeclarationInvalid(
            (Problem(DECLARATION_RELPATH, f"cannot be read: {type(exc).__name__}"),)
        )
    return parse_store_declaration(text)


def parse_store_declaration(text: str) -> StoreDeclaration | StoreDeclarationInvalid:
    """Validate declaration *text*. Never raises on bad content."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return StoreDeclarationInvalid(
            (Problem(DECLARATION_RELPATH, f"malformed YAML: {type(exc).__name__}"),)
        )
    if not isinstance(data, dict):
        return StoreDeclarationInvalid(
            (Problem(DECLARATION_RELPATH, "must be a mapping of keys to values"),)
        )
    problems: list[Problem] = []
    result = _build(data, problems)
    if problems or result is None:
        return StoreDeclarationInvalid(tuple(problems))
    return result


# -- validation -------------------------------------------------------------


def version_ok_for_intake(version: object) -> bool:
    """True when *version* is a supported version that carries ``intake``."""
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version in SUPPORTED_VERSIONS
        and version >= INTAKE_VERSION
    )


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _build(data: dict[Any, Any], problems: list[Problem]) -> StoreDeclaration | None:
    for key in data:
        if not isinstance(key, str):
            problems.append(Problem(str(key), "top-level keys must be strings"))
        elif key == "intake" and version_ok_for_intake(data.get("version")):
            continue
        elif key == "intake":
            problems.append(
                Problem(
                    "intake",
                    "the intake block arrives only with a higher version "
                    f"(declaration version {INTAKE_VERSION}); an earlier version may not carry it",
                )
            )
        elif key not in _TOP_KEYS:
            problems.append(
                Problem(
                    key,
                    f"unknown top-level key {key!r} (version {SUPPORTED_VERSION} "
                    f"knows {', '.join(sorted(_TOP_KEYS))}; new keys arrive only "
                    "with a higher version)",
                )
            )

    version = data.get("version")
    if "version" not in data:
        problems.append(Problem("version", "is required"))
    elif isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        problems.append(
            Problem(
                "version",
                f"{version!r} is not supported (this install reads versions "
                f"{', '.join(map(str, SUPPORTED_VERSIONS))}); the store is read-only here",
            )
        )

    store_id = data.get("store_id")
    if "store_id" not in data:
        problems.append(Problem("store_id", "is required"))
    elif not is_store_id(store_id):
        problems.append(
            Problem(
                "store_id",
                f"{store_id!r} is not 'st_' followed by {_STORE_ID_LEN} lowercase "
                "base32 characters",
            )
        )

    directory = data.get("directory", DEFAULT_DIRECTORY)
    if not _nonempty_str(directory):
        problems.append(Problem("directory", "must be a non-empty adapter name"))

    groups = _groups(data.get("groups", {}), problems)
    roles = _roles(data.get("roles", {}), problems)
    sync = _sync(data.get("sync", {}), problems)
    locations = _locations(data.get("locations", {}), problems)
    reviews = _reviews(data.get("reviews", []), problems)
    intake = _intake(data["intake"], problems) if "intake" in data else None

    if problems:
        return None
    assert isinstance(store_id, str) and isinstance(directory, str)
    assert isinstance(version, int)
    return StoreDeclaration(
        version=version,
        store_id=store_id,
        directory=directory,
        groups=groups,
        roles=roles,
        sync=sync,
        locations=locations,
        reviews=reviews,
        intake=intake,
    )


def _principal(value: object, key: str, problems: list[Problem]) -> Principal | None:
    if not isinstance(value, dict):
        problems.append(Problem(key, "must be a principal block {iss, sub}"))
        return None
    start = len(problems)
    for k in value:
        if k not in _PRINCIPAL_KEYS:
            problems.append(Problem(f"{key}.{k}", f"unknown principal key {k!r}"))
    for req in ("iss", "sub"):
        if not _nonempty_str(value.get(req)):
            problems.append(Problem(f"{key}.{req}", "is required and must be a non-empty string"))
    pids = value.get("provider_ids")
    pid_items: tuple[tuple[str, str], ...] = ()
    if pids is not None:
        if isinstance(pids, dict) and all(
            _nonempty_str(k) and _nonempty_str(v) for k, v in pids.items()
        ):
            pid_items = tuple(sorted((str(k), str(v)) for k, v in pids.items()))
        else:
            problems.append(
                Problem(f"{key}.provider_ids", "must map non-empty strings to non-empty strings")
            )
    if len(problems) > start:
        return None
    identity = asserter_identity_key(dict(value))
    if not identity:  # pragma: no cover - iss and sub are non-empty strings here
        problems.append(Problem(key, "has no derivable identity key"))
        return None
    return Principal(iss=value["iss"], sub=value["sub"], key=identity, provider_ids=pid_items)


def _groups(value: object, problems: list[Problem]) -> tuple[Group, ...]:
    if not isinstance(value, dict):
        problems.append(Problem("groups", "must map a group name to a list of principals"))
        return ()
    out: list[Group] = []
    for name, members in value.items():
        key = f"groups.{name}"
        if not _nonempty_str(name):
            problems.append(Problem("groups", "group names must be non-empty strings"))
            continue
        if not isinstance(members, list):
            problems.append(Problem(key, "must be a list of principal blocks"))
            continue
        built = [_principal(m, f"{key}[{i}]", problems) for i, m in enumerate(members)]
        out.append(Group(name, tuple(p for p in built if p is not None)))
    return tuple(out)


def _member(value: object, key: str, holds_read: bool, only_read: bool,
            problems: list[Problem]) -> Member | None:
    purpose: str | None = None
    target: object = value
    if isinstance(value, dict) and "purpose" in value:
        extra = set(value) - {"principal", "purpose"}
        if extra or "principal" not in value:
            problems.append(Problem(key, "a member with a purpose is {principal, purpose}"))
            return None
        if not holds_read:
            problems.append(Problem(f"{key}.purpose", "is allowed only in a role that holds read"))
            return None
        if value["purpose"] not in _PURPOSES:
            problems.append(
                Problem(f"{key}.purpose", f"must be one of {', '.join(_PURPOSES)}")
            )
            return None
        purpose = str(value["purpose"])
        target = value["principal"]
        key = f"{key}.principal"
    if target == "*":
        if not only_read:
            problems.append(
                Problem(key, '"*" is valid only in a role whose rights are exactly [read]')
            )
            return None
        return Member("any", purpose=purpose)
    if isinstance(target, str):
        name = target[len(_GROUP_PREFIX):] if target.startswith(_GROUP_PREFIX) else ""
        if not name.strip():
            problems.append(
                Problem(key, "a string member must be '*' or 'group:<name>'")
            )
            return None
        return Member("group", group=name, purpose=purpose)
    principal = _principal(target, key, problems)
    if principal is None:
        return None
    return Member("principal", principal=principal, purpose=purpose)


def _roles(value: object, problems: list[Problem]) -> tuple[Role, ...]:
    if not isinstance(value, dict):
        problems.append(Problem("roles", "must map a role name to {rights, members}"))
        return ()
    out: list[Role] = []
    for name, body in value.items():
        key = f"roles.{name}"
        if not _nonempty_str(name):
            problems.append(Problem("roles", "role names must be non-empty strings"))
            continue
        if not isinstance(body, dict):
            problems.append(Problem(key, "must be a mapping with rights and members"))
            continue
        start = len(problems)
        for k in body:
            if k not in ("rights", "members"):
                problems.append(Problem(f"{key}.{k}", f"unknown role key {k!r}"))
        rights = body.get("rights")
        right_set: frozenset[str] = frozenset()
        if not isinstance(rights, list) or not rights:
            problems.append(
                Problem(f"{key}.rights", f"must be a non-empty list drawn from {', '.join(RIGHTS)}")
            )
        else:
            bad = [r for r in rights if not isinstance(r, str) or r not in RIGHTS]
            if bad:
                problems.append(
                    Problem(
                        f"{key}.rights",
                        f"{bad[0]!r} is not a right (allowed: {', '.join(RIGHTS)})",
                    )
                )
            else:
                right_set = frozenset(rights)
                if right_set & {"compile", "answer"} and "read" not in right_set:
                    problems.append(
                        Problem(f"{key}.rights", "compile or answer requires read in the same role")
                    )
        members = body.get("members")
        built: list[Member] = []
        if not isinstance(members, list):
            problems.append(Problem(f"{key}.members", "must be a list"))
        elif len(problems) == start:
            for i, m in enumerate(members):
                got = _member(
                    m, f"{key}.members[{i}]", "read" in right_set, right_set == {"read"}, problems
                )
                if got is not None:
                    built.append(got)
        if len(problems) == start:
            out.append(Role(name, right_set, tuple(built)))
    return tuple(out)


def _sync(value: object, problems: list[Problem]) -> SyncDeclaration:
    if not isinstance(value, dict):
        problems.append(Problem("sync", "must be a mapping {mode, always_ask}"))
        return SyncDeclaration()
    for k in value:
        if k not in ("mode", "always_ask"):
            problems.append(Problem(f"sync.{k}", f"unknown sync key {k!r}"))
    mode = value.get("mode", "fast_forward")
    if not isinstance(mode, str) or mode not in SYNC_MODES:
        problems.append(
            Problem("sync.mode", f"{mode!r} must be one of {', '.join(SYNC_MODES)}")
        )
        mode = "fast_forward"
    ask = value.get("always_ask", [])
    if not isinstance(ask, list) or not all(_nonempty_str(a) for a in ask):
        problems.append(Problem("sync.always_ask", "must be a list of page type names"))
        ask = []
    return SyncDeclaration(mode=mode, always_ask=tuple(ask))


def _locations(value: object, problems: list[Problem]) -> Locations:
    if not isinstance(value, dict):
        problems.append(Problem("locations", "must be a mapping {bench, raw}"))
        return Locations()
    for k in value:
        if k not in ("bench", "raw"):
            hint = " (the inbox location was removed)" if k == "inbox" else ""
            problems.append(Problem(f"locations.{k}", f"unknown locations key {k!r}{hint}"))
    bench = value.get("bench")
    if "bench" in value and not _nonempty_str(bench):
        problems.append(Problem("locations.bench", "must be a non-empty string"))
        bench = None
    raw = value.get("raw")
    parsed_raw: str | tuple[RawLocation, ...] | None = None
    if "raw" in value:
        if isinstance(raw, str):
            if _nonempty_str(raw):
                parsed_raw = raw
            else:
                problems.append(Problem("locations.raw", "must be a non-empty string"))
        elif isinstance(raw, list):
            parsed_raw = _raw_list(raw, problems)
        else:
            problems.append(
                Problem("locations.raw", "must be one string or a list of {for, at} entries")
            )
    return Locations(bench=bench, raw=parsed_raw)


def _raw_list(raw: list[Any], problems: list[Problem]) -> tuple[RawLocation, ...] | None:
    if not raw:
        problems.append(Problem("locations.raw", "an empty list is invalid"))
        return None
    start = len(problems)
    out: list[RawLocation] = []
    seen: set[tuple[str, ...]] = set()
    for i, entry in enumerate(raw):
        key = f"locations.raw[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"for", "at"}:
            problems.append(Problem(key, "must be exactly {for, at}"))
            continue
        for_ = entry["for"]
        if not isinstance(for_, dict):
            problems.append(
                Problem(
                    f"{key}.for",
                    'must be one principal block {iss, sub}, never a group or "*"',
                )
            )
            continue
        principal = _principal(for_, f"{key}.for", problems)
        if not _nonempty_str(entry["at"]):
            problems.append(Problem(f"{key}.at", "must be a non-empty string"))
            continue
        if principal is None:
            continue
        if principal.key in seen:
            problems.append(Problem(f"{key}.for", "names a principal already listed"))
            continue
        seen.add(principal.key)
        out.append(RawLocation(principal, entry["at"]))
    return tuple(out) if len(problems) == start else None


def _reviews(value: object, problems: list[Problem]) -> tuple[Review, ...]:
    if not isinstance(value, list):
        problems.append(Problem("reviews", "must be a list"))
        return ()
    out: list[Review] = []
    for i, entry in enumerate(value):
        key = f"reviews[{i}]"
        if not isinstance(entry, dict) or set(entry) != set(_REVIEW_KEYS):
            problems.append(Problem(key, f"must be exactly {{{', '.join(_REVIEW_KEYS)}}}"))
            continue
        bad = [k for k in _REVIEW_KEYS if not _nonempty_str(entry[k])]
        for k in bad:
            problems.append(Problem(f"{key}.{k}", "must be a non-empty string"))
        if not bad:
            out.append(Review(**{k: entry[k] for k in _REVIEW_KEYS}))
    return tuple(out)


def _intake(value: object, problems: list[Problem]) -> IntakePolicy | None:
    if not isinstance(value, dict):
        problems.append(Problem("intake", "must be a mapping {default, rules}"))
        return None
    start = len(problems)
    for k in value:
        if k not in ("default", "rules"):
            problems.append(Problem(f"intake.{k}", f"unknown intake key {k!r}"))
    default = value.get("default")
    if not isinstance(default, str) or default not in INTAKE_VERDICTS:
        problems.append(
            Problem("intake.default", f"must be one of {', '.join(INTAKE_VERDICTS)}")
        )
    raw_rules = value.get("rules", [])
    rules: list[IntakeRule] = []
    if not isinstance(raw_rules, list):
        problems.append(Problem("intake.rules", "must be an ordered list of rules"))
    else:
        for i, entry in enumerate(raw_rules):
            rule = _intake_rule(entry, f"intake.rules[{i}]", problems)
            if rule is not None:
                rules.append(rule)
    if len(problems) > start or not isinstance(default, str):
        return None
    return IntakePolicy(default=default, rules=tuple(rules))


def _intake_rule(entry: object, key: str, problems: list[Problem]) -> IntakeRule | None:
    if not isinstance(entry, dict):
        problems.append(Problem(key, "must be a mapping {verdict, type, tag}"))
        return None
    start = len(problems)
    for k in entry:
        if k == "subject":
            problems.append(
                Problem(f"{key}.subject", "is not allowed: a subject is a page uid and a "
                        "proposer may not be able to read the store's pages")
            )
        elif k not in ("verdict", "type", "tag"):
            problems.append(Problem(f"{key}.{k}", f"unknown rule key {k!r}"))
    verdict = entry.get("verdict")
    if not isinstance(verdict, str) or verdict not in INTAKE_VERDICTS:
        problems.append(
            Problem(
                f"{key}.verdict",
                f"{verdict!r} is not a verdict; a store's intake rules only "
                f"{' or '.join(INTAKE_VERDICTS)}",
            )
        )
    keys: dict[str, str] = {}
    for name in ("type", "tag"):
        if name in entry:
            if _nonempty_str(entry[name]):
                keys[name] = entry[name].strip()
            else:
                problems.append(Problem(f"{key}.{name}", "must be a non-empty string"))
    if "type" not in keys and "tag" not in keys and len(problems) == start:
        problems.append(Problem(key, "must name a type, a tag or both"))
    if len(problems) > start:
        return None
    return IntakeRule(verdict=str(verdict), type=keys.get("type"), tag=keys.get("tag"))
