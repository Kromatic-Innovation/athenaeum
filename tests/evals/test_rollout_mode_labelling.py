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
own offline suite.

UNMARKED — it spends no token and spawns no real ``claude``, so it runs in
``ci.yml``'s default job. Importing ``tests.evals.rollout`` is NOT itself a
reason to carry ``pytest.mark.rollout``: that marker means token cost, not
module family (issue athenaeum#1742).
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
def _fake_claude_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test in this module gets a fake ``claude`` on PATH (so the
    ``shutil.which`` gate passes), a fake ``subprocess.run`` (so no real
    process is ever spawned), and an isolated ``CLAUDE_CONFIG_DIR`` (Quine
    review, issue athenaeum#1733 MUST item 1): the native CLI runners call
    ``seed_native_claude_config``, which reads ``CLAUDE_CONFIG_DIR`` (falling
    back to the OPERATOR's real ``~/.claude.json`` when unset) -- this
    fixture's whole point is a fully offline test, so it must never touch
    that real file, on this host or any other this suite ever runs on."""
    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _name: "/usr/bin/fake-claude")
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _fake_completed_process)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-isolated"))


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
# athenaeum#1826 defect 4: native arms in cli mode are unauthenticated --
# seed_native_claude_config mints a throwaway CLAUDE_CONFIG_DIR whose login
# does not follow on macOS, so a real spot-check answered every native cell
# with a login prompt (graded 0/6 as an ordinary miss). Reusing the
# operator's authenticated isolated config was judged unsafe here (see
# ``_not_logged_in_harness_failure``'s own docstring for why); this pins the
# chosen fallback -- marking the observable "Not logged in" answer.
# ---------------------------------------------------------------------------


def _completed_process_with(stdout: str) -> Any:
    def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    return _run


_NOT_LOGGED_IN_STDOUT = '{"type": "result", "result": "Not logged in \\u00b7 Please run /login"}\n'


def test_run_native_index_marks_harness_failure_when_not_logged_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "tests.evals.rollout.subprocess.run", _completed_process_with(_NOT_LOGGED_IN_STDOUT)
    )
    probe, _corpus = _pto_probe()
    record = run_native_index(probe, tmp_path, "core")
    assert record.harness_failure is not None


def test_run_native_grep_marks_harness_failure_when_not_logged_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "tests.evals.rollout.subprocess.run", _completed_process_with(_NOT_LOGGED_IN_STDOUT)
    )
    probe, _corpus = _pto_probe()
    record = run_native_grep(probe, tmp_path, "core")
    assert record.harness_failure is not None


def test_run_native_index_no_harness_failure_on_a_real_answer(tmp_path: Path) -> None:
    probe, _corpus = _pto_probe()
    record = run_native_index(probe, tmp_path, "core")
    assert record.harness_failure is None


def test_run_native_grep_no_harness_failure_on_a_real_answer(tmp_path: Path) -> None:
    probe, _corpus = _pto_probe()
    record = run_native_grep(probe, tmp_path, "core")
    assert record.harness_failure is None


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
    client = _fake_llm_client()
    record = run_push_breadcrumb_pull_api(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        client=client,
        session=session,
        context_fn=lambda *a, **k: "",
        search_backend="keyword",
    )
    assert record.mode == "api"
    # Quine review, issue athenaeum#1733 SHOULD item 3: PULL-style system
    # prompt mentions the recall tool.
    assert "recall" in client.calls[0]["system"]


def test_run_native_index_api_records_mode_api(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    session = EvalSession()
    client = _fake_llm_client()
    record = run_native_index_api(probe, tmp_path, "core", client=client, session=session)
    assert record.mode == "api"
    # Quine review, issue athenaeum#1733 SHOULD item 3: native system prompt
    # mentions the memory directory.
    assert str(tmp_path) in client.calls[0]["system"]


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
