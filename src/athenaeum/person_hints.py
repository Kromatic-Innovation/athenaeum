# SPDX-License-Identifier: Apache-2.0
"""Tier-2 person-hint candidates, shared by both intake transports (athenaeum#2653).

One helper, :func:`build_person_hints`, decides which existing people the tier-2
classifier is offered for a raw file. Both :func:`athenaeum.librarian.process_one`
(synchronous transport) and :func:`athenaeum.batch.process_batch_run` (Batch
API transport) call it, so the same raw file on the same corpus yields the same
candidates on either transport. :func:`cap_hint_claims` and
:func:`build_hint_action` do the same for the action-building end.

Two sources feed the candidate list:

- literal name/alias hits from the person registry (athenaeum#1866), capped at
  :data:`PERSON_HINT_MAX_CANDIDATES`;
- the configured owner (``owner.uid``, see :func:`athenaeum.config.resolve_owner`),
  offered on EVERY classify call in a RESERVED slot outside that cap, so a
  first-person claim ("I", "my", "the user") can attach to the owner's page.
  The owner candidate carries ``"role": "owner"``. It is de-duplicated against
  a literal hit on the same uid, and a hint-derived claim about the owner is
  exempt from :data:`~athenaeum.intake.PERSON_OBSERVATION_MAX_FANOUT`.

With no owner configured (or an owner whose page cannot be found) the result
is exactly the pre-athenaeum#2653 literal-hit list.

Layering: L3 service. Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.intake` (L2), :mod:`athenaeum.person_registry` (L2),
:mod:`athenaeum.identity_resolution` and :mod:`athenaeum.models` (L1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.config import resolve_owner
from athenaeum.identity_resolution import match_person_mentions
from athenaeum.intake import is_structured_jsonl_raw_file
from athenaeum.models import EntityAction, parse_frontmatter

if TYPE_CHECKING:
    from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile
    from athenaeum.person_registry import PersonRegistry

log = logging.getLogger(__name__)

#: Hard cap, per raw file, on how many tier-0 person-registry LITERAL hits
#: become hint candidates in the tier-2 classify prompt (athenaeum#1866).
#: Deliberately larger than
#: :data:`~athenaeum.intake.PERSON_OBSERVATION_MAX_FANOUT` (the cap on how many
#: hint-derived ACTIONS get built): a candidate costs a few hundred prompt
#: tokens on a call tier 2 already makes. The owner candidate does NOT count
#: against this cap (athenaeum#2653).
PERSON_HINT_MAX_CANDIDATES = 12

#: Hard cap, in characters, on the one-line description built for each
#: tier-2 person-hint candidate (athenaeum#1866).
PERSON_HINT_DESCRIPTION_MAX_CHARS = 200

#: ``role`` value marking the owner candidate in a candidate dict.
OWNER_ROLE = "owner"


@dataclass
class PersonHintSet:
    """The hint candidates for one raw file plus what the callers need to act."""

    candidates: list[dict[str, str]] = field(default_factory=list)
    uids: set[str] = field(default_factory=set)
    #: The owner uid when the owner was offered as a candidate, else ``None``.
    owner_uid: str | None = None


def _person_hint_description(path: Path) -> str:
    """One bounded line describing the person page at *path* for a hint.

    Prefers the frontmatter ``description:``; falls back to the first
    non-blank, non-heading body line. Returns ``""`` (never raises) when the
    page is unreadable or carries neither.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    meta, body = parse_frontmatter(text)
    desc = ""
    if isinstance(meta, dict):
        raw_desc = meta.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            desc = raw_desc.strip()
    if not desc:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            desc = stripped
            break
    collapsed = " ".join(desc.split())
    if len(collapsed) > PERSON_HINT_DESCRIPTION_MAX_CHARS:
        return collapsed[: PERSON_HINT_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
    return collapsed


def owner_uid_from_config(config: dict[str, Any] | None) -> str | None:
    """The configured owner uid, or ``None`` when no owner (uid) is configured."""
    owner = resolve_owner(config)
    uid = (owner or {}).get("uid") or ""
    return uid or None


def _owner_candidate(
    owner_uid: str,
    index: "EntityIndex",
    person_registry: "PersonRegistry | None",
) -> dict[str, str] | None:
    """Build the owner candidate dict, or ``None`` when the page is not found."""
    name = ""
    path: Path | None = None
    entry = person_registry.get_by_uid(owner_uid) if person_registry is not None else None
    if entry is not None:
        name, path = entry.name, entry.path
    else:
        found = index.get_by_uid(owner_uid)
        if found is not None and found.exists():
            meta, _ = parse_frontmatter(found.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and str(meta.get("type", "")).strip() == "person":
                name = str(meta.get("name", "") or "").strip()
                path = found
    if not name or path is None:
        return None
    return {
        "uid": owner_uid,
        "name": name,
        "description": _person_hint_description(path),
        "role": OWNER_ROLE,
    }


def build_person_hints(
    raw: "RawFile",
    wiki_root: Path,
    index: "EntityIndex",
    person_registry: "PersonRegistry | None",
    config: dict[str, Any] | None,
) -> PersonHintSet:
    """Build the tier-2 person-hint candidates for *raw* (athenaeum#1866, #2653).

    Literal registry hits first (capped, logged), then the owner in its
    reserved slot (see the module docstring). A structured machine-record
    ``.jsonl`` raw file (athenaeum#1684) gets no candidates at all, owner
    included: it is not prose a person said anything in.
    """
    result = PersonHintSet()
    if is_structured_jsonl_raw_file(raw):
        return result
    if person_registry is not None:
        person_hits = match_person_mentions(raw, wiki_root, index, person_registry)
        if len(person_hits) > PERSON_HINT_MAX_CANDIDATES:
            skipped_hits = person_hits[PERSON_HINT_MAX_CANDIDATES:]
            person_hits = person_hits[:PERSON_HINT_MAX_CANDIDATES]
            log.warning(
                "  T0 person-registry consult: hint-candidate cap %d reached "
                "for %s — skipping %d additional match(es): %s",
                PERSON_HINT_MAX_CANDIDATES,
                raw.ref,
                len(skipped_hits),
                [hit.uid for hit in skipped_hits],
            )
        for hit in person_hits:
            result.uids.add(hit.uid)
            result.candidates.append(
                {
                    "uid": hit.uid,
                    "name": hit.name,
                    "description": _person_hint_description(hit.path),
                }
            )
    owner_uid = owner_uid_from_config(config)
    if owner_uid is not None:
        owner_candidate = _owner_candidate(owner_uid, index, person_registry)
        if owner_candidate is not None:
            result.owner_uid = owner_uid
            if owner_uid in result.uids:
                # Matched literally too: one candidate, flagged as the owner.
                for c in result.candidates:
                    if c["uid"] == owner_uid:
                        c["role"] = OWNER_ROLE
            else:
                result.uids.add(owner_uid)
                result.candidates.append(owner_candidate)
    if result.candidates:
        log.info(
            "  T0 person-registry consult: %d hint candidate(s) for %s: %s",
            len(result.candidates),
            raw.ref,
            [c["uid"] for c in result.candidates],
        )
    return result


def cap_hint_claims(
    classified: "list[ClassifiedEntity]",
    owner_uid: str | None,
    ref: str,
) -> "tuple[list[ClassifiedEntity], list[str]]":
    """Apply :data:`~athenaeum.intake.PERSON_OBSERVATION_MAX_FANOUT` to hint claims.

    Returns ``(kept, dropped_uids)``. Order is preserved. A claim about the
    owner is exempt from the cap and does not count toward it (athenaeum#2653).
    """
    from athenaeum.intake import PERSON_OBSERVATION_MAX_FANOUT

    kept: list[ClassifiedEntity] = []
    dropped: list[str] = []
    count = 0
    for c in classified:
        if c.from_person_hint and not (owner_uid and c.existing_uid == owner_uid):
            if count >= PERSON_OBSERVATION_MAX_FANOUT:
                log.warning(
                    "  T2/T3 person-hint action cap %d reached for %s — "
                    "dropping additional hint-derived claim for uid=%s",
                    PERSON_OBSERVATION_MAX_FANOUT,
                    ref,
                    c.existing_uid,
                )
                if c.existing_uid:
                    dropped.append(c.existing_uid)
                continue
            count += 1
        kept.append(c)
    return kept, dropped


def build_hint_action(c: "ClassifiedEntity") -> EntityAction:
    """The tier-3 update action for a hint-derived claim (athenaeum#1866 AC4).

    ``observations`` is always the classifier's stated claim, never the
    ``raw.content[:2000]`` fallback.
    """
    return EntityAction(
        kind="update",
        name=c.name,
        entity_type="",
        tags=[],
        access="",
        existing_uid=c.existing_uid,
        observations=c.observations,
        from_person_hint=True,
    )


__all__ = [
    "OWNER_ROLE",
    "PERSON_HINT_DESCRIPTION_MAX_CHARS",
    "PERSON_HINT_MAX_CANDIDATES",
    "PersonHintSet",
    "build_hint_action",
    "build_person_hints",
    "cap_hint_claims",
    "owner_uid_from_config",
]
