# SPDX-License-Identifier: Apache-2.0
"""The packaged adapter emits the shell hook's overflow notice (issue athenaeum#1905).

``tests/evals/test_rollout_push_breadcrumb_spike.py`` proves byte-equivalence
between the harness and the SHELL hook, under an explicit
``ATHENAEUM_EVAL_HOOK=shell`` pin. Nothing pinned the DEFAULT hook — the
packaged adapter (:mod:`athenaeum.claude_code_adapter`) that athenaeum#1361/#1887
made the shipped ``UserPromptSubmit`` command — against the shell hook it
replaced, and a whole behaviour went missing in that gap: the awk ``END``-block
overflow notice at ``examples/claude-code/user-prompt-recall.sh``'s
``$OVERFLOW_TMPL`` render. athenaeum#1894 measured the cost
(``push_breadcrumb_pull`` 80.0% -> 70.5%); a zero-model-call byte-diff over all
48 ``core`` probes found the notice was the only consistent difference in the
breadcrumb text.

**What this module asserts, and what it deliberately does not.** It runs BOTH
hooks against the same materialized corpus and pins the thing that actually
regressed: **where the shell hook says candidates were withheld, the adapter
must say so too, in the shared template's own words.** It does NOT assert the
two notices carry the same NUMBERS, and that restraint is measured rather than
assumed — see difference 3 below. The exact arithmetic (which candidates are
withheld, by which rule, and how the count and the type breakdown render) is
pinned deterministically and in-process by
``tests/test_context_overflow_1905.py``; this module's job is the end-to-end
one that only a real subprocess can do.

Four differences from the shell hook remain. athenaeum#1905 closes none of
them, and they are stated here rather than left to be rediscovered as bugs:

1. **The shell hook's trailing newline.** Its ``$MATCHES`` accumulator ends
   every bullet with a ``\\n``, so its ``additionalContext`` always ends with
   one; the adapter renders ``preamble + "\\n" + text`` with no trailing
   separator. Present on every probe, overflow or not.
2. **The shell hook's spurious empty bullet before the notice.** Splitting the
   ``__ATHENAEUM_OVERFLOW__`` sentinel off ``$RESULTS`` leaves a trailing
   newline behind, so the render loop reads one extra empty record and emits a
   bare ``  - `` line ahead of the notice. A defect in a retired hook
   (athenaeum#1363 owns that hook's removal), not behaviour to reproduce.
3. **The two candidate windows can differ below the rendered top-N.** Each hook
   builds its own query terms and runs its own vector leg, so two hooks that
   render an IDENTICAL bullet list can still have fetched different tails — and
   therefore withheld different candidates. Measured on GitHub Actions
   (2026-09-26, where the vector backend is live, unlike a typical local run):
   probe ``confidentiality_rule`` rendered the same seven bullets on both sides
   while the shell reported ``at least 18`` withheld ``(2 client, 6 company,
   1 concept, 1 meeting, 3 note, 2 person, 2 principle, 1 project)`` and the
   adapter ``at least 19`` ``(2 client, 8 company, 1 concept, 1 meeting, 1 note,
   3 person, 2 principle, 1 project)``. Identical top-N does not imply an
   identical tail; an assertion that the counts match would be pinning a
   coincidence.
4. **The shell hook applies a relevance floor** (athenaeum#1665) the core does
   not. Inactive by default — ``resolve_recall_relevance_floor`` returns
   ``None`` for both backends on the default config, so it is not what
   difference 3 measured — but a configured floor would widen that gap further.

Requires ``bash``, ``jq`` and a real FTS5-capable ``sqlite3`` CLI; skips
cleanly otherwise, the same idiom the spike test above uses. No API client, no
``claude`` binary, no token spend — NOT ``rollout``-marked, so it runs in the
default selection.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from athenaeum.recall_overflow import render_overflow_line
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
#: index build on each side, so it takes a representative prefix and leaves the
#: exhaustive sweep to that (zero-cost, on-demand) diagnostic.
PROBE_COUNT = 6

#: Parses a rendered notice back into its ``(at_least, {type: count})`` inputs,
#: so :func:`_rerender` can prove the string came out of the shared renderer.
#: Anchored at both ends: a notice with anything extra around it does not match,
#: which is the point.
_NOTICE_RE = re.compile(
    r"^memory has (?P<at_least>at least )?(?P<total>\d+) more matching results "
    r"\((?P<types>[^)]*)\) that were withheld by the relevance cap "
    r"— call `recall` to see them\.$"
)


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
    """Vacuity guard: if the default resolution ever points back at a ``.sh``,
    this whole module silently degrades to comparing the shell hook with
    itself — which passes unconditionally and proves nothing about the
    adapter."""
    resolved = resolve_user_prompt_hook()
    if resolved.suffix == ".sh":
        pytest.skip(f"default UserPromptSubmit hook is not the packaged adapter: {resolved}")


def _hook_env(knowledge_root: Path, home: Path) -> dict[str, str]:
    """Isolated env for the shell hook, built here rather than borrowed from
    ``tests.evals.rollout.build_breadcrumb_hook_env`` — the adapter side goes
    through that helper, so a bug in it must not be able to move both sides of
    the comparison in the same direction."""
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

    The notice is identifiable without pattern-matching its wording: it is the
    only non-empty line that does not carry the ``  - `` bullet prefix, which
    the template guarantees by construction (see
    :func:`athenaeum.recall_overflow.render_overflow_line`). The shell hook's
    bare ``  - `` artifact line (difference 2 in this module's docstring) is
    dropped here so the bullet comparison is about pages, not about that
    defect.
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


def _rerender(notice: str) -> str:
    """Re-render *notice* from its own parsed inputs through the shared
    renderer. Equal output proves the string is exactly what
    :mod:`athenaeum.recall_overflow` produces from the packaged template —
    same wording, same separators, same ASCII-sorted type order — rather than
    a hand-built look-alike that merely reads right."""
    m = _NOTICE_RE.match(notice)
    assert m, f"notice does not match the packaged template's shape: {notice!r}"
    counts: dict[str, int] = {}
    for part in m.group("types").split(", "):
        n, _, name = part.partition(" ")
        counts[name] = int(n)
    assert sum(counts.values()) == int(m.group("total")), (
        f"the type breakdown does not sum to the stated total: {notice!r}"
    )
    return render_overflow_line(counts, at_least=bool(m.group("at_least")))


@pytest.fixture(scope="module")
def _corpus_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("overflow-1905") / "knowledge"
    build_corpus("core").materialize(root)
    return root


@pytest.fixture(scope="module")
def _comparison(_corpus_root: Path, tmp_path_factory: pytest.TempPathFactory) -> list[tuple]:
    """Both hooks over the first :data:`PROBE_COUNT` ``core`` probes, as
    ``(probe_id, shell_bullets, shell_notice, adapter_bullets, adapter_notice)``.

    Module-scoped: each side pays one real index build rather than one per
    probe. Each side keeps its own ``HOME``, and every query gets a fresh
    ``session_id`` (the hooks key their session-dedup file by it), so reusing a
    home across probes cannot bias a later call.
    """
    homes = tmp_path_factory.mktemp("homes")
    rows = []
    for probe in build_corpus("core").probes[:PROBE_COUNT]:
        shell = _run_shell_hook(
            _corpus_root, homes / "shell", probe.query, f"shell-{uuid.uuid4().hex}"
        )
        adapter = build_push_breadcrumb_context(
            _corpus_root,
            homes / "adapter",
            probe.query,
            session_id=f"adapter-{uuid.uuid4().hex}",
        )
        shell_bullets, shell_notice = _split(shell)
        adapter_bullets, adapter_notice = _split(adapter)
        rows.append((probe.id, shell_bullets, shell_notice, adapter_bullets, adapter_notice))
    assert rows, "test setup bug: core corpus has no probes"
    return rows


@pytest.fixture(autouse=True)
def _default_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_EVAL_HOOK", raising=False)
    _require("bash")
    _require("jq")
    _require_fts5_sqlite()
    _require_adapter_is_the_default_hook()


def test_adapter_says_candidates_were_withheld_wherever_the_shell_hook_does(
    _comparison: list[tuple],
) -> None:
    """AC1/AC2, and the athenaeum#1894 regression itself: the adapter's
    breadcrumb used to stop at its last bullet while the shell hook's went on
    to say "N more matching results ... call ``recall`` to see them."

    Conditioned on the shell hook, not on a hardcoded probe id, so the pin
    follows the corpus rather than a snapshot of it.
    """
    withholding = [
        (probe_id, adapter_bullets, adapter_notice)
        for probe_id, _sb, shell_notice, adapter_bullets, adapter_notice in _comparison
        if shell_notice
    ]
    assert withholding, (
        "test setup bug: no core probe in this prefix withheld any candidate on the "
        "shell hook, so this module proved nothing about the overflow notice"
    )

    for probe_id, adapter_bullets, adapter_notice in withholding:
        assert adapter_bullets, f"probe {probe_id!r}: adapter rendered no breadcrumb at all"
        assert adapter_notice, (
            f"probe {probe_id!r}: the shell hook reported withheld candidates and the "
            "adapter's breadcrumb ended at its last bullet — the athenaeum#1894 defect"
        )


def test_the_adapter_notice_is_the_shared_template_verbatim(
    _comparison: list[tuple],
) -> None:
    """athenaeum#1905's actual fix is single-sourcing, not a second copy of the
    sentence: the string the real adapter subprocess emitted must be exactly
    what :mod:`athenaeum.recall_overflow` renders from the packaged template.

    A reimplementation that merely read right — a different separator, an
    unsorted type list, a hardcoded literal drifting from
    ``src/athenaeum/prompts/recall_overflow_breadcrumb.md`` — fails here.
    """
    notices = [
        (probe_id, adapter_notice)
        for probe_id, _sb, _sn, _ab, adapter_notice in _comparison
        if adapter_notice
    ]
    assert notices, "test setup bug: the adapter rendered no overflow notice on any probe"

    for probe_id, notice in notices:
        assert notice == _rerender(notice), f"probe {probe_id!r}: notice is not template-rendered"
        assert not notice.startswith("-"), (
            f"probe {probe_id!r}: the notice acquired a bullet prefix, which would inflate "
            "every breadcrumb page count by one phantom page"
        )
