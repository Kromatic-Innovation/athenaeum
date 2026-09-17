# SPDX-License-Identifier: Apache-2.0
"""The NATIVE_INDEX / NATIVE_GREP spikes, encoded as tests (issue athenaeum#1725).

Modeled closely on ``test_rollout_pull_spike.py``: the same structural split
between a credential-free leg and a gated one, for the same reason.

1. **The spawn/config leg is proven and reproducible WITHOUT credentials.**
   ``test_native_index_spawn_reports_the_prepared_memory_directory`` and
   ``test_native_grep_spawn_reports_the_prepared_memory_directory`` each spawn
   the real ``claude`` binary with ``--settings`` naming an
   ``autoMemoryDirectory`` and assert the ``system``/``init`` event's
   ``memory_paths.auto`` names that SAME directory — verified in this
   container against live Claude Code 2.1.267 (finding 1: the event carries
   ``memory_paths`` unconditionally; the run then dies at the MODEL TURN with
   "Not logged in · Please run /login", exactly like the PULL spike, which
   these tests never touch). Skips cleanly when ``claude`` is absent from
   ``PATH``.

2. **The truncation leg is ALSO credential-free** — the ``attachment`` event
   that carries what Claude Code actually loaded (finding 4) is written
   during session hydration, before the auth-gated model turn, so it survives
   the same "not logged in" failure the spawn leg does.
   ``test_native_index_truncates_memory_md_at_medium_scale`` asserts the
   written index exceeds the documented cap and what came back is strictly
   shorter and flagged ``truncated_by_claude_code``. It is gated on a
   DIFFERENT axis instead: auto memory itself is behind a cached feature
   flag (finding 3) that only activates when the isolated config is seeded
   from an ambient ``cachedGrowthBookFeatures`` object
   (:func:`tests.evals.rollout.seed_native_claude_config`). A container with
   no such ambient state present would see auto memory never load at all —
   distinguishable from real truncation because NOTHING would have loaded
   (``index_bytes_loaded == 0``) — and the test skips loudly, naming that
   exact cause, rather than silently passing on an observation that never
   happened.

All three tests carry ``pytest.mark.rollout`` (deselected by default
alongside ``eval``/``embedding`` — see ``pyproject.toml``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.evals.corpus import build_corpus
from tests.evals.rollout import (
    NATIVE_INDEX_MAX_BYTES,
    NATIVE_INDEX_MAX_LINES,
    run_native_grep,
    run_native_index,
)

pytestmark = pytest.mark.rollout


def _pto_probe(scale: str = "core"):
    corpus = build_corpus(scale)
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    return probe, corpus


def _init_event(transcript: list[dict]) -> dict:
    events = [
        event
        for event in transcript
        if isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "init"
    ]
    assert events, f"no system/init event in the stream; transcript={transcript!r}"
    return events[0]


def test_native_index_spawn_reports_the_prepared_memory_directory(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")

    probe, _corpus = _pto_probe()
    record = run_native_index(probe, tmp_path, "core", timeout=60.0)

    init = _init_event(record.transcript)
    memory_paths = init.get("memory_paths") or {}
    auto_path = str(memory_paths.get("auto", "")).rstrip("/")
    expected = str((tmp_path / "memory").resolve())
    assert auto_path and Path(auto_path).resolve() == Path(expected), (
        f"memory_paths.auto={memory_paths.get('auto')!r} does not name the prepared memory "
        f"directory {expected!r}"
    )


def test_native_grep_spawn_reports_the_prepared_memory_directory(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")

    probe, _corpus = _pto_probe()
    record = run_native_grep(probe, tmp_path, "core", timeout=60.0)

    init = _init_event(record.transcript)
    memory_paths = init.get("memory_paths") or {}
    auto_path = str(memory_paths.get("auto", "")).rstrip("/")
    expected = str((tmp_path / "memory").resolve())
    assert auto_path and Path(auto_path).resolve() == Path(expected), (
        f"memory_paths.auto={memory_paths.get('auto')!r} does not name the prepared memory "
        f"directory {expected!r}"
    )
    # NATIVE_GREP writes topic files but no index at all.
    assert not (tmp_path / "memory" / "MEMORY.md").exists()


def test_native_index_truncates_memory_md_at_medium_scale(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")

    probe, _corpus = _pto_probe("medium")
    record = run_native_index(probe, tmp_path, "medium", timeout=60.0)

    native = record.transcript[0].get("native_memory")
    assert isinstance(native, dict), (
        f"transcript[0] carries no native_memory dict: {record.transcript[0]!r}"
    )

    if native["index_bytes_loaded"] == 0:
        pytest.skip(
            "auto memory did not activate in this container -- no ambient "
            "cachedGrowthBookFeatures to seed the isolated config (see "
            "seed_native_claude_config's docstring), or the feature is "
            "gated off entirely. Skipping rather than reporting a "
            "truncation observation that never happened."
        )

    # The `medium` scale (1,000 pages) writes an index well past the
    # documented cap -- the honest picture of what native memory becomes
    # past the cap, per the design doc's "truncation is the finding, not a
    # confound".
    assert (
        native["index_bytes_written"] > NATIVE_INDEX_MAX_BYTES
        or native["index_lines_written"] > NATIVE_INDEX_MAX_LINES
    ), f"expected the written index to exceed the documented cap: {native!r}"
    assert native["index_bytes_loaded"] < native["index_bytes_written"], (
        f"expected the LOADED index to be strictly shorter than the WRITTEN one: {native!r}"
    )
    assert native["truncated_by_claude_code"] is True, native
