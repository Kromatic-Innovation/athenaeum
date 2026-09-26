# SPDX-License-Identifier: Apache-2.0
"""The packaged adapter emits the shell hook's overflow notice (issue athenaeum#1905).

``tests/evals/test_rollout_push_breadcrumb_spike.py`` proves byte-equivalence
between the harness and the SHELL hook, under an explicit
``ATHENAEUM_EVAL_HOOK=shell`` pin. Nothing pinned the DEFAULT hook — the
packaged adapter (:mod:`athenaeum.claude_code_adapter`) that athenaeum#1361/#1887
made the shipped ``UserPromptSubmit`` command — against the shell hook it
replaced, and a whole behaviour went missing in that gap: the awk
``END``-block overflow notice at
``examples/claude-code/user-prompt-recall.sh``'s ``$OVERFLOW_TMPL`` render.
athenaeum#1894 measured the cost (``push_breadcrumb_pull`` 80.0% -> 70.5%);
a zero-model-call byte-diff over all 48 ``core`` probes found the notice was
the only consistent difference in the breadcrumb text.

This module runs BOTH hooks against the same materialized corpus and
compares. It is deliberately not a full byte-equivalence assertion, because
three differences remain that athenaeum#1905 does not close and should not
silently paper over:

1. **The shell hook's trailing newline.** Its ``$MATCHES`` accumulator ends
   every bullet with a ``\\n``, so its ``additionalContext`` always ends with
   one; the adapter renders ``preamble + "\\n" + text`` with no trailing
   separator. Present on every probe, overflow or not.
2. **The shell hook's spurious empty bullet before the notice.** Splitting
   the ``__ATHENAEUM_OVERFLOW__`` sentinel off ``$RESULTS`` leaves a trailing
   newline behind, so the render loop reads one extra empty record and emits
   a bare ``  - `` line ahead of the notice. It is a defect in a retired
   hook (athenaeum#1363 owns that hook's removal), not behaviour to
   reproduce.
3. **Candidate selection can still differ.** The shell hook applies a
   relevance floor (athenaeum#1665) the core does not, so on some probes the
   two render different pages — and therefore legitimately withhold
   different ones. That gap is real, and out of athenaeum#1905's scope (this
   issue is about the notice, which athenaeum#1894 isolated as the
   *consistent* difference).

So the comparison below is conditioned on what it can honestly assert:
**where the two hooks rendered the same bullets, they must render the same
notice, byte for byte** — same candidates in, same candidates withheld, same
sentence out. Plus two vacuity guards, because a conditional assertion that
never fires proves nothing.

Requires ``bash``, ``jq`` and a real FTS5-capable ``sqlite3`` CLI; skips
cleanly otherwise, the same idiom the spike test above uses. No API client,
no ``claude`` binary, no token spend — NOT ``rollout``-marked, so it runs in
the default selection.
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
    resolve_user_prompt_hook,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: How many ``core`` probes to compare. The full 48 is what athenaeum#1894's
#: offline diagnostic swept; this test pays a real ``session-start-recall.sh``
#: index build per probe on the shell side, so it takes a representative
#: prefix and leaves the exhaustive sweep to that (zero-cost, on-demand)
#: diagnostic.
PROBE_COUNT = 6


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not available on this runner")


def _require_fts5_sqlite() -> None:
    _require("sqlite3")
    probe = subprocess.run(
        ["sqlite3", ":memory:", "CREATE VIRTUAL TABLE t USING fts5(a);"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"sqlite3 CLI lacks FTS5: {probe.stderr.strip()}")


def _require_adapter_is_the_default_hook() -> None:
    """Vacuity guard 1: if the default resolution ever points back at a
    ``.sh``, this whole module silently degrades to comparing the shell hook
    with itself — which passes unconditionally and proves nothing about the
    adapter."""
    resolved = resolve_user_prompt_hook()
    if resolved.suffix == ".sh":
        pytest.skip(f"default UserPromptSubmit hook is not the packaged adapter: {resolved}")


def _hook_env(knowledge_root: Path, home: Path) -> dict[str, str]:
    """Isolated env for the shell hook, built here rather than borrowed from
    ``tests.evals.rollout.build_breadcrumb_hook_env`` — the adapter side goes
    through that helper, so a bug in it must not be able to move both sides
    of the comparison in the same direction."""
    return {
        "HOME": str(home),
        "ATHENAEUM_CACHE_DIR": str(home / ".cache" / "athenaeum"),
        "PATH": os.environ.get("PATH", ""),
        "KNOWLEDGE_ROOT": str(knowledge_root),
        "ATHENAEUM_SRC": str(_REPO_ROOT),
        "ATHENAEUM_PYTHON": sys.executable,
        "PYTHON": sys.executable,
        "PYTHONPATH": str((_REPO_ROOT / "src").resolve()),
        "ATHENAEUM_CLI": str(home / "no-such-athenaeum-binary"),
    }


def _run_shell_hook(knowledge_root: Path, home: Path, query: str, session_id: str) -> str:
    env = _hook_env(knowledge_root, home)
    if not (home / ".cache" / "athenaeum" / "wiki-index.db").is_file():
        start = subprocess.run(
            ["bash", str(SESSION_START_HOOK)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert start.returncode == 0, f"session-start-recall.sh failed: {start.stderr}"

    result = subprocess.run(
        ["bash", str(SHELL_USER_PROMPT_HOOK)],
        input=json.dumps({"prompt": query, "session_id": session_id}),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"user-prompt-recall.sh failed: {result.stderr}"
    if not result.stdout.strip():
        return ""
    return str(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])


def _split(additional_context: str) -> tuple[list[str], str]:
    """Separate rendered bullets from the (at most one) overflow notice.

    The notice is identifiable without pattern-matching its wording: it is
    the only non-empty line that does not carry the ``  - `` bullet prefix,
    which the template guarantees by construction (see
    :func:`athenaeum.recall_overflow.render_overflow_line`). The shell
    hook's bare ``  - `` artifact line (difference 2 in this module's
    docstring) is dropped here so the bullet comparison is about pages, not
    about that defect.
    """
    bullets: list[str] = []
    notice = ""
    for line in additional_context.split("\n"):
        if not line or line.startswith("[Knowledge context]"):
            continue
        if line.startswith("  - "):
            if line.strip() != "-":
                bullets.append(line)
            continue
        notice = line
    return bullets, notice


@pytest.fixture(scope="module")
def _corpus_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("overflow-1905") / "knowledge"
    build_corpus("core").materialize(root)
    return root


def _compare(corpus_root: Path, tmp_path: Path) -> list[tuple[str, list[str], str, list[str], str]]:
    """Run both hooks over the first :data:`PROBE_COUNT` ``core`` probes.

    Each side gets its own ``HOME`` (reused across probes — the hooks key
    their session-dedup file by ``session_id``, which is fresh per call) so
    the expensive index build is paid once per side rather than once per
    probe.
    """
    probes = build_corpus("core").probes[:PROBE_COUNT]
    assert probes, "test setup bug: core corpus has no probes"

    rows = []
    for probe in probes:
        shell = _run_shell_hook(
            corpus_root,
            tmp_path / "shell-home",
            probe.query,
            f"shell-{uuid.uuid4().hex}",
        )
        adapter = build_push_breadcrumb_context(
            corpus_root,
            tmp_path / "adapter-home",
            probe.query,
            session_id=f"adapter-{uuid.uuid4().hex}",
        )
        shell_bullets, shell_notice = _split(shell)
        adapter_bullets, adapter_notice = _split(adapter)
        rows.append((probe.id, shell_bullets, shell_notice, adapter_bullets, adapter_notice))
    return rows


def test_adapter_renders_the_same_overflow_notice_as_the_shell_hook(
    _corpus_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC2: the notice athenaeum#1894 found missing is back, and it is the
    SAME sentence the shell hook renders whenever the two hooks saw the same
    candidates.

    Counter-example this defeats, exactly as measured on 2026-09-26: the
    adapter's breadcrumb ends at its last bullet while the shell hook's goes
    on to say "N more matching results ... call ``recall`` to see them."
    """
    monkeypatch.delenv("ATHENAEUM_EVAL_HOOK", raising=False)
    _require("bash")
    _require("jq")
    _require_fts5_sqlite()
    _require_adapter_is_the_default_hook()

    rows = _compare(_corpus_root, tmp_path)

    # Vacuity guard 2: at least one probe must actually overflow, or every
    # assertion below is a comparison of two empty strings.
    assert any(notice for _id, _sb, notice, _ab, _an in rows), (
        "test setup bug: no core probe in this prefix withheld any candidate, "
        "so this module proved nothing about the overflow notice"
    )

    compared = 0
    for probe_id, shell_bullets, shell_notice, adapter_bullets, adapter_notice in rows:
        if shell_bullets != adapter_bullets:
            # Difference 3 in the module docstring: different candidates in,
            # so a different withheld set out. Not this issue's subject.
            continue
        compared += 1
        assert adapter_notice == shell_notice, (
            f"probe {probe_id!r}: the two hooks rendered identical bullets but "
            f"different overflow notices\n  shell:   {shell_notice!r}\n"
            f"  adapter: {adapter_notice!r}"
        )

    assert compared, (
        "no probe in this prefix produced identical bullets on both hooks, so the "
        "notice comparison never ran — widen PROBE_COUNT or fix candidate selection"
    )


def test_the_adapter_never_ends_a_capped_breadcrumb_without_the_notice(
    _corpus_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression in its own right, independent of the shell hook: on a
    probe where the SHELL hook says candidates were withheld and both hooks
    rendered the same pages, an adapter breadcrumb that stops at its last
    bullet is the athenaeum#1894 defect, whatever the shell hook is doing."""
    monkeypatch.delenv("ATHENAEUM_EVAL_HOOK", raising=False)
    _require("bash")
    _require("jq")
    _require_fts5_sqlite()
    _require_adapter_is_the_default_hook()

    rows = _compare(_corpus_root, tmp_path)
    overflowing = [
        (probe_id, adapter_notice)
        for probe_id, shell_bullets, shell_notice, adapter_bullets, adapter_notice in rows
        if shell_notice and shell_bullets == adapter_bullets
    ]
    assert overflowing, "test setup bug: no comparable probe withheld any candidate"

    for probe_id, adapter_notice in overflowing:
        assert adapter_notice, f"probe {probe_id!r}: adapter dropped the overflow notice"
        assert "call `recall` to see them." in adapter_notice
        assert not adapter_notice.startswith("-"), (
            f"probe {probe_id!r}: the notice acquired a bullet prefix, which would "
            "inflate every breadcrumb page count by one phantom page"
        )
