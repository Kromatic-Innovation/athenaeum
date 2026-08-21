# SPDX-License-Identifier: Apache-2.0
"""Backfill ``memory_class:`` across an existing wiki tree (issue athenaeum#996).

Executes the plan adopted on athenaeum#972: a deterministic ``type:`` -> class rule
map for the ~97% of pages whose ``type:`` decides the epistemic class
(:data:`athenaeum.memory_class.TYPE_TO_MEMORY_CLASS`), then an OPTIONAL
classifier-assisted pass over the residual — the intake/lifecycle types
(``auto-memory``/``preference``/``feedback``/``incident``/``issue``) and
frontmattered pages with no ``type:`` at all, which split across
``fact``/``decision``/``procedure``/``guideline`` on content rather than on
frontmatter.

Three invariants this module exists to hold:

1. **Never mint ``axiom``.** Not by rule, not by classifier. athenaeum#434 makes
   axiom-hood a human-approved promotion with a ledger record; a machine
   producing one would forge that record. The rule map has no ``axiom``
   target and :func:`_coerce_class` filters model output against
   :data:`~athenaeum.memory_class.MACHINE_ASSIGNABLE_MEMORY_CLASSES`, which
   excludes it — a model that answers ``axiom`` gets its answer DROPPED and
   counted, not written.
2. **Never overwrite, and never fabricate frontmatter.** A page with an
   existing non-empty ``memory_class`` is left alone; a page with no YAML
   frontmatter block at all is skipped and counted, never given a synthetic
   one (a 404-page population on the live store is a separate hygiene item,
   not this command's business).
3. **Byte-level idempotence.** The write is a textual INSERTION of one
   ``memory_class: <value>`` line at the end of the existing frontmatter
   block — not a ``parse_frontmatter`` -> ``render_frontmatter`` round trip,
   which would reflow key order/quoting on unrelated keys and make run 2
   differ from run 1 on pages this pass never meant to touch.

Layering: L2 (domain logic over the wiki tree). Imports models/schemas and
the provider seam; imported by ``_cmd_memory_class`` (L5). Holds no argparse
and prints nothing — the CLI module owns presentation. :func:`classify_residual`
additionally takes a function-level (deferred) import of the L3
:mod:`athenaeum.spend` ledger (issue athenaeum#1007) — the same deferred-import
convention this module already uses for :mod:`athenaeum.provider` /
:mod:`athenaeum.config` — so its classifier calls route through the shared
spend-recording path (ceiling enforcement + one ledger row per API call
batch) instead of bypassing it, which is what every other classifier call
site in athenaeum already does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.memory_class import (
    MACHINE_ASSIGNABLE_MEMORY_CLASSES,
    TYPE_TO_MEMORY_CLASS,
    memory_class_for_type,
)
from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)

#: Same shape as ``models._FM_RE`` but re-declared rather than imported: this
#: module needs the MATCH SPANS (to insert a line inside the block without
#: re-rendering it), which ``parse_frontmatter`` does not return, and it needs
#: to tell "no delimiter at all" apart from "delimiter present, YAML
#: unparseable" — ``parse_frontmatter`` collapses both to ``({}, text)``.
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)

#: ``type:`` values that reach the classifier instead of the rule map. These
#: are intake/lifecycle markers, not entity kinds — athenaeum#972's live scan put
#: 663 typed pages here. Anything else unmapped (an unknown ``type:``) is
#: reported as ``unmapped-type`` rather than silently classified.
RESIDUAL_TYPES: frozenset[str] = frozenset(
    {"auto-memory", "preference", "feedback", "incident", "issue"}
)

#: Pages per classifier call. athenaeum#972 priced the residual at ~20/call: batching
#: saves calls (and amortizes the system prompt), not tokens.
DEFAULT_BATCH_SIZE = 20

#: Body characters handed to the classifier per page. The class is decidable
#: from the opening of a memory; sending whole pages would multiply cost for
#: no discrimination.
_BODY_EXCERPT_CHARS = 600

_CLASSIFY_MAX_TOKENS = 2048

_CLASSIFIER_SYSTEM = """\
You assign an EPISTEMIC memory class to knowledge-base pages.

Classes (choose exactly one per page):
- fact: a specific true-of-the-world statement (a state, a number, an event).
- decision: a choice that was made and now stands.
- procedure: how to do something — steps, a runbook, an operating routine.
- guideline: a norm, preference, or rule of thumb that should shape behavior.
- reference: pointer/lookup material that is cited, not merged.
- entity: a page that primarily DESCRIBES a person, company, tool, or project.

You must never answer "axiom". Axiom status requires a human promotion
record and is not yours to assign; use "guideline" for a strongly-held norm.

Return ONLY a JSON array, one object per input page, each
{"i": <the page's integer index>, "memory_class": "<one of the six above>"}.
Omit a page entirely if you cannot decide — do not guess.\
"""

_CLASSIFIER_USER_TEMPLATE = """\
Classify each page below. Return the JSON array described in the system \
prompt and nothing else.

{pages}\
"""


@dataclass(frozen=True)
class PageOutcome:
    """What the backfill decided for one file, and why.

    ``memory_class`` is the value that WOULD be (or was) written; it is
    ``None`` for every skip. ``reason`` is the report's grouping key, so it
    is a closed vocabulary rather than prose:
    ``mechanical`` / ``classifier`` (assignments) and ``already-classed`` /
    ``no-frontmatter`` / ``empty-frontmatter`` / ``unparseable-frontmatter`` /
    ``retired`` / ``residual-undecided`` / ``unmapped-type`` (skips).
    """

    path: Path
    memory_class: str | None
    reason: str

    @property
    def assigned(self) -> bool:
        return self.memory_class is not None


@dataclass
class BackfillReport:
    """Counts + per-page outcomes for one backfill pass.

    Deliberately holds every outcome, not just totals: a taxonomy backfill
    over ~23k pages is only reviewable if an operator can see WHICH pages a
    given count refers to, and ``--dry-run`` is the review surface.
    """

    scanned: int = 0
    outcomes: list[PageOutcome] = field(default_factory=list)
    #: Classifier answers dropped for naming a class no machine may mint
    #: (``axiom``) or a value outside the taxonomy entirely.
    classifier_rejected: int = 0
    classifier_calls: int = 0
    classifier_available: bool = True

    def record(self, outcome: PageOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def assignments(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if o.assigned]

    def counts_by_class(self) -> dict[str, int]:
        """Assignment counts keyed by ``memory_class`` (AC2's dry-run report)."""
        counts: dict[str, int] = {}
        for outcome in self.assignments:
            counts[outcome.memory_class or ""] = (
                counts.get(outcome.memory_class or "", 0) + 1
            )
        return dict(sorted(counts.items()))

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "assigned": len(self.assignments),
            "counts_by_class": self.counts_by_class(),
            "counts_by_reason": self.counts_by_reason(),
            "classifier_calls": self.classifier_calls,
            "classifier_rejected": self.classifier_rejected,
            "classifier_available": self.classifier_available,
        }


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every ``.md`` page under *wiki_root*, sorted, infra ledgers excluded.

    ``_``-prefixed files (``_pending_questions.md``, ``MEMORY.md`` siblings'
    ledgers, schema templates) are intake/infra surfaces, not memories, and
    carry no ``memory_class`` axis.
    """
    return sorted(
        p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_")
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        # One unreadable page out of ~23k must not abort the sweep.
        log.warning("memory-class backfill: unreadable page %s: %s", path, exc)
        return None


def _classify_page(
    path: Path, text: str, *, include_retired: bool
) -> tuple[PageOutcome | None, dict[str, Any] | None, str]:
    """Decide *path* deterministically, or hand it to the residual.

    Returns ``(outcome, meta, body_excerpt)``. A non-``None`` outcome is the
    final word for this page. ``outcome is None`` means "residual" — the
    caller may send ``meta``/``body_excerpt`` to the classifier.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        # AC4: counted, never given synthetic frontmatter.
        return PageOutcome(path, None, "no-frontmatter"), None, ""

    meta, body = parse_frontmatter(text)
    if not meta:
        # ``parse_frontmatter`` returns ``{}`` for BOTH a block that failed to
        # load (it swallows the YAMLError) and a block that is legitimately
        # blank, so the raw block decides which the report names — calling an
        # empty page "unparseable" would send an operator hunting a YAML bug
        # that is not there. Neither is this command's to repair; both are
        # skipped and counted.
        reason = (
            "empty-frontmatter"
            if not match.group(1).strip()
            else "unparseable-frontmatter"
        )
        return PageOutcome(path, None, reason), None, ""

    existing = meta.get("memory_class")
    if isinstance(existing, str) and existing.strip():
        return PageOutcome(path, None, "already-classed"), None, ""

    if not include_retired and bool(meta.get("retired")):
        # AC3: retired auto-memory pages are excluded by default — paying a
        # classifier call to class a page that is on its way out is waste.
        return PageOutcome(path, None, "retired"), None, ""

    rule_class = memory_class_for_type(meta.get("type"))
    if rule_class is not None:
        return PageOutcome(path, rule_class, "mechanical"), None, ""

    page_type = meta.get("type")
    is_residual = (
        page_type is None
        or page_type == ""
        or (isinstance(page_type, str) and page_type.strip() in RESIDUAL_TYPES)
    )
    if not is_residual:
        # A ``type:`` we neither map nor recognize as a lifecycle marker.
        # Reported, not guessed.
        return PageOutcome(path, None, "unmapped-type"), None, ""

    return None, meta, body.strip()[:_BODY_EXCERPT_CHARS]


def _coerce_class(value: Any) -> str | None:
    """Return *value* iff a machine is allowed to assign it, else ``None``.

    This is the enforcement point for invariant (1): a classifier that
    answers ``"axiom"`` — or anything outside the taxonomy — is refused here,
    where it cannot be argued with, rather than in prompt wording.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if candidate in MACHINE_ASSIGNABLE_MEMORY_CLASSES:
        return candidate
    return None


def _render_batch(items: list[tuple[int, Path, dict[str, Any], str]]) -> str:
    blocks = []
    for index, path, meta, excerpt in items:
        name = meta.get("name") or path.stem
        page_type = meta.get("type") or "(none)"
        blocks.append(
            f"[{index}] name: {name}\n"
            f"    type: {page_type}\n"
            f"    body: {excerpt or '(empty)'}"
        )
    return "\n\n".join(blocks)


def classify_residual(
    residual: list[tuple[Path, dict[str, Any], str]],
    *,
    client: Any,
    config: dict[str, Any] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    wiki_root: Path | None = None,
) -> tuple[dict[Path, str], int, int]:
    """Class the residual in batched LLM calls.

    Returns ``(decisions, calls_made, rejected)``. Pages the model omits or
    answers unusably are simply absent from ``decisions`` — the caller
    reports them as ``residual-undecided`` rather than defaulting them, since
    a wrong class on a kernel dimension is worse than an absent one. A batch
    left unprocessed because the spend ceiling tripped (below) lands in the
    same ``residual-undecided`` bucket via the same absence — the caller
    cannot tell "the model declined" from "we never asked", which is correct:
    neither is a class this pass may guess.

    Routed through the ``classify`` knob of the provider seam (the same knob
    Tier-2 entity classification uses) so an operator can pin this pass to a
    backend without a new config key. Any per-batch failure is logged and
    skipped: a bad batch costs its pages a class, never the whole run.

    Issue athenaeum#1007: every batch call is routed through the shared spend
    ledger, mirroring the enforcement/recording every other classifier call
    site (librarian's Tier-2, ``query_topics``, the C4 contradiction
    detector) already has — this was the one classifier call site that made
    real API calls while staying invisible to ``spend.jsonl``. Two
    obligations, both per call site precedent:

    * **Ceiling enforcement** — :func:`athenaeum.spend.ceiling_tripped` is
      checked BEFORE each batch against this call's cumulative
      :class:`~athenaeum.models.TokenUsage`, mirroring
      ``librarian.py``'s/``merge.py``'s early-exit exactly: on a trip, log
      loudly and stop issuing further batches rather than silently
      continuing to burn budget. The per-day dollar half of the ceiling
      additionally accounts for spend already committed by EARLIER batches
      in this same call, because each batch's spend is recorded to the
      ledger (below) before the next batch's check runs.
    * **Spend recording** — one ledger row per API call batch (never one
      blended row for the whole pass), so ``athenaeum spend`` can attribute
      cost to individual batches the same way it attributes any other run.
      Best-effort via :func:`athenaeum.spend.record_spend`, which already
      swallows and logs its own failures — a ledger hiccup must never break
      the backfill.
    """
    from athenaeum import push_metrics, spend
    from athenaeum.config import resolve_model
    from athenaeum.json_utils import extract_json_array
    from athenaeum.models import TokenUsage
    from athenaeum.provider import (
        resolve_max_tokens,
        resolve_provider,
        resolve_thinking,
        response_text,
    )

    decisions: dict[Path, str] = {}
    calls = 0
    rejected = 0
    if not residual:
        return decisions, calls, rejected

    model = resolve_model(
        "classify", "ATHENAEUM_CLASSIFY_MODEL", "claude-haiku-4-5", config
    )
    max_tokens = resolve_max_tokens(
        "classify", "ATHENAEUM_CLASSIFY_MAX_TOKENS", _CLASSIFY_MAX_TOKENS, config
    )
    # Bounded-schema JSON over short excerpts — thinking buys nothing here and
    # costs latency on a pass that may make dozens of calls. Disabled
    # explicitly, mirroring the Tier-2 classify call site.
    thinking = resolve_thinking(
        "classify", "ATHENAEUM_CLASSIFY_THINKING", "disabled", config
    )
    # The backend actually serving the ``classify`` knob (issue athenaeum#1007) —
    # recorded on every ledger row so an operator pinning this pass to a
    # different provider than the run default sees the real backend, never a
    # hardcoded ``api`` that would misreport a subscription call.
    provider = resolve_provider(config, knob="classify")
    session_id = push_metrics.resolve_session_id() or None
    # Cumulative across every batch THIS call makes — the per-run half of
    # ``ceiling_tripped``'s check needs this call's own accrual, distinct
    # from the fresh per-batch accumulator below that becomes one ledger row.
    run_usage = TokenUsage()

    for start in range(0, len(residual), max(1, batch_size)):
        chunk = residual[start : start + max(1, batch_size)]
        items = [
            (offset, path, meta, excerpt)
            for offset, (path, meta, excerpt) in enumerate(chunk)
        ]

        # Issue athenaeum#378/#1007: the spend ceiling is the actual mitigation —
        # checked BEFORE spending on the next batch, mirroring librarian.py's/
        # merge.py's early-exit. A trip leaves this batch (and every batch
        # after it) undecided rather than silently continuing to burn budget.
        _ceiling = spend.ceiling_tripped(run_usage, provider=provider, config=config)
        if _ceiling is not None:
            log.warning(
                "memory-class classifier: spend ceiling reached (%s) — "
                "stopping early at offset %d; %d page(s) left undecided",
                _ceiling,
                start,
                len(residual) - start,
            )
            break

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                thinking=thinking,
                system=_CLASSIFIER_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": _CLASSIFIER_USER_TEMPLATE.format(
                            pages=_render_batch(items)
                        ),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 — one batch must not kill the pass
            log.warning(
                "memory-class classifier: batch at offset %d failed (%s): %s",
                start,
                exc.__class__.__name__,
                exc,
            )
            continue
        calls += 1

        # Issue athenaeum#1007: attribute this batch's tokens both to the
        # run-cumulative accumulator (the next iteration's ceiling check) and
        # to a FRESH per-batch accumulator that becomes exactly one ledger
        # row — never a blended row for the whole pass. Only recorded when
        # the response carried usage counters (a real SDK response always
        # does; a canned test double that omits ``usage`` simply records
        # nothing, matching ``query_topics``'s same guard).
        _usage = getattr(response, "usage", None)
        if _usage is not None:
            _counts = (
                int(getattr(_usage, "input_tokens", 0) or 0),
                int(getattr(_usage, "output_tokens", 0) or 0),
                int(getattr(_usage, "cache_creation_input_tokens", 0) or 0),
                int(getattr(_usage, "cache_read_input_tokens", 0) or 0),
            )
            run_usage.add(*_counts, model=model, knob="classify")
            batch_usage = TokenUsage()
            batch_usage.add(*_counts, model=model, knob="classify")
            spend.record_spend(
                batch_usage,
                run_type="memory-class-backfill",
                provider=provider,
                session_id=session_id,
                config=config,
                wiki_root=wiki_root,
            )

        parsed = extract_json_array(response_text(response)) or []
        for record in parsed:
            if not isinstance(record, dict):
                continue
            raw_index = record.get("i")
            if not isinstance(raw_index, int) or not 0 <= raw_index < len(chunk):
                continue
            resolved = _coerce_class(record.get("memory_class"))
            if resolved is None:
                rejected += 1
                continue
            decisions[chunk[raw_index][0]] = resolved

    return decisions, calls, rejected


def build_backfill_report(
    wiki_root: Path,
    *,
    use_classifier: bool = False,
    client: Any = None,
    config: dict[str, Any] | None = None,
    include_retired: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillReport:
    """Scan *wiki_root* and decide a ``memory_class`` for every eligible page.

    Pure with respect to the tree when *use_classifier* is ``False`` — the
    mechanical pass reads files and makes zero LLM calls. Nothing is written
    here; :func:`apply_backfill` is the only writer, so ``--dry-run`` is this
    function called alone.
    """
    report = BackfillReport()
    residual: list[tuple[Path, dict[str, Any], str]] = []

    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        report.scanned += 1
        outcome, meta, excerpt = _classify_page(
            path, text, include_retired=include_retired
        )
        if outcome is not None:
            report.record(outcome)
            continue
        assert meta is not None  # residual branch always carries its meta
        residual.append((path, meta, excerpt))

    if not residual:
        return report

    if not use_classifier or client is None:
        report.classifier_available = use_classifier and client is not None
        for path, _meta, _excerpt in residual:
            report.record(PageOutcome(path, None, "residual-undecided"))
        return report

    decisions, calls, rejected = classify_residual(
        residual, client=client, config=config, batch_size=batch_size, wiki_root=wiki_root
    )
    report.classifier_calls = calls
    report.classifier_rejected = rejected
    for path, _meta, _excerpt in residual:
        resolved = decisions.get(path)
        report.record(
            PageOutcome(
                path, resolved, "classifier" if resolved else "residual-undecided"
            )
        )
    return report


def insert_memory_class(text: str, memory_class: str) -> str | None:
    """Return *text* with a ``memory_class:`` line appended to its frontmatter.

    Returns ``None`` when *text* has no frontmatter block — the caller must
    skip, never synthesize one (AC4). The insertion is textual and touches no
    other byte of the file, which is what makes a second run a no-op at the
    byte level rather than merely at the field level.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    end = match.end(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"
    return f"{text[:end]}{newline}memory_class: {memory_class}{text[end:]}"


def apply_backfill(report: BackfillReport) -> int:
    """Write every assignment in *report*. Returns the number of files changed.

    Re-checks each file's frontmatter at write time rather than trusting the
    scan: the report may have been built minutes earlier, and a page that
    gained a ``memory_class`` in between must not be overwritten (invariant 2
    holds against the tree as it is NOW, not as it was scanned).
    """
    from athenaeum.atomic_io import atomic_write_text

    changed = 0
    for outcome in report.assignments:
        text = _read(outcome.path)
        if text is None:
            continue
        meta, _body = parse_frontmatter(text)
        existing = meta.get("memory_class")
        if isinstance(existing, str) and existing.strip():
            continue
        updated = insert_memory_class(text, outcome.memory_class or "")
        if updated is None or updated == text:
            continue
        atomic_write_text(outcome.path, updated)
        changed += 1
    return changed


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "RESIDUAL_TYPES",
    "TYPE_TO_MEMORY_CLASS",
    "BackfillReport",
    "PageOutcome",
    "apply_backfill",
    "build_backfill_report",
    "classify_residual",
    "discover_wiki_pages",
    "insert_memory_class",
]
