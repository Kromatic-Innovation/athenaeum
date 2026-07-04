# SPDX-License-Identifier: Apache-2.0
"""Issue #236 — opt-in Batch API mode for the librarian's tier-2/tier-3 calls.

Covers:
- Flag resolution: ``--batch-mode`` CLI > ``ATHENAEUM_BATCH_MODE`` env >
  yaml ``librarian.batch_mode`` > default off (#232 resolver pattern).
- Equivalence: a batch-mode run produces wiki output identical to the
  synchronous path on the same intake with the same (fake, deterministic)
  responses.
- Budget semantics: ``ATHENAEUM_MAX_API_CALLS`` enforced at batch-assembly
  time; remainder deferred via the #220 manifest.
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import anthropic as anthropic_mod
import pytest

from athenaeum.batch import (
    BATCH_POLL_INTERVAL_SECONDS,
    BatchExecutionError,
    BatchRequest,
    execute_batch,
)
from athenaeum.cli import main
from athenaeum.librarian import librarian_batch_mode, run
from athenaeum.models import TokenUsage
from athenaeum.tiers import DEFAULT_CLASSIFY_MODEL

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
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
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
    ) -> None:
        self._responder = responder
        self._polls_until_end = polls_until_end
        self._never_end = never_end
        self._fail_marker = fail_marker
        self._create_error = create_error
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
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

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
    """Fake Anthropic client exposing sync ``messages.create`` AND batches."""

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
        existing = user_msg.split("## Existing page content\n", 1)[1]
        existing = existing.split("\n\n## New observation", 1)[0]
        src = re.search(r"## New observation \(source: (.+)\)", user_msg).group(1)
        return existing.rstrip() + f"\n\nMerged note from {src}."
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
# Budget semantics (#220) at batch-assembly time
# ---------------------------------------------------------------------------


class TestBatchSelfResolvingGuard:
    """Issue #300 follow-up (#304): the deterministic self-resolving-claim
    guard must fire on the batch transport too, not just the sync path —
    an opus-model Quine review of the initial #304 PR found batch mode
    bypassed the guard entirely, the same bypass-class #296 needed a
    post-filter to close.
    """

    def test_self_resolving_claim_flagged_before_batch_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = [
            "WidgetFoo is primary. Human-confirmed (Tristan, 2026-07-02).\n"
        ]
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
        assert rc == 0
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
        assert out["a"].content[0].text == "ok"
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.batch_input_tokens == 100
        assert usage.batch_output_tokens == 50
        # Attempts are counted at assembly time by the caller, not here.
        assert usage.api_calls == 0


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
        assert out["a"].content[0].text == "ok"

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
        assert execute_batch(client, [], description="test") == {}
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
