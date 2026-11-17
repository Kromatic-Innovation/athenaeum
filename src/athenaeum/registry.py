# SPDX-License-Identifier: Apache-2.0
"""Source-handle registry builder (issue athenaeum#453, epic athenaeum#422).

The fact-mining pipeline (``athenaeum-adapters``) needs a canonical mapping
from a wiki entity to the corpus handles it resolves against — the domains,
email aliases, Slack channels/user-ids, LinkedIn URL, partner domains, Drive
folder ids and Mural board ids that identify the same real-world entity across
external sources. Those handles live ON the entity page as frontmatter (the
wiki owns knowledge about entities; adapters consume it), round-tripped
byte-for-byte by tier0 passthrough.

This module compiles that scattered frontmatter into a single ``registry.json``
index — ``entity uid → handle set`` — that adapters can load without walking
the wiki. It is a deterministic, LLM-free read of the wiki tree, exactly like
the ``people`` and ``compile-as-of`` commands.

**Tooling only — no data.** This builder ships in the public OSS repo and is
tested with synthetic fixtures. Populating real client handles is a separate,
operator-only, private-store operation (issue athenaeum#454); the builder must emit a
well-formed registry even when zero handles are populated, so athenaeum#454 is never a
precondition for it working.

Layering: L1 (data model — reads the wiki tree via :mod:`athenaeum.models`,
the L1 hub, for :func:`~athenaeum.models.parse_frontmatter`). No config, no
LLM client. Factoring rule: this module owns ONLY the deterministic
frontmatter → registry compile (extract known handle keys, sort, serialize);
it must never invent new handle keys' semantics or validate the handle
VALUES beyond non-empty-string cleanup — a source handle's meaning belongs to
the adapter that consumes it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from athenaeum.models import parse_frontmatter

#: List-valued source-handle keys — each holds zero or more string handles.
LIST_HANDLE_KEYS: tuple[str, ...] = (
    "domains",
    "alt_emails",
    "slack_channels",
    "slack_user_ids",
    "partner_domains",
    "drive_folder_ids",
    "mural_board_ids",
)

#: Scalar source-handle keys — a single string value, empty when unset.
SCALAR_HANDLE_KEYS: tuple[str, ...] = (
    "linkedin_url",
    "handles_verified",
    "apollo_organization_id",
    # Issue athenaeum#902: Google People API resource name (`people/c123...`) for a
    # contact-sync record. Registered here — the same seeding path
    # `apollo_organization_id` (athenaeum#874) uses — so `collect_handles` picks it
    # up from frontmatter and contact-sync records resolve their target person
    # deterministically through registry.json instead of by reasoning. 89% of
    # resource names recur day over day, so the registry warms almost at once.
    #
    # Unlike `email` (see `corrections.EMAIL_HANDLE_KEY`), a resource name is an
    # opaque provider id, not a contact identifier: it is not PII, so it belongs
    # in the registry rather than on the PII surface.
    "google_resource_name",
)

#: All source-handle keys, in canonical (template) order. This is the
#: contract documented in ``docs/extending/source-handles.md``; keep the three in
#: sync.
SOURCE_HANDLE_KEYS: tuple[str, ...] = LIST_HANDLE_KEYS + SCALAR_HANDLE_KEYS

#: registry.json schema version. Bump when the on-disk shape changes so
#: adapters can detect an incompatible index.
REGISTRY_VERSION = 1


def _clean_list(value: Any) -> list[str]:
    """Coerce a frontmatter list value into a list of non-empty strings.

    Tolerant of the shapes YAML frontmatter actually produces: a real list,
    a lone scalar (authored without brackets), or ``None``/missing. Falsy and
    whitespace-only entries are dropped so ``[""]`` or ``[null]`` count as
    unpopulated rather than smuggling empty handles into the index.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _clean_scalar(value: Any) -> str:
    """Coerce a frontmatter scalar into a trimmed string ("" when unset)."""
    if value is None:
        return ""
    return str(value).strip()


def collect_handles(meta: dict[str, Any]) -> dict[str, Any]:
    """Extract the populated source-handle keys from one page's frontmatter.

    Returns a dict containing only the keys that carry a real value, in
    canonical :data:`SOURCE_HANDLE_KEYS` order — list keys as cleaned
    ``list[str]``, scalar keys as non-empty ``str``. An entity with no
    populated handles yields ``{}`` (its caller then omits it from the
    registry), which is what makes the zero-handles case well-formed.
    """
    handles: dict[str, Any] = {}
    for key in LIST_HANDLE_KEYS:
        cleaned_list = _clean_list(meta.get(key))
        if cleaned_list:
            handles[key] = cleaned_list
    for key in SCALAR_HANDLE_KEYS:
        cleaned_scalar = _clean_scalar(meta.get(key))
        if cleaned_scalar:
            handles[key] = cleaned_scalar
    return handles


class UnplaceableSourceHandleError(Exception):
    """A raw-intake file's populated source-handle key(s) could not be placed
    as frontmatter on any page written for it (issue athenaeum#1109).

    Silently letting a populated :data:`SOURCE_HANDLE_KEYS` value fall through
    into page-body prose is the exact defect this guards against: the page
    compiles, the intake reports success, and ``registry.json`` is left
    silently missing the handle. Raising here — instead of logging a warning
    and letting the write proceed — surfaces the loss the same way every
    other Tier-2/3 processing failure already is: the raw file is left on
    disk untouched (nothing from this call is written), the run's per-file
    failure/stuck-file ledger records it, and the file is retried next run.

    :func:`athenaeum.librarian.tier0_handle_upsert` (and ``tier0_passthrough``)
    are the deterministic paths that ordinarily place a source handle as
    frontmatter without ever reaching here. This is the backstop for
    whatever those decline onto, or that never reaches tier0 at all.
    """


def assert_handles_placed(
    incoming: dict[str, Any], written_metas: Iterable[dict[str, Any]]
) -> None:
    """Raise :class:`UnplaceableSourceHandleError` if a source handle
    populated in *incoming* is missing from every dict in *written_metas*.

    *incoming* is normally :func:`collect_handles` applied to a raw file's
    OWN frontmatter; *written_metas* is the frontmatter of every page that
    raw's processing is about to write. A no-op when *incoming* is empty —
    the raw carries no source handles at all, the overwhelmingly common
    case — so this costs nothing on the hot path.

    List-valued keys compare as sets (placement, not list order, is the
    guarantee); scalar keys compare by value.
    """
    if not incoming:
        return
    placed: dict[str, Any] = {}
    for meta in written_metas:
        placed.update(collect_handles(meta))
    missing = [
        key
        for key, value in incoming.items()
        if (
            set(value) != set(placed.get(key) or [])
            if key in LIST_HANDLE_KEYS
            else placed.get(key) != value
        )
    ]
    if missing:
        raise UnplaceableSourceHandleError(
            f"source handle key(s) {sorted(missing)} were populated on the raw "
            "intake file but did not land as frontmatter on any page written "
            "for it — refusing to let them fall through to the LLM tiers and "
            "be folded into page-body prose instead (issue athenaeum#1109)"
        )


def build_registry(wiki_root: Path) -> dict[str, Any]:
    """Compile the source-handle registry from a wiki tree.

    Walks ``wiki_root/*.md`` (skipping ``_``-prefixed non-entity pages),
    parses each page's frontmatter, and records every entity that carries at
    least one populated source handle as ``entities[uid]``. Type-agnostic: any
    page with a non-empty ``uid`` and a populated handle is indexed (the keys
    live on the person/company templates, but nothing stops another entity
    type from carrying them).

    The returned dict is deterministic — entities are sorted by uid and each
    handle set preserves canonical key order — so re-running on an unchanged
    wiki produces byte-identical output. When no entity has any populated
    handle (the degenerate seed-not-landed-yet case, issue athenaeum#453/#454), the
    result is still well-formed: ``entities`` is an empty object and
    ``entity_count`` is ``0``.
    """
    entities: dict[str, dict[str, Any]] = {}
    if wiki_root.is_dir():
        for path in sorted(wiki_root.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue
            uid = _clean_scalar(meta.get("uid"))
            if not uid:
                continue
            handles = collect_handles(meta)
            if not handles:
                continue
            entities[uid] = {
                "type": _clean_scalar(meta.get("type")),
                "name": _clean_scalar(meta.get("name")),
                "handles": handles,
            }

    ordered = {uid: entities[uid] for uid in sorted(entities)}
    return {
        "version": REGISTRY_VERSION,
        "entity_count": len(ordered),
        "entities": ordered,
    }


def render_registry(registry: dict[str, Any]) -> str:
    """Serialize a registry dict to canonical JSON text (trailing newline)."""
    return json.dumps(registry, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


#: File name of the body-address sidecar, written beside ``registry.json`` by
#: the same ``athenaeum registry`` build (athenaeum#2714).
BODY_EMAIL_INDEX_NAME = "registry-body-emails.json"

#: Same shape :mod:`athenaeum.identity_resolution` uses to detect an address.
_BODY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def body_email_key(address: str) -> str:
    """Sidecar key for *address*: sha256 of its casefolded, stripped form.

    Keyed by digest so the sidecar never stores a raw address — a query hashes
    its own address and does an O(1) lookup.
    """
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()


def _is_person_page(meta: dict[str, Any] | None) -> bool:
    """True when *meta* declares ``type: person``.

    Only person pages own an address for the body fallback (athenaeum#2714):
    a company, project or meeting page that MENTIONS someone's address in its
    body must not resolve that address to itself.
    """
    return bool(meta) and _clean_scalar(meta.get("type") if meta else "") == "person"


def build_body_email_index(wiki_root: Path) -> dict[str, Any]:
    """Compile ``address digest -> [[uid, filename], ...]`` from page BODIES.

    One pass over ``wiki_root/*.md`` at build time (athenaeum#2714), so a
    recall query never scans page bodies. Only pages with a non-empty ``uid``
    are indexed; frontmatter addresses are not (they belong to the registry
    handle set / excluded store). Only ``type: person`` pages are indexed, so
    an address a non-person page merely mentions never resolves to that page.
    Deterministic: sorted keys and entries.
    """
    index: dict[str, set[tuple[str, str]]] = {}
    if wiki_root.is_dir():
        for path in sorted(wiki_root.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, body = parse_frontmatter(text)
            uid = _clean_scalar(meta.get("uid")) if meta else ""
            if not uid or not _is_person_page(meta):
                continue
            for address in _BODY_EMAIL_RE.findall(body):
                index.setdefault(body_email_key(address), set()).add((uid, path.name))
    return {
        "version": REGISTRY_VERSION,
        "addresses": {k: [list(e) for e in sorted(index[k])] for k in sorted(index)},
    }


def render_body_email_index(index: dict[str, Any]) -> str:
    """Compact JSON text for the sidecar (read whole on every lookup, so no indent)."""
    return json.dumps(index, separators=(",", ":"), sort_keys=True) + "\n"


def lookup_body_email(knowledge_root: Path, address: str) -> list[str]:
    """Sorted uids of pages whose body carried *address* at the last build.

    O(1) after one JSON load of the sidecar. Each hit is re-verified against
    its page (one file read per candidate, never a corpus scan), so a page
    edited since the build cannot resolve on an address it no longer carries.
    Missing/unreadable/malformed sidecar (a corpus never rebuilt since the
    sidecar existed) returns ``[]`` — the caller reports plain ``no-match``.
    """
    try:
        raw = json.loads(
            (knowledge_root / BODY_EMAIL_INDEX_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    addresses = raw.get("addresses") if isinstance(raw, dict) else None
    entries = addresses.get(body_email_key(address)) if isinstance(addresses, dict) else None
    if not isinstance(entries, list):
        return []
    wanted = address.strip().lower()
    uids: set[str] = set()
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        uid, filename = entry
        if not (isinstance(uid, str) and isinstance(filename, str)) or "/" in filename:
            continue
        try:
            text = (knowledge_root / "wiki" / filename).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if _clean_scalar(meta.get("uid") if meta else "") != uid or not _is_person_page(meta):
            continue
        if any(a.lower() == wanted for a in _BODY_EMAIL_RE.findall(body)):
            uids.add(uid)
    return sorted(uids)
