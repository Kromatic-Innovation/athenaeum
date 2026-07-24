# SPDX-License-Identifier: Apache-2.0
"""PII off-corpus — contacts surface, entity-page lint, observation log (#427).

Corpus hygiene + ambient-egress reduction, NOT encryption (see the issue's
threat model: recall injects pages into arbitrary agent prompts, so the
retrieval-layer exclusion is the cheapest egress reduction — at-rest
encryption is ~pointless when the librarian itself needs the keys). This
module is the **code-only slice**: migrating live entity pages to
durable-IDs-only is operator task #437 (out of scope); wiring "retracting an
observation flags a dependent merge" is the retraction cascade, #435 (out of
scope, blocked on #425's merge-provenance model).

Four pieces, in the order the issue settles them:

1. **Contacts surface** (:func:`contacts_surface_root` / :func:`is_pii_class`)
   — a thin convenience wrapper over :mod:`athenaeum.storage`'s #429 adapter
   layer. This module does NOT hardcode ``~/knowledge/contacts/`` in any
   corpus consumer: the path is an adapter-config choice (see
   ``athenaeum.yaml``'s ``storage.mapping: {pii: excluded}`` example), and
   every embed/recall/merge consumer excludes the resolved surface root **by
   construction** because it lives outside ``wiki/`` + the configured
   ``recall.extra_intake_roots`` (the same by-construction property
   :mod:`tests.test_storage`'s ``TestByConstructionExclusion`` already proves
   for #429's adapter layer in general). This module's ``contacts_surface_root``
   is just the writer-facing convenience that resolves to that same excluded
   root under the conventional ``pii`` entity class.

2. **Entity-page lint** (:data:`PII_FLAG` / :func:`has_inline_contact_fields` /
   :func:`lint_inline_contact_fields`) — flags durable/archival-contact
   confusion on a page that stays IN the corpus: an entity page should carry
   only durable identifiers (name, LinkedIn, record id, Google-Contact id);
   inline ``emails:`` / ``phones:`` frontmatter (or an email/phone-shaped
   string in the body) is flagged as a validation warning, mirroring the
   #424 ``memory_class`` precedent (:mod:`athenaeum.schemas` / :mod:`athenaeum._lint`)
   — recoverable, not a hard failure, because migrating existing pages is
   #437. ``pii: true`` is the belt-and-suspenders flag an operator can set on
   a page that legitimately carries PII inline in narrative; every corpus
   consumer additionally excludes a ``pii: true`` page even when it is NOT on
   the excluded surface (see point 3).

3. **Corpus-consumer wiring for ``pii: true``** — :func:`is_pii_flagged`
   is the single predicate (mirrors :func:`athenaeum.authority.is_pointer_stub`)
   consulted by :mod:`athenaeum.search` (embed index build + keyword
   scan-on-query) and :mod:`athenaeum.wiki_dedupe` (merge-candidate
   discovery) so a flagged page is excluded from ALL THREE corpus
   capabilities without needing to move it off the default wiki surface.

4. **Observation log + supersession fold** — an append-only JSONL ledger
   recording ``(identifier, person_id, observed_at, source_msg_id)``
   mirroring :mod:`athenaeum.provenance`'s merge-provenance ledger (JSONL,
   ``O_APPEND`` + fsync, tolerant reader that skips a torn trailing line).
   ``identifier -> person`` is ~1:1 (a taken-over inbox still identifies the
   ORIGINAL person — routing is not identity) but several persons are
   allowed for a genuinely shared address, so a read returns ALL live
   attributions. Corrections are supersession records
   ``(retracts: obs_id, reason, at)`` — never edits/tombstones of the
   original observation. :func:`fold_observations` resolves "latest
   uncontradicted" per identifier via a DETERMINISTIC FOLD (sort by
   ``observed_at`` then ``obs_id`` for a stable tie-break; drop any
   observation a supersession record retracts) — deliberately NO
   clustering/similarity step, so this does not recreate the wiki-dedup
   merge problem the rest of the codebase works hard to keep separate.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.storage import surface_root_for_class

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Contacts surface (thin convenience over the #429 adapter layer)
# ---------------------------------------------------------------------------

#: Conventional entity-class name this module's callers route through the
#: storage-adapter layer. NOT special-cased in :mod:`athenaeum.storage` —
#: it is just a class name like any other; an operator maps it to the
#: built-in ``excluded`` adapter (or a custom one) via ``storage.mapping``
#: in ``athenaeum.yaml``. See the module docstring's point 1.
PII_ENTITY_CLASS = "pii"


def contacts_surface_root(
    knowledge_root: Path,
    config: dict[str, Any] | None,
) -> Path:
    """Resolve the on-disk root for the ``pii`` entity class.

    Delegates entirely to :func:`athenaeum.storage.surface_root_for_class` —
    no hardcoded ``contacts/`` path here. Absent any ``storage.mapping``
    entry for ``pii``, this resolves to the default wiki surface (so calling
    this with an unconfigured knowledge base is a no-op convenience, not a
    silent PII leak — the operator must explicitly map ``pii`` to the
    ``excluded`` adapter, exactly as ``athenaeum.yaml``'s shipped example
    comment shows, for this root to actually land outside the corpus).
    """
    return surface_root_for_class(PII_ENTITY_CLASS, config, knowledge_root)


def is_pii_class_excluded(config: dict[str, Any] | None) -> bool:
    """True when the ``pii`` entity class currently resolves out of the corpus.

    Convenience predicate for callers (e.g. a writer deciding where to place
    a new contact record) that want to confirm the operator has actually
    wired ``pii`` to an excluded-policy adapter before writing there.
    """
    from athenaeum.storage import is_excluded

    return is_excluded(PII_ENTITY_CLASS, config)


# ---------------------------------------------------------------------------
# 2. Entity-page lint — inline email/phone flag
# ---------------------------------------------------------------------------

#: Frontmatter flag mirroring :data:`athenaeum.authority.POINTER_STUB_FLAG`'s
#: pattern: a real bool or truthy string variant marks a page as carrying
#: PII inline in its narrative on purpose (belt-and-suspenders — the page
#: still stays in the corpus unless ALSO routed to an excluded surface).
PII_FLAG = "pii"

#: Frontmatter (list-valued) fields that hold archival contact data directly,
#: per the issue's entity-page rule: entity pages carry durable identifiers
#: only (name, LinkedIn, record id, Google-Contact id); ``emails``/``phones``
#: are the two contact-data fields that must not live inline going forward.
#: Migrating pre-existing pages that already carry these is #437 (out of
#: scope) — this module only flags, never rewrites, a page.
CONTACT_FRONTMATTER_FIELDS: tuple[str, ...] = ("emails", "phones")

# A conservative email-shaped token — good enough to flag a body/narrative
# line as "looks like inline contact data" without trying to be a fully
# RFC 5322-correct validator (a lint, not a hard gate).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A conservative phone-shaped token: 7+ digits allowing common separators
# (spaces, dashes, dots, parens) and an optional leading '+' or '('.
# Deliberately permissive about separators (so "+1-555-0100" and
# "(555) 010-0100" both match) but requires enough digits that ordinary
# numbers (years, page counts, issue numbers) don't false-positive.
_PHONE_RE = re.compile(r"(?<!\w)([+(]?\d[\d\-.\s()]{6,}\d)(?!\w)")


def _has_enough_digits(candidate: str, *, minimum: int = 7) -> bool:
    return sum(ch.isdigit() for ch in candidate) >= minimum


def is_pii_flagged(meta: dict[str, Any] | None) -> bool:
    """True when frontmatter carries a truthy ``pii`` flag (belt-and-suspenders).

    Same coercion contract as :func:`athenaeum.authority.is_pointer_stub` /
    :func:`athenaeum.models.parse_deprecated`: a real bool or a truthy string
    variant; missing/falsey => False. Single source of truth consulted by
    every corpus consumer (:mod:`athenaeum.search`, :mod:`athenaeum.wiki_dedupe`)
    so a page an operator has hand-flagged is excluded from embed/recall/merge
    even when it has not been moved to the excluded surface.
    """
    if not meta:
        return False
    value = meta.get(PII_FLAG)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def find_inline_emails(text: str) -> list[str]:
    """Return every email-shaped token found in *text*, in order, deduped."""
    seen: list[str] = []
    for m in _EMAIL_RE.finditer(text or ""):
        token = m.group(0)
        if token not in seen:
            seen.append(token)
    return seen


def find_inline_phones(text: str) -> list[str]:
    """Return every phone-shaped token found in *text*, in order, deduped."""
    seen: list[str] = []
    for m in _PHONE_RE.finditer(text or ""):
        token = m.group(1)
        if _has_enough_digits(token) and token not in seen:
            seen.append(token)
    return seen


def _frontmatter_contact_values(meta: dict[str, Any]) -> dict[str, list[str]]:
    """Return ``{field: [values]}`` for any non-empty contact field present."""
    found: dict[str, list[str]] = {}
    for field in CONTACT_FRONTMATTER_FIELDS:
        raw = meta.get(field)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v).strip() for v in values if str(v).strip()]
        if values:
            found[field] = values
    return found


def has_inline_contact_fields(meta: dict[str, Any], body: str = "") -> bool:
    """True when *meta*/*body* carry archival contact data on an entity page.

    Checks (a) the ``emails``/``phones`` frontmatter fields for any non-empty
    value, and (b) the body text for an email- or phone-shaped token. Does
    NOT consult :data:`PII_FLAG` — that is a separate belt-and-suspenders
    exclusion signal (point 3), not a suppressor of this lint. A page that is
    flagged ``pii: true`` AND still carries inline contact data is arguably
    doing the right corpus-exclusion thing already, but the lint still
    reports the shape so an operator auditing entity pages sees it (the flag
    changes what the CORPUS does with the page, not whether the page's
    shape is worth flagging).
    """
    if _frontmatter_contact_values(meta):
        return True
    return bool(find_inline_emails(body) or find_inline_phones(body))


def lint_inline_contact_fields(
    meta: dict[str, Any], body: str = "", fpath: Path | None = None
) -> str | None:
    """Return a lint message when an entity page carries inline contact data.

    Mirrors :func:`athenaeum._lint.lint_untyped_memory_class`'s shape: a pure
    function returning ``None`` (nothing to report) or a human-readable
    message naming the file when *fpath* is given. Intended for a batch
    lint pass over a wiki tree; :func:`has_inline_contact_fields` is the
    underlying boolean predicate for callers (e.g. a pydantic validator)
    that want a ``UserWarning`` instead of a collected message — see
    :class:`athenaeum.schemas.PersonWiki`'s ``_warn_inline_contact_fields``.
    """
    if not has_inline_contact_fields(meta, body):
        return None
    fields = sorted(_frontmatter_contact_values(meta))
    reasons: list[str] = []
    if fields:
        reasons.append(f"frontmatter field(s) {fields!r}")
    if find_inline_emails(body):
        reasons.append("email-shaped text in body")
    if find_inline_phones(body):
        reasons.append("phone-shaped text in body")
    detail = "; ".join(reasons)
    msg = f"inline contact data on entity page ({detail})"
    return f"{fpath}: {msg}" if fpath else msg


# ---------------------------------------------------------------------------
# 4. Observation log (append-only JSONL) + supersession + deterministic fold
# ---------------------------------------------------------------------------
#
# Schema (per the issue, settled — not re-litigated here):
#   observation: (identifier, person_id, observed_at, source_msg_id)
#   supersession: (retracts: obs_id, reason, at)
#
# ``identifier -> person`` is ~1:1 in the common case (a taken-over inbox
# still identifies the ORIGINAL person — routing is not identity) but SEVERAL
# persons are allowed for a genuinely shared address (a read returns ALL live
# attributions for that identifier, not just the newest). Temporality
# reconstructs from ``observed_at`` — there is deliberately no pre-modeled
# validity window (``valid_from``/``valid_until``) on an observation; that
# would require deciding IN ADVANCE how long an attribution holds, which is
# exactly the kind of clustering-shaped machinery the deterministic fold
# below is designed to avoid.

#: Schema version stamped on every record (mirrors
#: :data:`athenaeum.provenance.MERGE_PROVENANCE_VERSION`) so a future reader
#: can migrate.
OBSERVATION_LOG_VERSION = 1

#: Ledger filename, written under the contacts (excluded) surface root.
OBSERVATION_LOG_FILENAME = "_observations.jsonl"

#: Sidecar filename for supersession (correction) records — kept separate
#: from the observation ledger itself so an observation file is pure
#: "what was asserted, when" and never needs an in-place rewrite; a
#: correction is always a NEW record in its own append-only file.
SUPERSESSION_LOG_FILENAME = "_observation_supersessions.jsonl"


@dataclass(frozen=True)
class Observation:
    """One append-only observation record.

    ``obs_id`` is caller-supplied (the writer mints it, e.g. a ULID or a
    content hash) — this module does not invent an ID scheme, matching
    :mod:`athenaeum.provenance`'s merge-provenance ledger (which likewise
    takes ``merge_id`` from the caller rather than generating one).
    """

    obs_id: str
    identifier: str
    person_id: str
    observed_at: str
    source_msg_id: str


@dataclass(frozen=True)
class Supersession:
    """One append-only supersession (correction) record.

    Retracts a prior :class:`Observation` by ``obs_id`` — never edits or
    deletes it. ``reason`` is free text (e.g. "inbox reassigned to Janice
    2026-06-01"); ``at`` is the ISO-8601 timestamp the correction itself was
    recorded (distinct from ``observed_at`` on the observation it retracts).
    """

    retracts: str
    reason: str
    at: str


def default_observation_log_path(contacts_root: Path) -> Path:
    """Default observation ledger path: ``<contacts_root>/_observations.jsonl``."""
    return Path(contacts_root) / OBSERVATION_LOG_FILENAME


def default_supersession_log_path(contacts_root: Path) -> Path:
    """Default supersession ledger path: ``<contacts_root>/_observation_supersessions.jsonl``."""
    return Path(contacts_root) / SUPERSESSION_LOG_FILENAME


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Identical discipline to :func:`athenaeum.provenance._append_jsonl_line` /
    :mod:`athenaeum.spend`'s ledger writer: a single small ``O_APPEND`` write
    is atomic on local filesystems, so a crash can at worst leave a torn
    TRAILING line (which the reader skips), never corrupt an
    already-written record. Duplicated (not imported) because
    ``provenance._append_jsonl_line`` is a private helper of that module and
    this ledger is a conceptually separate log — mirroring the pattern is
    the explicit brief, not reusing the private symbol across modules.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_observation_record(
    *,
    obs_id: str,
    identifier: str,
    person_id: str,
    observed_at: str,
    source_msg_id: str,
) -> dict[str, Any]:
    """Build one observation record dict (the on-disk JSONL shape)."""
    return {
        "v": OBSERVATION_LOG_VERSION,
        "obs_id": obs_id,
        "identifier": identifier,
        "person_id": person_id,
        "observed_at": observed_at,
        "source_msg_id": source_msg_id,
    }


def append_observation(
    contacts_root: Path,
    *,
    obs_id: str,
    identifier: str,
    person_id: str,
    observed_at: str,
    source_msg_id: str,
    log_path: Path | None = None,
) -> Observation:
    """Append one observation record. Raises on a write failure (not best-effort).

    Unlike :func:`athenaeum.provenance.record_merge_provenance` (which
    swallows write failures because the merge's file-level side effects have
    already happened by the time it runs), an observation append IS the
    entire side effect here — there is nothing else to protect, so a failure
    must surface to the caller rather than be silently dropped.
    """
    record = build_observation_record(
        obs_id=obs_id,
        identifier=identifier,
        person_id=person_id,
        observed_at=observed_at,
        source_msg_id=source_msg_id,
    )
    target = log_path if log_path is not None else default_observation_log_path(contacts_root)
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return Observation(
        obs_id=obs_id,
        identifier=identifier,
        person_id=person_id,
        observed_at=observed_at,
        source_msg_id=source_msg_id,
    )


def build_supersession_record(
    *, retracts: str, reason: str, at: str | None = None
) -> dict[str, Any]:
    """Build one supersession record dict (the on-disk JSONL shape)."""
    return {
        "v": OBSERVATION_LOG_VERSION,
        "retracts": retracts,
        "reason": reason,
        "at": at if at is not None else _now_iso(),
    }


def append_supersession(
    contacts_root: Path,
    *,
    retracts: str,
    reason: str,
    at: str | None = None,
    log_path: Path | None = None,
) -> Supersession:
    """Append one supersession (correction) record. Raises on write failure.

    ``retracts`` is the ``obs_id`` of the observation being corrected.
    Correction-of-a-correction is expressible (a later supersession can
    retract an earlier one's ``obs_id`` too, since supersessions are not
    addressable independently here — see :func:`fold_observations` for how
    the fold resolves the resulting chain) but the common case is retracting
    an :class:`Observation`.
    """
    record = build_supersession_record(retracts=retracts, reason=reason, at=at)
    target = (
        log_path if log_path is not None else default_supersession_log_path(contacts_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return Supersession(retracts=record["retracts"], reason=record["reason"], at=record["at"])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every well-formed JSON object line from *path*.

    Tolerates a torn/partial trailing line (a crash mid-write) or a hand-edit
    — such lines are skipped, not fatal, mirroring
    :func:`athenaeum.provenance.read_merge_provenance`. Returns ``[]`` when
    the file does not exist.
    """
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            records.append(record)
    return records


def read_observations(
    contacts_root: Path, *, log_path: Path | None = None
) -> list[Observation]:
    """Read every well-formed observation record, in file order."""
    target = log_path if log_path is not None else default_observation_log_path(contacts_root)
    out: list[Observation] = []
    for rec in _read_jsonl(target):
        try:
            out.append(
                Observation(
                    obs_id=str(rec["obs_id"]),
                    identifier=str(rec["identifier"]),
                    person_id=str(rec["person_id"]),
                    observed_at=str(rec["observed_at"]),
                    source_msg_id=str(rec["source_msg_id"]),
                )
            )
        except KeyError:
            continue  # malformed record (missing a required key); skip
    return out


def read_supersessions(
    contacts_root: Path, *, log_path: Path | None = None
) -> list[Supersession]:
    """Read every well-formed supersession record, in file order."""
    target = (
        log_path if log_path is not None else default_supersession_log_path(contacts_root)
    )
    out: list[Supersession] = []
    for rec in _read_jsonl(target):
        try:
            out.append(
                Supersession(
                    retracts=str(rec["retracts"]),
                    reason=str(rec["reason"]),
                    at=str(rec["at"]),
                )
            )
        except KeyError:
            continue
    return out


def fold_observations(
    observations: list[Observation],
    supersessions: list[Supersession] | None = None,
) -> dict[str, list[Observation]]:
    """Deterministically fold observations into ``{identifier: [live obs]}``.

    The read-side "latest uncontradicted" resolution the issue specifies:

    1. Drop every observation whose ``obs_id`` is named by ANY supersession's
       ``retracts`` — a corrected observation is gone, permanently (the
       supersession record itself is never deleted; it just removes its
       target from every future fold).
    2. Group the SURVIVING observations by ``identifier``.
    3. Within each identifier's group, keep one entry per DISTINCT
       ``person_id`` — the most recent survives (by ``observed_at``, then
       ``obs_id`` as a stable tie-break when two observations share a
       timestamp) — but a genuinely different ``person_id`` is NEVER
       collapsed into the same slot. This is what makes a shared-address
       read return ALL currently-attributed persons rather than only the
       newest write for that identifier.

    Deliberately no similarity/clustering step: two observations are "the
    same claim" if and only if they share both ``identifier`` AND
    ``person_id`` — string equality, nothing fuzzier. Two different people
    sharing one address is not a conflict to resolve; it is exactly the
    shared-address case the issue calls out, so both survive. A correction
    (Jason -> Janice) works by RETRACTING the Jason observation via a
    supersession, not by the fold guessing which of two same-identifier
    writes is more authoritative.

    Returns ``{identifier: [Observation, ...]}`` — each identifier's list is
    sorted by ``observed_at`` (then ``obs_id``) for deterministic output;
    an identifier with no surviving observations is simply absent (never an
    empty list).
    """
    retracted = {s.retracts for s in (supersessions or [])}
    live = [o for o in observations if o.obs_id not in retracted]

    by_identifier: dict[str, list[Observation]] = {}
    for obs in live:
        by_identifier.setdefault(obs.identifier, []).append(obs)

    folded: dict[str, list[Observation]] = {}
    for identifier, obs_list in by_identifier.items():
        # Keep the latest observation per distinct person_id (deterministic
        # tie-break on obs_id so two same-timestamp writes fold predictably
        # regardless of input order).
        latest_by_person: dict[str, Observation] = {}
        for obs in obs_list:
            current = latest_by_person.get(obs.person_id)
            if current is None or (obs.observed_at, obs.obs_id) > (
                current.observed_at,
                current.obs_id,
            ):
                latest_by_person[obs.person_id] = obs
        folded[identifier] = sorted(
            latest_by_person.values(), key=lambda o: (o.observed_at, o.obs_id)
        )
    return folded


def resolve_identifier(
    identifier: str,
    observations: list[Observation],
    supersessions: list[Supersession] | None = None,
) -> list[Observation]:
    """Convenience: fold, then return the live observations for one identifier.

    Returns ``[]`` when the identifier has no surviving observations (never
    seen, or every observation for it was retracted).
    """
    return fold_observations(observations, supersessions).get(identifier, [])


__all__ = [
    "PII_ENTITY_CLASS",
    "PII_FLAG",
    "CONTACT_FRONTMATTER_FIELDS",
    "OBSERVATION_LOG_VERSION",
    "OBSERVATION_LOG_FILENAME",
    "SUPERSESSION_LOG_FILENAME",
    "Observation",
    "Supersession",
    "contacts_surface_root",
    "is_pii_class_excluded",
    "is_pii_flagged",
    "find_inline_emails",
    "find_inline_phones",
    "has_inline_contact_fields",
    "lint_inline_contact_fields",
    "default_observation_log_path",
    "default_supersession_log_path",
    "build_observation_record",
    "build_supersession_record",
    "append_observation",
    "append_supersession",
    "read_observations",
    "read_supersessions",
    "fold_observations",
    "resolve_identifier",
]
