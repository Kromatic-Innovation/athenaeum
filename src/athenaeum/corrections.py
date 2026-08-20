# SPDX-License-Identifier: Apache-2.0
"""Deterministic field-correction fast path (issue athenaeum#797).

Implements the conformance format specified in `docs/field-corrections.md`:
a `.jsonl` batch a writer MAY submit into the ordinary `raw/<source>/` intake
tree to have a field-level change applied at tier 0 (mechanical, no LLM),
instead of paying LLM compilation per fact or bypassing the librarian
entirely.

**The one rule this module must preserve** (`field-corrections.md` §1.1):
conformance sets how deep in the tier ladder a submission enters; it never
sets whether it enters. Every failure to conform is a fallthrough to a
higher tier, never a rejection — a `None`/empty return from any function
here means "the caller must route this to ordinary intake / reasoning",
never "drop it."

Layering: L2 primitive, same tier as :mod:`athenaeum.intake`. Imports only
leaf/service modules — :mod:`athenaeum.models`, :mod:`athenaeum.provenance`,
:mod:`athenaeum.precedence`, :mod:`athenaeum.registry`,
:mod:`athenaeum.schemas`, :mod:`athenaeum.atomic_io`, :mod:`athenaeum.config`,
:mod:`athenaeum.storage`, and (function-local, per call site — see
:func:`_resolve_email_handle` and the §7.1 sensitivity-routing helpers)
:mod:`athenaeum.pii`. Must never import :mod:`athenaeum.intake`,
:mod:`athenaeum.librarian`, :mod:`athenaeum.merge`, or :mod:`athenaeum.tiers`
— `intake.py` imports :func:`parse_batch_envelope` from here (the "valid
envelope" single definition, §3.1), and a back-edge would reintroduce the
import cycle those modules were split to avoid (issue athenaeum#545). The
run-loop wiring (`librarian._run_correction_phase`), the escalation writer
(`tiers.tier4_escalate`) and discovery (`intake.discover_raw_files`) all call
INTO this module; it never calls back.

This module is organized in the order `docs/field-corrections.md` presents
its sections:

- §3.1 valid-envelope recognition (:func:`parse_batch_envelope`) — the
  single definition shared by `intake.discover_raw_files`'s skip and this
  module's own batch processing, so the two can never drift (§3.1's own
  warning: a schema_version check dropped from one site but not the other
  reintroduces the "seen by nothing" bug this design exists to remove).
- §3.2/§3.3/§5.2 record shape, target resolution, ``correction_id``
  (:func:`hoist_record`, :func:`compute_correction_id`, :func:`resolve_target`).
- §4/§5.1/§6/§7 the tier-0 applier (:func:`process_correction_record`) —
  op semantics, the delta gate, the precedence policy + monotone
  suppression rule, and routing (sensitivity + schema evolution).
- §5.3/§5.4/§8/§8.1/§10.2 batch-level orchestration
  (:func:`apply_correction_batch`) — the audit ledger, retirement,
  fallthrough handoff, and escalation, called from
  `librarian._run_correction_phase`.

**Decisions the design doc leaves to the implementation** (documented here,
not silently baked in — see the athenaeum#797 completion report for the full list):

- The undated-tie / escalation date comparison (§6.2's "newer ``observed_at``
  wins") has no PERSISTED counterpart to compare against — ``field_sources``'
  per-value shape is ``{value, source}`` only (§4), no ``observed_at``. This
  module compares the correction's ``observed_at`` against the target page's
  (or sensitive-surface record's) ``updated:`` timestamp as the closest
  available proxy for "when the incumbent was last touched."
- §7.2 schema evolution only fires for an attribute that is BOTH on the
  §6.3 allowlist AND has an explicit ``librarian.corrections.schema_slots``
  entry. An allowlisted attribute with no ``schema_slots`` entry writes
  directly as ordinary frontmatter (schemas.py's per-type models already
  tolerate unknown keys via ``extra="allow"``, same mechanism as
  source-handle keys) — §7.2's three dispositions are for when the
  deployment explicitly asks for non-default routing, not the general case.
- Escalation reuses the EXISTING ``tiers.tier4_escalate`` / ``EscalationItem``
  writer rather than inventing a correction-specific one — ``EscalationItem``
  already defaults ``members=[]`` and ``proposal=None``, so a memberless
  correction entry renders through the unmodified existing path. This
  module adds its OWN ``correction_id`` dedup pass in front of that call
  (§8's dedup requirement — ``tier4_escalate``'s own dedup keys on a
  members-involved/passage-hash shape that a correction does not carry).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_corrections_fields,
    resolve_corrections_max_batch_bytes,
    resolve_corrections_max_records_per_batch,
    resolve_corrections_max_records_per_run,
    resolve_corrections_schema_slots,
    resolve_corrections_sensitive_fields,
)
from athenaeum.models import (
    EntityIndex,
    WikiEntity,
    coerce_bucket,
    generate_uid,
    load_schema_list,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    validity_bound_str,
)
from athenaeum.precedence import source_rank
from athenaeum.provenance import parse_source
from athenaeum.registry import LIST_HANDLE_KEYS, SOURCE_HANDLE_KEYS
from athenaeum.schemas import KNOWN_TYPES, validate_wiki_meta
from athenaeum.storage import surface_root_for_class

log = logging.getLogger(__name__)

#: `schema_version` values this build knows how to process. A batch
#: declaring any other value is deliberately NOT a valid envelope
#: (`docs/field-corrections.md` §3.1 condition 3) — it is left as ordinary
#: intake rather than being skipped by discovery and then found
#: un-processable by the correction phase, which is exactly the silent-drop
#: bug §3.1 calls out by name.
KNOWN_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def parse_batch_envelope(first_line: str) -> dict[str, Any] | None:
    """Parse a `.jsonl` file's first line as a correction-batch envelope.

    Returns the parsed envelope dict when ALL of
    `docs/field-corrections.md` §3.1's conditions hold:

    1. It parses as JSON.
    2. ``record == "batch"``.
    3. ``schema_version`` is present and is a version this build knows how
       to process (:data:`KNOWN_SCHEMA_VERSIONS`).
    4. ``batch_id`` and ``created_at`` are present (non-empty).

    Returns ``None`` otherwise — deliberately not an exception. This is THE
    single definition of "valid envelope," used by both
    `intake.discover_raw_files`'s skip (so a conformant batch is claimed by
    the correction phase instead of being double-processed as prose) and by
    the correction phase itself. A line that fails this check is ordinary
    raw intake — nothing here rejects a batch or a file; only discovery's
    caller decides where a non-envelope line ends up (ordinary intake,
    unchanged).
    """
    try:
        obj = json.loads(first_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("record") != "batch":
        return None
    schema_version = obj.get("schema_version")
    # bool is an int subclass; exclude it explicitly (matches the
    # bool-is-an-int-subclass guard convention in athenaeum.config).
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return None
    if schema_version not in KNOWN_SCHEMA_VERSIONS:
        return None
    if not obj.get("batch_id") or not obj.get("created_at"):
        return None
    return obj


# ---------------------------------------------------------------------------
# §3.2 record shape, §5.2 correction_id, §3.3 target resolution
# ---------------------------------------------------------------------------

#: The full set of keys a correction record may carry. An unknown key makes
#: the record non-conformant (§3.2) so it takes the fallthrough path — a
#: typo'd key must not quietly drop a constraint.
ALLOWED_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "record",
        "correction_id",
        "target",
        "op",
        "field",
        "value",
        "source",
        "observed_at",
        "note",
        "usage_class",
        # Issue athenaeum#904 (AC2): optional decay annotations. Same shape as
        # `usage_class` — they ride alongside whatever field/value the
        # correction is actually proposing, applied to the TARGET entity's
        # page-level frontmatter regardless of op/field, never routed through
        # the field/value allowlist+precedence machinery.
        "bucket",
        "valid_until",
    }
)

#: Envelope ``defaults`` keys §3.2 permits to be hoisted into a record.
HOISTABLE_DEFAULT_KEYS: frozenset[str] = frozenset({"source", "observed_at", "op", "field"})

#: The closed §5.3 disposition vocabulary — every record ends in exactly one.
DISPOSITIONS: frozenset[str] = frozenset(
    {
        "applied",
        "noop",
        "deferred-lower-precedence",
        "escalated",
        "raised-tier",
        "routed-elsewhere",
        "held-schema-proposal",
        "recorded-as-prose",
    }
)

#: Dispositions §5.4 treats as terminal for the RECORD on the first pass
#: (a batch is retired once every record it carries is terminal).
_FIRST_PASS_TERMINAL: frozenset[str] = frozenset(
    {
        "applied",
        "noop",
        "routed-elsewhere",
        "deferred-lower-precedence",
        "recorded-as-prose",
        "raised-tier",  # terminal for the BATCH once §8.1's handoff file is written
        "held-schema-proposal",  # terminal once the proposal is recorded
        "escalated",  # terminal once the question is recorded
    }
)


def hoist_record(raw_record: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Hoist envelope ``defaults`` into *raw_record* — the record's own key
    wins (§3.2). Only :data:`HOISTABLE_DEFAULT_KEYS` are eligible; anything
    else on ``defaults`` (a submitter typo, e.g.) is ignored rather than
    silently smuggled into the effective record.
    """
    if not isinstance(defaults, dict):
        defaults = {}
    hoisted: dict[str, Any] = {
        k: v for k, v in defaults.items() if k in HOISTABLE_DEFAULT_KEYS
    }
    hoisted.update(raw_record)
    return hoisted


def compute_correction_id(
    *, schema_version: Any, target: Any, op: Any, field_name: Any, value: Any
) -> str:
    """§5.2: ``sha256(canonical_json([schema_version, target, op, field,
    value]))[:16]``, keys sorted, no insignificant whitespace.

    MUST be called with the EFFECTIVE (post-hoist, per :func:`hoist_record`)
    ``op``/``field``/``value`` — never the raw record's own possibly-absent
    copies — so an inlined record and an otherwise-identical record
    inheriting ``op``/``field`` from envelope ``defaults`` hash to the SAME
    id (the exact AC this function exists to satisfy). ``source`` and
    ``observed_at`` are deliberately excluded (§5.2): the same factual
    change proposed twice is the same correction regardless of when it was
    observed.
    """
    payload = [schema_version, target, op, field_name, value]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_registry(knowledge_root: Path) -> dict[str, Any]:
    """Load ``<knowledge_root>/registry.json``'s ``entities`` map.

    Returns ``{}`` when the file is missing, unreadable, or malformed — a
    handle-shaped target simply cannot resolve (§3.3: "a target that
    resolves to zero entities... does not fail. It is a correction whose
    entity identity needs reasoning, so it goes up the ladder"), never a
    crash.
    """
    registry_path = knowledge_root / "registry.json"
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entities = raw.get("entities") if isinstance(raw, dict) else None
    return entities if isinstance(entities, dict) else {}


#: The handle key resolved through the PII/contacts surface rather than
#: through ``registry.json`` (issue athenaeum#884).
#:
#: It is deliberately NOT a :data:`~athenaeum.registry.SOURCE_HANDLE_KEYS`
#: member and must never become one. ``registry.json`` is compiled from WIKI
#: frontmatter, and the athenaeum#502/#507 migrator scans every frontmatter value,
#: preserves only ``DURABLE_IDENTIFIER_FIELDS``, and explicitly folds
#: ``alt_emails`` onto the excluded record — so an email seeded as a registry
#: handle is migrated off the page on the next ``storage migrate-pii`` run and
#: its registry entry evaporates. The address lives on the PII surface by
#: design (athenaeum#427/#437), which is why the resolution has to read it there.
EMAIL_HANDLE_KEY = "email"


@dataclass(frozen=True)
class EmailHandleResolution:
    """Outcome of resolving an ``email`` handle through the PII surface.

    ``path`` is set only for ``kind == "resolved"``. ``reason`` is a stable,
    machine-readable token recorded alongside the ``raised-tier`` disposition
    so the ledger can tell these apart — the amendment on athenaeum#884 is
    explicit that an orphan uid must not be conflated with an ordinary
    zero-match: **zero-match means the address is unknown; orphan-uid means
    the address is known and its person page is missing.** The second is a
    store-consistency signal worth surfacing rather than swallowing, and both
    look identical if all you record is "raised a tier".

    The disposition itself stays ``raised-tier`` for every non-resolved kind —
    :data:`DISPOSITIONS` is a closed §5.3 vocabulary, and every one of these
    genuinely does raise a tier. What differs is the reason, not the outcome.
    """

    kind: str
    path: Path | None = None
    reason: str | None = None


def _resolve_email_handle(
    value: str,
    declared_type: str | None,
    *,
    index: EntityIndex,
    knowledge_root: Path | None,
    config: dict[str, Any] | None,
    excluded_index: Any | None = None,
) -> EmailHandleResolution:
    """Resolve ``email -> contact record -> uid -> wiki page`` (issue athenaeum#884).

    The tier-0, LLM-free half of the operator's decision on athenaeum#858/#859: put
    the resolution inside the librarian rather than exposing a reverse-lookup
    read API, so the caller never needs the uid and no new caller gains
    contact-surface access.

    **Every step goes through :mod:`athenaeum.pii`.** This function never
    constructs a contacts-surface path itself
    (``docs/one-way-in-one-way-out.md`` §3) — it asks ``pii`` for the surface
    root, for the matching records, and for the uid on a record. The librarian
    is not an exception to the one-way-out rule; it is an implementation of it.

    Ambiguity is deduped by UID, not by record: several records carrying the
    SAME uid are one person described twice, not an ambiguous address, and
    raising a tier for that would send a resolvable case to reasoning. Several
    DISTINCT uids is the genuine "which person is this?" question, and that is
    the one routed up.
    """
    from athenaeum import pii

    if knowledge_root is None:
        # No knowledge root supplied means this caller cannot reach the
        # excluded surface at all. Unresolvable, exactly as before this branch
        # existed — never an error, and never a create.
        return EmailHandleResolution(kind="unresolvable", reason="email-handle-unavailable")

    contacts_root = pii.contacts_surface_root(knowledge_root, config)
    records = pii.resolve_contact_records(contacts_root, value, index=excluded_index)
    if not records:
        return EmailHandleResolution(kind="unresolvable", reason="email-handle-no-match")

    uids: list[str] = []
    for record in records:
        uid = pii.uid_on_record(record)
        if uid is not None and uid not in uids:
            uids.append(uid)

    if not uids:
        # The address is known but no record carries a uid — nothing to join
        # to a page. Distinct from both zero-match and orphan-uid.
        return EmailHandleResolution(
            kind="unresolvable", reason="email-handle-record-without-uid"
        )
    if len(uids) > 1:
        return EmailHandleResolution(
            kind="unresolvable", reason="email-handle-ambiguous"
        )

    path = index.get_by_uid(uids[0])
    if path is None or not path.exists() or not index.has_entity_format(path):
        # The measured orphan population (roughly 47 of 12,960 records on the
        # 2026-08-12 snapshot): the address IS known, and its person page is
        # missing. Raise a tier, never create, never crash — and say WHY, so a
        # store-consistency problem is not filed away as "unknown address".
        return EmailHandleResolution(
            kind="unresolvable", reason="email-handle-orphan-uid"
        )

    if declared_type:
        guarded = _cross_type_guard(path, declared_type)
        if guarded is None:
            return EmailHandleResolution(
                kind="unresolvable", reason="email-handle-cross-type"
            )
        path = guarded

    return EmailHandleResolution(kind="resolved", path=path)


#: Per-run in-memory overlay of dry-run-created pages: ``entity_path ->
#: (meta, body)`` (issue athenaeum#873). A `dry_run` batch that CREATES an
#: entity (the athenaeum#865 tier-0 create-by-handle path) must not write
#: the page to disk, but a LATER record in the same batch keyed on the same
#: handle still needs to read it -- this dict is what makes that page
#: readable without touching the filesystem. Populated only by
#: :func:`process_correction_record`'s dry-run create branch; must be
#: constructed once per batch RUN (see `run_correction_phase`) and never
#: persisted. A real (non-dry) run never writes to it, so every check below
#: reduces to today's behaviour when it is empty or `None`.
DryRunPageOverlay = dict[Path, tuple[dict[str, Any], str]]


def _page_exists(path: Path, dry_run_pages: DryRunPageOverlay | None) -> bool:
    """Whether *path* is resolvable as a page: genuinely on disk, OR a
    dry-run create's notionally-written page recorded in *dry_run_pages*
    (issue athenaeum#873). ``dry_run_pages`` is only ever non-empty while
    processing a dry-run batch, so a real run's check is exactly
    ``path.exists()``, unchanged."""
    if path.exists():
        return True
    return dry_run_pages is not None and path in dry_run_pages


def resolve_target(
    target: Any,
    *,
    index: EntityIndex,
    registry_entities: dict[str, Any],
    knowledge_root: Path | None = None,
    config: dict[str, Any] | None = None,
    excluded_index: Any | None = None,
    dry_run_pages: DryRunPageOverlay | None = None,
) -> Path | None:
    """Resolve a §3.3 target shape to an existing entity-format page path.

    Returns ``None`` for anything that does not resolve UNAMBIGUOUSLY to
    exactly one existing entity-format page — a target resolving to zero or
    several entities is deliberately not a failure at this layer; the
    caller raises a tier (§8), it never rejects.

    Args:
        knowledge_root: Root of the knowledge base. Required only to resolve
            an ``email`` handle, which joins through the PII surface rather
            than ``registry.json`` (issue athenaeum#884). Optional and defaulting
            to ``None`` so every existing caller is untouched: without it an
            email handle simply does not resolve, which is exactly what
            happened before this branch existed.
        config: Resolved ``athenaeum.yaml``, passed to
            :func:`athenaeum.pii.contacts_surface_root` so the surface
            resolves per the operator's ``storage.mapping``.
        excluded_index: An already-built
            :class:`~athenaeum.pii.ExcludedRecordIndex` to resolve through,
            so a batch of corrections pays the surface scan once rather than
            once per record.
        dry_run_pages: The per-run dry-run page overlay (issue athenaeum#873),
            consulted only in the ``SOURCE_HANDLE_KEYS`` branch below — the
            only shape the athenaeum#865 create branch ever mints a page for
            (uid/name targets never create, so a page an overlay would carry
            can never be uid/name-addressed). Optional and defaulting to
            ``None`` so every existing caller is untouched.
    """
    if not isinstance(target, dict) or not target:
        return None

    uid = target.get("uid")
    if uid is not None:
        if not isinstance(uid, str) or not uid.strip():
            return None
        path = index.get_by_uid(uid.strip())
        if path is not None and path.exists() and index.has_entity_format(path):
            return path
        return None

    etype = target.get("type")
    name = target.get("name")
    handle = target.get("handle")

    if name is not None:
        if (
            not isinstance(etype, str)
            or not etype.strip()
            or not isinstance(name, str)
            or not name.strip()
        ):
            return None
        resolved = index.lookup(name.strip())
        if resolved is None:
            return None
        _, path = resolved
        if not path.exists() or not index.has_entity_format(path):
            return None
        return _cross_type_guard(path, etype.strip())

    if isinstance(handle, dict) and handle:
        if len(handle) != 1:
            return None  # ambiguous handle shape
        (key, value), = handle.items()
        if key == EMAIL_HANDLE_KEY:
            # Issue athenaeum#884: an email handle resolves through the PII
            # surface, NOT through registry.json — see EMAIL_HANDLE_KEY for
            # why it cannot be a registry handle key. Checked BEFORE the
            # SOURCE_HANDLE_KEYS allowlist below, which it is deliberately
            # not a member of.
            if not isinstance(value, str) or not value.strip():
                return None
            etype_str = etype.strip() if isinstance(etype, str) and etype.strip() else None
            return _resolve_email_handle(
                value.strip(),
                etype_str,
                index=index,
                knowledge_root=knowledge_root,
                config=config,
                excluded_index=excluded_index,
            ).path
        if key not in SOURCE_HANDLE_KEYS:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        matches = _handle_matches(registry_entities, key, value)
        if len(matches) != 1:
            return None  # zero or ambiguous — §3.3, raise a tier
        path = index.get_by_uid(matches[0])
        if (
            path is None
            or not _page_exists(path, dry_run_pages)
            or not index.has_entity_format(path)
        ):
            return None
        if isinstance(etype, str) and etype.strip():
            return _cross_type_guard(path, etype.strip(), dry_run_pages=dry_run_pages)
        return path

    return None


def _handle_matches(registry_entities: dict[str, Any], key: str, value: str) -> list[str]:
    """The registry uids whose ``handles.<key>`` carries *value* — shared by
    :func:`resolve_target` and :func:`resolve_target_for_apply` so the two
    can never drift on what "matches" means (issue athenaeum#865)."""
    matches: list[str] = []
    for candidate_uid, entity in registry_entities.items():
        if not isinstance(entity, dict):
            continue
        handles = entity.get("handles")
        if not isinstance(handles, dict):
            continue
        hv = handles.get(key)
        if isinstance(hv, list) and value in hv:
            matches.append(candidate_uid)
        elif isinstance(hv, str) and hv == value:
            matches.append(candidate_uid)
    return matches


@dataclass(frozen=True)
class TargetResolution:
    """§3.3 target resolution outcome, extended with the athenaeum#865 create
    branch. ``kind`` is exactly one of:

    - ``"existing"`` — resolved unambiguously to one existing entity-format
      page (``path`` set). Identical to what :func:`resolve_target` alone
      returns; every existing caller of *that* function is untouched.
    - ``"creatable"`` — a ``{"type", "handle"}`` target whose ``handle``
      key is a :data:`~athenaeum.registry.SOURCE_HANDLE_KEYS` member, whose
      ``type`` is a non-blank string, and which resolved to ZERO existing
      entities. The handle is a stable external key (§3.3's "external
      systems key on their own identifiers, not on athenaeum uids"), so a
      zero-match handle is the "this entity does not exist yet, and here is
      how a later submission finds it" signal — unlike a zero-match
      ``{"uid"}`` or ``{"type","name"}`` target, which stays unresolvable
      (a name match alone would manufacture a duplicate per spelling
      variant; there is nothing to dedupe a later submission against).
    - ``"unresolvable"`` — everything else: no match on the ``uid``/``name``
      shapes, an ambiguous (>1) handle match, a handle whose key is not on
      the allowlist, or a handle target with no/blank declared ``type``
      (the submitter must declare what to create). The caller raises a
      tier (§8), exactly as a bare :func:`resolve_target` miss always has.
    """

    kind: str
    path: Path | None = None
    entity_type: str | None = None
    handle_key: str | None = None
    handle_value: str | None = None
    #: Machine-readable detail recorded alongside the disposition (issue
    #: athenaeum#884). ``None`` for every pre-existing outcome, so nothing that
    #: reads this dataclass today sees a change. Set for the ``email`` handle
    #: branch so an orphan uid ("the address is known, its page is missing")
    #: is distinguishable in the ledger from an ordinary zero-match ("the
    #: address is unknown") — both raise a tier, and only the reason tells
    #: them apart.
    reason: str | None = None


def resolve_target_for_apply(
    target: Any,
    *,
    index: EntityIndex,
    registry_entities: dict[str, Any],
    knowledge_root: Path | None = None,
    config: dict[str, Any] | None = None,
    excluded_index: Any | None = None,
    dry_run_pages: DryRunPageOverlay | None = None,
) -> TargetResolution:
    """§3.3 resolution, extended with the athenaeum#865 tier-0 create branch.

    Delegates the "does an existing entity match" question to
    :func:`resolve_target` unchanged — this function only decides what a
    ``None`` from that call means: still unresolvable, or creatable.

    **The ``email``-handle carve-out (issue athenaeum#884) is load-bearing.** A
    zero-match ``handle: {email}`` target must NEVER enter athenaeum#865's tier-0
    create branch; it raises a tier per the ordinary §8 fallthrough. voltaire's
    *ordinary* conversation-intake path emits this target shape for every
    triaged correspondent with no significance gate in front of it, so a
    create-capable email handle would auto-create a person page per
    correspondent — cold senders and sales sequences included — which is
    exactly the "write everything and let the librarian decide" firehose the
    operator rejected. The guard below is written EXPLICITLY rather than left
    to rest on ``email`` being absent from ``SOURCE_HANDLE_KEYS``: that
    absence is load-bearing for a different reason (see
    :data:`EMAIL_HANDLE_KEY`), and a future widening of that tuple must not
    silently open the create branch to every address voltaire has ever seen.

    ``knowledge_root`` / ``config`` / ``excluded_index`` are threaded to
    :func:`resolve_target` — see its docstring; all three are optional and
    every existing caller is unaffected. ``dry_run_pages`` (issue athenaeum#873)
    is threaded the same way — see :func:`resolve_target`'s docstring.
    """
    existing = resolve_target(
        target,
        index=index,
        registry_entities=registry_entities,
        knowledge_root=knowledge_root,
        config=config,
        excluded_index=excluded_index,
        dry_run_pages=dry_run_pages,
    )
    if existing is not None:
        return TargetResolution(kind="existing", path=existing)

    if not isinstance(target, dict) or not target:
        return TargetResolution(kind="unresolvable")

    # Only a handle-shaped target ever creates (AC: "No name-only
    # creation") — a uid the writer invented, or a bare name match, has no
    # stable key for a later submission to dedupe against.
    if target.get("uid") is not None:
        return TargetResolution(kind="unresolvable")
    if target.get("name") is not None:
        return TargetResolution(kind="unresolvable")

    handle = target.get("handle")
    if not isinstance(handle, dict) or len(handle) != 1:
        return TargetResolution(kind="unresolvable")
    (key, value), = handle.items()
    if key == EMAIL_HANDLE_KEY:
        # THE CARVE-OUT (issue athenaeum#884). Never creatable, notwithstanding
        # athenaeum#865 — see this function's docstring for why. Re-run the
        # resolution to recover WHICH non-resolving case this was, so the
        # ledger can tell an orphan uid from an unknown address.
        etype = target.get("type")
        outcome = _resolve_email_handle(
            value.strip() if isinstance(value, str) else "",
            etype.strip() if isinstance(etype, str) and etype.strip() else None,
            index=index,
            knowledge_root=knowledge_root,
            config=config,
            excluded_index=excluded_index,
        )
        return TargetResolution(kind="unresolvable", reason=outcome.reason)
    if key not in SOURCE_HANDLE_KEYS:
        return TargetResolution(kind="unresolvable")
    if not isinstance(value, str) or not value.strip():
        return TargetResolution(kind="unresolvable")
    value = value.strip()

    matches = _handle_matches(registry_entities, key, value)
    if matches:
        # resolve_target already returned None, so this is >1 (ambiguous —
        # §3.3, raise) or a single match whose page is stale/missing/wrong
        # format (a registry/wiki inconsistency, not a "safe to create"
        # signal). Either way, not creatable.
        return TargetResolution(kind="unresolvable")

    etype = target.get("type")
    if not isinstance(etype, str) or not etype.strip():
        return TargetResolution(kind="unresolvable")

    return TargetResolution(
        kind="creatable",
        entity_type=etype.strip(),
        handle_key=key,
        handle_value=value,
    )


def _cross_type_guard(
    path: Path, declared_type: str, *, dry_run_pages: DryRunPageOverlay | None = None
) -> Path | None:
    """Reject a target resolving to a page of a DIFFERENT type than declared
    (mirrors ``librarian.tier0_handle_upsert``'s same guard).

    Consults *dry_run_pages* FIRST (issue athenaeum#873): a dry-run create
    mints its page only in the overlay, never on disk, so a later record's
    cross-type check must read the notionally-written meta from there
    rather than a path that was never written.
    """
    overlaid = dry_run_pages.get(path) if dry_run_pages is not None else None
    if overlaid is not None:
        meta, _ = overlaid
    else:
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return None
    existing_type = str(meta.get("type", "") or "").strip()
    if existing_type and existing_type != declared_type:
        return None
    return path


# ---------------------------------------------------------------------------
# §6.2 precedence policy + §6.3 monotone suppression
# ---------------------------------------------------------------------------


def _existing_scalar_source(meta: dict[str, Any], field_name: str) -> Any:
    """Incumbent attribution for a SCALAR field: ``field_sources.<field>``,
    falling back to page-level ``source:``, then ``None`` (unsourced)."""
    fs = meta.get("field_sources")
    if isinstance(fs, dict) and field_name in fs:
        return fs[field_name]
    return meta.get("source")


def _value_key(value: Any) -> Any:
    """Value-identity key (§4): ``repr(value)`` for dicts, the value itself
    for scalars — matches ``dedupe._perform_merge``'s existing convention."""
    return repr(value) if isinstance(value, dict) else value


def _list_field_sources(meta: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    fs = meta.get("field_sources")
    if isinstance(fs, dict):
        entries = fs.get(field_name)
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
    return []


def _existing_list_source_for_value(meta: dict[str, Any], field_name: str, value: Any) -> Any:
    key = _value_key(value)
    for entry in _list_field_sources(meta, field_name):
        if _value_key(entry.get("value")) == key:
            return entry.get("source")
    return meta.get("source")


def _parse_observed_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_monotone_clear(value: Any, op: str) -> bool:
    """Whether this op represents "unsetting" a monotone flag (§6.3) rather
    than "setting" it. ``remove`` is always an unset; ``set`` is an unset
    only when the proposed value is falsy (``None``/``""``/``False``/``0``)
    — a monotone flag's "off" state."""
    if op == "remove":
        return True
    return value in (None, "", False, 0)


def decide_verdict(
    *,
    existing_source: Any,
    incoming_source: Any,
    existing_value: Any,
    incoming_value: Any,
    observed_at: str | None,
    monotone: bool,
    op: str,
    existing_updated: Any,
) -> tuple[str, str]:
    """§6.2 conflict policy for a single value comparison.

    Returns ``(verdict, reason)`` where ``verdict`` is one of ``"apply"``,
    ``"defer"``, ``"noop"``, ``"escalate"``. Callers pre-filter the "value
    already present/absent" idempotency cases for ``add``/``remove`` (§4);
    this function only decides the case where a real value delta exists.

    The "incumbent is ``user:`` and the correction is not → defer" row of
    §6.2's table needs no separate branch: ``user`` occupies precedence
    rank 1 alone, so any non-``user`` incoming source already has a HIGHER
    (worse) numeric rank and is deferred by the ordinary rank comparison
    below.
    """
    if existing_value == incoming_value:
        return "noop", "identical value (delta gate)"

    incoming_rank = source_rank(incoming_source)
    existing_rank = source_rank(existing_source)

    if monotone:
        if _is_monotone_clear(incoming_value, op):
            if incoming_rank == 1:
                return "apply", "monotone unset at user: tier"
            return "defer", "monotone unset requires user: tier"
        return "apply", "monotone set by any permitted writer"

    if incoming_rank < existing_rank:
        return "apply", f"incoming rank {incoming_rank} outranks existing rank {existing_rank}"
    if incoming_rank > existing_rank:
        return "defer", f"incoming rank {incoming_rank} outranked by existing rank {existing_rank}"

    # Equal rank, differing value: break on observed_at (§6.2's own
    # tie-break). No per-value observed_at is persisted (§4's per-value
    # shape is {value, source} only), so the page's own `updated:` stamp is
    # the closest available proxy for "when the incumbent was last touched"
    # — see the module docstring's decisions list.
    incoming_dt = _parse_observed_date(observed_at)
    existing_dt = _parse_observed_date(existing_updated)
    if incoming_dt is None or existing_dt is None:
        return "escalate", "equal rank, undated — reasoning must settle it"
    if incoming_dt > existing_dt:
        return "apply", "equal rank, newer observed_at wins"
    if incoming_dt < existing_dt:
        return "defer", "equal rank, existing is newer"
    return "escalate", "equal rank, indistinguishable dates"


# ---------------------------------------------------------------------------
# §7 routing helpers (sensitivity surface + schema-slot record shape)
# ---------------------------------------------------------------------------


def _read_surface_record(surface_root: Path, uid: str) -> tuple[Path, dict[str, Any], str]:
    """Resolve *uid*'s record on the sensitivity-routed surface (issue athenaeum#872).

    Reached through :func:`athenaeum.pii.resolve_contact_record_for_uid` — the
    SAME uid-keyed resolution :func:`athenaeum.pii.read_person` uses — rather
    than a bespoke ``{uid}.json`` path this router alone understood. That is
    what makes a value this function's caller writes visible to
    ``classify_contact_value``/``iter_contact_records``/``is_bounced`` by
    construction, on whichever record shape the CONFIGURED surface actually
    uses (markdown for the built-in excluded surface; whatever a storage
    adapter's own shape is otherwise — see the module docstring's routing
    section).

    Returns ``(path, meta, body)``:

    - an EXISTING ``.md`` record already carrying *uid*, parsed; or
    - a **read-through** of a legacy ``{uid}.json`` record — the shape this
      router minted before issue athenaeum#872 — when no ``.md`` record carries
      this uid but that file exists. Its content is returned as *meta* so a
      correction against a uid a pre-fix run already wrote merges onto that
      data rather than starting over; *path* is still the canonical
      ``{uid}.md`` location, so the very next write to this uid lands there,
      upgrading the record to the canonical shape with no separate migration
      script. The legacy file itself is left in place, untouched — read-through,
      not delete-on-read;
    - or a deterministic ``{uid}.md`` fallback path, with empty
      ``meta``/``body``, when neither exists — the mint case, mirroring
      :func:`athenaeum.pii.mark_bounced`'s own resolve-then-mint discipline
      (deterministic naming keeps a second correction for the same uid
      resolving to the record the first one just minted, rather than
      re-scanning for a filename convention).
    """
    from athenaeum import pii

    existing_path = pii.resolve_contact_record_for_uid(surface_root, uid)
    if existing_path is not None:
        meta, body = parse_frontmatter(existing_path.read_text(encoding="utf-8"))
        return existing_path, (meta if isinstance(meta, dict) else {}), body

    canonical_path = surface_root / f"{uid}.md"
    legacy_path = surface_root / f"{uid}.json"
    if legacy_path.exists():
        try:
            legacy_raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            legacy_raw = None
        if isinstance(legacy_raw, dict):
            return canonical_path, legacy_raw, ""

    return canonical_path, {}, ""


def _write_surface_record(path: Path, record: dict[str, Any], *, body: str = "") -> None:
    """Write *record* to *path* in the SAME markdown-frontmatter shape every
    other writer on the excluded surface uses (issue athenaeum#872) — never a
    parallel JSON shape only this router wrote."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, render_frontmatter(record) + "\n" + body)


def _apply_usage_classification(
    surface_root: Path,
    value: Any,
    *,
    usage_class: str,
    source: Any,
    observed_at: str,
) -> None:
    """Assert *usage_class* for *value* on its contact record, through
    :func:`athenaeum.pii.classify_contact_value` (issue athenaeum#872) — the SAME
    store-level no-downgrade rule athenaeum#866 introduced enforces this, not a
    second implementation of it here. A no-op when no record on
    *surface_root* lists *value* — never mints (mirrors
    ``classify_contact_value``'s own refusal to mint); the caller only invokes
    this once the value is already listed, whether by this correction's own
    write moments earlier or an already-present match.
    """
    from athenaeum import pii

    pii.classify_contact_value(
        surface_root,
        str(value),
        usage_class=usage_class,
        source=source,
        observed_at=observed_at,
    )


def _record_as_prose(
    entity_path: Path,
    existing_meta: dict[str, Any],
    existing_body: str,
    *,
    field_name: str,
    value: Any,
    source: Any,
    note: str,
) -> None:
    """§7.2 case 3: no schema slot, one-off — append the fact as body prose."""
    today = date.today().isoformat()
    lines = [
        f"- {today}: `{field_name}` = {value!r} (source: {source}"
        + (f"; {note}" if note else "")
        + ")"
    ]
    new_body = existing_body.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    merged_meta = dict(existing_meta)
    merged_meta["updated"] = today
    validate_wiki_meta(merged_meta)
    atomic_write_text(entity_path, render_frontmatter(merged_meta) + "\n" + new_body)


def _build_created_entity_meta(
    *,
    entity_type: str,
    handle_key: str,
    handle_value: str,
    source: Any,
    today: str,
) -> dict[str, Any]:
    """Build the frontmatter dict for a tier-0 create-by-handle (athenaeum#865,
    the `resolve_target_for_apply` "creatable" branch).

    Mints a fresh ``uid`` via :func:`athenaeum.models.generate_uid` — the
    same 8-hex-char-uuid4 scheme every other create path in this repo uses
    (``tiers.tier3_create`` et al.); the caller checks it against the live
    index before writing, matching those paths' existing (uncollided-in-
    practice) convention.

    §3.2's record shape carries no ``name`` key, so no name reaches this
    function — the handle value itself seeds a placeholder ``name``, the
    minimum the schema requires for the page to exist at all. A later
    correction whose ``field`` is an allowlisted name-bearing attribute
    overwrites it through the ordinary apply path, same as any other field.

    Carries the handle key/value in the shape ``docs/source-handles.md``
    §3 specifies — list-valued for a ``LIST_HANDLE_KEYS`` member, scalar
    otherwise — which is what lets a later submission resolve to this page
    instead of creating a second one (AC 3/AC 5). Provenance for the
    handle value, and a page-level ``source`` default for every other
    field the record goes on to set, both carry the batch's declared
    ``source`` verbatim — never a synthetic one (AC 5: "a human can see
    where it came from").
    """
    uid = generate_uid()
    meta: dict[str, Any] = {
        "uid": uid,
        "type": entity_type,
        "name": handle_value,
        "access": "internal",
        "created": today,
        "updated": today,
        "source": source,
    }
    if handle_key in LIST_HANDLE_KEYS:
        meta[handle_key] = [handle_value]
        meta["field_sources"] = {handle_key: [{"value": handle_value, "source": source}]}
    else:
        meta[handle_key] = handle_value
        meta["field_sources"] = {handle_key: source}
    return meta


# ---------------------------------------------------------------------------
# §4/§5/§6/§7 the tier-0 applier
# ---------------------------------------------------------------------------


@dataclass
class CorrectionRecordResult:
    """Outcome of processing one correction record."""

    correction_id: str
    disposition: str
    reason: str = ""
    target: Any = None
    op: str | None = None
    field: str | None = None
    value: Any = None
    source: Any = None
    observed_at: str | None = None
    note: str = ""
    entity_path: Path | None = None
    entity_name: str = ""
    monotone: bool = False
    unknown_keys: list[str] = dc_field(default_factory=list)


def process_correction_record(
    raw_record: dict[str, Any],
    envelope: dict[str, Any],
    *,
    index: EntityIndex,
    knowledge_root: Path,
    registry_entities: dict[str, Any],
    config: dict[str, Any] | None,
    dry_run: bool = False,
    dry_run_pages: DryRunPageOverlay | None = None,
) -> CorrectionRecordResult:
    """Process one correction record against a target entity — the tier-0
    applier. Never raises for a non-conformant record; every failure mode
    returns ``disposition="raised-tier"`` (§8) with a human-readable
    ``reason``, so the caller can hand it to the fallthrough path (§8.1)
    instead of dropping it.

    ``dry_run_pages`` (issue athenaeum#873) is the per-run dry-run page overlay
    — see :data:`DryRunPageOverlay`. Optional and defaulting to ``None`` so
    every existing caller (dry_run or not) that does not pass one gets
    exactly today's behaviour: a dry-run create still previews correctly for
    a SINGLE record, it just cannot resolve a later record in the same
    batch to the page this one notionally created.
    """
    schema_version = envelope.get("schema_version")
    submitter = envelope.get("submitter")
    defaults = envelope.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}

    unknown_keys = sorted(set(raw_record) - ALLOWED_RECORD_KEYS)
    effective = hoist_record(raw_record, defaults)

    target = effective.get("target")
    op = effective.get("op")
    field_name = effective.get("field")
    value = effective.get("value")
    source = effective.get("source")
    observed_at = effective.get("observed_at")
    note = str(effective.get("note") or "")

    correction_id = compute_correction_id(
        schema_version=schema_version,
        target=target,
        op=op,
        field_name=field_name,
        value=value,
    )

    def _raised(reason: str) -> CorrectionRecordResult:
        return CorrectionRecordResult(
            correction_id=correction_id,
            disposition="raised-tier",
            reason=reason,
            target=target,
            op=op,
            field=field_name,
            value=value,
            source=source,
            observed_at=observed_at,
            note=note,
            unknown_keys=unknown_keys,
        )

    if raw_record.get("record") != "correction":
        return _raised("record is not a valid correction record")
    if unknown_keys:
        return _raised(f"unknown key(s) on correction record: {unknown_keys}")
    if not isinstance(target, dict) or not target:
        return _raised("missing or malformed target")
    if op not in ("set", "add", "remove"):
        return _raised("missing or invalid op")
    if not isinstance(field_name, str) or not field_name.strip():
        return _raised("missing or invalid field")
    if "value" not in effective:
        return _raised("missing value")
    if not isinstance(observed_at, str) or not observed_at.strip():
        return _raised("missing observed_at")

    try:
        parsed_source = parse_source(source)
    except ValueError:
        # §8: NOT a fail-open downgrade to rank 9 -- the source is the
        # authorization to write, so an unparseable source must reason,
        # never silently default to the weakest rank.
        return _raised("unparseable source")
    if parsed_source is None:
        return _raised("missing source")

    fields_cfg = resolve_corrections_fields(config)
    field_def = fields_cfg.get(field_name)
    if not isinstance(field_def, dict):
        return _raised("attribute not on the allowlist")
    shape = field_def.get("shape")
    writers = field_def.get("writers")
    monotone = bool(field_def.get("monotone", False))
    if not isinstance(writers, list) or submitter not in writers:
        return _raised(f"writer {submitter!r} not permitted for field {field_name!r}")
    if shape == "scalar" and op != "set":
        return _raised(f"op {op!r} invalid for scalar attribute {field_name!r}")
    if shape == "list" and op not in ("add", "remove"):
        return _raised(f"op {op!r} invalid for list attribute {field_name!r}")
    if shape not in ("scalar", "list"):
        return _raised(f"unrecognized shape {shape!r} for attribute {field_name!r}")

    sensitive_map = resolve_corrections_sensitive_fields(config)
    surface_class = sensitive_map.get(field_name)

    # athenaeum#872: a contact-value correction may declare the usage class the
    # value it writes should carry (`athenaeum.pii.USAGE_CLASSES`) — never
    # defaulted, so an undeclared value stays `unclassified` (see the
    # sensitivity-routing write path below, which only ever calls
    # `pii.classify_contact_value` when this is set). Declaring one is only
    # meaningful for an ADD onto a contact-identifier field
    # (`athenaeum.pii.CONTACT_IDENTIFIER_FIELDS`) routed to an excluded
    # surface — `classify_contact_value` classifies a VALUE already listed on
    # a contact record, which is exactly what such an add is about to make
    # true. Validated here, before target resolution/creation, so a
    # malformed declaration is rejected before any side effect (including an
    # athenaeum#865 tier-0 create) happens.
    usage_class = effective.get("usage_class")
    if usage_class is not None:
        from athenaeum import pii

        if not isinstance(usage_class, str) or usage_class not in pii.USAGE_CLASSES:
            return _raised(
                f"invalid usage_class {usage_class!r}; expected one of "
                f"{list(pii.USAGE_CLASSES)}"
            )
        if not (surface_class and op == "add" and field_name in pii.CONTACT_IDENTIFIER_FIELDS):
            return _raised(
                "usage_class is only valid for an add correction on a "
                "contact-identifier field routed to an excluded surface "
                f"(field={field_name!r}, op={op!r})"
            )

    # Issue athenaeum#904 (AC2): optional decay annotations, same "validate before
    # any side effect" discipline as usage_class above. Unlike usage_class,
    # these are not restricted to a specific op/field — a rule may tag the
    # TARGET entity's page as a decay bucket regardless of which attribute
    # the correction itself is fixing.
    raw_bucket = effective.get("bucket")
    try:
        correction_bucket = coerce_bucket(raw_bucket)
    except ValueError as exc:
        return _raised(f"invalid bucket: {exc}")
    # `valid_until` is a SUGGESTION (design brief) — fail-open normalize
    # here, matching every other valid_until write path in this codebase
    # (`models._coerce_iso_date`), rather than reject-at-boundary like
    # `bucket`. A malformed value normalizes to "" (no suggestion), never
    # raises a tier.
    correction_valid_until = validity_bound_str(
        {"valid_until": effective.get("valid_until")}, "valid_until"
    )

    resolution = resolve_target_for_apply(
        target,
        index=index,
        registry_entities=registry_entities,
        dry_run_pages=dry_run_pages,
    )
    if resolution.kind == "unresolvable":
        return _raised("target resolves to zero or several entities")

    just_created = False
    if resolution.kind == "existing":
        entity_path = resolution.path
        assert entity_path is not None
        # issue athenaeum#873: consult the dry-run overlay FIRST. A dry-run
        # create earlier in this same run (see the "creatable" branch below)
        # mints its page only in-memory — there is nothing on disk to read,
        # and there never will be for a dry run. Serving the overlay content
        # here instead of touching the filesystem is what lets a later
        # record in the same dry-run batch resolve to the SAME notionally-
        # created entity instead of falling through to "target page
        # unreadable" and raising a tier.
        overlaid = dry_run_pages.get(entity_path) if dry_run_pages is not None else None
        if overlaid is not None:
            existing_meta, existing_body = overlaid
        else:
            try:
                existing_text = entity_path.read_text(encoding="utf-8")
            except OSError:
                return _raised("target page unreadable")
            existing_meta, existing_body = parse_frontmatter(existing_text)
            if not existing_meta:
                return _raised("target page has no frontmatter")
    else:  # "creatable" — athenaeum#865
        # Mint the page now so the ordinary apply path below (schema-slot
        # routing, sensitivity routing, the §5.1 delta gate) runs for a
        # create exactly as it does for an update — one path, which is
        # what makes re-submission idempotent and lets a later batch
        # update what this one created (AC 2/AC 3).
        assert resolution.entity_type is not None
        assert resolution.handle_key is not None
        assert resolution.handle_value is not None

        # Issue athenaeum#971: gate the create branch's declared ``type`` the
        # same way the two other deterministic (non-LLM) create/upsert paths
        # already do — ``intake.py``'s tier0_passthrough eligibility check
        # and ``librarian.py``'s tier0_handle_upsert (both: unrecognized type
        # -> reject, i.e. this record is not eligible here and must fall
        # through to a higher tier). This is the closer precedent than
        # ``tiers.py``'s post-LLM tier-2 classify path, which COERCES an
        # unrecognized ``entity_type`` to ``"reference"`` -- that coercion is
        # safe there because tier-2 is the last stop for an already
        # LLM-judged entity. Here ``resolution.entity_type`` is an externally
        # declared string with zero LLM judgment in between (the same shape
        # as a raw frontmatter ``type:``), AND coercing would misfile a athenaeum#970
        # fold (e.g. a stale writer still declaring ``type: user``) into the
        # wrong bucket ("reference") instead of preserving it for correct
        # reclassification. Reject-and-escalate (this module's own idiom for
        # "not eligible here", per the module docstring's "every failure to
        # conform is a fallthrough to a higher tier, never a rejection") is
        # the matching semantics, not a fourth variant.
        schema_path = index.wiki_root / "_schema"
        valid_types = load_schema_list(schema_path, "types.md") or sorted(KNOWN_TYPES)
        if resolution.entity_type not in valid_types:
            return _raised(
                f"unrecognized entity type on create: {resolution.entity_type!r}"
            )

        today = date.today().isoformat()
        created_meta = _build_created_entity_meta(
            entity_type=resolution.entity_type,
            handle_key=resolution.handle_key,
            handle_value=resolution.handle_value,
            source=source,
            today=today,
        )
        try:
            validate_wiki_meta(created_meta)
        except PydanticValidationError as exc:
            return _raised(
                f"create violates schema for type {resolution.entity_type!r}: {exc}"
            )

        uid = str(created_meta["uid"])
        entity_name0 = str(created_meta["name"])
        entity_path = index.wiki_root / f"{uid}-{slugify(entity_name0)}.md"
        if entity_path.exists() or index.get_by_uid(uid) is not None:
            # A freshly-minted uid colliding with something already on
            # disk/in the index — vanishingly unlikely (uuid4), but never
            # overwrite an existing page under a uid this run just minted.
            return _raised("uid collision constructing new entity")

        if dry_run:
            # issue athenaeum#873: never write to disk (dry run's core
            # invariant) — but the page still needs to be READABLE by a
            # later record in this same batch, without touching disk. Stash
            # it in the per-run overlay instead of writing it.
            existing_meta, existing_body = created_meta, ""
            if dry_run_pages is not None:
                dry_run_pages[entity_path] = (existing_meta, existing_body)
        else:
            atomic_write_text(entity_path, render_frontmatter(created_meta) + "\n")
            # Re-parse the just-written bytes, same discipline every other
            # create path in this repo follows (batch.py/librarian.py):
            # downstream code sees exactly the on-disk bytes, not the
            # in-memory dict that produced them.
            existing_meta, existing_body = parse_frontmatter(
                entity_path.read_text(encoding="utf-8")
            )

        # Keep the in-run registry AND index views current for BOTH the
        # real write and a dry-run preview (issues athenaeum#865 / athenaeum#873): the
        # snapshot loaded once at the top of a run must reflect a page
        # created earlier in that SAME run — real or notional — or a later
        # record keyed on the same handle would not see it and would create
        # a second page/uid from the one batch. registry.json on disk is a
        # separately-compiled artifact (`athenaeum registry`) and is
        # deliberately NOT rewritten here regardless of dry_run — see the
        # athenaeum#865 completion report for why.
        index.register(
            WikiEntity(
                uid=uid,
                type=resolution.entity_type,
                name=entity_name0,
                created=today,
                updated=today,
                source=source,
            )
        )
        registry_entities[uid] = {
            "type": resolution.entity_type,
            "name": entity_name0,
            "handles": {resolution.handle_key: created_meta[resolution.handle_key]},
        }
        just_created = True

    entity_name = str(existing_meta.get("name", "") or entity_path.stem)

    def _result(
        disposition: str, reason: str, *, monotone_apply: bool = False
    ) -> CorrectionRecordResult:
        return CorrectionRecordResult(
            correction_id=correction_id,
            disposition=disposition,
            reason=reason,
            target=target,
            op=op,
            field=field_name,
            value=value,
            source=source,
            observed_at=observed_at,
            note=note,
            entity_path=entity_path,
            entity_name=entity_name,
            monotone=monotone_apply,
        )

    # §7.2 schema evolution -- decided before the conflict/write step since
    # it may redirect the write to an alias field or short-circuit entirely.
    schema_slots = resolve_corrections_schema_slots(config)
    slot = schema_slots.get(field_name)
    write_field = field_name
    if isinstance(slot, dict):
        alias = slot.get("alias_of")
        if isinstance(alias, str) and alias.strip():
            write_field = alias.strip()
        elif slot.get("propose_amendment"):
            return _result(
                "held-schema-proposal",
                f"no schema slot for {field_name!r}; schema-amendment proposal recorded",
            )
        elif slot.get("prose"):
            if not dry_run:
                _record_as_prose(
                    entity_path,
                    existing_meta,
                    existing_body,
                    field_name=field_name,
                    value=value,
                    source=source,
                    note=note,
                )
            return _result(
                "recorded-as-prose", f"no schema slot for {field_name!r}; recorded as prose"
            )

    # §7.1 sensitivity routing -- redirects BOTH the read of "existing
    # attribution" and the eventual write to the mapped surface, regardless
    # of the destination the correction named. ``surface_class`` was already
    # resolved above (needed there to validate ``usage_class``); reused here
    # rather than re-resolved.
    surface_path: Path | None = None
    surface_body = ""
    surface_root: Path | None = None
    if surface_class:
        uid = str(existing_meta.get("uid", "") or entity_path.stem)
        surface_root = surface_root_for_class(surface_class, config, knowledge_root)
        surface_path, surface_meta, surface_body = _read_surface_record(surface_root, uid)
        read_meta = dict(surface_meta)
        read_meta.setdefault("uid", uid)
    else:
        read_meta = existing_meta

    if shape == "scalar":
        existing_value = read_meta.get(write_field)
        existing_source = _existing_scalar_source(read_meta, write_field)
        if existing_value is None:
            # §6.2 decides between an incoming claim and an INCUMBENT one; its
            # every row compares two competing values. An absent field has no
            # incumbent, so there is nothing to conflict with and the policy
            # does not apply — the same reading §4 already gives the list path,
            # where `op: add` of a value not yet present applies outright
            # ("new value, not a conflict") without consulting rank at all.
            #
            # The bug this fixes: §6.2's incumbent-attribution fallback chain
            # (`field_sources.<field>` -> page-level `source:` -> unsourced) is
            # the attribution OF THE INCUMBENT VALUE, but the scalar branch
            # consulted it unconditionally. With no incumbent value the chain
            # still yielded the page-level `source:`, manufacturing a phantom
            # incumbent out of the page's own provenance.
            #
            # athenaeum#865 is what surfaced it. A page created by the tier-0
            # create path carries `source: <the submitter>` and
            # `updated: <today>`, so a second record filling any other field
            # tied on rank against its own batch's source and then lost the
            # observed_at tie-break to a today-stamp that every real-world
            # `observed_at` predates — a batch losing to a page it had itself
            # created moments earlier, which is what made AC 3 (create and
            # update are one path) unreachable.
            #
            # This narrows when precedence is consulted; it does not reorder
            # it. A genuine value conflict still takes exactly the §6.2 path it
            # always did, including "incumbent is `user:` -> defer, always" —
            # filling a field no one has ever set is not overwriting a
            # human-stated value, and §6.3's `writers` allowlist still bounds
            # which attributes a given submitter may touch at all.
            verdict, reason = "apply", "no incumbent value for this field; not a conflict (§4)"
        else:
            verdict, reason = decide_verdict(
                existing_source=existing_source,
                incoming_source=source,
                existing_value=existing_value,
                incoming_value=value,
                observed_at=observed_at,
                monotone=monotone,
                op=op,
                existing_updated=existing_meta.get("updated"),
            )
    elif op == "add":
        existing_list = read_meta.get(write_field)
        already_present = isinstance(existing_list, list) and any(
            _value_key(v) == _value_key(value) for v in existing_list
        )
        verdict, reason = (
            ("noop", "value already present (delta gate)")
            if already_present
            else ("apply", "new value, not a conflict (§4)")
        )
    else:  # op == "remove"
        existing_list = read_meta.get(write_field)
        present = isinstance(existing_list, list) and any(
            _value_key(v) == _value_key(value) for v in existing_list
        )
        if not present:
            verdict, reason = "noop", "value already absent (idempotent)"
        else:
            existing_source = _existing_list_source_for_value(read_meta, write_field, value)
            incoming_rank = source_rank(source)
            existing_rank = source_rank(existing_source)
            if monotone:
                if incoming_rank == 1:
                    verdict, reason = "apply", "monotone unset at user: tier"
                else:
                    verdict, reason = "defer", "monotone unset requires user: tier"
            elif incoming_rank <= existing_rank:
                verdict, reason = "apply", "incoming authority permits removal"
            else:
                verdict, reason = (
                    "defer",
                    f"incoming rank {incoming_rank} outranked by existing rank {existing_rank}",
                )

    if just_created and verdict == "noop":
        # The record's own field/value happens to equal what the create
        # step already wrote (the common case: the handle key IS the
        # field being asserted, e.g. field="domains" op="add"
        # value=<the same domain the target keyed on>). The per-field
        # delta is genuinely zero, but the ENTITY was just created — that
        # is not "nothing happened" (§5.1's delta gate is about an
        # UNCHANGED page, and this page did not exist a moment ago), so
        # reporting "noop" here would be factually wrong. The base create
        # write above already carries this field's correct value, so
        # return directly rather than falling into the "apply" write
        # branch below — that branch assumes "apply" means "not already
        # present" (list ops append unconditionally) and would duplicate
        # the value/field_sources entry the create step just wrote. This
        # is the only place athenaeum#865 touches decide_verdict's
        # verdict — a create-vs-update bookkeeping correction, not a
        # §6.2 precedence change.
        return _result("applied", "entity created; field already matches the handle-derived value")

    if verdict == "noop":
        if usage_class is not None and not dry_run:
            # athenaeum#872: the field-value delta gate above found *value*
            # already present, so nothing about the field itself changes —
            # but a declared usage_class may still upgrade (or attempt to
            # downgrade) the classification already recorded for it. The
            # address is, by construction of "already present", already
            # listed on the surface record, so `classify_contact_value`
            # resolves it without this correction writing anything first.
            # `_apply_usage_classification` is the only place that rule
            # runs — a downgrade attempt is refused there, at the store
            # level (issue athenaeum#866), never here.
            assert surface_root is not None
            _apply_usage_classification(
                surface_root,
                value,
                usage_class=usage_class,
                source=source,
                observed_at=observed_at,
            )
        return _result("noop", reason)
    if verdict == "defer":
        return _result("deferred-lower-precedence", reason)
    if verdict == "escalate":
        return _result("escalated", reason)

    # verdict == "apply"
    monotone_apply = monotone and verdict == "apply"
    if shape == "scalar":
        merged_read = dict(read_meta)
        merged_read[write_field] = value
        fs = dict(merged_read.get("field_sources") or {})
        fs[write_field] = source
        merged_read["field_sources"] = fs
    elif op == "add":
        merged_list = list(read_meta.get(write_field) or [])
        merged_list.append(value)
        merged_read = dict(read_meta)
        merged_read[write_field] = merged_list
        fs = dict(merged_read.get("field_sources") or {})
        fs_list = [e for e in (fs.get(write_field) or []) if isinstance(e, dict)]
        fs_list.append({"value": value, "source": source})
        fs[write_field] = fs_list
        merged_read["field_sources"] = fs
    else:  # remove
        existing_list = list(read_meta.get(write_field) or [])
        merged_list = [v for v in existing_list if _value_key(v) != _value_key(value)]
        merged_read = dict(read_meta)
        merged_read[write_field] = merged_list
        fs = dict(merged_read.get("field_sources") or {})
        fs_list = [
            e
            for e in (fs.get(write_field) or [])
            if isinstance(e, dict) and _value_key(e.get("value")) != _value_key(value)
        ]
        if fs_list:
            fs[write_field] = fs_list
        else:
            fs.pop(write_field, None)
        merged_read["field_sources"] = fs

    if dry_run:
        disposition = "routed-elsewhere" if surface_class else "applied"
        return _result(disposition, reason, monotone_apply=monotone_apply)

    if surface_class:
        assert surface_path is not None
        _write_surface_record(surface_path, merged_read, body=surface_body)
        if usage_class is not None:
            # athenaeum#872: the value is now listed on the surface record (the
            # write just above), so `classify_contact_value` can resolve it —
            # calling this any earlier would find no record to classify yet.
            assert surface_root is not None
            _apply_usage_classification(
                surface_root,
                value,
                usage_class=usage_class,
                source=source,
                observed_at=observed_at,
            )
        if monotone_apply:
            log.info(
                "corrections: monotone apply (routed) — field=%s target=%s source=%s",
                field_name,
                entity_name,
                source,
            )
        return _result("routed-elsewhere", reason, monotone_apply=monotone_apply)

    # Issue athenaeum#904 (AC2): stamp the target ENTITY page's decay bucket /
    # suggested valid_until, ordinary (non-excluded-surface) wiki pages only
    # — bucket/valid_until are wiki decay concepts, not contact-surface ones,
    # so the `surface_class` branch above never reaches here.
    #
    # `bucket` is a direct SET: an explicit correction naming a bucket is a
    # definitive classification decision, not a competing claim to be
    # weighed under §6.2 precedence (the field/value payload already went
    # through that ladder above; bucket rides alongside it).
    #
    # `valid_until` is a SUGGESTION (design brief) — only fills an ABSENT
    # bound, never overrides a `valid_until` the page already carries
    # (mirroring `_stamp_member_validity`'s "only-fill-never-override" rule
    # in `merge.py` for the analogous per-source case).
    if correction_bucket:
        merged_read["bucket"] = correction_bucket
    if correction_valid_until and not merged_read.get("valid_until"):
        merged_read["valid_until"] = correction_valid_until

    merged_read["updated"] = date.today().isoformat()
    validate_wiki_meta(merged_read)
    atomic_write_text(
        entity_path, render_frontmatter(merged_read) + "\n" + existing_body
    )
    if monotone_apply:
        log.info(
            "corrections: monotone apply — field=%s target=%s source=%s",
            field_name,
            entity_name,
            source,
        )
    return _result("applied", reason, monotone_apply=monotone_apply)


# ---------------------------------------------------------------------------
# §5.3/§5.4/§10.2 batch-level orchestration
# ---------------------------------------------------------------------------

#: Sources skipped by the batch-candidate walk below, mirroring
#: ``intake.discover_raw_files``'s ``answers`` exclusion (issue athenaeum#414) —
#: resolution OUTPUT, never a place a correction batch would legitimately
#: land.
_SKIPPED_SOURCES: frozenset[str] = frozenset({"answers"})


@dataclass
class BatchOutcome:
    """Outcome of processing one correction-batch file."""

    path: Path
    source: str
    envelope: dict[str, Any]
    records_total: int
    results: list[CorrectionRecordResult]
    carried_over: bool = False
    carry_over_reason: str = ""

    @property
    def batch_id(self) -> str:
        return str(self.envelope.get("batch_id", ""))

    @property
    def submitter(self) -> str:
        return str(self.envelope.get("submitter", ""))

    def all_terminal(self, *, escalations_recorded: bool = True) -> bool:
        """§5.4: a batch is retired once EVERY record reaches a terminal
        disposition. ``escalated`` counts as terminal only once the
        question is actually RECORDED in `_pending_questions.md` (§5.4's
        own distinction) — the caller passes ``escalations_recorded=False``
        when the §10.2 rate cap (or a dedup hit that still needs a NEXT-run
        retry, which does not apply here since dedup means it's already
        open) prevented that this pass, so the batch correctly carries over
        instead of being retired with an un-filed question."""
        if not escalations_recorded:
            return False
        return all(r.disposition in _FIRST_PASS_TERMINAL for r in self.results)


def find_correction_batches(raw_root: Path) -> list[tuple[Path, str, dict[str, Any]]]:
    """Walk ``raw/<source>/`` for `.jsonl` files whose first line is a valid
    batch envelope (§3.1) — the mirror image of `intake.discover_raw_files`'s
    skip: what that function excludes, this collects.

    This is NOT a second discovery walk in the sense §3.1 forbids (a
    reserved subtree invisible to ordinary discovery) — it walks the SAME
    ordinary `raw/<source>/` tree discovery already covers, filtering for
    the complementary shape. Returns ``(path, source, envelope)`` tuples
    sorted by filename so batches apply FIFO across submitters (§10.2).
    """
    candidates: list[tuple[Path, str, dict[str, Any]]] = []
    if not raw_root.exists():
        return candidates
    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir() or source_dir.name in _SKIPPED_SOURCES:
            continue
        for fpath in sorted(source_dir.glob("*.jsonl")):
            try:
                with fpath.open("r", encoding="utf-8") as fh:
                    first_line = fh.readline()
            except (OSError, UnicodeDecodeError):
                continue
            envelope = parse_batch_envelope(first_line)
            if envelope is not None:
                candidates.append((fpath, source_dir.name, envelope))
    candidates.sort(key=lambda c: c[0].name)
    return candidates


def _line_correction_id(line: str) -> str:
    """Fallback id for a batch line that is not even parseable JSON —
    hashed on raw text so the same malformed line always gets the same id
    (still useful for the audit ledger's per-record id list)."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def process_batch_file(
    path: Path,
    envelope: dict[str, Any],
    source: str,
    *,
    index: EntityIndex,
    knowledge_root: Path,
    config: dict[str, Any] | None,
    dry_run: bool = False,
    registry_entities: dict[str, Any] | None = None,
    dry_run_pages: DryRunPageOverlay | None = None,
) -> BatchOutcome:
    """Process every correction record in one batch file.

    A line that is not even parseable JSON, or parses to something other
    than a ``dict``, is NOT dropped — it gets a synthetic ``raised-tier``
    result (its raw text carried as the ``note``) so it reaches the §8.1
    handoff exactly like any other non-conformant record. This is the
    "nothing is rejected" rule applied one level below individual field
    validation.

    ``dry_run_pages`` (issue athenaeum#873) follows the same "caller may share
    one across several calls, or let this function default one" convention
    ``registry_entities`` already uses — a caller processing several batch
    files in one run (`run_correction_phase`) passes ONE overlay shared
    across all of them; a caller processing a single file on its own (most
    tests) gets a fresh, file-scoped one built here.
    """
    if registry_entities is None:
        registry_entities = load_registry(knowledge_root)
    if dry_run_pages is None:
        dry_run_pages = {}

    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()

    record_lines = [ln for ln in lines[1:] if ln.strip()]
    results: list[CorrectionRecordResult] = []
    for raw_line in record_lines:
        try:
            raw_record = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            results.append(
                CorrectionRecordResult(
                    correction_id=_line_correction_id(raw_line),
                    disposition="raised-tier",
                    reason="line is not valid JSON",
                    note=raw_line.strip()[:500],
                )
            )
            continue
        if not isinstance(raw_record, dict):
            results.append(
                CorrectionRecordResult(
                    correction_id=_line_correction_id(raw_line),
                    disposition="raised-tier",
                    reason="line is not a JSON object",
                    note=raw_line.strip()[:500],
                )
            )
            continue
        results.append(
            process_correction_record(
                raw_record,
                envelope,
                index=index,
                knowledge_root=knowledge_root,
                registry_entities=registry_entities,
                config=config,
                dry_run=dry_run,
                dry_run_pages=dry_run_pages,
            )
        )

    return BatchOutcome(
        path=path,
        source=source,
        envelope=envelope,
        records_total=len(record_lines),
        results=results,
    )


# ---------------------------------------------------------------------------
# §8.1 per-record fallthrough handoff
# ---------------------------------------------------------------------------


def write_correction_handoff(
    outcome: BatchOutcome,
    raised: list[CorrectionRecordResult],
    *,
    raw_root: Path,
) -> Path:
    """§8.1: for a batch with at least one raised record, write ONE ordinary
    raw-intake `.md` file in the same `raw/<source>/` directory stating each
    raised record as a plain claim, carrying its ``note`` and the reason it
    was raised, and — critically — its ORIGINAL ``source`` in
    ``field_sources`` (provenance-preserving: the handoff is a transport
    step, never a new assertion under the handoff's own name).

    Idempotency (keyed on batch_id + sorted correction_id set) is the
    CALLER's responsibility via the audit ledger (§5.3) — this function
    always writes; callers check the ledger before calling it.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uuid8 = uuid.uuid4().hex[:8]
    target_dir = raw_root / outcome.source
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{timestamp}-{uuid8}.md"

    lines = [
        f"Field corrections raised from batch `{outcome.batch_id}` "
        f"(submitter: `{outcome.submitter}`) that could not be applied at "
        "tier 0 and need reasoning:",
        "",
    ]
    field_sources: dict[str, Any] = {}
    for r in raised:
        target_desc = json.dumps(r.target, sort_keys=True) if r.target else "?"
        claim = (
            f"- target={target_desc} field={r.field!r} op={r.op!r} "
            f"value={r.value!r} — reason: {r.reason}"
        )
        if r.note:
            claim += f" (note: {r.note})"
        lines.append(claim)
        if r.field and r.source is not None:
            field_sources[str(r.field)] = r.source

    body = "\n".join(lines) + "\n"
    meta_lines = ["---", f"handoff_batch_id: {outcome.batch_id}", "field_sources:"]
    for k, v in field_sources.items():
        meta_lines.append(f"  {k}: {v!r}")
    meta_lines.append("---")
    content = "\n".join(meta_lines) + "\n\n" + body
    atomic_write_text(out_path, content)
    return out_path


# ---------------------------------------------------------------------------
# §5.3 audit ledger
# ---------------------------------------------------------------------------

CORRECTIONS_LEDGER_FILENAME = "_corrections_applied.jsonl"


def default_corrections_ledger_path(wiki_root: Path) -> Path:
    return wiki_root / CORRECTIONS_LEDGER_FILENAME


def _append_jsonl_line(path: Path, line: str) -> None:
    """Same append-only-JSONL discipline as
    ``provenance._append_jsonl_line`` / ``spend._append_line``: a single
    small ``O_APPEND`` write is atomic on local filesystems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def build_ledger_record(outcome: BatchOutcome) -> dict[str, Any]:
    """Build one `_corrections_applied.jsonl` line (§5.3).

    Records ``records_total`` and asserts (raises ``AssertionError`` —
    caller's job to fail the run loudly, per §5.3: "failing loudly on a
    mismatch") that the dispositions counted actually sum to it. Also
    records per-record ids for everything that was NOT ``applied``/``noop``.
    """
    counts: dict[str, int] = {}
    non_trivial_ids: list[str] = []
    raised_tier_ids: list[str] = []
    for r in outcome.results:
        counts[r.disposition] = counts.get(r.disposition, 0) + 1
        if r.disposition not in ("applied", "noop"):
            non_trivial_ids.append(r.correction_id)
        if r.disposition == "raised-tier":
            raised_tier_ids.append(r.correction_id)
    total_counted = sum(counts.values())
    if total_counted != outcome.records_total:
        raise AssertionError(
            f"corrections ledger denominator mismatch for batch "
            f"{outcome.batch_id!r}: records_total={outcome.records_total} but "
            f"dispositions summed to {total_counted} — a record was seen but "
            f"never dispositioned (§5.3)"
        )
    return {
        "batch_id": outcome.batch_id,
        "submitter": outcome.submitter,
        "source": outcome.source,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "records_total": outcome.records_total,
        "dispositions": counts,
        "non_trivial_correction_ids": non_trivial_ids,
        # §8.1 idempotency key: which raised-tier ids from THIS pass got (or
        # will get, once the caller writes it) a handoff file. A later pass
        # over the same batch_id (carried over for an unrelated reason, e.g.
        # the §10.2 escalation cap) scans prior lines for this batch_id and
        # skips re-emitting a handoff for ids already listed here.
        "raised_tier_correction_ids": raised_tier_ids,
    }


def append_corrections_ledger(wiki_root: Path, outcome: BatchOutcome) -> None:
    record = build_ledger_record(outcome)
    path = default_corrections_ledger_path(wiki_root)
    _append_jsonl_line(path, json.dumps(record, sort_keys=True) + "\n")


def previously_handed_off_correction_ids(wiki_root: Path, batch_id: str) -> set[str]:
    """§8.1 idempotency: correction_ids from *batch_id* that already got a
    handoff file written on an earlier pass, recovered from prior
    ``_corrections_applied.jsonl`` lines for this batch (their
    ``raised_tier_correction_ids``).

    A batch can be carried over (§5.4) for a reason unrelated to its
    raised-tier records — e.g. the §10.2 escalation rate cap holding an
    ``escalated`` record open — in which case the SAME raised-tier records
    are recomputed on the next pass. Without this check the caller would
    call :func:`write_correction_handoff` again for ids it already handed
    off, writing a duplicate `.md` file every carried-over run.
    """
    path = default_corrections_ledger_path(wiki_root)
    ids: set[str] = set()
    if not path.exists():
        return ids
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("batch_id") == batch_id:
                    ids.update(rec.get("raised_tier_correction_ids") or [])
    except OSError:
        return ids
    return ids


# ---------------------------------------------------------------------------
# §5.4 batch retirement
# ---------------------------------------------------------------------------


def _git(knowledge_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(knowledge_root),
        capture_output=True,
        text=True,
        check=False,
    )


def retire_batch(knowledge_root: Path, batch_path: Path) -> bool:
    """§5.4: once every record in a batch is terminal, retire it — a
    ``git rm`` after a provenance-snapshot commit, recoverable from git
    history, never hard-deleted (mirrors ``adapter-contract.md`` §4.5 /
    ``retire.py``'s exact two-commit pattern).

    Best-effort: when *knowledge_root* is not a git repository (a bare
    filesystem test fixture), falls back to a plain ``unlink`` — the batch
    is still removed from ``raw/`` (which is what actually prevents
    re-processing; retirement's git-recoverability is a nice-to-have on top
    of that, not the mechanism that stops the re-read). Returns ``True`` on
    success.
    """
    if not batch_path.exists():
        return True
    if (knowledge_root / ".git").is_dir():
        try:
            rel = str(batch_path.resolve().relative_to(knowledge_root.resolve()))
        except ValueError:
            rel = str(batch_path)
        _git(knowledge_root, "add", "--", rel)
        staged = _git(knowledge_root, "diff", "--cached", "--quiet")
        if staged.returncode != 0:
            _git(
                knowledge_root,
                "commit",
                "-m",
                f"librarian: field-correction batch provenance snapshot ({rel})",
            )
        rm_result = _git(knowledge_root, "rm", "--quiet", "-f", "--", rel)
        if rm_result.returncode == 0:
            _git(
                knowledge_root,
                "commit",
                "-m",
                f"librarian: field-correction batch retired ({rel})",
            )
            return True
        # git rm failed for some reason (e.g. not actually tracked) -- fall
        # through to the plain-unlink fallback below rather than leaving the
        # batch stuck forever.
    try:
        batch_path.unlink()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# §8/§10.2 escalation dedup helper
# ---------------------------------------------------------------------------

_CORRECTION_ID_MARKER = "Correction ID:"


def render_correction_id_marker(correction_id: str) -> str:
    """Embed a correction_id inside an escalation description in a shape
    :func:`open_correction_ids` can recover — deliberately NOT a
    ``**Key**:``-style tag (those are reserved by ``answers._parse_block``
    for ``Conflict type``/``Description``/``Also affects``/``Fingerprint``;
    inventing a new one risks the parser mis-classifying it)."""
    return f"{_CORRECTION_ID_MARKER} {correction_id}"


def open_correction_ids(pending_path: Path) -> set[str]:
    """§8/§10.2: correction_ids already escalated and still OPEN (unanswered)
    in `_pending_questions.md`, so a carried-over batch (or a later run)
    cannot double-file the same question."""
    from athenaeum.answers import parse_pending_questions

    ids: set[str] = set()
    for pq in parse_pending_questions(pending_path):
        if pq.answered:
            continue
        for line in pq.description.splitlines():
            stripped = line.strip()
            if stripped.startswith(_CORRECTION_ID_MARKER):
                ids.add(stripped.removeprefix(_CORRECTION_ID_MARKER).strip())
    return ids


# ---------------------------------------------------------------------------
# §10.1 phase orchestration — called from ``librarian._run_correction_phase``
# ---------------------------------------------------------------------------


def run_correction_phase(
    *,
    raw_root: Path,
    wiki_root: Path,
    knowledge_root: Path,
    index: EntityIndex,
    config: dict[str, Any] | None,
    escalate_one: Callable[[CorrectionRecordResult, BatchOutcome], bool],
    deadline_check: Callable[[], bool] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """§10.1 orchestration: find candidate batches, process each respecting
    the §10.2 volume bounds and a BATCH-BOUNDARY-ONLY deadline check, then
    ledger/handoff/retire. Deliberately makes zero LLM calls and touches no
    ``TokenUsage`` — every write here is mechanical (frontmatter merge,
    JSONL append, git). The caller supplies ``escalate_one`` because the
    actual `_pending_questions.md` write goes through
    ``tiers.tier4_escalate``, which this module must not import (layering,
    see the module docstring) — the caller (which DOES sit above `tiers`)
    owns the §10.2 rate cap and the correction_id dedup
    (:func:`open_correction_ids`) and returns whether the escalation was
    actually RECORDED this pass.

    Returns a summary dict: ``batches_processed``, ``batches_carried_over``,
    ``dispositions`` (disposition -> count, across every processed batch),
    ``records_total``.
    """
    max_batch_bytes = resolve_corrections_max_batch_bytes(config)
    max_records_per_batch = resolve_corrections_max_records_per_batch(config)
    max_records_per_run = resolve_corrections_max_records_per_run(config)

    summary: dict[str, Any] = {
        "batches_processed": 0,
        "batches_carried_over": 0,
        "dispositions": {},
        "records_total": 0,
    }
    registry_entities = load_registry(knowledge_root)
    # issue athenaeum#873: shared across every batch file this run processes,
    # same lifetime as `registry_entities` above — a dry-run create in one
    # batch file must be readable by a later record even if that record
    # lands in a DIFFERENT batch file processed later in this same run.
    dry_run_pages: DryRunPageOverlay = {}
    applied_this_run = 0

    for path, source, envelope in find_correction_batches(raw_root):
        if deadline_check is not None and deadline_check():
            # §10.1: deadline checked at BATCH boundaries only -- never
            # mid-batch. Every remaining candidate (including this one) is
            # untouched and naturally retried next run.
            summary["batches_carried_over"] += 1
            continue

        if applied_this_run >= max_records_per_run:
            summary["batches_carried_over"] += 1
            continue

        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8") as fh:
                n_records = sum(1 for i, ln in enumerate(fh) if i > 0 and ln.strip())
        except OSError:
            summary["batches_carried_over"] += 1
            continue

        if size > max_batch_bytes or n_records > max_records_per_batch:
            # §10.2: an over-bound batch is deferred WHOLE, never refused --
            # untouched, retried next run.
            summary["batches_carried_over"] += 1
            continue

        outcome = process_batch_file(
            path,
            envelope,
            source,
            index=index,
            knowledge_root=knowledge_root,
            config=config,
            dry_run=dry_run,
            registry_entities=registry_entities,
            dry_run_pages=dry_run_pages,
        )
        applied_this_run += sum(
            1 for r in outcome.results if r.disposition in ("applied", "routed-elsewhere")
        )

        # §7.2/§5.4: ``held-schema-proposal`` is terminal only once the
        # proposal is actually RECORDED on the human-decision surface, same
        # as ``escalated`` (§8's own text: "the question or proposal is
        # recorded... the pending-questions surface owns it from that
        # point"). Both go through the same ``escalate_one`` callback and
        # the same §10.2 rate cap / correction_id dedup.
        escalations_recorded = True
        if not dry_run:
            for r in outcome.results:
                if r.disposition in ("escalated", "held-schema-proposal"):
                    if not escalate_one(r, outcome):
                        escalations_recorded = False

        for k, v in build_ledger_record(outcome)["dispositions"].items():
            summary["dispositions"][k] = summary["dispositions"].get(k, 0) + v
        summary["records_total"] += outcome.records_total
        summary["batches_processed"] += 1

        if dry_run:
            continue

        raised = [r for r in outcome.results if r.disposition == "raised-tier"]
        if raised:
            # §8.1 idempotency: only hand off ids not already recorded as
            # handed-off in a prior pass over this same batch_id (a batch
            # carried over for an unrelated reason -- e.g. the escalation
            # cap above -- must not re-emit a duplicate handoff file).
            already_handed_off = previously_handed_off_correction_ids(
                wiki_root, outcome.batch_id
            )
            new_raised = [r for r in raised if r.correction_id not in already_handed_off]
            if new_raised:
                write_correction_handoff(outcome, new_raised, raw_root=raw_root)

        append_corrections_ledger(wiki_root, outcome)

        if outcome.all_terminal(escalations_recorded=escalations_recorded):
            retire_batch(knowledge_root, path)
        else:
            summary["batches_carried_over"] += 1

    return summary
