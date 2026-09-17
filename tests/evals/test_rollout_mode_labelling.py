# SPDX-License-Identifier: Apache-2.0
"""Offline pins for ``RolloutRecord.mode`` labelling (Quine review, issue
athenaeum#1733, MUST item 1): every CLI runner must record ``mode == "cli"``
and every api runner must record ``mode == "api"`` -- never left to an
implicit dataclass default.

The four CLI runners (:func:`run_pull`, :func:`run_push_breadcrumb_pull`,
:func:`run_native_index`, :func:`run_native_grep`) all gate on
``shutil.which(claude_binary)`` and then ``subprocess.run(...)`` a real
``claude`` binary -- both are monkeypatched here to a fake, so these tests
need no real CLI and no network, exactly like ``tests/evals/test_rollout.py``'s
own offline suite (this module is ``rollout``-marked for the same reason:
it imports ``tests.evals.rollout``, itself rollout-suite machinery).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    run_native_grep,
    run_native_grep_api,
    run_native_index,
    run_native_index_api,
    run_pull,
    run_pull_api,
    run_push_breadcrumb_pull,
    run_push_breadcrumb_pull_api,
)

pytestmark = pytest.mark.rollout

#: One line of valid ``claude -p --output-format stream-json`` output that
#: parses to a real (if empty of tool calls) answer -- enough for
#: :func:`tests.evals.rollout.parse_stream` to produce a well-formed
#: ``ParsedPullStream`` without a real ``claude`` process.
_FAKE_STREAM_JSON_STDOUT = '{"type": "result", "result": "fake cli answer"}\n'


def _fake_completed_process(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=0, stdout=_FAKE_STREAM_JSON_STDOUT, stderr=""
    )


@pytest.fixture(autouse=True)
def _fake_claude_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module gets a fake ``claude`` on PATH (so the
    ``shutil.which`` gate passes) and a fake ``subprocess.run`` (so no real
    process is ever spawned)."""
    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _name: "/usr/bin/fake-claude")
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _fake_completed_process)


def _pto_probe():
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    return probe, corpus


def test_run_pull_records_mode_cli(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    corpus.materialize(tmp_path)
    record = run_pull(probe, tmp_path, tmp_path / "cache", "core")
    assert record.mode == "cli"


def test_run_push_breadcrumb_pull_records_mode_cli(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    corpus.materialize(tmp_path)
    record = run_push_breadcrumb_pull(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        context_fn=lambda *a, **k: "",  # skip the real shipped-hook subprocess spawn
    )
    assert record.mode == "cli"


def test_run_native_index_records_mode_cli(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    record = run_native_index(probe, tmp_path, "core")
    assert record.mode == "cli"


def test_run_native_grep_records_mode_cli(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    record = run_native_grep(probe, tmp_path, "core")
    assert record.mode == "cli"


# ---------------------------------------------------------------------------
# api runners: mode == "api" for all four (rounding out test_rollout_api_mode.py's
# coverage of run_pull_api / run_native_grep_api with the remaining two).
# ---------------------------------------------------------------------------


def _fake_llm_client() -> FakeLLMClient:
    return FakeLLMClient(
        response=make_llm_response(
            "fake api answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )


def test_run_push_breadcrumb_pull_api_records_mode_api(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    corpus.materialize(tmp_path)
    session = EvalSession()
    record = run_push_breadcrumb_pull_api(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        client=_fake_llm_client(),
        session=session,
        context_fn=lambda *a, **k: "",
        search_backend="keyword",
    )
    assert record.mode == "api"


def test_run_native_index_api_records_mode_api(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    session = EvalSession()
    record = run_native_index_api(
        probe, tmp_path, "core", client=_fake_llm_client(), session=session
    )
    assert record.mode == "api"


def test_run_native_grep_api_records_mode_api(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    session = EvalSession()
    record = run_native_grep_api(
        probe, tmp_path, "core", client=_fake_llm_client(), session=session
    )
    assert record.mode == "api"


def test_run_pull_api_records_mode_api(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    wiki_root = corpus.materialize(tmp_path)
    session = EvalSession()
    record = run_pull_api(
        probe,
        wiki_root,
        tmp_path / "cache",
        "core",
        client=_fake_llm_client(),
        session=session,
        search_backend="keyword",
    )
    assert record.mode == "api"
