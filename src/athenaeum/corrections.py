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
:mod:`athenaeum.storage`. Must never import :mod:`athenaeum.intake`,
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

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_corrections_fields,
    resolve_corrections_max_batch_bytes,
    resolve_corrections_max_records_per_batch,
    resolve_corrections_max_records_per_run,
    resolve_corrections_schema_slots,
    resolve_corrections_sensitive_fields,
)
from athenaeum.models import EntityIndex, parse_frontmatter, render_frontmatter
from athenaeum.precedence import source_rank
from athenaeum.provenance import parse_source
from athenaeum.registry import SOURCE_HANDLE_KEYS
from athenaeum.schemas import validate_wiki_meta
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


def resolve_target(
    target: Any,
    *,
    index: EntityIndex,
    registry_entities: dict[str, Any],
) -> Path | None:
    """Resolve a §3.3 target shape to an existing entity-format page path.

    Returns ``None`` for anything that does not resolve UNAMBIGUOUSLY to
    exactly one existing entity-format page — a target resolving to zero or
    several entities is deliberately not a failure at this layer; the
    caller raises a tier (§8), it never rejects.
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
        if key not in SOURCE_HANDLE_KEYS:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
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
        if len(matches) != 1:
            return None  # zero or ambiguous — §3.3, raise a tier
        path = index.get_by_uid(matches[0])
        if path is None or not path.exists() or not index.has_entity_format(path):
            return None
        if isinstance(etype, str) and etype.strip():
            return _cross_type_guard(path, etype.strip())
        return path

    return None


def _cross_type_guard(path: Path, declared_type: str) -> Path | None:
    """Reject a target resolving to a page of a DIFFERENT type than declared
    (mirrors ``librarian.tier0_handle_upsert``'s same guard)."""
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


def _read_surface_record(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_surface_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(record, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
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
) -> CorrectionRecordResult:
    """Process one correction record against a target entity — the tier-0
    applier. Never raises for a non-conformant record; every failure mode
    returns ``disposition="raised-tier"`` (§8) with a human-readable
    ``reason``, so the caller can hand it to the fallthrough path (§8.1)
    instead of dropping it.
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

    entity_path = resolve_target(target, index=index, registry_entities=registry_entities)
    if entity_path is None:
        return _raised("target resolves to zero or several entities")

    try:
        existing_text = entity_path.read_text(encoding="utf-8")
    except OSError:
        return _raised("target page unreadable")
    existing_meta, existing_body = parse_frontmatter(existing_text)
    if not existing_meta:
        return _raised("target page has no frontmatter")
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
    # attribution" and the eventual write to the mapped surface,
    # regardless of the destination the correction named.
    sensitive_map = resolve_corrections_sensitive_fields(config)
    surface_class = sensitive_map.get(field_name)
    surface_path: Path | None = None
    if surface_class:
        uid = str(existing_meta.get("uid", "") or entity_path.stem)
        surface_root = surface_root_for_class(surface_class, config, knowledge_root)
        surface_path = surface_root / f"{uid}.json"
        read_meta: dict[str, Any] = _read_surface_record(surface_path)
        read_meta.setdefault("uid", uid)
    else:
        read_meta = existing_meta

    if shape == "scalar":
        existing_value = read_meta.get(write_field)
        existing_source = _existing_scalar_source(read_meta, write_field)
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

    if verdict == "noop":
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
        _write_surface_record(surface_path, merged_read)
        if monotone_apply:
            log.info(
                "corrections: monotone apply (routed) — field=%s target=%s source=%s",
                field_name,
                entity_name,
                source,
            )
        return _result("routed-elsewhere", reason, monotone_apply=monotone_apply)

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
) -> BatchOutcome:
    """Process every correction record in one batch file.

    A line that is not even parseable JSON, or parses to something other
    than a ``dict``, is NOT dropped — it gets a synthetic ``raised-tier``
    result (its raw text carried as the ``note``) so it reaches the §8.1
    handoff exactly like any other non-conformant record. This is the
    "nothing is rejected" rule applied one level below individual field
    validation.
    """
    if registry_entities is None:
        registry_entities = load_registry(knowledge_root)

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
    for r in outcome.results:
        counts[r.disposition] = counts.get(r.disposition, 0) + 1
        if r.disposition not in ("applied", "noop"):
            non_trivial_ids.append(r.correction_id)
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
    }


def append_corrections_ledger(wiki_root: Path, outcome: BatchOutcome) -> None:
    record = build_ledger_record(outcome)
    path = default_corrections_ledger_path(wiki_root)
    _append_jsonl_line(path, json.dumps(record, sort_keys=True) + "\n")


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
        )
        applied_this_run += sum(
            1 for r in outcome.results if r.disposition in ("applied", "routed-elsewhere")
        )

        escalations_recorded = True
        if not dry_run:
            for r in outcome.results:
                if r.disposition == "escalated":
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
            write_correction_handoff(outcome, raised, raw_root=raw_root)

        append_corrections_ledger(wiki_root, outcome)

        if outcome.all_terminal(escalations_recorded=escalations_recorded):
            retire_batch(knowledge_root, path)
        else:
            summary["batches_carried_over"] += 1

    return summary
