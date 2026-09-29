# SPDX-License-Identifier: Apache-2.0
"""The core builds the shell hook's query terms (issue athenaeum#1912).

``examples/claude-code/user-prompt-recall.sh`` and
:func:`athenaeum.context.build_context_for_turn` are supposed to be two
renderings of one retrieval. They were not: on the regex term-extraction
branch the shell read the canonical stopword list
``session-start-recall.sh`` caches at ``${CACHE_DIR}/stopwords.txt`` and
applied ``sort -u | head -8``, while the core applied a roughly 30-word
baked-in fallback and kept the first 8 terms in *prompt order*. Different
terms and a different term ORDER mean a different FTS5 ``OR`` query and a
different vector query string, which is a different bullet list — the
largest residual class in the ``push_breadcrumb_pull`` rollout-eval gap
(``docs/measurements/rollout-eval-adapter-cutover-2026-09-29.md``).

This module pins the term construction itself, in process: no subprocess, no
``chromadb``, no API key, no model call. The end-to-end proof that the two
hooks now emit the same bytes lives in
``tests/evals/test_hook_byte_equivalence_1912.py``; this one exists so a
regression in the term rule is diagnosed here, in milliseconds, rather than
as a mysterious bullet-list difference three layers up.

**How the expected-terms fixture was computed.** Not by this repository's
Python — that would make the test a tautology. Every entry in
``tests/evals/data/hook_parity/core_expected_terms.json`` is the output of
the shell hook's own pipeline (``user-prompt-recall.sh``'s regex branch),
run under ``LC_ALL=C`` over that probe's query with the canonical
``athenaeum.search.STOPWORDS`` written out as the cached ``stopwords.txt``::

    printf '%s\\n' "$PROMPT" \\
      | tr '[:upper:]' '[:lower:]' \\
      | tr -cs '[:alnum:]' '\\n' \\
      | grep -vE "^(${STOPWORDS})$" \\
      | grep -E '.{3,}' \\
      | sort -u \\
      | head -8

Regenerate it with that pipeline, never by copying what the core currently
returns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from athenaeum.context import _FALLBACK_STOPWORDS, _extract_terms, _resolve_stopwords
from athenaeum.search import STOPWORDS
from tests.evals.corpus import build_corpus
from tests.evals.rollout import build_breadcrumb_hook_env

_EXPECTED_TERMS_FIXTURE = (
    Path(__file__).resolve().parent / "data" / "hook_parity" / "core_expected_terms.json"
)


@pytest.fixture(scope="module")
def expected_terms() -> dict[str, list[str]]:
    return dict(json.loads(_EXPECTED_TERMS_FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture
def cache_with_canonical_stopwords(tmp_path: Path) -> Path:
    """A cache dir carrying the stopword list ``session-start-recall.sh``
    writes — ``athenaeum.search.STOPWORDS``, one per line."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "stopwords.txt").write_text("\n".join(STOPWORDS) + "\n", encoding="utf-8")
    return cache_dir


# ---------------------------------------------------------------------------
# The cached canonical list, and when the baked-in fallback is right
# ---------------------------------------------------------------------------


def test_the_cached_canonical_list_is_used_when_the_file_is_present(
    cache_with_canonical_stopwords: Path,
) -> None:
    """The shell hook's own rule: read ``${CACHE_DIR}/stopwords.txt``."""
    assert _resolve_stopwords(cache_with_canonical_stopwords) == frozenset(STOPWORDS)


def test_the_baked_in_fallback_is_used_when_the_file_is_absent(tmp_path: Path) -> None:
    assert _resolve_stopwords(tmp_path) == _FALLBACK_STOPWORDS


def test_the_baked_in_fallback_is_used_when_the_file_is_empty(tmp_path: Path) -> None:
    """``[ -s ... ]`` in the shell: an EMPTY file is not a stopword list.

    Pinned because a zero-byte ``stopwords.txt`` is exactly what a crashed or
    half-written ``session-start-recall.sh`` leaves behind, and reading it as
    "the canonical list is empty" would silently disable stopword filtering
    altogether rather than degrading to the fallback.
    """
    (tmp_path / "stopwords.txt").write_text("", encoding="utf-8")
    assert _resolve_stopwords(tmp_path) == _FALLBACK_STOPWORDS


def test_the_canonical_list_actually_differs_from_the_fallback() -> None:
    """Vacuity guard. If these two ever coincided, every assertion in this
    module about *which* list was applied would pass for the wrong reason."""
    assert frozenset(STOPWORDS) != _FALLBACK_STOPWORDS
    assert frozenset(STOPWORDS) - _FALLBACK_STOPWORDS


# ---------------------------------------------------------------------------
# The regex branch, over every core probe
# ---------------------------------------------------------------------------


def test_regex_branch_terms_match_the_shell_rule_for_every_core_probe(
    expected_terms: dict[str, list[str]],
    cache_with_canonical_stopwords: Path,
) -> None:
    """Every one of the 48 ``core`` probes, against the shell-computed fixture.

    Asserted as a whole-corpus diff rather than probe-by-probe so a rule
    change that moves many probes reports as one failure naming all of them,
    not as whichever probe happens to sort first.
    """
    stopwords = _resolve_stopwords(cache_with_canonical_stopwords)
    probes = build_corpus("core").probes
    assert {p.id for p in probes} == set(expected_terms), (
        "the expected-terms fixture and the core corpus have drifted apart; "
        "regenerate the fixture with the shell pipeline in this module's docstring"
    )

    mismatches = {}
    for probe in probes:
        actual = _extract_terms(
            probe.query, timeout=0.0, stopwords=stopwords, config=None, use_llm=False
        )
        if actual != expected_terms[probe.id]:
            mismatches[probe.id] = (expected_terms[probe.id], actual)
    assert not mismatches, f"terms diverge from the shell hook's rule: {mismatches}"


def test_regex_branch_terms_are_sorted_unique_and_capped_at_eight(
    expected_terms: dict[str, list[str]],
    cache_with_canonical_stopwords: Path,
) -> None:
    """The three properties ``sort -u | head -8`` provides, asserted directly
    rather than inferred from the fixture — a fixture regenerated from a
    broken pipeline would still satisfy the test above."""
    stopwords = _resolve_stopwords(cache_with_canonical_stopwords)
    for probe in build_corpus("core").probes:
        terms = _extract_terms(
            probe.query, timeout=0.0, stopwords=stopwords, config=None, use_llm=False
        )
        assert terms == sorted(terms), f"{probe.id}: not in codepoint order"
        assert len(terms) == len(set(terms)), f"{probe.id}: duplicate term"
        assert len(terms) <= 8, f"{probe.id}: more than eight terms"
        assert all(len(t) >= 3 and t.isalnum() and t == t.lower() for t in terms), (
            f"{probe.id}: a term is not a lowercase alphanumeric token of length 3+"
        )


def test_at_least_one_core_probe_exercises_the_eight_term_truncation(
    expected_terms: dict[str, list[str]],
) -> None:
    """Vacuity guard for the ``head -8`` half: if no probe ever produced more
    than eight candidate terms, the truncation rule would be untested and the
    prompt-order-vs-sorted distinction would not bite on this corpus."""
    assert any(len(terms) == 8 for terms in expected_terms.values())


# ---------------------------------------------------------------------------
# The LLM-topic branch
# ---------------------------------------------------------------------------


def test_llm_branch_terms_are_sorted_deduplicated_and_cut_to_eight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell applies ``sort -u | head -8`` to the extractor's output too
    (``user-prompt-recall.sh``'s LLM branch), so the core must as well.

    The fake return is deliberately hostile on all four axes at once —
    unsorted, duplicated, mixed case, more than eight tokens, and carrying
    multi-word topics and sub-three-character noise — because the old code
    was correct on none of them and a gentler fixture could pass by accident.
    No network: ``extract_topics`` is replaced outright.
    """
    fake_topics = [
        "Zebra Crossing",
        "apple",
        "APPLE",
        "b2b",
        "no",  # dropped: shorter than three characters
        "Yak",
        "mango",
        "Quince",
        "kiwi",
        "Lemon",
        "nectarine",
        "olive",
        "papaya",
    ]

    def fake_extract_topics(prompt: str, timeout: float, config: object) -> list[str]:
        return fake_topics

    monkeypatch.setattr("athenaeum.query_topics.extract_topics", fake_extract_topics)

    terms = _extract_terms(
        "anything at all", timeout=3.0, stopwords=frozenset(), config=None, use_llm=True
    )

    expected_all = sorted(
        {
            "zebra", "crossing", "apple", "b2b", "yak", "mango", "quince",
            "kiwi", "lemon", "nectarine", "olive", "papaya",
        }
    )
    assert terms == expected_all[:8]
    assert len(terms) == 8
    assert terms == sorted(terms)
    assert len(terms) == len(set(terms))


def test_llm_branch_does_not_apply_stopwords(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity detail, pinned so it is not "tidied" into symmetry later: the
    shell filters stopwords on its REGEX branch only — an LLM that returns
    "the Return Path" keeps ``the``-class tokens. Making the core stricter
    here would reintroduce a divergence in the other direction."""

    def fake_extract_topics(prompt: str, timeout: float, config: object) -> list[str]:
        return ["what", "widget"]

    monkeypatch.setattr("athenaeum.query_topics.extract_topics", fake_extract_topics)
    terms = _extract_terms(
        "anything at all", timeout=3.0, stopwords=frozenset(STOPWORDS), config=None, use_llm=True
    )
    assert terms == ["what", "widget"]


# ---------------------------------------------------------------------------
# The harness's own LLM asymmetry (issue athenaeum#1912, the ATHENAEUM_CLI trap's
# missing adapter-side twin)
# ---------------------------------------------------------------------------

#: A stand-in ``anthropic`` module that records any attempt to construct a
#: client or issue a request, and makes none itself. Placed FIRST on the
#: subprocess's ``PYTHONPATH`` so it shadows the real SDK.
_ANTHROPIC_SPY = textwrap.dedent(
    '''
    """Recording stand-in for the anthropic SDK. Makes no request, ever."""
    import os


    def _record(event):
        with open(os.environ["ATHENAEUM_TEST_SPY_MARKER"], "a", encoding="utf-8") as fh:
            fh.write(event + "\\n")


    class _Messages:
        def create(self, **kwargs):
            _record("messages.create")
            raise RuntimeError("spy: no request is ever issued")


    class Anthropic:
        def __init__(self, **kwargs):
            _record("Anthropic()")
            self.messages = _Messages()
    '''
).strip()


def _spy_dir(tmp_path: Path) -> Path:
    spy = tmp_path / "spy"
    spy.mkdir(parents=True, exist_ok=True)
    (spy / "anthropic.py").write_text(_ANTHROPIC_SPY + "\n", encoding="utf-8")
    return spy


def _run_adapter_with_spy(
    env: dict[str, str], tmp_path: Path, marker: Path, *, cache_dir: Path
) -> None:
    """Spawn the packaged adapter console script under the spy, with a
    ``config.env`` in *cache_dir* carrying a stub ``ANTHROPIC_API_KEY``."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "config.env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-not-a-real-key-athenaeum-1912\n", encoding="utf-8"
    )
    spawn_env = dict(env)
    spawn_env["PYTHONPATH"] = os.pathsep.join(
        [str(_spy_dir(tmp_path)), spawn_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    spawn_env["ATHENAEUM_TEST_SPY_MARKER"] = str(marker)
    subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=json.dumps(
            {"prompt": "what is the firm's PTO allowance?", "session_id": "spy-session"}
        ),
        env=spawn_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_breadcrumb_hook_env_pins_the_adapter_to_its_regex_extractor(
    tmp_path: Path,
) -> None:
    """Issue athenaeum#1912: the adapter's twin of the shell's ``ATHENAEUM_CLI`` trap.

    ``build_breadcrumb_hook_env`` points ``ATHENAEUM_CLI`` at a path that
    cannot exist, so ``user-prompt-recall.sh``'s ``command -v`` fails and the
    shell side is pinned to its offline regex extractor. The adapter does not
    shell out to that CLI at all — it calls
    :func:`athenaeum.query_topics.extract_topics` in process, and
    ``claude_code_adapter._load_config_env`` sources the hook home's
    ``config.env``, which ``session-start-recall.sh`` can ``op read`` an
    ``ANTHROPIC_API_KEY`` into. A rollout arm that must stay a pure retrieval
    measurement would then make a live model request per cell.

    The spy records a construction or a request without issuing one, so this
    test makes no outbound call of its own; the positive control below proves
    the spy is wired up and that the guarantee comes from the environment
    rather than from something incidental about the sandbox.
    """
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    hook_home = tmp_path / "home"
    env = build_breadcrumb_hook_env(knowledge_root, hook_home)
    marker = tmp_path / "pinned-marker.txt"

    _run_adapter_with_spy(
        env, tmp_path / "pinned", marker, cache_dir=Path(env["ATHENAEUM_CACHE_DIR"])
    )

    assert not marker.exists(), (
        "the adapter reached the LLM topic extractor under the breadcrumb hook "
        f"environment: {marker.read_text(encoding='utf-8') if marker.exists() else ''}"
    )


def test_positive_control_the_spy_fires_without_the_pin(tmp_path: Path) -> None:
    """Without the pin, the same spawn DOES reach the SDK — so the assertion
    above is about the environment, not about an inert spy or a sandbox that
    could not have made a request anyway."""
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    hook_home = tmp_path / "home"
    env = build_breadcrumb_hook_env(knowledge_root, hook_home)
    for name in ("ANTHROPIC_API_KEY", "ATHENAEUM_LLM_PROVIDER", "ATHENAEUM_TOPIC_LLM_PROVIDER"):
        env.pop(name, None)
    marker = tmp_path / "unpinned-marker.txt"

    _run_adapter_with_spy(
        env, tmp_path / "unpinned", marker, cache_dir=Path(env["ATHENAEUM_CACHE_DIR"])
    )

    assert marker.exists() and "Anthropic()" in marker.read_text(encoding="utf-8"), (
        "the spy never fired even with the pin removed — this module's guarantee "
        "would then be vacuous"
    )
