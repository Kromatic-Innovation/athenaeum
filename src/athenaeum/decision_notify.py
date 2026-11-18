# SPDX-License-Identifier: Apache-2.0
"""Push for the decision queue: a user-configured command notifier (athenaeum#2533).

Design athenaeum#2489 section 5. The poll surface is
:func:`athenaeum.decision_digest.queue_digest`; this module is the optional
push half. Core ships exactly one notifier, ``command``: it runs a
user-configured argv with the digest JSON on stdin. Services (a phone push,
chat, email) are the user's own scripts, never core dependencies or
credentials.

Rules this module holds to:

* **Per-user config only.** Settings come from ``notify.yaml`` in the user's
  config directory (``$XDG_CONFIG_HOME/athenaeum``, else ``~/.config/athenaeum``),
  never from the corpus's ``athenaeum.yaml``: a corpus can be cloned or shared,
  and a corpus-supplied command would be arbitrary code execution for every
  clone. A ``decisions.notify`` block found in the corpus config is refused
  with a warning and is never read for a command.
* **Off by default.** ``decisions.attention`` defaults to ``poll``; only
  ``push`` or ``both`` enables this module.
* **No shell.** The argv is passed to :func:`subprocess.run` as a list with
  ``shell=False``; nothing is interpolated into it.
* **Digest only.** The payload is the digest dict and nothing else, never page
  bodies. ``payload: counts`` additionally strips each offered item's summary.
* **Dedupe and throttle.** Sent state lives in ``decision-notify-state.json``
  in the cache directory, keyed per wiki root. ``digest_change`` fires only
  when ``digest_id`` differs from the last one sent; ``daily`` fires once per
  local day at or after ``daily_at``. ``quiet_hours`` suppresses a send
  (it is deferred, not dropped) and ``min_interval_minutes`` spaces attempts.
* **Failures never loop.** A failing command is logged, counted as an attempt,
  and not retried until the longer of ``min_interval_minutes`` and
  :data:`FAILURE_BACKOFF_MINUTES` has passed. Nothing here raises into the tick.

Layering: L4 domain module. Imports :mod:`athenaeum.decision_digest` (L4),
:mod:`athenaeum.atomic_io` and :mod:`athenaeum.config`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_cache_dir
from athenaeum.decision_digest import queue_digest

log = logging.getLogger(__name__)

NOTIFY_CONFIG_FILENAME = "notify.yaml"
STATE_FILENAME = "decision-notify-state.json"

ATTENTION_MODES = ("poll", "push", "both")
TRIGGERS = ("digest_change", "daily", "both")
PAYLOADS = ("full", "counts")

DEFAULT_MIN_INTERVAL_MINUTES = 120
DEFAULT_DAILY_AT = "09:00"
#: Floor on the wait after a failed send, whatever ``min_interval_minutes`` says.
FAILURE_BACKOFF_MINUTES = 15
#: Wall-clock cap on one notifier command.
COMMAND_TIMEOUT_SECONDS = 30


@dataclass
class NotifySettings:
    """Resolved per-user notifier settings."""

    attention: str = "poll"
    trigger: str = "digest_change"
    daily_at: time = field(default_factory=lambda: time(9, 0))
    quiet_hours: tuple[time, time] | None = None
    min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES
    payload: str = "full"
    commands: list[list[str]] = field(default_factory=list)

    @property
    def push_enabled(self) -> bool:
        return self.attention in ("push", "both") and bool(self.commands)


@dataclass
class NotifyResult:
    """What one :func:`maybe_notify` call did."""

    status: str  # disabled | nothing-to-send | deduped | quiet-hours | throttled | sent | failed
    detail: str = ""


def user_config_path() -> Path:
    """Path of the per-user ``notify.yaml`` (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path("~/.config").expanduser()
    return base / "athenaeum" / NOTIFY_CONFIG_FILENAME


def _parse_hhmm(raw: Any) -> time | None:
    if not isinstance(raw, str):
        return None
    try:
        hh, mm = raw.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _parse_quiet_hours(raw: Any) -> tuple[time, time] | None:
    if not isinstance(raw, str) or "-" not in raw:
        return None
    start, _, end = raw.partition("-")
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    if s is None or e is None or s == e:
        return None
    return s, e


def _choice(value: Any, allowed: tuple[str, ...], default: str, name: str) -> str:
    if value is None:
        return default
    if isinstance(value, str) and value in allowed:
        return value
    log.warning(
        "decision notify: ignoring invalid %s=%r (expected one of %s)", name, value, allowed
    )
    return default


def load_settings(path: Path | None = None) -> NotifySettings:
    """Read the per-user notifier settings; any problem degrades to push off."""
    cfg_path = path or user_config_path()
    settings = NotifySettings()
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else None
    except (OSError, yaml.YAMLError) as exc:
        log.warning("decision notify: cannot read %s: %s; push stays off", cfg_path, exc)
        return settings
    section = raw.get("decisions") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return settings
    settings.attention = _choice(section.get("attention"), ATTENTION_MODES, "poll", "attention")
    notify = section.get("notify")
    if not isinstance(notify, dict):
        return settings
    settings.trigger = _choice(notify.get("trigger"), TRIGGERS, "digest_change", "trigger")
    settings.payload = _choice(notify.get("payload"), PAYLOADS, "full", "payload")
    if notify.get("daily_at") is not None:
        parsed = _parse_hhmm(notify["daily_at"])
        if parsed is None:
            log.warning("decision notify: ignoring invalid daily_at=%r", notify["daily_at"])
        else:
            settings.daily_at = parsed
    if notify.get("quiet_hours") is not None:
        quiet = _parse_quiet_hours(notify["quiet_hours"])
        if quiet is None:
            log.warning("decision notify: ignoring invalid quiet_hours=%r", notify["quiet_hours"])
        settings.quiet_hours = quiet
    interval = notify.get("min_interval_minutes")
    if isinstance(interval, int) and not isinstance(interval, bool) and interval >= 0:
        settings.min_interval_minutes = interval
    elif interval is not None:
        log.warning("decision notify: ignoring invalid min_interval_minutes=%r", interval)
    channels = notify.get("channels")
    for channel in channels if isinstance(channels, list) else []:
        argv = channel.get("argv") if isinstance(channel, dict) else None
        if (
            isinstance(channel, dict)
            and channel.get("kind") == "command"
            and isinstance(argv, list)
            and argv
            and all(isinstance(a, str) and a for a in argv)
        ):
            settings.commands.append(list(argv))
        else:
            log.warning(
                "decision notify: ignoring invalid channel %r (core ships only kind: command)",
                channel,
            )
    return settings


def refuse_corpus_notify_block(config: dict[str, Any] | None) -> bool:
    """Warn if the corpus config carries ``decisions.notify``; never acted on."""
    section = config.get("decisions") if isinstance(config, dict) else None
    if isinstance(section, dict) and "notify" in section:
        log.warning(
            "decision notify: refusing the decisions.notify block in the corpus "
            "athenaeum.yaml; notifier settings are read only from the per-user %s "
            "(a corpus-supplied command would run on every clone). Ignored.",
            NOTIFY_CONFIG_FILENAME,
        )
        return True
    return False


def in_quiet_hours(quiet: tuple[time, time] | None, local_now: datetime) -> bool:
    """True when ``local_now`` is inside the (possibly midnight-wrapping) window."""
    if quiet is None:
        return False
    start, end = quiet
    t = local_now.time().replace(second=0, microsecond=0)
    if start < end:
        return start <= t < end
    return t >= start or t < end


def build_payload(digest: dict[str, Any], mode: str) -> dict[str, Any]:
    """The digest as sent; ``counts`` strips each offered item's summary."""
    payload = copy.deepcopy(digest)
    if mode == "counts":
        for item in payload.get("actionable_now") or []:
            if isinstance(item, dict):
                item.pop("summary", None)
    return payload


def _state_path() -> Path:
    return resolve_cache_dir() / STATE_FILENAME


def _load_state(wiki_root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = data.get(str(wiki_root)) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def _save_state(wiki_root: Path, entry: dict[str, Any]) -> None:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(wiki_root)] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))
    except OSError as exc:
        log.warning("decision notify: cannot persist state to %s: %s", path, exc)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_command(argv: list[str], payload: dict[str, Any]) -> bool:
    """Run one notifier argv with the payload JSON on stdin; never via a shell."""
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            shell=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("decision notify: command %r failed to run: %s", argv[0], exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "decision notify: command %r exited %d: %s",
            argv[0],
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return False
    return True


def maybe_notify(
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    *,
    settings: NotifySettings | None = None,
    now: datetime | None = None,
) -> NotifyResult:
    """Push the digest if the user's settings and the dedupe/throttle rules allow.

    ``settings`` defaults to :func:`load_settings`; ``now`` is injectable.
    Never raises.
    """
    try:
        refuse_corpus_notify_block(config)
        cfg = settings if settings is not None else load_settings()
        if not cfg.push_enabled:
            return NotifyResult("disabled")
        when = now or datetime.now(timezone.utc)
        local = when.astimezone()
        if in_quiet_hours(cfg.quiet_hours, local):
            return NotifyResult("quiet-hours")

        state = _load_state(wiki_root)
        last_attempt = _parse_ts(state.get("last_attempt_at"))
        wait = cfg.min_interval_minutes
        if state.get("last_status") == "failed":
            wait = max(wait, FAILURE_BACKOFF_MINUTES)
        if last_attempt is not None and when - last_attempt < timedelta(minutes=wait):
            return NotifyResult("throttled")

        digest = queue_digest(wiki_root, config=config, now=when)
        if not digest.get("actionable_now"):
            return NotifyResult("nothing-to-send")

        changed = digest["digest_id"] != state.get("last_sent_digest_id")
        due_daily = (
            local.time() >= cfg.daily_at
            and state.get("last_daily_date") != local.date().isoformat()
        )
        fire_change = cfg.trigger in ("digest_change", "both") and changed
        fire_daily = cfg.trigger in ("daily", "both") and due_daily
        if not (fire_change or fire_daily):
            return NotifyResult("deduped")

        payload = build_payload(digest, cfg.payload)
        sent = [run_command(argv, payload) for argv in cfg.commands]
        entry = dict(state)
        entry["last_attempt_at"] = when.isoformat()
        if any(sent):
            entry["last_status"] = "sent"
            entry["last_sent_digest_id"] = digest["digest_id"]
            entry["last_sent_at"] = when.isoformat()
            if fire_daily:
                entry["last_daily_date"] = local.date().isoformat()
            _save_state(wiki_root, entry)
            return NotifyResult("sent")
        entry["last_status"] = "failed"
        _save_state(wiki_root, entry)
        return NotifyResult("failed")
    except Exception as exc:  # noqa: BLE001 - a notifier must never break the tick
        log.warning("decision notify: skipped after unexpected error: %s", exc)
        return NotifyResult("failed", str(exc))


__all__ = [
    "NotifyResult",
    "NotifySettings",
    "build_payload",
    "load_settings",
    "maybe_notify",
    "user_config_path",
]
