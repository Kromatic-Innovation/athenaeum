# SPDX-License-Identifier: Apache-2.0
"""Sensitive-value routing/redaction mechanism (issues athenaeum#1023, athenaeum#1024).

Slices 2/4 and 3/4 of athenaeum#949's design note
(`docs/design/sensitivity-value-routing.md`). :func:`route_sensitive_values` (slice
2, athenaeum#1023) scans text for :func:`athenaeum.sensitivity.classify`
matches, routes each configured-on match's value to the secret vault (the
existing ``excluded`` storage surface, athenaeum#429), and returns the text
with every routed span replaced by a resolvable pointer.
:func:`resolve_sensitive_record` (slice 3, athenaeum#1024) is that
pointer's read path: it resolves a ``(sensitivity_class, record_id)`` pair
back to the original value, gated on the matched class's ``read_policy``
(§2's disposition (b) — see that function's own docstring for the full
contract). **This module is standalone in this slice** — nothing in the
rest of the repo calls either function yet. Wiring ``route_sensitive_values``
into the top of :func:`athenaeum.librarian.process_one`, before
``tier0_passthrough`` and both LLM exposures, remains athenaeum#1025 (slice
4) and is NOT implemented here.

**Dark by default (no half-wired state).** :func:`route_sensitive_values`
returns its input completely UNCHANGED whenever
``sensitivity.routing.enabled`` is unset/``False`` (the default,
:func:`athenaeum.config.resolve_sensitivity_routing`, athenaeum#1022) — a
deployment that has not opted in pays nothing and sees no behavior change.
This is deliberately a SEPARATE global switch from athenaeum#910's
``sensitivity.classes`` (which only says what a class IS) — defining a
class does not, by itself, turn on interception of raw intake.

**AC2/AC3 — the pointer contract and the uid problem, disposition (b).**
The pointer this module mints is keyed by a freshly-generated
``record_id`` (a ``uuid5`` derived from non-secret metadata: the caller's
``raw_ref``, the sensitivity class, and the match's character span — never
from the matched value itself, so the id cannot leak anything about the
value it names), NOT by an eventual wiki entity's ``uid``. This is a
deliberate, security-bearing choice the design note's §2 requires stated
explicitly rather than left implicit:

    The existing excluded-surface read path (``include_sensitive``/``read_entity``,
    ``mcp_server._excluded_block_for_hit``) and the settled contact-record
    routing story (``docs/design/field-corrections.md`` §7.1, issue athenaeum#872)
    are both uid-keyed BY CONSTRUCTION — they resolve through
    :class:`athenaeum.pii.ExcludedRecordIndex.by_uid`. This stage runs
    during the RAW sweep, before Tier 2/3 classification has decided
    whether the raw file even becomes a page, let alone minted that page's
    uid — so a uid-keyed join has nothing to key on for most of this
    stage's own input. Design note §2 evaluates and rejects the two
    alternatives (scoping to uid-bearing pages only; an advisory-only
    pointer with no read path) and proposes disposition (b): a NEW,
    record-keyed read path, independent of entity uid, resolved by
    :func:`resolve_sensitive_record` below (athenaeum#1024).
    The issue's non-goal that must not change is the READ PATH'S ACCESS
    CONTROL, not its key — resolution will gate on the matched class's
    ``read_policy.access`` exactly as the uid-keyed path gates on an
    entity's ``access:``, so no access-control posture changes; only the
    JOIN KEY differs, for the structural reason above.

**Vault records do not deduplicate a value across raw files or mentions,
by design.** Because the record id is derived from ``(raw_ref, class,
span)`` and never from the matched value itself (see above — a
value-derived id, even hashed, risks becoming a brute-force oracle for a
low-entropy secret), the SAME real-world secret appearing in N different
raw files, or N times in one file, mints N separate vault records with no
way to recognise they are the same underlying value. **Vault record count
is therefore not a proxy for distinct-secret count.** This is the accepted
trade, not a bug — treat unexplained vault growth accordingly.

**AC1 — composition with ``docs/design/field-corrections.md`` §7.1.** §7.1 settled
(athenaeum#872) that CONTACT-value routing (email/phone tied to an existing
entity) reads and writes through the shared contact-record path
(``pii.resolve_contact_record_for_uid`` / ``classify_contact_value`` /
``iter_contact_records``) rather than a private ``{uid}.json`` format. This
module honors the same underlying principle — "write through the
CONFIGURED surface's own primitives, never a hardcoded parallel store" — by
resolving its vault root through the existing ``storage.mapping``/adapter
layer (athenaeum#429) and rendering records with
:func:`athenaeum.models.render_frontmatter` /
:func:`athenaeum.atomic_io.atomic_write_text`, the same primitives §7.1's
settled path and ``storage_migrate._render_excluded_record`` are built
from. It does NOT reuse §7.1's *uid-keyed* contact-record shape itself, for
the AC3 reason above — §7.1's shape has no field to hold a value with no
owning entity yet. The two record shapes are siblings on the same surface,
not the same shape.

**AC7 — precedence.** A defined class whose resolved routing ``action`` is
``"off"`` (:func:`athenaeum.config.resolve_sensitivity_routing`) never
routes, even when the global switch is on — the operator's explicit
per-class opt-out, checked before any vault write. When more than one
class's recogniser matches the SAME character span (design note §7
Decision D6's stated escape hatch — two recogniser names wrapping the same
detector, each bound to a different class), or when two matches have
OVERLAPPING spans, precedence is deterministic: matches are sorted by
``(span start, sensitivity_class name)`` and the first-sorted match at each
position wins; a later match whose span overlaps an already-kept match is
dropped. This never means a value is left un-redacted — it only decides
which class's record/pointer names it when more than one would apply.

**AC9 — usage classification.** Routed values are NEVER auto-stamped with
a ``usage_class`` (issue athenaeum#866) by this module. Usage
classification is a contact-identifier-specific axis (outreach eligibility
for an email/phone tied to a person) — most sensitivity classes this
routes (an operator's ``secret``/``api_key`` class, for instance) are not
contact-shaped at all, and even for the built-in ``pii`` class's own
email/phone matches, this module has no entity/uid to classify usage
AGAINST at raw-sweep time (see AC3 above). Per ``docs/design/field-corrections.md``
§7.1's own rule ("a correction that omits ``usage_class`` ... reads back as
``unclassified`` (never outreach-eligible) ... the safe direction"), a
vault record this module writes carries no usage marker at all, which reads
back exactly as conservatively as an explicit `unclassified` would.

**AC10 — fail closed on any routing failure.** :func:`route_sensitive_values`
raises :class:`SensitivityRoutingError` — never partially redacts, never
falls through to returning the original text — for: a malformed
``sensitivity.*`` config (surfaced as this error rather than propagating
the lower-level config-error type, so every failure mode in this stage is
one exception family); a match with no character span (a hypothetical
future frontmatter-``field`` recogniser match — no built-in recognizer
produces one today, but the contract exists, see
:mod:`athenaeum.sensitivity`'s docstring, and this module refuses to guess
a substitution point rather than silently skip it); an unsafe resolved
vault surface (see below); or any exception raised while writing a vault
record (disk full, permission error, …). The intended caller (the
librarian raw-sweep hook, athenaeum#1025 — not wired in this slice) is
expected NOT to catch this locally, letting it propagate to the
entity-tier sweep loop's existing generic ``except Exception`` handler,
which is ALREADY the established fail-closed behavior for any other
mid-file processing error in this codebase: the raw file is left untouched
on disk (raw/ is append-only; nothing here ever writes to it), nothing is
written to the wiki for that file, the failure is logged and counted
toward the existing stuck-file ledger, and the file is retried on the next
sweep. The value is therefore never dropped and never lands in the wiki in
the clear — exactly AC10's two requirements. Every
:class:`SensitivityRoutingError` message is constructed from non-secret
metadata ONLY (ref, class name, span offsets, exception type name) — never
from a matched value or raw content — because the intended caller logs the
exception's string form verbatim.

**Unsafe vault surface, fail closed.** A sensitivity class's vault root is
resolved via ``storage.mapping`` exactly like any other storage-adapter
class (reusing athenaeum#429's existing layer) — but if an operator maps a
routed class to an adapter that PARTICIPATES in the corpus (a misconfigured
``storage.mapping`` entry pointing a `secret` class at the default wiki
surface, say), this module refuses to write there and raises
:class:`SensitivityRoutingError` instead: routing a value onto an in-corpus
surface is worse than not routing it at all — it is the exact failure this
whole issue exists to prevent, so an operator misconfiguration here must be
loud, not a silent leak. A class with no rule at all is refused (athenaeum#3168,
a BREAKING change): with routing switched on, every class that routes needs a
``storage.routes`` rule (or a ``storage.mapping`` entry) naming where its values
go, or ``to: []`` to discard them (:func:`refuse_unrouted_value_classes`). There
is no built-in fallback location any more, because "undeclared" must not mean
"stored somewhere the operator never chose". A discarded class
(``to: []``) replaces each recognised value with a fixed marker naming the class
and saying the value was not stored; no record is written. This governs what the
compile run writes. A value already in a raw file is handled by the write-time
screen and the screening phase.

**AC11 — idempotency / re-entrancy.** ``raw/`` is append-only and this
module never writes to it; each sweep reads the SAME on-disk raw content
and this module's classification always runs against that original,
never-mutated text (never against a previously-produced wiki page), so
re-running a sweep can never "double-redact" an already-pointered wiki
page — the source of truth for detection is always the untouched raw
content. Each routed match's ``record_id`` is derived deterministically
(``uuid5`` over ``raw_ref`` + class + span, no randomness), so
re-processing the same raw content mints the SAME record id and overwrites
the SAME vault record with byte-identical content — never a duplicate.

**AC12 — the correlation trade.** A distinguishable pointer per routed
value is a correlatable index into the vault, and the pointer *count* on a
page discloses how many distinct values of a class it holds — the same
trade :class:`athenaeum.pii.RedactionMarker.value_count` already makes
deliberately for contact redactions. Accepted here for the same reason:
without distinguishable pointers, AC2's "a reader can re-request the
specific value they need" is unsatisfiable (an agent that needs the SECOND
of three redacted values on a page has no way to ask for it from an
undifferentiated marker), and the alternative — a single generic marker
per page regardless of value count — is the exact "byte-identical for
every value on a page" failure the issue's Pointer Contract section cites
as the CURRENT migrator's dead end.

**AC8 — relationship to ``pii.RedactionMarker`` — left open.** Design note
§7.6 settles that ``RedactionMarker`` (the API-layer dataclass
``pii.assemble_excluded_read`` returns for a uid-keyed contact field a
reader asked to see) and this module's pointer are two INDEPENDENT
contracts, not one contract rendered two ways — the pointer this module
emits is a literal string substituted directly into a page's body text at
compile time, for a value that may have no owning uid at all. What the
note does NOT settle, and what this slice does not decide either, is
whether ``RedactionMarker`` should begin naming the read flag
(``include_sensitive=True``) the way this module's pointer names its own read
function — that sub-question is explicitly left with a follow-on issue,
not resolved here or by this module. See athenaeum#1023's issue thread for
the full disposition.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_sensitivity_routing
from athenaeum.models import is_page_authorized, parse_frontmatter, render_frontmatter
from athenaeum.sensitivity import (
    ClassifiedMatch,
    SensitivityConfigError,
    available_classes,
    classify,
)
from athenaeum.storage import (
    EXCLUDED,
    RouteDecision,
    StorageConfigError,
    StorageTargetError,
    apply_to_targets,
    available_adapters,
    note_discard,
    route_for,
    route_for_sensitivity_class,
)

#: Marker key on a vault record, mirroring :data:`athenaeum.pii.PII_FLAG`'s
#: belt-and-suspenders pattern — excluded even by the flag, not only by
#: surface placement.
SENSITIVITY_ROUTED_FLAG = "sensitivity_routed"

#: Fixed namespace for this module's deterministic ``uuid5`` record ids
#: (AC11). A constant, never regenerated — changing it would silently mint
#: a new id for every existing routed value on the next sweep.
_RECORD_ID_NAMESPACE = uuid.UUID("6f2b8b53-7c2e-4f1a-9b0e-6a2d6a9b6d1a")

#: The pointer's leading token — see :func:`_pointer_text`.
POINTER_PREFIX = "sensitive"


class SensitivityRoutingError(Exception):
    """Raised when a detected sensitive value cannot be safely routed (athenaeum#1023).

    Fail-closed by contract — see this module's docstring "AC10" section.
    The message MUST NEVER include a matched value or raw content; the
    intended caller (the librarian raw-sweep hook, athenaeum#1025) logs it
    verbatim.
    """


def _class_action(sensitivity_class: str, routing_cfg: dict[str, Any]) -> str:
    """Resolve one class's routing action: ``"route"`` (default) or ``"off"``."""
    per_class = routing_cfg.get("classes", {})
    block = per_class.get(sensitivity_class, {})
    return block.get("action", "route")


def _class_decision(
    sensitivity_class: str, config: dict[str, Any] | None, *, require_rule: bool = True
) -> RouteDecision:
    """The ``storage.routes`` / ``storage.mapping`` decision for a class, or a refusal.

    A class with no rule is refused, naming the rule to write (athenaeum#3168):
    it no longer falls back to the built-in excluded location, and it is not
    discarded either. ``require_rule=False`` (the read path) returns the
    unruled decision instead, so an already-stored record stays readable.
    """
    try:
        # A storage.routes sensitivity_class rule decides first; a class no
        # rule names keeps the storage.mapping lookup.
        decision = route_for_sensitivity_class(sensitivity_class, config) or route_for(
            sensitivity_class, config=config
        )
    except StorageConfigError as exc:
        raise SensitivityRoutingError(
            f"sensitivity class {sensitivity_class!r} storage.mapping is invalid: {exc}"
        ) from exc
    if decision.rule is None and require_rule:
        raise SensitivityRoutingError(_missing_rule_message(sensitivity_class))
    return decision


def _missing_rule_message(sensitivity_class: str) -> str:
    return (
        f"sensitivity class {sensitivity_class!r} has value routing switched on "
        "(sensitivity.routing.enabled) and no storage.routes rule. Write one: "
        f"`- sensitivity_class: {sensitivity_class}` with `to: [<location>]` to keep the "
        "value in a location outside the corpus, or `to: []` to discard it; or set "
        f"sensitivity.routing.classes.{sensitivity_class}.action to `off`"
    )


def refuse_unrouted_value_classes(config: dict[str, Any] | None) -> None:
    """Refuse a class that value routing would route but no rule places (athenaeum#3168).

    Called when routing is about to run. With ``sensitivity.routing.enabled`` on,
    every defined class that has a recogniser and is not switched ``off`` needs
    a ``storage.routes`` rule (or a ``storage.mapping`` entry). Raises
    :class:`SensitivityRoutingError` naming the rule to write. A class with no
    recogniser can match nothing and is not checked.
    """
    try:
        routing_cfg = resolve_sensitivity_routing(config)
        if not routing_cfg.get("enabled", False):
            return
        classes = available_classes(config)
    except (ValueError, SensitivityConfigError) as exc:
        raise SensitivityRoutingError(f"sensitivity configuration is invalid: {exc}") from exc
    for name in sorted(classes):
        if not classes[name].recognizers:
            continue
        if _class_action(name, routing_cfg) != "route":
            continue
        _class_decision(name, config)


def _vault_targets_for_class(
    sensitivity_class: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    for_read: bool = False,
) -> list[tuple[str, Path]]:
    """Resolve ``(name, vault root)`` for every target of *sensitivity_class* — fail closed.

    ``for_read=True`` (resolving an already-stored record) lets a class with no
    rule fall back to the built-in excluded location, where such records were
    written before athenaeum#3168; writes still refuse a class with no rule.

    See this module's docstring, "Unsafe vault surface, fail closed." A class
    whose rule discards (``to: []``) has no vault: this raises, and the callers
    that reach here never do for a discarded class (see :func:`_route_one`). The
    first entry is the primary (athenaeum#3169); a rule with one target yields one.
    """
    decision = _class_decision(sensitivity_class, config, require_rule=not for_read)
    unruled = decision.rule is None
    if not decision.targets and not unruled:
        raise SensitivityRoutingError(
            f"sensitivity class {sensitivity_class!r} is discarded ({decision.rule}); "
            "there is no vault root"
        )
    if unruled:
        # Read fallback only (``for_read``): the built-in excluded location.
        names: tuple[str, ...] = (EXCLUDED.name,)
    else:
        names = decision.targets
    roots: list[tuple[str, Path]] = []
    for name in names:
        try:
            adapter = EXCLUDED if unruled else available_adapters(config)[name]
        except (StorageConfigError, KeyError) as exc:
            raise SensitivityRoutingError(
                f"sensitivity class {sensitivity_class!r} storage.mapping is invalid: {exc}"
            ) from exc
        # Outside the corpus, NOT ``decision.sensitive``: a custom location
        # that is outside the corpus but not marked sensitive keeps working
        # exactly as before (athenaeum#3161).
        if adapter.corpus_policy.in_corpus:
            raise SensitivityRoutingError(
                f"storage.mapping routes sensitivity class {sensitivity_class!r} "
                f"to adapter {adapter.name!r}, which participates in the "
                "corpus — refusing to write a routed sensitive value there "
                "(fail closed)"
            )
        vault = adapter.resolve_root(knowledge_root) / "sensitivity" / sensitivity_class
        roots.append((name, vault))
    return roots


def _vault_root_for_class(
    sensitivity_class: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    for_read: bool = False,
) -> Path:
    """The primary target's vault root (the one reads resolve from).

    ``for_read=True`` falls back to the built-in excluded location for a class
    with no rule (athenaeum#3168); a write never passes it.
    """
    return _vault_targets_for_class(
        sensitivity_class, config, knowledge_root, for_read=for_read
    )[0][1]


def screen_raw_for_compile(
    raw: Any,
    *,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> tuple[dict[str, Any], str]:
    """The compile run's first read of a raw: route free-text values, drop discarded fields.

    The ONE place both transports (``librarian.process_one`` and the batch
    transport) call before any tier reads ``raw.content`` (athenaeum#3168).
    Two steps, both mutating only the in-memory ``raw`` (never the file):

    1. :func:`route_sensitive_values` over the body (frontmatter untouched).
    2. A frontmatter field a ``storage.routes`` rule with ``to: []`` discards is
       removed, so no tier writes it. A page type discarded whole is left for
       the capture gate; a bad storage table surfaces where the page
       destination is resolved.

    Returns the re-parsed ``(frontmatter, body)``. Raises
    :class:`SensitivityRoutingError` on a routing failure (fail closed).
    """
    raw_meta, raw_body = parse_frontmatter(raw.content)
    preamble = raw.content[: len(raw.content) - len(raw_body)]
    redacted_body = route_sensitive_values(
        raw_ref=raw.ref,
        text=raw_body,
        frontmatter=raw_meta,
        config=config,
        knowledge_root=knowledge_root,
    )
    if redacted_body != raw_body:
        raw._content = preamble + redacted_body
    raw_type = str(raw_meta.get("type") or "").strip()
    discarded_keys: dict[str, str] = {}
    if raw_type:
        try:
            if not route_for(raw_type, config=config).discarded:
                for key in raw_meta:
                    decision = route_for(raw_type, str(key), config=config)
                    if decision.discarded and decision.rule is not None:
                        discarded_keys[str(key)] = decision.rule
        except StorageConfigError:
            discarded_keys = {}
    if discarded_keys:
        for rule in discarded_keys.values():
            note_discard(rule)
        _, body_now = parse_frontmatter(raw.content)
        raw._content = (
            render_frontmatter({k: v for k, v in raw_meta.items() if str(k) not in discarded_keys})
            + "\n"
            + body_now
        )
    return parse_frontmatter(raw.content)


# ---------------------------------------------------------------------------
# athenaeum#3175 — the intake call site. The same screen the compile run uses
# (:func:`route_sensitive_values`, the ``storage.routes`` resolver), applied at
# the moment athenaeum writes a raw note rather than at the next compile.
# ---------------------------------------------------------------------------

_MAX_ID_PROBES = 64


def _collision_free_record_id(record_id: str, *, value: str, vault: Path) -> str:
    """The first deterministic id from *record_id* whose record is absent or holds *value*."""
    candidate = record_id
    for n in range(_MAX_ID_PROBES):
        if n:
            candidate = uuid.uuid5(_RECORD_ID_NAMESPACE, f"{record_id}|{n}").hex
        path = vault / f"{candidate}.md"
        if not path.exists():
            return candidate
        try:
            _meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return candidate
        if _value_from_vault_body(body) == value:
            return candidate
    raise SensitivityRoutingError(
        f"{vault.name}: no free vault record id after {_MAX_ID_PROBES} probes — failing closed"
    )


def _field_class_name(page_type: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", page_type)
    return f"field_{cleaned}"


def _field_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _route_one_field(
    *,
    raw_ref: str,
    page_type: str,
    key: str,
    value: Any,
    targets: tuple[str, ...],
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> str:
    """Write one routed frontmatter field's value to its vault(s); return the pointer."""
    sensitivity_class = _field_class_name(page_type)
    text = _field_text(value)
    record_id = _record_id_for(raw_ref, sensitivity_class, 0, 0)
    record_id = uuid.uuid5(_RECORD_ID_NAMESPACE, f"{record_id}|field|{key}").hex
    vaults: dict[str, Path] = {}
    try:
        adapters = available_adapters(config)
        for name in targets:
            adapter = adapters[name]
            if adapter.corpus_policy.in_corpus:
                raise SensitivityRoutingError(
                    f"storage rule routes field {key!r} of page type {page_type!r} to "
                    f"adapter {adapter.name!r}, which participates in the corpus — "
                    "refusing to write a routed value there (fail closed)"
                )
            vaults[name] = adapter.resolve_root(knowledge_root) / "sensitivity" / sensitivity_class
    except (StorageConfigError, KeyError) as exc:
        raise SensitivityRoutingError(
            f"field {key!r} of page type {page_type!r}: storage configuration is invalid"
        ) from exc
    primary = vaults[targets[0]]
    record_id = _collision_free_record_id(record_id, value=text, vault=primary)
    rendered = _render_vault_record(
        record_id=record_id,
        sensitivity_class=sensitivity_class,
        source_ref=raw_ref,
        value=text,
    )

    def _write(name: str) -> None:
        vaults[name].mkdir(parents=True, exist_ok=True)
        atomic_write_text(vaults[name] / f"{record_id}.md", rendered)

    try:
        apply_to_targets(tuple(vaults), _write)
    except StorageTargetError as exc:
        raise SensitivityRoutingError(f"{raw_ref}: {exc}") from exc
    return _pointer_text(sensitivity_class, record_id)


def _derived_intake_ref(
    meta: dict[str, object],
    body: str,
    screened_keys: set[str],
    config: dict[str, Any] | None,
) -> str:
    """A stable ref for a note with none supplied: the text, screened values blanked.

    Hashing the whole note would put a guessable digest of a low-entropy value
    (a national id, say) into a pointer; blanking each value the screen acts on
    first keeps the ref a function of the surrounding text only. Two notes that
    differ only in the value share a ref, which ``probe_collisions`` handles.
    """
    import hashlib

    try:
        spans = sorted(
            m.match.span
            for m in classify(text=body, frontmatter=meta, config=config)
            if m.match.span is not None
        )
    except SensitivityConfigError:
        spans = []
    blanked, pos = [], 0
    for start, end in spans:
        if start < pos:
            continue
        blanked.append(body[pos:start])
        pos = end
    blanked.append(body[pos:])
    kept = {str(k): v for k, v in meta.items() if str(k) not in screened_keys}
    digest = hashlib.sha256(
        ("\x00".join(blanked) + "\x01" + repr(sorted(kept.items()))).encode("utf-8")
    ).hexdigest()
    return f"intake:{digest}"


def screen_intake_content(
    content: str,
    *,
    raw_ref: str | None = None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> str:
    """Screen one raw note's text at write time: route or discard, return what to write.

    The intake call site of the same screen the compile run applies
    (:func:`route_sensitive_values` plus the ``storage.routes`` field rules of
    :func:`screen_raw_for_compile`), run before the raw note exists (athenaeum#3175):

    1. Free-text values in the body are routed to their vault or replaced by the
       fixed discard marker.
    2. A frontmatter field a rule routes for the note's declared ``type`` has its
       value written to the vault and replaced by a pointer; a field a rule
       discards is removed.

    Every vault write happens here, before the caller writes the raw note: a
    failure raises :class:`SensitivityRoutingError` and nothing has been
    written for the note. *raw_ref* must be stable for the same note (the record
    ids derive from it), so a retry after a crash converges on the same records.
    Left out, it is derived from the text with every screened value blanked.

    With no rule that applies, *content* is returned unchanged, byte for byte.
    """
    meta, body = parse_frontmatter(content)
    preamble = content[: len(content) - len(body)]
    raw_type = str(meta.get("type") or "").strip()
    discarded: dict[str, str] = {}
    routed: dict[str, tuple[str, ...]] = {}
    if raw_type:
        try:
            if not route_for(raw_type, config=config).discarded:
                for key in meta:
                    decision = route_for(raw_type, str(key), config=config)
                    if decision.discarded and decision.rule is not None:
                        discarded[str(key)] = decision.rule
                    elif decision.targets:
                        routed[str(key)] = decision.targets
        except StorageConfigError:
            discarded, routed = {}, {}

    if not discarded and not routed:
        try:
            routing_on = bool(resolve_sensitivity_routing(config).get("enabled", False))
        except ValueError:
            routing_on = True  # route_sensitive_values below raises the named error
        if not routing_on:
            return content

    if raw_ref is None:
        try:
            raw_ref = _derived_intake_ref(meta, body, set(discarded) | set(routed), config)
        except Exception:  # noqa: BLE001 - screen below fails closed on a bad config
            raw_ref = "intake:unkeyed"
    redacted_body = route_sensitive_values(
        raw_ref=raw_ref,
        text=body,
        frontmatter=meta,
        config=config,
        knowledge_root=knowledge_root,
        probe_collisions=True,
    )

    if not discarded and not routed:
        return content if redacted_body == body else preamble + redacted_body

    new_meta: dict[str, object] = {}
    for key, value in meta.items():
        k = str(key)
        if k in discarded:
            note_discard(discarded[k])
            continue
        if k in routed:
            if isinstance(value, str) and _POINTER_RE.fullmatch(value.strip()):
                # Already a pointer: screened before. Screening is idempotent.
                new_meta[key] = value
                continue
            new_meta[key] = _route_one_field(
                raw_ref=raw_ref,
                page_type=raw_type,
                key=k,
                value=value,
                targets=routed[k],
                config=config,
                knowledge_root=knowledge_root,
            )
            continue
        new_meta[key] = value
    return render_frontmatter(new_meta) + "\n" + redacted_body


def routed_classes_in(
    content: str, *, config: dict[str, Any] | None
) -> list[str]:
    """Names of the classes a screen would act on in *content*, never a value (athenaeum#3178).

    The free-text classes value routing would route or discard, and one
    ``field_<type>`` name per declared field a ``storage.routes`` rule routes or
    discards for the note's ``type``. Sorted, de-duplicated; ``[]`` when nothing
    applies. For a reader that must say what a file holds without quoting it.
    """
    meta, body = parse_frontmatter(content)
    found: set[str] = set()
    raw_type = str(meta.get("type") or "").strip()
    if raw_type:
        try:
            if not route_for(raw_type, config=config).discarded:
                for key, value in meta.items():
                    if isinstance(value, str) and _POINTER_RE.fullmatch(value.strip()):
                        continue  # already a pointer
                    decision = route_for(raw_type, str(key), config=config)
                    if decision.rule is not None and (decision.discarded or decision.targets):
                        found.add(_field_class_name(raw_type))
        except StorageConfigError:
            pass
    try:
        routing_cfg = resolve_sensitivity_routing(config)
        if routing_cfg.get("enabled", False):
            for m in classify(text=body, frontmatter=meta, config=config):
                if _class_action(m.sensitivity_class, routing_cfg) == "route":
                    found.add(m.sensitivity_class)
    except (ValueError, SensitivityConfigError):
        pass
    return sorted(found)


def _record_id_for(raw_ref: str, sensitivity_class: str, start: int, end: int) -> str:
    """Deterministic record id — derived from metadata only, never the value (AC11)."""
    name = f"{raw_ref}|{sensitivity_class}|{start}|{end}"
    return uuid.uuid5(_RECORD_ID_NAMESPACE, name).hex


def _pointer_text(sensitivity_class: str, record_id: str) -> str:
    """The resolvable, non-leaking pointer substituted for a routed span (AC2).

    Names the read a tool caller can actually make: ``read_entity`` with
    ``include_sensitive=true`` (athenaeum#3163), which resolves the pointer
    through :func:`resolve_pointers_in_text`. Pointers minted before that
    change named :func:`resolve_sensitive_record` instead; both shapes match
    :data:`_POINTER_RE`.
    """
    return (
        f"[{POINTER_PREFIX}:{sensitivity_class}:{record_id} — value withheld; "
        "read this page with include_sensitive=true to resolve]"
    )


def _discard_marker(sensitivity_class: str) -> str:
    """The fixed text that replaces a discarded value: the class, and that it was not stored.

    Carries nothing derived from the value, the source or the span.
    """
    return f"[{POINTER_PREFIX}:{sensitivity_class} — value not stored (discarded)]"


def _render_vault_record(
    *, record_id: str, sensitivity_class: str, source_ref: str, value: str
) -> str:
    meta: dict[str, Any] = {
        "record_id": record_id,
        "sensitivity_class": sensitivity_class,
        SENSITIVITY_ROUTED_FLAG: True,
        "source_ref": source_ref,
        "created": date.today().isoformat(),
    }
    body = (
        f"Sensitive value routed off raw intake {source_ref!r} to the secret "
        "vault (issue athenaeum#1023). This record is outside the corpus: "
        "not embedded, recalled, or merge-eligible.\n\n"
        f"{value}\n"
    )
    return render_frontmatter(meta) + "\n" + body


def _route_one(
    *,
    raw_ref: str,
    sensitivity_class: str,
    start: int,
    end: int,
    value: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    probe_collisions: bool = False,
) -> str:
    decision = _class_decision(sensitivity_class, config)
    if decision.discarded:
        # One branch, at the point the resolver is asked where to write: a
        # discarded class writes no record. Only the rule's counter moves.
        note_discard(decision.rule)
        return _discard_marker(sensitivity_class)
    record_id = _record_id_for(raw_ref, sensitivity_class, start, end)
    if probe_collisions:
        record_id = _collision_free_record_id(
            record_id,
            value=value,
            vault=_vault_root_for_class(sensitivity_class, config, knowledge_root),
        )
    # Every target in order, each write idempotent (the record id is derived from
    # the span, the file is replaced whole); a failure names the target and
    # raises (athenaeum#3169).
    rendered = _render_vault_record(
        record_id=record_id,
        sensitivity_class=sensitivity_class,
        source_ref=raw_ref,
        value=value,
    )
    vaults = dict(_vault_targets_for_class(sensitivity_class, config, knowledge_root))

    def _write(name: str) -> None:
        vaults[name].mkdir(parents=True, exist_ok=True)
        atomic_write_text(vaults[name] / f"{record_id}.md", rendered)

    try:
        apply_to_targets(tuple(vaults), _write)
    except StorageTargetError as exc:
        raise SensitivityRoutingError(f"{raw_ref}: {exc}") from exc
    return _pointer_text(sensitivity_class, record_id)


def route_sensitive_values(
    *,
    raw_ref: str,
    text: str,
    frontmatter: dict[str, Any] | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    probe_collisions: bool = False,
) -> str:
    """Scan *text*, route every matching, routable value, return redacted text.

    Standalone in this slice — not yet called from anywhere in this repo
    (athenaeum#1025 wires it into the top of
    :func:`athenaeum.librarian.process_one`). *raw_ref* is a stable,
    non-secret reference to the raw file being screened (its path relative
    to the knowledge root, for example) — used only to derive each match's
    deterministic ``record_id`` (AC11) and to name the file in any raised
    error message.

    *probe_collisions* (the intake call site, athenaeum#3175) lets a caller whose
    *raw_ref* is derived from the text rather than a unique file path stay safe
    when two different values share a ref and a span: an existing record that
    holds a different value moves the id to the next free one, deterministically,
    so a retry of the same text still converges on the same record.

    Returns *text* completely unchanged when ``sensitivity.routing.enabled``
    is off (the default) or nothing routable matched — see this module's
    docstring for the full design.

    Raises :class:`SensitivityRoutingError` — never returns a partially
    redacted or silently-unredacted string — on any failure. See "AC10" in
    this module's docstring for the fail-closed contract callers rely on.
    """
    try:
        routing_cfg = resolve_sensitivity_routing(config)
    except ValueError as exc:
        raise SensitivityRoutingError(
            f"{raw_ref}: sensitivity.routing config is invalid: {exc}"
        ) from exc

    if not routing_cfg.get("enabled", False):
        return text

    refuse_unrouted_value_classes(config)

    try:
        matches = classify(text=text, frontmatter=frontmatter, config=config)
    except SensitivityConfigError as exc:
        raise SensitivityRoutingError(
            f"{raw_ref}: sensitivity classification config is invalid: {exc}"
        ) from exc

    routable: list[ClassifiedMatch] = [
        m for m in matches if _class_action(m.sensitivity_class, routing_cfg) == "route"
    ]
    if not routable:
        return text

    # AC10: fail closed on any match this stage cannot safely substitute
    # in-place — never let it fall through un-redacted.
    unresolvable = [m for m in routable if m.match.span is None]
    if unresolvable:
        classes = sorted({m.sensitivity_class for m in unresolvable})
        raise SensitivityRoutingError(
            f"{raw_ref}: {len(unresolvable)} sensitivity match(es) in class(es) "
            f"{classes} have no text span (frontmatter-field match) — "
            "field-based routing is not implemented in this slice; failing "
            "closed rather than leaving the value unredacted"
        )

    # The unresolvable check above raised on any None span, so every
    # remaining match's span is guaranteed non-None here — but that's a
    # runtime invariant across a list, which mypy cannot infer from the
    # aggregate `if unresolvable: raise`. Narrow explicitly, once, so the
    # rest of the function works with plain (start, end) tuples instead of
    # re-reading the Optional `m.match.span` at each use site.
    resolved: list[tuple[int, int, ClassifiedMatch]] = []
    for m in routable:
        span = m.match.span
        assert span is not None, (
            "unresolvable (None-span) matches were raised above"
        )
        resolved.append((span[0], span[1], m))

    # AC7: deterministic precedence for overlapping / multiply-classified spans.
    ordered = sorted(resolved, key=lambda t: (t[0], t[2].sensitivity_class))
    kept: list[tuple[int, int, ClassifiedMatch]] = []
    last_end = -1
    for start, end, m in ordered:
        if start < last_end:
            continue
        kept.append((start, end, m))
        last_end = end

    # Write every vault record BEFORE mutating any text, so a failure partway
    # through never leaves some values pointered and others still embedded
    # in a half-redacted string (AC10/AC11).
    pointers: dict[tuple[int, int], str] = {}
    for start, end, m in kept:
        value = text[start:end]
        try:
            pointer = _route_one(
                raw_ref=raw_ref,
                sensitivity_class=m.sensitivity_class,
                start=start,
                end=end,
                value=value,
                config=config,
                knowledge_root=knowledge_root,
                probe_collisions=probe_collisions,
            )
        except SensitivityRoutingError:
            raise
        except Exception as exc:
            # Deliberate broad catch: ANY vault-write failure (disk full,
            # permission error, an adapter-specific exception type this
            # module has no reason to enumerate) must become a
            # SensitivityRoutingError so the caller's fail-closed contract
            # (AC10) holds regardless of the underlying cause.
            raise SensitivityRoutingError(
                f"{raw_ref}: failed to write sensitivity class "
                f"{m.sensitivity_class!r} value to the secret vault "
                f"({type(exc).__name__}) — failing closed"
            ) from exc
        pointers[(start, end)] = pointer

    redacted = text
    for start, end, _m in sorted(kept, key=lambda t: t[0], reverse=True):
        redacted = redacted[:start] + pointers[(start, end)] + redacted[end:]
    return redacted


# ---------------------------------------------------------------------------
# Slice 3/4 (athenaeum#1024) — the record-keyed read path. Standalone from
# the write path above except for the two pieces it deliberately reuses:
# :func:`_vault_root_for_class` (so a read is resolved from the SAME root a
# write landed on, never re-derived) and the ``(record_id, sensitivity_class)``
# shape :func:`_pointer_text`/:func:`_render_vault_record` already define.
# ---------------------------------------------------------------------------

#: A record id this module ever mints is exactly ``uuid.UUID.hex`` — 32
#: lowercase hex characters, no dashes, no path separators (see
#: :func:`_record_id_for`). Any other shape is rejected before it is used to
#: build a path, rather than sanitized: silently coercing a malformed id
#: into "the nearest safe-looking string" could resolve to an existing
#: record that was never the caller's intended target.
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: A sensitivity class name, restricted to the identifier-shaped charset
#: every example in the design note and this codebase's own built-in
#: ``pii``/config-driven classes uses (``pii``, ``secret``, ``api_key``,
#: ``hipaa``, ...). Deliberately excludes ``/``, ``\``, ``.``, whitespace,
#: and every other character that could make ``sensitivity_class`` behave
#: as more than one path segment once it reaches
#: :func:`_vault_root_for_class`, which (like :func:`route_sensitive_values`,
#: its only other caller) trusts the string it is given — there it comes
#: from operator config; here it comes from a caller-supplied pointer, which
#: this module must treat as untrusted input.
_SENSITIVITY_CLASS_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _value_from_vault_body(body: str) -> str | None:
    """Invert :func:`_render_vault_record`'s body shape, or ``None`` if it doesn't match.

    ``_render_vault_record`` always writes ``"<one-line prose>\\n\\n<value>\\n"``
    as the body (the caller's raw ``value`` is never itself further encoded).
    Splitting on the FIRST blank line recovers ``value`` exactly, including a
    value that happens to contain its own blank lines — ``maxsplit=1`` stops
    after the prose/value boundary, so nothing past it is touched. Stripping
    exactly one trailing newline undoes exactly the one ``_render_vault_record``
    appended, no more. A body with no blank line at all (never produced by
    this module's own writer, but possible if a vault record was hand-edited
    or truncated) does not match the shape and returns ``None`` rather than
    guessing.
    """
    parts = body.split("\n\n", 1)
    if len(parts) != 2:
        return None
    value_section = parts[1]
    if value_section.endswith("\n"):
        value_section = value_section[:-1]
    return value_section


def resolve_sensitive_record(
    sensitivity_class: str,
    record_id: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    caller_audience: set[str] | None = None,
    vault_read_allowed: Callable[[str], bool] | None = None,
) -> str | None:
    """Resolve a pointer's ``(sensitivity_class, record_id)`` back to its value.

    The read half of AC2/AC3's disposition (b) — see this module's own
    docstring and design note §2. :func:`route_sensitive_values` mints the
    pointer this function resolves; the two are the write/read halves of one
    contract, standalone from each other in this slice (see the "Follow-on
    implementation slices" list in ``docs/design/sensitivity-value-routing.md``
    §10) but sharing :func:`_vault_root_for_class` so a read is always
    resolved against the SAME root a write landed on.

    **Contract: resolves to the original value, or to nothing resolvable —
    never raises with any content in the exception.** Every failure mode
    below — a malformed ``record_id``; a malformed or path-traversal-shaped
    ``sensitivity_class``/``record_id``; an unknown class; a class this
    ``config`` cannot safely resolve a vault root for; a ``record_id`` that
    does not exist; a ``record_id`` that resolves under a DIFFERENT class
    than the one requested; a caller not authorized by the class's
    ``read_policy``; or any I/O/parse error reading the record — returns
    ``None``, indistinguishably from every other failure mode. This is
    deliberate, not merely convenient: per design note §2, "resolution
    failure cannot be used to probe the vault's contents" — a caller
    (or an attacker driving this function through a crafted pointer) must
    not be able to tell "wrong class" from "doesn't exist" from "not
    authorized" from the return value alone. The outer ``try/except`` is
    belt-and-suspenders over the explicit checks below it, so an
    unanticipated failure (a permission error, a symlink cycle, ...) fails
    closed the same way rather than propagating a bare traceback.

    **Access control — no new mechanism (AC2, and the issue's own
    athenaeum#1024 comment thread, "AC14").** This function gates on the
    matched class's ``read_policy.access``/``audience`` by calling
    :func:`athenaeum.models.is_page_authorized` — the SAME function the
    existing uid-keyed excluded-surface read path's caller
    (``mcp_server``'s Layer C, ``is_page_authorized(fm, caller_audience)``)
    already gates on, not a re-implementation of its access/audience
    comparison logic. Both read paths therefore share the one place that
    decision is actually made: a future fix to ``is_page_authorized`` (a new
    audience special-case, a bounce-mark posture change) applies to both
    paths at once, rather than needing to be ported across two independent
    implementations and risking the silent-divergence failure the comment
    thread flags. What legitimately differs between the two paths — by
    design, not by oversight (§2) — is only the INPUT each supplies to that
    one shared function: the uid-keyed path passes a page's own frontmatter;
    this function passes a synthetic ``{"access": ..., "audience": [...]}``
    built from the class's resolved :class:`~athenaeum.sensitivity.ReadPolicy`,
    since a routed value has no owning page to read a frontmatter block
    from. ``caller_audience`` follows the exact convention
    ``mcp_server.py``'s ``caller_audience: set[str] | None`` already
    establishes: ``None`` is the trusted/owner caller (authorized for
    everything, matching :func:`athenaeum.models.is_page_authorized`'s own
    ``None`` semantics); a non-``None`` set is a restricted caller, gated
    exactly as any other restricted read is. This parameter is not named in
    the issue's literal ``(sensitivity_class, record_id, config,
    knowledge_root)`` signature sketch, but the AC that follows it —
    "Gates on the matched class's ``read_policy``" — is unsatisfiable
    without a way to know who is asking; every other caller-facing gate in
    this codebase (``mcp_server.py``) reaches that same conclusion the same
    way.

    Args:
        sensitivity_class: The class named in the pointer, e.g. the
            ``pii`` in ``[sensitive:pii:<record_id> — ...]``. Untrusted —
            validated against an identifier-shaped charset before it is
            ever used to build a path.
        record_id: The record id named in the pointer. Untrusted —
            validated against the exact ``uuid.UUID.hex`` shape
            :func:`_record_id_for` mints before it is ever used to build a
            path.
        config: Resolved ``athenaeum.yaml`` — the SAME config the write
            path was called with, so class definitions and
            ``storage.mapping`` resolve identically.
        knowledge_root: Root of the knowledge base (parent of ``wiki/``) —
            the SAME root the write path was called with.
        caller_audience: Read-scope pin (issue athenaeum#312/#538
            convention). ``None`` (default) is the owner/trusted caller.

    Returns:
        The original value substituted at routing time, or ``None`` when
        nothing is resolvable for any reason above.
    """
    try:
        if not isinstance(record_id, str) or not isinstance(sensitivity_class, str):
            return None

        wanted_record_id = record_id.strip()
        if not _RECORD_ID_RE.match(wanted_record_id):
            return None

        wanted_class = sensitivity_class.strip()
        if not wanted_class or not _SENSITIVITY_CLASS_RE.match(wanted_class):
            return None

        # Gate BEFORE touching the specific record's bytes on disk — an
        # unauthorized caller never causes this function to read (or even
        # stat) the record file it was refused. Resolving `available_classes`
        # itself does no I/O beyond the already-in-hand `config` dict.
        try:
            classes = available_classes(config)
        except SensitivityConfigError:
            return None
        sensitivity_class_obj = classes.get(wanted_class)
        if sensitivity_class_obj is None:
            return None
        policy = sensitivity_class_obj.read_policy
        policy_meta: dict[str, Any] = {
            "access": policy.access,
            "audience": list(policy.audience),
        }
        if not is_page_authorized(policy_meta, caller_audience):
            return None

        # athenaeum#3176: a served process asks whether this request's
        # principal holds read on the record's vault, before the record is
        # looked up. A refusal resolves to nothing, like every other failure.
        try:
            targets = _vault_targets_for_class(
                wanted_class, config, knowledge_root, for_read=True
            )
        except SensitivityRoutingError:
            return None
        if vault_read_allowed is not None and not vault_read_allowed(targets[0][0]):
            return None
        vault_root = targets[0][1]

        vault_root_resolved = vault_root.resolve()
        record_path = (vault_root / f"{wanted_record_id}.md").resolve()
        # Defense-in-depth: the charset check above already rules out any
        # segment that could escape `vault_root`, but this containment check
        # (the same `Path.is_relative_to` idiom `mcp_server.py`'s raw-write
        # guard uses, never a string-prefix compare) holds even if that
        # charset is ever loosened without this check being revisited.
        if not record_path.is_relative_to(vault_root_resolved):
            return None
        if not record_path.is_file():
            return None

        try:
            text = record_path.read_text(encoding="utf-8")
        except OSError:
            return None

        record_meta, body = parse_frontmatter(text)
        if not record_meta.get(SENSITIVITY_ROUTED_FLAG):
            return None
        recorded_class = str(record_meta.get("sensitivity_class", "")).strip()
        if recorded_class != wanted_class:
            # The record this id resolved to (under the vault root already
            # scoped to `wanted_class`) does not itself claim that class —
            # a `storage.mapping` misconfiguration could in principle make
            # two classes share a physical root; refuse rather than trust
            # path placement alone (AC3's "different class" failure mode).
            return None

        return _value_from_vault_body(body)
    except Exception:  # noqa: BLE001 — never-raise contract is the anti-probing
        # boundary: a distinguishable failure would leak whether a record
        # exists. Belt-and-suspenders (see docstring): no anticipated path
        # reaches here, but this function's contract is "value or nothing
        # resolvable — never raise", full stop.
        return None


#: Matches a pointer of either wording (see :func:`_pointer_text`): the class
#: and record id are captured; everything after the id up to ``]`` is prose.
_POINTER_RE = re.compile(
    rf"\[{POINTER_PREFIX}:([A-Za-z0-9_-]+):([0-9a-f]{{32}}) [^\]]*\]"
)


def resolve_pointers_in_text(
    text: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    caller_audience: set[str] | None = None,
    vault_read_allowed: Callable[[str], bool] | None = None,
) -> str:
    """Replace each resolvable pointer in *text* with its value (athenaeum#3163).

    The entity read's ``include_sensitive`` path: a page body carries
    ``[sensitive:<class>:<record_id> ...]`` pointers, and this swaps each for
    the routed value via :func:`resolve_sensitive_record`, under the same
    ``read_policy`` gate. A pointer that does not resolve for any reason is
    left in place, so the caller cannot tell "not authorized" from "gone".
    """

    def _sub(match: re.Match[str]) -> str:
        value = resolve_sensitive_record(
            match.group(1),
            match.group(2),
            config,
            knowledge_root,
            caller_audience=caller_audience,
            vault_read_allowed=vault_read_allowed,
        )
        return match.group(0) if value is None else value

    return _POINTER_RE.sub(_sub, text)
