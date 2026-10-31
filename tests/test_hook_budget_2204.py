# SPDX-License-Identifier: Apache-2.0
"""Tests for the shipped-hook foreground budget and degradation status
(issue athenaeum#2204).

Three groups:

- ``TestResolveHookBudgetSeconds`` -- the resolver itself (env override,
  malformed-value fallback, default), plus a literal-parity pin against
  ``session-start-recall.sh``'s own bash fallback so the two can never
  silently drift (AC1: "one foreground budget").
- ``TestSnippetTimeoutCoversBudget`` -- AC6: a test fails if any shipped
  hook's snippet timeout is lower than the matching hook's foreground
  budget. Checked against the REAL ``settings-snippet.json`` for the two
  hooks that read the shared budget, plus a negative control proving the
  comparison itself can fail (a guard nobody has seen fail is
  indistinguishable from one that cannot).
- ``TestHookStatus`` -- :mod:`athenaeum.hook_status`'s own contract:
  record/read-latest, the AC9 once-per-session notice gate, and the
  unrecognised-event fallback text.

Shell-level coverage (AC2 step-timeout, AC3 background-build concurrency,
AC4 non-blocking reads, AC7 degradation recording from the real hooks, AC9
the notice actually reaching a hook's stdout) lives in
``tests/test_shell_hooks.py``, which already owns the subprocess harness
for ``examples/claude-code/*.sh``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from athenaeum.config import HOOK_BUDGET_SECONDS_DEFAULT, resolve_hook_budget_seconds

REPO_ROOT = Path(__file__).parent.parent
SESSION_START_SH = REPO_ROOT / "examples" / "claude-code" / "session-start-recall.sh"
SETTINGS_SNIPPET = REPO_ROOT / "examples" / "claude-code" / "settings-snippet.json"


class TestResolveHookBudgetSeconds:
    def test_default_is_ten(self) -> None:
        assert resolve_hook_budget_seconds(None) == 10
        assert HOOK_BUDGET_SECONDS_DEFAULT == 10

    def test_env_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("ATHENAEUM_HOOK_BUDGET_SECONDS", "30")
        assert resolve_hook_budget_seconds(None) == 30

    def test_malformed_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ATHENAEUM_HOOK_BUDGET_SECONDS", "not-a-number")
        assert resolve_hook_budget_seconds(None) == HOOK_BUDGET_SECONDS_DEFAULT

    def test_non_positive_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ATHENAEUM_HOOK_BUDGET_SECONDS", "0")
        assert resolve_hook_budget_seconds(None) == HOOK_BUDGET_SECONDS_DEFAULT
        monkeypatch.setenv("ATHENAEUM_HOOK_BUDGET_SECONDS", "-5")
        assert resolve_hook_budget_seconds(None) == HOOK_BUDGET_SECONDS_DEFAULT

    def test_shell_fallback_literal_matches_python_default(self) -> None:
        """``session-start-recall.sh`` mirrors this resolver's exact
        fallback literal in bash (``HOOK_BUDGET_SECONDS="${ATHENAEUM_HOOK_
        BUDGET_SECONDS:-10}"``) so the shell hook never needs a python call
        just to learn its own budget (see that resolver's own docstring).
        Pins the two literals equal so a change to one side alone is
        caught here rather than drifting silently.
        """
        text = SESSION_START_SH.read_text(encoding="utf-8")
        match = re.search(
            r'HOOK_BUDGET_SECONDS="\$\{ATHENAEUM_HOOK_BUDGET_SECONDS:-(\d+)\}"',
            text,
        )
        assert match is not None, (
            "session-start-recall.sh no longer resolves HOOK_BUDGET_SECONDS "
            "the expected way -- update this regex if the shape changed "
            "deliberately"
        )
        assert int(match.group(1)) == HOOK_BUDGET_SECONDS_DEFAULT


# ---------------------------------------------------------------------------
# AC6 — a test fails if any snippet timeout is lower than the matching
# hook's foreground budget.
# ---------------------------------------------------------------------------

#: Which `settings-snippet.json` hook command substrings share the one
#: foreground budget (issue athenaeum#2204's own ruling: ONE shared
#: default, not a per-hook table). `wiki-context-inject.sh` /
#: `pending-questions-surface.sh` / `pre-compact-save.sh` do fixed, cheap,
#: unrelated work (a cwd grep, a snoozed-question surface, a reminder
#: string) and were deliberately left out of this issue's redesign -- see
#: the PR body's "Reversible defaults" section.
_BUDGET_SHARING_COMMAND_SUBSTRINGS: tuple[str, ...] = (
    "session-start-recall.sh",
    "user-prompt-recall.sh",
)


def _iter_hook_commands(snippet: dict) -> list[tuple[str, int | None]]:
    """``(command string, timeout or None)`` for every hook entry in a
    parsed `settings-snippet.json`-shaped dict, across every event."""
    out: list[tuple[str, int | None]] = []
    for entries in snippet.get("hooks", {}).values():
        if not isinstance(entries, list):
            continue
        for group in entries:
            for hook in group.get("hooks", []):
                out.append((hook.get("command", ""), hook.get("timeout")))
    return out


def budget_violations(snippet: dict, budget_seconds: int) -> list[str]:
    """Hook commands (matching :data:`_BUDGET_SHARING_COMMAND_SUBSTRINGS`)
    whose snippet timeout is LOWER than *budget_seconds*, or missing
    entirely (no `timeout` key is a Claude Code host default this check
    cannot see into, so it counts as a violation here rather than a silent
    pass). One string per violation, naming the command and the numbers.
    """
    violations: list[str] = []
    for command, timeout in _iter_hook_commands(snippet):
        if not any(sub in command for sub in _BUDGET_SHARING_COMMAND_SUBSTRINGS):
            continue
        if timeout is None or timeout < budget_seconds:
            violations.append(
                f"{command!r}: snippet timeout {timeout!r} < foreground budget "
                f"{budget_seconds}"
            )
    return violations


class TestSnippetTimeoutCoversBudget:
    def test_real_snippet_covers_the_real_budget(self) -> None:
        snippet = json.loads(SETTINGS_SNIPPET.read_text(encoding="utf-8"))
        budget = resolve_hook_budget_seconds(None)
        assert budget_violations(snippet, budget) == []

    def test_guard_detects_a_lower_timeout(self) -> None:
        """Negative control: the comparison itself must be able to fail --
        a guard nobody has seen fail is indistinguishable from one that
        cannot (mirrors `tests/test_hook_examples_guard.py`'s own
        `test_catches_reintroduced_*` convention)."""
        bad_snippet = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/path/to/session-start-recall.sh",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        violations = budget_violations(bad_snippet, 10)
        assert len(violations) == 1
        assert "session-start-recall.sh" in violations[0]

    def test_guard_is_clean_on_a_generous_timeout(self) -> None:
        good_snippet = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/path/to/user-prompt-recall.sh",
                                "timeout": 60,
                            }
                        ]
                    }
                ]
            }
        }
        assert budget_violations(good_snippet, 10) == []

    def test_guard_ignores_unrelated_hooks(self) -> None:
        """A tight timeout on a hook that does NOT share the budget (e.g.
        `wiki-context-inject.sh`) is not this test's concern."""
        snippet = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/path/to/wiki-context-inject.sh",
                                "timeout": 1,
                            }
                        ]
                    }
                ]
            }
        }
        assert budget_violations(snippet, 10) == []


class TestHookStatus:
    def test_read_latest_degradation_is_none_when_nothing_recorded(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.hook_status import read_latest_degradation

        assert read_latest_degradation(tmp_path) is None

    def test_record_and_read_round_trip(self, tmp_path: Path) -> None:
        from athenaeum.hook_status import read_latest_degradation, record_degradation

        record_degradation(tmp_path, "session-start", "no_api_key", "no key cached")
        latest = read_latest_degradation(tmp_path)
        assert latest is not None
        assert latest["event"] == "no_api_key"
        assert latest["hook"] == "session-start"
        assert latest["detail"] == "no key cached"
        assert "ts" in latest

    def test_latest_is_the_most_recent_of_several(self, tmp_path: Path) -> None:
        from athenaeum.hook_status import read_latest_degradation, record_degradation

        record_degradation(tmp_path, "session-start", "no_api_key", "first")
        record_degradation(tmp_path, "session-start", "index_not_ready", "second")
        record_degradation(tmp_path, "user-prompt", "step_timeout", "third")
        latest = read_latest_degradation(tmp_path)
        assert latest is not None
        assert latest["event"] == "step_timeout"
        assert latest["detail"] == "third"

    def test_record_never_raises_on_unwritable_cache_dir(self, tmp_path: Path) -> None:
        from athenaeum.hook_status import record_degradation

        cache_dir = tmp_path / "does" / "not" / "exist"
        # No mkdir -- record_degradation must tolerate a missing parent
        # (read side: no file yet) without raising, even though it cannot
        # actually persist anything without a writable parent directory it
        # is allowed to create. The call itself must never raise.
        record_degradation(cache_dir, "session-start", "no_api_key", "x")

    def test_notice_text_has_a_fallback_for_an_unknown_event(self) -> None:
        from athenaeum.hook_status import notice_text

        assert notice_text({"event": "no_api_key"})
        assert notice_text({"event": "some_future_event_kind"})
        assert notice_text({})

    def test_maybe_session_notice_fires_once_per_session(self, tmp_path: Path) -> None:
        from athenaeum.hook_status import maybe_session_notice, record_degradation

        record_degradation(tmp_path, "session-start", "no_api_key", "x")

        first = maybe_session_notice(tmp_path, "session-a")
        assert first is not None

        second = maybe_session_notice(tmp_path, "session-a")
        assert second is None

    def test_maybe_session_notice_fires_again_for_a_different_session(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.hook_status import maybe_session_notice, record_degradation

        record_degradation(tmp_path, "session-start", "no_api_key", "x")

        assert maybe_session_notice(tmp_path, "session-a") is not None
        assert maybe_session_notice(tmp_path, "session-b") is not None
        # ...but only once each.
        assert maybe_session_notice(tmp_path, "session-a") is None
        assert maybe_session_notice(tmp_path, "session-b") is None

    def test_maybe_session_notice_is_none_when_nothing_degraded(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.hook_status import maybe_session_notice

        assert maybe_session_notice(tmp_path, "session-a") is None

    def test_maybe_session_notice_sanitizes_an_unsafe_session_id(
        self, tmp_path: Path
    ) -> None:
        """A session id is untrusted hook stdin JSON -- must never be used
        as a bare path component (e.g. `../../etc`)."""
        from athenaeum.hook_status import maybe_session_notice, record_degradation

        record_degradation(tmp_path, "session-start", "no_api_key", "x")
        notice = maybe_session_notice(tmp_path, "../../etc/passwd")
        assert notice is not None
        marker_dir = tmp_path / "hook_notice_shown"
        for marker in marker_dir.iterdir():
            assert marker.is_file()
            assert ".." not in marker.name
            assert "/" not in marker.name
