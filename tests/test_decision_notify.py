# SPDX-License-Identifier: Apache-2.0
"""Command notifier for the decision queue (athenaeum#2533, design athenaeum#2489 section 5)."""

from __future__ import annotations

import json
import logging
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import decision_digest, decision_notify
from athenaeum.decision_notify import (
    NotifySettings,
    load_settings,
    maybe_notify,
    user_config_path,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _item(n: int) -> dict[str, Any]:
    return {
        "type": "question",
        "id": f"question-{n}",
        "created_at": "2026-11-10",
        "summary": f"Is thing {n} true?",
        "confidence": None,
        "payload": {"n": n, "body": "PAGE BODY SECRET"},
    }


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TZ", "UTC")
    _time.tzset()
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    yield tmp_path
    monkeypatch.undo()
    _time.tzset()


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir()
    return w


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    items = [_item(1), _item(2)]
    monkeypatch.setattr(decision_digest, "collect_unframed_decisions", lambda *a, **k: items)
    return items


@pytest.fixture
def script(tmp_path: Path) -> tuple[list[str], Path]:
    """A notifier fixture script: appends its stdin as one line to a log file."""
    out = tmp_path / "received.jsonl"
    path = tmp_path / "notify.py"
    path.write_text(
        "import sys, json\n"
        f"open({str(out)!r}, 'a').write(json.dumps(json.load(sys.stdin)) + '\\n')\n"
    )
    return [sys.executable, str(path)], out


def _received(out: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in out.read_text().splitlines()] if out.exists() else []


def _settings(argv: list[str], **kw: Any) -> NotifySettings:
    base: dict[str, Any] = {"attention": "push", "commands": [argv], "min_interval_minutes": 0}
    base.update(kw)
    return NotifySettings(**base)


def _write_user_config(tmp_path: Path, body: dict[str, Any]) -> Path:
    p = tmp_path / "xdg" / "athenaeum" / "notify.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(body))
    return p


# AC1: per-user config only -------------------------------------------------


def test_config_path_honours_xdg(tmp_path: Path) -> None:
    assert user_config_path() == tmp_path / "xdg" / "athenaeum" / "notify.yaml"


def test_settings_read_from_user_config(tmp_path: Path) -> None:
    _write_user_config(
        tmp_path,
        {
            "decisions": {
                "attention": "both",
                "notify": {
                    "trigger": "daily",
                    "daily_at": "07:30",
                    "quiet_hours": "22:00-08:00",
                    "min_interval_minutes": 30,
                    "payload": "counts",
                    "channels": [{"kind": "command", "argv": ["/bin/true"]}],
                },
            }
        },
    )
    s = load_settings()
    assert (s.attention, s.trigger, s.payload, s.min_interval_minutes) == (
        "both",
        "daily",
        "counts",
        30,
    )
    assert s.commands == [["/bin/true"]] and s.quiet_hours is not None and s.push_enabled


def test_corpus_notify_block_refused_and_never_executed(
    wiki: Path, queue: list, script: tuple, caplog: pytest.LogCaptureFixture
) -> None:
    argv, out = script
    corpus_cfg = {
        "decisions": {
            "attention": "push",
            "notify": {"channels": [{"kind": "command", "argv": argv}]},
        }
    }
    with caplog.at_level(logging.WARNING):
        res = maybe_notify(wiki, corpus_cfg, now=NOW)  # no per-user config exists
    assert res.status == "disabled"
    assert not out.exists()
    assert "refusing the decisions.notify block" in caplog.text


# AC2: command runs argv with digest JSON on stdin -------------------------


def test_command_receives_digest_json_on_stdin(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    assert maybe_notify(wiki, None, settings=_settings(argv), now=NOW).status == "sent"
    (got,) = _received(out)
    expected = decision_digest.queue_digest(wiki, now=NOW)
    assert got["digest_id"] == expected["digest_id"]
    assert [i["id"] for i in got["actionable_now"]] == ["question-1", "question-2"]


def test_argv_never_goes_through_a_shell(
    wiki: Path, queue: list, tmp_path: Path, script: tuple
) -> None:
    argv, out = script
    marker = tmp_path / "pwned"
    res = maybe_notify(wiki, None, settings=_settings(argv + [f"; touch {marker}"]), now=NOW)
    assert res.status == "sent" and not marker.exists()


# AC3: dedupe, trigger, quiet hours, min interval ---------------------------


def test_dedupe_on_digest_id(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    s = _settings(argv)
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "sent"
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(hours=1)).status == "deduped"
    queue.append(_item(3))
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(hours=2)).status == "sent"
    assert len(_received(out)) == 2


def test_empty_queue_sends_nothing(
    wiki: Path, monkeypatch: pytest.MonkeyPatch, script: tuple
) -> None:
    monkeypatch.setattr(decision_digest, "collect_unframed_decisions", lambda *a, **k: [])
    assert (
        maybe_notify(wiki, None, settings=_settings(script[0]), now=NOW).status == "nothing-to-send"
    )


def test_trigger_daily_fires_once_per_day_after_daily_at(
    wiki: Path, queue: list, script: tuple
) -> None:
    argv, out = script
    s = _settings(argv, trigger="daily")  # daily_at 09:00 default
    early = NOW.replace(hour=8)
    assert maybe_notify(wiki, None, settings=s, now=early).status == "deduped"
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "sent"
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(hours=3)).status == "deduped"
    # same digest, next day: a daily reminder fires again
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(days=1)).status == "sent"
    assert len(_received(out)) == 2


def test_trigger_digest_change_ignores_the_clock(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    s = _settings(argv, trigger="digest_change")
    maybe_notify(wiki, None, settings=s, now=NOW)
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(days=2)).status == "deduped"


def test_trigger_both_fires_on_either(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    s = _settings(argv, trigger="both")
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "sent"
    queue.append(_item(3))  # change fires immediately
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=5)).status == "sent"
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=10)).status == "deduped"
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(days=1)).status == "sent"


def test_quiet_hours_defer_then_send(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    quiet = decision_notify._parse_quiet_hours("22:00-08:00")
    s = _settings(argv, quiet_hours=quiet)
    night = NOW.replace(hour=23)
    assert maybe_notify(wiki, None, settings=s, now=night).status == "quiet-hours"
    assert (
        maybe_notify(wiki, None, settings=s, now=NOW.replace(hour=3, day=19)).status
        == "quiet-hours"
    )
    assert not out.exists()
    assert maybe_notify(wiki, None, settings=s, now=NOW.replace(hour=8, day=19)).status == "sent"


def test_quiet_hours_same_day_window() -> None:
    q = decision_notify._parse_quiet_hours("12:00-14:00")
    assert decision_notify.in_quiet_hours(q, NOW)
    assert not decision_notify.in_quiet_hours(q, NOW.replace(hour=14))


def test_min_interval_throttles(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    s = _settings(argv, min_interval_minutes=120)
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "sent"
    queue.append(_item(3))
    assert (
        maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=119)).status == "throttled"
    )
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=121)).status == "sent"


# AC4: off by default -------------------------------------------------------


def test_push_off_by_default(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    assert load_settings().attention == "poll"
    assert maybe_notify(wiki, None, now=NOW).status == "disabled"
    s = _settings(argv, attention="poll")
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "disabled"
    assert not out.exists()


def test_push_needs_a_channel(tmp_path: Path) -> None:
    _write_user_config(tmp_path, {"decisions": {"attention": "push"}})
    assert not load_settings().push_enabled


# AC5: failure logged, no tight loop ---------------------------------------


def test_failure_logged_and_backed_off(
    wiki: Path, queue: list, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls = tmp_path / "calls"
    bad = tmp_path / "bad.py"
    bad.write_text(
        f"import sys\nopen({str(calls)!r}, 'a').write('x')\nsys.stderr.write('boom')\nsys.exit(3)\n"
    )
    s = _settings([sys.executable, str(bad)])  # min_interval 0: backoff floor still applies
    with caplog.at_level(logging.WARNING):
        assert maybe_notify(wiki, None, settings=s, now=NOW).status == "failed"
    assert "exited 3" in caplog.text and "boom" in caplog.text
    assert (
        maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=1)).status == "throttled"
    )
    assert calls.read_text() == "x"
    assert maybe_notify(wiki, None, settings=s, now=NOW + timedelta(minutes=16)).status == "failed"
    assert calls.read_text() == "xx"


def test_missing_command_is_a_logged_failure_not_a_raise(
    wiki: Path, queue: list, tmp_path: Path
) -> None:
    s = _settings([str(tmp_path / "does-not-exist")])
    assert maybe_notify(wiki, None, settings=s, now=NOW).status == "failed"


# AC6: payload is digest only; counts strips summaries ---------------------


def test_payload_is_digest_only_never_bodies(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    maybe_notify(wiki, None, settings=_settings(argv), now=NOW)
    raw = out.read_text()
    assert "PAGE BODY SECRET" not in raw
    (got,) = _received(out)
    assert got["actionable_now"][0]["summary"] == "Is thing 1 true?"
    assert set(got) == set(decision_digest.queue_digest(wiki, now=NOW))


def test_payload_counts_strips_summaries(wiki: Path, queue: list, script: tuple) -> None:
    argv, out = script
    maybe_notify(wiki, None, settings=_settings(argv, payload="counts"), now=NOW)
    (got,) = _received(out)
    assert got["actionable_now"] and all("summary" not in i for i in got["actionable_now"])
    assert got["pending_total"] == 2 and got["by_type"] == {"question": 2}
    assert "Is thing" not in out.read_text()


def test_state_persisted_in_cache_dir(
    wiki: Path, queue: list, script: tuple, tmp_path: Path
) -> None:
    maybe_notify(wiki, None, settings=_settings(script[0]), now=NOW)
    state = json.loads((tmp_path / "cache" / "decision-notify-state.json").read_text())
    assert state[str(wiki)]["last_sent_digest_id"].startswith("sha256:")


# Wiring: the librarian finalize phase fires the notifier -------------------


def _finalize_ctx(tmp_path: Path, **overrides: Any) -> Any:
    from athenaeum.librarian import RunContext

    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "wiki").mkdir(parents=True)
    defaults: dict[str, Any] = dict(
        raw_root=knowledge_root / "raw",
        wiki_root=knowledge_root / "wiki",
        knowledge_root=knowledge_root,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=None,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=False,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=None,
        heartbeat=None,
        out_run_stats=None,
        config={},
    )
    defaults.update(overrides)
    return RunContext(**defaults)


@pytest.mark.parametrize("dry_run,expected", [(False, 1), (True, 0)])
def test_finalize_phase_fires_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, expected: int
) -> None:
    from athenaeum.librarian import _run_finalize_phase

    calls: list[Path] = []
    monkeypatch.setattr(
        decision_notify, "maybe_notify", lambda root, cfg=None, **k: calls.append(root)
    )
    _run_finalize_phase(_finalize_ctx(tmp_path, dry_run=dry_run))
    assert len(calls) == expected
