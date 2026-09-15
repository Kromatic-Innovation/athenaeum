# SPDX-License-Identifier: Apache-2.0
"""Six-arm rollout runner: NONE / PUSH_PAGES_UPPER_BOUND / PUSH_BREADCRUMB /
PUSH_BREADCRUMB_PULL / ORACLE / PULL (issues athenaeum#1522, athenaeum#1574).

The north-star comparison needs one probe run across memory-delivery arms
and captured with enough fidelity to measure it. The arms decompose
unevenly, and the split below is deliberate (see the issue body):

* **NONE / PUSH_PAGES_UPPER_BOUND / PUSH_BREADCRUMB / ORACLE are
  single-shot completions** — only the assembled context differs, the
  model makes no tool choice. They reuse ``tests/evals/harness.py``'s
  ``EvalSession.observe_response`` / provider call shape (:func:`run_none`,
  :func:`run_push_pages_upper_bound`, :func:`run_push_breadcrumb`,
  :func:`run_oracle`), not a second provider abstraction.
* **PULL and PUSH_BREADCRUMB_PULL are real tool-use loops** (:func:`run_pull`,
  :func:`run_push_breadcrumb_pull`), because the whole point of those arms
  is whether the agent *decides* to call recall. ``recall_search()`` is a
  plain function, not something a model can decline to invoke — so both
  spawn ``claude -p`` with a scoped ``--mcp-config`` exposing only
  athenaeum's ``recall`` tool and ``--output-format stream-json`` so the
  tool-choice decision is visible in the stream. This is the SAME argv
  discipline :mod:`athenaeum.provider`'s ``ClaudeCliClient._build_argv``
  uses (prompt on stdin, never in argv — issue athenaeum#543 L4), with
  exactly the two text-only-pinning flags (``--tools ""``, unscoped
  ``--strict-mcp-config``) inverted. ``src/athenaeum/provider.py`` itself is
  left byte-unchanged — this module builds its own argv rather than
  parameterizing that one.

Neither PULL nor PUSH_BREADCRUMB_PULL choosing NOT to call recall is an
error — it is a recorded outcome: :func:`run_pull` / :func:`parse_pull_stream`
never raise on an empty tool call list, and ``RolloutRecord.recall_called``
is simply ``False``.

**PUSH means breadcrumbs (issue athenaeum#1574's operator decision).** The
shipped hook (``examples/claude-code/user-prompt-recall.sh``) injects at
most three 200-character-clamped ``name — description`` bullets, not five
full pages. :func:`run_push_breadcrumb` and :func:`run_push_breadcrumb_pull`
match that shape by ACTUALLY RUNNING the shipped hook
(:func:`build_push_breadcrumb_context`) rather than reimplementing its
SQL ranking / awk budget-and-clamp pass — see that function's docstring.
The original five-full-page arm survives, renamed to
:attr:`Arm.PUSH_PAGES_UPPER_BOUND` (:func:`run_push_pages_upper_bound`),
explicitly labelled an upper bound rather than the shipped configuration.

Reuse, not reimplementation:

* Corpus: :func:`tests.evals.corpus.build_corpus` / ``Corpus.materialize``.
* Index: ``athenaeum.search.get_backend(name).build_index`` (see
  ``tests/test_retrieval_golden_1420.py:132``).
* PUSH_PAGES_UPPER_BOUND delivery: mirrors ``athenaeum.mcp_server.recall_search``
  directly — same function, same default top_k.
* PUSH_BREADCRUMB / PUSH_BREADCRUMB_PULL delivery: the shipped hook itself,
  spawned as a subprocess exactly as Claude Code would invoke it — never a
  second implementation of its ranking/clamp/budget logic.
* Grading: intentionally NOT called here. ``tests/evals/metrics.py``'s
  ladder grades a *push* (ranked uids vs. ground truth); this module's job
  ends at capturing a :class:`RolloutRecord` with enough fidelity for a
  downstream grading/aggregation pass (issue athenaeum#1523) to consume,
  including a per-TURN token series that a summed-per-task total cannot
  reconstruct.
* Token ceiling: callers pass a ``session`` (an ``EvalSession`` instance,
  typically ``tests/evals/conftest.py``'s ``rollout_session`` fixture) —
  every arm's token usage lands there via ``observe_response``, never on
  ``harness.EVAL_TOKEN_CEILING``'s own accumulator. See
  ``tests/evals/rollout_session.py``.

Layering: sits under ``tests/evals/`` (test-only), like
``tests/evals/containment.py`` — no ``src/athenaeum/*.py`` module is added,
so no ``tests/fixtures/layer_declarations.py`` entry is needed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from athenaeum.mcp_server import recall_search
from athenaeum.provider import response_text as provider_response_text
from athenaeum.push_metrics import estimate_tokens
from athenaeum.search import get_backend
from tests.evals.containment import GridCell, build_grid
from tests.evals.corpus import Corpus, Probe, build_corpus
from tests.evals.harness import EvalSession, build_live_client

# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


class Arm(str, Enum):
    """The memory-delivery arms of the north-star comparison.

    A ``str`` subclass so an arm value round-trips through
    ``tests.evals.containment.GridCell.arm`` (a plain string field) without a
    lookup table: ``Arm.PULL.value == "pull"`` and ``Arm("pull") is
    Arm.PULL``.

    Issue athenaeum#1574 (operator decision, recorded on that issue): "PUSH
    means breadcrumbs." The original five-full-page arm — what ``PUSH`` used
    to mean — is renamed :attr:`PUSH_PAGES_UPPER_BOUND` and kept as an
    explicitly labelled upper bound (see ``run_push_pages_upper_bound`` and
    ``tests/evals/north_star_report.py``'s arm-legend section), never
    deleted. :attr:`PUSH_BREADCRUMB` and :attr:`PUSH_BREADCRUMB_PULL` are
    the two new arms that actually match what the shipped hook
    (``examples/claude-code/user-prompt-recall.sh``) delivers.
    """

    NONE = "none"
    PUSH_PAGES_UPPER_BOUND = "push_pages_upper_bound"
    PUSH_BREADCRUMB = "push_breadcrumb"
    PUSH_BREADCRUMB_PULL = "push_breadcrumb_pull"
    ORACLE = "oracle"
    PULL = "pull"

    @classmethod
    def _missing_(cls, value: object) -> Arm | None:
        """Back-compat for a result-store row that predates issue
        athenaeum#1574, which renamed ``"push"`` to
        ``"push_pages_upper_bound"`` (AC5: resuming an existing row must
        still work). ``Arm("push")`` — the exact string the older
        ``RolloutRecord.to_payload()`` persisted — resolves to the SAME arm
        the old value named (the five-full-page delivery), not a
        ``ValueError``. Any other unknown value still raises, same as a
        bare ``Enum`` — this is a single, named legacy alias, not a silent
        catch-all.
        """
        if value == "push":
            return cls.PUSH_PAGES_UPPER_BOUND
        return None


#: Enumeration order the runner always uses — the order every grid built by
#: :func:`run_probe_all_arms` is enumerated in, so a rerun's cell order is
#: stable (matches ``containment.build_grid``'s own determinism contract).
ALL_ARMS: tuple[Arm, ...] = (
    Arm.NONE,
    Arm.PUSH_PAGES_UPPER_BOUND,
    Arm.PUSH_BREADCRUMB,
    Arm.PUSH_BREADCRUMB_PULL,
    Arm.ORACLE,
    Arm.PULL,
)

#: Model used for both the single-shot arms and PULL's ``claude -p`` spawn
#: when the caller does not override it. Cheap and identical to the proven
#: spike's model (see the athenaeum#1522 spike evidence) — a rollout run is
#: many calls, so a cheap default keeps a bare invocation inexpensive.
DEFAULT_ROLLOUT_MODEL = DEFAULT_CLASSIFY_MODEL

#: The MCP tool name a PULL rollout looks for in the stream — the athenaeum
#: recall tool as FastMCP names it, matching the athenaeum#1522 spike
#: evidence's ``tools`` list entry verbatim.
RECALL_TOOL_NAME = "mcp__athenaeum__recall"

_SYSTEM_PROMPT = (
    "You are answering questions about a private knowledge base used only "
    "for evaluation. Answer using ONLY the context supplied below, if any. "
    "If the context does not contain the answer, say you do not know rather "
    "than guessing or using outside knowledge."
)


# ---------------------------------------------------------------------------
# Rollout record
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """One tool invocation observed in a PULL rollout's stream."""

    name: str
    query: str


@dataclasses.dataclass(frozen=True)
class TurnTokenUsage:
    """Token counts for ONE turn (issue athenaeum#1523 needs the per-turn
    series, not only a summed-per-task total)."""

    turn: int
    input_tokens: int
    output_tokens: int


@dataclasses.dataclass
class RolloutRecord:
    """Everything captured for one (probe, arm) rollout.

    ``injected_context_tokens`` is populated (a count, possibly ``0`` for an
    abstention probe with nothing to inject) for PUSH_PAGES_UPPER_BOUND,
    PUSH_BREADCRUMB, PUSH_BREADCRUMB_PULL and ORACLE, and left ``None`` for
    NONE/PULL — neither of those two delivers context the caller assembled:
    NONE gets none by design, PULL's context (if any) is whatever the model
    itself chose to pull via the tool call, which is already captured in
    ``tool_calls``/``transcript`` rather than a single token count.
    PUSH_BREADCRUMB_PULL is a hybrid — the breadcrumb IS caller-assembled
    (hence a real count here), but it may ALSO pull further content via the
    tool, which is captured the same way PULL's is.
    """

    arm: Arm
    probe_id: str
    probe_class: str
    corpus_scale: str
    answer: str
    turn_tokens: list[TurnTokenUsage] = dataclasses.field(default_factory=list)
    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    recall_called: bool = False
    injected_context_tokens: int | None = None
    turn_count: int = 0
    transcript: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turn_tokens)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turn_tokens)

    def to_payload(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for ``tests.evals.containment.ResultStore``
        (issue athenaeum#1523's persistence bridge).

        Every field the north-star report reads round-trips losslessly
        through :meth:`from_payload`: ``arm`` becomes its plain ``.value``
        string (``Arm`` is itself a ``str`` subclass, but ``json.dumps``
        would otherwise emit ``"Arm.PULL"``-style reprs for a raw enum
        member rather than the bare value -- explicit ``.value`` avoids
        relying on that), and the two nested dataclass lists become lists
        of plain dicts via :func:`dataclasses.asdict`. ``transcript`` is
        already JSON-safe (built from parsed ``stream-json`` events or
        plain string literals in :mod:`tests.evals.rollout` itself), so it
        passes through unchanged.
        """
        return {
            "arm": self.arm.value,
            "probe_id": self.probe_id,
            "probe_class": self.probe_class,
            "corpus_scale": self.corpus_scale,
            "answer": self.answer,
            "turn_tokens": [dataclasses.asdict(t) for t in self.turn_tokens],
            "tool_calls": [dataclasses.asdict(t) for t in self.tool_calls],
            "recall_called": self.recall_called,
            "injected_context_tokens": self.injected_context_tokens,
            "turn_count": self.turn_count,
            "transcript": self.transcript,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RolloutRecord:
        """Inverse of :meth:`to_payload`. Ignores unknown keys (for example
        a ``cell_key``/``replicate`` a caller merged into the same stored
        row) rather than raising, so a wider persisted row shape never
        breaks the read side."""
        return cls(
            arm=Arm(payload["arm"]),
            probe_id=payload["probe_id"],
            probe_class=payload["probe_class"],
            corpus_scale=payload["corpus_scale"],
            answer=payload["answer"],
            turn_tokens=[TurnTokenUsage(**t) for t in payload.get("turn_tokens", [])],
            tool_calls=[ToolCall(**t) for t in payload.get("tool_calls", [])],
            recall_called=payload.get("recall_called", False),
            injected_context_tokens=payload.get("injected_context_tokens"),
            turn_count=payload.get("turn_count", 0),
            transcript=payload.get("transcript", []),
        )


def _observe_turn(session: EvalSession, model: str, response: Any, turn: int) -> TurnTokenUsage:
    """Record *response* on *session* and return exactly the DELTA it added.

    Reuses ``EvalSession.observe_response`` — the same extraction
    :mod:`tests.evals.harness` uses for every live call — rather than a
    second copy of "where are the token counts on a response object".
    Reading the before/after delta off the session's own running totals
    (instead of reaching into the response a second time) is what makes
    this correct even when *session* already has other turns accumulated
    on it.
    """
    before_in, before_out = session.input_tokens, session.output_tokens
    session.observe_response(model, response)
    return TurnTokenUsage(
        turn=turn,
        input_tokens=session.input_tokens - before_in,
        output_tokens=session.output_tokens - before_out,
    )


def _single_shot(
    *,
    context: str | None,
    probe: Probe,
    client: Any,
    session: EvalSession,
    model: str,
) -> tuple[str, TurnTokenUsage, str]:
    """Send one context+question completion through *client*, observed on
    *session*. Returns ``(answer_text, turn_usage, user_message_text)``."""
    user_text = (
        f"Context:\n{context}\n\nQuestion: {probe.query}" if context else f"Question: {probe.query}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    turn_usage = _observe_turn(session, model, response, turn=1)
    answer = provider_response_text(response)
    return answer, turn_usage, user_text


# ---------------------------------------------------------------------------
# NONE / PUSH_PAGES_UPPER_BOUND / PUSH_BREADCRUMB / ORACLE — single-shot arms
# ---------------------------------------------------------------------------


def run_none(
    probe: Probe, corpus_scale: str, *, client: Any, session: EvalSession, model: str
) -> RolloutRecord:
    """NONE arm: no corpus context at all — the model answers from whatever
    it already knows (which, for a synthetic invented corpus, is nothing)."""
    answer, turn_usage, user_text = _single_shot(
        context=None, probe=probe, client=client, session=session, model=model
    )
    return RolloutRecord(
        arm=Arm.NONE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=[turn_usage],
        tool_calls=[],
        recall_called=False,
        injected_context_tokens=None,
        turn_count=1,
        transcript=[{"system": _SYSTEM_PROMPT, "user": user_text, "answer": answer}],
    )


def run_push_pages_upper_bound(
    probe: Probe,
    corpus_scale: str,
    *,
    wiki_root: Path,
    cache_dir: Path,
    search_backend: str,
    client: Any,
    session: EvalSession,
    model: str,
) -> RolloutRecord:
    """PUSH_PAGES_UPPER_BOUND arm: context is whatever ``recall_search``
    would actually deliver for this probe's query at ``top_k=5`` — the real
    retrieval path, mirrored directly rather than reimplemented
    (``athenaeum.mcp_server.recall_search``, issue athenaeum#1522's explicit
    reuse instruction).

    Named (and renamed from the original bare ``PUSH``) by issue
    athenaeum#1574's operator decision: this five-full-page delivery is NOT
    what the shipped hook injects — the shipped hook delivers at most three
    200-character breadcrumbs (:func:`run_push_breadcrumb`). This arm is
    kept as an explicitly labelled UPPER BOUND ("what if the model always
    got the whole page"), not the production configuration.
    """
    pushed = recall_search(
        wiki_root,
        probe.query,
        top_k=5,
        search_backend=search_backend,
        cache_dir=cache_dir,
    )
    answer, turn_usage, user_text = _single_shot(
        context=pushed, probe=probe, client=client, session=session, model=model
    )
    return RolloutRecord(
        arm=Arm.PUSH_PAGES_UPPER_BOUND,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=[turn_usage],
        tool_calls=[],
        recall_called=False,
        injected_context_tokens=estimate_tokens(pushed),
        turn_count=1,
        transcript=[
            {
                "system": _SYSTEM_PROMPT,
                "pushed_context": pushed,
                "user": user_text,
                "answer": answer,
            }
        ],
    )


#: Repo root, derived the same way ``tests/test_shell_hooks.py``'s
#: ``HOOKS_DIR`` is (``Path(__file__).parent...`` walked up to the repo
#: root) — ``rollout.py`` sits one directory deeper (``tests/evals/`` vs.
#: ``tests/``), hence the extra ``.parent``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _REPO_ROOT / "examples" / "claude-code"
SESSION_START_HOOK = _HOOKS_DIR / "session-start-recall.sh"
USER_PROMPT_HOOK = _HOOKS_DIR / "user-prompt-recall.sh"


def build_breadcrumb_hook_env(
    knowledge_root: Path, hook_home: Path, *, athenaeum_src: Path | None = None
) -> dict[str, str]:
    """Environment for shelling out to the SHIPPED hooks
    (:data:`SESSION_START_HOOK` / :data:`USER_PROMPT_HOOK`), scoped to
    *hook_home* as ``HOME`` so the hooks' own hardcoded
    ``${HOME}/.cache/athenaeum`` index/cache lives in a throwaway directory
    rather than a developer's real one.

    Same isolation shape ``tests/test_shell_hooks.py``'s ``hook_env``
    fixture uses — reused here, not reimplemented (issue athenaeum#1574
    plan step 1: "reusing the hook's SQL/awk contract rather than
    reimplementing it" extends to the harness that invokes it).
    """
    src = athenaeum_src or _REPO_ROOT
    return {
        "HOME": str(hook_home),
        "ATHENAEUM_CACHE_DIR": str(hook_home / ".cache" / "athenaeum"),
        "PATH": os.environ.get("PATH", ""),
        "KNOWLEDGE_ROOT": str(knowledge_root),
        "ATHENAEUM_SRC": str(src),
        "ATHENAEUM_PYTHON": sys.executable,
        # Deliberately a path that cannot exist, so `command -v $ATHENAEUM_CLI`
        # fails deterministically and the hook falls through to its offline
        # regex term extractor — same reasoning as hook_env's own
        # ATHENAEUM_CLI: a rollout arm that must stay a pure retrieval
        # measurement must never shell out to a second live LLM call of its
        # own (the only model call PUSH_BREADCRUMB/PUSH_BREADCRUMB_PULL make
        # is their own single-shot / claude -p turn, already accounted for).
        "ATHENAEUM_CLI": str(hook_home / "no-such-athenaeum-binary"),
    }


def build_push_breadcrumb_context(
    knowledge_root: Path,
    hook_home: Path,
    query: str,
    *,
    session_id: str | None = None,
    athenaeum_src: Path | None = None,
    timeout: float = 30.0,
) -> str:
    """Assemble the breadcrumb PUSH arm's context by ACTUALLY RUNNING the
    shipped hooks against *knowledge_root* — never a Python reimplementation
    of the hook's SQL ranking / awk 200-char clamp / ``LIMIT 3`` (issue
    athenaeum#1574 plan step 1; AC1's byte-equivalence requirement is
    structural here, not merely tested: there is no second code path that
    could drift from the shipped one).

    Runs :data:`SESSION_START_HOOK` under *hook_home* as a throwaway
    ``HOME`` to build the hook's own FTS5 index from *knowledge_root*, then
    runs :data:`USER_PROMPT_HOOK` for *query* against that SAME ``HOME`` and
    returns ``hookSpecificOutput.additionalContext`` verbatim — byte-for-byte
    what a real Claude Code session would receive for the same prompt on the
    same materialized corpus.

    Returns ``""`` (never raises) when the hook itself declines to inject
    anything — no index, a too-short prompt, no FTS match — mirroring the
    hook's own "exit 0, no output" behaviour; the hook never raises either.
    """
    if shutil.which("bash") is None:
        raise RuntimeError("bash not found on PATH (required to run the shipped hooks)")
    env = build_breadcrumb_hook_env(knowledge_root, hook_home, athenaeum_src=athenaeum_src)
    subprocess.run(
        ["bash", str(SESSION_START_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    stdin_payload = json.dumps(
        {"prompt": query, "session_id": session_id or f"rollout-{uuid.uuid4().hex}"}
    )
    result = subprocess.run(
        ["bash", str(USER_PROMPT_HOOK)],
        input=stdin_payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    return str(payload.get("hookSpecificOutput", {}).get("additionalContext", ""))


def run_push_breadcrumb(
    probe: Probe,
    corpus_scale: str,
    *,
    knowledge_root: Path,
    hook_home: Path,
    client: Any,
    session: EvalSession,
    model: str,
    context_fn: Callable[..., str] | None = None,
) -> RolloutRecord:
    """PUSH_BREADCRUMB arm: context is EXACTLY what the shipped
    ``user-prompt-recall.sh`` hook injects for this probe's query — at most
    three 200-character-clamped breadcrumbs, assembled by actually running
    the hook (:func:`build_push_breadcrumb_context`), never a
    reimplementation. This is what "PUSH" means in production (issue
    athenaeum#1574's operator decision); :func:`run_push_pages_upper_bound`
    is the five-full-page arm kept as an explicit upper bound.

    *context_fn* is an injectable seam (defaults to
    :func:`build_push_breadcrumb_context`) taking the same
    ``(knowledge_root, hook_home, query)`` positional shape — lets an
    offline caller (e.g. the grid-dispatch wiring test) substitute a stub
    and avoid the subprocess spawn entirely, the same pattern
    :func:`run_probe_all_arms`'s ``pull_runner`` seam already uses.
    """
    assemble = context_fn or build_push_breadcrumb_context
    breadcrumb = assemble(knowledge_root, hook_home, probe.query)
    answer, turn_usage, user_text = _single_shot(
        context=breadcrumb or None, probe=probe, client=client, session=session, model=model
    )
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=[turn_usage],
        tool_calls=[],
        recall_called=False,
        injected_context_tokens=estimate_tokens(breadcrumb) if breadcrumb else 0,
        turn_count=1,
        transcript=[
            {
                "system": _SYSTEM_PROMPT,
                "pushed_context": breadcrumb,
                "user": user_text,
                "answer": answer,
            }
        ],
    )


def _oracle_context(corpus: Corpus, probe: Probe) -> str:
    """The ground-truth pages for *probe* verbatim — the ORACLE arm's
    "perfect retrieval" upper bound. Empty for an abstention probe (no
    ``expected_uids``), which is the correct oracle context: there is
    nothing a perfect retriever could have found."""
    pages_by_uid = {page.uid: page for page in corpus.pages}
    parts = [
        pages_by_uid[uid].to_markdown() for uid in probe.expected_uids if uid in pages_by_uid
    ]
    return "\n\n---\n\n".join(parts)


def run_oracle(
    probe: Probe,
    corpus: Corpus,
    corpus_scale: str,
    *,
    client: Any,
    session: EvalSession,
    model: str,
) -> RolloutRecord:
    """ORACLE arm: context is the probe's actual ground-truth pages,
    bypassing retrieval entirely — the ceiling a real retriever is measured
    against."""
    context = _oracle_context(corpus, probe)
    answer, turn_usage, user_text = _single_shot(
        context=context or None, probe=probe, client=client, session=session, model=model
    )
    return RolloutRecord(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=[turn_usage],
        tool_calls=[],
        recall_called=False,
        injected_context_tokens=estimate_tokens(context) if context else 0,
        turn_count=1,
        transcript=[
            {
                "system": _SYSTEM_PROMPT,
                "oracle_context": context,
                "user": user_text,
                "answer": answer,
            }
        ],
    )


# ---------------------------------------------------------------------------
# PULL / PUSH_BREADCRUMB_PULL — the real tool-use loops
# ---------------------------------------------------------------------------


def build_pull_mcp_config(
    knowledge_root: Path, cache_dir: Path, *, athenaeum_bin: str | None = None
) -> dict[str, Any]:
    """The scoped ``--mcp-config`` payload: ONE server, athenaeum's own,
    pointed at the materialized rollout corpus. Matches the proven spike
    shape exactly (``{"mcpServers": {"athenaeum": {...}}}``).

    *athenaeum_bin* defaults to the console script alongside the CURRENT
    interpreter (``sys.executable``'s own venv), falling back to whatever
    ``athenaeum`` resolves to on ``PATH`` only when that sibling does not
    exist — never a hardcoded developer-machine path. The venv-sibling is
    tried FIRST and not the other way around: a container can carry an
    unrelated, differently-versioned ``athenaeum`` earlier on ``PATH`` (seen
    in practice — an older global install whose ``serve`` subcommand lacks
    ``--cache-dir`` entirely), and this rollout must exercise the code under
    test's OWN venv, not whatever happens to shadow it on ``PATH``.
    """
    venv_sibling = Path(sys.executable).with_name("athenaeum")
    fallback = str(venv_sibling) if venv_sibling.exists() else shutil.which("athenaeum")
    binary = athenaeum_bin or fallback or "athenaeum"
    return {
        "mcpServers": {
            "athenaeum": {
                "type": "stdio",
                "command": binary,
                "args": ["serve", "--path", str(knowledge_root), "--cache-dir", str(cache_dir)],
                "env": {},
            }
        }
    }


def build_pull_argv(claude_binary: str, mcp_config_path: Path, model: str) -> list[str]:
    """The PULL argv: inverts exactly the two flags
    ``athenaeum.provider.ClaudeCliClient._build_argv`` pins the production
    subprocess with (``--tools ""`` / unscoped ``--strict-mcp-config``) —
    here ``--strict-mcp-config`` STAYS (it is what makes the ``--mcp-config``
    scoping exclusive rather than additive to the ambient
    ``~/.claude.json`` server list), and ``--tools ""`` is simply never
    added, so the model's own built-in tool set plus the one scoped MCP
    server are both available. ``--output-format stream-json --verbose`` is
    what makes the tool-choice decision observable.
    """
    return [
        claude_binary,
        "-p",
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
    ]


@dataclasses.dataclass(frozen=True)
class ParsedPullStream:
    """Everything :func:`parse_pull_stream` extracts from a raw
    ``stream-json`` transcript."""

    mcp_connected: bool
    recall_tool_available: bool
    tool_calls: list[ToolCall]
    recall_called: bool
    turn_tokens: list[TurnTokenUsage]
    answer: str
    turn_count: int
    transcript: list[dict[str, Any]]


def parse_pull_stream(lines: Iterable[str]) -> ParsedPullStream:
    """Parse a ``claude -p --output-format stream-json`` transcript.

    Offline and dependency-free — this is the function
    ``tests/evals/test_rollout.py``'s recorded-fixture tests exercise
    without ever spawning a subprocess. Never raises on a transcript with
    no tool calls: an agent that legitimately chose not to call recall
    parses to ``recall_called=False``, the same as any other transcript —
    issue athenaeum#1522's "not calling recall is a recorded outcome, never
    an error" acceptance criterion.
    """
    events: list[dict[str, Any]] = []
    mcp_connected = False
    recall_tool_available = False
    tool_calls: list[ToolCall] = []
    turn_tokens: list[TurnTokenUsage] = []
    answer = ""
    turn = 0

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        event = json.loads(raw_line)
        events.append(event)
        event_type = event.get("type")

        if event_type == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers") or []
            mcp_connected = any(
                isinstance(server, dict)
                and server.get("name") == "athenaeum"
                and server.get("status") == "connected"
                for server in servers
            )
            tools = event.get("tools") or []
            recall_tool_available = RECALL_TOOL_NAME in tools

        elif event_type == "assistant":
            turn += 1
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            turn_tokens.append(
                TurnTokenUsage(
                    turn=turn,
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                )
            )
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == RECALL_TOOL_NAME:
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict):
                        query = str(tool_input.get("query", tool_input))
                    else:
                        query = str(tool_input)
                    tool_calls.append(ToolCall(name=RECALL_TOOL_NAME, query=query))
                elif block.get("type") == "text" and block.get("text"):
                    answer = str(block["text"])

        elif event_type == "result" and not answer:
            answer = str(event.get("result") or "")

    return ParsedPullStream(
        mcp_connected=mcp_connected,
        recall_tool_available=recall_tool_available,
        tool_calls=tool_calls,
        recall_called=bool(tool_calls),
        turn_tokens=turn_tokens,
        answer=answer,
        turn_count=turn,
        transcript=events,
    )


def run_pull(
    probe: Probe,
    knowledge_root: Path,
    cache_dir: Path,
    corpus_scale: str,
    *,
    claude_binary: str = "claude",
    model: str = DEFAULT_ROLLOUT_MODEL,
    timeout: float = 120.0,
    athenaeum_bin: str | None = None,
) -> RolloutRecord:
    """PULL arm: spawn ``claude -p`` with a scoped MCP config and let the
    model decide whether to call recall. Raises only on a genuine spawn
    failure (binary missing, timeout, non-JSON stdout) — choosing not to
    call recall is never one of those (see :func:`parse_pull_stream`).
    """
    if shutil.which(claude_binary) is None:
        raise RuntimeError(f"{claude_binary!r} not found on PATH")

    mcp_config = build_pull_mcp_config(knowledge_root, cache_dir, athenaeum_bin=athenaeum_bin)
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "mcp-config.json"
        config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
        argv = build_pull_argv(claude_binary, config_path, model)
        # Issue athenaeum#543 (L4) discipline, mirrored from
        # ``ClaudeCliClient._build_argv``: the prompt goes on stdin, never
        # as a positional argv element.
        proc = subprocess.run(
            argv,
            input=probe.query,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    parsed = parse_pull_stream((proc.stdout or "").splitlines())
    return RolloutRecord(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=parsed.answer,
        turn_tokens=parsed.turn_tokens,
        tool_calls=parsed.tool_calls,
        recall_called=parsed.recall_called,
        injected_context_tokens=None,
        turn_count=parsed.turn_count,
        transcript=parsed.transcript,
    )


def run_push_breadcrumb_pull(
    probe: Probe,
    knowledge_root: Path,
    hook_home: Path,
    cache_dir: Path,
    corpus_scale: str,
    *,
    claude_binary: str = "claude",
    model: str = DEFAULT_ROLLOUT_MODEL,
    timeout: float = 120.0,
    athenaeum_bin: str | None = None,
    context_fn: Callable[..., str] | None = None,
) -> RolloutRecord:
    """PUSH_BREADCRUMB_PULL arm: the breadcrumb context IS injected AND the
    ``recall`` tool remains available — matching the shipped hook's own
    design (inject a breadcrumb, expect the agent to PULL the full page
    when it looks useful; see the hook's own
    ``(use \\`recall\\` MCP tool for full details)`` wording). Reuses
    :func:`run_pull`'s ``claude -p`` loop verbatim (same MCP config, same
    argv, same stream parser); the only difference is the breadcrumb is
    prepended to the stdin prompt rather than the prompt being sent bare.

    ``recall_called`` is recorded exactly like PULL (issue athenaeum#1574
    AC2) — choosing not to pull further after seeing the breadcrumb is a
    legitimate, recorded outcome, never an error.
    """
    assemble = context_fn or build_push_breadcrumb_context
    breadcrumb = assemble(knowledge_root, hook_home, probe.query)

    if shutil.which(claude_binary) is None:
        raise RuntimeError(f"{claude_binary!r} not found on PATH")

    mcp_config = build_pull_mcp_config(knowledge_root, cache_dir, athenaeum_bin=athenaeum_bin)
    prompt_text = f"{breadcrumb}\n\n{probe.query}" if breadcrumb else probe.query
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "mcp-config.json"
        config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
        argv = build_pull_argv(claude_binary, config_path, model)
        proc = subprocess.run(
            argv,
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    parsed = parse_pull_stream((proc.stdout or "").splitlines())
    # The breadcrumb is recorded as transcript[0] — the SAME
    # ``{"pushed_context": ...}`` shape :func:`run_push_breadcrumb` uses, so
    # ``tests.evals.north_star_report._push_delivered_text`` (which reads
    # ``transcript[0]["pushed_context"]``) needs no PUSH_BREADCRUMB_PULL-
    # specific branch. The real stream-json events follow it; ``_pull_delivered_text``
    # scans the whole list for ``type == "user"`` entries, so the leading
    # dict (which has no ``type`` key) is simply skipped by that scan.
    transcript = [{"pushed_context": breadcrumb}, *parsed.transcript]
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=parsed.answer,
        turn_tokens=parsed.turn_tokens,
        tool_calls=parsed.tool_calls,
        recall_called=parsed.recall_called,
        injected_context_tokens=estimate_tokens(breadcrumb) if breadcrumb else 0,
        turn_count=parsed.turn_count,
        transcript=transcript,
    )


# ---------------------------------------------------------------------------
# Runner entrypoint — one probe, all six arms
# ---------------------------------------------------------------------------


def _find_probe(corpus: Corpus, probe_id: str) -> Probe:
    for probe in corpus.probes:
        if probe.id == probe_id:
            return probe
    raise KeyError(f"probe {probe_id!r} not found in corpus (scale={corpus.scale!r})")


def run_probe_all_arms(
    probe_id: str,
    corpus_scale: str,
    *,
    session: EvalSession,
    materialize_root: Path,
    model: str = DEFAULT_ROLLOUT_MODEL,
    search_backend: str = "fts5",
    claude_binary: str = "claude",
    replicate: int = 0,
    client: Any | None = None,
    pull_runner: Callable[..., RolloutRecord] | None = None,
    breadcrumb_context_fn: Callable[..., str] | None = None,
    breadcrumb_pull_runner: Callable[..., RolloutRecord] | None = None,
) -> dict[str, RolloutRecord]:
    """Run ONE probe across all six arms against a materialized corpus at
    *corpus_scale* (issue athenaeum#1522 AC2/AC3/AC4; issue athenaeum#1574
    added the two breadcrumb arms).

    Builds exactly one :class:`~tests.evals.containment.GridCell` per arm via
    ``containment.build_grid("full", ...)`` — "full" is uncapped on the arms
    axis, so passing all six ``Arm`` values with a single-element probe/
    corpus-scale/replicate list yields exactly ``len(ALL_ARMS)`` cells,
    reusing the SAME grid machinery a future multi-probe sweep would use
    rather than a bespoke loop.

    *client* and *pull_runner* are injectable seams (default to a real live
    client / :func:`run_pull`); *breadcrumb_context_fn* and
    *breadcrumb_pull_runner* are the SAME kind of seam for the two
    breadcrumb arms (default to :func:`build_push_breadcrumb_context` /
    :func:`run_push_breadcrumb_pull`) — so a caller, including the offline
    test suite, can supply stubs and exercise the arm-dispatch wiring
    without a network call or a subprocess spawn.
    """
    corpus = build_corpus(corpus_scale)
    probe = _find_probe(corpus, probe_id)
    wiki_root = corpus.materialize(materialize_root)
    cache_dir = materialize_root / "cache"
    hook_home = materialize_root / "hook_home"
    if search_backend != "keyword":
        get_backend(search_backend).build_index(wiki_root, cache_dir)

    cells: list[GridCell] = build_grid(
        "full",
        probes=[probe_id],
        arms=[arm.value for arm in ALL_ARMS],
        corpus_scales=[corpus_scale],
        replicates=[replicate],
    )

    resolved_client = client if client is not None else build_live_client()
    resolved_pull_runner = pull_runner if pull_runner is not None else run_pull
    resolved_breadcrumb_pull_runner = breadcrumb_pull_runner or run_push_breadcrumb_pull

    records: dict[str, RolloutRecord] = {}
    for cell in cells:
        arm = Arm(cell.arm)
        if arm is Arm.NONE:
            record = run_none(
                probe, corpus_scale, client=resolved_client, session=session, model=model
            )
        elif arm is Arm.PUSH_PAGES_UPPER_BOUND:
            record = run_push_pages_upper_bound(
                probe,
                corpus_scale,
                wiki_root=wiki_root,
                cache_dir=cache_dir,
                search_backend=search_backend,
                client=resolved_client,
                session=session,
                model=model,
            )
        elif arm is Arm.PUSH_BREADCRUMB:
            record = run_push_breadcrumb(
                probe,
                corpus_scale,
                knowledge_root=materialize_root,
                hook_home=hook_home,
                client=resolved_client,
                session=session,
                model=model,
                context_fn=breadcrumb_context_fn,
            )
        elif arm is Arm.ORACLE:
            record = run_oracle(
                probe, corpus, corpus_scale, client=resolved_client, session=session, model=model
            )
        elif arm is Arm.PUSH_BREADCRUMB_PULL:
            # Takes ``materialize_root`` (the KNOWLEDGE root), same as PULL
            # — see the PULL branch's own comment below for why that must
            # NOT be ``wiki_root``.
            record = resolved_breadcrumb_pull_runner(
                probe,
                materialize_root,
                hook_home,
                cache_dir,
                corpus_scale,
                claude_binary=claude_binary,
                model=model,
                context_fn=breadcrumb_context_fn,
            )
        else:
            # PULL gets ``materialize_root``, NOT ``wiki_root``, and the two
            # arms differing here is deliberate rather than a slip:
            # PUSH_PAGES_UPPER_BOUND calls ``recall_search(wiki_root, ...)``,
            # which takes the WIKI root directly, while PULL drives
            # ``athenaeum serve --path``, which takes the KNOWLEDGE root and
            # derives ``<path>/wiki`` and ``<path>/raw`` from it itself (see
            # ``_cmd_serve``'s ``--path`` help and ``_resolve_serve_roots``).
            # Handing ``serve`` the wiki root would make it look for
            # ``<materialize_root>/wiki/wiki`` and serve an empty corpus.
            # Pinned by
            # ``test_rollout.py::test_pull_arm_receives_the_knowledge_root_not_the_wiki_root``.
            record = resolved_pull_runner(
                probe,
                materialize_root,
                cache_dir,
                corpus_scale,
                claude_binary=claude_binary,
                model=model,
            )
        records[arm.value] = record
    return records
