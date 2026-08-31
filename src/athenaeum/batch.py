# SPDX-License-Identifier: Apache-2.0
"""Batch API execution for the librarian's tier-2/tier-3 phases (issue
athenaeum#236) — L4 domain/pipeline.

Contract: an alternate, opt-in EXECUTION STRATEGY for the same tier-2/
tier-3 work ``librarian.py``'s synchronous loop performs — same tier
semantics (see :func:`athenaeum.librarian.process_one`), different
transport (phased Anthropic Messages Batch API fan-out instead of one
call per file). Factoring rule: this module owns the BATCH TRANSPORT ONLY
— phase assembly, submission, polling, and result application. It must
NOT diverge on tier semantics (what counts as a match, what a
classification means, how a merge is written) from the synchronous path;
any such divergence belongs in ``tiers.py``/``librarian.py`` and must be
mirrored here, not invented here. The "known divergences" list below is
the complete, deliberate set — anything not listed there is a bug, not a
feature.

Opt-in via ``--batch-mode`` / ``ATHENAEUM_BATCH_MODE`` /
``librarian.batch_mode`` (resolved by
:func:`athenaeum.librarian.librarian_batch_mode`). When on, the entity-tier
loop is restructured into phased fan-out against the Anthropic Messages
Batch API, which bills all token usage at a 50% discount and completes most
batches within an hour (24h worst case) — well inside the nightly window:

  Phase 1: every ``tier2_classify`` call (one per raw file) in one batch.
  Phase 2: every ``tier3_create`` call (per new entity; depends only on its
           own file's tier-2 output) plus the ``tier3_merge`` calls whose
           target page is touched exactly once this run, in one batch.
           Pages targeted by more than one merge keep the synchronous path,
           applied serially in intake order so each merge sees the previous
           merge's output (simplest correct same-page grouping).

The C4 contradiction detector and resolver calls are NOT batched here —
they run in the merge phase before the entity tiers and stay synchronous;
the issue's cost analysis shows tier-2/tier-3 dominate spend.

Known divergences from the synchronous loop (deliberate, documented):

- Tier 0/1 run for the whole intake window up front, so an entity created
  from file A this run is not Tier-1-matchable by a later file B in the
  same run. The synchronous loop registers creations incrementally.
- The run-level API budget (athenaeum#220) is enforced with the same per-file
  ``>=`` gate as the synchronous loop at every point that spends calls:
  phase-1 assembly, phase-2 assembly (re-checked per file, since phase-1
  spend plus earlier files' tier-3 requests may have exhausted the cap by
  then), and the finalize-time same-page synchronous merges. Each batched
  request counts as one ``api_calls`` attempt at assembly time. To mirror
  the sync loop's guaranteed progress (an admitted file completes all its
  calls past the cap), each gate lets the FIRST file through even at the
  cap — so overshoot is bounded to one file's worth of calls per gate,
  never unbounded. Files deferred at phase 2 or finalize keep their raw
  files on disk and land in the athenaeum#220 deferred manifest; their tier-2 (and,
  at finalize, batched tier-3) spend is wasted — acceptable, the next run
  redoes them.

Polling interval and timeout are module constants — deliberately not a
config surface; the nightly window is latency-tolerant.

SCC membership (L4 domain/pipeline). Issue athenaeum#545 hoisted ``tier0_passthrough``
to the :mod:`athenaeum.intake` leaf, so ``batch.py`` now imports it from
``intake`` at TOP level and the former deferred ``from athenaeum.librarian
import tier0_passthrough`` back-edge (the librarian<->batch cycle) is GONE.
``batch.py`` is now FREE of the librarian import cycle: it imports no SCC
member that imports it back. ``librarian.py`` still function-locally imports
``process_batch_run`` FROM this module in its batch-mode branch, but that is
now a one-way edge (no cycle).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

import anthropic

from athenaeum import batch_state, spend
from athenaeum._retry import TransientAPIError, with_retry
from athenaeum.atomic_io import atomic_write_text
from athenaeum.intake import tier0_passthrough
from athenaeum.models import (
    EntityAction,
    EntityIndex,
    EscalationItem,
    RawFile,
    TokenUsage,
    WikiEntity,
    cache_usage_counts,
    parse_frontmatter,
    render_frontmatter,
)
from athenaeum.provider import AnthropicBatchClientBackend, response_text
from athenaeum.schemas import validate_wiki_meta
from athenaeum.self_resolving import flag_self_resolving_claims
from athenaeum.tiers import (
    TIER2_ADDRESS_RESOLVED_MARKER,
    TIER2_ADDRESS_UNRESOLVED_MARKER,
    Tier2ParseStats,
    existing_body_needs_full_echo,
    parse_merge_ops_response,
    parse_tier2_entities,
    partition_code_artifact_classifications,
    resolve_address_named_classifications,
    stamp_merge_provenance,
    tier1_programmatic_match,
    tier2_reclassify_larger_budget,
    tier2_request_params,
    tier3_create_params,
    tier3_entity_from_text,
    tier3_merge,
    tier3_merge_full,
    tier3_merge_params,
    tier4_escalate,
)

if TYPE_CHECKING:
    from anthropic.types.messages.batch_create_params import Request

log = logging.getLogger(__name__)

# Poll cadence for ``processing_status``. 30s keeps the nightly run
# responsive to the common fast-completion case without hammering the API;
# the timeout matches the Batch API's documented 24h processing ceiling.
BATCH_POLL_INTERVAL_SECONDS: float = 30.0
BATCH_POLL_TIMEOUT_SECONDS: float = 24 * 60 * 60.0

# Documented Messages Batch API submission limits (issue athenaeum#1144 AC7).
# A batch that breaches either is refused at ASSEMBLY with a clear, local
# error rather than being submitted and coming back as an opaque 400 — the
# refusal maps onto the same per-file failure path an unsubmittable batch
# already takes, so the raw files stay on disk and the next run retries them
# in smaller cohorts.
BATCH_MAX_REQUESTS: int = 100_000
BATCH_MAX_PAYLOAD_BYTES: int = 256 * 1024 * 1024


class BatchExecutionError(Exception):
    """A batch could not be submitted, polled to completion, or collected.

    Callers map this onto the per-file failure path for every file with a
    request in the affected batch (raw files stay on disk; next run
    retries them).
    """


@dataclass
class BatchRequest:
    """One Messages Batch API request: ``{custom_id, params}``."""

    custom_id: str
    params: dict[str, Any]


@dataclass
class BatchOutcome:
    """What one :func:`execute_batch` call produced (issue athenaeum#1144 AC3).

    Three distinguishable shapes, and the caller MUST tell them apart:

    - **collected** — ``in_flight`` is ``False`` and ``results`` maps every
      ``custom_id`` to its ``Message`` (or ``None`` for a per-request
      ``errored`` / ``canceled`` / ``expired`` result). This is today's
      behaviour, byte-for-byte.
    - **in flight** — ``in_flight`` is ``True``: the run's wall-clock
      deadline arrived before the batch ended. ``results`` is empty and
      ``batch_id`` names the batch, which is still running server-side and
      already paid for. The batch is deliberately NOT cancelled.
    - **nothing submitted** — no requests were passed; ``batch_id`` is ``""``
      and ``results`` is empty.
    """

    batch_id: str = ""
    results: dict[str, Any] = field(default_factory=dict)
    in_flight: bool = False


def execute_batch(
    client: anthropic.Anthropic,
    requests: list[BatchRequest],
    *,
    description: str,
    usage: TokenUsage | None = None,
    knob: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = BATCH_POLL_INTERVAL_SECONDS,
    timeout: float = BATCH_POLL_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> BatchOutcome:
    """Submit *requests*, poll to completion, return a :class:`BatchOutcome`.

    ``outcome.results`` maps ``{custom_id: Message}``. A ``None`` value marks
    a per-request ``errored`` / ``canceled`` /
    ``expired`` result — callers map those onto the existing per-file
    failure path. Token usage from succeeded results lands in *usage* via
    :meth:`TokenUsage.add_batch_tokens` (``api_calls`` attempts are counted
    at batch-assembly time by the caller, one per request — not here).

    *knob* (issue athenaeum#781) tags the model-knob for the WHOLE batch — every
    request in one ``execute_batch`` call shares the same knob (the tier-2
    classify batch and the tier-3 write batch are each submitted in their
    own call), unlike *model* which is read per-request from
    ``request.params["model"]`` because a batch can mix models.

    Raises :class:`BatchExecutionError` when the batch cannot be submitted,
    polled, or collected — transient errors that exhausted their retries
    AND non-transient ones (e.g. a 400 from a malformed payload) alike, so
    a whole-batch failure always reaches the callers' per-file failure
    path instead of escaping as a run-fatal traceback — or when the batch
    does not end within *timeout* (best-effort cancel on timeout).
    ``sleep`` is injectable so tests don't wait.

    *deadline* (issue athenaeum#1144) is the run's wall-clock deadline as an
    absolute :func:`time.monotonic` instant — ``RunContext.entity_deadline``
    when the athenaeum#440 entity share is armed, else ``RunContext.run_deadline``.
    When supplied, the poll loop stops at the EARLIER of batch-end or that
    deadline, and a deadline arriving first SPILLS: the batch is left running
    (**never** cancelled — it is already paid for server-side and is the whole
    point of the athenaeum#1138 handle) and an ``in_flight`` outcome carrying the
    batch id comes back instead of a raised
    :class:`BatchExecutionError`. ``None`` (every pre-athenaeum#1144 caller)
    preserves today's *timeout*-only semantics exactly.

    The deadline is converted ONCE at entry into a remaining-seconds budget
    and then measured against the same ``waited`` accumulator the *timeout*
    check already uses, rather than re-reading ``time.monotonic()`` each pass.
    That keeps both bounds on one clock — the injectable ``sleep`` — so a test
    can drive either path deterministically without waiting on real time
    (AC6).
    """
    if not requests:
        return BatchOutcome()

    payload = [{"custom_id": r.custom_id, "params": r.params} for r in requests]
    _refuse_oversized_batch(payload, description=description)
    try:
        batch = with_retry(
            lambda: client.messages.batches.create(
                requests=cast("list[Request]", payload)
            ),
            description=f"batch submit ({description})",
        )
    except Exception as exc:
        raise BatchExecutionError(
            f"batch submit failed ({description}): {exc}"
        ) from exc
    log.info(
        "Submitted batch %s: %d request(s) (%s)",
        batch.id,
        len(requests),
        description,
    )

    waited = 0.0
    # Remaining wall-clock budget for THIS poll, snapshotted at entry (see the
    # docstring). ``None`` when no deadline was supplied; clamped at 0 so an
    # already-expired deadline spills on the first pass instead of polling once.
    deadline_budget = (
        max(0.0, deadline - time.monotonic()) if deadline is not None else None
    )
    status = getattr(batch, "processing_status", "in_progress")
    while status != "ended":
        if deadline_budget is not None and waited >= deadline_budget:
            # Issue athenaeum#1144 AC3: do NOT cancel and do NOT raise. The batch is
            # committed server-side; cancelling would destroy work the caller
            # is about to persist a handle for.
            log.warning(
                "batch %s still in flight at the run deadline after %.0fs "
                "(%s) — leaving it running and spilling to a handle",
                batch.id,
                waited,
                description,
            )
            return BatchOutcome(batch_id=batch.id, in_flight=True)
        if waited >= timeout:
            try:
                client.messages.batches.cancel(batch.id)
            except Exception:  # noqa: BLE001 — cancel is best-effort
                log.warning("could not cancel timed-out batch %s", batch.id)
            raise BatchExecutionError(
                f"batch {batch.id} did not end within {timeout:.0f}s "
                f"({description})"
            )
        sleep(poll_interval)
        waited += poll_interval
        try:
            batch = with_retry(
                lambda: client.messages.batches.retrieve(batch.id),
                description=f"batch poll ({description})",
            )
        except Exception as exc:
            raise BatchExecutionError(
                f"batch poll failed ({description}): {exc}"
            ) from exc
        status = getattr(batch, "processing_status", "in_progress")

    log.info("Batch %s ended after %.0fs (%s)", batch.id, waited, description)

    results: dict[str, Any] = {r.custom_id: None for r in requests}
    # Map each request's custom_id to its serving model-id so batch token
    # usage attributes per model (issue athenaeum#247). The model lives in each
    # request's params (``messages.create`` payload).
    model_by_cid: dict[str, str | None] = {
        r.custom_id: r.params.get("model") for r in requests
    }
    try:
        entries = with_retry(
            lambda: client.messages.batches.results(batch.id),
            description=f"batch results ({description})",
        )
        for entry in entries:
            result = entry.result
            rtype = result.type
            if result.type == "succeeded":
                message = result.message
                if usage is not None:
                    inp, out, cache_w, cache_r = cache_usage_counts(message)
                    usage.add_batch_tokens(
                        inp,
                        out,
                        cache_w,
                        cache_r,
                        model=model_by_cid.get(entry.custom_id),
                        knob=knob,
                    )
                results[entry.custom_id] = message
            else:
                log.warning(
                    "batch request %s ended %s (%s)",
                    entry.custom_id,
                    rtype,
                    description,
                )
    except Exception as exc:
        raise BatchExecutionError(
            f"batch results failed ({description}): {exc}"
        ) from exc
    return BatchOutcome(batch_id=batch.id, results=results)


def _refuse_oversized_batch(
    payload: list[dict[str, Any]], *, description: str
) -> None:
    """Refuse a batch that breaches the documented API limits (AC7).

    The Batch API caps a submission at :data:`BATCH_MAX_REQUESTS` requests and
    :data:`BATCH_MAX_PAYLOAD_BYTES` of serialized payload. Breaching either
    returns a 400 whose text is about the wire format rather than about the
    cohort that produced it. Checking locally names the actual numbers, and
    raises the same :class:`BatchExecutionError` an unsubmittable batch
    already raises — so the caller's existing per-file failure path applies
    unchanged (raw files stay on disk, the next run retries them).
    """
    count = len(payload)
    if count > BATCH_MAX_REQUESTS:
        raise BatchExecutionError(
            f"batch refused before submit ({description}): {count} requests "
            f"exceeds the Batch API limit of {BATCH_MAX_REQUESTS}"
        )
    try:
        size = len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        # A payload that cannot be serialized locally is not a size problem;
        # let the SDK produce its own error rather than inventing one here.
        return
    if size > BATCH_MAX_PAYLOAD_BYTES:
        raise BatchExecutionError(
            f"batch refused before submit ({description}): payload is "
            f"{size} bytes, exceeding the Batch API limit of "
            f"{BATCH_MAX_PAYLOAD_BYTES}"
        )


class _BatchItemError(Exception):
    """A required per-request batch result was errored/canceled/expired."""


@dataclass
class _FileState:
    """Per-raw-file bookkeeping across the batch phases."""

    raw: RawFile
    matched: list[tuple[str, str, Path]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    actions: list[EntityAction] = field(default_factory=list)
    t2_id: str | None = None
    create_ids: list[tuple[str, EntityAction]] = field(default_factory=list)
    # (custom_id, action, page_path, meta-parsed-at-assembly,
    #  existing_body-read-at-assembly). Issue athenaeum#469: existing_body is retained
    #  so the patch-mode ops can be applied deterministically at finalize.
    merge_ids: list[tuple[str, EntityAction, Path, dict, str]] = field(
        default_factory=list
    )
    sync_merges: list[EntityAction] = field(default_factory=list)
    created: list[WikiEntity] = field(default_factory=list)
    # Issue athenaeum#1126: Tier-4 escalations for declined address-shaped
    # classifications, built at classification time and flushed at finalize
    # (in BOTH branches that unlink the raw file) so the fact survives even
    # when the declined address was this file's only classification.
    address_escalations: list[EscalationItem] = field(default_factory=list)
    failed: bool = False
    done: bool = False
    # Set when the budget re-check at phase-2 assembly or before the
    # finalize-time sync merges defers this file (athenaeum#220): raw stays on
    # disk, ref goes to the deferred manifest, nothing is written.
    deferred: bool = False
    # Issue athenaeum#1144: this file's batch was still running at the run's
    # wall-clock deadline. Distinct from ``deferred`` (never submitted, no
    # spend) and from ``failed`` (submitted and lost): the work is submitted,
    # PAID FOR, and recoverable from the athenaeum#1143 handle by a later run's
    # collect pass. Raw stays on disk, nothing is written, and a lease keeps
    # the next run from resubmitting it.
    in_flight: bool = False


@dataclass
class BatchRunResult:
    """Aggregate outcome of :func:`process_batch_run`."""

    created: int = 0
    updated: int = 0
    escalated: int = 0
    skipped: int = 0
    #: Files that dropped ALL entities on unparseable Tier-2 JSON, even after
    #: the athenaeum#472 control-character repair pass. (The batch transport cannot
    #: retry a single request synchronously, so repair is its only recovery
    #: mechanism — the sync path additionally retries once.)
    degraded: int = 0
    #: Files that dropped ALL entities because the Tier-2 response was
    #: TRUNCATED at the output-token budget (``stop_reason == "max_tokens"``),
    #: leaving an unterminated array (issue athenaeum#476). Kept SEPARATE from
    #: ``degraded`` (a genuine parse failure). The batch transport cannot retry
    #: a single request with a bigger budget synchronously, so a truncated
    #: file is preserved and retried on the next run — but the raised default
    #: ``max_tokens`` (athenaeum#476) makes a truncation far rarer to begin with.
    truncated: int = 0
    failed_refs: list[str] = field(default_factory=list)
    deferred_refs: list[str] = field(default_factory=list)
    #: Issue athenaeum#1144: files whose batch was still in flight when the run's
    #: wall-clock deadline arrived. Kept SEPARATE from ``failed_refs`` (which
    #: means "retry from scratch next run") and from ``deferred_refs`` (which
    #: means "never submitted, no spend"): this cohort is submitted and billed,
    #: and a later run collects it from the recorded handle rather than
    #: redoing it. Conflating the three would either double-bill the cohort or
    #: report committed spend as wasted.
    in_flight_refs: list[str] = field(default_factory=list)
    #: Batch ids left running at the deadline, in submit order — the handles
    #: recorded for them.
    in_flight_batch_ids: list[str] = field(default_factory=list)


def _spill_to_handle(
    outcome: BatchOutcome,
    raw_by_cid: dict[str, RawFile],
    *,
    knob: str,
    cache_dir: Path,
    config: dict[str, object] | None,
    result: BatchRunResult,
) -> None:
    """Persist an athenaeum#1143 handle for a batch left running at the deadline.

    Records the handle (which also takes the lease over the raw files, so the
    next run's claim loop does not resubmit them) and books the affected refs
    onto ``result.in_flight_refs``. Recording is best-effort in the same sense
    the rest of :mod:`athenaeum.batch_state` is fail-open: a store that cannot
    be written must not turn a successfully-submitted batch into a crashed
    run. It IS logged loudly, because the consequence — the next run
    rediscovering and resubmitting the same cohort at full price — is exactly
    the silent double-bill athenaeum#1143 exists to prevent.
    """
    result.in_flight_batch_ids.append(outcome.batch_id)
    try:
        batch_state.record_handle(
            cache_dir,
            batch_id=outcome.batch_id,
            knob=knob,
            refs=raw_by_cid,
            config=cast("dict[str, Any] | None", config),
        )
    except Exception:  # a marker store must never wedge a run
        log.exception(
            "could not record the pending-batch handle for %s (%s) — the next "
            "run may rediscover and RESUBMIT this cohort at full price",
            outcome.batch_id,
            knob,
        )
    log.warning(
        "batch %s (%s) left in flight at the run deadline: %d file(s) "
        "leased for collection by a later run",
        outcome.batch_id,
        knob,
        len({raw.ref for raw in raw_by_cid.values()}),
    )


def process_batch_run(
    raw_files: list[RawFile],
    index: EntityIndex,
    wiki_root: Path,
    client: anthropic.Anthropic,
    valid_types: list[str],
    valid_tags: list[str],
    valid_access: list[str],
    *,
    usage: TokenUsage,
    config: dict[str, object] | None,
    max_api_calls: int,
    provider: str = "api",
    sleep: Callable[[float], None] = time.sleep,
    write_client: "anthropic.Anthropic | None" = None,
    deadline: float | None = None,
    cache_dir: Path | None = None,
) -> BatchRunResult:
    """Process the intake window through the Batch API phases (issue athenaeum#236).

    Mirrors the per-file semantics of :func:`athenaeum.librarian.process_one`
    (tier 0/1 programmatic pass, per-file failure isolation, write-only-when-
    all-calls-succeeded, tier-4 escalation, raw deletion on success) while
    fanning the tier-2/tier-3 LLM calls out into two Messages Batch API
    submissions. See the module docstring for phase layout, budget
    semantics, and documented divergences from the synchronous loop.

    ``client`` serves the tier-2 classify batch (the ``classify`` knob).
    ``write_client`` (issue athenaeum#841) serves the tier-3 batch AND the
    synchronous same-page merge / truncation-retry fallbacks below it (the
    ``write`` knob) — ``None`` (every pre-athenaeum#841 caller) falls back to
    *client*, preserving the old single-client behavior byte-for-byte. In
    practice the two rarely differ in batch mode: the startup guard
    (:func:`athenaeum.librarian._resolve_run_config`) already rejects a
    ``classify``/``write`` combination where either resolves to
    ``claude-cli``, so both must resolve to ``api`` for a batch run to even
    start — but a distinct API key/timeout per knob is still honored.

    Issue athenaeum#483: the configured spend ceiling (athenaeum#378) is enforced at each
    phase boundary — before the tier-2 submit and before the tier-3 submit
    — since a submitted batch runs to completion server-side and cannot be
    halted mid-flight. A breach defers every not-yet-written file rather
    than submitting the next (potentially large) batch, mirroring the sync
    loop's "log loudly and defer the rest, never silently continue". The
    check is per-phase, so overshoot is bounded to at most one phase's cost
    instead of the whole run. *provider* selects the ceiling UNIT (dollars
    for the metered ``api`` path — always the case in batch mode, which is
    Anthropic-endpoint-only; tokens for ``claude-cli``).

    Issue athenaeum#1144: *deadline* is the run's wall-clock deadline as an
    absolute :func:`time.monotonic` instant, threaded down into
    :func:`execute_batch`. This is a BOUNDED WAIT, not submit-and-exit: when
    the batch ends inside the window the run continues synchronously into the
    next phase exactly as before, byte-for-byte. Only when the window runs out
    does the run persist an athenaeum#1143 handle for the still-running batch, mark
    that batch's files ``in_flight`` (NOT ``failed``, NOT ``deferred``), and
    return — so a later run collects work that is already paid for instead of
    resubmitting or discarding it. ``None`` preserves today's behaviour
    exactly: no deadline, no handle, no in-flight refs.

    *cache_dir* is where the handle store lives; ``None`` resolves it via
    :func:`athenaeum.batch_state.resolve_cache_dir`, which is what production
    wants and what keeps the submit side and a later run's collect side
    pointing at the same file.
    """
    effective_write_client = write_client if write_client is not None else client
    effective_cache_dir = (
        cache_dir if cache_dir is not None else batch_state.resolve_cache_dir()
    )
    from athenaeum.config import load_config, resolve_owner

    owner = resolve_owner(config)
    result = BatchRunResult()
    states: list[_FileState] = []
    t2_requests: list[BatchRequest] = []
    # ``custom_id -> RawFile`` for each phase, so a deadline spill can record
    # the athenaeum#1143 handle's ref map without re-deriving it from the request ids.
    t2_raw_by_cid: dict[str, RawFile] = {}
    t3_raw_by_cid: dict[str, RawFile] = {}

    # --- Tier 0/1 + phase-1 assembly (budget gate per file, athenaeum#220) ---
    for i, raw in enumerate(raw_files):
        if usage.api_calls >= max_api_calls:
            log.warning(
                "API call budget exhausted (%d/%d) at batch assembly — "
                "deferring remaining intake",
                usage.api_calls,
                max_api_calls,
            )
            result.deferred_refs = [r.ref for r in raw_files[i:]]
            break
        st = _FileState(raw=raw)
        states.append(st)
        log.info("Processing (batch): %s", raw.ref)
        try:
            passthrough = tier0_passthrough(raw, index, wiki_root, valid_types)
            if passthrough is not None:
                log.info(
                    "  T0 passthrough: %s → %s",
                    passthrough.name,
                    passthrough.filename,
                )
                st.created.append(passthrough)
                st.done = True
                continue

            # Issue athenaeum#662: filter junk-name matches before they cost a tier-3 call.
            st.matched = tier1_programmatic_match(raw, index, config=config)
            for name, _uid, fpath in st.matched:
                if index.has_entity_format(fpath):
                    log.info("  T1 match (entity format): %s → %s", name, fpath.name)
                else:
                    log.info("  T1 match (old format, skip): %s → %s", name, fpath.name)
                    st.skipped.append(name)

            # Deterministic self-resolving-document guard (athenaeum#300 follow-up,
            # athenaeum#304): flag embedded self-confirmation claims BEFORE the
            # tier2 request is assembled, mirroring the sync path in
            # librarian.process_one (see the longer comment there for the
            # disk-vs-downstream-wiki persistence distinction). Mutates
            # only this in-memory RawFile's cached content.
            raw._content = flag_self_resolving_claims(raw.content)

            # Empty content short-circuits without an API call, exactly
            # like tier2_classify's early return on the sync path.
            if raw.content.strip():
                st.t2_id = f"t2-{i}"
                # Each batched request counts as one api_call attempt,
                # recorded at assembly time (athenaeum#220 budget semantics).
                usage.api_calls += 1
                matched_names = [name for name, _, _ in st.matched]
                t2_raw_by_cid[st.t2_id] = raw
                t2_requests.append(
                    BatchRequest(
                        custom_id=st.t2_id,
                        params=tier2_request_params(
                            raw,
                            matched_names,
                            valid_types,
                            valid_tags,
                            valid_access,
                            wiki_root=wiki_root,
                            config=config,
                        ),
                    )
                )
        except Exception:
            log.exception("Failed to process %s", raw.ref)
            st.failed = True

    # --- Phase 1: tier-2 classification batch ---
    # Issue athenaeum#483: enforce the spend ceiling BEFORE submitting tier-2. Any
    # spend already accrued (the synchronous auto-memory merge/resolver phase
    # runs before the entity tiers) is reflected in ``usage`` by now; if it
    # has breached the ceiling we must not submit another batch that would run
    # to completion server-side. Defer every not-yet-resolved file and fall
    # through to finalize (which still writes the zero-cost T0 passthroughs).
    t2_results: dict[str, Any] = {}
    t2_ceiling = spend.ceiling_tripped(usage, provider=provider, config=config)
    if t2_ceiling is not None:
        pending = [st for st in states if not st.done and not st.failed]
        log.error(
            "Spend ceiling reached (%s) before the tier-2 batch — deferring "
            "%d file(s), not submitting (issue athenaeum#483)",
            t2_ceiling,
            len(pending),
        )
        for st in pending:
            st.deferred = True
    elif t2_requests:
        try:
            t2_outcome = execute_batch(
                client,
                t2_requests,
                description="tier2_classify",
                usage=usage,
                knob="classify",
                sleep=sleep,
                deadline=deadline,
            )
        except BatchExecutionError as exc:
            log.error("Tier-2 batch failed (%s) — affected files retried next run", exc)
        else:
            if t2_outcome.in_flight:
                # Issue athenaeum#1144: the classify batch is still running and paid
                # for. Every file with a request in it goes in-flight — not
                # failed (which would retry, and re-bill, the same work) and
                # not deferred (which would claim nothing was submitted).
                _spill_to_handle(
                    t2_outcome,
                    t2_raw_by_cid,
                    knob="classify",
                    cache_dir=effective_cache_dir,
                    config=config,
                    result=result,
                )
                for st in states:
                    if st.t2_id is not None and not st.failed and not st.done:
                        st.in_flight = True
            else:
                t2_results = t2_outcome.results

    # Parse classifications and build per-file actions (same shape as
    # process_one: creates from tier-2, updates from tier-1 matches).
    for st in states:
        if st.failed or st.done or st.deferred or st.in_flight:
            continue
        classified = []
        if st.t2_id is not None:
            msg = t2_results.get(st.t2_id)
            if msg is None:
                log.error(
                    "Tier-2 batch result failed for %s — retried next run",
                    st.raw.ref,
                )
                st.failed = True
                continue
            try:
                # Issue athenaeum#578: response_text skips any leading thinking block
                # (tier-2 classify runs disabled today; the helper is
                # text-block-equivalent for a text-only response and keeps the
                # batch site robust if the posture changes).
                text = response_text(msg)
            except Exception:
                log.exception("Failed to process %s", st.raw.ref)
                st.failed = True
                continue
            # athenaeum#472: repair bare control chars inside string values before
            # discarding a whole file's entities, and count any that still
            # degrade so the run summary can surface it. athenaeum#476: pass the
            # response's stop_reason so a max_tokens truncation is counted as
            # ``truncated`` (distinct from a genuine parse ``degraded``).
            t2_stats = Tier2ParseStats()
            classified = parse_tier2_entities(
                text,
                st.raw.ref,
                valid_types,
                valid_tags,
                valid_access,
                owner=owner,
                stats=t2_stats,
                stop_reason=getattr(msg, "stop_reason", None),
                wiki_root=wiki_root,
            )
            # athenaeum#476: a batch response TRUNCATED at max_tokens dropped every
            # entity — retry ONCE synchronously with a LARGER budget (the same
            # bigger-budget retry the sync path uses). This closes the gap athenaeum#472
            # left, where the retry existed only on the sync path; the tier-3
            # full-echo fallback below is the established precedent for a live
            # call at batch finalize. A retry that recovers clears the
            # truncation; one that still truncates leaves the file preserved
            # (never unlinked) for the next run.
            if t2_stats.truncated:
                retry_names = [name for name, _, _ in st.matched]
                retry_entities, retry_stats = tier2_reclassify_larger_budget(
                    st.raw,
                    retry_names,
                    valid_types,
                    valid_tags,
                    valid_access,
                    AnthropicBatchClientBackend(client),
                    wiki_root=wiki_root,
                    usage=usage,
                    config=config,
                    owner=owner,
                )
                if not retry_stats.degraded and not retry_stats.truncated:
                    log.info(
                        "tier2-classify-truncation-retry-recovered ref=%s: batch "
                        "retry with a larger max_tokens budget parsed successfully",
                        st.raw.ref,
                    )
                    classified = retry_entities
                    t2_stats.truncated -= 1
                    t2_stats.repaired += retry_stats.repaired
            result.degraded += t2_stats.degraded
            result.truncated += t2_stats.truncated
            log.info(
                "  T2 classified %d new entities (%s)", len(classified), st.raw.ref
            )
        # Issue athenaeum#680: never mint a wiki entity from a filename/path (a code
        # artifact) — the repo is the source of truth for its own code, so a
        # memory of it is stale by construction. Dropped at creation, on the
        # batch transport too (complementary to athenaeum#662's read-side stopwords).
        classified, _dropped_code = partition_code_artifact_classifications(
            classified, config
        )
        for _name in _dropped_code:
            log.info(
                "  T3 create skipped (issue athenaeum#680, code artifact): %s (%s)",
                _name,
                st.raw.ref,
            )
        # Issue athenaeum#1126: batch-transport parity with process_one — never
        # mint a NEW entity named after a bare email address. No shared
        # ExcludedRecordIndex is in scope on this transport (correctness
        # first: excluded_index=None lets resolve_handle_query build its own
        # per address, at the cost of the O(corpus) scan being repaid per
        # address rather than shared across the run).
        address_outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=wiki_root.parent,
            wiki_root=wiki_root,
            config=config,
            excluded_index=None,
        )
        classified = address_outcome.kept
        for _address, _uid, _display_name in address_outcome.resolved:
            log.info(
                "%s: address=%s uid=%s name=%r (%s)",
                TIER2_ADDRESS_RESOLVED_MARKER,
                _address,
                _uid,
                _display_name,
                st.raw.ref,
            )
        for _ref_name, _reason in address_outcome.declined:
            log.warning(
                "%s: ref=%s address=%s reason=%s",
                TIER2_ADDRESS_UNRESOLVED_MARKER,
                st.raw.ref,
                _ref_name,
                _reason,
            )
            st.address_escalations.append(
                EscalationItem(
                    raw_ref=st.raw.ref,
                    entity_name=_ref_name,
                    conflict_type="classification_failed",
                    description=(
                        f"This statement's subject ({_ref_name!r}) is an "
                        "email address that resolves to no known entity "
                        f"(reason: {_reason}); no address-named page was "
                        "created (athenaeum#1126). The statement text "
                        f"follows so the fact is not lost:\n\n"
                        f"{st.raw.content[:2000]}"
                    ),
                )
            )
        for c in classified:
            st.actions.append(
                EntityAction(
                    kind="create" if c.is_new else "update",
                    name=c.name,
                    entity_type=c.entity_type if c.is_new else "",
                    tags=c.tags if c.is_new else [],
                    access=c.access if c.is_new else "",
                    existing_uid=c.existing_uid,
                    observations=c.observations or st.raw.content[:2000],
                )
            )
        for name, uid_or_name, fpath in st.matched:
            if index.has_entity_format(fpath):
                st.actions.append(
                    EntityAction(
                        kind="update",
                        name=name,
                        entity_type="",
                        tags=[],
                        access="",
                        existing_uid=uid_or_name,
                        observations=st.raw.content[:2000],
                    )
                )
        if not st.actions:
            log.info("  No actions needed for %s", st.raw.ref)
            st.done = True

    # --- Phase 2 assembly: creates + unique-target merges ---
    # Group merges by target page uid: a page touched by exactly one merge
    # this run can be batched (its body is stable until the result lands);
    # a page touched by 2+ merges keeps the synchronous path, serialized
    # in intake order during finalization below.
    merge_uid_counts: dict[str, int] = {}
    for st in states:
        if st.failed or st.done or st.deferred or st.in_flight:
            continue
        for action in st.actions:
            if action.kind == "update" and action.existing_uid:
                merge_uid_counts[action.existing_uid] = (
                    merge_uid_counts.get(action.existing_uid, 0) + 1
                )

    t3_requests: list[BatchRequest] = []
    # Re-check the run budget per file before assembling its tier-3
    # requests: phase-1 spend plus earlier files' tier-3 requests may have
    # exhausted the cap by now, and the phase-1 gate alone would let every
    # admitted file bump ``api_calls`` past the cap unbounded. Mirroring
    # the sync loop's guaranteed progress (an admitted file completes all
    # its calls), the FIRST file that spends phase-2 budget proceeds even
    # at the cap, so overshoot is bounded to one file's worth of requests.
    # A deferred file keeps its raw on disk and lands in the athenaeum#220 deferred
    # manifest; its tier-2 spend is wasted — acceptable, the next run
    # re-classifies it.
    phase2_spent = False
    for i, st in enumerate(states):
        if st.failed or st.done or st.deferred or st.in_flight:
            continue
        if phase2_spent and usage.api_calls >= max_api_calls:
            log.warning(
                "API call budget exhausted (%d/%d) at phase-2 assembly — "
                "deferring %s",
                usage.api_calls,
                max_api_calls,
                st.raw.ref,
            )
            st.deferred = True
            continue
        # Fix for mid-assembly failures: if this file throws after some of
        # its requests were appended, drop them (and their attempt counts)
        # before submit so the batch carries no spend for a file that can
        # never be written.
        requests_mark = len(t3_requests)
        calls_mark = usage.api_calls
        try:
            for j, action in enumerate(st.actions):
                if action.kind == "create":
                    cid = f"t3-{i}-c{j}"
                    t3_raw_by_cid[cid] = st.raw
                    usage.api_calls += 1
                    t3_requests.append(
                        BatchRequest(
                            custom_id=cid,
                            params=tier3_create_params(
                                action,
                                st.raw.ref,
                                wiki_root=wiki_root,
                                config=config,
                            ),
                        )
                    )
                    st.create_ids.append((cid, action))
                elif action.kind == "update" and action.existing_uid:
                    existing_path = index.get_by_uid(action.existing_uid)
                    if not existing_path or not existing_path.exists():
                        log.warning(
                            "Could not find existing page for uid %s",
                            action.existing_uid,
                        )
                        continue
                    if merge_uid_counts.get(action.existing_uid, 0) > 1:
                        st.sync_merges.append(action)
                        continue
                    text = existing_path.read_text(encoding="utf-8")
                    meta, existing_body = parse_frontmatter(text)
                    # Anchor safety (issue athenaeum#562 / audit M20): a body that would
                    # break the <existing_page> fence can't use the batched patch
                    # path — hand it to the synchronous merge, which routes it to
                    # the anchor-free full-echo fallback.
                    if existing_body_needs_full_echo(existing_body):
                        st.sync_merges.append(action)
                        continue
                    cid = f"t3-{i}-m{j}"
                    t3_raw_by_cid[cid] = st.raw
                    usage.api_calls += 1
                    t3_requests.append(
                        BatchRequest(
                            custom_id=cid,
                            params=tier3_merge_params(
                                action, existing_body, st.raw.ref, config=config
                            ),
                        )
                    )
                    st.merge_ids.append(
                        (cid, action, existing_path, meta, existing_body)
                    )
            if len(t3_requests) > requests_mark or st.sync_merges:
                phase2_spent = True
        except Exception:
            log.exception("Failed to process %s", st.raw.ref)
            # Drop this file's already-appended requests and restore their
            # attempt counts — they are never submitted, so they must not
            # consume budget or batch spend.
            for dropped in t3_requests[requests_mark:]:
                t3_raw_by_cid.pop(dropped.custom_id, None)
            del t3_requests[requests_mark:]
            usage.api_calls = calls_mark
            st.failed = True

    # --- Phase 2: tier-3 batch ---
    # Issue athenaeum#483: re-check the spend ceiling before the tier-3 submit. The
    # tier-2 batch's cost is now in ``usage``, so a run that stayed under the
    # ceiling through tier-2 but would blow it on tier-3 stops here: the
    # assembled tier-3 requests are dropped (never submitted) and every file
    # with pending tier-3 work — batched creates/merges OR finalize-time sync
    # merges — is deferred rather than half-written. Finalize checks
    # ``st.deferred`` first, so a deferred file never tries to read a result
    # from the (unsubmitted) batch.
    t3_results: dict[str, Any] = {}
    t3_ceiling = spend.ceiling_tripped(usage, provider=provider, config=config)
    t3_pending = [
        st
        for st in states
        if not st.failed
        and not st.done
        and not st.deferred
        and not st.in_flight
        and (st.create_ids or st.merge_ids or st.sync_merges)
    ]
    if t3_ceiling is not None and t3_pending:
        log.error(
            "Spend ceiling reached (%s) before the tier-3 batch — deferring "
            "%d file(s) with pending writes, not submitting (issue athenaeum#483)",
            t3_ceiling,
            len(t3_pending),
        )
        for st in t3_pending:
            st.deferred = True
    elif t3_requests:
        try:
            t3_outcome = execute_batch(
                effective_write_client,
                t3_requests,
                description="tier3_write",
                usage=usage,
                knob="write",
                sleep=sleep,
                deadline=deadline,
            )
        except BatchExecutionError as exc:
            log.error("Tier-3 batch failed (%s) — affected files retried next run", exc)
        else:
            if t3_outcome.in_flight:
                # Issue athenaeum#1144: same spill as tier-2. A file whose tier-3
                # requests are in this batch cannot finalize — its
                # write-only-when-all-calls-succeeded guarantee is unmet — so
                # it goes in-flight rather than being half-written.
                _spill_to_handle(
                    t3_outcome,
                    t3_raw_by_cid,
                    knob="write",
                    cache_dir=effective_cache_dir,
                    config=config,
                    result=result,
                )
                for st in states:
                    if st.create_ids or st.merge_ids:
                        st.in_flight = True
            else:
                t3_results = t3_outcome.results

    # --- Finalize per file, in intake order ---
    # All of a file's calls must have succeeded before anything is written
    # (mirrors process_one / tier3_write's defer-writes-until-success).
    # Same-page synchronous merges execute here, serialized in intake
    # order, re-reading the page fresh so each sees the previous write.
    resolved_config = config if config is not None else load_config(wiki_root.parent)
    deferred_now: list[str] = []
    sync_merges_started = False
    for st in states:
        if st.failed:
            result.failed_refs.append(st.raw.ref)
            continue
        if st.in_flight:
            # Issue athenaeum#1144: submitted, billed, and recoverable from the
            # recorded handle. Write nothing and unlink nothing — the raw file
            # is what a later run's collect pass applies the result TO.
            result.in_flight_refs.append(st.raw.ref)
            continue
        if st.deferred:
            deferred_now.append(st.raw.ref)
            continue
        # Budget gate for the same-page synchronous merges below: each one
        # is a live API call at finalize time, so over-cap files defer here
        # too (their batched tier-3 spend is wasted — acceptable, the next
        # run redoes them). As at phase-2 assembly, the first file to run
        # sync merges proceeds even at the cap (guaranteed progress,
        # one-file overshoot — mirroring the sync loop).
        if st.sync_merges and sync_merges_started and usage.api_calls >= max_api_calls:
            log.warning(
                "API call budget exhausted (%d/%d) before synchronous "
                "merges — deferring %s",
                usage.api_calls,
                max_api_calls,
                st.raw.ref,
            )
            deferred_now.append(st.raw.ref)
            continue
        if st.done:
            result.created += len(st.created)
            result.skipped += len(st.skipped)
            if st.address_escalations:
                # Issue athenaeum#1126: this file's ONLY classification(s)
                # were declined address-shaped ones (no other actions), so
                # ``st.done`` was set at classification time with nothing
                # else to write. Flush the escalation(s) before unlinking —
                # otherwise the raw file's deletion below destroys the fact.
                tier4_escalate(
                    st.address_escalations,
                    wiki_root / "_pending_questions.md",
                    config=resolved_config,
                )
                result.escalated += len(st.address_escalations)
            st.raw.path.unlink()
            log.info("  Deleted: %s", st.raw.path)
            continue
        try:
            new_entities: list[WikiEntity] = []
            pending_updates: list[tuple[Path, str]] = []
            updated_uids: list[str] = []
            escalations: list[EscalationItem] = list(st.address_escalations)

            for cid, action in st.create_ids:
                msg = t3_results.get(cid)
                if msg is None:
                    raise _BatchItemError(cid)
                new_entities.append(
                    # Issue athenaeum#578: tier-3 create enables adaptive thinking —
                    # response_text skips any leading thinking block.
                    tier3_entity_from_text(action, response_text(msg), config=config)
                )

            for cid, action, page_path, meta, existing_body in st.merge_ids:
                msg = t3_results.get(cid)
                if msg is None:
                    raise _BatchItemError(cid)
                # Issue athenaeum#469: apply the batched patch-mode ops deterministically;
                # a live full-echo fallback runs only when the patch response is
                # unparseable, truncated, or fails to apply.
                updated_body, esc, needs_fallback = parse_merge_ops_response(
                    # Issue athenaeum#578: patch merge enables adaptive thinking —
                    # response_text skips any leading thinking block.
                    response_text(msg),
                    action,
                    st.raw.ref,
                    existing_body,
                    stop_reason=getattr(msg, "stop_reason", None),
                    wiki_root=wiki_root,
                )
                if needs_fallback:
                    updated_body, esc = tier3_merge_full(
                        action,
                        existing_body,
                        st.raw.ref,
                        AnthropicBatchClientBackend(effective_write_client),
                        usage=usage,
                        config=config,
                    )
                if esc:
                    escalations.append(esc)
                if updated_body:
                    stamp_merge_provenance(meta, config=config)
                    pending_updates.append(
                        (page_path, render_frontmatter(meta) + "\n" + updated_body)
                    )
                    updated_uids.append(action.existing_uid or "")

            if st.sync_merges:
                sync_merges_started = True
            for action in st.sync_merges:
                existing_path = index.get_by_uid(action.existing_uid or "")
                if not existing_path or not existing_path.exists():
                    log.warning(
                        "Could not find existing page for uid %s",
                        action.existing_uid,
                    )
                    continue
                text = existing_path.read_text(encoding="utf-8")
                meta, existing_body = parse_frontmatter(text)
                updated_body, esc = tier3_merge(
                    action,
                    existing_body,
                    st.raw.ref,
                    AnthropicBatchClientBackend(effective_write_client),
                    usage=usage,
                    config=config,
                    wiki_root=wiki_root,
                )
                if esc:
                    escalations.append(esc)
                if updated_body:
                    stamp_merge_provenance(meta, config=config)
                    pending_updates.append(
                        (
                            existing_path,
                            render_frontmatter(meta) + "\n" + updated_body,
                        )
                    )
                    updated_uids.append(action.existing_uid or "")

            # All calls for this file succeeded — apply writes (updates
            # first, then creates, matching the synchronous order).
            for path, content in pending_updates:
                atomic_write_text(path, content)
            for entity in new_entities:
                rendered = entity.render()
                # Same schema gate as process_one: re-parse the rendered
                # frontmatter so the validator sees the on-disk bytes.
                rendered_meta, _ = parse_frontmatter(rendered)
                validate_wiki_meta(rendered_meta)
                atomic_write_text(wiki_root / entity.filename, rendered)
                index.register(entity)
                log.info("  Created: %s → %s", entity.name, entity.filename)

            if escalations:
                tier4_escalate(
                    escalations,
                    wiki_root / "_pending_questions.md",
                    config=resolved_config,
                )

            result.created += len(new_entities)
            result.updated += len(updated_uids)
            result.escalated += len(escalations)
            result.skipped += len(st.skipped)
            st.raw.path.unlink()
            log.info("  Deleted: %s", st.raw.path)
        except _BatchItemError as exc:
            log.error(
                "Batch result failed for %s (request %s) — retried next run",
                st.raw.ref,
                exc,
            )
            result.failed_refs.append(st.raw.ref)
        except TransientAPIError as exc:
            log.error(
                "Gave up after %d retries (transient API overload) %s: %s",
                exc.attempts,
                st.raw.ref,
                type(exc.last_error).__name__,
            )
            result.failed_refs.append(st.raw.ref)
        except Exception:
            log.exception("Failed to process %s", st.raw.ref)
            result.failed_refs.append(st.raw.ref)

    # Intake order: files deferred at phase-2/finalize precede the tail
    # deferred at phase-1 assembly (raw_files[i:]).
    result.deferred_refs = deferred_now + result.deferred_refs
    return result
