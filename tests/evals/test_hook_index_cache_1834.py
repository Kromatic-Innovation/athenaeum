# SPDX-License-Identifier: Apache-2.0
"""Offline tests for issue athenaeum#1834: the breadcrumb hook's search
index must be built ONCE per (corpus_scale, knowledge_root) and reused
across that scale's cells, and a hook subprocess failure (timeout or
nonzero exit) must be recorded as ``RolloutRecord.harness_failure`` rather
than crashing the grid -- the failure mode that killed Evals run
35388861368 at 480/720 cells.

NOT ``rollout``-marked (issue athenaeum#1742) -- runs in the default
selection. No real subprocess is spawned anywhere in this module:
``subprocess.run`` is monkeypatched to a fake that records every argv it
was called with (so a test can assert exactly how many times
``SESSION_START_HOOK`` was spawned) and, for a SESSION_START_HOOK call,
writes the same marker file (``<HOME>/.cache/athenaeum/wiki-index.db``)
the real hook's FTS5 build leaves behind -- :func:`_hook_index_present`
reads that same marker to self-heal a stale cache entry, so the fake must
produce it too or every second call in these tests would (correctly)
rebuild.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals import rollout as rollout_module
from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    SESSION_START_HOOK,
    USER_PROMPT_HOOK,
    HookIndexBuildError,
    RolloutRecord,
    build_hook_index,
    build_push_breadcrumb_context,
    clear_hook_index_cache,
    run_probe_all_arms,
    run_push_breadcrumb,
)


@pytest.fixture(autouse=True)
def _reset_hook_index_cache():
    """Issue athenaeum#1834: without this, the first test to populate a
    cache key would make a LATER test see zero spawns (or a stale sticky
    failure) for the wrong reason. ``tmp_path`` already makes every test's
    ``knowledge_root`` unique, but the cache is module-level state shared
    across the whole process -- clearing it is cheap insurance regardless."""
    clear_hook_index_cache()
    yield
    clear_hook_index_cache()


def _probe(probe_id: str):
    corpus = build_corpus("core")
    return next(p for p in corpus.probes if p.id == probe_id), corpus


def _fake_subprocess_run(calls: list[list[str]]):
    """A ``subprocess.run`` stand-in: records every argv, and for a
    SESSION_START_HOOK invocation writes the marker file the real hook's
    FTS5 build leaves under ``env["HOME"]/.cache/athenaeum/wiki-index.db``
    (see :func:`tests.evals.rollout._hook_index_present`) so the cache's
    self-healing check treats this fake build as a real one."""

    def _run(argv: list[str], *, env: dict[str, str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        if argv[1] == str(SESSION_START_HOOK):
            cache_dir = Path(env["HOME"]) / ".cache" / "athenaeum"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "wiki-index.db").write_text("fake fts5 index", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        assert argv[1] == str(USER_PROMPT_HOOK)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return _run


# ---------------------------------------------------------------------------
# AC1: build the index once per (corpus_scale, knowledge_root)
# ---------------------------------------------------------------------------


def test_build_hook_index_spawns_session_start_hook_once_per_knowledge_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(rollout_module.subprocess, "run", _fake_subprocess_run(calls))
    knowledge_root = tmp_path / "knowledge"
    hook_home = tmp_path / "hook_home"

    build_hook_index(knowledge_root, hook_home)
    build_hook_index(knowledge_root, hook_home)
    build_hook_index(knowledge_root, hook_home)

    session_start_calls = [c for c in calls if c[1] == str(SESSION_START_HOOK)]
    assert len(session_start_calls) == 1


def test_two_probes_at_one_scale_spawn_exactly_one_session_start_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue athenaeum#1834 AC1's own counter-example test: two probes at
    one scale spawn exactly one ``SESSION_START_HOOK`` subprocess.
    Exercises :func:`build_push_breadcrumb_context` -- the exact entry
    point :func:`run_push_breadcrumb`/``run_push_breadcrumb_pull(_api)``
    call -- for two DIFFERENT probe queries against the SAME
    ``(knowledge_root, hook_home)``, the pair every probe at a given
    ``(corpus_scale, replicate)`` on the same worker slot shares
    (``north_star_cli._run_group``'s ``group_root``)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(rollout_module.subprocess, "run", _fake_subprocess_run(calls))
    knowledge_root = tmp_path / "knowledge"
    hook_home = tmp_path / "hook_home"

    build_push_breadcrumb_context(knowledge_root, hook_home, "first probe's query")
    build_push_breadcrumb_context(knowledge_root, hook_home, "second, different probe's query")

    session_start_calls = [c for c in calls if c[1] == str(SESSION_START_HOOK)]
    user_prompt_calls = [c for c in calls if c[1] == str(USER_PROMPT_HOOK)]
    assert len(session_start_calls) == 1
    # The per-cell call still runs USER_PROMPT_HOOK for EVERY probe -- only
    # the one-time index build is shared.
    assert len(user_prompt_calls) == 2


def test_build_hook_index_rebuilds_when_a_different_hook_home_has_no_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-healing guard (issue athenaeum#1834): the SAME knowledge_root
    paired with a DIFFERENT (never-built) hook_home must still get its own
    real build -- a cache keyed on knowledge_root alone must never let one
    hook_home's index stand in for another's. This is also what keeps
    ``test_rollout_push_breadcrumb_spike.py``'s own multi-probe test (one
    knowledge_root, a FRESH hook_home per probe) correct under this cache.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(rollout_module.subprocess, "run", _fake_subprocess_run(calls))
    knowledge_root = tmp_path / "knowledge"

    build_hook_index(knowledge_root, tmp_path / "hook_home_one")
    build_hook_index(knowledge_root, tmp_path / "hook_home_two")

    session_start_calls = [c for c in calls if c[1] == str(SESSION_START_HOOK)]
    assert len(session_start_calls) == 2


def test_cached_build_failure_is_sticky_and_does_not_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue athenaeum#1834: a failed build must not retry-storm the
    remaining cells at that scale -- a cache hit on a stored failure
    re-raises immediately rather than spawning a second (equally doomed)
    subprocess."""
    calls: list[list[str]] = []

    def _always_times_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300.0, output=b"", stderr=b"partial")

    monkeypatch.setattr(rollout_module.subprocess, "run", _always_times_out)
    knowledge_root = tmp_path / "knowledge"
    hook_home = tmp_path / "hook_home"

    with pytest.raises(HookIndexBuildError):
        build_hook_index(knowledge_root, hook_home)
    with pytest.raises(HookIndexBuildError):
        build_hook_index(knowledge_root, hook_home)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# AC2/AC3: hook subprocess failures become harness_failure, never a crash
# ---------------------------------------------------------------------------


def test_run_push_breadcrumb_hook_timeout_is_recorded_as_harness_failure(tmp_path: Path) -> None:
    """A stubbed hook that raises ``TimeoutExpired`` yields a harness-
    failure row rather than propagating and killing the grid."""
    probe, _corpus = _probe("pto_allowance")
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "I don't know.", usage=make_llm_usage(input_tokens=20, output_tokens=6)
        )
    )

    def _timing_out_context_fn(knowledge_root: Path, hook_home: Path, query: str) -> str:
        raise subprocess.TimeoutExpired(
            cmd=["bash", str(SESSION_START_HOOK)], timeout=300.0, output=b"", stderr=b"stuck"
        )

    record = run_push_breadcrumb(
        probe,
        "core",
        knowledge_root=tmp_path / "knowledge",
        hook_home=tmp_path / "hook_home",
        client=client,
        session=session,
        model="stub-model",
        context_fn=_timing_out_context_fn,
    )

    assert record.harness_failure is not None
    assert "TimeoutExpired" in record.harness_failure
    assert "stuck" in record.harness_failure
    assert record.injected_context_tokens == 0
    # The cell still ran (no exception escaped) -- a real answer was
    # produced against an empty context, same shape as the pre-existing
    # empty-breadcrumb harness-failure path.
    assert record.answer == "I don't know."


def test_run_push_breadcrumb_hook_index_build_error_takes_precedence_over_empty_breadcrumb_reason(
    tmp_path: Path,
) -> None:
    """The hook-failure reason must win over the generic "empty breadcrumb
    for a non-abstention probe" message -- a reader of the report should see
    the KNOWN cause, not a re-derived guess."""
    probe, _corpus = _probe("pto_allowance")
    assert probe.expected_uids
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "I don't know.", usage=make_llm_usage(input_tokens=20, output_tokens=6)
        )
    )

    def _raising_context_fn(knowledge_root: Path, hook_home: Path, query: str) -> str:
        raise HookIndexBuildError("CalledProcessError: exit 1 -- stderr tail: 'boom'")

    record = run_push_breadcrumb(
        probe,
        "core",
        knowledge_root=tmp_path / "knowledge",
        hook_home=tmp_path / "hook_home",
        client=client,
        session=session,
        model="stub-model",
        context_fn=_raising_context_fn,
    )

    assert record.harness_failure == "CalledProcessError: exit 1 -- stderr tail: 'boom'"


def test_hook_timeout_on_one_arm_does_not_abort_the_grid(tmp_path: Path) -> None:
    """Issue athenaeum#1834 AC2: a stubbed hook that raises
    ``TimeoutExpired`` yields one harness-failure row and the REMAINING
    cells still run -- pinned at the ``run_probe_all_arms`` grid-dispatch
    level, not just the single-arm level above."""
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "stub answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )

    def _stub_pull_runner(
        probe, knowledge_root, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        return RolloutRecord(
            arm=rollout_module.Arm.PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub pull answer",
            recall_called=False,
        )

    def _timing_out_breadcrumb_context_fn(knowledge_root, hook_home, query) -> str:
        raise subprocess.TimeoutExpired(cmd=["bash"], timeout=300.0)

    def _stub_breadcrumb_pull_runner(
        probe, knowledge_root, hook_home, cache_dir, corpus_scale, **kwargs
    ) -> RolloutRecord:
        return RolloutRecord(
            arm=rollout_module.Arm.PUSH_BREADCRUMB_PULL,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub breadcrumb-pull answer",
            recall_called=False,
        )

    def _stub_native_index_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=rollout_module.Arm.NATIVE_INDEX,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native index answer",
        )

    def _stub_native_grep_runner(probe, materialize_root, corpus_scale, **kwargs) -> RolloutRecord:
        return RolloutRecord(
            arm=rollout_module.Arm.NATIVE_GREP,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            corpus_scale=corpus_scale,
            answer="stub native grep answer",
        )

    records = run_probe_all_arms(
        "pto_allowance",
        "core",
        session=session,
        materialize_root=tmp_path,
        search_backend="keyword",
        client=client,
        mode="cli",
        pull_runner=_stub_pull_runner,
        breadcrumb_context_fn=_timing_out_breadcrumb_context_fn,
        breadcrumb_pull_runner=_stub_breadcrumb_pull_runner,
        native_index_runner=_stub_native_index_runner,
        native_grep_runner=_stub_native_grep_runner,
    )

    # Every arm still produced a record -- no exception escaped the grid.
    assert set(records) == {
        "none",
        "push_pages_upper_bound",
        "push_breadcrumb",
        "push_breadcrumb_pull",
        "oracle",
        "pull",
        "native_index",
        "native_grep",
    }
    assert records["push_breadcrumb"].harness_failure is not None
    assert "TimeoutExpired" in records["push_breadcrumb"].harness_failure
    # Every other arm ran cleanly, unaffected by the breadcrumb hook's
    # failure.
    for arm_value, record in records.items():
        if arm_value == "push_breadcrumb":
            continue
        assert record.harness_failure is None
