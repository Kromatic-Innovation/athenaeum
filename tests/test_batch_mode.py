# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#236 — opt-in Batch API mode for the librarian's tier-2/tier-3 calls.

Covers:
- Flag resolution: ``--batch-mode`` CLI > ``ATHENAEUM_BATCH_MODE`` env >
  yaml ``librarian.batch_mode`` > default off (athenaeum#232 resolver pattern).
- Equivalence: a batch-mode run produces wiki output identical to the
  synchronous path on the same intake with the same (fake, deterministic)
  responses.
- Budget semantics: ``ATHENAEUM_MAX_API_CALLS`` enforced at batch-assembly
  time; remainder deferred via the athenaeum#220 manifest.
- Per-result failures (``errored`` results) map onto the existing per-file
  failure path: raw file stays on disk, run returns 1.
- Same-page tier-3 merges stay synchronous and serialized in file order.
- Usage accounting: batch results feed ``TokenUsage`` (incl. cache
  counters) and bill at the 50% batch discount in ``estimated_cost_usd``.
- Polling: bounded by module constants, injectable sleep; timeout cancels.

All Anthropic traffic is faked; no live API, no network.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import anthropic as anthropic_mod
import pytest

import athenaeum.models as models_mod
from athenaeum import batch_state
from athenaeum.batch import (
    BATCH_MAX_REQUESTS,
    BATCH_POLL_INTERVAL_SECONDS,
    BatchExecutionError,
    BatchRequest,
    BatchRunResult,
    collect_pending_batches,
    execute_batch,
    process_batch_run,
)
from athenaeum.cli import main
from athenaeum.intake import discover_raw_files
from athenaeum.librarian import (
    EXIT_LIBRARIAN_REFUSAL,
    FALLBACK_ACCESS,
    FALLBACK_TAGS,
    librarian_batch_mode,
    run,
)
from athenaeum.models import EntityIndex, TokenUsage
from athenaeum.schemas import KNOWN_TYPES
from athenaeum.tiers import DEFAULT_CLASSIFY_MODEL, tier1_programmatic_match

# Issue athenaeum#964: ``librarian.FALLBACK_TYPES`` was consolidated into the
# one ``schemas.KNOWN_TYPES`` definition (drift fix -- see librarian.py's
# ``_run_entity_tier_phase`` call site for the same substitution). Sorted for
# a deterministic ``list[str]``, matching what ``process_batch_run`` expects.
FALLBACK_TYPES = sorted(KNOWN_TYPES)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _msg(
    text: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    stop_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


class _FakeBatches:
    """Stand-in for ``client.messages.batches`` (create/retrieve/results/cancel)."""

    def __init__(
        self,
        responder: Callable[[dict[str, Any]], str],
        *,
        polls_until_end: int = 1,
        never_end: bool = False,
        fail_marker: str | None = None,
        create_error: Exception | None = None,
        truncate_marker: str | None = None,
    ) -> None:
        self._responder = responder
        self._polls_until_end = polls_until_end
        self._never_end = never_end
        self._fail_marker = fail_marker
        self._create_error = create_error
        # Issue athenaeum#476: a request whose content contains this marker is returned
        # from the batch TRUNCATED (unterminated array + stop_reason
        # max_tokens), so a run-level test can exercise the batch-path
        # bigger-budget retry (the sync ``messages.create`` recovers it).
        self._truncate_marker = truncate_marker
        self.submitted: list[list[dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self._retrieve_counts: dict[str, int] = {}

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        if self._create_error is not None:
            raise self._create_error
        requests = list(requests)
        self.submitted.append(requests)
        batch_id = f"msgbatch_{len(self.submitted)}"
        self._retrieve_counts[batch_id] = 0
        # A batch that completes within the first poll cycle is reported as
        # already ``ended`` at create time, so ``execute_batch``'s poll loop
        # is skipped and no real ``time.sleep(BATCH_POLL_INTERVAL_SECONDS)``
        # runs. The run()-level batch tests reach ``execute_batch`` through
        # ``process_batch_run`` and cannot inject a no-op ``sleep``; without
        # this they each block ~30s per batch on wall-clock poll intervals.
        # Tests that specifically exercise the polling loop opt in via
        # ``polls_until_end`` (>1) or ``never_end`` and inject their own sleep.
        ends_immediately = not self._never_end and self._polls_until_end <= 1
        return SimpleNamespace(
            id=batch_id,
            processing_status="ended" if ends_immediately else "in_progress",
        )

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self._retrieve_counts[batch_id] += 1
        ended = (
            not self._never_end
            and self._retrieve_counts[batch_id] >= self._polls_until_end
        )
        return SimpleNamespace(
            id=batch_id,
            processing_status="ended" if ended else "in_progress",
        )

    def results(self, batch_id: str):
        idx = int(batch_id.split("_")[1]) - 1
        for req in self.submitted[idx]:
            user_msg = req["params"]["messages"][0]["content"]
            if self._fail_marker and self._fail_marker in user_msg:
                yield SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(
                        type="errored",
                        error=SimpleNamespace(type="invalid_request"),
                    ),
                )
            elif self._truncate_marker and self._truncate_marker in user_msg:
                # athenaeum#476: an unterminated array cut off at the output budget.
                yield SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(
                        type="succeeded",
                        message=_msg(
                            '[{"name": "WidgetTrunc", "entity_type": "concept"',
                            stop_reason="max_tokens",
                        ),
                    ),
                )
            else:
                yield SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(
                        type="succeeded",
                        message=_msg(self._responder(req["params"])),
                    ),
                )

    def cancel(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)


class _FakeClient:
    """Fake Anthropic client exposing sync ``messages.create`` AND batches.

    Issue athenaeum#554 (L11): left ad-hoc rather than repointed at
    ``tests.conftest.FakeLLMClient`` — it also models the
    ``client.messages.batches`` API surface (create/retrieve/results/cancel
    for batch mode), which the shared canned-response double doesn't cover.
    """

    def __init__(
        self,
        responder: Callable[[dict[str, Any]], str],
        *,
        allow_sync: bool = True,
        **batch_kwargs: Any,
    ) -> None:
        self.sync_calls: list[dict[str, Any]] = []
        self.batches = _FakeBatches(responder, **batch_kwargs)

        def create(**params: Any) -> SimpleNamespace:
            if not allow_sync:
                raise AssertionError(
                    "unexpected synchronous messages.create in batch mode"
                )
            self.sync_calls.append(params)
            return _msg(responder(params))

        self.messages = SimpleNamespace(create=create, batches=self.batches)


def _scripted_responder(params: dict[str, Any]) -> str:
    """Deterministic responses keyed only on request content.

    Drives BOTH the sync and batch paths so the equivalence test compares
    identical model behavior across the two transports.
    """
    user_msg = params["messages"][0]["content"]
    if params["model"] == DEFAULT_CLASSIFY_MODEL:
        m = re.search(r"Widget(\w+)", user_msg)
        if m:
            name = f"Widget{m.group(1)}"
            extra = " FAILCREATE" if "Bad" in name else ""
            return json.dumps(
                [
                    {
                        "name": name,
                        "entity_type": "concept",
                        "tags": [],
                        "access": "internal",
                        "observations": f"Facts about {name}.{extra}",
                    }
                ]
            )
        return "[]"
    if "## Entity to create" in user_msg:
        name = re.search(r"^Name: (.+)$", user_msg, re.MULTILINE).group(1)
        return f"# {name}\n\nFacts about {name}.\n\n[^1]: src"
    if "## Existing page content" in user_msg:
        # Issue athenaeum#469: the merge contract is now anchored edit operations, not
        # a full-page echo. An append_section op yields the same merged page
        # ("...\n\nMerged note from {src}.") the full-echo responder produced,
        # so every downstream content assertion is unchanged.
        src = re.search(r"## New observation \(source: (.+)\)", user_msg).group(1)
        return json.dumps(
            {"ops": [{"op": "append_section", "text": f"Merged note from {src}."}]}
        )
    raise AssertionError(f"unrecognized request: {user_msg[:120]}")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_root(
    tmp_path: Path,
    name: str,
    raw_contents: list[str],
    *,
    with_acme: bool = False,
) -> Path:
    root = tmp_path / name
    root.mkdir()
    wiki = root / "wiki"
    wiki.mkdir()
    if with_acme:
        (wiki / "acme1234-acme-corp.md").write_text(
            textwrap.dedent(
                """\
                ---
                uid: acme1234
                type: company
                name: Acme Corp
                access: internal
                created: '2024-01-01'
                updated: '2024-01-01'
                ---

                # Acme Corp

                Original body line.
            """
            ),
            encoding="utf-8",
        )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    for i, content in enumerate(raw_contents):
        (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
            content, encoding="utf-8"
        )
    return root


def _patch_uids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic uid sequence so sync and batch runs name pages alike."""
    counter = itertools.count(1)
    monkeypatch.setattr(
        "athenaeum.tiers.generate_uid", lambda: f"uid{next(counter):05d}"
    )


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    for var in (
        "ATHENAEUM_BATCH_MODE",
        "ATHENAEUM_MAX_API_CALLS",
        "ATHENAEUM_MAX_FILES",
        "ATHENAEUM_CLASSIFY_MODEL",
        "ATHENAEUM_WRITE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _freeze_recorded_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue athenaeum#1064: pin ``WikiEntity.__post_init__``'s ``recorded_at``
    stamp to one fixed instant for the duration of the test.

    The batch/sync equivalence tests run TWO full ``run()`` passes and
    assert their wiki output is byte-identical. Each pass stamps every new
    entity's ``recorded_at`` from the real wall clock independently, so a
    test whose two passes straddle a wall-clock second boundary fails on
    nothing but that one-second skew even though the equivalence the test
    exists to prove holds. Freezing the shared clock (rather than excluding
    ``recorded_at`` from ``_wiki_snapshot``) keeps the comparison a strict
    equality — see the issue for why the exclusion was rejected as a real
    weakening of what the test proves.
    """
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(models_mod, "_recorded_time_now", lambda: fixed)


def _wiki_snapshot(root: Path) -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted((root / "wiki").glob("*.md"))
    }


def _all_batch_messages(client: _FakeClient) -> list[str]:
    return [
        req["params"]["messages"][0]["content"]
        for batch in client.batches.submitted
        for req in batch
    ]


# ---------------------------------------------------------------------------
# Flag resolution: env > yaml > default
# ---------------------------------------------------------------------------


class TestBatchModeResolution:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_MODE", raising=False)
        assert librarian_batch_mode(None) is False
        assert librarian_batch_mode({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("ATHENAEUM_BATCH_MODE", value)
        assert librarian_batch_mode(None) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_env_falsy_wins_over_yaml(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_BATCH_MODE", value)
        assert librarian_batch_mode({"librarian": {"batch_mode": True}}) is False

    def test_yaml_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_MODE", raising=False)
        assert librarian_batch_mode({"librarian": {"batch_mode": True}}) is True
        assert librarian_batch_mode({"librarian": {"batch_mode": False}}) is False

    def test_invalid_env_falls_through_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_BATCH_MODE", "banana")
        assert librarian_batch_mode({"librarian": {"batch_mode": True}}) is True

    def test_non_bool_yaml_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_MODE", raising=False)
        # bool is an int subclass elsewhere; here a string must not enable.
        assert librarian_batch_mode({"librarian": {"batch_mode": "yes"}}) is False


class TestBatchModeCLI:
    def _capture_run(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import athenaeum.librarian as librarian_mod

        captured: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(librarian_mod, "run", fake_run)
        return captured

    def test_flag_passes_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture_run(monkeypatch)
        rc = main(
            ["run", "--knowledge-root", str(tmp_path), "--dry-run", "--batch-mode"]
        )
        assert rc == 0
        assert captured["batch_mode"] is True

    def test_absent_passes_none_so_resolver_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture_run(monkeypatch)
        rc = main(["run", "--knowledge-root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert captured["batch_mode"] is None

    def test_no_flag_passes_false_overriding_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The off-switch: --no-batch-mode pins False even when the env
        # default is on (explicit CLI > env > yaml precedence).
        monkeypatch.setenv("ATHENAEUM_BATCH_MODE", "1")
        captured = self._capture_run(monkeypatch)
        rc = main(
            ["run", "--knowledge-root", str(tmp_path), "--dry-run", "--no-batch-mode"]
        )
        assert rc == 0
        assert captured["batch_mode"] is False

    def test_explicit_false_overrides_env_on_at_run_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = ["Standalone fact about WidgetEnv gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_BATCH_MODE", "1")
        client = _FakeClient(_scripted_responder)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=False,
        )
        assert rc == 0
        # Synchronous path used despite env=on: no Batch API traffic.
        assert client.batches.submitted == []
        assert client.sync_calls


# ---------------------------------------------------------------------------
# process_batch_run — per-knob client routing (issue athenaeum#841)
#
# ``process_batch_run`` threads TWO clients: ``client`` (the tier-2 classify
# batch) and ``write_client`` (the tier-3 write batch + its same-page-merge/
# truncation-retry synchronous fallbacks). A direct unit call (not through
# ``run()``) lets the test hold two GENUINELY DISTINCT fake clients — the
# batch-mode startup guard forces both knobs onto the SAME provider in a
# real run (claude-cli is rejected for batch mode on either knob), so
# ``run()`` can never actually exercise two distinct client OBJECTS here;
# this proves the wiring itself, independent of that production constraint.
# ---------------------------------------------------------------------------


class TestProcessBatchRunPerKnobClientRouting:
    def test_tier2_batch_uses_client_tier3_batch_uses_write_client(
        self, tmp_path: Path
    ) -> None:
        contents = [
            "Standalone fact about WidgetRoute gadget.\n",
        ]
        root = _seed_root(tmp_path, "k", contents)
        raw_files = discover_raw_files(root / "raw")
        index = EntityIndex(root / "wiki")

        classify_client = _FakeClient(_scripted_responder, allow_sync=False)
        write_client = _FakeClient(_scripted_responder, allow_sync=False)

        result = process_batch_run(
            raw_files,
            index,
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
        )
        assert result.created == 1

        # Tier-2 (classify) requests landed on classify_client's batch
        # transport ONLY.
        assert len(classify_client.batches.submitted) == 1
        assert all(
            req["params"]["model"] == DEFAULT_CLASSIFY_MODEL
            for batch in classify_client.batches.submitted
            for req in batch
        )
        # Tier-3 (write) requests landed on write_client's batch transport
        # ONLY — never classify_client's.
        assert len(write_client.batches.submitted) == 1
        assert all(
            req["params"]["model"] != DEFAULT_CLASSIFY_MODEL
            for batch in write_client.batches.submitted
            for req in batch
        )
        assert classify_client is not write_client

    def test_no_write_client_falls_back_to_client_ac6(self, tmp_path: Path) -> None:
        """AC6: every pre-athenaeum#841 caller only ever passed the one
        positional ``client`` — omitting ``write_client`` must still serve
        BOTH batches off that ONE client, byte-identical to before."""
        contents = ["Standalone fact about WidgetSame gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        raw_files = discover_raw_files(root / "raw")
        index = EntityIndex(root / "wiki")

        client = _FakeClient(_scripted_responder, allow_sync=False)

        result = process_batch_run(
            raw_files,
            index,
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
        )
        assert result.created == 1
        # Both the tier-2 and tier-3 batches submitted through the SAME
        # (only) client.
        assert len(client.batches.submitted) == 2


# ---------------------------------------------------------------------------
# Equivalence: batch output == sync output for the same intake + responses
# ---------------------------------------------------------------------------


class TestBatchSyncEquivalence:
    def test_wiki_output_identical(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        contents = [
            "Standalone fact about WidgetAlpha gadget.\n",
            "Notes about WidgetBeta device.\n",
            "Acme Corp shipped a new product.\n",
        ]
        root_sync = _seed_root(tmp_path, "sync", contents, with_acme=True)
        root_batch = _seed_root(tmp_path, "batch", contents, with_acme=True)
        _clean_env(monkeypatch)
        _freeze_recorded_at(monkeypatch)
        caplog.set_level(logging.INFO, logger="athenaeum")

        sync_client = _FakeClient(_scripted_responder)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: sync_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_sync / "raw",
                wiki_root=root_sync / "wiki",
                knowledge_root=root_sync,
            )
            == 0
        )
        # Flag off → the Batch API surface is never touched.
        assert sync_client.batches.submitted == []
        assert sync_client.sync_calls, "sync path made no API calls"
        sync_done = [
            r.getMessage() for r in caplog.records if r.getMessage().startswith("Done:")
        ]
        caplog.clear()

        batch_client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: batch_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_batch / "raw",
                wiki_root=root_batch / "wiki",
                knowledge_root=root_batch,
                batch_mode=True,
            )
            == 0
        )
        batch_done = [
            r.getMessage() for r in caplog.records if r.getMessage().startswith("Done:")
        ]

        assert _wiki_snapshot(root_batch) == _wiki_snapshot(root_sync)
        # Summary accounting (created/updated/escalated/skipped/failed)
        # identical between the two transports.
        assert sync_done and sync_done == batch_done
        # Intake fully consumed on both paths.
        assert not list((root_sync / "raw" / "sessions").glob("*.md"))
        assert not list((root_batch / "raw" / "sessions").glob("*.md"))
        # Phased fan-out: one tier-2 batch, one tier-3 batch.
        assert len(batch_client.batches.submitted) == 2
        # The unique-target merge was batched, not synchronous.
        assert any(
            "## Existing page content" in m for m in _all_batch_messages(batch_client)
        )

    def test_multi_action_file_create_plus_merge_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One intake file producing BOTH a tier-3 create (WidgetMulti) and
        # a tier-3 merge (tier-1 match on Acme Corp) through each path.
        contents = ["WidgetMulti gadget built by Acme Corp.\n"]
        root_sync = _seed_root(tmp_path, "sync", contents, with_acme=True)
        root_batch = _seed_root(tmp_path, "batch", contents, with_acme=True)
        _clean_env(monkeypatch)
        _freeze_recorded_at(monkeypatch)

        sync_client = _FakeClient(_scripted_responder)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: sync_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_sync / "raw",
                wiki_root=root_sync / "wiki",
                knowledge_root=root_sync,
            )
            == 0
        )

        batch_client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: batch_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_batch / "raw",
                wiki_root=root_batch / "wiki",
                knowledge_root=root_batch,
                batch_mode=True,
            )
            == 0
        )

        assert _wiki_snapshot(root_batch) == _wiki_snapshot(root_sync)
        # Both actions of the one file went through the Batch API.
        msgs = _all_batch_messages(batch_client)
        assert any("## Entity to create" in m for m in msgs)
        assert any("## Existing page content" in m for m in msgs)

    def test_escalate_protocol_through_batch_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An ESCALATE: merge response through the batch transport must land
        # in the tier-4 escalation path exactly like the sync transport.
        def responder(params: dict[str, Any]) -> str:
            user_msg = params["messages"][0]["content"]
            if "## Existing page content" in user_msg:
                return "ESCALATE: principled conflict about Acme facts"
            return _scripted_responder(params)

        contents = ["Acme Corp conflicting update.\n"]
        root_sync = _seed_root(tmp_path, "sync", contents, with_acme=True)
        root_batch = _seed_root(tmp_path, "batch", contents, with_acme=True)
        _clean_env(monkeypatch)

        sync_client = _FakeClient(responder)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: sync_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_sync / "raw",
                wiki_root=root_sync / "wiki",
                knowledge_root=root_sync,
            )
            == 0
        )

        batch_client = _FakeClient(responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: batch_client)
        _patch_uids(monkeypatch)
        assert (
            run(
                raw_root=root_batch / "raw",
                wiki_root=root_batch / "wiki",
                knowledge_root=root_batch,
                batch_mode=True,
            )
            == 0
        )

        for root in (root_sync, root_batch):
            pending = root / "wiki" / "_pending_questions.md"
            assert pending.exists(), f"no escalation written under {root.name}"
            text = pending.read_text(encoding="utf-8")
            assert "acme corp" in text.lower()
            assert "principled conflict about Acme facts" in text
            # ESCALATE without a merged body leaves the page untouched and
            # consumes the raw file on both transports.
            page = (root / "wiki" / "acme1234-acme-corp.md").read_text(encoding="utf-8")
            assert "Original body line." in page
            assert "Merged note" not in page
            assert not list((root / "raw" / "sessions").glob("*.md"))


# ---------------------------------------------------------------------------
# Budget semantics (athenaeum#220) at batch-assembly time
# ---------------------------------------------------------------------------


class TestBatchSelfResolvingGuard:
    """Issue athenaeum#300 follow-up (athenaeum#304): the deterministic self-resolving-claim
    guard must fire on the batch transport too, not just the sync path —
    an opus-model Quine review of the initial athenaeum#304 PR found batch mode
    bypassed the guard entirely, the same bypass-class athenaeum#296 needed a
    post-filter to close.
    """

    def test_self_resolving_claim_flagged_before_batch_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = ["WidgetFoo is primary. Human-confirmed (Tristan, 2026-07-02).\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )

        tier2_prompts = [
            req["params"]["messages"][0]["content"]
            for batch in client.batches.submitted
            for req in batch
            if "Human-confirmed" in req["params"]["messages"][0]["content"]
        ]
        assert tier2_prompts, "expected the claim to reach a submitted tier2 request"
        assert "UNVERIFIED SELF-CLAIM" in tier2_prompts[0]
        assert tier2_prompts[0].index("UNVERIFIED SELF-CLAIM") < tier2_prompts[0].index(
            "Human-confirmed"
        )


class TestBatchBudget:
    def test_assembly_truncates_and_defers_via_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        contents = [f"Standalone fact about Widget{i} gadget.\n" for i in range(3)]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1,
            batch_mode=True,
        )
        assert rc == 0

        # File 0 admitted at assembly (1 tier-2 call); like the sync loop, an
        # admitted file completes its tier-3 work even past the cap. Files
        # 1-2 deferred at assembly — never submitted.
        assert [len(reqs) for reqs in client.batches.submitted] == [1, 1]
        manifest = root / "wiki" / "_deferred_work.md"
        assert manifest.exists()
        text = manifest.read_text(encoding="utf-8")
        assert "deferred_count: 2" in text
        assert "20240411T120000Z-aabbccd1.md" in text
        assert "20240412T120000Z-aabbccd2.md" in text
        assert "20240410T120000Z-aabbccd0.md" not in text
        # Deferred raw files stay on disk; the processed one is consumed.
        remaining = sorted(p.name for p in (root / "raw" / "sessions").glob("*.md"))
        assert remaining == [
            "20240411T120000Z-aabbccd1.md",
            "20240412T120000Z-aabbccd2.md",
        ]
        messages = [r.getMessage() for r in caplog.records]
        assert any("Done (DEGRADED — budget exhausted)" in m for m in messages)

    def test_zero_budget_defers_everything_without_submitting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = ["Standalone fact about WidgetSolo gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=0,
            batch_mode=True,
        )
        # Issue athenaeum#1135: a zero-budget run that submits and processes
        # nothing is EXACTLY the zero-progress DEGRADED REFUSAL (early-stop
        # reason + zero files committed) -- exits EXIT_LIBRARIAN_REFUSAL (3),
        # not the pre-athenaeum#1135 0.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        assert client.batches.submitted == []
        manifest = root / "wiki" / "_deferred_work.md"
        assert manifest.exists()
        assert "deferred_count: 1" in manifest.read_text(encoding="utf-8")
        assert list((root / "raw" / "sessions").glob("*.md"))


# ---------------------------------------------------------------------------
# Budget re-check at phase-2 assembly + finalize sync merges (QA blocker 1)
# ---------------------------------------------------------------------------


class TestPhase2BudgetGate:
    def _run(self, root: Path, cap: int) -> int:
        return run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=cap,
            batch_mode=True,
        )

    def test_phase2_assembly_recheck_defers_over_cap_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 3 files fit phase 1 (3 tier-2 calls <= cap 4) but their tier-3
        # creates would push to 6. The phase-2 re-check must defer files
        # once the cap is hit instead of bumping past it unbounded.
        contents = [f"Standalone fact about Widget{i} gadget.\n" for i in range(3)]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        assert self._run(root, cap=4) == 0
        # Phase 1: 3 tier-2 requests (calls 1-3). Phase 2: file 0's create
        # lands at call 4; files 1-2 are over-cap at assembly → deferred.
        assert [len(reqs) for reqs in client.batches.submitted] == [3, 1]
        names = " ".join(_wiki_snapshot(root))
        assert "widget0" in names
        assert "widget1" not in names and "widget2" not in names
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 2" in text
        assert "aabbccd1" in text and "aabbccd2" in text
        remaining = sorted(p.name for p in (root / "raw" / "sessions").glob("*.md"))
        assert remaining == [
            "20240411T120000Z-aabbccd1.md",
            "20240412T120000Z-aabbccd2.md",
        ]

    def test_phase2_overshoot_bounded_to_one_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cap exactly consumed by phase 1: the first phase-2 file still
        # proceeds (sync-path one-file overshoot semantics — an admitted
        # file completes), everything after it defers.
        contents = [f"Standalone fact about Widget{i} gadget.\n" for i in range(3)]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        assert self._run(root, cap=3) == 0
        # api_calls ends at 4 = cap + exactly one file's tier-3 spend.
        assert [len(reqs) for reqs in client.batches.submitted] == [3, 1]
        names = " ".join(_wiki_snapshot(root))
        assert "widget0" in names
        assert "widget1" not in names and "widget2" not in names
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 2" in text

    def test_finalize_sync_merges_gated_by_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two files merging into the SAME page go down the synchronous
        # serialized path at finalize; each is a live API call, so the cap
        # must gate them per file too.
        contents = ["Acme Corp update one.\n", "Acme Corp update two.\n"]
        root = _seed_root(tmp_path, "k", contents, with_acme=True)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder)  # sync allowed for merges
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        # Phase 1 uses 2 calls; the first sync merge lands at call 3 (cap),
        # so the second file must defer instead of running a 4th call.
        assert self._run(root, cap=3) == 0
        merge_calls = [
            c
            for c in client.sync_calls
            if "## Existing page content" in c["messages"][0]["content"]
        ]
        assert len(merge_calls) == 1
        page = (root / "wiki" / "acme1234-acme-corp.md").read_text(encoding="utf-8")
        assert "Merged note from sessions/20240410T120000Z-aabbccd0.md" in page
        assert "20240411T120000Z-aabbccd1.md" not in page
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 1" in text
        assert "aabbccd1" in text
        remaining = [p.name for p in (root / "raw" / "sessions").glob("*.md")]
        assert remaining == ["20240411T120000Z-aabbccd1.md"]


# ---------------------------------------------------------------------------
# Spend ceiling enforced at batch phase boundaries (issue athenaeum#483)
# ---------------------------------------------------------------------------


class TestBatchSpendCeiling:
    """The athenaeum#378 spend ceiling must halt a batch-mode run at a phase boundary.

    Before athenaeum#483 ``spend.ceiling_tripped`` was called only from the synchronous
    per-file loop, so a ``--batch-mode`` run — the exact path athenaeum#470's ``drain``
    forces — ran both tier batches to completion with ZERO dollar check.
    """

    def test_ceiling_blocks_tier3_submit_after_tier2_spend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A tiny per-run USD ceiling is NOT breached before tier-2 (no spend
        # yet), so tier-2 submits; the tier-2 batch's own cost then breaches
        # it, so the next phase (tier-3) must NOT submit and every file with a
        # pending create/merge defers instead of being half-written.
        contents = [f"Standalone fact about Widget{i} gadget.\n" for i in range(2)]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.000001")
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)
        caplog.set_level(logging.ERROR, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            batch_mode=True,
        )
        # Issue athenaeum#1135: zero files committed + an early-stop reason ==
        # the zero-progress DEGRADED REFUSAL, regardless of whether the trip
        # happened in the synchronous entity loop or (as here) batch.py's
        # own tier-2/tier-3 spend-ceiling checks.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        # Exactly ONE batch submitted (tier-2 classify); tier-3 was gated.
        assert len(client.batches.submitted) == 1
        # No entity page created — every tier-3 create was deferred, not written
        # (the only wiki/ file is the athenaeum#220 deferred-work manifest).
        assert "widget" not in " ".join(_wiki_snapshot(root)).lower()
        # Both files deferred; their raws stay on disk for the next run.
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 2" in text
        remaining = sorted(p.name for p in (root / "raw" / "sessions").glob("*.md"))
        assert remaining == [
            "20240410T120000Z-aabbccd0.md",
            "20240411T120000Z-aabbccd1.md",
        ]
        assert any(
            "Spend ceiling reached" in r.getMessage() and "tier-3" in r.getMessage()
            for r in caplog.records
        )

    def test_ceiling_before_tier2_submits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Spend already over the ceiling BEFORE tier-2 (e.g. the synchronous
        # auto-memory merge phase, which runs first, already breached it):
        # not even the tier-2 batch may be submitted. Forcing ``ceiling_tripped``
        # to read breached at the first boundary keeps this independent of the
        # exact token pricing exercised by the tier-3 test above.
        contents = ["Standalone fact about WidgetSolo gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)
        monkeypatch.setattr(
            "athenaeum.spend.ceiling_tripped",
            lambda *a, **k: "per-run API dollar ceiling reached (forced)",
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            batch_mode=True,
        )
        # Issue athenaeum#1135: same zero-progress DEGRADED REFUSAL as above.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        # Ceiling already breached → NO batch submitted at all.
        assert client.batches.submitted == []
        assert "widget" not in " ".join(_wiki_snapshot(root)).lower()
        text = (root / "wiki" / "_deferred_work.md").read_text(encoding="utf-8")
        assert "deferred_count: 1" in text
        assert list((root / "raw" / "sessions").glob("*.md"))


# ---------------------------------------------------------------------------
# Non-transient batch errors → BatchExecutionError → per-file failure path
# (QA blocker 2)
# ---------------------------------------------------------------------------


class TestNonTransientBatchErrors:
    def test_execute_batch_wraps_non_transient_submit_error(self) -> None:
        client = _FakeClient(
            lambda params: "ok",
            allow_sync=False,
            create_error=RuntimeError("400 invalid_request: bad params"),
        )
        with pytest.raises(BatchExecutionError):
            execute_batch(
                client, _one_request(), description="test", sleep=lambda s: None
            )

    def test_whole_batch_400_maps_to_per_file_failure_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # batches.create raising a 400-style (non-transient) error must not
        # crash the run with a traceback: every admitted file lands in the
        # failure accounting, the summary renders, and the run exits 1.
        contents = [
            "Standalone fact about WidgetZero gadget.\n",
            "Standalone fact about WidgetOne gadget.\n",
        ]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(
            _scripted_responder,
            allow_sync=False,
            create_error=RuntimeError("400 invalid_request: bad params"),
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 1
        # Raw files stay on disk for next-run pickup; nothing was written.
        remaining = sorted(p.name for p in (root / "raw" / "sessions").glob("*.md"))
        assert remaining == [
            "20240410T120000Z-aabbccd0.md",
            "20240411T120000Z-aabbccd1.md",
        ]
        assert _wiki_snapshot(root) == {}
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Failed files (will retry next run)" in m
            and "aabbccd0" in m
            and "aabbccd1" in m
            for m in messages
        )
        assert any(m.startswith("Done: 0 created") for m in messages)


# ---------------------------------------------------------------------------
# Per-result failure handling → existing per-file failure path
# ---------------------------------------------------------------------------


class TestBatchFailures:
    def test_errored_tier2_result_marks_file_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        contents = [
            "Standalone fact about WidgetZero gadget.\n",
            "BROKENMARKER fact about WidgetOne gadget.\n",
            "Standalone fact about WidgetTwo gadget.\n",
        ]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(
            _scripted_responder, allow_sync=False, fail_marker="BROKENMARKER"
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        # Failure accounting matches the sync path: failed files → exit 1.
        assert rc == 1

        # The failed file stays on disk for next-run pickup; others consumed.
        remaining = [p.name for p in (root / "raw" / "sessions").glob("*.md")]
        assert remaining == ["20240411T120000Z-aabbccd1.md"]
        names = " ".join(_wiki_snapshot(root))
        assert "widgetzero" in names
        assert "widgettwo" in names
        assert "widgetone" not in names
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Failed files (will retry next run)" in m
            and "20240411T120000Z-aabbccd1.md" in m
            for m in messages
        )

    def test_errored_tier3_result_fails_file_without_partial_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The responder plants FAILCREATE in WidgetBad's tier-2 observations,
        # so only its tier-3 create request carries the failure marker.
        contents = [
            "Standalone fact about WidgetGood gadget.\n",
            "Standalone fact about WidgetBad gadget.\n",
        ]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(
            _scripted_responder, allow_sync=False, fail_marker="FAILCREATE"
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 1
        names = " ".join(_wiki_snapshot(root))
        assert "widgetgood" in names
        assert "widgetbad" not in names
        remaining = [p.name for p in (root / "raw" / "sessions").glob("*.md")]
        assert remaining == ["20240411T120000Z-aabbccd1.md"]


# ---------------------------------------------------------------------------
# Same-page merge grouping: serialized synchronous fallback
# ---------------------------------------------------------------------------


class TestSamePageMerges:
    def test_same_target_merges_stay_synchronous_and_serialized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = [
            "Acme Corp update one.\n",
            "Acme Corp update two.\n",
        ]
        root = _seed_root(tmp_path, "k", contents, with_acme=True)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder)  # sync allowed for merges
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 0

        # No merge prompt went through the Batch API.
        assert not any(
            "## Existing page content" in m for m in _all_batch_messages(client)
        )
        # Both merges ran synchronously.
        merge_calls = [
            c
            for c in client.sync_calls
            if "## Existing page content" in c["messages"][0]["content"]
        ]
        assert len(merge_calls) == 2
        # Serialized: the second merge saw the first merge's output.
        page = (root / "wiki" / "acme1234-acme-corp.md").read_text(encoding="utf-8")
        ref0 = "Merged note from sessions/20240410T120000Z-aabbccd0.md"
        ref1 = "Merged note from sessions/20240411T120000Z-aabbccd1.md"
        assert ref0 in page and ref1 in page
        assert page.index(ref0) < page.index(ref1)


# ---------------------------------------------------------------------------
# Documented dedup divergence: tier 0/1 runs up front for the whole window
# ---------------------------------------------------------------------------


class TestBatchDedupDivergence:
    def test_same_new_entity_in_two_files_creates_duplicate_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Documented divergence from the sync loop (batch.py module
        # docstring): tier 0/1 runs for the whole intake window BEFORE any
        # creation, so an entity created from file A this run is not
        # tier-1-matchable by file B — batch mode creates duplicate pages
        # with distinct uids where the sync loop would merge. Pinned here
        # so a future change to this contract is deliberate.
        contents = [
            "Standalone fact about WidgetTwin gadget.\n",
            "More notes about WidgetTwin gadget.\n",
        ]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 0
        twin_pages = sorted(n for n in _wiki_snapshot(root) if "widgettwin" in n)
        assert twin_pages == [
            "uid00001-widgettwin.md",
            "uid00002-widgettwin.md",
        ]


# ---------------------------------------------------------------------------
# Mid-assembly failure: drop the file's already-appended requests
# ---------------------------------------------------------------------------


class TestMidAssemblyFailure:
    def test_failed_file_requests_dropped_before_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # File 1 assembles its tier-3 create, then blows up assembling its
        # merge. Its already-appended create request must be dropped before
        # submit — the file can never be written, so submitting its
        # requests would be pure wasted spend.
        contents = [
            "Standalone fact about WidgetFine gadget.\n",
            "WidgetOops gadget built by Acme Corp.\n",
        ]
        root = _seed_root(tmp_path, "k", contents, with_acme=True)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        import athenaeum.batch as batch_mod

        real_merge_params = batch_mod.tier3_merge_params

        def boom(action: Any, existing_body: str, source_ref: str, **kw: Any) -> Any:
            if "aabbccd1" in source_ref:
                raise RuntimeError("merge params exploded")
            return real_merge_params(action, existing_body, source_ref, **kw)

        monkeypatch.setattr(batch_mod, "tier3_merge_params", boom)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 1
        # Tier-3 batch carries only WidgetFine's create; WidgetOops's
        # appended-then-dropped create never reached the API.
        t3_msgs = [
            req["params"]["messages"][0]["content"]
            for req in client.batches.submitted[1]
        ]
        assert len(t3_msgs) == 1
        assert "WidgetFine" in t3_msgs[0]
        names = " ".join(_wiki_snapshot(root))
        assert "widgetfine" in names
        assert "widgetoops" not in names
        remaining = [p.name for p in (root / "raw" / "sessions").glob("*.md")]
        assert remaining == ["20240411T120000Z-aabbccd1.md"]


# ---------------------------------------------------------------------------
# Usage accounting + 50% batch discount
# ---------------------------------------------------------------------------


class TestBatchUsageAccounting:
    def test_add_batch_tokens_folds_counters_without_api_call(self) -> None:
        usage = TokenUsage()
        usage.add_batch_tokens(1000, 500, 200, 300)
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cache_creation_input_tokens == 200
        assert usage.cache_read_input_tokens == 300
        assert usage.api_calls == 0
        assert usage.batch_input_tokens == 1000
        assert usage.batch_output_tokens == 500

    def test_batch_tokens_bill_at_half_the_sync_rate(self) -> None:
        sync_usage = TokenUsage()
        sync_usage.add_tokens(1000, 500, 200, 300)
        batch_usage = TokenUsage()
        batch_usage.add_batch_tokens(1000, 500, 200, 300)
        assert batch_usage.estimated_cost_usd == pytest.approx(
            sync_usage.estimated_cost_usd * 0.5
        )

    def test_mixed_sync_and_batch_costs_compose(self) -> None:
        mixed = TokenUsage()
        mixed.add(1000, 500)  # sync call
        mixed.add_batch_tokens(1000, 500)  # batch result
        sync_only = TokenUsage()
        sync_only.add(1000, 500)
        assert mixed.estimated_cost_usd == pytest.approx(
            sync_only.estimated_cost_usd * 1.5
        )
        assert mixed.api_calls == 1
        assert mixed.total_tokens == 3000

    def test_execute_batch_records_usage_per_succeeded_result(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        usage = TokenUsage()
        out = execute_batch(
            client,
            [
                BatchRequest(
                    custom_id="a",
                    params={
                        "model": "m",
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "x"}],
                    },
                )
            ],
            description="test",
            usage=usage,
            sleep=lambda s: None,
        )
        assert out.results["a"].content[0].text == "ok"
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.batch_input_tokens == 100
        assert usage.batch_output_tokens == 50
        # Attempts are counted at assembly time by the caller, not here.
        assert usage.api_calls == 0

    def test_execute_batch_threads_knob_to_every_result(self) -> None:
        """athenaeum#781: execute_batch's ``knob=`` kwarg tags the WHOLE batch (every
        request in one submit shares the same knob -- tier2_classify and
        tier3_write batches are each submitted in their own execute_batch
        call, see process_batch_run)."""
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        usage = TokenUsage()
        execute_batch(
            client,
            [
                BatchRequest(
                    custom_id="a",
                    params={
                        "model": "m",
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "x"}],
                    },
                )
            ],
            description="test",
            usage=usage,
            knob="classify",
            sleep=lambda s: None,
        )
        assert usage.per_knob["classify"]["input_tokens"] == 100
        assert usage.per_knob["classify"]["batch_input_tokens"] == 100


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def _one_request() -> list[BatchRequest]:
    return [
        BatchRequest(
            custom_id="a",
            params={
                "model": "m",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "x"}],
            },
        )
    ]


class TestBatchPolling:
    def test_polls_until_ended_with_module_interval(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False, polls_until_end=3)
        sleeps: list[float] = []
        out = execute_batch(
            client, _one_request(), description="test", sleep=sleeps.append
        )
        assert sleeps == [BATCH_POLL_INTERVAL_SECONDS] * 3
        assert out.results["a"].content[0].text == "ok"
        # athenaeum#1144: a batch that ENDED is not in flight, and the outcome
        # carries the batch id either way.
        assert out.in_flight is False
        assert out.batch_id == "msgbatch_1"

    def test_timeout_cancels_and_raises(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False, never_end=True)
        with pytest.raises(BatchExecutionError):
            execute_batch(
                client,
                _one_request(),
                description="test",
                sleep=lambda s: None,
                timeout=BATCH_POLL_INTERVAL_SECONDS * 2.5,
            )
        assert client.batches.cancelled == ["msgbatch_1"]

    def test_empty_request_list_submits_nothing(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        empty = execute_batch(client, [], description="test")
        assert empty.results == {}
        assert empty.in_flight is False
        assert empty.batch_id == ""
        assert client.batches.submitted == []


# ---------------------------------------------------------------------------
# Dry-run guard
# ---------------------------------------------------------------------------


class TestBatchDryRun:
    def test_dry_run_makes_no_batch_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = ["Standalone fact about WidgetDry gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            batch_mode=True,
        )
        assert rc == 0
        assert client.batches.submitted == []
        assert client.sync_calls == []


# ---------------------------------------------------------------------------
# Issue athenaeum#476 — a Tier-2 batch result truncated at max_tokens is retried
# SYNCHRONOUSLY with a larger budget at finalize (closing athenaeum#472's sync-only
# gap), so an entity-dense file recovers instead of silently degrading.
# ---------------------------------------------------------------------------


class TestBatchTruncationRetry:
    def test_truncated_batch_result_retried_with_larger_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The raw content carries TRUNCATEME → the fake batch returns the tier2
        # classification TRUNCATED (stop_reason=max_tokens). The finalize path
        # must retry that one file synchronously with a bigger budget, which
        # the scripted responder answers cleanly with the WidgetTrunc entity.
        contents = ["Standalone fact about WidgetTrunc gadget. TRUNCATEME\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        # sync allowed: the truncation retry is a live messages.create call.
        client = _FakeClient(_scripted_responder, truncate_marker="TRUNCATEME")
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )
        assert rc == 0
        # Tier-2 went through the Batch API (first, truncated, attempt).
        assert client.batches.submitted
        # A synchronous classify retry fired with a LARGER budget than the
        # batch attempt used — the athenaeum#476 fix, on the batch path.
        batch_t2_budget = client.batches.submitted[0][0]["params"]["max_tokens"]
        classify_retries = [
            c
            for c in client.sync_calls
            if c["model"] == DEFAULT_CLASSIFY_MODEL
            and c["max_tokens"] > batch_t2_budget
        ]
        assert classify_retries, "expected a bigger-budget classify retry"
        # And the file RECOVERED: the WidgetTrunc page was written, not dropped.
        pages = list((root / "wiki").glob("*.md"))
        assert any("WidgetTrunc" in p.read_text(encoding="utf-8") for p in pages)


# ---------------------------------------------------------------------------
# Issue athenaeum#1144 — the run deadline threaded into the batch poll: a BOUNDED
# WAIT that spills to a handle instead of cancelling.
#
# The failure this closes: ``execute_batch`` polled against the module's 24h
# ``BATCH_POLL_TIMEOUT_SECONDS`` inside a bounded nightly window, and on
# timeout it CANCELLED the batch -- destroying work that is already paid for
# server-side. With a deadline the poll stops at the earlier of batch-end or
# the remaining run budget; if the deadline wins, the batch is left running,
# an athenaeum#1143 handle is recorded over its raw files, and those refs come back
# as ``in_flight`` -- neither ``failed`` (which would re-bill them) nor
# ``deferred`` (which would claim nothing was submitted).
#
# AC6: every test here drives the poll through the injectable ``sleep``. None
# waits on real time.
# ---------------------------------------------------------------------------


class TestBatchDeadlineSpill:
    def test_deadline_spill_leaves_batch_running_and_reports_in_flight(self) -> None:
        """AC2 + AC3: the poll stops at the deadline; no cancel, no raise."""
        client = _FakeClient(_scripted_responder, allow_sync=False, never_end=True)
        sleeps: list[float] = []

        out = execute_batch(
            client,
            _one_request(),
            description="test",
            sleep=sleeps.append,
            # Budget of ~70s against a 30s poll cadence: waited walks
            # 0 -> 30 -> 60 -> 90 and spills on the 90 pass. Driven entirely
            # by the injected sleep -- no real time passes.
            deadline=time.monotonic() + 70.0,
        )

        assert out.in_flight is True
        assert out.batch_id == "msgbatch_1"
        assert out.results == {}
        # AC3, the whole point: the batch is committed server-side and is the
        # artifact the handle exists to preserve.
        assert client.batches.cancelled == []
        assert sleeps == [BATCH_POLL_INTERVAL_SECONDS] * 3

    def test_already_expired_deadline_spills_without_polling(self) -> None:
        """A deadline already in the past spills on the first pass."""
        client = _FakeClient(_scripted_responder, allow_sync=False, never_end=True)
        sleeps: list[float] = []

        out = execute_batch(
            client,
            _one_request(),
            description="test",
            sleep=sleeps.append,
            deadline=time.monotonic() - 5.0,
        )

        assert out.in_flight is True
        assert sleeps == []
        assert client.batches.cancelled == []

    def test_batch_ending_inside_the_window_is_collected_unchanged(self) -> None:
        """AC2: a batch that ends in time continues synchronously, as today."""
        client = _FakeClient(lambda params: "ok", allow_sync=False, polls_until_end=3)
        sleeps: list[float] = []

        out = execute_batch(
            client,
            _one_request(),
            description="test",
            sleep=sleeps.append,
            deadline=time.monotonic() + 10_000.0,
        )

        assert out.in_flight is False
        assert out.results["a"].content[0].text == "ok"
        assert sleeps == [BATCH_POLL_INTERVAL_SECONDS] * 3

    def test_no_deadline_preserves_the_timeout_cancel_path(self) -> None:
        """AC2: ``timeout`` semantics are untouched when no deadline is given.

        The complement of the spill: with no deadline, a batch that never ends
        still hits the module timeout, is cancelled best-effort, and raises --
        exactly the pre-athenaeum#1144 behaviour.
        """
        client = _FakeClient(lambda params: "ok", allow_sync=False, never_end=True)

        with pytest.raises(BatchExecutionError):
            execute_batch(
                client,
                _one_request(),
                description="test",
                sleep=lambda s: None,
                timeout=BATCH_POLL_INTERVAL_SECONDS * 2.5,
                deadline=None,
            )

        assert client.batches.cancelled == ["msgbatch_1"]

    def test_deadline_beats_a_later_timeout(self) -> None:
        """Both bounds armed: the EARLIER one wins, and it is the deadline."""
        client = _FakeClient(lambda params: "ok", allow_sync=False, never_end=True)

        out = execute_batch(
            client,
            _one_request(),
            description="test",
            sleep=lambda s: None,
            timeout=10_000.0,
            deadline=time.monotonic() + 40.0,
        )

        assert out.in_flight is True
        assert client.batches.cancelled == []


class TestBatchAssemblyLimits:
    """AC7: a batch past the documented API limits is refused locally."""

    def test_request_count_over_the_limit_is_refused_before_submit(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        requests = [
            BatchRequest(
                custom_id=f"c{i}",
                params={
                    "model": "m",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "x"}],
                },
            )
            for i in range(BATCH_MAX_REQUESTS + 1)
        ]

        with pytest.raises(BatchExecutionError) as exc:
            execute_batch(client, requests, description="huge", sleep=lambda s: None)

        assert str(BATCH_MAX_REQUESTS) in str(exc.value)
        # Refused at ASSEMBLY: nothing reached the API, so there is no 400 to
        # interpret and no server-side cost.
        assert client.batches.submitted == []

    def test_payload_bytes_over_the_limit_are_refused_before_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the byte cap rather than building a 256 MB payload in a unit
        # test -- the predicate under test is the comparison, not the constant.
        monkeypatch.setattr("athenaeum.batch.BATCH_MAX_PAYLOAD_BYTES", 512)
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        requests = [
            BatchRequest(
                custom_id="big",
                params={
                    "model": "m",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "x" * 2048}],
                },
            )
        ]

        with pytest.raises(BatchExecutionError) as exc:
            execute_batch(client, requests, description="fat", sleep=lambda s: None)

        assert "exceeding the Batch API limit" in str(exc.value)
        assert client.batches.submitted == []

    def test_a_batch_inside_the_limits_submits_normally(self) -> None:
        client = _FakeClient(lambda params: "ok", allow_sync=False)
        out = execute_batch(client, _one_request(), description="fine", sleep=lambda s: None)
        assert out.in_flight is False
        assert len(client.batches.submitted) == 1


class TestProcessBatchRunDeadlineSpill:
    """AC4 + AC5: the spill persists a handle and books in-flight refs."""

    def test_tier2_spill_records_a_classify_handle_and_keeps_raw_on_disk(
        self, tmp_path: Path
    ) -> None:
        contents = ["Standalone fact about WidgetSpill gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        raw_files = discover_raw_files(root / "raw")
        index = EntityIndex(root / "wiki")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        client = _FakeClient(_scripted_responder, allow_sync=False, never_end=True)

        result = process_batch_run(
            raw_files,
            index,
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            deadline=time.monotonic() - 1.0,
            cache_dir=cache_dir,
        )

        ref = raw_files[0].ref
        # AC5: in-flight is its OWN bucket. Not failed (that would re-bill the
        # same classify call next run), not deferred (that would claim nothing
        # was submitted).
        assert result.in_flight_refs == [ref]
        assert result.failed_refs == []
        assert result.deferred_refs == []
        assert result.created == 0
        assert result.in_flight_batch_ids == ["msgbatch_1"]

        # AC4: the athenaeum#1143 handle is on disk with the batch id, the knob, and
        # the ref map -- everything a later run needs to collect it.
        handles = batch_state.load(cache_dir)
        assert list(handles) == ["msgbatch_1"]
        handle = handles["msgbatch_1"]
        assert handle.knob == "classify"
        assert [rec.ref for rec in handle.refs.values()] == [ref]

        # Nothing was written and the raw file is untouched -- it is what the
        # later collect applies the result TO.
        assert raw_files[0].path.exists()
        assert not list((root / "wiki").glob("*widget*"))
        # And the second phase never ran: only the tier-2 batch was submitted.
        assert len(client.batches.submitted) == 1

    def test_tier3_spill_records_a_write_handle(self, tmp_path: Path) -> None:
        """The tier-3 batch spills on its own: classify landed, write did not."""
        contents = ["Standalone fact about WidgetWrite gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        raw_files = discover_raw_files(root / "raw")
        index = EntityIndex(root / "wiki")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        # Two clients so the phases can end differently: classify ends at
        # create, write never does.
        classify_client = _FakeClient(_scripted_responder, allow_sync=False)
        write_client = _FakeClient(_scripted_responder, allow_sync=False, never_end=True)

        result = process_batch_run(
            raw_files,
            index,
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            deadline=time.monotonic() - 1.0,
            cache_dir=cache_dir,
        )

        assert result.in_flight_refs == [raw_files[0].ref]
        assert result.created == 0
        assert raw_files[0].path.exists()

        handles = batch_state.load(cache_dir)
        assert [h.knob for h in handles.values()] == ["write"]
        # The tier-2 batch DID complete and was collected -- only the write
        # batch spilled.
        assert len(classify_client.batches.submitted) == 1
        assert len(write_client.batches.submitted) == 1

    def test_no_deadline_preserves_todays_behaviour_exactly(self, tmp_path: Path) -> None:
        """AC1: ``deadline=None`` records no handle and books no in-flight refs."""
        contents = ["Standalone fact about WidgetPlain gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        raw_files = discover_raw_files(root / "raw")
        index = EntityIndex(root / "wiki")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        client = _FakeClient(_scripted_responder, allow_sync=False)

        result = process_batch_run(
            raw_files,
            index,
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            cache_dir=cache_dir,
        )

        assert result.created == 1
        assert result.in_flight_refs == []
        assert result.in_flight_batch_ids == []
        assert batch_state.load(cache_dir) == {}
        assert not raw_files[0].path.exists()


class TestLibrarianDeadlineThreading:
    """AC1, AC5, AC8 at the ``run()`` seam."""

    def _spilling_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[dict[str, Any], dict[str, Any], Path, int]:
        contents = ["Standalone fact about WidgetThread gadget.\n"]
        root = _seed_root(tmp_path, "k", contents)
        _clean_env(monkeypatch)
        client = _FakeClient(_scripted_responder, allow_sync=False)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
        _patch_uids(monkeypatch)

        captured: dict[str, Any] = {}
        raw_path = next((root / "raw" / "sessions").glob("*.md"))

        def fake_process_batch_run(*args: Any, **kwargs: Any) -> BatchRunResult:
            captured.update(kwargs)
            captured["raw_files"] = list(args[0])
            return BatchRunResult(in_flight_refs=[args[0][0].ref])

        monkeypatch.setattr("athenaeum.batch.process_batch_run", fake_process_batch_run)

        stats: dict[str, Any] = {}
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
            out_run_stats=stats,
        )
        return captured, stats, raw_path, rc

    def test_batch_branch_passes_a_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1: the librarian's batch branch threads its wall-clock deadline."""
        captured, _stats, _raw, _rc = self._spilling_run(tmp_path, monkeypatch)

        assert "deadline" in captured
        # A real run always arms an internal deadline (athenaeum#396's default), so
        # the batch poll is bounded rather than falling through to the 24h
        # module constant.
        assert isinstance(captured["deadline"], float)
        assert captured["deadline"] > time.monotonic()

    def test_in_flight_refs_are_reported_and_the_raw_file_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5: in-flight refs reach the run stats; AC8: nothing is consumed."""
        _captured, stats, raw_path, _rc = self._spilling_run(tmp_path, monkeypatch)

        assert stats["in_flight_refs"], "expected the spilled refs on the run stats"
        assert stats["deferred_refs"] == []
        assert stats["failed_files"] == []
        assert raw_path.exists()

    def test_spilled_run_reports_a_distinguishable_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC8: a spill is not a healthy zero-compile run, and says so.

        A run that compiled nothing because its batch is still in flight
        renders ``reason=batch-in-flight`` with an ``in_flight=`` count --
        distinct from the ``reason=completed`` a genuinely idle run renders,
        and distinct from the ``budget``/``deadline`` early-stop vocabulary
        the athenaeum#1135 zero-progress refusal keys on.
        """
        caplog.set_level(logging.INFO, logger="athenaeum")
        _captured, _stats, _raw, rc = self._spilling_run(tmp_path, monkeypatch)

        summary = [
            rec.getMessage()
            for rec in caplog.records
            if rec.getMessage().startswith("librarian-run-summary")
        ]
        assert summary, "expected a run-summary line"
        assert "reason=batch-in-flight" in summary[-1]
        assert "in_flight=1" in summary[-1]
        # Deliberately OUTSIDE the early-stop vocabulary: work in flight is
        # progress, so the zero-progress refusal must not fire.
        assert rc != EXIT_LIBRARIAN_REFUSAL

    def test_zero_yield_alarm_does_not_fire_on_a_spilled_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC8: spent calls whose results are still coming are not wasted."""
        _captured, stats, _raw, _rc = self._spilling_run(tmp_path, monkeypatch)

        assert stats.get("zero_yield") is not True


# ---------------------------------------------------------------------------
# Issue athenaeum#1145 — the collect-only adoption path.
#
# A run whose only work is collecting a PRIOR run's batch is a valid and
# useful run. Every test here seeds the handle store the way production does:
# by actually spilling a batch through athenaeum#1144's deadline path, then
# flipping the fake batch to `ended` and collecting it. That round trip is the
# point — a handle written by the submit side has to be readable by the
# collect side, and a test that hand-writes the store would not prove it.
# ---------------------------------------------------------------------------


def _index_names(index: EntityIndex) -> list[str]:
    """Every name/alias key the index currently holds."""
    return [key for key, _value in index.items()]


def _write_extra_raw(root: Path, content: str) -> Path:
    """Drop one more raw intake file into an already-seeded root."""
    sessions = root / "raw" / "sessions"
    existing = sorted(sessions.glob("*.md"))
    n = len(existing)
    path = sessions / f"2026-01-0{n + 2}T00-00-00Z-extra{n:03d}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _spill_a_classify_batch(
    tmp_path: Path,
    name: str,
    contents: list[str],
    *,
    with_acme: bool = False,
) -> tuple[Path, Path, _FakeClient, TokenUsage, list[Any]]:
    """Submit a tier-2 batch and spill it at the deadline. Returns the pieces."""
    root = _seed_root(tmp_path, name, contents, with_acme=with_acme)
    cache_dir = tmp_path / f"{name}-cache"
    cache_dir.mkdir()
    client = _FakeClient(_scripted_responder, allow_sync=False, never_end=True)
    raw_files = discover_raw_files(root / "raw")
    usage = TokenUsage()

    spilled = process_batch_run(
        raw_files,
        EntityIndex(root / "wiki"),
        root / "wiki",
        client,
        FALLBACK_TYPES,
        FALLBACK_TAGS,
        FALLBACK_ACCESS,
        usage=usage,
        config=None,
        max_api_calls=100,
        sleep=lambda s: None,
        deadline=time.monotonic() - 1.0,
        cache_dir=cache_dir,
    )
    assert spilled.in_flight_refs, "expected the submit to spill to a handle"
    # The batch is "still running" as far as the store is concerned; flip the
    # fake so the next retrieve reports it ended.
    client.batches._never_end = False
    return root, cache_dir, client, usage, raw_files


class TestCollectPendingBatches:
    def test_collect_applies_a_classify_handle_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 + AC3 + AC7: results land through finalize; handle retires."""
        _patch_uids(monkeypatch)
        root, cache_dir, client, usage, raw_files = _spill_a_classify_batch(
            tmp_path, "c1", ["Standalone fact about WidgetCollect gadget.\n"]
        )
        index = EntityIndex(root / "wiki")

        out = collect_pending_batches(
            index,
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        # AC7: the collected tier-2 handle pipelined straight into a NEW tier-3
        # batch in this same run, and that batch was collected too.
        assert out.created == 1
        assert out.collected_refs == [raw_files[0].ref]
        assert out.failed_refs == []
        # AC2: applied through the normal finalize path — the page exists and
        # the raw file was consumed, exactly as a within-run batch would.
        assert list((root / "wiki").glob("*widgetcollect*"))
        assert not raw_files[0].path.exists()
        # AC3: the handle is retired and its lease with it, in one store write.
        assert batch_state.load(cache_dir) == {}
        assert out.retired_handles and out.kept_handles == []

    def test_collect_books_usage_with_the_handles_knob_and_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5: token usage lands via add_batch_tokens, per knob AND per model.

        The submitting run's request payloads are gone by collect time, so the
        athenaeum#247 per-model attribution can only come from the handle — which
        is why athenaeum#1145 records each request's model on its ref record.
        """
        _patch_uids(monkeypatch)
        root, cache_dir, client, _submit_usage, _raws = _spill_a_classify_batch(
            tmp_path, "c2", ["Standalone fact about WidgetUsage gadget.\n"]
        )
        handle = next(iter(batch_state.load(cache_dir).values()))
        assert handle.knob == "classify"
        assert [r.model for r in handle.refs.values()] == [DEFAULT_CLASSIFY_MODEL]

        usage = TokenUsage()
        collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        # Booked at the batch (50%-discounted) counters, not the sync ones.
        assert usage.batch_input_tokens > 0
        assert usage.per_knob["classify"]["batch_input_tokens"] > 0
        assert usage.per_model[DEFAULT_CLASSIFY_MODEL]["batch_input_tokens"] > 0

    def test_collect_applies_a_write_handle_from_its_stored_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tier-3 spill collects too — its application context rides the handle.

        The per-request context for a tier-3 batch (which entity to create,
        which page to merge into, the body the merge ops were anchored
        against) exists nowhere but the handle once the submitting run exits.
        """
        _patch_uids(monkeypatch)
        root = _seed_root(
            tmp_path, "c3", ["Standalone fact about WidgetWriteCollect gadget.\n"]
        )
        cache_dir = tmp_path / "c3-cache"
        cache_dir.mkdir()
        classify_client = _FakeClient(_scripted_responder, allow_sync=False)
        write_client = _FakeClient(
            _scripted_responder, allow_sync=False, never_end=True
        )
        raw_files = discover_raw_files(root / "raw")
        usage = TokenUsage()

        spilled = process_batch_run(
            raw_files,
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            deadline=time.monotonic() - 1.0,
            cache_dir=cache_dir,
        )
        assert spilled.in_flight_refs == [raw_files[0].ref]
        handle = next(iter(batch_state.load(cache_dir).values()))
        assert handle.knob == "write"
        assert handle.work is not None and handle.work["files"]

        write_client.batches._never_end = False
        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        assert out.created == 1
        assert out.collected_refs == [raw_files[0].ref]
        assert list((root / "wiki").glob("*widgetwritecollect*"))
        assert not raw_files[0].path.exists()
        assert batch_state.load(cache_dir) == {}

    def test_collected_merge_updates_the_existing_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spilled tier-3 MERGE applies its anchored ops on collect."""
        _patch_uids(monkeypatch)
        root = _seed_root(
            tmp_path,
            "c4",
            ["Acme Corp shipped a thing.\n"],
            with_acme=True,
        )
        cache_dir = tmp_path / "c4-cache"
        cache_dir.mkdir()
        classify_client = _FakeClient(_scripted_responder, allow_sync=False)
        write_client = _FakeClient(
            _scripted_responder, allow_sync=False, never_end=True
        )
        raw_files = discover_raw_files(root / "raw")
        usage = TokenUsage()
        page = root / "wiki" / "acme1234-acme-corp.md"

        process_batch_run(
            raw_files,
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            deadline=time.monotonic() - 1.0,
            cache_dir=cache_dir,
        )
        handle = next(iter(batch_state.load(cache_dir).values()))
        merges = next(iter(handle.work["files"].values()))["merges"]
        assert merges, "expected a batched merge in the spilled handle"
        assert "Original body line." in next(iter(merges.values()))["existing_body"]

        write_client.batches._never_end = False
        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        assert out.updated == 1
        body = page.read_text(encoding="utf-8")
        assert "Original body line." in body
        assert "Merged note from" in body
        assert batch_state.load(cache_dir) == {}

    def test_a_batch_that_has_not_ended_keeps_its_handle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Still in flight: keep the handle, keep the lease, do NOT resubmit."""
        _patch_uids(monkeypatch)
        root, cache_dir, client, usage, raw_files = _spill_a_classify_batch(
            tmp_path, "c5", ["Standalone fact about WidgetPending gadget.\n"]
        )
        client.batches._never_end = True  # still running at collect time
        submitted_before = len(client.batches.submitted)

        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        assert out.kept_handles and out.retired_handles == []
        assert out.in_flight_refs == [raw_files[0].ref]
        assert out.created == 0
        # Nothing was resubmitted — the work is already paid for.
        assert len(client.batches.submitted) == submitted_before
        assert list(batch_state.load(cache_dir)) == out.kept_handles
        assert raw_files[0].path.exists()

    def test_a_leased_raw_file_that_vanished_is_dropped_not_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A result whose raw file is gone cannot finalize, so it is dropped."""
        _patch_uids(monkeypatch)
        root, cache_dir, client, usage, raw_files = _spill_a_classify_batch(
            tmp_path, "c6", ["Standalone fact about WidgetGone gadget.\n"]
        )
        raw_files[0].path.unlink()

        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        assert out.created == 0
        assert out.failed_refs == [raw_files[0].ref]
        # The handle still retires — nothing is left holding a lease over a
        # file that no longer exists.
        assert batch_state.load(cache_dir) == {}

    def test_no_handles_is_a_no_op(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "empty-cache"
        cache_dir.mkdir()
        root = _seed_root(tmp_path, "c7", [])
        client = _FakeClient(_scripted_responder, allow_sync=False)

        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            cache_dir=cache_dir,
        )

        assert out.collected_refs == []
        assert client.batches.submitted == []


class TestCollectOrderingAndIndexFreshness:
    def test_collected_creations_are_tier1_matchable_by_the_next_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC6: a collected tier-2 handle's creations are in the index first.

        Collect runs before the claim loop, so a fresh ``EntityIndex`` built
        for the new cohort reads the pages the collect just wrote — which is
        what lets this run's ``tier1_programmatic_match`` match an entity this
        run created, rather than deferring it a further run.
        """
        _patch_uids(monkeypatch)
        root, cache_dir, client, usage, _raws = _spill_a_classify_batch(
            tmp_path, "o1", ["Standalone fact about WidgetFresh gadget.\n"]
        )
        collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=usage,
            config=None,
            max_api_calls=100,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        # The index the claim loop would build next sees the created page...
        fresh_index = EntityIndex(root / "wiki")
        assert "widgetfresh" in _index_names(fresh_index)
        # ...and a newly-claimed file naming it tier-1 matches, rather than
        # paying for a tier-2 classify to rediscover it.
        later = _seed_root(tmp_path, "o1-later", ["More on WidgetFresh gadget.\n"])
        later_raw = discover_raw_files(later / "raw")[0]
        matched = tier1_programmatic_match(later_raw, fresh_index, config=None)
        assert [name for name, _uid, _path in matched] == ["widgetfresh"]

    def test_collect_precedes_the_claim_loop_and_any_new_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1: asserted by CALL SEQUENCE, not by reading the code.

        The three events must occur in this order: the collect's ``retrieve``
        of the outstanding batch, the claim loop's ``discover_raw_files``, and
        the new cohort's ``batches.create``.
        """
        _patch_uids(monkeypatch)
        root, cache_dir, client, _usage, _raws = _spill_a_classify_batch(
            tmp_path, "o2", ["Standalone fact about WidgetOrder gadget.\n"]
        )
        # A second, unleased file for this run to claim and submit.
        _write_extra_raw(root, "Standalone fact about WidgetSecond gadget.\n")

        sequence: list[str] = []
        real_retrieve = client.batches.retrieve
        real_create = client.batches.create

        def traced_retrieve(batch_id: str) -> Any:
            sequence.append("collect-retrieve")
            return real_retrieve(batch_id)

        def traced_create(*, requests: list[dict[str, Any]]) -> Any:
            # Distinguish the NEW COHORT's classify submit from the collect's
            # own pipelined tier-3 submit: both are submissions, but only the
            # first is the "new submit" the ordering has to come before. The
            # collect's tier-3 submit happening BEFORE the claim is athenaeum#1145
            # AC7 working, not a violation of AC1.
            model = requests[0]["params"]["model"]
            sequence.append(
                "submit-classify"
                if model == DEFAULT_CLASSIFY_MODEL
                else "submit-write"
            )
            return real_create(requests=requests)

        client.batches.retrieve = traced_retrieve  # type: ignore[method-assign]
        client.batches.create = traced_create  # type: ignore[method-assign]

        import athenaeum.librarian as lib_mod

        real_discover = lib_mod.discover_raw_files

        def traced_discover(*args: Any, **kwargs: Any) -> Any:
            sequence.append("claim")
            return real_discover(*args, **kwargs)

        monkeypatch.setattr(lib_mod, "discover_raw_files", traced_discover)
        monkeypatch.setattr(
            batch_state, "resolve_cache_dir", lambda: cache_dir
        )
        _clean_env(monkeypatch)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )

        # Collect first, then claim, then the new cohort's classify submit.
        assert sequence.index("collect-retrieve") < sequence.index("claim")
        assert sequence.index("claim") < sequence.index("submit-classify")
        # AC7: the collected classify handle pipelined into its tier-3 submit
        # within this same run — before the claim, since the collect owns the
        # whole phase.
        assert sequence.index("submit-write") < sequence.index("claim")

    def test_a_collect_only_run_succeeds_and_reports_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: collecting a prior batch is the whole of a valid run."""
        _patch_uids(monkeypatch)
        root, cache_dir, client, _usage, raw_files = _spill_a_classify_batch(
            tmp_path, "o3", ["Standalone fact about WidgetOnly gadget.\n"]
        )
        monkeypatch.setattr(batch_state, "resolve_cache_dir", lambda: cache_dir)
        _clean_env(monkeypatch)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)

        stats: dict[str, Any] = {}
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
            out_run_stats=stats,
        )

        assert rc == 0
        assert stats["collected_refs"] == [raw_files[0].ref]
        assert list((root / "wiki").glob("*widgetonly*"))
        # Collected work is progress: the zero-yield alarm must not fire on a
        # run that drained a file, even though it claimed none.
        assert stats.get("zero_yield") is not True

    def test_dry_run_collects_nothing_and_retires_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC8."""
        _patch_uids(monkeypatch)
        root, cache_dir, client, _usage, raw_files = _spill_a_classify_batch(
            tmp_path, "o4", ["Standalone fact about WidgetDry gadget.\n"]
        )
        before = batch_state.load(cache_dir)
        monkeypatch.setattr(batch_state, "resolve_cache_dir", lambda: cache_dir)
        _clean_env(monkeypatch)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
            dry_run=True,
        )

        assert batch_state.load(cache_dir) == before
        assert raw_files[0].path.exists()
        assert not list((root / "wiki").glob("*widgetdry*"))

    def test_a_collected_file_with_a_failed_request_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: the all-or-nothing guarantee survives the across-run split.

        The collect path does not get its own write path, so the per-file
        "every call succeeded before anything is written" rule is the SAME
        code — a file with one errored request writes nothing, keeps its raw,
        and is retried next run.
        """
        _patch_uids(monkeypatch)
        root = _seed_root(
            tmp_path, "c8", ["Standalone fact about WidgetBad gadget.\n"]
        )
        cache_dir = tmp_path / "c8-cache"
        cache_dir.mkdir()
        classify_client = _FakeClient(_scripted_responder, allow_sync=False)
        # The tier-3 create for WidgetBad comes back errored on collect.
        write_client = _FakeClient(
            _scripted_responder,
            allow_sync=False,
            never_end=True,
            fail_marker="WidgetBad",
        )
        raw_files = discover_raw_files(root / "raw")

        process_batch_run(
            raw_files,
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            deadline=time.monotonic() - 1.0,
            cache_dir=cache_dir,
        )
        write_client.batches._never_end = False

        out = collect_pending_batches(
            EntityIndex(root / "wiki"),
            root / "wiki",
            classify_client,
            FALLBACK_TYPES,
            FALLBACK_TAGS,
            FALLBACK_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
            sleep=lambda s: None,
            cache_dir=cache_dir,
        )

        assert out.created == 0
        assert out.failed_refs == [raw_files[0].ref]
        assert out.collected_refs == []
        # Nothing written, raw preserved for a fresh claim next run.
        assert not list((root / "wiki").glob("*widgetbad*"))
        assert raw_files[0].path.exists()
        # The handle still retires: its results are consumed, and leaving the
        # lease on would strand the file it just declined to write.
        assert batch_state.load(cache_dir) == {}
