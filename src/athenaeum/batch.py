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
    check_page_size_gate,
    existing_body_needs_full_echo,
    parse_merge_ops_response,
    parse_tier2_entities,
    partition_code_artifact_classifications,
    resolve_address_named_classifications,
    stamp_merge_provenance,
    tier1_programmatic_match,
    tier2_classify,
    tier2_reclassify_larger_budget,
    tier2_request_params,
    tier3_create,
    tier3_create_params,
    tier3_entity_from_text,
    tier3_merge,
    tier3_merge_full,
    tier3_merge_params,
    tier4_escalate,
)

if TYPE_CHECKING:
    from datetime import datetime

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


#: Characters per token for the submit-time input estimate. A reservation is
#: an ESTIMATE by construction — the exact figure only exists once the batch
#: returns — so this deliberately uses the standard ~4-chars-per-token
#: approximation over the assembled payloads rather than pulling in a
#: tokenizer. The settlement record carries the estimate-vs-actual delta, so
#: the accuracy of this constant is measurable from the ledger rather than
#: being an article of faith.
_CHARS_PER_TOKEN = 4.0


@dataclass
class _SpendReservation:
    """Records a batch's committed-but-unbilled cost (issue athenaeum#1147).

    ``TokenUsage.add_batch_tokens`` fires at COLLECT. Under the athenaeum#1138
    submit/collect split the submitting run's ``usage`` therefore never sees
    the cost of the batch it just submitted — and that cost is committed
    server-side and cannot be halted, so ``spend.ceiling_tripped`` is
    structurally blind to it.

    The three moments must not be collapsed: reserve at submit, settle at
    collect, and count outstanding reservations at ceiling-check time. This
    object owns the first two; :func:`athenaeum.spend.ceiling_tripped` owns
    the third.
    """

    wiki_root: Path
    config: dict[str, object] | None
    cache_dir: Path | None = None

    def reserve(
        self, *, batch_id: str, knob: str, requests: list[BatchRequest]
    ) -> None:
        if not batch_id:
            return
        est_in, est_out, model = _estimate_batch_tokens(
            requests,
            config=self.config,
            wiki_root=self.wiki_root,
            cache_dir=self.cache_dir,
        )
        # AC5: priced at the 50%-DISCOUNTED batch rate. Pricing a batch
        # reservation at the synchronous rate trips the ceiling roughly 2x too
        # early and defeats the entire purpose of the epic. Routed through
        # ``TokenUsage`` rather than a local multiplication so there is exactly
        # one pricing site in the codebase, and the discount, the cache
        # multipliers, and the per-model rate table all compose the same way
        # they do for real spend.
        priced = TokenUsage()
        priced.add_batch_tokens(est_in, est_out, model=model, knob=knob)
        spend.record_reservation(
            self.wiki_root,
            batch_id=batch_id,
            knob=knob,
            est_input_tokens=est_in,
            est_output_tokens=est_out,
            est_usd=priced.estimated_cost_usd,
            model=model,
            requests=len(requests),
            config=cast("dict[str, Any] | None", self.config),
            cache_dir=self.cache_dir,
        )

    def settle_measured(
        self,
        *,
        batch_id: str,
        knob: str,
        before: "_UsageSnapshot",
        after: "_UsageSnapshot",
        result_types: dict[str, int] | None = None,
    ) -> None:
        """Close the reservation with what ``add_batch_tokens`` actually booked.

        A batch whose every request came back ``expired`` books nothing, so
        the measured delta is genuinely ZERO — which is correct: the API
        documents an expired request as not billed (AC6). The record says so
        explicitly rather than leaving a reader to infer it from a zero.
        """
        types = result_types or {}
        source = (
            "expired"
            if after.usd == before.usd and set(types) == {"expired"}
            else "measured"
        )
        self._settle(
            batch_id=batch_id,
            knob=knob,
            actual_in=after.batch_input - before.batch_input,
            actual_out=after.batch_output - before.batch_output,
            actual_usd=after.usd - before.usd,
            reason="collected",
            actual_source=source,
        )

    def settle_at_estimate(self, *, batch_id: str, knob: str, reason: str) -> None:
        """Close a reservation whose actual is unknowable (AC6).

        A handle retired uncollected (athenaeum#1146: past retention,
        unretrievable, or with no applicable context) never yields a real
        figure — but the batch DID run and WAS billed. Settling at zero would
        under-report real spend; leaving it open would leak a permanent
        phantom charge against every future ceiling check. Settling at the
        estimate is the honest close, and ``actual_source="estimate"`` makes
        it distinguishable from a measured one.
        """
        record = self._outstanding(batch_id)
        est_usd = float(record.get("est_usd") or 0.0) if record else 0.0
        est_in = int(record.get("est_input_tokens") or 0) if record else 0
        est_out = int(record.get("est_output_tokens") or 0) if record else 0
        if record is None:
            return
        self._settle(
            batch_id=batch_id,
            knob=knob,
            actual_in=est_in,
            actual_out=est_out,
            actual_usd=est_usd,
            reason=reason,
            actual_source="estimate",
        )

    def _outstanding(self, batch_id: str) -> dict[str, Any] | None:
        for record in spend.outstanding_reservations(
            self.wiki_root, cache_dir=self.cache_dir
        ):
            if record.get("batch_id") == batch_id:
                return record
        return None

    def _settle(
        self,
        *,
        batch_id: str,
        knob: str,
        actual_in: int,
        actual_out: int,
        actual_usd: float,
        reason: str,
        actual_source: str,
    ) -> None:
        record = self._outstanding(batch_id)
        if record is None:
            # Nothing outstanding: either this batch was never reserved (a
            # caller with no reservation context) or it already settled.
            # Writing a second settlement would double-count in any future
            # reader, so this is a deliberate no-op.
            return
        spend.record_settlement(
            self.wiki_root,
            batch_id=batch_id,
            knob=knob,
            actual_input_tokens=max(0, actual_in),
            actual_output_tokens=max(0, actual_out),
            actual_usd=max(0.0, actual_usd),
            est_usd=float(record.get("est_usd") or 0.0),
            reason=reason,
            actual_source=actual_source,
            config=cast("dict[str, Any] | None", self.config),
            cache_dir=self.cache_dir,
        )


@dataclass(frozen=True)
class _UsageSnapshot:
    """The batch-attributed counters, sampled either side of a collect."""

    batch_input: int
    batch_output: int
    usd: float


def _usage_snapshot(usage: TokenUsage | None) -> _UsageSnapshot:
    if usage is None:
        return _UsageSnapshot(0, 0, 0.0)
    return _UsageSnapshot(
        batch_input=usage.batch_input_tokens,
        batch_output=usage.batch_output_tokens,
        usd=usage.estimated_cost_usd,
    )


def _estimate_batch_tokens(
    requests: list[BatchRequest],
    *,
    config: dict[str, object] | None,
    wiki_root: Path,
    cache_dir: Path | None = None,
) -> tuple[int, int, str | None]:
    """Estimate ``(input, output, model)`` for an assembled batch (AC2).

    INPUT comes from the assembled request payloads — the prompts are in hand
    at submit time, so this is the closest thing to a measurement available
    before the batch returns.

    OUTPUT follows the :func:`athenaeum.drain_advisor.observed_tokens_per_file`
    precedent: the recent spend ledger's observed output-tokens-per-file,
    falling back to that module's documented default when there is no usable
    history. Capped per request by its own ``max_tokens``, which the model
    cannot exceed — so a batch of small-budget requests is not estimated as if
    every one ran to the corpus-wide average.
    """
    if not requests:
        return 0, 0, None
    from athenaeum import drain_advisor

    est_input = 0
    for request in requests:
        params = request.params
        text_len = 0
        system = params.get("system")
        if isinstance(system, str):
            text_len += len(system)
        elif isinstance(system, list):
            text_len += sum(
                len(block.get("text", "")) for block in system if isinstance(block, dict)
            )
        for message in params.get("messages", []) or []:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                text_len += len(content)
            elif isinstance(content, list):
                text_len += sum(
                    len(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict)
                )
        est_input += int(text_len / _CHARS_PER_TOKEN)

    observed_output = drain_advisor.DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE
    try:
        ledger_path = spend.resolve_ledger_path(
            cast("dict[str, Any] | None", config),
            cache_dir=cache_dir,
            wiki_root=wiki_root,
        )
        observed = drain_advisor.observed_tokens_per_file(spend.read_ledger(ledger_path))
        if observed is not None:
            observed_output = observed[1]
    except Exception as exc:  # noqa: BLE001 — an estimate must never break a submit
        log.debug(
            "batch output estimate fell back to the default (%s): %s",
            type(exc).__name__,
            exc,
        )

    est_output = 0
    for request in requests:
        budget = request.params.get("max_tokens")
        cap = budget if isinstance(budget, int) and budget > 0 else None
        per_request = observed_output if cap is None else min(observed_output, cap)
        est_output += int(per_request)

    model = requests[0].params.get("model")
    return est_input, est_output, model if isinstance(model, str) else None


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
    reservation: "_SpendReservation | None" = None,
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
    # Issue athenaeum#1147: the submit is the moment the cost becomes committed
    # and unstoppable, so the reservation is written HERE — before the poll,
    # before any deadline spill, and before the tier-3 ceiling check that runs
    # after this call returns. Reserving after the poll would leave the very
    # window this ledger exists to cover uncovered.
    if reservation is not None:
        reservation.reserve(batch_id=batch.id, knob=knob or "", requests=requests)

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

    # Map each request's custom_id to its serving model-id so batch token
    # usage attributes per model (issue athenaeum#247). The model lives in each
    # request's params (``messages.create`` payload).
    model_by_cid: dict[str, str | None] = {
        r.custom_id: r.params.get("model") for r in requests
    }
    result_types: dict[str, int] = {}
    before = _usage_snapshot(usage)
    results = collect_batch_results(
        client,
        batch.id,
        model_by_cid,
        description=description,
        usage=usage,
        knob=knob,
        out_result_types=result_types,
    )
    # Issue athenaeum#1147: ``add_batch_tokens`` has just booked the actual, so
    # this is the settlement moment. The delta between the two snapshots IS
    # the batch's real cost — no second pricing path.
    if reservation is not None:
        reservation.settle_measured(
            batch_id=batch.id,
            knob=knob or "",
            before=before,
            after=_usage_snapshot(usage),
            result_types=result_types,
        )
    return BatchOutcome(batch_id=batch.id, results=results)


def collect_batch_results(
    client: anthropic.Anthropic,
    batch_id: str,
    model_by_cid: dict[str, str | None],
    *,
    description: str,
    usage: TokenUsage | None = None,
    knob: str | None = None,
    out_result_types: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Read an ENDED batch's results and book their token usage.

    Split out of :func:`execute_batch` (issue athenaeum#1145) so the
    across-run collect path books usage through the SAME code as the
    within-run one — including the athenaeum#247 per-model attribution, which is
    the reason *model_by_cid* is a parameter rather than being re-derived from
    the request payloads: a collecting run no longer has those payloads, only
    the models the athenaeum#1143 handle recorded at claim time.

    Returns ``{custom_id: Message}``, with ``None`` for a per-request
    ``errored`` / ``canceled`` / ``expired`` result — the caller's existing
    per-file failure path. Raises :class:`BatchExecutionError` if the results
    cannot be read at all.
    """
    results: dict[str, Any] = {cid: None for cid in model_by_cid}
    try:
        entries = with_retry(
            lambda: client.messages.batches.results(batch_id),
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
                # Issue athenaeum#1146: a per-request ``expired`` is the OPPOSITE
                # case from a batch-level "still in flight" — that request
                # never reached the model and is NOT billed, so it belongs on
                # the ordinary per-file failure path (raw stays, retried next
                # run), never on the keep-the-handle path. Counted by type so
                # the two can be told apart in the run summary rather than
                # only in log text.
                if out_result_types is not None and isinstance(rtype, str):
                    out_result_types[rtype] = out_result_types.get(rtype, 0) + 1
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
    return results


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
    # Issue athenaeum#1175: tier-3 CREATES that take the synchronous path because
    # the ``write`` knob is not batched this run. The merge counterpart
    # (``sync_merges``) already existed for same-page groups; this is its
    # create-side twin, and both run through the same finalize-time budget
    # gate.
    sync_creates: list[EntityAction] = field(default_factory=list)
    # Issue athenaeum#1175: classifications produced SYNCHRONOUSLY because the
    # ``classify`` knob is not batched this run. ``None`` means "this file's
    # classifications come from the tier-2 batch response"; a list means they
    # are already parsed and the batch-response path is skipped entirely
    # (including its athenaeum#476 truncation retry, which ``tier2_classify``
    # performs for itself).
    classified: list[Any] | None = None
    created: list[WikiEntity] = field(default_factory=list)
    # Issue athenaeum#1126: Tier-4 escalations for declined address-shaped
    # classifications, built at classification time and flushed at finalize
    # (in BOTH branches that unlink the raw file) so the fact survives even
    # when the declined address was this file's only classification. Issue
    # athenaeum#1182 reuses this SAME carrier (not a parallel channel) for
    # page-size-gate suppressions found at batch-assembly time (before a
    # merge item is ever built) -- despite the field name, this is the
    # general "escalation raised before finalize, for this file" list; both
    # kinds are flushed identically and both count toward
    # BatchRunResult.escalated.
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
    #: Issue athenaeum#1182: Tier-3 merges suppressed by the page-size
    #: invariant across BOTH of this transport's merge-dispatch sites — the
    #: batch-submission assembly loop (an over-threshold page never becomes
    #: a batch request) and the finalize-time ``sync_merges`` fallback
    #: (mirrors the synchronous transport's ``tier3_derive_actions`` exactly).
    #: Derived from the same per-file escalations flushed via
    #: ``tier4_escalate`` (by ``conflict_type == "oversize_page"``), the same
    #: way ``athenaeum.models.ProcessingResult.oversize_suppressed`` is.
    oversize_suppressed: int = 0
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


@dataclass
class _ResumedWork:
    """A previously-submitted batch's results, re-entering :func:`process_batch_run`.

    Issue athenaeum#1145. The collect path does NOT get its own write path: it
    hands the pipeline a set of already-built :class:`_FileState` objects plus
    the results the batch produced, and the SAME body runs from there —
    classification parsing, tier-3 assembly, the spend ceiling, and finalize,
    with its per-file "all calls succeeded before anything is written"
    guarantee and its raw-unlink-on-success. AC2 is structural, not a
    convention someone has to remember.

    Two entry points, one per knob:

    - a ``classify`` handle supplies *t2_results* and leaves *t3_results*
      ``None``, so the run parses the classifications, assembles a NEW tier-3
      batch, and submits it — the pipelining case (AC7), still subject to the
      spend ceiling and to athenaeum#1144's deadline;
    - a ``write`` handle supplies *t3_results* over states whose
      ``create_ids`` / ``merge_ids`` were restored from the handle, so the run
      goes straight to finalize.
    """

    states: list["_FileState"]
    t2_results: dict[str, Any] = field(default_factory=dict)
    #: ``None`` means "assemble and submit tier-3 normally".
    t3_results: dict[str, Any] | None = None


#: Version of the opaque ``work`` document :func:`_spill_to_handle` writes onto
#: an athenaeum#1143 handle. Bumped only for a shape change an older reader could
#: misread; an unknown version is ignored (the handle then retires uncollected
#: and its refs are re-claimed from scratch, which is safe — never silent).
WORK_DOC_VERSION = 1


def _action_to_json(action: EntityAction) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "name": action.name,
        "entity_type": action.entity_type,
        "tags": list(action.tags),
        "access": action.access,
        "existing_uid": action.existing_uid,
        "observations": action.observations,
    }


def _action_from_json(raw: Any) -> EntityAction | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    name = raw.get("name")
    if kind not in ("create", "update") or not isinstance(name, str) or not name:
        return None
    tags = raw.get("tags")
    uid = raw.get("existing_uid")
    return EntityAction(
        kind=kind,
        name=name,
        entity_type=str(raw.get("entity_type") or ""),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        access=str(raw.get("access") or ""),
        existing_uid=uid if isinstance(uid, str) else None,
        observations=str(raw.get("observations") or ""),
    )


def _escalation_to_json(item: EscalationItem) -> dict[str, Any]:
    return {
        "raw_ref": item.raw_ref,
        "entity_name": item.entity_name,
        "conflict_type": item.conflict_type,
        "description": item.description,
    }


def _escalation_from_json(raw: Any) -> EscalationItem | None:
    if not isinstance(raw, dict):
        return None
    ref = raw.get("raw_ref")
    if not isinstance(ref, str) or not ref:
        return None
    return EscalationItem(
        raw_ref=ref,
        entity_name=str(raw.get("entity_name") or ""),
        conflict_type=str(raw.get("conflict_type") or ""),
        description=str(raw.get("description") or ""),
    )


def _tier3_work_document(states: list["_FileState"]) -> dict[str, Any]:
    """Everything a later run needs to finalize a spilled tier-3 batch.

    A ``classify`` handle needs none of this — its refs are enough, because
    the collecting run re-runs the (programmatic, free) tier-0/1 pass and
    re-parses the classifications from the batch response. A ``write`` handle
    is the opposite: the parse already happened in the submitting run, and the
    per-request application context (which entity to create, which page to
    merge into, and the body the merge ops were generated against) exists
    nowhere else once that run exits.

    ``meta`` is deliberately NOT stored. It is re-parsed from the target page
    at collect time, so no YAML scalar (a date, most commonly) has to survive
    a JSON round-trip and come back as a string that ``render_frontmatter``
    would then write back differently. The body the ops apply to IS stored,
    because the ops were anchored against it. (Detecting a page that CHANGED
    under a pending handle is athenaeum#1146's job, not this one's.)
    """
    files: dict[str, Any] = {}
    for st in states:
        if not (st.create_ids or st.merge_ids):
            continue
        files[st.raw.ref] = {
            "path": str(st.raw.path),
            "skipped": list(st.skipped),
            "escalations": [
                _escalation_to_json(e) for e in st.address_escalations
            ],
            "sync_merges": [_action_to_json(a) for a in st.sync_merges],
            "creates": {cid: _action_to_json(a) for cid, a in st.create_ids},
            "merges": {
                cid: {
                    "action": _action_to_json(action),
                    "page_path": str(page_path),
                    "existing_body": existing_body,
                }
                for cid, action, page_path, _meta, existing_body in st.merge_ids
            },
        }
    return {"version": WORK_DOC_VERSION, "files": files}


def _ref_records(
    raw_by_cid: dict[str, RawFile], model_by_cid: dict[str, str | None]
) -> dict[str, batch_state.RefRecord]:
    """Build the handle's ``custom_id -> RefRecord`` map, models included.

    Hashing here rather than letting :func:`athenaeum.batch_state.record_handle`
    duck-type the ``RawFile`` is what lets the per-request MODEL ride along
    (athenaeum#1145 AC5) — ``record_handle`` accepts a ``RefRecord`` verbatim.
    """
    records: dict[str, batch_state.RefRecord] = {}
    for custom_id, raw in raw_by_cid.items():
        absolute = Path(raw.path).resolve()
        records[custom_id] = batch_state.RefRecord(
            ref=raw.ref,
            path=str(absolute),
            content_hash=batch_state.content_hash(absolute),
            model=model_by_cid.get(custom_id),
        )
    return records


def _spill_to_handle(
    outcome: BatchOutcome,
    raw_by_cid: dict[str, RawFile],
    *,
    knob: str,
    cache_dir: Path,
    config: dict[str, object] | None,
    result: BatchRunResult,
    model_by_cid: dict[str, str | None] | None = None,
    work: dict[str, Any] | None = None,
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
            refs=_ref_records(raw_by_cid, model_by_cid or {}),
            config=cast("dict[str, Any] | None", config),
            work=work,
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
    resume: _ResumedWork | None = None,
    batch_classify: bool = True,
    batch_write: bool = True,
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

    Issue athenaeum#1175: *batch_classify* / *batch_write* select which knobs
    this run actually batches. A knob resolved OFF takes the SYNCHRONOUS path
    inside this same function — ``tier2_classify`` per file at assembly, or
    ``tier3_create`` / ``tier3_merge`` per action at finalize — so the two can
    be mixed. The combination that matters is ``write`` batched with
    ``classify`` synchronous: ``write`` is nearly all the spend and is where a
    50% discount pays, while ``classify`` is the knob whose latency an
    operator most wants to keep interactive. Both ``True`` (every
    pre-athenaeum#1175 caller) is today's behaviour, byte-for-byte.

    Issue athenaeum#1145: *resume* re-enters this pipeline mid-way with a
    previously-submitted batch's results (see :class:`_ResumedWork`). It is
    what :func:`collect_pending_batches` uses, so a collected batch is applied
    by THIS function rather than by a second write path. When *resume* is
    supplied, *raw_files* is not read: the states come from the handle.
    """
    effective_write_client = write_client if write_client is not None else client
    effective_cache_dir = (
        cache_dir if cache_dir is not None else batch_state.resolve_cache_dir()
    )
    # Issue athenaeum#1147: one reservation recorder for the whole run, shared by
    # both phase submits and by the collect path.
    reservation = _SpendReservation(
        wiki_root=wiki_root, config=config, cache_dir=effective_cache_dir
    )
    from athenaeum.config import load_config, resolve_owner

    owner = resolve_owner(config)
    result = BatchRunResult()
    # Issue athenaeum#1145: a resumed run enters with its states already built from
    # the handle, so the tier-0/1 + phase-1 assembly loop below iterates
    # nothing (its cohort was claimed and submitted by an earlier run).
    states: list[_FileState] = list(resume.states) if resume is not None else []
    # A resumed ``write`` handle arrives with its tier-3 results already in
    # hand: its classifications were parsed by the SUBMITTING run, so it must
    # skip the tier-2 parse below entirely. Not merely a wasted pass — that
    # loop sets ``st.done`` on a state it finds no actions for, and a ``done``
    # state finalizes by unlinking the raw file and writing nothing, silently
    # destroying the very work this collect exists to apply.
    _t3_seeded = resume is not None and resume.t3_results is not None
    t2_requests: list[BatchRequest] = []
    # ``custom_id -> RawFile`` for each phase, so a deadline spill can record
    # the athenaeum#1143 handle's ref map without re-deriving it from the request ids.
    t2_raw_by_cid: dict[str, RawFile] = {}
    t3_raw_by_cid: dict[str, RawFile] = {}

    # --- Tier 0/1 + phase-1 assembly (budget gate per file, athenaeum#220) ---
    for i, raw in enumerate([] if resume is not None else raw_files):
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
                matched_names = [name for name, _, _ in st.matched]
                if not batch_classify:
                    # Issue athenaeum#1175: the ``classify`` knob is not batched
                    # this run. Call the SAME synchronous classifier the
                    # non-batch loop uses — including its own athenaeum#472
                    # repair and athenaeum#476 bigger-budget retries — rather
                    # than reimplementing tier-2 semantics here. ``api_calls``
                    # is NOT bumped by hand: ``tier2_classify`` records its
                    # own attempt through ``add_tokens``, and double-counting
                    # would eat the athenaeum#220 budget twice per file.
                    sync_stats = Tier2ParseStats()
                    st.classified = tier2_classify(
                        raw,
                        matched_names,
                        valid_types,
                        valid_tags,
                        valid_access,
                        AnthropicBatchClientBackend(client),
                        wiki_root=wiki_root,
                        usage=usage,
                        config=config,
                        stats=sync_stats,
                    )
                    result.degraded += sync_stats.degraded
                    result.truncated += sync_stats.truncated
                else:
                    st.t2_id = f"t2-{i}"
                    # Each batched request counts as one api_call attempt,
                    # recorded at assembly time (athenaeum#220 budget semantics).
                    usage.api_calls += 1
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
    t2_results: dict[str, Any] = dict(resume.t2_results) if resume is not None else {}
    # A resumed run submits no tier-2 batch, so there is no pre-submit ceiling
    # decision to make here — the athenaeum#483 gate exists to refuse a SUBMIT, and
    # refusing one that is not happening would defer every collected file
    # instead of applying results already paid for. The tier-3 gate below is
    # NOT skipped: a resumed classify handle can still submit a new tier-3
    # batch, and that submit must face the ceiling with the collected cost
    # already booked into ``usage`` (athenaeum#1145 AC1, reason 1).
    t2_ceiling = (
        None
        if resume is not None
        else spend.ceiling_tripped(
            usage,
            provider=provider,
            config=config,
            # Issue athenaeum#1147 AC4/AC7: the athenaeum#483 pre-submit check is
            # PRESERVED, not replaced — it simply now counts committed
            # in-flight cost as well as this run's own accrual.
            wiki_root=wiki_root,
            cache_dir=effective_cache_dir,
        )
    )
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
                reservation=reservation,
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
                    model_by_cid={
                        r.custom_id: r.params.get("model") for r in t2_requests
                    },
                )
                for st in states:
                    if st.t2_id is not None and not st.failed and not st.done:
                        st.in_flight = True
            else:
                t2_results = t2_outcome.results

    # Parse classifications and build per-file actions (same shape as
    # process_one: creates from tier-2, updates from tier-1 matches).
    for st in [] if _t3_seeded else states:
        if st.failed or st.done or st.deferred or st.in_flight:
            continue
        classified = []
        if st.classified is not None:
            # Issue athenaeum#1175: produced synchronously at assembly, already
            # parsed and already through tier2_classify's own repair/retry
            # ladder. The batch-response path below (and its athenaeum#476
            # truncation retry) does not apply.
            classified = st.classified
        elif st.t2_id is not None:
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
                    if not batch_write:
                        # Issue athenaeum#1175: the ``write`` knob is not batched
                        # this run — this create runs live at finalize,
                        # through the same finalize-time budget gate the
                        # same-page synchronous merges already use.
                        st.sync_creates.append(action)
                        continue
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
                    if not batch_write or merge_uid_counts.get(
                        action.existing_uid, 0
                    ) > 1:
                        # Issue athenaeum#1175: an unbatched ``write`` knob routes
                        # EVERY merge down the path same-page groups already
                        # take, so there is one synchronous merge
                        # implementation, not two.
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
                    # Issue athenaeum#1182: page-size invariant, enforced BEFORE a
                    # batch merge item is ASSEMBLED — no request is ever
                    # submitted for an over-threshold page, so it costs no
                    # spend and receives no merge. Reuses check_page_size_gate
                    # unchanged (same function tier3_derive_actions's
                    # synchronous "update" branch calls); the escalation is
                    # carried on st.address_escalations, which finalize
                    # already flushes through the SAME tier4_escalate() call
                    # every other per-file escalation on this transport uses
                    # (see that field's docstring for the "flushed in BOTH
                    # branches that unlink the raw file" contract).
                    oversize_escalation = check_page_size_gate(
                        action, existing_body, st.raw.ref, config
                    )
                    if oversize_escalation is not None:
                        st.address_escalations.append(oversize_escalation)
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
            if len(t3_requests) > requests_mark or st.sync_merges or st.sync_creates:
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
    # A resumed ``write`` handle goes straight to finalize; a resumed
    # ``classify`` handle leaves ``t3_results`` unseeded and assembles/submits
    # a new tier-3 batch below, exactly like a fresh cohort.
    t3_results: dict[str, Any] = (
        dict(resume.t3_results or {}) if resume is not None and _t3_seeded else {}
    )
    t3_ceiling = (
        None
        if _t3_seeded
        else spend.ceiling_tripped(
            usage,
            provider=provider,
            config=config,
            wiki_root=wiki_root,
            cache_dir=effective_cache_dir,
        )
    )
    t3_pending = [
        st
        for st in states
        if not st.failed
        and not st.done
        and not st.deferred
        and not st.in_flight
        and (st.create_ids or st.merge_ids or st.sync_merges or st.sync_creates)
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
                reservation=reservation,
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
                    model_by_cid={
                        r.custom_id: r.params.get("model") for r in t3_requests
                    },
                    # Issue athenaeum#1145: a tier-3 spill's application context —
                    # which entity to create, which page to merge into, the
                    # body the ops were anchored against — exists nowhere else
                    # once this run exits.
                    work=_tier3_work_document(states),
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
        _sync_work = st.sync_merges or st.sync_creates
        if _sync_work and sync_merges_started and usage.api_calls >= max_api_calls:
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
                # Issue athenaeum#1182: same derived-from-conflict_type counting
                # as the main finalize branch below and the synchronous
                # transport's _apply_tier3_results.
                result.oversize_suppressed += sum(
                    1
                    for _e in st.address_escalations
                    if _e.conflict_type == "oversize_page"
                )
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

            if st.sync_merges or st.sync_creates:
                sync_merges_started = True
            # Issue athenaeum#1175: unbatched tier-3 creates, live at finalize.
            # Ordered before the merges so a file's creates and merges execute
            # in the same relative order the batched path applies them in.
            for action in st.sync_creates:
                new_entities.append(
                    tier3_create(
                        action,
                        st.raw.ref,
                        AnthropicBatchClientBackend(effective_write_client),
                        wiki_root=wiki_root,
                        usage=usage,
                        config=config,
                    )
                )
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

                # Issue athenaeum#1182: page-size invariant, enforced BEFORE the
                # merge prompt is built or any model call is made — mirrors
                # tier3_derive_actions's synchronous "update" branch exactly
                # (same check_page_size_gate call), since this loop is that
                # same synchronous merge, just reached from the batch
                # transport's finalize step instead of process_one.
                oversize_escalation = check_page_size_gate(
                    action, existing_body, st.raw.ref, config
                )
                if oversize_escalation is not None:
                    escalations.append(oversize_escalation)
                    continue

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
            # Issue athenaeum#1182: derived from `escalations` by conflict_type,
            # mirroring athenaeum.librarian._apply_tier3_results on the
            # synchronous transport — covers BOTH this file's batched merge_ids
            # escalations and its finalize-time sync_merges escalations, since
            # both were folded into the same `escalations` list above.
            result.oversize_suppressed += sum(
                1 for _e in escalations if _e.conflict_type == "oversize_page"
            )
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


# ---------------------------------------------------------------------------
# Issue athenaeum#1145 — the collect-only adoption path.
#
# A run whose only work is collecting a PRIOR run's batch is a valid and
# useful run. This phase executes at the START of the entity phase, before
# the claim loop and before any new submission, for three independent
# reasons — any one of which alone forces the ordering:
#
#   1. Ceiling correctness. Collected batch cost enters ``usage`` via
#      ``add_batch_tokens``. Submitting first evaluates the pre-submit
#      ceiling check against a ``usage`` missing the cost the run is about to
#      book.
#   2. Index freshness. Collected tier-3 creates mutate the corpus that a new
#      cohort's ``tier1_programmatic_match`` reads. Submitting first means the
#      new cohort cannot tier-1-match entities this very run created.
#   3. Lease release. Leased refs must be released before the claim loop
#      computes the new submit set, or that set is computed against a stale
#      exclusion and either double-claims or starves.
# ---------------------------------------------------------------------------


@dataclass
class BatchCollectResult:
    """Aggregate outcome of :func:`collect_pending_batches`."""

    created: int = 0
    updated: int = 0
    escalated: int = 0
    skipped: int = 0
    degraded: int = 0
    truncated: int = 0
    #: Issue athenaeum#1182: summed from each inner process_batch_run() call's
    #: BatchRunResult.oversize_suppressed — see that field's docstring.
    oversize_suppressed: int = 0
    #: Refs whose results were applied and whose raw file was consumed. These
    #: ARE files drained this run, even though they were never in this run's
    #: claim — the athenaeum#470 backlog-drain advisor must see them.
    collected_refs: list[str] = field(default_factory=list)
    #: Refs whose application failed; raw stays on disk, re-claimed next run.
    failed_refs: list[str] = field(default_factory=list)
    #: Refs still held by a batch that had not ended at collect time, plus any
    #: whose collect pipelined into a NEW batch that then spilled (athenaeum#1144).
    in_flight_refs: list[str] = field(default_factory=list)
    #: Batch ids retired (results consumed) and kept (still in flight).
    retired_handles: list[str] = field(default_factory=list)
    kept_handles: list[str] = field(default_factory=list)
    #: Issue athenaeum#1146 AC7: every reconciliation outcome, counted by reason.
    #: Keys are drawn from :data:`RECONCILE_REASONS`. None of these is silent —
    #: the run summary renders the whole map, so an operator sees "3 handles
    #: kept in flight, 1 retired past retention, 2 refs discarded as mutated"
    #: without reading the log.
    reconciliation: dict[str, int] = field(default_factory=dict)

    def _count(self, reason: str, n: int = 1) -> None:
        if n:
            self.reconciliation[reason] = self.reconciliation.get(reason, 0) + n


#: Days the Batch API retains a batch's results. A handle older than this
#: cannot be collected however well-formed it is, so it is retired without a
#: retrieve attempt (issue athenaeum#1146 AC3).
BATCH_RETENTION_DAYS: float = 29.0

#: The reconciliation vocabulary (issue athenaeum#1146 AC7). Every terminal a
#: handle or one of its refs can reach has a name here, and every one is
#: counted — the point of the enumeration is that no outcome is silent.
RECONCILE_REASONS = (
    # Handle-level
    "collected",  # results applied, handle retired
    "in-flight",  # batch had not ended; handle kept, lease extended
    "unretrievable",  # retrieve 404'd; handle retired, refs released
    "retention-expired",  # older than BATCH_RETENTION_DAYS; retired
    "retrieve-error",  # transient failure; handle KEPT, nothing decided
    "results-unreadable",  # batch ended but results failed to read; kept
    "no-context",  # nothing applicable on the handle; retired uncollected
    "unknown-knob",  # handle knob is not classify/write; retired
    # Ref-level
    "raw-missing",  # leased raw file gone from disk; result discarded
    "raw-mutated",  # leased raw file changed under us; result discarded
    "request-expired",  # per-request expired — NOT billed, ordinary retry
    "request-errored",
    "request-canceled",
)


def _raw_file_from_record(
    record: batch_state.RefRecord,
) -> tuple[RawFile | None, str | None]:
    """Rebuild the :class:`RawFile` a ref record points at, or say why not.

    Returns ``(raw, None)`` when the file is present AND unchanged since claim
    time, else ``(None, reason)`` with a :data:`RECONCILE_REASONS` member.

    Two distinct discards, with the same remedy and different causes
    (athenaeum#1146 AC4/AC5):

    - **missing** — the file is gone. It cannot be finalized (finalize unlinks
      it on success) and there is nothing left to apply a result to.
    - **mutated** — the file is there but its bytes differ from the hash taken
      at claim time. The result describes CONTENT THAT NO LONGER EXISTS;
      applying it would write a classification of the old text onto the new.
      This is the case most likely to be missed, because everything about the
      handle still looks valid.

    An empty stored hash means the file was unreadable at claim time
    (athenaeum#1143's fail-open), so unchanged-ness cannot be PROVEN either way.
    That is treated as "not disproven" and the result is applied — the same
    direction athenaeum#1143 already chose, rather than discarding paid-for work
    on the absence of evidence.
    """
    path = Path(record.path)
    if not path.is_file():
        return None, "raw-missing"
    if record.content_hash and batch_state.content_hash(path) != record.content_hash:
        return None, "raw-mutated"
    from athenaeum.intake import RAW_FILE_RE

    source = record.ref.split("/", 1)[0] if "/" in record.ref else path.parent.name
    m = RAW_FILE_RE.match(path.name)
    return (
        RawFile(
            path=path,
            source=source,
            timestamp=m.group(1) if m else "",
            uuid8=m.group(2) if m else "",
        ),
        None,
    )


def _log_discard(reason: str, ref: str, batch_id: str) -> None:
    log.warning(
        "collect: %s — discarding the result for %s (batch %s); its raw file "
        "is re-claimed from scratch",
        "leased raw file is gone"
        if reason == "raw-missing"
        else "leased raw file CHANGED since claim time",
        ref,
        batch_id,
    )


def _states_for_classify_handle(
    handle: batch_state.PendingBatch,
    index: EntityIndex,
    *,
    config: dict[str, object] | None,
    out: "BatchCollectResult",
) -> tuple[list[_FileState], list[str]]:
    """Rebuild the per-file states a collected ``classify`` batch applies to.

    The tier-0/1 pass is re-run rather than restored: it is programmatic and
    free, and re-running it against the CURRENT index is strictly better than
    replaying a stale match set — a page created since the submit is now
    tier-1-matchable. ``tier0_passthrough`` is deliberately not re-run: a
    passthrough file never gets a tier-2 request, so no ref in a classify
    handle can be one.
    """
    states: list[_FileState] = []
    dropped: list[str] = []
    for custom_id, record in sorted(handle.refs.items()):
        raw, reason = _raw_file_from_record(record)
        if raw is None:
            _log_discard(reason or "raw-missing", record.ref, handle.batch_id)
            out._count(reason or "raw-missing")
            dropped.append(record.ref)
            continue
        st = _FileState(raw=raw)
        # Same in-memory guard the submitting run applied before assembling
        # the request, so the observations this file contributes are the same
        # bytes either transport would have produced.
        raw._content = flag_self_resolving_claims(raw.content)
        st.matched = tier1_programmatic_match(raw, index, config=config)
        for name, _uid, fpath in st.matched:
            if not index.has_entity_format(fpath):
                st.skipped.append(name)
        st.t2_id = custom_id
        states.append(st)
    return states, dropped


def _states_for_write_handle(
    handle: batch_state.PendingBatch, *, out: "BatchCollectResult"
) -> tuple[list[_FileState], list[str]]:
    """Rebuild the per-file states a collected ``write`` batch applies to.

    Everything comes from the handle's ``work`` document — see
    :func:`_tier3_work_document`. A handle with no usable document (recorded
    before athenaeum#1145, or written by a newer version this reader does not
    understand) yields no states: it retires uncollected and its refs are
    re-claimed from scratch. Wasteful, but never silent and never wrong.
    """
    work = handle.work
    if not isinstance(work, dict) or work.get("version") != WORK_DOC_VERSION:
        return [], sorted({r.ref for r in handle.refs.values()})
    files = work.get("files")
    if not isinstance(files, dict):
        return [], sorted({r.ref for r in handle.refs.values()})

    record_by_ref = {r.ref: r for r in handle.refs.values()}
    states: list[_FileState] = []
    dropped: list[str] = []
    for ref in sorted(files):
        entry = files[ref]
        record = record_by_ref.get(ref)
        if not isinstance(entry, dict) or record is None:
            dropped.append(ref)
            continue
        raw, reason = _raw_file_from_record(record)
        if raw is None:
            _log_discard(reason or "raw-missing", ref, handle.batch_id)
            out._count(reason or "raw-missing")
            dropped.append(ref)
            continue
        st = _FileState(raw=raw)
        skipped = entry.get("skipped")
        st.skipped = [str(x) for x in skipped] if isinstance(skipped, list) else []
        escalations = entry.get("escalations")
        if isinstance(escalations, list):
            st.address_escalations = [
                e
                for e in (_escalation_from_json(x) for x in escalations)
                if e is not None
            ]
        sync_merges = entry.get("sync_merges")
        if isinstance(sync_merges, list):
            st.sync_merges = [
                a for a in (_action_from_json(x) for x in sync_merges) if a is not None
            ]
        creates = entry.get("creates")
        if isinstance(creates, dict):
            for cid in sorted(creates):
                action = _action_from_json(creates[cid])
                if action is not None:
                    st.create_ids.append((cid, action))
        merges = entry.get("merges")
        if isinstance(merges, dict):
            for cid in sorted(merges):
                spec = merges[cid]
                if not isinstance(spec, dict):
                    continue
                action = _action_from_json(spec.get("action"))
                page_path = spec.get("page_path")
                existing_body = spec.get("existing_body")
                if action is None or not isinstance(page_path, str):
                    continue
                page = Path(page_path)
                if not page.is_file():
                    log.warning(
                        "collect: merge target %s is gone, dropping its result "
                        "(%s, batch %s)",
                        page_path,
                        ref,
                        handle.batch_id,
                    )
                    continue
                # ``meta`` is re-parsed from the page rather than restored from
                # the handle, so no YAML scalar has to survive a JSON round
                # trip. The BODY is the stored one: the merge ops were anchored
                # against it.
                meta, _current_body = parse_frontmatter(
                    page.read_text(encoding="utf-8")
                )
                st.merge_ids.append(
                    (
                        cid,
                        action,
                        page,
                        meta,
                        existing_body if isinstance(existing_body, str) else "",
                    )
                )
        states.append(st)
    return states, dropped


def _is_not_found(exc: BaseException) -> bool:
    """Whether *exc* is an authoritative "this batch does not exist" (AC3).

    The distinction matters more than it looks: retiring a handle on a
    TRANSIENT failure strands a live, paid-for batch forever, while keeping
    one for a batch that is genuinely gone strands its raw files behind a
    lease that will never be released. So the test is deliberately narrow —
    the SDK's own ``NotFoundError``, or an explicit HTTP 404 — and everything
    else (timeouts, connection errors, 5xx, anything unrecognised) is treated
    as transient and KEPT. ``with_retry`` has already exhausted its retries by
    the time this is consulted, so a persistent transient failure costs one
    more run's wait, not a lost batch.
    """
    if isinstance(exc, anthropic.NotFoundError):
        return True
    return getattr(exc, "status_code", None) == 404


def _retire_uncollected(
    cache_dir: Path,
    wiki_root: Path,
    handle: batch_state.PendingBatch,
    *,
    reason: str,
    out: "BatchCollectResult",
    reservation: "_SpendReservation | None" = None,
) -> None:
    """Retire a handle whose results were never applied, and RECORD it (AC8).

    Retiring releases the lease with the handle in one atomic store write, so
    the refs are claimable again on the next pass — nothing is stranded. What
    is lost is the batch's spend, which is an accepted failure mode but not an
    invisible one: a ledger record makes it recoverable after the fact.
    """
    refs = sorted({r.ref for r in handle.refs.values()})
    log.warning(
        "collect: retiring batch %s (%s) UNCOLLECTED — reason=%s; its %d raw "
        "file(s) are re-claimed from scratch and its batch spend is wasted "
        "(issue athenaeum#1146)",
        handle.batch_id,
        handle.knob,
        reason,
        len(refs),
    )
    batch_state.record_reconciliation(
        wiki_root,
        batch_id=handle.batch_id,
        knob=handle.knob,
        reason=reason,
        refs=refs,
        submitted_at=handle.submitted_at,
        cache_dir=cache_dir,
    )
    # Issue athenaeum#1147 AC6: close the reservation. This batch's real cost is
    # unknowable — it ran, it was billed, and its results are gone — so it
    # settles at the ESTIMATE. Leaving it open would leak a permanent phantom
    # charge against every future ceiling check, which is precisely the
    # failure the reservation ledger exists to prevent.
    if reservation is not None:
        reservation.settle_at_estimate(
            batch_id=handle.batch_id, knob=handle.knob, reason=reason
        )
    batch_state.retire_handle(cache_dir, handle.batch_id)
    out.retired_handles.append(handle.batch_id)
    out._count(reason)


def _keep_handle(
    cache_dir: Path,
    handle: batch_state.PendingBatch,
    *,
    reason: str,
    config: dict[str, object] | None,
    out: "BatchCollectResult",
    extend: bool,
) -> None:
    """Keep a handle (and, when *extend*, push its lease out) — AC1.

    Keeping without extending is a resubmit decision in disguise: the lease
    runs out, the claim loop hands the refs back, and the next run submits
    work that is still in flight and already paid for.
    """
    if extend:
        batch_state.extend_lease(
            cache_dir,
            handle.batch_id,
            config=cast("dict[str, Any] | None", config),
        )
    out.kept_handles.append(handle.batch_id)
    out.in_flight_refs.extend(sorted({r.ref for r in handle.refs.values()}))
    out._count(reason)


def collect_pending_batches(
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
    now: "datetime | None" = None,
    batch_classify: bool = True,
    batch_write: bool = True,
) -> BatchCollectResult:
    """Collect every outstanding athenaeum#1143 handle, oldest first.

    For each handle: retrieve the batch by id; if it has ENDED, read its
    results, apply them through :func:`process_batch_run` (via
    :class:`_ResumedWork` — the same finalize path, never a parallel one), and
    retire the handle, which releases its lease in the same atomic store write.

    Every other way a handle can fail to be collectable is enumerated and
    reconciled (issue athenaeum#1146). The distinction that governs the whole
    function, and that is routinely conflated with OPPOSITE correct responses:

    ==========================================  =================================
    Batch ``processing_status != "ended"``      Keep the handle, EXTEND the
                                                lease, do not resubmit — the
                                                work is in flight and paid for.
    Per-request ``result.type == "expired"``    That request never reached the
                                                model and is NOT billed —
                                                ordinary per-file failure path,
                                                raw stays, retried next run.
    ==========================================  =================================

    Treating the first as the second resubmits work that is already paid for
    and running. Treating the second as the first strands files forever.

    Collecting a ``classify`` handle and submitting the resulting tier-3 batch
    within the same run is supported and is the point: the resumed run reaches
    the normal tier-3 assembly, spend ceiling, and submit.
    """
    effective_cache_dir = (
        cache_dir if cache_dir is not None else batch_state.resolve_cache_dir()
    )
    reservation = _SpendReservation(
        wiki_root=wiki_root, config=config, cache_dir=effective_cache_dir
    )
    out = BatchCollectResult()
    handles = batch_state.load(effective_cache_dir)
    if not handles:
        return out

    for batch_id, handle in sorted(
        handles.items(), key=lambda kv: (kv[1].submitted_at, kv[0])
    ):
        knob = handle.knob
        reader = (
            write_client if knob == "write" and write_client is not None else client
        )

        # AC3, first half: a batch past the API's retention window cannot be
        # collected however well-formed its handle is. Checked BEFORE the
        # retrieve — it is the one un-collectable case that can be decided
        # without a network call, and deciding it locally means an expired
        # handle still reconciles when the API is unreachable.
        age = batch_state.submitted_age_days(handle, now=now)
        if age is not None and age > BATCH_RETENTION_DAYS:
            _retire_uncollected(
                effective_cache_dir,
                wiki_root,
                handle,
                reason="retention-expired",
                out=out,
                reservation=reservation,
            )
            continue

        try:
            batch = with_retry(
                lambda: reader.messages.batches.retrieve(batch_id),
                description=f"batch poll (collect {batch_id})",
            )
        except Exception as exc:  # noqa: BLE001 — narrowed by _is_not_found below
            # AC3, second half. ``with_retry`` has already exhausted its
            # retries; what is left is a decision about WHICH failure this is,
            # and it is made conservatively — see :func:`_is_not_found`.
            if _is_not_found(exc):
                _retire_uncollected(
                    effective_cache_dir,
                    wiki_root,
                    handle,
                    reason="unretrievable",
                    out=out,
                    reservation=reservation,
                )
            else:
                log.warning(
                    "collect: could not retrieve batch %s (%s: %s) — keeping "
                    "the handle; a transient failure must never retire a live "
                    "batch",
                    batch_id,
                    type(exc).__name__,
                    exc,
                )
                _keep_handle(
                    effective_cache_dir,
                    handle,
                    reason="retrieve-error",
                    config=config,
                    out=out,
                    extend=True,
                )
            continue

        if getattr(batch, "processing_status", None) != "ended":
            # AC1. INFO, not an error: most batches finish inside an hour, and
            # a run that arrives early is the expected case, not a fault.
            log.info(
                "collect: batch %s (%s) is still in flight — keeping the "
                "handle and extending its lease, not resubmitting its %d ref(s)",
                batch_id,
                knob,
                len({r.ref for r in handle.refs.values()}),
            )
            _keep_handle(
                effective_cache_dir,
                handle,
                reason="in-flight",
                config=config,
                out=out,
                extend=True,
            )
            continue

        if knob == "classify":
            states, dropped = _states_for_classify_handle(
                handle, index, config=config, out=out
            )
        elif knob == "write":
            states, dropped = _states_for_write_handle(handle, out=out)
        else:
            log.warning(
                "collect: handle %s has an unknown knob %r", batch_id, knob
            )
            _retire_uncollected(
                effective_cache_dir,
                wiki_root,
                handle,
                reason="unknown-knob",
                out=out,
                reservation=reservation,
            )
            continue

        out.failed_refs.extend(dropped)
        if not states:
            _retire_uncollected(
                effective_cache_dir,
                wiki_root,
                handle,
                reason="no-context",
                out=out,
                reservation=reservation,
            )
            continue

        model_by_cid = {cid: rec.model for cid, rec in handle.refs.items()}
        result_types: dict[str, int] = {}
        before = _usage_snapshot(usage)
        try:
            results = collect_batch_results(
                reader,
                batch_id,
                model_by_cid,
                description=f"collect {knob}",
                usage=usage,
                knob=knob,
                out_result_types=result_types,
            )
        except BatchExecutionError as exc:
            log.warning(
                "collect: could not read results for batch %s (%s) — keeping "
                "the handle for a later run",
                batch_id,
                exc,
            )
            _keep_handle(
                effective_cache_dir,
                handle,
                reason="results-unreadable",
                config=config,
                out=out,
                extend=True,
            )
            continue

        # Issue athenaeum#1147: the actual is booked, so the reservation this
        # batch's SUBMITTING run wrote (possibly on an earlier accounting day)
        # settles here — with the collect run's day and the estimate-vs-actual
        # delta. A batch whose every request expired settles at ZERO, which is
        # correct: the API documents an expired request as not billed.
        reservation.settle_measured(
            batch_id=batch_id,
            knob=knob,
            before=before,
            after=_usage_snapshot(usage),
            result_types=result_types,
        )

        # AC2 + AC7: per-request terminals, counted by type. These map onto the
        # EXISTING per-file failure path below (a ``None`` result raises
        # ``_BatchItemError`` at finalize, the raw stays on disk, the ref lands
        # in ``failed_refs``) — deliberately NOT onto the keep-the-handle path.
        for rtype, count in sorted(result_types.items()):
            out._count(f"request-{rtype}", count)

        resumed = (
            _ResumedWork(states=states, t2_results=results)
            if knob == "classify"
            else _ResumedWork(states=states, t3_results=results)
        )
        applied = process_batch_run(
            [],
            index,
            wiki_root,
            client,
            valid_types,
            valid_tags,
            valid_access,
            usage=usage,
            config=config,
            max_api_calls=max_api_calls,
            provider=provider,
            sleep=sleep,
            write_client=write_client,
            deadline=deadline,
            cache_dir=effective_cache_dir,
            resume=resumed,
            # Issue athenaeum#1175: a collected classify handle that pipelines
            # into tier-3 must honour THIS run's per-knob selection, not the
            # selection the submitting run happened to have.
            batch_classify=batch_classify,
            batch_write=batch_write,
        )

        out.created += applied.created
        out.updated += applied.updated
        out.escalated += applied.escalated
        out.skipped += applied.skipped
        out.degraded += applied.degraded
        out.truncated += applied.truncated
        out.oversize_suppressed += applied.oversize_suppressed
        out.failed_refs.extend(applied.failed_refs)
        out.in_flight_refs.extend(applied.in_flight_refs)
        unresolved = (
            set(applied.failed_refs)
            | set(applied.deferred_refs)
            | set(applied.in_flight_refs)
        )
        collected = sorted({st.raw.ref for st in states} - unresolved)
        out.collected_refs.extend(collected)
        out._count("collected", len(collected))
        # The handle's results have been consumed, whatever became of the
        # files downstream — a file whose collect pipelined into a NEW batch is
        # now held by that batch's OWN handle, and one that failed keeps its
        # raw on disk for a fresh claim. Retiring drops the handle and its
        # lease in one atomic store write.
        batch_state.retire_handle(effective_cache_dir, batch_id)
        out.retired_handles.append(batch_id)
        log.info(
            "collect: applied batch %s (%s) — created=%d updated=%d "
            "escalated=%d failed=%d",
            batch_id,
            knob,
            applied.created,
            applied.updated,
            applied.escalated,
            len(applied.failed_refs),
        )

    return out
