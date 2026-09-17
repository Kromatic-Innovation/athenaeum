# SPDX-License-Identifier: Apache-2.0
"""The native WRITER spike, encoded as a test (issue athenaeum#1726 AC3).

Modeled on ``tests/evals/test_rollout_native_spike.py``'s NATIVE_INDEX/
NATIVE_GREP spikes: the spawn/config leg is credential-free (the ``system``/
``init`` event carries ``memory_paths`` before any auth-gated model turn
runs) and needs only the real ``claude`` binary on ``PATH``, never a login
or an API key. Skips cleanly when ``claude`` is absent, and skips LOUDLY
(named cause) rather than silently passing when the isolated config could
not activate auto memory at all, rather than reporting an observation that
never happened.

Carries ``pytest.mark.rollout`` (deselected by default alongside ``eval``/
``embedding`` -- see ``pyproject.toml``), matching every other credential-
adjacent spike test in this directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.evals.corpus import generate_page_observations, load_core_pages, load_probes
from tests.evals.rollout import run_native_writer

pytestmark = pytest.mark.rollout


def test_native_writer_spawn_reports_the_prepared_memory_directory(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")

    pages = load_core_pages()
    page = next(p for p in pages if p.uid == "policy-pto")
    observations = generate_page_observations(page, load_probes())[:1]

    result = run_native_writer(observations, tmp_path, timeout=60.0)

    assert len(result.sessions) == 1
    init_events = [
        event
        for event in result.sessions[0].transcript
        if isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "init"
    ]
    assert init_events, f"no system/init event in the stream: {result.sessions[0].transcript!r}"
    memory_paths = init_events[0].get("memory_paths") or {}
    auto_path = str(memory_paths.get("auto", "")).rstrip("/")
    expected = str((tmp_path / "memory").resolve())
    if not auto_path:
        pytest.skip(
            "no memory_paths.auto reported -- this Claude Code build may not "
            "expose it at session init; skipping rather than asserting an "
            "observation this container cannot produce"
        )
    assert Path(auto_path).resolve() == Path(expected), (
        f"memory_paths.auto={memory_paths.get('auto')!r} does not name the prepared memory "
        f"directory {expected!r}"
    )
