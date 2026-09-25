# SPDX-License-Identifier: Apache-2.0
"""Page audit pass — read-and-reason over wiki pages (issue athenaeum#1624).

**Foundation module.** Three sibling issues (athenaeum#1627 audit-on-touch,
athenaeum#1630 stale-page review) key off :func:`audit_page` — the single
reusable, structured entry point this module exists to provide. Its
signature is deliberately narrow and I/O-free: given an already-built LLM
*client* and one page's already-parsed frontmatter dict + body text, it
returns one :class:`AuditVerdict`. It never reads or writes a file itself,
so a caller (this module's own :func:`build_audit_report`, or a future
librarian call site) decides entirely on its own when/whether to persist
the result. Read :func:`audit_page` first if you are a later lane wiring
this in.

Stamps two new frontmatter fields on a page:

- ``last_audited`` — ISO-8601 UTC timestamp of the audit pass.
- ``audit_version`` — the audit prompt/schema version the pass ran
  against (:data:`AUDIT_VERSION`).

For each declared-empty field named by a page's PENDING model-derivation
migration (issue athenaeum#1628 decision 4 — read from
:mod:`athenaeum.schema_migrations`'s registry via
:func:`schema_migrations.pending_migrations`, never a hard-coded tuple;
:data:`COORDINATE_FIELDS` below is kept only as a value DERIVED from that
same registry, for existing importers — today that is the v1->v2 entry's
``valid_from`` / ``valid_until`` / ``claimed_scope``, the kernel dimensions
:mod:`athenaeum.dimensions` already reads), the per-page call either fills
a value determinable from the page's own body and cited sources, or
records an ``undeterminable`` reason in the ``audit_findings:`` frontmatter
map — distinguishable from a page that was never checked at all (no
``last_audited``, no ``audit_findings`` entry). A populated coordinate
value is NEVER overwritten, either by the LLM prompt (only empty fields are
ever asked about) or by the write path (:func:`apply_audit_report`
re-checks emptiness immediately before writing, defending against a race
between scan and apply).

Also stamps ``schema_version`` (issue athenaeum#1628 decision 4): once every
field a pending MODEL migration names is either filled or recorded
``undeterminable``, :func:`apply_verdict_to_meta` bumps the page's
``schema_version`` to the highest version the registry says it has now
earned (:func:`_advance_schema_version`) — never partially, and never past
an unresolved migration (see that function's own docstring). ``schema_version``
is a SEPARATE marker from :data:`AUDIT_VERSION` (which still versions only
the audit prompt/response shape, unchanged by this) — see
:mod:`athenaeum.schema_migrations`'s module docstring for the distinction.

The same call also returns a GENERIC retirement-candidate flag (issue
athenaeum#1667 Decision 2): a page is a candidate only when it states NO claim
at all, or when it duplicates another page (duplicate detection is
deterministic, in code — see :func:`_find_duplicate_reasons` — not a
model judgment). Restating/summarizing a cited source is explicitly NOT a
criterion, and a light source page (a source summary plus validity info)
is never a candidate on that basis. Four sub-rules refine the no-claim
trigger (issue athenaeum#1849): a person page holding only a name, or a name
plus at most one affiliation line, is a placeholder awaiting enrichment
and is never a candidate on that basis; a page recording a dated
engagement or relationship outcome states a claim even alongside CRM /
sales-pipeline metadata; a page whose only content beyond its name is
CRM / sales-pipeline metadata or pipeline-list membership IS a candidate;
and a page whose own text declares the entity itself spurious (for
example an artifact of parsing a filename) IS a candidate. The check and
its parsing carry no source-type, adapter-name, or board-title string
anywhere — see this module's own text below, and
``tests/test_audit.py``'s ``git grep`` regression test. Out of scope
(athenaeum#1624's own "Out of scope" section): this command never deletes or
retires a page, and never fills ``subject`` (athenaeum#1244 / athenaeum#1615 own
that field).

**Transitory-class decay stamping (issue athenaeum#1713).** A page whose
``type:`` (and, for one class, body content) matches one of four
operator-named transitory classes — an incident record, a
deployment-status page, an operational source note, or a reference page
mirroring a GitHub issue (operator decision recorded 2026-09-16 on
athenaeum#1626) — gets ``bucket: daily`` plus a ``valid_until`` written
alongside the ordinary coordinate fills, via the SAME never-overwrite rule
:func:`apply_verdict_to_meta` already enforces for every other coordinate
(see :func:`_transitory_page_class` / :data:`TRANSITORY_PAGE_CLASSES`).
When the page's own body states its own end date, the EXISTING
model-driven ``valid_until`` fill (the same one every other page gets
asked about — :func:`audit_page` / :func:`parse_audit_response`) is used
as-is; otherwise a configurable default horizon
(:func:`athenaeum.config.resolve_audit_transitory_horizon_days`,
recommended 90 days) measured from the page's own ``last_audited`` stamp
is used instead. **This is not a second decay mechanism** — it is one
write into fields athenaeum#904 already built the read and sweep sides
for: recall's currency ranking
(:func:`athenaeum.mcp_server._is_deprioritized_for_currency`)
deprioritizes the page once ``valid_until`` passes, and a later
``athenaeum decay-sweep --apply`` run (:mod:`athenaeum.decay_sweep`, a
separate, manually-invoked command this module never calls or schedules)
will eventually ARCHIVE the page out of the live tree via the existing
two-commit ``git rm`` (recoverable via git history). Giving a page an
expiry here and it eventually being removed from the live tree are the
SAME operator decision, not two: there is no narrower value that opts a
page into read-time deprioritization alone without also making it
eligible for that eventual archival.

``subject`` is deliberately never touched here (issue athenaeum#1624 explicitly
excludes it; athenaeum#1244/athenaeum#1615 own subject resolution by meaning).

Write path: pages are edited via the SAME ``parse_frontmatter`` ->
mutate-dict -> ``render_frontmatter`` -> ``atomic_write_text`` idiom every
other in-place page editor in this codebase uses (see e.g.
``corrections.py``, ``person_registry.py``, ``pii.py``) — never through
``athenaeum.templates`` (those are user-facing scaffolds, not a writer;
see that subpackage's own docstring).

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.schema_migrations`
(L0, module scope — the migration registry this module's fields-to-determine
and ``schema_version`` bump both read) and :mod:`athenaeum.batch` (L4, for the
``--batch`` transport: :class:`~athenaeum.batch.BatchRequest` /
:func:`~athenaeum.batch.execute_batch` — both already-generic building
blocks, so this module adds nothing to ``batch.py`` itself and never
touches its tier-3 MERGE request builder) and :mod:`athenaeum.provider` /
:mod:`athenaeum.spend` (L3) at module scope; takes an already-built LLM
*client* as a parameter rather than importing a provider-construction
seam itself, matching :mod:`athenaeum.page_description`'s convention so
the CLI layer stays the only place that resolves credentials.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from athenaeum import schema_migrations
from athenaeum.models import TokenUsage, cache_usage_counts, parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

#: Bumped whenever the audit prompt or its response shape changes in a way
#: that would change a verdict for the same page. Stamped on every audited
#: page as ``audit_version:`` so a later pass can tell "audited under an old
#: prompt" apart from "never audited" (the latter has no ``last_audited`` at
#: all — see the module docstring).
AUDIT_VERSION = "audit-v5"

#: The kernel-dimension coordinate fields this pass may fill, DERIVED from
#: the schema-migrations registry (issue athenaeum#1628 decision 3) — the
#: union, in declaration order, of every ``derivation="model"`` migration's
#: ``fields`` in :data:`athenaeum.schema_migrations.MIGRATIONS`. Today that
#: is exactly the v1->v2 entry's three fields (``valid_from``/
#: ``valid_until``/``claimed_scope`` — mirrors ``dimensions.py``'s
#: ``VALID_TIME`` / ``SCOPE`` frontmatter readers), but this module no
#: longer hard-codes that tuple: a new model migration widens this set
#: automatically. Kept as a plain module-level constant purely so an
#: existing ``from athenaeum.audit import COORDINATE_FIELDS`` caller keeps
#: working unchanged — nothing in THIS module reads it any more; see
#: :func:`_pending_model_fields` below, which reads the registry directly
#: per-page instead.
COORDINATE_FIELDS: tuple[str, ...] = tuple(
    field_name
    for migration in schema_migrations.MIGRATIONS
    if migration.derivation == "model"
    for field_name in migration.fields
)

_AUDIT_MAX_TOKENS = 1024

#: Stays inline (never moved to a ``.md`` file) under this repo's registry
#: convention — see the module docstring's "Where the prompt lives" note and
#: `prompt_registry.py`'s ``("audit.audit_system", ...)`` entry; every edit
#: here is regenerated into the golden and `docs/design/prompts.md` via
#: ``python -m athenaeum.prompt_registry --write`` (issue athenaeum#1849).
AUDIT_SYSTEM = """\
You are auditing ONE knowledge-base page. Read only the page's own body and \
its cited sources below — never guess, never use outside knowledge.

Do three things:

1. COORDINATES. For each field listed under "Fields to determine", decide:
   - a determinable value, in plain text, when the page's own body or \
cited sources state a stated role, event, or effective date/scope \
explicitly, or
   - "undeterminable" with a one-line reason, when they do not.
   A date value must be ISO-8601 (YYYY-MM-DD). Never invent a value that \
is not actually stated. A date field (valid_from/valid_until) may ONLY be \
filled from a stated role, event, or effective date — NEVER from \
relationship or contact metadata. None of the following ever justify a \
date fill, even when stated on the page: a connect date (for example a \
LinkedIn connect date), a CRM first-contact, last-contact, last-email, or \
meeting date, a note date, an updated-timestamp, or any ingestion/import \
date. When the only dates available are of that kind, report the field \
as undeterminable and name the excluded date class in the reason.

2. RETIREMENT CANDIDACY. A page is a retirement candidate ONLY when at \
least one of these two things is true:
   - it states no claim at all — no independent observation, judgment, or \
synthesis, just a name/heading or nothing, or
     - a person page whose body holds only a name, or a name plus a \
single affiliation line, is a placeholder awaiting enrichment and is \
NOT a candidate on that basis,
     - a page recording a dated engagement or relationship outcome \
states a claim and is NOT a candidate, even when it also carries CRM / \
sales-pipeline metadata,
     - a page whose only content beyond its name is CRM / \
sales-pipeline metadata, or pipeline-list membership, states no claim \
and IS a candidate, or
     - a page whose own text says the entity itself is spurious — for \
example, an artifact of parsing a filename — states no claim and IS a \
candidate.
   The placeholder rule above covers a name plus at most one affiliation \
line; a person page whose only additional content is pipeline-stage, \
deal-status, or similar CRM metadata falls under the pipeline-metadata \
rule, not the placeholder rule.
   - its content duplicates another page.
   Restating or summarizing a cited source is NOT, on its own, a reason \
to flag a page — a page that accurately summarizes and scopes its source \
still adds value by making that source findable. A page whose entire \
content is a summary of a source it names (for example a whiteboard or \
board source page) is light BY DESIGN, not by deficiency: it asserts the \
source of truth and the chain of evidence another page relies on. Never \
flag such a page for retirement merely for being light.

3. SOURCE SUMMARY. Only when this page's own type is a source page: \
decide whether it gives a summary of the source it names (a summary, not \
the full detail) plus any information about that source's validity. When \
a source page lacks that summary, report it via "source_summary_missing" \
with a one-line reason — this is a finding to record, never a reason to \
flag the page for retirement.

Return ONLY a JSON object, no markdown fence, no prose, in exactly this \
shape (include a coordinate key only for a field actually listed under \
"Fields to determine"; include "source_summary_missing" only when it \
applies):

{
  "<field>": {"value": "<determined value>"},
  "<field>": {"undeterminable": "<one-line reason>"},
  "retirement_candidate": true,
  "retirement_reason": "<one-line reason, empty string when false>",
  "source_summary_missing": "<one-line reason, omit key when not applicable>"
}\
"""

AUDIT_USER_TEMPLATE = """\
Page name: {name}
Page type: {page_type}
Fields to determine: {fields}

Body:
{body}\
"""

#: Body characters handed to the auditor per page — generous relative to
#: :mod:`athenaeum.page_description`'s excerpt since a temporal-validity
#: statement or a source-vs-claim distinction can sit anywhere in the page,
#: not only its opening paragraph.
_BODY_CHARS = 4000


def _now_iso(now: Callable[[], datetime] | None = None) -> str:
    dt = now() if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_populated(value: object) -> bool:
    """Whether a frontmatter value counts as already set.

    Deliberately NOT ``isinstance(value, str) and value.strip()``: YAML
    parses an unquoted ``valid_from: 2026-01-01`` into a ``datetime.date``,
    not a string, so a string-only test reports a populated date as empty —
    which then asks the model to fill it and overwrites it with a string,
    breaking the "populated coordinate is NEVER overwritten" invariant this
    module's docstring states. Any non-``None``, non-blank value counts;
    strings keep the blank-string test they always had.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _pending_model_fields(meta: dict[str, Any]) -> tuple[str, ...]:
    """Fields *meta*'s pending MODEL migrations name (issue athenaeum#1628
    decision 4) — the union, de-duplicated and order-stable, of every
    ``derivation="model"`` migration :func:`schema_migrations.pending_migrations`
    returns for *meta*. A page already past every model migration (its
    ``schema_version`` already covers them) contributes nothing here — the
    audit pass has nothing left to ask about for it.
    """
    fields: list[str] = []
    for migration in schema_migrations.pending_migrations(meta):
        if migration.derivation != "model":
            continue
        for name in migration.fields:
            if name not in fields:
                fields.append(name)
    return tuple(fields)


def _empty_coordinate_fields(meta: dict[str, Any]) -> list[str]:
    """*meta*'s pending-model-migration fields that are missing/blank right
    now — the only ones a prompt ever asks about, and the only ones a
    verdict may ever fill."""
    empty = []
    for name in _pending_model_fields(meta):
        if not _is_populated(meta.get(name)):
            empty.append(name)
    return empty


def render_audit_prompt(meta: dict[str, Any], body: str, empty_fields: list[str]) -> str:
    """Build the per-page user prompt. Pure text assembly, no I/O."""
    name = meta.get("name") or meta.get("uid") or "(unknown)"
    page_type = meta.get("type") or "(none)"
    return AUDIT_USER_TEMPLATE.format(
        name=name,
        page_type=page_type,
        fields=", ".join(empty_fields) if empty_fields else "(none — every coordinate already set)",
        body=(body or "")[:_BODY_CHARS] or "(empty)",
    )


def _valid_date_string(value: str) -> bool:
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


#: Date coordinate fields (subset of :data:`COORDINATE_FIELDS`) — the only
#: two fields the excluded-date-class guard below ever applies to.
_DATE_FIELDS: tuple[str, ...] = ("valid_from", "valid_until")

#: Generic KEY-NAME pattern (never a source-type/adapter-name literal — see
#: AC6 / ``test_flagging_logic_is_generic``) matching frontmatter keys that
#: hold relationship/contact/ingestion metadata rather than a stated role,
#: event, or effective date (issue athenaeum#1667 Decision 1): connect dates,
#: CRM first/last-contact/last-email/meeting dates, note/ingestion
#: timestamps. A value under a matching key can never legitimately fill
#: ``valid_from``/``valid_until``, however the page states it.
_EXCLUDED_DATE_KEY_RE = re.compile(
    r"(_connected_on$|first_contact|last_contact|last_email|contact_date|"
    r"meeting|^updated$|updated_at$|^created$|created_at$|ingested)",
    re.IGNORECASE,
)


def _normalize_date_like(value: object) -> str | None:
    """Normalize a frontmatter value to a comparable ISO-ish string, or
    ``None`` when it is not a date/string value (dict/list/None/etc)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _excluded_date_values(meta: dict[str, Any]) -> dict[str, str]:
    """Map excluded-class frontmatter VALUE -> the key name it came from.

    Built purely from *meta*'s own key NAMES via :data:`_EXCLUDED_DATE_KEY_RE`
    — never a source-type/adapter-name/board-title literal — so a model
    ``valid_from``/``valid_until`` fill matching one of these values is
    refused as relationship/contact/ingestion metadata (issue athenaeum#1667
    Decision 1), whatever page type or adapter it came from.
    """
    excluded: dict[str, str] = {}
    for key, raw in meta.items():
        if not isinstance(key, str) or not _EXCLUDED_DATE_KEY_RE.search(key):
            continue
        normalized = _normalize_date_like(raw)
        if normalized:
            excluded[normalized] = key
    return excluded


def _fields_to_ask(empty_fields: list[str], date_fill: str) -> list[str]:
    """Fields actually put in front of the model this pass.

    With ``date_fill == "off"`` (issue athenaeum#1667 Decision 1 fallback,
    option B), the date fields are withheld from the prompt entirely —
    :func:`_date_fill_off_findings` records why. ``claimed_scope`` is
    unaffected in either mode.
    """
    if date_fill == "off":
        return [f for f in empty_fields if f not in _DATE_FIELDS]
    return list(empty_fields)


def _date_fill_off_findings(empty_fields: list[str], asked_fields: list[str]) -> dict[str, str]:
    """Forced findings for date fields withheld from the prompt by
    ``date_fill == "off"``. Empty when every empty field was asked about."""
    asked = set(asked_fields)
    return {
        f: "undeterminable: date filling disabled (audit.date_fill=off)"
        for f in empty_fields
        if f in _DATE_FIELDS and f not in asked
    }


def parse_audit_response(
    text: str, empty_fields: list[str], *, meta: dict[str, Any] | None = None
) -> tuple[dict[str, str], dict[str, str], bool, str]:
    """Parse the model's JSON verdict text.

    Returns ``(coordinate_fills, audit_findings, retirement_candidate,
    retirement_reason)``. ``coordinate_fills``/``audit_findings`` keys are
    always a subset of *empty_fields* (plus the standalone
    ``"source_summary_missing"`` finding key, which is not a coordinate
    field) — a field not asked about is never written, whatever the model
    returns for it (defense against a model echoing a field it was not
    asked to fill). Malformed/unparseable JSON yields empty fills/findings
    and ``retirement_candidate=False`` rather than raising — the caller
    surfaces that as a per-page error instead.

    *meta* — the page's own frontmatter, when supplied — gates a
    ``valid_from``/``valid_until`` fill through :func:`_excluded_date_values`:
    a model-proposed date matching an excluded relationship/contact/
    ingestion value is refused and recorded as a finding instead of filled
    (issue athenaeum#1667 Decision 1). Omitting *meta* (legacy callers) skips
    that guard — the date is still parsed but never cross-checked.
    """
    from athenaeum.json_utils import extract_json_object

    obj = extract_json_object(text) or {}
    fills: dict[str, str] = {}
    findings: dict[str, str] = {}
    excluded = _excluded_date_values(meta) if meta else {}
    for name in empty_fields:
        entry = obj.get(name)
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        reason = entry.get("undeterminable")
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if name in _DATE_FIELDS:
                if not _valid_date_string(candidate):
                    findings[name] = "undeterminable: model returned an unparseable date"
                    continue
                excluded_key = excluded.get(candidate)
                if excluded_key is not None:
                    findings[name] = (
                        f"undeterminable: date matches {excluded_key}, which is "
                        "contact/ingestion metadata"
                    )
                    continue
            fills[name] = candidate
        elif isinstance(reason, str) and reason.strip():
            findings[name] = f"undeterminable: {reason.strip()}"
    summary_missing = obj.get("source_summary_missing")
    if isinstance(summary_missing, str) and summary_missing.strip():
        findings["source_summary_missing"] = summary_missing.strip()
    retirement_candidate = bool(obj.get("retirement_candidate"))
    raw_reason = obj.get("retirement_reason")
    retirement_reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    return fills, findings, retirement_candidate, retirement_reason


@dataclass(frozen=True)
class AuditVerdict:
    """The structured result of one :func:`audit_page` call.

    The stable contract siblings athenaeum#1627/athenaeum#1630 are meant to build on:
    field NAMES and MEANINGS here should not change without a
    :data:`AUDIT_VERSION` bump. ``error`` non-``None`` marks a page the call
    could not audit (malformed response, API failure, ...); every other
    field is then a zero/empty placeholder and :func:`apply_audit_report`
    skips writing it.

    ``uid`` (issue athenaeum#1667 Decision 3): despite the name, this holds the
    page's IDENTITY key — the page's real ``uid`` when it has one, otherwise
    its wiki-root-relative POSIX path (see :func:`identity_key`), so a
    uid-less ``type: auto-memory`` page is addressable end-to-end. Kept as
    ``uid`` rather than renamed to avoid rippling into
    :mod:`athenaeum.audit_on_touch`/:mod:`athenaeum.audit_queue`, which
    already read this field.

    ``scan_type``/``scan_name``/``scan_cluster_id`` are set ONLY for a
    uid-less page: a snapshot of those three frontmatter values as read at
    SCAN time, for :func:`apply_audit_report`'s identity re-check (path
    alone is not enough to prove a uid-less page hasn't been renamed or
    reclustered between scan and apply).
    """

    uid: str
    path: Path
    audited_at: str
    audit_version: str
    coordinate_fills: dict[str, str] = field(default_factory=dict)
    audit_findings: dict[str, str] = field(default_factory=dict)
    retirement_candidate: bool = False
    retirement_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    scan_type: str | None = None
    scan_name: str | None = None
    scan_cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "path": str(self.path),
            "audited_at": self.audited_at,
            "audit_version": self.audit_version,
            "coordinate_fills": dict(self.coordinate_fills),
            "audit_findings": dict(self.audit_findings),
            "retirement_candidate": self.retirement_candidate,
            "retirement_reason": self.retirement_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "scan_type": self.scan_type,
            "scan_name": self.scan_name,
            "scan_cluster_id": self.scan_cluster_id,
        }


def _cost_usd(input_tokens: int, output_tokens: int, *, model: str, is_batch: bool) -> float:
    """Price *input_tokens*/*output_tokens* via the codebase's one rate table
    (:class:`athenaeum.models.TokenUsage`) rather than a second one here."""
    usage = TokenUsage()
    if is_batch:
        usage.add_batch_tokens(input_tokens, output_tokens, model=model)
    else:
        usage.add_tokens(input_tokens, output_tokens, model=model)
    return usage.estimated_cost_usd


def _verdict_from_response(
    *,
    uid: str,
    path: Path,
    meta: dict[str, Any],
    response: Any,
    model: str,
    audit_version: str,
    now: Callable[[], datetime] | None,
    empty_fields: list[str],
    is_batch: bool,
) -> AuditVerdict:
    from athenaeum.provider import response_text

    audited_at = _now_iso(now)
    input_tokens, output_tokens, _cache_w, _cache_r = cache_usage_counts(response)
    cost = _cost_usd(input_tokens, output_tokens, model=model, is_batch=is_batch)
    try:
        text = response_text(response)
    except (AttributeError, IndexError, ValueError) as exc:
        return AuditVerdict(
            uid=uid,
            path=path,
            audited_at=audited_at,
            audit_version=audit_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            error=f"unreadable response: {exc}",
        )
    fills, findings, retirement_candidate, retirement_reason = parse_audit_response(
        text, empty_fields, meta=meta
    )
    return AuditVerdict(
        uid=uid,
        path=path,
        audited_at=audited_at,
        audit_version=audit_version,
        coordinate_fills=fills,
        audit_findings=findings,
        retirement_candidate=retirement_candidate,
        retirement_reason=retirement_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def audit_page(
    client: Any,
    *,
    uid: str,
    path: Path,
    meta: dict[str, Any],
    body: str,
    model: str,
    max_tokens: int = _AUDIT_MAX_TOKENS,
    audit_version: str = AUDIT_VERSION,
    now: Callable[[], datetime] | None = None,
    date_fill: str = "constrained",
) -> AuditVerdict:
    """Audit ONE page synchronously. The reusable per-page entry point.

    Callers (this module's own :func:`build_audit_report`, and — per
    athenaeum#1624's own framing — a future inline call from the librarian for
    audit-on-touch/stale-review) supply an already-built *client* (any
    object exposing ``.messages.create(**params)``, the same seam every
    other athenaeum call site uses — see :mod:`athenaeum.provider`) and one
    page's already-parsed ``(meta, body)``. Nothing here reads or writes a
    file: *path* is carried through onto the returned verdict purely as an
    identifying label for the caller's own report/apply step.

    Never raises on a bad model response or a transport failure — returns
    an :class:`AuditVerdict` with ``error`` set instead, so one page's
    failure cannot abort a caller iterating over many.
    """
    empty_fields = _empty_coordinate_fields(meta)
    asked_fields = _fields_to_ask(empty_fields, date_fill)
    prompt = render_audit_prompt(meta, body, asked_fields)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=AUDIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — one page must not kill a run
        return AuditVerdict(
            uid=uid,
            path=path,
            audited_at=_now_iso(now),
            audit_version=audit_version,
            error=f"{exc.__class__.__name__}: {exc}",
        )
    verdict = _verdict_from_response(
        uid=uid,
        path=path,
        meta=meta,
        response=response,
        model=model,
        audit_version=audit_version,
        now=now,
        empty_fields=asked_fields,
        is_batch=False,
    )
    forced = _date_fill_off_findings(empty_fields, asked_fields)
    if forced:
        verdict = replace(verdict, audit_findings={**forced, **verdict.audit_findings})
    return verdict


#: Messages Batch API's own ``custom_id`` charset/length constraint.
_CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _batch_custom_id(identity: str) -> str:
    """Wire-level ``custom_id`` for *identity* (issue athenaeum#1667 Plan item 9).

    The Messages Batch API limits ``custom_id`` to ``[a-zA-Z0-9_-]{1,64}``;
    a ``uid`` usually satisfies that already, but a wiki-relative PATH
    identity (uid-less auto-memory pages) contains ``.``/``/`` and can
    exceed 64 chars. When *identity* does not already satisfy the pattern,
    use a deterministic digest instead — :func:`audit_pages_via_batch`
    keeps the reverse map back to *identity*.
    """
    if _CUSTOM_ID_RE.match(identity):
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"p-{digest}"


def build_audit_batch_request(
    uid: str,
    meta: dict[str, Any],
    body: str,
    *,
    model: str,
    max_tokens: int = _AUDIT_MAX_TOKENS,
    date_fill: str = "constrained",
) -> Any:
    """Build one :class:`athenaeum.batch.BatchRequest` for *uid* (``--batch``).

    Shares :func:`render_audit_prompt` with the synchronous path
    (:func:`audit_page`) so batch and sync verdicts are driven by
    byte-identical prompts — the ONLY difference between the two transports
    is submission (one call now vs. one request in a Batch API job), never
    the tier semantics, mirroring the divergence discipline
    :mod:`athenaeum.batch`'s own module docstring documents for the
    librarian's tier-2/tier-3 phases.
    """
    from athenaeum.batch import BatchRequest

    empty_fields = _empty_coordinate_fields(meta)
    asked_fields = _fields_to_ask(empty_fields, date_fill)
    prompt = render_audit_prompt(meta, body, asked_fields)
    return BatchRequest(
        custom_id=_batch_custom_id(uid),
        params={
            "model": model,
            "max_tokens": max_tokens,
            "system": AUDIT_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
    )


def audit_pages_via_batch(
    client: Any,
    pages: list[tuple[str, Path, dict[str, Any], str]],
    *,
    model: str,
    max_tokens: int = _AUDIT_MAX_TOKENS,
    audit_version: str = AUDIT_VERSION,
    now: Callable[[], datetime] | None = None,
    usage: TokenUsage | None = None,
    date_fill: str = "constrained",
) -> list[AuditVerdict]:
    """Audit *pages* through ``batch.py``'s transport (``--batch``).

    *pages* is ``[(uid, path, meta, body), ...]``. Routes every request
    through :func:`athenaeum.batch.execute_batch` — the SAME generic
    transport the tier-2/tier-3 phases use — and never touches that
    module's tier-3 MERGE request builder (``tier3_merge*``), which a
    concurrent lane (athenaeum#1463) owns. A page whose result is missing
    (errored/canceled/expired) or unparseable gets an :class:`AuditVerdict`
    with ``error`` set, same as the synchronous path.
    """
    from athenaeum.batch import execute_batch

    empty_fields_by_uid = {uid: _empty_coordinate_fields(meta) for uid, _p, meta, _b in pages}
    asked_fields_by_uid = {
        uid: _fields_to_ask(fields, date_fill) for uid, fields in empty_fields_by_uid.items()
    }
    # Reverse map wire-level custom_id -> identity, since a path-shaped
    # identity is digested (see :func:`_batch_custom_id`) before submission.
    custom_id_to_uid = {_batch_custom_id(uid): uid for uid, _p, _m, _b in pages}
    requests = [
        build_audit_batch_request(
            uid, meta, body, model=model, max_tokens=max_tokens, date_fill=date_fill
        )
        for uid, _path, meta, body in pages
    ]
    outcome = execute_batch(client, requests, description="audit", knob="classify", usage=usage)

    verdicts: list[AuditVerdict] = []
    by_uid = {uid: (path, meta) for uid, path, meta, _body in pages}
    for req in requests:
        custom_id = req.custom_id
        uid = custom_id_to_uid.get(custom_id, custom_id)
        path, meta = by_uid[uid]
        message = outcome.results.get(custom_id)
        if message is None:
            verdicts.append(
                AuditVerdict(
                    uid=uid,
                    path=path,
                    audited_at=_now_iso(now),
                    audit_version=audit_version,
                    error="batch request did not succeed (errored/canceled/expired)",
                )
            )
            continue
        verdict = _verdict_from_response(
            uid=uid,
            path=path,
            meta=meta,
            response=message,
            model=model,
            audit_version=audit_version,
            now=now,
            empty_fields=asked_fields_by_uid[uid],
            is_batch=True,
        )
        forced = _date_fill_off_findings(empty_fields_by_uid[uid], asked_fields_by_uid[uid])
        if forced:
            verdict = replace(verdict, audit_findings={**forced, **verdict.audit_findings})
        verdicts.append(verdict)
    return verdicts


# --------------------------------------------------------------------------- #
# Page discovery + selection
# --------------------------------------------------------------------------- #


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every ``.md`` page under *wiki_root*, sorted, infra ledgers excluded."""
    return sorted(p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        log.warning("audit: unreadable page %s: %s", path, exc)
        return None


def _stratified_sample(
    candidates: list[tuple[Path, dict[str, Any]]], n: int, seed: int
) -> list[tuple[Path, dict[str, Any]]]:
    """Deterministic sample of *n* candidates, stratified by ``type:``.

    Per-type floor (issue athenaeum#1667 Plan item 7): every non-empty
    stratum gets at least 1 page. When *n* is smaller than the number of
    strata (the degenerate case), the *n* LARGEST strata each get exactly
    1 page (ties broken by sorted type name) and every other stratum gets
    0. Otherwise every stratum gets its floor of 1, and the remaining
    ``n - len(strata)`` slots are allocated proportionally to stratum size
    (largest-remainder) over each stratum's REMAINING capacity
    (``size - 1``). Each stratum sampled with ``random.Random(seed)`` in a
    FIXED (sorted stratum name) order so the same *seed* over the same
    candidate set always selects the same pages (issue athenaeum#1624 AC2).
    """
    if n >= len(candidates):
        return list(candidates)
    strata: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for item in candidates:
        _path, meta = item
        page_type = str(meta.get("type") or "(none)")
        strata.setdefault(page_type, []).append(item)
    for key in strata:
        strata[key].sort(key=lambda it: str(it[0]))

    floor_order = sorted(strata, key=lambda k: (-len(strata[k]), k))

    if n <= len(strata):
        keep = set(floor_order[:n])
        quotas: dict[str, int] = {key: (1 if key in keep else 0) for key in strata}
    else:
        quotas = {key: 1 for key in strata}
        remaining = n - len(strata)
        pool = [(key, len(strata[key]) - 1) for key in strata if len(strata[key]) - 1 > 0]
        pool_total = sum(cap for _k, cap in pool)
        if remaining > 0 and pool_total > 0:
            extra: dict[str, int] = {}
            remainders: list[tuple[float, str]] = []
            for key, cap in pool:
                exact = remaining * cap / pool_total
                base = min(int(exact), cap)
                extra[key] = base
                remainders.append((exact - int(exact), key))
            allocated_extra = sum(extra.values())
            remainders.sort(key=lambda pair: (-pair[0], pair[1]))
            idx = 0
            cap_by_key = dict(pool)
            while allocated_extra < remaining and idx < len(remainders):
                _frac, key = remainders[idx]
                if extra[key] < cap_by_key[key]:
                    extra[key] += 1
                    allocated_extra += 1
                idx += 1
            for key, amount in extra.items():
                quotas[key] += amount

    rng = random.Random(seed)
    selected: list[tuple[Path, dict[str, Any]]] = []
    for key in sorted(strata):
        quota = min(quotas.get(key, 0), len(strata[key]))
        selected.extend(rng.sample(strata[key], quota))
    selected.sort(key=lambda it: str(it[0]))
    return selected


def identity_key(path: Path, meta: dict[str, Any], wiki_root: Path) -> str:
    """The audit identity for one page (issue athenaeum#1667 Decision 3).

    ``uid`` when present and non-blank; otherwise the page's
    wiki-root-relative POSIX path, so a uid-less ``type: auto-memory``
    page (the librarian's cluster pages, which never carry a ``uid:``) is
    still addressable end-to-end. NOT ``name:`` (not unique across
    auto-memory pages) and NOT ``cluster_id`` (shared by every member of a
    cluster) — see the issue's own "why not the obvious alternatives".
    """
    uid_value = meta.get("uid")
    if isinstance(uid_value, str) and uid_value.strip():
        return uid_value.strip()
    try:
        return path.resolve().relative_to(wiki_root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - defensive: path outside wiki_root
        return path.name


def _is_retired(meta: dict[str, Any]) -> bool:
    return bool(meta.get("retired"))


def _is_auto_memory_eligible(meta: dict[str, Any]) -> bool:
    """``type: auto-memory`` and not (truthy) ``retired:`` (issue athenaeum#1667
    Decision 3). Selected on ``type:``, never the ``auto-*.md`` filename
    glob — some glob matches carry a different ``type:`` and are not
    auto-memory pages."""
    return meta.get("type") == "auto-memory" and not _is_retired(meta)


def _collect_eligible_pages(wiki_root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    """Every page under *wiki_root* eligible for audit: a ``uid:`` page, or
    a non-retired ``type: auto-memory`` page (issue athenaeum#1667 Decision 3).
    Frontmatter-less/unparseable pages are silently excluded."""
    pages: list[tuple[Path, dict[str, Any], str]] = []
    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        if not meta:
            continue
        uid_value = meta.get("uid")
        has_uid = isinstance(uid_value, str) and bool(uid_value.strip())
        if not has_uid and not _is_auto_memory_eligible(meta):
            continue
        pages.append((path, meta, body))
    return pages


def _normalize_body(body: str) -> str:
    return " ".join((body or "").split())


def _find_duplicate_reasons(
    selected: list[tuple[Path, dict[str, Any], str]],
    all_eligible: list[tuple[Path, dict[str, Any], str]],
    wiki_root: Path,
) -> dict[str, str]:
    """identity -> "duplicate of <other identity>" for a *selected* page
    whose whitespace-normalised body exactly matches another ELIGIBLE page
    anywhere in the corpus (issue athenaeum#1667 Plan item 5) — computed
    deterministically in code, before prompting, over the FULL eligible
    corpus (not only the sampled/selected set); a single-page prompt
    cannot see other pages, so the model is never asked to judge
    duplicates. With more than one peer, the lexicographically smallest
    identity is named, for seed-independent determinism.
    """
    by_norm: dict[str, list[str]] = {}
    for path, meta, body in all_eligible:
        norm = _normalize_body(body)
        if not norm:
            continue
        by_norm.setdefault(norm, []).append(identity_key(path, meta, wiki_root))

    reasons: dict[str, str] = {}
    for path, meta, body in selected:
        norm = _normalize_body(body)
        if not norm:
            continue
        own = identity_key(path, meta, wiki_root)
        peers = sorted(k for k in by_norm.get(norm, []) if k != own)
        if peers:
            reasons[own] = f"duplicate of {peers[0]}"
    return reasons


def _is_bare_person_stub(meta: dict[str, Any], body: str) -> bool:
    """Whether *meta*/*body* is a ``type: person`` page whose body is
    nothing but its own H1 heading (issue athenaeum#1869).

    Deterministic override for the retirement-candidacy verdict — like
    :func:`_find_duplicate_reasons` above, never a model judgment. A live
    50-page dry run on 2026-09-19 against the ``audit-v3`` prompt flagged
    7 of 7 bare name-only person stubs as retirement candidates despite
    the prompt's OWN placeholder exemption. athenaeum#1869 read that as a
    structural defect in the wording and rewrote the section into
    ``audit-v4``, unmeasured. The measurement (athenaeum#1871, four
    ``evals.yml`` runs, two samples per arm) showed the rewrite did NOT
    fix this case — a bare stub is flagged under BOTH wordings — while
    regressing two other cases, so athenaeum#1877 reverted the wording
    and kept this override. That history is why the override stays load
    bearing rather than belt-and-braces: no prompt version measured so
    far passes this case on its own, and this rule is what keeps a bare
    person stub off the retirement list on the live path.

    "Only its H1 heading" is narrow on purpose: after discarding blank
    lines, the remaining non-blank lines must be zero, or exactly one
    line starting with ``#``. A name plus a single affiliation line is
    TWO non-blank lines and is therefore NOT covered here — that case
    stays the prompt's job (see ``AUDIT_SYSTEM`` section 2's placeholder
    rule, which the two-sample A/B measured as correct on that case).

    *body* is assumed already stripped of YAML frontmatter — the same
    contract :func:`~athenaeum.models.parse_frontmatter` returns and
    every caller of this function already holds.
    """
    if meta.get("type") != "person":
        return False
    lines = [line for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return True
    return len(lines) == 1 and lines[0].strip().startswith("#")


# --------------------------------------------------------------------------- #
# Transitory-class decay stamping (issue athenaeum#1713)
# --------------------------------------------------------------------------- #

#: The operator's four named transitory page classes (decision recorded
#: 2026-09-16 on athenaeum#1626), each mapped to the frontmatter ``type:``
#: value(s) that identify it in this corpus today. OPERATOR-ADJUSTABLE: this
#: is exactly the four classes the operator named, no additions — but WHICH
#: ``type:`` value(s) fall under each class is this module's own call, and
#: is meant to be edited here (not the matching logic in
#: :func:`_transitory_page_class`) as the corpus's actual type usage becomes
#: clearer. Keys are the class names verbatim from the operator's decision,
#: used as :attr:`AuditVerdict`-adjacent bookkeeping only (never written to
#: a page).
TRANSITORY_PAGE_CLASSES: dict[str, frozenset[str]] = {
    "incident record": frozenset({"incident"}),
    "deployment-status page": frozenset({"deployment-status"}),
    "operational source note": frozenset({"source"}),
    # "reference page mirroring a GitHub issue" is intentionally NOT a
    # blanket ``type: reference`` match — see _transitory_page_class below,
    # which additionally requires _mirrors_github_issue: not every
    # reference page duplicates the system of record.
    "reference page mirroring a GitHub issue": frozenset({"reference"}),
}

#: A GitHub issue URL, matched against a candidate ``type: reference``
#: page's ``source_ref`` frontmatter value or its body — the narrow signal
#: for "mirrors a GitHub issue" (issue athenaeum#1713): a reference page
#: that duplicates a GitHub issue as its system of record, not any
#: reference page.
_GITHUB_ISSUE_URL_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/issues/\d+")


def _mirrors_github_issue(meta: dict[str, Any], body: str) -> bool:
    """Whether *meta*/*body* cite a GitHub issue URL directly."""
    source_ref = meta.get("source_ref")
    if isinstance(source_ref, str) and _GITHUB_ISSUE_URL_RE.search(source_ref):
        return True
    return bool(_GITHUB_ISSUE_URL_RE.search(body or ""))


def _transitory_page_class(meta: dict[str, Any], body: str) -> str | None:
    """Return the operator-named transitory class *meta*/*body* match, or
    ``None`` when the page is durable (issue athenaeum#1713).

    Deterministic, in code — like :func:`_find_duplicate_reasons` above,
    never a model judgment, so AC4 (a durable page never receives a
    ``bucket``/``valid_until`` write from this pass) holds regardless of
    what an LLM might guess.
    """
    page_type = meta.get("type")
    if not isinstance(page_type, str):
        return None
    normalized = page_type.strip()
    for class_name, types in TRANSITORY_PAGE_CLASSES.items():
        if normalized not in types:
            continue
        if class_name == "reference page mirroring a GitHub issue" and not _mirrors_github_issue(
            meta, body
        ):
            continue
        return class_name
    return None


def _default_transitory_valid_until(audited_at: str, horizon_days: int) -> str:
    """*audited_at* (:func:`_now_iso`'s ``YYYY-MM-DDTHH:MM:SSZ`` shape) plus
    *horizon_days*, as a bare ``YYYY-MM-DD`` date string — the same shape
    :func:`parse_audit_response` writes for a model-derived ``valid_until``
    fill, so both land through :func:`apply_verdict_to_meta` identically.
    """
    dt = datetime.strptime(audited_at, "%Y-%m-%dT%H:%M:%SZ")
    return (dt.date() + timedelta(days=horizon_days)).isoformat()


def _apply_transitory_class(
    verdict: AuditVerdict, meta: dict[str, Any], body: str, config: dict[str, Any] | None
) -> AuditVerdict:
    """Add ``bucket``/``valid_until`` to *verdict*'s coordinate fills when
    *meta*/*body* match one of :data:`TRANSITORY_PAGE_CLASSES` (issue
    athenaeum#1713) — additively, never overwriting a coordinate value
    already populated at SCAN time (*meta*, as read for this pass; the
    write-time re-check against a possibly-newer on-disk value still
    happens in :func:`apply_verdict_to_meta`, same as every other
    coordinate). A page that already carries its own end date from the
    ordinary model-driven ``valid_from``/``valid_until`` fill (the SAME
    fill every other page gets asked about) keeps that date rather than
    the default horizon — the default only applies when the page states no
    end date of its own.
    """
    if _transitory_page_class(meta, body) is None:
        return verdict

    fills = dict(verdict.coordinate_fills)
    changed = False

    if not _is_populated(meta.get("bucket")) and "bucket" not in fills:
        fills["bucket"] = "daily"
        changed = True

    if not _is_populated(meta.get("valid_until")) and not fills.get("valid_until"):
        from athenaeum.config import resolve_audit_transitory_horizon_days

        horizon_days = resolve_audit_transitory_horizon_days(config)
        fills["valid_until"] = _default_transitory_valid_until(verdict.audited_at, horizon_days)
        changed = True

    if not changed:
        return verdict
    return replace(verdict, coordinate_fills=fills)


def select_audit_pages(
    wiki_root: Path,
    *,
    limit: int | None = None,
    sample: int | None = None,
    seed: int = 0,
    uids: list[str] | None = None,
) -> list[tuple[Path, dict[str, Any], str]]:
    """Return ``[(path, meta, body), ...]`` for pages eligible for audit.

    Selector precedence: *uids* (explicit list) takes the candidate set
    as-is; otherwise *sample* draws a stratified-by-``type:`` subset of the
    full corpus; otherwise every page under *wiki_root* is a candidate.
    *limit* then further bounds whatever set was chosen, taken in sorted-
    path order — independent of *sample*, so ``--limit`` always caps the
    pages actually processed this pass regardless of selector.
    Frontmatter-less or unparseable pages are silently excluded. A page is
    eligible when it carries a ``uid:``, OR when it is a non-retired
    ``type: auto-memory`` page (issue athenaeum#1667 Decision 3) — *uids*
    matches either kind of page via :func:`identity_key`.
    """
    all_pages = _collect_eligible_pages(wiki_root)

    if uids is not None:
        wanted = set(uids)
        candidates = [
            item for item in all_pages if identity_key(item[0], item[1], wiki_root) in wanted
        ]
    elif sample is not None:
        pairs = _stratified_sample([(p, m) for p, m, _b in all_pages], sample, seed)
        by_path = {p: (m, b) for p, m, b in all_pages}
        candidates = [(p, m, by_path[p][1]) for p, m in pairs]
    else:
        candidates = list(all_pages)

    candidates.sort(key=lambda it: str(it[0]))
    if limit is not None:
        candidates = candidates[:limit]
    return candidates


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class AuditReport:
    """Per-page verdicts + totals for one audit pass. JSON- and text-renderable."""

    scanned: int = 0
    verdicts: list[AuditVerdict] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    llm_calls: int = 0
    llm_available: bool = True
    used_batch: bool = False

    @property
    def audited(self) -> list[AuditVerdict]:
        return [v for v in self.verdicts if v.error is None]

    @property
    def failed(self) -> list[AuditVerdict]:
        return [v for v in self.verdicts if v.error is not None]

    @property
    def retirement_candidates(self) -> list[AuditVerdict]:
        return [v for v in self.audited if v.retirement_candidate]

    @property
    def total_input_tokens(self) -> int:
        return sum(v.input_tokens for v in self.verdicts)

    @property
    def total_output_tokens(self) -> int:
        return sum(v.output_tokens for v in self.verdicts)

    @property
    def total_cost_usd(self) -> float:
        return sum(v.cost_usd for v in self.verdicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "audited": len(self.audited),
            "failed": len(self.failed),
            "skipped": [{"path": str(p), "reason": r} for p, r in self.skipped],
            "retirement_candidates": len(self.retirement_candidates),
            "llm_calls": self.llm_calls,
            "llm_available": self.llm_available,
            "used_batch": self.used_batch,
            "totals": {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "cost_usd": self.total_cost_usd,
            },
            "verdicts": [v.to_dict() for v in self.verdicts],
        }

    def render_text(self) -> str:
        lines = [
            f"scanned: {self.scanned}",
            f"audited: {len(self.audited)}",
            f"failed: {len(self.failed)}",
            f"skipped: {len(self.skipped)}",
            f"retirement candidates: {len(self.retirement_candidates)}",
            f"llm calls: {self.llm_calls}",
        ]
        for v in self.verdicts:
            if v.error is not None:
                lines.append(f"  {v.path.name}: ERROR {v.error}")
                continue
            bits = []
            if v.coordinate_fills:
                bits.append("filled=" + ",".join(sorted(v.coordinate_fills)))
            if v.audit_findings:
                bits.append("undeterminable=" + ",".join(sorted(v.audit_findings)))
            if v.retirement_candidate:
                bits.append(f"retirement_candidate ({v.retirement_reason})")
            lines.append(f"  {v.path.name}: " + (", ".join(bits) if bits else "no change"))
        # Issue athenaeum#1869: a page skipped mid-run (for example a spend
        # ceiling trip) must be visible in the SAME summary a normal
        # audited/failed page is, not only in `to_dict()`'s JSON — this is
        # how the report says WHERE the run stopped.
        for skip_path, reason in self.skipped:
            lines.append(f"  {skip_path.name}: SKIPPED {reason}")
        lines.append(
            f"totals: {self.total_input_tokens} in / {self.total_output_tokens} out "
            f"tokens, ${self.total_cost_usd:.4f}"
        )
        return "\n".join(lines)


def build_audit_report(
    wiki_root: Path,
    *,
    client: Any = None,
    model: str,
    audit_version: str = AUDIT_VERSION,
    limit: int | None = None,
    sample: int | None = None,
    seed: int = 0,
    uids: list[str] | None = None,
    use_batch: bool = False,
    now: Callable[[], datetime] | None = None,
    max_tokens: int = _AUDIT_MAX_TOKENS,
    run_usage: TokenUsage | None = None,
    config: dict[str, Any] | None = None,
) -> AuditReport:
    """Scan *wiki_root* and audit every selected page. Writes nothing.

    ``--apply`` is a separate step (:func:`apply_audit_report`) so a dry
    run is exactly "call this function and print the report" — no code
    path unique to dry-run exists to drift from what apply sees.
    """
    from athenaeum.config import resolve_audit_date_fill

    date_fill = resolve_audit_date_fill(config)

    report = AuditReport(used_batch=use_batch)
    candidates = select_audit_pages(wiki_root, limit=limit, sample=sample, seed=seed, uids=uids)
    report.scanned = len(candidates)

    all_eligible = _collect_eligible_pages(wiki_root)
    duplicate_reasons = _find_duplicate_reasons(candidates, all_eligible, wiki_root)

    def _finalize(verdict: AuditVerdict, meta: dict[str, Any], body: str) -> AuditVerdict:
        uid_value = meta.get("uid")
        has_uid = isinstance(uid_value, str) and bool(uid_value.strip())
        if not has_uid:
            verdict = replace(
                verdict,
                scan_type=meta.get("type") if isinstance(meta.get("type"), str) else None,
                scan_name=meta.get("name") if isinstance(meta.get("name"), str) else None,
                scan_cluster_id=(
                    meta.get("cluster_id") if isinstance(meta.get("cluster_id"), str) else None
                ),
            )
        # Precedence (issue athenaeum#1869): the duplicate override wins when
        # both apply. It is an orthogonal, evidenced reason that already
        # shipped (issue athenaeum#1667); the bare-stub override below only
        # ever forces False, so checking it first would just get overwritten
        # by a True duplicate verdict anyway — this `elif` makes that
        # ordering explicit instead of relying on write order.
        dup_reason = duplicate_reasons.get(verdict.uid)
        if dup_reason is not None:
            verdict = replace(verdict, retirement_candidate=True, retirement_reason=dup_reason)
        elif _is_bare_person_stub(meta, body):
            verdict = replace(verdict, retirement_candidate=False, retirement_reason="")
        verdict = _apply_transitory_class(verdict, meta, body, config)
        return verdict

    if client is None:
        report.llm_available = False
        for path, _meta, _body in candidates:
            report.skipped.append((path, "no-llm-client"))
        return report

    usage = run_usage if run_usage is not None else TokenUsage()

    # Issue athenaeum#1869: ``spend.max_usd_per_run`` / ``max_usd_per_day``
    # (and the subscription-path token equivalents) were recorded to the
    # ledger by this function but never enforced — a full-corpus run
    # projected to ~$19.5 against a $20 per-run cap with nothing to stop
    # it. Mirrors ``merge.py``'s C4-phase guard: resolve the provider once,
    # check ``spend.ceiling_tripped`` before each unit of further LLM work,
    # and degrade to a recorded skip (never an exception) on a trip.
    from athenaeum import spend
    from athenaeum.provider import resolve_provider

    resolved_provider = resolve_provider(config, knob="classify")

    if use_batch:
        pages = [
            (identity_key(path, meta, wiki_root), path, meta, body)
            for path, meta, body in candidates
        ]
        meta_by_identity = {identity: (meta, body) for identity, _p, meta, body in pages}
        if pages:
            # wiki_root is passed here (batch path only, per ceiling_tripped's
            # own docstring / issue athenaeum#1147): only the batch submit path
            # can leave outstanding server-side reservations uncounted by
            # `usage` alone. The per-page branch below omits it, matching
            # every pre-athenaeum#1147 / non-batch call site.
            _ceiling = spend.ceiling_tripped(
                usage, provider=resolved_provider, config=config, wiki_root=wiki_root
            )
            if _ceiling is not None:
                log.error(
                    "Spend ceiling reached (%s) — stopping before batch submit", _ceiling
                )
                for path, _meta, _body in candidates:
                    report.skipped.append((path, f"spend ceiling reached ({_ceiling})"))
            else:
                verdicts = audit_pages_via_batch(
                    client,
                    pages,
                    model=model,
                    max_tokens=max_tokens,
                    audit_version=audit_version,
                    now=now,
                    usage=usage,
                    date_fill=date_fill,
                )
                verdicts = [_finalize(v, *meta_by_identity[v.uid]) for v in verdicts]
                report.verdicts.extend(verdicts)
                report.llm_calls += len(pages)
    else:
        for idx, (path, meta, body) in enumerate(candidates):
            _ceiling = spend.ceiling_tripped(usage, provider=resolved_provider, config=config)
            if _ceiling is not None:
                log.error("Spend ceiling reached (%s) — stopping early", _ceiling)
                for skip_path, _meta, _body in candidates[idx:]:
                    report.skipped.append((skip_path, f"spend ceiling reached ({_ceiling})"))
                break
            verdict = audit_page(
                client,
                uid=identity_key(path, meta, wiki_root),
                path=path,
                meta=meta,
                body=body,
                model=model,
                max_tokens=max_tokens,
                audit_version=audit_version,
                now=now,
                date_fill=date_fill,
            )
            verdict = _finalize(verdict, meta, body)
            report.verdicts.append(verdict)
            report.llm_calls += 1
            usage.add(
                verdict.input_tokens,
                verdict.output_tokens,
                model=model,
                knob="classify",
            )

    if usage.api_calls or usage.billable_tokens:
        spend.record_spend(
            usage,
            run_type=spend.RUN_TYPE_AUDIT,
            provider=resolved_provider,
            files_processed=len(report.audited),
            config=config,
            wiki_root=wiki_root,
        )

    return report


def _migration_satisfied(migration: Any, meta: dict[str, Any], findings: dict[str, str]) -> bool:
    """Whether every one of *migration*'s fields is either populated on
    *meta* or recorded (by name) in *findings* — issue athenaeum#1628 decision 4.
    A migration with no fields at all (the v0->v1 worked rule example) is
    vacuously satisfied: there is nothing for it to have resolved.
    """
    return all(_is_populated(meta.get(name)) or name in findings for name in migration.fields)


def _advance_schema_version(meta: dict[str, Any], findings: dict[str, str]) -> int:
    """The highest ``schema_version`` *meta* has earned, walking
    :data:`schema_migrations.MIGRATIONS` forward from its CURRENT version.

    Issue athenaeum#1628 decision 4 / Plan item 3: bump to the highest version
    whose migrations' fields are all populated or recorded in
    ``audit_findings``, and leave it UNCHANGED otherwise. "Otherwise" is
    all-or-nothing, not partial credit: the instant a migration in the walk
    is not satisfied, this returns *meta*'s ORIGINAL version outright,
    discarding any progress an earlier vacuous (fields-less, rule/eager)
    step would otherwise have made in this same call — a page must not
    silently gain ``schema_version: 1`` off the back of an unresolved
    model migration that happens to sit right after it in the chain.
    """
    original = schema_migrations.page_schema_version(meta)
    reached = original
    for migration in schema_migrations.MIGRATIONS:
        if migration.from_version != reached:
            continue
        if not _migration_satisfied(migration, meta, findings):
            return original
        reached = migration.to_version
    return reached


def apply_verdict_to_meta(meta: dict[str, Any], verdict: AuditVerdict) -> tuple[int, int]:
    """Stamp ONE verdict onto an already-parsed ``meta`` dict, in place.

    The exact mutation :func:`apply_audit_report` performs per page,
    factored out so a caller that already holds a page's ``meta`` in
    memory — a librarian touch-point mid-flow, not a fresh ``wiki_root``
    rescan — can apply the SAME stamping rules without forcing a second
    disk read. See :mod:`athenaeum.audit_on_touch` (issue athenaeum#1627),
    the caller this was extracted for; this is the ONE stamping
    implementation both it and :func:`apply_audit_report` below use.

    Never touches a field NOT already in ``verdict.coordinate_fills`` /
    ``verdict.audit_findings`` — both are already a subset of the fields
    that were empty when the page was scanned (see :func:`audit_page`) —
    and never overwrites a coordinate that is populated by the time this
    runs, whether it was populated at scan time or filled by another
    writer since (the same "populated coordinate is NEVER overwritten"
    invariant the module docstring states).

    Returns ``(fields_filled, fields_undeterminable)`` — counts of fields
    this call actually wrote (a field already populated by the time this
    runs is skipped either way, so it counts toward neither number), for a
    caller that wants to accumulate its own coordinate counters (e.g.
    :class:`athenaeum.audit_on_touch.AuditOnTouchCounters`).
    """
    meta["last_audited"] = verdict.audited_at
    meta["audit_version"] = verdict.audit_version

    raw_findings = meta.get("audit_findings")
    findings: dict[str, str] = dict(raw_findings) if isinstance(raw_findings, dict) else {}

    filled = 0
    for name, value in verdict.coordinate_fills.items():
        if _is_populated(meta.get(name)):
            continue
        meta[name] = value
        findings.pop(name, None)
        filled += 1

    undeterminable = 0
    for name, reason in verdict.audit_findings.items():
        if _is_populated(meta.get(name)):
            continue
        findings[name] = reason
        undeterminable += 1

    # Always write the map back, including when it has emptied out: a page
    # whose every recorded finding has since been resolved must not keep a
    # stale `audit_findings:` block.
    if findings:
        meta["audit_findings"] = findings
    else:
        meta.pop("audit_findings", None)

    # Issue athenaeum#1628 decision 4: bump schema_version only when every
    # pending migration's fields are now populated-or-recorded; otherwise
    # leave it exactly as it was (see _advance_schema_version). Only write
    # the key when it actually advances — a page that fails to resolve
    # stays with no explicit schema_version at all rather than gaining a
    # redundant `schema_version: 0` on every audit pass.
    advanced = _advance_schema_version(meta, findings)
    if advanced > schema_migrations.page_schema_version(meta):
        meta["schema_version"] = advanced

    return filled, undeterminable


def apply_audit_report(report: AuditReport, wiki_root: Path) -> int:
    """Write every successful verdict in *report*. Returns files-changed count.

    Re-reads each page at write time (not trusting the scan) and re-checks
    ``uid`` identity plus each coordinate field's emptiness immediately
    before writing — the same defensive re-check
    :func:`athenaeum.page_description.apply_description_backfill` performs
    for its own single field, generalized to three (now via
    :func:`apply_verdict_to_meta`, which performs that re-check). A
    populated coordinate is never overwritten; a coordinate already filled
    by another writer between scan and apply is silently skipped, never
    clobbered.
    """
    from athenaeum.atomic_io import atomic_write_text

    changed = 0
    for verdict in report.audited:
        text = _read(verdict.path)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        if not meta:
            continue

        current_identity = identity_key(verdict.path, meta, wiki_root)
        if current_identity != verdict.uid:
            continue

        uid_value = meta.get("uid")
        has_uid = isinstance(uid_value, str) and bool(uid_value.strip())
        if not has_uid:
            # Uid-less (auto-memory) page: identity is the path, and path
            # alone can silently re-target a different page after a
            # rename/recluster — re-check type/name/cluster_id against the
            # SCAN-time snapshot (issue athenaeum#1667 Plan item 8).
            if (
                meta.get("type") != verdict.scan_type
                or meta.get("name") != verdict.scan_name
                or meta.get("cluster_id") != verdict.scan_cluster_id
            ):
                continue

        apply_verdict_to_meta(meta, verdict)

        atomic_write_text(verdict.path, render_frontmatter(meta) + "\n" + body)
        changed += 1
    return changed


__all__ = [
    "AUDIT_SYSTEM",
    "AUDIT_USER_TEMPLATE",
    "AUDIT_VERSION",
    "COORDINATE_FIELDS",
    "TRANSITORY_PAGE_CLASSES",
    "AuditReport",
    "AuditVerdict",
    "apply_audit_report",
    "apply_verdict_to_meta",
    "audit_page",
    "audit_pages_via_batch",
    "build_audit_batch_request",
    "build_audit_report",
    "discover_wiki_pages",
    "identity_key",
    "parse_audit_response",
    "render_audit_prompt",
    "select_audit_pages",
]
