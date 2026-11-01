# SPDX-License-Identifier: Apache-2.0
"""Read-only contact-surface lint and attribution backfill report (issue athenaeum#2218).

Child I of epic athenaeum#2192, designed in
``docs/design/attribution-verification.md`` §10.4. Builds the
``contact-surface-lint`` command that ``pii._log_collision_summary``'s hint
already names.

**What this module owns.** :func:`run_contact_surface_lint`, the one entry
point :mod:`athenaeum._cmd_contact_surface_lint` calls. It is READ-ONLY in
the sense design §10.4 means: it never writes a contact record and never
retires or folds anything (unlike ``athenaeum repair --contact-records``,
whose ``--apply`` DOES write). It writes exactly two things on the excluded
surface, both append-only evidence rather than a surface mutation: an
attribution-log row per (value, uid) pair checked (``--attribution``, via
:func:`athenaeum.pii_attribution.check_contact_attribution` with
``writer="lint"``) and one observe-report row per run (``--attribution``
only, :func:`athenaeum.pii_attribution.append_attribution_observe_report`).
It never opens an excluded-surface file itself: every read goes through
:mod:`athenaeum.pii`, :mod:`athenaeum.pii_attribution` or
:mod:`athenaeum.contact_record_repair` accessors, matching the "one way in"
rule (``docs/design/one-way-in-one-way-out.md`` §4.1) every other reader of
this surface already follows.

Three base reports (design §10.4's first bullet list), built from one
:class:`athenaeum.pii_attribution.AttributionContext` so a CLI run pays the
corpus/surface scans once:

- **pageless_records** -- reuses athenaeum#2217's own page-less detection,
  :func:`athenaeum.contact_record_repair.plan_contact_record_repair`'s
  ``pageless_records`` field (a uid with no live page AND no merge record),
  rather than re-deriving "does this uid have a page" a second time.
- **shared_values** -- values :func:`athenaeum.pii.identifiers_on_record`
  finds on two or more uids with a live page (seam D2's own holder rule --
  a pageless uid is reported under ``pageless_records`` instead), broken
  down by each (value, uid) pair's
  ``field_sources`` provenance ``type`` (or ``"unsourced"`` when no
  ``field_sources`` entry names that value). Deliberately the SAME
  identifier universe :mod:`athenaeum.pii_attribution`'s own D2/D4 checks
  and :class:`athenaeum.pii.ExcludedRecordIndex` use (``emails`` /
  ``former_emails`` / ``alt_emails`` -- not ``phones``, which neither of
  those already walks either): a lint that found MORE sharing than the seam
  itself can see would raise findings the seam can never act on.
- **observed_source_mismatch** -- ``observed`` values
  (:data:`athenaeum.pii.USAGE_CLASS_OBSERVED`) whose
  :func:`athenaeum.pii.classification_for_value` source still resolves to a
  readable file that does not contain the value, via
  :func:`athenaeum.pii_attribution.resolve_source_value_presence` -- the
  same source-resolution primitives C3 (:mod:`athenaeum.pii_attribution`'s
  ``_check_c3``) uses, so "does this source contain the value" is answered
  identically everywhere it is asked.

Everything printed or JSON-serialized is a count, a uid, a ``value_key``
(the keyed hash :func:`athenaeum.pii_attribution.value_key_for` already
computes for logs and memos) or a source path/type string -- never a raw
contact value. This module never calls ``mask_contact_address`` because it
never HOLDS an unmasked value past the point of hashing it.

``--attribution`` adds :func:`run_attribution_sweep`: the seam, in REPORT
mode, over every (value, uid) pair the base scan already walked, counted by
disposition, by fired check id, and (for suspects) by question class
(:func:`athenaeum.pii_attribution.question_class_for`). Those per-class
suspect counts ARE design §9.3's "per-class shadow counts" -- every suspect
an observe-mode writer parks is a shadow entry (design §9.1), and this sweep
counts exactly that population for the run it covers, so the observe report
(:func:`_persist_observe_report`) reuses the sweep's own tally rather than a
second count.

``--escalate`` (opt-in) runs athenaeum#2212's existing
:func:`athenaeum.pii_attribution.escalate_shadow_suspects` over the suspects
ALREADY parked on the surface by the real writers -- not suspects this
sweep itself finds; this sweep calls the pure, non-parking
``check_contact_attribution`` seam directly (design §4.1: parking is the
WRITER's job, and this module is not one), so it never adds to that pool
itself. The cap is the issue's own named knob,
:func:`athenaeum.librarian.librarian_max_api_calls` (the general run-wide
LLM-call budget, default 800) -- deliberately NOT
``resolve_attribution_max_escalations_per_run`` (default 25), which design
§13's cost table sizes for a *continuous nightly trickle* through a
backlog, not this command's *one-time backfill report over a whole
surface* ("order 10^3, once"). A per-run cap of 25 would need dozens of
invocations to ever clear such a backlog.

Layering: L4. Imports :mod:`athenaeum.contact_record_repair` (L4 sibling,
for the page-less detection reuse), :mod:`athenaeum.pii_attribution` (L3),
:mod:`athenaeum.pii` (L3), :mod:`athenaeum.contact_record_fold` (L3, for
``resolve_contacts_root``) and :mod:`athenaeum.models` (L1). Never imported
by anything below L4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum import pii, pii_attribution
from athenaeum.contact_record_fold import resolve_contacts_root
from athenaeum.contact_record_repair import plan_contact_record_repair
from athenaeum.models import parse_frontmatter, resolve_page_type

if TYPE_CHECKING:
    from athenaeum.models import TokenUsage
    from athenaeum.provider import LLMBackend


class ContactSurfaceLintError(RuntimeError):
    """Raised when the contacts surface cannot be resolved for linting.

    Distinct from a FINDING (a dirty report is a successful run with
    something to report); this is a setup failure -- no surface to read at
    all. The CLI maps this to the issue's error exit code (2).
    """


@dataclass(frozen=True)
class SharedValueEntry:
    """One value held by two or more uids (design §10.4's ``shared_values``).

    ``value_key`` is :func:`athenaeum.pii_attribution.value_key_for`'s keyed
    hash -- never the value itself. ``source_types`` is
    ``{uid: source_type}`` for every holder, where ``source_type`` is a
    ``field_sources`` entry's ``source.type`` when one names this value on
    that uid's record, else ``"unsourced"``.
    """

    value_key: str
    uids: tuple[str, ...]
    source_types: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservedMismatchEntry:
    """One ``observed`` value whose cited source no longer contains it.

    ``source_ref`` is the classification's own ``source`` (a raw file path
    string, or a provenance dict naming one) -- never the contact value.
    """

    value_key: str
    uid: str
    source_ref: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class SurfaceLintReport:
    """The base, deterministic surface report (design §10.4's first bullet list)."""

    records_scanned: int = 0
    uids_scanned: int = 0
    pageless_uids: tuple[str, ...] = ()
    shared_values: tuple[SharedValueEntry, ...] = ()
    observed_source_mismatch: tuple[ObservedMismatchEntry, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing was found -- a clean surface."""
        return not (
            self.pageless_uids or self.shared_values or self.observed_source_mismatch
        )

    @property
    def shared_value_source_type_counts(self) -> dict[str, int]:
        """``{source_type: count}`` over every (value, uid) pair in :attr:`shared_values`."""
        counts: dict[str, int] = {}
        for entry in self.shared_values:
            for source_type in entry.source_types.values():
                counts[source_type] = counts.get(source_type, 0) + 1
        return counts

    def counts(self) -> dict[str, int]:
        return {
            "records_scanned": self.records_scanned,
            "uids_scanned": self.uids_scanned,
            "pageless_records": len(self.pageless_uids),
            "shared_values": len(self.shared_values),
            "observed_source_mismatch": len(self.observed_source_mismatch),
        }


@dataclass(frozen=True)
class AttributionSweepReport:
    """What one ``--attribution`` report-mode sweep found (design §10.4's second bullet).

    Deliberately carries no separate pageless-uid tally: see
    :func:`run_attribution_sweep`'s own docstring ("measured, not
    assumed") for why a pageless target's dispositions are not a second
    finding hiding under a different name here.
    """

    pairs_considered: int = 0
    by_disposition: dict[str, int] = field(default_factory=dict)
    by_check: dict[str, int] = field(default_factory=dict)
    by_question_class: dict[str, int] = field(default_factory=dict)


def _field_source_type_for(meta: dict[str, Any], identifier: str) -> str:
    """The ``field_sources`` provenance ``type`` recorded for *identifier* on *meta*.

    ``"unsourced"`` when no ``field_sources`` entry across
    :data:`athenaeum.pii.RECORDABLE_CONTACT_FIELDS` names this value, or
    when the entry's ``source`` carries no ``type`` (a plain string, or a
    dict with no ``type`` key) -- design §10.4's own vocabulary: "broken
    down by per-value provenance source type (``field_sources`` type, or
    ``unsourced``)".
    """
    raw = meta.get("field_sources")
    if not isinstance(raw, dict):
        return "unsourced"
    wanted = pii.normalize_identifier(identifier)
    for field_name in pii.RECORDABLE_CONTACT_FIELDS:
        entries = raw.get(field_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if pii.normalize_identifier(str(entry.get("value", ""))) != wanted:
                continue
            source = entry.get("source")
            if isinstance(source, dict):
                source_type = source.get("type")
                if isinstance(source_type, str) and source_type.strip():
                    return source_type.strip()
            return "unsourced"
    return "unsourced"


def _record_identifiers(meta: dict[str, Any]) -> list[str]:
    """Every address *meta* lists, including a slug-keyed record's bare
    ``identifier:`` (issue athenaeum#2218's own completeness carve-out --
    mirrors :func:`athenaeum.pii._contact_fact_identifier_keys`'s
    documented fallback for the exact same shape, but reads only the public
    :func:`athenaeum.pii.identifiers_on_record` plus this one extra field,
    so a slug-keyed bounce record's own address is never silently invisible
    to a surface-wide sweep).
    """
    found = pii.identifiers_on_record(meta)
    if found:
        return found
    own = meta.get("identifier")
    if isinstance(own, str) and own.strip():
        return [own.strip()]
    return []


def _target_name_type(
    context: "pii_attribution.AttributionContext", uid: str
) -> tuple[str | None, str | None]:
    """Resolve ``(name, type)`` for *uid*'s wiki page, or ``(None, None)``.

    The same two frontmatter fields :class:`athenaeum.models.EntityIndex`
    itself reads per page (``read_page_record``); this does not add a
    second index, it reads the one page :meth:`EntityIndex.get_by_uid`
    already names.
    """
    if context.entity_index is None:
        return None, None
    path = context.entity_index.get_by_uid(uid)
    if path is None:
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    meta, _ = parse_frontmatter(text)
    if not isinstance(meta, dict):
        return None, None
    name = meta.get("name")
    page_type = resolve_page_type(meta)
    return (
        name if isinstance(name, str) and name else None,
        page_type if page_type else None,
    )


def scan_contact_surface(
    knowledge_root: Path, context: "pii_attribution.AttributionContext"
) -> SurfaceLintReport:
    """The base, deterministic surface report (design §10.4). Read-only."""
    plan = plan_contact_record_repair(Path(knowledge_root), context.contacts_root)
    pageless_uids = tuple(uid for uid, _paths in plan.pageless_records)

    idx = context.excluded_index
    value_to_uids: dict[str, list[str]] = {}
    value_to_source_types: dict[str, dict[str, str]] = {}
    mismatches: list[ObservedMismatchEntry] = []
    # (normalized_value, uid) pairs already contributed a mismatch entry.
    # Two record FILES sharing one uid (a store defect ExcludedRecordIndex
    # itself warns about -- `_warn_collisions`) can both list the same
    # identifier; without this, the SAME (value, uid) pair would be
    # double-counted here exactly as PR review caught it being
    # double-counted in `run_attribution_sweep` below.
    mismatch_pairs_seen: set[tuple[str, str]] = set()
    records_scanned = 0
    uids = idx.uids()
    for uid in uids:
        # Only a uid with a live page counts as a `shared_values` holder --
        # the same rule seam D2 applies ("a record whose uid has no live wiki
        # page does not count"; `pii_attribution._has_live_page`, including
        # its "no EntityIndex means live" default). A pageless uid is already
        # reported under `pageless_records`, or -- when a merge absorbed it --
        # is `repair --contact-records`' fold work; counting it here too
        # would report one defect twice (PR review finding).
        holder_counts = (
            context.entity_index is None
            or context.entity_index.get_by_uid(uid) is not None
        )
        for path in idx.all_by_uid(uid):
            records_scanned += 1
            meta = pii.read_bounce_record(path)
            for identifier in _record_identifiers(meta):
                key = pii.normalize_identifier(identifier)
                if not key:
                    continue
                if holder_counts:
                    holders = value_to_uids.setdefault(key, [])
                    if uid not in holders:
                        holders.append(uid)
                    value_to_source_types.setdefault(key, {})[uid] = (
                        _field_source_type_for(meta, identifier)
                    )

                pair = (key, uid)
                if pair in mismatch_pairs_seen:
                    continue
                classification = pii.classification_for_value(meta, identifier)
                if classification.usage_class == pii.USAGE_CLASS_OBSERVED:
                    presence = pii_attribution.resolve_source_value_presence(
                        classification.source, identifier
                    )
                    if presence is False:
                        mismatch_pairs_seen.add(pair)
                        mismatches.append(
                            ObservedMismatchEntry(
                                value_key=pii_attribution.value_key_for(context, identifier),
                                uid=uid,
                                source_ref=classification.source,
                            )
                        )

    shared_values = tuple(
        SharedValueEntry(
            value_key=pii_attribution.value_key_for(context, value),
            uids=tuple(holder_uids),
            source_types=dict(value_to_source_types.get(value, {})),
        )
        for value, holder_uids in value_to_uids.items()
        if len(holder_uids) >= 2
    )

    return SurfaceLintReport(
        records_scanned=records_scanned,
        uids_scanned=len(uids),
        pageless_uids=pageless_uids,
        shared_values=shared_values,
        observed_source_mismatch=tuple(mismatches),
    )


def run_attribution_sweep(
    context: "pii_attribution.AttributionContext",
    config: dict[str, Any] | None,
) -> AttributionSweepReport:
    """``--attribution``: the seam, in report mode, over every (value, uid) pair.

    Writes exactly one attribution-log row per pair
    (:func:`athenaeum.pii_attribution.check_contact_attribution`'s own
    behaviour, ``writer=`` :data:`athenaeum.pii_attribution.WRITER_LINT`).
    Never parks a suspect: this module is not a writer deciding how to act
    on a verdict (design §4.1) -- it only counts.

    **One pair, one verdict, one log row (PR review finding).** A uid with
    two record FILES that both list the same identifier -- a store defect
    :class:`athenaeum.pii.ExcludedRecordIndex` itself warns about via
    ``_warn_collisions`` -- would otherwise run the seam twice for the SAME
    (value, uid) pair, double-counting it in every tally below and writing
    two attribution-log rows for one logical pair. The issue's own AC says
    "over every (value, uid) pair on the surface", and a pair visited twice
    is still ONE pair, so a per-uid ``seen`` set of normalized values
    (:func:`athenaeum.pii.normalize_identifier` -- the one normaliser this
    seam already uses everywhere else, not a second one) keeps each pair to
    exactly one verdict, first-record-wins (the same "first match in scan
    order wins" convention :class:`~athenaeum.pii.ExcludedRecordIndex`
    itself follows).

    **Pageless uids are deliberately NOT excluded** (PR review, Seer finding
    rejected on adjudication): design §10.4 says "every (value, uid) pair
    on the surface", and a value filed before the seam existed whose uid has
    since lost its page is exactly the kind of value this backfill report
    exists to surface, not the kind to hide.

    **Measured, not assumed: a pageless target's ``(None, None)``
    name/type does NOT skew its values toward ``suspect``.** D3 (not a
    person) is the only check gated on ``target_name``/``target_type`` at
    all, and BOTH of its branches require one of them to be truthy to fire
    -- so a pageless uid (which has neither) can never trigger D3, making
    it if anything LESS likely to read ``suspect`` than a live-page target,
    never more. Verified directly: an isolated value with no other
    evidence returns ``pass`` identically for a live-page target and a
    pageless one, and a value that triggers D3 for an explicit
    ``target_type="company"`` or a name-matched company page does NOT
    trigger anything for a pageless target with no name/type to match.
    Every other check (D1, D2, D4, C1-C3) is independent of the CURRENT
    target's own name/type. So there is no second finding hiding under a
    different name here, and no separate tally is added.
    """
    idx = context.excluded_index
    by_disposition: dict[str, int] = {}
    by_check: dict[str, int] = {}
    by_question_class: dict[str, int] = {}
    pairs_considered = 0
    for uid in idx.uids():
        target_name, target_type = _target_name_type(context, uid)
        seen_values: set[str] = set()
        for path in idx.all_by_uid(uid):
            meta = pii.read_bounce_record(path)
            for identifier in _record_identifiers(meta):
                normalized = pii.normalize_identifier(identifier)
                if not normalized or normalized in seen_values:
                    continue
                seen_values.add(normalized)
                classification = pii.classification_for_value(meta, identifier)
                verdict = pii_attribution.check_contact_attribution(
                    identifier,
                    target_uid=uid,
                    target_name=target_name,
                    target_type=target_type,
                    source_ref=classification.source,
                    writer=pii_attribution.WRITER_LINT,
                    context=context,
                )
                pairs_considered += 1
                by_disposition[verdict.disposition] = (
                    by_disposition.get(verdict.disposition, 0) + 1
                )
                for check in verdict.checks:
                    by_check[check.check_id] = by_check.get(check.check_id, 0) + 1
                if verdict.disposition == pii_attribution.DISPOSITION_SUSPECT:
                    question_class = pii_attribution.question_class_for(verdict.checks)
                    by_question_class[question_class] = (
                        by_question_class.get(question_class, 0) + 1
                    )
    return AttributionSweepReport(
        pairs_considered=pairs_considered,
        by_disposition=by_disposition,
        by_check=by_check,
        by_question_class=by_question_class,
    )


def persist_observe_report(
    context: "pii_attribution.AttributionContext",
    config: dict[str, Any] | None,
    attribution: AttributionSweepReport,
) -> "pii_attribution.ObserveReport":
    """Write the design §9.3 observe report for this run (issue athenaeum#2218 AC3).

    ``per_class_shadow_counts`` is *attribution*'s own ``by_question_class``
    tally (every suspect counted there IS a shadow entry in observe mode --
    design §9.1). ``model_by_class`` / ``prompt_sha256_by_class`` cover
    EVERY question class, not only the ones this sweep happened to see a
    suspect in, so a sibling reader (athenaeum#2220's ``auto`` preflight)
    can compare "the model/sha in effect" for a class even when this run
    found zero suspects for it.
    """
    model_by_class: dict[str, str] = {}
    prompt_sha256_by_class: dict[str, str] = {}
    for question_class in pii_attribution.QUESTION_CLASSES:
        model, prompt_sha256 = pii_attribution.attribution_call_identity(
            question_class, config
        )
        model_by_class[question_class] = model
        prompt_sha256_by_class[question_class] = prompt_sha256
    return pii_attribution.append_attribution_observe_report(
        context.contacts_root,
        per_class_shadow_counts=dict(attribution.by_question_class),
        model_by_class=model_by_class,
        prompt_sha256_by_class=prompt_sha256_by_class,
    )


@dataclass(frozen=True)
class ContactSurfaceLintResult:
    """Everything one ``athenaeum contact-surface-lint`` invocation produced."""

    surface: SurfaceLintReport
    attribution: AttributionSweepReport | None = None
    observe_report: "pii_attribution.ObserveReport | None" = None
    escalation: "pii_attribution.ShadowEscalationSummary | None" = None

    @property
    def ok(self) -> bool:
        """True when there is nothing an operator needs to act on.

        The base surface must be clean, AND (when ``--attribution`` ran) the
        sweep must have found no suspects -- a suspect is exactly the
        finding design §10.4 built this report to surface.
        """
        if not self.surface.ok:
            return False
        if self.attribution is not None and self.attribution.by_disposition.get(
            pii_attribution.DISPOSITION_SUSPECT, 0
        ):
            return False
        return True


def run_contact_surface_lint(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    *,
    contacts_root: Path | None = None,
    run_attribution: bool = False,
    escalate: bool = False,
    client: "LLMBackend | None" = None,
    usage: "TokenUsage | None" = None,
    escalate_limit: int | None = None,
) -> ContactSurfaceLintResult:
    """Run the lint (``run_attribution=False``) or the full backfill report.

    Raises :class:`ContactSurfaceLintError` when the ``pii`` class is not
    mapped to an excluded surface, or when it resolves to the wiki root
    itself (the same refusal :func:`athenaeum._cmd_repair._cmd_repair_contact_records`
    applies -- scanning the wiki as if it were the excluded surface would
    read wiki pages as if they were contact records).
    """
    root = Path(knowledge_root)
    resolved_contacts_root = (
        Path(contacts_root)
        if contacts_root is not None
        else resolve_contacts_root(root, config)
    )
    if resolved_contacts_root is None:
        raise ContactSurfaceLintError(
            "The `pii` class is not mapped to an excluded surface "
            "(storage.mapping.pii); there is no contacts surface to lint."
        )
    if resolved_contacts_root.resolve() == (root / "wiki").resolve():
        raise ContactSurfaceLintError("Refusing: the contacts root is the wiki root.")

    context = pii_attribution.build_attribution_context(
        root, config, contacts_root=resolved_contacts_root
    )
    surface = scan_contact_surface(root, context)

    attribution_report: AttributionSweepReport | None = None
    observe_report: pii_attribution.ObserveReport | None = None
    escalation_summary: pii_attribution.ShadowEscalationSummary | None = None
    if run_attribution:
        attribution_report = run_attribution_sweep(context, config)
        observe_report = persist_observe_report(context, config, attribution_report)
        if escalate:
            escalation_summary = pii_attribution.escalate_shadow_suspects(
                context,
                client=client,
                config=config,
                usage=usage,
                limit=escalate_limit,
            )

    return ContactSurfaceLintResult(
        surface=surface,
        attribution=attribution_report,
        observe_report=observe_report,
        escalation=escalation_summary,
    )


__all__ = [
    "ContactSurfaceLintError",
    "SharedValueEntry",
    "ObservedMismatchEntry",
    "SurfaceLintReport",
    "AttributionSweepReport",
    "ContactSurfaceLintResult",
    "scan_contact_surface",
    "run_attribution_sweep",
    "persist_observe_report",
    "run_contact_surface_lint",
]
