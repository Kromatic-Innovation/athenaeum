# SPDX-License-Identifier: Apache-2.0
"""Durable LLM-spend ledger (issue athenaeum#378).

Athenaeum runs on two cost models that must never be blended:

* the ``claude-cli`` **subscription** path — no invoice; consumes the
  operator's Claude Code subscription quota. Constrained in TOKENS.
* the metered ``anthropic`` **API** path (contradiction resolver on the api
  backend, batch mode, and the per-turn ``query-topics`` recall extractor —
  which, like every other call site, routes through the provider seam
  (:func:`athenaeum.provider.build_llm_client`) and is metered only when the
  resolved provider is ``api``). Constrained in real DOLLARS.

The in-memory :class:`~athenaeum.models.TokenUsage` accumulator is logged at
end-of-run and then DISCARDED — nothing persists spend across runs, so
"how much has athenaeum spent, and is any of it real money?" is unanswerable
from data (a code audit once mis-answered exactly this — see issue athenaeum#378).

This module appends **one JSONL record per pipeline run** to
``~/.cache/athenaeum/spend.jsonl``. Each record carries:

* ``provider`` — ``claude-cli`` vs ``anthropic``. This field is the whole
  point: it makes "are we spending real money?" an empirical question rather
  than a grep over the code.
* ``run_type`` — ``librarian`` / ``answers`` / ``query-topics`` / ... A shared
  constant set lives below (``RUN_TYPE_*``); a scheduled nightly compile
  tags itself ``librarian-nightly`` (issue athenaeum#1136) rather than the
  bare ``librarian`` an interactive/session run keeps using, so
  ``athenaeum spend --by-provider`` (:func:`summarize`'s ``by_provider``)
  can attribute burn to one apart from the other. Both belong to the SAME
  librarian *family* — see :func:`is_librarian_run_type`, which
  :mod:`athenaeum.drain_advisor` matches against instead of the exact
  ``"librarian"`` literal so a nightly row still counts toward observed
  drain throughput.
* ``models`` — the serving model-id(s).
* the FOUR token counters kept **separate** (cache-read is ~10x cheaper than
  input; collapsing them destroys the cost signal).
* ``estimated_cost_usd`` — provider-tagged: always ``0.0`` on the
  subscription path so subscription rows can never be summed into the dollar
  total.

Schema v2 (issue athenaeum#487, conforming to cwc#1629's accounting contract) adds,
additively — pre-v2 readers keep working:

* ``billing_mode`` — ``subscription`` | ``api``. The canonical vocabulary
  alongside ``subscription_covered``; real ``api`` dollars and ``subscription``
  notional are two metrics that are NEVER summed.
* ``tokens_by_model`` — per-model token attribution (``tokens x model`` is the
  fact; dollars are derived). A mixed-model row stays repriceable per model
  instead of collapsing into an unrepriceable blended total. A superset of
  hestia's ``cost-ledger.ts`` ``{input, output, total}`` shape (one reader
  serves both) plus athenaeum's cache/batch splits.
* ``notional_usd`` — the counterfactual API-rate cost of the run's tokens,
  so a subscription row reports utilization instead of reading as $0.

Pre-v2 rows (and any row with no per-model attribution) stay readable and are
counted as *unpriceable* by :func:`summarize` — never silently dropped.

Schema v3 (issue athenaeum#781) adds, additively — pre-v3 readers keep working:

* ``tokens_by_knob`` — per-KNOB token attribution, a SIBLING of
  ``tokens_by_model`` (same bucket shape), keyed by the model-knob string
  (``classify`` / ``write`` / ``resolve`` / ``topic`` / ``reasoning_t1`` /
  ``reasoning_t2`` — see ``prompt_registry._META_ROWS``, the single source of
  truth). ``tokens_by_model`` is deliberately NOT reshaped to carry this —
  it stays a superset of hestia's ``cost-ledger.ts`` shape so that cross-repo
  reader keeps working (see the ``tokens_by_model`` docstring above). A knob
  has no hestia counterpart, so it is athenaeum-only.

Pre-v3 rows (and any row with no per-knob attribution) stay readable and are
counted as *knob-unattributed* by :func:`summarize` — mirroring the
pre-v2/*unpriceable* treatment exactly: never dropped, still counted in
``record_count`` and their billing bucket.

The ledger is append-only and crash-safe: each record is a single
``O_APPEND`` write of one small line, and the reader tolerates a torn
trailing line. It records ONLY counts, model ids, run type, provider,
session id and timestamp — never prompt/response content, environment
values, or credentials.

**Repricing (issue athenaeum#788).** ``tokens_by_model`` exists so a historical row
can be repriced when a rate is corrected or made config-owned (athenaeum#783) —
:func:`reprice` is that door. It recomputes each row from its per-model
attribution against the CURRENT rate table and reports the delta against the
stored figure. It is **read-only**: the ledger is append-only by design, so
repricing reports a corrected number and never rewrites history. Rows with no
per-model attribution stay *unpriceable* — counted and reported, never dropped
and never priced at zero.

**Layering:** L3 service. Module scope imports :mod:`athenaeum.config` (L2)
and, as of issue athenaeum#980 (S5), :mod:`athenaeum.store` (L0/L1, for the
shared :func:`~athenaeum.store.append_line_durable` primitive — strictly
DOWNWARD, so no cycle). ``athenaeum.models.TokenUsage`` is a
``TYPE_CHECKING``-only import
(the type is never constructed here — callers pass their own accumulator in)
so this module carries no MODULE-SCOPE runtime dependency on
:mod:`athenaeum.models`. The athenaeum#788 reprice path takes a FUNCTION-level
import of :func:`athenaeum.models.cost_for_token_bucket` — the same
convention this module already uses for the :mod:`athenaeum.config` resolvers
— so that a reprice reuses the writer's own pricing arithmetic instead of
reimplementing (and drifting from) it. :mod:`athenaeum.models` imports nothing
from athenaeum, so no cycle is possible.
Consumed by the L4 pipeline (:mod:`athenaeum.librarian`, :mod:`athenaeum.drain`)
at the end of a run — never imports either back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.config import resolve_cache_dir
from athenaeum.store import append_line_durable

if TYPE_CHECKING:  # avoid an import cycle at runtime (models imports nothing here)
    from athenaeum.models import TokenUsage

log = logging.getLogger(__name__)

#: Schema version stamped on every record so a future reader can migrate.
#: v2 (issue athenaeum#487) adds per-model token attribution (``tokens_by_model``),
#: the ``billing_mode`` vocabulary, and the ``notional_usd`` counterfactual —
#: all ADDITIVE. Pre-v2 rows stay readable and are counted as *unpriceable*
#: (they lack per-model attribution, so they cannot be repriced), never
#: dropped. v3 (issue athenaeum#781) adds per-KNOB token attribution
#: (``tokens_by_knob``), a SIBLING field alongside ``tokens_by_model`` —
#: also ADDITIVE. Pre-v3 rows stay readable and are counted as
#: *knob-unattributed*, never dropped. See the module docstring and
#: :func:`summarize`.
LEDGER_VERSION = 3

#: The two billing modes, in cwc#1629's vocabulary. ``subscription`` notional
#: dollars and ``api`` real dollars are two metrics that are NEVER summed.
BILLING_MODE_SUBSCRIPTION = "subscription"
BILLING_MODE_API = "api"

#: Ledger filename under the cache dir.
LEDGER_FILENAME = "spend.jsonl"

#: The two transports, in the terms the ledger uses. ``resolve_provider``
#: returns ``api`` / ``claude-cli``; the ledger records the SDK path as
#: ``anthropic`` (the metered, real-dollar transport) to read naturally in a
#: report ("API $0.42") and ``claude-cli`` unchanged (the subscription path).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CLAUDE_CLI = "claude-cli"

#: The ``run_type`` vocabulary (issue athenaeum#1136). Previously every call
#: site passed a bare string literal with no shared source of truth — the
#: module docstring's ``run_type`` line above was the only place the
#: vocabulary was written down. ``RUN_TYPE_LIBRARIAN`` is UNCHANGED: every
#: pre-athenaeum#1136 caller (and every non-nightly ``athenaeum run``
#: invocation today) keeps writing this exact value, so an operator who
#: never touches ``--run-type``/``ATHENAEUM_RUN_TYPE`` sees byte-identical
#: ledger rows. ``RUN_TYPE_LIBRARIAN_NIGHTLY`` is the new value a scheduled
#: nightly compile declares itself with, so ``athenaeum spend
#: --by-provider`` can attribute burn to it separately from an interactive
#: session — see :func:`is_librarian_run_type` for why this is a FAMILY
#: (matched by prefix) rather than a second exact literal.
RUN_TYPE_LIBRARIAN = "librarian"
RUN_TYPE_LIBRARIAN_NIGHTLY = "librarian-nightly"
RUN_TYPE_ANSWERS = "answers"
RUN_TYPE_QUERY_TOPICS = "query-topics"
RUN_TYPE_MEMORY_CLASS_BACKFILL = "memory-class-backfill"


def is_librarian_run_type(run_type: object) -> bool:
    """True for ``RUN_TYPE_LIBRARIAN`` and every member of its FAMILY (athenaeum#1136).

    A family member is ``RUN_TYPE_LIBRARIAN`` itself, or any value prefixed
    ``"librarian-"`` (currently only :data:`RUN_TYPE_LIBRARIAN_NIGHTLY`, but
    written as a prefix match rather than a second exact literal so a future
    librarian-family run_type doesn't need a THIRD call site updated to
    match it). :mod:`athenaeum.drain_advisor` uses this — NOT an exact
    ``== RUN_TYPE_LIBRARIAN`` comparison — to decide whether a ledger row
    informs the observed files-per-night drain rate: matching only the bare
    literal would silently drop every nightly row from that calculation the
    moment the nightly started tagging itself distinctly, degrading drain
    advice with no error (the exact trap this function exists to close).
    Non-string input (missing/malformed ``run_type`` field on a hand-edited
    or pre-v1 ledger row) returns ``False`` rather than raising.
    """
    if not isinstance(run_type, str):
        return False
    return run_type == RUN_TYPE_LIBRARIAN or run_type.startswith(
        RUN_TYPE_LIBRARIAN + "-"
    )


def ledger_provider(resolved_provider: str | None) -> str:
    """Map a :func:`resolve_provider` value to the ledger's provider term."""
    return PROVIDER_CLAUDE_CLI if resolved_provider == "claude-cli" else PROVIDER_ANTHROPIC


def default_cache_dir() -> Path:
    """Default athenaeum cache dir (``ATHENAEUM_CACHE_DIR`` env, else
    ``~/.cache/athenaeum``).

    Issue athenaeum#521: routes through the shared resolver so the spend ledger lands
    under the same cache dir the rest of athenaeum honours, instead of ignoring
    ``ATHENAEUM_CACHE_DIR``.
    """
    return resolve_cache_dir().resolve()


def default_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the ledger path: ``<cache_dir>/spend.jsonl`` (cache dir default)."""
    base = cache_dir if cache_dir is not None else default_cache_dir()
    return Path(base) / LEDGER_FILENAME


def durable_ledger_path(wiki_root: Path, *, cache_dir: Path | None = None) -> Path:
    """The R3 ``operational``/``store-durable`` location (design note §5.2
    table row 8; issue athenaeum#980 AC4): ``<wiki_root>/spend.jsonl``, alongside
    every other operational ledger this codebase already keeps behind the
    seam (``_calibration.jsonl``, ``_axiom_governance.jsonl``, ...).

    Backward-compatible with an on-disk store that pre-dates this move: if
    the legacy ``<cache_dir>/spend.jsonl`` already has records and the new
    location does not yet exist, this still resolves to the LEGACY path —
    an existing installation keeps reading/writing exactly where it always
    has until an explicit migration copies the file forward. A fresh store
    (neither path populated yet) and an already-migrated store (the new path
    already exists) both resolve to the new, behind-the-seam location.
    """
    new_path = Path(wiki_root) / LEDGER_FILENAME
    legacy_path = default_ledger_path(cache_dir)
    if new_path.exists() or not legacy_path.exists():
        return new_path
    return legacy_path


def resolve_ledger_path(
    config: dict[str, Any] | None = None,
    *,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
) -> Path:
    """Resolve the active ledger path: explicit override else the default.

    Honours ``spend.ledger_path`` / ``ATHENAEUM_SPEND_LEDGER`` (a full file
    path) first. Otherwise: when *wiki_root* is supplied, resolves via
    :func:`durable_ledger_path` (issue athenaeum#980 AC4 — behind the seam,
    with the legacy-store fallback that function documents); when *wiki_root*
    is omitted (every caller this slice did not migrate), behavior is
    UNCHANGED from before athenaeum#980 — ``<cache_dir>/spend.jsonl``, so an
    un-migrated caller's resolution is byte-for-byte identical to today.
    """
    from athenaeum.config import resolve_spend_ledger_path

    override = resolve_spend_ledger_path(config)
    if override is not None:
        return override
    if wiki_root is not None:
        return durable_ledger_path(wiki_root, cache_dir=cache_dir)
    return default_ledger_path(cache_dir)


# ---------------------------------------------------------------------------
# Record construction + append
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


#: Per-model counters carried on ``tokens_by_model`` beyond hestia's core
#: ``{input, output, total}`` — athenaeum's cache/batch splits (athenaeum#487 keeps
#: them; athenaeum#239/#236 make them cost-relevant). A hestia-shaped reader that only
#: reads ``input``/``output``/``total`` ignores these extra keys, so one reader
#: serves both ledgers.
_PER_MODEL_DETAIL_KEYS: tuple[str, ...] = (
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "batch_input_tokens",
    "batch_output_tokens",
    "batch_cache_creation_input_tokens",
    "batch_cache_read_input_tokens",
)


def tokens_by_model(usage: "TokenUsage") -> dict[str, dict[str, int]]:
    """Per-model token attribution for a ledger row (issue athenaeum#487).

    Keyed by model-id, each value is a SUPERSET of hestia's
    ``cost-ledger.ts`` ``CostLedgerTokens`` shape — the core
    ``{input, output, total}`` (``total`` excludes cache, matching hestia so
    cwc#1627's one reader serves both ledgers) plus athenaeum's cache/batch
    detail. Sourced from :attr:`TokenUsage.per_model`, which the tier/batch
    call sites populate with the ``model=`` kwarg (athenaeum#247). Empty when the run
    tagged no model — such a row carries no per-model attribution and is
    therefore *unpriceable* (see :func:`summarize`).
    """
    out: dict[str, dict[str, int]] = {}
    for model, bucket in usage.per_model.items():
        inp = int(bucket.get("input_tokens", 0) or 0)
        outp = int(bucket.get("output_tokens", 0) or 0)
        entry = {"input": inp, "output": outp, "total": inp + outp}
        for key in _PER_MODEL_DETAIL_KEYS:
            entry[key] = int(bucket.get(key, 0) or 0)
        out[model] = entry
    return out


def tokens_by_knob(usage: "TokenUsage") -> dict[str, dict[str, int]]:
    """Per-knob token attribution for a ledger row (issue athenaeum#781).

    Same bucket shape as :func:`tokens_by_model` — a SIBLING field, not a
    reshape of it (``tokens_by_model`` keeps its existing shape so the
    hestia ``cost-ledger.ts`` reader is unaffected; a knob has no hestia
    counterpart). Sourced from :attr:`TokenUsage.per_knob`, which each call
    site populates with the same ``knob=`` string it already passes to
    :func:`athenaeum.config.resolve_model`. Empty when the run tagged no
    knob — such a row carries no per-knob attribution and is therefore
    *knob-unattributed* (see :func:`summarize`), mirroring how an
    untagged-model row is *unpriceable*.
    """
    out: dict[str, dict[str, int]] = {}
    for knob, bucket in usage.per_knob.items():
        inp = int(bucket.get("input_tokens", 0) or 0)
        outp = int(bucket.get("output_tokens", 0) or 0)
        entry = {"input": inp, "output": outp, "total": inp + outp}
        for key in _PER_MODEL_DETAIL_KEYS:
            entry[key] = int(bucket.get(key, 0) or 0)
        out[knob] = entry
    return out


def build_record(
    usage: "TokenUsage",
    *,
    run_type: str,
    provider: str,
    session_id: str | None = None,
    files_processed: int | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Build one ledger record from a :class:`TokenUsage` accumulator.

    *provider* is a :func:`resolve_provider` value (``api`` / ``claude-cli``);
    it is mapped to the ledger term via :func:`ledger_provider`. The USD figure
    is provider-tagged — ``0.0`` on the subscription path regardless of the
    accumulator's ``subscription_covered`` flag — so subscription rows can
    never be summed into a dollar total downstream.

    *files_processed* (issue athenaeum#470) is the count of raw intake files the run
    actually drained (compiled + removed from the queue). Added only when given
    so pre-athenaeum#470 readers and non-file run types (``answers`` / ``query-topics``)
    are unaffected; the backlog-drain advisor reads it to derive observed
    files-per-run throughput across runs.
    """
    prov = ledger_provider(provider)
    is_subscription = prov == PROVIDER_CLAUDE_CLI
    usd = 0.0 if is_subscription else round(usage.estimated_cost_usd, 6)
    stamp = (ts if ts is not None else _now_utc()).astimezone(timezone.utc)
    record = {
        "v": LEDGER_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "run_type": run_type,
        "provider": prov,
        # ``billing_mode`` (issue athenaeum#487, cwc#1629) is the canonical vocabulary;
        # ``subscription_covered`` is retained ADDITIVELY so pre-v2 readers keep
        # working. Real ``api`` dollars and ``subscription`` notional are never
        # summed.
        "billing_mode": BILLING_MODE_SUBSCRIPTION if is_subscription else BILLING_MODE_API,
        "subscription_covered": is_subscription,
        "session_id": session_id,
        "models": sorted(usage.per_model.keys()),
        # Per-model token attribution (issue athenaeum#487): the fact is
        # tokens x model x timestamp, so a mixed-model row stays repriceable per
        # model instead of collapsing into an unrepriceable blended total.
        "tokens_by_model": tokens_by_model(usage),
        # Per-knob token attribution (issue athenaeum#781): a SIBLING of
        # tokens_by_model, not a reshape — answers "what does the
        # contradiction resolver / write / classify / ... knob cost me?",
        # which run_type/models cannot answer on their own (see the module
        # docstring's v3 note).
        "tokens_by_knob": tokens_by_knob(usage),
        "api_calls": usage.api_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "batch_input_tokens": usage.batch_input_tokens,
        "batch_output_tokens": usage.batch_output_tokens,
        "total_tokens": usage.total_tokens,
        # ``estimated_cost_usd`` stays provider-tagged (0 on the subscription
        # path — never summed into a dollar total). ``notional_usd`` (issue
        # athenaeum#487) is the counterfactual API-rate cost of the same tokens: it
        # equals ``estimated_cost_usd`` on an api row and reveals a subscription
        # row's utilization instead of leaving it reading as $0 of activity.
        "estimated_cost_usd": usd,
        "notional_usd": round(usage.notional_cost_usd, 6),
    }
    if files_processed is not None:
        record["files_processed"] = int(files_processed)
    return record


def _append_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


def record_spend(
    usage: "TokenUsage",
    *,
    run_type: str,
    provider: str,
    session_id: str | None = None,
    files_processed: int | None = None,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
    ledger_path: Path | None = None,
) -> bool:
    """Append one spend record for a finished pipeline run. Best-effort.

    No-ops (returns ``False``) when the ledger is disabled or *usage* recorded
    nothing (no calls and no tokens). Every failure is swallowed and logged at
    debug level: a ledger write must NEVER break or slow the run it measures.
    Returns ``True`` when a record was written.

    *wiki_root*, when supplied, resolves the ledger behind the seam (issue
    athenaeum#980 AC4) via :func:`resolve_ledger_path`; omitted, resolution is
    unchanged from before that issue.
    """
    try:
        from athenaeum.config import resolve_spend_ledger_enabled

        if not resolve_spend_ledger_enabled(config):
            return False
        # Nothing happened — don't clutter the ledger with empty runs.
        # ``billable_tokens`` (issue athenaeum#1137), not ``total_tokens``: a
        # subscription run consisting only of cache traffic (zero input,
        # zero output) would otherwise read as "nothing happened" and be
        # silently dropped from the ledger, the same cache-blind bug one
        # layer down from the ceiling comparisons this issue fixes.
        if usage.api_calls == 0 and usage.billable_tokens == 0:
            return False
        record = build_record(
            usage,
            run_type=run_type,
            provider=provider,
            session_id=session_id,
            files_processed=files_processed,
        )
        target = ledger_path if ledger_path is not None else resolve_ledger_path(
            config, cache_dir=cache_dir, wiki_root=wiki_root
        )
        _append_line(target, json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — ledger must never break a run
        # Issue athenaeum#568 (H1): a failed ledger write was invisible at debug level,
        # yet ``drain.run_drain``'s MANDATORY cumulative dollar ceiling is
        # computed by re-reading this ledger — a silent failure makes it read
        # $0 forever (unbounded real spend), and reports $0 to the cross-repo
        # accounting contract (athenaeum#487), indistinguishable from an idle day. Log
        # LOUDLY (WARNING) and keep returning ``False`` so callers can act.
        log.warning(
            "spend ledger write FAILED (%s): %s — cumulative spend ceilings "
            "that read this ledger will under-count; the drain guard verifies "
            "writability at startup (issue athenaeum#568)",
            type(exc).__name__,
            exc,
        )
        return False


def record_spend_per_knob_provider(
    usage: "TokenUsage",
    knob_providers: dict[str, str],
    knob_models: dict[str, str],
    *,
    run_type: str,
    default_provider: str,
    session_id: str | None = None,
    files_processed: int | None = None,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
    ledger_path: Path | None = None,
) -> bool:
    """Split *usage* into one ledger row PER DISTINCT PROVIDER actually used
    this run (issue athenaeum#841 AC2), instead of :func:`record_spend`'s
    single ``provider=`` assumption.

    :func:`record_spend` assumes one provider served the whole run.
    Issue athenaeum#841 threads a per-knob client through the librarian
    pipeline, so a single run can now genuinely mix providers (e.g.
    ``classify`` on ``claude-cli`` while ``write`` stays on ``api``) — no
    single ``provider=`` on one ledger row is fully correct any more, and
    ``athenaeum spend --by-knob``'s ``{subscription, api, unknown}`` split
    (derived from each ROW's ``provider`` field) would misattribute every
    knob in a mixed run to whichever provider happened to be passed.

    Groups ``usage.per_knob``'s token buckets by each knob's ACTUAL resolved
    provider (*knob_providers*, keyed by knob — see
    :func:`athenaeum.librarian._arm_run_deadline`) and writes one
    :func:`record_spend` row per distinct provider, each carrying only that
    provider's own token/knob/model attribution (*knob_models* — each
    knob resolves to exactly one model for the whole run) and the correct
    ``billing_mode``. A knob with tokens but no entry in *knob_providers* (or
    *knob_models*) falls back to *default_provider* (unmodeled) — defensive,
    should not happen for a caller that resolves every knob it tags.

    A run whose knobs all resolve to ONE provider (the common case: no
    ``llm.providers.<knob>`` override, or every override agrees with the
    global default) writes exactly the SAME single row :func:`record_spend`
    would have written — byte-identical ledger output for the default
    config (mirrors this epic's AC6 elsewhere).

    ``api_calls`` and *files_processed* are NOT split per provider — the
    accumulator tracks ``api_calls`` as one run-level counter, not
    knob-attributed, so precise per-provider call counts aren't derivable.
    Both ride on the row for *default_provider* only, so summing them across
    a run's ledger rows still equals the true run-level figure exactly once,
    never double-counted or dropped. Untagged tokens (``usage.input_tokens``
    etc. minus the per-knob-tagged subset — e.g. a call site that forgot a
    ``knob=`` tag) also ride on the *default_provider* row, so no token
    silently vanishes from the ledger.
    """
    # Cache-inclusive basis (issue athenaeum#1137) — same reasoning as
    # record_spend's identical guard above; this function short-circuits
    # BEFORE ever reaching that guard for a multi-provider run, so it needs
    # the same fix independently.
    if usage.api_calls == 0 and usage.billable_tokens == 0:
        return False

    providers_used = {
        knob_providers.get(knob, default_provider) for knob in usage.per_knob
    }
    # Always include the default provider's row — it carries api_calls,
    # files_processed, and any untagged remainder even when every tagged
    # knob happens to resolve elsewhere.
    providers_used.add(default_provider)
    if len(providers_used) <= 1:
        return record_spend(
            usage,
            run_type=run_type,
            provider=default_provider,
            session_id=session_id,
            files_processed=files_processed,
            config=config,
            cache_dir=cache_dir,
            wiki_root=wiki_root,
            ledger_path=ledger_path,
        )

    from athenaeum.models import TokenUsage as _TokenUsage

    _KEYS = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    tagged_totals = {k: 0 for k in _KEYS}
    for bucket in usage.per_knob.values():
        for k in _KEYS:
            tagged_totals[k] += int(bucket.get(k, 0) or 0)

    wrote_any = False
    for provider in sorted(providers_used):
        sub = _TokenUsage()
        sub.subscription_covered = provider == "claude-cli"
        for knob, bucket in usage.per_knob.items():
            if knob_providers.get(knob, default_provider) != provider:
                continue
            # ``add_tokens`` (not ``add``) — ``api_calls`` is set explicitly
            # below from *usage*'s own counter, not incremented once per
            # knob tagged into this row.
            sub.add_tokens(
                int(bucket.get("input_tokens", 0) or 0),
                int(bucket.get("output_tokens", 0) or 0),
                int(bucket.get("cache_creation_input_tokens", 0) or 0),
                int(bucket.get("cache_read_input_tokens", 0) or 0),
                model=knob_models.get(knob),
                knob=knob,
            )
        if provider == default_provider:
            sub.api_calls = usage.api_calls
            # Untagged remainder (a call site that never tagged a knob) —
            # attributed to the default provider so the sum across rows
            # still equals the run's true totals.
            sub.add_tokens(
                max(usage.input_tokens - tagged_totals["input_tokens"], 0),
                max(usage.output_tokens - tagged_totals["output_tokens"], 0),
                max(
                    usage.cache_creation_input_tokens
                    - tagged_totals["cache_creation_input_tokens"],
                    0,
                ),
                max(
                    usage.cache_read_input_tokens
                    - tagged_totals["cache_read_input_tokens"],
                    0,
                ),
            )
        wrote = record_spend(
            sub,
            run_type=run_type,
            provider=provider,
            session_id=session_id,
            files_processed=files_processed if provider == default_provider else None,
            config=config,
            cache_dir=cache_dir,
            wiki_root=wiki_root,
            ledger_path=ledger_path,
        )
        wrote_any = wrote_any or wrote
    return wrote_any


# ---------------------------------------------------------------------------
# Reading + summarising (the `athenaeum spend` command + the ceilings)
# ---------------------------------------------------------------------------


def parse_since(spec: str, *, now: datetime | None = None) -> datetime:
    """Parse a ``--since`` value into a UTC lower-bound datetime.

    Accepts a relative window (``7d`` / ``24h`` / ``30m`` / ``2w``) or an
    absolute ISO-8601 date/datetime (``2026-07-01`` / ``2026-07-01T09:00``).
    A bare date is treated as UTC midnight. Raises :class:`ValueError` on an
    unparseable value.
    """
    now = (now if now is not None else _now_utc()).astimezone(timezone.utc)
    s = spec.strip().lower()
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if len(s) >= 2 and s[-1] in units and s[:-1].isdigit():
        return now - timedelta(**{units[s[-1]]: int(s[:-1])})
    # Absolute ISO date/datetime.
    iso = spec.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_ledger(
    ledger_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read ledger records, tolerating a torn/partial trailing line.

    Malformed lines (a crash mid-write, or hand-editing) are skipped, not
    fatal. Optional ``since`` / ``until`` bounds filter by ``ts`` (inclusive
    lower, exclusive upper); records with an unparseable ts are dropped when a
    bound is given.
    """
    if not ledger_path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        raw_text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if not isinstance(record, dict):
            continue
        if since is not None or until is not None:
            ts = _parse_ts(record.get("ts"))
            if ts is None:
                continue
            if since is not None and ts < since:
                continue
            if until is not None and ts >= until:
                continue
        records.append(record)
    return records


def _blank_bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
        # Cache-inclusive sibling of ``total_tokens`` (issue athenaeum#1137) —
        # ADDITIVE, never replaces it (AC2: total_tokens stays the hestia
        # cost-ledger.ts contract). input + output + cache_creation +
        # cache_read, matching TokenUsage.billable_tokens exactly so the
        # reported subscription figure agrees with the guarded one.
        "billable_tokens": 0,
        "api_calls": 0,
        "estimated_cost_usd": 0.0,
        "records": 0,
    }


def resolve_billing_bucket(record: dict[str, Any]) -> str:
    """Which cost path a ledger record belongs to: ``api`` / ``subscription`` / ``unknown``.

    ``billing_mode`` (v2, issue athenaeum#487) is authoritative when present and in
    the known vocabulary. For a pre-``billing_mode`` (pre-v2) row it is derived
    from ``provider``, which maps unambiguously (the api provider term -> api,
    the subscription CLI handle -> subscription).

    A row whose billing mode cannot be determined either way — no known
    ``billing_mode`` AND no recognized ``provider`` (a hand-edited or corrupt
    row) — resolves to ``"unknown"``, NEVER silently to ``api`` (issue
    athenaeum#694). Unknown must stay a DISTINCT state from zero so a consumer
    cannot mistake an undeterminable row for api spend, or for no activity.
    """
    billing_mode = record.get("billing_mode")
    if billing_mode == BILLING_MODE_API:
        return "api"
    if billing_mode == BILLING_MODE_SUBSCRIPTION:
        return "subscription"
    provider = record.get("provider")
    if provider == PROVIDER_CLAUDE_CLI:
        return "subscription"
    if provider == PROVIDER_ANTHROPIC:
        return "api"
    return "unknown"


def _record_billable_tokens(record: dict[str, Any]) -> int:
    """Cache-inclusive token count for one ledger ROW (issue athenaeum#1137).

    The record-dict sibling of :attr:`TokenUsage.billable_tokens` — same
    four-field sum, but reading a JSONL row's fields instead of a live
    accumulator's. :func:`build_record` has always written
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` as their
    own top-level fields (no ledger migration needed); a pre-cache-tracking
    row simply lacks them and they default to 0, degrading gracefully to
    the row's ``input_tokens + output_tokens`` (== its ``total_tokens``).
    """
    return (
        int(record.get("input_tokens", 0) or 0)
        + int(record.get("output_tokens", 0) or 0)
        + int(record.get("cache_creation_input_tokens", 0) or 0)
        + int(record.get("cache_read_input_tokens", 0) or 0)
    )


def _accumulate(bucket: dict[str, Any], record: dict[str, Any]) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_tokens",
        "api_calls",
    ):
        bucket[key] += int(record.get(key, 0) or 0)
    bucket["billable_tokens"] += _record_billable_tokens(record)
    bucket["estimated_cost_usd"] += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    bucket["records"] += 1


def summarize(
    records: list[dict[str, Any]],
    *,
    by_model: bool = False,
    by_provider: bool = False,
    by_knob: bool = False,
) -> dict[str, Any]:
    """Summarise ledger records, keeping the two cost paths SEPARATE.

    Returns a dict with a ``subscription`` bucket (report its TOKENS) and an
    ``api`` bucket (report its DOLLARS) — never a blended total. ``by_model``
    adds per-model sub-buckets; ``by_provider`` adds a per-run-type breakdown
    within each path; ``by_knob`` (issue athenaeum#781) adds per-knob sub-buckets,
    mirroring ``by_model`` exactly (record-level attribution, same never-a-
    blended-total split within each knob).
    """
    subscription = _blank_bucket()
    api = _blank_bucket()
    unknown = _blank_bucket()
    buckets = {"subscription": subscription, "api": api, "unknown": unknown}
    per_model: dict[str, dict[str, Any]] = {}
    per_run_type: dict[str, dict[str, Any]] = {}
    per_knob: dict[str, dict[str, Any]] = {}
    unpriceable = 0
    knob_unattributed = 0

    def _blank_slot() -> dict[str, Any]:
        return {
            "subscription": _blank_bucket(),
            "api": _blank_bucket(),
            "unknown": _blank_bucket(),
        }

    for record in records:
        path = resolve_billing_bucket(record)
        _accumulate(buckets[path], record)
        # A row with no per-model attribution (pre-v2, or a v2 run that tagged
        # no model) cannot be repriced at a new rate table (issue athenaeum#487,
        # cwc#1627's failure mode). Count it as unpriceable — it is NOT dropped
        # and stays in ``record_count`` and its billing bucket; the count just
        # tells a repricing consumer how many rows it must treat as opaque.
        if not record.get("tokens_by_model"):
            unpriceable += 1
        # Mirrors the unpriceable count above, one level down: a row with no
        # per-knob attribution (pre-v3, or a v3 run that tagged no knob)
        # cannot say WHERE its spend went — count it as knob-unattributed
        # (issue athenaeum#781). Never dropped; still in record_count and its
        # billing bucket.
        if not record.get("tokens_by_knob"):
            knob_unattributed += 1
        if by_model:
            for model in record.get("models") or ["(untagged)"]:
                slot = per_model.setdefault(model, _blank_slot())
                _accumulate(slot[path], record)
        if by_provider:
            rt = str(record.get("run_type", "(unknown)"))
            slot = per_run_type.setdefault(rt, _blank_slot())
            _accumulate(slot[path], record)
        if by_knob:
            for knob in record.get("tokens_by_knob") or ["(unattributed)"]:
                slot = per_knob.setdefault(knob, _blank_slot())
                _accumulate(slot[path], record)

    # The subscription path carries no real dollars — surface tokens only.
    subscription["estimated_cost_usd"] = 0.0
    summary: dict[str, Any] = {
        "record_count": len(records),
        # Count of rows with no per-model attribution — pre-v2 rows and any v2
        # run that tagged no model (issue athenaeum#487). Additive; the existing
        # ``subscription``/``api``/``record_count`` shape is unchanged so
        # cwc#1218's /good-morning section does not regress.
        "unpriceable_records": unpriceable,
        # Count of rows with no per-knob attribution — pre-v3 rows and any v3
        # run that tagged no knob (issue athenaeum#781). Additive, same treatment
        # as ``unpriceable_records`` one level down.
        "knob_unattributed_records": knob_unattributed,
        "subscription": subscription,
        "api": api,
        # Rows whose billing mode could not be determined (issue athenaeum#694):
        # a DISTINCT state from zero, always present (blank when none) so a
        # consumer sees an undeterminable row explicitly instead of it being
        # silently folded into ``api``. Its tokens/dollars stay isolated here and
        # are never summed into ``api`` or ``subscription``.
        "unknown": unknown,
    }
    if by_model:
        summary["by_model"] = per_model
    if by_provider:
        summary["by_run_type"] = per_run_type
    if by_knob:
        summary["by_knob"] = per_knob
    return summary


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_summary(
    summary: dict[str, Any],
    *,
    since_label: str,
    by_model: bool = False,
    by_provider: bool = False,
    by_knob: bool = False,
) -> str:
    """Render a human report that never blends dollars into subscription rows."""
    sub = summary["subscription"]
    api = summary["api"]
    lines = [f"Athenaeum spend (since {since_label}):"]
    # Headline figure is the cache-inclusive ``billable_tokens`` (issue
    # athenaeum#1137), not ``total_tokens`` — this is what the subscription
    # ceilings actually gate on, so the report and the guard agree (AC5).
    lines.append(
        f"  Subscription  {_fmt_tokens(sub['billable_tokens'])} tokens"
        f"  ({sub['api_calls']} calls, {sub['records']} run(s))"
    )
    lines.append(
        f"  API           ${api['estimated_cost_usd']:.2f}"
        f"       ({api['api_calls']} calls, {api['records']} run(s))"
    )
    # Surface undeterminable rows only when present (issue athenaeum#694) — a
    # distinct state from zero, reported in TOKENS (its billing mode, and so its
    # unit, is by definition unknown), never blended into the API dollar figure.
    unknown = summary.get("unknown")
    if unknown and unknown["records"] > 0:
        lines.append(
            f"  Unknown       {_fmt_tokens(unknown['total_tokens'])} tokens"
            f"  ({unknown['api_calls']} calls, {unknown['records']} run(s)"
            f" — billing mode undeterminable)"
        )
    if by_provider and summary.get("by_run_type"):
        lines.append("  By run type:")
        for rt, slot in sorted(summary["by_run_type"].items()):
            s, a = slot["subscription"], slot["api"]
            lines.append(
                f"    {rt:<14} sub {_fmt_tokens(s['billable_tokens'])} tok"
                f"  / api ${a['estimated_cost_usd']:.2f}"
            )
    if by_model and summary.get("by_model"):
        lines.append("  By model:")
        for model, slot in sorted(summary["by_model"].items()):
            s, a = slot["subscription"], slot["api"]
            lines.append(
                f"    {model:<28} sub {_fmt_tokens(s['billable_tokens'])} tok"
                f"  / api ${a['estimated_cost_usd']:.2f}"
            )
    if by_knob and summary.get("by_knob"):
        # Mirrors the "By model:" rendering above (issue athenaeum#781) — the
        # subscription/api split stays intact within each knob, never blended.
        lines.append("  By knob:")
        for knob, slot in sorted(summary["by_knob"].items()):
            s, a = slot["subscription"], slot["api"]
            lines.append(
                f"    {knob:<28} sub {_fmt_tokens(s['billable_tokens'])} tok"
                f"  / api ${a['estimated_cost_usd']:.2f}"
            )
    return "\n".join(lines)


def format_budget_window(budget_window: dict[str, Any]) -> str | None:
    """Render :func:`budget_window_status` as one or two human report lines.

    Issue athenaeum#1135 (AC2/6): an ADDITIONAL line alongside
    :func:`format_summary`'s existing ``--since``-window totals — never a
    replacement for them (the ``--since`` default stays 7d; see that
    function's docstring for why narrowing it would lose information). Skips
    a path entirely when its ceiling is unconfigured (``configured=False``),
    mirroring :func:`spend_headroom_warning`'s "an unset ceiling never
    reports" contract. Returns ``None`` when NEITHER path is configured, so
    the caller can omit the section rather than print an empty header.
    """
    lines: list[str] = []
    api = budget_window["api"]
    if api["configured"]:
        lines.append(
            f"  Budget window (today, API)   ${api['consumed_usd']:.2f}"
            f" / ${api['cap_usd']:.2f}"
            f"  ({api['fraction_consumed'] * 100:.0f}%)"
        )
    sub = budget_window["subscription"]
    if sub["configured"]:
        lines.append(
            f"  Budget window (today, sub)   {_fmt_tokens(int(sub['consumed_tokens']))}"
            f" / {_fmt_tokens(int(sub['cap_tokens']))} tokens"
            f"  ({sub['fraction_consumed'] * 100:.0f}%)"
        )
    if not lines:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Repricing (issue athenaeum#788) — recompute history at CURRENT rates, read-only
# ---------------------------------------------------------------------------


def _blank_reprice_bucket() -> dict[str, Any]:
    return {
        # Stored figures, summed over EVERY record in this billing bucket —
        # including the unpriceable ones, so the bucket's stored total still
        # reconciles against ``summarize``.
        "stored_usd": 0.0,
        "stored_notional_usd": 0.0,
        # Stored figures for the REPRICEABLE subset only. The like-for-like
        # base for the delta below — comparing a repriced subset against a
        # stored total that also covers unpriceable rows would report a
        # difference that is really just the rows repricing could not touch.
        "stored_usd_priceable": 0.0,
        "stored_notional_usd_priceable": 0.0,
        # Recomputed at the CURRENT active rate table, repriceable rows only.
        "repriced_usd": 0.0,
        "repriced_notional_usd": 0.0,
        # repriced - stored_*_priceable.
        "delta_usd": 0.0,
        "delta_notional_usd": 0.0,
        "records": 0,
        "repriced_records": 0,
        # Rows in this bucket carrying no per-model attribution, and the stored
        # dollars they account for. Reported, never dropped and never zeroed.
        "unpriceable_records": 0,
        "unpriceable_stored_usd": 0.0,
        "unpriceable_stored_notional_usd": 0.0,
    }


def reprice_record(record: dict[str, Any]) -> float | None:
    """Recompute one record's API-rate cost from ``tokens_by_model`` (athenaeum#788).

    Returns the recomputed dollar figure at the CURRENT active rate table
    (:func:`athenaeum.models.configure_model_rates` — so an operator's
    ``athenaeum.yaml`` ``pricing:`` section applies), or ``None`` when the row
    carries no per-model attribution and is therefore *unpriceable* — the same
    condition :func:`summarize` counts as ``unpriceable_records``. ``None`` is
    deliberately NOT ``0.0``: a pre-v2 row (or any run that tagged no model) has
    an unknown price, not a zero one, and a caller must be able to tell them
    apart (issue athenaeum#788's third acceptance criterion).

    This is a pure computation over the record dict — it never touches the
    ledger file. The ledger is append-only by design; repricing REPORTS a
    corrected figure, it does not rewrite history.

    The per-model arithmetic (cache multipliers, the Batch API 50% discount,
    longest-prefix rate match) is delegated to
    :func:`athenaeum.models.cost_for_token_bucket` — the same code path that
    priced the row when it was written, so a reprice can never drift from the
    writer's formula. That import is function-level to keep this module's
    MODULE-scope runtime dependency set at :mod:`athenaeum.config` alone (see
    the module docstring's layering note); :mod:`athenaeum.models` imports
    nothing from athenaeum, so there is no cycle to create.
    """
    from athenaeum.models import cost_for_token_bucket

    by_model = record.get("tokens_by_model")
    if not isinstance(by_model, dict) or not by_model:
        return None
    total = 0.0
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        total += cost_for_token_bucket(model, bucket)
    return total


def reprice(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Reprice ledger records at the CURRENT rates, keeping the paths SEPARATE.

    Mirrors :func:`summarize`'s contract exactly, one dimension over: a
    ``subscription`` / ``api`` / ``unknown`` split that is NEVER blended (issue
    athenaeum#487, athenaeum#694), reporting for each bucket the stored figure, the
    recomputed figure, and the delta between them.

    The UNIT differs per path, as everywhere else in this module:

    * **api** — real dollars. ``stored_usd`` is what the row recorded;
      ``repriced_usd`` is the same tokens at today's table.
    * **subscription** — real dollars are ``0.0`` on this path by construction
      and STAY ``0.0`` after repricing (a subscription row never becomes real
      money). The figure repricing actually corrects here is the counterfactual
      ``notional_usd`` (issue athenaeum#487), so the subscription bucket's signal is
      ``*_notional_usd``. Both are carried on every bucket for a uniform shape,
      but they are never summed with each other.
    * **unknown** — an undeterminable row (issue athenaeum#694) is repriced within its
      own bucket and never folded into ``api``.

    Rows with no ``tokens_by_model`` are counted as ``unpriceable_records``,
    per bucket and at the top level, and their STORED dollars are reported as
    ``unpriceable_stored_usd``. They are never dropped from ``record_count``,
    never contribute to ``repriced_usd``, and never silently price at zero.
    The delta is computed against ``stored_usd_priceable`` — the stored value of
    exactly the rows that were repriced — so it is a like-for-like comparison.

    Read-only: like :func:`reprice_record`, this only reads the records handed
    to it. Nothing in the reprice path opens the ledger for writing.
    """
    buckets = {
        "subscription": _blank_reprice_bucket(),
        "api": _blank_reprice_bucket(),
        "unknown": _blank_reprice_bucket(),
    }
    unpriceable = 0

    for record in records:
        bucket = buckets[resolve_billing_bucket(record)]
        is_subscription = bucket is buckets["subscription"]
        stored_usd = float(record.get("estimated_cost_usd", 0.0) or 0.0)
        stored_notional = float(record.get("notional_usd", 0.0) or 0.0)
        bucket["records"] += 1
        bucket["stored_usd"] += stored_usd
        bucket["stored_notional_usd"] += stored_notional

        recomputed = reprice_record(record)
        if recomputed is None:
            unpriceable += 1
            bucket["unpriceable_records"] += 1
            bucket["unpriceable_stored_usd"] += stored_usd
            bucket["unpriceable_stored_notional_usd"] += stored_notional
            continue

        bucket["repriced_records"] += 1
        bucket["stored_usd_priceable"] += stored_usd
        bucket["stored_notional_usd_priceable"] += stored_notional
        # The subscription path is not billed, so its recomputed REAL dollars
        # stay 0.0 exactly as ``build_record`` writes them — repricing must
        # never turn subscription notional into money owed. Its recomputed
        # figure lands on ``notional`` alone.
        bucket["repriced_usd"] += 0.0 if is_subscription else recomputed
        bucket["repriced_notional_usd"] += recomputed

    for bucket in buckets.values():
        bucket["delta_usd"] = bucket["repriced_usd"] - bucket["stored_usd_priceable"]
        bucket["delta_notional_usd"] = (
            bucket["repriced_notional_usd"] - bucket["stored_notional_usd_priceable"]
        )

    return {
        "record_count": len(records),
        "unpriceable_records": unpriceable,
        "repriced_records": sum(b["repriced_records"] for b in buckets.values()),
        **buckets,
    }


def format_reprice(reprice_summary: dict[str, Any], *, since_label: str) -> str:
    """Render a repricing report: stored vs recomputed vs delta, never blended."""
    sub = reprice_summary["subscription"]
    api = reprice_summary["api"]
    unknown = reprice_summary["unknown"]
    lines = [
        f"Athenaeum spend reprice (since {since_label}) "
        f"— recomputed at CURRENT rates; ledger NOT modified:"
    ]
    lines.append(
        f"  API           stored ${api['stored_usd_priceable']:.4f}"
        f"  ->  repriced ${api['repriced_usd']:.4f}"
        f"  (delta {api['delta_usd']:+.4f}, {api['repriced_records']} row(s))"
    )
    # Subscription real dollars are $0 stored and $0 repriced by construction —
    # report the NOTIONAL, which is the figure repricing actually corrects here.
    lines.append(
        f"  Subscription  notional ${sub['stored_notional_usd_priceable']:.4f}"
        f"  ->  ${sub['repriced_notional_usd']:.4f}"
        f"  (delta {sub['delta_notional_usd']:+.4f}, {sub['repriced_records']} row(s))"
    )
    if unknown["records"] > 0:
        lines.append(
            f"  Unknown       notional ${unknown['stored_notional_usd_priceable']:.4f}"
            f"  ->  ${unknown['repriced_notional_usd']:.4f}"
            f"  (delta {unknown['delta_notional_usd']:+.4f},"
            f" {unknown['repriced_records']} row(s)"
            f" — billing mode undeterminable)"
        )
    tail = (
        f"  Repriced {reprice_summary['repriced_records']} of "
        f"{reprice_summary['record_count']} row(s); "
        f"{reprice_summary['unpriceable_records']} unpriceable "
        f"(no per-model attribution — reported, not dropped, not zeroed)"
    )
    # Name the stored dollars repricing could NOT touch, not just the row
    # count: a reader who sees only a count has no way to tell whether the
    # opaque rows are a rounding error or most of the bill, and "not zeroed"
    # should be legible in the human report, not only in --json.
    stranded = sum(
        b["unpriceable_stored_usd"]
        for b in (sub, api, unknown)
    )
    if reprice_summary["unpriceable_records"]:
        tail += f", holding ${stranded:.4f} of stored API cost"
    lines.append(tail + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spend ceiling (issue athenaeum#378, part 4) — halt the pass on breach
# ---------------------------------------------------------------------------


def _start_of_accounting_day(
    now: datetime, config: dict[str, Any] | None = None
) -> datetime:
    """Midnight of the CONFIGURED accounting day containing *now* (issue athenaeum#1136).

    Renamed from ``_start_of_utc_day`` — that name stopped being accurate the
    moment the day boundary became configurable. Resolves the accounting
    timezone via :func:`athenaeum.config.resolve_spend_accounting_timezone`
    (default: the operator's own system-local timezone, NOT UTC — see that
    function's docstring for the starvation bug this fixes), converts *now*
    into it, truncates to that zone's midnight, and converts the result BACK
    to UTC — :func:`read_ledger`'s ``since=`` bound compares against each
    record's ``ts``, which the ledger always stores in UTC (see
    :func:`build_record`), so the boundary itself must be UTC even though it
    was computed in local wall-clock time.
    """
    from athenaeum.config import resolve_spend_accounting_timezone

    tz = resolve_spend_accounting_timezone(config)
    local_now = now.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def spend_today(
    ledger_path: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Subscription tokens and API dollars recorded SO FAR in the current
    ACCOUNTING day (issue athenaeum#1136 — see
    :func:`athenaeum.config.resolve_spend_accounting_timezone`; the window
    is UTC-midnight-aligned only when that resolves to UTC, no longer
    unconditionally).

    Reads the ledger (tolerating torn lines). Used by the per-day ceiling to
    account for spend already committed by earlier runs today. *config* is
    passed through to the accounting-timezone resolver — omitted, it falls
    back to the system-local default exactly like every other caller here
    that doesn't have a config in scope.
    """
    now = now if now is not None else _now_utc()
    records = read_ledger(ledger_path, since=_start_of_accounting_day(now, config))
    tokens = 0
    usd = 0.0
    for record in records:
        if record.get("provider") == PROVIDER_CLAUDE_CLI:
            # Cache-inclusive basis (issue athenaeum#1137), not
            # ``total_tokens`` — the per-day ceiling this feeds
            # (``ceiling_tripped``) compares the current run's
            # ``usage.billable_tokens`` against this same prior-spend
            # figure, so both sides of the comparison must use the same
            # basis or the sum undercounts real consumption.
            tokens += _record_billable_tokens(record)
        else:
            usd += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    return {"subscription_tokens": float(tokens), "api_usd": usd}


# ---------------------------------------------------------------------------
# Spend headroom (issue athenaeum#926) — warn BEFORE a ceiling trips
# ---------------------------------------------------------------------------


def _headroom_slot(cap_usd: float | None, consumed_usd: float) -> dict[str, Any]:
    """Build one headroom slot (per-run or per-day).

    ``configured=False`` (cap unset) reports ``remaining_usd`` and
    ``fraction_consumed`` as ``None`` — a DISTINCT value from ``0``, so an
    unset ceiling can never be mistaken for one sitting at 0% (or, worse,
    read as fully exhausted by a caller that treats a missing fraction as 1.0
    or a missing remaining as 0.0). ``consumed_usd`` is always a real number
    (it is simply "what this run/day has spent"), regardless of whether a cap
    is configured to compare it against.
    """
    if cap_usd is None:
        return {
            "configured": False,
            "cap_usd": None,
            "consumed_usd": consumed_usd,
            "remaining_usd": None,
            "fraction_consumed": None,
        }
    return {
        "configured": True,
        "cap_usd": cap_usd,
        "consumed_usd": consumed_usd,
        # Not clamped at 0 — a negative remaining figure is the amount OVER
        # the cap, which the warning/trip messages want to name.
        "remaining_usd": cap_usd - consumed_usd,
        "fraction_consumed": consumed_usd / cap_usd,
    }


def _token_headroom_slot(
    cap_tokens: float | None, consumed_tokens: float
) -> dict[str, Any]:
    """Token-denominated sibling of :func:`_headroom_slot`, for the subscription path.

    Same ``configured``/``None``-vs-``0`` contract as :func:`_headroom_slot` —
    kept as a separate function (rather than a unit parameter on the dollar
    one) so neither report shape has to guess which unit a caller meant.
    """
    if cap_tokens is None:
        return {
            "configured": False,
            "cap_tokens": None,
            "consumed_tokens": consumed_tokens,
            "remaining_tokens": None,
            "fraction_consumed": None,
        }
    return {
        "configured": True,
        "cap_tokens": cap_tokens,
        "consumed_tokens": consumed_tokens,
        "remaining_tokens": cap_tokens - consumed_tokens,
        "fraction_consumed": consumed_tokens / cap_tokens,
    }


def budget_window_status(
    config: dict[str, Any] | None,
    *,
    ledger_path: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Today's spend against the configured per-day ceilings (issue athenaeum#1135 AC2/6).

    The ``athenaeum spend`` report's ``--since`` window (default 7d) answers
    "how much has this cost over time" — a DIFFERENT question from "is
    today's spend approaching the ceiling that would stop a run RIGHT NOW".
    Before this issue there was no single figure that put the two together:
    an operator reading the 7-day report had no way to tell whether the
    day-scoped budget ``athenaeum run`` actually enforces (via
    :func:`ceiling_tripped`) was anywhere close to tripping. This reads the
    SAME day-scoped window :func:`spend_today` (and therefore
    :func:`ceiling_tripped`) uses, reported against BOTH per-day ceilings —
    API dollars and subscription tokens — independently, mirroring this
    module's never-blended-total philosophy (never combined into one
    number). A pure read (no ledger write); never raises on its own — same
    contract as :func:`spend_headroom`, which this is a day-scoped-report
    sibling of (that one is per-run-usage-aware and warns pre-trip during a
    run; this one is a standalone report a human or ``librarian-run-degraded``
    marker line can consult at any time).
    """
    from athenaeum.config import (
        resolve_spend_max_tokens_per_day,
        resolve_spend_max_usd_per_day,
    )

    target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
    today = spend_today(target, config=config, now=now)
    usd_cap = resolve_spend_max_usd_per_day(config)
    token_cap = resolve_spend_max_tokens_per_day(config)
    return {
        "api": _headroom_slot(usd_cap, today["api_usd"]),
        "subscription": _token_headroom_slot(
            token_cap, today["subscription_tokens"]
        ),
    }


def spend_headroom(
    usage: "TokenUsage",
    *,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Report remaining headroom against the metered API dollar ceilings (athenaeum#926).

    ``ceiling_tripped`` answers "did a ceiling breach?" and, below the
    threshold, reports nothing at all — the gap this closes (see the issue's
    "The gap, precisely" section, which quotes exactly the two dollar checks
    this mirrors: ``usage.estimated_cost_usd >= run_cap_usd`` and
    ``prior + usage >= day_cap_usd``). This function reports the SAME two
    figures — remaining dollars and fraction consumed — for BOTH the per-run
    and per-day API dollar caps, independently of whether either one has
    tripped, so a run at 74% and a run at 1% stop reading identically.

    Returns ``{"per_run": <slot>, "per_day": <slot>}``, each slot built by
    :func:`_headroom_slot`. Scoped to the metered ``anthropic``/API dollar
    ceilings (:func:`athenaeum.config.resolve_spend_max_usd_per_run` /
    :func:`resolve_spend_max_usd_per_day`) — the pair the issue's motivation
    and gap sections are entirely about; the subscription TOKEN ceilings
    already have no such gap (:func:`ceiling_tripped`'s token branch is
    unconditional in the sense that this issue does not extend to it, and
    ``usage.estimated_cost_usd`` is ``0.0`` by construction on a
    subscription-covered run, so a subscription run's dollar headroom simply
    reads as ~0% consumed rather than something misleading).

    A pure computation over its inputs plus one ledger READ (never a write) —
    mirrors :func:`ceiling_tripped`'s own use of :func:`spend_today` for the
    per-day figure, so "prior spend today" is computed identically in both
    places.
    """
    from athenaeum.config import resolve_spend_max_usd_per_day, resolve_spend_max_usd_per_run

    run_cap_usd = resolve_spend_max_usd_per_run(config)
    day_cap_usd = resolve_spend_max_usd_per_day(config)

    consumed_run = usage.estimated_cost_usd
    target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
    prior_today = spend_today(target, config=config, now=now)["api_usd"]
    consumed_day = prior_today + consumed_run

    return {
        "per_run": _headroom_slot(run_cap_usd, consumed_run),
        "per_day": _headroom_slot(day_cap_usd, consumed_day),
    }


def spend_headroom_warning(
    usage: "TokenUsage",
    *,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Human-readable warning when a run ends close to an API dollar cap (athenaeum#926).

    "Close" means at or above
    :func:`athenaeum.config.resolve_spend_warning_threshold_pct` (default 75%)
    of fraction consumed, computed via :func:`spend_headroom`. Checked for
    BOTH caps independently and named separately in the message — a per-run
    warning and a per-day warning call for different operator actions (the
    former means slow down THIS run's prompting, the latter means the day's
    budget is running out regardless of any one run), so folding them into a
    single undifferentiated message would lose the actionable distinction.

    Fires regardless of whether the ceiling actually trips: at or past 100%
    this still returns a message (the trip itself, and the decision to stop,
    stay :func:`ceiling_tripped`'s job — this is only the human-visible
    signal that headroom is running out, which today has no signal at all
    below the trip line).

    Returns ``None`` when neither cap is configured (an unset ceiling never
    warns — see :func:`_headroom_slot`) or both configured caps are below the
    threshold.
    """
    from athenaeum.config import resolve_spend_warning_threshold_pct

    threshold_fraction = resolve_spend_warning_threshold_pct(config) / 100.0
    headroom = spend_headroom(
        usage, config=config, ledger_path=ledger_path, cache_dir=cache_dir, now=now
    )

    parts: list[str] = []
    for cap_key, label in (("per_run", "per-run"), ("per_day", "per-day")):
        slot = headroom[cap_key]
        if not slot["configured"]:
            continue
        fraction = slot["fraction_consumed"]
        if fraction is None or fraction < threshold_fraction:
            continue
        parts.append(
            f"{label} API dollar cap at {fraction * 100:.0f}% "
            f"(${slot['consumed_usd']:.2f}/${slot['cap_usd']:.2f} spent, "
            f"${slot['remaining_usd']:.2f} remaining)"
        )

    if not parts:
        return None
    return "spend headroom warning: " + "; ".join(parts)


def ceiling_tripped(
    usage: "TokenUsage",
    *,
    provider: str,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return a human reason when a configured spend ceiling is breached, else None.

    The path determines the UNIT: the ``claude-cli`` subscription path is
    bounded in TOKENS (per-run and per-day), the metered ``anthropic`` API path
    in DOLLARS (per-run and per-day). The per-day figures add spend already
    committed earlier today (from the ledger) to the current run's accrual.
    The subscription TOKEN figure is ``usage.billable_tokens`` (issue
    athenaeum#1137) — input + output + cache-creation + cache-read — NOT
    ``usage.total_tokens``, which excludes cache and undercounted a real
    recorded run by ~56x; see :attr:`athenaeum.models.TokenUsage.billable_tokens`.
    A FIFTH, subscription-only ceiling (issue athenaeum#785) derives a second
    per-day token figure from a declared weekly limit and a percentage of it
    (``weekly_token_limit / 7 * max_pct_per_day / 100``); it is independent of
    the absolute per-day token ceiling above and never touches the API branch.
    Returns ``None`` when no ceiling is configured or none is breached — a
    ceiling is strictly opt-in.

    Issue athenaeum#926: on the metered API path, this is also the SAME path a
    headroom warning is surfaced on — every caller that checks for a trip
    (``librarian.py``, ``merge.py``, ``batch.py``) already funnels through
    here, so a warning logged from inside this function reaches all of them
    for free, using the exact log stream a trip's own ``log.error`` already
    goes to. The warning check runs BEFORE the trip checks below and does not
    gate on their outcome, so a run at or past the ceiling both trips AND
    still logs the warning (never a trip with no warning).
    """
    from athenaeum.config import (
        resolve_spend_max_pct_per_day,
        resolve_spend_max_tokens_per_day,
        resolve_spend_max_tokens_per_run,
        resolve_spend_max_usd_per_day,
        resolve_spend_max_usd_per_run,
        resolve_spend_weekly_token_limit,
    )

    is_subscription = ledger_provider(provider) == PROVIDER_CLAUDE_CLI

    if is_subscription:
        # Cache-inclusive basis (issue athenaeum#1137): ``usage.billable_tokens``,
        # not ``usage.total_tokens``. The subscription path's real
        # consumption lands overwhelmingly in prompt-cache traffic
        # (cache_creation_input_tokens / cache_read_input_tokens), which
        # ``total_tokens`` excludes by design (it is the hestia
        # cost-ledger.ts contract — see ``tokens_by_model``'s docstring). A
        # ceiling gated on ``total_tokens`` alone measured ~56x too small
        # against a real recorded run and was effectively disarmed.
        run_cap = resolve_spend_max_tokens_per_run(config)
        if run_cap is not None and usage.billable_tokens >= run_cap:
            return (
                f"per-run subscription token ceiling reached "
                f"({usage.billable_tokens:,}/{run_cap:,} tokens)"
            )
        day_cap = resolve_spend_max_tokens_per_day(config)
        if day_cap is not None:
            target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
            prior = spend_today(target, config=config, now=now)["subscription_tokens"]
            day_total = prior + usage.billable_tokens
            if day_total >= day_cap:
                return (
                    f"per-day subscription token ceiling reached "
                    f"({int(day_total):,}/{day_cap:,} tokens today)"
                )
        # Weekly-token-limit + max-percent-per-day (issue athenaeum#785): a SECOND,
        # independent way to bound the subscription per-day figure, derived
        # rather than absolute. Both knobs are strictly opt-in — either unset
        # means this ceiling does nothing, so setting only one of the two
        # leaves behavior unchanged (there is no denominator/no percentage to
        # apply). Deliberately token-denominated and subscription-only: it
        # never reaches the API branch below, so it cannot affect a metered
        # run — subscription notional and API real dollars are two metrics the
        # ledger never blends (athenaeum#487, cwc#1629). Day boundary matches every
        # other per-day ceiling: the configured ACCOUNTING day (issue
        # athenaeum#1136) via ``_start_of_accounting_day`` (``spend_today``),
        # not a rolling 7-day window (deferred; see the athenaeum#785 design
        # notes).
        weekly_limit = resolve_spend_weekly_token_limit(config)
        max_pct = resolve_spend_max_pct_per_day(config)
        if weekly_limit is not None and max_pct is not None:
            effective_day_cap = weekly_limit / 7 * (max_pct / 100)
            target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
            prior = spend_today(target, config=config, now=now)["subscription_tokens"]
            day_total = prior + usage.billable_tokens
            if day_total >= effective_day_cap:
                return (
                    f"per-day subscription percent-of-weekly ceiling reached "
                    f"({int(day_total):,}/{effective_day_cap:,.0f} tokens today, "
                    f"{max_pct:g}% of weekly limit {weekly_limit:,} tokens)"
                )
        return None

    # Metered API path — dollars.
    #
    # Issue athenaeum#926: check headroom BEFORE the trip checks below, and log
    # unconditionally on the outcome — never gated on whether a trip follows —
    # so a run that ends past the ceiling still gets the warning, not only the
    # trip. Best-effort: a warning must never break the run it measures, same
    # contract as the rest of this module's ledger I/O.
    try:
        _warning = spend_headroom_warning(
            usage, config=config, ledger_path=ledger_path, cache_dir=cache_dir, now=now
        )
        if _warning is not None:
            log.warning("%s", _warning)
    except Exception as exc:  # noqa: BLE001 — a warning must never break the run
        log.debug("spend headroom warning check failed (%s): %s", type(exc).__name__, exc)

    run_cap_usd = resolve_spend_max_usd_per_run(config)
    if run_cap_usd is not None and usage.estimated_cost_usd >= run_cap_usd:
        return (
            f"per-run API dollar ceiling reached "
            f"(${usage.estimated_cost_usd:.2f}/${run_cap_usd:.2f})"
        )
    day_cap_usd = resolve_spend_max_usd_per_day(config)
    if day_cap_usd is not None:
        target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
        prior = spend_today(target, config=config, now=now)["api_usd"]
        day_total = prior + usage.estimated_cost_usd
        if day_total >= day_cap_usd:
            return (
                f"per-day API dollar ceiling reached "
                f"(${day_total:.2f}/${day_cap_usd:.2f} today)"
            )
    return None
