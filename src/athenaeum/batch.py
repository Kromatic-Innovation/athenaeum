# SPDX-License-Identifier: Apache-2.0
"""Batch API execution for the librarian's tier-2/tier-3 phases (issue #236).

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
- The run-level API budget (#220) is enforced with the same per-file
  ``>=`` gate as the synchronous loop at every point that spends calls:
  phase-1 assembly, phase-2 assembly (re-checked per file, since phase-1
  spend plus earlier files' tier-3 requests may have exhausted the cap by
  then), and the finalize-time same-page synchronous merges. Each batched
  request counts as one ``api_calls`` attempt at assembly time. To mirror
  the sync loop's guaranteed progress (an admitted file completes all its
  calls past the cap), each gate lets the FIRST file through even at the
  cap — so overshoot is bounded to one file's worth of calls per gate,
  never unbounded. Files deferred at phase 2 or finalize keep their raw
  files on disk and land in the #220 deferred manifest; their tier-2 (and,
  at finalize, batched tier-3) spend is wasted — acceptable, the next run
  redoes them.

Polling interval and timeout are module constants — deliberately not a
config surface; the nightly window is latency-tolerant.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

from athenaeum._retry import TransientAPIError, with_retry
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
from athenaeum.schemas import validate_wiki_meta
from athenaeum.self_resolving import flag_self_resolving_claims
from athenaeum.tiers import (
    parse_tier2_entities,
    parse_tier3_merge,
    stamp_merge_provenance,
    tier1_programmatic_match,
    tier2_request_params,
    tier3_create_params,
    tier3_entity_from_text,
    tier3_merge,
    tier3_merge_params,
    tier4_escalate,
)

log = logging.getLogger("athenaeum")

# Poll cadence for ``processing_status``. 30s keeps the nightly run
# responsive to the common fast-completion case without hammering the API;
# the timeout matches the Batch API's documented 24h processing ceiling.
BATCH_POLL_INTERVAL_SECONDS: float = 30.0
BATCH_POLL_TIMEOUT_SECONDS: float = 24 * 60 * 60.0


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


def execute_batch(
    client: anthropic.Anthropic,
    requests: list[BatchRequest],
    *,
    description: str,
    usage: TokenUsage | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = BATCH_POLL_INTERVAL_SECONDS,
    timeout: float = BATCH_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit *requests*, poll to completion, return ``{custom_id: Message}``.

    A ``None`` value marks a per-request ``errored`` / ``canceled`` /
    ``expired`` result — callers map those onto the existing per-file
    failure path. Token usage from succeeded results lands in *usage* via
    :meth:`TokenUsage.add_batch_tokens` (``api_calls`` attempts are counted
    at batch-assembly time by the caller, one per request — not here).

    Raises :class:`BatchExecutionError` when the batch cannot be submitted,
    polled, or collected — transient errors that exhausted their retries
    AND non-transient ones (e.g. a 400 from a malformed payload) alike, so
    a whole-batch failure always reaches the callers' per-file failure
    path instead of escaping as a run-fatal traceback — or when the batch
    does not end within *timeout* (best-effort cancel on timeout).
    ``sleep`` is injectable so tests don't wait.
    """
    if not requests:
        return {}

    payload = [{"custom_id": r.custom_id, "params": r.params} for r in requests]
    try:
        batch = with_retry(
            lambda: client.messages.batches.create(requests=payload),
            description=f"batch submit ({description})",
        )
    except Exception as exc:  # noqa: BLE001 — any submit failure is batch-fatal
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
    status = getattr(batch, "processing_status", "in_progress")
    while status != "ended":
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
        except Exception as exc:  # noqa: BLE001 — any poll failure is batch-fatal
            raise BatchExecutionError(
                f"batch poll failed ({description}): {exc}"
            ) from exc
        status = getattr(batch, "processing_status", "in_progress")

    log.info("Batch %s ended after %.0fs (%s)", batch.id, waited, description)

    results: dict[str, Any] = {r.custom_id: None for r in requests}
    # Map each request's custom_id to its serving model-id so batch token
    # usage attributes per model (issue #247). The model lives in each
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
            rtype = getattr(result, "type", "errored")
            if rtype == "succeeded":
                message = result.message
                if usage is not None:
                    inp, out, cache_w, cache_r = cache_usage_counts(message)
                    usage.add_batch_tokens(
                        inp,
                        out,
                        cache_w,
                        cache_r,
                        model=model_by_cid.get(entry.custom_id),
                    )
                results[entry.custom_id] = message
            else:
                log.warning(
                    "batch request %s ended %s (%s)",
                    entry.custom_id,
                    rtype,
                    description,
                )
    except Exception as exc:  # noqa: BLE001 — any results failure is batch-fatal
        raise BatchExecutionError(
            f"batch results failed ({description}): {exc}"
        ) from exc
    return results


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
    # (custom_id, action, page_path, meta-parsed-at-assembly)
    merge_ids: list[tuple[str, EntityAction, Path, dict]] = field(default_factory=list)
    sync_merges: list[EntityAction] = field(default_factory=list)
    created: list[WikiEntity] = field(default_factory=list)
    failed: bool = False
    done: bool = False
    # Set when the budget re-check at phase-2 assembly or before the
    # finalize-time sync merges defers this file (#220): raw stays on
    # disk, ref goes to the deferred manifest, nothing is written.
    deferred: bool = False


@dataclass
class BatchRunResult:
    """Aggregate outcome of :func:`process_batch_run`."""

    created: int = 0
    updated: int = 0
    escalated: int = 0
    skipped: int = 0
    failed_refs: list[str] = field(default_factory=list)
    deferred_refs: list[str] = field(default_factory=list)


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
    sleep: Callable[[float], None] = time.sleep,
) -> BatchRunResult:
    """Process the intake window through the Batch API phases (issue #236).

    Mirrors the per-file semantics of :func:`athenaeum.librarian.process_one`
    (tier 0/1 programmatic pass, per-file failure isolation, write-only-when-
    all-calls-succeeded, tier-4 escalation, raw deletion on success) while
    fanning the tier-2/tier-3 LLM calls out into two Messages Batch API
    submissions. See the module docstring for phase layout, budget
    semantics, and documented divergences from the synchronous loop.
    """
    from athenaeum.config import load_config, resolve_owner
    from athenaeum.librarian import tier0_passthrough

    owner = resolve_owner(config)
    result = BatchRunResult()
    states: list[_FileState] = []
    t2_requests: list[BatchRequest] = []

    # --- Tier 0/1 + phase-1 assembly (budget gate per file, #220) ---
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

            st.matched = tier1_programmatic_match(raw, index)
            for name, _uid, fpath in st.matched:
                if index.has_entity_format(fpath):
                    log.info("  T1 match (entity format): %s → %s", name, fpath.name)
                else:
                    log.info("  T1 match (old format, skip): %s → %s", name, fpath.name)
                    st.skipped.append(name)

            # Deterministic self-resolving-document guard (#300 follow-up,
            # #304): flag embedded self-confirmation claims BEFORE the
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
                # recorded at assembly time (#220 budget semantics).
                usage.api_calls += 1
                matched_names = [name for name, _, _ in st.matched]
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
    t2_results: dict[str, Any] = {}
    if t2_requests:
        try:
            t2_results = execute_batch(
                client,
                t2_requests,
                description="tier2_classify",
                usage=usage,
                sleep=sleep,
            )
        except BatchExecutionError as exc:
            log.error("Tier-2 batch failed (%s) — affected files retried next run", exc)

    # Parse classifications and build per-file actions (same shape as
    # process_one: creates from tier-2, updates from tier-1 matches).
    for st in states:
        if st.failed or st.done:
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
                text = msg.content[0].text
            except Exception:
                log.exception("Failed to process %s", st.raw.ref)
                st.failed = True
                continue
            classified = parse_tier2_entities(
                text, st.raw.ref, valid_types, valid_tags, valid_access, owner=owner
            )
            log.info(
                "  T2 classified %d new entities (%s)", len(classified), st.raw.ref
            )
        for c in classified:
            st.actions.append(
                EntityAction(
                    kind="create",
                    name=c.name,
                    entity_type=c.entity_type,
                    tags=c.tags,
                    access=c.access,
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
        if st.failed or st.done:
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
    # A deferred file keeps its raw on disk and lands in the #220 deferred
    # manifest; its tier-2 spend is wasted — acceptable, the next run
    # re-classifies it.
    phase2_spent = False
    for i, st in enumerate(states):
        if st.failed or st.done:
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
                    cid = f"t3-{i}-m{j}"
                    usage.api_calls += 1
                    t3_requests.append(
                        BatchRequest(
                            custom_id=cid,
                            params=tier3_merge_params(
                                action, existing_body, st.raw.ref, config=config
                            ),
                        )
                    )
                    st.merge_ids.append((cid, action, existing_path, meta))
            if len(t3_requests) > requests_mark or st.sync_merges:
                phase2_spent = True
        except Exception:
            log.exception("Failed to process %s", st.raw.ref)
            # Drop this file's already-appended requests and restore their
            # attempt counts — they are never submitted, so they must not
            # consume budget or batch spend.
            del t3_requests[requests_mark:]
            usage.api_calls = calls_mark
            st.failed = True

    # --- Phase 2: tier-3 batch ---
    t3_results: dict[str, Any] = {}
    if t3_requests:
        try:
            t3_results = execute_batch(
                client,
                t3_requests,
                description="tier3_write",
                usage=usage,
                sleep=sleep,
            )
        except BatchExecutionError as exc:
            log.error("Tier-3 batch failed (%s) — affected files retried next run", exc)

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
            st.raw.path.unlink()
            log.info("  Deleted: %s", st.raw.path)
            continue
        try:
            new_entities: list[WikiEntity] = []
            pending_updates: list[tuple[Path, str]] = []
            updated_uids: list[str] = []
            escalations: list[EscalationItem] = []

            for cid, action in st.create_ids:
                msg = t3_results.get(cid)
                if msg is None:
                    raise _BatchItemError(cid)
                new_entities.append(
                    tier3_entity_from_text(action, msg.content[0].text, config=config)
                )

            for cid, action, page_path, meta in st.merge_ids:
                msg = t3_results.get(cid)
                if msg is None:
                    raise _BatchItemError(cid)
                updated_body, esc = parse_tier3_merge(
                    msg.content[0].text,
                    action,
                    st.raw.ref,
                    stop_reason=getattr(msg, "stop_reason", None),
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
                    client,
                    usage=usage,
                    config=config,
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
                path.write_text(content, encoding="utf-8")
            for entity in new_entities:
                rendered = entity.render()
                # Same schema gate as process_one: re-parse the rendered
                # frontmatter so the validator sees the on-disk bytes.
                rendered_meta, _ = parse_frontmatter(rendered)
                validate_wiki_meta(rendered_meta)
                (wiki_root / entity.filename).write_text(rendered, encoding="utf-8")
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
