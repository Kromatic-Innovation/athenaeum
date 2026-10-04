# SPDX-License-Identifier: Apache-2.0
"""Byte-equivalence proof for the PUSH_BREADCRUMB arm (issue athenaeum#1574
AC1).

AC1 is deliberately the sharp one: "A breadcrumb PUSH arm delivers
byte-equivalent context to what ``examples/claude-code/user-prompt-recall.sh``
injects for the same query on the same materialized corpus, pinned by a
test that RUNS THE REAL HOOK against the corpus and DIFFS." So this module
invokes the real, shipped ``examples/claude-code/user-prompt-recall.sh``
(after ``session-start-recall.sh`` builds its index) and diffs against
:func:`tests.evals.rollout.build_push_breadcrumb_context`'s own output for
the identical query on the identical materialized corpus.

**What the diff proves, after issue athenaeum#1363.** AC1's "must not
reimplement the hook's SQL ranking / awk 200-character clamp / ``LIMIT 3``
in Python" is satisfied structurally now rather than by vigilance: the hook
has no SQL, no awk and no slot cap of its own — it is a thin launcher for
:mod:`athenaeum.claude_code_adapter`, and
:func:`~tests.evals.rollout.build_push_breadcrumb_context` reaches the same
core. What this test still pins is therefore worth keeping and worth
stating precisely: that the eval harness's context builder and the SHIPPED
launcher agree byte-for-byte, so the arm measures what a user's hook
actually injects rather than something only the harness can produce.

The two hook invocations below are constructed INDEPENDENTLY (this module
builds its own env dict, mirroring ``tests/test_shell_hooks.py``'s
``hook_env`` fixture shape by hand, rather than calling
``tests.evals.rollout.build_breadcrumb_hook_env``) so a bug in that shared
env-building helper cannot make this test pass by construction — the two
sides genuinely differ in everything except the corpus, the query and the
hook scripts themselves. Each invocation gets its own throwaway ``HOME``
and a distinct ``session_id`` (real Claude Code session ids are unique per
session; reusing one across the two invocations would trip the hook's own
session-dedup ``SEEN_FILE`` logic and bias the SECOND call's results,
which is not what "same query on the same corpus" means).

Requires ``bash`` and a SQLite build with FTS5 (probed through the
``sqlite3`` module, which is what builds the index — the hook example runs
no SQL of its own since issue athenaeum#1363) — skips cleanly (never
fails) when either is absent, same idiom ``tests/test_shell_hooks.py``
uses throughout.
No API client, no ``claude`` binary, no token spend — NOT ``rollout``-marked
(issue athenaeum#1742); runs in the default selection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.evals.corpus import build_corpus
from tests.evals.rollout import (
    SESSION_START_HOOK,
    SHELL_USER_PROMPT_HOOK,
    build_push_breadcrumb_context,
)


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not available on this runner")


def _require_fts5_sqlite() -> None:
    """A SQLite build can exist without FTS5 compiled in — the index build
    and this test's corpus both need the real extension. Skip cleanly
    rather than fail on a stripped-down build.

    Probes the ``sqlite3`` MODULE, not the CLI (issue athenaeum#1363): the
    index is built by ``athenaeum.search.build_fts5_index`` and the hook
    example no longer runs any SQL of its own, so the CLI's presence proves
    nothing about this path — and requiring it would skip the whole test on
    a runner that can in fact run it.
    """
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(a);")
    except _sqlite3.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"sqlite3 module lacks FTS5: {exc}")
    finally:
        conn.close()


def _hand_built_hook_env(knowledge_root: Path, home: Path) -> dict[str, str]:
    """An INDEPENDENT env construction (not
    ``tests.evals.rollout.build_breadcrumb_hook_env``) — same field shape
    as ``tests/test_shell_hooks.py``'s ``hook_env`` fixture, built by hand
    so this test's "expected" side cannot share a bug with the
    implementation under test."""
    athenaeum_src = Path(__file__).resolve().parent.parent.parent
    return {
        "HOME": str(home),
        "ATHENAEUM_CACHE_DIR": str(home / ".cache" / "athenaeum"),
        "PATH": os.environ.get("PATH", ""),
        "KNOWLEDGE_ROOT": str(knowledge_root),
        "ATHENAEUM_SRC": str(athenaeum_src),
        "ATHENAEUM_PYTHON": sys.executable,
        "ATHENAEUM_CLI": str(home / "no-such-athenaeum-binary"),
    }


def _run_hook_directly(knowledge_root: Path, home: Path, query: str, session_id: str) -> str:
    """Hand-rolled hook invocation: run ``session-start-recall.sh`` then
    ``user-prompt-recall.sh`` directly via ``subprocess``, exactly the
    two-step shape ``tests/test_shell_hooks.py::TestUserPromptRecall``
    uses, and return ``hookSpecificOutput.additionalContext`` verbatim."""
    env = _hand_built_hook_env(knowledge_root, home)
    start = subprocess.run(
        ["bash", str(SESSION_START_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert start.returncode == 0, f"session-start-recall.sh failed: {start.stderr}"

    result = subprocess.run(
        ["bash", str(SHELL_USER_PROMPT_HOOK)],
        input=json.dumps({"prompt": query, "session_id": session_id}),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"user-prompt-recall.sh failed: {result.stderr}"
    assert result.stdout, "expected hookSpecificOutput JSON on stdout"
    payload = json.loads(result.stdout)
    return str(payload["hookSpecificOutput"]["additionalContext"])


def test_push_breadcrumb_arm_is_byte_equivalent_to_the_real_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: runs the REAL shipped hook against the REAL materialized
    rollout corpus (``tests.evals.corpus.build_corpus``), and separately
    calls :func:`build_push_breadcrumb_context` — the exact function
    :func:`tests.evals.rollout.run_push_breadcrumb` uses to assemble its
    context — against the SAME corpus and query, then asserts the two
    strings are byte-identical.

    Issue athenaeum#1887 pinned ``ATHENAEUM_EVAL_HOOK=shell``: this AC is
    specifically about the shell hook's own ranking/clamp/budget logic, so
    it must keep exercising that implementation regardless of which hook
    the harness spawns by default now (the packaged adapter — a DIFFERENT
    implementation this AC says nothing about).
    """
    monkeypatch.setenv("ATHENAEUM_EVAL_HOOK", "shell")
    _require("bash")
    _require_fts5_sqlite()

    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    knowledge_root = tmp_path / "knowledge"
    corpus.materialize(knowledge_root)

    expected = _run_hook_directly(
        knowledge_root,
        tmp_path / "hand_built_home",
        probe.query,
        session_id=f"expected-{uuid.uuid4().hex}",
    )
    actual = build_push_breadcrumb_context(
        knowledge_root,
        tmp_path / "implementation_home",
        probe.query,
        session_id=f"actual-{uuid.uuid4().hex}",
    )

    # Sanity: the corpus/query combination must actually produce a
    # non-empty breadcrumb, or a diff of two empty strings would prove
    # nothing about the ranking/clamp/budget logic this AC is about.
    assert expected != "", "test setup bug: the probe query matched nothing in the FTS5 index"
    assert expected.startswith("[Knowledge context] Wiki pages relevant to this message")

    assert actual == expected


def test_push_breadcrumb_arm_matches_the_real_hook_across_multiple_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same proof as above, repeated over several probes so the
    byte-equivalence claim is not an artifact of one lucky query — a
    ranking/clamp divergence that only shows up on ties, on a
    description-bearing page, or on zero-hit queries would otherwise slip
    through a single-probe test.

    Issue athenaeum#1887: same ``ATHENAEUM_EVAL_HOOK=shell`` pin as the
    single-probe test above, and for the same reason.
    """
    monkeypatch.setenv("ATHENAEUM_EVAL_HOOK", "shell")
    _require("bash")
    _require_fts5_sqlite()

    corpus = build_corpus("core")
    knowledge_root = tmp_path / "knowledge"
    corpus.materialize(knowledge_root)

    probe_ids = [p.id for p in corpus.probes[:5]]
    assert probe_ids, "test setup bug: core corpus has no probes"

    for probe_id in probe_ids:
        probe = next(p for p in corpus.probes if p.id == probe_id)
        expected = _run_hook_directly(
            knowledge_root,
            tmp_path / f"hand_built_home-{probe_id}",
            probe.query,
            session_id=f"expected-{uuid.uuid4().hex}",
        )
        actual = build_push_breadcrumb_context(
            knowledge_root,
            tmp_path / f"implementation_home-{probe_id}",
            probe.query,
            session_id=f"actual-{uuid.uuid4().hex}",
        )
        assert actual == expected, f"byte-equivalence diverged for probe {probe_id!r}"
