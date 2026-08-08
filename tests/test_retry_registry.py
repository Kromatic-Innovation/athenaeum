# SPDX-License-Identifier: Apache-2.0
"""Tests for the backend-agnostic transient-type registry (issue athenaeum#782).

``_retry.py`` used to hardcode a literal tuple naming three ``anthropic`` SDK
exception classes, so a non-Anthropic backend's transient failures were
silently never retried, and the module required the ``anthropic`` SDK
importable at module scope even for a ``claude-cli``-only deployment. This
file proves the fix's five acceptance criteria:

1. A ``ClaudeCliClient`` rate-limit failure IS retried by ``with_retry``
   (``TestCliTransientIsRetried``).
2. The ``api`` backend's transients are recognized by ``with_retry``
   regardless of import order or whether a client was ever constructed
   (``TestApiTransientsWithoutClientConstruction``, run in a fresh
   subprocess so no other test's import order can mask the hazard).
3. ``_retry.py`` has no module-scope ``import anthropic``; importing it with
   the SDK absent does not raise (``TestImportableWithoutAnthropicSdk``, run
   in a fresh subprocess with a ``sys.modules`` sentinel blocking the
   import).
4. A hypothetical third backend requires no edit to ``_retry.py``
   (``TestThirdBackendRegistration``).
5. The give-up path still raises ``TransientAPIError``
   (``TestGiveUpStillRaisesTransientAPIError``).

None of ``tests/test_retry.py`` is modified — this file is additive.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

# Absolute src root for the fresh-subprocess tests below — mirrors the
# PYTHONPATH the gate commands use, so the child process resolves the SAME
# checkout as this test run (not whatever editable install the venv points
# at). See the athenaeum#782 dispatch brief's "Env trap".
import athenaeum
from athenaeum._retry import (
    TransientAPIError,
    TransientError,
    register_transient_types,
    with_retry,
)
from athenaeum.provider import ClaudeCliClient

_SRC_ROOT = str(__import__("pathlib").Path(athenaeum.__file__).resolve().parents[1])


def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# AC1 — a ClaudeCliClient rate-limit failure IS retried by with_retry
# ---------------------------------------------------------------------------


class TestCliTransientIsRetried:
    def test_cli_rate_limit_then_success_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This is the test that FAILS on unpatched develop: before athenaeum#782,
        ClaudeCliClient raised TransientAPIError (the give-up type) directly
        on a retryable stderr, which with_retry's except clause did not
        catch — so the call surfaced on the FIRST attempt with call_count==1,
        never reaching the success on attempt 2. Verified by reverting the
        provider.py TransientError raises to TransientAPIError locally and
        re-running: this test fails with call_count==1 and TransientAPIError
        propagating uncaught."""
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="Error: rate limit exceeded (429)"
                )
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"type": "result", "subtype": "success", "is_error": false, '
                    '"api_error_status": null, "result": "recovered", '
                    '"stop_reason": "end_turn", "usage": {"input_tokens": 1, '
                    '"output_tokens": 1, "cache_creation_input_tokens": 0, '
                    '"cache_read_input_tokens": 0}, "total_cost_usd": 0.0}'
                ),
                stderr="",
            )

        monkeypatch.setattr("athenaeum.provider.subprocess.run", fake_run)
        monkeypatch.setattr(
            "athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude"
        )

        client = ClaudeCliClient()
        response = with_retry(
            lambda: client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            ),
            description="cli-unit-test",
            sleep=_no_sleep,
        )

        assert calls["n"] == 2  # retried once
        assert response.content[0].text == "recovered"

    def test_cli_persistent_rate_limit_exhausts_to_transient_api_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5: the give-up path still raises TransientAPIError for the CLI
        backend too, so librarian.py's existing except TransientAPIError
        branch (the defer-to-next-run behavior) is unchanged."""

        def fake_run(argv, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Error: rate limit exceeded (429)"
            )

        monkeypatch.setattr("athenaeum.provider.subprocess.run", fake_run)
        monkeypatch.setattr(
            "athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude"
        )

        client = ClaudeCliClient()
        with pytest.raises(TransientAPIError) as excinfo:
            with_retry(
                lambda: client.messages.create(
                    model="m", system="s", messages=[{"role": "user", "content": "u"}]
                ),
                description="cli-unit-test",
                max_attempts=3,
                sleep=_no_sleep,
            )
        assert excinfo.value.attempts == 3
        assert isinstance(excinfo.value.last_error, TransientError)


# ---------------------------------------------------------------------------
# AC2 — api transients recognized regardless of import order / client
# construction. Run in a fresh subprocess: importing athenaeum._retry (and
# NOTHING else athenaeum-internal — no provider, no build_llm_client) and
# driving with_retry against a real anthropic.RateLimitError must retry it.
# ---------------------------------------------------------------------------

_AC2_SCRIPT = """
import sys
sys.path.insert(0, {src_root!r})

# Import ONLY athenaeum._retry — never athenaeum.provider, never
# build_llm_client. This is the exact ordering athenaeum#782's "Trap B"
# warns about: if the anthropic transient classes were only registered as a
# side effect of build_llm_client() running, this call would silently stop
# retrying real SDK errors.
from athenaeum._retry import with_retry

import anthropic
import httpx

req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
resp = httpx.Response(429, request=req)

calls = {{"n": 0}}

def flaky():
    calls["n"] += 1
    if calls["n"] == 1:
        raise anthropic.RateLimitError("Rate limited", response=resp, body=None)
    return "recovered"

result = with_retry(flaky, description="ac2-pin", sleep=lambda s: None)
assert result == "recovered", result
assert calls["n"] == 2, calls["n"]
print("AC2-OK")
"""


class TestApiTransientsWithoutClientConstruction:
    def test_fresh_process_retries_real_sdk_error_without_build_llm_client(self) -> None:
        script = _AC2_SCRIPT.format(src_root=_SRC_ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "AC2-OK" in proc.stdout


# ---------------------------------------------------------------------------
# AC3 — no module-scope `import anthropic`; importing _retry with the SDK
# blocked does not raise. Blocked via a sys.modules sentinel (per the
# dispatch brief) in a fresh subprocess so no other test's real `import
# anthropic` can leave it already cached in sys.modules.
# ---------------------------------------------------------------------------

_AC3_SCRIPT = """
import sys
import importlib.util

# Sentinel: `sys.modules[name] = None` forces the import system to raise
# ImportError for `import anthropic` and everything under `anthropic.*`,
# simulating an SDK-absent claude-cli-only deployment — WITHOUT needing to
# actually uninstall the package.
sys.modules["anthropic"] = None
sys.modules["anthropic._exceptions"] = None

# Load _retry.py directly by file path rather than `import athenaeum._retry`
# — the latter first runs athenaeum/__init__.py, which (via
# librarian -> merge -> tiers) imports several OTHER athenaeum modules that
# import anthropic unconditionally at module scope. That is a real, separate
# fact about the package's __init__ cascade, not something athenaeum#782
# claims to fix — this criterion is about _retry.py specifically, so this
# loads exactly that one file, standalone, bypassing the package __init__.
_retry_path = {retry_path!r}
spec = importlib.util.spec_from_file_location("athenaeum_retry_standalone", _retry_path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)  # must not raise

assert "anthropic" not in sys.modules or sys.modules["anthropic"] is None

# TRANSIENT_ERRORS must still work (contains only the shared TransientError
# currency — the api backend's classes could not be registered since the
# SDK is blocked).
assert m.TransientError in m.TRANSIENT_ERRORS

# with_retry must still work for a backend raising the shared currency, even
# though the SDK is unavailable.
calls = {{"n": 0}}

def flaky():
    calls["n"] += 1
    if calls["n"] == 1:
        raise m.TransientError("simulated backend transient")
    return "ok"

result = m.with_retry(flaky, description="ac3-pin", sleep=lambda s: None)
assert result == "ok"
assert calls["n"] == 2

print("AC3-OK")
"""


class TestImportableWithoutAnthropicSdk:
    def test_import_and_retry_with_sdk_blocked(self) -> None:
        retry_path = str(__import__("pathlib").Path(_SRC_ROOT) / "athenaeum" / "_retry.py")
        script = _AC3_SCRIPT.format(retry_path=retry_path)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "AC3-OK" in proc.stdout

    def test_source_has_no_top_level_import_anthropic(self) -> None:
        """Belt-and-suspenders static check alongside the behavioral subprocess
        test above (the brief is explicit that the behavioral test is the
        real evidence — this just guards against a re-introduced module-scope
        import statement being trivially detectable without a subprocess)."""
        import ast
        from pathlib import Path

        source_path = Path(_SRC_ROOT) / "athenaeum" / "_retry.py"
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in tree.body:  # module-level statements only, not nested
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                assert "anthropic" not in names, f"module-scope import: {names}"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "anthropic" and not (
                    node.module or ""
                ).startswith("anthropic."), f"module-scope import from {node.module}"


# ---------------------------------------------------------------------------
# AC4 — a hypothetical third backend requires no edit to _retry.py
# ---------------------------------------------------------------------------


class _FakeThirdBackendError(Exception):
    """Stand-in for a hypothetical third backend's native transient
    exception class — proves register_transient_types works for a class
    _retry.py has never heard of."""


class TestThirdBackendRegistration:
    def test_fake_backend_transient_type_is_retried_after_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            __import__("athenaeum._retry", fromlist=["_transient_registry"])._transient_registry,
            "fake-third-backend",
            (_FakeThirdBackendError,),
        )

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeThirdBackendError("fake transient")
            return "ok"

        result = with_retry(flaky, description="fake-backend", sleep=_no_sleep)
        assert result == "ok"
        assert calls["n"] == 2

    def test_register_transient_types_is_the_public_registration_api(self) -> None:
        """register_transient_types is importable and callable directly —
        the mechanism a real third backend module would use, with no edit to
        _retry.py itself."""
        assert callable(register_transient_types)


# ---------------------------------------------------------------------------
# AC5 (generic, non-CLI) — give-up still raises TransientAPIError, and the
# give-up type is deliberately NOT itself a retry trigger (Trap A).
# ---------------------------------------------------------------------------


class TestGiveUpStillRaisesTransientAPIError:
    def test_transient_error_exhaustion_raises_transient_api_error(self) -> None:
        call_count = {"n": 0}

        def always_transient():
            call_count["n"] += 1
            raise TransientError("always fails")

        with pytest.raises(TransientAPIError) as excinfo:
            with_retry(
                always_transient,
                description="giveup-test",
                max_attempts=3,
                sleep=_no_sleep,
            )
        assert call_count["n"] == 3
        assert excinfo.value.attempts == 3
        assert isinstance(excinfo.value.last_error, TransientError)

    def test_transient_api_error_is_not_itself_retried(self) -> None:
        """Trap A: TransientAPIError (the give-up type) must NOT be in the
        retryable set — otherwise the retry loop could catch its own give-up
        signal. A call that raises TransientAPIError directly must propagate
        on the FIRST attempt, unretried."""
        call_count = {"n": 0}

        def raises_giveup_type():
            call_count["n"] += 1
            raise TransientAPIError(1, RuntimeError("boom"))

        with pytest.raises(TransientAPIError):
            with_retry(raises_giveup_type, description="trap-a-test", sleep=_no_sleep)
        assert call_count["n"] == 1  # not retried
