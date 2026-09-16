# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1653 — bounded ``last_error_detail`` alongside the bare last_error class name.

The stuck-file ledger (athenaeum#663) used to record only ``type(exc).__name__``
for a failing raw file, so a provider ``BadRequestError`` could never be
root-caused once the run log rotated. This suite pins:

- :func:`athenaeum.stuck_ledger.error_detail` — a pure helper that reads
  ``status_code`` / ``request_id`` / the response body's ``error.type`` /
  ``error.message`` off a fake exception shaped like the ``anthropic`` SDK's
  ``APIStatusError`` (never the real SDK class — no live API, no network),
  unwraps a ``TransientAPIError``-shaped wrapper's ``last_error`` first,
  collapses whitespace, and caps at 500 characters.
- :func:`athenaeum.librarian._record_stuck_failure`'s new keyword-only
  ``error_detail`` parameter: sets ``entry["last_error_detail"]`` when given,
  removes any stale value when ``None``.
- The **hard invariant**: ``last_error`` stays the bare exception class name
  regardless of ``error_detail`` — :func:`athenaeum.stuck_ledger.held_stuck_summary`
  groups on it and ``_RETIRED_LAST_ERRORS`` matches it exactly.
- Both real call sites in ``librarian.py`` (the ``TransientAPIError`` branch
  and the non-transient branch, including the pre-action ``last_action: null``
  path) via the ``run()`` harness, with the detail surfacing all the way into
  ``out_run_stats["stuck_files"]`` and both the newly-stuck and hold-out
  ``STUCK_FILE_PREFIX`` warnings.
- A ledger written before this change (no ``last_error_detail`` key at all)
  still loads and summarises identically.

Every exception here is a fake, hand-built with the SDK's documented
attribute names (``status_code``, ``request_id``, ``body``) -- no
``anthropic`` import, no live API, no network. Every ledger is a ``tmp_path``
fixture. This lane never touches a live store.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from athenaeum._retry import TransientAPIError
from athenaeum.librarian import (
    STUCK_FILE_PREFIX,
    _load_stuck_ledger,
    _record_stuck_failure,
    run,
)
from athenaeum.models import RawFile
from athenaeum.stuck_ledger import (
    STUCK_MANIFEST_NAME,
    error_detail,
    held_stuck_summary,
    load_stuck_ledger,
    stuck_content_hash,
)

# Reuse the deadline suite's run harness, same as tests/test_librarian_stuck_files.py.
from tests.test_librarian_deadline import _seed_knowledge_root

# A distinctive marker standing in for "a raw file's real content" / "a
# request payload" -- must NEVER appear in a produced detail string.
_SECRET_PAYLOAD_MARKER = "PII-SECRET-RAW-FILE-PAYLOAD-MARKER"


def _make_raw(content: str, name: str = "aabb0011") -> RawFile:
    return RawFile(
        path=Path(f"/tmp/fake/sessions/20240407T120000Z-{name}.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8=name,
        _content=content,
    )


class _FakeProviderError(Exception):
    """Stand-in for ``anthropic.BadRequestError`` / ``APIStatusError`` --
    same documented attribute names (``status_code``, ``request_id``,
    ``body``), no SDK import, no live API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str,
        error_type: str,
        error_message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.body = {"error": {"type": error_type, "message": error_message}}
        # A real request payload -- error_detail() must NEVER read this.
        self.request = SimpleNamespace(content=_SECRET_PAYLOAD_MARKER.encode())


# ---------------------------------------------------------------------------
# Pure helper: athenaeum.stuck_ledger.error_detail
# ---------------------------------------------------------------------------


class TestErrorDetailHelper:
    def test_includes_status_request_id_type_and_message(self) -> None:
        exc = _FakeProviderError(
            "Error code: 400 - bad request",
            status_code=400,
            request_id="req_abc123",
            error_type="invalid_request_error",
            error_message="max_tokens exceeds the model's context window",
        )
        detail = error_detail(exc)
        assert "400" in detail
        assert "req_abc123" in detail
        assert "invalid_request_error" in detail
        assert "max_tokens exceeds the model's context window" in detail

    def test_plain_exception_falls_back_to_str(self) -> None:
        detail = error_detail(ValueError("just a plain message"))
        assert detail == "just a plain message"

    def test_caps_at_500_characters(self) -> None:
        long_text = "x" * 2000
        exc = _FakeProviderError(
            long_text,
            status_code=400,
            request_id="r",
            error_type="t",
            error_message=long_text,
        )
        detail = error_detail(exc)
        assert len(detail) == 500

    def test_collapses_whitespace(self) -> None:
        exc = _FakeProviderError(
            "multi\n\nline\tmessage",
            status_code=400,
            request_id="r",
            error_type="t",
            error_message="also\nmulti\nline",
        )
        detail = error_detail(exc)
        assert "\n" not in detail
        assert "\t" not in detail

    def test_never_includes_request_payload_or_raw_content(self) -> None:
        exc = _FakeProviderError(
            "Error code: 400 - bad request",
            status_code=400,
            request_id="req_abc123",
            error_type="invalid_request_error",
            error_message="short message",
        )
        detail = error_detail(exc)
        assert _SECRET_PAYLOAD_MARKER not in detail

        # Extra, non-error keys on the body (e.g. an echoed prompt) must also
        # never be read -- only body["error"]["type"/"message"] is.
        exc2 = Exception("plain")
        exc2.body = {  # type: ignore[attr-defined]
            "error": {"type": "t", "message": "m"},
            "input": {"prompt": _SECRET_PAYLOAD_MARKER},
        }
        detail2 = error_detail(exc2)
        assert _SECRET_PAYLOAD_MARKER not in detail2

    def test_unwraps_transient_api_error_last_error(self) -> None:
        inner = _FakeProviderError(
            "Error code: 529 - Overloaded",
            status_code=529,
            request_id="req_529xyz",
            error_type="overloaded_error",
            error_message="Overloaded",
        )
        wrapper = TransientAPIError(attempts=3, last_error=inner)
        detail = error_detail(wrapper)
        assert "529" in detail
        assert "req_529xyz" in detail
        assert "overloaded_error" in detail
        assert "Overloaded" in detail


# ---------------------------------------------------------------------------
# _record_stuck_failure: set / clear error_detail; last_error stays the class name
# ---------------------------------------------------------------------------


class TestRecordStuckFailureErrorDetail:
    def test_sets_last_error_detail_when_given(self) -> None:
        ledger: dict = {}
        raw = _make_raw("content")
        entry = _record_stuck_failure(
            ledger,
            raw,
            error="BadRequestError",
            action=None,
            threshold=1,
            error_detail="status_code=400 error.type=invalid_request_error",
        )
        assert entry is not None
        assert entry["last_error"] == "BadRequestError"
        assert entry["last_error_detail"] == "status_code=400 error.type=invalid_request_error"
        # Pre-action path (issue athenaeum#1653 AC3): no failing action was
        # ever annotated, so last_action must never appear.
        assert "last_action" not in entry

    def test_clears_stale_detail_when_none(self) -> None:
        ledger: dict = {}
        raw = _make_raw("content")
        _record_stuck_failure(
            ledger,
            raw,
            error="BadRequestError",
            action="update:X",
            threshold=5,
            error_detail="detail-1",
        )
        assert ledger[raw.ref]["last_error_detail"] == "detail-1"
        _record_stuck_failure(
            ledger,
            raw,
            error="BadRequestError",
            action="update:X",
            threshold=5,
            error_detail=None,
        )
        assert "last_error_detail" not in ledger[raw.ref]

    def test_omitted_error_detail_defaults_to_none_and_sets_nothing(self) -> None:
        """Byte-identical to pre-athenaeum#1653 behavior for a caller that
        does not pass error_detail at all (matches every existing test in
        tests/test_librarian_stuck_files.py)."""
        ledger: dict = {}
        raw = _make_raw("content")
        entry = _record_stuck_failure(ledger, raw, error="E", action="update:X", threshold=1)
        assert entry is not None
        assert "last_error_detail" not in entry


# ---------------------------------------------------------------------------
# Hard invariant: last_error stays the bare class name; held_stuck_summary /
# _RETIRED_LAST_ERRORS behavior is byte-identical with or without a detail.
# ---------------------------------------------------------------------------


class TestHardInvariantUnchanged:
    def test_last_error_and_held_stuck_summary_unaffected_by_detail(self) -> None:
        raw = _make_raw("content")
        detail = (
            "status_code=400 request_id=req_1 error.type=invalid_request_error error.message=bad"
        )

        with_detail: dict = {}
        for _ in range(2):
            crossed = _record_stuck_failure(
                with_detail,
                raw,
                error="BadRequestError",
                action=None,
                threshold=2,
                error_detail=detail,
            )
        assert crossed is not None
        assert crossed["last_error"] == "BadRequestError"  # hard invariant
        assert crossed["last_error_detail"] == detail

        without_detail: dict = {}
        for _ in range(2):
            _record_stuck_failure(
                without_detail, raw, error="BadRequestError", action=None, threshold=2
            )

        summary_with = held_stuck_summary(with_detail, [raw])
        summary_without = held_stuck_summary(without_detail, [raw])
        # held_stuck_summary never reads last_error_detail -- identical output.
        assert summary_with == summary_without
        assert summary_with == {
            "considered": 1,
            "held": 1,
            "dominant_error": "BadRequestError",
            "dominant_error_count": 1,
            "error_counts": {"BadRequestError": 1},
        }

    def test_retired_last_error_still_dropped_when_detail_present(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        payload = {
            "updated": "2026-09-15T00:00:00+00:00",
            "files": {
                "sessions/retired.md": {
                    "hash": "deadbeef",
                    "failures": 5,
                    "escalated": True,
                    "last_error": "PersonNeverLLMRewriteError",
                    "last_error_detail": "some detail that must not save this entry",
                }
            },
        }
        (wiki_root / STUCK_MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
        assert load_stuck_ledger(wiki_root) == {}


# ---------------------------------------------------------------------------
# Backward compatibility: a ledger written before this change loads and
# summarises exactly as before.
# ---------------------------------------------------------------------------


class TestPreChangeLedgerCompatibility:
    def test_old_ledger_without_detail_field_loads_and_summarises_identically(
        self, tmp_path: Path
    ) -> None:
        raw = _make_raw("content")
        old_entry = {
            "hash": stuck_content_hash(raw),
            "failures": 3,
            "escalated": True,
            "first_failed": "2026-09-01T00:00:00+00:00",
            "last_failed": "2026-09-10T00:00:00+00:00",
            "last_action": None,
            "last_error": "BadRequestError",
            # deliberately no "last_error_detail" key at all
        }
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        payload = {"updated": "2026-09-10T00:00:00+00:00", "files": {raw.ref: old_entry}}
        (wiki_root / STUCK_MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_stuck_ledger(wiki_root)
        assert loaded[raw.ref] == old_entry  # untouched, no field injected

        summary = held_stuck_summary(loaded, [raw])
        assert summary["held"] == 1
        assert summary["dominant_error"] == "BadRequestError"
        assert summary["dominant_error_count"] == 1


# ---------------------------------------------------------------------------
# End-to-end via the run() harness: both real call sites in librarian.py
# ---------------------------------------------------------------------------


class TestRunHarnessCallSites:
    def test_non_transient_pre_action_failure_carries_detail_into_summary(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """The non-transient branch (librarian.py's bare ``except Exception``),
        pre-action path: process_one raises BEFORE any tier3_write action is
        annotated, mirroring the two real ``last_action: null`` ledger
        entries the issue's motivation cites."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        seeded_text = "Met with Alice Zhang about topic 0 at Acme Corp."
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "1")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")

        def fake_process_one(raw, *args, **kwargs):
            raise _FakeProviderError(
                "Error code: 400 - bad request",
                status_code=400,
                request_id="req_preaction",
                error_type="invalid_request_error",
                error_message="max_tokens exceeds context window",
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats,
        )

        assert len(stats["stuck_files"]) == 1
        surfaced = stats["stuck_files"][0]
        assert surfaced["action"] is None  # pre-action path
        assert surfaced["error"] == "_FakeProviderError"  # bare class name, unchanged shape
        detail = surfaced["error_detail"]
        assert detail is not None
        expected_substrings = (
            "400",
            "req_preaction",
            "invalid_request_error",
            "max_tokens exceeds context window",
        )
        for expected in expected_substrings:
            assert expected in detail
        assert _SECRET_PAYLOAD_MARKER not in detail
        assert seeded_text not in detail  # AC4: never the raw file's own text
        assert STUCK_FILE_PREFIX in caplog.text
        assert "detail=" in caplog.text

        ledger = _load_stuck_ledger(root / "wiki")
        ref = next(iter(ledger))
        assert ledger[ref]["last_error"] == "_FakeProviderError"
        assert "last_action" not in ledger[ref]
        assert ledger[ref]["last_error_detail"] == detail

    def test_transient_branch_carries_unwrapped_detail(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """The transient branch (librarian.py's ``except TransientAPIError``):
        the ledger's ``last_error`` stays ``TransientAPIError:<inner class>``
        (unchanged shape) and the detail is unwrapped from ``.last_error``."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "1")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")

        inner = _FakeProviderError(
            "Error code: 529 - Overloaded",
            status_code=529,
            request_id="req_529xyz",
            error_type="overloaded_error",
            error_message="Overloaded",
        )

        def fake_process_one(raw, *args, **kwargs):
            raise TransientAPIError(attempts=3, last_error=inner)

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats,
        )

        assert len(stats["stuck_files"]) == 1
        surfaced = stats["stuck_files"][0]
        assert surfaced["error"] == "TransientAPIError:_FakeProviderError"  # unchanged shape
        detail = surfaced["error_detail"]
        assert detail is not None
        for expected in ("529", "req_529xyz", "overloaded_error", "Overloaded"):
            assert expected in detail
        assert _SECRET_PAYLOAD_MARKER not in detail

        ledger = _load_stuck_ledger(root / "wiki")
        ref = next(iter(ledger))
        assert ledger[ref]["last_error"] == "TransientAPIError:_FakeProviderError"
        assert ledger[ref]["last_error_detail"] == detail

    def test_hold_out_warning_carries_detail_on_a_later_run(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A file already over threshold is held out of a LATER run's intake
        window by ``_hold_out_unworkable_raw`` -- its warning must carry the
        detail too, not just the newly-stuck warning from the crossing run."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "1")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")

        def fake_process_one(raw, *args, **kwargs):
            raise _FakeProviderError(
                "Error code: 400 - bad request",
                status_code=400,
                request_id="req_holdout",
                error_type="invalid_request_error",
                error_message="max_tokens exceeds context window",
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        def _run() -> dict:
            stats: dict = {}
            run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_api_calls=100,
                max_runtime=0,
                out_run_stats=stats,
            )
            return stats

        stats1 = _run()  # crosses threshold=1 immediately -- newly-stuck warning
        assert len(stats1["stuck_files"]) == 1
        assert stats1["stuck_files"][0]["error_detail"]
        assert "req_holdout" in stats1["stuck_files"][0]["error_detail"]

        caplog.clear()
        stats2 = _run()  # now known-stuck -- held out of intake, NOT re-attempted
        assert len(stats2["stuck_files"]) == 1
        surfaced2 = stats2["stuck_files"][0]
        assert surfaced2["error_detail"]
        assert "req_holdout" in surfaced2["error_detail"]
        assert STUCK_FILE_PREFIX in caplog.text
        assert "detail=" in caplog.text
        assert "req_holdout" in caplog.text
        assert "invalid_request_error" in caplog.text
