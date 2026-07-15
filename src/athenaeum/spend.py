# SPDX-License-Identifier: Apache-2.0
"""Durable LLM-spend ledger (issue #378).

Athenaeum runs on two cost models that must never be blended:

* the ``claude-cli`` **subscription** path — no invoice; consumes the
  operator's Claude Code subscription quota. Constrained in TOKENS.
* the metered ``anthropic`` **API** path (contradiction resolver on the api
  backend, batch mode, and the per-turn ``query-topics`` recall extractor,
  which always talks to the SDK directly). Constrained in real DOLLARS.

The in-memory :class:`~athenaeum.models.TokenUsage` accumulator is logged at
end-of-run and then DISCARDED — nothing persists spend across runs, so
"how much has athenaeum spent, and is any of it real money?" is unanswerable
from data (a code audit once mis-answered exactly this — see issue #378).

This module appends **one JSONL record per pipeline run** to
``~/.cache/athenaeum/spend.jsonl``. Each record carries:

* ``provider`` — ``claude-cli`` vs ``anthropic``. This field is the whole
  point: it makes "are we spending real money?" an empirical question rather
  than a grep over the code.
* ``run_type`` — ``librarian`` / ``answers`` / ``query-topics`` / ...
* ``models`` — the serving model-id(s).
* the FOUR token counters kept **separate** (cache-read is ~10x cheaper than
  input; collapsing them destroys the cost signal).
* ``estimated_cost_usd`` — provider-tagged: always ``0.0`` on the
  subscription path so subscription rows can never be summed into the dollar
  total.

The ledger is append-only and crash-safe: each record is a single
``O_APPEND`` write of one small line, and the reader tolerates a torn
trailing line. It records ONLY counts, model ids, run type, provider,
session id and timestamp — never prompt/response content, environment
values, or credentials.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid an import cycle at runtime (models imports nothing here)
    from athenaeum.models import TokenUsage

log = logging.getLogger(__name__)

#: Schema version stamped on every record so a future reader can migrate.
LEDGER_VERSION = 1

#: Ledger filename under the cache dir.
LEDGER_FILENAME = "spend.jsonl"

#: The two transports, in the terms the ledger uses. ``resolve_provider``
#: returns ``api`` / ``claude-cli``; the ledger records the SDK path as
#: ``anthropic`` (the metered, real-dollar transport) to read naturally in a
#: report ("API $0.42") and ``claude-cli`` unchanged (the subscription path).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CLAUDE_CLI = "claude-cli"


def ledger_provider(resolved_provider: str | None) -> str:
    """Map a :func:`resolve_provider` value to the ledger's provider term."""
    return PROVIDER_CLAUDE_CLI if resolved_provider == "claude-cli" else PROVIDER_ANTHROPIC


def default_cache_dir() -> Path:
    """Default athenaeum cache dir (``~/.cache/athenaeum``)."""
    return (Path("~/.cache/athenaeum").expanduser()).resolve()


def default_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the ledger path: ``<cache_dir>/spend.jsonl`` (cache dir default)."""
    base = cache_dir if cache_dir is not None else default_cache_dir()
    return Path(base) / LEDGER_FILENAME


def resolve_ledger_path(
    config: dict[str, Any] | None = None,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Resolve the active ledger path: explicit override else the default.

    Honours ``spend.ledger_path`` / ``ATHENAEUM_SPEND_LEDGER`` (a full file
    path); otherwise ``<cache_dir>/spend.jsonl``. Chiefly a test/relocation
    seam — the common case leaves it unset and writes under the cache dir.
    """
    from athenaeum.config import resolve_spend_ledger_path

    override = resolve_spend_ledger_path(config)
    if override is not None:
        return override
    return default_ledger_path(cache_dir)


# ---------------------------------------------------------------------------
# Record construction + append
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def build_record(
    usage: "TokenUsage",
    *,
    run_type: str,
    provider: str,
    session_id: str | None = None,
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Build one ledger record from a :class:`TokenUsage` accumulator.

    *provider* is a :func:`resolve_provider` value (``api`` / ``claude-cli``);
    it is mapped to the ledger term via :func:`ledger_provider`. The USD figure
    is provider-tagged — ``0.0`` on the subscription path regardless of the
    accumulator's ``subscription_covered`` flag — so subscription rows can
    never be summed into a dollar total downstream.
    """
    prov = ledger_provider(provider)
    usd = 0.0 if prov == PROVIDER_CLAUDE_CLI else round(usage.estimated_cost_usd, 6)
    stamp = (ts if ts is not None else _now_utc()).astimezone(timezone.utc)
    return {
        "v": LEDGER_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "run_type": run_type,
        "provider": prov,
        "subscription_covered": prov == PROVIDER_CLAUDE_CLI,
        "session_id": session_id,
        "models": sorted(usage.per_model.keys()),
        "api_calls": usage.api_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "batch_input_tokens": usage.batch_input_tokens,
        "batch_output_tokens": usage.batch_output_tokens,
        "total_tokens": usage.total_tokens,
        "estimated_cost_usd": usd,
    }


def _append_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    A single small ``O_APPEND`` write is atomic on local filesystems, so a
    crash can at worst leave a torn TRAILING line — which the reader skips —
    but never corrupts an already-written record. Creates the parent dir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def record_spend(
    usage: "TokenUsage",
    *,
    run_type: str,
    provider: str,
    session_id: str | None = None,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> bool:
    """Append one spend record for a finished pipeline run. Best-effort.

    No-ops (returns ``False``) when the ledger is disabled or *usage* recorded
    nothing (no calls and no tokens). Every failure is swallowed and logged at
    debug level: a ledger write must NEVER break or slow the run it measures.
    Returns ``True`` when a record was written.
    """
    try:
        from athenaeum.config import resolve_spend_ledger_enabled

        if not resolve_spend_ledger_enabled(config):
            return False
        # Nothing happened — don't clutter the ledger with empty runs.
        if usage.api_calls == 0 and usage.total_tokens == 0:
            return False
        record = build_record(
            usage, run_type=run_type, provider=provider, session_id=session_id
        )
        target = ledger_path if ledger_path is not None else resolve_ledger_path(
            config, cache_dir=cache_dir
        )
        _append_line(target, json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — ledger must never break a run
        log.debug("spend ledger write skipped (%s): %s", type(exc).__name__, exc)
        return False


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
        "api_calls": 0,
        "estimated_cost_usd": 0.0,
        "records": 0,
    }


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
    bucket["estimated_cost_usd"] += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    bucket["records"] += 1


def summarize(
    records: list[dict[str, Any]],
    *,
    by_model: bool = False,
    by_provider: bool = False,
) -> dict[str, Any]:
    """Summarise ledger records, keeping the two cost paths SEPARATE.

    Returns a dict with a ``subscription`` bucket (report its TOKENS) and an
    ``api`` bucket (report its DOLLARS) — never a blended total. ``by_model``
    adds per-model sub-buckets; ``by_provider`` adds a per-run-type breakdown
    within each path.
    """
    subscription = _blank_bucket()
    api = _blank_bucket()
    per_model: dict[str, dict[str, Any]] = {}
    per_run_type: dict[str, dict[str, Any]] = {}

    for record in records:
        prov = record.get("provider")
        bucket = subscription if prov == PROVIDER_CLAUDE_CLI else api
        _accumulate(bucket, record)
        if by_model:
            for model in record.get("models") or ["(untagged)"]:
                slot = per_model.setdefault(
                    model, {"subscription": _blank_bucket(), "api": _blank_bucket()}
                )
                _accumulate(
                    slot["subscription" if prov == PROVIDER_CLAUDE_CLI else "api"],
                    record,
                )
        if by_provider:
            rt = str(record.get("run_type", "(unknown)"))
            slot = per_run_type.setdefault(
                rt, {"subscription": _blank_bucket(), "api": _blank_bucket()}
            )
            _accumulate(
                slot["subscription" if prov == PROVIDER_CLAUDE_CLI else "api"],
                record,
            )

    # The subscription path carries no real dollars — surface tokens only.
    subscription["estimated_cost_usd"] = 0.0
    summary: dict[str, Any] = {
        "record_count": len(records),
        "subscription": subscription,
        "api": api,
    }
    if by_model:
        summary["by_model"] = per_model
    if by_provider:
        summary["by_run_type"] = per_run_type
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
) -> str:
    """Render a human report that never blends dollars into subscription rows."""
    sub = summary["subscription"]
    api = summary["api"]
    lines = [f"Athenaeum spend (since {since_label}):"]
    lines.append(
        f"  Subscription  {_fmt_tokens(sub['total_tokens'])} tokens"
        f"  ({sub['api_calls']} calls, {sub['records']} run(s))"
    )
    lines.append(
        f"  API           ${api['estimated_cost_usd']:.2f}"
        f"       ({api['api_calls']} calls, {api['records']} run(s))"
    )
    if by_provider and summary.get("by_run_type"):
        lines.append("  By run type:")
        for rt, slot in sorted(summary["by_run_type"].items()):
            s, a = slot["subscription"], slot["api"]
            lines.append(
                f"    {rt:<14} sub {_fmt_tokens(s['total_tokens'])} tok"
                f"  / api ${a['estimated_cost_usd']:.2f}"
            )
    if by_model and summary.get("by_model"):
        lines.append("  By model:")
        for model, slot in sorted(summary["by_model"].items()):
            s, a = slot["subscription"], slot["api"]
            lines.append(
                f"    {model:<28} sub {_fmt_tokens(s['total_tokens'])} tok"
                f"  / api ${a['estimated_cost_usd']:.2f}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spend ceiling (issue #378, part 4) — halt the pass on breach
# ---------------------------------------------------------------------------


def _start_of_utc_day(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spend_today(
    ledger_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, float]:
    """Subscription tokens and API dollars recorded SO FAR in the current UTC day.

    Reads the ledger (tolerating torn lines). Used by the per-day ceiling to
    account for spend already committed by earlier runs today.
    """
    now = now if now is not None else _now_utc()
    records = read_ledger(ledger_path, since=_start_of_utc_day(now))
    tokens = 0
    usd = 0.0
    for record in records:
        if record.get("provider") == PROVIDER_CLAUDE_CLI:
            tokens += int(record.get("total_tokens", 0) or 0)
        else:
            usd += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    return {"subscription_tokens": float(tokens), "api_usd": usd}


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
    Returns ``None`` when no ceiling is configured or none is breached — a
    ceiling is strictly opt-in.
    """
    from athenaeum.config import (
        resolve_spend_max_tokens_per_day,
        resolve_spend_max_tokens_per_run,
        resolve_spend_max_usd_per_day,
        resolve_spend_max_usd_per_run,
    )

    is_subscription = ledger_provider(provider) == PROVIDER_CLAUDE_CLI

    if is_subscription:
        run_cap = resolve_spend_max_tokens_per_run(config)
        if run_cap is not None and usage.total_tokens >= run_cap:
            return (
                f"per-run subscription token ceiling reached "
                f"({usage.total_tokens:,}/{run_cap:,} tokens)"
            )
        day_cap = resolve_spend_max_tokens_per_day(config)
        if day_cap is not None:
            target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
            prior = spend_today(target, now=now)["subscription_tokens"]
            day_total = prior + usage.total_tokens
            if day_total >= day_cap:
                return (
                    f"per-day subscription token ceiling reached "
                    f"({int(day_total):,}/{day_cap:,} tokens today)"
                )
        return None

    # Metered API path — dollars.
    run_cap_usd = resolve_spend_max_usd_per_run(config)
    if run_cap_usd is not None and usage.estimated_cost_usd >= run_cap_usd:
        return (
            f"per-run API dollar ceiling reached "
            f"(${usage.estimated_cost_usd:.2f}/${run_cap_usd:.2f})"
        )
    day_cap_usd = resolve_spend_max_usd_per_day(config)
    if day_cap_usd is not None:
        target = ledger_path or resolve_ledger_path(config, cache_dir=cache_dir)
        prior = spend_today(target, now=now)["api_usd"]
        day_total = prior + usage.estimated_cost_usd
        if day_total >= day_cap_usd:
            return (
                f"per-day API dollar ceiling reached "
                f"(${day_total:.2f}/${day_cap_usd:.2f} today)"
            )
    return None
