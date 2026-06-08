# SPDX-License-Identifier: Apache-2.0
"""Tests for the transient-API retry/backoff wrapper (issue #193).

Covers the standalone ``with_retry`` helper, its integration through
``tier2_classify`` (529-then-success retried to success), and the librarian
``run()`` loop's distinct give-up log path (transient-API vs malformed-file).
All Anthropic calls are mocked; no live API, no network. Backoff sleep is
injected/patched so tests never actually wait.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from anthropic._exceptions import OverloadedError

from athenaeum._retry import (
    TRANSIENT_ERRORS,
    TransientAPIError,
    with_retry,
)
from athenaeum.models import RawFile
from athenaeum.tiers import tier2_classify

# ---------------------------------------------------------------------------
# Helpers — build authentic SDK exceptions
# ---------------------------------------------------------------------------


def _overloaded_error(retry_after: str | None = None) -> OverloadedError:
    """Build a real anthropic OverloadedError (HTTP 529)."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp = httpx.Response(529, request=req, headers=headers)
    return OverloadedError("Overloaded", response=resp, body=None)


def _rate_limit_error() -> anthropic.RateLimitError:
    """Build a real anthropic RateLimitError (HTTP 429)."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError("Rate limited", response=resp, body=None)


def _bad_request_error() -> anthropic.BadRequestError:
    """Build a real anthropic BadRequestError (HTTP 400) — NON-transient."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError("Bad request", response=resp, body=None)


def _no_sleep(_seconds: float) -> None:
    """Sleep stub so tests never actually wait on backoff."""
    return None


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


# ---------------------------------------------------------------------------
# with_retry — unit behavior
# ---------------------------------------------------------------------------


class TestWithRetry:
    def test_returns_immediately_on_success(self) -> None:
        call = MagicMock(return_value="ok")
        result = with_retry(call, description="unit", sleep=_no_sleep)
        assert result == "ok"
        assert call.call_count == 1

    def test_retries_529_then_succeeds(self) -> None:
        """A 529 followed by success is retried and ultimately returns."""
        # Match responses by call count, NOT a fragile slot index.
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _overloaded_error()
            return "recovered"

        result = with_retry(flaky, description="unit", sleep=_no_sleep)
        assert result == "recovered"
        assert calls["n"] == 2

    def test_retries_429_then_succeeds(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _rate_limit_error()
            return "recovered"

        assert with_retry(flaky, description="unit", sleep=_no_sleep) == "recovered"
        assert calls["n"] == 2

    def test_retries_connection_error_then_succeeds(self) -> None:
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise anthropic.APIConnectionError(request=req)
            return "recovered"

        assert with_retry(flaky, description="unit", sleep=_no_sleep) == "recovered"
        assert calls["n"] == 2

    def test_persistent_529_raises_transient_after_exhaustion(self) -> None:
        """All attempts hit 529 -> TransientAPIError carrying the last error."""
        call = MagicMock(side_effect=_overloaded_error())
        with pytest.raises(TransientAPIError) as excinfo:
            with_retry(
                call,
                description="unit",
                max_attempts=3,
                sleep=_no_sleep,
            )
        assert call.call_count == 3
        assert excinfo.value.attempts == 3
        assert isinstance(excinfo.value.last_error, OverloadedError)

    def test_exhaustion_guard_is_O_safe(self) -> None:
        """The post-loop guard must NOT be a bare ``assert`` (issue #207).

        Under ``python -O`` asserts are stripped, so a control-flow guard
        built on ``assert`` vanishes. The real contract: after exhausting
        retries the wrapper re-raises the captured transient error wrapped in
        ``TransientAPIError`` — and it must do so via a runtime guard that
        survives ``-O``. We assert the raised type is ``TransientAPIError``
        (never ``AssertionError``), and that its ``last_error`` is the
        original transient error.
        """
        original = _overloaded_error()
        call = MagicMock(side_effect=original)
        with pytest.raises(TransientAPIError) as excinfo:
            with_retry(call, description="unit", max_attempts=2, sleep=_no_sleep)
        assert not isinstance(excinfo.value, AssertionError)
        assert excinfo.value.last_error is original

    def test_non_transient_error_is_not_retried(self) -> None:
        """A 400 BadRequestError fails fast — no retry, original error raised."""
        call = MagicMock(side_effect=_bad_request_error())
        with pytest.raises(anthropic.BadRequestError):
            with_retry(call, description="unit", sleep=_no_sleep)
        assert call.call_count == 1

    def test_sleep_called_between_attempts(self) -> None:
        """Backoff sleep fires once per retry (max_attempts - 1 on full failure)."""
        slept: list[float] = []
        call = MagicMock(side_effect=_overloaded_error())
        with pytest.raises(TransientAPIError):
            with_retry(
                call,
                description="unit",
                max_attempts=4,
                sleep=slept.append,
            )
        assert len(slept) == 3  # 4 attempts -> 3 backoff windows
        assert all(s <= 60.0 for s in slept)

    def test_honors_retry_after_header(self) -> None:
        """When the server sends Retry-After, the backoff uses it (capped)."""
        slept: list[float] = []
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _overloaded_error(retry_after="7")
            return "ok"

        with_retry(flaky, description="unit", sleep=slept.append)
        assert slept == [7.0]

    def test_retry_after_capped_at_max_delay(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _overloaded_error(retry_after="999")
            return "ok"

        with_retry(flaky, description="unit", max_delay=60.0, sleep=slept.append)
        assert slept == [60.0]

    def test_transient_errors_membership(self) -> None:
        """529/429/connection are transient; 400 is not."""
        assert anthropic.RateLimitError in TRANSIENT_ERRORS
        assert OverloadedError in TRANSIENT_ERRORS
        assert anthropic.APIConnectionError in TRANSIENT_ERRORS
        assert anthropic.BadRequestError not in TRANSIENT_ERRORS


# ---------------------------------------------------------------------------
# Integration — tier2_classify retries a 529 then succeeds
# ---------------------------------------------------------------------------


class TestClassificationRetryIntegration:
    def test_classify_retries_529_then_processes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 529 on the first classify call is retried; the file is processed
        (entities returned), not deferred."""
        # Patch the helper's sleep so the test doesn't wait on backoff.
        monkeypatch.setattr("athenaeum._retry.time.sleep", _no_sleep)

        raw = _make_raw("Met with Alice Zhang, who leads product at Acme Corp.")

        ok_response = MagicMock()
        ok_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Alice Zhang",
                            "entity_type": "person",
                            "tags": ["active"],
                            "access": "internal",
                            "observations": "Product leader at Acme Corp.",
                        }
                    ]
                )
            )
        ]

        # First call 529s, second returns the classification. Match by call
        # count via side_effect list (not a slot the helper indexes into).
        client = MagicMock()
        client.messages.create.side_effect = [_overloaded_error(), ok_response]

        results = tier2_classify(
            raw,
            matched_names=[],
            valid_types=["person"],
            valid_tags=["active"],
            valid_access=["internal"],
            client=client,
        )

        assert client.messages.create.call_count == 2  # retried once
        assert len(results) == 1
        assert results[0].name == "Alice Zhang"

    def test_classify_persistent_529_raises_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A persistent 529 surfaces as TransientAPIError (not a generic
        Exception), so the librarian can log it distinctly."""
        monkeypatch.setattr("athenaeum._retry.time.sleep", _no_sleep)

        raw = _make_raw("Some substantive content about Acme Corp.")
        client = MagicMock()
        client.messages.create.side_effect = _overloaded_error()

        with pytest.raises(TransientAPIError):
            tier2_classify(
                raw,
                matched_names=[],
                valid_types=["person"],
                valid_tags=["active"],
                valid_access=["internal"],
                client=client,
            )


# ---------------------------------------------------------------------------
# Integration — run() loop logs transient give-up distinctly from malformed
# ---------------------------------------------------------------------------


class TestRunDistinctGiveUpLog:
    """The run() loop must log a transient-API give-up distinctly from a
    malformed-file failure (acceptance criterion of #193)."""

    def _seed_knowledge_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        root.mkdir()
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| person |\n"
        )
        (wiki / "_schema" / "tags.md").write_text(
            "# Tags\n\n| Tag |\n|-----|\n| active |\n"
        )
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )
        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")
        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Runner"], cwd=root, check=True
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Met with Alice Zhang about product strategy at Acme Corp.\n"
        )
        return root

    def test_persistent_529_logged_as_transient_and_deferred(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        monkeypatch.setattr("athenaeum._retry.time.sleep", _no_sleep)
        root = self._seed_knowledge_root(tmp_path)
        raw_file = root / "raw" / "sessions" / "20240410T120000Z-aabbccdd.md"

        # Client always 529s on classify -> retries exhausted -> TransientAPIError.
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _overloaded_error()
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.DEBUG, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        messages = [r.getMessage() for r in caplog.records]
        # Distinct transient give-up line present...
        assert any(
            "transient API overload" in m and "Gave up after" in m for m in messages
        ), messages
        # ...and the generic malformed-file line is NOT used for this case.
        assert not any("Failed to process" in m for m in messages), messages
        # File deferred (not deleted) so the next healthy run can drain it.
        assert raw_file.exists()
        assert rc == 1  # run() returns 1 when files were deferred

    def test_malformed_file_logged_as_failed_not_transient(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-transient (malformed) failure still logs 'Failed to process',
        proving the two give-up log paths diverge."""
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        monkeypatch.setattr("athenaeum._retry.time.sleep", _no_sleep)
        root = self._seed_knowledge_root(tmp_path)
        raw_file = root / "raw" / "sessions" / "20240410T120000Z-aabbccdd.md"

        # Non-transient error (400) -> fails fast, generic malformed log path.
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _bad_request_error()
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.DEBUG, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        messages = [r.getMessage() for r in caplog.records]
        assert any("Failed to process" in m for m in messages), messages
        assert not any("transient API overload" in m for m in messages), messages
        assert raw_file.exists()
        assert rc == 1
        # Fast-fail: a malformed error is NOT retried.
        assert mock_client.messages.create.call_count == 1
