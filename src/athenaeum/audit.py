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

For each of the three coordinate fields declared empty on a page
(:data:`COORDINATE_FIELDS` — ``valid_from`` / ``valid_until`` /
``claimed_scope``, the kernel dimensions :mod:`athenaeum.dimensions`
already reads), the per-page call either fills a value determinable from
the page's own body and cited sources, or records an ``undeterminable``
reason in the ``audit_findings:`` frontmatter map — distinguishable from a
page that was never checked at all (no ``last_audited``, no
``audit_findings`` entry). A populated coordinate value is NEVER
overwritten, either by the LLM prompt (only empty fields are ever asked
about) or by the write path (:func:`apply_audit_report` re-checks
emptiness immediately before writing, defending against a race between
scan and apply).

The same call also returns a GENERIC retirement-candidate flag: whether the
page states any claim beyond restatement/usage of its cited sources. The
check and its parsing carry no source-type, adapter-name, or board-title
string anywhere — see this module's own text below, and
``tests/test_audit.py``'s ``git grep`` regression test. Out of scope
(athenaeum#1624's own "Out of scope" section): this command never deletes or
retires a page, and never fills ``subject`` (athenaeum#1244 / athenaeum#1615 own
that field).

``subject`` is deliberately never touched here (issue athenaeum#1624 explicitly
excludes it; athenaeum#1244/athenaeum#1615 own subject resolution by meaning).

Write path: pages are edited via the SAME ``parse_frontmatter`` ->
mutate-dict -> ``render_frontmatter`` -> ``atomic_write_text`` idiom every
other in-place page editor in this codebase uses (see e.g.
``corrections.py``, ``person_registry.py``, ``pii.py``) — never through
``athenaeum.templates`` (those are user-facing scaffolds, not a writer;
see that subpackage's own docstring).

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.batch` (L4, for the
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

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from athenaeum.models import TokenUsage, cache_usage_counts, parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

#: Bumped whenever the audit prompt or its response shape changes in a way
#: that would change a verdict for the same page. Stamped on every audited
#: page as ``audit_version:`` so a later pass can tell "audited under an old
#: prompt" apart from "never audited" (the latter has no ``last_audited`` at
#: all — see the module docstring).
AUDIT_VERSION = "audit-v1"

#: The three kernel-dimension coordinate fields this pass may fill. Mirrors
#: ``dimensions.py``'s ``VALID_TIME`` / ``SCOPE`` frontmatter readers
#: (``valid_from``/``valid_until``, ``claimed_scope``) — see that module's
#: docstring for why these three keys and no others.
COORDINATE_FIELDS: tuple[str, ...] = ("valid_from", "valid_until", "claimed_scope")

_AUDIT_MAX_TOKENS = 1024

AUDIT_SYSTEM = """\
You are auditing ONE knowledge-base page. Read only the page's own body and \
its cited sources below — never guess, never use outside knowledge.

Do two things:

1. COORDINATES. For each field listed under "Fields to determine", decide:
   - a determinable value, in plain text, when the page's own body or \
cited sources state it explicitly, or
   - "undeterminable" with a one-line reason, when they do not.
   A date value must be ISO-8601 (YYYY-MM-DD). Never invent a value that \
is not actually stated.

2. RETIREMENT CANDIDACY. Decide whether this page states any claim beyond \
a restatement or bare usage-log of its cited sources — an independent \
observation, judgment, or synthesis the sources do not already contain. A \
page with no such claim is a retirement candidate.

Return ONLY a JSON object, no markdown fence, no prose, in exactly this \
shape (include a key only for a field actually listed under "Fields to \
determine"):

{
  "<field>": {"value": "<determined value>"},
  "<field>": {"undeterminable": "<one-line reason>"},
  "retirement_candidate": true,
  "retirement_reason": "<one-line reason, empty string when false>"
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


def _empty_coordinate_fields(meta: dict[str, Any]) -> list[str]:
    """Coordinate fields on *meta* that are missing/blank — the only ones a
    prompt ever asks about, and the only ones a verdict may ever fill."""
    empty = []
    for name in COORDINATE_FIELDS:
        value = meta.get(name)
        if not (isinstance(value, str) and value.strip()):
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


def parse_audit_response(
    text: str, empty_fields: list[str]
) -> tuple[dict[str, str], dict[str, str], bool, str]:
    """Parse the model's JSON verdict text.

    Returns ``(coordinate_fills, audit_findings, retirement_candidate,
    retirement_reason)``. ``coordinate_fills``/``audit_findings`` keys are
    always a subset of *empty_fields* — a field not asked about is never
    written, whatever the model returns for it (defense against a model
    echoing a field it was not asked to fill). Malformed/unparseable JSON
    yields empty fills/findings and ``retirement_candidate=False`` rather
    than raising — the caller surfaces that as a per-page error instead.
    """
    from athenaeum.json_utils import extract_json_object

    obj = extract_json_object(text) or {}
    fills: dict[str, str] = {}
    findings: dict[str, str] = {}
    for name in empty_fields:
        entry = obj.get(name)
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        reason = entry.get("undeterminable")
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if name in ("valid_from", "valid_until") and not _valid_date_string(candidate):
                findings[name] = "undeterminable: model returned an unparseable date"
                continue
            fills[name] = candidate
        elif isinstance(reason, str) and reason.strip():
            findings[name] = f"undeterminable: {reason.strip()}"
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
    except (AttributeError, IndexError) as exc:
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
        text, empty_fields
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
    prompt = render_audit_prompt(meta, body, empty_fields)
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
    return _verdict_from_response(
        uid=uid,
        path=path,
        meta=meta,
        response=response,
        model=model,
        audit_version=audit_version,
        now=now,
        empty_fields=empty_fields,
        is_batch=False,
    )


def build_audit_batch_request(
    uid: str,
    meta: dict[str, Any],
    body: str,
    *,
    model: str,
    max_tokens: int = _AUDIT_MAX_TOKENS,
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
    prompt = render_audit_prompt(meta, body, empty_fields)
    return BatchRequest(
        custom_id=uid,
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
    requests = [
        build_audit_batch_request(uid, meta, body, model=model, max_tokens=max_tokens)
        for uid, _path, meta, body in pages
    ]
    outcome = execute_batch(client, requests, description="audit", knob="classify", usage=usage)

    verdicts: list[AuditVerdict] = []
    by_uid = {uid: (path, meta) for uid, path, meta, _body in pages}
    for req in requests:
        uid = req.custom_id
        path, meta = by_uid[uid]
        message = outcome.results.get(uid)
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
        verdicts.append(
            _verdict_from_response(
                uid=uid,
                path=path,
                meta=meta,
                response=message,
                model=model,
                audit_version=audit_version,
                now=now,
                empty_fields=empty_fields_by_uid[uid],
                is_batch=True,
            )
        )
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

    Proportional (largest-remainder) allocation across strata, each
    stratum sampled with ``random.Random(seed)`` in a FIXED (sorted
    stratum name) order so the same *seed* over the same candidate set
    always selects the same pages (issue athenaeum#1624 AC2).
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

    total = len(candidates)
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for key, items in strata.items():
        exact = n * len(items) / total
        base = int(exact)
        quotas[key] = base
        remainders.append((exact - base, key))
    allocated = sum(quotas.values())
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    idx = 0
    while allocated < n and idx < len(remainders):
        _frac, key = remainders[idx]
        if quotas[key] < len(strata[key]):
            quotas[key] += 1
            allocated += 1
        idx += 1

    rng = random.Random(seed)
    selected: list[tuple[Path, dict[str, Any]]] = []
    for key in sorted(strata):
        quota = min(quotas.get(key, 0), len(strata[key]))
        selected.extend(rng.sample(strata[key], quota))
    selected.sort(key=lambda it: str(it[0]))
    return selected


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
    Frontmatter-less or unparseable pages, and pages with no ``uid:``, are
    silently excluded (nothing to stamp against).
    """
    all_pages: list[tuple[Path, dict[str, Any], str]] = []
    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        uid_value = meta.get("uid") if meta else None
        if not isinstance(uid_value, str) or not uid_value.strip():
            continue
        all_pages.append((path, meta, body))

    if uids is not None:
        wanted = set(uids)
        candidates = [item for item in all_pages if item[1]["uid"] in wanted]
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
    report = AuditReport(used_batch=use_batch)
    candidates = select_audit_pages(wiki_root, limit=limit, sample=sample, seed=seed, uids=uids)
    report.scanned = len(candidates)

    if client is None:
        report.llm_available = False
        for path, _meta, _body in candidates:
            report.skipped.append((path, "no-llm-client"))
        return report

    usage = run_usage if run_usage is not None else TokenUsage()

    if use_batch:
        pages = [(meta["uid"], path, meta, body) for path, meta, body in candidates]
        if pages:
            verdicts = audit_pages_via_batch(
                client,
                pages,
                model=model,
                max_tokens=max_tokens,
                audit_version=audit_version,
                now=now,
                usage=usage,
            )
            report.verdicts.extend(verdicts)
            report.llm_calls += len(pages)
    else:
        for path, meta, body in candidates:
            verdict = audit_page(
                client,
                uid=meta["uid"],
                path=path,
                meta=meta,
                body=body,
                model=model,
                max_tokens=max_tokens,
                audit_version=audit_version,
                now=now,
            )
            report.verdicts.append(verdict)
            report.llm_calls += 1
            usage.add(
                verdict.input_tokens,
                verdict.output_tokens,
                model=model,
                knob="classify",
            )

    if usage.api_calls or usage.billable_tokens:
        from athenaeum import spend
        from athenaeum.provider import resolve_provider

        spend.record_spend(
            usage,
            run_type=spend.RUN_TYPE_AUDIT,
            provider=resolve_provider(config, knob="classify"),
            files_processed=len(report.audited),
            config=config,
            wiki_root=wiki_root,
        )

    return report


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
        existing = meta.get(name)
        if isinstance(existing, str) and existing.strip():
            continue
        meta[name] = value
        findings.pop(name, None)
        filled += 1

    undeterminable = 0
    for name, reason in verdict.audit_findings.items():
        existing = meta.get(name)
        if isinstance(existing, str) and existing.strip():
            continue
        findings[name] = reason
        undeterminable += 1

    if findings:
        meta["audit_findings"] = findings

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
        if not meta or meta.get("uid") != verdict.uid:
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
    "AuditReport",
    "AuditVerdict",
    "apply_audit_report",
    "apply_verdict_to_meta",
    "audit_page",
    "audit_pages_via_batch",
    "build_audit_batch_request",
    "build_audit_report",
    "discover_wiki_pages",
    "parse_audit_response",
    "render_audit_prompt",
    "select_audit_pages",
]
