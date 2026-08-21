# SPDX-License-Identifier: Apache-2.0
"""Sensitive-value routing/redaction mechanism (issue athenaeum#1023).

Slice 2/4 of athenaeum#949's design note
(`docs/sensitivity-value-routing.md`). :func:`route_sensitive_values` scans
text for :func:`athenaeum.sensitivity.classify` matches, routes each
configured-on match's value to the secret vault (the existing ``excluded``
storage surface, athenaeum#429), and returns the text with every routed
span replaced by a resolvable pointer. **This module is standalone in this
slice** — nothing calls it yet. Wiring it into the top of
:func:`athenaeum.librarian.process_one`, before ``tier0_passthrough`` and
both LLM exposures, is athenaeum#1025 (slice 4); reading a pointer's value
back via ``resolve_sensitive_record`` is athenaeum#1024 (slice 3) and is
NOT implemented here — the pointer text below names that function because
the design note's proposed pointer format (§1) names it, not because it
exists yet in this repo.

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

    The existing excluded-surface read path (``with_pii``/``read_entity``,
    ``mcp_server._excluded_block_for_hit``) and the settled contact-record
    routing story (``docs/field-corrections.md`` §7.1, issue athenaeum#872)
    are both uid-keyed BY CONSTRUCTION — they resolve through
    :class:`athenaeum.pii.ExcludedRecordIndex.by_uid`. This stage runs
    during the RAW sweep, before Tier 2/3 classification has decided
    whether the raw file even becomes a page, let alone minted that page's
    uid — so a uid-keyed join has nothing to key on for most of this
    stage's own input. Design note §2 evaluates and rejects the two
    alternatives (scoping to uid-bearing pages only; an advisory-only
    pointer with no read path) and proposes disposition (b): a NEW,
    record-keyed read path, independent of entity uid, resolved later by
    a function this module names but does not implement (athenaeum#1024).
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

**AC1 — composition with ``docs/field-corrections.md`` §7.1.** §7.1 settled
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
AGAINST at raw-sweep time (see AC3 above). Per ``docs/field-corrections.md``
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
loud, not a silent leak. With no explicit ``storage.mapping`` entry for the
class (the common case), the vault root defaults directly to the built-in
:data:`athenaeum.storage.EXCLUDED` adapter — NOT the generic storage
layer's own default (:data:`athenaeum.storage.WIKI_MARKDOWN_EMBEDDED`,
intentionally "byte-identical to today" for every OTHER class) — because
"undeclared" must mean "safe" for a security vault target, the opposite of
what "undeclared" means for an ordinary entity class.

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
(``with_pii=True``) the way this module's pointer names its own read
function — that sub-question is explicitly left with a follow-on issue,
not resolved here or by this module. See athenaeum#1023's issue thread for
the full disposition.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_sensitivity_routing, resolve_storage_mapping
from athenaeum.models import render_frontmatter
from athenaeum.sensitivity import ClassifiedMatch, SensitivityConfigError, classify
from athenaeum.storage import EXCLUDED, StorageConfigError, resolve_adapter_for_class

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


def _vault_root_for_class(
    sensitivity_class: str,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Path:
    """Resolve the on-disk vault root for *sensitivity_class* — fail closed.

    See this module's docstring, "Unsafe vault surface, fail closed."
    """
    mapping = resolve_storage_mapping(config)
    if sensitivity_class.strip() in mapping:
        try:
            adapter = resolve_adapter_for_class(sensitivity_class, config)
        except StorageConfigError as exc:
            raise SensitivityRoutingError(
                f"sensitivity class {sensitivity_class!r} storage.mapping is invalid: {exc}"
            ) from exc
        if adapter.corpus_policy.in_corpus:
            raise SensitivityRoutingError(
                f"storage.mapping routes sensitivity class {sensitivity_class!r} "
                f"to adapter {adapter.name!r}, which participates in the "
                "corpus — refusing to write a routed sensitive value there "
                "(fail closed)"
            )
        root = adapter.resolve_root(knowledge_root)
    else:
        root = EXCLUDED.resolve_root(knowledge_root)
    return root / "sensitivity" / sensitivity_class


def _record_id_for(raw_ref: str, sensitivity_class: str, start: int, end: int) -> str:
    """Deterministic record id — derived from metadata only, never the value (AC11)."""
    name = f"{raw_ref}|{sensitivity_class}|{start}|{end}"
    return uuid.uuid5(_RECORD_ID_NAMESPACE, name).hex


def _pointer_text(sensitivity_class: str, record_id: str) -> str:
    """The resolvable, non-leaking pointer substituted for a routed span (AC2).

    Names ``resolve_sensitive_record`` per the design note's proposed
    format (§1) even though that function is not implemented until
    athenaeum#1024 (slice 3) — the pointer text is fixed by the design, not
    by this module's own scope.
    """
    return (
        f"[{POINTER_PREFIX}:{sensitivity_class}:{record_id} — value withheld; "
        "resolve via athenaeum.sensitivity_routing.resolve_sensitive_record()]"
    )


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
) -> str:
    record_id = _record_id_for(raw_ref, sensitivity_class, start, end)
    vault_root = _vault_root_for_class(sensitivity_class, config, knowledge_root)
    vault_root.mkdir(parents=True, exist_ok=True)
    record_path = vault_root / f"{record_id}.md"
    atomic_write_text(
        record_path,
        _render_vault_record(
            record_id=record_id,
            sensitivity_class=sensitivity_class,
            source_ref=raw_ref,
            value=value,
        ),
    )
    return _pointer_text(sensitivity_class, record_id)


def route_sensitive_values(
    *,
    raw_ref: str,
    text: str,
    frontmatter: dict[str, Any] | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> str:
    """Scan *text*, route every matching, routable value, return redacted text.

    Standalone in this slice — not yet called from anywhere in this repo
    (athenaeum#1025 wires it into the top of
    :func:`athenaeum.librarian.process_one`). *raw_ref* is a stable,
    non-secret reference to the raw file being screened (its path relative
    to the knowledge root, for example) — used only to derive each match's
    deterministic ``record_id`` (AC11) and to name the file in any raised
    error message.

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
